"""预算分配：候选池、价值/成本模型、贪心与配额约束（SPEC 第 5 节）。

测试策略
--------
预算这一层最容易出的错，是**"看起来花完了钱、结构也好看，但账是假的"**：
- 缺报价的号被当成 0 成本，于是性价比无穷大排在最前（这类系统的经典事故）；
- 重复触达按线性叠加，曝光被虚报；
- 约束报告里 `satisfied` 恒为 True——**因为分母口径悄悄变了**；
- 反事实基线偷偷用了不一样的定向口径，于是"我们比基线好"只是口径差。

所以断言分两类：
1. **可复算**：value / cost / 等效条数 / 各项占比全部在测试里手算一遍再对，
   占比一律用 `_plan_shares()` 从 `plan.selected` 独立重算，不复用 allocator 自己的增量计数器。
2. **不可作弊**：约束报告必须能被独立重算推翻；不可满足的配额必须**如实报违规**，
   不许靠"把方案退成空的"让比例型约束的分母归零来伪装合规
   （`TestInfeasibleConstraints` 是这个真实缺陷的回归测试）。

分配器测试主要用手工 `Candidate`（不经过门禁），这样能精确构造"约束一定会打架"的场景；
真实数据用来跑端到端与结构性断言。所有断言都不绑 `output/budget.json` 里的具体金额。
"""

from __future__ import annotations

from typing import Any

import pytest

from koxpilot.budget.allocator import (
    Selection,
    Slot,
    allocate,
    build_slots,
    validate_plan,
)
from koxpilot.budget.planner import ALL_VERDICTS, plan_baseline, plan_campaign
from koxpilot.budget.policy import (
    COUNTRY_MAX_SHARE,
    HEAD_MAX_SHARE,
    INCLUDE_REVIEW_BY_DEFAULT,
    KPI_EXPONENTS,
    LONGTAIL_MIN_SHARE,
    MAX_POSTS_PER_KOX,
    MIN_UTILIZATION_TARGET,
    POST_DECAY_SCAN,
    POST_MARGINAL_DECAY,
    RATIO_CLAMP,
    SINGLE_KOX_MAX_SHARE,
    constraint_snapshot,
    effective_posts,
)
from koxpilot.budget.value import (
    Candidate,
    build_candidates,
    estimate_cost,
    kpi_weight_of,
    targeting_reason,
)
from koxpilot.gates.thresholds import Thresholds
from koxpilot.taxonomy import HEAD_BUCKETS, LONGTAIL_BUCKETS
from koxpilot.types import CampaignSpec

NEUTRAL = CampaignSpec()
STRUCTURAL = ("head_max_share", "longtail_min_share", "country_max_share")


# ---------------------------------------------------------------------------
# 手工候选工厂
# ---------------------------------------------------------------------------
def cand(
    kox_id: str,
    *,
    bucket: str = "micro",
    country: str = "US",
    cost: float = 1_000.0,
    value: float = 100_000.0,
    followers: float = 50_000.0,
    platform: str = "tiktok",
    verdict: str = "pass",
    discount: float = 1.0,
) -> Candidate:
    return Candidate(
        kox_id=kox_id,
        handle=f"@{kox_id.lower()}",
        platform=platform,
        country=country,
        bucket=bucket,
        group=f"{platform}|{bucket}",
        verdict=verdict,
        followers=followers,
        avg_views=value / 2.0,
        engagement_rate=0.05,
        cost_usd=cost,
        price_estimated=False,
        authenticity_discount=discount,
        fit_score=1.0,
        audience_match=1.0,
        kpi_weight=1.0,
        value=value,
        efficiency=value / cost,
    )


#: 多国长尾池：结构约束在物理上可满足，用来测"配额真的被执行了"。
def longtail_pool(n: int, *, cost: float = 500.0, value: float = 50_000.0) -> list[Candidate]:
    countries = ("JP", "DE", "BR", "KR")
    return [
        cand(
            f"L{i:02d}",
            bucket="nano",
            country=countries[i % len(countries)],
            cost=cost,
            value=value,
            followers=5_000,
        )
        for i in range(n)
    ]


def _plan_shares(plan: Any) -> dict[str, float]:
    """独立重算三项占比——**不复用** `Selection` 的增量计数器。

    这是刻意的：只信 allocator 自己维护的 head_usd/longtail_usd，
    一旦增量更新写错（例如 give_back 忘了减），约束报告就会"自证合规"。
    """
    total = sum(a.amount_usd for a in plan.selected)
    if total <= 0:
        return {"head": 0.0, "longtail": 0.0, "max_country": 0.0, "max_single": 0.0}
    head = sum(a.amount_usd for a in plan.selected if a.bucket in HEAD_BUCKETS)
    tail = sum(a.amount_usd for a in plan.selected if a.bucket in LONGTAIL_BUCKETS)
    by_country: dict[str, float] = {}
    for a in plan.selected:
        by_country[a.country] = by_country.get(a.country, 0.0) + a.amount_usd
    return {
        "head": head / total,
        "longtail": tail / total,
        "max_country": max(by_country.values()) / total,
        "max_single": max(a.amount_usd for a in plan.selected) / total,
    }


def _checks(plan: Any) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in plan.constraints["checks"]}


