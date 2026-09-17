/**
 * 工程叙事文案（Engineering Notes / Architecture 两页用）。
 *
 * 纪律说明：本文件只放**文字与历史事件记录**，来源是仓库里的 `koxpilot-build/02-ITERATION-LOG.md`。
 * 任何"当前指标"都不写在这里 —— 页面上的最终数字一律从 `public/data/*.json` 读或由 TS 现算。
 * 历史值（例如"修复前 F1 = 1.000"）无法从最终产物里读到，因此以字面量记录，并在 UI 上明确标注为
 * 「历史值 · 来自迭代日志」，与"最终采用值（读 JSON）"分列摆放，避免混淆。
 */

export interface LogEntry {
  id: string;
  weight: 'critical' | 'high' | 'normal';
  kicker: string;
  title: string;
  /** 一句话结论，卡片折叠时也能看懂。 */
  punchline: string;
  found: string;
  cause: string;
  fix: string[];
  cost?: string;
  files: string[];
}

export const ITERATION_LOG: LogEntry[] = [
  {
    id: 'self-proof-1',
    weight: 'critical',
    kicker: '评测自证 · 第一次',
    title: 'Jaccard 规则的 F1 = 1.000，我先假设是自己在作弊',
    punchline:
      '规则对照组 precision / recall / F1 全是 1.000。这不可能是模型好，只可能是 ground truth 和特征是同一件事的两面。',
    found:
      '跑 Prompt 横评时，纯规则对照组（declared 与 observed 品类 Jaccard < 0.34 判错配）三项指标全部 1.000。正常任务不会出现这种数字，于是我停下来查评测链路而不是继续往下跑。',
    cause:
      '数据生成器注入 tag_mismatch 时让 observed_categories 与 declared_categories 完全不重叠，不注入时必然重叠。于是 gt 严格等价于「两个集合是否相交」，而规则算的正是这件事 —— 等于拿答案去考答案。',
    fix: [
      '把「达人自称 declared」「真实产出 gt.true_categories」「系统带噪观测 observed」三件事拆成三个字段，其中 true_categories 只进 gt 块，门禁看不到',
      'ground truth 改为在 declared vs true_categories 上按业务判据计算（实质性偏离份额阈值 0.60）',
      '刻意制造双向噪声：40% 注入只偏到相邻品类（规则假阳性来源），一部分观测被 bio 文本带偏（假阴性来源）',
    ],
    cost: '规则基线 F1 从 1.000 掉到 0.641 —— 数字难看多了，但这才是真的。',
    files: ['datagen/generator.py::_categories', 'datagen/config.py'],
  },
  {
    id: 'self-proof-2',
    weight: 'critical',
    kicker: '评测自证 · 第二次',
    title: '5 个 arm 的召回精确相同（0.857），说明第一次只修了一半',
    punchline:
      '不同方法的 precision 明显分散、预测两两差异最高 146/600 条，召回却一模一样 —— 那不是巧合，是可分性退化。',
    found:
      '第一次修完重跑横评，规则基线与 Prompt v1/v2 × 双模型共 5 个 arm 的召回全部等于 0.857。我把 42 个正例逐条拉出来看：被检出的 36 个 Jaccard 全等于 0.00，漏掉的 6 个全等于 1.00。',
    cause:
      '观测模型是整块替换的 —— observed 要么整体等于 true_cats、要么整体等于 declared，而实质错配的 true_cats 取自 far_pool（与 declared 及其邻居都不相交），所以 Jaccard 只有 0 和 1 两个取值。正例于是变成「要么白送、要么信息上不可能检出」，召回成了阶跃函数。第一次修复消除了 gt 与特征的等价关系，却没消除可分性的退化。',
    fix: [
      '观测改为逐品类独立采样：真实品类以 0.82 概率被识别（漏项）',
      '自称品类以 0.30 概率被主页文本带进观测（部分污染，而非整块覆盖）',
      '以 0.12 概率多识别一个相邻品类（测量误差）',
      '三个机制叠加后 observed 出现「部分真实 + 部分自称」的混合态，Jaccard 形成连续分布',
    ],
    cost:
      '正例落在中间带 (0.15~0.85) 的比例从 0.0% 升到 39.2%；恰好等于 1（信息上不可检出）的从 14.3% 降到 0.8%；各 arm 召回重新分散（规则 0.857 / ARK 0.762 / Azure 0.691）。规则基线 F1 最终降到产物里的 0.5625。',
    files: ['datagen/generator.py', 'datagen/config.py'],
  },
  {
    id: 'cache-key',
    weight: 'high',
    kicker: '静默错误结论',
    title: '缓存 key 只 hash 了 kox_id，不 hash 内容',
    punchline: '重新生成数据集后 ID 不变、内容全变，旧预测会被当成有效缓存复用 —— 不报错，只给错结论。',
    found:
      '准备重新生成数据集时复查构建期 LLM 磁盘缓存，key 是 `pbench::{version}::{model}::sha1([kox_id...])`，只 hash 了 ID 列表。',
    cause:
      'kox_id 是 KOX-000001 这种确定性编号，重新生成数据后不变。于是横评看起来正常跑完，实际是拿旧数据的预测去对新数据的 ground truth。这类 bug 比崩溃危险得多。',
    fix: [
      'cache key 纳入真正进入 prompt 的字段内容（declared / observed / bio 等）',
      '补一条测试断言：相同 kox_id 但内容不同的 batch 必须产生不同 key',
      '旧缓存全部失效是正确行为，没有为兼容旧 key 做妥协；重生成数据前先删掉 output/llm_cache.json',
    ],
    files: ['llm/cache.py', 'llm/promptbench.py'],
  },
  {
    id: 'neutral-spec',
    weight: 'high',
    kicker: '语义边界错配',
    title: '评测表里混进了一格「恒等于 1.0」的无意义数据',
    punchline:
      '规则版语义适配分的分位数 p10 到 p90 一路 1.0。函数没错、中性 spec 也没错，错在拿中性 spec 去报一个只有具体 campaign 下才成立的指标。',
    found: 'LLM 真调跑完后逐格核对 metrics.json 表 6，看到语义适配分 mean 1.0、低于复审阈值占比 0.0 —— 5,000 个样本毫无区分度。',
    cause:
      'harness 里传的是默认构造的 CampaignSpec()（库级中性画像），target_categories 为空，而 rule_fit_score 对空目标品类直接 return 1.0，语义是「该维度不做 campaign 约束」。',
    fix: [
      'rule_fit_distribution 增加中性 spec 显式短路，返回 status="skipped_neutral_spec" 并说明原因 —— 宁可标注「这里跳过了」，也不要给一个假装有意义的 1.0',
      'llm_vs_rule_report 新增 campaign_specs 参数，按真实 campaign 分别报分布',
      '在 docstring 里写清这个坑，避免以后有人再传中性 spec',
    ],
    cost: '修完才有区分度：三个 campaign 的 mean 落在 0.60~0.62，p10 = 0.20，低于复审阈值占比 35%~37%。',
    files: ['eval/harness.py', 'eval/llm_compare.py'],
  },
  {
    id: 'concurrent-cache',
    weight: 'normal',
    kicker: '已知局限（没假装不存在）',
    title: '两个进程并发写同一个缓存文件，互相覆盖',
    punchline: '同一任务两个进程报出的 F1 不一致（ARK 0.653 vs 0.681），因为各自命中的缓存不同。',
    found: '误启了两个 runner 进程，两者各持一份内存副本、各自「写临时文件 + 原子替换整份文件」，互相覆盖对方的写入。',
    cause: 'Cache._flush_locked 有 threading.RLock，在单进程多线程下正确，但没有跨进程锁。',
    fix: [
      '收拢成单进程重跑（命中缓存后很快）',
      '写进工程边界声明：构建期脚本不支持多进程并发，要并发就得换带文件锁或独立 key 空间的实现',
      '顺带一个坑：用 pkill -f "koxpilot.llm.promptbench" 清进程时把执行命令的 shell 自己也杀了（命令行文本自匹配），后来改用不自匹配的写法',
    ],
    files: ['llm/cache.py'],
  },
  {
    id: 'run-to-run',
    weight: 'normal',
    kicker: '实验可信度边界',
    title: '大模型 run-to-run 波动：同 prompt 同模型两次 F1 不同',
    punchline: '结论层面（v3 > v2 > v1、LLM 强于规则）是稳的，小数点后第二位不稳。所有模型数字都应读作「单次实测值」。',
    found: '同一份样本、同一个 prompt、同一个模型，两次跑出 ARK F1 = 0.653 与 0.681。',
    cause: '推理类模型默认不是贪心解码。',
    fix: [
      '报告里所有模型对比数字明确标注为单次实测值，不是均值±方差',
      '不做「多次取最好」的挑选 —— 那等于用方差换结论',
    ],
    files: ['llm/promptbench.py'],
  },
];

