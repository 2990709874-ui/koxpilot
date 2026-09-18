# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/datagen/generator.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
"""合成达人数据集生成器（SPEC 3.1 / 3.2 / 3.3）。

核心设计：**注入的是"可观测信号的统计偏移"，不是标记位**
------------------------------------------------------------
比如"买粉"这件事，本模块做的是：
  1. 真的往 ``follower_history`` 里造一个无内容支撑的单月突刺；
  2. 真的把 ``engagement_rate`` / ``view_follower_ratio`` 乘一个 <1 的系数
     （分母虚高导致互动率被稀释）；
  3. **不写任何 "bought=true" 字段给门禁读**。
门禁引擎只能看到 kox 里除 ``gt`` 以外的字段，能不能检出完全取决于偏移量
是否落到同平台同量级分位数的异常区。因此 eval 出来的 precision/recall 是真实检测能力，
不是"用生成规则评测生成数据"的自证。

同时刻意保留两类重叠（见 config 的 STRONG_SHARE / NATURAL_OUTLIER_RATE）：
  - weak 偏移的问题号 → false negative 的来源；
  - 天然极端的正常号 → false positive 的来源。

可复现性：每个达人用 ``SEED`` 派生的独立子种子 ``Random(SEED * 1_000_003 + i)``，
外加固定的整体洗牌种子分配注入池，因此同一份代码跑两次逐字节相同（见 tests/test_datagen_repro.py）。
"""

from __future__ import annotations

import math
import random
from typing import Any

from ..taxonomy import (
    AGE_BUCKETS,
    BRANDS_BY_CATEGORY,
    BUCKET_BOUNDS,
    BUCKET_ORDER,
    CATEGORIES,
    CATEGORY_ADJACENCY,
    CONTROVERSY_TYPES,
    COUNTRIES,
    GEO_NEIGHBORS,
    HIGH_RISK_FLAG_TYPES,
    LOW_RISK_FLAG_TYPES,
    PLATFORM_WEIGHTS,
    PLATFORMS,
    follower_bucket,
)
from ..types import Kox
from . import config as C

__all__ = ["InjectionPlan", "build_kox", "generate_dataset", "plan_injections"]

# handle 生成词表（纯合成，无真实账号）
_ADJ = (
    "gadget", "glow", "urban", "daily", "smart", "cozy", "hyper", "neon", "swift", "bold",
    "lush", "prime", "vivid", "calm", "mega", "nova", "pixel", "sunny", "iron", "velvet",
    "brisk", "clever", "solar", "amber", "quiet", "rapid", "fresh", "noble", "keen", "witty",
)
_NOUN = (
    "amy", "leo", "kai", "mia", "juno", "rex", "zoe", "finn", "nova", "sage",
    "lab", "diary", "review", "corner", "studio", "notes", "hub", "picks", "life", "room",
    "tester", "hunter", "maker", "guide", "geek", "mom", "chef", "rider", "gamer", "stylist",
)


