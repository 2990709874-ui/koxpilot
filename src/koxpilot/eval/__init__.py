"""评测 harness（SPEC 第 7 节）：六张表 + 成本账 + 反事实价值账。"""

from __future__ import annotations

from .ablation import ablation_report
from .audit import cost_audit_report, counterfactual_report, plan_audit
from .harness import (
    EVAL_VERSION,
    EvalContext,
    build_context,
    load_eval_inputs,
    reject_fp_attribution,
    review_fp_attribution,
    run_full_eval,
)
from .llm_compare import llm_vs_rule_report
from .llm_fit import llm_fit_audit, load_llm_fit_scores
from .metrics import (
    confusion_matrix,
    fraud_pred_loose,
    fraud_pred_strict,
    fraud_report,
    verdict_report,
)
from .sensitivity import sensitivity_report
from .strata import strata_report

__all__ = [
    "EVAL_VERSION",
    "EvalContext",
    "ablation_report",
    "build_context",
    "confusion_matrix",
    "cost_audit_report",
    "counterfactual_report",
    "fraud_pred_loose",
    "fraud_pred_strict",
    "fraud_report",
    "llm_fit_audit",
    "llm_vs_rule_report",
    "load_llm_fit_scores",
    "load_eval_inputs",
    "plan_audit",
    "reject_fp_attribution",
    "review_fp_attribution",
    "run_full_eval",
    "sensitivity_report",
    "strata_report",
    "verdict_report",
]
