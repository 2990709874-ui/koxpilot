"""G1 真实性门禁：水号识别 7 条信号（SPEC 4.G1）。

阈值全部来自 ``(platform, follower_bucket)`` 分组的经验分位数（thresholds.json），
本文件里**没有任何判定用的裸数字**（唯一的常量是 SPEC 明写的 z>2.5，且在 policy.py 里带出处）。

两个输出必须分清（面试高频追问点）
----------------------------------
- ``hard_hits`` / ``reasons``：**离散判定**，用于 pass/review/reject 合成；
- ``fraud_score``：**连续异常分**，用于画 PR 曲线、算 AUC。
  它不是"命中几条"的计数，而是各信号在同组分布中"越界深度"的加权和
  （从 P80 开始线性起坡）。若只用离散命中数当分数，AUC 会退化成三四个台阶，失去意义。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..stats import robust_zscores
from ..types import Reason
from .humanize import group_label, num, pct, ratio, source_label
from .policy import (
    FRAUD_SCORE_RAMP_LOWER,
    FRAUD_SCORE_RAMP_UPPER,
    FRAUD_SCORE_Z_RAMP,
    G1_HARD_RULES,
    G1_WEIGHTS,
    GRADED_PENALTY_GAIN,
    MIN_HISTORY_MONTHS,
    SPIKE_GROWTH_FLOOR_Q,
    SPIKE_ZSCORE_MIN,
    VIRAL_SUPPORT_LAG_MONTHS,
)
from .signals import extract_signals, kox_group_key, monthly_growth_rates
from .thresholds import Thresholds

__all__ = ["G1Outcome", "evaluate_g1", "spike_evidence"]


@dataclass(slots=True)
class G1Outcome:
    reasons: tuple[Reason, ...] = ()
    hard_hits: tuple[str, ...] = ()
    penalty: float = 0.0            # 加权命中扣分（未打置信度折扣）
    fraud_score: float = 0.0        # 连续异常分 [0,1]
    signal_ranks: dict[str, float] = field(default_factory=dict)


def _ramp_upper(rank: float) -> float:
    """上尾斜坡：rank 从 RAMP_UPPER 起线性升到 1.0 得 0→1 分。"""
    span = 1.0 - FRAUD_SCORE_RAMP_UPPER
    return min(1.0, max(0.0, (rank - FRAUD_SCORE_RAMP_UPPER) / span))


def _ramp_lower(rank: float) -> float:
    """下尾斜坡：rank 从 RAMP_LOWER 起线性降到 0.0 得 0→1 分。"""
    return min(1.0, max(0.0, (FRAUD_SCORE_RAMP_LOWER - rank) / FRAUD_SCORE_RAMP_LOWER))


def _depth(
    thresholds: Thresholds,
    group: str,
    signal: str,
    actual: float,
    threshold: float,
    upper: bool,
) -> float:
    """越界深度 ∈[0,1]：0 = 刚好压线，1 = 深入尾部（证据饱和）。

    做法：把"实际值"和"阈值"都映射成同组经验百分位，再按剩余尾部空间归一化。
    用百分位差而不是绝对差，是为了跨信号/跨量级可比（互动率的 0.01 与播放比的 0.01 不是一回事）。
    """
    rank_a = thresholds.percentile_rank(group, signal, actual)
    rank_t = thresholds.percentile_rank(group, signal, threshold)
    if upper:
        room = max(1e-6, 1.0 - rank_t)
        return min(1.0, max(0.0, (rank_a - rank_t) / room))
    room = max(1e-6, rank_t)
    return min(1.0, max(0.0, (rank_t - rank_a) / room))


def spike_evidence(kox: Mapping[str, Any]) -> dict[str, Any] | None:
    """G1.5 的证据计算：找出"最像买粉"的那个月。

    步骤（面试可逐步复算）：
      1. 由 12 个月粉丝历史求 11 个月度环比增速；
      2. 用**鲁棒 z-score**（中位数 + MAD）衡量每个月增速的异常度
         —— 用 MAD 而不是标准差，是因为突刺本身会把标准差撑大，造成掩蔽效应；
      3. 只保留 ``viral_months`` 里没有内容支撑的月份（允许 1 个月滞后）；
      4. 取其中 z 最大的月份作为证据。
    Returns:
        None 表示历史太短或没有无支撑突刺；否则含 month / growth / zscore。
    """
    history = kox.get("follower_history")
    if not isinstance(history, list) or len(history) < MIN_HISTORY_MONTHS:
        return None
    rates = monthly_growth_rates(history)
    if len(rates) < MIN_HISTORY_MONTHS - 1:
        return None
    zs = robust_zscores(rates)
    viral = set(int(m) for m in (kox.get("viral_months") or []) if isinstance(m, (int, float)))
    best: dict[str, Any] | None = None
    max_z = 0.0
    for i, z in enumerate(zs):
        month = i + 1  # rates[i] 是 history[i] -> history[i+1] 的增长，落在第 i+1 个月
        supported = any(
            month - lag in viral for lag in range(VIRAL_SUPPORT_LAG_MONTHS + 1)
        ) or month in viral
        max_z = max(max_z, z if not supported else 0.0)
        if supported or z <= 0:
            continue
        if best is None or z > float(best["zscore"]):
            best = {"month": month, "growth": rates[i], "zscore": z}
    if best is not None:
        best["max_unsupported_z"] = max_z
    return best


def evaluate_g1(kox: Mapping[str, Any], thresholds: Thresholds) -> G1Outcome:
    """纯函数：只吃可观测信号 + 阈值表，不吃 campaign、不吃 ground truth。"""
    sig = extract_signals(kox)
    gkey = kox_group_key(kox)
    label = group_label(gkey)
    reasons: list[Reason] = []
    hard: list[str] = []
    penalty = 0.0
    ranks: dict[str, float] = {}
    score_terms: list[tuple[str, float]] = []

    def hit(
        rule_id: str,
        signal: str,
        actual: float | str | None,
        threshold: float | str | None,
        text: str,
        depth: float = 0.0,
    ) -> None:
        nonlocal penalty
        # 分级惩罚：越界越深，扣分越重（见 policy.GRADED_PENALTY_GAIN 的推导）
        weight = G1_WEIGHTS[rule_id] * (1.0 + GRADED_PENALTY_GAIN * depth)
        severity = "hard" if rule_id in G1_HARD_RULES else "soft"
        reasons.append(
            Reason(
                gate="G1",
                rule_id=rule_id,
                signal=signal,
                actual=actual,
                threshold=threshold,
                weight=round(weight, 4),
                depth=round(depth, 4),
                severity=severity,
                source=f"quantile:{thresholds.source(gkey, signal)}"
                if signal in {"engagement_rate", "comment_like_ratio", "view_follower_ratio",
                              "followers_per_day", "comment_dup_rate", "comment_emoji_only_rate"}
                else "policy:spec_4.G1.5",
                human_text=text,
            )
        )
        penalty += weight
        if severity == "hard":
            hard.append(rule_id)

    # ---- G1.1 / G1.2 互动率双尾 --------------------------------------
    er = sig["engagement_rate"]
    if er is not None:
        p95 = thresholds.value(gkey, "engagement_rate", "p95")
        p05 = thresholds.value(gkey, "engagement_rate", "p05")
        rank = thresholds.percentile_rank(gkey, "engagement_rate", er)
        ranks["engagement_rate"] = rank
        score_terms.append(("G1.1", _ramp_upper(rank)))
        score_terms.append(("G1.2", _ramp_lower(rank)))
        src = source_label(thresholds.source(gkey, "engagement_rate"))
        if p95 is not None and er > p95:
            hit(
                "G1.1",
                "engagement_rate",
                round(er, 6),
                round(p95, 6),
                f"互动率 {pct(er)} 高于 {label} 同行的 P95（{pct(p95)}），"
                f"互动量与粉丝规模不成比例，疑似互动农场/互赞群刷量。{src}。",
                _depth(thresholds, gkey, "engagement_rate", er, p95, upper=True),
            )
        elif p05 is not None and er < p05:
            hit(
                "G1.2",
                "engagement_rate",
                round(er, 6),
                round(p05, 6),
                f"互动率仅 {pct(er)}，低于 {label} 同行的 P05（{pct(p05)}），"
                f"有粉丝没互动，疑似买粉或僵尸粉稀释。{src}。",
                _depth(thresholds, gkey, "engagement_rate", er, p05, upper=False),
            )

    # ---- G1.3 评论/点赞比越界 ----------------------------------------
    clr = sig["comment_like_ratio"]
    if clr is not None:
        lo = thresholds.value(gkey, "comment_like_ratio", "p02")
        hi = thresholds.value(gkey, "comment_like_ratio", "p98")
        rank = thresholds.percentile_rank(gkey, "comment_like_ratio", clr)
        ranks["comment_like_ratio"] = rank
        score_terms.append(("G1.3", max(_ramp_upper(rank), _ramp_lower(rank))))
        if lo is not None and hi is not None and (clr < lo or clr > hi):
            side = "高于上界" if clr > hi else "低于下界"
            hit(
                "G1.3",
                "comment_like_ratio",
                round(clr, 6),
                round(hi if clr > hi else lo, 6),
                f"评论/点赞比 {ratio(clr)} {side}，超出 {label} 同行的正常区间 "
                f"[{ratio(lo)}, {ratio(hi)}]（P02–P98），评论区结构异常。"
                f"{source_label(thresholds.source(gkey, 'comment_like_ratio'))}。",
                _depth(thresholds, gkey, "comment_like_ratio", clr,
                       hi if clr > hi else lo, upper=clr > hi),
            )

    # ---- G1.4 播放/粉丝比越界 ----------------------------------------
    vfr = sig["view_follower_ratio"]
    if vfr is not None:
        lo = thresholds.value(gkey, "view_follower_ratio", "p02")
        hi = thresholds.value(gkey, "view_follower_ratio", "p98")
        rank = thresholds.percentile_rank(gkey, "view_follower_ratio", vfr)
        ranks["view_follower_ratio"] = rank
        score_terms.append(("G1.4", max(_ramp_upper(rank), _ramp_lower(rank))))
        if lo is not None and hi is not None and (vfr < lo or vfr > hi):
            if vfr > hi:
                text = (
                    f"播放/粉丝比 {ratio(vfr)} 高于 {label} 同行的 P98（{ratio(hi)}），"
                    f"播放量远超粉丝盘能解释的范围，疑似播放注水。"
                )
            else:
                text = (
                    f"播放/粉丝比仅 {ratio(vfr)}，低于 {label} 同行的 P02（{ratio(lo)}），"
                    f"粉丝规模撑不起播放量，疑似粉丝虚高（买粉）。"
                )
            hit(
                "G1.4",
                "view_follower_ratio",
                round(vfr, 6),
                round(hi if vfr > hi else lo, 6),
                text + source_label(thresholds.source(gkey, "view_follower_ratio")) + "。",
                _depth(thresholds, gkey, "view_follower_ratio", vfr,
                       hi if vfr > hi else lo, upper=vfr > hi),
            )

    # ---- G1.5 粉丝突刺无内容支撑 --------------------------------------
    # 双重条件（第二条是本实现对 SPEC 的修正，见 policy.SPIKE_GROWTH_FLOOR_Q 注释）：
    #   ① 该月增速的鲁棒 z-score > 2.5（SPEC 原文）
    #   ② 该月增速 > 同组"最好月份增速"分布的 P90（数据标定，压掉短序列 z 的固有假阳性）
    spike = spike_evidence(kox)
    growth_floor = thresholds.value(gkey, "max_monthly_growth", SPIKE_GROWTH_FLOOR_Q)
    if spike is not None:
        z = float(spike["zscore"])
        growth = float(spike["growth"])
        z_lo, z_hi = FRAUD_SCORE_Z_RAMP
        z_term = min(1.0, max(0.0, (z - z_lo) / (z_hi - z_lo)))
        if growth_floor is not None and growth <= growth_floor:
            # 幅度不够就不算突刺，连续分也同步打折（否则 AUC 会被大量温和波动污染）
            z_term *= 0.35
        score_terms.append(("G1.5", z_term))
        ranks["spike_zscore"] = z
        if z > SPIKE_ZSCORE_MIN and (growth_floor is None or growth > growth_floor):
            hit(
                "G1.5",
                "follower_history",
                round(z, 3),
                SPIKE_ZSCORE_MIN,
                f"第 {spike['month']} 个月粉丝环比 {pct(growth, 0)}，"
                f"鲁棒 z-score={num(z)} 超过 {SPIKE_ZSCORE_MIN}，"
                f"且该月增速高于 {label} 同行「最好月份」增速的 P90"
                f"（{pct(growth_floor, 0) if growth_floor is not None else '—'}），"
                f"但该月及前一个月都没有爆款内容记录（viral_months 不含该月），"
                f"典型的买粉突刺形态。阈值来源：SPEC 4.G1.5 的 z>2.5 + 同组增速分位数下限。",
                min(1.0, max(0.0, (z - SPIKE_ZSCORE_MIN) / (FRAUD_SCORE_Z_RAMP[1] * 2.0))),
            )

    # ---- G1.6 新号巨量粉 ---------------------------------------------
    fpd = sig["followers_per_day"]
    if fpd is not None:
        p97 = thresholds.value(gkey, "followers_per_day", "p97")
        rank = thresholds.percentile_rank(gkey, "followers_per_day", fpd)
        ranks["followers_per_day"] = rank
        score_terms.append(("G1.6", _ramp_upper(rank)))
        if p97 is not None and fpd > p97:
            hit(
                "G1.6",
                "followers_per_day",
                round(fpd, 3),
                round(p97, 3),
                f"日均涨粉 {num(fpd, 1)} 人，高于 {label} 同行的 P97（{num(p97, 1)} 人）"
                f"——账号只有 {num(kox.get('account_age_days'), 0)} 天就攒到 "
                f"{num(kox.get('followers'), 0)} 粉，增长曲线不自然（也可能是 MCN 起号，故只计软信号）。"
                f"{source_label(thresholds.source(gkey, 'followers_per_day'))}。",
                _depth(thresholds, gkey, "followers_per_day", fpd, p97, upper=True),
            )

    # ---- G1.7 评论质量异常 -------------------------------------------
    dup = sig["comment_dup_rate"]
    emo = sig["comment_emoji_only_rate"]
    dup_p97 = thresholds.value(gkey, "comment_dup_rate", "p97")
    emo_p97 = thresholds.value(gkey, "comment_emoji_only_rate", "p97")
    term = 0.0
    if dup is not None:
        r = thresholds.percentile_rank(gkey, "comment_dup_rate", dup)
        ranks["comment_dup_rate"] = r
        term = max(term, _ramp_upper(r))
    if emo is not None:
        r = thresholds.percentile_rank(gkey, "comment_emoji_only_rate", emo)
        ranks["comment_emoji_only_rate"] = r
        term = max(term, _ramp_upper(r))
    score_terms.append(("G1.7", term))
    dup_hit = dup is not None and dup_p97 is not None and dup > dup_p97
    emo_hit = emo is not None and emo_p97 is not None and emo > emo_p97
    if dup_hit or emo_hit:
        if dup_hit:
            signal_name, actual, thr = "comment_dup_rate", dup, dup_p97
            detail = f"评论重复率 {pct(dup)} 高于 {label} 同行的 P97（{pct(dup_p97)}）"
        else:
            signal_name, actual, thr = "comment_emoji_only_rate", emo, emo_p97
            detail = f"纯 emoji 评论占比 {pct(emo)} 高于 {label} 同行的 P97（{pct(emo_p97)}）"
        hit(
            "G1.7",
            signal_name,
            round(float(actual), 6),
            round(float(thr), 6),
            f"{detail}，评论区疑似机器批量生成（模板化/无语义）。"
            f"{source_label(thresholds.source(gkey, signal_name))}。",
            _depth(thresholds, gkey, signal_name, float(actual), float(thr), upper=True),
        )

    # ---- 连续异常分：按 G1 权重归一加权 ---------------------------------
    total_w = sum(G1_WEIGHTS[r] for r, _ in score_terms) or 1.0
    fraud_score = sum(G1_WEIGHTS[r] * s for r, s in score_terms) / total_w

    return G1Outcome(
        reasons=tuple(reasons),
        hard_hits=tuple(hard),
        penalty=penalty,
        fraud_score=min(1.0, max(0.0, fraud_score)),
        signal_ranks=ranks,
    )
