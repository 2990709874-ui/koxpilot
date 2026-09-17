"""反事实价值审计里"有效曝光提升"这组口径的针对性测试（`eval/audit.py`）。

单独成文件的原因：这组断言全部围绕**分母**做——相对提升 `(kox − base) / base` 的分母是
基线的有效曝光，而基线只选 4~7 个人，某些样本几乎把钱全花在水号上，分母会趋近 0。
`output/multiseed.json` 里 BRIEF-003 的跨种子均值因此变成 **+29977%**（单个种子 +3585 倍
把均值整个绑架了），而 BRIEF-002 是 +5946%。这种数字放进材料就是等着被问穿。

所以这里钉三件事：
1. 有界口径（`rate_gap_pp` / `symmetric_uplift`）在分母爆炸时**仍然有界**；
2. 分母为 0 时相对提升是 `None`（"无定义"），**不是 0.0**（0.0 会被读成"没有提升"，
   而事实恰好相反：基线的钱全打在水号上）；
3. 分母不可靠时 `headline` **不引用**那个比率。
"""

from __future__ import annotations

from typing import Any

import pytest

from koxpilot.eval.audit import (
    RATIO_FRAGILE_BASELINE_RATE,
    counterfactual_report,
    plan_audit,
)
from koxpilot.types import Allocation, BudgetPlan


