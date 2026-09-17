"""模型标识口径：产物里"这批 token 是哪个模型跑的"必须只有一个答案。

这个文件守的是一句**曾经站不住的话**：早期 `llm_bench.json` 的 `models.ark` 存的是
endpoint id（`ep-...`），而 `per_task["fit::ark"].model` 存的是服务端回报的型号
（`doubao-...`）。两处同名不同义，任何照着 `models` 写材料的人都会说出
"我们用的模型是 ep-2026…"——那不是模型名，是资源 id，现场会被问穿。

因此这里的断言分三层：
1. **判据只看"谁说的"**：``resolve_display`` 不许对字符串做模式匹配（不许"以 ep- 开头
   就当 endpoint"），也不许在服务端没回报时假装服务端确认过；
2. **写侧不合并语义**：失败调用不得把请求 id 冒充成服务端回报；服务端回报多个型号时
   不得静默挑一个；
3. **读侧只有一套口径**：已提交的 `metrics.json` 里出现 endpoint id 的地方，
   只能是 `requested_model_id`（"我们请求的是什么"），不能出现在任何"模型是谁"的字段上。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from koxpilot.llm.identity import (
    IDENTITY_SCHEMA,
    IDENTITY_SCHEMA_LEGACY,
    identity_from_bench,
    identity_from_ledger,
    model_display_map,
    resolve_display,
)
from koxpilot.llm.provider import LLMResponse, Usage
from koxpilot.llm.runner import CallLog, Ledger

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output"


class _Prov:
    """只带 model 字段的最小 provider 替身（记账只用到这一个属性）。"""

    def __init__(self, model: str) -> None:
        self.model = model


def _resp(model: str, served: str) -> LLMResponse:
    return LLMResponse(
        text="[]", usage=Usage(10, 2), model=model, provider="fake", latency_ms=5, served_model=served
    )


# ===========================================================================
# 1. resolve_display：只按"谁说的"判定
# ===========================================================================
class TestResolveDisplay:
    def test_served_name_wins_over_requested_endpoint_id(self) -> None:
        display, source, served = resolve_display("ep-20260308011145-z5d47", ["doubao-seed-2-0-lite"])
        assert display == "doubao-seed-2-0-lite"
        assert source == "served_by_api"
        assert served == ["doubao-seed-2-0-lite"]

    def test_same_name_is_marked_as_confirmed_not_as_guess(self) -> None:
        """请求名与服务端名相同（Azure deployment 就是型号名）：可以说"服务端确认过"。"""
        display, source, _ = resolve_display("gpt-5.5-2026-04-24", ["gpt-5.5-2026-04-24"])
        assert display == "gpt-5.5-2026-04-24"
        assert source == "requested_id_confirmed_by_api"

    def test_without_any_served_name_it_says_requested_id_only(self) -> None:
        """调用全失败时只有请求 id。这时**不能**声称服务端确认过——来源必须自证其弱。"""
        display, source, served = resolve_display("ep-x", [])
        assert (display, source, served) == ("ep-x", "requested_id_only", [])

    def test_does_not_pattern_match_on_the_ep_prefix(self) -> None:
        """"以 ep- 开头就是 endpoint"这类启发式一律不许有：换个供应商就会把对的说成错的。"""
        display, source, _ = resolve_display("ep-looks-like-endpoint", ["ep-looks-like-endpoint"])
        assert display == "ep-looks-like-endpoint"
        assert source == "requested_id_confirmed_by_api"

    def test_multiple_served_models_are_reported_not_silently_picked(self) -> None:
        """灰度/版本切换会回报两个型号。硬选一个就是编，必须如实报"多个"。"""
        display, source, served = resolve_display("ep-x", ["doubao-a", "doubao-b"])
        assert source == "multiple_served_models"
        assert served == ["doubao-a", "doubao-b"]
        assert "doubao-a" in display and "doubao-b" in display

    def test_blank_served_names_are_dropped(self) -> None:
        assert resolve_display("ep-x", ["", "  ", None])[1] == "requested_id_only"  # type: ignore[list-item]

    def test_nothing_known_is_unknown_not_a_made_up_name(self) -> None:
        assert resolve_display(None, [])[0] == "unknown"


# ===========================================================================
# 2. 写侧：记账不合并两个语义
# ===========================================================================
class TestCallLogKeepsTwoNamesApart:
    def test_success_records_both_requested_and_served(self) -> None:
        log = CallLog.from_response("fit", "ark", _Prov("ep-x"), _resp("doubao-1", "doubao-1"), 12)
        assert log.requested_model == "ep-x"
        assert log.served_model == "doubao-1"
        assert log.model == "doubao-1"  # 展示名
        d = log.to_dict()
        assert d["requested_model"] == "ep-x" and d["served_model"] == "doubao-1"

    def test_failure_never_passes_the_requested_id_off_as_served(self) -> None:
        log = CallLog.from_failure("fit", "ark", _Prov("ep-x"), 12, "boom")
        assert log.requested_model == "ep-x"
        assert log.served_model == ""  # 关键：不能拿请求 id 填这里
        assert log.ok is False and log.usage.total_tokens == 0

    def test_response_without_model_field_yields_empty_served_model(self) -> None:
        """服务端没回报 model 时，``LLMResponse.model`` 退回请求 id，但 served_model 必须为空。"""
        log = CallLog.from_response("tag", "ark", _Prov("ep-x"), _resp("ep-x", ""), 3)
        assert log.model == "ep-x" and log.served_model == ""
        _, source, _ = resolve_display(log.requested_model, [log.served_model])
        assert source == "requested_id_only"


class TestLedgerSummary:
    def test_summary_carries_requested_id_and_all_served_names(self) -> None:
        led = Ledger()
        led.add(CallLog.from_response("fit", "ark", _Prov("ep-x"), _resp("doubao-1", "doubao-1"), 12))
        led.add(CallLog.from_response("fit", "ark", _Prov("ep-x"), _resp("doubao-1", "doubao-1"), 12))
        slot = led.summarize()["fit::ark"]
        assert slot["requested_model_id"] == "ep-x"
        assert slot["served_models"] == ["doubao-1"]

    def test_second_served_name_is_not_overwritten_by_the_last_call(self) -> None:
        """被覆盖掉的那个名字恰恰是"这批 token 谁跑的"最关键的证据。"""
        led = Ledger()
        led.add(CallLog.from_response("tag", "ark", _Prov("ep-x"), _resp("doubao-a", "doubao-a"), 1))
        led.add(CallLog.from_response("tag", "ark", _Prov("ep-x"), _resp("doubao-b", "doubao-b"), 1))
        assert led.summarize()["tag::ark"]["served_models"] == ["doubao-a", "doubao-b"]

    def test_failed_only_slot_has_no_served_model(self) -> None:
        led = Ledger()
        led.add(CallLog.from_failure("tag", "ark", _Prov("ep-x"), 5, "boom"))
        slot = led.summarize()["tag::ark"]
        assert slot["served_models"] == [] and slot["requested_model_id"] == "ep-x"


# ===========================================================================
# 3. 归一：新旧产物形状都只给一个答案
# ===========================================================================
class TestIdentityFromLedger:
    def test_endpoint_id_and_model_name_are_both_kept(self) -> None:
        led = Ledger()
        led.add(CallLog.from_response("fit", "ark", _Prov("ep-x"), _resp("doubao-1", "doubao-1"), 12))
        led.add(
            CallLog.from_response("tag", "azure", _Prov("gpt-5.5"), _resp("gpt-5.5", "gpt-5.5"), 12)
        )
        ident = identity_from_ledger({"ark": "ep-x", "azure": "gpt-5.5"}, led.summarize())
        assert ident["schema"] == IDENTITY_SCHEMA
        ark = ident["by_model_key"]["ark"]
        assert ark["requested_model_id"] == "ep-x"
        assert ark["display"] == "doubao-1"
        assert ark["requested_id_equals_served"] is False
        assert model_display_map(ident) == {"ark": "doubao-1", "azure": "gpt-5.5"}

    def test_provider_with_zero_calls_still_appears(self) -> None:
        """配了但一次没调用的 provider 不能从标识表里消失（否则读者以为没配）。"""
        ident = identity_from_ledger({"ark": "ep-x"}, {})
        entry = ident["by_model_key"]["ark"]
        assert entry["display"] == "ep-x"
        assert entry["display_source"] == "requested_id_only"
        assert entry["requested_id_equals_served"] is None


class TestIdentityFromBench:
    def test_v2_artifact_is_passed_through(self) -> None:
        bench = {
            "models": {"ark": "doubao-1"},
            "model_identity": {
                "schema": IDENTITY_SCHEMA,
                "by_model_key": {
                    "ark": {
                        "model_key": "ark",
                        "requested_model_id": "ep-x",
                        "served_models": ["doubao-1"],
                        "display": "doubao-1",
                        "display_source": "served_by_api",
                    }
                },
            },
        }
        ident = identity_from_bench(bench)
        assert ident["schema"] == IDENTITY_SCHEMA
        assert model_display_map(ident) == {"ark": "doubao-1"}

    def test_legacy_artifact_is_reconstructed_and_labelled_as_legacy(self) -> None:
        """旧形状必须照旧能读，但**不能**被当成 v2 解释——schema 与来源都要自报家门。"""
        bench = {
            "models": {"ark": "ep-x"},
            "per_task": {"fit::ark": {"model_key": "ark", "model": "doubao-1"}},
        }
        ident = identity_from_bench(bench)
        assert ident["schema"] == IDENTITY_SCHEMA_LEGACY
        assert "统一模型标识口径" in str(ident["note"])
        entry = ident["by_model_key"]["ark"]
        assert entry["display"] == "doubao-1"
        assert entry["requested_model_id"] == "ep-x"
        assert entry["display_source"].endswith("::inferred_from_legacy_per_task")

    def test_absent_bench_is_absent_not_empty_success(self) -> None:
        ident = identity_from_bench(None)
        assert ident["schema"] == "absent" and ident["by_model_key"] == {}
        assert model_display_map(ident) == {}

    def test_garbage_entries_do_not_crash_the_reader(self) -> None:
        ident = identity_from_bench({"models": "not-a-dict", "per_task": ["nope"]})
        assert ident["by_model_key"] == {}


# ===========================================================================
# 4. 已提交产物：endpoint id 只能出现在"我们请求的是什么"这一个位置
# ===========================================================================
def _load(name: str) -> dict[str, Any]:
    path = OUTPUT_DIR / name
    if not path.exists():
        pytest.skip(f"缺少 {path}")
    return json.loads(path.read_text("utf-8"))


def _paths_of_value(node: Any, needle: str, prefix: str = "") -> list[str]:
    """把值等于 ``needle`` 的所有 JSON 路径列出来（列表下标写成 ``[i]``）。"""
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            found += _paths_of_value(v, needle, f"{prefix}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _paths_of_value(v, needle, f"{prefix}[{i}]")
    elif isinstance(node, str) and node == needle:
        found.append(prefix)
    return found


@pytest.fixture(scope="module")
def bench() -> dict[str, Any]:
    return _load("llm_bench.json")


@pytest.fixture(scope="module")
def metrics() -> dict[str, Any]:
    return _load("metrics.json")


class TestShippedArtifacts:
    def test_bench_and_per_task_names_are_reconciled_by_the_reader(
        self, bench: dict[str, Any]
    ) -> None:
        """已提交的 bench 仍是旧形状（重跑要真花 token），但读侧必须给出唯一答案。"""
        ident = identity_from_bench(bench)
        display = model_display_map(ident)
        for slot in bench["per_task"].values():
            key = slot["model_key"]
            # per_task 里记的模型名与归一后的展示名必须一致；若不一致，
            # 说明同一份产物里仍有两个"模型是谁"的说法。
            assert slot["model"] == display[key], f"{key} 的展示名与 per_task 不一致"

    def test_metrics_never_presents_an_endpoint_id_as_the_model(
        self, bench: dict[str, Any], metrics: dict[str, Any]
    ) -> None:
        """核心回归：metrics.json 里凡出现请求 id 的地方，只能是 requested_model_id。

        这条断言不依赖任何硬编码型号：请求 id 从 bench 的 model_identity 里现取。
        """
        ident = identity_from_bench(bench)
        for key, entry in ident["by_model_key"].items():
            requested = entry["requested_model_id"]
            if entry["requested_id_equals_served"] is not False:
                continue  # 请求 id 本身就是型号名，无从混淆
            paths = _paths_of_value(metrics, requested)
            assert paths, f"{key} 的请求 id 应至少作为 requested_model_id 出现一次"
            bad = [p for p in paths if not p.endswith("requested_model_id")]
            assert not bad, f"{key} 的请求 id（{requested}）被当成模型名报在：{bad}"

    def test_model_display_is_consistent_across_the_two_places_that_name_models(
        self, metrics: dict[str, Any]
    ) -> None:
        """表 6 与成本账各有一处 `models`，两处必须相等——不然又回到"读者猜"。"""
        t6 = metrics["table_6_llm_vs_rule"]
        if t6.get("status") != "ok":
            pytest.skip("本环境未跑过 LLM，表 6 无模型信息")
        account = metrics["cost_audit"]["token_account"]
        assert t6["models"] == account["models"]
        assert t6["model_identity"] == account["model_identity"]

    def test_fit_audit_reports_display_name_and_requested_id_separately(
        self, metrics: dict[str, Any]
    ) -> None:
        audit = metrics["table_6_llm_vs_rule"]["semantic_fit_llm_vs_rule"]
        if audit.get("status") != "ok":
            pytest.skip("本环境没有 LLM 适配分")
        llm = audit["llm"]
        assert llm["model"] == metrics["table_6_llm_vs_rule"]["models"][llm["primary_model_key"]]
        assert "requested_model_id" in llm and "model_source" in llm

    def test_honesty_list_discloses_that_the_shipped_bench_is_legacy_shaped(
        self, bench: dict[str, Any], metrics: dict[str, Any]
    ) -> None:
        """产物是旧口径这件事必须写在诚实性清单里，而不是靠读者自己发现。"""
        if identity_from_bench(bench)["schema"] != IDENTITY_SCHEMA_LEGACY:
            pytest.skip("bench 已是统一口径产物，无需披露")
        notes = " ".join(str(x) for x in metrics["honesty_notes"])
        assert "模型标识口径" in notes
        assert "model_identity" in notes
