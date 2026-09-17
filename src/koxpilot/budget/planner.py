"""A5 预算 Agent 的编排层：门禁 -> 候选池 -> 分配 -> 基线对照。

对外暴露三条臂：
- ``plan_campaign``：跑 KOXPilot 决策臂；
- ``plan_baseline``：跑"按粉丝量选人"反事实基线臂（SPEC 6.2）；
- ``plan_diversified_no_gate``：跑"只做结构分散化、不做质量门禁"的第三臂。

三臂共享同一批 GateResult 与同一套定向筛选，差异只在**选人依据**：

======================  ==============  ====================  ==========================
臂                      候选池           结构配额（分层/地域/单人）  排序依据
======================  ==============  ====================  ==========================
baseline_followers      pass+review+reject  只受总预算约束        粉丝量降序
diversified_no_gate     pass+review+reject  **强制**              名义曝光/报价
koxpilot                pass(+review)       **强制**              质量加权价值/报价
======================  ==============  ====================  ==========================

有了中间那条臂，"KOXPilot 比基线好"才能拆成两笔账：
基线 → 第三臂 = **分散化贡献**（不把钱压在少数头部大号上带来的差异）；
第三臂 → KOXPilot = **门禁与质量排序贡献**（识别水号/高风险号并按质量加权价值排序的差异）。
没有它，两臂差额只能笼统说成"门禁+排序+结构"，那是一句无法归因的话。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..gates.engine import evaluate_all
from ..gates.thresholds import Thresholds
from ..types import BudgetPlan, CampaignSpec, GateResult
from .allocator import allocate
from .policy import INCLUDE_REVIEW_BY_DEFAULT
from .value import build_candidates

__all__ = [
    "gate_results_for",
    "plan_campaign",
    "plan_baseline",
    "plan_diversified_no_gate",
    "ALL_VERDICTS",
]

ALL_VERDICTS: tuple[str, ...] = ("pass", "review", "reject")


def gate_results_for(
    records: Sequence[Mapping[str, Any]],
    spec: CampaignSpec,
    thresholds: Thresholds,
    fit_scores: Mapping[str, float] | None = None,
) -> dict[str, GateResult]:
    """按 campaign 口径跑一遍四层门禁，返回 ``kox_id -> GateResult``。"""
    results = evaluate_all(list(records), spec, thresholds, fit_scores)  # type: ignore[arg-type]
    return {r.kox_id: r for r in results}


def plan_campaign(
    records: Sequence[Mapping[str, Any]],
    spec: CampaignSpec,
    thresholds: Thresholds,
    results: Mapping[str, GateResult] | None = None,
    include_review: bool = INCLUDE_REVIEW_BY_DEFAULT,
    budget_usd: float | None = None,
) -> tuple[BudgetPlan, dict[str, GateResult]]:
    """KOXPilot 决策臂。返回 ``(方案, 门禁结果)``（门禁结果给基线臂与审计复用）。"""
    res = dict(results) if results is not None else gate_results_for(records, spec, thresholds)
    verdicts = ("pass", "review") if include_review else ("pass",)
    candidates, skipped = build_candidates(records, res, spec, thresholds, verdicts)
    plan = allocate(candidates, spec, "koxpilot", budget_usd, skipped)
    return plan, res


def plan_baseline(
    records: Sequence[Mapping[str, Any]],
    spec: CampaignSpec,
    thresholds: Thresholds,
    results: Mapping[str, GateResult] | None = None,
    budget_usd: float | None = None,
) -> BudgetPlan:
    """反事实基线臂：不看门禁，按粉丝量降序花完预算。"""
    res = dict(results) if results is not None else gate_results_for(records, spec, thresholds)
    candidates, skipped = build_candidates(records, res, spec, thresholds, ALL_VERDICTS)
    return allocate(candidates, spec, "followers", budget_usd, skipped)


def plan_diversified_no_gate(
    records: Sequence[Mapping[str, Any]],
    spec: CampaignSpec,
    thresholds: Thresholds,
    results: Mapping[str, GateResult] | None = None,
    budget_usd: float | None = None,
) -> BudgetPlan:
    """第三臂：**只做结构分散化，不做质量门禁**。

    与基线臂完全同一个候选池（含 review/reject 的号），但强制执行 KOXPilot 的
    分层/地域/单人配额；排序依据是"每美元买到的名义曝光"——只用 ``avg_views`` 与报价，
    **不读门禁判定、不读真实性折扣、不读语义适配与 KPI 权重**（见 allocator.BASIS_VIEWS）。

    为什么排序依据必须换掉：如果第三臂沿用 ``value(k)``，它就已经在用门禁体系算出来的
    真实性折扣去避开水号，"分散化贡献"里会混进门禁的功劳，归因立刻失真。
    为什么不用随机排序：随机会引入一个新的随机源，跨种子方差直接吞掉待测的差额；
    "每美元曝光"是采购侧不用任何风控信息也能自己算出来的最朴素排序，
    正好对应"一个只懂分散投放、不懂识别水号的买手"。
    """
    res = dict(results) if results is not None else gate_results_for(records, spec, thresholds)
    candidates, skipped = build_candidates(records, res, spec, thresholds, ALL_VERDICTS)
    return allocate(candidates, spec, "diversified_no_gate", budget_usd, skipped)
