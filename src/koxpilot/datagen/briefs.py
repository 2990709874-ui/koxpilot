"""3 个预置 campaign brief（SPEC 1.2 的 3C 例子 + 美妆 + 游戏应用）。

每个 brief 含两块：
- ``raw_text``：自然语言原文，供 A1 BriefAgent（LLM）解析；
- ``spec``：结构化字段，**无 LLM 时的兜底**，也是 Python 侧评测/预算的实际输入。
两者刻意一一对应，这样"LLM 解析对不对"可以被逐字段 diff（这是 A1 的评测口径）。
"""

from __future__ import annotations

from typing import Any

__all__ = ["BRIEFS", "build_briefs"]

BRIEFS: list[dict[str, Any]] = [
    {
        "brief_id": "BRIEF-001",
        "name": "3C 小家电新品 · 北美 · 看转化",
        "raw_text": (
            "3C 小家电新品，主打北美市场，预算 8 万美元，目标 25-40 岁女性，"
            "TikTok 和 YouTube 为主，看重转化不是曝光。"
        ),
        "spec": {
            "campaign_id": "BRIEF-001",
            "name": "3C 小家电新品 · 北美 · 看转化",
            "target_categories": ["3c_digital", "home_appliance"],
            "target_markets": ["US", "CA"],
            "target_languages": ["en"],
            "target_age_buckets": ["25-34", "35-44"],
            "target_gender": "f",
            "budget_usd": 80000.0,
            "kpi": "conversion",
            "platforms": ["tiktok", "youtube"],
            "competitor_brands": ["Anker", "Dreame"],
            "regulated_category": None,
        },
    },
    {
        "brief_id": "BRIEF-002",
        "name": "国货彩妆 · 东南亚 · 看互动",
        "raw_text": (
            "国货彩妆品牌进东南亚，主打印尼、马来西亚、越南三个市场，预算 4.5 万美元，"
            "目标 18-30 岁女性，Instagram 和 TikTok，希望先把口碑和互动做起来，"
            "尽量多用腰尾部达人，避开最近半年合作过 SHEGLAM 的号。"
        ),
        "spec": {
            "campaign_id": "BRIEF-002",
            "name": "国货彩妆 · 东南亚 · 看互动",
            "target_categories": ["beauty_care", "fashion"],
            "target_markets": ["ID", "MY", "VN"],
            "target_languages": ["id", "ms", "vi", "en"],
            "target_age_buckets": ["18-24", "25-34"],
            "target_gender": "f",
            "budget_usd": 45000.0,
            "kpi": "engagement",
            "platforms": ["instagram", "tiktok"],
            "competitor_brands": ["SHEGLAM", "Focallure"],
            "regulated_category": None,
        },
    },
    {
        "brief_id": "BRIEF-003",
        "name": "休闲手游全球上线 · 日韩+巴西 · 看曝光",
        "raw_text": (
            "一款休闲手游全球上线，重点是日本、韩国和巴西，预算 12 万美元，"
            "主要人群 18-34 岁男性为主，YouTube 和 TikTok，这一轮先要量，先把安装曝光打出来，"
            "游戏是青少年也能玩的，品牌安全要求按儿童向标准审。"
        ),
        "spec": {
            "campaign_id": "BRIEF-003",
            "name": "休闲手游全球上线 · 日韩+巴西 · 看曝光",
            "target_categories": ["gaming_app", "3c_digital"],
            "target_markets": ["JP", "KR", "BR"],
            "target_languages": ["ja", "ko", "pt", "en"],
            "target_age_buckets": ["18-24", "25-34"],
            "target_gender": "m",
            "budget_usd": 120000.0,
            "kpi": "reach",
            "platforms": ["youtube", "tiktok"],
            "competitor_brands": ["Mihoyo", "Garena"],
            "regulated_category": "kids",
        },
    },
]


def build_briefs() -> dict[str, Any]:
    """返回落盘用结构。``spec.raw_text`` 会被回填，方便前端只读 spec 一块。"""
    briefs = []
    for b in BRIEFS:
        spec = dict(b["spec"])
        spec["raw_text"] = b["raw_text"]
        briefs.append({**b, "spec": spec})
    return {
        "meta": {
            "n": len(briefs),
            "note": (
                "raw_text 供 LLM 解析；spec 为无 LLM 时的结构化兜底，"
                "Python 侧评测与预算分配一律以 spec 为输入，保证可复现。"
            ),
        },
        "briefs": briefs,
    }
