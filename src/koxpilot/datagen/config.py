"""合成数据集的生成参数（唯一可调旋钮集中在这里）。

设计意图
--------
1. **可复现**：SEED 固定，且每个达人用 ``SEED`` 派生的独立子种子，
   这样即使将来在生成循环里插入新字段，也不会把后面所有达人的随机流冲掉。
2. **防自证**：注入问题时**只允许改可观测信号的分布**（把互动率压低、把粉丝曲线做出突刺……），
   绝不允许写一个 "is_bought=True" 的标记位让门禁去读。
   门禁能不能检出来，取决于偏移量是否真的落到分位数异常区——这是真检测。
3. **刻意留噪声重叠**：
   - ``*_STRONG_SHARE``：只有这一部分问题号的偏移量足够大（大概率被检出）；
     其余为 weak 偏移，与正常号分布重叠 → 制造 **false negative**。
   - ``NATURAL_OUTLIER_RATE``：一小部分**正常号**天然落在极端区间 → 制造 **false positive**。
   如果 F1 > 0.97，说明这两个旋钮太理想化了，应加大重叠。
"""

from __future__ import annotations

from typing import Final

SEED: Final[int] = 20270919
N_KOX: Final[int] = 5_000
SAMPLE_SIZE: Final[int] = 50
DATASET_VERSION: Final[str] = "1.0.0"
HISTORY_MONTHS: Final[int] = 12

# ---------------------------------------------------------------------------
# 问题注入比例（SPEC 3.3，逐行对应）
# ---------------------------------------------------------------------------
INJECTION_RATES: Final[dict[str, float]] = {
    "bought_followers": 0.06,
    "engagement_pod": 0.04,
    "bot_comments": 0.03,
    "view_inflation": 0.03,
    "tag_mismatch": 0.12,
    "source_conflict": 0.18,
    "brand_safety_high": 0.025,
    "brand_safety_low": 0.07,
    "missing_fields": 0.09,
    "audience_geo_mismatch": 0.05,
}

FRAUD_TYPES: Final[tuple[str, ...]] = (
    "bought_followers",
    "engagement_pod",
    "bot_comments",
    "view_inflation",
)

# ---------------------------------------------------------------------------
# 注入强度：strong = 偏移足够大（可检出概率高）；weak = 与正常分布重叠（大概率漏检）
# ---------------------------------------------------------------------------
STRONG_SHARE: Final[dict[str, float]] = {
    "bought_followers": 0.70,
    "engagement_pod": 0.72,
    "bot_comments": 0.68,
    "view_inflation": 0.70,
}

# 各类水号的**可观测信号偏移倍率区间**（strong / weak）
SHIFTS: Final[dict[str, dict[str, tuple[float, float]]]] = {
    # 买粉：粉丝曲线出现无内容支撑的突刺；互动率被稀释（分母虚高）
    "bought_followers": {
        # strong 的偏移幅度刻意做到"机理连带"：真买粉会同时压低互动率和播放/粉丝比
        # （分母虚高），所以 strong 号大概率同时触发 G1.2 + G1.4 + G1.5 三条信号，
        # 这正是门禁要求"双信号互证才 reject"的现实依据。
        "er_mult_strong": (0.15, 0.42),
        "er_mult_weak": (0.62, 0.95),
        "vfr_mult_strong": (0.22, 0.55),
        "vfr_mult_weak": (0.70, 0.97),
        # 突刺幅度：strong 明显超出自然增长；weak 刻意落在"高波动正常号"的自然范围里，
        # 这样 G1.5 一定会漏掉一部分（真实世界里小额多次买粉本来就很难查）
        "spike_mult_strong": (1.55, 3.40),
        "spike_mult_weak": (1.10, 1.38),
    },
    # 互动农场：互动率被人为顶高，评论重复率同步升高
    "engagement_pod": {
        "er_mult_strong": (2.20, 4.20),
        "er_mult_weak": (1.15, 1.45),
        "dup_add_strong": (0.22, 0.45),
        "dup_add_weak": (0.02, 0.07),
        "clr_mult_strong": (1.6, 2.8),
        "clr_mult_weak": (1.04, 1.22),
    },
    # 机器评论：评论重复率 / 纯 emoji 率显著升高，评论/点赞比也被刷高
    "bot_comments": {
        "dup_abs_strong": (0.45, 0.82),
        "dup_abs_weak": (0.14, 0.26),
        "emoji_abs_strong": (0.45, 0.85),
        "emoji_abs_weak": (0.14, 0.28),
        "lang_mismatch_add": (0.05, 0.25),
        # 机器评论会把评论数刷上去 -> 评论/点赞比同步异常（G1.3），
        # 于是 strong 号有 G1.7 + G1.3 两条互证信号
        "clr_mult_strong": (2.00, 3.40),
        "clr_mult_weak": (1.04, 1.20),
    },
    # 播放注水：播放/粉丝比异常高，互动率被稀释
    "view_inflation": {
        "vfr_mult_strong": (2.9, 5.6),
        "vfr_mult_weak": (1.25, 1.62),
        "er_div_strong": (2.2, 4.0),
        "er_div_weak": (1.10, 1.34),
    },
}

