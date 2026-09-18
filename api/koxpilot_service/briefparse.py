"""A1 BriefAgent：自然语言 brief → ``CampaignSpec``，LLM 可选、规则兜底。

两条路径
--------
``parse_path="llm"``   环境里有模型凭据 -> 真调模型（复用 ``koxpilot.llm`` 的
                       ``build_provider`` 与 ``brief_parse_messages``，和构建期
                       ``make llm`` 用的是同一个 Prompt、同一个 provider 适配层）。
``parse_path="rule"``  没凭据、或调用失败/超时/返回不可解析 -> 走确定性规则解析。

**任何情况下都不抛异常给调用方**：LLM 那条路上出的任何问题都降级成规则路径，
并在 ``llm_runtime.reason`` 里如实写清降级原因。接口不能因为「模型不可用」而报错——
A1 是可选增强，不是必需依赖。

规则解析器的口径
----------------
与前端 ``web/src/engine/briefParse.ts`` **同一套口径**：同样的字段集、同样的词表、
同样的 ``hit / derived / default`` 三态语义。两侧都要能对同一句话给出同样的 spec，
否则「前端降级到浏览器引擎」时用户会看到解析结果突然变了。

  hit      原文直接命中（``matched`` 是原文片段）
  derived  由命中项按业务规则推导，不是原文命中（如从市场派生语言）
  default  没识别到，如实用默认值，绝不假装识别到
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .labels import GENDER_LABEL, KPI_LABEL, REGULATED_LABEL

# ---------------------------------------------------------------------------
# 词表：与 briefParse.ts 逐项对齐
# ---------------------------------------------------------------------------
CATEGORY_WORDS: Dict[str, List[str]] = {
    "3c_digital": ["3c", "数码", "电子产品", "手机", "耳机", "充电", "笔记本", "相机", "智能硬件", "平板"],
    "home_appliance": ["小家电", "家电", "家居", "厨电", "扫地机", "吸尘器", "清洁电器", "厨房", "生活电器"],
    "beauty_care": ["彩妆", "美妆", "护肤", "个护", "化妆", "香水", "口红", "面膜", "美容"],
    "fashion": ["服饰", "服装", "鞋", "箱包", "穿搭", "时尚", "快时尚", "内衣", "配饰"],
    "mother_baby": ["母婴", "婴儿", "宝宝", "奶粉", "纸尿裤", "童装", "儿童用品"],
    "food_health": ["食品", "零食", "保健", "营养", "膳食", "健身补剂", "饮料", "咖啡"],
    "gaming_app": ["手游", "游戏", "电竞", "app", "应用", "小游戏", "休闲游戏", "独立游戏"],
    "auto_travel": ["汽车", "出行", "旅行", "旅游", "电动车", "新能源车", "摩托", "户外出行"],
}

PLATFORM_WORDS: Dict[str, List[str]] = {
    "tiktok": ["tiktok", "tik tok", "tt", "抖音国际版", "国际版抖音", "抖音"],
    "youtube": ["youtube", "yt", "油管", "优管"],
    "instagram": ["instagram", "ins", "ig", "照片墙"],
    "x": ["twitter", " x "],
    "kwai": ["kwai", "快手国际版"],
}

COUNTRY_WORDS: Tuple[Tuple[str, str], ...] = (
    ("美国", "US"), ("加拿大", "CA"), ("英国", "GB"), ("澳大利亚", "AU"), ("澳洲", "AU"),
    ("德国", "DE"), ("法国", "FR"), ("西班牙", "ES"), ("意大利", "IT"), ("荷兰", "NL"),
    ("瑞典", "SE"), ("波兰", "PL"), ("日本", "JP"), ("韩国", "KR"), ("台湾", "TW"),
    ("香港", "HK"), ("新加坡", "SG"), ("马来西亚", "MY"), ("马来", "MY"), ("印尼", "ID"),
    ("印度尼西亚", "ID"), ("越南", "VN"), ("泰国", "TH"), ("菲律宾", "PH"), ("印度", "IN"),
    ("巴西", "BR"), ("墨西哥", "MX"), ("阿根廷", "AR"), ("智利", "CL"), ("哥伦比亚", "CO"),
    ("秘鲁", "PE"), ("沙特", "SA"), ("阿联酋", "AE"), ("迪拜", "AE"), ("埃及", "EG"),
    ("土耳其", "TR"), ("以色列", "IL"), ("南非", "ZA"), ("尼日利亚", "NG"), ("俄罗斯", "RU"),
)

REGION_WORDS: Tuple[Tuple[str, List[str]], ...] = (
    ("北美", ["US", "CA"]),
    ("东南亚", ["ID", "MY", "VN", "TH", "PH", "SG"]),
    ("日韩", ["JP", "KR"]),
    ("拉美", ["BR", "MX", "AR"]),
    ("南美", ["BR", "AR", "CL"]),
    ("中东", ["SA", "AE", "EG"]),
    ("西欧", ["GB", "DE", "FR", "NL"]),
    ("欧洲", ["GB", "DE", "FR", "ES", "IT", "NL"]),
    ("大中华", ["TW", "HK"]),
    ("港台", ["TW", "HK"]),
)

KPI_WORDS: Tuple[Tuple[str, List[str]], ...] = (
    ("conversion", ["转化", "成交", "下单", "出单", "roi", "roas", "销量", "带货"]),
    ("engagement", ["互动", "口碑", "评论", "种草", "讨论", "话题度", "社区"]),
    ("reach", ["曝光", "触达", "声量", "安装", "拉新量", "先要量", "reach", "播放量"]),
)

GENDER_WORDS: Tuple[Tuple[str, List[str]], ...] = (
    ("f", ["女性", "女生", "女孩", "宝妈", "女用户", "妈妈"]),
    ("m", ["男性", "男生", "男孩", "男用户"]),
)

REGULATED_WORDS: Tuple[Tuple[str, List[str]], ...] = (
    ("kids", ["儿童向", "儿童", "青少年", "低龄", "未成年"]),
    ("medical", ["医疗", "医美", "功效宣称", "保健功效", "药", "械字号"]),
    ("finance", ["金融", "理财", "借贷", "加密", "博彩"]),
)

AVOID_WORDS = ("避开", "避免", "回避", "不要", "排除", "不能", "剔除", "不合作", "规避")

AGE_HINT: Tuple[Tuple[str, List[str]], ...] = (
    ("18-24", ["青少年", "学生", "z 世代", "z世代", "年轻人"]),
    ("25-34", ["宝妈", "职场", "白领", "新手父母"]),
)

BUCKET_RANGE: Dict[str, Tuple[int, int]] = {
    "18-24": (18, 24),
    "25-34": (25, 34),
    "35-44": (35, 44),
    "45+": (45, 200),
}

MULTIPLIER: Dict[str, float] = {
    "万": 1e4, "w": 1e4, "千": 1e3, "k": 1e3, "m": 1e6, "百万": 1e6, "亿": 1e8,
}

#: 默认值：与 briefParse.ts 的 PARSE_DEFAULTS 一致
DEFAULT_BUDGET_USD = 50000
DEFAULT_KPI = "balanced"
DEFAULT_MARKETS = ("US",)

#: brief 文本长度上限（契约 §7 的 bad_request）
MAX_BRIEF_CHARS = 2000


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    """全角转半角 + 小写，**保持与原文等长**（下标可直接用于取原文片段）。"""
    out: List[str] = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        elif code == 0x3000:
            ch = " "
        lowered = ch.lower()
        out.append(lowered if len(lowered) == 1 else ch)
    return "".join(out)


def snippet(raw: str, at: int, length: int) -> str:
    """取原文片段做证据展示（带少量上下文），与 TS 侧 snippet() 同口径。"""
    if at < 0:
        return ""
    start = max(0, at - 4)
    end = min(len(raw), at + length + 6)
    body = raw[start:end].strip()
    return "%s%s%s" % ("…" if start > 0 else "", body, "…" if end < len(raw) else "")


def _uniq(items: List[str]) -> List[str]:
    seen: Dict[str, bool] = {}
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen[item] = True
            out.append(item)
    return out


def _money(value: float) -> str:
    return "${:,.0f}".format(value)


# ---------------------------------------------------------------------------
# 各字段解析
# ---------------------------------------------------------------------------
_BUDGET_PATTERNS: Tuple[Tuple[str, Any], ...] = (
    ("dollar_prefix", re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(万|w|k|m)?")),
    ("cn_unit", re.compile(r"([\d,]+(?:\.\d+)?)\s*(万|w|k|m|千|百万)?\s*(?:美元|美金|usd|dollars?)")),
    ("near_word", re.compile(r"预算[^\d]{0,8}([\d,]+(?:\.\d+)?)\s*(万|w|k|m|千|百万)?")),
)

_BUDGET_HOW: Dict[str, str] = {
    "dollar_prefix": "识别到 $ 前缀的金额（可带千分位与万/k/m 量级后缀）",
    "cn_unit": "识别到「数字 + 量级词 + 货币词（美元/美金/USD）」的写法",
    "near_word": "取「预算」二字后 8 个字符内的第一个数字，无货币词时按美元计",
}


def _parse_budget(low: str) -> Optional[Dict[str, Any]]:
    for rule, pattern in _BUDGET_PATTERNS:
        match = pattern.search(low)
        if not match:
            continue
        try:
            number = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if number <= 0:
            continue
        unit = match.group(2)
        usd = number * (MULTIPLIER.get(unit, 1.0) if unit else 1.0)
        # 明显不是金额（例如把「25-40 岁」误吃进来）时放弃这条规则
        if usd < 100:
            continue
        return {"usd": usd, "rule": rule, "at": match.start(), "len": len(match.group(0))}
    return None


def _scan_words(low: str, table: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """每个 key 只取最早命中的那个词，结果按出现位置排序（与 TS 侧一致）。"""
    hits: List[Dict[str, Any]] = []
    for key, words in table.items():
        for word in words:
            at = low.find(word)
            if at >= 0:
                hits.append({"key": key, "word": word.strip(), "at": at})
                break
    hits.sort(key=lambda h: h["at"])
    return hits


def _buckets_for_range(lo: int, hi: int) -> List[str]:
    """年龄区间与四档求交集：有重叠即纳入（宁可多召回一档，也不悄悄丢人群）。"""
    out: List[str] = []
    for bucket in ("18-24", "25-34", "35-44", "45+"):
        low_b, high_b = BUCKET_RANGE[bucket]
        if lo <= high_b and hi >= low_b:
            out.append(bucket)
    return out


# ---------------------------------------------------------------------------
# 规则解析主入口
# ---------------------------------------------------------------------------
class ParseResult(object):
    """A1 的输出：spec 字典 + 逐字段证据 + 友好提示 + 真实耗时。"""

    def __init__(
        self,
        spec: Dict[str, Any],
        fields: List[Dict[str, Any]],
        warnings: List[str],
        parse_path: str,
        elapsed_ms: float,
        llm_note: Optional[str] = None,
    ) -> None:
        self.spec = spec
        self.fields = fields
        self.warnings = warnings
        self.parse_path = parse_path
        self.elapsed_ms = elapsed_ms
        self.llm_note = llm_note


def parse_brief_rule(raw_text: str, campaign_id: str = "CUSTOM") -> ParseResult:
    """纯规则解析。与 ``web/src/engine/briefParse.ts`` 同口径、同三态语义。"""
    started = time.time()
    raw = raw_text or ""
    low = normalize(raw)
    fields: List[Dict[str, Any]] = []
    warnings: List[str] = []

    def push(
        key: str,
        label: str,
        value: Any,
        display: str,
        status: str,
        how: str,
        at: int = -1,
        length: int = 0,
    ) -> None:
        fields.append(
            {
                "key": key,
                "label": label,
                "value": value,
                "display": display,
                "status": status,
                "how": how,
                "matched": "" if status == "default" else snippet(raw, at, length),
            }
        )

    # ---- 预算 ----
    hit = _parse_budget(low)
    if hit is not None:
        budget = int(round(hit["usd"]))
        matched_text = snippet(raw, hit["at"], hit["len"])
        push(
            "budget_usd", "预算", budget, _money(budget), "hit",
            "从「%s」%s" % (matched_text, _BUDGET_HOW[hit["rule"]]),
            hit["at"], hit["len"],
        )
    else:
        budget = DEFAULT_BUDGET_USD
        push(
            "budget_usd", "预算", budget, "%s（未识别，用默认值）" % _money(budget), "default",
            "原文里没找到可识别的金额写法，按默认 %s 计算" % _money(budget),
        )
        warnings.append("没识别到预算金额，按默认 $50,000 计算 —— 试试写「预算 8 万美元」或「$80,000」。")

    # ---- 品类 ----
    cat_hits = _scan_words(low, CATEGORY_WORDS)
    categories = _uniq([h["key"] for h in cat_hits])
    for h in cat_hits:
        push(
            "target_categories", "品类", h["key"], _category_zh(h["key"]), "hit",
            "原文出现「%s」，归到品类「%s」" % (h["word"], _category_zh(h["key"])),
            h["at"], len(h["word"]),
        )
    if len(categories) == 1:
        adjacent = _adjacent_category(categories[0])
        if adjacent:
            categories = categories + [adjacent]
            push(
                "target_categories", "品类（相邻补全）", adjacent, _category_zh(adjacent), "derived",
                "只命中一个品类时，按公开的品类相邻关系补一个相邻品类进召回面"
                "（这是规则推导，不是原文命中）",
            )
    if not categories:
        push(
            "target_categories", "品类", [], "不限品类（未识别）", "default",
            "原文里没出现品类关键词，召回不做品类硬筛选",
        )
        warnings.append(
            "没识别到品类，A2 召回不会做品类硬筛选，候选池会明显偏大 —— 试试写「3C 小家电」「彩妆」「休闲手游」。"
        )

    # ---- 平台 ----
    plat_hits = _scan_words(low, PLATFORM_WORDS)
    platforms = _uniq([h["key"] for h in plat_hits])
    for h in plat_hits:
        push(
            "platforms", "平台", h["key"], _platform_zh(h["key"]), "hit",
            "原文出现「%s」，识别为平台「%s」" % (h["word"], _platform_zh(h["key"])),
            h["at"], len(h["word"]),
        )
    if not platforms:
        push("platforms", "平台", [], "不限平台（未识别）", "default", "原文里没点名平台，按不限平台处理")
        warnings.append("没识别到平台，按「不限平台」处理。")

    # ---- 市场 ----
    country_hits: List[Dict[str, Any]] = []
    seen_codes: Dict[str, bool] = {}
    for word, code in COUNTRY_WORDS:
        at = low.find(word)
        if at >= 0 and code not in seen_codes:
            seen_codes[code] = True
            country_hits.append({"key": code, "word": word, "at": at})
    country_hits.sort(key=lambda h: h["at"])
    region_hits = [
        {"word": word, "codes": codes, "at": low.find(word)}
        for word, codes in REGION_WORDS
        if low.find(word) >= 0
    ]

    if country_hits:
        markets = _uniq([h["key"] for h in country_hits])
        for h in country_hits:
            push(
                "target_markets", "市场", h["key"], "%s（%s）" % (h["word"], h["key"]), "hit",
                "原文出现「%s」，映射到国家码 %s" % (h["word"], h["key"]),
                h["at"], len(h["word"]),
            )
        for r in region_hits:
            push(
                "target_markets", "市场（区域词让位）", None,
                "识别到「%s」，但原文已列明具体国家，按具体国家为准（未展开 %s）"
                % (r["word"], "/".join(r["codes"])),
                "derived",
                "原文同时出现区域词和具体国家时，以具体国家为准，区域词只记录不展开",
                r["at"], len(r["word"]),
            )
    elif region_hits:
        markets = _uniq([code for r in region_hits for code in r["codes"]])
        for r in region_hits:
            push(
                "target_markets", "市场（区域展开）", r["codes"],
                "%s → %s" % (r["word"], "/".join(r["codes"])), "hit",
                "原文出现区域词「%s」，展开为该区域的重点国家" % r["word"],
                r["at"], len(r["word"]),
            )
    else:
        markets = list(DEFAULT_MARKETS)
        push(
            "target_markets", "市场", markets, "US（未识别，用默认值）", "default",
            "原文里没出现国家或区域，按默认美国市场处理",
        )
        warnings.append("没识别到市场，按默认 US 处理 —— 试试写「北美」「东南亚」或直接点国家名。")

    # ---- 语言（从市场派生）----
    languages = _uniq([lang for lang in (_country_lang(m) for m in markets) if lang] + ["en"])
    push(
        "target_languages", "语言", languages, "/".join(languages), "derived",
        "按目标市场的主要语言推导，并保留 en 作为出海通行兜底语",
    )

    # ---- KPI ----
    kpi = DEFAULT_KPI
    kpi_hit: Optional[Dict[str, Any]] = None
    for key, words in KPI_WORDS:
        for word in words:
            at = low.find(word)
            if at >= 0 and (kpi_hit is None or at < kpi_hit["at"]):
                kpi = key
                kpi_hit = {"at": at, "word": word}
    if kpi_hit is not None:
        push(
            "kpi", "KPI", kpi, KPI_LABEL.get(kpi, kpi), "hit",
            "原文出现「%s」，本次投放按「%s」为主要目标" % (kpi_hit["word"], KPI_LABEL.get(kpi, kpi)),
            kpi_hit["at"], len(kpi_hit["word"]),
        )
    else:
        push(
            "kpi", "KPI", kpi, KPI_LABEL.get(kpi, kpi), "default",
            "原文没说侧重转化还是曝光还是互动，按均衡口径处理",
        )

    # ---- 性别 ----
    gender: Optional[str] = None
    for key, words in GENDER_WORDS:
        for word in words:
            at = low.find(word)
            if at >= 0:
                gender = key
                push(
                    "target_gender", "性别", key, GENDER_LABEL.get(key, key), "hit",
                    "原文出现「%s」，受众性别按「%s」约束" % (word, GENDER_LABEL.get(key, key)),
                    at, len(word),
                )
                break
        if gender:
            break
    if gender is None:
        push("target_gender", "性别", None, "不限性别（未识别）", "default", "原文没提性别，不做性别约束")

    # ---- 年龄 ----
    age_buckets: List[str] = []
    age_match = re.search(r"(\d{2})\s*[-~—～到至]\s*(\d{2})\s*(?:岁|years?)?", low)
    if age_match and int(age_match.group(1)) >= 10 and int(age_match.group(2)) <= 99:
        lo = int(age_match.group(1))
        hi = int(age_match.group(2))
        age_buckets = _buckets_for_range(lo, hi)
        push(
            "target_age_buckets", "年龄", age_buckets,
            "%d-%d 岁 → %s" % (lo, hi, "/".join(age_buckets)), "hit",
            "原文写了「%d-%d 岁」，与四个年龄档求交集，有重叠就纳入（不自行收窄人群）" % (lo, hi),
            age_match.start(), len(age_match.group(0)),
        )
    else:
        for bucket, words in AGE_HINT:
            for word in words:
                at = low.find(word)
                if at >= 0:
                    age_buckets = [bucket]
                    push(
                        "target_age_buckets", "年龄", age_buckets, "%s → %s" % (word, bucket), "hit",
                        "原文出现人群词「%s」，对应年龄档 %s" % (word, bucket),
                        at, len(word),
                    )
                    break
            if age_buckets:
                break
    if not age_buckets:
        push("target_age_buckets", "年龄", [], "不限年龄（未识别）", "default", "原文没提年龄，不做年龄约束")

    # ---- 竞品回避 ----
    competitors: List[str] = []
    avoid_context = any(word in low for word in AVOID_WORDS)
    for brand in _all_brands():
        at = low.find(brand.lower())
        if at < 0:
            continue
        # 要求原文里出现过回避语境词，否则不认（避免把自家品牌当成竞品）
        if not avoid_context:
            continue
        competitors.append(brand)
        push(
            "competitor_brands", "竞品回避", brand, brand, "hit",
            "原文点名「%s」且出现了回避语境词，本次投放避开近期与它合作过的达人" % brand,
            at, len(brand),
        )
    if not competitors:
        push(
            "competitor_brands", "竞品回避", [], "无（未识别到需要回避的品牌）", "default",
            "原文没有「避开某品牌」这类表述",
        )

    # ---- 受管制口径 ----
    regulated: Optional[str] = None
    for key, words in REGULATED_WORDS:
        for word in words:
            at = low.find(word)
            if at >= 0:
                regulated = key
                push(
                    "regulated_category", "受管制口径", key, REGULATED_LABEL.get(key, key), "hit",
                    "原文出现「%s」，品牌安全按「%s」的更严标准审"
                    % (word, REGULATED_LABEL.get(key, key)),
                    at, len(word),
                )
                break
        if regulated:
            break
    if regulated is None:
        push(
            "regulated_category", "受管制口径", None, "无（未识别）", "default",
            "原文没有需要按受管制品类加严的表述",
        )

    name_parts = [
        "+".join(_category_zh(c) for c in categories) or "不限品类",
        "/".join(markets),
        KPI_LABEL.get(kpi, kpi),
    ]
    spec = {
        "campaign_id": campaign_id,
        "name": "自定义 · %s" % " · ".join(name_parts),
        "target_categories": categories,
        "target_markets": markets,
        "target_languages": languages,
        "target_age_buckets": age_buckets,
        "target_gender": gender,
        "budget_usd": float(budget),
        "kpi": kpi,
        "platforms": platforms,
        "competitor_brands": competitors,
        "regulated_category": regulated,
        "raw_text": raw,
    }
    return ParseResult(spec, fields, warnings, "rule", (time.time() - started) * 1000.0)


# ---------------------------------------------------------------------------
# taxonomy 取词：这些常量在 koxpilot 包里，不在本模块重复维护
# ---------------------------------------------------------------------------
def _category_zh(key: str) -> str:
    from koxpilot.taxonomy import CATEGORY_ZH

    return CATEGORY_ZH.get(key, key)


def _adjacent_category(key: str) -> Optional[str]:
    from koxpilot.taxonomy import CATEGORY_ADJACENCY

    adjacent = CATEGORY_ADJACENCY.get(key, ())
    return adjacent[0] if adjacent else None


def _country_lang(code: str) -> Optional[str]:
    from koxpilot.taxonomy import COUNTRIES

    info = COUNTRIES.get(code)
    return str(info["lang"]) if info else None


def _all_brands() -> List[str]:
    from koxpilot.taxonomy import COMPETITOR_GROUPS

    return [brand for group in COMPETITOR_GROUPS for brand in group]


_PLATFORM_ZH = {
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "instagram": "Instagram",
    "x": "X",
    "kwai": "Kwai",
}


def _platform_zh(key: str) -> str:
    return _PLATFORM_ZH.get(key, key)


# ---------------------------------------------------------------------------
# LLM 路径
# ---------------------------------------------------------------------------
#: 单次 brief 解析允许的最长等待。超时就降级走规则，绝不让接口跟着挂。
LLM_TIMEOUT_SECONDS = float(os.environ.get("KOXPILOT_LLM_TIMEOUT", "20"))

#: 允许的字段取值域（模型返回越界值时丢弃该字段，不让脏值进入判定链）
_VALID_KPI = ("conversion", "reach", "engagement", "balanced")
_VALID_AGE = ("18-24", "25-34", "35-44", "45+")
_VALID_REGULATED = ("medical", "finance", "kids", "alcohol")


def llm_runtime_status() -> Dict[str, Any]:
    """探测模型凭据是否齐备。**不发任何请求**，用于 health / meta 如实上报。"""
    if os.environ.get("KOXPILOT_DISABLE_LLM", "").strip().lower() in ("1", "true", "yes"):
        return {
            "available": False,
            "provider": None,
            "reason": "已通过环境变量显式关闭模型调用，A1 使用确定性规则解析",
        }
    try:
        from koxpilot.llm import available_providers
    except Exception as exc:  # pragma: no cover - 只在包不完整时发生
        return {
            "available": False,
            "provider": None,
            "reason": "模型适配层不可用（%s），A1 使用确定性规则解析" % exc,
        }
    try:
        providers = available_providers()
    except Exception as exc:
        return {
            "available": False,
            "provider": None,
            "reason": "探测模型凭据时出错（%s），A1 使用确定性规则解析" % exc,
        }
    if not providers:
        return {
            "available": False,
            "provider": None,
            "reason": "未配置模型凭据，A1 使用确定性规则解析",
        }
    return {
        "available": True,
        "provider": providers[0],
        "reason": "已配置模型凭据，A1 优先真调模型解析 brief，失败则自动降级到规则解析",
    }


def _coerce_spec(
    payload: Any, raw_text: str, campaign_id: str
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """把模型返回的 JSON 收敛成合法 spec。

    返回 ``(spec, meta)``；形状不对返回 ``None``（交由调用方降级到规则解析）。
    ``meta`` 记录哪些字段是模型真给了值、哪些是我们兜底填的，以便字段证据里
    如实标 ``hit`` / ``default``，不把兜底值说成模型识别结果。
    """
    if not isinstance(payload, dict):
        return None

    def str_list(key: str, allowed: Optional[Tuple[str, ...]] = None) -> List[str]:
        values = payload.get(key) or []
        if not isinstance(values, list):
            return []
        out: List[str] = []
        for item in values:
            text = str(item).strip()
            if not text:
                continue
            if allowed is not None and text not in allowed:
                continue
            if text not in out:
                out.append(text)
        return out

    from koxpilot.taxonomy import CATEGORIES, PLATFORMS

    budget = payload.get("budget_usd")
    budget_explicit = True
    try:
        budget_usd = float(budget) if budget is not None else float(DEFAULT_BUDGET_USD)
    except (TypeError, ValueError):
        budget_usd = float(DEFAULT_BUDGET_USD)
        budget_explicit = False
    if budget is None:
        budget_explicit = False
    if budget_usd <= 0:
        budget_usd = float(DEFAULT_BUDGET_USD)
        budget_explicit = False

    kpi = str(payload.get("kpi") or DEFAULT_KPI).strip().lower()
    kpi_explicit = kpi in _VALID_KPI and bool(payload.get("kpi"))
    if kpi not in _VALID_KPI:
        kpi = DEFAULT_KPI
    regulated = payload.get("regulated_category")
    regulated_value = str(regulated).strip().lower() if regulated else None
    if regulated_value not in _VALID_REGULATED:
        regulated_value = None

    markets = [m.upper() for m in str_list("target_markets")]
    markets_explicit = bool(markets)
    if not markets:
        markets = list(DEFAULT_MARKETS)
    languages = [lang.lower() for lang in str_list("target_languages")]
    languages_explicit = bool(languages)
    if not languages:
        languages = _uniq([lang for lang in (_country_lang(m) for m in markets) if lang] + ["en"])

    spec = {
        "campaign_id": campaign_id,
        "name": "自定义（模型解析）",
        "target_categories": str_list("target_categories", tuple(CATEGORIES)),
        "target_markets": markets,
        "target_languages": languages,
        "target_age_buckets": str_list("target_age_buckets", _VALID_AGE),
        # 「all」这类取值交给 CampaignSpec.__post_init__ 归一成「不约束」
        "target_gender": payload.get("target_gender"),
        "budget_usd": budget_usd,
        "kpi": kpi,
        "platforms": str_list("platforms", tuple(PLATFORMS)),
        "competitor_brands": str_list("competitor_brands"),
        "regulated_category": regulated_value,
        "raw_text": raw_text,
    }
    meta = {
        "budget_explicit": budget_explicit,
        "kpi_explicit": kpi_explicit,
        "markets_explicit": markets_explicit,
        "languages_explicit": languages_explicit,
    }
    return spec, meta


def _llm_fields(spec: Dict[str, Any], meta: Dict[str, Any], evidence: str) -> List[Dict[str, Any]]:
    """把模型解析出的 spec 翻成契约 §4 的字段证据形状。

    LLM 路径拿不到「命中了原文第几个字」这种下标级证据（模型不回报 span），
    所以 ``matched`` 一律留空、``how`` 里带上模型自己给出的 evidence 说明，
    **不伪造原文片段**。status 统一记 ``hit``：它确实是从原文解析出来的。
    """
    note = evidence.strip() if evidence else "模型未给出逐字段依据"
    rows: List[Dict[str, Any]] = []

    def add(key: str, label: str, value: Any, display: str, status: str) -> None:
        rows.append(
            {
                "key": key,
                "label": label,
                "value": value,
                "display": display,
                "status": status,
                "how": "由模型从 brief 原文解析；模型给出的依据：%s" % note,
                "matched": "",
            }
        )

    budget = int(round(float(spec["budget_usd"])))
    add(
        "budget_usd", "预算", budget,
        _money(budget) if meta["budget_explicit"] else "%s（模型未给出金额，用默认值）" % _money(budget),
        "hit" if meta["budget_explicit"] else "default",
    )
    cats = spec["target_categories"]
    add(
        "target_categories", "品类", cats,
        "/".join(_category_zh(c) for c in cats) if cats else "不限品类",
        "hit" if cats else "default",
    )
    plats = spec["platforms"]
    add(
        "platforms", "平台", plats,
        "/".join(_platform_zh(p) for p in plats) if plats else "不限平台",
        "hit" if plats else "default",
    )
    markets = spec["target_markets"]
    add(
        "target_markets", "市场", markets, "/".join(markets),
        "hit" if meta["markets_explicit"] else "default",
    )
    langs = spec["target_languages"]
    add(
        "target_languages", "语言", langs, "/".join(langs),
        "hit" if meta["languages_explicit"] else "derived",
    )
    add(
        "kpi", "KPI", spec["kpi"], KPI_LABEL.get(spec["kpi"], spec["kpi"]),
        "hit" if meta["kpi_explicit"] else "default",
    )
    gender = spec["target_gender"]
    add(
        "target_gender", "性别", gender,
        GENDER_LABEL.get(str(gender), "不限性别") if gender else "不限性别",
        "hit" if gender else "default",
    )
    ages = spec["target_age_buckets"]
    add("target_age_buckets", "年龄", ages, "/".join(ages) if ages else "不限年龄", "hit" if ages else "default")
    brands = spec["competitor_brands"]
    add("competitor_brands", "竞品回避", brands, "、".join(brands) if brands else "无", "hit" if brands else "default")
    regulated = spec["regulated_category"]
    add(
        "regulated_category", "受管制口径", regulated,
        REGULATED_LABEL.get(str(regulated), "无") if regulated else "无",
        "hit" if regulated else "default",
    )
    return rows


def parse_brief(raw_text: str, campaign_id: str = "CUSTOM") -> ParseResult:
    """A1 主入口：有凭据先试模型，任何失败都静默降级到规则解析并如实标注。"""
    status = llm_runtime_status()
    if not status["available"]:
        result = parse_brief_rule(raw_text, campaign_id)
        result.llm_note = status["reason"]
        return result

    started = time.time()
    provider_name = status["provider"]
    try:
        from koxpilot.llm import build_provider
        from koxpilot.llm.prompts import brief_parse_messages

        provider = build_provider(provider_name)
        # 构建期批量跑允许等 180 秒；在线接口不行——这是一个人在等页面出结果。
        # 收紧超时与重试次数，宁可降级到规则解析，也不让请求悬在那里。
        provider.timeout = int(LLM_TIMEOUT_SECONDS)
        provider.max_retries = 1
        response = provider.chat(brief_parse_messages(raw_text), json_mode=True)
        payload = response.json_payload
        if isinstance(payload, str):
            payload = json.loads(payload)
        coerced = _coerce_spec(payload, raw_text, campaign_id)
        if coerced is None:
            raise ValueError("模型返回的 JSON 不是对象，无法当作投放规格使用")
        spec, meta = coerced
        evidence = str(payload.get("evidence") or "") if isinstance(payload, dict) else ""
        fields = _llm_fields(spec, meta, evidence)
        warnings: List[str] = []
        ambiguities = payload.get("ambiguities") if isinstance(payload, dict) else None
        if isinstance(ambiguities, list):
            warnings = [str(a) for a in ambiguities if str(a).strip()]
        hard = payload.get("hard_requirements") if isinstance(payload, dict) else None
        if isinstance(hard, list):
            for item in hard:
                text = str(item).strip()
                if text:
                    warnings.append("brief 里的硬性要求：%s" % text)
        return ParseResult(
            spec,
            fields,
            warnings,
            "llm",
            (time.time() - started) * 1000.0,
            "已用 %s 真调模型解析 brief（模型 %s，端到端 %d ms）"
            % (provider_name, response.served_model or response.model, response.latency_ms),
        )
    except Exception as exc:
        # 任何失败（网络、鉴权、超时、模型胡说）都降级；接口绝不因此报错。
        result = parse_brief_rule(raw_text, campaign_id)
        result.llm_note = (
            "模型调用失败（%s：%s），已自动降级为确定性规则解析"
            % (provider_name, str(exc)[:200])
        )
        return result
