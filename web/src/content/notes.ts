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
  /**
   * 有这个 id 的条目，会由 Notes.tsx 在卡片里额外渲染一块「产物现场证据」——
   * 那一块的每个数字都是运行时从 public/data 下的 JSON 现读的，本文件不写任何当前值。
   */
  evidenceId?: string;
}

export const ITERATION_LOG: LogEntry[] = [
  {
    id: 'ablation-sign',
    weight: 'critical',
    kicker: '虚假声称 · 一个绝对值把结论说反了',
    title: '消融表用 abs(delta) 判「有贡献」，于是负贡献规则也被标成有贡献',
    punchline:
      '早期 contributes = abs(ΔF1) ≥ 0.001，不看符号。有一条 G1 规则关掉之后严口径 F1 反而更高，却被标成"有贡献"，note 还跟着总括成「所有 G1 规则都有可测量的边际贡献」。',
    found:
      '逐条核对 table_4_ablation.by_g1_rule 时发现：某条规则的 ΔF1 是正号（关掉它 F1 上升），但那一行的 contributes 是 true。既然 delta = 变体 − 全量，正号只能读作"这条规则在拖累严口径 F1"。',
    cause:
      '判据写成了绝对值：只要"有可测量的变化"就算有贡献。这类 bug 不会崩、不会报错，只会让一句总括结论方向相反地写进产物和页面 —— 比崩溃危险得多。',
    fix: [
      '判定钉在 contribution_sign() 上，输出三档枚举：positive（delta ≤ −eps）/ negative（delta ≥ +eps）/ negligible（abs(delta) < eps），contributes 只表示正贡献',
      '每行额外给 contribution_note 说明这条规则到底在做什么，by_layer 则对水号 F1 与三分类准确率各给一个符号',
      '产物里把结果拆成 positive_rules / negative_rules / dead_rules 三个列表，并删掉那句「所有规则都有贡献」的总括句',
      '前端消融表同步从"✓ / —"两态改成三态 Badge，负贡献那一行整行标黄，判据文字也从 contribution_criteria 现读',
    ],
    cost: '对外口径从"7 条规则全部有效"退成"6 正 1 负"，并且必须在页面上解释为什么负贡献那条没被删掉。',
    files: ['eval/metrics.py::contribution_sign', 'tests/test_eval_metrics.py::TestAblation', 'web/src/tabs/Evaluation.tsx'],
    evidenceId: 'ablation-sign',
  },
  {
    id: 'uplift-denominator',
    weight: 'critical',
    kicker: '虚假声称 · 分母趋 0 导致比率爆炸',
    title: '有效曝光提升是「以基线为分母」的比率，基线几乎全打水漂时它会放大几千倍',
    punchline:
      '按粉丝量买的基线只选 4~7 个头部号，某些种子它把 99.9% 预算花在水号上 —— 分母趋近 0，单个 campaign 的相对提升观测到过 +3585×，一个样本就能绑架整组均值。',
    found:
      '把反事实审计推到 12 个种子上做汇总时，某些 campaign 的跨种子 mean 高到不像话（几万个百分点），而 median 只有一百多个百分点。差这么远只能是分母问题，不是效果问题。',
    cause:
      '相对提升的分母是基线自己的有效曝光。基线选人极少、又容易整单踩在水号上，分母可以逼近 0。更糟的是旧实现在分母为 0 时返回 0.0，等于把"基线全打水漂、KOXPilot 全中"这个最有利的样本记成"没有提升" —— 方向相反的虚假声称，一样得修。',
    fix: [
      '给每条 per-campaign 与 totals 补有界口径：rate_gap_pp（有效曝光率差，∈[−100,100] 百分点）、symmetric_uplift（∈[−1,1]）、absolute_gain_views（绝对增量曝光）',
      '加 ratio_denominator_fragile 标记；分母不可靠时 headline 不再引用那个无界比率',
      '分母为 0 时相对提升给 null（无定义），不再给 0.0',
      '多种子汇总的 per-campaign 主结论换成有界口径，无界比率降级成 effective_view_uplift_ratio_reference 仅供参考',
      '前端把有界口径提到显眼位置，无界比率保留但明确标注"只能单条看"',
    ],
    cost: '最好看的那个"+3585×"彻底不能用了；对外能说的是"有效曝光率从 A 提到 B，差 xx 个百分点"这种朴素得多的说法。',
    files: ['eval/audit.py', 'eval/multiseed.py', 'tests/test_audit_value.py'],
    evidenceId: 'uplift-denominator',
  },
  {
    id: 'third-arm',
    weight: 'critical',
    kicker: '归因缺口 · 补一条对照臂后结论反转',
    title: '"少浪费的钱"里到底哪部分是门禁的功劳？加了第三臂才发现单种子那个答案是反的',
    punchline:
      '原来只有"KOXPilot vs 按粉丝量买"两臂，差额说不清是门禁筛出来的还是仅仅因为结构分散化。补了只做分散、不看门禁的第三臂之后：单种子看像分散化贡献更大，12 个种子看恰好相反。',
    found:
      '基线只选 4~7 个头部号，而 KOXPilot 会买上百条 —— 两臂之间同时变了"分散程度"和"质量判断"两件事。这种对照没法支撑"门禁值这么多钱"的说法，所以补了中间臂 diversified_no_gate（强制结构配额、按每美元名义曝光排序，仍不看门禁），做链式差分。',
    cause:
      '定稿那一个种子上，结构分散化那一段的差额比门禁与质量排序更大，如果就此对外说"价值主要来自分散化"，会被 12 个种子直接推翻：分散化那一段的标准差是均值的三倍多、有若干种子为负；而门禁与质量排序那一段 12/12 为正、离散度小得多。单种子结论不是稳健结论。',
    fix: [
      '三臂链式差分：基线 → 第三臂（结构分散化的贡献）→ KOXPilot（门禁与质量排序的贡献），并显式校验两段可加、负贡献不截断',
      '字段名如实叫 by_gating_and_quality_ranking —— 第三臂到 KOXPilot 之间同时变了两件事，共用同一批分数无法再拆，不谎报成"纯门禁效果"',
      '把 12 种子的分布（mean ± std、95% CI、为负的种子数）作为对外主口径，单种子数字降级成"定稿种子那一次"',
      '如实登记第三臂的两个缺陷：它绝对有效曝光常常超过 KOXPilot，且平均选五百多人在采购上不可执行 —— 它是归因隔离臂，不是"更强的基线"',
      '守卫测试：第三臂对"质量字段整体对调 / 伪造 verdict / 伪造分数"必须完全不变（并反向断言 KOXPilot 臂会变）',
    ],
    cost: '招牌数字从"少浪费 32.5%"退成"12 个种子 21.5% ± 13.7%，其中定稿种子那一次是 32.5%"，且必须承认对照臂在绝对曝光上赢过我们。',
    files: ['eval/audit.py::counterfactual_report', 'eval/multiseed.py', 'tests/test_value_attribution.py'],
    evidenceId: 'third-arm',
  },
  {
    id: 'fit-audit',
    weight: 'high',
    kicker: '口径落差 · 从自述改成可审计',
    title: 'A4 的 LLM 适配分明明打好了，正式链路却用的是规则版',
    punchline:
      '缓存里有真实 LLM 打的适配分，但 budget / eval 走的是 rule_fit_score。以前文档只写一句"没注入"，那是无法被检验的自述；现在它是一份带判据的注入反事实。',
    found:
      '梳理"哪些是真 LLM、哪些是规则"时对上了这处落差：llm_cache.json 有每个 brief 数百条 fit 分，正式指标链路一条都没消费。',
    cause:
      '构建期离线打分只覆盖了抽样候选，覆盖率不足以支撑全量注入；而且 LLM 打分里对市场/语言/平台的扣分与规则层已有的判据重复，直接接上会双重惩罚。',
    fix: [
      '新增离线注入反事实：把缓存里的 LLM 分注入 evaluate_all 后真的重跑门禁与预算，报判定翻转数与 gt 口径下的浪费金额差',
      '"要不要升格为正式口径"由三条现算判据决定（覆盖率、双算率、覆盖子集上是否单向只降），不由我写死结论',
      '正式链路的 fit_source 里一条 injected: 都不许出现，并有测试断言',
      '全表禁止出现任何准确率类字段 —— 语义适配没有 ground truth，给"准确率"就是编的',
      '前端把覆盖率、双算率、判定翻转数、金额差与最终决策一并展示，不只展示结论',
    ],
    cost: '结论只能是"这份缓存回答不了全量注入会更好还是更差"，而不是"LLM 有用/没用"；A4 在正式指标里仍是规则实现。',
    files: ['eval/llm_fit.py', 'tests/test_llm_fit_audit.py', 'web/src/tabs/CostValue.tsx'],
    evidenceId: 'fit-audit',
  },
  {
    id: 'decay-scan',
    weight: 'high',
    kicker: '关键参数长期无证据',
    title: '采购模型里的边际衰减 decay=0.7 是个假设，扫描常量摆了很久却没进产物',
    punchline:
      'POST_DECAY_SCAN = (0.5, 0.7, 0.9) 在策略代码里定义了很久，结果一直没落到产物 —— 也就是这个直接决定"同一个人买几条"的参数，长期没有任何敏感性证据。',
    found:
      '自查文档与代码的对不上之处时发现：注释声称 metrics 会给三档扫描，metrics.json 里没有这个字段。',
    cause: '扫描函数写了但没接进产物流水线，文档先于实现被写下，属于"声称与产物不一致"。',
    fix: [
      '补 decay 扫描并落进 metrics.budget_decay_sensitivity：三档共用同一批门禁结果（门禁与 decay 无关），逐档重跑三臂预算 + gt 审计',
      '参照档的数字必须与正式预算/反事实结果逐项一致（同一套代码路径），否则说明扫描走了平行实现',
      '结论分两半，两半都写：价值结论稳（合计少浪费相对离差很小、两段归因符号不翻转），但选中名单会变（Jaccard 与金额加权重叠低于自设判据），判定 selection_stable = false',
      '前端把两半并排展示，不允许只展示"结论稳"那一半',
    ],
    cost: '这份达人名单的正确读法从"最优解"退成"给定 decay=0.7 假设下的一个方案"。扫描也不能证明 0.7 本身对 —— 那需要真实投放的重复触达数据。',
    files: ['eval/decay_scan.py', 'budget/policy.py::POST_DECAY_SCAN', 'web/src/tabs/CostValue.tsx'],
    evidenceId: 'decay-scan',
  },
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
  /** 页面用它挑对应的"从产物现读"函数；不做字符串模糊匹配，避免加行时串味。 */
  finalKey: string;
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
    finalKey: 'rule_baseline_f1',
    finalSource: 'prompt_bench.json → rule_baseline_f1',
    why: '修掉第一次评测自证：gt 不再等价于「两个集合是否相交」',
    worse: true,
    spotlight: true,
  },
  {
    metric: '对外口径：少浪费占预算比',
    early: '定稿单种子那一次',
    finalKey: 'saved_share_multiseed',
    finalSource: 'multiseed.json → A_value_robustness.saved_share_of_budget',
    why: '单种子不是稳健结论。对外改用 12 种子分布，定稿那一次降级成"其中一个种子"',
    worse: true,
    spotlight: true,
  },
  {
    metric: '价值主要来自哪一段',
    early: '结构分散化（单种子看更大）',
    finalKey: 'arm_attribution',
    finalSource: 'multiseed.json → A_value_robustness.arm_attribution',
    why: '补第三臂 diversified_no_gate 后，12 种子把这个结论反转成「门禁与质量排序」',
    worse: false,
    spotlight: true,
  },
  {
    metric: '反事实价值：少浪费金额（定稿种子）',
    early: '$104,239（42.5%）',
    finalKey: 'saved_usd_single',
    finalSource: 'audit.json → counterfactual_value_audit.totals.saved_usd',
    why: '数据重新生成后候选池结构变化',
    worse: true,
  },
  {
    metric: '有效曝光提升（无界比率）',
    early: '+133.6%',
    finalKey: 'uplift_unbounded',
    finalSource: 'audit.json → counterfactual_value_audit.totals.effective_view_uplift',
    why: '同上；且这个口径分母是基线有效曝光，已降级为"只能单条看"',
    worse: true,
  },
  {
    metric: '有效曝光的对外口径（有界）',
    early: '只有无界比率可用',
    finalKey: 'uplift_bounded',
    finalSource: 'audit.json → counterfactual_value_audit.totals.effective_view_uplift_bounded',
    why: '分母趋 0 会让比率爆炸到几千倍，改用有界的有效曝光率差（百分点）当主口径',
    worse: false,
  },
  {
    metric: 'G1 逐条规则的贡献判定',
    early: '7/7 有贡献（abs 判定）',
    finalKey: 'ablation_split',
    finalSource: 'metrics.json → table_4_ablation.positive_rules / negative_rules / dead_rules',
    why: '绝对值判据不看符号，把"关掉后 F1 反而更好"的规则也算成有贡献',
    worse: true,
  },
  {
    metric: 'decay 假设的敏感性证据',
    early: '无（扫描常量未进产物）',
    finalKey: 'decay',
    finalSource: 'metrics.json → budget_decay_sensitivity.stability',
    why: '关键采购参数长期没有证据；补扫描后价值结论稳、但选中名单不稳',
    worse: true,
  },
  {
    metric: 'A4 LLM 适配分的落差',
    early: '只有一句"没注入"',
    finalKey: 'fit_audit',
    finalSource: 'metrics.json → table_6_llm_vs_rule.semantic_fit_llm_vs_rule',
    why: '把无法检验的自述改成带三条判据的注入反事实；覆盖率不够，不予升格',
    worse: false,
  },
  {
    metric: '三分类整体准确率',
    early: '0.6776',
    finalKey: 'verdict_accuracy',
    finalSource: 'metrics.json → table_2_verdict_confusion.accuracy',
    why: '数据重新生成后候选池结构变化',
    worse: true,
  },
  {
    metric: '阈值敏感性最大 F1 偏移',
    early: '0.0523（不稳健）',
    finalKey: 'sensitivity',
    finalSource: 'metrics.json → table_5_sensitivity.max_abs_f1_shift',
    why: '没有为了好看去调阈值，也没有把「≤0.05 才算稳健」的判据放宽到 0.06',
    worse: true,
  },
  {
    metric: '规则版语义适配分布',
    early: 'mean 1.0（假象）',
    finalKey: 'neutral_spec',
    finalSource: '迭代日志实测：mean 0.60~0.62，p10 = 0.20',
    why: '修掉中性 spec 误传，指标才有区分度',
    worse: false,
  },
];

