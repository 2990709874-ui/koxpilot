# 02 · 四层质量门禁规则手册

> 本文档回答三个问题：**每条规则的判据是什么**、**门槛这个数字从哪来**、**判定是怎么合成出来的**。
>
> 每条规则都配一条来自 `output/gate_results_sample.json` 的**真实命中记录**（含 `actual` / `threshold` / `source` / 人话解释），不用编造的例子。
>
> 相关文档：[01 架构](01-architecture.md) · [03 评测方法](03-evaluation.md) · [04 Prompt 与成本](04-prompt-and-cost.md) · [05 边界声明](05-boundaries.md)

---

## 0. 设计前提：判定必须可追责

达人筛选工具最常见的形态是给一个 0–100 的"健康分"。这个形态在真实投放里过不了审：运营问"为什么这个人被拦了"，回答"综合评分 62"等于没回答；法务问"拦得住吗"，回答"模型说的"等于没回答。

所以门禁的输出不是分数，是**判定 + 证据链**：

```
GateResult
├── verdict            pass / review / reject
├── completeness       G0 完整度
├── authenticity_score G1 真实性（1 - 加权扣分）
├── consistency_score  G2 一致性
├── brand_safety_score G3 品牌安全
├── fraud_score        连续异常分（只给 AUC / 排序用，不参与判定）
├── fit_score + fit_source
├── hard_hits[]        命中的硬信号规则 ID
├── blocked_by         硬阻断规则 ID（仅 G3.1）
└── reasons[]  ← 每条都是一个 Reason
        rule_id / gate / signal / actual / threshold
        / weight / depth / severity / source / human_text
```

四条硬性约束（都有 pytest 守着）：

| 约束 | 为什么 | 测试位置 |
| --- | --- | --- |
| 非 `pass` 必有证据，`pass` 必须零证据 | 禁止"无理由 reject" | `tests/test_gates.py` |
| 每条 `Reason` 的 `source` 必须是 `quantile:` 或 `policy:` 前缀 | 禁止魔法数字 | 同上（真实数据抽查 400 条） |
| 每条 `Reason` 必须有非空 `human_text` | 证据链要给人看，不是给日志看 | 同上 |
| 判定逻辑物理上读不到 `gt` | 反自证，见 [03](03-evaluation.md) | `tests/test_no_leakage.py` |

---

## 1. 数字只允许有两个出处

这是 `src/koxpilot/gates/policy.py` 文件头写死的纪律，全项目对魔法数字零容忍：

- **A) 数据分位数** —— 在 `gates/thresholds.py` 里从达人库标定，落盘到 `output/thresholds.json`，证据链里标 `source=quantile:group:tiktok|nano`（含回退级别）。
- **B) 显式 policy 常量** —— 就在 `policy.py` 里，且**必须写明它来自 SPEC 哪一行、或它的推导过程**，证据链里标 `source=policy:spec_4.G2.1`。

所以每一条命中记录都能被追到源头。抽一条真实的看：

```
[G1.1] hard  w=0.448 depth=0.41
  互动率 24.4% 高于 TikTok · nano（1k–10k 粉） 同行的 P95（19.3%），
  互动量与粉丝规模不成比例，疑似互动农场/互赞群刷量。阈值来源：同组样本分位数（tiktok|nano）。
  signal=engagement_rate actual=0.244299 threshold=0.193442 source=quantile:group:tiktok|nano
```

这里 `w=0.448` 不是常量 0.30，是 `0.30 × (1 + 1.2 × 0.4115)` 分级惩罚后的有效权重（见 §4.3）。

---

## 2. 阈值标定：鲁棒分位数（本项目最重要的一个工程决策）

### 2.1 问题：污染样本会把阈值推到自己身上

达人库里约 16% 是水号，而**水号恰恰长在分布尾部**。如果直接取经验分位数：

- 「同组 P95 的互动率」这个门槛，会被互赞群刷出来的高互动样本自己往外推；
- 结果是 **P95 阈值落在水号堆里**，单条规则的召回被结构性封顶在 `(1-q)/prevalence`；
- 实测后果：G1.2 的召回被压到 **0.3 以下**。

这是一个"看指标看不出来"的错误——阈值看着很专业（分位数标定！），召回天花板已经被数据污染钉死了。

### 2.2 解法：在对数空间用 MAD 估尺度

```
thr = exp( median(log x) + z_q · 1.4826 · MAD(log x) )
```

- `median` 与 `MAD` 的击穿点都是 50%，**16% 污染撼动不了它们**；
- `1.4826 · MAD` 是正态分布下对标准差的一致估计；
- `z_q` 是目标分位对应的正态分位数。
- 语义上，得到的是「**干净核心分布**的 P95」，而不是「被污染的经验 P95」。

