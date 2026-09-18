"""HTTP 服务层的契约测试：``api/koxpilot_service`` 必须与 CLI 同口径、且严格按契约输出。

为什么把它放进主测试集
----------------------
接口层最容易悄悄漂移：有人为了让界面好看，在服务层多加一层"修正"，
于是同一个达人在 CLI 里是 review、在接口里变成 pass，这种事一旦发生就再也说不清。
所以这里断言的都是**口径**而不是数字：

- ``explain`` 的判定与证据文案必须与直接调 ``gates.engine.evaluate`` 完全一致
  （也就是与 CLI ``koxpilot explain`` 完全一致，CLI 本身就是这么调的）；
- ``plan`` 的 ``parity_payload.verdicts`` 必须覆盖本次召回集合的每一条、按 kox_id 升序；
- 对外文案里不允许出现代码标识符（界面刚做过一轮去内部化清洗，接口不能再灌回去）；
- 业务错误一律 ``ok:false`` + 契约里写明的错误码，不抛异常。

本文件不启 HTTP 服务（那需要 fastapi/uvicorn，属于部署期依赖），
只直接调服务层函数——契约字段的组装逻辑全在这一层，端点只是薄薄的一层转发。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
API_DIR = REPO_ROOT / "api"

# 服务层不是安装包，按部署时的布局把 api/ 放进 sys.path
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

pytest.importorskip("koxpilot_service", reason="api/ 目录不存在时跳过服务层契约测试")

from koxpilot_service import service as svc  # noqa: E402
from koxpilot_service.labels import looks_like_identifier  # noqa: E402
from koxpilot_service.store import STORE  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def service_ready() -> Any:
    """预热数据仓（读仓库里的真产物），并**关掉 LLM 路径**。

    关 LLM 是测试纪律（见 conftest 第 3 条）：A1 有凭据时会真调模型，
    测试里绝不允许发网络请求，所以这里强制走确定性规则解析。
    """
    import os

    previous = os.environ.get("KOXPILOT_DISABLE_LLM")
    os.environ["KOXPILOT_DISABLE_LLM"] = "1"
    assert STORE.wait_ready(120.0), "数据仓预热超时"
    assert STORE.is_ready(), "数据仓未就绪：%s" % STORE.error
    yield
    if previous is None:
        os.environ.pop("KOXPILOT_DISABLE_LLM", None)
    else:
        os.environ["KOXPILOT_DISABLE_LLM"] = previous


@pytest.fixture(scope="module")
def plan_payload() -> dict[str, Any]:
    return svc.plan(
        {
            "brief_text": "巴西和墨西哥的 Instagram 彩妆，18-24 女性，预算 8 万美金，避开 Focallure",
            "options": {"top_n": 500, "explain_limit": 60},
        }
    )


# ---------------------------------------------------------------------------
# explain 与 CLI 同口径
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kox_id", ["KOX-000002", "KOX-000004", "KOX-000005", "KOX-000007", "KOX-001146"]
)
def test_explain_matches_engine_verbatim(kox_id: str) -> None:
    from koxpilot.gates.engine import evaluate
    from koxpilot.types import CampaignSpec

    snap = STORE.snapshot()
    assert snap is not None
    expected = evaluate(snap.by_id[kox_id], CampaignSpec(), snap.thresholds)

    payload = svc.explain(kox_id)
    assert payload["ok"] is True
    assert payload["kox_id"] == kox_id
    assert payload["verdict"] == expected.verdict

    # 证据链逐条逐字一致（顺序也必须一致）
    hits = [hit["detail"] for gate in payload["gates"] for hit in gate["hits"]]
    assert hits == [r.human_text for r in expected.reasons]
    # reason_human 是同一批 human_text 拼出来的，所以每条都必须能逐字找到
    for text in hits:
        assert text in payload["reason_human"]


def test_explain_shape_and_not_found() -> None:
    payload = svc.explain("KOX-000002")
    assert set(payload) == {"ok", "kox_id", "profile", "signals", "gates", "verdict", "reason_human"}
    assert set(payload["profile"]) == {"handle", "platform", "market", "followers"}
    assert [g["gate"] for g in payload["gates"]] == ["G0", "G1", "G2", "G3"]
    for gate in payload["gates"]:
        assert gate["passed"] == (len(gate["hits"]) == 0)
    for sig in payload["signals"]:
        assert set(sig) == {"label", "value", "threshold", "verdict"}
        assert looks_like_identifier(sig["label"]) is None

    with pytest.raises(svc.ServiceError) as err:
        svc.explain("KOX-999999")
    assert err.value.code == "not_found"


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
def test_plan_shape(plan_payload: dict[str, Any]) -> None:
    assert set(plan_payload) == {
        "ok",
        "brief",
        "funnel",
        "candidates",
        "allocation",
        "timings",
        "parity_payload",
    }
    assert plan_payload["brief"]["parse_path"] in ("rule", "llm")
    assert isinstance(plan_payload["brief"]["notes"], list)
    assert [f["stage"] for f in plan_payload["funnel"]] == ["recall", "gate", "allocated"]
    # 漏斗必须逐级收窄，否则说明某一层的口径串了
    counts = [f["count"] for f in plan_payload["funnel"]]
    assert counts[0] >= counts[1] >= counts[2]
    assert [t["agent"] for t in plan_payload["timings"]] == ["A1", "A2", "A3", "A4", "A5", "A6"]
    for row in plan_payload["timings"]:
        assert row["ms"] >= 0


def test_plan_parity_payload_covers_recall_sorted(plan_payload: dict[str, Any]) -> None:
    from koxpilot.budget.value import targeting_reason
    from koxpilot.types import CampaignSpec

    rows = plan_payload["parity_payload"]["verdicts"]
    ids = [r["kox_id"] for r in rows]
    assert ids == sorted(ids), "契约要求按 kox_id 升序"
    assert len(ids) == len(set(ids))
    assert len(ids) == plan_payload["funnel"][0]["count"]
    for row in rows:
        assert row["verdict"] in ("pass", "review", "reject")

    # 召回集合确实是定向筛选的结果（本例 182 人 < top_n，不涉及截断）
    snap = STORE.snapshot()
    assert snap is not None
    spec = CampaignSpec.from_dict(
        {
            "campaign_id": "CUSTOM",
            "target_categories": ["beauty_care", "fashion"],
            "target_markets": ["BR", "MX"],
            "platforms": ["instagram"],
        }
    )
    pool = {str(k["kox_id"]) for k in snap.records if targeting_reason(k, spec) is None}
    assert set(ids) == pool


def test_plan_candidates_are_human_readable(plan_payload: dict[str, Any]) -> None:
    rows = plan_payload["candidates"]
    assert rows, "本例应有候选"
    assert len(rows) <= 60
    for row in rows:
        assert set(row) == {
            "kox_id",
            "handle",
            "platform",
            "market",
            "followers",
            "verdict",
            "gate_hits",
            "fit_score",
            "fit_source",
            "reason_human",
            "allocated_usd",
            "expected_reach",
        }
        assert row["fit_source"] in ("cached_llm", "rule_fallback")
        assert looks_like_identifier(row["reason_human"]) is None
        for hit in row["gate_hits"]:
            assert hit["gate"] in ("G0", "G1", "G2", "G3")
            assert looks_like_identifier(hit["gate_label"]) is None
            assert looks_like_identifier(hit["signal_label"]) is None
    # 进了清单的排在前面，金额降序
    allocated = [r["allocated_usd"] for r in rows if r["allocated_usd"] > 0]
    assert allocated == sorted(allocated, reverse=True)


def test_plan_allocation_matches_arms(plan_payload: dict[str, Any]) -> None:
    alloc = plan_payload["allocation"]
    assert set(alloc) == {
        "budget_usd",
        "allocated_usd",
        "unallocated_usd",
        "unallocated_why",
        "picked",
        "arms",
        "saved_usd",
        "saved_share",
    }
    assert alloc["allocated_usd"] <= alloc["budget_usd"] + 1e-6
    assert abs(alloc["budget_usd"] - alloc["allocated_usd"] - alloc["unallocated_usd"]) < 0.5
    arms = {a["arm"]: a for a in alloc["arms"]}
    assert "koxpilot" in arms and "follower_rank" in arms
    assert abs(arms["koxpilot"]["spend"] - alloc["allocated_usd"]) < 0.01
    for arm in alloc["arms"]:
        assert looks_like_identifier(arm["label"]) is None
        assert arm["expected_value"] >= 0
    assert alloc["unallocated_why"]


def test_plan_preset_uses_frozen_spec() -> None:
    """预置 brief 必须直接用数据集里的固化 spec（前端用的也是它）。"""
    snap = STORE.snapshot()
    assert snap is not None
    brief_id = snap.briefs[0]["brief_id"]
    payload = svc.plan({"brief_id": brief_id, "options": {"top_n": 500}})
    assert payload["brief"]["source"] == "preset"
    assert payload["brief"]["parse_path"] == "preset"
    frozen = snap.briefs_by_id[brief_id]["spec"]
    budget = next(f for f in payload["brief"]["fields"] if f["key"] == "budget_usd")
    assert budget["value"] == int(round(float(frozen["budget_usd"])))
    assert payload["allocation"]["budget_usd"] == pytest.approx(float(frozen["budget_usd"]))


def test_plan_notes_say_why_a1_did_not_use_the_model(plan_payload: dict[str, Any]) -> None:
    """`parse_path` 说结果，`notes` 必须说原因 —— 这一条防的是"悄悄降级"。

    夹具里模型调用被显式关掉，所以这次必然走规则解析；此时 notes 里必须有一句
    把原因讲明白的话，而不是只留一个 `rule` 让人以为本来就没打算用模型。
    """
    brief = plan_payload["brief"]
    assert brief["parse_path"] == "rule"
    notes = brief["notes"]
    assert any("规则解析" in n for n in notes), notes
    # 原因里不许出现环境变量名之类的内部标识（面向使用者的话）
    assert all("KOXPILOT_" not in n for n in notes), notes


def test_plan_top_n_truncates_and_says_so() -> None:
    payload = svc.plan({"brief_text": "全球投放，预算 10 万美元", "options": {"top_n": 50}})
    recall = payload["funnel"][0]
    assert recall["count"] == 50
    # 截断了就必须在标签里说清楚，不能让人以为本次只召回了 50 人
    assert "top_n" in recall["label"]
    assert len(payload["parity_payload"]["verdicts"]) == 50


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "bad_request"),
        ({"brief_text": "   "}, "bad_request"),
        ({"brief_text": "彩妆 " * 700}, "bad_request"),
        ({"brief_id": "BRIEF-NOPE"}, "not_found"),
    ],
)
def test_plan_errors(body: dict[str, Any], code: str) -> None:
    with pytest.raises(svc.ServiceError) as err:
        svc.plan(body)
    assert err.value.code == code


# ---------------------------------------------------------------------------
# gate/batch
# ---------------------------------------------------------------------------
def test_gate_batch_matches_engine() -> None:
    from koxpilot.gates.engine import evaluate
    from koxpilot.types import CampaignSpec

    snap = STORE.snapshot()
    assert snap is not None
    ids = [str(k["kox_id"]) for k in snap.records[:200]]
    payload = svc.gate_batch({"kox_ids": list(reversed(ids))})
    assert payload["ok"] is True
    got = [r["kox_id"] for r in payload["verdicts"]]
    assert got == sorted(ids)
    for row in payload["verdicts"]:
        expected = evaluate(snap.by_id[row["kox_id"]], CampaignSpec(), snap.thresholds).verdict
        assert row["verdict"] == expected


def test_gate_batch_limits() -> None:
    with pytest.raises(svc.ServiceError) as err:
        svc.gate_batch({"kox_ids": ["KOX-000001"] * (svc.GATE_BATCH_LIMIT + 1)})
    assert err.value.code == "too_many"

    with pytest.raises(svc.ServiceError) as err:
        svc.gate_batch({"kox_ids": "KOX-000001"})
    assert err.value.code == "bad_request"

    with pytest.raises(svc.ServiceError) as err:
        svc.gate_batch({"kox_ids": ["NOPE-1"]})
    assert err.value.code == "not_found"


# ---------------------------------------------------------------------------
# meta / health
# ---------------------------------------------------------------------------
def test_meta_is_honest() -> None:
    from koxpilot_service.store import health_payload

    payload, elapsed = health_payload()
    assert payload["ok"] is True
    assert payload["status"] in ("ready", "warming", "failed")
    assert elapsed < 800.0, "health 必须远快于契约规定的 800ms"
    meta = payload["meta"]
    assert set(meta) == {
        "engine",
        "engine_version",
        "dataset_sha256",
        "kox_count",
        "thresholds_source",
        "llm_runtime",
        "served_at",
        "elapsed_ms",
    }
    assert meta["engine"] == "python"
    assert len(meta["dataset_sha256"]) == 64
    assert meta["kox_count"] == 5000
    assert set(meta["llm_runtime"]) == {"available", "provider", "reason"}


def test_meta_dataset_sha_matches_frontend() -> None:
    """服务与静态站必须读同一份数据集——这是「一套系统」的硬证据。"""
    import json

    consistency = REPO_ROOT / "web" / "public" / "data" / "consistency.json"
    if not consistency.exists():
        pytest.skip("前端产物未生成")
    with consistency.open(encoding="utf-8") as fh:
        frontend = json.load(fh)
    snap = STORE.snapshot()
    assert snap is not None
    assert snap.dataset_sha256 == frontend["dataset"]["sha256_from_reference"]
