# 04 · Prompt 工程与成本决策

> 这份文档回答两个决策，两个都用实测数字回答，不用立场回答：
>
> **① Prompt 怎么写才有用？** —— 三版谱系、6 个 arm 的控制变量实验、增益归因到哪一处改动。
> **② LLM 到底该不该用、用在哪？** —— 三种方案的调用量对照、真实 token 账、以及"不用 LLM 要付多少准确率"。
>
> 相关文档：[01 架构](01-architecture.md) · [02 门禁规则](02-gates.md) · [03 评测方法](03-evaluation.md) · [05 边界声明](05-boundaries.md)

---

## 1. Prompt 七条原则（每条都配"不这么做会发生什么"）

这七条写在 `src/koxpilot/llm/prompts.py` 里，是从踩坑反推出来的，不是从最佳实践列表抄来的。

| 原则 | 不这么做会发生什么 |
| --- | --- |
| **① 单任务** —— 一个 prompt 只做一件判定 | 让模型"同时判标签错配和适配度"，两个任务会互相污染；错的那个会拖着对的那个一起返回低置信度，且无法归因 |
| **② 严格 JSON 输出** —— 给 schema、给字段类型、不给自由文本 | 解析失败率上去后，你会被迫写正则兜底，而正则兜底会**静默吞掉**模型的错误，指标看起来更好 |
| **③ 受控词表同源注入** —— 品类词表 / 邻接表从 `taxonomy.py` 注入，不在 prompt 里手抄 | 手抄的词表会和代码里的词表漂移。模型返回一个代码不认识的品类名，下游只能丢弃或猜——两者都会污染指标 |
| **④ 强制 evidence** —— 必须返回判定依据，不只是结论 | 没有 evidence 就无法人工抽检，也无法在证据链里给运营解释。而且要求给依据本身会提升判定质量（v1→v3 的一部分收益来自这里） |
| **⑤ 批量但不过量** —— 每批 12 条 | 批量 1 条 = token 和延迟爆炸；批量 50 条 = 模型在后半段开始偷懒、漏项、串号。12 是实测下"输出完整率"和"单位成本"的折中 |
| **⑥ 拒绝合理猜测** —— 信息不足必须显式返回"不足"，不许推断 | 模型很擅长把"不知道"包装成"看起来合理的判断"。这类输出在指标上表现为难以定位的假阳性 |
| **⑦ 明确职责边界** —— prompt 里写清"你不负责什么" | 不写的话模型会顺手替你做决策（比如自己决定该 reject），而 reject 权限在门禁手里，不在模型手里 |