class InjectionPlan:
    """问题注入分配表：把 5,000 个索引按 SPEC 3.3 的比例切成互不冲突的池。

    - 4 类水号**互斥**（一个号只属于一种造假类型），合计 16%；
    - tag_mismatch / source_conflict / brand_safety / missing / geo_mismatch **相互独立**，
      因为现实里"标签错配的号也可能缺报价"；
    - natural_outlier 只从**非水号**里抽（否则就不叫"正常号天然极端"了）。
    """

    def __init__(self, n: int, seed: int) -> None:
        self.n = n
        self.fraud_type: dict[int, str] = {}
        self.fraud_strong: dict[int, bool] = {}
        self.natural_outlier: dict[int, str] = {}
        self.tag_mismatch: set[int] = set()
        self.source_conflict: set[int] = set()
        self.brand_safety_high: set[int] = set()
        self.brand_safety_low: set[int] = set()
        self.controversy: set[int] = set()
        self.missing: dict[int, int] = {}
        self.geo_mismatch: set[int] = set()
        self.viral: set[int] = set()
        self.young: set[int] = set()
        self._build(seed)

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _pool(rng: random.Random, n: int, rate: float) -> list[int]:
        idx = list(range(n))
        rng.shuffle(idx)
        return idx[: int(round(n * rate))]

    def _build(self, seed: int) -> None:
        n = self.n
        # 每类注入用**独立的**派生 RNG，互不干扰，便于将来单独调比例而不影响其它类
        r_fraud = random.Random(seed + 101)
        r_tag = random.Random(seed + 102)
        r_src = random.Random(seed + 103)
        r_bs = random.Random(seed + 104)
        r_missing = random.Random(seed + 105)
        r_geo = random.Random(seed + 106)
        r_nat = random.Random(seed + 107)
        r_viral = random.Random(seed + 108)
        r_young = random.Random(seed + 109)
        r_strong = random.Random(seed + 110)

        # 4 类水号：一次洗牌后按累计比例切片，保证互斥且比例精确
        idx = list(range(n))
        r_fraud.shuffle(idx)
        cursor = 0
        for ftype in C.FRAUD_TYPES:
            take = int(round(n * C.INJECTION_RATES[ftype]))
            for i in idx[cursor : cursor + take]:
                self.fraud_type[i] = ftype
                self.fraud_strong[i] = r_strong.random() < C.STRONG_SHARE[ftype]
            cursor += take

        self.tag_mismatch = set(self._pool(r_tag, n, C.INJECTION_RATES["tag_mismatch"]))
        self.source_conflict = set(self._pool(r_src, n, C.INJECTION_RATES["source_conflict"]))

        bs_pool = list(range(n))
        r_bs.shuffle(bs_pool)
        n_high = int(round(n * C.INJECTION_RATES["brand_safety_high"]))
        n_low = int(round(n * C.INJECTION_RATES["brand_safety_low"]))
        self.brand_safety_high = set(bs_pool[:n_high])
        low_slice = bs_pool[n_high : n_high + n_low]
        self.brand_safety_low = set(low_slice)
        n_ctrl = int(round(len(low_slice) * C.CONTROVERSY_SHARE_OF_LOW))
        self.controversy = set(low_slice[:n_ctrl])

        for i in self._pool(r_missing, n, C.INJECTION_RATES["missing_fields"]):
            counts = sorted(C.MISSING_COUNT_WEIGHTS)
            weights = [C.MISSING_COUNT_WEIGHTS[c] for c in counts]
            self.missing[i] = r_missing.choices(counts, weights=weights, k=1)[0]

        self.geo_mismatch = set(self._pool(r_geo, n, C.INJECTION_RATES["audience_geo_mismatch"]))
        self.viral = set(self._pool(r_viral, n, C.VIRAL_RATE))
        self.young = set(self._pool(r_young, n, C.YOUNG_ACCOUNT_RATE))

        clean = [i for i in range(n) if i not in self.fraud_type]
        r_nat.shuffle(clean)
        kinds = sorted(C.NATURAL_OUTLIER_SHIFTS)
        for i in clean[: int(round(n * C.NATURAL_OUTLIER_RATE))]:
            self.natural_outlier[i] = r_nat.choice(kinds)

    def as_summary(self) -> dict[str, int]:
        counts = {f: 0 for f in C.FRAUD_TYPES}
        for ftype in self.fraud_type.values():
            counts[ftype] += 1
        counts.update(
            {
                "tag_mismatch": len(self.tag_mismatch),
                "source_conflict": len(self.source_conflict),
                "brand_safety_high": len(self.brand_safety_high),
                "brand_safety_low": len(self.brand_safety_low),
                "controversy": len(self.controversy),
                "missing_fields": len(self.missing),
                "audience_geo_mismatch": len(self.geo_mismatch),
                "natural_outlier": len(self.natural_outlier),
                "viral": len(self.viral),
                "young_account": len(self.young),
            }
        )
        return counts


def plan_injections(n: int = C.N_KOX, seed: int = C.SEED) -> InjectionPlan:
    return InjectionPlan(n, seed)


# ---------------------------------------------------------------------------
# 单个达人生成
# ---------------------------------------------------------------------------


def _u(rng: random.Random, rng_range: tuple[float, float]) -> float:
    return rng.uniform(rng_range[0], rng_range[1])


def _lognormal(rng: random.Random, median: float, sigma: float) -> float:
    return median * math.exp(sigma * rng.gauss(0.0, 1.0))


