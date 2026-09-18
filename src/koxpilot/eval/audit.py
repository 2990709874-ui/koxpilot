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
from ..llm.identity import identity_from_bench, model_display_map
from ..types import BudgetPlan
from .metrics import gt_of

__all__ = [
    "ARM_LABELS",
    "FRAUD_RESIDUAL_VIEW_SHARE",
    "JUDGE_GROUND_TRUTH",
    "LENIENT_VIEW_ASSUMPTION",
    "MAIN_VIEW_ASSUMPTION",
    "RATIO_FRAGILE_BASELINE_RATE",
    "arm_attribution",
    "cost_audit_report",
    "counterfactual_report",
    "effective_view_calibers",
    "plan_audit",
]

#: 三条对照臂的产物命名（与 budget/planner.py 的三个入口一一对应）
ARM_LABELS: dict[str, str] = {
    "baseline_followers": "基线：粉丝量降序、无结构约束、不看门禁",
    "diversified_no_gate": "第三臂：强制分层/地域/单人配额，排序按每美元名义曝光，仍不看门禁",
    "koxpilot": "KOXPilot：门禁过滤 + 结构约束 + 质量加权价值排序",
}

#: 敏感性假设：水号仍有多少比例的曝光算"有效"。
#: 主口径取 0（水号曝光全部作废），本常量用于给出宽松对照，证明结论稳健。
FRAUD_RESIDUAL_VIEW_SHARE: float = 0.5

#: 有效曝光与浪费金额的裁判来源。写成常量是为了让对外接口能把"谁在判"标出来，
#: 而不是靠文档口头约定：``ground_truth`` = 数据集标注，不是引擎自己的分数。
JUDGE_GROUND_TRUTH: str = "ground_truth"

#: 两个口径的假设原文。对外接口与前端都引用这两句，避免同一件事出现两种措辞。
MAIN_VIEW_ASSUMPTION: str = "主口径：标注为水号的达人，其曝光按 0 计入有效曝光"
LENIENT_VIEW_ASSUMPTION: str = (
    f"宽松口径：水号曝光按 {FRAUD_RESIDUAL_VIEW_SHARE:.0%} 计入有效曝光。两个口径同向才说明结论不依赖该假设"
)

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


def effective_view_calibers(
    audit: Mapping[str, Any], spend_usd: float | None = None
) -> dict[str, Any]:
    """把 :func:`plan_audit` 的结果翻成"每美元有效曝光"的对外口径（主口径 + 宽松口径）。

    为什么要这个函数：多条臂的花费能差几十倍（门禁只买得起过关的那几个人），
    绝对有效曝光并排摆出来会把"花得少"读成"做得差"，所以对外只能按每美元比。
    而这个换算必须**只写一处**——接口、CLI、前端各写一遍除法，早晚会出现
    一处按主口径、一处按宽松口径的两套数字。

    两条纪律都在这里落实：
    - **裁判是 gt**：分子取 ``plan_audit`` 已经按标注算好的有效曝光，
      不接受引擎自评的 value(k)/authenticity_score（那是自证）；
    - **两个口径一起给**：主口径（水号曝光按 0）与宽松口径（按
      :data:`FRAUD_RESIDUAL_VIEW_SHARE` 计）同时输出，只给对自己有利的那个不算结论。

    Args:
        audit: :func:`plan_audit` 的返回值。
        spend_usd: 分母花费。缺省用 ``audit["spent_usd"]``；显式传入是为了让调用方
            能用自己四舍五入后的花费当分母，保证接口里 ``每美元 × 花费`` 自洽。
    Returns:
        含 ``judge`` 与四个数字的字典；花费为 0 时两个"每美元"字段给 ``None``
        （**不是 0.0**：没花钱与花了钱没效果是两件事）。
    """
    eff = float(audit["effective_views_gt"])
    eff_lenient = float(audit["effective_views_gt_lenient"])
    money = float(audit["spent_usd"] if spend_usd is None else spend_usd)
    return {
        "judge": JUDGE_GROUND_TRUTH,
        "effective_views_gt": round(eff, 1),
        "effective_views_gt_per_dollar": round(eff / money, 2) if money > 0 else None,
        "effective_views_gt_lenient": round(eff_lenient, 1),
        "effective_views_gt_lenient_per_dollar": (
            round(eff_lenient / money, 2) if money > 0 else None
        ),
    }


