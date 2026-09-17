"""KOXPilot 的全部 Prompt。

设计原则（这些原则本身是要拿去面试讲的，所以写在代码里而不是脑子里）
--------------------------------------------------------------------
1. **一个 Prompt 只做一件事。** brief 解析、标签错配判定、语义适配打分拆成三个
   独立 Prompt，而不是塞进一个"你是达人营销专家，请综合评估"的大 Prompt。
   理由：拆开后每个环节能单独评测、单独回归、单独换模型；合在一起只能整体
   看好坏，出错时无法定位是哪一步崩的。

2. **输出契约先于自由发挥。** 所有 Prompt 都要求严格 JSON，字段名、取值域、
   枚举值全部写死，且与 ``koxpilot.taxonomy`` 中的受控词表**同源**——
   词表是从代码里注入 Prompt 的，不是手抄的，避免二者漂移。

3. **强制给依据。** 每个判定都要求同时输出 ``evidence``。这不只是为了可解释性——
   要求模型说出依据本身会降低瞎判概率，而且下游产品要把依据展示给投放决策人看。

4. **批量但不过量。** 标签判定和适配打分按批发送（默认 12 条/次）。批量太小
   token 浪费在重复的 system prompt 上，批量太大模型会在后半段偷懒漏项。

5. **拒绝"合理猜测"。** 明确要求信息不足时输出低置信度而不是硬猜。
   投放场景里"我不确定"是有价值的输出（转人工核验），瞎猜是有害的。

6. **明确职责边界。** 适配打分的 Prompt 里显式禁止模型评估"是否水号/报价是否合理/
   品牌安全"——那三件事由确定性规则引擎负责。让模型少管事，是为了让它管好一件事。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# 受控词表从 taxonomy 单一来源注入，避免 Prompt 与数据集口径漂移
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC.parent) not in sys.path:  # pragma: no cover - 便于脚本直跑
    sys.path.insert(0, str(_SRC.parent))

from koxpilot.taxonomy import (  # noqa: E402
    AGE_BUCKETS,
    CATEGORY_ADJACENCY,
    CATEGORY_ZH,
    PLATFORMS,
)

__all__ = [
    "KPI_VOCAB",
    "CAMPAIGN_SPEC_FIELDS",
    "brief_parse_messages",
    "tag_judge_messages",
    "fit_score_messages",
    "category_block",
    "adjacency_block",
]

# KPI 取值域与数据集 / 预算分配器保持一致（reach 而非 awareness）
KPI_VOCAB: dict[str, str] = {
    "reach": "曝光量优先，看重触达规模与单位曝光成本",
    "engagement": "互动优先，看重评论、分享、社区讨论与口碑",
    "conversion": "转化优先，看重带货能力、点击与成交",
}

CAMPAIGN_SPEC_FIELDS: tuple[str, ...] = (
    "target_categories",
    "target_markets",
    "target_languages",
    "target_age_buckets",
    "target_gender",
    "budget_usd",
    "kpi",
    "platforms",
    "competitor_brands",
    "regulated_category",
)


def category_block() -> str:
    return "\n".join(f"  - {k}: {v}" for k, v in CATEGORY_ZH.items())


def adjacency_block() -> str:
    """把品类邻接关系显式喂给模型。

    这一条是踩过坑之后加的：不给邻接表时，模型会把「3C数码 vs 游戏应用」
    这种天然重叠的组合判成错配，导致误报率高。邻接关系是业务知识，
    不该指望模型自己悟出来——能用确定性数据喂的，就别让模型猜。
    """
    return "\n".join(
        f"  - {CATEGORY_ZH[k]}（{k}）↔ " + "、".join(f"{CATEGORY_ZH[x]}（{x}）" for x in v)
        for k, v in CATEGORY_ADJACENCY.items()
    )


_AGE_LIST = " | ".join(AGE_BUCKETS)
_PLAT_LIST = " | ".join(PLATFORMS)
_KPI_LIST = "\n".join(f"  - {k}: {v}" for k, v in KPI_VOCAB.items())


# ---------------------------------------------------------------------------
# A1 BriefAgent：自然语言 brief → 结构化 CampaignSpec
# ---------------------------------------------------------------------------
def _brief_system() -> str:
    return f"""你是出海达人营销的投放策略解析器。你的唯一职责是把客户的自然语言投放 brief，
翻译成下游系统能直接消费的结构化投放规格（CampaignSpec）。

品类（target_categories）只能从以下受控词表取值：
{category_block()}

KPI（kpi）只能取以下之一：
{_KPI_LIST}

年龄段（target_age_buckets）只能从这四档取值：{_AGE_LIST}
平台（platforms）只能从以下取值：{_PLAT_LIST}

严格输出如下 JSON，不要输出任何解释文字、不要用代码围栏：

