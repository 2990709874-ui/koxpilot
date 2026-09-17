"""门禁 policy 常量：**每一个数字都标注来源**。

设计纪律
--------
项目对"魔法数字"零容忍。数字只允许有两个出处：
  A) **数据分位数**（在 gates/thresholds.py 里从达人库标定，落盘到 output/thresholds.json）；
  B) **显式 policy 常量**（就是本文件），且必须写明它来自 SPEC 的哪一行、或它的推导逻辑。

因此本文件里没有"拍脑袋"的值：要么是 SPEC 第 4 节写死的语义门槛（如 Jaccard < 0.34），
要么是可推导的权重（见 G1_WEIGHTS 的推导注释）。
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# 分位数标定参数（决定 thresholds.json 里出现哪些 key）
# ---------------------------------------------------------------------------
#: 每个 (platform, follower_bucket) 组至少要有这么多可用样本才用自己的分位数，
#: 否则退化到 platform 级、再退化到全局。60 的来源：
#: P02/P98 这种尾部分位数在 n<50 时基本由单个样本决定，60 是"尾部至少落在两个样本之间"的下限。
MIN_GROUP_SAMPLES: Final[int] = 60

#: 每个信号需要标定的分位点，直接对应 SPEC 4 的 G1 规则表
SIGNAL_QUANTILES: Final[dict[str, tuple[float, ...]]] = {
    "engagement_rate": (0.05, 0.95),            # G1.1 / G1.2
    "comment_like_ratio": (0.02, 0.98),         # G1.3
    "view_follower_ratio": (0.02, 0.98),        # G1.4
    "followers_per_day": (0.97,),               # G1.6
    "max_monthly_growth": (0.90, 0.97),         # G1.5 的绝对增速下限（见 SPIKE_GROWTH_FLOOR_Q）
    "comment_dup_rate": (0.97,),                # G1.7
    "comment_emoji_only_rate": (0.97,),         # G1.7
    "risk_severity_score": (0.90,),             # G3.2
    "avg_cpm_usd": (0.50,),                     # 预算侧：报价缺失时按同组 CPM 中位数估算
}

#: 连续分位数网格（p0, p2, ..., p100）——两个用途：
#: 1) 算连续异常分 fraud_score（AUC 需要连续分，不能只有 0/1）；
#: 2) 前端证据链抽屉画"实际值 vs 同组分布"的迷你图，直接读这份网格。
GRID_STEP_PCT: Final[int] = 2

#: **鲁棒分位数估计**（本项目最重要的一个工程决策，面试必讲）
#:
#: 问题：达人库里 16% 是水号，而水号恰恰长在分布尾部。直接取经验分位数，
#: 阈值会被污染样本自己往外推 —— 结果是"P95 阈值落在水号堆里"，
#: 单条规则的召回率被结构性封顶在 (1-q)/prevalence（实测 G1.2 的召回被压到 0.3 以下）。
#:
#: 解法：在对数空间用 median + 1.4826·MAD 估计尺度（击穿点 50%，16% 污染撼动不了它），
#: 再按正态分位数换算到目标百分位：thr = exp(median(log x) + z_q · 1.4826·MAD(log x))。
#: 语义上等价于"**干净核心分布**的 P95"，而不是"被污染的经验 P95"。
#: 两种阈值都会落盘（``p95`` = 鲁棒值用于判定，``p95_empirical`` = 经验值用于对照）。
ROBUST_SPACE: Final[dict[str, str]] = {
    "engagement_rate": "log",
    "comment_like_ratio": "log",
    "view_follower_ratio": "log",
    "followers_per_day": "log",
    "comment_dup_rate": "linear",
    "comment_emoji_only_rate": "linear",
    "max_monthly_growth": "log1p",
    "risk_severity_score": "empirical",   # 90% 的值是 0，鲁棒估计无意义，直接用经验分位数
    "avg_cpm_usd": "empirical",           # 只取中位数，两种口径一致
}

# ---------------------------------------------------------------------------
# G0 完整性（SPEC 4.G0）
# ---------------------------------------------------------------------------
CRITICAL_FIELDS: Final[tuple[str, ...]] = (
    "followers",
    "avg_views",
    "engagement_rate",
    "audience_geo",
    "quoted_price_usd",
)
#: SPEC 4.G0 原文：completeness < 0.8 → review
COMPLETENESS_REVIEW_MAX: Final[float] = 0.8
#: 置信度打折：G0 命中时后续层的扣分权重乘这个系数（SPEC 4.G0「后续层的置信度打折」）。
#: 0.85 的推导：缺 1/5 关键字段时，剩余信号的证据量约为 4/5，取其平方根 ≈0.89 作为
#: 保守的信息衰减，向下取整到 0.85，宁可少扣分（避免因缺数据而误 reject）。
LOW_CONFIDENCE_DISCOUNT: Final[float] = 0.85

# ---------------------------------------------------------------------------
# G1 真实性（SPEC 4.G1，7 条）
# ---------------------------------------------------------------------------
#: SPEC 4.G1.5 原文：某月环比增速 z-score > 2.5
SPIKE_ZSCORE_MIN: Final[float] = 2.5
#: 爆款内容对突刺的"支撑"允许 1 个月滞后（爆款出现在 m 月，粉丝涨在 m 或 m+1 月）
VIRAL_SUPPORT_LAG_MONTHS: Final[int] = 1
#: 粉丝历史至少这么多个月才做突刺检测（否则 MAD 尺度不可信）
MIN_HISTORY_MONTHS: Final[int] = 6
#: G1.5 的第二个必要条件（本实现相对 SPEC 的**修正**，必须写进文档）：
#: 光看 z-score 不够。11 个点的短序列上，MAD 尺度很小，纯 z>2.5 在正常号里会有
#: 约 8.6% 的假阳性率（实测值，见 docs/04-evaluation.md 的 before/after）。
#: 因此追加要求：该月增速还必须高于**同组达人"最好月份增速"分布的 P90**——
#: 也就是说，这个突刺即便跟同行的最佳月份比也算离谱。阈值仍然来自数据分位数，不是拍的。
SPIKE_GROWTH_FLOOR_Q: Final[str] = "p90"

#: SPEC 4.G1.7 写的绝对门槛。本实现**不直接用它做判定**（改用同组 P97，见 README 偏离说明），
#: 但把它作为 spec_reference 落盘，方便对照与回归。
SPEC_COMMENT_DUP_ABS: Final[float] = 0.35
SPEC_COMMENT_EMOJI_ABS: Final[float] = 0.40

#: G1 各规则权重。推导逻辑（不是拍的）：
#:  - 硬信号（hard）：单条代表"直接指向造假机制"的证据，权重取 ~0.28，
#:    这样任意两条硬信号相加 ≥0.54 → authenticity_score ≤0.46 < FLOOR_REJECT(0.5) → reject，
#:    与 SPEC「命中 ≥2 条硬信号 → reject」完全等价（两条判定路径互为交叉验证）。
#:  - 软信号（soft）：单独出现有大量良性解释（如 x 平台转发文化导致 clr 偏高），
#:    权重取硬信号的一半以下，保证"1 硬 + 1 软"不足以 reject，只 review。
G1_WEIGHTS: Final[dict[str, float]] = {
    "G1.1": 0.30,
    "G1.2": 0.28,
    "G1.3": 0.12,
    "G1.4": 0.26,
    "G1.5": 0.30,
    "G1.6": 0.14,
    "G1.7": 0.26,
}
G1_HARD_RULES: Final[frozenset[str]] = frozenset({"G1.1", "G1.2", "G1.4", "G1.5", "G1.7"})
#: **分级惩罚增益**（对 SPEC「1 - 加权命中」的细化，需在文档中声明）
#: 纯计数式扣分有个产品上说不通的后果：一个评论重复率 82%（远超同组 P97）的号，
#: 只命中 1 条规则就只能判 review；而两个刚好压线的软性异常却能判 reject。
#: 所以扣分改成 ``w_effective = w * (1 + GAIN * depth)``，depth ∈[0,1] 是越界深度
#: （用实际值与阈值在同组经验分布中的百分位差归一化得到，见 g1._depth）。
#: GAIN=1.2 的推导：要让"深入尾部的单条硬信号"刚好能触发 reject，
#: 需要 w_max = w * (1+GAIN) > FLOOR_REJECT=0.5，取最小的硬信号权重 0.26 -> GAIN > 0.923；
#: 留出余量取 1.2，此时压线命中仍只扣 0.26（=review），深度命中扣 0.572（=reject）。
GRADED_PENALTY_GAIN: Final[float] = 1.2

#: SPEC 4「authenticity_score < floor_reject → REJECT」
AUTHENTICITY_FLOOR_REJECT: Final[float] = 0.50
#: SPEC 4「命中 ≥2 条硬信号 → reject」
HARD_HITS_REJECT_MIN: Final[int] = 2

#: 连续异常分的斜坡起点（百分位）。含义：从 P80 开始线性升到 P100 计 0→1 分。
#: 为什么不是从 P95 才开始：AUC 需要在"接近阈值"的区间里也有区分度，
#: 若只在命中后才给分，score 会退化成 0/1，AUC 失去意义。
FRAUD_SCORE_RAMP_UPPER: Final[float] = 0.80
FRAUD_SCORE_RAMP_LOWER: Final[float] = 0.20
#: 突刺 z-score 的连续化区间：z=1.0 起算，z=3.5 满分
FRAUD_SCORE_Z_RAMP: Final[tuple[float, float]] = (1.0, 3.5)

# ---------------------------------------------------------------------------
# G2 一致性（SPEC 4.G2，6 条）
# ---------------------------------------------------------------------------
#: SPEC 4.G2.1 原文：Jaccard < 0.34
DECLARED_OBSERVED_JACCARD_MIN: Final[float] = 0.34
#: SPEC 4.G2.2 原文：三源两两 Jaccard 平均 < 0.5
SOURCE_JACCARD_MIN: Final[float] = 0.5
#: G2.3 语义适配：fit_score < 该值 → review。
#: 0.5 的含义是"规则版打分的中点"——规则版 fit 由品类命中度构造（见 g2.py rule_fit_score），
#: 目标品类完全不命中且相邻品类也不命中时才会低于 0.5，因此 0.5 = "连相邻品类都不沾"。
FIT_SCORE_REVIEW_MAX: Final[float] = 0.5
#: SPEC 4.G2.5 原文：目标市场在 audience_geo 中占比 < 0.35
AUDIENCE_GEO_MIN: Final[float] = 0.35
#: SPEC 4.G2.6 原文：目标年龄段+性别加权重叠度 < 0.4
AUDIENCE_MATCH_MIN: Final[float] = 0.4
#: G2.6 的年龄/性别加权：年龄结构比性别结构更能决定转化，故 0.6 / 0.4。
#: 这是产品判断，写在这里以便被追问时能明确它是"可配置的产品假设"而非物理常数。
AUDIENCE_AGE_WEIGHT: Final[float] = 0.6
AUDIENCE_GENDER_WEIGHT: Final[float] = 0.4

G2_WEIGHTS: Final[dict[str, float]] = {
    "G2.1": 0.30,
    "G2.2": 0.20,
    "G2.3": 0.25,
    "G2.4": 0.20,
    "G2.5": 0.25,
    "G2.6": 0.20,
}

# ---------------------------------------------------------------------------
# G3 品牌安全（SPEC 4.G3，5 条）
# ---------------------------------------------------------------------------
#: severity 分值：high 是硬阻断（不参与累加），low/medium 按 1 : 2.5 折算。
#: 2.5 的推导：一条 medium 约等于 2~3 条 low 的舆情风险，取中值 2.5。
SEVERITY_POINTS: Final[dict[str, float]] = {"low": 1.0, "medium": 2.5, "high": 10.0}
#: 单条 flag 的命中次数上限（hits 超过 3 次后边际风险递减，避免单条 flag 撑爆分数）
SEVERITY_HITS_CAP: Final[int] = 3
#: SPEC 4.G3.3 原文：竞品合作 months_ago < 6
COMPETITOR_RECENT_MONTHS: Final[int] = 6
#: 竞品合作在 3 个月内视为更严重（排他期通常 3 个月），升级为 reject 候选
COMPETITOR_HARD_MONTHS: Final[int] = 3
#: G3.5 受管制品类：把 severity 门槛乘以该系数（更严）。
#: 0.5 = "受管制品类下，一半的风险载荷就该进人工复核"。
REGULATED_SEVERITY_MULTIPLIER: Final[float] = 0.5

G3_WEIGHTS: Final[dict[str, float]] = {
    "G3.1": 1.00,
    "G3.2": 0.30,
    "G3.3": 0.35,
    "G3.4": 0.30,
    "G3.5": 0.45,
}

# ---------------------------------------------------------------------------
# 判定合成
# ---------------------------------------------------------------------------
ALL_GATES: Final[tuple[str, ...]] = ("G0", "G1", "G2", "G3")

#: 敏感性扫描时允许被缩放的阈值信号（G2/G3 的语义门槛也参与，见 eval/sensitivity.py）
SENSITIVITY_SIGNALS: Final[tuple[str, ...]] = (
    "engagement_rate",
    "comment_like_ratio",
    "view_follower_ratio",
    "followers_per_day",
    "comment_dup_rate",
    "comment_emoji_only_rate",
)


def policy_snapshot() -> dict[str, object]:
    """把 policy 常量打包落盘到 thresholds.json，保证 TS 侧读到同一份配置。"""
    return {
        "critical_fields": list(CRITICAL_FIELDS),
        "completeness_review_max": COMPLETENESS_REVIEW_MAX,
        "low_confidence_discount": LOW_CONFIDENCE_DISCOUNT,
        "spike_zscore_min": SPIKE_ZSCORE_MIN,
        "viral_support_lag_months": VIRAL_SUPPORT_LAG_MONTHS,
        "min_history_months": MIN_HISTORY_MONTHS,
        "spec_reference_comment_dup_abs": SPEC_COMMENT_DUP_ABS,
        "spec_reference_comment_emoji_abs": SPEC_COMMENT_EMOJI_ABS,
        "g1_weights": dict(G1_WEIGHTS),
        "graded_penalty_gain": GRADED_PENALTY_GAIN,
        "g1_hard_rules": sorted(G1_HARD_RULES),
        "authenticity_floor_reject": AUTHENTICITY_FLOOR_REJECT,
        "hard_hits_reject_min": HARD_HITS_REJECT_MIN,
        "fraud_score_ramp_upper": FRAUD_SCORE_RAMP_UPPER,
        "fraud_score_ramp_lower": FRAUD_SCORE_RAMP_LOWER,
        "fraud_score_z_ramp": list(FRAUD_SCORE_Z_RAMP),
        "declared_observed_jaccard_min": DECLARED_OBSERVED_JACCARD_MIN,
        "source_jaccard_min": SOURCE_JACCARD_MIN,
        "fit_score_review_max": FIT_SCORE_REVIEW_MAX,
        "audience_geo_min": AUDIENCE_GEO_MIN,
        "audience_match_min": AUDIENCE_MATCH_MIN,
        "audience_age_weight": AUDIENCE_AGE_WEIGHT,
        "audience_gender_weight": AUDIENCE_GENDER_WEIGHT,
        "g2_weights": dict(G2_WEIGHTS),
        "severity_points": dict(SEVERITY_POINTS),
        "severity_hits_cap": SEVERITY_HITS_CAP,
        "competitor_recent_months": COMPETITOR_RECENT_MONTHS,
        "competitor_hard_months": COMPETITOR_HARD_MONTHS,
        "regulated_severity_multiplier": REGULATED_SEVERITY_MULTIPLIER,
        "g3_weights": dict(G3_WEIGHTS),
        "min_group_samples": MIN_GROUP_SAMPLES,
        "grid_step_pct": GRID_STEP_PCT,
    }
