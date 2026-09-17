# 05 · 工程边界与局限声明

> 这份文档的目的不是免责，而是**把"哪部分是真算的、哪部分是造的、哪部分我知道不行"写在读者最容易看到的地方**。
>
> 判断一个作品集项目是否可信，最快的方法不是看它宣称了什么，而是看它敢不敢主动说自己不行在哪。README 给的是结论；这份文档给的是结论的适用范围。
>
> 相关文档：[01 架构](01-architecture.md) · [02 门禁规则](02-gates.md) · [03 评测方法](03-evaluation.md) · [04 Prompt 与成本](04-prompt-and-cost.md)

---

## 0. 一句话总结边界

> KOXPilot 证明的是**"这套判定/分配/评测的方法论能落地成可复算的代码"**，
> 不证明"这些数字能迁移到真实平台的达人库上"。
> 前者靠固定种子 + 逐字节复现 + 双实现比对来担保；后者需要真实数据，我没有，也不假装有。

---

## 1. 真实性总表（每行都能自己验）

| 组成部分 | 真实性 | 证据落在哪 | 自己验的命令 |
| --- | --- | --- | --- |
| 达人数据（5,000 条） | **合成** | `data/kox_5000.json`，seed=20270919，sha256 `29500afd5390f3fd9…` | `make data` 后比对 sha256 |
| 分位数阈值（25 组） | **真实计算** | `output/thresholds.json`，无监督标定，不读 `gt` | `make gate` |
| G0/G1/G2/G3 判定 | **真实计算** | `output/verdicts.json`（5,000 条）+ `output/gate_results_sample.json`（前 200 条带完整证据链） | `make gate` |
| 预算分配 | **真实计算** | `output/budget.json`，约束校验字段 `all_enforced_satisfied` | `make budget` |
| 评测指标（六张表） | **真实计算** | `output/metrics.json`，不含时间戳，可逐字节回归 | `make eval` |
| 浏览器内门禁/预算复算 | **真实计算** | TS 独立实现，与 Python 逐条比对 `web/public/data/consistency.json` | `make web` |
| A1 brief 解析 | **真实 LLM，构建期** | `output/llm_cache.json.brief_specs`、`output/llm_bench.json` 中 `brief::ark` / `brief::azure` 各 3 calls | `make llm`（需 key） |
| A4 语义适配打分 | **真实 LLM，构建期** | `llm_cache.json.fit_scores`（每 brief 260 条）、`llm_bench.json` 中 `fit::ark` 66 calls | `make llm` |
| 标签错配横评 | **真实 LLM，构建期** | `output/prompt_bench.json`（600 样本 × 3 版 prompt × 2 模型） | `make promptbench` |
| token / 成本数字 | **真实 usage** | `metrics.json.cost_audit.token_account.source` 明写「各 API 返回的 usage 字段，非估算」 | `make eval` 后读该字段 |
| 生产环境部署 / 真实投放回收 | **没有** | —— | —— |

**读表方法**：凡是"真实计算"，任何人 clone 下来跑 `make all` 都应该得到**逐字节相同**的产物；凡是"真实 LLM"，需要自己配 endpoint 才能复跑，仓库里留的是调用代码 + 固化结果 + 真实 usage 账；凡是"合成"，数字只在这份数据集的假设下成立。

---

## 2. 边界一：数据是我自己造的

### 2.1 为什么必须造

要评测"水号识别准不准"，就必须知道谁是水号。真实平台数据没有这个标签——**没有 ground truth 就只能给出"我们拦了 807 个人"这类无法证伪的说法**。所以我选择：牺牲数据真实性，换取评测可证伪性。

生成器（`src/koxpilot/datagen/`）注入 4 类造假：买粉、互赞群、机器评论、刷播放，`gt.verdict` 由注入计划按 `reject > review > pass` 优先级合成，测试里逐条重算校验（`tests/test_dataset_reproducibility.py`）。

### 2.2 这带来什么后果（诚实版）