def _pick_weighted(rng: random.Random, weights: dict[str, float]) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _make_handle(rng: random.Random, used: set[str]) -> str:
    base = f"@{rng.choice(_ADJ)}_{rng.choice(_NOUN)}"
    handle = base
    bump = 0
    while handle in used:
        bump += 1
        handle = f"{base}{bump if bump > 1 else rng.randint(10, 9999)}"
    used.add(handle)
    return handle


def _followers_in_bucket(rng: random.Random, bucket: str) -> int:
    """在分层区间内取粉丝数：区间内按对数均匀（真实达人库长尾形态）。"""
    lo, hi = BUCKET_BOUNDS[bucket]
    hi_eff = min(hi, 12_000_000) if bucket == "mega" else hi
    x = math.exp(rng.uniform(math.log(lo), math.log(hi_eff)))
    return int(min(max(x, lo), hi_eff - 1))


def _build_history(
    rng: random.Random,
    followers: int,
    spike: tuple[int, float] | None,
) -> list[int]:
    """反向构造 12 个月粉丝曲线，保证 ``history[-1] == followers``。

    做法：先给这个账号抽一组月度增速（**账号自身的波动率也是随机的**，
    见 config.GROWTH_VOL_RANGE 的注释），若有突刺则替换其中一个月的增速；
    再用 followers 除以增速累积得到起点，正向累乘生成曲线。
    这样"突刺"是**真的写在数据里的形态**，G1.5 的 z-score 必须自己算出来。

    Args:
        spike: (month_index, multiplier)，month_index ∈ [1, 11]。
    """
    months = C.HISTORY_MONTHS
    base = _u(rng, C.GROWTH_BASE_RANGE)
    vol = _u(rng, C.GROWTH_VOL_RANGE)
    rates = [
        max(C.GROWTH_RATE_FLOOR, base + vol * rng.gauss(0.0, 1.0)) for _ in range(months - 1)
    ]
    if spike is not None:
        m, mult = spike
        rates[m - 1] = mult - 1.0
    total = 1.0
    for r in rates:
        total *= 1.0 + r
    start = max(followers / total, 60.0)
    history = [start]
    for r in rates:
        history.append(history[-1] * (1.0 + r))
    out = [int(round(v)) for v in history]
    out[-1] = int(followers)
    return out


def _audience_geo(rng: random.Random, country: str, mismatch: bool) -> dict[str, float]:
    home_share = (
        _u(rng, C.GEO_MISMATCH_HOME_SHARE) if mismatch else _u(rng, C.GEO_HOME_SHARE_NORMAL)
    )
    neighbors = GEO_NEIGHBORS.get(country, ("US", "GB", "IN"))
    k = rng.randint(2, 3)
    picked = list(neighbors[:k])
    rest = max(0.0, 1.0 - home_share)
    weights = [rng.uniform(0.4, 1.0) for _ in picked]
    wsum = sum(weights) + rng.uniform(0.5, 1.4)  # 剩余归入 other
    geo: dict[str, float] = {country: round(home_share, 4)}
    used = home_share
    for cc, w in zip(picked, weights):
        share = rest * w / wsum
        geo[cc] = round(share, 4)
        used += share
    geo["other"] = round(max(0.0, 1.0 - used), 4)
    return geo


def _audience_age(rng: random.Random, category: str) -> dict[str, float]:
    skew = {
        "mother_baby": (0.10, 0.44, 0.33, 0.13),
        "gaming_app": (0.46, 0.36, 0.13, 0.05),
        "auto_travel": (0.16, 0.38, 0.30, 0.16),
        "food_health": (0.20, 0.36, 0.28, 0.16),
    }.get(category, (0.28, 0.40, 0.21, 0.11))
    raw = [max(0.01, s * rng.uniform(0.72, 1.32)) for s in skew]
    tot = sum(raw)
    vals = [round(v / tot, 4) for v in raw]
    vals[-1] = round(1.0 - sum(vals[:-1]), 4)
    return dict(zip(AGE_BUCKETS, vals))


