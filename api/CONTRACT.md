# KOXPilot HTTP API 契约（v1，冻结）

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
    "engine_version": "1.0.0",
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
- `options.top_n`：召回上限，默认 200，上限 500。
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
    { "stage": "recall",  "label": "召回",     "count": 182 },
    { "stage": "gate",    "label": "通过门禁", "count": 7 },
    { "stage": "allocated","label": "进入清单","count": 4 }
  ],
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
      { "arm": "koxpilot", "label": "KOXPilot 决策", "spend": 78400, "expected_value": 1234.5 },
      { "arm": "follower_rank", "label": "按粉丝量排序", "spend": 80000, "expected_value": 901.2 }
    ],
    "saved_usd": 21400,
    "saved_share": 0.267
  },
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
