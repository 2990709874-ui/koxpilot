# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/llm/prompt_variants.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
"""Prompt 版本谱系：把「Prompt 工程」变成可测量的实验，而不是玄学。

为什么要有这个文件
------------------
"会写 Prompt"是个很难证伪的说法。要证明它，唯一的办法是：
**同一个任务、同一份 ground truth、同一个模型，只改 Prompt，看指标怎么变。**

这里保留了「标签错配判定」任务的三个真实迭代版本。它们不是为了演示而编造的，
而是我在实际调试时依次遇到的三个问题的产物：

- **v1 朴素版**：只描述任务，不给判定标准。
  预期问题：模型会把所有品类不完全一致的情况都判成错配（把"3C 数码 vs 游戏应用"
  这种天然相邻品类也算错），导致 precision 很低、recall 虚高。

- **v2 加负例约束版**：补上"什么不算错配"的四条豁免规则（相邻品类、宽泛包含、
  单源离群、信息不足）。
  预期改善：precision 上升。
  预期残留问题：模型仍会把 declared 与 observed 的**任何**差异当作同等严重，
  且缺少"从投放方视角判断"的锚点。

- **v3 定义业务判据版**（当前生产版）：把判据从"标签是否一致"改写成
  **"如果投放方按 declared 去选人，会不会选错人"**，并要求输出 severity 与 evidence。
  预期改善：判定与业务后果对齐，precision/recall 更均衡。

每一版的假设都写在 ``hypothesis`` 里，实验结果由
``python -m koxpilot.llm.promptbench`` 真实测出来，写进 ``output/prompt_bench.json``。
**如果实测结果和我的假设相反，就按实测写结论。** 这份文件的价值在于展示
"评测驱动的 Prompt 迭代"这个方法，不在于证明我第一次就猜对了。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .prompts import adjacency_block, category_block

__all__ = ["TAG_VARIANTS", "PromptVariant", "build_messages"]

_CAT_LIST = category_block()
_ADJ_LIST = adjacency_block()

_OUTPUT_CONTRACT = """严格输出 JSON 数组，不要代码围栏、不要额外文字：

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


# ---------------------------------------------------------------------------
# v1：朴素版 —— 只说任务，不给标准
# ---------------------------------------------------------------------------
_V1 = f"""你是达人标签审核员。判断每个达人的品类标签是否存在错配。

达人有三组品类标签：
- declared_categories：达人自称的品类
- observed_categories：系统分析其近期内容得到的品类
- source_tags：三个第三方数据供应商各自给的品类

品类词表：
{_CAT_LIST}

{_OUTPUT_CONTRACT}"""


# ---------------------------------------------------------------------------
# v2：加负例约束 —— 补上"什么不算错配"
# ---------------------------------------------------------------------------
_V2 = f"""你是达人标签审核员。判断每个达人的品类标签是否存在错配。

达人有三组品类标签：
- declared_categories：达人自己在主页 bio 里声称的品类（最不可信，常年不更新、为接商单夸大）
- observed_categories：系统对该达人近 30 条内容做分析后得到的实际品类（最接近事实）
- source_tags：三个第三方数据供应商各自给的品类（口径不一）

以下四种情况**不算**错配，请不要误判（这是最容易出错的地方）：
1. 品类天然相邻或有重叠（如 3C 数码 与 游戏应用；美妆个护 与 服饰鞋包）
2. declared 比 observed 更宽泛，但核心品类一致（如声称"家居家电"，实际做"小家电"）
3. 只有某一个供应商的标签离群，另外两个与 observed 一致
4. observed_categories 为空或信息明显不足 —— 此时应判 false 且 confidence ≤ 0.3

品类词表：
{_CAT_LIST}

{_OUTPUT_CONTRACT}"""