{{
  "target_categories": ["<品类 key，按相关性排序，1-3 个>"],
  "target_markets": ["<ISO 3166-1 alpha-2 国家码大写，如 US/GB/JP>"],
  "target_languages": ["<ISO 639-1 语言码小写，如 en/ja/id>"],
  "target_age_buckets": ["<{_AGE_LIST}>"],
  "target_gender": "<f|m|all>",
  "budget_usd": <数字，美元总预算；未提及则 null>,
  "kpi": "<reach|engagement|conversion>",
  "platforms": ["<{_PLAT_LIST}>"],
  "competitor_brands": ["<brief 中点名要避开或对标的品牌；没有则空数组>"],
  "regulated_category": <"kids"|"medical"|"finance"|"alcohol"|null，是否需要按更严标准做合规审查>,
  "hard_requirements": ["<brief 中明确的硬性要求，中文短句，如「多用腰尾部达人」>"],
  "evidence": "<一到两句中文，说明你从原文哪些措辞推出了以上关键字段>",
  "ambiguities": ["<你做了假设或原文语义不清的地方，中文短句；没有则空数组>"]
}}

判断纪律：
- 客户给的年龄区间常常跨档（如「25-40 岁」），请映射到覆盖它的档位组合（25-34 与 35-44），
  不要自行收窄。
- target_languages 若未明说，按 target_markets 的主要语言推断，并在 ambiguities 里注明这是推断；
  出海场景通常应保留 en 作为兜底语言。
- platforms 若未明说，返回空数组，不要替客户决定。
- budget_usd 只填数字。若 brief 给的是人民币，按 1 USD = 7.1 CNY 换算并在 ambiguities 说明。
- 「先要量」「打曝光」→ reach；「做口碑/互动/种草」→ engagement；「看重成交/带货/ROI」→ conversion。
- 宁可在 ambiguities 里多写一条，也不要静默地替客户做决定。"""


def brief_parse_messages(raw_text: str) -> list[dict[str, str]]:
    """把一句话 brief 变成结构化投放规格。"""
    return [
        {"role": "system", "content": _brief_system()},
        {
            "role": "user",
            "content": f"客户 brief 原文：\n---\n{raw_text.strip()}\n---\n请输出 CampaignSpec JSON。",
        },
    ]


# ---------------------------------------------------------------------------
# A3-G2.1 标签错配判定：LLM 版（与规则版做带 ground truth 的横评）
# ---------------------------------------------------------------------------
_OUTPUT_CONTRACT_TAG = """严格输出 JSON 数组，不要代码围栏、不要额外文字：

[
  {
    "kox_id": "<原样回填>",
    "mismatch": <true|false>,
    "confidence": <0.0-1.0>,
    "severity": "<none|minor|major>",
    "evidence": "<一句中文说明依据>"
  }
]

数组长度必须与输入达人数一致，kox_id 原样回填，顺序保持一致。"""


def _tag_system() -> str:
    return f"""你是达人（KOL/KOC）标签质量审核员。达人的品类标签来自三类互相独立的来源，
它们经常互相打架：

- declared_categories：达人自己在主页 bio 里声称的品类（最不可信，常年不更新、为接商单夸大）
- observed_categories：系统对该达人**近 30 条内容**做分析后得到的实际品类（最接近事实）
- source_tags：三个第三方数据供应商各自给的品类（口径不一，可能都对也可能都错）

你的任务：判断这个达人是否存在**实质性的标签错配**。

"实质性错配"的定义：**如果投放方按 declared_categories 去选这个达人，会选错人。**
判断时以"这次投放会不会因此浪费预算"为锚点，而不是以"标签字符串是否相同"为锚点。

不算实质性错配的情况（这些是噪声，不要误判）：
- 品类天然相邻或有重叠，参见下方邻接表
- declared 比 observed 更宽泛，但核心品类一致
- 只有某一个供应商的标签离群，另外两个与 observed 一致

品类邻接表（互为相邻的品类之间存在天然内容重叠，不构成错配）：
{adjacency_block()}

severity 判定：
- major：投放方几乎必然选错人（品类完全不相关且不相邻）
- minor：会造成一定偏差但仍有可用性（品类部分偏离）
- none：不构成错配

品类词表：
{category_block()}

{_OUTPUT_CONTRACT_TAG}