# ===========================================================================
# 采购模型：按"条"买
# ===========================================================================
class TestPurchaseModel:
    def test_effective_posts_is_geometric_sum(self) -> None:
        assert effective_posts(0) == 0.0
        assert effective_posts(1) == 1.0
        assert effective_posts(2, 0.7) == pytest.approx(1.7)
        assert effective_posts(3, 0.7) == pytest.approx(1.0 + 0.7 + 0.49)
        assert effective_posts(3, 1.0) == pytest.approx(3.0), "decay=1 时退化为线性叠加"

    def test_effective_posts_is_strictly_below_linear(self) -> None:
        """等效条数必须严格小于条数——否则就是把重复触达当新增触达，即虚报曝光。"""
        for n in range(2, 6):
            assert effective_posts(n, POST_MARGINAL_DECAY) < n

    def test_negative_posts_do_not_blow_up(self) -> None:
        assert effective_posts(-3) == 0.0

    def test_marginal_efficiency_strictly_decreases_in_index(self) -> None:
        """凹性前提：同一达人第 n 条的边际性价比随 n 严格递减。

        这是"按边际性价比全局降序取名额"能自动保证"先买第 n-1 条"的**唯一依据**。
        若哪天有人把 decay 改成 >1（或让第二条更便宜），贪心的正确性前提就没了。
        """
        c = cand("K1")
        effs = [Slot(c, i, POST_MARGINAL_DECAY).marginal_efficiency for i in range(1, 6)]
        assert all(a > b for a, b in zip(effs, effs[1:])), effs

    def test_build_slots_rejects_non_concave_decay(self) -> None:
        for bad in (1.5, 0.0, -0.1):
            with pytest.raises(ValueError):
                build_slots([cand("K1")], decay=bad)

    def test_build_slots_shape(self) -> None:
        slots = build_slots([cand("K1"), cand("K2")], max_posts=3)
        assert len(slots) == 6
        assert sorted({s.index for s in slots}) == [1, 2, 3]
        assert all(s.cost_usd == 1_000.0 for s in slots)


# ===========================================================================
# 定向筛选（与"选人依据"必须分离）
# ===========================================================================
class TestTargeting:
    def base(self, **over: Any) -> dict[str, Any]:
        rec = {
            "kox_id": "K",
            "platform": "tiktok",
            "country": "US",
            "declared_categories": ["beauty"],
            "observed_categories": ["beauty"],
            "audience_geo": {"US": 0.8, "other": 0.2},
        }
        rec.update(over)
        return rec

    def test_neutral_spec_filters_nothing(self) -> None:
        assert targeting_reason(self.base(), NEUTRAL) is None

    def test_platform_filter(self) -> None:
        spec = CampaignSpec(platforms=("youtube",))
        assert targeting_reason(self.base(), spec) == "platform_off_target"
        assert targeting_reason(self.base(platform="youtube"), spec) is None

    def test_category_filter_accepts_adjacent_and_declared(self) -> None:
        """相邻品类算在射程内，自称命中也算——定向不做质量判断（那是 G2.1 的活）。"""
        spec = CampaignSpec(target_categories=("3c_digital",))
        assert targeting_reason(self.base(), spec) == "category_off_target"
        assert targeting_reason(self.base(observed_categories=["3c_digital"]), spec) is None
        assert (
            targeting_reason(
                self.base(declared_categories=["3c_digital"], observed_categories=[]), spec
            )
            is None
        ), "自称命中也应进入射程（是否可信交给门禁）"

    def test_market_filter_accepts_any_positive_share(self) -> None:
        spec = CampaignSpec(target_markets=("JP",))
        assert targeting_reason(self.base(), spec) == "market_off_target"
        assert targeting_reason(self.base(country="JP"), spec) is None
        assert targeting_reason(self.base(audience_geo={"JP": 0.01, "US": 0.99}), spec) is None

    def test_missing_geo_is_not_filtered_out(self) -> None:
        """受众地域缺失不该被定向砍掉：那是完整性问题（G0 管），在这里砍会重复惩罚并掩盖缺数。"""
        spec = CampaignSpec(target_markets=("JP",))
        assert targeting_reason(self.base(audience_geo=None), spec) is None
        assert targeting_reason(self.base(audience_geo={}), spec) is None

    def test_targeting_is_shared_between_arms(
        self, records: list[dict[str, Any]], thresholds: Thresholds, spec: CampaignSpec
    ) -> None:
        """两臂必须共用同一套定向筛选，否则"我们比基线好"可能只是口径差。

        断言方式：两臂 skipped 里**定向类原因**的 kox_id 集合必须完全一致，
        差异只允许出现在 gate_* 这类原因上。
        """
        codes = {"platform_off_target", "category_off_target", "market_off_target"}
        plan_a, results = plan_campaign(records, spec, thresholds)
        plan_b = plan_baseline(records, spec, thresholds, results)
        a = {s["kox_id"] for s in plan_a.skipped if s["reason"] in codes}
        b = {s["kox_id"] for s in plan_b.skipped if s["reason"] in codes}
        assert a == b, f"两臂定向口径不一致，差异 {len(a ^ b)} 人"
        assert a, "定向没有筛掉任何人，这个对照没有意义"


