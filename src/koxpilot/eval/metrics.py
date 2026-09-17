"""评测口径与指标计算（SPEC 第 7 节）。

**口径先于数字**：本文件把"什么算预测为水号""什么算预测正确"写死成函数，
所有表格都必须复用这些函数，不允许各表各算一套（那是指标注水的头号来源）。

三个口径定义（面试必答，故在代码里逐条固化）
--------------------------------------------
1. ``fraud_pred_strict``：门禁**因真实性而拒**——authenticity 低于地板，或命中 ≥2 条硬信号。
   这是产品真正会自动拦掉的人，对应"精确率优先"的运营口径。
2. ``fraud_pred_loose``：G1 有任何一条命中就算怀疑（对应"进人核队列"的口径，召回优先）。
3. ``fraud_score``：连续异常分，只用于 AUC / PR 曲线；离散命中数不能当连续分用。

诚实性纪律：本文件是**唯一**允许读取 ``gt`` 的地方之一（另一处是 audit.py），
且只作为裁判读取；任何被评测的判定逻辑都不得从这里取值。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..gates.policy import AUTHENTICITY_FLOOR_REJECT, HARD_HITS_REJECT_MIN
from ..stats import prf1, roc_auc
from ..types import GateResult, Verdict

__all__ = [
    "FRAUD_TYPES",
    "VERDICTS",
    "binary_scores",
    "confusion_matrix",
    "fraud_pred_loose",
    "fraud_pred_strict",
    "fraud_report",
    "gt_of",
    "macro_f1",
    "verdict_report",
]

VERDICTS: tuple[Verdict, ...] = ("pass", "review", "reject")
FRAUD_TYPES: tuple[str, ...] = (
    "bought_followers",
    "engagement_pod",
    "bot_comments",
    "view_inflation",
)


def gt_of(kox: Mapping[str, Any]) -> dict[str, Any]:
    """取 ground truth 块（仅评测使用）。"""
    gt = kox.get("gt")
    return dict(gt) if isinstance(gt, Mapping) else {}


def fraud_pred_strict(result: GateResult) -> bool:
    """严口径：门禁会自动拦掉的"真实性不过关"。"""
    return (
        result.authenticity_score < AUTHENTICITY_FLOOR_REJECT
        or len(result.hard_hits) >= HARD_HITS_REJECT_MIN
    )


def fraud_pred_loose(result: GateResult) -> bool:
    """宽口径：G1 命中任意一条即视为"值得人核的可疑号"。"""
    return any(r.gate == "G1" for r in result.reasons)


def binary_scores(tp: int, fp: int, fn: int, tn: int) -> dict[str, float]:
    """P/R/F1 + 特异度 + 支撑数，一次给全（少一个就会被追问）。"""
    out = prf1(tp, fp, fn)
    total = tp + fp + fn + tn
    out.update(
        {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "specificity": round(tn / (tn + fp), 4) if (tn + fp) else 0.0,
            "accuracy": round((tp + tn) / total, 4) if total else 0.0,
            "n": total,
        }
    )
    return out


def _binary_from_pairs(pairs: Iterable[tuple[bool, bool]]) -> dict[str, float]:
    tp = fp = fn = tn = 0
    for pred, truth in pairs:
        if pred and truth:
            tp += 1
        elif pred and not truth:
            fp += 1
        elif truth:
            fn += 1
        else:
            tn += 1
    return binary_scores(tp, fp, fn, tn)


def confusion_matrix(
    records: Sequence[Mapping[str, Any]], results: Mapping[str, GateResult]
) -> dict[str, dict[str, int]]:
    """``matrix[gt_verdict][pred_verdict] = count``（行 = 真值，列 = 预测）。"""
    matrix = {g: {p: 0 for p in VERDICTS} for g in VERDICTS}
    for kox in records:
        res = results.get(str(kox.get("kox_id")))
        if res is None:
            continue
        truth = str(gt_of(kox).get("verdict", "pass"))
        if truth in matrix:
            matrix[truth][res.verdict] += 1
    return matrix


def macro_f1(per_class: Mapping[str, Mapping[str, float]]) -> float:
    vals = [float(v["f1"]) for v in per_class.values()]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def verdict_report(
    records: Sequence[Mapping[str, Any]], results: Mapping[str, GateResult]
) -> dict[str, Any]:
    """三分类混淆矩阵 + 每档 precision/recall/F1 + 总体准确率（SPEC 7 表 2）。"""
    matrix = confusion_matrix(records, results)
    total = sum(sum(row.values()) for row in matrix.values())
    correct = sum(matrix[v][v] for v in VERDICTS)
    per_class: dict[str, dict[str, float]] = {}
    for v in VERDICTS:
        tp = matrix[v][v]
        fn = sum(matrix[v][p] for p in VERDICTS if p != v)
        fp = sum(matrix[g][v] for g in VERDICTS if g != v)
        tn = total - tp - fn - fp
        per_class[v] = binary_scores(tp, fp, fn, tn)
    return {
        "n": total,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "macro_f1": macro_f1(per_class),
        "matrix": matrix,
        "per_class": per_class,
        "matrix_note": "行 = gt.verdict，列 = 门禁判定；对角线为一致格",
    }


def fraud_report(
    records: Sequence[Mapping[str, Any]], results: Mapping[str, GateResult]
) -> dict[str, Any]:
    """水号识别指标：严/宽两个离散口径 + 连续分 AUC + 按 fraud_type 分报（SPEC 7 表 1）。"""
    strict_pairs: list[tuple[bool, bool]] = []
    loose_pairs: list[tuple[bool, bool]] = []
    scores: list[float] = []
    labels: list[int] = []
    by_type: dict[str, dict[str, Any]] = {}
    clean_scores: list[float] = []

    for kox in records:
        res = results.get(str(kox.get("kox_id")))
        if res is None:
            continue
        gt = gt_of(kox)
        truth = bool(gt.get("is_fraud"))
        ftype = gt.get("fraud_type")
        strict = fraud_pred_strict(res)
        loose = fraud_pred_loose(res)
        strict_pairs.append((strict, truth))
        loose_pairs.append((loose, truth))
        scores.append(res.fraud_score)
        labels.append(1 if truth else 0)
        if not truth:
            clean_scores.append(res.fraud_score)
        if truth and isinstance(ftype, str):
            slot = by_type.setdefault(
                ftype, {"n": 0, "strict_hit": 0, "loose_hit": 0, "score_sum": 0.0, "scores": []}
            )
            slot["n"] += 1
            slot["strict_hit"] += int(strict)
            slot["loose_hit"] += int(loose)
            slot["score_sum"] += res.fraud_score
            slot["scores"].append(res.fraud_score)

    per_type: dict[str, Any] = {}
    for ftype in FRAUD_TYPES:
        slot = by_type.get(ftype)
        if not slot:
            continue
        n = int(slot["n"])
        pos = list(slot["scores"])
        auc = roc_auc(pos + clean_scores, [1] * len(pos) + [0] * len(clean_scores))
        per_type[ftype] = {
            "n": n,
            "recall_strict": round(slot["strict_hit"] / n, 4) if n else 0.0,
            "recall_loose": round(slot["loose_hit"] / n, 4) if n else 0.0,
            "mean_fraud_score": round(float(slot["score_sum"]) / n, 4) if n else 0.0,
            "auc_vs_clean": auc,
        }

    return {
        "strict": _binary_from_pairs(strict_pairs),
        "loose": _binary_from_pairs(loose_pairs),
        "auc": roc_auc(scores, labels),
        "prevalence": round(sum(labels) / len(labels), 4) if labels else 0.0,
        "per_fraud_type": per_type,
        "definitions": {
            "strict": "authenticity < 地板 或 硬信号 ≥2 条（门禁自动拦掉的人）",
            "loose": "G1 命中任意一条（进人核队列的人）",
            "auc": "用连续异常分 fraud_score 算，与离散判定无关",
            "auc_vs_clean": "该造假类型 vs 全部真实号的一对多 AUC",
        },
    }
