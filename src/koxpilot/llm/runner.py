"""构建期批量推理 runner：真调大模型，落盘固化，记录真实 token 用量。

为什么需要这个模块
------------------
线上 Demo 必须零外部依赖（评审在公网，调不到需要鉴权的 endpoint），
所以语义类判定在**构建期**跑完并固化到 ``output/llm_cache.json``。

三个任务：

1. ``brief`` —— 3 个自然语言 brief → 结构化 CampaignSpec（双模型各跑一遍）
2. ``tag``   —— 标签错配判定（双模型各跑一遍）。这个任务有 ground truth，
               因此可以和规则版做**同一裁判下的横评**，用来回答
               "这里到底值不值得花 token"。这是成本决策的实证依据，不是拍脑袋。
3. ``fit``   —— 候选达人的语义适配打分（用主模型跑，喂给 A4）

工程细节
--------
- **磁盘缓存 + 幂等**：cache key = (task, model, 输入内容的 sha1)。
  中断后重跑不会重复花 token，也不会因为重跑产生不同结果。
- **真实 usage**：每次调用的 prompt/completion/reasoning token 全部记账，
  写进 ``output/llm_bench.json``。成本审计里的所有数字来自这里。
- **失败隔离**：单个 batch 失败不影响整体，记录到 ``failures`` 里，
  评测时按"未覆盖"处理，绝不用默认值填充冒充成功。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .prompts import brief_parse_messages, fit_score_messages, tag_judge_messages
from .identity import identity_from_ledger, model_display_map
from .provider import (
    LLMError,
    LLMProvider,
    LLMResponse,
    Usage,
    available_providers,
    build_provider,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = REPO_ROOT / "output"
CACHE_PATH = OUTPUT_DIR / "llm_cache.json"
BENCH_PATH = OUTPUT_DIR / "llm_bench.json"

TAG_BATCH = 12
FIT_BATCH = 12
TAG_SAMPLE_N = 600  # 标签横评抽样量：够算稳定的 P/R，又不至于把预算烧光
FIT_SAMPLE_N = 260  # 每个 brief 的候选适配打分量


# ---------------------------------------------------------------------------
# 记账
# ---------------------------------------------------------------------------
@dataclass
class CallLog:
    task: str
    model_key: str
    #: 展示用模型名（服务端回报优先，失败时退回请求 id）——保留原字段名与位置，
    #: 免得所有调用点都要改。
    model: str
    n_items: int
    usage: Usage
    latency_ms: int
    ok: bool
    error: str = ""
    #: 我们发请求时填进 body 的 ``model``（ARK 是 endpoint id）。
    requested_model: str = ""
    #: 服务端明确回报的模型名；响应没带就留空。空 ≠ "和请求 id 相同"，
    #: 所以这里绝不用请求 id 兜底填充。
    served_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "model_key": self.model_key,
            "model": self.model,
            "requested_model": self.requested_model,
            "served_model": self.served_model,
            "n_items": self.n_items,
            "latency_ms": self.latency_ms,
            "ok": self.ok,
            "error": self.error,
            **self.usage.to_dict(),
        }

    @classmethod
    def from_response(
        cls, task: str, model_key: str, prov: LLMProvider, resp: LLMResponse, n_items: int
    ) -> CallLog:
        """成功调用的记账：请求 id 与服务端名各归各位。"""
        return cls(
            task,
            model_key,
            resp.model,
            n_items,
            resp.usage,
            resp.latency_ms,
            True,
            requested_model=prov.model,
            served_model=resp.served_model,
        )

    @classmethod
    def from_failure(
        cls, task: str, model_key: str, prov: LLMProvider, n_items: int, error: str
    ) -> CallLog:
        """失败调用的记账：只知道请求 id，服务端名留空——不能拿请求 id 冒充服务端回报。"""
        return cls(
            task,
            model_key,
            prov.model,
            n_items,
            Usage(),
            0,
            False,
            error[:300],
            requested_model=prov.model,
            served_model="",
        )


@dataclass
class Ledger:
    calls: list[CallLog] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, log: CallLog) -> None:
        with self._lock:
            self.calls.append(log)

    def summarize(self) -> dict[str, Any]:
        by: dict[str, dict[str, Any]] = {}
        served: dict[str, set[str]] = {}
        requested: dict[str, str] = {}
        for c in self.calls:
            key = f"{c.task}::{c.model_key}"
            slot = by.setdefault(
                key,
                {
                    "task": c.task,
                    "model_key": c.model_key,
                    "model": c.model,
                    "calls": 0,
                    "failed_calls": 0,
                    "items": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "reasoning_tokens": 0,
                    "cached_tokens": 0,
                    "total_tokens": 0,
                    "latency_ms_total": 0,
                },
            )
            # 同一 (task, model_key) 下服务端可能回报多个名字（灰度/版本切换），
            # 因此收集成集合而不是"最后一个覆盖前一个"——被覆盖掉的那个名字
            # 恰恰是"这批 token 到底谁跑的"最关键的证据。
            if c.served_model:
                served.setdefault(key, set()).add(c.served_model)
            if c.requested_model and key not in requested:
                requested[key] = c.requested_model
            slot["calls"] += 1
            if not c.ok:
                slot["failed_calls"] += 1
            slot["items"] += c.n_items
            slot["prompt_tokens"] += c.usage.prompt_tokens
            slot["completion_tokens"] += c.usage.completion_tokens
            slot["reasoning_tokens"] += c.usage.reasoning_tokens
            slot["cached_tokens"] += c.usage.cached_tokens
            slot["total_tokens"] += c.usage.total_tokens
            slot["latency_ms_total"] += c.latency_ms
        for key, slot in by.items():
            n = max(slot["calls"], 1)
            slot["avg_latency_ms"] = round(slot["latency_ms_total"] / n, 1)
            slot["tokens_per_item"] = (
                round(slot["total_tokens"] / slot["items"], 2) if slot["items"] else None
            )
            slot["requested_model_id"] = requested.get(key)
            slot["served_models"] = sorted(served.get(key, set()))
        return by


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def _sha1(obj: Any) -> str:
    return hashlib.sha1(
        json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


class Cache:
    """磁盘缓存。每次写入立刻 flush——沙箱可能随时重启，不能把结果攒在内存里。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._dirty = 0
        self.data: dict[str, Any] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text("utf-8"))
            except json.JSONDecodeError:
                print(f"[cache] {path} 损坏，忽略并重建", file=sys.stderr)
                self.data = {}
        self.data.setdefault("_meta", {})
        self.data.setdefault("entries", {})

    def get(self, key: str) -> Any | None:
        with self._lock:
            return self.data["entries"].get(key)

    def put(self, key: str, value: Any, *, flush_every: int = 8) -> None:
        """写入并周期性 flush。

        并发写场景下每次都整文件落盘会成为瓶颈（缓存文件会长到几十 MB），
        因此按 ``flush_every`` 批量落盘；``flush()`` 本身是原子替换，
        中途被杀最多丢最后几条，重跑时命中缓存补齐即可。
        """
        with self._lock:
            self.data["entries"][key] = value
            self._dirty += 1
            if self._dirty >= flush_every:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(self.path)
        self._dirty = 0



