"""A5 预算 Agent 的编排层：门禁 -> 候选池 -> 分配 -> 基线对照。

对外只暴露两个函数：
- ``plan_campaign``：跑 KOXPilot 决策臂；
- ``plan_baseline``：跑"按粉丝量选人"反事实基线臂（SPEC 6.2）。

两臂共享同一批 GateResult 与同一套定向筛选，唯一差异是**选人依据**：
基线臂拿到完全一样的候选池（含被门禁判 review/reject 的号），只按粉丝量降序花钱。
这样反事实审计里的差额，能干净地归因到"质量门禁 + 价值排序"，而不是归因到定向口径不同。
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

__all__ = ["gate_results_for", "plan_campaign", "plan_baseline", "ALL_VERDICTS"]

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