纪律：
- 信息明显不足（如 observed_categories 为空）时，mismatch 填 false 且 confidence ≤ 0.3，
  并在 evidence 中写明"信息不足"。不要靠猜。"""


def tag_judge_messages(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """批量判定标签错配。

    只喂判定必需的三组标签，不喂粉丝量/互动率——避免模型被"这是个大号所以应该没问题"
    这类无关信号带偏。这个字段裁剪本身是 Prompt 工程的一部分。
    """
    payload = [
        {
            "kox_id": r["kox_id"],
            "declared_categories": r.get("declared_categories") or [],
            "observed_categories": r.get("observed_categories") or [],
            "source_tags": r.get("source_tags") or {},
        }
        for r in records
    ]
    return [
        {"role": "system", "content": _tag_system()},
        {
            "role": "user",
            "content": (
                f"共 {len(payload)} 个达人，请逐个判定并按顺序返回长度为 {len(payload)} 的 JSON 数组：\n"
                + json.dumps(payload, ensure_ascii=False, indent=1)
            ),
        },
    ]


# ---------------------------------------------------------------------------
# A4 FitAgent：语义适配打分
# ---------------------------------------------------------------------------
def _fit_system() -> str:
    return f"""你是出海达人营销的选号顾问。给定一个投放需求（CampaignSpec）和一批候选达人的
**内容侧画像**，你要给每个达人打一个"内容适配度"分数。

重要边界——你**只**评估内容与受众层面的适配。以下三件事由确定性规则引擎负责，
不在你的评估范围内，掺杂进来会污染分工、也会让系统无法定位问题：
- 是否是水号 / 数据是否注水（由 G1 真实性门禁负责）
- 报价是否合理 / 预算够不够（由预算分配器负责）
- 是否有品牌安全违规（由 G3 品牌安全门禁负责）

你只回答一个问题：**这个达人的内容调性和受众结构，跟这次投放要卖的东西、要打的人群，对不对味？**

评分刻度（严格按此刻度，不要通货膨胀）：
- 0.85-1.00：高度对味。品类正中、受众重叠高、内容形态适合该 KPI
- 0.65-0.84：可用。品类相关或受众部分重叠，需要在创意上做适配
- 0.40-0.64：勉强。有一定关联但需要较大让步，通常只在预算充足时考虑
- 0.15-0.39：不对味。品类偏离或受众错位
- 0.00-0.14：完全不相关

品类词表：
{category_block()}

品类邻接表（相邻品类有天然内容重叠，适配度不应给到最低档）：
{adjacency_block()}

KPI 含义：
{_KPI_LIST}

严格输出 JSON 数组，不要代码围栏、不要额外文字：

[
  {{
    "kox_id": "<原样回填>",
    "fit_score": <0.0-1.0，保留两位小数>,
    "audience_fit": <0.0-1.0，仅看受众结构（年龄/性别/地域）与目标人群的契合>,
    "content_fit": <0.0-1.0，仅看内容品类与调性和产品的契合>,
    "angle": "<一句中文，给出该达人最可行的内容切入角度；不适配则写不适配的原因>"
  }}
]

纪律：
- 数组长度必须与输入达人数一致，kox_id 原样回填，顺序保持一致。
- fit_score 不是 audience_fit 与 content_fit 的机械平均：
  KPI = conversion 时 content_fit 权重更高（内容能不能带货）；
  KPI = reach 时 audience_fit 权重更高（触达对不对人）；
  KPI = engagement 时两者接近，但更看内容能否引发讨论。
- 不要因为达人粉丝多就给高分。粉丝量与报价都不在你的评估范围内。
- 达人自身所在国家不等于其受众所在国家，请以 audience_geo 为准判断地域契合。"""


def fit_score_messages(spec: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """批量给候选达人打语义适配分。"""
    brief_view = {
        "target_categories": spec.get("target_categories") or [],
        "target_markets": spec.get("target_markets") or [],
        "target_languages": spec.get("target_languages") or [],
        "target_age_buckets": spec.get("target_age_buckets") or [],
        "target_gender": spec.get("target_gender"),
        "kpi": spec.get("kpi"),
        "regulated_category": spec.get("regulated_category"),
        "hard_requirements": spec.get("hard_requirements") or [],
    }
    payload = [
        {
            "kox_id": r["kox_id"],
            "platform": r.get("platform"),
            "creator_country": r.get("country"),
            "language": r.get("language"),
            "declared_categories": r.get("declared_categories") or [],
            "observed_categories": r.get("observed_categories") or [],
            "audience_geo": r.get("audience_geo") or {},
            "audience_age": r.get("audience_age") or {},
            "audience_gender": r.get("audience_gender") or {},
        }
        for r in records
    ]
    return [
        {"role": "system", "content": _fit_system()},
        {
            "role": "user",
            "content": (
                "投放需求 CampaignSpec：\n"
                + json.dumps(brief_view, ensure_ascii=False, indent=1)
                + f"\n\n候选达人共 {len(payload)} 个，请按顺序返回长度为 {len(payload)} 的 JSON 数组：\n"
                + json.dumps(payload, ensure_ascii=False, indent=1)
            ),
        },
    ]