**两种阈值都落盘**：`p95` 是鲁棒值（用于判定），`p95_empirical` 是经验值（用于对照）。任何人可以打开 `thresholds.json` 看两者差多少——这个差值就是污染的规模。

以 `tiktok|nano` 组的互动率为例，这是当前产物里的真实数字：

| 分位 | 鲁棒值（判定用） | 经验值（对照） | 差异 |
| --- | --- | --- | --- |
| P95（上尾） | **0.19344182** | 0.22949270 | 鲁棒值**紧 15.7%** |
| P05（下尾） | **0.03814663** | 0.03683360 | 鲁棒值**高 3.6%（更严）** |

上尾被经验分位推高了 3.6 个百分点——这 3.6pp 就是被互赞群样本自己顶出去的空间，落在这个区间里的水号在经验口径下全部逃逸。下尾方向相反：鲁棒值更高意味着买粉号（低互动）更容易被 G1.2 抓到。**两个方向都是往"抓得更准"的方向修正**，这不是巧合，是把污染样本从尺度估计里剔除后的必然结果。

### 2.3 按信号选空间，不一刀切

`policy.ROBUST_SPACE`：

| 信号 | 空间 | 理由 |
| --- | --- | --- |
| `engagement_rate` / `comment_like_ratio` / `view_follower_ratio` / `followers_per_day` | `log` | 右偏重尾，对数化后近正态 |
| `comment_dup_rate` / `comment_emoji_only_rate` | `linear` | 本身是有界比率 |
| `max_monthly_growth` | `log1p` | 可能为 0 |
| `risk_severity_score` | `empirical` | **90% 的值是 0，鲁棒估计无意义** |
| `avg_cpm_usd` | `empirical` | 只取中位数，两种口径一致 |

最后一行埋了一个真实的坑，见 §8.1。

### 2.4 分组与回退

- 分组键：`(platform, follower_bucket)`，当前 `thresholds.json` 里有 **25 组**、**72 个回退单元格**。
- `MIN_GROUP_SAMPLES = 60`：组内可用样本不足 60 就退到 platform 级、再退到 global 级。60 的来源不是拍的——**P02/P98 这种尾部分位在 n<50 时基本由单个样本决定，60 是"尾部至少落在两个样本之间"的下限**。
- 回退级别如实写进 `source`：`quantile:group:` / `quantile:platform:` / `quantile:global:`。
- 另外落盘一份 `p0, p2, …, p100` 的连续网格（`GRID_STEP_PCT=2`），两个用途：算连续异常分 `fraud_score`（AUC 需要连续分），以及前端证据链抽屉里画"实际值 vs 同组分布"的迷你图。
- 标定过程**完全无监督**，不读 `gt`——`tests/test_no_leakage.py` 用运行期哨兵把 `calibrate` 也罩住了。

当前标定覆盖：`n_records=5000`，其中 `n_records_with_followers=4865`（135 条缺粉丝数，进不了分桶，这部分只能吃 platform / global 阈值）。`thresholds_version=1.1.0`。

---

## 3. 判定合成：三条 reject 路径 + 优先级

`gates/engine.py` 的 `synthesize_verdict`，优先级从高到低：

| 优先级 | 条件 | 判定 | 说明 |
| --- | --- | --- | --- |
| 1 | `blocked_by` 非空（G3.1 high severity） | **reject** | 品牌安全硬阻断，不进预算分配 |
| 2 | `authenticity_score < 0.50` | **reject** | `AUTHENTICITY_FLOOR_REJECT` |
| 3 | `len(hard_hits) >= 2` | **reject** | `HARD_HITS_REJECT_MIN` |
| 4 | 有任何 `reasons` 或 `completeness < 0.8` | **review** | —— |
| 5 | 其余 | **pass** | 零证据 |

### 3.1 路径 2 与路径 3 互为交叉验证（这是刻意设计的）

G1 硬信号权重取 ~0.28 不是随手取的：**任意两条硬信号相加 ≥ 0.54 → `authenticity = 1 - 0.54 = 0.46 < 0.50` → reject**，与 SPEC 的「≥2 条硬信号 → reject」**完全等价**。

两条路径算的是同一件事，但走的是不同代码。好处是任何一边写错，两边结论就会分叉。`tests/test_gates.py` 里有一条断言把这个不变量钉住了：**"最小的 2 条硬信号权重之和" 必须 > `1 - floor`**。这样以后有人调权重（比如把 G1.4 从 0.26 降到 0.20）而忘了同步 `HARD_HITS_REJECT_MIN`，测试会立刻红，而不是等指标偏移了靠人眼发现。

