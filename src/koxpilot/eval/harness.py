"""评测 harness（SPEC 第 7 节）：把六张表 + 两本账拼成 ``output/metrics.json``。

设计要点
--------
1. **落盘产物不含时间戳**：metrics.json 要能"同一份代码 + 同一份数据 -> 逐字节相同"，
   带上生成时间就再也无法做回归对比了。版本可追溯靠数据集 sha256 + 各模块版本号。
2. **表 2 附口径对照**：SPEC 3.3 的 gt 合成规则**没有**把"多源标签冲突"计入 review，
   而 SPEC 4.G2.2 明确要求口径冲突需人核。这是 SPEC 自身的两处不一致。
   本实现的处理是：**保留 G2.2 的 review 判定**（产品上确实该人核），
   同时输出"屏蔽 G2.2 后的三分类矩阵"作为口径对照，并在 review 假阳性归因表里
   把这部分单列成"口径性假阳性"。不藏、不改 gt、不悄悄关规则。
3. **归因表**：每个不一致格子都给出"是哪几条规则造成的"，让弱项可被定位而不只是被承认。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..budget.planner import plan_baseline, plan_campaign, plan_diversified_no_gate
from ..budget.policy import constraint_snapshot
from ..datagen.config import DATASET_VERSION, SEED
from ..gates.engine import evaluate_all
from ..gates.policy import ALL_GATES, policy_snapshot
from ..gates.thresholds import THRESHOLDS_VERSION, Thresholds, calibrate
from ..io_utils import data_dir, file_sha256, load_json, output_dir
from ..llm.identity import IDENTITY_SCHEMA_LEGACY, model_display_map
from ..types import BudgetPlan, CampaignSpec, GateResult, Kox
from .ablation import ablation_report
from .audit import cost_audit_report, counterfactual_report
from .llm_compare import llm_vs_rule_report
from .llm_fit import llm_fit_audit
from .metrics import fraud_report, gt_of, verdict_report
from .sensitivity import sensitivity_report
from .strata import strata_report

__all__ = [
    "EVAL_VERSION",
    "EvalContext",
    "budget_section",
    "build_context",
    "review_fp_attribution",
    "reject_fp_attribution",
    "run_full_eval",
]

EVAL_VERSION = "1.0.0"

#: 归因表里最多列出多少种规则组合（长尾组合只贡献个位数，全列出反而看不清主因）
ATTRIBUTION_TOP_N = 8


@dataclass(slots=True)
class EvalContext:
    """一次评测所需的全部输入，跑完一次门禁后被各张表共享（避免重复计算）。"""

    records: list[Kox]
    dataset_meta: dict[str, Any]
    thresholds: Thresholds
    results: dict[str, GateResult]
    briefs: list[dict[str, Any]] = field(default_factory=list)
    dataset_sha256: str = ""

    @property
    def specs(self) -> list[CampaignSpec]:
        return [CampaignSpec.from_dict(b["spec"]) for b in self.briefs]


def build_context(
    records: Sequence[Kox],
    dataset_meta: Mapping[str, Any] | None = None,
    briefs: Sequence[Mapping[str, Any]] = (),
    thresholds: Thresholds | None = None,
    dataset_sha256: str = "",
) -> EvalContext:
    """标定阈值并跑一遍**库级中性口径**门禁（campaign 无关，与 gt.verdict 对齐）。"""
    recs = list(records)
    thr = thresholds if thresholds is not None else calibrate(recs, dataset_meta)
    results = {r.kox_id: r for r in evaluate_all(recs, thresholds=thr, enabled_gates=ALL_GATES)}
    return EvalContext(
        records=recs,
        dataset_meta=dict(dataset_meta or {}),
        thresholds=thr,
        results=results,
        briefs=[dict(b) for b in briefs],
        dataset_sha256=dataset_sha256,
    )


def _rule_signature(res: GateResult) -> str:
    ids = sorted({r.rule_id for r in res.reasons})
    return "+".join(ids) if ids else "(completeness<0.8 only)"


def _attribution(
    records: Sequence[Kox],
    results: Mapping[str, GateResult],
    gt_verdict: str,
    pred_verdict: str,
) -> dict[str, Any]:
    """某个混淆格子的规则归因：哪些规则组合把这批人推到了这一档。"""
    counts: dict[str, int] = {}
    rule_hits: dict[str, int] = {}
    total = 0
    for kox in records:
        res = results.get(str(kox.get("kox_id")))
        if res is None or res.verdict != pred_verdict:
            continue
        if str(gt_of(kox).get("verdict")) != gt_verdict:
            continue
        total += 1
        sig = _rule_signature(res)
        counts[sig] = counts.get(sig, 0) + 1
        for rid in {r.rule_id for r in res.reasons}:
            rule_hits[rid] = rule_hits.get(rid, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:ATTRIBUTION_TOP_N]
    return {
        "cell": f"gt={gt_verdict} -> pred={pred_verdict}",
        "n": total,
        "top_rule_signatures": [
            {"rules": sig, "n": n, "share": round(n / total, 4) if total else 0.0}
            for sig, n in top
        ],
        "rule_involvement": dict(sorted(rule_hits.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def review_fp_attribution(ctx: EvalContext) -> dict[str, Any]:
    """gt=pass 却被判 review 的归因，并把"口径性假阳性"单独量化。"""
    base = _attribution(ctx.records, ctx.results, "pass", "review")
    solely_g2_2 = 0
    involving_g2_2 = 0
    for kox in ctx.records:
        res = ctx.results.get(str(kox.get("kox_id")))
        if res is None or res.verdict != "review":
            continue
        if str(gt_of(kox).get("verdict")) != "pass":
            continue
        ids = {r.rule_id for r in res.reasons}
        if "G2.2" in ids:
            involving_g2_2 += 1
            if ids == {"G2.2"}:
                solely_g2_2 += 1
    n = int(base["n"])
    base.update(
        {
            "solely_caused_by_G2_2": solely_g2_2,
            "involving_G2_2": involving_g2_2,
            "solely_G2_2_share": round(solely_g2_2 / n, 4) if n else 0.0,
            "calibration_note": (
                "SPEC 3.3 合成 gt.verdict 时未把「多源标签冲突」计入 review，"
                "而 SPEC 4.G2.2 要求口径冲突需人核 —— 两处口径不一致。"
                f"因此这 {n} 个假阳性里有 {solely_g2_2} 个是**纯口径性**的"
                "（唯一命中就是 G2.2），产品上它们确实该进人核队列。"
                "本实现选择保留 G2.2 判定，并在 variant_rule_disabled_G2_2 里给出对照矩阵。"
            ),
        }
    )
    return base


def reject_fp_attribution(ctx: EvalContext) -> dict[str, Any]:
    """gt=pass 却被判 reject 的归因（误杀是最贵的错误，必须能定位到规则）。"""
    return _attribution(ctx.records, ctx.results, "pass", "reject")


def _missed_fraud_profile(ctx: EvalContext) -> dict[str, Any]:
    """漏掉的水号长什么样（按类型 + 强弱偏移分布），用于解释召回缺口。"""
    from .metrics import fraud_pred_strict

    missed: dict[str, int] = {}
    caught: dict[str, int] = {}
    for kox in ctx.records:
        gt = gt_of(kox)
        if not gt.get("is_fraud"):
            continue
        res = ctx.results.get(str(kox.get("kox_id")))
        if res is None:
            continue
        ftype = str(gt.get("fraud_type"))
        if fraud_pred_strict(res):
            caught[ftype] = caught.get(ftype, 0) + 1
        else:
            missed[ftype] = missed.get(ftype, 0) + 1
    rows = []
    for ftype in sorted(set(missed) | set(caught)):
        m, c = missed.get(ftype, 0), caught.get(ftype, 0)
        rows.append(
            {
                "fraud_type": ftype,
                "caught": c,
                "missed": m,
                "miss_rate": round(m / (m + c), 4) if (m + c) else 0.0,
            }
        )
    rows.sort(key=lambda r: -float(r["miss_rate"]))
    return {
        "by_fraud_type": rows,
        "note": (
            "漏检主要来自 weak 偏移样本：datagen 刻意让一部分问题号的偏移量落在正常区间内"
            "（STRONG_SHARE < 1），这是为了避免评测自证——如果所有水号都被造成极端值，"
            "召回率会虚高到没有参考意义。"
        ),
    }


def budget_section(
    ctx: EvalContext,
) -> tuple[dict[str, Any], dict[str, BudgetPlan], dict[str, BudgetPlan], dict[str, BudgetPlan]]:
    """对每个 brief 跑三条臂：KOXPilot / 基线（粉丝量）/ 第三臂（只分散化、不门禁）。

    这是"多臂预算 + 复用同一批 GateResult"的**唯一正确入口**：三臂共用 `plan_campaign`
    产出的 `results`，所以三边看到的门禁判定完全一致，差异只来自候选池与选人依据。
    它曾经叫 `_budget_section`（私有），而 `eval/multiseed.py` 不得不 import 一个下划线符号；
    私有名字会推着下一个复用者自己重拼一遍编排——那才是口径漂移的真正来源，所以这里转公开。

    返回 ``(表格行, koxpilot 方案, 基线方案, 第三臂方案)``。
    """
    plans: dict[str, BudgetPlan] = {}
    baselines: dict[str, BudgetPlan] = {}
    diversified: dict[str, BudgetPlan] = {}
    rows: list[dict[str, Any]] = []
    for spec in ctx.specs:
        plan, results = plan_campaign(ctx.records, spec, ctx.thresholds)
        base = plan_baseline(ctx.records, spec, ctx.thresholds, results)
        div = plan_diversified_no_gate(ctx.records, spec, ctx.thresholds, results)
        plans[spec.campaign_id] = plan
        baselines[spec.campaign_id] = base
        diversified[spec.campaign_id] = div
        rows.append(
            {
                "campaign_id": spec.campaign_id,
                "name": spec.name,
                "kpi": spec.kpi,
                "budget_usd": spec.budget_usd,
                "candidate_pool": plan.candidate_pool,
                "n_selected": len(plan.selected),
                "spent_usd": round(plan.spent_usd, 2),
                "utilization": round(plan.spent_usd / plan.budget_usd, 4)
                if plan.budget_usd
                else 0.0,
                "est_cpm_usd": round(plan.est_cpm_usd, 3),
                "est_cpe_usd": round(plan.est_cpe_usd, 4),
                "tier_mix": {k: round(v, 4) for k, v in plan.tier_mix.items()},
                "country_mix": {k: round(v, 4) for k, v in plan.country_mix.items()},
                "n_price_estimated": plan.n_price_estimated,
                "constraints_ok": plan.constraints.get("all_enforced_satisfied"),
                "trace": plan.trace,
                # 第三臂的落地情况（结构约束是否真的被执行到位），只放摘要不放全量 trace
                "diversified_no_gate": {
                    "candidate_pool": div.candidate_pool,
                    "n_selected": len(div.selected),
                    "spent_usd": round(div.spent_usd, 2),
                    "utilization": round(div.spent_usd / div.budget_usd, 4)
                    if div.budget_usd
                    else 0.0,
                    "tier_mix": {k: round(v, 4) for k, v in div.tier_mix.items()},
                    "country_mix": {k: round(v, 4) for k, v in div.country_mix.items()},
                    "constraints_ok": div.constraints.get("all_enforced_satisfied"),
                    "violations": div.constraints.get("violations"),
                },
            }
        )
    return (
        {"constraint_policy": constraint_snapshot(), "per_campaign": rows},
        plans,
        baselines,
        diversified,
    )


def run_full_eval(
    ctx: EvalContext,
    bench_path: Path | str | None = None,
    llm_cache_path: Path | str | None = None,
) -> dict[str, Any]:
    """跑全部六张表 + 成本账 + 反事实价值账，返回 metrics.json 的完整 payload。"""
    bench = Path(bench_path) if bench_path is not None else output_dir() / "llm_bench.json"
    cache_default = output_dir() / "llm_cache.json"
    llm_cache = Path(llm_cache_path) if llm_cache_path is not None else cache_default

    table1 = fraud_report(ctx.records, ctx.results)
    table2 = verdict_report(ctx.records, ctx.results)
    table3 = strata_report(ctx.records, ctx.results)

    # 口径对照：屏蔽 G2.2 后重跑（分数由 engine 从 reason 合成，因此屏蔽是真屏蔽）
    results_no_g2_2 = {
        r.kox_id: r
        for r in evaluate_all(
            ctx.records, thresholds=ctx.thresholds, enabled_gates=ALL_GATES, disabled_rules=("G2.2",)
        )
    }
    table2_variant = verdict_report(ctx.records, results_no_g2_2)

    baseline_metrics = {
        "fraud_f1_strict": float(table1["strict"]["f1"]),
        "fraud_precision_strict": float(table1["strict"]["precision"]),
        "fraud_recall_strict": float(table1["strict"]["recall"]),
        "fraud_auc": float(table1["auc"]),
        "verdict_accuracy": float(table2["accuracy"]),
        "verdict_macro_f1": float(table2["macro_f1"]),
        "n_reject": sum(1 for r in ctx.results.values() if r.verdict == "reject"),
        "n_review": sum(1 for r in ctx.results.values() if r.verdict == "review"),
        "n_pass": sum(1 for r in ctx.results.values() if r.verdict == "pass"),
    }
    table4 = ablation_report(ctx.records, CampaignSpec(), ctx.thresholds, baseline_metrics)
    table5 = sensitivity_report(
        ctx.records,
        CampaignSpec(),
        ctx.thresholds,
        {
            "fraud_precision_strict": baseline_metrics["fraud_precision_strict"],
            "fraud_recall_strict": baseline_metrics["fraud_recall_strict"],
            "fraud_f1_strict": baseline_metrics["fraud_f1_strict"],
            "fraud_auc": baseline_metrics["fraud_auc"],
            "verdict_accuracy": baseline_metrics["verdict_accuracy"],
            "n_reject": float(baseline_metrics["n_reject"]),
        },
    )
    table6 = llm_vs_rule_report(
        ctx.records, ctx.results, CampaignSpec(), bench, campaign_specs=ctx.specs
    )
    # A4 落差的量化：LLM 适配分 vs 规则版 + "注入正式链路"的离线反事实。
    # 放在表 6 里是因为它就是"LLM vs 规则"的第二格；**它只读不写**，
    # 正式链路的 fit_score 仍然是规则版，metrics.json 的逐字节可复现性不受影响。
    # model_display 从表 6 归一好的 model_identity 里取，保证整份 metrics 只有一套模型口径。
    table6["semantic_fit_llm_vs_rule"] = llm_fit_audit(
        ctx.records,
        ctx.specs,
        ctx.thresholds,
        llm_cache,
        model_display=model_display_map(table6.get("model_identity") or {}),
    )

    budget_rows, plans, baselines, diversified = budget_section(ctx)
    counterfactual = counterfactual_report(ctx.records, plans, baselines, diversified)
    rule_f1 = None
    llm_f1 = None
    if table6.get("status") == "ok":
        same = table6.get("rule_arm_same_sample") or {}
        rule_f1 = same.get("f1")
        best = table6.get("best_llm_model")
        if best:
            llm_f1 = (table6.get("llm_arm") or {}).get(best, {}).get("f1")
    else:
        rule_f1 = (table6.get("rule_arm") or {}).get("tag_mismatch", {}).get("f1")
    cost = cost_audit_report(plans, bench, rule_f1, llm_f1)

    weak_spots = list(table3["weak_spots"])
    honesty: list[str] = [
        f"水号识别严口径 F1 = {table1['strict']['f1']:.4f}（精确率 {table1['strict']['precision']:.4f}/"
        f"召回率 {table1['strict']['recall']:.4f}），AUC = {table1['auc']:.4f}；"
        "召回缺口主要来自刻意保留的 weak 偏移样本，不做美化。",
        review_fp_attribution(ctx)["calibration_note"],
        f"三分类整体准确率 {table2['accuracy']:.4f}、macro F1 {table2['macro_f1']:.4f}；"
        f"屏蔽 G2.2 的口径对照版为 {table2_variant['accuracy']:.4f} / {table2_variant['macro_f1']:.4f}。",
        table5["note"],
        table4["note"],
    ]
    if table6.get("status") != "ok":
        honesty.append(str(table6.get("explanation")))
    fit_audit = table6["semantic_fit_llm_vs_rule"]
    if fit_audit.get("status") == "ok":
        honesty.append(
            "A4 语义适配分：正式链路用的是规则版，不是 LLM 版。"
            + str(fit_audit["headline"])
            + "（三条不予升格的判据见 table_6_llm_vs_rule.semantic_fit_llm_vs_rule.blockers）"
        )
    else:
        honesty.append(
            "A4 语义适配分对照未跑：" + str(fit_audit.get("note")) + " 正式链路走规则版。"
        )
    identity = table6.get("model_identity") or {}
    if identity.get("schema") == IDENTITY_SCHEMA_LEGACY:
        # 已提交的 llm_bench.json 是"统一模型标识"之前的产物（重跑要真调 LLM 花 token，
        # 本次没跑）。读侧已把它归一成同一套口径，但产物本身仍是旧形状——
        # 这件事必须写在诚实性清单里，而不是让读者以为产物天生就是统一的。
        legacy = [
            f"{k}: 请求 id={v.get('requested_model_id')} / 服务端回报={v.get('served_models')}"
            for k, v in sorted((identity.get("by_model_key") or {}).items())
            if v.get("requested_id_equals_served") is False
        ]
        honesty.append(
            "模型标识口径：已提交的 output/llm_bench.json 生成于口径统一之前"
            "（其 models 字段存的是请求用的 id，per_task 里存的是服务端回报的型号）。"
            "本文件里 models 一律取服务端回报的型号，请求 id 保留在 model_identity。"
            + ("两者不同的模型：" + "；".join(legacy) + "。" if legacy else "")
            + "重跑 `make llm` 后产物自身即为统一口径（model_identity.schema=model_identity_v2）。"
        )
    attribution = counterfactual.get("value_attribution")
    if attribution:
        honesty.append(
            "三臂价值归因（基线 → 只分散化不门禁 → KOXPilot）："
            + str(attribution["headline"])
            + "；两段贡献按链式差分定义，可加，负值照实写。"
        )

    return {
        "meta": {
            "eval_version": EVAL_VERSION,
            "thresholds_version": THRESHOLDS_VERSION,
            "dataset_version": ctx.dataset_meta.get("dataset_version", DATASET_VERSION),
            "dataset_seed": ctx.dataset_meta.get("seed", SEED),
            "dataset_n": len(ctx.records),
            "dataset_sha256": ctx.dataset_sha256,
            "no_timestamp_note": (
                "本文件刻意不含生成时间：同一份代码 + 同一份数据必须产出逐字节相同的 metrics.json，"
                "这样任何指标变化都可归因到代码或数据的变化。"
            ),
        },
        "definitions": {
            **table1["definitions"],
            "verdict_gt": "gt.verdict 为 campaign 无关的库级判定，故门禁按中性画像口径评测",
            "anti_self_proof": (
                "门禁入口物理剥离 gt 字段；datagen 只注入可观测信号的统计偏移；"
                "审计一律以 gt 为裁判，不用引擎自身分数。"
            ),
        },
        "gate_policy": policy_snapshot(),
        "table_1_fraud_detection": table1,
        "table_2_verdict_confusion": {
            **table2,
            "variant_rule_disabled_G2_2": table2_variant,
            "review_false_positive_attribution": review_fp_attribution(ctx),
            "reject_false_positive_attribution": reject_fp_attribution(ctx),
            "missed_fraud_profile": _missed_fraud_profile(ctx),
        },
        "table_3_strata": table3,
        "table_4_ablation": table4,
        "table_5_sensitivity": table5,
        "table_6_llm_vs_rule": table6,
        "budget": budget_rows,
        "counterfactual_value_audit": counterfactual,
        "cost_audit": cost,
        "weak_spots": weak_spots,
        "honesty_notes": honesty,
    }


def load_eval_inputs() -> tuple[list[Kox], dict[str, Any], list[dict[str, Any]], str]:
    """从 ``data/`` 读入达人库与 brief（供 CLI 使用）。"""
    kox_path = data_dir() / "kox_5000.json"
    briefs_path = data_dir() / "briefs.json"
    if not kox_path.exists() or not briefs_path.exists():
        raise FileNotFoundError(f"缺少 {kox_path} 或 {briefs_path}，请先跑 `make data`")
    dataset = load_json(kox_path)
    briefs_payload = load_json(briefs_path)
    return (
        list(dataset["kox"]),
        dict(dataset.get("meta") or {}),
        list(briefs_payload["briefs"]),
        file_sha256(kox_path),
    )
