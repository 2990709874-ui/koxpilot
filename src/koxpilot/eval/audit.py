"""A6 审计（SPEC 6.1 成本账 + 6.2 反事实价值账）——把技术指标翻译成钱。

两条口径纪律
------------
1. **审计用 ground truth 当裁判，不用引擎自己的分数。**
   有效曝光、浪费金额一律按 ``gt`` 计算。如果用引擎的 authenticity_score 去算
   "我省了多少钱"，那是拿自己的判断给自己打分（自证），数字再漂亮也不能信。
2. **保守假设要给敏感性。** 水号的曝光按 0 计入有效曝光是最保守的口径；
   同时给出"水号曝光按 50% 计"的版本，证明结论不依赖这个假设。

成本账里凡属**外推**的数字，一律显式标注 ``extrapolated=true``，
真实 token 只来自 ``output/llm_bench.json`` 的 API usage。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..io_utils import load_json
from ..types import BudgetPlan
from .metrics import gt_of

__all__ = [
    "FRAUD_RESIDUAL_VIEW_SHARE",
    "RATIO_FRAGILE_BASELINE_RATE",
    "plan_audit",
    "counterfactual_report",
    "cost_audit_report",
]

#: 敏感性假设：水号仍有多少比例的曝光算"有效"。
#: 主口径取 0（水号曝光全部作废），本常量用于给出宽松对照，证明结论稳健。
FRAUD_RESIDUAL_VIEW_SHARE: float = 0.5

#: 何时认定"相对提升比率的分母太小、不能当聚合口径用"。
#: 基线只选 4~7 人，某些样本几乎把钱全花在水号上，其有效曝光率会趋近 0；
#: 此时 (kox − base) / base 会放大到几十上百倍，均值就被单个样本绑架。
#: 低于这条线时，比率仍照实给出，但打上 ``ratio_denominator_fragile`` 并改用有界口径叙述。
RATIO_FRAGILE_BASELINE_RATE: float = 0.05


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    """分母为 0 时返回 ``None``，**不返回 0.0**。

    这一点很要紧：基线有效曝光为 0 意味着"基线的钱全打在水号上"，
    此时相对提升在数学上无定义（趋于 +∞），而写成 0.0 会被读成"没有提升"——
    把一个最有利的样本记成中性，是反向的虚假声称。
    """
    return numerator / denominator if denominator > 0 else None


def _bounded_uplift(
    base_eff: float,
    kox_eff: float,
    base_nominal: float,
    kox_nominal: float,
) -> dict[str, Any]:
    """给"有效曝光提升"配一组**有界**口径，供跨 campaign / 跨种子聚合使用。

    - ``rate_gap_pp``：两臂"有效曝光率"（有效/名义）之差，取值恒在 [−100, +100] pp；
    - ``symmetric_uplift``：``(kox − base) / (kox + base)``，取值恒在 [−1, +1]，符号与相对提升一致；
    - ``absolute_gain_views``：绝对差值，量纲是曝光数，不含除法。

    三者都不以基线为分母，所以基线趋近 0 时不会爆炸；``symmetric_uplift`` 在
    "基线有效曝光为 0" 这种极端样本上正好取到 +1（上界），而不是 +∞。
    """
    base_rate = _safe_ratio(base_eff, base_nominal)
    kox_rate = _safe_ratio(kox_eff, kox_nominal)
    total = base_eff + kox_eff
    return {
        "definition": (
            "rate_gap_pp = KOXPilot 有效曝光率 − 基线有效曝光率（百分点，∈[−100,100]）；"
            "symmetric_uplift = (kox − base)/(kox + base)（∈[−1,1]，符号同相对提升）；"
            "absolute_gain_views = kox − base（曝光数）。三者均不以基线为分母。"
        ),
        "baseline_effective_view_rate": round(base_rate, 4) if base_rate is not None else None,
        "koxpilot_effective_view_rate": round(kox_rate, 4) if kox_rate is not None else None,
        "rate_gap_pp": (
            round((kox_rate - base_rate) * 100.0, 2)
            if (base_rate is not None and kox_rate is not None)
            else None
        ),
        "symmetric_uplift": round((kox_eff - base_eff) / total, 4) if total > 0 else None,
        "absolute_gain_views": round(kox_eff - base_eff, 0),
        "ratio_denominator_views": round(base_eff, 0),
        "ratio_denominator_fragile": bool(
            base_rate is None or base_rate < RATIO_FRAGILE_BASELINE_RATE
        ),
        "fragile_threshold_baseline_rate": RATIO_FRAGILE_BASELINE_RATE,
    }


def plan_audit(plan: BudgetPlan, gt_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """用 gt 审计单个方案：钱花到哪些人身上、有效曝光多少。"""
    fraud_spend = 0.0
    high_risk_spend = 0.0
    wasted_spend = 0.0
    review_spend = 0.0
    reject_spend = 0.0
    eff_views = 0.0
    eff_views_lenient = 0.0
    nominal_views = 0.0
    n_fraud = 0
    n_high_risk = 0
    fraud_examples: list[dict[str, Any]] = []

    for a in plan.selected:
        gt = gt_by_id.get(a.kox_id, {})
        is_fraud = bool(gt.get("is_fraud"))
        is_high = str(gt.get("brand_safety")) == "high"
        nominal_views += a.est_views
        if is_fraud:
            fraud_spend += a.amount_usd
            n_fraud += 1
            eff_views_lenient += a.est_views * FRAUD_RESIDUAL_VIEW_SHARE
            if len(fraud_examples) < 5:
                fraud_examples.append(
                    {
                        "kox_id": a.kox_id,
                        "handle": a.handle,
                        "amount_usd": round(a.amount_usd, 2),
                        "fraud_type": gt.get("fraud_type"),
                        "followers_bucket": a.bucket,
                    }
                )
        else:
            eff_views += a.est_views
            eff_views_lenient += a.est_views
        if is_high:
            high_risk_spend += a.amount_usd
            n_high_risk += 1
        if is_fraud or is_high:
            wasted_spend += a.amount_usd
        if a.verdict == "review":
            review_spend += a.amount_usd
        elif a.verdict == "reject":
            reject_spend += a.amount_usd

    spent = plan.spent_usd
    return {
        "strategy": plan.strategy,
        "n_selected": len(plan.selected),
        "spent_usd": round(spent, 2),
        "utilization": round(spent / plan.budget_usd, 4) if plan.budget_usd else 0.0,
        "n_fraud_selected": n_fraud,
        "n_high_risk_selected": n_high_risk,
        "fraud_spend_usd": round(fraud_spend, 2),
        "fraud_spend_share": round(fraud_spend / spent, 4) if spent else 0.0,
        "high_risk_spend_usd": round(high_risk_spend, 2),
        "high_risk_spend_share": round(high_risk_spend / spent, 4) if spent else 0.0,
        "wasted_spend_usd": round(wasted_spend, 2),
        "wasted_spend_share": round(wasted_spend / spent, 4) if spent else 0.0,
        "spend_on_gate_review_usd": round(review_spend, 2),
        "spend_on_gate_reject_usd": round(reject_spend, 2),
        "nominal_views": round(nominal_views, 0),
        "effective_views_gt": round(eff_views, 0),
        "effective_views_gt_lenient": round(eff_views_lenient, 0),
        # 有效曝光率 = 有效/名义 ∈ [0,1]：这是**有界**的单臂质量口径，
        # 两臂相减即得 rate_gap_pp，用来替代分母会趋 0 的相对提升比率。
        "effective_view_rate": (
            round(eff_views / nominal_views, 4) if nominal_views > 0 else None
        ),
        "effective_view_rate_lenient": (
            round(eff_views_lenient / nominal_views, 4) if nominal_views > 0 else None
        ),
        "effective_cpm_usd": round(spent / (eff_views / 1000.0), 3) if eff_views > 0 else None,
        "effective_views_per_1k_usd": round(eff_views / (spent / 1000.0), 0) if spent else 0.0,
    }


def counterfactual_report(
    records: Sequence[Mapping[str, Any]],
    plans: Mapping[str, BudgetPlan],
    baselines: Mapping[str, BudgetPlan],
) -> dict[str, Any]:
    """SPEC 6.2：逐 campaign 对比 KOXPilot 与"按粉丝量选人"基线，并给出美元结论。"""
    gt_by_id = {str(k.get("kox_id")): gt_of(k) for k in records}
    per_campaign: list[dict[str, Any]] = []
    total_saved = 0.0
    total_budget = 0.0
    total_base_eff = 0.0
    total_kox_eff = 0.0
    total_base_eff_l = 0.0
    total_kox_eff_l = 0.0
    total_base_nominal = 0.0
    total_kox_nominal = 0.0

    for campaign_id, plan in plans.items():
        base = baselines.get(campaign_id)
        if base is None:
            continue
        kox_audit = plan_audit(plan, gt_by_id)
        base_audit = plan_audit(base, gt_by_id)
        saved = float(base_audit["wasted_spend_usd"]) - float(kox_audit["wasted_spend_usd"])
        base_eff = float(base_audit["effective_views_gt"])
        kox_eff = float(kox_audit["effective_views_gt"])
        uplift = _safe_ratio(kox_eff - base_eff, base_eff)
        base_eff_l = float(base_audit["effective_views_gt_lenient"])
        kox_eff_l = float(kox_audit["effective_views_gt_lenient"])
        uplift_l = _safe_ratio(kox_eff_l - base_eff_l, base_eff_l)
        base_nominal = float(base_audit["nominal_views"])
        kox_nominal = float(kox_audit["nominal_views"])
        bounded = _bounded_uplift(base_eff, kox_eff, base_nominal, kox_nominal)
        bounded_l = _bounded_uplift(base_eff_l, kox_eff_l, base_nominal, kox_nominal)
        per_1k_base = float(base_audit["effective_views_per_1k_usd"])
        per_1k_kox = float(kox_audit["effective_views_per_1k_usd"])
        total_saved += saved
        total_budget += plan.budget_usd
        total_base_eff += base_eff
        total_kox_eff += kox_eff
        total_base_eff_l += base_eff_l
        total_kox_eff_l += kox_eff_l
        total_base_nominal += base_nominal
        total_kox_nominal += kox_nominal
        per_campaign.append(
            {
                "campaign_id": campaign_id,
                "budget_usd": round(plan.budget_usd, 2),
                "baseline": base_audit,
                "koxpilot": kox_audit,
                "saved_usd": round(saved, 2),
                "saved_share_of_budget": round(saved / plan.budget_usd, 4)
                if plan.budget_usd
                else 0.0,
                # 相对提升：分母是**基线**有效曝光，单 campaign 上分母可能只有几万曝光甚至 0，
                # 因此它只适合"看这一条"，不适合跨 campaign / 跨种子求均值。
                "effective_view_uplift": round(uplift, 4) if uplift is not None else None,
                "effective_view_uplift_lenient": round(uplift_l, 4)
                if uplift_l is not None
                else None,
                "effective_view_uplift_bounded": bounded,
                "effective_view_uplift_bounded_lenient": bounded_l,
                "effective_view_uplift_caveat": _uplift_caveat(base_eff, bounded),
                "effective_views_per_1k_usd": {
                    "baseline": per_1k_base,
                    "koxpilot": per_1k_kox,
                    "uplift": round((per_1k_kox - per_1k_base) / per_1k_base, 4)
                    if per_1k_base > 0
                    else None,
                },
                "headline": _campaign_headline(plan.budget_usd, saved, uplift, bounded),
            }
        )

    overall_uplift = _safe_ratio(total_kox_eff - total_base_eff, total_base_eff)
    overall_uplift_l = _safe_ratio(total_kox_eff_l - total_base_eff_l, total_base_eff_l)
    overall_bounded = _bounded_uplift(
        total_base_eff, total_kox_eff, total_base_nominal, total_kox_nominal
    )
    return {
        "method": {
            "baseline": "按粉丝量降序选人直到花完预算（不看门禁、不做结构约束），行业最朴素做法",
            "shared": "两臂共用同一份定向筛选与同一份报价/估价口径，唯一差异是选人依据",
            "judge": "浪费金额与有效曝光全部按 gt 计算，不使用引擎自身分数（防自证）",
            "conservative_assumption": "主口径下水号曝光按 0 计入有效曝光",
            "lenient_assumption": f"对照口径下水号曝光按 {FRAUD_RESIDUAL_VIEW_SHARE:.0%} 计",
            "uplift_metrics": (
                "effective_view_uplift 是以基线为分母的相对提升（无界，分母趋 0 会爆炸）；"
                "effective_view_uplift_bounded 提供有界替代（rate_gap_pp ∈[−100,100] pp、"
                "symmetric_uplift ∈[−1,1]、absolute_gain_views）。"
                "跨 campaign / 跨种子聚合一律用有界口径。"
            ),
        },
        "per_campaign": per_campaign,
        "totals": {
            "budget_usd": round(total_budget, 2),
            "saved_usd": round(total_saved, 2),
            "saved_share_of_budget": round(total_saved / total_budget, 4) if total_budget else 0.0,
            "effective_views_baseline": round(total_base_eff, 0),
            "effective_views_koxpilot": round(total_kox_eff, 0),
            "effective_view_uplift": round(overall_uplift, 4)
            if overall_uplift is not None
            else None,
            # 宽松口径（水号曝光按 50% 计）在 totals 上一直缺位，导致"结论不依赖保守假设"
            # 这句话在总口径上没有产物支撑。这里补齐，与 per_campaign 同源。
            "effective_views_baseline_lenient": round(total_base_eff_l, 0),
            "effective_views_koxpilot_lenient": round(total_kox_eff_l, 0),
            "effective_view_uplift_lenient": round(overall_uplift_l, 4)
            if overall_uplift_l is not None
            else None,
            "effective_view_uplift_bounded": overall_bounded,
            "lenient_assumption": (
                f"宽松口径 = 水号曝光按 {FRAUD_RESIDUAL_VIEW_SHARE:.0%} 计入有效曝光"
                "（主口径按 0 计）。两个口径同向才说明结论不依赖该假设。"
            ),
        },
        "headline": _totals_headline(
            total_budget, total_saved, overall_uplift, overall_uplift_l, overall_bounded
        ),
    }


def _totals_headline(
    budget: float,
    saved: float,
    uplift: float | None,
    uplift_lenient: float | None,
    bounded: Mapping[str, Any],
) -> str:
    """总口径一句话结论：主口径比率 + 宽松口径比率 + 有界口径，三样都给。

    只写当前产物里真有的数字：任何一项无定义（分母为 0）就说"无定义"，不用 0% 顶替。
    """
    if not budget:
        return "无预算，无结论"
    parts = [
        f"三个 campaign 合计 ${budget:,.0f} 预算，少浪费 ${saved:,.0f}（{saved / budget:.1%}）",
        f"有效曝光提升 {f'{uplift:+.1%}' if uplift is not None else '无定义（基线有效曝光为 0）'}",
        f"宽松口径（水号曝光按 {FRAUD_RESIDUAL_VIEW_SHARE:.0%} 计）"
        f"{f'{uplift_lenient:+.1%}' if uplift_lenient is not None else '无定义'}",
    ]
    if bounded["rate_gap_pp"] is not None:
        parts.append(
            f"有界口径：有效曝光率 {_pct(bounded['baseline_effective_view_rate'])}"
            f" → {_pct(bounded['koxpilot_effective_view_rate'])}"
            f"（{bounded['rate_gap_pp']:+.1f}pp，symmetric_uplift {bounded['symmetric_uplift']:+.4f}）"
        )
    return "，".join(parts)


def _pct(value: float | None) -> str:
    """把 0~1 的比率写成人能读的百分数；``None`` 明确写成"无定义"而不是 0%。"""
    return f"{value:.1%}" if value is not None else "无定义（名义曝光为 0）"


def _uplift_caveat(base_eff: float, bounded: Mapping[str, Any]) -> str:
    """相对提升比率的分母体检结论，直接落进产物（读者不必自己去算分母大小）。"""
    head = (
        f"该比率的分母是基线有效曝光 {base_eff:,.0f} 次"
        f"（占基线名义曝光 {_pct(bounded['baseline_effective_view_rate'])}）。"
    )
    if bounded["ratio_denominator_fragile"]:
        body = (
            f"分母已低于名义曝光的 {RATIO_FRAGILE_BASELINE_RATE:.0%}"
            "（ratio_denominator_fragile=true），此时比率会被放大到数倍甚至数百倍，"
            "**不要拿它跨样本求均值**；"
        )
    else:
        body = "本样本分母充足，比率可读；但跨样本聚合仍应统一用有界口径，"
    return head + body + "聚合请用 effective_view_uplift_bounded 里的 rate_gap_pp 或 symmetric_uplift。"


def _campaign_headline(
    budget: float,
    saved: float,
    uplift: float | None,
    bounded: Mapping[str, Any],
) -> str:
    """单 campaign 的一句话结论。

    **分母不可靠时不说那个比率**：基线有效曝光低于名义曝光的 5%（或为 0）时，
    "提升 +3585%" 这种数字虽然算得对，却会被当成成绩单读——这里换成有界口径叙述，
    比率本体仍留在 ``effective_view_uplift`` 字段里供人核对。
    """
    if not budget:
        return "预算为 0，无结论"
    # saved < 0 时"少浪费 $-5,118"读起来像成绩，直说"反而多浪费"才不会被误读。
    money = (
        f"少浪费 ${saved:,.0f}（占预算 {saved / budget:.1%}）"
        if saved >= 0
        else f"反而比基线多浪费 ${-saved:,.0f}（占预算 {-saved / budget:.1%}）"
    )
    head = f"同样 ${budget:,.0f} 预算，{money}"
    gap = bounded["rate_gap_pp"]
    rates = (
        f"有效曝光率 {_pct(bounded['baseline_effective_view_rate'])}"
        f" → {_pct(bounded['koxpilot_effective_view_rate'])}（{gap:+.1f}pp）"
        if gap is not None
        else "两臂名义曝光为 0，无有效曝光率可比"
    )
    if bounded["ratio_denominator_fragile"] or uplift is None:
        return (
            f"{head}，按 ground truth 计的{rates}"
            "；相对提升比率的分母（基线有效曝光）过小，已标记为不可聚合，故此处不引用该比率"
        )
    return f"{head}，按 ground truth 计的有效曝光提升 {uplift:+.1%}；{rates}"


def cost_audit_report(
    plans: Mapping[str, BudgetPlan],
    bench_path: Path | str,
    rule_arm_f1: float | None = None,
    llm_arm_f1: float | None = None,
) -> dict[str, Any]:
    """SPEC 6.1：三种架构的调用次数账 + （有真实 usage 时）token 与成本外推。

    调用次数是**确定性真数**（候选池大小可数）；token 与美元金额只有在
    ``output/llm_bench.json`` 存在时才给，且标注为按真实单次 usage 线性外推。
    """
    pools = {cid: plan.candidate_pool for cid, plan in plans.items()}
    total_pool = sum(pools.values())
    schemes = [
        {
            "scheme": "全 LLM",
            "desc": "每个达人的每层判定都问模型",
            "calls_per_campaign": {cid: n * 4 for cid, n in pools.items()},
            "calls_total": total_pool * 4,
        },
        {
            "scheme": "KOXPilot 混合",
            "desc": "G0/G1 纯规则，G2 部分规则，仅 brief 解析与语义适配用 LLM",
            "calls_per_campaign": {cid: 1 + n for cid, n in pools.items()},
            "calls_total": len(pools) + total_pool,
        },
        {
            "scheme": "全规则",
            "desc": "不用 LLM，语义适配退化为关键词匹配",
            "calls_per_campaign": {cid: 0 for cid in pools},
            "calls_total": 0,
        },
    ]
    ratio = (
        round(schemes[0]["calls_total"] / schemes[1]["calls_total"], 2)
        if schemes[1]["calls_total"]
        else None
    )
    payload: dict[str, Any] = {
        "candidate_pool_per_campaign": pools,
        "schemes": schemes,
        "call_reduction_vs_full_llm": ratio,
        "accuracy_cost_of_all_rules": (
            {
                "metric": "标签错配 F1（同一抽样、同一 ground truth）",
                "rule_arm": rule_arm_f1,
                "llm_arm": llm_arm_f1,
                "f1_drop_if_all_rules": round(llm_arm_f1 - rule_arm_f1, 4)
                if (rule_arm_f1 is not None and llm_arm_f1 is not None)
                else None,
            }
        ),
    }
    path = Path(bench_path)
    if not path.exists():
        payload.update(
            {
                "token_account": None,
                "status": "llm_not_run",
                "explanation": (
                    "调用次数为可数真值；token 与美元数字必须来自真实 API usage，"
                    "本环境未配置 endpoint 故留空。配置后执行 "
                    "`python -m koxpilot.llm.runner` 生成 output/llm_bench.json 即自动补齐。"
                ),
            }
        )
        return payload

    bench = load_json(path)
    totals = bench.get("totals") or {}
    calls = int(totals.get("calls") or 0)
    payload.update(
        {
            "status": "ok",
            "token_account": {
                "source": "output/llm_bench.json（各 API 返回的 usage 字段，非估算）",
                "models": bench.get("models"),
                "measured_calls": calls,
                "prompt_tokens": totals.get("prompt_tokens"),
                "completion_tokens": totals.get("completion_tokens"),
                "reasoning_tokens": totals.get("reasoning_tokens"),
                "total_tokens": totals.get("total_tokens"),
                "tokens_per_call": round(float(totals.get("total_tokens") or 0) / calls, 1)
                if calls
                else None,
                "per_task": bench.get("per_task"),
                "extrapolated": False,
            },
            "scheme_token_extrapolation": {
                "basis": "按实测单次调用 token 均值 × 各方案调用次数线性外推",
                "extrapolated": True,
                "tokens": {
                    s["scheme"]: round(
                        float(totals.get("total_tokens") or 0) / calls * int(s["calls_total"]), 0
                    )
                    if calls
                    else None
                    for s in schemes
                },
            },
        }
    )
    return payload
