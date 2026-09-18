"""预算分配的约束与价值模型常量（SPEC 第 5 节）。

纪律：**本文件是 budget/ 下唯一允许出现数字的地方**，每个数字都必须写清
"出自 SPEC 原文" 还是 "本实现的设计选择 + 理由"。
allocator/value 里不允许出现裸常量（除 0/1 这类结构性数字）。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "COUNTRY_MAX_SHARE",
    "HEAD_MAX_SHARE",
    "INCLUDE_REVIEW_BY_DEFAULT",
    "KPI_EXPONENTS",
    "LONGTAIL_MIN_SHARE",
    "MAX_REPAIR_STEPS",
    "MIN_UTILIZATION_TARGET",
    "PRICE_ESTIMATE_QUANTILE",
    "RATIO_CLAMP",
    "REVIEW_SPEND_DISCOUNT",
    "SINGLE_KOX_MAX_SHARE",
    "constraint_snapshot",
]

# ---------------------------------------------------------------------------
# 硬约束（全部来自 SPEC 5「约束」小节，原文照搬，不允许悄悄放松）
# ---------------------------------------------------------------------------

#: 单达人预算占比上限（SPEC 5）：防止一个号吃掉整盘预算，也是抗单点造假风险的手段
SINGLE_KOX_MAX_SHARE: Final[float] = 0.20

#: 头部（macro + mega）金额占比上限（SPEC 5）：避免全押头部
HEAD_MAX_SHARE: Final[float] = 0.45

#: 长尾（nano + micro）金额占比下限（SPEC 5）
LONGTAIL_MIN_SHARE: Final[float] = 0.25

#: 单一国家金额占比上限（SPEC 5）：地域分散
COUNTRY_MAX_SHARE: Final[float] = 0.60

# ---------------------------------------------------------------------------
# 价值模型（SPEC 5 的 value(k) 公式里 kpi_weight 的具体化）
# ---------------------------------------------------------------------------

#: kpi_weight = er_ratio**a × comment_ratio**b
#:   er_ratio      = 该达人互动率 / 同组（platform|bucket）互动率中位数
#:   comment_ratio = 该达人评论/点赞比 / 同组中位数
#:
#: 为什么用"比值的幂"而不是"加权求和"：
#:   1. 比值天然做了同组归一，跨平台跨量级可比（TikTok 的 6% 互动率和 YouTube 的 2% 不是一回事）；
#:   2. 幂次形式让 value 对各因子是乘性的，与 value(k) 公式其余项（曝光×真实性×适配）量纲一致；
#:   3. 指数是**显式设计选择**，含义可口述：
#:      - reach（要量）：只看曝光，不给互动加成 → 全 0
#:      - engagement（要互动）：互动率一次方
#:      - conversion（要转化）：互动率与"评论占比"各半次方
#:        —— 评论是购买意图的近似代理（问价/问链接都发生在评论区），
#:           点赞则近似只反映滑动手感，所以转化目标下评论权重与互动率同级
#:      - balanced：互动率半次方 + 评论 1/4 次方
KPI_EXPONENTS: Final[dict[str, tuple[float, float]]] = {
    "reach": (0.0, 0.0),
    "engagement": (1.0, 0.0),
    "conversion": (0.5, 0.5),
    "balanced": (0.5, 0.25),
}

#: 归一化比值的截断区间。理由：单个极端号（互动率是同组中位数的 30 倍）
#: 往往本身就是异常/水号形态，不截断会让它靠 kpi_weight 反超优质号。
#: 上界 3.0 ≈ 真实分布里 P99 与 P50 的比值量级（见 thresholds.json 可复算）。
RATIO_CLAMP: Final[tuple[float, float]] = (0.25, 3.0)

#: 报价缺失时的估算口径（SPEC 5：「缺失则按同组 avg_cpm 估算并标注估算值」）。
#: 取同组 CPM 的 P50 而非均值：CPM 右偏，均值会被头部报价拉高，导致低估缺价号的成本。
PRICE_ESTIMATE_QUANTILE: Final[str] = "p50"

#: 期望预算利用率下限，仅用于在 constraints 报告里给出"是否明显花不完"的提示，不是硬约束。
MIN_UTILIZATION_TARGET: Final[float] = 0.90

#: 结构修正阶段的最大迭代步数（= 池子大小的安全上限倍数），防御性上限，正常远不会触达。
MAX_REPAIR_STEPS: Final[int] = 4000

#: 结构修正的最大轮数。多条约束会互相打架（退掉超配国家的内容时可能把长尾也退掉了），
#: 所以修正必须多轮迭代到不动点；8 轮是"经验上 2~3 轮就收敛"之上留的安全余量。
#: 轮数用尽仍未全满足时，如实在 constraints.violations 里报告，绝不假装满足。
MAX_REPAIR_ROUNDS: Final[int] = 8

#: review 档默认不进分配池：review 的产品语义是"待人核"，自动分配不应替人拍板。
INCLUDE_REVIEW_BY_DEFAULT: Final[bool] = False

#: review 档"带折扣下单"的单人金额系数（**只用于预算放宽建议的测算**，
#: 正式分配链路仍受 INCLUDE_REVIEW_BY_DEFAULT 约束，行为不变）。
#:
#: 为什么是 0.5：review 的语义是"待人核"，不是"不能投"。采购侧对待这类号的常规做法
#: 是先给一个小额试投位（试水单），人核通过再追加到正常量级。取正常金额的一半，
#: 对应"先投一半、留一半等复核结论"这个可执行的动作，而不是一个拟合出来的数。
REVIEW_SPEND_DISCOUNT: Final[float] = 0.5

# ---------------------------------------------------------------------------
# 采购模型：按"条"买，而不是按"人"买
# ---------------------------------------------------------------------------
#
# 为什么需要这个（这是实现时被数据打出来的一个必要修正，值得主动交代）：
# 达人营销的采购单位是**内容条数**，一个达人可以在一个 campaign 里投 1~3 条。
# 如果强行"每人只买一条"，在本数据集上会出现结构性荒谬：
# BRIEF-001 经定向筛选 + 门禁后候选池只有约 90 人、总报价约 2 万美元，
# 而预算是 8 万美元 —— 预算利用率会被钉在 15% 左右，
# 那不是分配器的问题，而是采购模型与现实脱节。
#
#: 单个达人在一个 campaign 内最多投几条内容
MAX_POSTS_PER_KOX: Final[int] = 3

#: 同一达人第 n 条内容的**边际有效曝光衰减系数**：第 n 条的等效曝光 = decay^(n-1) × avg_views。
#: 这是一个**显式建模假设**，不是拟合出来的数字：同一达人的受众高度重叠，
#: 第二条内容触达的多是同一批人，净增触达必然衰减（广告学里的 frequency discount）。
#: 0.7 取业界常用的重复触达折扣量级。
#: 由于它是假设而非观测，metrics.json 的 budget 段会同时给出
#: decay ∈ {0.5, 0.7, 0.9} 三档结果，用来证明"选谁"的结论不依赖这个数字。
POST_MARGINAL_DECAY: Final[float] = 0.7

#: 敏感性对照档位
POST_DECAY_SCAN: Final[tuple[float, ...]] = (0.5, 0.7, 0.9)


def effective_posts(posts: int, decay: float = POST_MARGINAL_DECAY) -> float:
    """n 条内容的**等效条数**（几何衰减求和）：1 + d + d² + …

    用等效条数而不是条数去算曝光/CPM，避免把重复触达当成新增触达（那是虚报曝光）。
    """
    total = 0.0
    for i in range(max(0, posts)):
        total += decay**i
    return total


def constraint_snapshot() -> dict[str, float]:
    """落盘用的约束快照（前端与文档直接引用，避免两处写死同一批数字）。"""
    return {
        "single_kox_max_share": SINGLE_KOX_MAX_SHARE,
        "head_max_share": HEAD_MAX_SHARE,
        "longtail_min_share": LONGTAIL_MIN_SHARE,
        "country_max_share": COUNTRY_MAX_SHARE,
        "min_utilization_target": MIN_UTILIZATION_TARGET,
        "max_posts_per_kox": MAX_POSTS_PER_KOX,
        "post_marginal_decay": POST_MARGINAL_DECAY,
    }
