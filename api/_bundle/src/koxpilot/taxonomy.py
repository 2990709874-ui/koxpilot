# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/taxonomy.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
"""共享分类学：粉丝分层、品类表、国家-语言表、品类关键词。

设计意图
--------
datagen（造数据）与 gates（判数据）都需要这些常量。放在共享模块而不是各自定义，
是为了**避免两侧口径漂移**——如果 datagen 认为 100k 是 mid、gates 认为是 micro，
所有按分层分组的分位数阈值就全错了，而且这种 bug 极难发现。

注意：本模块只含"公开可知的业务分类学"，不含任何判定阈值，
所以 gates 引用它不违反"阈值必须可追溯"的纪律。
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# 粉丝分层（SPEC 3.1）
# ---------------------------------------------------------------------------
BUCKET_ORDER: Final[tuple[str, ...]] = ("nano", "micro", "mid", "macro", "mega")

# 左闭右开区间，单位：粉丝数
BUCKET_BOUNDS: Final[dict[str, tuple[int, int]]] = {
    "nano": (1_000, 10_000),
    "micro": (10_000, 100_000),
    "mid": (100_000, 500_000),
    "macro": (500_000, 2_000_000),
    "mega": (2_000_000, 10**12),
}

HEAD_BUCKETS: Final[frozenset[str]] = frozenset({"macro", "mega"})
LONGTAIL_BUCKETS: Final[frozenset[str]] = frozenset({"nano", "micro"})


def follower_bucket(followers: float | None) -> str:
    """粉丝数 -> 分层名。None / 缺失时归入 nano（最保守的一档）。"""
    if followers is None:
        return "nano"
    f = float(followers)
    for name in BUCKET_ORDER:
        lo, hi = BUCKET_BOUNDS[name]
        if lo <= f < hi:
            return name
    return "mega" if f >= BUCKET_BOUNDS["mega"][0] else "nano"


def group_key(platform: str, bucket: str) -> str:
    """分位数分组键。前端 TS 侧读 thresholds.json 时必须用同样的拼接方式。"""
    return f"{platform}|{bucket}"


# ---------------------------------------------------------------------------
# 平台 / 国家 / 语言（SPEC 3.1）
# ---------------------------------------------------------------------------
PLATFORMS: Final[tuple[str, ...]] = ("tiktok", "youtube", "instagram", "x", "kwai")

# 权重刻意不均，模拟真实达人库的平台倾斜
PLATFORM_WEIGHTS: Final[dict[str, float]] = {
    "tiktok": 0.38,
    "youtube": 0.21,
    "instagram": 0.25,
    "x": 0.10,
    "kwai": 0.06,
}

# 40 个国家 + 采样权重 + 主语言（出海重点市场权重更高）
COUNTRIES: Final[dict[str, dict[str, object]]] = {
    "US": {"w": 0.150, "lang": "en"},
    "CA": {"w": 0.035, "lang": "en"},
    "GB": {"w": 0.055, "lang": "en"},
    "AU": {"w": 0.025, "lang": "en"},
    "DE": {"w": 0.045, "lang": "de"},
    "FR": {"w": 0.040, "lang": "fr"},
    "ES": {"w": 0.026, "lang": "es"},
    "IT": {"w": 0.024, "lang": "it"},
    "NL": {"w": 0.014, "lang": "nl"},
    "SE": {"w": 0.010, "lang": "sv"},
    "PL": {"w": 0.014, "lang": "pl"},
    "JP": {"w": 0.045, "lang": "ja"},
    "KR": {"w": 0.035, "lang": "ko"},
    "TW": {"w": 0.014, "lang": "zh"},
    "HK": {"w": 0.010, "lang": "zh"},
    "SG": {"w": 0.014, "lang": "en"},
    "MY": {"w": 0.018, "lang": "ms"},
    "ID": {"w": 0.042, "lang": "id"},
    "VN": {"w": 0.032, "lang": "vi"},
    "TH": {"w": 0.030, "lang": "th"},
    "PH": {"w": 0.028, "lang": "en"},
    "IN": {"w": 0.040, "lang": "hi"},
    "PK": {"w": 0.010, "lang": "ur"},
    "BD": {"w": 0.008, "lang": "bn"},
    "BR": {"w": 0.045, "lang": "pt"},
    "MX": {"w": 0.035, "lang": "es"},
    "AR": {"w": 0.014, "lang": "es"},
    "CL": {"w": 0.008, "lang": "es"},
    "CO": {"w": 0.012, "lang": "es"},
    "PE": {"w": 0.007, "lang": "es"},
    "SA": {"w": 0.020, "lang": "ar"},
    "AE": {"w": 0.018, "lang": "ar"},
    "EG": {"w": 0.014, "lang": "ar"},
    "TR": {"w": 0.018, "lang": "tr"},
    "IL": {"w": 0.007, "lang": "he"},
    "ZA": {"w": 0.010, "lang": "en"},
    "NG": {"w": 0.012, "lang": "en"},
    "KE": {"w": 0.006, "lang": "en"},
    "RU": {"w": 0.016, "lang": "ru"},
    "UA": {"w": 0.008, "lang": "uk"},
}

# 目标市场 -> 可接受语言集（G2.4 用）。英语在所有市场都可接受（出海内容通行语）。
MARKET_LANGUAGES: Final[dict[str, frozenset[str]]] = {
    code: frozenset({str(info["lang"]), "en"}) for code, info in COUNTRIES.items()
}

# 地理邻近关系：用于合成受众地域分布（主国 + 邻国溢出）
GEO_NEIGHBORS: Final[dict[str, tuple[str, ...]]] = {
    "US": ("CA", "GB", "MX"),
    "CA": ("US", "GB", "FR"),
    "GB": ("US", "IE", "AU"),
    "AU": ("GB", "US", "NZ"),
    "DE": ("AT", "CH", "NL"),
    "FR": ("BE", "CH", "CA"),
    "ES": ("MX", "AR", "CO"),
    "IT": ("CH", "DE", "FR"),
    "NL": ("BE", "DE", "GB"),
    "SE": ("NO", "DK", "FI"),
    "PL": ("DE", "CZ", "UA"),
    "JP": ("TW", "KR", "US"),
    "KR": ("JP", "TW", "US"),
    "TW": ("HK", "JP", "SG"),
    "HK": ("TW", "SG", "MY"),
    "SG": ("MY", "ID", "HK"),
    "MY": ("SG", "ID", "TH"),
    "ID": ("MY", "SG", "PH"),
    "VN": ("TH", "ID", "PH"),
    "TH": ("VN", "MY", "ID"),
    "PH": ("ID", "SG", "US"),
    "IN": ("PK", "BD", "AE"),
    "PK": ("IN", "AE", "SA"),
    "BD": ("IN", "PK", "MY"),
    "BR": ("PT", "AR", "MX"),
    "MX": ("US", "CO", "AR"),
    "AR": ("CL", "BR", "ES"),
    "CL": ("AR", "PE", "BR"),
    "CO": ("MX", "PE", "EC"),
    "PE": ("CL", "CO", "MX"),
    "SA": ("AE", "EG", "KW"),
    "AE": ("SA", "EG", "IN"),
    "EG": ("SA", "AE", "TR"),
    "TR": ("DE", "AE", "RU"),
    "IL": ("US", "TR", "DE"),
    "ZA": ("NG", "KE", "GB"),
    "NG": ("ZA", "KE", "GB"),
    "KE": ("NG", "ZA", "GB"),
    "RU": ("UA", "TR", "DE"),
    "UA": ("PL", "RU", "DE"),
}

# ---------------------------------------------------------------------------
# 8 大行业品类（SPEC 3.1）
# ---------------------------------------------------------------------------
CATEGORIES: Final[tuple[str, ...]] = (
    "3c_digital",
    "beauty_care",
    "home_appliance",
    "fashion",
    "mother_baby",
    "food_health",
    "gaming_app",
    "auto_travel",
)

CATEGORY_ZH: Final[dict[str, str]] = {
    "3c_digital": "3C数码",
    "beauty_care": "美妆个护",
    "home_appliance": "家居家电",
    "fashion": "服饰鞋包",
    "mother_baby": "母婴",
    "food_health": "食品保健",
    "gaming_app": "游戏应用",
    "auto_travel": "汽车出行",
}

# 品类相邻表：用于合成"合理的多品类账号"，也用于 G2.3 规则版兜底打分
CATEGORY_ADJACENCY: Final[dict[str, tuple[str, ...]]] = {
    "3c_digital": ("home_appliance", "gaming_app"),
    "beauty_care": ("fashion", "food_health"),
    "home_appliance": ("3c_digital", "mother_baby"),
    "fashion": ("beauty_care", "auto_travel"),
    "mother_baby": ("home_appliance", "food_health"),
    "food_health": ("beauty_care", "mother_baby"),
    "gaming_app": ("3c_digital", "auto_travel"),
    "auto_travel": ("gaming_app", "fashion"),
}

# G2.3 规则版（无 LLM）关键词表：达人 bio 关键词 -> 品类。
# 这是"退化版"语义适配，LLM 的 fit_score 从外部注入时会覆盖它。
CATEGORY_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "3c_digital": ("gadget", "tech", "unbox", "phone", "laptop", "audio", "digital"),
    "beauty_care": ("beauty", "makeup", "skincare", "glow", "cosmetic", "kbeauty"),
    "home_appliance": ("home", "kitchen", "appliance", "clean", "living", "cook"),
    "fashion": ("style", "fashion", "outfit", "ootd", "wear", "sneaker"),
    "mother_baby": ("mom", "baby", "kids", "parent", "family", "toddler"),
    "food_health": ("food", "eat", "recipe", "fit", "health", "nutrition", "supp"),
    "gaming_app": ("game", "gaming", "play", "esport", "app", "stream"),
    "auto_travel": ("auto", "car", "drive", "travel", "trip", "ev"),
}

AGE_BUCKETS: Final[tuple[str, ...]] = ("18-24", "25-34", "35-44", "45+")

# ---------------------------------------------------------------------------
# 品牌与竞品（用于 past_collabs 与 G3.3 竞品冲突）
# ---------------------------------------------------------------------------
BRANDS_BY_CATEGORY: Final[dict[str, tuple[str, ...]]] = {
    "3c_digital": ("Anker", "Baseus", "UGREEN", "Soundcore", "EarFun"),
    "beauty_care": ("Florasis", "Judydoll", "Focallure", "SHEGLAM", "Yatsen"),
    "home_appliance": ("Dreame", "Roborock", "Xiaomi Home", "Eureka", "Narwal"),
    "fashion": ("SHEIN", "Cider", "Halara", "Urbanic", "Zaful"),
    "mother_baby": ("Bebebus", "Babycare", "Newell", "Munchkin", "Kidsclan"),
    "food_health": ("Genki Forest", "Swisse", "BeyondNut", "OatOat", "Vitagreen"),
    "gaming_app": ("Mihoyo", "Garena", "TapTap", "Playmate", "NetEase Games"),
    "auto_travel": ("BYD Auto", "NIO", "Trip.com", "Klook", "Autel"),
}

# 竞品组：同组内互为竞品（G3.3）
COMPETITOR_GROUPS: Final[tuple[tuple[str, ...], ...]] = (
    ("Anker", "Baseus", "UGREEN", "EarFun", "Soundcore"),
    ("Dreame", "Roborock", "Narwal", "Eureka", "Xiaomi Home"),
    ("SHEIN", "Cider", "Halara", "Urbanic", "Zaful"),
    ("Florasis", "Judydoll", "Focallure", "SHEGLAM", "Yatsen"),
    ("Mihoyo", "Garena", "NetEase Games", "TapTap", "Playmate"),
)


def competitors_of(brand: str) -> frozenset[str]:
    """返回与 brand 互为竞品的品牌集合（不含自身）。"""
    out: set[str] = set()
    for grp in COMPETITOR_GROUPS:
        if brand in grp:
            out.update(grp)
    out.discard(brand)
    return frozenset(out)


# ---------------------------------------------------------------------------
# 风险内容类型（SPEC 3.3 品牌安全）
# ---------------------------------------------------------------------------
HIGH_RISK_FLAG_TYPES: Final[tuple[str, ...]] = (
    "political_content",
    "adult_content",
    "gambling_promo",
    "medical_claim_strong",
)
LOW_RISK_FLAG_TYPES: Final[tuple[str, ...]] = (
    "medical_claim",
    "profanity",
    "alcohol_mention",
    "shock_humor",
    "unverified_claim",
)
CONTROVERSY_TYPES: Final[tuple[str, ...]] = (
    "public_dispute",
    "refund_scandal",
    "plagiarism_claim",
    "sponsorship_undisclosed",
)

# 受管制品类（G3.5）：对应 campaign_spec.regulated_category
#
# 这张表必须覆盖 brief 解析 Prompt 里允许模型输出的**全部** regulated_category 取值
# （见 llm/prompts.py 的输出契约，元测试在 test_llm_fake.py 里钉死）。
# 少一个取值不会报错，只会让 G3.5 静默拿到空集合、这条规则对该品类**整体失效**——
# 而严重度阈值那边又照样按受管制品类收紧，于是"规则跑了"但"没查该查的东西"。
REGULATED_CATEGORY_FLAGS: Final[dict[str, tuple[str, ...]]] = {
    "medical": ("medical_claim", "medical_claim_strong", "unverified_claim"),
    "finance": ("gambling_promo", "unverified_claim"),
    "kids": ("adult_content", "profanity", "shock_humor", "alcohol_mention"),
    # 酒类：未成年相关（adult_content）与赌博推广同框风险最高，
    # 另外酒类广告在多数市场对"无依据健康声明"也是红线。
    "alcohol": ("adult_content", "gambling_promo", "unverified_claim"),
}
