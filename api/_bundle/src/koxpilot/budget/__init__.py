# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/budget/__init__.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
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
from .value import (
    Candidate,
    build_candidates,
    estimate_cost,
    kpi_weight_of,
    targeting_reason,
)

__all__ = [
    "ALL_VERDICTS",
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
]