export const METHOD_RULES: string[] = [
  '看到 F1 接近 1.0 —— 先假设自己在作弊，去找 ground truth 与特征之间的等价路径',
  '看到多个方法指标完全相同 —— 先假设可分性退化，或代码没真的用上预测',
  '看到某个指标在全样本上毫无区分度（分位数全等）—— 先怀疑调用方传错了语义边界，而不是函数写错了',
  '判「有没有用」的地方一律不许出现 abs()：符号就是结论，取绝对值等于把方向丢掉',
  '任何以"对照组"为分母的比率 —— 先问分母能不能趋近 0；能，就必须同时给一个有界口径，且分母为 0 时给 null 而不是 0',
  '两个方案之间同时变了两件事 —— 差额就不可归因，要么加中间臂，要么把字段名写成"两件事的合计"，不许挑一个好听的说法',
  '任何"招牌数字"只有单次实验支撑 —— 先当它是运气，多跑几个种子再决定对外怎么说；反转了就改口径',
  '代码里摆着的扫描常量、缓存里躺着的分数，如果没进产物，就等于这个假设没有证据 —— 补证据，或者明写"无证据"',
  '任何「重新生成数据」之后 —— 先确认缓存、固化产物、baseline 是否都跟着失效了',
  '修完之后接受更差的数字，并把变差的原因写清楚',
];