def _audience_gender(rng: random.Random, category: str) -> dict[str, float]:
    female_base = {
        "beauty_care": 0.82,
        "mother_baby": 0.78,
        "fashion": 0.71,
        "food_health": 0.58,
        "home_appliance": 0.55,
        "3c_digital": 0.34,
        "gaming_app": 0.27,
        "auto_travel": 0.29,
    }[category]
    f = min(0.96, max(0.04, female_base + rng.gauss(0.0, 0.09)))
    return {"f": round(f, 4), "m": round(1.0 - f, 4)}


def _is_substantive_mismatch(declared: list[str], true_cats: list[str]) -> bool:
    """业务判据：declared 与真实内容品类之间，是否构成**实质性**错配。

    判据不是"标签字符串是否相同"，而是"投放方按 declared 选这个人，会不会真的选错"：

    - 有核心品类重叠 → 不算错配（投放方要的那部分内容确实存在）
    - 真实品类全部与 declared **相邻** → 不算错配（相邻品类有天然内容重叠，仍可用）
    - 其余（无重叠且存在非相邻品类）→ 算实质性错配

    这个函数定义的是 ground truth，**只在数据生成时使用，门禁引擎不可调用**。
    """
    d, t = set(declared), set(true_cats)
    if not t or (d & t):
        return False
    adj: set[str] = set()
    for c in d:
        adj |= set(CATEGORY_ADJACENCY[c])
    return not t <= adj


