"""KOXPilot 统计原语（全部手写，不依赖 sklearn/pandas）。

设计意图
--------
本模块是整个项目"阈值可解释"的地基。项目纪律要求：
1. 任何门禁阈值都必须能追溯到**数据分位数**或**显式配置**，不允许魔法数字；
2. 评测指标（AUC / P / R / F1）必须能用几句话讲清算法，
   因此不引入 sklearn，而是自己实现（AUC 用 Mann–Whitney U 的秩和公式，含并列秩修正）。

所有函数都是纯函数：输入序列 -> 输出数值，不读全局状态、不写文件。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Final

__all__ = [
    "jaccard",
    "mad",
    "mean",
    "median",
    "percentile_rank",
    "prf1",
    "quantile",
    "quantiles",
    "robust_zscores",
    "roc_auc",
    "safe_div",
]

_EPS: Final[float] = 1e-12


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """除零保护除法。评测里到处需要它，集中一处实现避免各处写 if。"""
    if denominator is None or abs(denominator) < _EPS:
        return default
    return numerator / denominator


def mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return math.fsum(values) / len(values)


def quantile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（等价于 numpy 默认的 'linear' / R 的 type-7）。

    手写原因：这是全项目所有 G1 阈值的唯一来源，必须能逐步复算核对。
    实现：把 q 映射到排序后数组的实数位置 h = (n-1)*q，
    再在 floor(h) 与 ceil(h) 两个样本之间线性插值。

    Args:
        values: 样本序列（不要求已排序，函数内部排序，不修改入参）。
        q: 分位点，取值 [0, 1]。
    Returns:
        分位数值。空序列返回 0.0（调用方需自行判断样本量是否足够）。
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0, 1], got {q}")
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return float(ordered[0])
    h = (n - 1) * q
    lo = math.floor(h)
    hi = math.ceil(h)
    if lo == hi:
        return float(ordered[int(h)])
    frac = h - lo
    return float(ordered[lo] * (1.0 - frac) + ordered[hi] * frac)


def quantiles(values: Sequence[float], qs: Iterable[float]) -> dict[str, float]:
    """一次算多个分位数，key 形如 ``p05`` / ``p97`` / ``p02``。

    key 用两位（必要时三位）整数百分位表示，前端 TS 侧读同一份 JSON 时按同名 key 取值。
    """
    ordered = sorted(values)
    out: dict[str, float] = {}
    for q in qs:
        pct = q * 100.0
        label = f"p{int(round(pct)):02d}" if abs(pct - round(pct)) < 1e-9 else f"p{pct:g}"
        out[label] = quantile(ordered, q)
    return out


def median(values: Sequence[float]) -> float:
    return quantile(values, 0.5)


def mad(values: Sequence[float]) -> float:
    """Median Absolute Deviation（中位数绝对偏差）。

    用于 G1.5 粉丝突刺检测：单账号的月度增速序列很短（11 个点）且本身就含突刺，
    普通标准差会被突刺自己撑大导致检不出来（掩蔽效应），所以用 MAD 做鲁棒尺度。
    """
    if not values:
        return 0.0
    med = median(values)
    return median([abs(v - med) for v in values])


def robust_zscores(values: Sequence[float]) -> list[float]:
    """鲁棒 z-score：z_i = (x_i - median) / (1.4826 * MAD)。

    1.4826 = 1/Φ⁻¹(0.75)，作用是让 MAD 在正态分布下与标准差一致，
    这样 SPEC 里 "z > 2.5" 的门槛与常规 σ 语义可比。
    MAD 为 0 时（序列几乎恒定）退化为用均值绝对偏差，仍为 0 则返回全 0。
    """
    if not values:
        return []
    med = median(values)
    scale = 1.4826 * mad(values)
    if scale < _EPS:
        scale = mean([abs(v - med) for v in values])
    if scale < _EPS:
        return [0.0] * len(values)
    return [(v - med) / scale for v in values]


def percentile_rank(sorted_values: Sequence[float], x: float) -> float:
    """x 在已排序样本中的经验百分位（0~1），用二分查找。

    G1 的连续异常分（用于算 AUC）就是基于它构造的：
    把"实际值"映射成"在同平台同量级同行里排第几"，天然无量纲、跨组可比。
    """
    n = len(sorted_values)
    if n == 0:
        return 0.5
    lo, hi = 0, n
    while lo < hi:  # bisect_left
        mid = (lo + hi) // 2
        if sorted_values[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    left = lo
    lo, hi = 0, n
    while lo < hi:  # bisect_right
        mid = (lo + hi) // 2
        if sorted_values[mid] <= x:
            lo = mid + 1
        else:
            hi = mid
    right = lo
    return (left + right) / (2.0 * n)


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """集合 Jaccard 相似度。两个空集定义为 1.0（都没声明品类不算冲突）。

    G2.1 / G2.2 的品类一致性判定基于它。定义边界写在这里以免各处口径不一。
    """
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


def roc_auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """ROC-AUC，用 Mann–Whitney U 秩和公式实现（含并列秩平均修正）。

        AUC = (R_pos - n_pos*(n_pos+1)/2) / (n_pos * n_neg)

    其中 R_pos 是正样本的秩和。含义：随机取一正一负，正样本分更高的概率
    （并列算 0.5）。这是可口述复算的版本：O(n log n)，不需要扫阈值画曲线。

    Args:
        scores: 连续预测分（越大越像正类）。
        labels: 0/1 真值。
    Returns:
        AUC。正类或负类为空时返回 0.5（无判别信息）。
    """
    if len(scores) != len(labels):
        raise ValueError("scores/labels length mismatch")
    n = len(scores)
    n_pos = sum(1 for y in labels if y == 1)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 秩从 1 开始，并列取平均
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    r_pos = math.fsum(ranks[i] for i in range(n) if labels[i] == 1)
    return (r_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def prf1(tp: int, fp: int, fn: int) -> dict[str, float]:
    """由混淆计数算 precision / recall / f1。所有分母为 0 的格子按 0.0 报，不隐藏。"""
    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)
    f1 = safe_div(2 * precision * recall, precision + recall)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }
