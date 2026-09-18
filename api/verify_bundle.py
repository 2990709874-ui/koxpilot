"""一次性核验脚本：证明 ``_bundle`` 里的降级副本与仓库源码**算出来的东西一模一样**。

为什么要有它
------------
``build_bundle.py`` 对源码做了机械改写（摘 ``slots=`` 等）。「机械」和「保行为」是
两件事，前者不自动推出后者。所以这里不靠论证，靠对数：

    仓库源码 @ Python 3.11   ->  canonical JSON  ->  sha256
    bundle 副本 @ Python 3.8 ->  canonical JSON  ->  sha256

两个 sha256 相等才算过。指纹覆盖：
- 全库 5,000 条的门禁判定 + 四层分数 + 命中规则 + 每条 human_text；
- 3 个预置 brief 的完整预算方案（选人、金额、条数、结构约束报告、trace）。

用法：
    python3 api/verify_bundle.py dump   # 用当前解释器 + 当前 PYTHONPATH 算并打印 sha256
    python3 api/verify_bundle.py dump --out /tmp/a.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any, Dict, List


def fingerprint(data_root: str) -> Dict[str, Any]:
    """跑真链路并返回一份确定性的、可 sha256 的结果快照。"""
    from koxpilot.budget.planner import plan_baseline, plan_campaign
    from koxpilot.gates.engine import evaluate, evaluate_all
    from koxpilot.gates.thresholds import Thresholds
    from koxpilot.io_utils import load_json
    from koxpilot.types import CampaignSpec

    root = data_root.rstrip("/")
    dataset = load_json("%s/data/kox_5000.json" % root)
    records: List[Dict[str, Any]] = list(dataset["kox"])
    briefs = load_json("%s/data/briefs.json" % root)["briefs"]
    thresholds = Thresholds.from_dict(load_json("%s/output/thresholds.json" % root))

    # --- 全库中性口径门禁 ---
    gate_rows: List[Dict[str, Any]] = []
    for res in sorted(evaluate_all(records, thresholds=thresholds), key=lambda r: r.kox_id):
        gate_rows.append(
            {
                "kox_id": res.kox_id,
                "verdict": res.verdict,
                "group": res.group_key,
                "scores": [
                    round(res.completeness, 10),
                    round(res.authenticity_score, 10),
                    round(res.consistency_score, 10),
                    round(res.brand_safety_score, 10),
                    round(res.fraud_score, 10),
                    round(res.fit_score, 10),
                ],
                "blocked_by": res.blocked_by,
                "hard_hits": list(res.hard_hits),
                "reasons": [
                    [r.rule_id, r.signal, str(r.actual), str(r.threshold),
                     round(r.weight, 10), round(r.depth, 10), r.severity, r.source, r.human_text]
                    for r in res.reasons
                ],
            }
        )

    # --- 每个 brief 的完整预算方案（含基线臂） ---
    plans: List[Dict[str, Any]] = []
    for brief in briefs:
        spec = CampaignSpec.from_dict(brief["spec"])
        plan, results = plan_campaign(records, spec, thresholds)
        base = plan_baseline(records, spec, thresholds, results)
        plans.append({"campaign_id": spec.campaign_id, "koxpilot": plan.to_dict(),
                      "baseline": base.to_dict()})

    # --- 单条 explain（CLI 口径）---
    explains: List[Dict[str, Any]] = []
    by_id = {str(k.get("kox_id")): k for k in records}
    for kox_id in ("KOX-000001", "KOX-000020", "KOX-000123", "KOX-004999"):
        kox = by_id.get(kox_id)
        if kox is None:
            continue
        res = evaluate(kox, CampaignSpec(), thresholds)
        explains.append({"kox_id": kox_id, "verdict": res.verdict,
                         "texts": [r.human_text for r in res.reasons]})

    return {
        "python": "%d.%d" % sys.version_info[:2],
        "n_records": len(records),
        "gates": gate_rows,
        "plans": plans,
        "explains": explains,
    }


def canonical(payload: Dict[str, Any]) -> bytes:
    """去掉「解释器版本」这一项后做确定性序列化——我们比的是结果，不是环境。"""
    body = {k: v for k, v in payload.items() if k != "python"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["dump"])
    ap.add_argument("--data-root", required=True, help="含 data/ 与 output/ 的目录")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    payload = fingerprint(args.data_root)
    blob = canonical(payload)
    digest = hashlib.sha256(blob).hexdigest()
    print("python=%s  records=%d  sha256=%s" % (payload["python"], payload["n_records"], digest))
    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(blob)
        print("canonical -> %s (%d bytes)" % (args.out, len(blob)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
