"""预算分配的候选池构建与价值/成本模型（SPEC 第 5 节的 value(k) / cost(k)）。

    value(k) = avg_views(k) × authenticity_discount(k) × fit_score(k) × audience_match(k) × kpi_weight(k)
    cost(k)  = quoted_price_usd(k)，缺失则按同组 avg_cpm 中位数估算并标注 price_estimated

三条纪律
--------
1. **只读可观测字段**：候选构建同样不碰 ``gt``（gt 只在 eval/audit 里作为裁判出现）。
2. **缺失不当 0**：报价缺失走估算并打标；avg_views 缺失则直接出池并记录原因，
   绝不用 0 参与排序（0 成本会让缺价号排在最前，是这类系统的经典事故）。
3. **定向前置筛选与选人依据分离**：``targeting_reason`` 只做 campaign 定向过滤，
   KOXPilot 臂、第三臂 ``diversified_no_gate`` 与"按粉丝量选人"基线臂**共用同一个定向筛选**，
   三臂的差异只在候选池（是否按门禁过滤）与选人依据（质量加权价值 / 名义曝光 / 粉丝量），
   这样反事实审计与三臂价值归因才是干净对照。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..gates.g2 import audience_match_score, rule_fit_score
from ..gates.thresholds import Thresholds
from ..taxonomy import CATEGORY_ADJACENCY, follower_bucket, group_key
from ..types import CampaignSpec, GateResult
from .policy import KPI_EXPONENTS, PRICE_ESTIMATE_QUANTILE, RATIO_CLAMP

__all__ = [
    "Candidate",
    "build_candidates",
    "estimate_cost",
    "kpi_weight_of",
    "targeting_reason",
]


@dataclass(slots=True)
class Candidate:
    """进入分配器的一个候选（已算好 value / cost / efficiency）。"""

    kox_id: str
    handle: str
    platform: str
    country: str
    bucket: str
    group: str
    verdict: str
    followers: float
    avg_views: float
    engagement_rate: float
    cost_usd: float
    price_estimated: bool
    authenticity_discount: float
    fit_score: float
    audience_match: float
    kpi_weight: float
    value: float
    efficiency: float

    @property
    def est_engagements(self) -> float:
        return self.avg_views * self.engagement_rate


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def targeting_reason(kox: Mapping[str, Any], spec: CampaignSpec) -> str | None:
    """campaign 定向筛选。返回 None 表示通过，否则返回不通过的原因码。

    只做"这个号在不在这次投放的射程内"，**不做任何质量判断**（质量判断是门禁的活）。
    三个维度都遵循"spec 没给就不约束"：
      - platform：不在投放平台清单
      - category：自称/观测品类都不沾目标品类及其相邻品类
      - market：既不是目标市场本地账号，受众里也没有任何目标市场占比
    """
    if spec.platforms and str(kox.get("platform")) not in spec.platforms:
        return "platform_off_target"

    if spec.target_categories:
        cats = {str(c) for c in (kox.get("declared_categories") or [])}
        cats |= {str(c) for c in (kox.get("observed_categories") or [])}
        allowed: set[str] = set()
        for target in spec.target_categories:
            allowed.add(target)
            allowed.update(CATEGORY_ADJACENCY.get(target, ()))
        if not (cats & allowed):
            return "category_off_target"

    if spec.target_markets:
        geo = kox.get("audience_geo")
        local = str(kox.get("country")) in spec.target_markets
        share = None
        if isinstance(geo, Mapping) and geo:
            share = sum(float(geo.get(m, 0.0) or 0.0) for m in spec.target_markets)
        if not local and share is not None and share <= 0.0:
            return "market_off_target"
    return None


def _clamped_ratio(actual: float | None, reference: float | None) -> float:
    """同组归一比值，落在 RATIO_CLAMP 内。任一侧缺失/非正 → 1.0（中性，不奖不罚）。"""
    lo, hi = RATIO_CLAMP
    if actual is None or reference is None or reference <= 0 or actual <= 0:
        return 1.0
    return min(hi, max(lo, actual / reference))


def kpi_weight_of(
    kox: Mapping[str, Any], spec: CampaignSpec, thresholds: Thresholds, group: str
) -> float:
    """KPI 权重：同组归一后的互动率与评论占比的幂次组合（指数见 policy.KPI_EXPONENTS）。"""
    a, b = KPI_EXPONENTS.get(spec.kpi, KPI_EXPONENTS["balanced"])
    if a == 0.0 and b == 0.0:
        return 1.0
    er_ratio = _clamped_ratio(
        _num(kox.get("engagement_rate")), thresholds.value(group, "engagement_rate", "median")
    )
    clr_ratio = _clamped_ratio(
        _num(kox.get("comment_like_ratio")), thresholds.value(group, "comment_like_ratio", "median")
    )
    return round((er_ratio**a) * (clr_ratio**b), 6)


def estimate_cost(
    kox: Mapping[str, Any], thresholds: Thresholds, group: str
) -> tuple[float | None, bool]:
    """返回 ``(cost_usd, price_estimated)``；无法定价时返回 ``(None, True)``。"""
    quoted = _num(kox.get("quoted_price_usd"))
    if quoted is not None and quoted > 0:
        return quoted, False
    views = _num(kox.get("avg_views"))
    cpm = thresholds.value(group, "avg_cpm_usd", PRICE_ESTIMATE_QUANTILE)
    if views is None or views <= 0 or cpm is None or cpm <= 0:
        return None, True
    return cpm * views / 1000.0, True


def build_candidates(
    records: Sequence[Mapping[str, Any]],
    results: Mapping[str, GateResult],
    spec: CampaignSpec,
    thresholds: Thresholds,
    include_verdicts: tuple[str, ...] = ("pass",),
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """构建候选池。

    Args:
        records: 达人库（可含 gt；本函数不读）。
        results: kox_id -> GateResult（**必须是用同一个 campaign_spec 跑出来的**，
            否则 fit/geo 相关的判定与这里的 value 口径会打架）。
        include_verdicts: 允许进池的门禁档位。默认只要 pass。
    Returns:
        ``(candidates, skipped)``；skipped 每条含 kox_id + reason，用于产品里解释"谁被排除了、为什么"。
    """
    candidates: list[Candidate] = []
    skipped: list[dict[str, Any]] = []

    for kox in records:
        kox_id = str(kox.get("kox_id"))
        reason = targeting_reason(kox, spec)
        if reason is not None:
            skipped.append({"kox_id": kox_id, "reason": reason})
            continue
        result = results.get(kox_id)
        if result is None:
            skipped.append({"kox_id": kox_id, "reason": "no_gate_result"})
            continue
        if result.verdict not in include_verdicts:
            skipped.append({"kox_id": kox_id, "reason": f"gate_{result.verdict}"})
            continue

        views = _num(kox.get("avg_views"))
        if views is None or views <= 0:
            skipped.append({"kox_id": kox_id, "reason": "avg_views_missing"})
            continue
        followers = _num(kox.get("followers")) or 0.0
        bucket = follower_bucket(followers if followers > 0 else None)
        group = group_key(str(kox.get("platform", "unknown")), bucket)

        cost, estimated = estimate_cost(kox, thresholds, group)
        if cost is None or cost <= 0:
            skipped.append({"kox_id": kox_id, "reason": "price_unavailable"})
            continue

        fit = result.fit_score if result.fit_source != "disabled" else rule_fit_score(kox, spec)
        audience = audience_match_score(kox, spec)
        weight = kpi_weight_of(kox, spec, thresholds, group)
        discount = result.authenticity_score
        value = views * discount * fit * audience * weight
        candidates.append(
            Candidate(
                kox_id=kox_id,
                handle=str(kox.get("handle", "")),
                platform=str(kox.get("platform", "unknown")),
                country=str(kox.get("country", "??")),
                bucket=bucket,
                group=group,
                verdict=result.verdict,
                followers=followers,
                avg_views=views,
                engagement_rate=_num(kox.get("engagement_rate")) or 0.0,
                cost_usd=cost,
                price_estimated=estimated,
                authenticity_discount=discount,
                fit_score=fit,
                audience_match=audience,
                kpi_weight=weight,
                value=value,
                efficiency=value / cost,
            )
        )
    return candidates, skipped
