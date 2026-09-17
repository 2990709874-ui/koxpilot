"""gates：四层质量门禁 G0/G1/G2/G3（纯规则 + 分位数标定阈值）。

公开接口：
    calibrate(records)                         -> Thresholds       # 标定阈值
    evaluate(kox, spec, thresholds, fit_score)  -> GateResult       # 单条判定（纯函数）
    evaluate_all(records, spec, thresholds)     -> list[GateResult] # 批量
"""

from __future__ import annotations

from .engine import evaluate, evaluate_all, observable_view
from .g0 import completeness_of, evaluate_g0
from .g1 import evaluate_g1, spike_evidence
from .g2 import audience_match_score, evaluate_g2, rule_fit_score, source_agreement
from .g3 import evaluate_g3, high_severity_flags
from .policy import ALL_GATES, policy_snapshot
from .signals import extract_signals, kox_group_key
from .thresholds import Thresholds, calibrate

__all__ = [
    "ALL_GATES",
    "Thresholds",
    "audience_match_score",
    "calibrate",
    "completeness_of",
    "evaluate",
    "evaluate_all",
    "evaluate_g0",
    "evaluate_g1",
    "evaluate_g2",
    "evaluate_g3",
    "extract_signals",
    "high_severity_flags",
    "kox_group_key",
    "observable_view",
    "policy_snapshot",
    "rule_fit_score",
    "source_agreement",
    "spike_evidence",
]