def _share_of_total(part: float, total: float) -> float | None:
    """把某一段贡献表示成"占总贡献的比例"。

    总贡献 ≤ 0 时返回 ``None``：此时"占比"没有可读的含义（分母为 0 或方向相反，
    会出现 −140% 这类只会误导人的数字），宁可留空并在 note 里说清楚。
    """
    if total <= 0:
        return None
    return round(part / total, 4)


def arm_attribution(
    base_audit: Mapping[str, Any],
    div_audit: Mapping[str, Any],
    kox_audit: Mapping[str, Any],
    budget_usd: float,
) -> dict[str, Any]:
    """三臂价值归因：把"KOXPilot − 基线"拆成**分散化**与**门禁+质量排序**两段。

    两段的定义严格按链式差分（可加，恒等式一定成立）：

        总差额 = (第三臂 − 基线) + (KOXPilot − 第三臂)
               = 分散化贡献      + 门禁与质量排序贡献

    口径两条：
    - 钱：``wasted_spend_usd``（花在 gt 水号 / gt 高风险号上的钱）越低越好，
      所以"少浪费"= 前一臂 − 后一臂；
    - 曝光质量：用**有界**的 ``effective_view_rate``（有效曝光/名义曝光）之差，
      单位百分点。这里刻意不用"相对提升比率"——它的分母是基线有效曝光，
      基线几乎全买水号时分母趋 0，拆出来的两段会各自爆炸成几百倍，加起来还对不上总数。

    每段贡献都可能是**负数**（例如结构分散化把钱推向长尾，长尾里水号更密时会更浪费）。
    这不是 bug，也不做任何截断——负贡献照实写进产物。
    """
    waste_base = float(base_audit["wasted_spend_usd"])
    waste_div = float(div_audit["wasted_spend_usd"])
    waste_kox = float(kox_audit["wasted_spend_usd"])
    saved_total = waste_base - waste_kox
    saved_div = waste_base - waste_div
    saved_gate = waste_div - waste_kox

    rate_base = _opt(base_audit.get("effective_view_rate"))
    rate_div = _opt(div_audit.get("effective_view_rate"))
    rate_kox = _opt(kox_audit.get("effective_view_rate"))
    gap_total = _gap_pp(rate_base, rate_kox)
    gap_div = _gap_pp(rate_base, rate_div)
    gap_gate = _gap_pp(rate_div, rate_kox)
    eff_base = _opt(base_audit.get("effective_views_gt"))
    eff_div = _opt(div_audit.get("effective_views_gt"))
    eff_kox = _opt(kox_audit.get("effective_views_gt"))
    n_base = int(base_audit["n_selected"])
    n_div = int(div_audit["n_selected"])
    n_kox = int(kox_audit["n_selected"])

    caveats = [
        f"三臂选中人数差一个量级（基线 {n_base} 人 / 第三臂 {n_div} 人 / KOXPilot {n_kox} 人）。"
        "'分散化贡献'里本身就包含'把同一笔钱摊到更多达人'的大数效应——那正是这条臂要度量的东西，"
        "但也意味着三臂在'人数'上不是同量级对照，不能拿人数本身论优劣。"
    ]
    if eff_div is not None and eff_kox is not None and eff_div > eff_kox:
        caveats.append(
            f"第三臂的**绝对**有效曝光（{eff_div:,.0f}）高于 KOXPilot（{eff_kox:,.0f}）。"
            "原因是第三臂按'每美元名义曝光'排序，会优先买走 CPM 最便宜的长尾，"
            "而 KOXPilot 最大化的是'质量加权价值/报价'（含语义适配、KPI 权重、真实性折扣），"
            "会主动放弃一部分便宜但不对味的曝光。所以三臂可比的口径是**有效曝光率**与**浪费金额**；"
            "绝对曝光数不是 KOXPilot 的优化目标，这条如实写出来，不用'我们曝光也最多'去掩盖。"
        )

    return {
        "definition": (
            "链式差分：总差额 =（第三臂 − 基线）+（KOXPilot − 第三臂）"
            "= 分散化贡献 + 门禁与质量排序贡献。两段可加，且允许为负（负值照实写）。"
        ),
        "arms": dict(ARM_LABELS),
        "waste_reduction_usd": {
            "wasted_baseline": round(waste_base, 2),
            "wasted_diversified_no_gate": round(waste_div, 2),
            "wasted_koxpilot": round(waste_kox, 2),
            "total": round(saved_total, 2),
            "by_diversification": round(saved_div, 2),
            "by_gating_and_quality_ranking": round(saved_gate, 2),
            "share_of_total_by_diversification": _share_of_total(saved_div, saved_total),
            "share_of_total_by_gating": _share_of_total(saved_gate, saved_total),
            "share_of_budget_by_diversification": round(saved_div / budget_usd, 4)
            if budget_usd
            else None,
            "share_of_budget_by_gating": round(saved_gate / budget_usd, 4)
            if budget_usd
            else None,
        },
        "effective_view_rate_pp": {
            "baseline": rate_base,
            "diversified_no_gate": rate_div,
            "koxpilot": rate_kox,
            "gap_total_pp": gap_total,
            "gap_by_diversification_pp": gap_div,
            "gap_by_gating_and_quality_ranking_pp": gap_gate,
        },
        "n_selected": {
            "baseline": n_base,
            "diversified_no_gate": n_div,
            "koxpilot": n_kox,
        },
        "effective_views_gt": {
            "baseline": round(eff_base, 0) if eff_base is not None else None,
            "diversified_no_gate": round(eff_div, 0) if eff_div is not None else None,
            "koxpilot": round(eff_kox, 0) if eff_kox is not None else None,
        },
        "n_fraud_selected": {
            "baseline": int(base_audit["n_fraud_selected"]),
            "diversified_no_gate": int(div_audit["n_fraud_selected"]),
            "koxpilot": int(kox_audit["n_fraud_selected"]),
        },
        "caveats": caveats,
        "note": (
            "share_of_total_* 在总差额 ≤ 0 时留空（None）：那种情况下'占总贡献的百分比'"
            "没有可读含义，用金额与百分点差本身来看即可。"
            "第三臂与 KOXPilot 的差额里同时包含'门禁过滤 review/reject'与"
            "'排序依据从每美元曝光换成质量加权价值'两件事——它们共用同一批真实性/适配分数，"
            "在实现上无法再拆得更细，所以这里合并命名为'门禁与质量排序'，不谎报成纯门禁效果。"
        ),
    }