# ===========================================================================
# 价值 / 成本模型
# ===========================================================================
class TestValueModel:
    @pytest.fixture()
    def thr(self) -> Thresholds:
        return Thresholds(
            global_={
                "engagement_rate": {"median": 0.05, "grid": []},
                "comment_like_ratio": {"median": 0.10, "grid": []},
                "avg_cpm_usd": {"p50": 20.0, "grid": []},
            }
        )

    def test_quoted_price_wins_and_is_not_marked_estimated(self, thr: Thresholds) -> None:
        assert estimate_cost({"quoted_price_usd": 3_000.0, "avg_views": 100_000}, thr, "g") == (
            3_000.0,
            False,
        )

    def test_price_estimated_from_group_cpm(self, thr: Thresholds) -> None:
        """报价缺失按同组 CPM 中位数估算，并**必须打上估算标记**。"""
        cost, est = estimate_cost({"quoted_price_usd": None, "avg_views": 100_000}, thr, "g")
        assert cost == pytest.approx(20.0 * 100_000 / 1000.0)
        assert est is True

    def test_unpriceable_returns_none_not_zero(self, thr: Thresholds) -> None:
        """无法定价必须返回 None。返回 0 会让缺价号性价比无穷大、排在最前——经典事故。"""
        assert estimate_cost({"quoted_price_usd": None, "avg_views": None}, thr, "g") == (None, True)
        assert estimate_cost({"quoted_price_usd": 0, "avg_views": 0}, thr, "g") == (None, True)
        # 有曝光但该组没有 CPM 阈值可用 → 同样不许瞎猜
        assert estimate_cost({"quoted_price_usd": None, "avg_views": 100_000}, Thresholds(), "g") == (
            None,
            True,
        )

    def test_bool_is_not_a_number(self, thr: Thresholds) -> None:
        """True 在 Python 里是 1——报价字段被写成布尔时必须当缺失，不能当 $1。"""
        cost, est = estimate_cost({"quoted_price_usd": True, "avg_views": 100_000}, thr, "g")
        assert (cost, est) == (pytest.approx(2_000.0), True)

    def test_kpi_weight_formula_is_reproducible(self, thr: Thresholds) -> None:
        """逐档复算 kpi_weight = er_ratio**a × clr_ratio**b（两项都取中位数的 2 倍）。"""
        rec = {"engagement_rate": 0.10, "comment_like_ratio": 0.20}
        for kpi, (a, b) in KPI_EXPONENTS.items():
            got = kpi_weight_of(rec, CampaignSpec(kpi=kpi), thr, "g")
            assert got == pytest.approx(round((2.0**a) * (2.0**b), 6), abs=1e-6), kpi

    def test_reach_kpi_ignores_engagement(self, thr: Thresholds) -> None:
        """reach = 只要曝光，不给互动加成，否则"要量"的单子会被互动率反向排序。"""
        spec = CampaignSpec(kpi="reach")
        hot = kpi_weight_of({"engagement_rate": 0.5, "comment_like_ratio": 0.5}, spec, thr, "g")
        cold = kpi_weight_of({"engagement_rate": 0.01, "comment_like_ratio": 0.01}, spec, thr, "g")
        assert hot == cold == 1.0

    def test_kpi_weight_is_clamped(self, thr: Thresholds) -> None:
        """极端号不许靠 kpi_weight 反超优质号（它本身往往就是水号形态）。"""
        lo, hi = RATIO_CLAMP
        spec = CampaignSpec(kpi="engagement")
        wild = kpi_weight_of({"engagement_rate": 5.0, "comment_like_ratio": 0.1}, spec, thr, "g")
        dead = kpi_weight_of({"engagement_rate": 0.0005, "comment_like_ratio": 0.1}, spec, thr, "g")
        assert wild == pytest.approx(hi)
        assert dead == pytest.approx(lo)

    def test_missing_signal_is_neutral_not_zero(self, thr: Thresholds) -> None:
        """信号缺失时权重取 1.0（不奖不罚）；取 0 会让缺数据的号价值直接归零。"""
        spec = CampaignSpec(kpi="balanced")
        assert kpi_weight_of({}, spec, thr, "g") == 1.0
        assert kpi_weight_of({"engagement_rate": None}, spec, thr, "g") == 1.0
        assert kpi_weight_of({"engagement_rate": 0.10}, spec, thr, "g") == pytest.approx(
            round(2.0**0.5, 6)
        ), "一项缺失只应中性化该项，不影响另一项"

    def test_unknown_kpi_falls_back_to_balanced(self, thr: Thresholds) -> None:
        rec = {"engagement_rate": 0.10, "comment_like_ratio": 0.20}
        assert kpi_weight_of(rec, CampaignSpec(kpi="nonsense"), thr, "g") == kpi_weight_of(
            rec, CampaignSpec(kpi="balanced"), thr, "g"
        )


