"""数据集可复现性与 schema 完整性（SPEC 3.1 / 3.2 / 3.3）。

这组测试回答的问题是最基础也最容易被糊弄的一个：
**"你给我的这 5,000 条数据，我自己能不能一模一样地再造一遍？"**

如果答案是否，那么后面所有指标都无法回归对比——指标变了你分不清是代码变了还是数据抖了。
因此这里断言的是"逐字节相同"，而不是"统计上差不多"。

假设与口径（写在这里，避免读者猜）
----------------------------------
- 官方种子/规模取自 ``datagen.config``（SEED / N_KOX），不在测试里另写常量。
- 落盘一致性用 ``io_utils.dump_dataset`` 的确定性序列化重新写到 ``tmp_path`` 后与
  仓库里的 ``data/kox_5000.json`` **比字节**；测试绝不改仓库文件。
- 数据集里所有具体分布（gt 三档、注入比例）一律用区间断言，不绑等号：
  这些数字会随生成器的合理演进而移动，绑死等号只会制造假红灯。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from koxpilot.datagen import config as C
from koxpilot.datagen.briefs import build_briefs
from koxpilot.datagen.generator import build_kox, generate_dataset, plan_injections
from koxpilot.io_utils import dump_dataset
from koxpilot.taxonomy import (
    AGE_BUCKETS,
    BUCKET_BOUNDS,
    CATEGORIES,
    COUNTRIES,
    PLATFORMS,
    follower_bucket,
)

# SPEC 3.2 的字段清单：每条记录都必须**存在**这些 key（值可以是 None = 注入的缺失）
REQUIRED_FIELDS: tuple[str, ...] = (
    "kox_id",
    "handle",
    "platform",
    "country",
    "language",
    "verified",
    "account_age_days",
    "followers",
    "following",
    "posts",
    "declared_categories",
    "observed_categories",
    "source_tags",
    "avg_views",
    "avg_likes",
    "avg_comments",
    "avg_shares",
    "engagement_rate",
    "view_follower_ratio",
    "comment_like_ratio",
    "follower_history",
    "viral_months",
    "audience_geo",
    "audience_age",
    "audience_gender",
    "quoted_price_usd",
    "avg_cpm_usd",
    "past_collabs",
    "content_flags",
    "controversy",
    "comment_dup_rate",
    "comment_emoji_only_rate",
    "comment_lang_mismatch_rate",
    "_missing",
    "gt",
)

GT_FIELDS: tuple[str, ...] = (
    "is_fraud",
    "fraud_type",
    "tag_mismatch",
    "brand_safety",
    "verdict",
    "true_categories",
    "mismatch_injected",
)


# ---------------------------------------------------------------------------
# 1. 同种子两次生成必须完全一致
# ---------------------------------------------------------------------------
def test_same_seed_generates_identical_dataset() -> None:
    a = generate_dataset(300, 20270919)
    b = generate_dataset(300, 20270919)
    assert a["kox"] == b["kox"], "同种子两次生成的达人记录不一致：生成器存在隐式全局状态"
    assert a["meta"]["injection_actual_counts"] == b["meta"]["injection_actual_counts"]


def test_injection_plan_is_deterministic() -> None:
    p1 = plan_injections(500, 777)
    p2 = plan_injections(500, 777)
    assert p1.fraud_type == p2.fraud_type
    assert p1.tag_mismatch == p2.tag_mismatch
    assert p1.missing == p2.missing
    assert p1.as_summary() == p2.as_summary()


def test_different_seed_changes_dataset() -> None:
    """反向哨兵：如果换种子数据也不变，说明"可复现"其实是"写死了"。"""
    a = generate_dataset(300, 20270919)
    b = generate_dataset(300, 20270920)
    assert a["kox"] != b["kox"], "换了种子数据却没变，说明随机性根本没接进生成过程"


def test_single_record_is_reproducible_in_isolation() -> None:
    """单条记录只依赖 (index, plan, seed)，不依赖前面生成过多少条。"""
    plan = plan_injections(200, C.SEED)
    first = build_kox(137, plan, set(), C.SEED)
    second = build_kox(137, plan, set(), C.SEED)
    assert first == second


def test_briefs_are_deterministic() -> None:
    assert build_briefs() == build_briefs()


# ---------------------------------------------------------------------------
# 2. 规模 / 主键 / schema
# ---------------------------------------------------------------------------
def test_dataset_size_and_unique_ids(records: list[dict[str, Any]]) -> None:
    assert len(records) == C.N_KOX
    ids = [r["kox_id"] for r in records]
    assert len(set(ids)) == len(ids), "kox_id 出现重复，后续按 id 索引的评测会静默串号"
    assert ids[0] == "KOX-000001" and ids[-1] == f"KOX-{C.N_KOX:06d}"
    assert ids == sorted(ids), "kox_id 未按序生成，落盘顺序不稳定会破坏逐字节复现"


def test_handles_are_unique(records: list[dict[str, Any]]) -> None:
    handles = [r["handle"] for r in records]
    assert len(set(handles)) == len(handles)


def test_every_record_has_full_schema(records: list[dict[str, Any]]) -> None:
    for rec in records:
        missing = [f for f in REQUIRED_FIELDS if f not in rec]
        assert not missing, f"{rec.get('kox_id')} 缺字段 {missing}"
        gt_missing = [f for f in GT_FIELDS if f not in rec["gt"]]
        assert not gt_missing, f"{rec['kox_id']} 的 gt 缺字段 {gt_missing}"


def test_declared_missing_fields_are_actually_none(records: list[dict[str, Any]]) -> None:
    """``_missing`` 是自述清单，必须与实际的 None 完全对齐（否则 G0 完整度算的是假账）。"""
    for rec in records:
        declared = set(rec["_missing"])
        assert declared <= set(C.MISSING_FIELD_CANDIDATES)
        actual = {f for f in C.MISSING_FIELD_CANDIDATES if rec.get(f) is None}
        assert declared == actual, f"{rec['kox_id']} _missing={sorted(declared)} 实际 None={sorted(actual)}"


def test_enum_fields_stay_in_vocabulary(records: list[dict[str, Any]]) -> None:
    for rec in records:
        assert rec["platform"] in PLATFORMS
        assert rec["country"] in COUNTRIES
        assert set(rec["declared_categories"]) <= set(CATEGORIES)
        assert set(rec["observed_categories"]) <= set(CATEGORIES)
        assert set(rec["gt"]["true_categories"]) <= set(CATEGORIES)
        assert rec["observed_categories"], "观测品类为空：内容分析系统不该返回空结果"
        assert rec["gt"]["brand_safety"] in {"none", "low", "high"}
        assert rec["gt"]["verdict"] in {"pass", "review", "reject"}


def test_numeric_invariants(records: list[dict[str, Any]]) -> None:
    """派生量必须与原子量自洽——数据集自身不自洽的话，门禁的所有比值都无意义。"""
    for rec in records:
        history = rec["follower_history"]
        assert len(history) == C.HISTORY_MONTHS
        assert all(v > 0 for v in history)
        if rec.get("followers") is not None:
            assert history[-1] == rec["followers"], f"{rec['kox_id']} 粉丝历史末点与 followers 不一致"
            assert follower_bucket(rec["followers"]) in BUCKET_BOUNDS
        views, er = rec.get("avg_views"), rec.get("engagement_rate")
        if views and er is not None:
            total = rec["avg_likes"] + rec["avg_comments"] + rec["avg_shares"]
            assert abs(total / views - er) < 5e-4, f"{rec['kox_id']} 互动率与互动量不自洽"
        for key in ("comment_dup_rate", "comment_emoji_only_rate", "comment_lang_mismatch_rate"):
            assert 0.0 <= rec[key] <= 0.95


def test_audience_profiles_are_normalized(records: list[dict[str, Any]]) -> None:
    for rec in records:
        age = rec["audience_age"]
        assert set(age) == set(AGE_BUCKETS)
        assert abs(sum(age.values()) - 1.0) < 1e-3
        gender = rec["audience_gender"]
        assert abs(sum(gender.values()) - 1.0) < 1e-3
        geo = rec.get("audience_geo")
        if geo:  # 可能被注入为 None
            assert abs(sum(geo.values()) - 1.0) < 5e-3
            assert all(v >= 0 for v in geo.values())


# ---------------------------------------------------------------------------
# 3. 注入比例与 gt 合成规则
# ---------------------------------------------------------------------------
def test_fraud_types_are_mutually_exclusive_and_on_target(records: list[dict[str, Any]]) -> None:
    """4 类水号互斥；总比例落在目标值附近（用区间，不绑等号）。"""
    n = len(records)
    fraud = [r for r in records if r["gt"]["is_fraud"]]
    for r in fraud:
        assert r["gt"]["fraud_type"] in C.FRAUD_TYPES
    target = sum(C.INJECTION_RATES[f] for f in C.FRAUD_TYPES)
    assert abs(len(fraud) / n - target) < 0.01
    for ftype in C.FRAUD_TYPES:
        share = sum(1 for r in fraud if r["gt"]["fraud_type"] == ftype) / n
        assert abs(share - C.INJECTION_RATES[ftype]) < 0.005
    # 非水号必须 fraud_type=None，否则评测按类型分报时会把干净号算进去
    assert all(r["gt"]["fraud_type"] is None for r in records if not r["gt"]["is_fraud"])


def test_gt_verdict_matches_documented_composition(dataset: dict[str, Any]) -> None:
    """逐条复算 SPEC 3.3 末尾的 gt.verdict 合成规则（reject > review > pass）。

    注意 else 分支：``audience_geo`` 错位不会在 gt 里留字段，所以必须回到注入计划
    (``plan.geo_mismatch``) 才能判定。用 ``verdict == "review"`` 自己去证明
    "可能是 geo 导致的" 是循环论证，那种断言永远为真、抓不到任何回归。
    """
    records = dataset["kox"]
    plan = plan_injections(len(records), dataset["meta"]["seed"])
    for idx, rec in enumerate(records):  # 注入计划用 0-based index 索引
        gt = rec["gt"]
        has_missing = bool(rec["_missing"])
        geo_mismatch = idx in plan.geo_mismatch
        if gt["is_fraud"] or gt["brand_safety"] == "high":
            expected = "reject"
        elif gt["tag_mismatch"] or gt["brand_safety"] == "low" or has_missing or geo_mismatch:
            expected = "review"
        else:
            expected = "pass"
        assert gt["verdict"] == expected, (
            f"{rec['kox_id']} gt.verdict={gt['verdict']} 但按注入计划应为 {expected}"
            f"（fraud={gt['is_fraud']} tag={gt['tag_mismatch']} bs={gt['brand_safety']} "
            f"missing={has_missing} geo={geo_mismatch}）"
        )


def test_verdict_counts_are_reported_and_balanced(dataset: dict[str, Any]) -> None:
    """meta 里自报的三档分布必须与实际一致，且三档都有足够样本可评测。

    区间取得很宽（±10pp 量级）：这里要防的是"某一档塌成 0"这种结构性问题，
    而不是把当前实测值（pass 3058 / review 1034 / reject 908）钉死。
    """
    counts = dataset["meta"]["verdict_counts"]
    records = dataset["kox"]
    recount = {v: sum(1 for r in records if r["gt"]["verdict"] == v) for v in counts}
    assert counts == recount, "meta.verdict_counts 与逐条 gt 不一致（自报数字不可信）"
    n = len(records)
    assert 0.45 <= counts["pass"] / n <= 0.75
    assert 0.10 <= counts["review"] / n <= 0.35
    assert 0.10 <= counts["reject"] / n <= 0.30


def test_natural_outliers_only_on_clean_accounts(dataset: dict[str, Any]) -> None:
    """天然极端值只注入非水号——否则"正常号的假阳性来源"这个设计就不成立。"""
    plan = plan_injections(len(dataset["kox"]), dataset["meta"]["seed"])
    assert plan.natural_outlier, "没有任何天然极端值样本：假阳性来源缺失，precision 会虚高"
    assert not (set(plan.natural_outlier) & set(plan.fraud_type))


def test_weak_injections_exist(dataset: dict[str, Any]) -> None:
    """强弱偏移都要有：全 strong 会让召回虚高，全 weak 则没有可检出信号。"""
    plan = plan_injections(len(dataset["kox"]), dataset["meta"]["seed"])
    strong = sum(1 for i, v in plan.fraud_strong.items() if v and i in plan.fraud_type)
    total = len(plan.fraud_type)
    assert 0.3 < strong / total < 0.9, f"强偏移占比 {strong / total:.2f} 落在极端，评测会失真"


# ---------------------------------------------------------------------------
# 4. 落盘产物与当前代码一致（逐字节）
# ---------------------------------------------------------------------------
def test_disk_dataset_matches_regenerated_bytes(
    disk_dataset: dict[str, Any], tmp_path: Path
) -> None:
    """``data/kox_5000.json`` 必须能被当前代码 + 其自报种子逐字节重建。

    重建结果写在 tmp_path，仓库文件只读。
    meta 里含 ``calibrated``/时间类字段时本测试会失败——这是刻意的：
    数据集 meta 不允许出现时间戳（否则永远无法做逐字节回归）。
    """
    seed = int(disk_dataset["meta"]["seed"])
    n = int(disk_dataset["meta"]["n"])
    rebuilt = generate_dataset(n, seed)
    out = dump_dataset(tmp_path / "rebuilt.json", rebuilt)
    disk_path = Path(__file__).resolve().parents[1] / "data" / "kox_5000.json"
    assert out.read_bytes() == disk_path.read_bytes(), (
        "落盘数据集与当前代码重新生成的结果不一致："
        "要么生成器改了没重跑 `make data`，要么序列化不确定"
    )


def test_disk_sample_is_prefix_of_full(disk_dataset: dict[str, Any]) -> None:
    path = Path(__file__).resolve().parents[1] / "data" / f"kox_sample_{C.SAMPLE_SIZE}.json"
    if not path.exists():
        pytest.skip("无样本文件")
        return
    sample = json.loads(path.read_text("utf-8"))
    assert sample["kox"] == disk_dataset["kox"][: C.SAMPLE_SIZE]