def _opt(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _gap_pp(before: float | None, after: float | None) -> float | None:
    """两个比率之差，单位百分点；任一侧无定义（名义曝光为 0）则返回 None。"""
    if before is None or after is None:
        return None
    return round((after - before) * 100.0, 2)


def counterfactual_report(
    records: Sequence[Mapping[str, Any]],
    plans: Mapping[str, BudgetPlan],
    baselines: Mapping[str, BudgetPlan],
    diversified: Mapping[str, BudgetPlan] | None = None,
) -> dict[str, Any]:
    """SPEC 6.2：逐 campaign 对比 KOXPilot 与"按粉丝量选人"基线，并给出美元结论。

    ``diversified`` 给出第三臂（只做结构分散化、不做门禁）时，额外输出
    ``value_attribution``：把两臂差额拆成分散化贡献与门禁贡献（见 :func:`arm_attribution`）。
    """
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
    div_totals = {"wasted": 0.0, "eff": 0.0, "nominal": 0.0, "n_selected": 0, "n_fraud": 0}
    base_totals = {"wasted": 0.0, "n_selected": 0, "n_fraud": 0}
    kox_totals = {"wasted": 0.0, "n_selected": 0, "n_fraud": 0}
    has_div = bool(diversified)

    for campaign_id, plan in plans.items():
        base = baselines.get(campaign_id)
        if base is None:
            continue
        kox_audit = plan_audit(plan, gt_by_id)
        base_audit = plan_audit(base, gt_by_id)
        div_plan = (diversified or {}).get(campaign_id)
        div_audit = plan_audit(div_plan, gt_by_id) if div_plan is not None else None
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
        if div_audit is not None:
            div_totals["wasted"] += float(div_audit["wasted_spend_usd"])
            div_totals["eff"] += float(div_audit["effective_views_gt"])
            div_totals["nominal"] += float(div_audit["nominal_views"])
            div_totals["n_selected"] += int(div_audit["n_selected"])
            div_totals["n_fraud"] += int(div_audit["n_fraud_selected"])
        for acc, aud in ((base_totals, base_audit), (kox_totals, kox_audit)):
            acc["wasted"] += float(aud["wasted_spend_usd"])
            acc["n_selected"] += int(aud["n_selected"])
            acc["n_fraud"] += int(aud["n_fraud_selected"])
        row: dict[str, Any] = {
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
            "effective_view_uplift_lenient": round(uplift_l, 4) if uplift_l is not None else None,
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
        if div_audit is not None:
            row["diversified_no_gate"] = div_audit
            row["value_attribution"] = arm_attribution(
                base_audit, div_audit, kox_audit, plan.budget_usd
            )
        per_campaign.append(row)

    overall_uplift = _safe_ratio(total_kox_eff - total_base_eff, total_base_eff)
    overall_uplift_l = _safe_ratio(total_kox_eff_l - total_base_eff_l, total_base_eff_l)
    overall_bounded = _bounded_uplift(
        total_base_eff, total_kox_eff, total_base_nominal, total_kox_nominal
    )
    # 总口径的三臂归因：把三个 campaign 的钱与曝光先汇总，再做同一套链式差分。
    # 汇总后再算比率（而不是把三个 campaign 的比率取平均），避免小预算 campaign 被放大权重。
    attribution: dict[str, Any] | None = None
    if has_div:
        attribution = arm_attribution(
            {
                "wasted_spend_usd": base_totals["wasted"],
                "n_selected": base_totals["n_selected"],
                "n_fraud_selected": base_totals["n_fraud"],
                "effective_views_gt": total_base_eff,
                "effective_view_rate": _safe_ratio(total_base_eff, total_base_nominal),
            },
            {
                "wasted_spend_usd": div_totals["wasted"],
                "n_selected": div_totals["n_selected"],
                "n_fraud_selected": div_totals["n_fraud"],
                "effective_views_gt": div_totals["eff"],
                "effective_view_rate": _safe_ratio(div_totals["eff"], div_totals["nominal"]),
            },
            {
                "wasted_spend_usd": kox_totals["wasted"],
                "n_selected": kox_totals["n_selected"],
                "n_fraud_selected": kox_totals["n_fraud"],
                "effective_views_gt": total_kox_eff,
                "effective_view_rate": _safe_ratio(total_kox_eff, total_kox_nominal),
            },
            total_budget,
        )
        attribution["headline"] = _attribution_headline(attribution)
    return {
        "method": {
            "baseline": "按粉丝量降序选人直到花完预算（不看门禁、不做结构约束），行业最朴素做法",
            "third_arm": (
                "diversified_no_gate：与基线同一个候选池（含 review/reject），"
                "但强制 KOXPilot 的分层/地域/单人配额，排序依据换成'每美元名义曝光'，"
                "全程不读门禁判定与真实性折扣——用来把两臂差额拆成分散化与门禁两段"
            )
            if has_div
            else "本次未跑第三臂（diversified_no_gate），故不输出价值归因分解",
            "shared": "三臂共用同一份定向筛选与同一份报价/估价口径，差异只在候选池与选人依据",
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
        "value_attribution": attribution,
        "headline": _totals_headline(
            total_budget, total_saved, overall_uplift, overall_uplift_l, overall_bounded
        ),
    }


def _attribution_headline(attribution: Mapping[str, Any]) -> str:
    """三臂归因的一句话结论：两段金额各自多少、方向如何，负值直说"反而更浪费"。"""
    money = attribution["waste_reduction_usd"]
    gaps = attribution["effective_view_rate_pp"]
    total = float(money["total"])
    div = float(money["by_diversification"])
    gate = float(money["by_gating_and_quality_ranking"])

    def seg(name: str, amount: float) -> str:
        if amount >= 0:
            return f"{name}少浪费 ${amount:,.0f}"
        return f"{name}反而多浪费 ${-amount:,.0f}"

    parts = [
        f"相对基线共少浪费 ${total:,.0f}",
        f"其中{seg('结构分散化', div)}，{seg('门禁与质量排序', gate)}",
    ]
    if gaps["gap_total_pp"] is not None:
        parts.append(
            f"有效曝光率 {_pct(gaps['baseline'])} →（第三臂）{_pct(gaps['diversified_no_gate'])}"
            f" →（KOXPilot）{_pct(gaps['koxpilot'])}"
            f"，分散化 {gaps['gap_by_diversification_pp']:+.1f}pp、"
            f"门禁与质量排序 {gaps['gap_by_gating_and_quality_ranking_pp']:+.1f}pp"
        )
    return "，".join(parts)


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
    # `models` 取展示名（服务端回报优先），请求 id 单独放 `model_identity`：
    # 成本账里"这些 token 是谁花的"必须只有一个答案。
    identity = identity_from_bench(bench)
    payload.update(
        {
            "status": "ok",
            "token_account": {
                "source": "output/llm_bench.json（各 API 返回的 usage 字段，非估算）",
                "models": model_display_map(identity) or bench.get("models"),
                "model_identity": identity,
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
