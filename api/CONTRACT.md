# KOXPilot HTTP API 契约（v1.1）

> v1.1 相对 v1.0 只改一处口径：`options.top_n` 不再截断进入计算的候选池（见 §4），
> 并新增只读的 `scope` 块把这件事写进响应体（见 §4.2）。字段只增不减，v1.0 的调用方不需要改。

> 这份契约是前后端唯一的对齐依据。后端实现与前端调用**都必须**严格匹配本文件。
> 任何字段增删都要先改这里。

## 0. 总原则

1. **后端跑的是真链路**：`api/` 直接 import `koxpilot` 包，调用与 CLI 完全相同的函数
   （同一套阈值、同一套门禁规则、同一套预算分配器）。不是为接口重写一遍逻辑。
2. **浏览器端 TS 引擎不下线**：它从「唯一实现」降级为「第二实现 + 离线兜底」。
   两件事同时成立才是这个作品的卖点：
   - 后端可达时：前端展示 Python 真实计算结果；
   - 同一条 brief 也在浏览器里跑一遍 TS 引擎，**逐条比对判定结果**，实时给出差异条数。
3. **公开可访问**：不带任何鉴权头也能调用（校招评审是外部人）。
4. **LLM 是可选增强，不是必需依赖**：有凭据就真调模型做 brief 解析，没凭据就用确定性规则解析，
   并在响应里如实标明用了哪条路径。任何情况下接口都不能 500。

## 1. 基础约定

- Base path 一律以 `/api` 开头（部署网关按此前缀路由）。
- 所有响应 `Content-Type: application/json`，顶层为 object。
- 所有响应都带 `meta` 字段（见 §2）。
- 出错时返回 HTTP 200 + `{"ok": false, "error": {...}, "meta": {...}}`，
  只有真正的服务器崩溃才允许 5xx。前端据 `ok` 判断。
- CORS：允许所有来源、允许 `POST, GET, OPTIONS`、允许 `Content-Type` 头。

## 2. 公共 meta 结构

```json
{
  "meta": {
    "engine": "python",
    "engine_version": "1.1.0",
    "dataset_sha256": "29500afd5390…",
    "kox_count": 5000,
    "thresholds_source": "output/thresholds.json",
    "llm_runtime": {
      "available": false,
      "provider": null,
      "reason": "未配置模型凭据，A1 使用确定性规则解析"
    },
    "served_at": "2026-09-18T14:30:00+08:00",
    "elapsed_ms": 412
  }
}
```

`dataset_sha256` 必须与前端 `public/data/consistency.json` 里的 `dataset_sha256` 一致——
这是「前端看到的数据和后端算的数据是同一份」的证据。

## 3. `GET /api/health`

用途：前端启动时探活，决定用后端还是降级到浏览器引擎。**必须在 800ms 内返回。**

```json
{ "ok": true, "status": "ready", "meta": { … } }
```

冷启动尚未加载完数据集时返回 `"status": "warming"`，前端按不可用处理并稍后重试一次。

## 4. `POST /api/plan` （核心接口）

请求：

```json
{
  "brief_text": "巴西和墨西哥的 Instagram 彩妆，18-24 女性，预算 8 万美金，避开 Focallure",
  "brief_id": null,
  "options": { "top_n": 200, "explain_limit": 60 }
}
```

- `brief_text`：自由文本，必填（除非给了 `brief_id`）。
- `brief_id`：可选，取预置 brief（`data/briefs.json` 里的 id），此时忽略 `brief_text`。
- `options.top_n`：**展示用的明细条数上限**，默认 200，上限 500。
  它与 `explain_limit` 一样只裁「返回多少条候选明细」（实际条数取两者的较小值），
  **不裁进入计算的候选池**——命中定向的全部候选都进门禁与预算计算。
  > v1.0 里这个参数同时截断了召回集合，导致同一条 BRIEF-001 在服务上只花掉 7.1% 的预算、
  > 在离线 CLI 上花掉 99.8%（分配器的结构配额是在候选池上求解的，池被截掉之后
  > 「买得起又能满足配额」的库存也跟着消失）。截断展示是合理的，截断计算是错的，故本条口径已修正。
- `options.explain_limit`：返回多少条带完整证据链的达人，默认 60，上限 200。

响应：

