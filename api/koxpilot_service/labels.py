"""代码标识符 → 中文人话的翻译表。接口对外**只说人话**。

为什么单独一个模块
------------------
界面刚做过一轮「去内部化」清洗：把 ``keep_rule_fit_in_formal_chain`` 这种
只有作者看得懂的符号从界面上清掉了。如果接口再把它们从后端灌回去，那轮清洗就白做了。
所以凡是要出现在响应里的标签，一律先过这里翻译；翻不出来的**如实回退原文**并不假装，
但 :func:`assert_no_identifier` 会在开发期把这种漏网情况暴露出来。

规则标签刻意与前端 ``web/src/lib/pipeline.ts`` 的 ``RULE_LABEL`` 逐字对齐：
同一条规则在「Python 服务算的」和「浏览器 TS 算的」两处必须叫同一个名字，
否则一致性比对界面上会出现「同一条规则两种说法」这种自伤。
"""

from __future__ import annotations

import re
from typing import Dict, Optional

#: 四层门禁的中文名（G0/G1/G2/G3 的层号本身保留，它是业务口径不是代码标识符）
GATE_LABEL: Dict[str, str] = {
    "G0": "数据完整性",
    "G1": "真实性",
    "G2": "一致性",
    "G3": "品牌安全",
}

#: 规则号 -> 中文人话。与 web/src/lib/pipeline.ts 的 RULE_LABEL 逐字一致。
RULE_LABEL: Dict[str, str] = {
    "G0.1": "关键字段缺失",
    "G0.2": "低置信度（缺失后仍判定）",
    "G1.1": "互动率异常高（同组上尾 P95）",
    "G1.2": "互动率异常低（同组下尾 P05）",
    "G1.3": "评论/点赞比越界 [P02, P98]",
    "G1.4": "播放/粉丝比异常高（P98）",
    "G1.5": "粉丝断层式突增",
    "G1.6": "日均涨粉超同组 P97（新号巨量粉）",
    "G1.7": "评论重复率 / 纯 emoji 率高",
    "G2.1": "自称品类 vs 实际内容错配",
    "G2.2": "多源标签互相冲突",
    "G2.3": "语义适配分过低",
    "G2.4": "语言与目标市场不符",
    "G2.5": "受众地域不在目标市场",
    "G2.6": "受众人群（年龄/性别）不匹配",
    "G3.1": "高危内容硬阻断",
    "G3.2": "风险内容载荷超限",
    "G3.3": "近期竞品合作冲突",
    "G3.4": "争议历史",
    "G3.5": "受管制品类需额外审",
}

#: 可观测信号名 -> 中文（explain 接口的 signals 数组用）
SIGNAL_LABEL: Dict[str, str] = {
    "engagement_rate": "互动率",
    "comment_like_ratio": "评论/点赞比",
    "view_follower_ratio": "播放/粉丝比",
    "followers_per_day": "日均涨粉",
    "comment_dup_rate": "评论重复率",
    "comment_emoji_only_rate": "纯 emoji 评论占比",
    "max_monthly_growth": "单月最大涨粉率",
    "risk_severity_score": "风险内容载荷分",
    "avg_cpm_usd": "千次曝光报价",
}

#: 定向筛选未通过的原因 -> 中文（漏斗与「谁被排除了」用）
SKIP_REASON_LABEL: Dict[str, str] = {
    "platform_off_target": "不在本次投放的平台清单里",
    "category_off_target": "内容品类与目标品类（含相邻品类）不沾",
    "market_off_target": "既不是目标市场本地账号，受众里也没有目标市场",
    "no_gate_result": "没有门禁判定结果",
    "avg_views_missing": "平均播放量缺失，无法估算曝光",
    "price_unavailable": "报价缺失且无法按同组 CPM 估算",
    "gate_pass": "门禁判定为可投，但未进入本次清单",
    "gate_review": "门禁判定需人核",
    "gate_reject": "门禁判定不可投",
}

#: KPI -> 中文
KPI_LABEL: Dict[str, str] = {
    "conversion": "转化",
    "reach": "曝光",
    "engagement": "互动",
    "balanced": "均衡（未指定侧重）",
}

#: 性别 -> 中文
GENDER_LABEL: Dict[str, str] = {"f": "女性", "m": "男性"}

#: 受管制口径 -> 中文
REGULATED_LABEL: Dict[str, str] = {
    "medical": "医疗/功效宣称",
    "finance": "金融/理财",
    "kids": "儿童向",
    "alcohol": "酒类",
}

#: 预算多臂 -> 中文
ARM_LABEL: Dict[str, str] = {
    "koxpilot": "KOXPilot 决策",
    "follower_rank": "按粉丝量排序",
    # 第三臂的存在意义：把「比基线好」拆成「分散化的功劳」与「门禁的功劳」两笔账
    "diversified_no_gate": "只做结构分散、不做质量门禁",
}

#: 六个 Agent -> 中文（timings 用）
AGENT_LABEL: Dict[str, str] = {
    "A1": "brief 解析",
    "A2": "定向召回",
    "A3": "四层门禁",
    "A4": "语义适配",
    "A5": "预算分配",
    "A6": "成本与反事实价值审计",
}

#: 语义适配分的来源（契约 §4 只允许这两个取值）
FIT_SOURCE_CACHED_LLM = "cached_llm"
FIT_SOURCE_RULE_FALLBACK = "rule_fallback"

#: 单条信号相对同组阈值的位置
SIGNAL_IN_RANGE = "in_range"
SIGNAL_OUT_OF_RANGE = "out_of_range"
SIGNAL_MISSING = "missing"
SIGNAL_NO_THRESHOLD = "no_threshold"
#: 高于同组参考线、但对应规则还要求别的条件（本次没命中）。
#: 单独一个状态而不含糊地写成 in_range，是因为「值确实在尾部」与「规则没判它」是两件事，
#: 合并成一个状态就等于在界面上掩盖了其中一件。
SIGNAL_ABOVE_REFERENCE = "above_reference"


def gate_label(gate: str) -> str:
    return GATE_LABEL.get(gate, gate)


def rule_label(rule_id: str) -> str:
    """规则号 -> 中文人话。表里没有时回退规则号本身（业务口径，非代码标识符）。"""
    return RULE_LABEL.get(rule_id, rule_id)


def signal_label(name: str) -> str:
    return SIGNAL_LABEL.get(name, name)


def skip_reason_label(reason: str) -> str:
    return SKIP_REASON_LABEL.get(reason, reason)


def kpi_label(kpi: str) -> str:
    return KPI_LABEL.get(kpi, kpi)


#: 看起来像「代码标识符」的形状：snake_case / camelCase / 带点的模块路径
_IDENTIFIER_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+")

#: 白名单：这些是业务专有名词或对外正式写法，不算「内部标识符」
_ALLOWED = (
    "kox_id",
    "KOXPilot",
)


def looks_like_identifier(text: Optional[str]) -> Optional[str]:
    """text 里是否夹带了代码标识符；有则返回第一个命中的片段，否则 None。

    只在开发/自测阶段调用（见 tests 与 selfcheck），线上不做拦截——
    宁可把一个不完美的标签发出去，也不要因为文案检查让接口 500。
    """
    if not text:
        return None
    for match in _IDENTIFIER_SHAPE.finditer(text):
        token = match.group(0)
        if token in _ALLOWED:
            continue
        return token
    return None
