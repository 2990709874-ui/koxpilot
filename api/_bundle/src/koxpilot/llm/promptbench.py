# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/llm/promptbench.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
"""Prompt 迭代实验：同一份 ground truth 下，三版 Prompt × 双模型的真实横评。

运行
----
    python -m koxpilot.llm.promptbench --n 600

产物
----
``output/prompt_bench.json``，包含：

- 每个 (Prompt 版本 × 模型) 组合的 precision / recall / F1 / accuracy / 混淆矩阵
- 每个组合的真实 token 消耗与平均延迟
- 规则版（Jaccard 阈值）作为零成本对照组
- ``verdict``：实测结论。**如果实测与事前假设相反，以实测为准并如实记录。**

方法论纪律
----------
1. **控制变量**：三版 Prompt 用完全相同的 600 条样本、相同的 batch 切分、
   相同的 user message 构造方式。唯一变量是 system prompt。
2. **不看答案调 Prompt**：三版 Prompt 的措辞在跑评测之前就已写定
   （见 prompt_variants.py 的 hypothesis 字段），不是拿着指标反复微调出来的。
   否则就是在 600 条测试集上过拟合。
3. **覆盖率单独报**：模型漏项/格式崩坏导致的未覆盖样本单独统计，
   不按"判对"也不按"判错"处理，避免用格式失败洗指标。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

from .identity import resolve_display
from .prompt_variants import TAG_VARIANTS, build_messages
from .provider import LLMError, LLMProvider, Usage, available_providers, build_provider
from .runner import Cache, _batched, _parse_array, _sha1, pmap, rule_tag_mismatch

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = REPO_ROOT / "output"
BENCH_PATH = OUTPUT_DIR / "prompt_bench.json"
CACHE_PATH = OUTPUT_DIR / "llm_cache.json"

BATCH = 12


def _prf(tp: int, fp: int, fn: int, tn: int, covered: int) -> dict[str, Any]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {
        "precision": round(p, 4),
        "recall": round(r, 4),
        "f1": round(f1, 4),
        "accuracy": round((tp + tn) / covered, 4) if covered else 0.0,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }


def run_arm(
    variant: Any,
    provider: LLMProvider,
    model_key: str,
    records: list[dict[str, Any]],
    cache: Cache,
    workers: int = 8,
) -> dict[str, Any]:
    batches = list(_batched(records, BATCH))

    def work(_i: int, batch: list[dict[str, Any]]) -> dict[str, Any] | None:
        # 缓存 key 必须由**实际发给模型的完整消息**决定（system + user 全文）。
        # 曾经这里只 hash [kox_id]：一旦 prompt 文案或达人内容变化，仍会命中旧缓存，
        # bench 出来的是"上一版的答案"，而指标看起来完全正常——属于静默的评测作弊。
        msgs = build_messages(variant, batch)
        ck = f"pbench::{variant.version}::{model_key}::{_sha1(msgs)}"
        hit = cache.get(ck)
        if hit is not None:
            return hit
        try:
            resp = provider.chat(msgs)
            items = _parse_array(resp.json_payload(), model_key)
            rec = {
                "items": items,
                "usage": resp.usage.to_dict(),
                "latency_ms": resp.latency_ms,
                # 服务端回报的型号（没回报就是空串）。存进缓存是为了让"这批 token 谁跑的"
                # 在不重新花钱的前提下也能回答；旧缓存条目没有这个键，会如实退回请求 id。
                "served_model": resp.served_model,
            }
            cache.put(ck, rec)
            return rec
        except (LLMError, KeyError, ValueError) as exc:
            print(
                f"  [{variant.version}/{model_key}] batch 失败：{str(exc)[:120]}",
                file=sys.stderr,
                flush=True,
            )
            return None

    results = pmap(work, batches, workers, label=f"{variant.version}/{model_key}")
    cache.flush()

    preds: dict[str, dict[str, Any]] = {}
    usage_total = Usage()
    calls = len(batches)
    failed = 0
    latency_total = 0
    served: set[str] = set()
    for rec in results:
        if rec is None:
            failed += 1
            continue
        if rec.get("served_model"):
            served.add(str(rec["served_model"]))
        u = rec.get("usage") or {}
        usage_total = usage_total + Usage(
            prompt_tokens=int(u.get("prompt_tokens") or 0),
            completion_tokens=int(u.get("completion_tokens") or 0),
            reasoning_tokens=int(u.get("reasoning_tokens") or 0),
            cached_tokens=int(u.get("cached_tokens") or 0),
        )
        latency_total += int(rec.get("latency_ms") or 0)
        for item in rec.get("items") or []:
            if isinstance(item, dict) and item.get("kox_id"):
                preds[item["kox_id"]] = item

    tp = fp = fn = tn = 0
    covered = 0
    sev_major_on_gt = 0
    conf_sum = 0.0
    for r in records:
        item = preds.get(r["kox_id"])
        if not item:
            continue
        covered += 1
        gt = bool((r.get("gt") or {}).get("tag_mismatch"))
        pred = bool(item.get("mismatch"))
        try:
            conf_sum += float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            pass
        if gt and item.get("severity") == "major":
            sev_major_on_gt += 1
        tp += gt and pred
        fp += (not gt) and pred
        fn += gt and (not pred)
        tn += (not gt) and (not pred)

    metrics = _prf(tp, fp, fn, tn, covered)
    # 模型标识与 llm_bench 同一套口径：`model` 是展示名（服务端回报优先），
    # 请求时用的 id（ARK 是 endpoint id）单列。同名不同义是产物里最容易被问穿的地方。
    display, display_source, served_models = resolve_display(provider.model, served)
    return {
        "prompt_version": variant.version,
        "prompt_name": variant.name,
        "model_key": model_key,
        "model": display,
        "requested_model_id": provider.model,
        "served_models": served_models,
        "model_display_source": display_source,
        "n_samples": len(records),
        "n_covered": covered,
        "coverage": round(covered / max(len(records), 1), 4),
        "calls": calls,
        "failed_calls": failed,
        "avg_latency_ms": round(latency_total / max(calls - failed, 1), 1),
        "avg_confidence": round(conf_sum / max(covered, 1), 4),
        "major_severity_on_true_mismatch": sev_major_on_gt,
        **metrics,
        **usage_total.to_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Prompt 迭代横评")
    ap.add_argument("--n", type=int, default=600, help="抽样条数")
    ap.add_argument("--models", default="", help="provider 列表，逗号分隔；留空自动探测")
    ap.add_argument("--seed", type=int, default=20270919)
    ap.add_argument("--workers", type=int, default=8, help="并发请求数")
    args = ap.parse_args(argv)

    kox_path = DATA_DIR / "kox_5000.json"
    if not kox_path.exists():
        print(f"缺少 {kox_path}，请先跑 `make data`", file=sys.stderr)
        return 3
    raw = json.loads(kox_path.read_text("utf-8"))
    records: list[dict[str, Any]] = raw["kox"] if isinstance(raw, dict) else raw

    # 与 runner.py 的 tag 任务使用相同的种子与抽样方式，保证两处结果可直接对照
    rng = random.Random(args.seed)
    sample = sorted(rng.sample(records, min(args.n, len(records))), key=lambda r: r["kox_id"])
    n_pos = sum(1 for r in sample if (r.get("gt") or {}).get("tag_mismatch"))
    print(f"[bench] 样本 {len(sample)} 条，其中真实错配 {n_pos} 条（正例率 {n_pos/len(sample):.1%}）")

    model_keys = [m.strip() for m in args.models.split(",") if m.strip()] or available_providers()
    providers: dict[str, LLMProvider] = {}
    for mk in model_keys:
        try:
            providers[mk] = build_provider(mk)
        except LLMError as exc:
            print(f"[init] 跳过 {mk}：{exc}", file=sys.stderr)
    if not providers:
        print("没有可用 provider", file=sys.stderr)
        return 2
    print(f"[init] 参评模型（请求时用的 id）：{ {k: v.model for k, v in providers.items()} }")

    cache = Cache(CACHE_PATH)
    started = time.time()
    arms: list[dict[str, Any]] = []

    # 零成本对照组：规则
    tp = fp = fn = tn = 0
    for r in sample:
        gt = bool((r.get("gt") or {}).get("tag_mismatch"))
        pred = rule_tag_mismatch(r)
        tp += gt and pred
        fp += (not gt) and pred
        fn += gt and (not pred)
        tn += (not gt) and (not pred)
    arms.append(
        {
            "prompt_version": "rule",
            "prompt_name": "规则对照组：declared/observed Jaccard < 0.34",
            "model_key": "none",
            "model": "n/a",
            "requested_model_id": None,
            "served_models": [],
            "model_display_source": "not_a_model_arm",
            "n_samples": len(sample),
            "n_covered": len(sample),
            "coverage": 1.0,
            "calls": 0,
            "failed_calls": 0,
            "avg_latency_ms": 0.0,
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            **_prf(tp, fp, fn, tn, len(sample)),
        }
    )
    print(
        f"[rule] P={arms[0]['precision']:.3f} R={arms[0]['recall']:.3f} "
        f"F1={arms[0]['f1']:.3f} acc={arms[0]['accuracy']:.3f}  (0 token)"
    )

    for variant in TAG_VARIANTS:
        for mk, prov in providers.items():
            print(f"\n[run] Prompt {variant.version}（{variant.name}） × {mk}")
            arm = run_arm(variant, prov, mk, sample, cache, args.workers)
            arms.append(arm)
            print(
                f"  → P={arm['precision']:.3f} R={arm['recall']:.3f} F1={arm['f1']:.3f} "
                f"acc={arm['accuracy']:.3f} cov={arm['coverage']:.2f} "
                f"tokens={arm['total_tokens']} 失败batch={arm['failed_calls']}"
            )

    llm_arms = [a for a in arms if a["prompt_version"] != "rule"]
    best = max(llm_arms, key=lambda a: a["f1"]) if llm_arms else None
    by_version: dict[str, list[dict[str, Any]]] = {}
    for a in llm_arms:
        by_version.setdefault(a["prompt_version"], []).append(a)
    version_avg = {
        v: {
            "avg_f1": round(sum(x["f1"] for x in xs) / len(xs), 4),
            "avg_precision": round(sum(x["precision"] for x in xs) / len(xs), 4),
            "avg_recall": round(sum(x["recall"] for x in xs) / len(xs), 4),
            "total_tokens": sum(x["total_tokens"] for x in xs),
        }
        for v, xs in by_version.items()
    }

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_s": round(time.time() - started, 1),
        "task": "标签错配判定（G2.1）",
        "n_samples": len(sample),
        "n_positives": n_pos,
        "positive_rate": round(n_pos / len(sample), 4),
        "control_variables": (
            "三版 Prompt 使用完全相同的样本、batch 切分与 user message 构造方式；"
            "唯一变量是 system prompt。三版措辞在跑评测前已写定，未按指标反复微调。"
        ),
        "prompt_variants": [v.to_meta() for v in TAG_VARIANTS],
        "arms": arms,
        "version_average": version_avg,
        "best_arm": {k: best[k] for k in ("prompt_version", "model_key", "f1", "precision", "recall")}
        if best
        else None,
        "rule_baseline_f1": arms[0]["f1"],
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    BENCH_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")

    print("\n===== Prompt 迭代结论（实测）=====")
    print(f"规则对照组 F1 = {arms[0]['f1']:.3f}（0 token）")
    for v in sorted(version_avg):
        d = version_avg[v]
        print(f"Prompt {v}: 平均 F1 = {d['avg_f1']:.3f}（P={d['avg_precision']:.3f} R={d['avg_recall']:.3f}）")
    if best:
        print(f"最优组合：Prompt {best['prompt_version']} × {best['model_key']}，F1 = {best['f1']:.3f}")
    print(f"写出 {BENCH_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
