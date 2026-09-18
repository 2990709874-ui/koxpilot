# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/eval/sensitivity.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
"""阈值敏感性扫描（SPEC 第 7 节表 5）：关键阈值 ±20%，看指标稳不稳。

它回答的问题是"你这套阈值是不是调参调出来的"
--------------------------------------------
如果把全部判定阈值同时放大/缩小 20%，F1 只动零点几个百分点，
说明结论由**数据结构**决定，不是由某个精心挑选的数字决定；
反之若指标剧烈摆动，就必须承认这套阈值脆弱。两种结果都照实报。

方向性说明（容易被追问）：``Thresholds.scaled(f)`` 是把分位数阈值整体乘 f，
对上尾规则（>P95）放大 = 放松，对下尾规则（<P05）放大 = 收紧，
所以单一 factor 同时覆盖了"更宽松"和"更严格"两个方向，不需要分开扫两遍。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..gates.engine import evaluate_all
from ..gates.policy import ALL_GATES, SENSITIVITY_SIGNALS
from ..gates.thresholds import Thresholds
from ..types import CampaignSpec, Kox
from .metrics import fraud_report, verdict_report

__all__ = ["SCAN_FACTORS", "sensitivity_report"]

#: 扫描系数。SPEC 要求 ±20%，这里额外加 ±10% 让曲线有中间点（否则看不出是线性还是断崖）。
SCAN_FACTORS: tuple[float, ...] = (0.8, 0.9, 1.0, 1.1, 1.2)

#: 判定"稳健"的经验界：严口径 F1 在 ±20% 扫描下的最大偏移不超过这个值。
#: 0.05 的来源：5,000 样本、水号占比 16% 时，F1 的自举标准差量级约 0.012，
#: 取 4 倍标准差作为"明显超出抽样噪声"的门槛。
STABILITY_TOLERANCE: float = 0.05


def _metrics(
    records: Sequence[Kox], spec: CampaignSpec, thresholds: Thresholds
) -> dict[str, float]:
    results = {r.kox_id: r for r in evaluate_all(records, spec, thresholds, None, ALL_GATES)}
    fraud = fraud_report(records, results)
    verdict = verdict_report(records, results)
    return {
        "fraud_precision_strict": float(fraud["strict"]["precision"]),
        "fraud_recall_strict": float(fraud["strict"]["recall"]),
        "fraud_f1_strict": float(fraud["strict"]["f1"]),
        "fraud_auc": float(fraud["auc"]),
        "verdict_accuracy": float(verdict["accuracy"]),
        "n_reject": float(sum(1 for r in results.values() if r.verdict == "reject")),
    }


def sensitivity_report(
    records: Sequence[Kox],
    spec: CampaignSpec,
    thresholds: Thresholds,
    baseline: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """全信号联动扫描 + 单信号扫描（后者定位"哪个阈值最敏感"）。"""
    base = dict(baseline) if baseline is not None else _metrics(records, spec, thresholds)

    overall: list[dict[str, Any]] = []
    for factor in SCAN_FACTORS:
        m = base if factor == 1.0 else _metrics(records, spec, thresholds.scaled(factor))
        overall.append(
            {
                "factor": factor,
                "metrics": m,
                "d_fraud_f1_strict": round(m["fraud_f1_strict"] - base["fraud_f1_strict"], 4),
                "d_verdict_accuracy": round(m["verdict_accuracy"] - base["verdict_accuracy"], 4),
            }
        )
    max_shift = max(abs(float(row["d_fraud_f1_strict"])) for row in overall)

    per_signal: list[dict[str, Any]] = []
    for signal in SENSITIVITY_SIGNALS:
        row: dict[str, Any] = {"signal": signal, "points": []}
        shifts: list[float] = []
        for factor in (0.8, 1.2):
            m = _metrics(records, spec, thresholds.scaled(factor, (signal,)))
            shift = round(m["fraud_f1_strict"] - base["fraud_f1_strict"], 4)
            shifts.append(abs(shift))
            row["points"].append(
                {"factor": factor, "fraud_f1_strict": m["fraud_f1_strict"], "delta": shift}
            )
        row["max_abs_shift"] = round(max(shifts), 4)
        per_signal.append(row)
    per_signal.sort(key=lambda r: -float(r["max_abs_shift"]))

    return {
        "baseline": base,
        "factors": list(SCAN_FACTORS),
        "scaled_signals": list(SENSITIVITY_SIGNALS),
        "overall": overall,
        "per_signal": per_signal,
        "max_abs_f1_shift": round(max_shift, 4),
        "stability_tolerance": STABILITY_TOLERANCE,
        "stable": max_shift <= STABILITY_TOLERANCE,
        "most_sensitive_signal": per_signal[0]["signal"] if per_signal else None,
        "note": (
            "factor 是判定阈值的整体缩放系数：对上尾规则放大=放松、对下尾规则放大=收紧，"
            "因此一次扫描同时覆盖两个方向。"
        ),
    }
