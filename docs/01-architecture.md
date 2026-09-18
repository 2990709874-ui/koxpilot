# 01 · 系统架构与 6 Agent 编排

> 这份文档回答：**这个系统由哪些部件组成、数据怎么流、每个部件是真算的还是读固化的、以及为什么这么选**。
>
> 有一件事我在开头就说清楚，免得读者读到一半才发现：**这里的 6 个 Agent 是概念编排，不是某个 Agent 框架的实例**。源码里没有 `agents/` 目录、没有工具调用循环、没有多轮自主决策。它是一条**确定性流水线 + 构建期批量 LLM 调用**。理由和取舍在 §3。
>
> 相关文档：[02 门禁规则](02-gates.md) · [03 评测方法](03-evaluation.md) · [04 Prompt 与成本](04-prompt-and-cost.md) · [05 边界声明](05-boundaries.md)

---

## 1. 产品要解决的问题，决定了架构长什么样

### 1.1 输入输出

```
输入：一句话 brief
     「3C 小家电新品，投美加两国 TikTok + YouTube，预算 8 万美金，
       要 3C 数码和家居家电方向的达人，看有效曝光」

输出：一份可执行、可追责的达人采购清单
     ├─ 69 位达人 / 具体买几条内容 / 每条多少钱 / 合计 $79,855（预算利用率 99.82%）
     ├─ 每个被拦下的人：命中哪条规则、实际值多少、门槛多少、门槛来自哪
     ├─ 结构约束是否满足：单达人 ≤20% / 头部 ≤45% / 长尾 ≥25% / 单国 ≤60%
     └─ 反事实对照：相比"按粉丝量买"，少浪费多少钱、有效曝光差多少
```

### 1.2 为什么不是"达人搜索"

达人营销的痛点不在"找不到人"，在**投前判断**：这个号的粉丝是不是买的、标签是不是错的、受众是不是在目标市场、这笔钱按什么依据分。这些判断今天大量靠人肉 + Excel + 供应商口头保证。

所以 KOXPilot 的架构重心不是检索，是**判定 → 定价 → 分配 → 审计**这条链。这直接决定了三个架构选择：

| 选择 | 而不是 | 因为 |
| --- | --- | --- |
| 输出**判定 + 证据链** | 输出健康分 / 排序列表 | 运营要能回答"为什么拦他"，法务要能追责 |
| 判定用**确定性规则 + 统计** | 端到端交给 LLM | 判定必须可复算、可回归、可审计（见 §7.1） |
| 采购单位是**内容条数** | 人 | 真实合作按条计价，"选中 69 人"不是可执行的采购单 |

---

## 2. 6 Agent 编排：概念、源码位置、真实性

| Agent | 职责 | 源码位置 | 实现方式 | 落盘产物 |
| --- | --- | --- | --- | --- |
| **A1 BriefAgent** | 自然语言 brief → `CampaignSpec` | `llm/runner.py::run_brief` + `llm/prompts.py` | **真实 LLM（构建期）** | `llm_cache.json.brief_specs` / `campaign_specs`；账在 `llm_bench.json` 的 `brief::ark` 3 calls、`brief::azure` 3 calls |
| **A2 RecallAgent** | 定向筛选 + 候选召回 + 报价估算 | `budget/value.py::build_candidates` / `targeting_reason` | 确定性规则 | `budget.json` 的 `candidate_pool`（93 / 50 / 79） |
| **A3 GateAgent** | G0/G1/G2/G3 四层门禁 | `gates/engine.py` + `g0..g3.py` + `thresholds.py` | 确定性规则 + 统计标定 | `thresholds.json` / `verdicts.json` / `gate_results_sample.json` |
| **A4 FitAgent** | 语义适配打分 | `llm/runner.py::run_fit`（LLM）+ `gates/g2.py::rule_fit_score`（规则）+ `eval/llm_fit.py`（两者的对照与注入反事实） | **真实 LLM（构建期，离线实验）+ 规则（正式链路）** | `llm_cache.json.fit_scores`（每 brief 260 条）；`fit::ark` 66 calls；`metrics.json.table_6_llm_vs_rule.semantic_fit_llm_vs_rule` |
| **A5 BudgetAgent** | 预算分配 + 结构约束 + 基线对照 | `budget/planner.py` / `allocator.py` / `policy.py` | 确定性优化（启发式） | `budget.json` |
| **A6 AuditAgent** | 反事实价值审计 + 成本审计 | `eval/audit.py` | 确定性计算（读 `gt` 当裁判） | `metrics.json.counterfactual_value_audit` / `cost_audit`；`audit.json` |

