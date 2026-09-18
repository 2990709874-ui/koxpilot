# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/gates/g3.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
"""G3 品牌安全门禁 5 条（SPEC 4.G3）。

阈值来源分两类：
- G3.2 的"累计 severity 分"门槛：从达人库 ``risk_severity_score`` 的**同组 P90** 标定
  （数据集里约 9.5% 的达人带 low/medium flag，所以 P90 天然落在"有无风险载荷"的分界上——
   这是刻意的：让门槛自己长在数据里，而不是我拍一个 "> 2.5 分"）；
- 其余为 SPEC 明写的语义门槛（竞品 6 个月、high severity 硬阻断），常量在 policy.py 带出处。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..taxonomy import REGULATED_CATEGORY_FLAGS, competitors_of
from ..types import CampaignSpec, Reason
from .humanize import num
from .policy import (
    COMPETITOR_HARD_MONTHS,
    COMPETITOR_RECENT_MONTHS,
    G3_WEIGHTS,
    REGULATED_SEVERITY_MULTIPLIER,
)
from .signals import kox_group_key, risk_severity_score
from .thresholds import Thresholds

__all__ = ["G3Outcome", "evaluate_g3", "high_severity_flags"]

_FLAG_ZH = {
    "political_content": "政治敏感内容",
    "adult_content": "成人内容",
    "gambling_promo": "赌博推广",
    "medical_claim_strong": "强医疗功效宣称",
    "medical_claim": "医疗功效宣称",
    "profanity": "粗俗用语",
    "alcohol_mention": "酒类提及",
    "shock_humor": "猎奇/冒犯性幽默",
    "unverified_claim": "未经证实的宣称",
}
_CONTROVERSY_ZH = {
    "public_dispute": "公开争执",
    "refund_scandal": "退款纠纷",
    "plagiarism_claim": "抄袭指控",
    "sponsorship_undisclosed": "未披露广告合作",
}
_REGULATED_ZH = {"medical": "医疗健康", "finance": "金融", "kids": "儿童向", "alcohol": "酒类"}


@dataclass()
class G3Outcome:
    reasons: tuple[Reason, ...] = ()
    penalty: float = 0.0
    blocked_by: str | None = None
    severity_score: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


def high_severity_flags(kox: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(f)
        for f in (kox.get("content_flags") or [])
        if isinstance(f, Mapping) and str(f.get("severity")) == "high"
    ]


def evaluate_g3(
    kox: Mapping[str, Any],
    spec: CampaignSpec,
    thresholds: Thresholds,
) -> G3Outcome:
    """纯函数。G3.3/G3.5 依赖 campaign（竞品清单、受管制品类），为空时跳过。"""
    reasons: list[Reason] = []
    penalty = 0.0
    blocked_by: str | None = None
    details: dict[str, Any] = {}
    gkey = kox_group_key(kox)

    def hit(
        rule_id: str,
        signal: str,
        actual: float | str | None,
        threshold: float | str | None,
        text: str,
        source: str,
        severity: str = "soft",
    ) -> None:
        nonlocal penalty, blocked_by
        weight = G3_WEIGHTS[rule_id]
        reasons.append(
            Reason(
                gate="G3",
                rule_id=rule_id,
                signal=signal,
                actual=actual,
                threshold=threshold,
                weight=weight,
                severity=severity,  # type: ignore[arg-type]
                source=source,
                human_text=text,
            )
        )
        penalty += weight
        if severity == "block" and blocked_by is None:
            blocked_by = rule_id

    # ---- G3.1 高危内容 → 硬阻断 ---------------------------------------
    highs = high_severity_flags(kox)
    details["high_flags"] = [str(f.get("type")) for f in highs]
    if highs:
        names = "、".join(_FLAG_ZH.get(str(f.get("type")), str(f.get("type"))) for f in highs)
        hits = sum(int(f.get("hits") or 1) for f in highs)
        hit(
            "G3.1",
            "content_flags.severity",
            f"high×{len(highs)}",
            "high",
            f"内容审核命中高危标记：{names}（累计 {hits} 次）。"
            f"出海投放里这类内容一旦上线就是品牌事故，按品牌安全策略**硬阻断**，不进入预算分配。",
            "policy:spec_4.G3.1",
            severity="block",
        )

    # ---- G3.2 中低危累计载荷 -------------------------------------------
    score = risk_severity_score(kox)
    details["severity_score"] = round(score, 3)
    base_thr = thresholds.value(gkey, "risk_severity_score", "p90")
    thr = base_thr
    regulated = spec.regulated_category
    if thr is not None and regulated:
        thr = thr * REGULATED_SEVERITY_MULTIPLIER
    details["severity_threshold"] = None if thr is None else round(thr, 3)
    if thr is not None and score > thr:
        flags = [
            f"{_FLAG_ZH.get(str(f.get('type')), str(f.get('type')))}"
            f"（{f.get('severity')}×{f.get('hits')}）"
            for f in (kox.get("content_flags") or [])
            if isinstance(f, Mapping) and str(f.get("severity")) != "high"
        ]
        extra = (
            f"，且本次是{_REGULATED_ZH.get(regulated, regulated)}品类、门槛按 "
            f"{REGULATED_SEVERITY_MULTIPLIER:g} 倍收紧"
            if regulated
            else ""
        )
        hit(
            "G3.2",
            "risk_severity_score",
            round(score, 3),
            round(thr, 3),
            f"中低危内容累计风险载荷 {num(score, 1)} 分，高于同组 P90 门槛 {num(thr, 1)} 分"
            f"{extra}。命中项：{'、'.join(flags) or '争议记录'}。建议人工过一遍内容再决定。",
            f"quantile:{thresholds.source(gkey, 'risk_severity_score')}",
        )

    # ---- G3.3 竞品合作冲突 --------------------------------------------
    if spec.competitor_brands:
        competitor_set: set[str] = set(spec.competitor_brands)
        for brand in spec.competitor_brands:
            competitor_set |= set(competitors_of(brand))
        recent: list[dict[str, Any]] = []
        for collab in kox.get("past_collabs") or []:
            if not isinstance(collab, Mapping):
                continue
            brand = str(collab.get("brand"))
            months = collab.get("months_ago")
            if brand in competitor_set and isinstance(months, (int, float)):
                if months < COMPETITOR_RECENT_MONTHS:
                    recent.append({"brand": brand, "months_ago": int(months)})
        details["competitor_collabs"] = recent
        if recent:
            nearest = min(recent, key=lambda c: c["months_ago"])
            hard = nearest["months_ago"] < COMPETITOR_HARD_MONTHS
            hit(
                "G3.3",
                "past_collabs",
                f"{nearest['brand']}@{nearest['months_ago']}mo",
                f"<{COMPETITOR_RECENT_MONTHS}mo",
                f"{nearest['months_ago']} 个月前刚合作过竞品 {nearest['brand']}"
                + (
                    f"，落在通常 {COMPETITOR_HARD_MONTHS} 个月的排他期内，"
                    f"直接排除（既有法务风险也会被观众吐槽）。"
                    if hard
                    else f"，虽超出 {COMPETITOR_HARD_MONTHS} 个月排他期但仍在 "
                    f"{COMPETITOR_RECENT_MONTHS} 个月内，需客户确认是否接受。"
                ),
                "policy:spec_4.G3.3",
                severity="block" if hard else "soft",
            )

    # ---- G3.4 争议历史 ------------------------------------------------
    controversy = kox.get("controversy")
    if isinstance(controversy, Mapping) and controversy:
        ctype = _CONTROVERSY_ZH.get(str(controversy.get("type")), str(controversy.get("type")))
        hit(
            "G3.4",
            "controversy",
            str(controversy.get("type")),
            "null",
            f"存在争议历史：{ctype}（{controversy.get('months_ago')} 个月前，"
            f"严重度 {controversy.get('severity', 'low')}）。不必然否决，但要人工判断舆情是否已冷却。",
            "policy:spec_4.G3.4",
        )
        details["controversy"] = str(controversy.get("type"))

    # ---- G3.5 受管制品类叠加更严阈值 ------------------------------------
    if regulated:
        sensitive = set(REGULATED_CATEGORY_FLAGS.get(regulated, ()))
        matched = [
            str(f.get("type"))
            for f in (kox.get("content_flags") or [])
            if isinstance(f, Mapping) and str(f.get("type")) in sensitive
        ]
        details["regulated_matched_flags"] = matched
        if matched:
            names = "、".join(_FLAG_ZH.get(m, m) for m in matched)
            hit(
                "G3.5",
                "content_flags.type",
                "/".join(matched),
                f"regulated:{regulated}",
                f"本次是{_REGULATED_ZH.get(regulated, regulated)}品类（合规要求更高），"
                f"而该达人内容含 {names}，按更严阈值需人工合规审核后才能投。",
                "policy:spec_4.G3.5",
            )

    return G3Outcome(
        reasons=tuple(reasons),
        penalty=penalty,
        blocked_by=blocked_by,
        severity_score=score,
        details=details,
    )