def _categories(
    rng: random.Random, plan: InjectionPlan, i: int
) -> tuple[list[str], list[str], list[str], dict[str, list[str]]]:
    """生成 自称品类 / 真实内容品类（隐藏）/ 观测品类 / 三源标签。

    ⚠️ 防自证设计（这是本数据集最关键的一处设计，务必理解后再改）
    ------------------------------------------------------------------
    早期版本里，注入错配时让 ``observed`` 与 ``declared`` 完全不重叠、未注入时必然重叠，
    结果"declared/observed 的 Jaccard 阈值"这条规则能 **100%** 还原 ground truth
    （实测 P=R=F1=1.000）。那样的评测是自证，没有任何信息量。

    现在的设计把三者分开：

    1. ``declared``   —— 达人自称品类（可观测）
    2. ``true_cats``  —— 达人**真实**在产出的内容品类（隐藏，只进 gt 块）
    3. ``observed``   —— 内容分析系统对 ``true_cats`` 的**带噪观测**（可观测）

    ground truth 由 :func:`_is_substantive_mismatch` 在 ``declared`` vs ``true_cats``
    上按业务判据算出；门禁引擎只能看到 ``declared`` 与 ``observed``。

    噪声被有意设计成双向，分别制造假阴性与假阳性：

    - **偏到相邻品类的错配**（占注入的 40%）：真实内容偏离了自称，但偏到相邻品类，
      按业务判据 **不算** 实质错配 → 规则会命中但 gt 为 False → 假阳性来源
    - **观测被 bio 带偏**（约 12%）：内容分析器受主页文本影响，把观测拉回 declared
      → 真错配但看起来不错配 → 假阴性来源
    - **观测漏项 / 多识别一个相邻品类**：常规测量误差

    这样一来，任何只看 ``declared`` 与 ``observed`` 的规则都不可能完美还原 gt，
    而知道品类邻接关系与业务判据的模型有机会做得更好——这个差距是可测量的，
    也正是"这个环节值不值得花 token"的实证依据。
    """
    primary = rng.choice(CATEGORIES)
    declared = [primary]
    if rng.random() < C.MULTI_CATEGORY_RATE:
        declared.append(rng.choice(CATEGORY_ADJACENCY[primary]))
    declared = sorted(set(declared))

    adj_of_declared: set[str] = set()
    for c in declared:
        adj_of_declared |= set(CATEGORY_ADJACENCY[c])
    adj_of_declared -= set(declared)
    far_pool = [c for c in CATEGORIES if c not in declared and c not in adj_of_declared]

    # ---- 真实内容品类 -------------------------------------------------
    if i in plan.tag_mismatch:
        if rng.random() < C.MISMATCH_SUBSTANTIVE_SHARE and far_pool:
            true_cats = [rng.choice(far_pool)]
            if rng.random() < C.MISMATCH_SECOND_CATEGORY_RATE:
                extra = [c for c in far_pool if c not in true_cats]
                if extra:
                    true_cats.append(rng.choice(extra))
        elif adj_of_declared:
            # 只偏到相邻品类：不构成实质错配，但足以让观测标签偏离 declared
            true_cats = [rng.choice(sorted(adj_of_declared))]
        else:  # pragma: no cover - CATEGORY_ADJACENCY 保证非空
            true_cats = list(declared)
    else:
        true_cats = list(declared)
        roll = rng.random()
        if roll < 0.10 and far_pool:
            # 正常达人偶尔跨界接了一批别的品类，但主品类没变
            true_cats = declared + [rng.choice(far_pool)]
        elif roll < 0.30:
            true_cats = declared + [rng.choice(CATEGORY_ADJACENCY[declared[0]])]
    true_cats = sorted(set(true_cats))

    # ---- 观测品类：对真实品类的逐项带噪观测 ----------------------------
    # 关键：这里是**逐品类独立采样**，不是整块替换。三个独立机制叠加后，
    # observed 会出现「部分真实 + 部分自称」的混合态，使
    # Jaccard(declared, observed) 在 0~1 之间连续分布。
    # 详见 config.py 中 CAT_DETECT_RATE 一节的说明。
    obs: set[str] = set()

    # 机制 1：每个真实品类以 CAT_DETECT_RATE 被识别到（漏项 → 假阴性来源）
    for c in true_cats:
        if rng.random() < C.CAT_DETECT_RATE:
            obs.add(c)

    # 机制 2：每个自称品类以 CAT_BIO_LEAK_RATE 被主页文本带进观测。
    #         这是"观测被 bio 污染"的连续版本 —— 只污染一部分，而不是整块覆盖。
    for c in declared:
        if rng.random() < C.CAT_BIO_LEAK_RATE:
            obs.add(c)

    # 机制 3：常规测量误差，多识别一个与真实主品类相邻的品类
    if true_cats and rng.random() < C.CAT_ADJ_FP_RATE:
        obs.add(rng.choice(CATEGORY_ADJACENCY[true_cats[0]]))

    # 兜底：内容分析系统不会返回空结果，真的什么都没识别到时退回主真实品类
    if not obs:
        obs.add(true_cats[0] if true_cats else declared[0])
    observed = sorted(obs)

    # ---- 三源标签 -----------------------------------------------------
    if i in plan.source_conflict:
        others = [c for c in CATEGORIES if c not in declared]
        src = {
            "src_platform": [declared[0]],
            "src_vendor_a": sorted(set(declared)),
            "src_vendor_b": [rng.choice(others)],
        }
        if rng.random() < 0.45:
            src["src_vendor_a"] = [rng.choice(others)]
    else:
        src = {
            "src_platform": [declared[0]],
            "src_vendor_a": sorted(set(declared)),
            "src_vendor_b": sorted(set(observed)) or [declared[0]],
        }
    return declared, true_cats, observed, src