**A4 有一处必须点明的落差（本轮已量化）**：`llm_cache.json` 里确实有 LLM 打的 260×3 条适配分，但 `make budget` / `make eval` 走的是 `rule_fit_score`，**没有把 LLM fit 注入正式指标链路**。

以前这句话到"没注入"就结束了，属于无法验证的自述。现在 `eval/llm_fit.py` 把它做成了可审计的对照，产物在 `table_6_llm_vs_rule.semantic_fit_llm_vs_rule`，并且"要不要升格为正式口径"由三条现算判据决定，而不是由我写死：

- **覆盖率 14.9%**（33/222 人）：注入会让 `fit_score` 变成 LLM/规则混合口径，产物里 `fit_source_mix_in_pool` 直接把混合比例写出来；
- **双算 97.1%**：「LLM 判低而规则判高」的 517 人里，绝大多数早已被定向筛选 / G2.4 语言 / `audience_match` 乘子各扣过一次；
- **单向偏置**：被覆盖的 33 人上 LLM 分只会更低、没有一个更高。

三条判据全过时，产物里的 `decision` 会自动变成 `llm_fit_promotable`（`tests/test_llm_fit_audit.py` 用一份构造缓存钉住了这个翻转）。当前结论是**方案 B：正式链路继续用规则版**。注入的离线反事实也真跑了：判定只翻转 1 条、合计少浪费 $735，样本 33 人、方向在 campaign 间还翻转，所以这个数字**不作为任何招牌结论**。详见 [05 §3.2](05-boundaries.md#32-这里有一个我必须点明的落差本轮已量化并给出不予升格的判据)。

---

## 3. 关于"Agent"这个词，我用得有多严格

### 3.1 事实

源码里**没有** agent 抽象类、没有 planner/executor 循环、没有 tool registry、没有 memory。所谓"6 个 Agent"是**职责划分的命名**：每个 Agent 对应一个模块边界清晰、输入输出契约明确的处理阶段。

真实的执行图是一条 DAG：

```
brief.json ──A1──▶ CampaignSpec ──┬──A2──▶ 候选池 ──┐
                                  │                 │
kox_5000.json ──A3(标定)──▶ thresholds.json          │
        └──────A3(判定)──▶ GateResult × 5000 ────────┼──A5──▶ BudgetPlan
                                  │                  │            │
                            A4 fit_score ────────────┘            │
                                                                  ▼
                                                          A6 审计 ──▶ metrics.json
```

### 3.2 为什么不做成真 Agent 循环

诚实的回答是：**这个问题不需要**，硬做会更差。

1. **流程是已知的、固定的。** 「解析需求 → 筛人 → 判质量 → 定价 → 分配 → 审计」是投放团队既有的 SOP，不存在需要模型临场规划的部分。让 LLM 去决定"下一步做什么"只会引入不确定性和额外 token。
2. **判定必须可复算。** 门禁结论要能被运营质询、被回归测试锁定。Agent 的自主决策路径每次都可能不同，`make all` 就没法产出逐字节相同的产物，[03](03-evaluation.md) 里那套"逐字节回归"的评测纪律直接失效。
3. **LLM 该用在它真有优势的地方。** 我实测了它在标签错配判定上的表现：同 600 样本下规则 F1=0.5625、LLM 最佳 arm F1=0.6824——**这才是该用 LLM 的地方**（语义理解），而不是流程控制。详见 [04](04-prompt-and-cost.md)。

所以我的立场是：**"Agent"应该用来描述职责边界，而不是用来描述实现是否调用了某个框架。** 如果面试官认为这不算 Agent 系统，我接受这个判断，但我更愿意为"哪里该用 LLM"这个决策给出实测依据，而不是为了名头加一层 orchestrator。

---

## 4. 三个执行域：什么时候跑什么

这是理解整个架构最关键的一张表——**它决定了"这个 Demo 能不能被评审跑起来"**。

| 执行域 | 触发 | 是否调 LLM | 是否需要 key | 产物 |
| --- | --- | --- | --- | --- |
| **构建期（我跑，一次性）** | `make llm` / `make promptbench` | **是** | 是 | `llm_cache.json` / `llm_bench.json` / `prompt_bench.json` |
| **离线确定性（任何人可复跑）** | `make all`（= `data`→`gate`→`budget`→`eval`） | 否 | **否** | `kox_5000.json` / `thresholds.json` / `verdicts.json` / `budget.json` / `metrics.json` / `audit.json` |
| **线上 HTTP 服务（访客点一次「开始计算」）** | `api/`（FastAPI，4 个端点，契约见 `api/CONTRACT.md`） | **A1 可选**：服务端配了凭据才调，失败即回落规则版 | 访客不需要；服务端可配可不配 | `plan` / `explain` / `gate.batch` 响应，附 `parity_payload.verdicts` 供浏览器对撞 |
| **浏览器（访客打开页面）** | `make web` 构建后的静态站 | 否 | 否 | 前端实时重算门禁与预算；服务不可达时独立完成全流程 |

四条设计后果：

1. **评审不需要任何 key 就能验证核心结论。** 门禁、预算、六张表全是确定性的，clone 下来 `make all` 应得到逐字节相同的产物。
2. **LLM 能力不是"我说有"，而是有账。** 构建期 172 次调用、1,103,473 tokens 全部来自各 API 返回的 `usage` 字段（`cost_audit.token_account.source` 明写"非估算"）。
3. **线上两条通道不是冗余，是互为证据。** 服务侧 `api/koxpilot_service/` 不含任何门禁/预算判定逻辑，它 import 的就是本表第二行那套代码；浏览器侧是独立的 TS 第二实现。`/api/plan` 把本次召回集里每个 `kox_id` 的判定放在 `parity_payload.verdicts` 里回传，浏览器重算后逐条比对——线上每次点击都在做 §8 那件事，而不只是构建期做一次。
4. **踩过的坑，写在这里而不是藏起来**：`make llm` 跑完**必须重跑 `make eval`**，否则 `cost_audit.status` 停在 `llm_not_run`，成本账是空的。Makefile 的 `llm` target 现在末尾自动串了一次 `cli eval`；手工调 `python -m koxpilot.llm.runner` 的人得自己补这一步。

---

## 5. 数据契约：三个 dataclass 撑起整条链

模块之间不传裸 dict，传三个有 schema 的对象（`src/koxpilot/types.py`）。这不是洁癖——**契约明确才能让前端 TS 独立实现同一套逻辑并逐条比对**（§8）。

### 5.1 `CampaignSpec`（A1 输出，全链路输入）

关键字段：`campaign_id` / `name` / `target_countries` / `target_platforms` / `target_categories` / `budget_usd` / `kpi` / `target_age` / `target_gender` / `language` / `competitor_brands` / `regulated_category`。

三个真实 brief（`data/briefs.json`，确定性构造，LLM 解析结果单独存在 cache 里做对照）：

| ID | 场景 | 市场 | 目标品类 | 预算 | 特殊字段 |
| --- | --- | --- | --- | --- | --- |
| BRIEF-001 | 3C 小家电新品 | US / CA | `3c_digital` + `home_appliance` | $80,000 | TikTok + YouTube |
| BRIEF-002 | 国货彩妆出海 | ID / MY / VN | `beauty_care` + `fashion` | $45,000 | —— |
| BRIEF-003 | 休闲手游买量 | JP / KR / BR | `gaming_app` + `3c_digital` | $120,000 | `regulated_category=kids` |

**空 `CampaignSpec()` 是一个有意义的口径**，不是"缺参数"：它表示"库级中性判定"——只跑与 campaign 无关的规则，用于回答"这个人本身有没有问题"。评测表 2 必须用它才能和 `gt.verdict` 对齐。这个语义边界我用错过一次，代价是一张全是 1.0 的无意义表，复盘在 [03 §2.3](03-evaluation.md#23-第三道网表-6-的语义适配分曾经恒等于-10)。

### 5.2 `GateResult`（A3 输出）

结构见 [02 §0](02-gates.md#0-设计前提判定必须可追责)。这里只强调两个架构性质：

- **`to_dict()` 的字段集合被 pytest 精确锁定**，因为它是前端 TS 的契约。
- **序列化后不含任何裁判字段**（`gt` / `is_fraud` / `fraud_type` / `true_categories`），有专门的测试扫。否则前端和下游会"顺便"拿到答案。

### 5.3 `BudgetPlan`（A5 输出）

`selected[]`（每项含 `kox_id` / `posts` / `cost_usd` / `value`）/ `spent_usd` / `budget_usd` / `tier_mix` / `country_mix` / `est_cpm_usd` / `n_price_estimated` / `constraints`（含 `all_enforced_satisfied` 布尔）/ `skipped`。

---

## 6. A5 预算分配：这一层的设计比门禁更容易被忽略

### 6.1 决策变量不是"选不选这个人"

而是"**在这个人身上买几条内容**"（0..`MAX_POSTS_PER_KOX=3`）。第 n 条的边际价值按 `POST_MARGINAL_DECAY = 0.7` 的幂衰减——同一个达人连发 3 条，受众高度重叠，净增触达是递减的。

实现上把每个候选展开成 slot，按**边际性价比**（边际价值 / 边际成本）贪心，再做结构约束修正。

### 6.2 价值函数

```
value = avg_views × authenticity_discount × fit_score × audience_match × kpi_weight
```

- `authenticity_discount` 来自 A3 的 `authenticity_score`——**门禁的结论直接进价格**，这是"质量门禁有经济意义"的落点，而不是一个摆设标签。
- 报价缺失时用同组 `avg_cpm_usd` 的 **P50** 估价，估价人数如实上报在 `n_price_estimated`。
- 候选池默认**只收 `pass`**（`INCLUDE_REVIEW_BY_DEFAULT = False`）——review 的人要人核完才能进池，这是产品语义，不是筛数据。

### 6.3 五条结构约束（`budget/policy.py`）

| 约束 | 值 | 为什么 |
| --- | --- | --- |
| 单达人预算占比 | ≤ **20%** | 避免把一半预算押在一个号上 |
| 头部（macro + mega）占比 | ≤ **45%** | 头部 CPM 贵且转化未必好 |
| 长尾（nano + micro）占比 | ≥ **25%** | 强制保留长尾配比（真实投放的经验结构） |
| 单国家占比 | ≤ **60%** | 多市场 campaign 不能塌到一个国家 |
| 预算利用率 | 提示线 **90%** | 低于此提示"钱没花完" |

实测三个 campaign 的利用率都极高（**99.82% / 99.99% / 99.97%**），`all_enforced_satisfied` 全部为真：

| Campaign | 候选池 | 选中 | 花费 / 预算 | 利用率 |
| --- | --- | --- | --- | --- |
| BRIEF-001 | 93 | 69 | $79,855.29 / $80,000 | 99.82% |
| BRIEF-002 | 50 | 39 | $44,995.72 / $45,000 | 99.99% |
| BRIEF-003 | 79 | 73 | $119,966.67 / $120,000 | 99.97% |

利用率这么高不是巧合，是"按条买"带来的：内容条数是细粒度可拆的，最后剩几百美金还能再买一条 nano 的内容。如果按人买，尾差会大得多。

### 6.4 这个算法的诚实定位

贪心的凹性前提在 `decay ∈ (0,1]` 时成立（代码里有断言），但**叠加 5 条结构约束后，"贪心 + 修正"不再保证最优**，我也没有做 MILP 对照，所以说不出"离最优差多少"。另外 `decay=0.7` 是**建模假设不是观测值**，三档敏感性扫描已补进 `metrics.json.budget_decay_sensitivity`：合计少浪费的相对离差仅 1.4%、两段归因符号不翻转，**但选中名单会变**（金额加权重叠最低 0.651）——即结论稳、名单不唯一。都登记在 [05 §7](05-boundaries.md#7-边界六预算分配是启发式不是最优解)。

---

## 7. A6 审计：两个都得敢被追问

### 7.1 反事实价值审计

三条臂**共享同一批 `GateResult` 与同一套定向筛选**，差异只在候选池与选人依据：

| 臂 | 候选池 | 结构配额（分层/地域/单人） | 排序依据 |
| --- | --- | --- | --- |
| 基线 `baseline_followers` | pass+review+reject | 只受总预算约束 | 粉丝量降序 |
| 第三臂 `diversified_no_gate` | pass+review+reject | **强制** | 名义曝光/报价（不读门禁与真实性信号） |
| `koxpilot` | pass | **强制** | 质量加权价值/报价 |

有了中间那条臂，差额才拆得开：基线 → 第三臂 = **分散化贡献**，第三臂 → KOXPilot = **门禁与质量排序贡献**（链式差分，两段可加）。落在 `metrics.json.counterfactual_value_audit.value_attribution` 与 `per_campaign[*].value_attribution`。

审计纪律（`eval/audit.py` 里写死）：**浪费金额与有效曝光一律按 `gt` 算，绝不用引擎自己的分数**。否则就是"我说他是水号，所以我省了钱"的自我表扬。

结果：三个 campaign 合计 $245,000，少浪费 **$79,718.55（占总预算 32.54%）**、有效曝光 **+98.64%**；拆开看，分散化 **$56,222.64**、门禁与质量排序 **$23,495.91**（有效曝光率 67.30% →第三臂 86.48% → KOXPilot 94.23%）。

但这个数字有四处必须自己先说的水分：

- **单种子的归因比例不稳。** 定稿种子上分散化占了 70.5%，但 12 种子重跑后分散化只有 **$12,636 ± $38,983**（4/12 个种子为负、95% CI 跨 0），而门禁与质量排序是 **$40,028 ± $12,560**（12/12 为正）。所以"价值主要来自分散化"是**单种子的采样运气，不能当结论**；稳定的那一段是门禁。详见 [06 表 1 附](06-robustness.md)。
- **三臂人数不同量级**（基线 18 人 / 第三臂 516 人 / KOXPilot 181 人合计）。分散化贡献里本身就含"把钱摊到更多人"的大数效应——那正是这条臂要度量的东西，但不能拿人数本身论优劣。
- **第三臂的绝对有效曝光反而比 KOXPilot 高**（19.7M vs 13.5M，12/12 个种子都如此）：它按每美元曝光排序，专挑 CPM 最便宜的长尾。KOXPilot 优化的是质量加权价值（含语义适配/KPI 权重/真实性折扣），会主动放弃便宜但不对味的曝光。所以三臂可比的口径是**浪费金额与有效曝光率**，绝对曝光数不是 KOXPilot 的优化目标——这条写在产物的 `caveats` 里，不用"我们曝光也最多"糊过去。
- **BRIEF-002 是负例**（多浪费 $5,117.70、有效曝光 −2.3%），根因是基线那 4 个头部号**恰好都是真号**（浪费率 0.84%），而 KOXPilot 的 39 人里有 3 个水号（12.21%）——两段贡献在这条 campaign 上都是负的（分散化 −$3,319、门禁 −$1,799）。对照 BRIEF-001 基线 8 人里有 5 个水号（浪费率 66.98%）——**n=4 的基线方差极大**。

我把负例留在产物里没删。完整的边界讨论在 [05 §7](05-boundaries.md#反事实价值审计的边界)。

### 7.2 成本审计

`cost_audit.status = "ok"`，**只用 API 返回的 usage 字段**，凡是从实测外推到全库的字段一律显式带 `extrapolated=true`。方法论细节在 [04](04-prompt-and-cost.md)。

---

## 8. 双实现一致性：浏览器内实时复算的可信度证据

"前端实时重算门禁"这个卖点很容易注水成"前端读一份写死的 JSON"。所以我让 TS 侧**独立实现**四层门禁与预算分配，然后**逐条比对**。

`web/public/data/consistency.json`：

| 项 | 值 |
| --- | --- |
| 比对样本 | 全库 **5,000** 条 |
| matched / diff_count / match_rate | **5,000 / 0 / 1.0** |
| 比对字段 | `verdict` / `group_key` / `rules[]` / `completeness` / `authenticity` / `consistency` / `brand_safety` / `fraud_score` |
| 证据链抽样 | 200 条逐条比对，`diff_records = 0` |
| 预算臂比对 | `compared_arms = 6`，`matched_arms = 6` |

三个关键性质：

1. **这份 JSON 由 `web/scripts/verify-parity.mjs` 在每次 `pnpm run refresh`（即 `make web`）时重新生成**，页面直接读取——**不存在写死的数字**。要作弊就得改脚本，而脚本在仓库里。
2. **两个实现读同一份 `output/thresholds.json`，TS 侧不做任何再标定。** 所以这条比对证明的是"**两套判定逻辑等价**"，**不**证明"标定过程也被复算了"——标定只有 Python 一份实现，正确性靠 pytest 保证，我不把它算进双实现互证的范围。
3. `rules[]` 参与比对是重点。只比 `verdict` 太松：两个实现可能因为不同的规则命中而巧合得到同一个判定。比到规则 ID 级别，加上 200 条证据链的 `actual` / `threshold` 全等，才能说逻辑真的一致。

前端"真算 / 读固化"的分界：

- **真算**：四层门禁全部规则、证据链人话生成、预算分配与约束校验。
- **读固化**：合成数据集、分位数阈值表、LLM 的 brief 解析与适配分、六张表指标、prompt 横评结果。

---

## 9. 技术选型：三个"没有做"的决定

| 没做 | 常见做法 | 我的理由 |
| --- | --- | --- |
| **没用向量检索做达人召回** | embedding + ANN | 候选规模 5,000，定向筛选（国家/平台/品类）已经把池子压到 50–93 人。这个量级上向量检索解决不了任何实际问题，只增加不可解释性。真实规模（百万级）下我会加，但那时召回的瓶颈也不是相似度，是**冷启动达人的标签质量**——恰好是 G2 在管的事 |
| **没让 LLM 直接判水号** | prompt 里丢一个达人 profile 问"这是不是水号" | 水号判定的核心是"和同组同行比"，需要分位数分布；LLM 拿不到分布，只能凭常识给绝对判断。而且这条链要能回归、要能给运营解释门槛来自哪 |
| **没做在线 LLM 推理** | 页面上点一下调模型 | 我用的 endpoint 在内网，公网评审调不通——在线调用会让 Demo 直接不可用。真实广告系统同样把语义判定做离线批处理 + 缓存。仓库留 provider 无关适配层，配任意 OpenAI 兼容 endpoint 即可复跑 |

安全纪律：**内网 API key 与内网域名一行都不进代码 / 不进仓库 / 不进文档**，只有 `.env.example` 里的占位符。

---

## 10. 目录结构（按执行域读）

```
koxpilot/
├── src/koxpilot/
│   ├── cli.py              # data / gate / budget / eval / explain / all —— 只做"读文件、调库、写文件"
│   ├── types.py            # CampaignSpec / GateResult / BudgetPlan / Reason
│   ├── stats.py            # 分位数、MAD、prf1、roc_auc（自己实现，无 sklearn 依赖）
│   ├── taxonomy.py         # 品类词表 + 相邻品类图 + 市场语言表（同一份注入 prompt，见 04）
│   ├── datagen/            # ← 合成数据（config / generator / briefs / injections）
│   ├── gates/              # ← A3：engine / g0..g3 / signals / thresholds / policy / humanize
│   ├── budget/             # ← A2+A5：planner / allocator / value / policy
│   ├── eval/               # ← A6：harness / metrics / audit / llm_compare / llm_fit / sensitivity
│   └── llm/                # ← A1+A4：runner / promptbench / prompts / prompt_variants / provider / identity
├── data/                   # 合成数据集 + 3 个 brief（产物，可由 make data 重建）
├── output/                 # 全部指标与判定产物（可由 make all 重建，逐字节可复现）
├── tests/                  # 766 项，含反数据泄漏的 AST 静态扫描 + 运行期哨兵 + 文档数字防漂移
├── api/                    # ← 线上 HTTP 服务：CONTRACT.md（冻结契约）+ koxpilot_service/（只做编排）+ build_bundle/verify_bundle
├── web/                    # TS 独立实现 + 一致性比对脚本
└── docs/                   # 你正在读的这 6 份
```

一个刻意的设计：**CLI 只做"读文件、调库、写文件"**，所有业务逻辑在库里。这样 pytest 完全绕开 CLI 直接测函数，而 CLI 自身几乎不需要测试。同时每个子命令都打印**可核对的关键数字**而不是 "done"——构建期日志本身就是交付物的一部分，可以拿终端输出直接对照 `metrics.json`。

---

## 11. 如何自己验证这份文档里的数字

```bash
cd koxpilot
make help                 # 看清哪些 target 需要 key、哪些不需要
make all                  # data → gate → budget → eval，全确定性，无需任何 key

# ① §6.3 预算三个 campaign 的候选池/选中/利用率/约束
PYTHONPATH=src python -m koxpilot.cli budget      # 终端直接打印，可与 budget.json 对照
PYTHONPATH=src python -c "
import json; p=json.load(open('output/budget.json'))['plans']
for x in p:
    k=x['koxpilot']
    print(x['campaign_id'], k['candidate_pool'], len(k['selected']),
          round(k['spent_usd'],2), k['budget_usd'],
          k['constraints']['all_enforced_satisfied'])"

# ② §2 六个 Agent 的 LLM 调用账（哪个 Agent 花了多少 call）
PYTHONPATH=src python -c "
import json; b=json.load(open('output/llm_bench.json'))
print(json.dumps(b,ensure_ascii=False,indent=1)[:1200])"

# ③ §8 双实现一致性（这份 JSON 每次 make web 都会重新生成）
PYTHONPATH=src python -c "
import json; print(json.dumps(json.load(open('web/public/data/consistency.json')),ensure_ascii=False,indent=1)[:800])"

# ④ §7.1 反事实审计（含 BRIEF-002 负例）
PYTHONPATH=src python -c "
import json; a=json.load(open('output/audit.json'))['counterfactual_value_audit']
print(json.dumps(a,ensure_ascii=False,indent=1)[:1500])"

# ⑤ §5.1 三个 brief 的 spec
PYTHONPATH=src python -c "
import json; [print(b['spec']['campaign_id'], b['spec']) for b in json.load(open('data/briefs.json'))]"

# ⑥ §4 确定性可复现（同种子两次生成应逐字节相同）
sha256sum data/kox_5000.json && make data && sha256sum data/kox_5000.json

# ⑦ 全量测试（含反泄漏静态扫描 + 运行期哨兵）
make test
```

---

**下一篇** → [02 · 四层质量门禁规则手册](02-gates.md) ｜ **回到** [README](../README.md)