# 正常号里天然的极端值比例（制造 false positive 的空间）。
# 语义：这些号没有任何造假，只是**天生**互动率高/低或播放比极端（例如内容被推荐算法猛推）。
NATURAL_OUTLIER_RATE: Final[float] = 0.07
NATURAL_OUTLIER_SHIFTS: Final[dict[str, tuple[float, float]]] = {
    # 幅度刻意比 SHIFTS 里的 strong 造假**温和**：正常号的天生极端不应该和买粉一样极端，
    # 但仍足以越过同组尾部阈值 -> 这是假阳性的真实来源（也是 precision 上不去的原因）
    "er_high": (1.50, 2.30),
    "er_low": (0.38, 0.66),
    "vfr_high": (1.60, 2.60),
    "vfr_low": (0.40, 0.68),
    "dup_high": (0.26, 0.46),
    "spike": (1.35, 2.60),   # 真实爆红：会同时写入 viral_months（G1.5 因此不该命中）
    # 下面两类是**相关的**双信号异常，专门用来制造"双信号互证也会错"的真实假阳性：
    # algo_push = 被推荐算法猛推的正常号（播放和互动同时飙高，看起来像互动农场+播放注水）
    # dormant   = 粉丝老化/沉睡号（互动和播放同时萎缩，看起来像买粉）
    "algo_push": (1.50, 2.30),
    "dormant": (0.34, 0.60),
}

# 粉丝曲线的自然形态参数。**每个账号有自己的波动率**（GROWTH_VOL_RANGE 里抽），
# 这一点很关键：如果所有号的月增速都来自同一个窄分布，那么"突刺"在统计上会变得
# 毫无悬念地可检（实测 recall 会到 1.00，属于典型的注入过于理想化）。
# 引入异质波动率后，高波动的正常号会天然产生 z>2.5 的月份（假阳性来源），
# 而小额买粉的突刺会被埋在自身的高波动里（假阴性来源）。
GROWTH_BASE_RANGE: Final[tuple[float, float]] = (-0.004, 0.055)
GROWTH_VOL_RANGE: Final[tuple[float, float]] = (0.004, 0.085)
GROWTH_RATE_FLOOR: Final[float] = -0.07

# ---------------------------------------------------------------------------
# 基线分布参数（按平台/分层）
# ---------------------------------------------------------------------------
# 分层采样权重（长尾多、头部少，模拟真实达人库）
BUCKET_WEIGHTS: Final[dict[str, float]] = {
    "nano": 0.34,
    "micro": 0.36,
    "mid": 0.18,
    "macro": 0.09,
    "mega": 0.03,
}

# 互动率基线（lognormal 的中位数），按平台 × 分层。粉丝越多互动率越低（行业常识）。
ER_MEDIAN_BY_PLATFORM: Final[dict[str, float]] = {
    "tiktok": 0.062,
    "youtube": 0.038,
    "instagram": 0.045,
    "x": 0.021,
    "kwai": 0.055,
}
ER_BUCKET_FACTOR: Final[dict[str, float]] = {
    "nano": 1.45,
    "micro": 1.15,
    "mid": 0.92,
    "macro": 0.74,
    "mega": 0.58,
}
ER_SIGMA: Final[float] = 0.42          # lognormal 的 sigma（对数标准差）

