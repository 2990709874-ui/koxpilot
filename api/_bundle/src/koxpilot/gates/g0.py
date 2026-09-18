# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/gates/g0.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
"""G0 完整性门禁（SPEC 4.G0）。

规则
----
- **G0.1（SPEC 原文）**：``completeness = 1 - missing(critical)/len(critical)``，
  ``completeness < 0.8`` → review，且后续层置信度打折。
- **G0.2（本实现新增，需在文档中声明）**：只缺 1 个关键字段时 completeness = 0.8，
  按 SPEC 的严格不等式**不会**命中 G0.1；但 SPEC 3.3 的 gt 合成规则里
  "关键字段缺失 → review"是无论缺几个都成立的。若不补 G0.2，
  会出现一批"gt=review 而门禁 pass"的系统性漏检（数据集里约 4.5% 的达人只缺 1 个字段）。
  G0.2 的权重刻意低于 G0.1（缺 1 个字段的证据强度确实更弱），只触发 review，不参与 reject。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..types import Reason
from .humanize import pct
from .policy import COMPLETENESS_REVIEW_MAX, CRITICAL_FIELDS

__all__ = ["G0Outcome", "completeness_of", "evaluate_g0"]

_FIELD_ZH = {
    "followers": "粉丝数",
    "avg_views": "平均播放",
    "engagement_rate": "互动率",
    "audience_geo": "受众地域",
    "quoted_price_usd": "报价",
}


@dataclass(frozen=True)
class G0Outcome:
    completeness: float
    missing: tuple[str, ...]
    reasons: tuple[Reason, ...]
    low_confidence: bool


def completeness_of(kox: Mapping[str, Any]) -> tuple[float, tuple[str, ...]]:
    """关键字段完整度 + 缺失字段列表。

    判缺口径：字段不存在、为 None、或为空 dict/list 都算缺。
    （SPEC 3.3 的注入用 None 表示缺失，但真实多源数据里空 dict 同样常见。）
    """
    missing: list[str] = []
    for field_name in CRITICAL_FIELDS:
        value = kox.get(field_name, None)
        if value is None or (isinstance(value, (dict, list, str)) and len(value) == 0):
            missing.append(field_name)
    completeness = 1.0 - len(missing) / len(CRITICAL_FIELDS)
    return completeness, tuple(missing)


def evaluate_g0(kox: Mapping[str, Any]) -> G0Outcome:
    """纯函数：只看 kox 自身的字段存在性，不需要阈值表也不需要 campaign。"""
    completeness, missing = completeness_of(kox)
    reasons: list[Reason] = []
    missing_zh = "、".join(_FIELD_ZH.get(m, m) for m in missing)

    if completeness < COMPLETENESS_REVIEW_MAX:
        reasons.append(
            Reason(
                gate="G0",
                rule_id="G0.1",
                signal="completeness",
                actual=round(completeness, 4),
                threshold=COMPLETENESS_REVIEW_MAX,
                weight=1.0,
                severity="soft",
                source="policy:spec_4.G0",
                human_text=(
                    f"关键字段完整度仅 {pct(completeness)}（缺 {missing_zh}），"
                    f"低于门禁要求的 {pct(COMPLETENESS_REVIEW_MAX)}，"
                    f"该达人转人工复核，且真实性/一致性判定的置信度按 15% 折损计。"
                ),
            )
        )
    elif missing:
        reasons.append(
            Reason(
                gate="G0",
                rule_id="G0.2",
                signal="missing_critical_fields",
                actual=len(missing),
                threshold=0,
                weight=0.4,
                severity="soft",
                source="policy:derived_from_spec_3.3_missing_review",
                human_text=(
                    f"缺 1 个关键字段（{missing_zh}），完整度 {pct(completeness)} 恰好卡在门槛上，"
                    f"不足以判 reject，但报价/受众缺失会直接影响预算分配，先转人工补数。"
                ),
            )
        )
    return G0Outcome(
        completeness=completeness,
        missing=missing,
        reasons=tuple(reasons),
        low_confidence=completeness < COMPLETENESS_REVIEW_MAX,
    )