# ---------------------------------------------------------------------------
# v3：定义业务判据 —— 当前生产版（与 prompts.py 中的 _TAG_JUDGE_SYSTEM 保持一致）
# ---------------------------------------------------------------------------
_V3 = f"""你是达人（KOL/KOC）标签质量审核员。达人的品类标签来自三类互相独立的来源，
它们经常互相打架：

- declared_categories：达人自己在主页 bio 里声称的品类（最不可信，常年不更新、为接商单夸大）
- observed_categories：系统对该达人**近 30 条内容**做分析后得到的实际品类（最接近事实）
- source_tags：三个第三方数据供应商各自给的品类（口径不一，可能都对也可能都错）

你的任务：判断这个达人是否存在**实质性的标签错配**。

"实质性错配"的定义：**如果投放方按 declared_categories 去选这个达人，会选错人。**
也就是说，达人实际产出的内容与其声称的品类，在营销可用性上不是一回事。
判断时请以"这次投放会不会因此浪费预算"为锚点，而不是以"标签字符串是否相同"为锚点。

不算实质性错配的情况（这些是噪声，不要误判）：
- 品类天然相邻或有重叠，参见下方邻接表
- declared 比 observed 更宽泛，但核心品类一致（如声称"家居家电"，实际做"小家电"）
- 只是某一个供应商的标签离群，另外两个与 observed 一致

品类邻接表（互为相邻的品类之间存在天然内容重叠，不构成错配）：
{_ADJ_LIST}

severity 的判定：
- major：投放方几乎必然选错人（品类完全不相关）
- minor：会造成一定偏差但仍有可用性（品类部分偏离）
- none：不构成错配

品类词表：
{_CAT_LIST}

{_OUTPUT_CONTRACT}

纪律：
- 信息明显不足（如 observed_categories 为空）时，mismatch 填 false 且 confidence ≤ 0.3，
  并在 evidence 中写明"信息不足"。不要靠猜。"""


@dataclass(frozen=True)
class PromptVariant:
    version: str
    name: str
    system: str
    change: str
    hypothesis: str

    def to_meta(self) -> dict[str, str]:
        return {
            "version": self.version,
            "name": self.name,
            "change": self.change,
            "hypothesis": self.hypothesis,
            "system_prompt_chars": str(len(self.system)),
        }


TAG_VARIANTS: tuple[PromptVariant, ...] = (
    PromptVariant(
        version="v1",
        name="朴素版：只描述任务",
        system=_V1,
        change="基线。只说明三类标签来源和输出格式，不给任何判定标准。",
        hypothesis="模型会把任何品类差异都当错配，precision 低、recall 偏高。",
    ),
    PromptVariant(
        version="v2",
        name="加负例约束版",
        system=_V2,
        change="补充四条「不算错配」的豁免规则（相邻品类 / 宽泛包含 / 单源离群 / 信息不足）。",
        hypothesis="precision 明显上升；但因缺少业务锚点，severity 判定仍不稳。",
    ),
    PromptVariant(
        version="v3",
        name="定义业务判据版（生产版）",
        system=_V3,
        change=(
            "两处改动：(1) 把判据从「标签是否一致」改写为「投放方按 declared 选人会不会选错」，"
            "给 severity 明确定义、要求以浪费预算为锚点；"
            "(2) 把品类邻接关系从举例改为从 taxonomy 注入的完整确定性邻接表。"
            "⚠️ 本版含两处改动，v2→v3 的增益无法在二者之间做严格归因，如实记录。"
        ),
        hypothesis="判定与业务后果对齐 + 邻接表消除相邻品类误报，F1 高于 v2 且 precision/recall 更均衡。",
    ),
)


def build_messages(variant: PromptVariant, records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """按指定 Prompt 版本构造消息。

    user 部分在三个版本间保持完全一致 —— 这是控制变量的要求：
    只有 system prompt 在变，输入数据的呈现方式不变，才能把指标差异归因到 Prompt 本身。
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
        {"role": "system", "content": variant.system},
        {
            "role": "user",
            "content": (
                f"共 {len(payload)} 个达人，请逐个判定并按顺序返回长度为 {len(payload)} 的 JSON 数组：\n"
                + json.dumps(payload, ensure_ascii=False, indent=1)
            ),
        },
    ]
