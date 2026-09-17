"""三臂价值归因的守卫测试（第三臂 ``diversified_no_gate`` + ``eval/audit.arm_attribution``）。

为什么单独成文件
----------------
"KOXPilot 比按粉丝量买的基线少浪费了 X 美元"这句话，在只有两臂的时候是**无法归因**的：
差额里同时混着"结构分散化"（不把钱压在少数头部大号上）与"质量门禁 + 质量排序"
（识别水号/高风险号并按质量加权价值排序）。材料上写成"门禁省下了 X 美元"就是一句
站不住的声称——第三臂正是为了让这句话有资格被说出口而存在。

所以这里钉四件事，每一件都对应一种"会让归因失真"的具体作弊：
1. **第三臂不许偷看质量信号**：只用 ``avg_views / cost`` 排序。若它沿用 ``value(k)``
   （含真实性折扣/语义适配/KPI 权重），就等于用门禁的判断力去避开水号，
   "分散化贡献"里会白拿门禁的功劳。守卫方式：把候选的质量字段整体打乱后，
   第三臂的选择必须**逐人逐条完全不变**，而 KOXPilot 臂必须变。
2. **第三臂必须真的强制结构约束、且真的不做门禁过滤**：否则它不是"基线与 KOXPilot 之间的
   那一步"，而是另一条无关的臂，链式差分就没有意义。
3. **两段贡献必须可加**：``总差额 = 分散化 + 门禁与质量排序`` 是恒等式；
   任何一侧换成"相对提升比率"都会破坏可加性（分母不同），这里显式钉住。
4. **负贡献必须照实写**：分散化把钱推向长尾、长尾里水号更密时，这一段会是**负数**。
   不许截断成 0，headline 必须直说"反而多浪费"，占比在总差额 ≤ 0 时必须留空而不是给出
   −140% 这种只会误导人的数字。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from koxpilot.budget.allocator import (
    BASIS_VALUE,
    BASIS_VIEWS,
    STRATEGIES,
    Slot,
    allocate,
    build_slots,
)
from koxpilot.budget.planner import (
    plan_baseline,
    plan_campaign,
    plan_diversified_no_gate,
)
from koxpilot.budget.policy import HEAD_MAX_SHARE, LONGTAIL_MIN_SHARE
from koxpilot.budget.value import Candidate
from koxpilot.eval.audit import ARM_LABELS, arm_attribution, counterfactual_report
from koxpilot.eval.multiseed import arm_attribution_robustness
from koxpilot.gates.thresholds import Thresholds
from koxpilot.types import Allocation, BudgetPlan, CampaignSpec, GateResult

STRUCTURAL = ("head_max_share", "longtail_min_share", "country_max_share")


# ---------------------------------------------------------------------------
# 手工候选：刻意让"名义曝光/报价"与"质量加权价值/报价"给出**相反**的排序
# ---------------------------------------------------------------------------
def cand(
    kox_id: str,
    *,
    bucket: str = "micro",
    country: str = "US",
    cost: float = 1_000.0,
    avg_views: float = 100_000.0,
    quality: float = 1.0,
    followers: float = 50_000.0,
    verdict: str = "pass",
) -> Candidate:
    """``quality`` 把三个质量因子一次性拉齐：value = avg_views × quality。

    这样"名义曝光"与"质量加权价值"可以被独立操纵——第三臂只该看前者。
    """
    return Candidate(
        kox_id=kox_id,
        handle=f"@{kox_id.lower()}",
        platform="tiktok",
        country=country,
        bucket=bucket,
        group=f"tiktok|{bucket}",
        verdict=verdict,
        followers=followers,
        avg_views=avg_views,
        engagement_rate=0.05,
        cost_usd=cost,
        price_estimated=False,
        authenticity_discount=quality,
        fit_score=1.0,
        audience_match=1.0,
        kpi_weight=1.0,
        value=avg_views * quality,
        efficiency=avg_views * quality / cost,
    )


def _mixed_pool() -> list[Candidate]:
    """一半是"曝光便宜但质量差"的号，一半是"曝光贵但质量好"的号。

    按每美元名义曝光排序时前者胜出，按质量加权价值排序时后者胜出——
    两把尺子必须给出不同答案，否则"第三臂没偷看质量"这个断言是空转的。
    """
    countries = ("US", "JP", "DE", "BR")
    pool: list[Candidate] = []
    for i in range(8):  # 便宜的水号：名义曝光高、真实性折扣低
        pool.append(
            cand(
                f"CHEAP{i:02d}",
                bucket="nano",
                country=countries[i % 4],
                cost=400.0,
                avg_views=200_000.0,
                quality=0.05,
                followers=8_000,
            )
        )
    for i in range(8):  # 贵的真人号：名义曝光低、质量满分
        pool.append(
            cand(
                f"GOOD{i:02d}",
                bucket="micro",
                country=countries[i % 4],
                cost=1_000.0,
                avg_views=60_000.0,
                quality=1.0,
                followers=60_000,
            )
        )
    return pool


def _posts_by_id(plan: BudgetPlan) -> dict[str, int]:
    return {a.kox_id: a.posts for a in plan.selected}


def _scramble_quality(pool: list[Candidate]) -> list[Candidate]:
    """把质量因子在候选之间**整体对调**，名义曝光与报价一字不动。

    对调而不是随机扰动：这样"如果排序偷看了质量"就必然换出不同的选择集，
    测试不会因为扰动太小而假绿。
    """
    out: list[Candidate] = []
    for c, donor in zip(pool, reversed(pool)):
        q = donor.authenticity_discount
        out.append(
            replace(
                c,
                authenticity_discount=q,
                fit_score=donor.fit_score,
                kpi_weight=donor.kpi_weight,
                value=c.avg_views * q,
                efficiency=c.avg_views * q / c.cost_usd,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 1. 第三臂不许偷看质量信号
# ---------------------------------------------------------------------------
class TestThirdArmIgnoresQualitySignals:
    def test_slot_basis_switches_the_ruler_only(self) -> None:
        c = cand("K1", cost=500.0, avg_views=100_000.0, quality=0.1)
        assert Slot(c, 1, basis=BASIS_VALUE).marginal_value == pytest.approx(10_000.0)
        assert Slot(c, 1, basis=BASIS_VIEWS).marginal_value == pytest.approx(100_000.0)
        # 两把尺子共用同一套衰减，所以"换尺子"不改变贪心的凹性前提
        assert Slot(c, 2, basis=BASIS_VIEWS).marginal_value < Slot(c, 1, basis=BASIS_VIEWS).marginal_value

    def test_unknown_basis_and_strategy_are_rejected_loudly(self) -> None:
        with pytest.raises(ValueError):
            build_slots([cand("K1")], basis="followers_lol")
        with pytest.raises(ValueError):
            allocate([cand("K1")], CampaignSpec(budget_usd=1_000.0), "not_an_arm")
        assert set(STRATEGIES) == {"followers", "diversified_no_gate", "koxpilot"}
        assert STRATEGIES["diversified_no_gate"] == (True, BASIS_VIEWS)

    def test_third_arm_selection_is_invariant_to_quality_scrambling(self) -> None:
        """核心守卫：质量字段整体对调后，第三臂的选择必须**完全不变**。"""
        spec = CampaignSpec(budget_usd=20_000.0)
        pool = _mixed_pool()
        scrambled = _scramble_quality(pool)
        before = allocate(pool, spec, "diversified_no_gate")
        after = allocate(scrambled, spec, "diversified_no_gate")
        assert _posts_by_id(before) == _posts_by_id(after)
        assert before.spent_usd == pytest.approx(after.spent_usd)
        assert before.n_posts == after.n_posts

    def test_koxpilot_arm_does_react_to_the_same_scrambling(self) -> None:
        """反向对照：同一次对调必须把 KOXPilot 臂的选择改掉。

        没有这条，上面那条"不变"可能只是因为候选池本身对排序不敏感（假绿）。
        """
        spec = CampaignSpec(budget_usd=20_000.0)
        before = allocate(_mixed_pool(), spec, "koxpilot")
        after = allocate(_scramble_quality(_mixed_pool()), spec, "koxpilot")
        assert _posts_by_id(before) != _posts_by_id(after)

    def test_third_arm_buys_the_cheap_nominal_exposure_koxpilot_avoids(self) -> None:
        """两臂在同一个池子上给出不同答案：第三臂爱便宜曝光，KOXPilot 爱质量。"""
        spec = CampaignSpec(budget_usd=20_000.0)
        pool = _mixed_pool()
        div = allocate(pool, spec, "diversified_no_gate")
        kox = allocate(pool, spec, "koxpilot")
        div_cheap = sum(a.amount_usd for a in div.selected if a.kox_id.startswith("CHEAP"))
        kox_cheap = sum(a.amount_usd for a in kox.selected if a.kox_id.startswith("CHEAP"))
        assert div_cheap / div.spent_usd > kox_cheap / kox.spent_usd
        # 第三臂的名义曝光更高，但它买的是折扣后几乎没人看的曝光
        assert sum(a.est_views for a in div.selected) > sum(a.est_views for a in kox.selected)
        assert sum(a.est_effective_views for a in div.selected) < sum(
            a.est_effective_views for a in kox.selected
        )

    def test_giving_back_slots_also_uses_the_arms_own_ruler(self) -> None:
        """退让阶段同样不许偷看质量分：否则结构修正会悄悄"帮"第三臂避开水号。

        这个池子会真的走进 ``_repair_longtail`` 的退让分支（长尾占比 0%，候选池结构性不足），
        退让时按 ``marginal_efficiency_of_last`` 挑"最该退的那条"——那个函数若用回质量加权
        价值，第三臂就会在修正阶段偷到门禁的判断力。同样用"质量整体对调"来钉住。
        """
        spec = CampaignSpec(budget_usd=20_000.0)
        pool = (
            [
                cand(f"BIG{i:02d}", bucket="mega", country="US", cost=4_000.0,
                     avg_views=900_000.0, quality=0.05, followers=5_000_000)
                for i in range(3)
            ]
            + [
                cand(f"MID{i:02d}", bucket="mid", country=("US", "JP", "DE", "BR")[i % 4],
                     cost=500.0, avg_views=100_000.0, quality=0.2, followers=200_000)
                for i in range(10)
            ]
            + [
                cand(f"TAIL{i:02d}", bucket="nano", country=("JP", "DE", "BR")[i % 3],
                     cost=300.0, avg_views=25_000.0, quality=1.0, followers=6_000)
                for i in range(6)
            ]
        )
        before = allocate(pool, spec, "diversified_no_gate")
        after = allocate(_scramble_quality(pool), spec, "diversified_no_gate")
        assert _posts_by_id(before) == _posts_by_id(after)
        assert any("分层修正" in t for t in before.trace), "这个池子本应触发长尾修正"
        # 修正失败时如实记违规（第三臂不因为"是对照臂"就放宽记账口径）
        assert "longtail_min_share" in before.constraints["violations"]


# ---------------------------------------------------------------------------
# 2. 第三臂必须"强制结构约束 + 不做门禁过滤"
# ---------------------------------------------------------------------------
class TestThirdArmSitsBetweenTheOtherTwo:
    def test_structural_caps_are_enforced_for_third_arm_but_not_for_baseline(self) -> None:
        spec = CampaignSpec(budget_usd=20_000.0)
        pool = _mixed_pool()
        div = allocate(pool, spec, "diversified_no_gate")
        base = allocate(pool, spec, "followers")
        div_flags = {c["name"]: c["enforced"] for c in div.constraints["checks"]}
        base_flags = {c["name"]: c["enforced"] for c in base.constraints["checks"]}
        for name in STRUCTURAL:
            assert div_flags[name] is True, name
            assert base_flags[name] is False, name

    @pytest.mark.parametrize("campaign_index", [0, 1, 2])
    def test_on_real_data_third_arm_shares_the_baseline_pool_and_koxpilots_structure(
        self,
        records: list[dict[str, Any]],
        thresholds: Thresholds,
        specs: list[CampaignSpec],
        campaign_index: int,
    ) -> None:
        spec = specs[campaign_index]
        kox, res = plan_campaign(records, spec, thresholds)
        base = plan_baseline(records, spec, thresholds, res)
        div = plan_diversified_no_gate(records, spec, thresholds, res)

        # 候选池：与基线同源（含 review/reject），比 KOXPilot 宽
        assert div.candidate_pool == base.candidate_pool > kox.candidate_pool
        assert div.strategy == "diversified_no_gate"
        # 门禁没有生效：被门禁挡掉的号仍然可能被买
        assert {a.verdict for a in div.selected} - {"pass"}
        assert {a.verdict for a in kox.selected} <= {"pass", "review"}
        # 结构约束真的生效了（基线在同一份数据上是破线的）
        assert div.constraints["violations"] == []
        div_actual = {c["name"]: c["actual"] for c in div.constraints["checks"]}
        assert div_actual["head_max_share"] <= HEAD_MAX_SHARE + 1e-6
        assert div_actual["longtail_min_share"] >= LONGTAIL_MIN_SHARE - 1e-6
        base_actual = {c["name"]: c["satisfied"] for c in base.constraints["checks"]}
        assert not all(base_actual[n] for n in STRUCTURAL), "基线在这份数据上本应结构破线"


# ---------------------------------------------------------------------------
# 3. 归因可加性
# ---------------------------------------------------------------------------
def _audit(
    *, wasted: float, n: int, fraud: int, eff: float, rate: float | None
) -> dict[str, Any]:
    return {
        "wasted_spend_usd": wasted,
        "n_selected": n,
        "n_fraud_selected": fraud,
        "effective_views_gt": eff,
        "effective_view_rate": rate,
    }


class TestAttributionIsAdditive:
    def test_two_segments_sum_to_the_total_in_usd(self) -> None:
        attr = arm_attribution(
            _audit(wasted=6_000.0, n=8, fraud=5, eff=100_000.0, rate=0.1),
            _audit(wasted=4_000.0, n=200, fraud=40, eff=900_000.0, rate=0.3),
            _audit(wasted=1_000.0, n=70, fraud=3, eff=800_000.0, rate=0.8),
            budget_usd=10_000.0,
        )
        money = attr["waste_reduction_usd"]
        assert money["total"] == pytest.approx(5_000.0)
        assert money["by_diversification"] == pytest.approx(2_000.0)
        assert money["by_gating_and_quality_ranking"] == pytest.approx(3_000.0)
        assert money["by_diversification"] + money["by_gating_and_quality_ranking"] == pytest.approx(
            money["total"]
        )
        assert money["share_of_total_by_diversification"] == pytest.approx(0.4)
        assert money["share_of_total_by_gating"] == pytest.approx(0.6)

    def test_bounded_rate_gaps_are_additive_too(self) -> None:
        attr = arm_attribution(
            _audit(wasted=6_000.0, n=8, fraud=5, eff=100_000.0, rate=0.1),
            _audit(wasted=4_000.0, n=200, fraud=40, eff=900_000.0, rate=0.3),
            _audit(wasted=1_000.0, n=70, fraud=3, eff=800_000.0, rate=0.8),
            budget_usd=10_000.0,
        )
        gaps = attr["effective_view_rate_pp"]
        assert gaps["gap_by_diversification_pp"] == pytest.approx(20.0)
        assert gaps["gap_by_gating_and_quality_ranking_pp"] == pytest.approx(50.0)
        assert gaps["gap_total_pp"] == pytest.approx(70.0)
        assert (
            gaps["gap_by_diversification_pp"] + gaps["gap_by_gating_and_quality_ranking_pp"]
        ) == pytest.approx(gaps["gap_total_pp"])
        # 每一段都在有界区间内（这正是不拆"相对提升比率"的理由）
        for key in ("gap_total_pp", "gap_by_diversification_pp",
                    "gap_by_gating_and_quality_ranking_pp"):
            assert -100.0 <= gaps[key] <= 100.0

    def test_undefined_rate_yields_none_not_zero(self) -> None:
        """某臂什么都没买（名义曝光 0）时，那一段百分点差无定义，不许当 0。"""
        attr = arm_attribution(
            _audit(wasted=0.0, n=0, fraud=0, eff=0.0, rate=None),
            _audit(wasted=4_000.0, n=200, fraud=40, eff=900_000.0, rate=0.3),
            _audit(wasted=1_000.0, n=70, fraud=3, eff=800_000.0, rate=0.8),
            budget_usd=10_000.0,
        )
        gaps = attr["effective_view_rate_pp"]
        assert gaps["gap_by_diversification_pp"] is None
        assert gaps["gap_total_pp"] is None
        assert gaps["gap_by_gating_and_quality_ranking_pp"] == pytest.approx(50.0)

    def test_arm_labels_name_all_three_arms(self) -> None:
        assert set(ARM_LABELS) == {"baseline_followers", "diversified_no_gate", "koxpilot"}
        assert "不看门禁" in ARM_LABELS["diversified_no_gate"]


# ---------------------------------------------------------------------------
# 4. 负贡献照实写
# ---------------------------------------------------------------------------
class TestNegativeContributionIsToldStraight:
    @pytest.fixture
    def negative(self) -> dict[str, Any]:
        """分散化把钱推向水号更密的长尾 → 第三臂比基线**更**浪费。"""
        return arm_attribution(
            _audit(wasted=2_000.0, n=6, fraud=2, eff=500_000.0, rate=0.5),
            _audit(wasted=5_000.0, n=180, fraud=60, eff=300_000.0, rate=0.3),
            _audit(wasted=800.0, n=60, fraud=1, eff=700_000.0, rate=0.7),
            budget_usd=10_000.0,
        )

    def test_negative_segment_is_not_clamped(self, negative: dict[str, Any]) -> None:
        money = negative["waste_reduction_usd"]
        assert money["by_diversification"] == pytest.approx(-3_000.0)
        assert money["by_gating_and_quality_ranking"] == pytest.approx(4_200.0)
        assert money["total"] == pytest.approx(1_200.0)
        gaps = negative["effective_view_rate_pp"]
        assert gaps["gap_by_diversification_pp"] == pytest.approx(-20.0)

    def test_headline_says_more_waste_not_less(self, negative: dict[str, Any]) -> None:
        headline = negative.get("headline") or ""
        if not headline:  # per-campaign 的 attribution 不带 headline，用总口径函数格式化
            from koxpilot.eval.audit import _attribution_headline

            headline = _attribution_headline(negative)
        assert "结构分散化反而多浪费 $3,000" in headline
        assert "门禁与质量排序少浪费 $4,200" in headline

    def test_share_of_total_is_none_when_total_is_not_positive(self) -> None:
        attr = arm_attribution(
            _audit(wasted=1_000.0, n=6, fraud=2, eff=500_000.0, rate=0.5),
            _audit(wasted=5_000.0, n=180, fraud=60, eff=300_000.0, rate=0.3),
            _audit(wasted=1_000.0, n=60, fraud=1, eff=700_000.0, rate=0.7),
            budget_usd=10_000.0,
        )
        money = attr["waste_reduction_usd"]
        assert money["total"] == pytest.approx(0.0)
        assert money["share_of_total_by_diversification"] is None
        assert money["share_of_total_by_gating"] is None
        assert "留空" in attr["note"]

    def test_third_arm_winning_on_absolute_views_is_disclosed(self) -> None:
        """第三臂绝对有效曝光更高时必须自己说出来，不许用"我们曝光也最多"掩盖。"""
        attr = arm_attribution(
            _audit(wasted=6_000.0, n=8, fraud=5, eff=100_000.0, rate=0.1),
            _audit(wasted=4_000.0, n=200, fraud=40, eff=2_000_000.0, rate=0.3),
            _audit(wasted=1_000.0, n=70, fraud=3, eff=800_000.0, rate=0.8),
            budget_usd=10_000.0,
        )
        joined = " ".join(attr["caveats"])
        assert "绝对" in joined and "有效曝光率" in joined
        assert "人数" in joined  # 三臂人数不同量级也必须披露


# ---------------------------------------------------------------------------
# 5. 报告接线：有第三臂就出归因，没有就明说没有
# ---------------------------------------------------------------------------
def _alloc(kox_id: str, amount: float, views: float, discount: float = 1.0) -> Allocation:
    return Allocation(
        kox_id=kox_id,
        handle=f"@{kox_id}",
        platform="tiktok",
        country="US",
        bucket="micro",
        amount_usd=amount,
        posts=1,
        effective_posts=1.0,
        price_estimated=False,
        est_views=views,
        est_effective_views=views * discount,
        est_engagements=views * 0.03,
        value_score=views * discount,
        efficiency=1.0,
        fit_score=0.5,
        authenticity_discount=discount,
        audience_match=0.5,
    )


def _plan(strategy: str, allocs: list[Allocation], budget: float = 10_000.0) -> BudgetPlan:
    return BudgetPlan(
        campaign_id="C1",
        budget_usd=budget,
        spent_usd=sum(a.amount_usd for a in allocs),
        selected=allocs,
        strategy=strategy,
    )


def _records(fraud_ids: set[str], all_ids: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "kox_id": kid,
            "gt": {
                "is_fraud": kid in fraud_ids,
                "fraud_type": "bought_followers" if kid in fraud_ids else None,
                "brand_safety": "low",
            },
        }
        for kid in all_ids
    ]


class TestCounterfactualReportWiring:
    @pytest.fixture
    def three_arms(self) -> dict[str, Any]:
        base = _plan("baseline_followers", [_alloc("bad1", 8_000, 800_000), _alloc("ok1", 2_000, 100_000)])
        div = _plan(
            "diversified_no_gate",
            [_alloc("bad2", 3_000, 400_000), _alloc("ok2", 4_000, 300_000), _alloc("ok3", 3_000, 200_000)],
        )
        kox = _plan("koxpilot", [_alloc("ok4", 6_000, 400_000), _alloc("ok5", 4_000, 300_000)])
        recs = _records({"bad1", "bad2"}, ["bad1", "bad2", "ok1", "ok2", "ok3", "ok4", "ok5"])
        return counterfactual_report(recs, {"C1": kox}, {"C1": base}, {"C1": div})

    def test_per_campaign_and_totals_both_carry_the_attribution(
        self, three_arms: dict[str, Any]
    ) -> None:
        row = three_arms["per_campaign"][0]
        assert "diversified_no_gate" in row
        money = row["value_attribution"]["waste_reduction_usd"]
        # 基线把 $8,000 砸在水号 bad1 上；第三臂只剩 $3,000 砸在 bad2；KOXPilot 一分不浪费
        assert money["total"] == pytest.approx(8_000.0)
        assert money["by_diversification"] == pytest.approx(5_000.0)
        assert money["by_gating_and_quality_ranking"] == pytest.approx(3_000.0)
        top = three_arms["value_attribution"]
        assert top is not None
        assert "headline" in top and "少浪费" in top["headline"]
        assert "diversified_no_gate" in three_arms["method"]["third_arm"]
        assert "不读门禁判定" in three_arms["method"]["third_arm"]

    def test_totals_attribution_is_computed_on_pooled_money_not_averaged_ratios(
        self, three_arms: dict[str, Any]
    ) -> None:
        money = three_arms["value_attribution"]["waste_reduction_usd"]
        assert money["wasted_baseline"] == pytest.approx(8_000.0)
        assert money["wasted_diversified_no_gate"] == pytest.approx(3_000.0)
        assert money["wasted_koxpilot"] == pytest.approx(0.0)
        assert money["total"] == pytest.approx(
            money["by_diversification"] + money["by_gating_and_quality_ranking"]
        )

    def test_without_third_arm_the_report_says_so_instead_of_faking_it(self) -> None:
        base = _plan("baseline_followers", [_alloc("bad1", 10_000, 900_000)])
        kox = _plan("koxpilot", [_alloc("ok1", 10_000, 800_000)])
        rep = counterfactual_report(_records({"bad1"}, ["bad1", "ok1"]), {"C1": kox}, {"C1": base})
        assert rep["value_attribution"] is None
        assert "未跑第三臂" in rep["method"]["third_arm"]
        assert "value_attribution" not in rep["per_campaign"][0]

    def test_real_data_end_to_end_attribution_is_additive(
        self,
        records: list[dict[str, Any]],
        thresholds: Thresholds,
        specs: list[CampaignSpec],
    ) -> None:
        plans: dict[str, BudgetPlan] = {}
        bases: dict[str, BudgetPlan] = {}
        divs: dict[str, BudgetPlan] = {}
        for spec in specs:
            kox, res = plan_campaign(records, spec, thresholds)
            plans[spec.campaign_id] = kox
            bases[spec.campaign_id] = plan_baseline(records, spec, thresholds, res)
            divs[spec.campaign_id] = plan_diversified_no_gate(records, spec, thresholds, res)
        rep = counterfactual_report(records, plans, bases, divs)
        for row in rep["per_campaign"]:
            money = row["value_attribution"]["waste_reduction_usd"]
            assert money["by_diversification"] + money["by_gating_and_quality_ranking"] == pytest.approx(
                money["total"], abs=0.02
            )
            gaps = row["value_attribution"]["effective_view_rate_pp"]
            assert gaps["gap_by_diversification_pp"] + gaps[
                "gap_by_gating_and_quality_ranking_pp"
            ] == pytest.approx(gaps["gap_total_pp"], abs=0.02)
        total_money = rep["value_attribution"]["waste_reduction_usd"]
        assert total_money["total"] == pytest.approx(rep["totals"]["saved_usd"], abs=0.02)
        # 门禁与质量排序这一段在官方种子上应为正（这是产物里写进文档的方向）
        assert total_money["by_gating_and_quality_ranking"] > 0.0


# ---------------------------------------------------------------------------
# 6. 跨种子汇总：可加性自检与反向证据
# ---------------------------------------------------------------------------
def _seed_row(
    seed: int,
    *,
    total: float,
    div: float | None,
    gate: float | None,
    gap_div: float = 5.0,
    gap_gate: float = 10.0,
    div_eff: float = 1_000.0,
    kox_eff: float = 900.0,
) -> dict[str, Any]:
    return {
        "seed": seed,
        "value": {
            "saved_usd": total,
            "saved_usd_by_diversification": div,
            "saved_usd_by_gating": gate,
            "rate_gap_pp_by_diversification": gap_div,
            "rate_gap_pp_by_gating": gap_gate,
            "diversified_no_gate_effective_views": div_eff,
            "koxpilot_effective_views": kox_eff,
            "diversified_no_gate_n_selected": 200,
        },
    }


class TestMultiseedAttributionRobustness:
    def test_additivity_self_check_and_negative_seed_counting(self) -> None:
        per_seed = [
            _seed_row(1, total=100.0, div=40.0, gate=60.0),
            _seed_row(2, total=100.0, div=-20.0, gate=120.0),
            _seed_row(3, total=100.0, div=10.0, gate=90.0),
        ]
        out = arm_attribution_robustness(per_seed)
        assert out["additivity_check"]["ok"] is True
        assert out["additivity_check"]["mean_total"] == pytest.approx(100.0)
        assert out["n_seeds_diversification_contribution_negative"] == 1
        assert out["seeds_diversification_contribution_negative"] == [2]
        assert out["n_seeds_gating_contribution_negative"] == 0
        # 第三臂绝对曝光超过 KOXPilot 的种子必须被数出来（反向证据）
        assert out["n_seeds_third_arm_more_effective_views"] == 3
        assert "不是一个'更好的方案'" in out["caveat"]

    def test_missing_third_arm_degrades_explicitly(self) -> None:
        per_seed = [_seed_row(1, total=100.0, div=None, gate=None)]
        out = arm_attribution_robustness(per_seed)
        assert out["status"] == "third_arm_missing"
        assert "saved_usd_by_diversification" not in out


# ---------------------------------------------------------------------------
# 7. 真实数据上的端到端守卫：门禁信号一点都不许漏进第三臂
# ---------------------------------------------------------------------------
def _forge(res: dict[str, GateResult], *, verdict: str | None, scramble: bool) -> dict[str, GateResult]:
    """伪造门禁结果：可以整体改判，也可以把真实性/适配分数打乱。

    打乱用 ``kox_id`` 的稳定哈希，保证测试自身是确定性的。
    """
    out: dict[str, GateResult] = {}
    for kid, r in res.items():
        kwargs: dict[str, Any] = {}
        if verdict is not None:
            kwargs["verdict"] = verdict
        if scramble:
            noise = (sum(ord(ch) for ch in kid) % 97) / 100.0 + 0.01
            kwargs["authenticity_score"] = noise
            kwargs["fit_score"] = 1.0 - noise
        out[kid] = replace(r, **kwargs)
    return out


class TestNoGateSignalLeaksIntoThirdArmOnRealData:
    def test_third_arm_ignores_forged_verdicts(
        self,
        records: list[dict[str, Any]],
        thresholds: Thresholds,
        spec: CampaignSpec,
    ) -> None:
        """把全库判定改成 reject，第三臂的选择必须一字不变（它本来就不看 verdict）。"""
        _, res = plan_campaign(records, spec, thresholds)
        honest = plan_diversified_no_gate(records, spec, thresholds, res)
        tampered = plan_diversified_no_gate(
            records, spec, thresholds, _forge(res, verdict="reject", scramble=False)
        )
        assert _posts_by_id(honest) == _posts_by_id(tampered)
        assert honest.spent_usd == pytest.approx(tampered.spent_usd)

    def test_third_arm_ignores_forged_authenticity_and_fit_but_koxpilot_does_not(
        self,
        records: list[dict[str, Any]],
        thresholds: Thresholds,
        spec: CampaignSpec,
    ) -> None:
        """把真实性折扣与语义适配分数打乱：第三臂不动，KOXPilot 臂必须动。

        这两个分数是 ``value(k)`` 的因子，也是门禁体系的产出。第三臂若对它们敏感，
        "分散化贡献"就白拿了门禁的判断力，归因分解那张表就不能写进材料。
        """
        _, res = plan_campaign(records, spec, thresholds)
        forged = _forge(res, verdict=None, scramble=True)
        div_before = plan_diversified_no_gate(records, spec, thresholds, res)
        div_after = plan_diversified_no_gate(records, spec, thresholds, forged)
        assert _posts_by_id(div_before) == _posts_by_id(div_after)

        kox_before, _ = plan_campaign(records, spec, thresholds, res)
        kox_after, _ = plan_campaign(records, spec, thresholds, forged)
        assert _posts_by_id(kox_before) != _posts_by_id(kox_after), "反向对照失效，上面那条会假绿"
