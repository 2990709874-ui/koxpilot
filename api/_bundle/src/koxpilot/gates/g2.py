# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/gates/g2.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
"""G2 一致性门禁：标签/口径/campaign 适配 6 条（SPEC 4.G2）。

与 G1 的关键区别：**G2 是 campaign 条件的**。
G2.1 / G2.2 只看达人自身（库级恒定），G2.3–G2.6 依赖 campaign_spec。
当 campaign_spec 相应字段为空时（``CampaignSpec`` 默认的 neutral 画像），
对应规则自动跳过 —— 这不是偷懒，而是评测口径的必要条件：
SPEC 3.3 的 ``gt.verdict`` 是**campaign 无关**的库级判定，
若拿"带 campaign 条件的判定"去比 campaign 无关的 gt，混淆矩阵会被口径差异污染。
（唯一例外是 G2.5：markets 为空时退化为"以达人自称的所属国为目标市场"，
  这恰好对应 gt 里"受众地域错位"的定义，且只用到可观测字段。）

G2.3 的 LLM 注入点
------------------
``evaluate_g2(..., fit_score=None)``：
- 传入 float → 直接用外部（LLM）算好的语义适配分；
- 传 None   → 退化为规则版 ``rule_fit_score``（品类映射表 + handle 关键词）。
这样 LLM 与规则两条路径的 G2.3 判定可以逐条 diff（SPEC 7 第 6 张表的口径）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..stats import jaccard
from ..taxonomy import (
    CATEGORY_ADJACENCY,
    CATEGORY_KEYWORDS,
    MARKET_LANGUAGES,
)
from ..types import CampaignSpec, Reason
from .humanize import cats, pct, ratio
from .policy import (
    AUDIENCE_AGE_WEIGHT,
    AUDIENCE_GENDER_WEIGHT,
    AUDIENCE_GEO_MIN,
    AUDIENCE_MATCH_MIN,
    DECLARED_OBSERVED_JACCARD_MIN,
    FIT_SCORE_REVIEW_MAX,
    G2_WEIGHTS,
    SOURCE_JACCARD_MIN,
)

__all__ = [
    "G2Outcome",
    "audience_geo_share",
    "audience_match_score",
    "evaluate_g2",
    "rule_fit_score",
    "source_agreement",
]

#: 规则版 fit 的三档打分（可追溯：完全命中 / 相邻品类 / 完全不沾）
_FIT_EXACT = 1.0
_FIT_ADJACENT = 0.65
_FIT_MISS = 0.20
_FIT_KEYWORD = 0.70          # handle/bio 关键词命中：弱证据，只能顶到 0.7
_FIT_DECLARED_DISCOUNT = 0.9  # 只在"自称品类"里命中、观测品类没命中 → 打 9 折


@dataclass()
class G2Outcome:
    reasons: tuple[Reason, ...] = ()
    penalty: float = 0.0
    fit_score: float = 1.0
    fit_source: str = "skipped"
    audience_match: float = 1.0
    geo_share: float = 1.0
    details: dict[str, Any] = field(default_factory=dict)


def rule_fit_score(kox: Mapping[str, Any], spec: CampaignSpec) -> float:
    """规则版语义适配分（无 LLM 兜底）。

    打分逻辑（刻意做成三档 + 关键词兜底，便于口头解释）：
      - 观测品类里有 campaign 目标品类 → 1.0
      - 只有相邻品类（如目标 3C，达人做家电）→ 0.65
      - 仅自称品类命中而观测品类没命中 → 上述分数 × 0.9（自称不如观测可信）
      - 什么都不沾但 handle 里有目标品类关键词 → 0.70
      - 完全不沾 → 0.20
    """
    if not spec.target_categories:
        return 1.0
    observed = [str(c) for c in (kox.get("observed_categories") or [])]
    declared = [str(c) for c in (kox.get("declared_categories") or [])]
    best = _FIT_MISS
    for target in spec.target_categories:
        adjacency = set(CATEGORY_ADJACENCY.get(target, ()))
        for cat in observed:
            if cat == target:
                best = max(best, _FIT_EXACT)
            elif cat in adjacency:
                best = max(best, _FIT_ADJACENT)
        for cat in declared:
            if cat == target:
                best = max(best, _FIT_EXACT * _FIT_DECLARED_DISCOUNT)
            elif cat in adjacency:
                best = max(best, _FIT_ADJACENT * _FIT_DECLARED_DISCOUNT)
    handle = str(kox.get("handle", "")).lower()
    if best <= _FIT_MISS:
        for target in spec.target_categories:
            if any(kw in handle for kw in CATEGORY_KEYWORDS.get(target, ())):
                best = max(best, _FIT_KEYWORD)
    return round(best, 4)


def audience_geo_share(kox: Mapping[str, Any], markets: tuple[str, ...]) -> float | None:
    """目标市场在受众地域里的合计占比。audience_geo 缺失返回 None（交给 G0）。"""
    geo = kox.get("audience_geo")
    if not isinstance(geo, Mapping) or not geo:
        return None
    if not markets:
        return None
    return float(sum(float(geo.get(m, 0.0) or 0.0) for m in markets))


def audience_match_score(kox: Mapping[str, Any], spec: CampaignSpec) -> float:
    """年龄+性别加权重叠度（SPEC 4.G2.6）。

    权重来自 policy（年龄 0.6 / 性别 0.4）。只给了一维时权重归一到该维。
    两维都没给（neutral 画像）返回 1.0 —— "没有人群要求"不该被判成"人群不匹配"。
    """
    age_profile = kox.get("audience_age")
    gender_profile = kox.get("audience_gender")
    parts: list[tuple[float, float]] = []
    if spec.target_age_buckets and isinstance(age_profile, Mapping):
        share = sum(float(age_profile.get(b, 0.0) or 0.0) for b in spec.target_age_buckets)
        parts.append((AUDIENCE_AGE_WEIGHT, min(1.0, share)))
    if spec.target_gender and isinstance(gender_profile, Mapping):
        share = float(gender_profile.get(spec.target_gender, 0.0) or 0.0)
        parts.append((AUDIENCE_GENDER_WEIGHT, min(1.0, share)))
    if not parts:
        return 1.0
    total_w = sum(w for w, _ in parts)
    return round(sum(w * v for w, v in parts) / total_w, 4)


def source_agreement(kox: Mapping[str, Any]) -> float | None:
    """三源标签两两 Jaccard 的平均值（SPEC 4.G2.2）。少于 2 源返回 None。"""
    tags = kox.get("source_tags")
    if not isinstance(tags, Mapping):
        return None
    sets = [list(v or []) for v in tags.values()]
    if len(sets) < 2:
        return None
    pairs: list[float] = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            pairs.append(jaccard(sets[i], sets[j]))
    if not pairs:
        return None
    return round(sum(pairs) / len(pairs), 4)


def evaluate_g2(
    kox: Mapping[str, Any],
    spec: CampaignSpec,
    fit_score: float | None = None,
) -> G2Outcome:
    """纯函数。``fit_score`` 为 LLM 注入点（None 时走规则版）。"""
    reasons: list[Reason] = []
    penalty = 0.0
    details: dict[str, Any] = {}

    def hit(
        rule_id: str,
        signal: str,
        actual: float | str | None,
        threshold: float | str | None,
        text: str,
        source: str,
    ) -> None:
        nonlocal penalty
        weight = G2_WEIGHTS[rule_id]
        reasons.append(
            Reason(
                gate="G2",
                rule_id=rule_id,
                signal=signal,
                actual=actual,
                threshold=threshold,
                weight=weight,
                severity="soft",
                source=source,
                human_text=text,
            )
        )
        penalty += weight

    # ---- G2.1 自称 vs 观测品类 ----------------------------------------
    declared = [str(c) for c in (kox.get("declared_categories") or [])]
    observed = [str(c) for c in (kox.get("observed_categories") or [])]
    j_do = jaccard(declared, observed)
    details["declared_observed_jaccard"] = round(j_do, 4)
    if j_do < DECLARED_OBSERVED_JACCARD_MIN:
        hit(
            "G2.1",
            "declared_vs_observed_categories",
            round(j_do, 4),
            DECLARED_OBSERVED_JACCARD_MIN,
            f"自称品类 {cats(declared)} 与近 30 条内容的观测品类 {cats(observed)} "
            f"重叠度 Jaccard={ratio(j_do)} 低于 {DECLARED_OBSERVED_JACCARD_MIN}，"
            f"标签与实际内容错配，按此标签选人会选错人群。",
            "policy:spec_4.G2.1",
        )

    # ---- G2.2 多源标签冲突 --------------------------------------------
    agree = source_agreement(kox)
    details["source_agreement"] = agree
    if agree is not None and agree < SOURCE_JACCARD_MIN:
        hit(
            "G2.2",
            "source_tags",
            agree,
            SOURCE_JACCARD_MIN,
            f"三个数据源给的品类两两平均一致度仅 {ratio(agree)}（低于 {SOURCE_JACCARD_MIN}）："
            f"平台={cats((kox.get('source_tags') or {}).get('src_platform'))}，"
            f"供应商A={cats((kox.get('source_tags') or {}).get('src_vendor_a'))}，"
            f"供应商B={cats((kox.get('source_tags') or {}).get('src_vendor_b'))}，"
            f"口径打架，需人工核定主品类。",
            "policy:spec_4.G2.2",
        )

    # ---- G2.3 与 campaign 目标品类的语义适配（LLM 注入点）---------------
    fit_value = 1.0
    fit_source = "skipped:no_target_categories"
    if spec.target_categories:
        if fit_score is None:
            fit_value = rule_fit_score(kox, spec)
            fit_source = "rule:category_map+keyword"
        else:
            fit_value = float(min(1.0, max(0.0, fit_score)))
            fit_source = "injected:llm_fit_score"
        details["fit_score"] = fit_value
        details["fit_source"] = fit_source
        if fit_value < FIT_SCORE_REVIEW_MAX:
            hit(
                "G2.3",
                "campaign_fit",
                round(fit_value, 4),
                FIT_SCORE_REVIEW_MAX,
                f"与本次 campaign 目标品类 {cats(list(spec.target_categories))} 的语义适配分仅 "
                f"{ratio(fit_value)}（门槛 {FIT_SCORE_REVIEW_MAX}）：达人观测品类为 {cats(observed)}，"
                f"既非目标品类也非相邻品类，内容调性对不上。"
                f"（打分来源：{'LLM 语义分' if fit_source.startswith('injected') else '规则版品类映射表'}）",
                f"policy:spec_4.G2.3|{fit_source}",
            )

    # ---- G2.4 语言不匹配 ----------------------------------------------
    lang = str(kox.get("language") or "")
    allowed: set[str] = set(spec.target_languages)
    if not allowed and spec.target_markets:
        for market in spec.target_markets:
            allowed |= set(MARKET_LANGUAGES.get(market, frozenset()))
    if allowed and lang:
        details["language_allowed"] = sorted(allowed)
        if lang not in allowed:
            hit(
                "G2.4",
                "language",
                lang,
                "/".join(sorted(allowed)),
                f"达人内容语言为 {lang}，不在目标市场可接受语言集 "
                f"{{{', '.join(sorted(allowed))}}} 内，本地化不匹配。",
                "policy:spec_4.G2.4",
            )

    # ---- G2.5 受众地域重叠（markets 为空时退化为达人自称市场）------------
    markets = spec.target_markets or (str(kox.get("country") or ""),)
    markets = tuple(m for m in markets if m)
    geo_share = audience_geo_share(kox, markets)
    details["target_markets"] = list(markets)
    details["geo_share"] = geo_share
    if geo_share is not None and geo_share < AUDIENCE_GEO_MIN:
        scope = "本次目标市场" if spec.target_markets else "该达人自称所属市场"
        hit(
            "G2.5",
            "audience_geo",
            round(geo_share, 4),
            AUDIENCE_GEO_MIN,
            f"{scope}（{'/'.join(markets)}）在其受众地域中只占 {pct(geo_share)}，"
            f"低于 {pct(AUDIENCE_GEO_MIN)} 门槛，钱会花给不在目标市场的观众。",
            "policy:spec_4.G2.5",
        )

    # ---- G2.6 受众人群重叠 --------------------------------------------
    match = audience_match_score(kox, spec)
    details["audience_match"] = match
    if (spec.target_age_buckets or spec.target_gender) and match < AUDIENCE_MATCH_MIN:
        want = []
        if spec.target_age_buckets:
            want.append("/".join(spec.target_age_buckets) + " 岁")
        if spec.target_gender:
            want.append("女性" if spec.target_gender == "f" else "男性")
        hit(
            "G2.6",
            "audience_age+gender",
            round(match, 4),
            AUDIENCE_MATCH_MIN,
            f"目标人群（{'、'.join(want)}）与该达人受众画像的加权重叠度只有 {ratio(match)}，"
            f"低于 {AUDIENCE_MATCH_MIN}（年龄权重 {AUDIENCE_AGE_WEIGHT}/性别权重 "
            f"{AUDIENCE_GENDER_WEIGHT}），人群偏差过大。",
            "policy:spec_4.G2.6",
        )

    return G2Outcome(
        reasons=tuple(reasons),
        penalty=penalty,
        fit_score=fit_value,
        fit_source=fit_source,
        audience_match=match,
        geo_share=1.0 if geo_share is None else geo_share,
        details=details,
    )