```json
{
  "ok": true,
  "brief": {
    "source": "free_text",
    "parse_path": "rule",
    "notes": ["未配置模型凭据，A1 使用确定性规则解析"],
    "fields": [
      {
        "key": "budget_usd",
        "label": "预算",
        "value": 80000,
        "display": "$80,000",
        "status": "hit",
        "how": "从「8 万美金」识别为美元金额",
        "matched": "8 万美金"
      }
    ]
  },
  "funnel": [
    { "stage": "recall",  "label": "召回（命中定向的全部候选，计算不截断）", "count": 182 },
    { "stage": "gate",    "label": "通过门禁", "count": 7 },
    { "stage": "allocated","label": "进入清单","count": 4 }
  ],
  "scope": {
    "recall_total": 182,
    "computed_on": 182,
    "detail_rows": 60,
    "truncated_for_compute": false,
    "note": "命中定向的 182 人全部进入门禁与预算计算（召回不截断）…"
  },
  "candidates": [
    {
      "kox_id": "KOX-01234",
      "handle": "@example",
      "platform": "instagram",
      "market": "BR",
      "followers": 250000,
      "verdict": "pass",
      "gate_hits": [
        { "gate": "G2", "gate_label": "一致性", "signal_label": "多源标签互相冲突", "detail": "…" }
      ],
      "fit_score": 0.81,
      "fit_source": "cached_llm",
      "reason_human": "可投：三项信号均在阈值内…",
      "allocated_usd": 24000,
      "expected_reach": 180000
    }
  ],
  "allocation": {
    "budget_usd": 80000,
    "allocated_usd": 78400,
    "unallocated_usd": 1600,
    "unallocated_why": "剩余额度低于单人最低起投",
    "picked": 4,
    "arms": [
      {
        "arm": "koxpilot",
        "label": "KOXPilot 决策",
        "spend": 1249.75,
        "n_selected": 4,
        "judge": "ground_truth",
        "effective_views_gt": 67252.0,
        "effective_views_gt_per_dollar": 53.81,
        "effective_views_gt_lenient": 67252.0,
        "effective_views_gt_lenient_per_dollar": 53.81,
        "waste_usd": 0,
        "engine_expected_value": 45264.8,
        "engine_value_per_dollar": 36.22,
        "expected_value": 67252.0,
        "value_per_dollar": 53.81
      },
      {
        "arm": "follower_rank",
        "label": "按粉丝量排序",
        "spend": 79997.39,
        "judge": "ground_truth",
        "effective_views_gt": 3997286.0,
        "effective_views_gt_per_dollar": 49.97,
        "effective_views_gt_lenient": 4104928.0,
        "effective_views_gt_lenient_per_dollar": 51.31,
        "waste_usd": 4326.86,
        "engine_expected_value": 1094186.7,
        "engine_value_per_dollar": 13.68,
        "expected_value": 3997286.0,
        "value_per_dollar": 49.97
      }
    ],
    "judge": {
      "metric": "effective_views_gt_per_dollar",
      "judge": "ground_truth",
      "main_assumption": "主口径：标注为水号的达人，其曝光按 0 计入有效曝光",
      "lenient_assumption": "宽松口径：水号曝光按 50% 计入有效曝光。两个口径同向才说明结论不依赖该假设",
      "engine_score_role": "engine_value_per_dollar 是引擎的事前估分（选人排序依据），只作次级信息展示，不参与判优",
      "pipeline_separation": "选人与分钱（A1–A5）只读可观测字段，读不到标注；只有 A6 审计这一组对比读标注（静态扫描 + 运行期哨兵在守这条边界）"
    },
    "saved_usd": 21400,
    "saved_share": 0.267
  },
  "advice": [
    {
      "key": "expand_markets",
      "title": "加投相邻市场：AR、CO、US",
      "action": "目标市场从 BR、MX 扩到 AR、BR、CO、MX、US（相邻市场…），其余定向不动",
      "spendable_usd": 44540.97,
      "extra_spendable_usd": 43291.22,
      "utilization_after": 0.5568,
      "extra_picked": 24,
      "quality_note": "新进清单的 24 人同样逐条过了四层门禁…"
    }
  ],
  "timings": [
    { "agent": "A1", "label": "brief 解析", "ms": 12 },
    { "agent": "A3", "label": "四层门禁", "ms": 133 }
  ],
  "parity_payload": {
    "verdicts": [ { "kox_id": "KOX-01234", "verdict": "pass" } ]
  },
  "meta": { … }
}
```

**`parity_payload.verdicts` 是实时一致性比对的关键**：必须给出本次召回集合内
**每一条**达人的 `kox_id` + 最终判定（`pass|review|reject`），顺序按 `kox_id` 升序。
前端拿同一批 `kox_id` 在浏览器里跑 TS 引擎，逐条比对，算出差异条数。

