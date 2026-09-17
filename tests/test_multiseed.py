"""``eval/multiseed.py`` 的测试。

测什么、不测什么
----------------
- **测**：统计工具的正确性（能对着教科书/已知封闭解核对的部分）、种子派生的确定性、
  单种子结果与既有 harness 口径一致、汇总函数在"造好的输入"上给出可预期的分类结论、
  以及"跑多种子不会写任何文件"这条硬约束。
- **不测**：具体指标落在哪个值（那是 ``output/multiseed.json`` 的事，用等号绑住会让
  测试变成记账本）。涉及实测值的断言一律用区间或方向。

为了跑得快，涉及完整链路的测试都用 ``N_SMALL`` 规模的库和 2 个种子；
统计工具的测试完全不跑链路。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from koxpilot.datagen.config import SEED
from koxpilot.eval import multiseed as ms

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 小规模：只验证逻辑成立，不关心分布形状（全量 5,000 × 12 种子在测试里太慢）
N_SMALL = 400


# ---------------------------------------------------------------------------
# 种子派生
# ---------------------------------------------------------------------------
def test_seed_list_starts_with_official_seed_and_is_deterministic() -> None:
    a = ms.seed_list(12)
    b = ms.seed_list(12)
    assert a == b, "种子列表必须完全确定性"
    assert a[0] == SEED, "第一个种子必须是原始种子，否则无法与既有结论对照"
    assert len(set(a)) == 12, "种子不能重复"
    assert all(a[i + 1] - a[i] == ms.SEED_STEP for i in range(len(a) - 1))


def test_seed_list_rejects_zero() -> None:
    with pytest.raises(ValueError):
        ms.seed_list(0)


# ---------------------------------------------------------------------------
# 统计工具：对着已知封闭解核对
# ---------------------------------------------------------------------------
def test_std_is_sample_std_ddof_one() -> None:
    # [1,2,3,4] 的样本标准差（ddof=1）= sqrt(5/3)
    assert ms._std([1, 2, 3, 4]) == pytest.approx(math.sqrt(5 / 3))
    assert ms._std([7]) == 0.0, "单点样本的 std 定义为 0，不能崩"


def test_summarize_shapes_and_ci_contains_mean() -> None:
    out = ms.summarize([0.1, 0.2, 0.3, 0.4])
    assert out["n"] == 4
    assert out["mean"] == pytest.approx(0.25)
    assert out["min"] == 0.1 and out["max"] == 0.4
    assert out["median"] == pytest.approx(0.25)
    assert out["ci95_low"] < out["mean"] < out["ci95_high"], "CI 必须包住均值"


def test_summarize_constant_series_has_zero_width_ci() -> None:
    out = ms.summarize([0.5] * 5)
    assert out["std"] == 0.0
    assert out["ci95_low"] == out["ci95_high"] == 0.5


def test_betainc_matches_known_values() -> None:
    # I_x(1,1) = x（Beta(1,1) 就是均匀分布）
    for x in (0.1, 0.37, 0.9):
        assert ms._betainc(1.0, 1.0, x) == pytest.approx(x, abs=1e-10)
    # 对称性 I_x(a,b) = 1 - I_{1-x}(b,a)
    assert ms._betainc(2.5, 3.5, 0.3) == pytest.approx(1 - ms._betainc(3.5, 2.5, 0.7), abs=1e-10)


def test_t_distribution_tail_and_quantile() -> None:
    # t 分布对称：P(T > 0) = 0.5
    assert ms._t_sf(0.0, 7) == pytest.approx(0.5, abs=1e-9)
    # df=1（Cauchy）：P(T > 1) = 0.25
    assert ms._t_sf(1.0, 1) == pytest.approx(0.25, abs=1e-6)
    # 教科书临界值：t_{0.975, df=11} ≈ 2.201，df=1 ≈ 12.706
    assert ms._t_ppf975(11) == pytest.approx(2.201, abs=0.002)
    assert ms._t_ppf975(1) == pytest.approx(12.706, abs=0.01)


def test_one_sample_t_test_signal_vs_noise() -> None:
    # 明显偏离 0 的一组数 -> 小 p
    strong = ms.t_test_one_sample([0.30, 0.28, 0.33, 0.31, 0.29], 0.0)
    assert strong["p_two_sided"] < 1e-4
    # 围绕 0 上下抖动 -> 不显著
    noisy = ms.t_test_one_sample([0.3, -0.28, 0.1, -0.15, 0.02], 0.0)
    assert noisy["p_two_sided"] > 0.3
    # 常数序列不能抛异常（std=0 的分支）
    assert ms.t_test_one_sample([0.2, 0.2, 0.2], 0.0)["p_two_sided"] == 0.0


def test_f_test_detects_variance_gap_and_survives_degenerate_input() -> None:
    wide = [0.0, 0.9, 0.05, 0.8, 0.02, 0.95, 0.4, 0.7]
    tight = [0.04, 0.05, 0.03, 0.06, 0.05, 0.04, 0.05, 0.04]
    res = ms.f_test_variance_ratio(wide, tight)
    assert res["f"] is not None and res["f"] > 1, "方差大的放前面，F 应 > 1"
    assert res["p_two_sided"] < 0.01
    # 同分布 -> 不显著
    same = ms.f_test_variance_ratio(wide, list(reversed(wide)))
    assert same["p_two_sided"] > 0.5
    # 一侧方差为 0 -> 明确返回 None 而不是 ZeroDivisionError
    degenerate = ms.f_test_variance_ratio(wide, [0.1] * 5)
    assert degenerate["f"] is None and degenerate["p_two_sided"] is None


def test_sig_keeps_small_p_values_visible() -> None:
    assert ms._sig(1.234e-9) == pytest.approx(1.23e-9)
    assert ms._sig(0.0) == 0.0


# ---------------------------------------------------------------------------
# 单种子链路
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def two_seed_payload() -> dict[str, Any]:
    return ms.run_multiseed(ms.seed_list(2), n=N_SMALL, workers=1)


def test_run_seed_matches_harness_pipeline_shape() -> None:
    row = ms.run_seed(SEED, n=N_SMALL)
    assert row["seed"] == SEED
    assert row["dataset"]["n"] == N_SMALL
    assert len(row["per_campaign"]) == 3, "3 个 brief 必须都跑到"
    assert row["value"]["budget_usd"] == pytest.approx(245000.0), "预算口径必须与 briefs.json 一致"
    for key in ("fraud_f1_strict", "fraud_auc", "verdict_accuracy", "verdict_macro_f1"):
        assert 0.0 <= row["gates"][key] <= 1.0
    # saved_usd 必须等于两臂浪费金额之差（口径一致性，防止各算一套）
    for c in row["per_campaign"]:
        assert c["saved_usd"] == pytest.approx(
            c["baseline_wasted_usd"] - c["koxpilot_wasted_usd"], abs=0.02
        )
        assert c["koxpilot_loses"] == (c["saved_usd"] < 0)


def test_run_seed_is_deterministic() -> None:
    assert ms.run_seed(SEED, n=N_SMALL) == ms.run_seed(SEED, n=N_SMALL)


def test_different_seeds_give_different_datasets_but_same_campaigns() -> None:
    a, b = ms.run_seed(SEED, n=N_SMALL), ms.run_seed(SEED + ms.SEED_STEP, n=N_SMALL)
    assert a["dataset"]["verdict_counts"] != b["dataset"]["verdict_counts"], (
        "换种子必须换出不同的库，否则多种子实验什么也没检验"
    )
    assert [c["campaign_id"] for c in a["per_campaign"]] == [
        c["campaign_id"] for c in b["per_campaign"]
    ]
    assert [c["budget_usd"] for c in a["per_campaign"]] == [
        c["budget_usd"] for c in b["per_campaign"]
    ], "campaign 定义跨种子必须恒定，否则价值差额无法归因到数据随机性"


# ---------------------------------------------------------------------------
# 汇总结构
# ---------------------------------------------------------------------------
def test_payload_has_all_four_sections_and_reproducible_meta(
    two_seed_payload: dict[str, Any],
) -> None:
    p = two_seed_payload
    for key in (
        "A_value_robustness",
        "B_variance_attribution",
        "C_gate_metric_robustness",
        "D_weak_spot_stability",
        "per_seed",
        "headline",
    ):
        assert key in p
    meta = p["meta"]
    assert meta["seeds"] == ms.seed_list(2)
    assert meta["n_seeds"] == 2
    assert "multiseed" in meta["reproduce"], "meta 必须带可复现命令"
    assert "timestamp" not in meta and "generated_at" not in meta, (
        "产物不许带时间戳，否则无法做逐字节回归对比"
    )


def test_value_robustness_counts_losses_consistently(two_seed_payload: dict[str, Any]) -> None:
    a = two_seed_payload["A_value_robustness"]
    expected = sum(1 for s in two_seed_payload["per_seed"] if s["value"]["saved_usd"] < 0)
    assert a["n_seeds_koxpilot_loses_overall"] == expected
    assert len(a["seeds_koxpilot_loses_overall"]) == expected
    assert a["win_rate_overall"] == pytest.approx(1 - expected / 2)
    for cid, row in a["per_campaign"].items():
        n_lose = sum(
            1
            for s in two_seed_payload["per_seed"]
            for c in s["per_campaign"]
            if c["campaign_id"] == cid and c["koxpilot_loses"]
        )
        assert row["n_seeds_koxpilot_loses"] == n_lose


def test_variance_attribution_reports_both_arms_and_extreme_counts(
    two_seed_payload: dict[str, Any],
) -> None:
    b = two_seed_payload["B_variance_attribution"]
    pooled = b["pooled_all_campaigns"]
    assert pooled["n_obs"] == 2 * 3
    for band in b["conditional_win_rate_by_baseline_luck"]["bands"]:
        assert band["n_obs"] >= 0
        if band["n_obs"]:
            assert 0.0 <= band["koxpilot_win_rate"] <= 1.0
    n_bands = sum(x["n_obs"] for x in b["conditional_win_rate_by_baseline_luck"]["bands"])
    assert n_bands == pooled["n_obs"], "分档必须是全集划分，不能漏观测也不能重复计数"
    for _cid, row in b["per_campaign"].items():
        # 只锁结构：两臂都要真的选到人、浪费率都在 [0,1]。
        # "KOXPilot 比基线分散得多"是全量 5,000 人库上的**实证结论**（见 output/multiseed.json），
        # 在 N_SMALL=400 的库上候选池太小、结论本就不该成立，故不在此断言。
        assert row["baseline_n_selected"]["mean"] > 0
        assert row["koxpilot_n_selected"]["mean"] > 0
        for arm in ("baseline_waste_share", "koxpilot_waste_share"):
            assert 0.0 <= row[arm]["min"] <= row[arm]["max"] <= 1.0


def test_gate_metric_robustness_covers_required_metrics(two_seed_payload: dict[str, Any]) -> None:
    m = two_seed_payload["C_gate_metric_robustness"]["metrics"]
    for key in (
        "fraud_precision_strict",
        "fraud_recall_strict",
        "fraud_f1_strict",
        "fraud_auc",
        "verdict_accuracy",
        "verdict_macro_f1",
    ):
        assert m[key]["n"] == 2
        assert 0.0 <= m[key]["mean"] <= 1.0
        assert m[key]["std"] >= 0.0


def test_weak_spot_classification_logic_on_synthetic_input() -> None:
    """用**造好的**输入锁住分类逻辑，避免只在真实数据上"碰巧对"。"""

    def fake(seeds: int, cell_f1: float, jitter: float) -> list[dict[str, Any]]:
        out = []
        for i in range(seeds):
            f1 = cell_f1 + (jitter if i % 2 else -jitter)
            out.append(
                {
                    "seed": i,
                    "strata": {
                        "global_fraud_f1": 0.72,
                        "cells": {
                            "follower_bucket::mega": {
                                "f1": f1,
                                "positives": 20,
                                "n": 130,
                                "low_support": False,
                            },
                            "country::SA": {
                                "f1": 0.72,
                                "positives": 15,
                                "n": 90,
                                "low_support": False,
                            },
                            "platform×bucket::youtube|macro": {
                                "f1": 0.70,
                                "positives": 13,
                                "n": 84,
                                "low_support": False,
                            },
                        },
                        "top_weak": ["follower_bucket::mega"],
                    },
                }
            )
        return out

    # 稳定低于全局 0.25 -> 系统性弱点
    sys_out = ms.weak_spot_stability(fake(8, 0.47, 0.02))
    mega = next(c for c in sys_out["known_weak_spots"] if c["cell"] == "mega")
    assert mega["classification"] == "systematic_weak"
    # 与全局持平但抖动大 -> 采样噪声
    noise = next(c for c in sys_out["known_weak_spots"] if c["cell"] == "SA")
    assert noise["classification"] == "sampling_noise"
    # 幅度小但方向一致 -> mild_but_consistent
    mild = next(c for c in sys_out["known_weak_spots"] if c["cell"] == "youtube|macro")
    assert mild["classification"] == "mild_but_consistent"


def test_weak_spot_stability_on_real_two_seeds(two_seed_payload: dict[str, Any]) -> None:
    d = two_seed_payload["D_weak_spot_stability"]
    assert len(d["known_weak_spots"]) == len(ms.KNOWN_WEAK_SPOTS)
    for cell in d["known_weak_spots"]:
        assert cell["classification"] in {
            "systematic_weak",
            "mild_but_consistent",
            "sampling_noise",
        }
        assert cell["n_seeds_in_top_weak_list"] <= 2
    disc = d["systematic_weak_discovery"]
    assert disc["n_eligible_cells"] >= 1
    deltas = [r["delta_mean"] for r in disc["weakest_cells"]]
    assert deltas == sorted(deltas), "发现榜必须按 delta 升序（最弱在前）"


# ---------------------------------------------------------------------------
# 硬约束：不许污染既有产物
# ---------------------------------------------------------------------------
def test_multiseed_run_writes_nothing(tmp_path: Path) -> None:
    """跑多种子链路期间不得产生任何文件写入（这是"不污染既有产物"的机制保证）。"""
    watched = [REPO_ROOT / "data", REPO_ROOT / "output"]
    before = {
        f: f.stat().st_mtime_ns
        for d in watched
        if d.exists()
        for f in d.iterdir()
        if f.is_file()
    }
    ms.run_multiseed(ms.seed_list(2), n=N_SMALL, workers=1)
    after = {
        f: f.stat().st_mtime_ns
        for d in watched
        if d.exists()
        for f in d.iterdir()
        if f.is_file()
    }
    assert before == after, "run_multiseed 必须全程内存计算：落盘只能由 CLI 显式做"


def test_module_does_not_import_llm_layer() -> None:
    """确定性实验绝不许调 LLM：源码里不得出现对 llm 包的引用。"""
    src = (REPO_ROOT / "src" / "koxpilot" / "eval" / "multiseed.py").read_text("utf-8")
    assert "from ..llm" not in src and "koxpilot.llm" not in src
    assert "import requests" not in src and "urllib" not in src


def test_parallel_and_sequential_agree() -> None:
    """多进程结果必须与顺序结果完全一致（worker 无共享状态、无文件写入）。"""
    seq = ms.run_multiseed(ms.seed_list(2), n=N_SMALL, workers=1)
    par = ms.run_multiseed(ms.seed_list(2), n=N_SMALL, workers=2)
    assert seq == par