# ===========================================================================
# 候选池
# ===========================================================================
class TestCandidatePool:
    def test_default_pool_only_takes_pass(
        self, records: list[dict[str, Any]], results: dict[str, Any], thresholds: Thresholds
    ) -> None:
        """默认只收 pass：review 的产品语义是"待人核"，自动分配不该替人拍板。"""
        assert INCLUDE_REVIEW_BY_DEFAULT is False
        pool, skipped = build_candidates(records, results, NEUTRAL, thresholds)
        assert pool, "候选池为空，后面的断言没有意义"
        assert {c.verdict for c in pool} == {"pass"}
        assert {"gate_review", "gate_reject"} <= {s["reason"] for s in skipped}

    def test_every_record_is_either_in_pool_or_explained(
        self, records: list[dict[str, Any]], results: dict[str, Any], thresholds: Thresholds
    ) -> None:
        """不许有人"凭空消失"——产品要能回答"为什么没选它"。"""
        pool, skipped = build_candidates(records, results, NEUTRAL, thresholds)
        assert len(pool) + len(skipped) == len(records), "有 kox_id 同时进池又被跳过（或漏掉了）"
        assert {c.kox_id for c in pool} | {s["kox_id"] for s in skipped} == {
            r["kox_id"] for r in records
        }
        assert all(s["reason"] for s in skipped)

    def test_value_formula_is_reproducible(
        self, records: list[dict[str, Any]], results: dict[str, Any], thresholds: Thresholds
    ) -> None:
        """逐条复算 value = views × authenticity × fit × audience × kpi_weight。"""
        pool, _ = build_candidates(records, results, NEUTRAL, thresholds)
        for c in pool:
            expected = c.avg_views * c.authenticity_discount * c.fit_score * c.audience_match * c.kpi_weight
            assert c.value == pytest.approx(expected, rel=1e-9), c.kox_id
            assert c.efficiency == pytest.approx(c.value / c.cost_usd, rel=1e-9), c.kox_id

    def test_no_zero_or_negative_cost_in_pool(
        self, records: list[dict[str, Any]], results: dict[str, Any], thresholds: Thresholds
    ) -> None:
        pool, _ = build_candidates(records, results, NEUTRAL, thresholds)
        assert all(c.cost_usd > 0 for c in pool)
        assert all(c.avg_views > 0 for c in pool)
        assert all(c.efficiency > 0 for c in pool)

    def test_include_verdicts_widens_pool_monotonically(
        self, records: list[dict[str, Any]], results: dict[str, Any], thresholds: Thresholds
    ) -> None:
        strict, _ = build_candidates(records, results, NEUTRAL, thresholds, ("pass",))
        wide, _ = build_candidates(records, results, NEUTRAL, thresholds, ALL_VERDICTS)
        assert {c.kox_id for c in strict} < {c.kox_id for c in wide}

    def test_records_without_gate_result_are_skipped_not_assumed_pass(
        self, records: list[dict[str, Any]], results: dict[str, Any], thresholds: Thresholds
    ) -> None:
        """门禁结果缺失必须出池。当成 pass 会让"根本没跑门禁的人"直接拿到预算。"""
        partial = dict(list(results.items())[:100])
        pool, skipped = build_candidates(records[:200], partial, NEUTRAL, thresholds)
        assert "no_gate_result" in {s["reason"] for s in skipped}
        assert all(c.kox_id in partial for c in pool)

    def test_pass_tier_carries_no_authenticity_discount(
        self, records: list[dict[str, Any]], results: dict[str, Any], thresholds: Thresholds
    ) -> None:
        """默认池（只收 pass）里真实性折扣恒为 1.0——这是**门禁口径的结构性后果**，值得锁住。

        任何真实性扣分都会把判定推到 review 以下，所以"pass 且被打折"在当前口径下不存在。
        若哪天有被打折的号能拿到 pass，这条会红：那时要么是 G1 惩罚漏传，
        要么是 pass 的门槛被放松了，两种都必须被人看见。
        """
        pool, _ = build_candidates(records, results, NEUTRAL, thresholds)
        assert {c.authenticity_discount for c in pool} == {1.0}

    def test_authenticity_discount_actually_reduces_value(
        self, records: list[dict[str, Any]], results: dict[str, Any], thresholds: Thresholds
    ) -> None:
        """放开 review 后必须出现被打折的号，且折扣要**真的**压低 value（不是记录了不用）。"""
        pool, _ = build_candidates(records, results, NEUTRAL, thresholds, ("pass", "review"))
        discounted = [c for c in pool if c.authenticity_discount < 1.0]
        assert discounted, "放开 review 后仍没有任何被打折的号，说明折扣没接进候选池"
        for c in discounted:
            undiscounted = c.avg_views * c.fit_score * c.audience_match * c.kpi_weight
            assert c.value < undiscounted, f"{c.kox_id} 的折扣没有作用到 value"
            assert c.value == pytest.approx(undiscounted * c.authenticity_discount, rel=1e-9)