### 3.2 三处严格不等式，用 `math.nextafter` 卡过边界

- `authenticity == 0.50` 应 **pass**（不是 reject）
- `completeness == 0.8` 应 **pass**（不是 review）
- `hard_hits == 1` 只 **review**

这类 `<` 写成 `<=` 的错误在真实数据上几乎不可见（恰好等于门槛的样本极少），但一旦写错，整体 reject 率就系统性偏移。测试里用 `math.nextafter` 贴着门槛两侧打表。

---

## 4. G0 完整性（2 条）

关键字段 `CRITICAL_FIELDS`（5 个）：`followers` / `avg_views` / `engagement_rate` / `audience_geo` / `quoted_price_usd`。
`completeness = 非空关键字段数 / 5`（空 dict、空 list 与 `None` 同等判缺）。

| 规则 | 判据 | severity | weight | source |
| --- | --- | --- | --- | --- |
| **G0.1** | `completeness < 0.8` | soft | 1.00 | `policy:spec_4.G0` |
| **G0.2** | 恰好缺 1 个关键字段（`completeness == 0.8`） | soft | 0.40 | `policy:derived_from_spec_3.3_missing_review` |

真实命中：

```
[G0.1] KOX-000012  actual=0.6  threshold=0.8
  关键字段完整度仅 60.0%（缺 互动率、受众地域），低于门禁要求的 80.0%，
  该达人转人工复核，且真实性/一致性判定的置信度按 15% 折损计。

[G0.2] KOX-000018  actual=1  threshold=0  w=0.4
  缺 1 个关键字段（粉丝数），完整度 80.0% 恰好卡在门槛上，不足以判 reject，
  但报价/受众缺失会直接影响预算分配，先转人工补数。
```

### 4.1 G0.2 的来历：它是补一个真实漏检

SPEC 只写了 `completeness < 0.8 → review`。但 `completeness` 的取值在 5 个关键字段下是离散的 `{0, 0.2, 0.4, 0.6, 0.8, 1.0}`——**缺 1 个字段恰好等于 0.8，严格不等式不触发，什么都不命中**。而数据生成器给这批人打的 `gt.verdict` 是 `review`，于是产生了一批系统性漏检（当前全库 229 条 = 4.58%）。

处理方式：新增 G0.2，权重 0.40（**必须低于 G0.1 的 1.00**，因为"缺 1 个"的证据强度确实更弱），与 G0.1 互斥。`tests/test_gates.py` 遍历缺 0~5 个字段验证互斥性与权重方向。

### 4.2 G0 永不 block，且会给后续层打折

- **缺数据不等于封杀**：G0 只能 review，不能 reject / block。
- **置信度打折**：G0.1 命中时，后续 G1/G2/G3 的扣分权重乘 `LOW_CONFIDENCE_DISCOUNT = 0.85`。0.85 的推导：缺 1/5 关键字段时剩余证据量约 4/5，取平方根 ≈0.89，向下取整到 0.85 作为保守的信息衰减——**宁可少扣分，避免因缺数据而误 reject**。
- 测试里有一条反直觉断言：G0.1 命中后，打折后的分数必须**更高**（缺数据不该反过来加重处罚）。

---

## 5. G1 真实性 / 水号识别（7 条）

| 规则 | 信号 | 判据 | severity | 基础权重 |
| --- | --- | --- | --- | --- |
| **G1.1** | `engagement_rate` | > 同组 **P95** | hard | 0.30 |
| **G1.2** | `engagement_rate` | < 同组 **P05** | hard | 0.28 |
| **G1.3** | `comment_like_ratio` | 越出 **[P02, P98]** | soft | 0.12 |
| **G1.4** | `view_follower_ratio` | > 同组 **P98** | hard | 0.26 |
| **G1.5** | `follower_history` | 突刺（**双条件**，见 §5.2） | hard | 0.30 |
| **G1.6** | `followers_per_day` | > 同组 **P97** | soft | 0.14 |
| **G1.7** | `comment_dup_rate` / `comment_emoji_only_rate` | > 同组 **P97** | hard | 0.26 |

硬信号集合 `G1_HARD_RULES = {G1.1, G1.2, G1.4, G1.5, G1.7}`。

**软硬怎么分**：硬 = 单条证据就直接指向某个造假机制；软 = 单独出现有大量良性解释（例如 X 平台的转发文化会让评论/点赞比天然偏高、MCN 起号会让日均涨粉很快）。软信号权重取硬信号的一半以下，保证「1 硬 + 1 软」不足以 reject，只 review。