def _alloc(kox_id: str, amount: float, views: float) -> Allocation:
    return Allocation(
        kox_id=kox_id,
        handle=f"@{kox_id}",
        platform="tiktok",
        country="US",
        bucket="mid",
        amount_usd=amount,
        posts=1,
        effective_posts=1.0,
        price_estimated=False,
        est_views=views,
        est_effective_views=views,
        est_engagements=views * 0.03,
        value_score=1.0,
        efficiency=1.0,
        fit_score=0.5,
        authenticity_discount=1.0,
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
    """只造审计需要的 gt 字段（`gt_of` 读 is_fraud / brand_safety / fraud_type）。"""
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


class TestEffectiveViewRate:
    def test_rate_is_effective_over_nominal_and_bounded(self) -> None:
        """单臂的有效曝光率恒在 [0,1]：它是有界口径的基石。"""
        allocs = [_alloc("k1", 5_000, 1_000_000), _alloc("k2", 5_000, 1_000_000)]
        audit = plan_audit(_plan("koxpilot", allocs), {"k1": {"is_fraud": True}, "k2": {}})
        assert audit["nominal_views"] == 2_000_000
        assert audit["effective_views_gt"] == 1_000_000
        assert audit["effective_view_rate"] == 0.5
        assert 0.0 <= audit["effective_view_rate"] <= 1.0
        # 宽松口径（水号曝光按 50% 计）必然不低于主口径，且同样有界
        assert audit["effective_view_rate_lenient"] == 0.75

    def test_rate_is_none_not_zero_when_nothing_was_bought(self) -> None:
        """名义曝光为 0 时"有效曝光率 = 0"是错的（无定义），必须给 None。"""
        audit = plan_audit(_plan("koxpilot", []), {})
        assert audit["nominal_views"] == 0
        assert audit["effective_view_rate"] is None
        assert audit["effective_view_rate_lenient"] is None


class TestUpliftDenominatorExplosion:
    """回归（真实数据）：per-campaign 相对提升的分母趋 0 → 比率爆炸 → 跨种子均值失真。"""

    @pytest.fixture
    def exploding(self) -> dict[str, Any]:
        # 基线：$10,000 全买水号，只有 1% 的曝光落在真人身上 → 分母极小
        base = _plan(
            "baseline_followers",
            [_alloc("bad1", 9_900, 990_000), _alloc("ok1", 100, 10_000)],
        )
        # KOXPilot：同预算全买真人
        kox = _plan("koxpilot", [_alloc("ok2", 10_000, 800_000)])
        records = _records({"bad1"}, ["bad1", "ok1", "ok2"])
        return counterfactual_report(records, {"C1": kox}, {"C1": base})

    def test_ratio_really_does_explode(self, exploding: dict[str, Any]) -> None:
        """先证明问题存在：这个构造下相对提升是 +7900%，一个样本就能带偏任何均值。"""
        row = exploding["per_campaign"][0]
        assert row["effective_view_uplift"] == pytest.approx(79.0, rel=1e-3)

    def test_bounded_metrics_stay_bounded(self, exploding: dict[str, Any]) -> None:
        b = exploding["per_campaign"][0]["effective_view_uplift_bounded"]
        assert -100.0 <= b["rate_gap_pp"] <= 100.0
        assert -1.0 <= b["symmetric_uplift"] <= 1.0
        # 有界口径仍然给出正确方向（KOXPilot 更好）
        assert b["rate_gap_pp"] > 0
        assert b["symmetric_uplift"] > 0
        assert b["absolute_gain_views"] == 790_000

    def test_fragile_denominator_is_flagged_and_kept_out_of_the_headline(
        self, exploding: dict[str, Any]
    ) -> None:
        row = exploding["per_campaign"][0]
        b = row["effective_view_uplift_bounded"]
        assert b["ratio_denominator_fragile"] is True
        assert b["baseline_effective_view_rate"] < RATIO_FRAGILE_BASELINE_RATE
        assert "7900" not in row["headline"] and "+7900.0%" not in row["headline"]
        assert "不引用该比率" in row["headline"]
        assert "不要拿它跨样本求均值" in row["effective_view_uplift_caveat"]

    def test_healthy_denominator_keeps_the_ratio_in_the_headline(self) -> None:
        """分母充足时不该反过来把比率藏起来——有界口径是补充，不是替换。"""
        base = _plan("baseline_followers", [_alloc("ok1", 10_000, 500_000)])
        kox = _plan("koxpilot", [_alloc("ok2", 10_000, 1_000_000)])
        rep = counterfactual_report(_records(set(), ["ok1", "ok2"]), {"C1": kox}, {"C1": base})
        row = rep["per_campaign"][0]
        assert row["effective_view_uplift"] == pytest.approx(1.0)
        assert row["effective_view_uplift_bounded"]["ratio_denominator_fragile"] is False
        assert "+100.0%" in row["headline"]


class TestUpliftUndefined:
    def test_zero_baseline_effective_views_gives_none_not_zero(self) -> None:
        """基线把钱全打在水号上（有效曝光 = 0）时，相对提升无定义。

        旧实现返回 0.0，等于把"基线全废、KOXPilot 全中"这种最有利的样本记成"无提升"——
        这是一个方向相反的虚假声称，同样不能留。
        """
        base = _plan("baseline_followers", [_alloc("bad1", 10_000, 900_000)])
        kox = _plan("koxpilot", [_alloc("ok1", 10_000, 800_000)])
        rep = counterfactual_report(_records({"bad1"}, ["bad1", "ok1"]), {"C1": kox}, {"C1": base})
        row = rep["per_campaign"][0]
        assert row["effective_view_uplift"] is None
        b = row["effective_view_uplift_bounded"]
        assert b["baseline_effective_view_rate"] == 0.0
        # 有界口径在这个极端样本上取到上界，而不是 +∞
        assert b["symmetric_uplift"] == 1.0
        assert b["rate_gap_pp"] == 100.0
        assert b["ratio_denominator_fragile"] is True
        assert "无定义" in row["effective_view_uplift_caveat"] or row["effective_view_uplift"] is None
        assert rep["totals"]["effective_view_uplift"] is None
        assert "无定义" in rep["headline"]


class TestTotalsHaveBothAssumptions:
    def test_totals_expose_lenient_uplift_and_bounded_gap(self) -> None:
        """totals 过去只有主口径比率，"结论不依赖保守假设"这句话在总口径上无产物支撑。"""
        base = _plan(
            "baseline_followers",
            [_alloc("bad1", 5_000, 500_000), _alloc("ok1", 5_000, 500_000)],
        )
        kox = _plan("koxpilot", [_alloc("ok2", 10_000, 900_000)])
        rep = counterfactual_report(_records({"bad1"}, ["bad1", "ok1", "ok2"]), {"C1": kox}, {"C1": base})
        totals = rep["totals"]
        for key in (
            "effective_view_uplift",
            "effective_view_uplift_lenient",
            "effective_views_baseline_lenient",
            "effective_views_koxpilot_lenient",
            "effective_view_uplift_bounded",
            "lenient_assumption",
        ):
            assert key in totals, key
        # 主口径 500k → 900k = +80%；宽松口径 750k → 900k = +20%，两者同向
        assert totals["effective_view_uplift"] == pytest.approx(0.8)
        assert totals["effective_view_uplift_lenient"] == pytest.approx(0.2)
        assert totals["effective_view_uplift_bounded"]["rate_gap_pp"] == pytest.approx(50.0)
        assert "宽松口径" in rep["headline"]

    def test_method_block_tells_readers_which_metric_to_aggregate(self) -> None:
        base = _plan("baseline_followers", [_alloc("ok1", 10_000, 500_000)])
        kox = _plan("koxpilot", [_alloc("ok2", 10_000, 600_000)])
        rep = counterfactual_report(_records(set(), ["ok1", "ok2"]), {"C1": kox}, {"C1": base})
        method = rep["method"]["uplift_metrics"]
        assert "rate_gap_pp" in method and "symmetric_uplift" in method
        assert "聚合" in method