def build_kox(index: int, plan: InjectionPlan, used_handles: set[str], seed: int = C.SEED) -> Kox:
    """生成第 ``index`` 个达人（0-based）。返回 SPEC 3.2 的 compact dict。"""
    rng = random.Random(seed * 1_000_003 + index)

    platform = _pick_weighted(rng, PLATFORM_WEIGHTS) if PLATFORMS else "tiktok"
    bucket = _pick_weighted(rng, C.BUCKET_WEIGHTS)
    followers = _followers_in_bucket(rng, bucket)

    country = rng.choices(
        list(COUNTRIES), weights=[float(v["w"]) for v in COUNTRIES.values()], k=1
    )[0]
    language = str(COUNTRIES[country]["lang"])
    if rng.random() < 0.12:      # 一部分达人用英语做内容语言（出海通行做法）
        language = "en"

    age_lo, age_hi = C.AGE_DAYS_RANGE[bucket]
    if index in plan.young:
        account_age_days = rng.randint(*C.YOUNG_ACCOUNT_AGE_RANGE)
    else:
        account_age_days = rng.randint(age_lo, age_hi)

    verified = rng.random() < C.VERIFIED_PROB[bucket]
    posts = max(3, int(account_age_days * rng.uniform(0.03, 0.55)))
    following = max(
        3, int(followers ** rng.uniform(0.18, 0.42) * rng.uniform(1.5, 40.0))
    )

    # ---- 基线可观测信号 -----------------------------------------------
    er = _lognormal(
        rng, C.ER_MEDIAN_BY_PLATFORM[platform] * C.ER_BUCKET_FACTOR[bucket], C.ER_SIGMA
    )
    vfr = _lognormal(
        rng, C.VFR_MEDIAN_BY_PLATFORM[platform] * C.VFR_BUCKET_FACTOR[bucket], C.VFR_SIGMA
    )
    comment_share = _u(rng, C.COMMENT_SHARE_RANGE)
    share_share = _u(rng, C.SHARE_SHARE_RANGE)
    dup = _u(rng, C.COMMENT_DUP_BASE)
    emoji_only = _u(rng, C.COMMENT_EMOJI_BASE)
    lang_mismatch = _u(rng, C.COMMENT_LANG_MISMATCH_BASE)

    spike: tuple[int, float] | None = None
    viral_months: list[int] = []
    if index in plan.viral:
        m = rng.randint(2, C.HISTORY_MONTHS - 1)
        spike = (m, _u(rng, C.NATURAL_OUTLIER_SHIFTS["spike"]))
        viral_months = [m]

    # ---- 正常号的天然极端值（false positive 的来源）---------------------
    nat = plan.natural_outlier.get(index)
    if nat == "er_high":
        er *= _u(rng, C.NATURAL_OUTLIER_SHIFTS["er_high"])
    elif nat == "er_low":
        er *= _u(rng, C.NATURAL_OUTLIER_SHIFTS["er_low"])
    elif nat == "vfr_high":
        vfr *= _u(rng, C.NATURAL_OUTLIER_SHIFTS["vfr_high"])
    elif nat == "vfr_low":
        vfr *= _u(rng, C.NATURAL_OUTLIER_SHIFTS["vfr_low"])
    elif nat == "dup_high":
        dup = _u(rng, C.NATURAL_OUTLIER_SHIFTS["dup_high"])
    elif nat == "algo_push":
        # 双信号相关异常（正常号）：算法猛推 -> 播放与互动同时超常
        lo, hi = C.NATURAL_OUTLIER_SHIFTS["algo_push"]
        er *= _u(rng, (lo, hi))
        vfr *= _u(rng, (lo * 1.15, hi * 1.25))
    elif nat == "dormant":
        lo, hi = C.NATURAL_OUTLIER_SHIFTS["dormant"]
        er *= _u(rng, (lo, hi))
        vfr *= _u(rng, (lo, hi))
    elif nat == "spike" and spike is None:
        m = rng.randint(2, C.HISTORY_MONTHS - 1)
        spike = (m, _u(rng, C.NATURAL_OUTLIER_SHIFTS["spike"]))
        viral_months = [m]      # 真爆红：有内容支撑，G1.5 不该判它

    # ---- 水号注入：只改可观测信号的统计偏移 -----------------------------
    ftype = plan.fraud_type.get(index)
    strong = plan.fraud_strong.get(index, False)
    if ftype == "bought_followers":
        s = C.SHIFTS["bought_followers"]
        er *= _u(rng, s["er_mult_strong" if strong else "er_mult_weak"])
        vfr *= _u(rng, s["vfr_mult_strong" if strong else "vfr_mult_weak"])
        m = rng.randint(2, C.HISTORY_MONTHS - 1)
        while m in viral_months or (m - 1) in viral_months:
            m = rng.randint(2, C.HISTORY_MONTHS - 1)
        spike = (m, _u(rng, s["spike_mult_strong" if strong else "spike_mult_weak"]))
        # 关键：买粉的突刺**不**写入 viral_months（没有爆款内容支撑）
    elif ftype == "engagement_pod":
        s = C.SHIFTS["engagement_pod"]
        er *= _u(rng, s["er_mult_strong" if strong else "er_mult_weak"])
        dup = min(0.95, dup + _u(rng, s["dup_add_strong" if strong else "dup_add_weak"]))
        comment_share *= _u(rng, s["clr_mult_strong" if strong else "clr_mult_weak"])
    elif ftype == "bot_comments":
        s = C.SHIFTS["bot_comments"]
        dup = _u(rng, s["dup_abs_strong" if strong else "dup_abs_weak"])
        emoji_only = _u(rng, s["emoji_abs_strong" if strong else "emoji_abs_weak"])
        lang_mismatch = min(0.9, lang_mismatch + _u(rng, s["lang_mismatch_add"]))
        comment_share *= _u(rng, s["clr_mult_strong" if strong else "clr_mult_weak"])
    elif ftype == "view_inflation":
        s = C.SHIFTS["view_inflation"]
        vfr *= _u(rng, s["vfr_mult_strong" if strong else "vfr_mult_weak"])
        er /= _u(rng, s["er_div_strong" if strong else "er_div_weak"])

    er = min(0.62, max(0.0006, er))
    vfr = min(14.0, max(0.008, vfr))
    comment_share = min(0.36, max(0.004, comment_share))

    avg_views = max(40, int(round(followers * vfr)))
    total_eng = er * avg_views
    avg_comments = int(round(total_eng * comment_share))
    avg_shares = int(round(total_eng * share_share))
    avg_likes = max(1, int(round(total_eng - avg_comments - avg_shares)))
    engagement_rate = (avg_likes + avg_comments + avg_shares) / avg_views
    view_follower_ratio = avg_views / followers
    comment_like_ratio = avg_comments / avg_likes

    history = _build_history(rng, followers, spike)

    declared, true_categories, observed, source_tags = _categories(rng, plan, index)
    primary = declared[0]

    audience_geo = _audience_geo(rng, country, index in plan.geo_mismatch)
    audience_age = _audience_age(rng, primary)
    audience_gender = _audience_gender(rng, primary)

    avg_cpm = round(
        C.CPM_BASE_BY_PLATFORM[platform] * C.CPM_BUCKET_FACTOR[bucket] * _u(rng, C.CPM_NOISE), 2
    )
    quoted_price = round(avg_cpm * avg_views / 1000.0 * rng.uniform(0.85, 1.25), C.ROUND_USD)

    past_collabs: list[dict[str, Any]] = []
    for _ in range(rng.randint(0, C.PAST_COLLAB_MAX)):
        cat = rng.choice(declared + observed)
        past_collabs.append(
            {
                "brand": rng.choice(BRANDS_BY_CATEGORY[cat]),
                "category": cat,
                "months_ago": rng.randint(1, 24),
            }
        )

    content_flags: list[dict[str, Any]] = []
    if index in plan.brand_safety_high:
        content_flags.append(
            {
                "type": rng.choice(HIGH_RISK_FLAG_TYPES),
                "severity": "high",
                "hits": rng.randint(1, 4),
            }
        )
    if index in plan.brand_safety_low:
        for _ in range(rng.randint(1, 2)):
            content_flags.append(
                {
                    "type": rng.choice(LOW_RISK_FLAG_TYPES),
                    "severity": rng.choice(["low", "medium"]),
                    "hits": rng.randint(1, 3),
                }
            )
    controversy = None
    if index in plan.controversy:
        controversy = {
            "type": rng.choice(CONTROVERSY_TYPES),
            "months_ago": rng.randint(1, 20),
            "severity": rng.choice(["low", "medium"]),
        }

    kox: Kox = {
        "kox_id": f"KOX-{index + 1:06d}",
        "handle": _make_handle(rng, used_handles),
        "platform": platform,
        "country": country,
        "language": language,
        "verified": verified,
        "account_age_days": account_age_days,
        "followers": followers,
        "following": following,
        "posts": posts,
        "declared_categories": declared,
        "observed_categories": observed,
        "source_tags": source_tags,
        "avg_views": avg_views,
        "avg_likes": avg_likes,
        "avg_comments": avg_comments,
        "avg_shares": avg_shares,
        "engagement_rate": round(engagement_rate, C.ROUND_RATE),
        "view_follower_ratio": round(view_follower_ratio, C.ROUND_RATE),
        "comment_like_ratio": round(comment_like_ratio, C.ROUND_RATE),
        "follower_history": history,
        "viral_months": viral_months,
        "audience_geo": audience_geo,
        "audience_age": audience_age,
        "audience_gender": audience_gender,
        "quoted_price_usd": quoted_price,
        "avg_cpm_usd": avg_cpm,
        "past_collabs": past_collabs,
        "content_flags": content_flags,
        "controversy": controversy,
        "comment_dup_rate": round(min(0.95, max(0.0, dup)), C.ROUND_RATE),
        "comment_emoji_only_rate": round(min(0.95, max(0.0, emoji_only)), C.ROUND_RATE),
        "comment_lang_mismatch_rate": round(min(0.95, max(0.0, lang_mismatch)), C.ROUND_RATE),
        "_missing": [],
    }

    # ---- 关键字段缺失（在最后执行：前面的派生计算需要真值）----------------
    n_missing = plan.missing.get(index, 0)
    if n_missing:
        fields = list(C.MISSING_FIELD_CANDIDATES)
        rng.shuffle(fields)
        missing = sorted(fields[:n_missing])
        for f in missing:
            kox[f] = None
        kox["_missing"] = missing

    # ---- ground truth（SPEC 3.3 末尾的确定性合成规则）--------------------
    brand_safety = (
        "high"
        if index in plan.brand_safety_high
        else ("low" if index in plan.brand_safety_low else "none")
    )
    is_fraud = ftype is not None
    # gt 的错配标签由业务判据在 declared vs 真实内容品类上算出，
    # 而不是直接用"是否被注入"——注入里有 40% 只偏到相邻品类，按判据不算实质错配。
    tag_mismatch = _is_substantive_mismatch(declared, true_categories)
    geo_mismatch = index in plan.geo_mismatch
    has_missing = bool(kox["_missing"])
    if is_fraud or brand_safety == "high":
        verdict = "reject"
    elif tag_mismatch or brand_safety == "low" or has_missing or geo_mismatch:
        verdict = "review"
    else:
        verdict = "pass"
    kox["gt"] = {
        "is_fraud": is_fraud,
        "fraud_type": ftype,
        "tag_mismatch": tag_mismatch,
        "brand_safety": brand_safety,
        "verdict": verdict,
        # 隐藏的真实内容品类：只用于生成与评测，门禁引擎读不到 gt 块
        "true_categories": true_categories,
        "mismatch_injected": index in plan.tag_mismatch,
    }
    return kox