### 5.1 七条规则的真实命中（全部来自 `gate_results_sample.json`）

| 规则 | 样本 | actual | threshold | source |
| --- | --- | --- | --- | --- |
| G1.1 | KOX-000002 | 0.244299 | 0.193442 | `quantile:group:tiktok\|nano` |
| G1.2 | KOX-000013 | 0.026162 | 0.030615 | `quantile:group:tiktok\|micro` |
| G1.3 | KOX-000002 | 0.113772 | 0.097561（区间 [0.021, 0.098]） | `quantile:group:tiktok\|nano` |
| G1.4 | KOX-000021 | 2.992583 | 2.467562 | `quantile:group:tiktok\|nano` |
| G1.5 | KOX-000011 | z=2.776 | 2.5（+ 同组增速 P90=22%） | `policy:spec_4.G1.5` |
| G1.6 | KOX-000004 | 60.042 人/天 | 26.489 | `quantile:group:youtube\|nano` |
| G1.7 | KOX-000002 | 0.491926 | 0.21256 | `quantile:group:tiktok\|nano` |

两条完整人话，展示证据链的信息密度：

```
[G1.6] soft  日均涨粉 60.0 人，高于 YouTube · nano（1k–10k 粉） 同行的 P97（26.5 人）——
       账号只有 96 天就攒到 5764 粉，增长曲线不自然（也可能是 MCN 起号，故只计软信号）。

[G1.5] hard  第 2 个月粉丝环比 23%，鲁棒 z-score=2.78 超过 2.5，且该月增速高于
       TikTok · nano（1k–10k 粉） 同行「最好月份」增速的 P90（22%），
       但该月及前一个月都没有爆款内容记录（viral_months 不含该月），典型的买粉突刺形态。
```

注意 G1.6 那句「也可能是 MCN 起号，故只计软信号」——**把规则自己的不确定性写进证据链**，是这套门禁和"健康分"最大的区别。运营看到这句话知道该怎么核。

### 5.2 G1.5 是本实现对 SPEC 的**修正**（双条件）

SPEC 原文只要求「某月环比增速 z-score > 2.5」。实现后发现：**11 个点的短序列上 MAD 尺度很小，纯 `z>2.5` 在正常号里会有约 8.6% 的假阳性率**（实测）。

所以追加第二个必要条件：该月增速还必须**高于同组达人「最好月份增速」分布的 P90**（`SPIKE_GROWTH_FLOOR_Q="p90"`）——即"这个突刺跟同行的最佳月份比也算离谱"。阈值仍来自数据分位数，没有引入拍的数。

配套细节：

- `MIN_HISTORY_MONTHS = 6`：历史不足 6 个月不做突刺检测（MAD 尺度不可信）。
- `VIRAL_SUPPORT_LAG_MONTHS = 1`：爆款内容对突刺的"解释"允许 1 个月滞后（爆款在 m 月，粉丝涨在 m 或 m+1 月都算被解释）。滞后 2 个月不算。
- 测试专门有一条 off-by-one 项：spike 注入在第 3/5/9 月都要定位准（`rates[i]` 对应第 `i+1` 月）。