# ===========================================================================
# 分配器：约束执行
# ===========================================================================
class TestAllocatorConstraints:
    def test_empty_pool_and_zero_budget_are_safe(self) -> None:
        assert allocate([], CampaignSpec(budget_usd=10_000)).spent_usd == 0.0
        assert allocate([cand("K1")], CampaignSpec(budget_usd=0)).spent_usd == 0.0
        assert allocate([], CampaignSpec(budget_usd=10_000)).selected == []

    def test_never_overspends(self) -> None:
        pool = [cand(f"K{i}", cost=700.0, value=100_000 - i) for i in range(40)]
        plan = allocate(pool, CampaignSpec(budget_usd=10_000))
        assert plan.spent_usd <= plan.budget_usd + 1e-6
        assert plan.constraints["utilization"] <= 1.0 + 1e-9

    def test_single_kox_cap_is_enforced(self) -> None:
        """只有 3 个候选、其中一个价值极高时，贪心会想把钱全砸给它——单人上限必须拦住。"""
        pool = [
            cand("K1", value=1_000_000, cost=1_000),
            cand("K2", bucket="nano", value=10, cost=1_000),
            cand("K3", bucket="nano", value=10, cost=1_000),
        ]
        plan = allocate(pool, CampaignSpec(budget_usd=100_000))
        by_id = {a.kox_id: a for a in plan.selected}
        assert by_id["K1"].amount_usd <= SINGLE_KOX_MAX_SHARE * plan.budget_usd + 1e-6
        assert by_id["K1"].posts <= MAX_POSTS_PER_KOX
        assert _checks(plan)["single_kox_max_share"]["satisfied"] is True

    def test_head_and_longtail_quota_are_enforced(self) -> None:
        """头部全是高性价比时贪心天然想全押头部——配额必须把它拉回来。

        池子刻意做成"结构上可满足"（多国长尾充足），所以这里可以要求**全部约束满足**。
        """
        pool = [
            cand(f"H{i}", bucket="mega", country="US", value=500_000, cost=2_000, followers=5e6)
            for i in range(10)
        ]
        pool += longtail_pool(60)
        plan = allocate(pool, CampaignSpec(budget_usd=60_000))
        shares = _plan_shares(plan)
        assert shares["head"] <= HEAD_MAX_SHARE + 1e-6, shares
        assert shares["longtail"] >= LONGTAIL_MIN_SHARE - 1e-6, shares
        assert plan.constraints["all_enforced_satisfied"] is True
        # 配额确实"咬住"了：不然 10 个 mega × 3 条全买得下（$60,000 刚好等于预算）
        mega_posts = sum(a.posts for a in plan.selected if a.bucket == "mega")
        assert 0 < mega_posts < 30, f"头部上限没有真正起作用（mega_posts={mega_posts}）"

    def test_country_cap_is_enforced(self) -> None:
        pool = [cand(f"U{i}", country="US", value=200_000, cost=1_000) for i in range(30)]
        pool += [
            cand(f"J{i}", country="JP", bucket="nano", value=100_000, cost=1_000) for i in range(30)
        ]
        plan = allocate(pool, CampaignSpec(budget_usd=40_000))
        shares = _plan_shares(plan)
        assert shares["max_country"] <= COUNTRY_MAX_SHARE + 1e-6, shares
        assert len({a.country for a in plan.selected}) > 1, "地域上限应当强制方案跨国"

    def test_constraint_report_matches_independent_recount(self) -> None:
        """约束报告不许自证：用从 `selected` 独立重算的占比去对 `checks` 里的 actual。"""
        pool = [
            cand(f"H{i}", bucket="macro", country="US", value=400_000, cost=1_500, followers=1e6)
            for i in range(15)
        ]
        pool += [cand(f"M{i}", country="US", value=120_000, cost=600) for i in range(40)]
        pool += longtail_pool(40, cost=300, value=60_000)
        plan = allocate(pool, CampaignSpec(budget_usd=50_000))
        shares = _plan_shares(plan)
        checks = _checks(plan)
        assert checks["head_max_share"]["actual"] == pytest.approx(shares["head"], abs=1e-4)
        assert checks["longtail_min_share"]["actual"] == pytest.approx(shares["longtail"], abs=1e-4)
        assert checks["country_max_share"]["actual"] == pytest.approx(shares["max_country"], abs=1e-4)
        assert checks["total_budget"]["actual"] == pytest.approx(
            round(sum(a.amount_usd for a in plan.selected), 2), abs=0.02
        )
        assert checks["single_kox_max_share"]["actual"] == pytest.approx(
            max(a.amount_usd for a in plan.selected) / plan.budget_usd, abs=1e-4
        )

    def test_violations_list_is_derived_from_checks(self) -> None:
        """violations 必须与 checks 完全一致，不能是另算一遍的"第二本账"。"""
        pool = [cand(f"H{i}", bucket="mega", value=500_000, cost=2_000, followers=5e6) for i in range(30)]
        plan = allocate(pool, CampaignSpec(budget_usd=50_000))
        checks = _checks(plan)
        expected = [n for n, c in checks.items() if c["enforced"] and not c["satisfied"]]
        assert sorted(plan.constraints["violations"]) == sorted(expected)
        assert plan.constraints["all_enforced_satisfied"] == (not expected)

    def test_validate_plan_strict_flag_controls_enforcement(self) -> None:
        sel = Selection(budget=10_000)
        strict = validate_plan(sel, True)
        loose = validate_plan(sel, False)
        assert sum(c["enforced"] for c in strict["checks"]) == 5
        assert sum(c["enforced"] for c in loose["checks"]) == 1
        assert {c["name"] for c in loose["checks"] if c["enforced"]} == {"total_budget"}

    def test_constraint_snapshot_matches_policy_constants(self) -> None:
        """快照落盘给前端与文档用；两处各写死一份同样的数字才是真正的隐患。"""
        snap = constraint_snapshot()
        assert snap == {
            "single_kox_max_share": SINGLE_KOX_MAX_SHARE,
            "head_max_share": HEAD_MAX_SHARE,
            "longtail_min_share": LONGTAIL_MIN_SHARE,
            "country_max_share": COUNTRY_MAX_SHARE,
            "min_utilization_target": MIN_UTILIZATION_TARGET,
            "max_posts_per_kox": MAX_POSTS_PER_KOX,
            "post_marginal_decay": POST_MARGINAL_DECAY,
        }
        assert allocate([], CampaignSpec(budget_usd=1)).constraints["policy"] == snap


