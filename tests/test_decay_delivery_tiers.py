"""敏感性扫描判"名单不稳"之后，产物必须给出**产品动作**，而不是只留一句免责声明。

两条被本文件钉住的东西，都是从同一类自伤里长出来的：

1. ``caveats`` 曾经算完就被丢掉。``decay_scan`` 模块里专门写了注释解释"把 blockers 和
   caveats 混成一个列表，就会出现'扫过了没问题'这种听起来通过、实际掩盖限定条件的说法"，
   然后返回值里根本没有 ``caveats`` 这个键——注释在替一段不存在的输出背书。这与该模块
   最初要修的那个 bug（``POST_DECAY_SCAN`` 的注释承诺了产物里没有的扫描）是同一个毛病。
2. ``selection_stable=False`` 只是一个判据结果。判据不达标却照发一份完整名单，等于把
   "这份名单里有一部分不可靠"的成本转嫁给投手。分层交付把不确定性定位到具体的人和金额上。

所以这里测的不是"字段存在"，而是"结论与动作之间没有断点"。
"""

from __future__ import annotations

from typing import Any

import pytest

from koxpilot.budget.planner import plan_campaign
from koxpilot.budget.policy import POST_MARGINAL_DECAY
from koxpilot.eval.decay_scan import decay_sensitivity_report


@pytest.fixture(scope="module")
def report(records, specs, thresholds) -> dict[str, Any]:
    return decay_sensitivity_report(records, specs, thresholds)


def test_caveats_actually_reach_the_artifact(report) -> None:
    """caveats 必须真的出现在产物里，且与 blockers 分开。

    本次实测是"价值结论稳、但选人会挪"，正是 caveats 该发挥作用的中间情形：
    如果它被丢掉，读者只会看到 blockers 为空，从而读成"全绿"。
    """
    assert "caveats" in report, "caveats 算了却没进产物 —— 注释又在替不存在的输出背书"
    assert "blockers" in report
    assert isinstance(report["caveats"], list)
    # 名单判据本次不达标，因此 caveats 不能是空的
    assert report["stability"]["selection_stable"] is False
    assert report["caveats"], "selection_stable=False 却没有任何 caveat"
    joined = " ".join(report["caveats"])
    assert "不能" in joined or "**不是**" in joined, "caveat 必须写清哪句话不能说"
    # 价值结论本次是站得住的，所以 blockers 应为空：两类记录不能混为一谈
    assert report["blockers"] == []


def test_unstable_selection_triggers_a_delivery_rule_not_just_a_disclaimer(report) -> None:
    """判据不达标 → 必须存在分流规则，且触发条件写明。"""
    policy = report["delivery_policy"]
    assert policy["trigger"] == "selection_stable == false"
    assert report["stability"]["selection_stable"] is False, "前提变了，本测试的意义要重新审"
    for tier in ("stable_core", "assumption_sensitive"):
        assert policy[tier]["n_kox"] >= 0
        assert policy[tier]["action"], f"{tier} 没有动作，等于没分流"
    assert "人工确认" in policy["assumption_sensitive"]["action"]


def test_core_tier_is_exactly_the_intersection_across_decay_levels(report) -> None:
    """核心层的定义必须可验算：三档都选中，一档没中就不算。"""
    levels = report["scan"]
    for block in report["per_campaign"]:
        tiers = block["delivery_tiers"]
        for row in tiers["stable_core"]["kox"]:
            assert row["selected_at_decays"] == levels, (
                f"{row['kox_id']} 进了核心层但不是三档全选中：{row['selected_at_decays']}"
            )
        for row in tiers["assumption_sensitive"]["kox"]:
            assert 0 < len(row["selected_at_decays"]) < len(levels), (
                f"{row['kox_id']} 进了敏感层却是全选中或全未选中"
            )


def test_tiers_partition_the_reference_plan_without_loss(report, records, specs, thresholds) -> None:
    """两层加起来必须正好等于正式链路那份名单 —— 不能有人被分层弄丢或重复计。"""
    real = [s for s in specs if not s.is_neutral]
    for spec in real:
        block = next(b for b in report["per_campaign"] if b["campaign_id"] == spec.campaign_id)
        tiers = block["delivery_tiers"]
        plan, _ = plan_campaign(records, spec, thresholds, decay=POST_MARGINAL_DECAY)
        prod_ids = {a.kox_id for a in plan.selected}
        core_ids = {r["kox_id"] for r in tiers["stable_core"]["kox"]}
        sens_ids = {r["kox_id"] for r in tiers["assumption_sensitive"]["kox"]}
        assert core_ids <= prod_ids
        assert core_ids | (sens_ids & prod_ids) == prod_ids, "分层没有覆盖正式名单"
        assert not (core_ids & sens_ids), "同一个达人同时进了两层"
        assert tiers["n_selected_reference"] == len(prod_ids)
        # 金额也要对得上：参照档两层金额之和 == 该 campaign 实际花出去的钱
        amount = (
            tiers["stable_core"]["amount_usd_in_reference_plan"]
            + tiers["assumption_sensitive"]["amount_usd_in_reference_plan"]
        )
        assert abs(amount - plan.spent_usd) < 0.05, f"两层金额之和 {amount} != 实付 {plan.spent_usd}"


def test_sensitive_tier_amount_range_shows_the_swing(report) -> None:
    """敏感层每个人要带金额区间：只给参照档金额会掩盖"换档就归零"这件事。"""
    for block in report["per_campaign"]:
        rows = block["delivery_tiers"]["assumption_sensitive"]["kox"]
        for row in rows:
            assert row["amount_usd_min"] <= row["amount_usd_reference"] <= row["amount_usd_max"]
            # 只在部分档位入选 ⇒ 至少有一档金额为 0
            assert row["amount_usd_min"] == 0.0, f"{row['kox_id']} 未在某档缺席，不该进敏感层"


def test_policy_totals_match_per_campaign_blocks(report) -> None:
    """顶层汇总不能与逐 campaign 明细打架 —— 这是最容易出现的手抄错。"""
    policy = report["delivery_policy"]
    blocks = report["per_campaign"]
    for tier in ("stable_core", "assumption_sensitive"):
        n = sum(b["delivery_tiers"][tier]["n_in_reference_plan"] for b in blocks)
        amt = sum(b["delivery_tiers"][tier]["amount_usd_in_reference_plan"] for b in blocks)
        assert policy[tier]["n_kox"] == n
        assert abs(policy[tier]["amount_usd"] - amt) < 0.05
    shares = policy["stable_core"]["share_of_spend"] + policy["assumption_sensitive"]["share_of_spend"]
    assert abs(shares - 1.0) < 0.01, "两层占比之和不为 1，分层口径有洞"