/** 「修完之后数字变差，但照实采用」。final 由页面从 JSON 读，这里只存历史值与原因。 */
export interface RegressionRow {
  metric: string;
  early: string;
  /** 最终值从哪个产物字段读（UI 会显示这个路径，方便核对）。 */
  finalSource: string;
  why: string;
  worse: boolean;
  spotlight?: boolean;
}

export const REGRESSIONS: RegressionRow[] = [
  {
    metric: '规则基线 F1（标签错配任务）',
    early: '1.000',
    finalSource: 'prompt_bench.json → rule_baseline_f1',
    why: '修掉第一次评测自证：gt 不再等价于「两个集合是否相交」',
    worse: true,
    spotlight: true,
  },
  {
    metric: '反事实价值：少浪费金额',
    early: '$104,239（42.5%）',
    finalSource: 'audit.json → counterfactual_value_audit.totals.saved_usd',
    why: '数据重新生成后候选池结构变化',
    worse: true,
    spotlight: true,
  },
  {
    metric: '有效曝光提升',
    early: '+133.6%',
    finalSource: 'audit.json → counterfactual_value_audit.totals.effective_view_uplift',
    why: '同上',
    worse: true,
  },
  {
    metric: '三分类整体准确率',
    early: '0.6776',
    finalSource: 'metrics.json → table_2_verdict_confusion.accuracy',
    why: '同上',
    worse: true,
  },
  {
    metric: '阈值敏感性最大 F1 偏移',
    early: '0.0523（不稳健）',
    finalSource: 'metrics.json → table_5_sensitivity.max_abs_f1_shift',
    why: '没有为了好看去调阈值，也没有把「≤0.05 才算稳健」的判据放宽到 0.06',
    worse: true,
  },
  {
    metric: '规则版语义适配分布',
    early: 'mean 1.0（假象）',
    finalSource: '迭代日志实测：mean 0.60~0.62，p10 = 0.20',
    why: '修掉中性 spec 误传，指标才有区分度',
    worse: false,
  },
];