export const GUARD_TESTS: string[] = [
  '静态扫描测试：AST 遍历门禁 / 预算 / LLM 层源码，断言不出现对 gt、true_categories、is_fraud 等字段的任何访问',
  '泄漏哨兵测试：断言朴素 Jaccard 规则的 F1 必须 < 0.95，且正例的 Jaccard 分布不得退化成双峰',
  '消融符号测试：断言贡献判定是带符号的三态而不是 abs()，且总括 note 里不许再出现「所有规则都有贡献」这句话',
  '有界 uplift 测试：断言分母为 0 时相对提升是 null 而非 0，且 rate_gap_pp / symmetric_uplift 永远落在各自的有界区间内',
  '三臂归因测试：第三臂对「质量字段整体对调 / 伪造 verdict / 伪造分数」必须完全不变（并反向断言 KOXPilot 臂会变），两段贡献必须可加、负贡献不许截断',
  'A4 口径审计测试：升格结论必须由覆盖率/双算率/单向性三条判据现算（喂一份"覆盖率 100% + 双向"的假缓存必须自动翻成"可升格"），正式链路 fit_source 里不许有 injected:，全表不许出现任何准确率类字段',
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
  {
    title: '对照臂只有三条，且第三臂不是"更强的基线"',
    body:
      '反事实只有"按粉丝量降序"这一个朴素基线，加一条只做结构分散、不看门禁的归因隔离臂。第三臂按每美元名义曝光排序，本质是 CPM 最优，它平均要选五百多人（采购上不可执行），绝对有效曝光还常常超过 KOXPilot。所以本实验回答的是"比朴素买法好多少、其中门禁占多少"，没有回答"对专业买手还赢多少"。',
    tone: 'warn',
  },
  {
    title: '"门禁贡献"是门禁过滤 + 质量排序的合计，不能再拆',
    body:
      '第三臂到 KOXPilot 之间同时变了两件事：候选池只收 pass，排序换成质量加权价值。二者共用同一批真实性/适配分数，实现上无法分离，所以字段名如实叫 by_gating_and_quality_ranking。要再拆需要第四臂（"过滤 pass 但仍按每美元曝光排序"），本轮没做。',
    tone: 'warn',
  },
  {
    title: '价值结论用 12 种子，名单用单种子',
    body:
      '对外的价值幅度一律读 12 个种子的分布（含为负的种子数），单种子只是"定稿那一次"。但页面上展示的达人名单、逐单计划仍来自定稿种子 + decay=0.7 这一组假设 —— decay 扫描已证明价值结论不依赖该假设，而选中名单会随它变动。名单是"一个方案"，不是唯一解。',
    tone: 'warn',
  },
  {
    title: 'A4 的 LLM 适配分只做离线对照，没接进正式链路',
    body:
      '缓存里的 LLM 适配分覆盖不到整个候选池，且与规则层对市场/语言/平台的扣分重复。是否升格由覆盖率、双算率、覆盖子集单向性三条现算判据决定，当前判定为不升格；注入反事实的结果如实展示，包括它在某个 campaign 上反而多浪费钱。',
    tone: 'muted',
  },
];