**这是与 SPEC 的偏离，必须显式声明**：SPEC 与部分注释仍写"只看 z>2.5"，源码是双条件，[05 §10](05-boundaries.md#10-已知的文档代码不一致自查发现未修复项全部登记在此) 里有登记。

### 5.3 G1.7 用同组 P97，不用 SPEC 的绝对阈值

SPEC 写的是绝对门槛（`comment_dup_rate > 0.35`、`emoji_only_rate > 0.40`）。本实现**不用它做判定**，改用同组 P97，原因：评论重复率的正常水平跨平台差异极大（短视频平台的"沙发""哈哈哈"本来就多），绝对阈值会在某些平台上系统性误判。

SPEC 的两个绝对值仍作为 `spec_reference_comment_dup_abs=0.35` / `spec_reference_comment_emoji_abs=0.40` 落盘到 `thresholds.json`，方便对照与回归。上面 KOX-000002 的实测 threshold 是 **0.21256**，比 SPEC 的 0.35 严得多——说明该组同行的评论重复率本来就低，绝对阈值在这里会漏检。

### 5.4 分级惩罚：`w_effective = w × (1 + 1.2 × depth)`

纯计数式扣分有个产品上说不通的后果：

> 一个评论重复率 82%（远超同组 P97）的号，只命中 1 条规则 → 只能 review；
> 而两个刚好压线的软性异常 → 却能 reject。

所以扣分改成按**越界深度** `depth ∈ [0,1]` 加权（depth 由实际值与阈值在同组经验分布中的百分位差归一化得到，见 `g1._depth`）：

`GRADED_PENALTY_GAIN = 1.2` 的推导：要让"深入尾部的单条硬信号"刚好能触发 reject，需要 `w × (1 + GAIN) > 0.5`；取最小硬信号权重 0.26 得 `GAIN > 0.923`；留余量取 **1.2**。此时压线命中仍只扣 0.26（review），深度命中扣 `0.26 × 2.2 = 0.572`（reject）。

测试逐条复算 `w = w0 × (1 + GAIN × depth)`，并断言 depth 与 weight 双单调。

### 5.5 `fraud_score` 是连续分，不是命中计数

AUC 需要连续分。如果 `fraud_score` 用"命中了几条规则"来算，它会退化成台阶函数，AUC 就没有意义了（只有几个取值，ROC 曲线只有几个点）。

实现：每个信号按百分位做 ramp，**从 P80 起线性升到 P100 计 0→1 分**（`FRAUD_SCORE_RAMP_UPPER=0.80` / `LOWER=0.20` 管上下尾），突刺分单独按 `z ∈ [1.0, 3.5]` 连续化。

- 为什么不是从 P95 才开始给分：AUC 需要在"接近阈值"的区间里也有区分度，只在命中后给分就等于自己毁掉 AUC。
- 守卫：`tests/test_gates.py` 在真实 1,500 条上断言 `fraud_score` 去重后 **> 500 个不同取值**。这条测试直接守住 AUC=0.8809 这个数字的可信度。
- **`fraud_score` 不参与判定**，只用于排序、AUC、PR 曲线。

---

## 6. G2 一致性（6 条）

| 规则 | 信号 | 判据 | 门槛来源 | weight |
| --- | --- | --- | --- | --- |
| **G2.1** | 自称品类 vs 近 30 条内容观测品类 | Jaccard **< 0.34** | `policy:spec_4.G2.1` | 0.30 |
| **G2.2** | 平台 / 供应商A / 供应商B 三源品类 | 两两平均一致度 **< 0.5** | `policy:spec_4.G2.2` | 0.20 |
| **G2.3** | `fit_score`（语义适配） | **< 0.5** | `policy` + LLM/规则注入 | 0.25 |
| **G2.4** | 内容语言 vs 目标市场语言 | 不在可接受集合内 | `policy` 词表 | 0.20 |
| **G2.5** | 自称市场在 `audience_geo` 中占比 | **< 0.35** | `policy:spec_4.G2.5` | 0.25 |
| **G2.6** | 目标年龄 + 性别加权重叠度 | **< 0.4** | `policy:spec_4.G2.6` | 0.20 |

**G2 全层不允许 hard / block**——一致性问题是"选错人"，不是"这个人不能用"。

真实命中：

```
[G2.1] KOX-000021  actual=0.3333  threshold=0.34
  自称品类 家居家电 与近 30 条内容的观测品类 汽车出行/美妆个护/家居家电 重叠度 Jaccard=0.333
  低于 0.34，标签与实际内容错配，按此标签选人会选错人群。

[G2.2] KOX-000004  actual=0.3333  threshold=0.5
  三个数据源给的品类两两平均一致度仅 0.333（低于 0.5）：
  平台=游戏应用，供应商A=游戏应用，供应商B=美妆个护，口径打架，需人工核定主品类。

[G2.5] KOX-000017  actual=0.1601  threshold=0.35
  该达人自称所属市场（PL）在其受众地域中只占 16.0%，低于 35.0% 门槛，
  钱会花给不在目标市场的观众。
```

### 6.1 几个容易写错的边界（都在测试里打了表）

- **G2.1 的 0.34 卡在 1/3 和 1/2 之间**：`1/3 = 0.333 < 0.34 < 0.5`。这两个比值恰好夹住门槛，所以测试贴着两侧打表——上面 KOX-000021 的 0.3333 就是 1/3 这个真实边界样本。
- **多源一致度：单源必须返回 `None` 而不是 0**。只有一个数据源时"一致度"无定义，返回 0 会被误判成"完全冲突"，凭空造出一批 review。
- **G2.5 受众地域缺失是 G0 的活**，G2.5 静默跳过（否则同一件事被扣两次分）。
- **G2.6 的 `0.6 × age + 0.4 × gender`** 是**产品假设**，不是物理常数：年龄结构比性别结构更能决定转化。写在 `policy.py` 里以便被追问时能明确它可配置。
- **"没有人群要求"不该被判"人群不匹配"**——spec 没给目标人群时，G2.6 跳过。
- **G2.4 把英文当作普遍可接受**：`MARKET_LANGUAGES` 给每个国家的语言集都并入了 `"en"`（出海内容常用英文）。这是刻意的产品假设，`tests/test_gates.py` 里有 `test_g24_treats_english_as_universally_acceptable` 把它钉成显式契约——以后要改成"日本只收日文"，必须先改测试，而不是悄悄改词表让判定漂移。

### 6.2 `fit_score` 的三个来源，`fit_source` 如实标注

| `fit_source` | 含义 |
| --- | --- |
| `skipped:no_target_categories` | spec 没给目标品类（库级中性口径），G2.3 跳过 |
| `rule:...` | 规则版打分（`g2.rule_fit_score`，按目标品类 / 相邻品类命中度构造） |
| `injected:...` | 外部注入（LLM 打的分），越界会被 clamp |

**注入这条路真实存在，但正式链路不走它**：`make budget` / `make eval` 的每一条 `GateResult` 的 `fit_source` 都是 `rule:` 或 `skipped:`，`injected:` 只出现在 `eval/llm_fit.py` 的离线对照里（覆盖率 14.9%、双算率 97.1%，不予升格的判据见 [05 §3.2](05-boundaries.md#32-一处必须点明的落差本轮已量化并给出不予升格的判据)）。`tests/test_llm_fit_audit.py::test_formal_chain_has_no_injected_fit_source` 把这句话钉成了测试：谁把 LLM 分接进正式链路而不同步改口径说明，立刻红。

`FIT_SCORE_REVIEW_MAX = 0.5` 的含义是"规则版打分的中点"：目标品类完全不命中**且相邻品类也不命中**时才会低于 0.5，所以 0.5 = "连相邻品类都不沾"。

**这里有一个语义边界必须点明**：`rule_fit_score` 在 `target_categories` 为空时返回 **1.0**，这是**正确**的——"没有品类要求"就不该扣适配分。错的是拿这个中性口径去报 campaign 级别的适配分布，那会得到"全是 1.0"的无意义数据。这个坑确实踩过一次，完整复盘在 [03 §2.3](03-evaluation.md#23-第三道网表-6-的语义适配分曾经恒等于-10)。

---

## 7. G3 品牌安全（5 条）

| 规则 | 判据 | severity | weight |
| --- | --- | --- | --- |
| **G3.1** | 内容审核命中 **high** severity 标记 | **block** | 1.00 |
| **G3.2** | `risk_severity_score` > 同组 **P90** | soft | 0.30 |
| **G3.3** | 竞品合作 `months_ago < 6` → review；**< 3 → reject 候选** | soft/hard | 0.35 |
| **G3.4** | 存在争议历史 | soft | 0.30 |
| **G3.5** | 受管制品类下的额外收紧 | soft | 0.45 |

风险载荷计算：`SEVERITY_POINTS = {low: 1.0, medium: 2.5, high: 10.0}`，单条 flag 的命中次数封顶 `SEVERITY_HITS_CAP = 3`（超过 3 次边际风险递减，避免单条 flag 撑爆分数）。`medium : low = 2.5 : 1` 的推导：一条 medium 约等于 2~3 条 low 的舆情风险，取中值。

真实命中：

```
[G3.1] KOX-000062  block  actual=high×1  threshold=high
  内容审核命中高危标记：强医疗功效宣称（累计 4 次）。出海投放里这类内容一旦上线就是品牌事故，
  按品牌安全策略**硬阻断**，不进入预算分配。

[G3.2] KOX-000014  soft   actual=7.5  threshold=0.0
  中低危内容累计风险载荷 7.5 分，高于同组 P90 门槛 0.0 分。命中项：医疗功效宣称（medium×3）。
  建议人工过一遍内容再决定。

[G3.4] KOX-000033  soft   actual=plagiarism_claim  threshold=null
  存在争议历史：抄袭指控（18 个月前，严重度 low）。不必然否决，但要人工判断舆情是否已冷却。
```

### 7.1 三个实现选择

1. **G3.1 block 不进层分累加**。已经硬阻断了，再扣一次 `brand_safety_score` 是双重惩罚，而且会让证据链解释不通（"为什么这个人的品牌安全分是 0.0 而不是被阻断"）。
2. **受管制品类把门槛 ×0.5**（`REGULATED_SEVERITY_MULTIPLIER`）。效果是同一个达人在医疗/儿童类单子下被拦、在普通单子下不拦。测试用 `low×2 = 2.0` 这个恰好夹在两个门槛之间的值来验证。当前只有 BRIEF-003（`regulated_category=kids`）触发，全库命中 279 条。
3. **G3.3 竞品档位**：6 个月内 review，**3 个月内**升级为 reject 候选（排他期通常 3 个月）。边界是严格的——恰好 3 个月**不算**在排他期内。无竞品清单时静默跳过。

---

## 8. 规则命中率实测：两个口径差别巨大

### 8.1 库级中性口径（`output/verdicts.json`，全库 5,000 条）

判定分布：**pass 1,740 / review 2,453 / reject 807**。

| 规则 | 命中 | 占比 | 其中导致 reject |
| --- | --- | --- | --- |
| G0.1 / G0.2 | 221 / 229 | 4.42% / 4.58% | 20 / 31 |
| G1.1 | 292 | 5.84% | 179 |
| G1.2 | 420 | 8.40% | 301 |
| G1.3 | 339 | 6.78% | 198 |
| G1.4 | 342 | 6.84% | 214 |
| G1.5 | 326 | 6.52% | 259 |
| G1.6 | 406 | 8.12% | 117 |
| G1.7 | 323 | 6.46% | 239 |
| G2.1 | 727 | 14.54% | 102 |
| **G2.2** | **1,591** | **31.82%** | 247 |
| G2.5 | 242 | 4.84% | 41 |
| G3.1 | 125 | 2.50% | **125（全部，唯一 block 路径）** |
| G3.2 | 350 | 7.00% | 51 |
| G3.4 | 105 | 2.10% | 15 |
| G2.3 / G2.4 / G2.6 / G3.3 / G3.5 | **0** | **0%** | 0 |

（"其中导致 reject" 是共现计数，不是归因：同一个 reject 的人可能命中多条。）

两个必须解释的现象：

**① G2.2 是最大的 review 制造机（31.82%）。** 三源标签打架在合成数据里被注入得比较密。它单独造成了 **467 个 `gt=pass` 却被判 `review`** 的案例（全部 `gt=pass → pred=review` 共 1,281 例）。屏蔽 G2.2 后三分类 accuracy 从 **0.6590 升到 0.7504**、macro_f1 从 **0.6731 升到 0.7397**。

G2.2 **没有**因此被关掉。理由：三源口径打架在真实供应商数据里是常态，"需人工核定主品类"是正确的产品动作，只是它和 `gt.verdict` 这个"这个人本身有没有问题"的标签口径不对齐——**这是评测口径的局限，不是规则的错**。因此处理方式是把屏蔽 G2.2 的对照口径一起写进 `metrics.json` 交由读者自行判断，而不是删规则换分数。

**② G3.2 的分位阈值退化成了 `> 0`。** 上面 KOX-000014 那条的 `threshold=0.0`——因为 90% 的达人风险载荷本来就是 0，同组 P90 自然落在 0。`policy.ROBUST_SPACE` 里其实已经承认了这件事（`risk_severity_score: "empirical"`，注释写"90% 的值是 0，鲁棒估计无意义"），但**承认零膨胀和处理零膨胀是两回事**：现在这条规则实际上是个 `> 0` 的硬编码，动态阈值名存实亡。为什么留着不修，见 [05 §5](05-boundaries.md#5-边界四分位数阈值会退化且暂不修复)。

### 8.2 campaign 口径（换真实 spec 重跑）

把 `CampaignSpec()` 换成三个真实 brief，那 5 条一直是 0 的规则立刻活过来：

| 规则 | BRIEF-001 | BRIEF-002 | BRIEF-003 |
| --- | --- | --- | --- |
| G2.3 语义适配不足 | 1,863 | 1,764 | 1,794 |
| G2.4 语言不匹配 | 2,952 | 2,556 | 2,372 |
| **G2.5 受众地域偏离** | **3,993** | **4,389** | **4,240** |
| G2.6 人群画像重叠低 | 173 | 166 | 167 |
| G3.3 竞品排他期 | 329 | 177 | 182 |
| G3.5 受管制品类 | 0 | 0 | 279 |
| **判定分布** | pass **155** / review 3,916 / reject 929 | pass **85** / review 4,039 / reject 876 | pass **138** / review 3,994 / reject 868 |

（G0/G1 各条命中数在两个口径下完全一致——它们与 campaign 无关，这也是一条自洽性检查。）

三个结论：

1. **campaign 口径下 pass 率只有 1.7%–3.1%。** 放到真实运营里这意味着人核队列会爆。这是产品可用性问题，阈值没有为了数字好看而放松。
2. **G2.5 在 campaign 口径下吞掉 80%–88% 的人。** 合成数据的 `audience_geo` 分布比真实达人分散，而门槛是"自称市场占比 ≥ 35%"。这条规则的门槛在真实数据上应该重新标定。
3. **报告的三分类指标只覆盖 15/20 条规则。** 剩下 5 条 campaign 规则没有对应的 ground truth，因此**从未被评测过**。详见 [05 §4](05-boundaries.md#4-边界三报告的三分类指标只覆盖-1520-条规则)。

---

## 9. 消融与降级：不许静默改语义

`evaluate(..., disabled_rules=...)` 支持精确移除单条规则。消融最常见的坑是"关掉一层导致另一层走进别的分支"，指标看着合理其实口径已经变了。守卫是一条不变量断言：

```
part ⊆ full   且   part == { full 中属于开启层的规则 }
```

其他结构性守卫（都在 `tests/test_gates.py`）：

- 三个层分数**各自只受本层影响**（串味会让证据链解释不通）。
- `evaluate` 是纯函数，不改入参；`evaluate_all` 的结果与记录顺序无关（排除跨记录状态）。
- 注入的 fit 分按 `kox_id` 对齐，不许串号——串号会让"LLM vs 规则"的对比彻底失真。缓存里出现数据集里不存在的 `kox_id` 时，`eval/llm_fit.py` 会把它计入 `n_scored_ids_not_in_dataset` 并如实报出来，而不是当成"覆盖了"。
- `to_dict()` 的字段集合被精确锁定（这是前端 TS 的契约，见 [01 §双实现](01-architecture.md)）。
- 空阈值表（组样本不足到什么都没标定）时不崩、不瞎判。
- 信号缺失必须**静默跳过**，不能拿 0 冒充极低值去命中下尾规则（这会让缺数据的人被判成买粉）。

---

## 10. 如何自己验证这份文档里的数字

```bash
cd koxpilot
make gate                     # 标定 25 组阈值 + 全库跑门禁，产出 thresholds.json / verdicts.json / gate_results_sample.json

# ① 逐条看证据链（文档里所有 actual/threshold/source 都能这样复现）
PYTHONPATH=src python -m koxpilot.cli explain KOX-000002            # G1.1 + G1.3 + G1.7 三连
PYTHONPATH=src python -m koxpilot.cli explain KOX-000011            # G1.5 双条件突刺
PYTHONPATH=src python -m koxpilot.cli explain KOX-000012            # G0.1 完整度 0.6
PYTHONPATH=src python -m koxpilot.cli explain KOX-000018            # G0.2 恰好 0.8
PYTHONPATH=src python -m koxpilot.cli explain KOX-000014            # G3.2 门槛退化成 0.0
PYTHONPATH=src python -m koxpilot.cli explain KOX-000062            # G3.1 硬阻断
PYTHONPATH=src python -m koxpilot.cli explain KOX-000021 --campaign BRIEF-001   # campaign 口径下多出的 5 条规则

# ② §8.1 中性口径命中率
PYTHONPATH=src python -c "
import json,collections
v=json.load(open('output/verdicts.json'))['verdicts']
c=collections.Counter(r for x in v for r in x['rules'])
print(json.dumps(dict(sorted(c.items())),indent=1))
print(collections.Counter(x['verdict'] for x in v))"

# ③ §2 鲁棒 vs 经验分位数差多少（污染规模）
PYTHONPATH=src python -c "
import json; t=json.load(open('output/thresholds.json'))
g=t['groups']['tiktok|nano']['engagement_rate']
print({k:v for k,v in g.items() if 'p95' in k or 'p05' in k})"

# ④ §1 policy 常量全量快照（每个数字都在 thresholds.json 里）
PYTHONPATH=src python -c "
import json; print(json.dumps(json.load(open('output/thresholds.json'))['policy'],ensure_ascii=False,indent=1))"

# ⑤ §8.1 屏蔽 G2.2 的对照口径 + review 假阳性归因（467 例纯由 G2.2 造成）
PYTHONPATH=src python -c "
import json; t=json.load(open('output/metrics.json'))['table_2_verdict_confusion']
print(json.dumps(t['variant_rule_disabled_G2_2'],ensure_ascii=False,indent=1)[:500])
print(json.dumps(t['review_false_positive_attribution'],ensure_ascii=False,indent=1)[:600])"

# ⑥ 规则层测试（108 项，含边界打表与消融不变量）
PYTHONPATH=src python -m pytest tests/test_gates.py -q
```

---

**上一篇** ← [01 · 系统架构与 Agent 编排](01-architecture.md) ｜ **下一篇** → [03 · 评测方法与结果](03-evaluation.md)
