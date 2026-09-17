"""模型标识归一：把"我们请求的是什么"和"服务端说它是什么"分开写。

为什么需要这个模块
------------------
这里原本有一处真实的产物不一致：`llm_bench.json` 的 `models.ark` 是
**endpoint id**（`ep-20260308011145-z5d47`，因为 ARK 用 endpoint 当 `model` 字段发请求），
而 `per_task["fit::ark"].model` 是**服务端回报的模型名**（`doubao-seed-2-0-lite-260215`）。
两处都叫 "model"，含义却不同。这不影响任何 token 数字，但它会让读者以为跑了两个模型，
更糟的是——一旦有人照着 `models.ark` 写材料，"我们用的模型是 ep-2026…"这句话在评审现场
是站不住的（那是资源 id，不是模型）。

处理原则：**不猜、不改历史产物**。
1. 两个概念各自命名：``requested_model_id``（我们发出去的）与 ``served_models``（服务端回报的）；
2. 对外展示用 ``display``：能确定服务端型号就用它，否则退回请求 id，并用
   ``display_source`` 说明这个字符串是哪来的；
3. 旧形状的 `llm_bench.json`（`models` 是请求 id、`per_task[*].model` 是服务端名）**照旧能读**，
   归一结果里 ``schema="legacy_v1"``、``note`` 直说"该产物是统一口径之前生成的，
   重跑 `make llm` 后会变成 v2"——而不是悄悄把它当成 v2 解释。

字符串上不做任何模式匹配（比如"以 ep- 开头就算 endpoint"）：那种启发式今天对、
明天换个供应商就错，还会让错误看起来像正确。判据只有一个——**这个名字是谁说的**。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "IDENTITY_SCHEMA",
    "IDENTITY_SCHEMA_LEGACY",
    "identity_from_bench",
    "identity_from_ledger",
    "model_display_map",
    "resolve_display",
]

IDENTITY_SCHEMA = "model_identity_v2"
IDENTITY_SCHEMA_LEGACY = "legacy_v1"

_LEGACY_NOTE = (
    "该 llm_bench.json 生成于「统一模型标识口径」之前：`models[key]` 是我们发请求时用的 "
    "id（ARK 用 endpoint id），`per_task[*].model` 是服务端回报的模型名。两者都保留，"
    "对外展示取服务端名。重跑 `make llm` 后产物会带 `model_identity`，此处即为 v2。"
)


def resolve_display(
    requested: str | None,
    served: Iterable[str],
) -> tuple[str, str, list[str]]:
    """决定对外展示名。返回 ``(展示名, 来源, 去重排序后的服务端名列表)``。

    判据只有一个：**这个名字是谁说的**。不对字符串做任何模式匹配
    （比如"以 ep- 开头就算 endpoint"）——那种启发式今天对、明天换供应商就错，
    而且会让错误看起来像正确。
    """
    served_list = sorted({str(s) for s in served if str(s or "").strip()})
    # 服务端名可能与请求 id 相同（直接用模型名发请求），也可能一个都没有（全部调用失败）。
    # 前者不算"两个名字"，但也不能因此宣称"服务端确认过"，所以来源分开记。
    distinct_served = [s for s in served_list if s != requested]
    if len(distinct_served) == 1:
        return distinct_served[0], "served_by_api", served_list
    if not distinct_served and served_list and requested in served_list:
        return str(requested), "requested_id_confirmed_by_api", served_list
    if len(distinct_served) > 1:
        # 服务端回报了多个型号（灰度/版本切换）。这时不存在唯一正确的展示名，
        # 硬选一个就是编——如实报"多个"，让读者去看 served_models。
        return "multiple:" + ",".join(distinct_served), "multiple_served_models", served_list
    if requested:
        return str(requested), "requested_id_only", served_list
    return "unknown", "unknown", served_list


def _entry(
    model_key: str,
    requested: str | None,
    served: Iterable[str],
) -> dict[str, Any]:
    display, source, served_list = resolve_display(requested, served)
    return {
        "model_key": model_key,
        "requested_model_id": requested,
        "served_models": served_list,
        "display": display,
        "display_source": source,
        # 请求 id 与服务端名不是同一个字符串本身**不是错误**（endpoint id 天生如此），
        # 所以这里报的是事实而不是"是否合规"。多于一个服务端名才是真需要人看的信号。
        "requested_id_equals_served": (
            None if not served_list or requested is None else display == str(requested)
        ),
        "multiple_served_models": source == "multiple_served_models",
    }


def identity_from_ledger(
    requested: Mapping[str, str],
    per_task: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """构建期用：``requested`` 是 provider 配置里的 id，``per_task`` 是 ledger 汇总。"""
    served: dict[str, set[str]] = {}
    for slot in per_task.values():
        key = str(slot.get("model_key") or "")
        if not key:
            continue
        bucket = served.setdefault(key, set())
        for name in slot.get("served_models") or ([slot["model"]] if slot.get("model") else []):
            bucket.add(str(name))
    keys = sorted(set(requested) | set(served))
    return {
        "schema": IDENTITY_SCHEMA,
        "by_model_key": {
            key: _entry(key, requested.get(key), served.get(key, set())) for key in keys
        },
    }


def identity_from_bench(bench: Mapping[str, Any] | None) -> dict[str, Any]:
    """读侧统一入口：新旧两种 ``llm_bench.json`` 形状都归一到同一结构。

    新形状（v2）产物里已经带 ``model_identity``，直接透传；
    旧形状用 ``models`` + ``per_task[*].model`` 重建，并把 ``schema`` 标成 ``legacy_v1``。
    """
    if not isinstance(bench, Mapping):
        return {"schema": "absent", "by_model_key": {}}
    existing = bench.get("model_identity")
    if isinstance(existing, Mapping) and existing.get("by_model_key"):
        return {
            "schema": str(existing.get("schema") or IDENTITY_SCHEMA),
            "by_model_key": {
                str(k): dict(v)
                for k, v in (existing.get("by_model_key") or {}).items()
                if isinstance(v, Mapping)
            },
        }
    models = bench.get("models")
    requested = (
        {str(k): str(v) for k, v in models.items() if isinstance(v, str)}
        if isinstance(models, Mapping)
        else {}
    )
    per_task = bench.get("per_task") if isinstance(bench.get("per_task"), Mapping) else {}
    out = identity_from_ledger(requested, per_task)  # type: ignore[arg-type]
    out["schema"] = IDENTITY_SCHEMA_LEGACY
    out["note"] = _LEGACY_NOTE
    # 旧产物里只有 `per_task[*].model` 一个字符串，而它在"调用全失败"时存的其实是请求 id。
    # 因此这里的来源一律打上 legacy 标记：读者不该把它当成"服务端确认过"的强证据。
    for entry in out["by_model_key"].values():
        entry["display_source"] = f"{entry['display_source']}::inferred_from_legacy_per_task"
    return out


def model_display_map(identity: Mapping[str, Any]) -> dict[str, str]:
    """``model_key -> 对外展示名``。产物里凡是"报模型是谁"的地方都走这一份，避免两处口径。"""
    by_key = identity.get("by_model_key")
    if not isinstance(by_key, Mapping):
        return {}
    return {
        str(key): str(entry.get("display"))
        for key, entry in by_key.items()
        if isinstance(entry, Mapping)
    }