另有一条**反泄漏纪律**，它不是 prompt 技巧而是评测底线：**prompt 里绝不能出现 ground truth**。这一条有专门的测试逐条检查"该达人隐藏真实品类中既非 declared 也非 observed 的那部分"不出现在任何一条消息里——把答案写进 prompt 是最隐蔽的泄漏，模型"猜得准"其实是抄的，而 token 账、延迟、输出格式全都正常。详见 [03 §3.4](03-evaluation.md#34-通道封闭--prompt-全文体检)。

---

## 2. 三版 Prompt 谱系：控制变量实验

### 2.1 实验设计

任务：**标签错配判定**（对应门禁 G2.1）。

| 设计项 | 值 |
| --- | --- |
| 样本 | 600 条，正例 42 条，`positive_rate = 0.07` |
| 控制变量 | 三版使用**完全相同的样本、batch 切分、user message 构造方式**，唯一变量是 **system prompt** |
| 模型 | 2 个（ark / azure），每版每模型各 1 个 arm |
| 对照组 | 规则版 G2.1（Jaccard < 0.34），0 token |
| 反过拟合声明 | **三版措辞在跑评测前已写定，未按指标反复微调** |
| 总耗时 | 967.3s |

最后一行很重要：如果按指标反复改 prompt 措辞，本质上是在 600 条测试样本上做梯度下降，报出来的 F1 就是过拟合的。这句声明写在 `prompt_bench.json` 的 `control_variables` 字段里，不是写在文档里——**放在产物里才有约束力**。

### 2.2 三版的假设与改动

| 版本 | 名称 | 改动 | 事前假设 | system prompt 字符数 |
| --- | --- | --- | --- | --- |
| **v1** | 朴素版：只描述任务 | 基线。只说明三类标签来源和输出格式，**不给任何判定标准** | 模型会把任何品类差异都当错配 → precision 低、recall 偏高 | 544 |
| **v2** | 加负例约束版 | 补充四条「不算错配」的豁免规则：相邻品类 / 宽泛包含 / 单源离群 / 信息不足 | precision 明显上升；但缺业务锚点，severity 判定仍不稳 | 831 |
| **v3** | 定义业务判据版（生产版） | **两处改动**：(1) 判据从「标签是否一致」改写为「**投放方按 declared 选人会不会选错**」，给 severity 明确定义、要求以浪费预算为锚点；(2) 品类邻接关系从**举例**改为从 `taxonomy` 注入的**完整确定性邻接表** | 判定与业务后果对齐 + 邻接表消除相邻品类误报 → F1 高于 v2 且 P/R 更均衡 | 1,607 |

**每一版都先写假设再跑**，假设也落盘在 `prompt_bench.json`。这样"结果符合预期"和"结果打脸"都能被看见——如果只在跑完后写解释，那永远都是符合预期。

---

## 3. 结果：6 个 arm 全表

### 3.1 按版本取均值（跨两个模型）

| 版本 | avg P | avg R | avg F1 | 两模型合计 token |
| --- | --- | --- | --- | --- |
| 规则对照 | 0.4186 | 0.8571 | **0.5625** | **0** |
| v1 | 0.2367 | 0.8453 | 0.3627 | 434,018 |
| v2 | 0.3554 | 0.7857 | 0.4889 | 467,327 |
| **v3** | **0.6136** | 0.7976 | **0.6905** | 492,460 |

**最佳 arm：v3 × azure，P=0.6875 / R=0.7857 / F1=0.7333。**

### 3.2 逐 arm 明细（这张表比均值有用）

| 版本 | 模型 | P | R | F1 | total token | 其中 reasoning | 平均延迟 | 平均自报置信度 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| rule | —— | 0.4186 | 0.8571 | 0.5625 | 0 | 0 | 0 ms | —— |
| v1 | ark | 0.3000 | 0.7857 | 0.4342 | 266,059 | 117,662 | 54,249 ms | 0.9278 |
| v1 | azure | **0.1735** | 0.9048 | **0.2912** | 167,959 | 44,188 | 20,140 ms | 0.8845 |
| v2 | ark | 0.3908 | 0.8095 | 0.5271 | 288,775 | 133,697 | 60,295 ms | 0.9194 |
| v2 | azure | 0.3200 | 0.7619 | 0.4507 | 178,552 | 45,418 | 21,828 ms | 0.9083 |
| v3 | ark | 0.5397 | 0.8095 | 0.6476 | 310,247 | 132,804 | 57,694 ms | 0.9501 |
| **v3** | **azure** | **0.6875** | 0.7857 | **0.7333** | 182,213 | **26,338** | **14,766 ms** | 0.9363 |

---

## 4. 这张表该怎么读（四个结论 + 一个不能得出的结论）

### 4.1 结论一：不给判定标准的 prompt，比一条 Jaccard 规则还差

**v1 的两个 arm（F1 0.4342 / 0.2912）都显著低于规则对照组的 0.5625。**

azure 在 v1 上 precision 只有 **0.1735**——181 个假阳性，把 600 条里几乎所有品类有差异的人都判成了错配。事前假设写的就是这个（"模型会把任何品类差异都当错配"），实测比假设更极端。

这条结论对"要不要上 LLM"这个问题很关键：**"把任务丢给大模型"本身不产生价值**。v1 花了 43 万 token，换来一个比 0 token 的规则更差的结果。如果只做了 v1 就下结论，正确的结论会是"LLM 不适合这个任务"——而那是错的。

### 4.2 结论二：收益几乎全部来自 precision

| 版本 | avg P | avg R |
| --- | --- | --- |
| v1 | 0.2367 | 0.8453 |
| v2 | 0.3554（**+0.1187**） | 0.7857（−0.0596） |
| v3 | 0.6136（**+0.2582**） | 0.7976（+0.0119） |

recall 从头到尾在 0.79–0.85 之间几乎不动，**F1 从 0.3627 涨到 0.6905 全部是 precision 贡献的**（0.2367 → 0.6136，2.6 倍）。

机理很清楚：模型本来就"愿意判错配"（recall 天然高），缺的是**知道什么不算错配**。v2 的四条豁免规则和 v3 的完整邻接表，做的都是同一件事——**告诉模型哪里该收手**。

这也解释了为什么 LLM 在这个任务上打得过规则：规则版是 P=0.4186 / R=0.8571 的"高召回低精确"形态，和 v1 惊人地相似；而 v3 是 P=0.6875 / R=0.7857 的均衡形态。**LLM 赚的是精确率**，因为它能读懂"美妆博主偶尔发一条穿搭"和"美妆标签实际全是游戏内容"的区别，而 Jaccard 只会数集合交集。

### 4.3 结论三：v3 的增益无法严格归因，产物中已标注警告

`prompt_bench.json` 里 v3 的 `change` 字段结尾写着：

> ⚠️ **本版含两处改动，v2→v3 的增益无法在二者之间做严格归因，如实记录。**

两处改动是"改写业务判据"和"注入完整邻接表"。要严格归因，需要再跑两个 arm（只改判据 / 只加邻接表），这两个 arm 未跑。所以正确的说法是"**这两处改动合计带来 +0.2016 的 avg F1**"，而不是"业务判据的重要性大于邻接表"。

因此在产物里保留一个 ⚠️，而不在文档里给出一个无法验证的归因故事。

### 4.4 结论四：更好的 prompt 可以同时更准、更便宜、更快

看 azure 那三行：

| azure | F1 | reasoning token | 平均延迟 |
| --- | --- | --- | --- |
| v1 | 0.2912 | 44,188 | 20,140 ms |
| v2 | 0.4507 | 45,418 | 21,828 ms |
| **v3** | **0.7333** | **26,338（−40%）** | **14,766 ms（−27%）** |

v3 在 azure 上**准确率翻倍的同时，reasoning token 降了 40%、延迟降了 27%**。解释：判据模糊时模型要自己"想清楚标准是什么"，这部分思考被计入 reasoning token；判据明确后它不需要自己发明标准了。

**但这个规律不是普适的。** 同样的 v3 在 ark 上 reasoning token 反而从 117,662 涨到 132,804（+13%），延迟基本不变。所以正确的表述是"**在这个任务上，对 azure 成立**"，而不是"好 prompt 一定更省钱"。

### 4.5 不能得出的结论：azure 比 ark 好

v3 上 azure F1 0.7333 > ark 0.6476，看起来差距不小（+0.0857）。但：

- **v1 上完全相反**：ark 0.4342 > azure 0.2912（+0.1430）。
- 也就是说，**azure 对 prompt 质量的敏感度远高于 ark**。用 v1 会得出"ark 更好"，用 v3 会得出"azure 更好"。
- 正确的结论是：**这两个模型不可比，或者说"哪个模型更好"取决于你的 prompt 写到什么水平**。用单一 prompt 版本给模型排名是典型的以偏概全。
- 另外这只是**单轮实验**，temperature 不为 0，没做多轮取均值方差（[05 §3.3](05-boundaries.md#33-单次运行的波动没有被消掉)）。

顺带说明一处必须点出的不一致：`prompt_variants.py` 的注释称 v3 与生产 prompt 保持一致，但两者**字符数不同**（v3 system 1,607 字符，生产 `_tag_system()` 1,553 字符；user message 一致）。所以横评里 v3 的表现应读作"**≈ 生产版**"，不能读成"= 生产版"。登记在 [05 §10](05-boundaries.md#10-已知的文档代码不一致自查发现未修复项全部登记在此)。

---

## 5. 成本决策：LLM 用在哪，用多少

### 5.1 三种方案的调用量对照（`metrics.json.cost_audit`）

候选池：BRIEF-001 93 人 / BRIEF-002 50 人 / BRIEF-003 79 人。

| 方案 | 说明 | 三个 campaign 合计调用 |
| --- | --- | --- |
| **全 LLM** | 每个达人的每层判定都问模型 | **888** |
| **KOXPilot 混合** | G0/G1 纯规则，G2 部分规则，仅 brief 解析与语义适配用 LLM | **225** |
| **全规则** | 不用 LLM，语义适配退化为关键词匹配 | **0** |

`call_reduction_vs_full_llm = 3.95`（混合方案把调用量压到全 LLM 的约 1/4）。

### 5.2 "不用 LLM"要付的准确率代价，有数字

`accuracy_cost_of_all_rules`：

| 口径 | 值 |
| --- | --- |
| 指标 | 标签错配 F1（同一抽样、同一 ground truth） |
| 规则臂 | 0.5625 |
| LLM 臂 | 0.6824 |
| **`f1_drop_if_all_rules`** | **0.1199** |

**这才是"该不该用 LLM"的正确问法**：不是"LLM 强不强"，而是"**省掉它要付多少准确率，这个代价在哪个环节可接受**"。

相应的取舍是：

- **G0/G1（完整性、水号识别）不用 LLM。** 这两层的判据是"和同组同行比分位数"，LLM 拿不到分布，而且这两层必须能被回归测试锁定。
- **G2 的标签错配用 LLM。** 这里 0.1199 的 F1 差距对应的是"按错标签选人"的真金白银，值得付 token。
- **G3 品牌安全不用 LLM。** 它需要的是确定性和可追责（法务口径），不是语义理解。
- **A1 brief 解析用 LLM。** 这是唯一一个输入是自然语言的环节，规则做不了。

### 5.3 真实 token 账（不是估算）

`cost_audit.status = "ok"`，`token_account.source` 原文：

> **`output/llm_bench.json`（各 API 返回的 usage 字段，非估算）**

这句话是这一节的前提。**如果成本账是估算的，那这一整节都不该写。**

总账（`make llm` 的 brief + tag + fit 三个任务）：

| 项 | 值 |
| --- | --- |
| measured_calls | **172** |
| prompt_tokens | 536,827 |
| completion_tokens | 566,646 |
| 其中 reasoning_tokens | **403,202** |
| **total_tokens** | **1,103,473** |
| tokens_per_call | 6,415.5 |
| `extrapolated` | **false** |

按任务 × 模型分账：

| 任务 | 模型 | calls | 失败 | items | total token | token/item | 平均延迟 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| brief | azure | 3 | 0 | 3 | 4,168 | 1,389.3 | 6,175 ms |
| brief | ark | 3 | 0 | 3 | 7,782 | 2,594.0 | 32,111 ms |
| tag | ark | 50 | 0 | 600 | 309,751 | 516.3 | 62,820 ms |
| tag | azure | 50 | **1** | 600 | 179,632 | 299.4 | 14,756 ms |
| fit | ark | 66 | 0 | **780** | 602,140 | 772.0 | 75,238 ms |

三个值得注意的读数：

1. **reasoning token 占 completion 的 71%**（403,202 / 566,646）。推理型模型的成本结构和传统模型完全不同——**按 completion 计费的话，你付的钱大部分买的是"思考过程"而不是"答案"**。这也是 §4.4 那个"好 prompt 更便宜"结论的经济意义所在。
2. **fit 任务是最大的开销**（602,140 token = 总量的 55%），因为 items 是 780（260 条 × 3 个 campaign）。语义适配是"每人每单子都要算一次"的任务，它的成本随 campaign 数线性增长——这是把它做成**离线批处理 + 缓存**而不是在线调用的直接理由。
3. **ark 的延迟比 azure 高 4 倍**（tag 任务 62.8s vs 14.8s per call）。如果要做在线推理，这个延迟决定了体验；离线批处理下它只影响构建时长（`make llm` 全程 939.4s）。

### 5.4 外推与实测的界限，用字段标出来

`scheme_token_extrapolation`：

| 方案 | 外推 token | `extrapolated` |
| --- | --- | --- |
| 全 LLM | 5,697,000 | **true** |
| KOXPilot 混合 | 1,443,497 | **true** |
| 全规则 | 0 | true |

`basis` 字段写明："按实测单次调用 token 均值 × 各方案调用次数线性外推"。

**外推不是实测。** 5,697,000 这个数字是拿 6,415.5 tokens/call 乘以 888 次得到的，它假设"每次调用的 token 消耗相同"——这个假设在不同任务间明显不成立（brief 2,594 token/item vs tag 299 token/item）。因此该字段标记为 `extrapolated=true`，而实测账那一块是 `extrapolated=false`。

**两块账不要相加，也不要混读。** 另外 `make promptbench` 的 3 版 × 2 模型横评（v1/v2/v3 各 434,018 / 467,327 / 492,460 token，合计 1,393,805）是**另一笔账**，记在 `prompt_bench.json` 的各 arm 里，不在 `cost_audit` 的 1,103,473 里。

---

## 6. 两个工程细节（均为实际踩过的坑）

### 6.1 `make llm` 跑完必须重跑 `make eval`

这是实际踩到过的坑：

> `cost_audit` 和表 6 要读 `output/llm_bench.json`。
> **`make llm` 跑完不重跑 eval，`metrics.json` 的 `cost_audit.status` 会停在 `"llm_not_run"`**——
> 成本账全是空的，而 `metrics.json` 看起来完全正常（其余五张表都在，都是对的）。

这个坑的性质和 [03 §2.2](03-evaluation.md#22-第二道网缓存-key-只-hash-了-kox_id) 的缓存 key 是同一类：**产物看起来正常，缺的那部分不会报错**。

修法在 Makefile 里，`llm` target 末尾自动串了一次 `cli eval`：

```makefile
llm:
	$(PY) -m koxpilot.llm.runner --tasks brief,tag,fit --workers 12
	@echo ">>> LLM 产物已更新，正在重跑 eval 以接入真实 token 账 ..."
	$(PY) -m koxpilot.cli eval
```

手工调 `python -m koxpilot.llm.runner` 的人得自己补这一步。**检查方法**：看 `cost_audit.status` 是不是 `"ok"`。

### 6.2 调用失败按"未覆盖"处理，不用默认值填充

`tag::azure` 有 **1 次调用失败**（172 次里唯一一次）。处理方式：

- `runner.py` 返回空列表并记录 failure；
- 评测侧按**未覆盖**处理，`coverage = 0.98`（600 条判了 588 条），如实落盘；
- **不用中性值（比如 0.5）填空**。

理由：拿默认值填空会让指标看起来更平滑，实际是在编数据。代价是 azure 的表 6 结果基于 588 条而非 600 条——**这也正是"azure 略高 0.0015"这个名次不可信的第二个理由**（[03 §表 6](03-evaluation.md#表-6--llm-vs-规则table_6_llm_vs_rule)）。

### 6.3 缓存 key 必须 hash 实际 messages

`key = 任务 + 模型/版本 + sha1(实际发给模型的完整 messages)`。为什么不能只 hash `kox_id`、以及这个 bug 怎么被抓到的，见 [03 §2.2](03-evaluation.md#22-第二道网缓存-key-只-hash-了-kox_id)。

### 6.4 模型标识：请求 id 与服务端型号是两件事（已统一）

这里曾经有一处真实的产物不一致：`metrics.json.table_6.models.ark` 记的是 **endpoint id**（`ep-20260308011145-z5d47`，ARK 用 endpoint 当 `model` 字段发请求），而 `cost_audit.token_account.per_task` 里记的是**服务端回报的型号**（`doubao-seed-2-0-lite-260215`）。两处都叫 "model"，含义不同。它不影响任何 token 数字，但照着 `models` 写材料的人会说出"所用模型是 ep-2026…"——那是资源 id 不是模型，现场会被问穿。

现在的口径（`src/koxpilot/llm/identity.py`）：

| 字段 | 含义 | 来源 |
| --- | --- | --- |
| `models[key]` | **对外展示名**：服务端回报的型号优先 | 归一结果 |
| `model_identity.by_model_key[key].requested_model_id` | 发请求时填进 body 的 `model`（ARK 是 endpoint id） | provider 配置 / `ARK_MODEL` |
| `...served_models` | 服务端在响应里回报的型号（可能多个：灰度/版本切换） | API 响应的 `model` 字段 |
| `...display_source` | 展示名是**谁说的** | `served_by_api` / `requested_id_confirmed_by_api` / `requested_id_only` / `multiple_served_models` |

三条纪律：

1. **不做字符串模式匹配。** 不存在"以 `ep-` 开头就算 endpoint"这种判据——换供应商就会把对的说成错的，而且会让错误看起来像正确。唯一判据是"这个名字是谁说的"。
2. **调用失败不得把请求 id 冒充成服务端回报。** 全部失败的槽位 `served_models = []`、`display_source = requested_id_only`，来源自证其弱。
3. **服务端回报多个型号时不许静默挑一个**，`display` 如实写成 `multiple:a,b`。

已提交的 `output/llm_bench.json` 仍是统一之前的产物（重跑 `make llm` 要真花 token，本次没跑）。读侧会把它归一成同一套口径，并在 `metrics.json.honesty_notes` 里直说"该产物生成于口径统一之前"，`model_identity.schema` 标为 `legacy_v1`；重跑后即为 `model_identity_v2`。守这件事的测试是 `tests/test_model_identity.py::TestShippedArtifacts::test_metrics_never_presents_an_endpoint_id_as_the_model`——它从产物里现取请求 id，断言它在 `metrics.json` 里**只**出现在 `requested_model_id` 上。

---

## 7. 如何自己验证这份文档里的数字

```bash
cd koxpilot

# ① 成本账来源必须是"非估算"，status 必须是 ok（不是 llm_not_run）
PYTHONPATH=src python -c "
import json; c=json.load(open('output/metrics.json'))['cost_audit']
t=c['token_account']
print('status =', c['status'])
print('source =', t['source'])
print('calls',t['measured_calls'],'total',t['total_tokens'],
      'prompt',t['prompt_tokens'],'completion',t['completion_tokens'],
      'reasoning',t['reasoning_tokens'],'per_call',t['tokens_per_call'],
      'extrapolated',t['extrapolated'])
for k,v in t['per_task'].items():
    print(' ',k,v['calls'],'calls / failed',v['failed_calls'],'/ items',v['items'],
          '/ tok',v['total_tokens'],'/ per_item',v['tokens_per_item'],'/ lat',v['avg_latency_ms'])"

# ② 三方案调用量对照 + 不用 LLM 的 F1 代价 + 外推标记
PYTHONPATH=src python -c "
import json; c=json.load(open('output/metrics.json'))['cost_audit']
for s in c['schemes']: print(s['scheme'], s['calls_total'], s['desc'])
print('reduction x', c['call_reduction_vs_full_llm'])
print(json.dumps(c['accuracy_cost_of_all_rules'],ensure_ascii=False))
print(json.dumps(c['scheme_token_extrapolation'],ensure_ascii=False))"

# ③ Prompt 横评：控制变量声明 + 三版假设 + 6 个 arm 全表
PYTHONPATH=src python -c "
import json; b=json.load(open('output/prompt_bench.json'))
print('样本',b['n_samples'],'正例',b['n_positives'],'正例率',b['positive_rate'],'耗时',b['elapsed_s'])
print('控制变量:',b['control_variables'])
for v in b['prompt_variants']: print(v['version'],v['name'],'| chars',v['system_prompt_chars'],'|',v['change'])
for a in b['arms']:
    print(f\"{a['prompt_version']:5s} {a['model_key']:6s} P={a['precision']} R={a['recall']} F1={a['f1']} \"
          f\"tok={a['total_tokens']} reasoning={a['reasoning_tokens']} lat={a['avg_latency_ms']}ms\")
print('版本均值',json.dumps(b['version_average'],ensure_ascii=False))
print('best',json.dumps(b['best_arm'],ensure_ascii=False),'规则基线',b['rule_baseline_f1'])"

# ④ 生产 prompt 与 v3 的字符数差异（§4.5 那处不一致）
#    预期输出：production 1553 / v1 544 / v2 831 / v3 1607
PYTHONPATH=src python -c "
from koxpilot.llm.prompts import _tag_system
from koxpilot.llm.prompt_variants import TAG_VARIANTS
print('production', len(_tag_system()))
for v in TAG_VARIANTS: print(v.version, len(v.system))"

# ⑤ 复跑 LLM（需要 key；跑完会自动重跑 eval 接入 token 账）
make llm
make promptbench

# ⑥ 反泄漏：prompt 全文体检（确认答案没被写进 prompt）
PYTHONPATH=src python -m pytest tests/test_no_leakage.py -q
```

---

**上一篇** ← [03 · 评测方法与结果](03-evaluation.md) ｜ **下一篇** → [05 · 工程边界与局限声明](05-boundaries.md)
