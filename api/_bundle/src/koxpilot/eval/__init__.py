# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/eval/__init__.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
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
    "load_eval_inputs",
    "load_llm_fit_scores",
    "plan_audit",
    "reject_fp_attribution",
    "review_fp_attribution",
    "run_full_eval",
    "sensitivity_report",
    "strata_report",
    "verdict_report",
]