# ===========================================================================
# 不可满足的配额：必须如实报违规，不许"退成空方案"伪装合规
# ===========================================================================
class TestInfeasibleConstraints:
    """真实缺陷的回归测试（本轮修复）。

    原实现里三个修正循环的终止条件都是 `while sel.spent > 0 and <约束未满足>`，
    而比例型约束的分母是**实际花费**：
    当候选池只有一个国家时 `max_country_share ≡ 1.0`，退让永远改善不了它，
    循环于是一路退到 $0；此时 0/0 被算成 0.0，
    报告里 `country_max_share` 与 `head_max_share` 双双显示 **satisfied=True**，
    只剩 `longtail_min_share` 报红——即
    **"什么都不投 → 于是没有违规"，并且把违规指向了错误的约束**。
    单国 brief（例如只投美国）会稳定命中这条路径。

    修复后的口径：退让若不能改善结构就停手；修正整体若把方案变得更差则回滚；
    宁可交付"结构不达标但说清楚了"的方案，也不交付"没投所以不违规"的空方案。
    """

    def test_single_country_pool_keeps_plan_and_reports_the_real_violation(self) -> None:
        pool = [cand(f"K{i}", country="US") for i in range(40)]
        plan = allocate(pool, CampaignSpec(budget_usd=50_000))

        assert plan.selected, "整盘方案被退空了——这正是被修掉的缺陷"
        assert plan.spent_usd > 0
        checks = _checks(plan)
        assert checks["country_max_share"]["actual"] == pytest.approx(1.0)
        assert checks["country_max_share"]["satisfied"] is False
        assert plan.constraints["violations"] == ["country_max_share"]
        assert plan.constraints["all_enforced_satisfied"] is False
        # 报告的占比必须能被独立重算出来（而不是分母归零后的 0.0）
        assert _plan_shares(plan)["max_country"] == pytest.approx(1.0)
        assert any("单一国家" in t for t in plan.trace), plan.trace

    def test_head_only_pool_reports_head_violation_instead_of_vacuous_zero(self) -> None:
        pool = [cand(f"H{i}", bucket="mega", cost=2_000, value=500_000, followers=5e6) for i in range(30)]
        plan = allocate(pool, CampaignSpec(budget_usd=50_000))

        assert plan.selected and plan.spent_usd > 0
        checks = _checks(plan)
        assert checks["head_max_share"]["actual"] == pytest.approx(1.0)
        assert checks["head_max_share"]["satisfied"] is False
        assert "head_max_share" in plan.constraints["violations"]
        assert "longtail_min_share" in plan.constraints["violations"]
        assert _plan_shares(plan)["head"] == pytest.approx(1.0)

    def test_repair_never_returns_a_worse_plan_than_greedy(self) -> None:
        """安全网：修正阶段的产出不得比"贪心不修正"更差（更少违规 or 更高花费）。

        用一批结构上必然打架的池子扫一遍：每一个都要么达标，要么保留方案并如实报违规。
        """
        pools = {
            "all_us_micro": [cand(f"K{i}") for i in range(40)],
            "all_head": [
                cand(f"H{i}", bucket="mega", cost=2_000, value=400_000, followers=5e6)
                for i in range(20)
            ],
            "head_plus_one_nano": [
                cand(f"H{i}", bucket="macro", cost=2_000, value=400_000, followers=1e6)
                for i in range(20)
            ]
            + [cand("L0", bucket="nano", cost=100, value=1_000, followers=5_000)],
            "single_candidate": [cand("ONLY", cost=500)],
        }
        for name, pool in pools.items():
            plan = allocate(pool, CampaignSpec(budget_usd=50_000))
            assert plan.selected, f"{name}: 方案被退空"
            assert plan.spent_usd > 0, f"{name}: 花费退成 0"
            checks = _checks(plan)
            # 任何"满足"都必须是真的满足，而不是分母归零的假象
            for key, share_key in (
                ("head_max_share", "head"),
                ("country_max_share", "max_country"),
                ("longtail_min_share", "longtail"),
            ):
                assert checks[key]["actual"] == pytest.approx(
                    _plan_shares(plan)[share_key], abs=1e-4
                ), f"{name}/{key}"

    def test_infeasible_plan_still_respects_hard_budget(self) -> None:
        """结构不达标不能成为超预算的借口。"""
        pool = [cand(f"K{i}", cost=999.0) for i in range(80)]
        plan = allocate(pool, CampaignSpec(budget_usd=20_000))
        assert plan.spent_usd <= plan.budget_usd + 1e-6
        assert _checks(plan)["total_budget"]["satisfied"] is True


# ===========================================================================
# 基线臂
# ===========================================================================
class TestBaselineArm:
    def test_baseline_records_but_does_not_enforce_structure(self) -> None:
        """基线只受总预算约束，结构项**记录但不判定**——这正是"基线为什么翻车"的证据。"""
        pool = [cand(f"H{i}", bucket="mega", value=10, cost=2_000, followers=5e6) for i in range(30)]
        pool += longtail_pool(30, cost=200, value=999_999)
        plan = allocate(pool, CampaignSpec(budget_usd=40_000), strategy="followers")
        checks = _checks(plan)
        assert checks["total_budget"]["enforced"] is True
        for name in ("single_kox_max_share", *STRUCTURAL):
            assert checks[name]["enforced"] is False, name
        assert plan.spent_usd <= plan.budget_usd + 1e-6
        assert plan.strategy == "followers"

    def test_baseline_ignores_value_ordering(self) -> None:
        """基线的选人依据必须是粉丝量，不能偷偷用 value（否则对照没意义）。"""
        pool = [
            cand("BIG", bucket="mega", followers=9e6, value=1.0, cost=1_000),
            cand("SMALL", bucket="nano", followers=2_000, value=1e9, cost=1_000),
        ]
        plan = allocate(pool, CampaignSpec(budget_usd=1_000), strategy="followers")
        assert [a.kox_id for a in plan.selected] == ["BIG"]

    def test_baseline_violates_structure_on_real_data(
        self, records: list[dict[str, Any]], thresholds: Thresholds, spec: CampaignSpec
    ) -> None:
        """结构性证据：真实数据上基线会把钱堆到少数头部号，若这些约束"生效"就会违规。

        这条不比较任何具体金额，只断言"基线的结构指标里至少有一项越线"——
        否则整个"结构约束有用"的论证就是空的。
        """
        _, results = plan_campaign(records, spec, thresholds)
        plan = plan_baseline(records, spec, thresholds, results)
        checks = _checks(plan)
        would_violate = [n for n in ("single_kox_max_share", *STRUCTURAL) if not checks[n]["satisfied"]]
        assert would_violate, "基线居然天然满足全部结构约束，那结构约束就不构成差异"
        assert len(plan.selected) < 30, "基线应当集中在少数头部号上"

    def test_baseline_pool_contains_rejected_records(
        self, records: list[dict[str, Any]], thresholds: Thresholds, spec: CampaignSpec
    ) -> None:
        """基线必须"不看门禁"：入选里应当出现被判 reject 的号，否则反事实审计没有落差可算。"""
        _, results = plan_campaign(records, spec, thresholds)
        plan = plan_baseline(records, spec, thresholds, results)
        verdicts = {a.verdict for a in plan.selected}
        assert verdicts - {"pass"}, f"基线只选到 pass（{verdicts}），它并没有绕开门禁"