export const KNOWN_DEFECTS: string[] = [
  '关键字段只缺 1 个时 completeness = 0.8 不触发 G0.1，会造成 G0 层漏检 —— 这是刻意保留的真实缺陷，没有为了指标去改判据',
  'SPEC 3.3 合成 gt.verdict 时未把「多源标签冲突」计入 review，而 SPEC 4.G2.2 要求口径冲突需人核，两处口径不一致。因此假阳性里有一部分是纯口径性的（唯一命中就是 G2.2），产品上它们确实该进人核队列。本实现保留 G2.2 判定，并给出屏蔽 G2.2 的对照矩阵',
  '阈值 ±20% 扰动下最大 F1 偏移超过自设的稳健判据（≤0.05），stable = false 如实保留在产物里',
  'G1 里有一条规则对严口径 F1 是负贡献（关掉它 F1 反而更高），没有删掉它 —— 它是软信号、单独命中只推 review 不 reject，且是宽口径召回的来源之一。产物里标成 negative 并列进 negative_rules，不粉饰成"有贡献"',
  '按层消融里有一层关掉后三分类准确率反而更高，同样保留 —— 原因是 gt 与门禁在"多源标签冲突"上的口径冲突，指标不是唯一裁判，但反例照实标在表里',
  'decay ∈ {0.5, 0.7, 0.9} 三档下选中名单会变（Jaccard 与金额加权重叠低于自设的 0.8 判据），selection_stable = false 如实保留；扫描只能证明价值结论不依赖这个假设，不能证明 0.7 本身是对的',
];