# 播放/粉丝比基线
VFR_MEDIAN_BY_PLATFORM: Final[dict[str, float]] = {
    "tiktok": 0.72,
    "youtube": 0.28,
    "instagram": 0.36,
    "x": 0.19,
    "kwai": 0.62,
}
VFR_BUCKET_FACTOR: Final[dict[str, float]] = {
    "nano": 1.30,
    "micro": 1.10,
    "mid": 0.95,
    "macro": 0.82,
    "mega": 0.66,
}
VFR_SIGMA: Final[float] = 0.46

# 评论/点赞占互动的份额（决定 comment_like_ratio）
COMMENT_SHARE_RANGE: Final[tuple[float, float]] = (0.018, 0.055)
SHARE_SHARE_RANGE: Final[tuple[float, float]] = (0.045, 0.135)

# 评论质量基线（正常号）
COMMENT_DUP_BASE: Final[tuple[float, float]] = (0.02, 0.16)
COMMENT_EMOJI_BASE: Final[tuple[float, float]] = (0.03, 0.20)
COMMENT_LANG_MISMATCH_BASE: Final[tuple[float, float]] = (0.0, 0.12)

# CPM 基线（USD），按平台。报价 = CPM × 千次播放 × 议价噪声
CPM_BASE_BY_PLATFORM: Final[dict[str, float]] = {
    "tiktok": 11.0,
    "youtube": 22.0,
    "instagram": 15.0,
    "x": 9.0,
    "kwai": 7.5,
}
CPM_BUCKET_FACTOR: Final[dict[str, float]] = {
    "nano": 0.78,
    "micro": 0.95,
    "mid": 1.12,
    "macro": 1.35,
    "mega": 1.75,
}
CPM_NOISE: Final[tuple[float, float]] = (0.72, 1.42)

# 账号年龄（天）区间，按分层（粉丝越多一般号越老，但允许例外 -> G1.6 新号巨量粉）
AGE_DAYS_RANGE: Final[dict[str, tuple[int, int]]] = {
    "nano": (90, 2200),
    "micro": (150, 2800),
    "mid": (280, 3400),
    "macro": (420, 3900),
    "mega": (600, 4200),
}
# 一小部分号刻意"年轻但粉多"（G1.6 的检测对象；注意：这本身不一定是造假）
YOUNG_ACCOUNT_RATE: Final[float] = 0.05
YOUNG_ACCOUNT_AGE_RANGE: Final[tuple[int, int]] = (70, 240)

VERIFIED_PROB: Final[dict[str, float]] = {
    "nano": 0.05,
    "micro": 0.15,
    "mid": 0.38,
    "macro": 0.66,
    "mega": 0.85,
}

# 自然爆红（合法）比例：会写入 viral_months，G1.5 需要靠它区分"买粉突刺"与"真爆红"
VIRAL_RATE: Final[float] = 0.16
# 争议历史：只在「品牌安全低风险」池内抽取，这样 gt.brand_safety="low" 能覆盖它，
# 与门禁 G3.4 的 review 语义对齐（否则 G3.4 会产生一批口径性假阳性）。
CONTROVERSY_SHARE_OF_LOW: Final[float] = 0.30
MULTI_CATEGORY_RATE: Final[float] = 0.42       # 有第二品类的比例
# 被注入错配的达人中，真实内容偏到「非相邻品类」的比例。
# 剩下的只偏到相邻品类 —— 按业务判据不构成实质错配，是规则假阳性的主要来源。
MISMATCH_SUBSTANTIVE_SHARE: Final[float] = 0.60
PAST_COLLAB_MAX: Final[int] = 3