def pmap(fn: Any, items: list[Any], workers: int, label: str = "") -> list[Any]:
    """有序并发 map。

    模型调用是纯 IO 等待，线程池足够；用 as_completed 收集后按原下标回填，
    保证结果顺序与输入一致——顺序敏感的地方（批次内 kox_id 对齐）不能乱。
    """
    if workers <= 1 or len(items) <= 1:
        return [fn(i, it) for i, it in enumerate(items)]
    out: list[Any] = [None] * len(items)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fn, i, it): i for i, it in enumerate(items)}
        for fut in as_completed(futs):
            idx = futs[fut]
            out[idx] = fut.result()
            done += 1
            if label and (done % 10 == 0 or done == len(items)):
                print(f"  [{label}] {done}/{len(items)} 批完成", flush=True)
    return out


def _batched(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ---------------------------------------------------------------------------
# 任务实现
# ---------------------------------------------------------------------------
def _parse_array(resp_payload: Any, who: str) -> list[Any]:
    """把模型输出规整成数组。

    部分模型会把数组包一层（``{"results": [...]}``），这里统一剥开。
    仍然不是数组就抛错——静默返回空数组会让覆盖率虚高、指标失真。
    """
    parsed = resp_payload
    if isinstance(parsed, dict):
        for v in parsed.values():
            if isinstance(v, list):
                parsed = v
                break
    if not isinstance(parsed, list):
        raise LLMError(who, f"期望 JSON 数组，得到 {type(parsed).__name__}")
    return parsed


def run_brief(
    providers: dict[str, LLMProvider],
    briefs: list[dict[str, Any]],
    cache: Cache,
    ledger: Ledger,
    workers: int = 4,
) -> dict[str, dict[str, Any]]:
    jobs = [(b, mk, prov) for b in briefs for mk, prov in providers.items()]

    def work(_i: int, job: tuple[dict[str, Any], str, LLMProvider]) -> tuple[str, str, Any]:
        brief, mk, prov = job
        bid = brief["brief_id"]
        # 同其余任务：hash 完整消息，让 system 文案改版也能自动失效缓存。
        msgs = brief_parse_messages(brief["raw_text"])
        ck = f"brief::{mk}::{_sha1(msgs)}"
        hit = cache.get(ck)
        if hit is not None:
            return bid, mk, hit
        try:
            resp = prov.chat(msgs)
            spec = resp.json_payload()
            ledger.add(CallLog.from_response("brief", mk, prov, resp, 1))
            cache.put(ck, spec, flush_every=1)
            print(
                f"[brief] {bid} / {mk} ok tokens={resp.usage.total_tokens} "
                f"({resp.latency_ms}ms) 品类={spec.get('target_categories')} kpi={spec.get('kpi')}",
                flush=True,
            )
            return bid, mk, spec
        except (LLMError, KeyError, ValueError) as exc:
            ledger.add(CallLog.from_failure("brief", mk, prov, 1, str(exc)))
            print(f"[brief] {bid} / {mk} FAILED: {exc}", file=sys.stderr, flush=True)
            return bid, mk, None

    out: dict[str, dict[str, Any]] = {}
    for bid, mk, spec in pmap(work, jobs, workers):
        if spec is not None:
            out.setdefault(bid, {})[mk] = spec
    cache.flush()
    return out


def run_tag(
    providers: dict[str, LLMProvider],
    records: list[dict[str, Any]],
    cache: Cache,
    ledger: Ledger,
    workers: int = 8,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for mk, prov in providers.items():
        batches = list(_batched(records, TAG_BATCH))

        def work(_i: int, batch: list[dict[str, Any]], mk: str = mk, prov: LLMProvider = prov) -> list[Any]:
            # 同 run_fit：key = 任务 + 模型 + 实际消息全文 hash。
            # 直接 hash messages 而不是自己再列一遍字段，好处是 prompt 以后加/改字段
            # （或改 system 文案）时 key 自动跟着变，不会出现"key 忘了同步"的漏洞。
            msgs = tag_judge_messages(batch)
            ck = f"tag::{mk}::{_sha1(msgs)}"
            hit = cache.get(ck)
            if hit is not None:
                return hit
            try:
                resp = prov.chat(msgs)
                items = _parse_array(resp.json_payload(), mk)
                ledger.add(CallLog.from_response("tag", mk, prov, resp, len(batch)))
                cache.put(ck, items)
                return items
            except (LLMError, KeyError, ValueError) as exc:
                ledger.add(CallLog.from_failure("tag", mk, prov, len(batch), str(exc)))
                print(f"[tag] {mk} batch 失败: {str(exc)[:140]}", file=sys.stderr, flush=True)
                return []

        results = pmap(work, batches, workers, label=f"tag/{mk}")
        for items in results:
            for item in items or []:
                if isinstance(item, dict) and item.get("kox_id"):
                    out.setdefault(item["kox_id"], {})[mk] = item
        cache.flush()
        print(f"[tag] {mk} 完成，覆盖 {sum(1 for v in out.values() if mk in v)}/{len(records)}", flush=True)
    return out


def run_fit(
    provider: LLMProvider,
    model_key: str,
    briefs_specs: dict[str, dict[str, Any]],
    pools: dict[str, list[dict[str, Any]]],
    cache: Cache,
    ledger: Ledger,
    workers: int = 8,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for bid, spec in briefs_specs.items():
        pool = pools.get(bid, [])
        batches = list(_batched(pool, FIT_BATCH))

        def work(_i: int, batch: list[dict[str, Any]], bid: str = bid, spec: dict[str, Any] = spec) -> list[Any]:
            # key 必须覆盖 prompt 的全部输入：spec + 每个候选进入 prompt 的内容字段
            # （platform/country/language/品类/受众画像）+ system 文案版本。
            # 只 hash spec + kox_id 会导致数据重生成或 prompt 改版后错误命中旧缓存。
            msgs = fit_score_messages(spec, batch)
            ck = f"fit::{model_key}::{bid}::{_sha1(msgs)}"
            hit = cache.get(ck)
            if hit is not None:
                return hit
            try:
                resp = provider.chat(msgs)
                items = _parse_array(resp.json_payload(), model_key)
                ledger.add(CallLog.from_response("fit", model_key, provider, resp, len(batch)))
                cache.put(ck, items)
                return items
            except (LLMError, KeyError, ValueError) as exc:
                ledger.add(CallLog.from_failure("fit", model_key, provider, len(batch), str(exc)))
                print(f"[fit] {bid} batch 失败: {str(exc)[:140]}", file=sys.stderr, flush=True)
                return []

        results = pmap(work, batches, workers, label=f"fit/{bid}")
        out[bid] = {}
        for items in results:
            for item in items or []:
                if isinstance(item, dict) and item.get("kox_id"):
                    out[bid][item["kox_id"]] = item
        cache.flush()
        print(f"[fit] {bid} 完成 {len(out[bid])}/{len(pool)}", flush=True)
    return out


# ---------------------------------------------------------------------------
# 规则版标签错配（用于横评的对照组）
# ---------------------------------------------------------------------------
def rule_tag_mismatch(rec: dict[str, Any], threshold: float = 0.34) -> bool:
    """规则版：declared 与 observed 品类的 Jaccard 相似度低于阈值即判错配。

    这是对照组，故意保持朴素——横评的意义在于回答
    "多花 token 换来的准确率提升值不值"，所以对照组必须是真实场景里
    最省事的那种做法。
    """
    d = set(rec.get("declared_categories") or [])
    o = set(rec.get("observed_categories") or [])
    if not d or not o:
        return False
    inter = len(d & o)
    union = len(d | o)
    return (inter / union if union else 0.0) < threshold


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4)}


def bench_tag(
    records: list[dict[str, Any]], llm_out: dict[str, dict[str, Any]], model_keys: list[str]
) -> dict[str, Any]:
    """标签错配任务：规则 vs 各模型，在同一 ground truth 下横评。"""
    by_id = {r["kox_id"]: r for r in records}
    result: dict[str, Any] = {"n_samples": len(records), "arms": {}}

    # 对照组：规则
    tp = fp = fn = tn = 0
    for r in records:
        gt = bool((r.get("gt") or {}).get("tag_mismatch"))
        pred = rule_tag_mismatch(r)
        tp += gt and pred
        fp += (not gt) and pred
        fn += gt and (not pred)
        tn += (not gt) and (not pred)
    result["arms"]["rule_jaccard"] = {
        "kind": "rule",
        "tokens": 0,
        "coverage": 1.0,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        **_prf(tp, fp, fn),
        "accuracy": round((tp + tn) / max(len(records), 1), 4),
    }

    # 实验组：各模型
    for mk in model_keys:
        tp = fp = fn = tn = 0
        covered = 0
        for kid, rec in by_id.items():
            item = (llm_out.get(kid) or {}).get(mk)
            if not item:
                continue
            covered += 1
            gt = bool((rec.get("gt") or {}).get("tag_mismatch"))
            pred = bool(item.get("mismatch"))
            tp += gt and pred
            fp += (not gt) and pred
            fn += gt and (not pred)
            tn += (not gt) and (not pred)
        result["arms"][mk] = {
            "kind": "llm",
            "coverage": round(covered / max(len(records), 1), 4),
            "n_judged": covered,
            "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
            **_prf(tp, fp, fn),
            "accuracy": round((tp + tn) / max(covered, 1), 4),
        }
    return result


# ---------------------------------------------------------------------------
# 候选池：不依赖 gates 模块，用最朴素的硬过滤，避免与门禁实现耦合
# ---------------------------------------------------------------------------
def build_pool(records: list[dict[str, Any]], spec: dict[str, Any], n: int, seed: int) -> list[dict[str, Any]]:
    markets = set(spec.get("target_markets") or [])
    plats = set(spec.get("platforms") or [])
    cats = set(spec.get("target_categories") or [])
    cats.discard(None)

    def hit(r: dict[str, Any]) -> int:
        s = 0
        if markets and r.get("country") in markets:
            s += 2
        if plats and r.get("platform") in plats:
            s += 2
        rc = set(r.get("declared_categories") or []) | set(r.get("observed_categories") or [])
        if cats & rc:
            s += 3
        return s

    scored = sorted(records, key=lambda r: (-hit(r), r["kox_id"]))
    top = [r for r in scored if hit(r) > 0][: n * 2]
    if len(top) < n:
        top = scored[: n * 2]
    rng = random.Random(seed)
    rng.shuffle(top)
    return sorted(top[:n], key=lambda r: r["kox_id"])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KOXPilot 构建期批量推理")
    ap.add_argument("--tasks", default="brief,tag,fit", help="要跑的任务，逗号分隔")
    ap.add_argument("--models", default="", help="provider 列表，逗号分隔；留空自动探测")
    ap.add_argument("--primary", default="", help="fit 任务用的主模型；留空取第一个")
    ap.add_argument("--tag-n", type=int, default=TAG_SAMPLE_N)
    ap.add_argument("--fit-n", type=int, default=FIT_SAMPLE_N)
    ap.add_argument("--seed", type=int, default=20270919)
    ap.add_argument("--workers", type=int, default=8, help="并发请求数")
    args = ap.parse_args(argv)

    model_keys = [m.strip() for m in args.models.split(",") if m.strip()] or available_providers()
    if not model_keys:
        print(
            "没有可用的 provider。请先按 .env.example 配置 ARK_* / AZURE_OPENAI_* / OPENAI_* 环境变量。",
            file=sys.stderr,
        )
        return 2
    providers: dict[str, LLMProvider] = {}
    for mk in model_keys:
        try:
            providers[mk] = build_provider(mk)
        except LLMError as exc:
            print(f"[init] 跳过 {mk}：{exc}", file=sys.stderr)
    if not providers:
        return 2
    print(f"[init] 可用模型（请求时用的 id）：{ {k: v.model for k, v in providers.items()} }")

    kox_path = DATA_DIR / "kox_5000.json"
    briefs_path = DATA_DIR / "briefs.json"
    if not kox_path.exists() or not briefs_path.exists():
        print(f"缺少数据文件：{kox_path} / {briefs_path}。请先跑 `make data`。", file=sys.stderr)
        return 3
    raw = json.loads(kox_path.read_text("utf-8"))
    # 数据集落盘结构为 {"meta": ..., "kox": [...]}（见 io_utils.dump_dataset）；
    # 兼容裸数组与旧字段名，避免构建期脚本因文件格式演进而静默拿到空列表。
    if isinstance(raw, dict):
        records: list[dict[str, Any]] = raw.get("kox") or raw.get("records") or []
    else:
        records = raw
    if not records:
        print(f"{kox_path} 里没有达人记录，请先跑 `make data`。", file=sys.stderr)
        return 3
    briefs_raw = json.loads(briefs_path.read_text("utf-8"))
    briefs: list[dict[str, Any]] = briefs_raw["briefs"] if isinstance(briefs_raw, dict) else briefs_raw
    print(f"[init] 达人 {len(records)} 条，brief {len(briefs)} 个")

    cache = Cache(CACHE_PATH)
    ledger = Ledger()
    tasks = {t.strip() for t in args.tasks.split(",") if t.strip()}
    started = time.time()

    bundle: dict[str, Any] = {}
    if CACHE_PATH.exists():
        try:
            prev = json.loads(CACHE_PATH.read_text("utf-8"))
            bundle = {k: v for k, v in prev.items() if k not in {"_meta", "entries"}}
        except json.JSONDecodeError:
            pass

    # --- brief ---
    brief_specs_by_model: dict[str, dict[str, Any]] = bundle.get("brief_specs", {})
    if "brief" in tasks:
        brief_specs_by_model = run_brief(providers, briefs, cache, ledger, args.workers)
        bundle["brief_specs"] = brief_specs_by_model

    primary = args.primary or next(iter(providers))
    # 主模型的解析结果作为下游正式使用的 spec；解析失败则退回数据集里的兜底 spec
    fallback = {b["brief_id"]: b.get("spec", {}) for b in briefs}
    final_specs: dict[str, dict[str, Any]] = {}
    for b in briefs:
        bid = b["brief_id"]
        cand = (brief_specs_by_model.get(bid) or {}).get(primary)
        final_specs[bid] = cand or fallback.get(bid, {})
    bundle["campaign_specs"] = final_specs
    bundle["campaign_spec_source"] = primary

    # --- tag ---
    if "tag" in tasks:
        rng = random.Random(args.seed)
        sample = sorted(rng.sample(records, min(args.tag_n, len(records))), key=lambda r: r["kox_id"])
        tag_out = run_tag(providers, sample, cache, ledger, args.workers)
        bundle["tag_judge"] = tag_out
        bundle["tag_bench"] = bench_tag(sample, tag_out, list(providers))
        b = bundle["tag_bench"]["arms"]
        print("\n[bench] 标签错配任务横评（同一 ground truth）：")
        for name, arm in b.items():
            print(
                f"  {name:14s} P={arm['precision']:.3f} R={arm['recall']:.3f} "
                f"F1={arm['f1']:.3f} acc={arm['accuracy']:.3f} cov={arm['coverage']}"
            )

    # --- fit ---
    if "fit" in tasks:
        pools = {
            bid: build_pool(records, spec, args.fit_n, args.seed) for bid, spec in final_specs.items()
        }
        bundle["fit_pool_ids"] = {bid: [r["kox_id"] for r in p] for bid, p in pools.items()}
        bundle["fit_scores"] = run_fit(providers[primary], primary, final_specs, pools, cache, ledger, args.workers)

    # --- 落盘 ---
    summary = ledger.summarize()
    # 模型标识：把"我们请求的 id"和"服务端回报的型号"分成两个字段写清楚。
    # 早期产物里 `models` 是请求 id（ARK 是 endpoint）、`per_task[*].model` 是服务端型号，
    # 两处同名不同义；现在 `models` 与 `per_task[*].model` 统一取展示名，
    # endpoint id 保留在 `model_identity` 里，谁想核对都能核对。
    identity = identity_from_ledger({k: v.model for k, v in providers.items()}, summary)
    display = model_display_map(identity)
    for slot in summary.values():
        slot["model"] = display.get(str(slot.get("model_key")), slot.get("model"))
    bench = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_s": round(time.time() - started, 1),
        "models": {k: display.get(k, v.model) for k, v in providers.items()},
        "model_identity": identity,
        "primary_model_key": primary,
        "per_task": summary,
        "totals": {
            "calls": sum(s["calls"] for s in summary.values()),
            "failed_calls": sum(s["failed_calls"] for s in summary.values()),
            "prompt_tokens": sum(s["prompt_tokens"] for s in summary.values()),
            "completion_tokens": sum(s["completion_tokens"] for s in summary.values()),
            "reasoning_tokens": sum(s["reasoning_tokens"] for s in summary.values()),
            "total_tokens": sum(s["total_tokens"] for s in summary.values()),
        },
        "tag_bench": bundle.get("tag_bench"),
        "note": (
            "token 数字取自各 API 返回的 usage 字段，非估算。"
            "本文件由构建期 runner 生成，线上 Demo 不再调用任何模型 endpoint。"
        ),
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    BENCH_PATH.write_text(json.dumps(bench, ensure_ascii=False, indent=2), "utf-8")

    cache.data.update(bundle)
    cache.data["_meta"] = {
        "generated_at": bench["generated_at"],
        "models": bench["models"],
        "model_identity": identity,
        "primary_model_key": primary,
        "tag_sample_n": args.tag_n,
        "fit_sample_n": args.fit_n,
        "seed": args.seed,
    }
    cache.flush()

    t = bench["totals"]
    print(
        f"\n[done] {t['calls']} 次调用（失败 {t['failed_calls']}），"
        f"总 token {t['total_tokens']}（其中 reasoning {t['reasoning_tokens']}），"
        f"耗时 {bench['elapsed_s']}s"
    )
    print(f"[done] 写出 {CACHE_PATH.relative_to(REPO_ROOT)} 与 {BENCH_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
