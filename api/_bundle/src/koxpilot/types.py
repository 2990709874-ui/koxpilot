# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/types.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
"""KOXPilot 跨模块数据契约（dataclass + 类型别名）。

设计意图
--------
达人对象（Kox）刻意保持为 ``dict[str, Any]``：它是 SPEC 3.2 的 compact JSON schema，
前端 TypeScript 会**逐字段**消费同一份文件，用 dataclass 反而会引入两套字段名风险。
而**引擎内部产物**（判定理由、门禁结果、预算方案）全部用 dataclass，
保证 Python 侧字段名固定、可 asdict 落盘、可被 pytest 静态检查。
"""

from __future__ import annotations

# --- build_bundle.py 注入：Python 3.8 运行期泛型兼容别名（注解位置无需替换）---
from typing import (  # noqa: E402
    Dict as _py38_dict,
    FrozenSet as _py38_frozenset,
    List as _py38_list,
    Set as _py38_set,
    Tuple as _py38_tuple,
    Type as _py38_type,
)

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Kox = _py38_dict[str, Any]
Verdict = Literal["pass", "review", "reject"]
Severity = Literal["low", "medium", "high"]

#: ``target_gender`` 的归一表。
#:
#: 为什么必须有：达人侧的 ``audience_gender`` 只有 ``f`` / ``m`` 两个 key，
#: 而 brief 解析 Prompt 允许模型输出 ``all``（"不限性别"）。若原样存进 spec，
#: G2.6 会去 profile 里取 ``all`` 这个不存在的 key，拿到 **0.0**，
#: 于是"没有性别要求"被当成"受众完全不匹配"——达人被白白扣分，且没有任何报错。
#: 归一到 ``None`` 才符合 CampaignSpec 的既有语义（空 = 该维度不做约束）。
_GENDER_ALIASES: dict[str, str] = {
    "f": "f",
    "female": "f",
    "women": "f",
    "w": "f",
    "m": "m",
    "male": "m",
    "men": "m",
}

# ---------------------------------------------------------------------------
# campaign 侧契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignSpec:
    """A1 BriefAgent 的结构化输出，也是门禁 / 预算的唯一 campaign 输入。

    所有字段都可为空：为空表示"该维度不做 campaign 条件约束"，
    此时相关规则自动降级（见 gates/g2.py 的 neutral 语义）。
    这是评测能对齐 gt 的关键——gt.verdict 是 campaign 无关的库级判定。
    """

    campaign_id: str = "neutral"
    name: str = "库级中性画像（campaign 无关）"
    target_categories: tuple[str, ...] = ()
    target_markets: tuple[str, ...] = ()       # ISO 国家码
    target_languages: tuple[str, ...] = ()
    target_age_buckets: tuple[str, ...] = ()   # 与 audience_age 的 key 对齐
    target_gender: str | None = None           # "f" | "m" | None
    budget_usd: float = 0.0
    kpi: str = "balanced"                      # conversion | reach | engagement | balanced
    platforms: tuple[str, ...] = ()
    competitor_brands: tuple[str, ...] = ()
    regulated_category: str | None = None      # medical | finance | kids | None
    raw_text: str = ""

    @property
    def is_neutral(self) -> bool:
        return not (self.target_categories or self.target_markets or self.target_languages)

    def __post_init__(self) -> None:
        """把 ``target_gender`` 归一到 ``f`` / ``m`` / ``None``。

        无法识别的取值（``all`` / ``unisex`` / 拼错）一律降级为 ``None``（不做约束），
        而**不是**原样留着。理由：留着会让 G2.6 的性别重叠度稳定算出 0.0，
        这是一种没有任何报错、只会让判定悄悄变严的失效方式；
        降级为"不约束"至少是可解释、可预期的。
        """
        norm = _GENDER_ALIASES.get(str(self.target_gender or "").strip().lower())
        if norm != self.target_gender:
            object.__setattr__(self, "target_gender", norm)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CampaignSpec:
        """从 briefs.json 的 ``spec`` 块构造（容忍缺字段，多余字段忽略）。"""

        def tup(key: str) -> tuple[str, ...]:
            value = payload.get(key) or []
            return tuple(str(v) for v in value)

        return cls(
            campaign_id=str(payload.get("campaign_id", "neutral")),
            name=str(payload.get("name", "")),
            target_categories=tup("target_categories"),
            target_markets=tup("target_markets"),
            target_languages=tup("target_languages"),
            target_age_buckets=tup("target_age_buckets"),
            target_gender=payload.get("target_gender"),
            budget_usd=float(payload.get("budget_usd") or 0.0),
            kpi=str(payload.get("kpi", "balanced")),
            platforms=tup("platforms"),
            competitor_brands=tup("competitor_brands"),
            regulated_category=payload.get("regulated_category"),
            raw_text=str(payload.get("raw_text", "")),
        )


NEUTRAL_SPEC = CampaignSpec()

# ---------------------------------------------------------------------------
# 门禁侧契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reason:
    """一条门禁判定证据。前端证据链抽屉直接渲染这个结构。

    字段语义（面试会被逐个问，所以固定死）：
      gate       所属层（G0/G1/G2/G3）
      rule_id    规则号（G1.2 等），与 SPEC 第 4 节表格一一对应
      signal     被检查的可观测信号名（== kox JSON 里的字段名，可下钻）
      actual     该达人的实际值
      threshold  触发阈值（分位数标定值或显式 policy 常量）
      weight     该条命中在本层扣分中的权重
      human_text 中文人话，一句话说清"为什么被判"
      severity   hard = 计入硬信号计数 / soft = 只扣分 / block = 硬阻断
      source     阈值来源，如 "quantile:group:tiktok|micro" 或 "policy:spec_4.G1.7"
      depth      **越界深度** 0~1：0 = 刚好压线，1 = 深入尾部（证据饱和）。
                 weight 已按 depth 做过分级放大（见 gates/policy.GRADED_PENALTY_GAIN），
                 前端可以直接用 depth 画"证据强度条"。
    """

    gate: str
    rule_id: str
    signal: str
    actual: float | str | None
    threshold: float | str | None
    weight: float
    human_text: str
    severity: Literal["hard", "soft", "block"] = "soft"
    source: str = ""
    depth: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GateResult:
    """``evaluate(kox, campaign_spec, thresholds)`` 的返回值（纯函数、无副作用）。

    fraud_score 是**连续**异常分（0~1），专门给 AUC 用；
    verdict 是离散三分类。二者分离是为了让 PR 曲线/AUC 有意义。
    """

    kox_id: str
    verdict: Verdict
    reasons: tuple[Reason, ...]
    completeness: float
    authenticity_score: float
    consistency_score: float
    brand_safety_score: float
    fraud_score: float
    fit_score: float
    fit_source: str
    hard_hits: tuple[str, ...]
    blocked_by: str | None = None
    group_key: str = ""

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(r.rule_id for r in self.reasons)

    def has_rule(self, rule_id: str) -> bool:
        return any(r.rule_id == rule_id for r in self.reasons)

    def gate_reasons(self, gate: str) -> tuple[Reason, ...]:
        return tuple(r for r in self.reasons if r.gate == gate)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kox_id": self.kox_id,
            "verdict": self.verdict,
            "group_key": self.group_key,
            "scores": {
                "completeness": round(self.completeness, 4),
                "authenticity": round(self.authenticity_score, 4),
                "consistency": round(self.consistency_score, 4),
                "brand_safety": round(self.brand_safety_score, 4),
                "fraud_score": round(self.fraud_score, 4),
                "fit_score": round(self.fit_score, 4),
            },
            "fit_source": self.fit_source,
            "hard_hits": list(self.hard_hits),
            "blocked_by": self.blocked_by,
            "reasons": [r.to_dict() for r in self.reasons],
        }


# ---------------------------------------------------------------------------
# 预算侧契约
# ---------------------------------------------------------------------------


@dataclass()
class Allocation:
    """单个达人的预算分配结果。"""

    kox_id: str
    handle: str
    platform: str
    country: str
    bucket: str
    amount_usd: float
    posts: int
    effective_posts: float         # 等效条数（重复触达衰减后），用于算曝光与 CPM
    price_estimated: bool
    est_views: float               # 名义曝光（平台口径 avg_views）
    est_effective_views: float     # 有效曝光 = 名义 × authenticity_discount（扣水后）
    est_engagements: float
    value_score: float
    efficiency: float          # value / cost
    fit_score: float
    authenticity_discount: float
    audience_match: float
    kpi_weight: float = 1.0
    verdict: str = "pass"
    picked_by: str = "greedy"      # greedy | repair_longtail | fill | baseline

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("amount_usd", "est_views", "est_effective_views", "est_engagements"):
            d[k] = round(d[k], 2)
        for k in ("value_score", "efficiency", "fit_score", "authenticity_discount", "audience_match"):
            d[k] = round(d[k], 6)
        return d


@dataclass()
class BudgetPlan:
    """A5 BudgetAgent 输出。含约束校验报告，方便 pytest 直接断言。"""

    campaign_id: str
    budget_usd: float
    spent_usd: float
    selected: list[Allocation] = field(default_factory=list)
    est_total_views: float = 0.0
    est_effective_views: float = 0.0
    est_total_engagements: float = 0.0
    est_cpm_usd: float = 0.0
    est_cpe_usd: float = 0.0
    tier_mix: dict[str, float] = field(default_factory=dict)
    country_mix: dict[str, float] = field(default_factory=dict)
    platform_mix: dict[str, float] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    n_price_estimated: int = 0
    candidate_pool: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    strategy: str = "koxpilot"
    n_posts: int = 0
    purchase_model: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "strategy": self.strategy,
            "budget_usd": round(self.budget_usd, 2),
            "spent_usd": round(self.spent_usd, 2),
            "utilization": round(self.spent_usd / self.budget_usd, 4) if self.budget_usd else 0.0,
            "n_selected": len(self.selected),
            "n_posts": self.n_posts,
            "purchase_model": self.purchase_model,
            "candidate_pool": self.candidate_pool,
            "n_price_estimated": self.n_price_estimated,
            "est_total_views": round(self.est_total_views, 0),
            "est_effective_views": round(self.est_effective_views, 0),
            "est_total_engagements": round(self.est_total_engagements, 0),
            "est_cpm_usd": round(self.est_cpm_usd, 3),
            "est_cpe_usd": round(self.est_cpe_usd, 4),
            "tier_mix": {k: round(v, 4) for k, v in self.tier_mix.items()},
            "country_mix": {k: round(v, 4) for k, v in self.country_mix.items()},
            "platform_mix": {k: round(v, 4) for k, v in self.platform_mix.items()},
            "constraints": self.constraints,
            "trace": self.trace,
            "selected": [a.to_dict() for a in self.selected],
            "skipped_sample": self.skipped[:20],
        }