export const METHOD_RULES: string[] = [
  '看到 F1 接近 1.0 —— 先假设自己在作弊，去找 ground truth 与特征之间的等价路径',
  '看到多个方法指标完全相同 —— 先假设可分性退化，或代码没真的用上预测',
  '看到某个指标在全样本上毫无区分度（分位数全等）—— 先怀疑调用方传错了语义边界，而不是函数写错了',
  '任何「重新生成数据」之后 —— 先确认缓存、固化产物、baseline 是否都跟着失效了',
  '修完之后接受更差的数字，并把变差的原因写清楚',
];

export const GUARD_TESTS: string[] = [
  '静态扫描测试：AST 遍历门禁 / 预算 / LLM 层源码，断言不出现对 gt、true_categories、is_fraud 等字段的任何访问',
  '泄漏哨兵测试：断言朴素 Jaccard 规则的 F1 必须 < 0.95，且正例的 Jaccard 分布不得退化成双峰',
  '双实现一致性：TS 引擎与 Python 参考实现对同一份 5,000 条数据逐字段比对（含中文 human_text 逐字符）',
];

export interface Boundary {
  title: string;
  body: string;
  tone: 'warn' | 'muted' | 'good';
}

export const BOUNDARIES: Boundary[] = [
  {
    title: '数据是合成的，不是真实平台数据',
    body:
      '5,000 个达人全部由固定种子（seed=20270919）程序生成，schema 按 SPEC 3.2 设计，问题注入只改可观测信号的统计偏移。所以本页所有指标衡量的是「引擎在已知问题结构上的识别能力」，不能直接外推到真实平台数据；换真数据必须重新标定阈值。',
    tone: 'warn',
  },
  {
    title: '线上 Demo 不调任何模型 endpoint',
    body:
      'brief 解析与语义适配的 LLM 结果在构建期真调后固化成产物（token 账见 Cost & Value 页，取自 API 返回的 usage 字段，非估算）。线上只跑纯规则的门禁、预算、审计 —— 这既是为了可复现，也是为了让「浏览器里真算」这句话没有水分。',
    tone: 'muted',
  },
  {
    title: '门禁读不到 ground truth，审计才允许读',
    body:
      '引擎入口物理剥离 gt 字段，配套一条 AST 静态扫描测试断言门禁 / 预算 / LLM 层源码里不出现任何 gt 访问。评测与反事实审计反过来必须用 gt 当裁判（浪费金额、有效曝光都按 gt 算），不使用引擎自身分数，否则就是自证。',
    tone: 'good',
  },
  {
    title: '阈值只有一个权威来源',
    body:
      'thresholds.json 由 Python 侧标定并落盘，TS 引擎只读不标定。前端如果自己再标定一遍，等于制造第二套真值，双实现一致性校验就失去意义。',
    tone: 'muted',
  },
  {
    title: '构建期脚本不支持多进程并发',
    body: '磁盘缓存只有线程锁没有跨进程锁，两个 runner 同时跑会互相覆盖写入。已知局限，需要并发得换带文件锁或独立 key 空间的实现。',
    tone: 'warn',
  },
  {
    title: '标签错配任务存在召回天花板',
    body:
      '一部分正例的观测被 bio 完全带偏，只看 declared + observed 在信息上无法检出。要突破必须引入第三方信号（实际带货商品类目、评论语义）—— 这是下一步方案，不是已完成的能力。',
    tone: 'warn',
  },
];

export const KNOWN_DEFECTS: string[] = [
  '关键字段只缺 1 个时 completeness = 0.8 不触发 G0.1，会造成 G0 层漏检 —— 这是刻意保留的真实缺陷，没有为了指标去改判据',
  'SPEC 3.3 合成 gt.verdict 时未把「多源标签冲突」计入 review，而 SPEC 4.G2.2 要求口径冲突需人核，两处口径不一致。因此假阳性里有一部分是纯口径性的（唯一命中就是 G2.2），产品上它们确实该进人核队列。本实现保留 G2.2 判定，并给出屏蔽 G2.2 的对照矩阵',
  '阈值 ±20% 扰动下最大 F1 偏移超过自设的稳健判据（≤0.05），stable = false 如实保留在产物里',
];
