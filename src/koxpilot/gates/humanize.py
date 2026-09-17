"""human_text 生成工具：把数值判定翻译成投放同学能读懂的一句中文。

设计意图
--------
证据链的价值在于"客户问为什么选他/为什么不选他，能当场答"。
所以每条 reason 的 human_text 都必须同时含三样东西：**实际值、同组阈值、业务含义**。
本模块只做格式化，不做判定，保证文案风格统一（前端直接渲染，不再二次加工）。
"""

from __future__ import annotations

from typing import Final

from ..taxonomy import BUCKET_BOUNDS, CATEGORY_ZH

PLATFORM_LABEL: Final[dict[str, str]] = {
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "instagram": "Instagram",
    "x": "X",
    "kwai": "Kwai",
}

BUCKET_LABEL: Final[dict[str, str]] = {
    "nano": "nano（1k–10k 粉）",
    "micro": "micro（10k–100k 粉）",
    "mid": "mid（100k–500k 粉）",
    "macro": "macro（500k–2M 粉）",
    "mega": "mega（2M+ 粉）",
}


def group_label(group_key: str) -> str:
    """``tiktok|micro`` -> ``TikTok · micro（10k–100k 粉）``。"""
    platform, _, bucket = group_key.partition("|")
    return f"{PLATFORM_LABEL.get(platform, platform)} · {BUCKET_LABEL.get(bucket, bucket)}"


def pct(x: float | None, digits: int = 1) -> str:
    if x is None:
        return "缺失"
    return f"{x * 100:.{digits}f}%"


def num(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "缺失"
    if abs(x) >= 10000:
        return f"{x:,.0f}"
    return f"{x:.{digits}f}"


def ratio(x: float | None, digits: int = 3) -> str:
    if x is None:
        return "缺失"
    return f"{x:.{digits}f}"


def cats(items: list[str] | tuple[str, ...] | None) -> str:
    if not items:
        return "（无）"
    return "/".join(CATEGORY_ZH.get(c, c) for c in items)


def source_label(source: str) -> str:
    """阈值来源的中文说明，直接进 human_text 尾巴，便于追问时定位。"""
    if source.startswith("group:"):
        return f"阈值来源：同组样本分位数（{source.split(':', 1)[1]}）"
    if source.startswith("platform:"):
        return f"阈值来源：平台级分位数回退（{source.split(':', 1)[1]}，该细分组样本不足）"
    if source.startswith("fallback:"):
        return f"阈值来源：回退（{source.split(':', 1)[1]}，该细分组样本不足 60）"
    if source == "global" or source.startswith("global"):
        return "阈值来源：全库分位数回退"
    return f"阈值来源：{source}"


def bucket_span(bucket: str) -> str:
    lo, hi = BUCKET_BOUNDS.get(bucket, (0, 0))
    return f"{lo:,}–{hi:,}"