字段口径要求：
- `gate_hits[].gate_label` / `signal_label` 必须是**中文人话**，不允许出现代码标识符。
- `reason_human` 直接复用 Python 侧 `gates/humanize.py` 的输出，保证与 CLI `explain` 一致。
- `fit_source` 取 `cached_llm` | `rule_fallback`，如实反映这条的语义适配分从哪来。
- `brief.source` 取 `free_text` | `preset`；`brief.parse_path` 取 `llm` | `rule` | `preset`
  （`preset` 表示这条 brief 的投放规格是构建期真调模型产出、固化在 `data/briefs.json` 里的，
  服务读取而不重新解析——这样它与前端读的那份 spec 逐字段相同）。
- `brief.notes`：字符串数组，可为空。装两类话——解析时发现的歧义与硬性要求，以及
  **A1 这次走的是模型还是规则、以及为什么**（没凭据 / 超时 / 报错都会写在这里）。
  前端可以不显示它，但服务端不允许把这句话吞掉：`parse_path` 说的是结果，`notes` 说的是原因。

### 4.1 三条臂：按「每美元」比，且以标注结果为裁判

`arms[]` 里三条臂的 `spend` 可以差几十倍（KOXPilot 只买门禁过关的人，基线臂会把预算花光），
所以**绝对值不可直接并排比较**；同时**判优不能用引擎自己的分数**——用引擎的估分给引擎打分是自证。
因此判优字段一律取 A6 审计按 ground truth 的结算（Python 侧 `eval/audit.plan_audit` +
`eval/audit.effective_view_calibers`，服务层不写第二套除法）：

| 字段 | 含义 |
| --- | --- |
| `judge` | 裁判来源，恒为 `"ground_truth"`（数据集标注，不是引擎分数） |
| `n_selected` | 这条臂实际拿到钱的达人数 = 要签、要沟通、要交付的**合约数**。是成本项，不参与判优——摊得越薄每美元越便宜，但报价、寄样、审稿、结算的成本不在判优指标里 |
| `effective_views_gt` | 主口径有效曝光：标注为水号的达人，其曝光按 **0** 计 |
| `effective_views_gt_per_dollar` | **判优字段** = `effective_views_gt / spend` |
| `effective_views_gt_lenient` | 宽松口径有效曝光：水号曝光按 **50%** 计（`FRAUD_RESIDUAL_VIEW_SHARE`） |
| `effective_views_gt_lenient_per_dollar` | 宽松口径的每美元有效曝光 |
| `waste_usd` | 这条臂花在真水号 / 真高风险号上的钱，与 `saved_usd` 同源（`wasted_spend_usd`），不另算一份 |
| `engine_expected_value` | 引擎**事前估分**合计（`budget/value.value(k)` 按等效条数加总） |
| `engine_value_per_dollar` | 每美元的引擎事前估分 |

- 两个"每美元"字段在 `spend` 为 0 时给 `null`，不允许写 0——「没花钱」和「花了钱没效果」是两件事。
- **两个口径都必须给出**：只给对自己有利的那个不算结论。两个口径的最高项一致时，才可以说
  结论不依赖「水号曝光是否完全作废」这个假设；不一致就照实说不一致。
- `engine_*` 是选人排序的依据，只能作为**次级信息**（前端收进折叠区），不参与本对比的判优。
- 兼容字段：`expected_value` 与 `effective_views_gt` 同数同义，`value_per_dollar` 与
  `effective_views_gt_per_dollar` 同数同义。保留是因为 v1 已经放出去了，新调用方请用
  `effective_views_gt*`。

前端主对比只允许用 `effective_views_gt_per_dollar` 与 `waste_usd`，并必须同时标明各臂花费不同。

**口径分离（`allocation.judge`）**：`judge` 块把「谁在判、按什么假设判、读标注的是哪一段」写进响应体，
不靠文档口头约定。硬边界是：选人与分钱（A1–A5：召回 / 语义适配 / 门禁 / 估价 / 分配）**只读可观测字段，
读不到 `gt`**；只有 A6 审计这一组对比读 `gt`。这条边界由 `tests/test_no_leakage.py` 的 AST 静态扫描
（生产模块不得出现 `gt` 访问）与运行期泄漏哨兵共同守住。

### 4.2 `scope`：这次计算用了多少人

