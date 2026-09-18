"""README 里的关键数字必须与产物对得上 —— 一条防"文档漂移"的测试。

为什么需要这个文件
------------------
README 曾经写着一句"**没有一个数字是手写进文档的**"。这句话经不起追问：数字当然是手打进
markdown 的，它们只是**来源**于产物。真实的风险也正在这里——产物重跑之后数值变了，而文档
里的字不会自己改。这个项目已经栽过一次同类的跟头：``POST_DECAY_SCAN`` 的注释替一段不存在
的输出背书；``decay_scan`` 里的 ``caveats`` 算完没进产物。文档与产物脱钩是同一个毛病的
第三种形态。

所以这里不改口气、改机制：把 README 中**会被面试官逐个核对**的数字，逐条对到产物字段上。
数字漂了就红，而不是等人来发现。

三条编写纪律
------------
1. **断言的是"文档里确实出现了这个字符串"**，不是"产物里有这个值"。反向写法（拿产物的值
   去格式化再断言相等）会漏掉真正的故障模式：文档里写着旧值。
2. **格式必须和 README 里的写法一致**（千分位、百分号、正负号、有效位），因为文档面向人读，
   核对的也是人眼看到的那串字符。
3. **只钉"招牌数字"**。把每个数字都钉上会让测试变成 README 的副本，改一个字就红一片，
   最后只会被人 `-k` 跳过。这里选的是决策质量、业务价值、方差、成本这四类里被反复引用的值。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
OUTPUT = ROOT / "output"


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def metrics() -> dict[str, Any]:
    return json.loads((OUTPUT / "metrics.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def multiseed() -> dict[str, Any]:
    return json.loads((OUTPUT / "multiseed.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def llm_bench() -> dict[str, Any]:
    return json.loads((OUTPUT / "llm_bench.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def prompt_bench() -> dict[str, Any]:
    return json.loads((OUTPUT / "prompt_bench.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def audit() -> dict[str, Any]:
    return json.loads((OUTPUT / "audit.json").read_text(encoding="utf-8"))


def _assert_in_readme(readme: str, text: str, what: str) -> None:
    assert text in readme, f"README 里找不到 {what} 的当前值 `{text}` —— 产物变了但文档没跟上"


# ---------------------------------------------------------------- 决策质量


def test_fraud_metrics_match_readme(readme: str, metrics: dict[str, Any]) -> None:
    table = metrics["table_1_fraud_detection"]
    _assert_in_readme(readme, f"{table['strict']['f1']:.4f}", "水号识别 F1（严口径）")
    _assert_in_readme(readme, f"{table['strict']['precision']:.4f}", "水号识别精确率")
    _assert_in_readme(readme, f"{table['strict']['recall']:.4f}", "水号识别召回率")
    _assert_in_readme(readme, f"{table['auc']:.4f}", "水号识别 AUC")


def test_tag_mismatch_metrics_match_readme(readme: str, metrics: dict[str, Any]) -> None:
    tri = metrics["table_2_verdict_confusion"]
    _assert_in_readme(readme, f"{tri['accuracy']:.4f}", "标签错配三分类准确率")
    _assert_in_readme(readme, f"{tri['macro_f1']:.4f}", "标签错配 macro F1")


def test_cost_reduction_claim_matches_readme(readme: str, metrics: dict[str, Any]) -> None:
    """「相对全 LLM 方案缩减 3.95×」是成本叙事的招牌，必须与产物一致。"""
    cost = metrics["cost_audit"]
    _assert_in_readme(readme, f"{cost['call_reduction_vs_full_llm']:.2f}", "调用量缩减倍数")
    _assert_in_readme(readme, f"{cost['token_account']['measured_calls']} 次调用", "实测调用次数")


# ---------------------------------------------------------------- 业务价值


def test_single_seed_value_matches_readme(readme: str, metrics: dict[str, Any]) -> None:
    cf = metrics["counterfactual_value_audit"]["totals"]
    _assert_in_readme(readme, f"${cf['saved_usd']:,.0f}", "单种子少浪费金额")
    _assert_in_readme(readme, f"{cf['saved_share_of_budget'] * 100:.1f}%", "单种子少浪费占预算")


def test_multiseed_headline_matches_readme(readme: str, multiseed: dict[str, Any]) -> None:
    """21.5% ± 13.7% 是对外报的招牌口径，漂了必须红。"""
    a = multiseed["A_value_robustness"]
    share = a["saved_share_of_budget"]
    _assert_in_readme(readme, f"{share['mean'] * 100:.1f}%", "12 种子少浪费均值")
    _assert_in_readme(readme, f"{share['std'] * 100:.1f}%", "12 种子少浪费 std")
    _assert_in_readme(
        readme,
        f"{share['ci95_low'] * 100:.1f}%~{share['ci95_high'] * 100:.1f}%",
        "12 种子 95% CI",
    )
    assert (
        f"{a['n_seeds_koxpilot_loses_overall']}/{a['n_seeds']}" in readme
    ), "跑输基线的种子数没写进 README"


def test_arm_attribution_matches_readme(readme: str, multiseed: dict[str, Any]) -> None:
    """两段归因的金额与"几个种子为负"都要对上 —— 这是最容易被追问的一段。"""
    attr = multiseed["A_value_robustness"]["arm_attribution"]
    gate = attr["saved_usd_by_gating"]
    div = attr["saved_usd_by_diversification"]
    _assert_in_readme(readme, f"${gate['mean'] / 1000:.1f}k", "门禁贡献均值")
    _assert_in_readme(readme, f"${gate['std'] / 1000:.1f}k", "门禁贡献 std")
    _assert_in_readme(readme, f"${div['mean'] / 1000:.1f}k", "分散化贡献均值")
    _assert_in_readme(readme, f"${div['std'] / 1000:.1f}k", "分散化贡献 std")
    n = attr["n_seeds"]
    assert f"{attr['n_seeds_diversification_contribution_negative']}/{n}" in readme, (
        "分散化贡献为负的种子数没写进 README"
    )
    assert f"{attr['n_seeds_gating_contribution_negative']}/{n}" in readme, (
        "门禁贡献为负的种子数（0/12）没写进 README —— 这是「稳定的那一段是门禁」的直接证据"
    )
    assert f"{attr['n_seeds_third_arm_more_effective_views']}/{n}" in readme, (
        "第三臂绝对曝光更高的种子数没写进 README"
    )


def test_variance_story_matches_readme(readme: str, multiseed: dict[str, Any]) -> None:
    pooled = multiseed["B_variance_attribution"]["pooled_all_campaigns"]
    _assert_in_readme(
        readme, f"{pooled['baseline_waste_share']['std'] * 100:.1f}%", "基线浪费率 std"
    )
    _assert_in_readme(
        readme, f"{pooled['koxpilot_waste_share']['std'] * 100:.1f}%", "KOXPilot 浪费率 std"
    )
    _assert_in_readme(
        readme, f"{pooled['std_ratio_baseline_over_koxpilot']:.1f} 倍", "std 倍数"
    )
    n_obs = pooled["n_obs"]
    assert f"{pooled['n_obs_baseline_share_above_50pct']}/{n_obs}" in readme, "基线爆掉的局数没写"


def test_pooled_p_value_is_not_used_as_the_headline(readme: str, multiseed: dict[str, Any]) -> None:
    """pooled p 值已被判为不可用，README 必须写明它不可用，而不是拿它当证据。

    这条测的是**口径纪律**：伪重复下的 p 值不能出现在结论位置。
    """
    b = multiseed["B_variance_attribution"]
    assert b["independence_unit"]["pooled_p_value_usable"] is False
    pooled_p = f"{b['pooled_all_campaigns']['variance_ratio_test']['p_two_sided']:.1e}"
    if pooled_p in readme:
        idx = readme.index(pooled_p)
        window = readme[max(0, idx - 200) : idx + 200]
        assert any(w in window for w in ("不可当真", "不用", "伪重复", "高估")), (
            f"README 里出现了 pooled p 值 {pooled_p} 却没有紧跟'不可用'的限定"
        )
    # 独立单位上的 p 值必须出现（这才是可用的证据）
    for row in b["per_campaign"].values():
        p = row["variance_ratio_test"]["p_two_sided"]
        _assert_in_readme(readme, f"{p:.1e}", "逐 campaign 方差比检验 p 值")


# ---------------------------------------------------------------- 假设与降级


def test_decay_delivery_tiers_match_readme(readme: str, metrics: dict[str, Any]) -> None:
    """分层交付的人数/金额/占比三个数都在 README 表格里，必须逐个对上。"""
    policy = metrics["budget_decay_sensitivity"]["delivery_policy"]
    for tier in ("stable_core", "assumption_sensitive"):
        block = policy[tier]
        _assert_in_readme(readme, f"**{block['n_kox']}**" if tier == "stable_core" else str(block["n_kox"]), f"{tier} 人数")
        _assert_in_readme(readme, f"${block['amount_usd']:,.0f}", f"{tier} 金额")
        _assert_in_readme(readme, f"{block['share_of_spend'] * 100:.1f}%", f"{tier} 占比")


def test_decay_stability_numbers_match_readme(readme: str, metrics: dict[str, Any]) -> None:
    st = metrics["budget_decay_sensitivity"]["stability"]
    _assert_in_readme(readme, f"{st['spend_overlap_share_min_vs_reference']:.3f}", "金额加权重叠最低值")


# ---------------------------------------------------------------- 成本与模型


def test_llm_cost_numbers_match_readme(readme: str, llm_bench: dict[str, Any]) -> None:
    totals = llm_bench["totals"]
    _assert_in_readme(readme, f"{totals['total_tokens']:,}", "LLM 总 token")
    _assert_in_readme(readme, f"{totals['reasoning_tokens']:,}", "reasoning token")
    _assert_in_readme(readme, f"{totals['calls']} 次调用", "LLM 调用次数")


def test_promptbench_numbers_match_readme(readme: str, prompt_bench: dict[str, Any]) -> None:
    """v1 < v2 < v3 与"规则对照组"四个 F1 都要在 README 里。"""
    _assert_in_readme(readme, f"{prompt_bench['rule_baseline_f1']:.3f}", "规则对照组 F1")
    for version, block in prompt_bench["version_average"].items():
        _assert_in_readme(readme, f"{block['avg_f1']:.3f}", f"prompt {version} 平均 F1")
        _assert_in_readme(readme, f"{block['total_tokens']:,}", f"prompt {version} token")
    ordered = [b["avg_f1"] for _, b in sorted(prompt_bench["version_average"].items())]
    assert ordered == sorted(ordered), "v1<v2<v3 这个结论已经不成立，README 的叙事需要重写"


def test_third_arm_beats_us_numbers_match_readme(readme: str, audit: dict[str, Any]) -> None:
    """「第三臂在主指标上打赢了我」那一节的数字必须来自产物。

    这一节是对外材料里**最不利于自己**的一段，所以它比招牌数字更需要被钉住：
    一旦产物重跑后第三臂不再赢、或者赢的幅度变了，而 README 还挂着旧的
    "+47.4%"，那就是拿一段假的自我批评在博取信任 —— 比夸大成绩更糟。
    """
    per_campaign = {row["campaign_id"]: row for row in audit["counterfactual_value_audit"]["per_campaign"]}

    for campaign_id, row in per_campaign.items():
        kox = row["koxpilot"]
        third = row["diversified_no_gate"]
        base = row["baseline"]

        # 三臂的每千美元有效曝光都按人眼看到的千分位写法钉住
        for arm, label in ((kox, "KOXPilot"), (third, "第三臂"), (base, "基线")):
            _assert_in_readme(
                readme,
                f"{arm['effective_views_per_1k_usd']:,.0f}",
                f"{campaign_id} {label} 的每千美元有效曝光",
            )

        # 结论本身（第三臂更高）必须仍然成立，否则这一节的叙事要重写
        assert third["effective_views_per_1k_usd"] > kox["effective_views_per_1k_usd"], (
            f"{campaign_id} 上第三臂已经不再赢了 —— README 那节「第三臂在主指标上打赢了我」"
            "必须重写，而不是留着一段过期的自我批评"
        )
        gap = third["effective_views_per_1k_usd"] / kox["effective_views_per_1k_usd"] - 1
        _assert_in_readme(readme, f"+{gap * 100:.1f}%", f"{campaign_id} 第三臂领先幅度")

    # BRIEF-001 的取舍表：浪费、水号、高风险号、花在 reject 上的钱、合约数
    b1 = per_campaign["BRIEF-001"]
    kox1, third1 = b1["koxpilot"], b1["diversified_no_gate"]
    _assert_in_readme(readme, f"${kox1['wasted_spend_usd']:,.0f}", "BRIEF-001 我方浪费金额")
    _assert_in_readme(readme, f"${third1['wasted_spend_usd']:,.0f}", "BRIEF-001 第三臂浪费金额")
    _assert_in_readme(readme, f"{third1['wasted_spend_share'] * 100:.2f}%", "BRIEF-001 第三臂浪费占比")
    _assert_in_readme(readme, f"{kox1['wasted_spend_share'] * 100:.2f}%", "BRIEF-001 我方浪费占比")
    _assert_in_readme(
        readme, f"${third1['spend_on_gate_reject_usd']:,.0f}", "BRIEF-001 第三臂花在 reject 达人上的钱"
    )
    assert (
        f"| **{kox1['n_selected']} 份** | **{third1['n_selected']} 份** |" in readme
    ), "BRIEF-001 的合约数对比（69 份 vs 228 份）没写进 README —— 这是「可执行性」那一层的唯一量化证据"
    assert kox1["n_high_risk_selected"] == 0, "我方已经买进品牌安全高风险号了，README 的 0 不再成立"

    # BRIEF-002 那条对我们更不利的细节：浪费金额与有效曝光率都输给第三臂
    b2 = per_campaign["BRIEF-002"]
    kox2, third2, base2 = b2["koxpilot"], b2["diversified_no_gate"], b2["baseline"]
    assert kox2["wasted_spend_usd"] > third2["wasted_spend_usd"], (
        "BRIEF-002 上我方浪费已经不再高于第三臂 —— README 那段自陈需要更新"
    )
    _assert_in_readme(readme, f"${kox2['wasted_spend_usd']:,.0f}", "BRIEF-002 我方浪费金额")
    _assert_in_readme(readme, f"${third2['wasted_spend_usd']:,.0f}", "BRIEF-002 第三臂浪费金额")
    _assert_in_readme(readme, f"{kox2['effective_view_rate'] * 100:.2f}%", "BRIEF-002 我方有效曝光率")
    _assert_in_readme(readme, f"{third2['effective_view_rate'] * 100:.2f}%", "BRIEF-002 第三臂有效曝光率")
    _assert_in_readme(readme, f"{kox2['n_fraud_selected']} 个水号", "BRIEF-002 进入我方名单的水号个数")
    # 连按粉丝量基线都赢了我们这件事，也必须留在文档里
    assert base2["effective_views_per_1k_usd"] > kox2["effective_views_per_1k_usd"], (
        "BRIEF-002 上基线不再赢我们了 —— README 里那句「稳定赢基线也不成立」需要改回来"
    )
    _assert_in_readme(readme, f"${base2['wasted_spend_usd']:,.0f}", "BRIEF-002 基线浪费金额")


def test_test_count_claim_matches_reality(readme: str) -> None:
    """README 声称的测试数量必须与实际收集到的一致 —— 这句是最容易过期的自夸。

    计数方式刻意用"数 node id 行数"而不是解析 pytest 的总结行：总结行的措辞随
    ``addopts`` / 版本变化，解析失败会静默 skip，等于这条断言白写。
    """
    import os
    import subprocess

    m = re.search(r"\*\*(\d{2,4})\s*个测试\*\*", readme)
    assert m is not None, "README 未声称测试数量 —— 要么写上并被钉住，要么别写"
    claimed = int(m.group(1))

    env = dict(os.environ, PYTHONPATH="src")
    proc = subprocess.run(
        ["python", "-m", "pytest", "--collect-only", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    actual = sum(1 for line in proc.stdout.splitlines() if "::" in line)
    assert actual > 0, f"收集失败，无法核对：{proc.stdout[-300:]}{proc.stderr[-300:]}"
    assert claimed == actual, f"README 写 {claimed} 个测试，实际收集到 {actual} 个"
