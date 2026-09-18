# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/eval/strata.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
"""分层评测（SPEC 第 7 节表 3）：按平台 / 粉丝分层 / 国家 / 平台×分层 报指标，并主动挑出弱项。

设计要点
--------
- **最小支撑数**：格子里正样本太少时 F1 的方差极大，报出来只会误导。
  低于 ``MIN_SUPPORT_POSITIVE`` 的格子仍然照实输出，但会打上
  ``low_support=true`` 并且**不参与**弱项排序，避免"用噪声当结论"。
- **弱项主动暴露**：``weak_spots`` 按 F1 升序取前若干个，附带一句人话解释，
  直接进报告与前端。指标不好看的格子不许藏。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..taxonomy import BUCKET_ORDER, PLATFORMS, follower_bucket
from ..types import GateResult
from .metrics import binary_scores, fraud_pred_strict, gt_of

__all__ = ["MIN_SUPPORT_POSITIVE", "WEAK_SPOT_TOP_N", "strata_report"]

#: 一个格子至少要有这么多真水号，其 F1 才被认为可用于比较。
#: 10 的来源：正样本 <10 时单个样本的翻转会让 recall 抖动 >10 个百分点，量级上盖过真实差异。
MIN_SUPPORT_POSITIVE: int = 10

#: 弱项清单长度（报告与前端各展示这么多条）
WEAK_SPOT_TOP_N: int = 6


def _cell() -> dict[str, Any]:
    return {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "verdict_correct": 0, "n": 0}


def _accumulate(
    cells: dict[str, dict[str, Any]],
    key: str,
    pred_fraud: bool,
    truth_fraud: bool,
    verdict_ok: bool,
) -> None:
    c = cells.setdefault(key, _cell())
    c["n"] += 1
    c["verdict_correct"] += int(verdict_ok)
    if pred_fraud and truth_fraud:
        c["tp"] += 1
    elif pred_fraud:
        c["fp"] += 1
    elif truth_fraud:
        c["fn"] += 1
    else:
        c["tn"] += 1


def _finalize(cells: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, c in cells.items():
        stats = binary_scores(int(c["tp"]), int(c["fp"]), int(c["fn"]), int(c["tn"]))
        positives = int(c["tp"]) + int(c["fn"])
        stats.update(
            {
                "n": int(c["n"]),
                "positives": positives,
                "verdict_accuracy": round(int(c["verdict_correct"]) / int(c["n"]), 4)
                if c["n"]
                else 0.0,
                "low_support": positives < MIN_SUPPORT_POSITIVE,
            }
        )
        out[key] = stats
    return dict(sorted(out.items()))


def strata_report(
    records: Sequence[Mapping[str, Any]], results: Mapping[str, GateResult]
) -> dict[str, Any]:
    """四种切法的分层指标 + 弱项清单。"""
    by_platform: dict[str, dict[str, Any]] = {}
    by_bucket: dict[str, dict[str, Any]] = {}
    by_country: dict[str, dict[str, Any]] = {}
    by_group: dict[str, dict[str, Any]] = {}

    for kox in records:
        res = results.get(str(kox.get("kox_id")))
        if res is None:
            continue
        gt = gt_of(kox)
        truth = bool(gt.get("is_fraud"))
        pred = fraud_pred_strict(res)
        verdict_ok = res.verdict == str(gt.get("verdict", "pass"))
        platform = str(kox.get("platform", "unknown"))
        followers = kox.get("followers")
        bucket = follower_bucket(float(followers) if isinstance(followers, (int, float)) else None)
        country = str(kox.get("country", "??"))
        _accumulate(by_platform, platform, pred, truth, verdict_ok)
        _accumulate(by_bucket, bucket, pred, truth, verdict_ok)
        _accumulate(by_country, country, pred, truth, verdict_ok)
        _accumulate(by_group, f"{platform}|{bucket}", pred, truth, verdict_ok)

    platform_cells = _finalize(by_platform)
    bucket_cells = _finalize(by_bucket)
    country_cells = _finalize(by_country)
    group_cells = _finalize(by_group)

    ranked = sorted(
        (
            (dim, key, stats)
            for dim, cells in (
                ("platform", platform_cells),
                ("follower_bucket", bucket_cells),
                ("country", country_cells),
                ("platform×bucket", group_cells),
            )
            for key, stats in cells.items()
            if not stats["low_support"]
        ),
        key=lambda item: (item[2]["f1"], -item[2]["positives"], item[1]),
    )
    weak = [
        {
            "dimension": dim,
            "cell": key,
            "f1": stats["f1"],
            "precision": stats["precision"],
            "recall": stats["recall"],
            "positives": stats["positives"],
            "n": stats["n"],
            "note": (
                f"{dim}={key} 的水号识别 F1 为 {stats['f1']:.3f}"
                f"（精确率 {stats['precision']:.3f} / 召回率 {stats['recall']:.3f}，"
                f"该格 {stats['n']} 人含 {stats['positives']} 个真水号）"
            ),
        }
        for dim, key, stats in ranked[:WEAK_SPOT_TOP_N]
    ]
    return {
        "min_support_positive": MIN_SUPPORT_POSITIVE,
        "by_platform": {p: platform_cells[p] for p in PLATFORMS if p in platform_cells},
        "by_follower_bucket": {b: bucket_cells[b] for b in BUCKET_ORDER if b in bucket_cells},
        "by_country": country_cells,
        "by_platform_bucket": group_cells,
        "weak_spots": weak,
        "note": (
            "低支撑格子（真水号 < %d）照实输出但不参与弱项排序，"
            "避免用高方差数字下结论。" % MIN_SUPPORT_POSITIVE
        ),
    }
