"""decay 敏感性扫描的守护测试。

这条扫描存在的理由：`budget/policy.py` 里的 `POST_DECAY_SCAN = (0.5, 0.7, 0.9)` 长期
"定义了但没有产物"，于是采购模型里的 `decay=0.7` 这个关键假设**没有任何证据**。
补上扫描之后，真正需要被钉住的不是"数字好看"，而是三件事：

1. 参照档（0.7）跑出来的必须与正式链路**同一个结果** —— 否则扫描用的是另一套代码路径，
   报出来的稳定性与正式结论无关。
2. 三档必须**真的重跑了分配**，不能因为 `decay` 没传进 allocator 而三档完全相同 ——
   那种情况下扫描会给出"完美稳定"的假绿，比没有扫描更危险。
3. "选人不稳"这件事必须被如实报出，不能被 `selection_stable=True` 悄悄掩盖。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from koxpilot.budget.planner import (
    plan_baseline,
    plan_campaign,
    plan_diversified_no_gate,
)
from koxpilot.budget.policy import POST_DECAY_SCAN, POST_MARGINAL_DECAY
from koxpilot.eval.decay_scan import decay_sensitivity_report


def _selected_ids(plan: Any) -> set[str]:
    return {a.kox_id for a in plan.selected}


def test_scan_covers_the_declared_policy_levels_and_the_reference(records, specs, thresholds) -> None:
    """扫描集合必须覆盖常量声明的三档，且把正式链路那一档并进来。"""
    rep = decay_sensitivity_report(records, specs, thresholds)
    assert rep["status"] == "ok"
    assert set(POST_DECAY_SCAN) <= set(rep["scan"])
    assert POST_MARGINAL_DECAY in rep["scan"]
    assert rep["reference_decay"] == POST_MARGINAL_DECAY
    # 常量名要写进产物：读者能顺着它找到唯一的定义处，而不是在文档里猜。
    assert rep["policy_constant"] == "budget.policy.POST_DECAY_SCAN"


def test_reference_level_reproduces_the_production_plan_exactly(records, specs, thresholds) -> None:
    """参照档必须与正式链路逐人一致。

    这是整条扫描的立足点：如果 0.7 档跑出来的方案与 `plan_campaign` 默认调用不同，
    那扫描衡量的是另一套东西，"结论稳定"就毫无意义。
    """
    rep = decay_sensitivity_report(records, specs, thresholds)
    ref = rep["reference_decay"]
    blocks = {b["campaign_id"]: b for b in rep["per_campaign"]}
    for spec in specs:
        if spec.is_neutral:
            continue
        plan, _ = plan_campaign(records, spec, thresholds)
        rows = {r["decay"]: r for r in blocks[spec.campaign_id]["by_decay"]}
        assert rows[ref]["n_selected"] == len(plan.selected)
        assert rows[ref]["spent_usd"] == pytest.approx(round(plan.spent_usd, 2))
        assert rows[ref]["n_posts"] == plan.n_posts


def test_decay_actually_reaches_the_allocator(records, specs, thresholds) -> None:
    """三档必须真的改变了分配结果 —— 防"参数没传进去"的假稳定。

    只断言"报告里三档数字不同"是不够的：那可能只是审计噪声。这里直接从 planner 层验证，
    换档之后**买的条数**必然变化（`effective_posts` 是 decay 的函数，贪心的加买意愿随之变）。
    """
    spec = next(s for s in specs if not s.is_neutral)
    posts: set[int] = set()
    for level in (0.5, 0.7, 0.9):
        plan, _ = plan_campaign(records, spec, thresholds, decay=level)
        posts.add(plan.n_posts)
    assert len(posts) > 1, f"三档买到的条数完全相同（{posts}），decay 很可能没有传到 allocator"


def test_all_three_arms_honor_the_decay_argument(records, specs, thresholds) -> None:
    """三条臂都必须接受 decay —— 只给 KOXPilot 换档、对照臂不换，就是不公平对照。"""
    spec = next(s for s in specs if not s.is_neutral)
    _, results = plan_campaign(records, spec, thresholds)
    for fn in (plan_baseline, plan_diversified_no_gate):
        low = fn(records, spec, thresholds, results, decay=0.5)
        high = fn(records, spec, thresholds, results, decay=0.9)
        # 至少有一处可观测差异（条数或等效条数），否则说明 decay 被吞掉了。
        assert (low.n_posts, round(low.est_cpm_usd, 4)) != (
            high.n_posts,
            round(high.est_cpm_usd, 4),
        ), f"{fn.__name__} 在 0.5 与 0.9 档结果完全相同，decay 没有生效"


def test_selection_instability_is_reported_not_hidden(records, specs, thresholds) -> None:
    """选人重叠度必须被算出来并与阈值比对，且结论要与数字一致。"""
    rep = decay_sensitivity_report(records, specs, thresholds)
    st = rep["stability"]
    jac = st["selection_jaccard_min_vs_reference"]
    ovl = st["spend_overlap_share_min_vs_reference"]
    assert jac is not None and ovl is not None
    # selection_stable 必须是由数字推出来的，而不是写死的常量。
    expected = min(jac, ovl) >= st["threshold"]
    assert st["selection_stable"] is expected
    # 本项目的实测结论是"选人会变"。若某天它变成 True，说明分配对该假设不再敏感，
    # 那是好事但必须有人重新读一遍结论文案 —— 所以这里钉住当前事实。
    assert st["selection_stable"] is False
    assert "选谁" in rep["headline"]


def test_value_conclusion_signs_are_checked_across_levels(records, specs, thresholds) -> None:
    """三档的两段归因符号必须逐档检查，且报告要落每一档的金额。"""
    rep = decay_sensitivity_report(records, specs, thresholds)
    st = rep["stability"]
    for key in (
        "saved_usd_total_by_decay",
        "saved_usd_by_diversification_by_decay",
        "saved_usd_by_gating_and_quality_ranking_by_decay",
    ):
        by_decay = st[key]
        assert len(by_decay) == len(rep["scan"]), f"{key} 缺档位"
    signs = st["sign_stable"]
    assert set(signs) == {
        "saved_usd_total",
        "saved_usd_by_diversification",
        "saved_usd_by_gating_and_quality_ranking",
    }
    assert all(isinstance(v, bool) for v in signs.values())


def test_report_refuses_to_invent_numbers_without_a_campaign(records, specs, thresholds) -> None:
    """没有真实 campaign 时必须如实报状态，不许给一份看起来正常的空报告。"""
    rep = decay_sensitivity_report(records, [], thresholds)
    assert rep["status"] == "no_campaign_spec"
    assert "per_decay" not in rep
    assert "stability" not in rep


def test_report_is_deterministic_and_json_serializable(records, specs, thresholds) -> None:
    """同一输入两次调用必须逐字节相同 —— metrics.json 的可复现性依赖这一点。"""
    a = decay_sensitivity_report(records, specs, thresholds)
    b = decay_sensitivity_report(records, specs, thresholds)
    assert json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(
        b, ensure_ascii=False, sort_keys=True
    )


def test_scan_does_not_claim_to_identify_the_true_decay(records, specs, thresholds) -> None:
    """必须写明这条扫描回答不了"哪档更接近真实世界"。

    这不是措辞洁癖：敏感性扫描很容易被读成"我们验证了 0.7 是对的"。产物里必须堵住这个读法。
    """
    rep = decay_sensitivity_report(records, specs, thresholds)
    assert "建模假设" in rep["assumption_kind"]
    assert "不回答" in rep["note"]