| 字段 | 含义 |
| --- | --- |
| `recall_total` | 命中定向的人数 |
| `computed_on` | **真正进入门禁与预算计算的人数**；必须等于 `recall_total` |
| `detail_rows` | 本次返回的候选明细条数（= min(`explain_limit`, `top_n`, 实际条数)） |
| `truncated_for_compute` | 恒为 `false`；为 `true` 就意味着服务与离线 CLI 在算两道题 |

`parity_payload.verdicts` 覆盖的是 `computed_on` 这批人（不是 `detail_rows`）：
前端拿这批 id 交给浏览器 TS 引擎重算，少给一条，两侧算的就不是同一道题，
页面上的金额也会与 `allocation` 对不上。

### 4.3 `advice[]`：预算花不出去时的放宽建议

触发条件：`allocated_usd / budget_usd < 60%`。达到 60% 时 `advice` 为**空数组**（钱花得出去就不需要建议）。
最多三条，互相独立（每条都是「只改这一处」），按 `extra_spendable_usd` 降序：

| `key` | 放宽的是什么 | 计算方式 |
| --- | --- | --- |
| `relax_age` | 人群定向：去掉 `target_age_buckets` | 在放宽后的规格上真跑一遍 A2→A3→A5 |
| `expand_markets` | 地域定向：按 `taxonomy.neighbor_markets` 扩到相邻市场 | 同上（相邻关系取包内既有口径，不在服务层写国家表） |
| `discount_review` | 采购动作：`review` 档带折扣进清单 | 纳入 review 重跑分配，review 单人金额乘 `budget/policy.REVIEW_SPEND_DISCOUNT` |

字段：
- `title` / `action`：中文人话，`action` 写清「改哪一处、其余不动」。
- `spendable_usd`：该放宽下**真跑出来**的花费；`extra_spendable_usd` = 它减去本次花费，
  允许为 0 或负数（放宽了也没多花出钱是一个有效结论，不做截断）。
- `utilization_after`：放宽后的预算利用率。
- `extra_picked`：比本次清单多进来的人数。
- `quality_note`：这批新增达人的质量口径（门禁判定是什么、放宽的是定向还是采购动作、风险敞口多少）。

三条建议全部走 `koxpilot` 包内既有函数，服务层不新增任何判定或排序逻辑。

## 5. `GET /api/kox/{kox_id}/explain`

单个达人的完整证据链，等价于 CLI `koxpilot explain`。

```json
{
  "ok": true,
  "kox_id": "KOX-01234",
  "profile": { "handle": "@example", "platform": "instagram", "market": "BR", "followers": 250000 },
  "signals": [ { "label": "互动率", "value": 0.021, "threshold": 0.015, "verdict": "in_range" } ],
  "gates": [ { "gate": "G1", "gate_label": "真实性", "passed": true, "hits": [] } ],
  "verdict": "pass",
  "reason_human": "…",
  "meta": { … }
}
```

`kox_id` 不存在时：`{"ok": false, "error": {"code": "not_found", "message": "…"}}`，HTTP 仍 200。

## 6. `POST /api/gate/batch`

给一批达人记录跑门禁，用于一致性核验（前端把它拿到的记录发回后端，请后端判一遍）。

请求 `{"kox_ids": ["KOX-00001", "KOX-00002"]}`（上限 5000）
响应 `{"ok": true, "verdicts": [{"kox_id": "…", "verdict": "pass"}], "meta": {…}}`

## 7. 错误码

| code | 含义 | 前端处理 |
| --- | --- | --- |
| `bad_request` | brief 为空或超长（>2000 字） | 输入框下方提示 |
| `not_found` | kox_id / brief_id 不存在 | 提示并保持原状 |
| `too_many` | 超出批量上限 | 自动分批重试 |
| `internal` | 后端异常（已捕获） | 降级到浏览器引擎，顶部标明 |

## 8. 前端降级契约

前端必须实现三态，并在界面上如实显示当前处于哪一态：

| 态 | 触发条件 | 界面表述（中性、不辩解） |
| --- | --- | --- |
| `backend` | `/api/health` 200 且 `status=ready` | 「计算源：Python 服务」 |
| `browser` | health 超时 / 失败 / `plan` 返回 `ok:false` | 「计算源：浏览器引擎（服务未连接）」 |
| `probing` | 启动后 800ms 内 | 骨架态，不显示结论 |

降级发生时**功能不允许缺失**：浏览器引擎必须能完成同样的召回→门禁→预算全流程。
