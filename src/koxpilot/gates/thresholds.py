"""分位数阈值标定器：把"阈值"从代码里彻底赶到数据里。

为什么必须这样做
----------------
硬编码 "engagement_rate > 0.12 = 互动农场" 在跨平台跨量级场景下必然错：
TikTok nano 号 12% 互动率很正常，YouTube mega 号 12% 一定有鬼。
所以所有 G1 阈值都按 ``(platform, follower_bucket)`` 分组、从**当前达人库的经验分布**标定。

落盘结构（output/thresholds.json）——前端 TS 引擎读同一份文件才能与 Python 对齐：
```
{
  "meta":   {...},                       # 种子、样本量、标定时间、版本
  "policy": {...},                       # policy.py 的常量快照
  "groups": {"tiktok|micro": {"engagement_rate": {"n":..,"source":..,"p05":..,"p95":..,"grid":[..]}}},
  "platform": {"tiktok": {...}},         # 一级回退
  "global":   {...}                      # 二级回退
}
```
样本量不足（< MIN_GROUP_SAMPLES）的组自动回退，并在 ``source`` 字段里如实标注是哪一级，
这样核验时可以直接指着 kwai|mega 说明：这一组只有 9 个样本，采用的是平台级阈值。
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any

from ..stats import mad, median, quantile
from ..types import Kox
from .policy import (
    GRID_STEP_PCT,
    MIN_GROUP_SAMPLES,
    ROBUST_SPACE,
    SIGNAL_QUANTILES,
    policy_snapshot,
)
from .signals import (
    OBSERVABLE_SIGNALS,
    extract_signals,
    kox_group_key,
)

__all__ = ["THRESHOLDS_VERSION", "Thresholds", "calibrate", "robust_quantile"]

THRESHOLDS_VERSION = "1.1.0"

_NORM = NormalDist()
_MAD_TO_SIGMA = 1.4826

_GRID_QS: tuple[float, ...] = tuple(
    round(p / 100.0, 4) for p in range(0, 101, GRID_STEP_PCT)
)


def _q_label(q: float) -> str:
    pct = q * 100.0
    return f"p{int(round(pct)):02d}"


def robust_quantile(values: Sequence[float], q: float, space: str) -> float:
    """鲁棒分位数估计：``median + z_q · 1.4826 · MAD``，可在 log / log1p / 线性空间做。

    为什么不用经验分位数（这是本项目的核心工程判断，见 policy.ROBUST_SPACE 注释）：
    达人库本身含 16% 水号且水号就长在尾部，经验分位数会被污染样本推着走，
    等于"让作弊者自己定义什么算作弊"。median/MAD 的击穿点是 50%，16% 污染动不了它。

    Args:
        values: 样本（含污染）。
        q: 目标百分位（0~1）。
        space: ``log`` / ``log1p`` / ``linear`` / ``empirical``。
    Returns:
        阈值。样本不足或 MAD 退化为 0 时自动回落到经验分位数（宁可保守，不要 NaN）。
    """
    if not values:
        return 0.0
    if space == "empirical":
        return quantile(values, q)
    if space == "log":
        xs = [math.log(v) for v in values if v > 0.0]
    elif space == "log1p":
        xs = [math.log1p(v) for v in values if v > -1.0]
    else:
        xs = list(values)
    if len(xs) < 8:
        return quantile(values, q)
    center = median(xs)
    scale = _MAD_TO_SIGMA * mad(xs)
    if scale <= 0.0:
        return quantile(values, q)
    z = _NORM.inv_cdf(min(max(q, 1e-6), 1 - 1e-6))
    est = center + z * scale
    if space == "log":
        return math.exp(est)
    if space == "log1p":
        return math.expm1(est)
    return est


def _summarize(
    values: Sequence[float], qs: Iterable[float], source: str, space: str
) -> dict[str, Any]:
    """标定单个 (组, 信号) 的阈值块。

    同时落盘鲁棒阈值（``p95``，判定用）与经验阈值（``p95_empirical``，对照用），
    这样"污染把经验阈值推出去多少"在 JSON 里一眼可见，前端也能画两条线做对比。
    """
    ordered = sorted(values)
    entry: dict[str, Any] = {
        "n": len(ordered),
        "source": source,
        "method": space if space != "empirical" else "empirical",
    }
    for q in qs:
        label = _q_label(q)
        entry[label] = round(robust_quantile(ordered, q, space), 8)
        entry[f"{label}_empirical"] = round(quantile(ordered, q), 8)
    entry["median"] = round(quantile(ordered, 0.5), 8)
    entry["grid"] = [round(quantile(ordered, q), 8) for q in _GRID_QS]
    return entry


@dataclass(slots=True)
class Thresholds:
    """标定结果。**不可变语义**：需要改数时用 ``scaled()`` 产生新对象（敏感性扫描用）。"""

    groups: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    platform: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    global_: dict[str, dict[str, Any]] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)

    # -- 查询 ---------------------------------------------------------------
    def signal(self, group: str, name: str) -> dict[str, Any]:
        """取某组某信号的阈值块。组不存在时按 platform -> global 回退。"""
        entry = self.groups.get(group, {}).get(name)
        if entry is not None:
            return entry
        plat = group.split("|", 1)[0]
        entry = self.platform.get(plat, {}).get(name)
        if entry is not None:
            return entry
        return self.global_.get(name, {"n": 0, "source": "missing", "grid": []})

    def value(self, group: str, name: str, q_label: str) -> float | None:
        """取具体分位数值，如 ``value("tiktok|micro", "engagement_rate", "p95")``。"""
        entry = self.signal(group, name)
        v = entry.get(q_label)
        return float(v) if isinstance(v, (int, float)) else None

    def source(self, group: str, name: str) -> str:
        return str(self.signal(group, name).get("source", "missing"))

    def percentile_rank(self, group: str, name: str, x: float) -> float:
        """用分位数网格反查 x 的经验百分位（线性插值）。

        这是连续异常分 fraud_score 的基础：把"实际值"变成"在同组里排第几"，
        无量纲、跨组可比、且不需要把 5,000 条原始值塞进 thresholds.json。
        """
        grid = self.signal(group, name).get("grid") or []
        if not grid:
            return 0.5
        if x <= grid[0]:
            return 0.0
        if x >= grid[-1]:
            return 1.0
        step = 1.0 / (len(grid) - 1)
        for i in range(len(grid) - 1):
            lo, hi = grid[i], grid[i + 1]
            if lo <= x <= hi:
                frac = 0.0 if hi == lo else (x - lo) / (hi - lo)
                return min(1.0, max(0.0, (i + frac) * step))
        return 1.0

    # -- 变换 ---------------------------------------------------------------
    def scaled(self, factor: float, signals: Iterable[str] | None = None) -> Thresholds:
        """把指定信号的**判定阈值**统一乘以 factor，返回新对象（敏感性扫描用）。

        注意方向性：对上尾规则（> P95）放大阈值 = 放松；对下尾规则（< P05）放大阈值 = 收紧。
        因此单一 factor 就能同时考察"放松"和"收紧"两个方向的稳定性，这是刻意的设计。
        ``grid`` 不缩放：它只服务于连续分与前端画图，不参与判定。
        """
        target = set(signals) if signals is not None else set(OBSERVABLE_SIGNALS)

        def scale_block(block: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
            out: dict[str, dict[str, Any]] = {}
            for name, entry in block.items():
                new_entry = dict(entry)
                if name in target:
                    for key, val in entry.items():
                        if key.startswith("p") and isinstance(val, (int, float)):
                            new_entry[key] = float(val) * factor
                out[name] = new_entry
            return out

        return Thresholds(
            groups={g: scale_block(b) for g, b in self.groups.items()},
            platform={p: scale_block(b) for p, b in self.platform.items()},
            global_=scale_block(self.global_),
            meta={**self.meta, "scaled_by": factor, "scaled_signals": sorted(target)},
            policy=dict(self.policy),
        )

    # -- 序列化 -------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "meta": self.meta,
            "policy": self.policy,
            "groups": self.groups,
            "platform": self.platform,
            "global": self.global_,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Thresholds:
        return cls(
            groups=dict(payload.get("groups") or {}),
            platform=dict(payload.get("platform") or {}),
            global_=dict(payload.get("global") or {}),
            meta=dict(payload.get("meta") or {}),
            policy=dict(payload.get("policy") or {}),
        )


def calibrate(records: Sequence[Kox], dataset_meta: Mapping[str, Any] | None = None) -> Thresholds:
    """从达人库标定全部阈值。

    纪律：
    - **只读可观测信号**（经 extract_signals 白名单），绝不触碰 ``gt``；
    - 缺失值直接排除，不用 0 填充（否则 P05 会被一堆假 0 拉到地板）；
    - followers 缺失的记录不参与分组标定（它的分层不可知），但仍可被判定。
    """
    by_group: dict[str, dict[str, list[float]]] = {}
    by_platform: dict[str, dict[str, list[float]]] = {}
    overall: dict[str, list[float]] = {name: [] for name in OBSERVABLE_SIGNALS}
    n_used = 0

    for kox in records:
        sig = extract_signals(kox)
        platform = str(kox.get("platform", "unknown"))
        gkey = kox_group_key(kox)
        has_followers = kox.get("followers") is not None
        if has_followers:
            n_used += 1
        for name in OBSERVABLE_SIGNALS:
            val = sig.get(name)
            if val is None:
                continue
            overall.setdefault(name, []).append(val)
            by_platform.setdefault(platform, {}).setdefault(name, []).append(val)
            if has_followers:
                by_group.setdefault(gkey, {}).setdefault(name, []).append(val)

    def space_of(name: str) -> str:
        return ROBUST_SPACE.get(name, "empirical")

    global_block = {
        name: _summarize(vals, SIGNAL_QUANTILES.get(name, (0.5,)), "global", space_of(name))
        for name, vals in overall.items()
        if vals
    }
    platform_block: dict[str, dict[str, dict[str, Any]]] = {}
    for plat, sigs in sorted(by_platform.items()):
        platform_block[plat] = {}
        for name, vals in sigs.items():
            qs = SIGNAL_QUANTILES.get(name, (0.5,))
            if len(vals) >= MIN_GROUP_SAMPLES:
                platform_block[plat][name] = _summarize(
                    vals, qs, f"platform:{plat}", space_of(name)
                )
            else:
                platform_block[plat][name] = {**global_block[name], "source": "global_fallback"}

    group_block: dict[str, dict[str, dict[str, Any]]] = {}
    fallback_log: list[dict[str, Any]] = []
    for gkey, sigs in sorted(by_group.items()):
        plat = gkey.split("|", 1)[0]
        group_block[gkey] = {}
        for name in OBSERVABLE_SIGNALS:
            vals = sigs.get(name, [])
            qs = SIGNAL_QUANTILES.get(name, (0.5,))
            if len(vals) >= MIN_GROUP_SAMPLES:
                group_block[gkey][name] = _summarize(vals, qs, f"group:{gkey}", space_of(name))
            else:
                fb = platform_block.get(plat, {}).get(name) or global_block.get(name)
                if fb is None:
                    continue
                group_block[gkey][name] = {
                    **fb,
                    "source": f"fallback:{fb.get('source', 'global')}",
                    "group_n": len(vals),
                }
                fallback_log.append({"group": gkey, "signal": name, "group_n": len(vals)})

    meta = {
        "thresholds_version": THRESHOLDS_VERSION,
        "calibrated_at_unix": int(time.time()),
        "n_records": len(records),
        "n_records_with_followers": n_used,
        "n_groups": len(group_block),
        "min_group_samples": MIN_GROUP_SAMPLES,
        "grid_step_pct": GRID_STEP_PCT,
        "grid_labels": [f"p{int(round(q * 100)):02d}" for q in _GRID_QS],
        "signal_quantiles": {k: list(v) for k, v in SIGNAL_QUANTILES.items()},
        "robust_space": dict(ROBUST_SPACE),
        "threshold_semantics": (
            "pXX = 鲁棒估计（median + z·1.4826·MAD，等价于干净核心分布的 PXX，判定实际使用）；"
            "pXX_empirical = 经验分位数（含污染，仅作对照）；grid = p0..p100 步长 2% 的经验分位数网格。"
        ),
        "n_fallback_cells": len(fallback_log),
        "fallback_cells": fallback_log,
        "dataset_seed": (dataset_meta or {}).get("seed"),
        "dataset_version": (dataset_meta or {}).get("dataset_version"),
        "note": (
            "阈值全部由达人库经验分布标定，不含硬编码判定数值；"
            "TS 侧引擎必须读取本文件而非自行硬编码，否则双实现无法对齐。"
        ),
    }
    return Thresholds(
        groups=group_block,
        platform=platform_block,
        global_=global_block,
        meta=meta,
        policy=policy_snapshot(),
    )