# ---------------------------------------------------------------------------
# 内容品类「观测过程」的噪声模型
# ---------------------------------------------------------------------------
# 为什么要有这一组参数（第二次防自证修复，务必读懂再改）
# --------------------------------------------------------
# 第一次修复引入了隐藏的 true_categories，让 ground truth 不再等于
# 「declared 与 observed 是否重叠」。但观测仍然是**整块替换**的：
# observed 要么整体等于 true_cats，要么整体等于 declared。
# 由于实质错配的 true_cats 取自 far_pool（与 declared 及其邻居都不相交），
# 结果 Jaccard(declared, observed) 只能取两个值：
#
#   - 0.00 —— 观测到真实品类，规则白送就能命中（实测占正例 36/42）
#   - 1.00 —— 观测被 bio 完全带偏，任何只看 declared+observed 的方法都不可能命中（6/42）
#
# 于是召回变成**阶跃函数**：规则基线、Prompt v1/v2 × 双模型五个 arm 的召回
# 精确相同（0.857 = 36/42），尽管它们彼此的预测差异高达 146/600。
# 召回没有区分度，等于这个横评只剩 precision 一个维度有信息量。
#
# 本次修复把观测改成**逐品类独立采样**的过程，更接近真实的内容分析系统：
#
#   1. 每个真实品类被识别出来的概率 = CAT_DETECT_RATE（会漏项）
#   2. 每个自称品类被 bio 文本"带"进观测的概率 = CAT_BIO_LEAK_RATE（会串入假信号）
#   3. 以 CAT_ADJ_FP_RATE 的概率多识别一个与真实品类相邻的品类（测量误差）
#
# 三者叠加后，observed 会出现「部分真实 + 部分自称」的混合态，
# Jaccard 因此在 0~1 之间形成连续分布，召回重新变成真实的 tradeoff：
# 想抓住 Jaccard=0.5 的样本就必须放宽阈值，而放宽阈值一定会牺牲 precision。
# 这才是一个值得拿去做 Prompt 横评的任务。
CAT_DETECT_RATE: Final[float] = 0.82      # 单个真实品类被内容分析识别到的概率
CAT_BIO_LEAK_RATE: Final[float] = 0.30    # 单个自称品类被主页文本带进观测的概率
CAT_ADJ_FP_RATE: Final[float] = 0.12      # 多识别一个相邻品类的概率
# 实质错配时真实内容含第二个远端品类的比例。调高是为了让「部分识别」
# 能产出更多中间态 Jaccard（只有一个真实品类时，漏项就直接退化成空集）。
MISMATCH_SECOND_CATEGORY_RATE: Final[float] = 0.55

# 缺失字段（SPEC 3.3「关键字段缺失 9%」）。
# 刻意做成 1~3 个字段随机缺：只缺 1 个时 completeness = 0.8 不触发 G0.1，
# 这会真实地制造一批 G0 层的漏检（在 metrics.json 的 weak_spots 里如实承认）。
MISSING_FIELD_CANDIDATES: Final[tuple[str, ...]] = (
    "quoted_price_usd",
    "audience_geo",
    "avg_views",
    "engagement_rate",
    "followers",
)
MISSING_COUNT_WEIGHTS: Final[dict[int, float]] = {1: 0.50, 2: 0.34, 3: 0.16}

# 受众地域错位：主市场占比压到这个区间（正常号主市场占比 0.45~0.85）
GEO_MISMATCH_HOME_SHARE: Final[tuple[float, float]] = (0.08, 0.30)
GEO_HOME_SHARE_NORMAL: Final[tuple[float, float]] = (0.42, 0.86)

# 数值序列化精度（保证两次生成逐字节相同 + JSON 体积可控）
ROUND_RATE: Final[int] = 6
ROUND_USD: Final[int] = 2

# 评测侧要用的"水号真实曝光折损"系数：直接引用注入时的偏移语义，
# 不是凭空拍的数字（见 eval/counterfactual.py 的引用注释）。
EFFECTIVE_VIEW_MULTIPLIER: Final[dict[str, float]] = {
    "bought_followers": 0.40,   # 粉丝虚高，真实触达按 er/vfr 折损中位数取
    "engagement_pod": 0.60,     # 播放大体真实，互动虚假
    "bot_comments": 0.75,       # 主要污染评论区
    "view_inflation": 0.30,     # 播放本身注水，折损最重
}
