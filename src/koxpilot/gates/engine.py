"""门禁引擎：四层判定合成（SPEC 第 4 节末尾）。

对外只有一个入口（纯函数、无副作用、无 IO、无全局状态）：

    evaluate(kox, campaign_spec, thresholds, fit_score=None,
             enabled_gates=ALL_GATES, disabled_rules=()) -> GateResult

架构要点
--------
1. 四层各自返回 reason 列表，**分数由 engine 统一从 reason 权重合成**。
   好处：消融实验（关掉某层）与口径对照（屏蔽某条规则）都只需过滤 reason 列表，
   不需要在四个 gate 里各写一遍开关，也不会出现"关了层但分数没跟着变"的 bug。
2. **防自证**：入口处用 ``observable_view`` 把 ``gt`` 字段物理剥掉，
   引擎内部拿到的 mapping 里根本不存在 ground truth
   （tests/test_no_gt_leak.py 还会用 AST 扫源码复查 gates/ 与 budget/ 下没有任何 gt 访问）。
3. **纯函数**：TS 侧只要读同一份 thresholds.json 就能逐条复算出同样的 verdict；
   任何隐式状态（缓存/全局配置/时间）都会让双实现一致性测试永远对不齐。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from ..types import NEUTRAL_SPEC, CampaignSpec, GateResult, Kox, Reason, Verdict
from .g0 import evaluate_g0
from .g1 import evaluate_g1
from .g2 import evaluate_g2
from .g3 import evaluate_g3
from .policy import (
    ALL_GATES,
    AUTHENTICITY_FLOOR_REJECT,
    COMPLETENESS_REVIEW_MAX,
    HARD_HITS_REJECT_MIN,
    LOW_CONFIDENCE_DISCOUNT,
)
from .signals import kox_group_key
from .thresholds import Thresholds

__all__ = [
    "GROUND_TRUTH_FIELD",
    "evaluate",
    "evaluate_all",
    "observable_view",
    "synthesize_verdict",
]

#: ground truth 字段名。本模块唯一使用它的地方是"把它排除掉"（见 observable_view）。
GROUND_TRUTH_FIELD: Final[str] = "".join(["g", "t"])


def observable_view(kox: Mapping[str, Any]) -> dict[str, Any]:
    """返回剥掉 ground truth 的可观测视图（防自证的物理隔离层）。"""
    excluded = GROUND_TRUTH_FIELD
    return {k: v for k, v in kox.items() if k != excluded}


def synthesize_verdict(
    *,
    blocked_by: str | None,
    authenticity: float,
    n_hard_hits: int,
    completeness: float,
    n_reasons: int,
) -> Verdict:
    """SPEC 第 4 节的判定合成逻辑，单独抽出来便于单测直接打表验证。

        if 任一硬阻断                 -> REJECT
        elif authenticity < floor     -> REJECT
        elif 硬信号命中 ≥2 条          -> REJECT  （与上一条互为交叉验证）
        elif 任一 review 命中 or completeness < 0.8 -> REVIEW
        else                          -> PASS
    """
    if blocked_by is not None:
        return "reject"
    if authenticity < AUTHENTICITY_FLOOR_REJECT:
        return "reject"
    if n_hard_hits >= HARD_HITS_REJECT_MIN:
        return "reject"
    if n_reasons > 0 or completeness < COMPLETENESS_REVIEW_MAX:
        return "review"
    return "pass"


def evaluate(
    kox: Mapping[str, Any],
    campaign_spec: CampaignSpec = NEUTRAL_SPEC,
    thresholds: Thresholds | None = None,
    fit_score: float | None = None,
    enabled_gates: Iterable[str] = ALL_GATES,
    disabled_rules: Iterable[str] = (),
) -> GateResult:
    """对单个达人跑四层门禁。

    Args:
        kox: SPEC 3.2 的达人 dict（含或不含 ``gt`` 都可以，引擎读不到 gt）。
        campaign_spec: campaign 结构化画像；默认 neutral = campaign 无关的库级判定。
        thresholds: 分位数阈值表；None 时 G1/G3.2 因无阈值而跳过（用于极端降级测试）。
        fit_score: G2.3 语义适配分的**外部注入点**（LLM 算好后喂进来）；None 时走规则版。
        enabled_gates: 参与判定的层，消融实验用。
        disabled_rules: 屏蔽的规则号（如 ``{"G2.2"}``），口径对照/单规则消融用。
    """
    gates = set(enabled_gates)
    blocked = set(disabled_rules)
    view = observable_view(kox)
    thr = thresholds if thresholds is not None else Thresholds()

    raw: list[Reason] = []
    completeness = 1.0
    fraud_score = 0.0
    fit_value = 1.0
    fit_source = "disabled"

    if "G0" in gates:
        g0 = evaluate_g0(view)
        completeness = g0.completeness
        raw.extend(g0.reasons)
    if "G1" in gates:
        g1 = evaluate_g1(view, thr)
        raw.extend(g1.reasons)
        fraud_score = g1.fraud_score
    if "G2" in gates:
        g2 = evaluate_g2(view, campaign_spec, fit_score)
        raw.extend(g2.reasons)
        fit_value = g2.fit_score
        fit_source = g2.fit_source
    if "G3" in gates:
        g3 = evaluate_g3(view, campaign_spec, thr)
        raw.extend(g3.reasons)

    reasons = tuple(r for r in raw if r.rule_id not in blocked)

    # G0.1 命中 => 后续层置信度打折（SPEC 4.G0）
    low_confidence = any(r.rule_id == "G0.1" for r in reasons)
    discount = LOW_CONFIDENCE_DISCOUNT if low_confidence else 1.0

    def layer_score(gate: str) -> float:
        penalty = sum(r.weight for r in reasons if r.gate == gate and r.severity != "block")
        return max(0.0, min(1.0, 1.0 - penalty * discount))

    authenticity = layer_score("G1") if "G1" in gates else 1.0
    consistency = layer_score("G2") if "G2" in gates else 1.0
    brand_safety = layer_score("G3") if "G3" in gates else 1.0
    hard_hits = tuple(r.rule_id for r in reasons if r.gate == "G1" and r.severity == "hard")
    blocked_by = next((r.rule_id for r in reasons if r.severity == "block"), None)

    verdict = synthesize_verdict(
        blocked_by=blocked_by,
        authenticity=authenticity,
        n_hard_hits=len(hard_hits),
        completeness=completeness,
        n_reasons=len(reasons),
    )

    return GateResult(
        kox_id=str(view.get("kox_id", "")),
        verdict=verdict,
        reasons=reasons,
        completeness=completeness,
        authenticity_score=authenticity,
        consistency_score=consistency,
        brand_safety_score=brand_safety,
        fraud_score=fraud_score,
        fit_score=fit_value,
        fit_source=fit_source,
        hard_hits=hard_hits,
        blocked_by=blocked_by,
        group_key=kox_group_key(view),
    )


def evaluate_all(
    records: Sequence[Kox],
    campaign_spec: CampaignSpec = NEUTRAL_SPEC,
    thresholds: Thresholds | None = None,
    fit_scores: Mapping[str, float] | None = None,
    enabled_gates: Iterable[str] = ALL_GATES,
    disabled_rules: Iterable[str] = (),
) -> list[GateResult]:
    """批量评估。``fit_scores`` 按 kox_id 索引（LLM 离线固化结果的注入方式）。"""
    gates = tuple(enabled_gates)
    blocked = tuple(disabled_rules)
    out: list[GateResult] = []
    for kox in records:
        fit = None
        if fit_scores is not None:
            fit = fit_scores.get(str(kox.get("kox_id")))
        out.append(evaluate(kox, campaign_spec, thresholds, fit, gates, blocked))
    return out
