"""A4 LLM 适配分口径审计的守卫测试（``eval/llm_fit.py``）。

为什么需要单独一组测试
----------------------
"我们用 LLM 做了语义适配打分"这句话，在正式链路走 ``rule_fit_score`` 的情况下是**半句真话**：
LLM 分确实真调过、确实固化在 ``output/llm_cache.json`` 里，但 ``make budget`` / ``make eval``
的每一个数字都跟它无关。以前这个落差只在文档里"承认"了一句，没有任何数字——
于是既无法验证，也无法被推翻，属于典型的"看起来诚实、实际站不住"。

本文件钉住四类不许发生的事：
1. **不许把结论写死**：不予升格是**由覆盖率 / 双算率 / 单向性三项现算判据**得出的。
   构造一份"覆盖率满、双向、无重叠"的假缓存时，结论必须自动翻成"可升格"——
   如果翻不过来，说明代码里写死了一句"我们决定不接"，那不是判据，是口号。
2. **不许悄悄注入**：正式链路的 ``fit_source`` 必须全是规则版/跳过，一条 ``injected:`` 都不许有。
   这条是"文档说走规则版"这句声称的兵器化：谁在正式链路里偷接 LLM 分，这里立刻红。
3. **不许伪造覆盖**：缓存缺失 / brief 对不上 / kox_id 不在数据集里，都必须如实报状态，
   不许退化成"覆盖率 0% 但照常给出一堆均值"，也不许给出复跑不了的结论。
4. **不许声称准确率**：数据集 gt 里没有"语义适配"这一维，所以这张表只许报一致性与下游影响，
   不许出现任何 accuracy / precision / recall / f1 字段——LLM 适配分的"准确率"目前无法被评。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from koxpilot.budget.planner import gate_results_for, plan_campaign
from koxpilot.budget.value import build_candidates
from koxpilot.eval.llm_fit import (
    COVERAGE_MIN_FOR_PROMOTION,
    DOUBLE_COUNT_MAX_FOR_PROMOTION,
    FIT_INJECTION_DECISION_KEEP_RULE,
    FIT_INJECTION_DECISION_PROMOTABLE,
    llm_fit_audit,
    load_llm_fit_scores,
)
from koxpilot.gates.g2 import rule_fit_score
from koxpilot.gates.policy import FIT_SCORE_REVIEW_MAX
from koxpilot.gates.thresholds import Thresholds
from koxpilot.types import CampaignSpec

ACCURACY_WORDS = ("accuracy", "precision", "recall", "f1", "auc")


# ---------------------------------------------------------------------------
# 工具：写一份假的 llm_cache.json（结构与真实缓存一致，分值由测试指定）
# ---------------------------------------------------------------------------
def write_cache(
    tmp_path: Path,
    fit_scores: dict[str, dict[str, float]],
    meta: dict[str, Any] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "_meta": meta
        or {
            "models": {"fake": "fake-model-v0"},
            "primary_model_key": "fake",
            "fit_sample_n": sum(len(v) for v in fit_scores.values()),
            "seed": 7,
        },
        "fit_scores": {
            bid: {kid: {"kox_id": kid, "fit_score": score} for kid, score in scores.items()}
            for bid, scores in fit_scores.items()
        },
    }
    path = tmp_path / "llm_cache.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    return path


def pool_ids(
    records: list[dict[str, Any]], spec: CampaignSpec, thresholds: Thresholds
) -> list[str]:
    """本 campaign 真实候选池（定向通过 + 门禁 pass）的 kox_id。"""
    res = gate_results_for(records, spec, thresholds)
    candidates, _ = build_candidates(records, res, spec, thresholds, ("pass",))
    return [c.kox_id for c in candidates]


@pytest.fixture(scope="module")
def small_spec() -> CampaignSpec:
    """一个有品类/市场/语言要求的 spec：适配分只有在有品类要求时才参与判定。"""
    return CampaignSpec(
        campaign_id="BRIEF-TEST",
        name="测试用 campaign",
        target_categories=("beauty_care",),
        target_markets=("US",),
        platforms=("tiktok", "youtube", "instagram"),
        budget_usd=50_000.0,
        kpi="reach",
    )


def walk_keys(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            out.append(str(key))
            out.extend(walk_keys(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(walk_keys(item))
    return out


# ---------------------------------------------------------------------------
# 1. 缓存缺失 / 对不上：如实报状态，不给任何数字
# ---------------------------------------------------------------------------
def test_missing_cache_reports_not_run_instead_of_zero_coverage(
    small_records: list[dict[str, Any]],
    thresholds: Thresholds,
    small_spec: CampaignSpec,
    tmp_path: Path,
) -> None:
    out = llm_fit_audit(small_records, [small_spec], thresholds, tmp_path / "nope.json")
    assert out["status"] == "llm_fit_not_run"
    assert "koxpilot.llm.runner" in out["rerun"]
    # 关键：不许在没跑 LLM 的情况下给出任何看起来像结论的数字
    assert "totals" not in out and "per_campaign" not in out and "decision" not in out


def test_brief_id_mismatch_is_reported_not_silently_empty(
    small_records: list[dict[str, Any]],
    thresholds: Thresholds,
    small_spec: CampaignSpec,
    tmp_path: Path,
) -> None:
    cache = write_cache(tmp_path, {"BRIEF-SOMETHING-ELSE": {"KOX-000001": 0.4}})
    out = llm_fit_audit(small_records, [small_spec], thresholds, cache)
    assert out["status"] == "llm_fit_not_run"
    assert "brief_id 不匹配" in out["note"]


def test_scored_ids_outside_dataset_are_counted(
    small_records: list[dict[str, Any]],
    thresholds: Thresholds,
    small_spec: CampaignSpec,
    tmp_path: Path,
) -> None:
    ids = pool_ids(small_records, small_spec, thresholds)
    assert ids, "构造的 spec 在小样本上应有候选池"
    scores = {kid: 0.7 for kid in ids}
    scores["KOX-999999"] = 0.9  # 不在数据集里的号（缓存与数据集错配的典型症状）
    cache = write_cache(tmp_path, {"BRIEF-TEST": scores})
    out = llm_fit_audit(small_records, [small_spec], thresholds, cache)
    row = out["per_campaign"][0]
    assert row["n_scored_ids_not_in_dataset"] == 1
    assert row["scored_ids_not_in_dataset_sample"] == ["KOX-999999"]
    # 覆盖率的分母是候选池，分子只算"确实存在于数据集且在池里"的号
    assert row["coverage"]["candidate_pool"] == len(ids)
    assert row["coverage"]["covered_by_llm"] == len(ids)


def test_neutral_spec_only_is_refused(
    small_records: list[dict[str, Any]], thresholds: Thresholds, tmp_path: Path
) -> None:
    cache = write_cache(tmp_path, {"BRIEF-TEST": {"KOX-000001": 0.4}})
    out = llm_fit_audit(small_records, [CampaignSpec()], thresholds, cache)
    assert out["status"] == "no_campaign_spec"


def test_loader_ignores_malformed_entries(tmp_path: Path) -> None:
    path = tmp_path / "llm_cache.json"
    path.write_text(
        json.dumps(
            {
                "_meta": {"models": {"a": "m"}, "primary_model_key": "a"},
                "fit_scores": {
                    "BRIEF-001": {
                        "KOX-1": {"fit_score": 0.5},
                        "KOX-2": {"fit_score": "high"},  # 非数值：必须丢掉而不是当 0
                        "KOX-3": {"angle": "缺分值"},
                    }
                },
            },
            ensure_ascii=False,
        ),
        "utf-8",
    )
    scores, meta = load_llm_fit_scores(path)
    assert scores == {"BRIEF-001": {"KOX-1": 0.5}}
    assert meta["primary_model_key"] == "a"


# ---------------------------------------------------------------------------
# 2. 结论必须由判据算出来，不许写死
# ---------------------------------------------------------------------------
def test_full_coverage_two_sided_cache_flips_the_decision_to_promotable(
    small_records: list[dict[str, Any]],
    thresholds: Thresholds,
    small_spec: CampaignSpec,
    tmp_path: Path,
) -> None:
    """三项判据全过时，结论必须自动变成"可升格"。

    这是本文件最重要的一条：它证明"方案 B（继续用规则版）"是**算出来的**，
    不是代码里写死的一句话。构造方式：给候选池里每个人都打分（覆盖率 100%），
    一半略高于规则分、一半略低（双向），且全部 ≥ review 门槛（无双算争议）。
    """
    by_id = {str(r["kox_id"]): r for r in small_records}
    ids = pool_ids(small_records, small_spec, thresholds)
    assert len(ids) >= 4
    scores: dict[str, float] = {}
    for i, kid in enumerate(ids):
        rule = rule_fit_score(by_id[kid], small_spec)
        higher = min(1.0, rule + 0.05)
        lower = max(FIT_SCORE_REVIEW_MAX, rule - 0.05)
        scores[kid] = higher if i % 2 == 0 else lower
    out = llm_fit_audit(
        small_records, [small_spec], thresholds, write_cache(tmp_path, {"BRIEF-TEST": scores})
    )
    totals = out["totals"]
    assert totals["coverage_share"] == 1.0 >= COVERAGE_MIN_FOR_PROMOTION
    assert totals["n_llm_below_rule_above"] == 0  # 没有争议样本 => 没有双算问题
    assert out["checks"] == {
        "coverage_ok": True,
        "no_double_counting": True,
        "not_one_sided_on_covered": True,
    }
    assert out["decision"] == FIT_INJECTION_DECISION_PROMOTABLE
    assert out["blockers"] == []


def test_low_coverage_alone_blocks_promotion_and_says_why(
    small_records: list[dict[str, Any]],
    thresholds: Thresholds,
    small_spec: CampaignSpec,
    tmp_path: Path,
) -> None:
    by_id = {str(r["kox_id"]): r for r in small_records}
    ids = pool_ids(small_records, small_spec, thresholds)
    keep = ids[:1]  # 只覆盖一个人
    scores = {kid: min(1.0, rule_fit_score(by_id[kid], small_spec) + 0.05) for kid in keep}
    out = llm_fit_audit(
        small_records, [small_spec], thresholds, write_cache(tmp_path, {"BRIEF-TEST": scores})
    )
    assert out["decision"] == FIT_INJECTION_DECISION_KEEP_RULE
    assert out["checks"]["coverage_ok"] is False
    assert any("覆盖率不足" in b for b in out["blockers"])
    assert f"{COVERAGE_MIN_FOR_PROMOTION:.0%}" in " ".join(out["blockers"])


def test_one_sided_penalty_is_called_out_even_at_full_coverage(
    small_records: list[dict[str, Any]],
    thresholds: Thresholds,
    small_spec: CampaignSpec,
    tmp_path: Path,
) -> None:
    """覆盖率满、也没有双算争议，但 LLM 分只会更低：仍然不许升格。

    这正是真实缓存的形态（采样池偏向"品类正中"的号，规则版给满分），
    如果不拦住，"注入后指标变化"就会被单向罚分本身解释掉。
    """
    by_id = {str(r["kox_id"]): r for r in small_records}
    ids = pool_ids(small_records, small_spec, thresholds)
    scores = {
        kid: max(FIT_SCORE_REVIEW_MAX, rule_fit_score(by_id[kid], small_spec) - 0.1) for kid in ids
    }
    out = llm_fit_audit(
        small_records, [small_spec], thresholds, write_cache(tmp_path, {"BRIEF-TEST": scores})
    )
    assert out["totals"]["llm_only_lowers_on_covered"] is True
    assert out["checks"]["not_one_sided_on_covered"] is False
    assert out["decision"] == FIT_INJECTION_DECISION_KEEP_RULE
    assert any("单向偏置" in b for b in out["blockers"])


def test_double_counting_is_attributed_to_the_three_existing_rulers(
    small_records: list[dict[str, Any]],
    thresholds: Thresholds,
    small_spec: CampaignSpec,
    tmp_path: Path,
) -> None:
    """"LLM 判低、规则判高"的人必须逐项归因到已有口径，而不是笼统说"有重叠"。"""
    by_id = {str(r["kox_id"]): r for r in small_records}
    # 挑一批规则分 ≥ 门槛的人（含池外的号），LLM 全给 0.1 => 全是争议样本
    disputed = [
        str(r["kox_id"])
        for r in small_records
        if rule_fit_score(r, small_spec) >= FIT_SCORE_REVIEW_MAX
    ][:40]
    assert len(disputed) >= 10
    scores = {kid: 0.1 for kid in disputed}
    out = llm_fit_audit(
        small_records, [small_spec], thresholds, write_cache(tmp_path, {"BRIEF-TEST": scores})
    )
    dbl = out["per_campaign"][0]["double_counted"]
    assert dbl["n_llm_below_rule_above"] == len(disputed)
    # 三项归因数之和不小于"被任一项覆盖"的人数（同一个人可能同时命中多项）
    assert (
        dbl["already_off_target_by_targeting"]
        + dbl["already_flagged_by_G2_4_language"]
        + dbl["already_low_audience_match"]
        >= dbl["already_covered_by_any"]
    )
    assert 0.0 <= float(dbl["already_covered_share"]) <= 1.0
    for example in dbl["examples"]:
        assert example["llm_fit"] < FIT_SCORE_REVIEW_MAX <= example["rule_fit"]
        assert (
            example["targeting_reason"] is not None
            or example["hits_G2_4_language"] is True
            or float(example["audience_match"]) < FIT_SCORE_REVIEW_MAX
        )
    assert DOUBLE_COUNT_MAX_FOR_PROMOTION < 1.0


def test_empty_candidate_pool_leaves_coverage_undefined_not_zero(
    small_records: list[dict[str, Any]], thresholds: Thresholds, tmp_path: Path
) -> None:
    """候选池为空时覆盖率必须留空。给 0.0 会被读成"有池子但一个都没覆盖"。"""
    spec = CampaignSpec(
        campaign_id="BRIEF-TEST",
        name="平台清单里没有任何真实平台",
        target_categories=("beauty_care",),
        platforms=("platform-that-does-not-exist",),
        budget_usd=10_000.0,
    )
    scores = {str(small_records[0]["kox_id"]): 0.6}
    cache = write_cache(tmp_path, {"BRIEF-TEST": scores})
    out = llm_fit_audit(small_records, [spec], thresholds, cache)
    row = out["per_campaign"][0]
    assert row["coverage"]["candidate_pool"] == 0
    assert row["coverage"]["coverage_share"] is None
    assert out["totals"]["coverage_share"] is None
    assert out["decision"] == FIT_INJECTION_DECISION_KEEP_RULE


# ---------------------------------------------------------------------------
# 3. 只读不写：审计不许影响正式链路
# ---------------------------------------------------------------------------
def test_audit_does_not_touch_the_formal_plan(
    small_records: list[dict[str, Any]],
    thresholds: Thresholds,
    small_spec: CampaignSpec,
    tmp_path: Path,
) -> None:
    before, res_before = plan_campaign(small_records, small_spec, thresholds)
    ids = pool_ids(small_records, small_spec, thresholds)
    # 只覆盖一半候选池、分值压到规则版之下但仍在 review 门槛之上：
    # 这样池子里必然同时存在 injected 与 rule 两种来源，"混合尺子"才看得见。
    scores = {kid: 0.55 for kid in ids[: max(1, len(ids) // 2)]}
    out = llm_fit_audit(
        small_records, [small_spec], thresholds, write_cache(tmp_path, {"BRIEF-TEST": scores})
    )
    after, res_after = plan_campaign(small_records, small_spec, thresholds)
    assert [a.kox_id for a in before.selected] == [a.kox_id for a in after.selected]
    assert {k: v.fit_source for k, v in res_before.items()} == {
        k: v.fit_source for k, v in res_after.items()
    }
    assert out["formal_chain_fit_source"] == "rule:category_map+keyword"
    # 反事实里的混合口径必须可见：同一个池子里既有 injected 也有 rule
    mix = out["per_campaign"][0]["injection_counterfactual"]["fit_source_mix_in_pool"]
    assert "injected:llm_fit_score" in mix


def test_injection_counterfactual_reports_both_arms_with_gt(
    small_records: list[dict[str, Any]],
    thresholds: Thresholds,
    small_spec: CampaignSpec,
    tmp_path: Path,
) -> None:
    by_id = {str(r["kox_id"]): r for r in small_records}
    ids = pool_ids(small_records, small_spec, thresholds)
    scores = {kid: max(0.0, rule_fit_score(by_id[kid], small_spec) - 0.6) for kid in ids}
    out = llm_fit_audit(
        small_records, [small_spec], thresholds, write_cache(tmp_path, {"BRIEF-TEST": scores})
    )
    cf = out["per_campaign"][0]["injection_counterfactual"]
    assert cf["n_selected_rule"] > 0 and cf["n_selected_llm"] >= 0
    assert cf["selection_overlap"] <= min(cf["n_selected_rule"], cf["n_selected_llm"])
    assert cf["wasted_delta_usd_llm_minus_rule"] == pytest.approx(
        cf["wasted_spend_usd_llm"] - cf["wasted_spend_usd_rule"], abs=0.02
    )
    # 大幅压低适配分必然把一部分人推出 pass 或推下排序
    assert cf["n_verdict_flips"] >= 1 or cf["candidate_pool_llm"] <= cf["candidate_pool_rule"]


def test_audit_is_deterministic(
    small_records: list[dict[str, Any]],
    thresholds: Thresholds,
    small_spec: CampaignSpec,
    tmp_path: Path,
) -> None:
    ids = pool_ids(small_records, small_spec, thresholds)
    cache = write_cache(tmp_path, {"BRIEF-TEST": {kid: 0.55 for kid in ids}})
    first = llm_fit_audit(small_records, [small_spec], thresholds, cache)
    second = llm_fit_audit(small_records, [small_spec], thresholds, cache)
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )


# ---------------------------------------------------------------------------
# 4. 不许声称"LLM 适配分的准确率"
# ---------------------------------------------------------------------------
def test_audit_never_claims_accuracy_of_llm_fit(
    small_records: list[dict[str, Any]],
    thresholds: Thresholds,
    small_spec: CampaignSpec,
    tmp_path: Path,
) -> None:
    ids = pool_ids(small_records, small_spec, thresholds)
    out = llm_fit_audit(
        small_records,
        [small_spec],
        thresholds,
        write_cache(tmp_path, {"BRIEF-TEST": {kid: 0.6 for kid in ids}}),
    )
    keys = [k.lower() for k in walk_keys(out)]
    assert not [k for k in keys if any(w in k for w in ACCURACY_WORDS)], (
        "语义适配没有 ground truth，报任何准确率类指标都是无源之水"
    )
    assert any("没有 ground truth" in c for c in out["caveats"])


# ---------------------------------------------------------------------------
# 5. 真实数据 + 真实缓存：正式链路必须仍是规则版
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def real_cache_path() -> Path:
    path = Path(__file__).resolve().parents[1] / "output" / "llm_cache.json"
    if not path.exists():
        pytest.skip("缺少 output/llm_cache.json（未跑过 make llm）")
    return path


def test_real_cache_audit_matches_the_documented_verdict(
    disk_dataset: dict[str, Any],
    specs: list[CampaignSpec],
    real_cache_path: Path,
) -> None:
    records = list(disk_dataset["kox"])
    from koxpilot.gates.thresholds import calibrate

    thr = calibrate(records, disk_dataset.get("meta"))
    out = llm_fit_audit(records, specs, thr, real_cache_path)
    if out["status"] != "ok":
        pytest.skip(f"缓存与当前 brief 对不上：{out.get('note')}")
    totals = out["totals"]
    # 覆盖率区间断言（不绑死实测值）：现状远低于升格门槛
    assert 0.0 < float(totals["coverage_share"]) < COVERAGE_MIN_FOR_PROMOTION
    assert float(totals["double_counted_share"]) > DOUBLE_COUNT_MAX_FOR_PROMOTION
    assert out["decision"] == FIT_INJECTION_DECISION_KEEP_RULE
    assert len(out["blockers"]) >= 2
    for row in out["per_campaign"]:
        assert row["on_all_scored"]["n"] > 0
        assert row["injection_counterfactual"]["wasted_spend_usd_rule"] >= 0.0


def test_formal_chain_has_no_injected_fit_source(
    disk_dataset: dict[str, Any], specs: list[CampaignSpec]
) -> None:
    """正式链路的每一条 GateResult 都不许是 ``injected:``。

    这是"正式指标用的是规则版适配分"这句声称的兵器化守卫：
    以后任何人把 LLM 分接进 ``gate_results_for``/``plan_campaign`` 而没有同步改口径说明，
    这条测试会立刻红——比事后在评审现场被问穿便宜得多。
    """
    records = list(disk_dataset["kox"])
    from koxpilot.gates.thresholds import calibrate

    thr = calibrate(records, disk_dataset.get("meta"))
    for spec in specs:
        results = gate_results_for(records, spec, thr)
        sources = {r.fit_source for r in results.values()}
        assert not any(s.startswith("injected") for s in sources), sources
        assert any(s.startswith("rule:") for s in sources)


def test_metrics_json_carries_the_fit_gap_quantified(repo_root: Path) -> None:
    path = repo_root / "output" / "metrics.json"
    if not path.exists():
        pytest.skip("缺少 output/metrics.json（先跑 make eval）")
    metrics = json.loads(path.read_text("utf-8"))
    audit = metrics["table_6_llm_vs_rule"]["semantic_fit_llm_vs_rule"]
    assert audit["status"] in {"ok", "llm_fit_not_run", "no_campaign_spec"}
    if audit["status"] != "ok":
        return
    assert audit["decision"] == FIT_INJECTION_DECISION_KEEP_RULE
    assert audit["formal_chain_fit_source"] == "rule:category_map+keyword"
    assert audit["blockers"], "不予升格必须给出具体判据，不能只给结论"
    note = " ".join(str(n) for n in metrics["honesty_notes"])
    assert "正式链路用的是规则版" in note
