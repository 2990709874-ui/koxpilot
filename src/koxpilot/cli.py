"""KOXPilot 命令行入口：``data / gate / budget / eval / explain / all``。

设计意图
--------
CLI 只做三件事：**读文件、调库、写文件**。所有业务逻辑都在库里，
这样 pytest 可以完全绕开 CLI 直接测函数，而 CLI 自身几乎不需要测试
（唯一需要保证的是"落盘的文件确实是库算出来的那份"）。

每个子命令都打印**可核对的关键数字**而不是"done"：
构建期日志本身就是交付物的一部分，面试时可以直接拿终端输出对照 metrics.json。
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .budget.planner import plan_baseline, plan_campaign, plan_diversified_no_gate
from .datagen.briefs import build_briefs
from .datagen.config import N_KOX, SAMPLE_SIZE, SEED
from .datagen.generator import generate_dataset
from .eval.harness import build_context, load_eval_inputs, run_full_eval
from .eval.multiseed import DEFAULT_N_SEEDS
from .gates.thresholds import calibrate
from .io_utils import (
    data_dir,
    dump_dataset,
    dump_json,
    file_sha256,
    load_json,
    output_dir,
)
from .types import CampaignSpec

__all__ = [
    "main",
    "cmd_data",
    "cmd_gate",
    "cmd_budget",
    "cmd_eval",
    "cmd_multiseed",
    "cmd_explain",
]

#: 落盘的门禁明细样本量（全量 5,000 条带证据链约 20MB，仓库里只留样本，前端用 TS 侧实时复算）
GATE_SAMPLE_N = 200


def _p(path: Any) -> str:
    """相对仓库根的短路径，便于日志阅读。"""
    from .io_utils import repo_root

    try:
        return str(path.relative_to(repo_root()))
    except (AttributeError, ValueError):
        return str(path)


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------
def cmd_data(args: argparse.Namespace) -> int:
    n = int(args.n or N_KOX)
    seed = int(args.seed or SEED)
    dataset = generate_dataset(n, seed)
    meta = dataset["meta"]
    full = dump_dataset(data_dir() / "kox_5000.json", dataset)
    # 样本条数取 min(SAMPLE_SIZE, n)：--n 小于 SAMPLE_SIZE 时若照抄 SAMPLE_SIZE，
    # 样本文件的 meta.n 会大于实际条数（自报数字对不上实际内容）。
    sample_n = min(SAMPLE_SIZE, len(dataset["kox"]))
    sample = dump_dataset(
        data_dir() / f"kox_sample_{SAMPLE_SIZE}.json",
        {"meta": {**meta, "n": sample_n, "note": "前 %d 条样本，供 git 内联查看" % sample_n},
         "kox": dataset["kox"][:sample_n]},
    )
    briefs = dump_json(data_dir() / "briefs.json", build_briefs())
    print(f"[data] seed={seed} n={len(dataset['kox'])} -> {_p(full)}  sha256={file_sha256(full)[:16]}…")
    print(f"[data] 注入实际比例：{meta['injection_actual_rates']}")
    print(f"[data] gt 三档分布：{meta['verdict_counts']}")
    print(f"[data] 样本 -> {_p(sample)}；brief -> {_p(briefs)}")
    return 0


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------
def cmd_gate(args: argparse.Namespace) -> int:
    records, meta, _briefs, sha = load_eval_inputs()
    ctx = build_context(records, meta, dataset_sha256=sha)
    thr_path = dump_json(output_dir() / "thresholds.json", ctx.thresholds.to_dict())

    counts = {"pass": 0, "review": 0, "reject": 0}
    for r in ctx.results.values():
        counts[r.verdict] += 1
    verdicts = dump_json(
        output_dir() / "verdicts.json",
        {
            "meta": {
                "dataset_sha256": sha,
                "n": len(records),
                "counts": counts,
                "note": "库级中性口径（campaign 无关）的门禁判定，供前端与回归对比",
            },
            "verdicts": [
                {
                    "kox_id": r.kox_id,
                    "verdict": r.verdict,
                    "group_key": r.group_key,
                    "completeness": round(r.completeness, 4),
                    "authenticity": round(r.authenticity_score, 4),
                    "consistency": round(r.consistency_score, 4),
                    "brand_safety": round(r.brand_safety_score, 4),
                    "fraud_score": round(r.fraud_score, 4),
                    "rules": list(r.rule_ids),
                }
                for r in sorted(ctx.results.values(), key=lambda x: x.kox_id)
            ],
        },
        indent=None,
    )
    sample_ids = [str(k["kox_id"]) for k in records[:GATE_SAMPLE_N]]
    detail = dump_json(
        output_dir() / "gate_results_sample.json",
        {
            "meta": {"n": len(sample_ids), "note": "带完整证据链的门禁明细样本"},
            "results": [ctx.results[i].to_dict() for i in sample_ids if i in ctx.results],
        },
    )
    groups = len(ctx.thresholds.groups)
    print(f"[gate] 标定 {groups} 个 (platform|bucket) 分组 -> {_p(thr_path)}")
    print(f"[gate] 判定分布：{counts}（共 {len(records)} 人）")
    print(f"[gate] 明细 -> {_p(verdicts)}；证据链样本 -> {_p(detail)}")
    return 0


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------
def cmd_budget(args: argparse.Namespace) -> int:
    records, meta, briefs, sha = load_eval_inputs()
    thr_path = output_dir() / "thresholds.json"
    if thr_path.exists():
        from .gates.thresholds import Thresholds

        thresholds = Thresholds.from_dict(load_json(thr_path))
    else:
        thresholds = calibrate(records, meta)

    payload: dict[str, Any] = {"meta": {"dataset_sha256": sha, "n_briefs": len(briefs)}, "plans": []}
    for brief in briefs:
        spec = CampaignSpec.from_dict(brief["spec"])
        plan, results = plan_campaign(records, spec, thresholds)
        base = plan_baseline(records, spec, thresholds, results)
        div = plan_diversified_no_gate(records, spec, thresholds, results)
        # 第三臂只落"摘要"：它按每美元曝光排序会买进几百个便宜长尾，
        # 全量 selected 会把 budget.json 撑到三倍大，而它的用途是价值归因对照，不是可执行方案。
        div_summary = {k: v for k, v in div.to_dict().items() if k not in ("selected", "skipped_sample")}
        div_summary["selected_note"] = (
            "第三臂只保留摘要（不含逐人明细）：它是价值归因用的对照臂，"
            "完整逐人结果可用 koxpilot.budget.planner.plan_diversified_no_gate 现算复现。"
        )
        payload["plans"].append(
            {
                "campaign_id": spec.campaign_id,
                "name": spec.name,
                "koxpilot": plan.to_dict(),
                "baseline_followers": base.to_dict(),
                "diversified_no_gate": div_summary,
            }
        )
        ok = plan.constraints.get("all_enforced_satisfied")
        print(
            f"[budget] {spec.campaign_id} 候选 {plan.candidate_pool} 人 -> 选中 {len(plan.selected)} 人，"
            f"花费 ${plan.spent_usd:,.0f}/{plan.budget_usd:,.0f}"
            f"（利用率 {plan.spent_usd / plan.budget_usd:.1%}），约束全部满足={ok}"
        )
        print(
            f"[budget]   分层占比 {{{', '.join(f'{k}:{v:.1%}' for k, v in plan.tier_mix.items())}}}；"
            f"CPM ${plan.est_cpm_usd:.2f}；估价人数 {plan.n_price_estimated}"
        )
        print(
            f"[budget]   基线（按粉丝量）选中 {len(base.selected)} 人，花费 ${base.spent_usd:,.0f}"
        )
        print(
            f"[budget]   第三臂（只分散化、不门禁）选中 {len(div.selected)} 人，"
            f"花费 ${div.spent_usd:,.0f}，约束全部满足={div.constraints.get('all_enforced_satisfied')}"
        )
    out = dump_json(output_dir() / "budget.json", payload)
    print(f"[budget] -> {_p(out)}")
    return 0


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------
def cmd_eval(args: argparse.Namespace) -> int:
    records, meta, briefs, sha = load_eval_inputs()
    thr_path = output_dir() / "thresholds.json"
    thresholds = None
    if thr_path.exists() and not args.recalibrate:
        from .gates.thresholds import Thresholds

        thresholds = Thresholds.from_dict(load_json(thr_path))
    ctx = build_context(records, meta, briefs, thresholds, sha)
    metrics = run_full_eval(ctx)
    metrics_path = dump_json(output_dir() / "metrics.json", metrics)
    audit_path = dump_json(
        output_dir() / "audit.json",
        {
            "meta": metrics["meta"],
            "counterfactual_value_audit": metrics["counterfactual_value_audit"],
            "cost_audit": metrics["cost_audit"],
            "budget": metrics["budget"],
        },
    )

    t1 = metrics["table_1_fraud_detection"]
    t2 = metrics["table_2_verdict_confusion"]
    print(
        f"[eval] 水号识别 严口径 P={t1['strict']['precision']:.4f} R={t1['strict']['recall']:.4f} "
        f"F1={t1['strict']['f1']:.4f} | 宽口径 F1={t1['loose']['f1']:.4f} | AUC={t1['auc']:.4f}"
    )
    for ftype, row in t1["per_fraud_type"].items():
        print(
            f"[eval]   {ftype:18s} n={row['n']:4d} 严召回={row['recall_strict']:.3f} "
            f"宽召回={row['recall_loose']:.3f} AUC={row['auc_vs_clean']:.3f}"
        )
    print(f"[eval] 三分类 准确率={t2['accuracy']:.4f} macroF1={t2['macro_f1']:.4f}")
    for v, row in t2["per_class"].items():
        print(f"[eval]   {v:6s} P={row['precision']:.4f} R={row['recall']:.4f} F1={row['f1']:.4f}")
    print(
        f"[eval] 敏感性 ±20% 最大 F1 偏移={metrics['table_5_sensitivity']['max_abs_f1_shift']:.4f}"
        f"（稳健={metrics['table_5_sensitivity']['stable']}）"
    )
    print(f"[eval] 反事实价值：{metrics['counterfactual_value_audit']['headline']}")
    attribution = metrics["counterfactual_value_audit"].get("value_attribution")
    if attribution:
        print(f"[eval] 三臂价值归因：{attribution['headline']}")
        for caveat in attribution["caveats"]:
            print(f"[eval]   注意：{caveat}")
    fit_audit = metrics["table_6_llm_vs_rule"]["semantic_fit_llm_vs_rule"]
    if fit_audit.get("status") == "ok":
        print(f"[eval] A4 适配分口径：{fit_audit['headline']}")
        for blocker in fit_audit["blockers"]:
            print(f"[eval]   不予升格的判据：{blocker}")
    else:
        print(f"[eval] A4 适配分口径：{fit_audit.get('note')}")
    print("[eval] 弱项（如实呈现）：")
    for spot in metrics["weak_spots"][:3]:
        print(f"[eval]   - {spot['note']}")
    print(f"[eval] -> {_p(metrics_path)}；{_p(audit_path)}")
    return 0


# ---------------------------------------------------------------------------
# multiseed
# ---------------------------------------------------------------------------
def cmd_multiseed(args: argparse.Namespace) -> int:
    """多种子稳健性实验：全程内存跑 N 个种子，只写 output/multiseed.json。

    刻意**不读 data/ 也不复用 output/thresholds.json**：每个种子必须自己生成数据集、
    自己标定阈值，否则跨种子的"波动范围"会被定稿阈值人为压窄。
    """
    from .eval.multiseed import format_summary, run_multiseed, seed_list

    seeds = seed_list(int(args.seeds))
    print(f"[multiseed] 即将跑 {len(seeds)} 个种子：{seeds}")
    payload = run_multiseed(seeds, n=int(args.n or N_KOX), workers=int(args.workers))
    print(format_summary(payload))
    out = dump_json(output_dir() / "multiseed.json", payload)
    print(f"[multiseed] -> {_p(out)}")
    return 0


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------
def cmd_explain(args: argparse.Namespace) -> int:
    """打印单个达人的完整证据链（面试现场逐条复算用）。"""
    records, meta, briefs, sha = load_eval_inputs()
    target = args.kox_id
    kox = next((k for k in records if str(k.get("kox_id")) == target), None)
    if kox is None:
        print(f"找不到 {target}", file=sys.stderr)
        return 1
    thr_path = output_dir() / "thresholds.json"
    if thr_path.exists():
        from .gates.thresholds import Thresholds

        thresholds = Thresholds.from_dict(load_json(thr_path))
    else:
        thresholds = calibrate(records, meta)
    spec = CampaignSpec()
    if args.campaign:
        brief = next((b for b in briefs if b["spec"]["campaign_id"] == args.campaign), None)
        if brief is None:
            print(f"找不到 campaign {args.campaign}", file=sys.stderr)
            return 1
        spec = CampaignSpec.from_dict(brief["spec"])

    from .gates.engine import evaluate

    res = evaluate(kox, spec, thresholds)
    # handle 本身已带 @（datagen 生成时就是 "@xxx"），这里不要再补一个，否则打成 "@@xxx"
    print(f"{res.kox_id} {kox.get('handle')} | {kox.get('platform')} | {kox.get('country')} | "
          f"{kox.get('followers')} 粉 | 组={res.group_key}")
    print(f"判定：{res.verdict}（campaign={spec.campaign_id}）")
    print(
        f"分数：完整度={res.completeness:.3f} 真实性={res.authenticity_score:.3f} "
        f"一致性={res.consistency_score:.3f} 品牌安全={res.brand_safety_score:.3f} "
        f"异常分={res.fraud_score:.3f} 适配={res.fit_score:.3f}({res.fit_source})"
    )
    if not res.reasons:
        print("证据链：无命中")
    for r in res.reasons:
        print(f"  [{r.rule_id}] {r.severity:5s} w={r.weight:.3f} depth={r.depth:.2f} | {r.human_text}")
        print(f"      signal={r.signal} actual={r.actual} threshold={r.threshold} source={r.source}")
    gt = kox.get("gt")
    if gt and args.show_gt:
        print(f"ground truth（仅供核对，门禁读不到）：{gt}")
    return 0


# ---------------------------------------------------------------------------
# all
# ---------------------------------------------------------------------------
def cmd_all(args: argparse.Namespace) -> int:
    for step in (cmd_data, cmd_gate, cmd_budget, cmd_eval):
        code = step(args)
        if code != 0:
            return code
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="koxpilot", description="KOXPilot Python 核心 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_data = sub.add_parser("data", help="生成合成达人数据集与 brief")
    p_data.add_argument("--n", type=int, default=None, help=f"达人数量（默认 {N_KOX}）")
    p_data.add_argument("--seed", type=int, default=None, help=f"随机种子（默认 {SEED}）")
    p_data.set_defaults(func=cmd_data)

    p_gate = sub.add_parser("gate", help="标定阈值并对全库跑四层门禁")
    p_gate.set_defaults(func=cmd_gate)

    p_budget = sub.add_parser("budget", help="对每个 brief 跑预算分配与基线对照")
    p_budget.set_defaults(func=cmd_budget)

    p_eval = sub.add_parser("eval", help="跑评测 harness，产出 metrics.json / audit.json")
    p_eval.add_argument("--recalibrate", action="store_true", help="忽略已有 thresholds.json 重新标定")
    p_eval.set_defaults(func=cmd_eval)

    p_ms = sub.add_parser("multiseed", help="多种子稳健性实验（纯确定性、零 LLM，只写 output/multiseed.json）")
    p_ms.add_argument("--seeds", type=int, default=DEFAULT_N_SEEDS, help=f"种子数量（默认 {DEFAULT_N_SEEDS}，含原始种子 {SEED}）")
    p_ms.add_argument("--n", type=int, default=None, help=f"每个种子的达人数量（默认 {N_KOX}）")
    p_ms.add_argument("--workers", type=int, default=1, help="并行进程数（默认 1；worker 全程内存计算，无共享文件写入）")
    p_ms.set_defaults(func=cmd_multiseed)

    p_explain = sub.add_parser("explain", help="打印单个达人的完整证据链")
    p_explain.add_argument("kox_id")
    p_explain.add_argument("--campaign", default="", help="按某个 campaign 口径判定（默认库级中性）")
    p_explain.add_argument("--show-gt", action="store_true", help="同时打印 ground truth 供核对")
    p_explain.set_defaults(func=cmd_explain)

    p_all = sub.add_parser("all", help="data -> gate -> budget -> eval")
    p_all.add_argument("--n", type=int, default=None)
    p_all.add_argument("--seed", type=int, default=None)
    p_all.add_argument("--recalibrate", action="store_true")
    p_all.set_defaults(func=cmd_all)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