- **指标上限由生成器的假设决定。** 我在生成器里认为"买粉会让互动率偏低 + 粉丝曲线出现无爆款支撑的突刺"，门禁 G1.2/G1.5 恰好查这两件事。这不是循环论证（判定逻辑物理上读不到 `gt`，见 [03 §3](03-evaluation.md#3-物理隔离让读到答案这件事在代码层面不可能)），但**是同一个人的两次假设**——如果真实平台的买粉行为不长这样，G1 的召回会掉。
- **最弱的那一类恰好说明了这点。** 刷播放（view_inflation）严口径召回只有 **0.4733**，是四类里最低的；因为我给它的注入信号只体现在 `view_follower_ratio` 一个维度上，而 G1.4 用的是同组 P98 上尾——真实号里天然存在爆款高播放，这两者在特征空间里本来就重叠。这个数字我没有去调高，它就是这套特征该有的样子。
- **不能外推。** 「严口径 F1=0.7200 / AUC=0.8809」是**在这个数据集上**的数字。任何把它读成"KOXPilot 能识别 72% 的真实水号"的说法，都是我不背的。

### 2.3 我做了什么来降低"数据太好做"的风险

- 天然极端值**只**注入在非水号身上——保证假阳性来源真实存在，否则 precision 会虚高。
- 标签错配的正例不做"整组替换品类"，而保留中间带：正例 Jaccard 落在 `(0.15, 0.85)` 的比例 39.2%，恰好为 0 的占 60.0%。测试 `test_naive_jaccard_rule_is_far_from_perfect` 强制"扫 49 个阈值的最佳 F1 必须 < 0.90"——把"送分题"这件事钉成了会失败的断言。
- 这条守卫是**有历史的**：早期版本正例几乎全 0、负例几乎全 1，一条朴素规则就能拿满分，各消融 arm 指标完全相同。详见 [03 · 反自证](03-evaluation.md#21-第一道网标签错配的-f1-曾经是-10)。

---

## 3. 边界二：LLM 只在构建期跑，运行期是确定性的

### 3.1 事实

| 阶段 | 是否调模型 | 说明 |
| --- | --- | --- |
| `make data` / `gate` / `budget` / `eval` | **否** | 全确定性，零外部依赖，无 key 也能跑完 |
| `make llm` / `make promptbench` | **是** | 构建期批量推理，结果固化到 `output/llm_cache.json`、`output/llm_bench.json`、`output/prompt_bench.json` |
| 前端 Demo | **否** | 读固化产物 + 浏览器内重算门禁与预算 |

三个理由（主动交代，不是被追问才说）：

1. 我用的模型 endpoint 在内网，公网访问者调不通——**在线调用会让 Demo 对评审直接不可用**。
2. 真实广告系统同样把语义类判定做离线批处理 + 缓存，这是成本与延迟约束下的生产实践。
3. 仓库提供 provider 无关适配层，配任意 OpenAI 兼容 endpoint 即可复跑全流程（`.env.example` 里只有占位符，内网域名与 key 一行都没进仓库）。

### 3.2 这里有一个我必须点明的落差

**`metrics.json` 六张表里报告的 `fit_score`，用的是规则版，不是 LLM 版。**

`llm_cache.json` 里确实有每个 brief 260 条 LLM 打的适配分（`fit_scores`），但当前 CLI 的 `budget` / `eval` 路径读的是 `data/briefs.json` 的确定性 spec 和 `g2.py` 里的 `rule_fit_score`——**没有把 LLM 的 fit 分注入正式指标链路**。

- 为什么：正式指标必须逐字节可复现，而 LLM 结果依赖 key 与模型版本，一旦注入，"跑 `make all` 得到相同产物"这个担保就没了。
- 代价：A4 这个 Agent 在正式指标里是"规则实现 + 离线 LLM 实验"，而不是"LLM 在线上跑"。表 6（`table_6_llm_vs_rule`）是这个 LLM 能力的**唯一**量化落点：同 600 样本下规则 F1=0.5625、LLM 最佳 arm F1=0.6824。
- 我没有为了文档好写去改这条链路。想改的正确做法是加一个显式的 `--use-llm-fit` 开关并单独出一份 metrics，而不是悄悄换默认值。

### 3.3 单次运行的波动没有被消掉

`make llm` 全程 172 次调用、耗时 939.4s、**1 次失败**（`tag::azure`）。失败的批次按"未覆盖"处理，不用默认值填充——因为拿 0.5 之类的中性值填空会让指标看起来更平滑，实际是在编数据。代价是那一批样本的 LLM 覆盖率略低于 100%。

模型 run-to-run 波动同样存在（temperature 不为 0 的 arm 会抖），我只跑了一轮，**没有做多次重复取均值/方差**。所以表 6 里 ark F1=0.6809 vs azure F1=0.6824 这 0.0015 的差距，**不构成"azure 更好"的结论**，只能读成"两个模型在这个任务上打平"。

---

## 4. 边界三：报告的三分类指标只覆盖 15/20 条规则

这是这份文档里最容易被忽略、但最该被追问的一条。

`metrics.json` 表 2 的三分类（accuracy=0.6590 / macro_f1=0.6731）跑在**库级中性口径**上——即 `CampaignSpec()` 空 spec，因为 `gt.verdict` 是"这个人本身有没有问题"，与具体单子无关，只有中性口径才能和它对齐。

后果：**5 条 campaign 相关规则在这个口径下一次都没触发。**

实测（`verdicts.json` 全库 5,000 条，中性口径命中率）：

| 规则 | 中性口径命中 | 占比 |
| --- | --- | --- |
| G0.1 / G0.2 | 221 / 229 | 4.42% / 4.58% |
| G1.1–G1.7 | 292 / 420 / 339 / 342 / 326 / 406 / 323 | 5.84%–8.40% |
| G2.1 | 727 | 14.54% |
| G2.2 | 1,591 | **31.82%** |
| G2.5 | 242 | 4.84% |
| G3.1 / G3.2 / G3.4 | 125 / 350 / 105 | 2.50% / 7.00% / 2.10% |
| **G2.3 / G2.4 / G2.6 / G3.3 / G3.5** | **0** | **0%（口径决定，不是没写）** |

把 spec 换成真实 campaign 再跑一遍（`evaluate_all(records, spec, thresholds)`，三个 brief 全跑），这 5 条立刻活过来：

| 规则 | BRIEF-001 | BRIEF-002 | BRIEF-003 |
| --- | --- | --- | --- |
| G2.3 语义适配不足 | 1,863 | 1,764 | 1,794 |
| G2.4 语言不匹配 | 2,952 | 2,556 | 2,372 |
| G2.5 受众地域偏离 | **3,993** | **4,389** | **4,240** |
| G2.6 人群画像重叠低 | 173 | 166 | 167 |
| G3.3 竞品排他期 | 329 | 177 | 182 |
| G3.5 受管制品类 | 0 | 0 | 279（`regulated_category=kids`） |
| **判定分布** | pass 155 / review 3916 / reject 929 | pass 85 / review 4039 / reject 876 | pass 138 / review 3994 / reject 868 |

三个必须承认的结论：

1. **campaign 口径下 pass 率只有 1.7%–3.1%**（155 / 85 / 138 人）。放到真实运营里，这种通过率意味着"门禁太严，人核队列会爆"。这是产品可用性问题，不是 bug，但我没有为了数字好看去松阈值。
2. **G2.5（受众地域）是最大的 review 制造机**，campaign 口径下命中 80%–88%。原因是合成数据的 `audience_geo` 分布相对分散，而门槛是"自称市场在受众地域中占比 ≥ 35%"。真实达人的受众集中度通常更高，这条在真实数据上不会这么暴力——但**在当前数据集上它确实吞掉了大部分候选**。
3. **表 2 的 macro_f1 不能被解读为"整套门禁的准确率"**，它只是 15 条规则在中性口径下对齐 `gt.verdict` 的能力。剩下 5 条 campaign 规则**没有对应的 ground truth，也就没有被评测过**。

---

## 5. 边界四：分位数阈值会退化，且我留着不修

`gate_results_sample.json` 里有这么一条真实证据：

```
[G3.2] soft w=0.300 | 中低危内容累计风险载荷 7.5 分，高于同组 P90 门槛 0.0 分。
       signal=risk_severity_score actual=7.5 threshold=0.0 source=quantile:group:youtube|micro
```

**门槛是 0.0。** 因为绝大多数达人的风险载荷本来就是 0，同组 P90 自然落在 0——于是"高于 P90"退化成了"只要有任何中低危标记就命中"。这条规则此刻实际上是个 `> 0` 的硬编码，动态阈值的部分名存实亡。

- 影响面：G3.2 全库命中 350 条（7.00%），severity 是 soft，不参与 reject 路径，所以对 `reject` 结论无影响；但它会把人推进 review。
- 为什么不修：能修的方式（比如"P90 为 0 时回退到绝对阈值 2.0"）会让阈值来源从"数据标定"变成"我拍的数"，而**我拍的数在这份合成数据上一定比分位数好看**。我宁愿留下一条"人话解释里写着门槛 0.0、一眼看出退化"的证据链，也不愿留下一个看不出来的魔法常数。
- 一般化的教训：**分位数阈值在零膨胀分布上不成立**。任何用 pXX 做门槛的系统都该有"该分位是否退化"的守卫，我现在没有。

同类还有一处：`thresholds.json` 里 25 个 `(platform|follower_bucket)` 组、72 个回退单元格，组样本不足 `MIN_GROUP_SAMPLES=60` 时回退 platform / global 级。回退这件事本身是设计，但**回退后阈值的适用性下降没有被量化**——`source` 字段只如实标注了级别（`quantile:group:` / `quantile:platform:` / `quantile:global:`），没有给出置信度折损。

---

## 6. 边界五：阈值敏感性没有通过自己设的稳健线

`metrics.json.table_5_sensitivity`：把所有阈值同时 ±20% 扰动，**最大 F1 偏移 0.0582**，`stable=false`。

我设的稳健线是 5%（超过即判 false）。0.0582 只超了一点点，但**它是 false 我就写 false**，不改线也不藏字段。含义：这套门禁的绝对指标对阈值缩放是敏感的，报告的 F1=0.7200 应该被读成"约 0.72 ± 0.06"这个量级，而不是四位有效数字的精确结论。

为什么不去优化：让它稳定的最直接手段是**把规则做钝**（阈值取更极端的分位，只抓最明显的水号），这样扰动 ±20% 也不改变判定——代价是召回崩掉。我选了敏感但有召回的版本，并把敏感性摊在指标里。

---

## 7. 边界六：预算分配是启发式，不是最优解

`src/koxpilot/budget/allocator.py` 的方法是"边际性价比贪心 + 结构约束修正"，**不是**求解器意义上的最优。

- 采购单位是**内容条数**不是人：同一达人最多买 3 条（`MAX_POSTS_PER_KOX=3`），第 n 条边际价值按 `POST_MARGINAL_DECAY=0.7` 的幂衰减。
- 贪心的合理性前提是价值函数在条数上凹（decay ∈ (0,1] 时成立，代码里有断言）；但**叠加了 5 条结构约束后（单达人 ≤20%、头部 ≤45%、长尾 ≥25%、单国家 ≤60%、利用率 ≥90%），贪心 + 修正不再保证最优**。我没有做 MILP 对照，所以**说不出"离最优差多少"**。
- `POST_DECAY_SCAN = (0.5, 0.7, 0.9)` 这个常量在 `budget/policy.py` 里定义了，注释也说会给三档结果，但**当前 `metrics.json.budget` 里没有对应的扫描输出**——即 decay=0.7 这个关键参数**没有敏感性证据**。这是代码与注释不一致，我把它写在这里而不是删掉注释。<!-- TODO: 待核实是否需要补 decay 扫描，或改注释 -->
- 缺报价的达人用同组 `avg_cpm_usd` 的 P50 估价（`n_price_estimated` 字段如实上报估价人数），估价误差没有传导到方案置信度上。

### 反事实价值审计的边界

`metrics.json.counterfactual_value_audit`：三个 campaign 合计预算 $245,000，KOXPilot 臂相比"按粉丝量选人"基线**少浪费 $79,718.55（占总预算 32.54%）、有效曝光 +98.64%**（6,782,758 → 13,473,202）。

必须同时说清五件事：

1. **浪费与有效曝光一律按 `gt` 算**，不用引擎自己的分数当裁判（`eval/audit.py` 里写死，`method.judge` 字段明写"防自证"）。否则就是"我说他是水号，所以我省了钱"的自我表扬。另外主口径下**水号曝光按 0 计**，同时给一份"按 50% 计"的宽松对照（`effective_views_gt_lenient`），两个口径都落盘。
2. ~~**基线弱得超出我原本的预期，这反而削弱了结论。**~~ **已补第三臂（本轮）。** 「按粉丝量降序买到花完」在三个 campaign 里分别只选了 **8 / 4 / 6 个人**（都是最贵的头部号），而 KOXPilot 臂选了 **69 / 39 / 73 个人**，所以 +98.64% 里确实混着"分散投放 vs 集中押注"的效应。现在补了 `diversified_no_gate` 臂（同一套结构配额、按每美元名义曝光排序、**仍不看门禁**），把差额拆成两段：
   - **定稿单种子**：分散化少浪费 $56,222.64（70.5%）、门禁与质量排序少浪费 $23,495.91（29.5%）；
   - **12 种子**：分散化 **$12,636 ± $38,983**（4/12 个种子为负、CI 跨 0）、门禁与质量排序 **$40,028 ± $12,560**（12/12 为正）。
   所以我原来的猜测（"增量主要来自分散投放"）在单种子上看似成立，跨种子后**被自己的数据推翻**：稳定的那一段是门禁。口径、可加性校验与三条反向事实（第三臂绝对曝光更高、人数不同量级、"门禁贡献"实为门禁+排序合计）见 [03 §4 附](03-evaluation.md)。
3. **BRIEF-002 是负例，我留在产物里，而且它的根因是"基线运气好"。** 该 campaign 里基线选的那 4 个头部号**恰好都是真号**（浪费仅 $375.93 = 0.84%），而 KOXPilot 选的 39 人里有 3 个水号（浪费 $5,493.63 = 12.21%）。对照 BRIEF-001：基线 8 人里有 5 个是水号，浪费率 66.98%，于是 KOXPilot 少浪费 $51,406.59（占预算 64.3%）。**同一个方法在两个单子上一个 +373.5%、一个 −2.3%，说明 n=4 的基线方差极大**——三个 campaign 的样本量根本不足以给出稳定的价值结论。
4. **三臂的候选池不完全相同。** 三臂共享同一批 `GateResult` 与同一套定向筛选（这是为了让差额归因干净），但 KOXPilot 臂只收 `pass`（`INCLUDE_REVIEW_BY_DEFAULT=False`），基线臂与第三臂收全部三档。这是刻意的（那两条臂就该不看门禁），代价是"池子大小差异"这一项被算进了第三臂 → KOXPilot 那一段（即 `by_gating_and_quality_ranking`），它和"排序依据换成质量加权价值"无法再拆开。
5. **只有一个粉丝量基线 + 一条归因隔离臂。** 仍没有"按 CPM 排序""按互动率排序"等更强对照（第三臂的排序是"每美元名义曝光"，本质上就是 CPM 最优，但它同时叠了结构配额，不能当纯 CPM 基线读）。所以正确读法是"相比最粗暴的做法 + 相比只会分散不会风控的做法"，不是"相比专业买手"。

---

## 8. 边界七：成本账的覆盖范围

`metrics.json.cost_audit.status = "ok"`，`token_account.source` 原文：**「output/llm_bench.json（各 API 返回的 usage 字段，非估算）」**。

实测总账：**172 次调用、1,103,473 total tokens**（prompt 536,827 / completion 566,646，其中 reasoning 403,202），平均 6,415.5 tokens/call。

边界：

- 这个账**只覆盖 `make llm`（brief + tag + fit）**，不含 `make promptbench` 的 3 版 × 2 模型横评（后者 token 单独记在 `prompt_bench.json` 的各 arm 里，v1/v2/v3 分别 434,018 / 467,327 / 492,460）。两处不要相加当"项目总消耗"。
- `cost_audit` 里凡是从实测外推到"全库 5,000 条"的字段，都显式带 `extrapolated=true`。**外推不是实测**，别混读。
- 有一个我今天真踩到的坑，值得写在这里：**`make llm` 跑完必须重跑 `make eval`**，否则 `cost_audit.status` 会停在 `llm_not_run`，成本账全是空的。现在 Makefile 的 `llm` target 已经在末尾自动串了一次 `cli eval`，但如果你是手工调 `python -m koxpilot.llm.runner`，这一步得自己补。详见 [04 · 成本](04-prompt-and-cost.md)。

---

## 9. 边界八：前端"真算"的范围有多大

这是"浏览器内实时复算"这个设计的可信度证据，也是边界的一部分——因为"前端真算"很容易变成"前端读一份写死的 JSON"。

`web/public/data/consistency.json`（由 `web/scripts/verify-parity.mjs` 在每次 `pnpm run refresh` / `make web` 时重新生成，页面直接读取，**不存在写死的数字**）：

| 项 | 值 |
| --- | --- |
| 比对样本 | 全库 **5,000** 条 |
| matched | **5,000** |
| diff_count | **0** |
| match_rate | **1.0** |
| 比对字段 | `verdict` / `group_key` / `rules[]` / `completeness` / `authenticity` / `consistency` / `brand_safety` / `fraud_score` |
| 证据链抽样 | 200 条逐条比对，`diff_records = 0` |
| 预算臂比对 | `compared_arms = 6`，`matched_arms = 6` |

两个实现读**同一份** `output/thresholds.json`，**TS 侧不做任何再标定**——所以这条比对证明的是"两套判定逻辑等价"，**不**证明"阈值标定也被复算了"。标定只有 Python 一份实现，它的正确性靠 pytest 保证，不靠双实现互证。

真算 / 读固化的分界：

- **浏览器真算**：四层门禁全部规则、证据链人话生成、预算分配与约束校验。
- **读固化产物**：合成数据集本身、分位数阈值表、LLM 的 brief 解析与适配分、六张表的评测指标、prompt 横评结果。

---

## 10. 已知的文档/代码不一致（我自己查出来的，没修的都在这）

诚实清单优先于整洁清单。以下都是当前仓库真实存在的，不一致方向一律**以源码和 `output/` 产物为准**。

| # | 不一致 | 现状 | 处理 |
| --- | --- | --- | --- |
| 1 | `01-SPEC.md` 写 G1.7（评论重复率）用绝对阈值 | 源码用**同组 P97**（实测样本 threshold=0.21256，`source=quantile:group:tiktok\|nano`） | 以源码为准，[02](02-gates.md) 里按实现写 |
| 2 | SPEC / 注释写 G1.5 只要 `z > 2.5` | 源码是**双条件**：z>2.5 **且**该月增速高于同组"最好月份"增速的 P90 | 以源码为准 |
| 3 | `datagen/config.py` 注释说"缺 1 个关键字段会制造 G0 漏检" | 已修：新增 **G0.2**（completeness 恰好 0.8 时转 review），漏检已堵，注释过期 | 注释待更新 |
| 4 | `prompt_variants.py` 注释称 v3 与生产 prompt 一致 | 实际**不完全一致**：v3 system 1,607 字符，生产 `_tag_system()` 1,553 字符（user message 一致） | 见 [04](04-prompt-and-cost.md)，横评结论按"v3 ≈ 生产"读，不能读成"等于" |
| 5 | `budget/policy.py` 注释说 metrics 会给 decay ∈ {0.5,0.7,0.9} 三档 | `metrics.json.budget` 里**没有**这个扫描 | 已在 §7 声明为"关键参数无敏感性证据" |
| 6 | `README.md` Prompt 表写 v2 ≈44 万 / v3 ≈45 万 token | 当前产物是 v2 **467,327** / v3 **492,460** | 以产物为准（我不改 README，已在最终汇报中提出） |
| 7 | `llm_cache.json` 有 A4 的 fit 分，但正式 budget/eval 不消费 | 见 §3.2 | 已声明，不偷偷接上 |
| 8 | `eval/harness.py` 声明"落盘产物不含生成时间，同代码+同数据必须逐字节相同" | `metrics.json`/`audit.json`/`verdicts.json`/`budget.json`/`multiseed.json` 确实逐字节可复现，但 `thresholds.json` 的 `meta.calibrated_at_unix` 是**墙上时钟**（`int(time.time())`），该文件因此无法逐字节回归对比 | 不改字段（改了就和已发布的 `output/thresholds.json` 对不上）；改成**可执行约束**：`tests/test_e2e_smoke.py` 实测"重跑两次后唯一变化的产物是 thresholds.json，且唯一变化的叶子是 `meta.calibrated_at_unix`"，并要求 `metrics.json` 里不得出现该字段 |

---

## 11. 明确**不**声称的事

避免读者好意地替我夸大：

1. **不声称**这些指标能迁移到真实达人库。数据是合成的（§2）。
2. **不声称**这是一个多 Agent 框架实现。6 个 Agent 是**概念编排**，源码里没有 `agents/` 目录、没有工具调用循环、没有多轮自主决策——是一条确定性流水线 + 构建期批量 LLM 调用。详见 [01 架构](01-architecture.md)。
3. **不声称** LLM 在线上跑。运行期零模型调用（§3）。
4. **不声称**预算方案最优。启发式贪心，无求解器对照（§7）。
5. **不声称**门禁指标覆盖全部规则。5/20 条 campaign 规则未被评测（§4）。
6. **不声称** azure 比 ark 好。0.0015 的 F1 差距在单轮实验里是噪声（§3.3）。
7. **不声称**这套东西上线能省多少钱。反事实审计是在合成数据 + 单一弱基线下的对照，且三个 campaign 里有一个是负例（§7）。

---

## 12. 要变成真东西，缺的是什么

按优先级，不按好写程度：

1. **接真实数据的最小闭环**：拿一批真实达人 + 人工标注的 review/reject 结论，重跑 G1/G2 的分位标定与阈值，看召回掉多少。这是唯一能推翻或确认 §2 全部结论的实验。
2. **campaign 规则的 ground truth**：G2.3–G2.6 / G3.3 / G3.5 需要"这个人适不适合这个单子"的人工标注，否则永远无法评测（§4）。
3. **强基线**：至少补"按 CPM 排序""按互动率排序"两条对照臂。~~归因隔离臂~~ 已补（`diversified_no_gate`，见 §7 与 [03 §4 附](03-evaluation.md)），它把差额拆成"分散化"与"门禁+质量排序"两段；仍缺的是把"门禁过滤"与"质量排序"再拆开的第四臂。另外 3 个 campaign 的样本量给不出稳定结论，至少要 20+ 个 brief。
4. **零膨胀分位守卫**：阈值标定时检测退化并显式降级（§5）。
5. **decay 敏感性扫描**：把 `POST_DECAY_SCAN` 真的跑出来（§7）。
6. **LLM 多轮重复实验**：至少 3 轮取均值与标准差，才谈模型/prompt 差异（§3.3）。
7. **在线推理路径**：给 A1/A4 做真在线调用 + 缓存 + 降级，并把"LLM 挂了怎么办"写成显式策略（现在是"失败按未覆盖处理"，够诚实但不够产品）。

---

## 13. 如何自己验证这份文档里的数字

```bash
cd koxpilot

# ① 确定性全链路（不需要任何 API key，产物应逐字节可复现）
make all                    # data -> gate -> budget -> eval
sha256sum data/kox_5000.json                    # 应为 29500afd5390f3fd9…

# ② §4 的规则命中率（中性口径 15 条 / campaign 口径 20 条）
PYTHONPATH=src python -c "
import json,collections
v=json.load(open('output/verdicts.json'))['verdicts']
c=collections.Counter(r for x in v for r in x['rules'])
print(dict(sorted(c.items())))"

PYTHONPATH=src python -c "
import collections
from koxpilot.eval.harness import load_eval_inputs
from koxpilot.gates.thresholds import Thresholds
from koxpilot.gates.engine import evaluate_all
from koxpilot.types import CampaignSpec
from koxpilot.io_utils import load_json, output_dir
recs, meta, briefs, sha = load_eval_inputs()
thr = Thresholds.from_dict(load_json(output_dir()/'thresholds.json'))
for b in briefs:
    spec = CampaignSpec.from_dict(b['spec'])
    res = evaluate_all(list(recs), spec, thr)
    print(spec.campaign_id, collections.Counter(r.verdict for r in res))
    print('  ', dict(sorted(collections.Counter(x for r in res for x in r.rule_ids).items())))"

# ③ §5 的 G3.2 阈值退化（门槛 0.0 的真实证据链）
PYTHONPATH=src python -m koxpilot.cli explain KOX-000014

# ④ §6 敏感性 stable=false / §8 双实现一致性 / §8 成本账来源
PYTHONPATH=src python -c "
import json; m=json.load(open('output/metrics.json'))
print(m['table_5_sensitivity']['max_abs_f1_shift'], m['table_5_sensitivity']['stable'])
print(m['cost_audit']['status'], m['cost_audit']['token_account']['source'])"
PYTHONPATH=src python -c "
import json; c=json.load(open('web/public/data/consistency.json')); print(json.dumps(c,ensure_ascii=False)[:400])"

# ⑤ §7 的负例 BRIEF-002
PYTHONPATH=src python -c "
import json; a=json.load(open('output/audit.json'))
print(json.dumps(a['counterfactual_value_audit'],ensure_ascii=False,indent=1)[:1200])"

# ⑥ 反数据泄漏 / 反自证的测试网
make test
```

---

**上一篇** ← [04 · Prompt 工程与成本决策](04-prompt-and-cost.md) ｜ **回到** [README](../README.md)
