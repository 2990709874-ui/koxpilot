"""A5 预算分配：value/cost 贪心 + 分层配额修正（SPEC 第 5 节）。"""

from __future__ import annotations

from .allocator import Selection, allocate, validate_plan
from .planner import (
    ALL_VERDICTS,
    gate_results_for,
    plan_baseline,
    plan_campaign,
    plan_diversified_no_gate,
)
from .policy import constraint_snapshot
from .value import Candidate, build_candidates, estimate_cost, kpi_weight_of, targeting_reason

__all__ = [
    "Candidate",
    "Selection",
    "allocate",
    "build_candidates",
    "constraint_snapshot",
    "estimate_cost",
    "gate_results_for",
    "kpi_weight_of",
    "plan_baseline",
    "plan_campaign",
    "plan_diversified_no_gate",
    "targeting_reason",
    "validate_plan",
    "ALL_VERDICTS",
]
