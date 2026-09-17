"""可观测信号抽取：门禁引擎能看到的全部输入，就是这里列出的东西。

设计意图（防自证的第一道物理隔离）
------------------------------------
门禁不直接摸 kox 原始 dict，而是先经过本模块把"可观测信号"抽出来。
好处有三：
1. 想读 ground truth 也读不到 —— 抽取函数只认这份白名单；
2. 阈值标定与规则判定用**同一个抽取函数**，不可能出现"标定用 A 口径、判定用 B 口径"的经典 bug；
3. 缺失字段（``_missing``）在这里统一变成 ``None``，各规则只需判断 None 即可跳过。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from ..stats import safe_div
from ..taxonomy import follower_bucket, group_key
from .policy import SEVERITY_HITS_CAP, SEVERITY_POINTS

__all__ = [
    "OBSERVABLE_SIGNALS",
    "extract_signals",
    "kox_group_key",
    "monthly_growth_rates",
    "risk_severity_score",
]

#: 参与分位数标定的信号名（与 policy.SIGNAL_QUANTILES 的 key 一致）
OBSERVABLE_SIGNALS: Final[tuple[str, ...]] = (
    "engagement_rate",
    "comment_like_ratio",
    "view_follower_ratio",
    "followers_per_day",
    "comment_dup_rate",
    "comment_emoji_only_rate",
    "max_monthly_growth",
    "risk_severity_score",
    "avg_cpm_usd",
)


def _num(value: Any) -> float | None:
    """安全取数：None / 非数值 / 布尔 一律视为缺失。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def kox_group_key(kox: Mapping[str, Any]) -> str:
    """分位数分组键 ``platform|bucket``。followers 缺失时落入 nano 组（最保守）。"""
    return group_key(str(kox.get("platform", "unknown")), follower_bucket(_num(kox.get("followers"))))


def risk_severity_score(kox: Mapping[str, Any]) -> float:
    """累计风险载荷分（只统计 low/medium；high 走 G3.1 硬阻断，不参与累加）。

    单条 flag 的 hits 在 SEVERITY_HITS_CAP 处截断：一条 flag 命中 10 次和命中 3 次，
    对品牌方的决策含义差别不大，但不截断会让分数被单条 flag 主导。
    """
    total = 0.0
    for flag in kox.get("content_flags") or []:
        if not isinstance(flag, Mapping):
            continue
        severity = str(flag.get("severity", "low"))
        if severity == "high":
            continue
        hits = _num(flag.get("hits")) or 1.0
        total += SEVERITY_POINTS.get(severity, 1.0) * min(hits, float(SEVERITY_HITS_CAP))
    controversy = kox.get("controversy")
    if isinstance(controversy, Mapping):
        total += SEVERITY_POINTS.get(str(controversy.get("severity", "low")), 1.0)
    return total


def monthly_growth_rates(history: list[Any] | None) -> list[float]:
    """粉丝历史 -> 月度环比增速序列（长度 = len(history) - 1）。"""
    if not history or len(history) < 2:
        return []
    rates: list[float] = []
    for prev, cur in zip(history, history[1:]):
        p, c = _num(prev), _num(cur)
        if p is None or c is None or p <= 0:
            rates.append(0.0)
        else:
            rates.append((c - p) / p)
    return rates


def extract_signals(kox: Mapping[str, Any]) -> dict[str, float | None]:
    """抽出全部可观测标量信号。缺失一律 None，绝不用 0 冒充。

    ``followers_per_day`` 是派生信号（G1.6「新号巨量粉」），
    在这里派生而不是在规则里派生，是为了让标定与判定共享同一定义。
    """
    followers = _num(kox.get("followers"))
    age_days = _num(kox.get("account_age_days"))
    fpd: float | None = None
    if followers is not None and age_days is not None and age_days > 0:
        fpd = safe_div(followers, age_days)
    growth = monthly_growth_rates(kox.get("follower_history"))
    return {
        "engagement_rate": _num(kox.get("engagement_rate")),
        "comment_like_ratio": _num(kox.get("comment_like_ratio")),
        "view_follower_ratio": _num(kox.get("view_follower_ratio")),
        "followers_per_day": fpd,
        "comment_dup_rate": _num(kox.get("comment_dup_rate")),
        "comment_emoji_only_rate": _num(kox.get("comment_emoji_only_rate")),
        "max_monthly_growth": max(growth) if growth else None,
        "risk_severity_score": risk_severity_score(kox),
        "avg_cpm_usd": _num(kox.get("avg_cpm_usd")),
    }