# ===========================================================================
# 分配器：算法性质
# ===========================================================================
class TestAllocatorAlgorithm:
    def test_deterministic_and_input_order_independent(self) -> None:
        pool = [cand(f"K{i:02d}", cost=500 + i, value=100_000 - 37 * i) for i in range(60)]
        a = allocate(pool, CampaignSpec(budget_usd=20_000))
        b = allocate(list(reversed(pool)), CampaignSpec(budget_usd=20_000))
        assert [(x.kox_id, x.posts) for x in a.selected] == [(x.kox_id, x.posts) for x in b.selected]
        assert a.spent_usd == pytest.approx(b.spent_usd)

    def test_ties_are_broken_deterministically(self) -> None:
        """完全同质的候选也必须有稳定顺序，否则每次跑出来的方案都不一样。"""
        pool = [cand(f"K{i:02d}", cost=1_000, value=100_000) for i in range(30)]
        a = [x.kox_id for x in allocate(pool, CampaignSpec(budget_usd=5_000)).selected]
        b = [x.kox_id for x in allocate(list(reversed(pool)), CampaignSpec(budget_usd=5_000)).selected]
        assert a == b == sorted(a)

    def test_posts_never_exceed_cap(self) -> None:
        pool = [cand("K1", cost=100, value=1e6), cand("K2", bucket="nano", cost=100, value=9e5)]
        plan = allocate(pool, CampaignSpec(budget_usd=100_000))
        assert all(a.posts <= MAX_POSTS_PER_KOX for a in plan.selected)
        assert all(a.posts >= 1 for a in plan.selected), "入选却买 0 条是无意义的记录"

    def test_amount_equals_posts_times_cost(self) -> None:
        pool = [cand(f"K{i}", cost=300 + i, value=50_000 + i) for i in range(30)]
        plan = allocate(pool, CampaignSpec(budget_usd=15_000))
        by_id = {c.kox_id: c for c in pool}
        for a in plan.selected:
            assert a.amount_usd == pytest.approx(a.posts * by_id[a.kox_id].cost_usd)
        assert plan.spent_usd == pytest.approx(sum(a.amount_usd for a in plan.selected))
        assert plan.n_posts == sum(a.posts for a in plan.selected)

    def test_views_use_effective_posts_not_raw_posts(self) -> None:
        """曝光必须按等效条数算；按原始条数算就是把重复触达当新增触达。"""
        plan = allocate([cand("K1", cost=100, value=100_000)], CampaignSpec(budget_usd=1_500))
        a = plan.selected[0]
        assert a.posts == 3
        eq = effective_posts(3, POST_MARGINAL_DECAY)
        assert a.effective_posts == pytest.approx(round(eq, 4))
        assert a.est_views == pytest.approx(50_000 * eq)
        assert a.est_views < 3 * 50_000, "等效曝光不该等于线性叠加"

    def test_effective_views_never_exceed_nominal(self) -> None:
        """有效曝光 = 名义曝光 × 真实性折扣，必须 ≤ 名义曝光。反过来就是虚报。"""
        pool = [cand(f"K{i}", cost=500, value=80_000, discount=0.6) for i in range(20)]
        plan = allocate(pool, CampaignSpec(budget_usd=8_000))
        assert plan.est_effective_views <= plan.est_total_views + 1e-6
        assert plan.est_effective_views == pytest.approx(0.6 * plan.est_total_views)
        for a in plan.selected:
            assert a.est_effective_views <= a.est_views + 1e-6

    def test_mix_shares_sum_to_one(self) -> None:
        pool = [cand(f"U{i}", country="US", cost=500, value=50_000) for i in range(10)]
        pool += [cand(f"J{i}", country="JP", bucket="nano", cost=300, value=40_000) for i in range(10)]
        plan = allocate(pool, CampaignSpec(budget_usd=6_000))
        for mix in (plan.tier_mix, plan.country_mix, plan.platform_mix):
            assert sum(mix.values()) == pytest.approx(1.0, abs=1e-4)
        assert set(plan.country_mix) == {a.country for a in plan.selected}

    def test_cpm_and_cpe_are_consistent(self) -> None:
        pool = [cand(f"K{i}", cost=400, value=60_000) for i in range(20)]
        plan = allocate(pool, CampaignSpec(budget_usd=8_000))
        assert plan.est_cpm_usd == pytest.approx(plan.spent_usd / (plan.est_total_views / 1000.0))
        assert plan.est_cpe_usd == pytest.approx(plan.spent_usd / plan.est_total_engagements)

    def test_every_selection_has_a_provenance_tag(self) -> None:
        """每个入选都要能说清是哪个阶段挑进来的（贪心/长尾修正/补位/基线）。"""
        pool = [
            cand(f"H{i}", bucket="macro", country="US", value=400_000, cost=1_500, followers=1e6)
            for i in range(15)
        ]
        pool += longtail_pool(40, cost=300, value=60_000)
        plan = allocate(pool, CampaignSpec(budget_usd=30_000))
        assert all(a.picked_by in {"greedy", "repair_longtail", "fill", "baseline"} for a in plan.selected)
        assert plan.trace, "没有 trace 就无法向采购解释这个方案"

    def test_purchase_model_is_recorded(self) -> None:
        plan = allocate([cand("K1")], CampaignSpec(budget_usd=50_000), max_posts=2, decay=0.9)
        assert plan.purchase_model == {"max_posts_per_kox": 2, "post_marginal_decay": 0.9}
        assert all(a.posts <= 2 for a in plan.selected)

    @pytest.mark.parametrize("decay", POST_DECAY_SCAN)
    def test_conclusion_is_robust_to_decay_assumption(self, decay: float) -> None:
        """衰减系数是**建模假设**而非观测值，所以"选谁"的结论不该被它左右。

        断言：三档 decay 下入选达人集合与默认档的 Jaccard 相似度 ≥0.6，且预算约束每档都成立。
        这条测试是为了让"我们用了 0.7"这个选择站得住脚——否则报告里的敏感性扫描只是摆设。
        """
        pool = [cand(f"K{i:02d}", cost=400 + (i % 7) * 50, value=60_000 + i * 137) for i in range(60)]
        pool += longtail_pool(20, cost=250, value=30_000)
        spec = CampaignSpec(budget_usd=25_000)
        ref = {a.kox_id for a in allocate(pool, spec).selected}
        plan = allocate(pool, spec, decay=decay)
        got = {a.kox_id for a in plan.selected}
        overlap = len(ref & got) / len(ref | got)
        assert overlap >= 0.6, f"decay={decay} 时入选集合与默认档只有 {overlap:.0%} 重叠"
        assert plan.spent_usd <= plan.budget_usd + 1e-6
        assert plan.purchase_model["post_marginal_decay"] == decay