def generate_dataset(n: int = C.N_KOX, seed: int = C.SEED) -> dict[str, Any]:
    """生成完整数据集。返回 ``{"meta": {...}, "kox": [...]}``。

    meta 块是**唯一**在 SPEC 3.2 之外增加的结构（记录种子/版本/注入计数），
    目的是让任何人拿到 JSON 就能验证它出自哪个种子；每条达人记录本身严格遵循 SPEC 3.2。
    """
    plan = plan_injections(n, seed)
    used: set[str] = set()
    records = [build_kox(i, plan, used, seed) for i in range(n)]
    counts = plan.as_summary()
    return {
        "meta": {
            "dataset_version": C.DATASET_VERSION,
            "seed": seed,
            "n": n,
            "generator": "koxpilot.datagen.generator",
            "schema": "SPEC-3.2-compact",
            "note": (
                "全部为程序合成数据，不含任何真实平台数据。问题注入只改可观测信号的统计偏移，"
                "门禁引擎不可读取 gt 字段。"
            ),
            "injection_target_rates": dict(C.INJECTION_RATES),
            "injection_actual_counts": counts,
            "injection_actual_rates": {k: round(v / n, 6) for k, v in counts.items()},
            "strong_share": dict(C.STRONG_SHARE),
            "natural_outlier_rate": C.NATURAL_OUTLIER_RATE,
            "verdict_counts": _verdict_counts(records),
        },
        "kox": records,
    }


def _verdict_counts(records: list[Kox]) -> dict[str, int]:
    out = {"pass": 0, "review": 0, "reject": 0}
    for k in records:
        out[k["gt"]["verdict"]] += 1
    return out


def bucket_of(kox: Kox) -> str:
    """便捷函数：取达人的粉丝分层（followers 缺失时返回 nano）。"""
    return follower_bucket(kox.get("followers"))


BUCKETS = BUCKET_ORDER
