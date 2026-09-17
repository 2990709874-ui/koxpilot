"""表 6 附：A4 的 **LLM 语义适配分 vs 规则版**，以及"把 LLM 分注入正式链路"的离线反事实。

为什么要有这个模块
------------------
``output/llm_cache.json`` 里有真实 LLM 打出来的适配分（每 brief 260 条），而
``make budget`` / ``make eval`` 的正式链路走的是 ``g2.rule_fit_score``。
这个落差此前只在文档里"承认"了一句，**没有任何数字**——"我们有 LLM 适配分，只是没接进去"
是一句无法被检验的话：接进去会更好还是更差？覆盖率够不够？两套分是不是同一个口径？
一个都答不上来，现场被追问就只能沉默。

本模块把这句话换成三组现算的数字，并把"要不要升格为正式口径"落成**由数据判定**的结论
（见 :data:`COVERAGE_MIN_FOR_PROMOTION` / :data:`DOUBLE_COUNT_MAX_FOR_PROMOTION`），
而不是写死一句"我们决定不接"：

1. **覆盖率**：LLM 只给采样池打了分，落到每个 campaign 真实候选池上还剩多少比例。
   覆盖率不足时注入 = 一部分人走 LLM 口径、另一部分走规则口径的**混合尺子**，
   由此产出的预算差异主要反映"谁碰巧在采样池里"，不反映选人质量。
2. **单向偏置**：被覆盖到的那批候选，两套分的方向分布。如果 LLM 分**只会更低不会更高**
   （采样池偏向"品类正中"的号，规则版给满分），注入就等于对一个非随机子集单向罚分。
3. **口径重叠（双算）**：LLM 打低分的理由里，有多少其实是"受众不在目标市场 / 语言不符 /
   平台不符 / 人群结构对不上"——这四件事在正式链路里已分别由**定向筛选**
   （``value.targeting_reason``）、**G2.4 语言**、**``audience_match_score`` 乘子**各扣一次。
   注入 G2.3 会让同一个事实被扣两次分。

反事实那部分是真跑的：用缓存里的 LLM 分注入 :func:`evaluate_all`（``fit_source`` 会如实
标成 ``injected:llm_fit_score``），重跑门禁与预算分配，再用 ``gt`` 审计两套方案的浪费。
**不猜、不估**：缓存缺失就报 ``status="llm_fit_not_run"`` 并给出复跑命令。

本模块**只读不写**：它不修改正式链路的任何判定，正式指标仍然逐字节可复现。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..budget.planner import gate_results_for, plan_campaign
from ..budget.value import build_candidates, targeting_reason
from ..gates.g2 import audience_match_score, rule_fit_score
from ..gates.policy import FIT_SCORE_REVIEW_MAX
from ..gates.thresholds import Thresholds
from ..io_utils import load_json, output_dir
from ..llm.identity import model_display_map
from ..stats import mean
from ..types import CampaignSpec, GateResult, Kox
from .audit import plan_audit
from .metrics import gt_of

__all__ = [
    "COVERAGE_MIN_FOR_PROMOTION",
    "DOUBLE_COUNT_MAX_FOR_PROMOTION",
    "FIT_INJECTION_DECISION_KEEP_RULE",
    "FIT_INJECTION_DECISION_PROMOTABLE",
    "RERUN_HINT",
    "llm_fit_audit",
    "load_llm_fit_scores",
]

#: 覆盖率低于这个值就不许升格为正式口径：混合尺子产出的差异无法归因到选人质量。
COVERAGE_MIN_FOR_PROMOTION = 0.90

#: "LLM 低分但规则高分"的人里，已被定向/语言/人群三项各自处理掉的比例上限。
#: 超过这个值说明 LLM 分与既有确定性口径大面积重叠，注入 G2.3 等于同一事实扣两次。
DOUBLE_COUNT_MAX_FOR_PROMOTION = 0.20

FIT_INJECTION_DECISION_KEEP_RULE = "keep_rule_fit_in_formal_chain"
FIT_INJECTION_DECISION_PROMOTABLE = "llm_fit_promotable"

RERUN_HINT = "python -m koxpilot.llm.runner --tasks fit（需配置 endpoint，会真调 LLM 花 token）"

#: 判定"人群结构对不上"的阈值，与 G2.3 的 review 门槛取同一档，避免又造一个口径。
AUDIENCE_MATCH_LOW = FIT_SCORE_REVIEW_MAX


def load_llm_fit_scores(
    cache_path: Path | str | None = None,
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    """从 ``llm_cache.json`` 读出 ``brief_id -> {kox_id: fit_score}`` 与 LLM 元信息。

    缓存不存在 / 没有 ``fit_scores`` 段时返回 ``({}, meta)``——由调用方决定怎么报，
    本函数不造任何默认分（造默认分就等于伪造 LLM 结果）。
    """
    path = Path(cache_path) if cache_path is not None else output_dir() / "llm_cache.json"
    if not path.exists():
        return {}, {}
    bundle = load_json(path)
    if not isinstance(bundle, Mapping):
        return {}, {}
    meta_raw = bundle.get("_meta")
    meta = dict(meta_raw) if isinstance(meta_raw, Mapping) else {}
    raw = bundle.get("fit_scores")
    out: dict[str, dict[str, float]] = {}
    if isinstance(raw, Mapping):
        for brief_id, entries in raw.items():
            if not isinstance(entries, Mapping):
                continue
            scored: dict[str, float] = {}
            for kox_id, entry in entries.items():
                value = entry.get("fit_score") if isinstance(entry, Mapping) else entry
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    scored[str(kox_id)] = float(value)
            if scored:
                out[str(brief_id)] = scored
    return out, meta


def _llm_meta(
    meta: Mapping[str, Any],
    model_display: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """LLM 侧元信息。

    ``model`` 报**展示名**（服务端回报的型号优先），``requested_model_id`` 报我们发请求
    时用的 id。这两个字段必须分开：ARK 用 endpoint id 发请求，早期
    ``llm_cache.json._meta.models`` 里存的就是 ``ep-...``，直接当模型名报出去
    会说出"我们用的模型是 ep-2026…"这种站不住的话。

    ``model_display`` 由调用方从 ``llm_bench.json`` 归一后传入（旧缓存自身没有
    服务端型号信息）；拿不到就如实退回请求 id，并在 ``model_source`` 里说明。
    """
    key = meta.get("primary_model_key")
    models = meta.get("models") if isinstance(meta.get("models"), Mapping) else {}
    requested = models.get(key) if key is not None else None
    own = meta.get("model_identity")
    display = None
    source = "unavailable"
    if isinstance(own, Mapping):  # v2 缓存自带归一结果，优先用它
        display = model_display_map(own).get(str(key))
        if display:
            source = "llm_cache._meta.model_identity"
    if not display and model_display:
        display = model_display.get(str(key))
        if display:
            source = "llm_bench.json/model_identity"
    if not display:
        display = requested
        source = "llm_cache._meta.models（我们请求时用的 id，非服务端回报的型号）"
    return {
        "primary_model_key": key,
        "model": display,
        "requested_model_id": requested,
        "model_source": source,
        "fit_sample_n": meta.get("fit_sample_n"),
        "sample_seed": meta.get("seed"),
        "generated_at": meta.get("generated_at"),
    }


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _share(part: int, whole: int) -> float | None:
    """比例。分母为 0 时返回 ``None`` 而不是 0.0——"没有样本"不等于"比例为零"。"""
    return None if whole <= 0 else round(part / whole, 4)


def _score_comparison(
    ids: Sequence[str],
    llm: Mapping[str, float],
    rule: Mapping[str, float],
) -> dict[str, Any]:
    """两套分在同一批 kox 上的对照。样本为空时如实报 ``n=0``，不报任何均值。"""
    if not ids:
        return {"n": 0, "note": "这批 kox 上没有 LLM 分，无法对照"}
    llm_vals = [llm[k] for k in ids]
    rule_vals = [rule[k] for k in ids]
    diffs = [llm[k] - rule[k] for k in ids]
    lower = sum(1 for d in diffs if d < 0)
    higher = sum(1 for d in diffs if d > 0)
    return {
        "n": len(ids),
        "llm_fit_mean": _round(mean(llm_vals)),
        "rule_fit_mean": _round(mean(rule_vals)),
        "mean_diff_llm_minus_rule": _round(mean(diffs)),
        "max_abs_diff": _round(max(abs(d) for d in diffs)),
        "n_llm_lower": lower,
        "n_llm_higher": higher,
        "n_equal": len(ids) - lower - higher,
        "n_llm_below_review_threshold": sum(1 for v in llm_vals if v < FIT_SCORE_REVIEW_MAX),
        "n_rule_below_review_threshold": sum(1 for v in rule_vals if v < FIT_SCORE_REVIEW_MAX),
        "same_side_of_threshold_share": _share(
            sum(
                1
                for k in ids
                if (llm[k] < FIT_SCORE_REVIEW_MAX) == (rule[k] < FIT_SCORE_REVIEW_MAX)
            ),
            len(ids),
        ),
        "llm_only_lowers": higher == 0 and lower > 0,
    }


def _double_counting(
    ids: Sequence[str],
    by_id: Mapping[str, Mapping[str, Any]],
    llm: Mapping[str, float],
    rule: Mapping[str, float],
    results: Mapping[str, GateResult],
    spec: CampaignSpec,
) -> dict[str, Any]:
    """"LLM 判低、规则判高"的那批人里，有多少已被别的确定性口径处理过。

    三个对照口径是**正式链路里真实存在的扣分点**，不是事后凑的解释：
    - ``targeting_reason``：平台/品类/市场不在射程内 —— 预算侧定向直接剔除；
    - ``G2.4``：语言不匹配 —— G2 里独立一条规则；
    - ``audience_match_score``：年龄/性别人群结构 —— ``value(k)`` 里的独立乘子。
    """
    disputed = [k for k in ids if llm[k] < FIT_SCORE_REVIEW_MAX <= rule[k]]
    off_target = 0
    language = 0
    audience = 0
    covered = 0
    examples: list[dict[str, Any]] = []
    for kox_id in disputed:
        kox = by_id[kox_id]
        reason = targeting_reason(kox, spec)
        res = results.get(kox_id)
        lang_hit = bool(res is not None and res.has_rule("G2.4"))
        aud = audience_match_score(kox, spec)
        aud_low = aud < AUDIENCE_MATCH_LOW
        off_target += 1 if reason is not None else 0
        language += 1 if lang_hit else 0
        audience += 1 if aud_low else 0
        if reason is not None or lang_hit or aud_low:
            covered += 1
            if len(examples) < 3:
                examples.append(
                    {
                        "kox_id": kox_id,
                        "llm_fit": _round(llm[kox_id], 2),
                        "rule_fit": _round(rule[kox_id], 2),
                        "targeting_reason": reason,
                        "hits_G2_4_language": lang_hit,
                        "audience_match": _round(aud),
                    }
                )
    return {
        "n_llm_below_rule_above": len(disputed),
        "already_off_target_by_targeting": off_target,
        "already_flagged_by_G2_4_language": language,
        "already_low_audience_match": audience,
        "already_covered_by_any": covered,
        "already_covered_share": _share(covered, len(disputed)),
        "examples": examples,
        "note": (
            "这三项在正式链路里各自已经扣过一次分（定向剔除 / G2.4 / audience_match 乘子）。"
            "把同一个事实再计入 G2.3 就是双算，会让'加了 LLM 之后指标变好'变成口径叠加的假象。"
        ),
    }


def _injection_counterfactual(
    records: Sequence[Kox],
    spec: CampaignSpec,
    thresholds: Thresholds,
    llm: Mapping[str, float],
    gt_by_id: Mapping[str, Mapping[str, Any]],
    res_rule: Mapping[str, GateResult],
) -> dict[str, Any]:
    """真跑一遍"注入 LLM fit"的门禁 + 预算，和正式口径逐项对照。

    ``res_rule`` 由调用方传入（与覆盖率那段共用同一批规则版判定），避免同一个 campaign
    把 5,000 条门禁跑两遍——也顺带保证"对照的两边"确实来自同一次规则版评估。
    """
    res_llm = gate_results_for(records, spec, thresholds, llm)
    plan_rule, _ = plan_campaign(records, spec, thresholds, res_rule)
    plan_llm, _ = plan_campaign(records, spec, thresholds, res_llm)
    audit_rule = plan_audit(plan_rule, gt_by_id)
    audit_llm = plan_audit(plan_llm, gt_by_id)

    flips: list[dict[str, str]] = []
    for kox_id in sorted(llm):
        before = res_rule.get(kox_id)
        after = res_llm.get(kox_id)
        if before is None or after is None or before.verdict == after.verdict:
            continue
        flips.append({"kox_id": kox_id, "before": before.verdict, "after": after.verdict})

    candidates_llm, _ = build_candidates(records, res_llm, spec, thresholds, ("pass",))
    source_mix: dict[str, int] = {}
    for cand in candidates_llm:
        source = res_llm[cand.kox_id].fit_source
        source_mix[source] = source_mix.get(source, 0) + 1

    picked_rule = {a.kox_id for a in plan_rule.selected}
    picked_llm = {a.kox_id for a in plan_llm.selected}
    rate_rule = audit_rule["effective_view_rate"]
    rate_llm = audit_llm["effective_view_rate"]
    return {
        "candidate_pool_rule": plan_rule.candidate_pool,
        "candidate_pool_llm": plan_llm.candidate_pool,
        "fit_source_mix_in_pool": dict(sorted(source_mix.items())),
        "n_verdict_flips": len(flips),
        "verdict_flips": flips[:5],
        "n_selected_rule": len(plan_rule.selected),
        "n_selected_llm": len(plan_llm.selected),
        "selection_overlap": len(picked_rule & picked_llm),
        "spent_usd_rule": audit_rule["spent_usd"],
        "spent_usd_llm": audit_llm["spent_usd"],
        "wasted_spend_usd_rule": audit_rule["wasted_spend_usd"],
        "wasted_spend_usd_llm": audit_llm["wasted_spend_usd"],
        "wasted_delta_usd_llm_minus_rule": round(
            audit_llm["wasted_spend_usd"] - audit_rule["wasted_spend_usd"], 2
        ),
        "effective_view_rate_rule": rate_rule,
        "effective_view_rate_llm": rate_llm,
        "effective_view_rate_gap_pp": (
            None
            if rate_rule is None or rate_llm is None
            else round((rate_llm - rate_rule) * 100.0, 2)
        ),
        "note": (
            "注入只改 G2.3 的分值来源，其余 24 条规则、阈值、结构约束、排序公式完全不变；"
            "候选池与选人变化主要来自 value(k) 里 fit_score 因子的变化，不是判定翻转。"
        ),
    }


def _campaign_row(
    records: Sequence[Kox],
    by_id: Mapping[str, Mapping[str, Any]],
    spec: CampaignSpec,
    thresholds: Thresholds,
    llm_all: Mapping[str, float],
    gt_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    known = {k: v for k, v in llm_all.items() if k in by_id}
    missing = sorted(set(llm_all) - set(known))
    rule_all = {k: rule_fit_score(by_id[k], spec) for k in known}

    res_rule = gate_results_for(records, spec, thresholds)
    candidates, _ = build_candidates(records, res_rule, spec, thresholds, ("pass",))
    pool_ids = [c.kox_id for c in candidates]
    covered = sorted(set(pool_ids) & set(known))

    row: dict[str, Any] = {
        "campaign_id": spec.campaign_id,
        "name": spec.name,
        "n_llm_scored": len(llm_all),
        "n_scored_ids_not_in_dataset": len(missing),
        "scored_ids_not_in_dataset_sample": missing[:3],
        "coverage": {
            "candidate_pool": len(pool_ids),
            "covered_by_llm": len(covered),
            "coverage_share": _share(len(covered), len(pool_ids)),
            "note": (
                "分母是本 campaign 真实候选池（定向通过且门禁 pass），"
                "不是 5,000 全库——全库口径会把覆盖率算得更难看，但那个分母与预算决策无关。"
            ),
        },
        "on_all_scored": _score_comparison(sorted(known), known, rule_all),
        "on_covered_candidates": _score_comparison(covered, known, rule_all),
        "double_counted": _double_counting(sorted(known), by_id, known, rule_all, res_rule, spec),
    }
    if known:
        row["injection_counterfactual"] = _injection_counterfactual(
            records, spec, thresholds, known, gt_by_id, res_rule
        )
    else:
        row["injection_counterfactual"] = {"status": "no_scored_ids_in_this_dataset"}
    return row


def _totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pool = sum(int(r["coverage"]["candidate_pool"]) for r in rows)
    covered = sum(int(r["coverage"]["covered_by_llm"]) for r in rows)
    disputed = sum(int(r["double_counted"]["n_llm_below_rule_above"]) for r in rows)
    dbl = sum(int(r["double_counted"]["already_covered_by_any"]) for r in rows)
    cf = [
        r["injection_counterfactual"]
        for r in rows
        if "n_verdict_flips" in r["injection_counterfactual"]
    ]
    wasted_rule = sum(float(c["wasted_spend_usd_rule"]) for c in cf)
    wasted_llm = sum(float(c["wasted_spend_usd_llm"]) for c in cf)
    covered_rows = [r["on_covered_candidates"] for r in rows if r["on_covered_candidates"]["n"] > 0]
    return {
        "candidate_pool": pool,
        "covered_by_llm": covered,
        "coverage_share": _share(covered, pool),
        "n_llm_below_rule_above": disputed,
        "double_counted_share": _share(dbl, disputed),
        "n_verdict_flips": sum(int(c["n_verdict_flips"]) for c in cf),
        "n_scored_ids_not_in_dataset": sum(int(r["n_scored_ids_not_in_dataset"]) for r in rows),
        "wasted_spend_usd_rule": round(wasted_rule, 2),
        "wasted_spend_usd_llm": round(wasted_llm, 2),
        "wasted_delta_usd_llm_minus_rule": round(wasted_llm - wasted_rule, 2),
        "llm_only_lowers_on_covered": bool(
            covered_rows and all(r["llm_only_lowers"] for r in covered_rows)
        ),
    }


def _decision(totals: Mapping[str, Any]) -> dict[str, Any]:
    """由数字算出结论，不写死。覆盖率/双算率/单向性三项全过才判"可升格"。"""
    coverage = totals["coverage_share"]
    double = totals["double_counted_share"]
    one_sided = bool(totals["llm_only_lowers_on_covered"])
    coverage_ok = coverage is not None and coverage >= COVERAGE_MIN_FOR_PROMOTION
    # 没有"LLM 判低而规则判高"的人时 double 是 None：那是**没有双算问题**，
    # 不能当成不合格——把"无样本"和"不合格"混为一谈会造出一条假的拦截理由。
    double_ok = double is None or double <= DOUBLE_COUNT_MAX_FOR_PROMOTION
    checks = {
        "coverage_ok": coverage_ok,
        "no_double_counting": double_ok,
        "not_one_sided_on_covered": not one_sided,
    }
    blockers: list[str] = []
    if not coverage_ok:
        blockers.append(
            f"覆盖率不足：LLM 分只覆盖三个 campaign 候选池的 "
            f"{_pct(coverage)}（{totals['covered_by_llm']}/{totals['candidate_pool']} 人），"
            f"低于升格门槛 {COVERAGE_MIN_FOR_PROMOTION:.0%}。注入后 fit_score 变成"
            "'一部分人 LLM、一部分人规则'的混合尺子，预算差异主要反映谁在采样池里。"
        )
    if not double_ok:
        blockers.append(
            f"口径重叠（双算）：'LLM 判低而规则判高'的 {totals['n_llm_below_rule_above']} 人里，"
            f"{_pct(double)} 已被定向筛选 / G2.4 语言 / audience_match 乘子各自处理过，"
            f"高于允许上限 {DOUBLE_COUNT_MAX_FOR_PROMOTION:.0%}。注入等于同一事实扣两次分。"
        )
    if one_sided:
        blockers.append(
            "单向偏置：被覆盖到的候选上 LLM 分只会更低、没有一个更高（采样池偏向品类正中的号，"
            "规则版给满分），注入相当于对一个非随机子集单向罚分，不是中性替换。"
        )
    promotable = all(checks.values())
    return {
        "decision": (
            FIT_INJECTION_DECISION_PROMOTABLE if promotable else FIT_INJECTION_DECISION_KEEP_RULE
        ),
        "decision_label": (
            "三项判据全过：可以讨论把 LLM fit 升格为正式口径（仍需重跑全量打分并另行评审）"
            if promotable
            else "方案 B：正式链路继续用规则版 fit_score，LLM 版只作为离线对照量化"
        ),
        "checks": checks,
        "blockers": blockers,
        "thresholds": {
            "coverage_min_for_promotion": COVERAGE_MIN_FOR_PROMOTION,
            "double_count_max_for_promotion": DOUBLE_COUNT_MAX_FOR_PROMOTION,
        },
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _headline(totals: Mapping[str, Any], decision: Mapping[str, Any]) -> str:
    delta = totals["wasted_delta_usd_llm_minus_rule"]
    direction = "少浪费" if delta < 0 else ("多浪费" if delta > 0 else "浪费不变")
    return (
        f"LLM 适配分覆盖候选池 {_pct(totals['coverage_share'])}"
        f"（{totals['covered_by_llm']}/{totals['candidate_pool']} 人）；"
        f"离线注入后门禁判定只翻转 {totals['n_verdict_flips']} 条，"
        f"三个 campaign 合计{direction} ${abs(delta):,.0f}（gt 口径）。"
        f"结论：{decision['decision_label']}。"
    )


def llm_fit_audit(
    records: Sequence[Kox],
    specs: Sequence[CampaignSpec],
    thresholds: Thresholds,
    cache_path: Path | str | None = None,
    model_display: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """A4 LLM 适配分与规则版的对照 + 注入正式链路的离线反事实。

    Args:
        records: 达人库（含 gt；本函数只用 gt 做事后审计，不参与任何选人）。
        specs: campaign spec 列表，``campaign_id`` 需与缓存里的 brief_id 对齐。
        thresholds: 与正式链路同一套阈值。
        cache_path: ``llm_cache.json`` 路径，默认 ``output/llm_cache.json``。
        model_display: ``model_key -> 展示用模型名``，由调用方从 ``llm_bench.json``
            归一后传入（见 ``llm.identity``）。缺省时元信息里如实报请求 id。

    Returns:
        ``status="ok"`` 时含 per_campaign / totals / decision；
        缓存缺失或没有 fit 分时 ``status="llm_fit_not_run"``，附复跑命令，**不填任何估算值**。
    """
    scores, meta = load_llm_fit_scores(cache_path)
    real_specs = [s for s in specs if not s.is_neutral]
    if not scores:
        return {
            "status": "llm_fit_not_run",
            "note": (
                "output/llm_cache.json 里没有 fit_scores（未跑过 LLM 适配打分）。"
                "本表不填任何估算数字。"
            ),
            "rerun": RERUN_HINT,
        }
    if not real_specs:
        return {
            "status": "no_campaign_spec",
            "note": "没有真实 campaign spec（中性画像不做品类约束，适配分恒为 1.0，对照无意义）。",
        }

    by_id = {str(r.get("kox_id")): r for r in records}
    gt_by_id = {str(r.get("kox_id")): gt_of(r) for r in records}
    rows: list[dict[str, Any]] = []
    matched = 0
    for spec in real_specs:
        llm_all = scores.get(spec.campaign_id)
        if not llm_all:
            rows.append(
                {
                    "campaign_id": spec.campaign_id,
                    "name": spec.name,
                    "status": "no_llm_fit_for_this_campaign",
                }
            )
            continue
        matched += 1
        rows.append(_campaign_row(records, by_id, spec, thresholds, llm_all, gt_by_id))

    scored_rows = [r for r in rows if "coverage" in r]
    if not scored_rows:
        return {
            "status": "llm_fit_not_run",
            "note": (
                "缓存里的 fit_scores 与当前 brief 对不上（brief_id 不匹配），无法对照。"
                "这通常意味着 brief 重新生成过而 LLM 分没重跑。"
            ),
            "rerun": RERUN_HINT,
            "llm": _llm_meta(meta, model_display),
        }

    totals = _totals(scored_rows)
    decision = _decision(totals)
    return {
        "status": "ok",
        "llm": _llm_meta(meta, model_display),
        "review_threshold": FIT_SCORE_REVIEW_MAX,
        "formal_chain_fit_source": "rule:category_map+keyword",
        "headline": _headline(totals, decision),
        **decision,
        "totals": totals,
        "per_campaign": rows,
        "caveats": [
            "注入反事实只是**离线对照**：正式 metrics.json / budget.json 里的 fit_score 仍是规则版，"
            "所以正式指标依旧'同一份代码 + 同一份数据 -> 逐字节相同'。",
            f"反事实的样本极小：真正被 LLM 打过分又在候选池里的只有 "
            f"{totals['covered_by_llm']} 人（占 {_pct(totals['coverage_share'])}），"
            "金额差额不构成'LLM 更好/更差'的证据，只能说明'注入的影响主要不在判定翻转上'。",
            "LLM 分本身没有 ground truth：数据集的 gt 里没有'语义适配'这一维，"
            "所以这里报的是两套分的一致性与注入后的下游影响，**不报 LLM 适配分的准确率**。",
            "缓存的 _meta 里没有数据集指纹（dataset_sha256），"
            "只能靠 kox_id 是否存在于当前数据集来判断错配（见 n_scored_ids_not_in_dataset）。",
        ],
    }