# ===========================================================================
# 编排层 + 真实数据端到端
# ===========================================================================
class TestPlanner:
    def test_plan_campaign_on_real_data(
        self, records: list[dict[str, Any]], thresholds: Thresholds, spec: CampaignSpec
    ) -> None:
        plan, results = plan_campaign(records, spec, thresholds)
        assert plan.candidate_pool > 0
        assert plan.selected, "真实 brief 下一个人都没选上，方案不可用"
        assert plan.spent_usd <= plan.budget_usd + 1e-6
        assert {a.verdict for a in plan.selected} == {"pass"}
        assert results, "门禁结果必须回传给基线臂与审计复用"

    def test_include_review_widens_but_still_respects_constraints(
        self, records: list[dict[str, Any]], thresholds: Thresholds, spec: CampaignSpec
    ) -> None:
        strict, results = plan_campaign(records, spec, thresholds)
        wide, _ = plan_campaign(records, spec, thresholds, results, include_review=True)
        assert wide.candidate_pool >= strict.candidate_pool
        assert {a.verdict for a in wide.selected} <= {"pass", "review"}
        assert wide.spent_usd <= wide.budget_usd + 1e-6

    def test_two_arms_share_gate_results(
        self, records: list[dict[str, Any]], thresholds: Thresholds, spec: CampaignSpec
    ) -> None:
        """复用同一批 GateResult，差额才能干净归因到"选人依据"。"""
        plan_a, results = plan_campaign(records, spec, thresholds)
        plan_b = plan_baseline(records, spec, thresholds, results)
        assert plan_a.candidate_pool < plan_b.candidate_pool, "基线池必须更宽（含 review/reject）"
        assert plan_a.strategy == "koxpilot" and plan_b.strategy == "followers"

    def test_plan_is_deterministic(
        self, records: list[dict[str, Any]], thresholds: Thresholds, spec: CampaignSpec
    ) -> None:
        a, _ = plan_campaign(records, spec, thresholds)
        b, _ = plan_campaign(records, spec, thresholds)
        assert [(x.kox_id, x.posts, x.amount_usd) for x in a.selected] == [
            (x.kox_id, x.posts, x.amount_usd) for x in b.selected
        ]

    def test_all_real_briefs_produce_compliant_plans(
        self, records: list[dict[str, Any]], thresholds: Thresholds, specs: list[CampaignSpec]
    ) -> None:
        """每个真实 brief 都要产出**结构达标**的可交付方案，且报告能被独立重算验证。

        这里敢要求"全部达标"，是因为三个 brief 的候选池结构上都做得到；
        一旦哪天数据或定向变了导致做不到，这条测试会红——那时应该去看是不是候选池
        真的不足（并如实在 violations 里体现），而不是把这条断言放松掉。
        """
        for sp in specs:
            plan, _ = plan_campaign(records, sp, thresholds)
            tag = sp.campaign_id
            assert plan.spent_usd <= plan.budget_usd + 1e-6, tag
            shares = _plan_shares(plan)
            checks = _checks(plan)
            assert checks["head_max_share"]["actual"] == pytest.approx(shares["head"], abs=1e-4), tag
            assert checks["longtail_min_share"]["actual"] == pytest.approx(
                shares["longtail"], abs=1e-4
            ), tag
            assert checks["country_max_share"]["actual"] == pytest.approx(
                shares["max_country"], abs=1e-4
            ), tag
            assert plan.constraints["violations"] == [], f"{tag}: {plan.constraints['violations']}"
            assert plan.constraints["all_enforced_satisfied"] is True, tag
            assert plan.constraints["utilization"] >= MIN_UTILIZATION_TARGET, tag

    def test_price_estimated_flag_survives_to_the_plan(
        self, records: list[dict[str, Any]], thresholds: Thresholds, specs: list[CampaignSpec]
    ) -> None:
        """"这条报价是估算的"必须一路传到方案里——采购要知道哪几笔钱是拍的。"""
        for sp in specs:
            plan, _ = plan_campaign(records, sp, thresholds)
            assert plan.n_price_estimated == sum(1 for a in plan.selected if a.price_estimated)
