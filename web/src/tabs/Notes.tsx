import React from 'react';
import { AlertTriangle, ArrowDown, BookOpen, Bug, ChevronDown, FileWarning, ShieldCheck, Sigma, Wrench } from 'lucide-react';
import { Badge, Note, Panel, TruthChip } from '../components/ui';
import { BOUNDARIES, GUARD_TESTS, ITERATION_LOG, KNOWN_DEFECTS, METHOD_RULES, REGRESSIONS, type LogEntry } from '../content/notes';
import type { Loose } from '../lib/artifacts';
import { fixed, int0, pct1, usd0 } from '../lib/format';

/** 带符号的定点数：+0.0149 / −26.93。方向就是结论，所以符号不能省。 */
function signedFixed(v: number, digits = 2): string {
  if (!Number.isFinite(v)) return '—';
  if (v === 0) return '0';
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}`;
}

const WEIGHT_STYLE: Record<LogEntry['weight'], { border: string; kicker: string; icon: React.ReactElement }> = {
  critical: {
    border: 'border-rose-300 bg-rose-50',
    kicker: 'bg-rose-100 text-rose-700',
    icon: <AlertTriangle size={13} className="text-rose-600" />,
  },
  high: {
    border: 'border-amber-200 bg-amber-50',
    kicker: 'bg-amber-100 text-amber-700',
    icon: <Bug size={13} className="text-amber-600" />,
  },
  normal: {
    border: 'border-slate-200 bg-slate-50',
    kicker: 'bg-slate-100 text-slate-700',
    icon: <FileWarning size={13} className="text-slate-600" />,
  },
};

function LogCard({ e, defaultOpen, evidence }: { e: LogEntry; defaultOpen: boolean; evidence?: React.ReactNode }): React.ReactElement {
  const [open, setOpen] = React.useState(defaultOpen);
  const st = WEIGHT_STYLE[e.weight];
  return (
    <div className={`rounded-2xl border ${st.border}`}>
      <button onClick={() => setOpen(!open)} className="flex w-full items-start gap-2.5 px-4 py-3 text-left">
        {st.icon}
        <div className="min-w-0 flex-1">
          <span className={`num inline-block rounded px-1.5 py-0.5 text-[9.5px] ${st.kicker}`}>{e.kicker}</span>
          <h4 className="mt-1.5 text-[14px] font-semibold leading-snug text-slate-900">{e.title}</h4>
          <p className="mt-1 text-[12px] leading-relaxed text-slate-700">{e.punchline}</p>
        </div>
        <ChevronDown size={14} className={`mt-1 shrink-0 text-slate-500 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="grid gap-3 border-t border-slate-300 px-4 py-3 md:grid-cols-3">
          <div>
            <div className="mb-1 text-[10.5px] font-medium uppercase tracking-wider text-slate-500">怎么发现的</div>
            <p className="text-[11.5px] leading-relaxed text-slate-600">{e.found}</p>
          </div>
          <div>
            <div className="mb-1 text-[10.5px] font-medium uppercase tracking-wider text-slate-500">根因</div>
            <p className="text-[11.5px] leading-relaxed text-slate-600">{e.cause}</p>
          </div>
          <div>
            <div className="mb-1 text-[10.5px] font-medium uppercase tracking-wider text-slate-500">修法</div>
            <ul className="space-y-1">
              {e.fix.map((f) => (
                <li key={f} className="flex gap-1.5 text-[11.5px] leading-relaxed text-slate-600">
                  <Wrench size={10} className="mt-[3px] shrink-0 text-live-600" />
                  {f}
                </li>
              ))}
            </ul>
          </div>
          {evidence && (
            <div className="md:col-span-3">
              <div className="rounded-xl border border-live-200 bg-live-50 px-3 py-2.5">
                <div className="mb-1.5 flex items-center gap-1.5">
                  <Sigma size={11} className="text-live-600" />
                  <span className="text-[10.5px] font-medium uppercase tracking-wider text-live-700">
                    修完之后的产物现场证据（本页运行时从 public/data 现读）
                  </span>
                </div>
                {evidence}
              </div>
            </div>
          )}
          {e.cost && (
            <div className="md:col-span-3">
              <div className="rounded-lg border border-slate-200 bg-slate-100 px-3 py-2">
                <span className="text-[10.5px] font-medium uppercase tracking-wider text-slate-500">修完的代价 </span>
                <span className="text-[12px] leading-relaxed text-slate-800">{e.cost}</span>
              </div>
            </div>
          )}
          <div className="md:col-span-3 flex flex-wrap gap-1.5">
            {e.files.map((f) => (
              <Badge key={f} className="border-slate-300 font-mono text-slate-500">
                {f}
              </Badge>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** 证据块里的一格：一个数字 + 它的产物字段路径。字段路径直接写在下面，方便打开 JSON 核对。 */
function Cell({ label, value, path, tone = 'plain' }: { label: string; value: string; path: string; tone?: 'plain' | 'good' | 'warn' }): React.ReactElement {
  const cls = tone === 'good' ? 'text-emerald-700' : tone === 'warn' ? 'text-amber-700' : 'text-live-700';
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-100 px-2.5 py-1.5">
      <div className="text-[10.5px] leading-snug text-slate-600">{label}</div>
      <div className={`num mt-0.5 text-[14px] font-semibold leading-none ${cls}`}>{value}</div>
      <div className="num mt-1 text-[9.5px] leading-snug text-slate-500">{path}</div>
    </div>
  );
}

/** 大号 before → after：把"我把自己的数字改小了"这件事放在最显眼的位置。 */
function SpotlightSwap({
  label,
  early,
  earlyTag,
  final,
  finalSub,
  why,
  source,
}: {
  label: string;
  early: string;
  earlyTag: string;
  final: string;
  finalSub?: string;
  why: string;
  source: string;
}): React.ReactElement {
  return (
    <div className="relative overflow-hidden rounded-2xl border border-rose-200 bg-gradient-to-br from-rose-100 via-transparent to-live-100 px-4 py-4">
      <div className="text-[11px] font-medium text-slate-600">{label}</div>
      <div className="mt-3 flex flex-wrap items-end gap-x-5 gap-y-3">
        <div>
          <div className="text-[10px] text-rose-600/80">{earlyTag}</div>
          <div className="num relative mt-0.5 text-[30px] font-semibold leading-none text-rose-600/55">
            <span className="relative">
              {early}
              <span className="absolute left-[-4%] top-1/2 h-[2px] w-[108%] -translate-y-1/2 rotate-[-8deg] bg-rose-500" />
            </span>
          </div>
          <div className="muted mt-1">历史值 · 来自迭代日志</div>
        </div>
        <ArrowDown size={18} className="mb-3 -rotate-90 text-slate-500" />
        <div>
          <div className="text-[10px] text-live-600">最终采用（本页从 JSON 读）</div>
          <div className="num mt-0.5 text-[34px] font-semibold leading-none text-live-700">{final}</div>
          {finalSub && <div className="muted mt-1">{finalSub}</div>}
        </div>
      </div>
      <p className="mt-3 text-[12px] leading-relaxed text-slate-700">{why}</p>
      <div className="num mt-2 text-[10px] text-slate-500">数据来源：{source}</div>
    </div>
  );
}

export function NotesTab({
  promptBench,
  audit,
  metrics,
  multiseed,
}: {
  promptBench: Loose | null;
  audit: Loose | null;
  metrics: Loose | null;
  multiseed: Loose | null;
}): React.ReactElement {
  const ruleF1 = promptBench ? Number(promptBench.rule_baseline_f1) : null;
  const cfTotals = (audit?.counterfactual_value_audit?.totals ?? null) as Loose | null;
  const bounded = (cfTotals?.effective_view_uplift_bounded ?? null) as Loose | null;
  const va = (audit?.counterfactual_value_audit?.value_attribution ?? null) as Loose | null;
  const t2 = metrics?.table_2_verdict_confusion as Loose | undefined;
  const t4 = metrics?.table_4_ablation as Loose | undefined;
  const t5 = metrics?.table_5_sensitivity as Loose | undefined;
  const decay = metrics?.budget_decay_sensitivity as Loose | undefined;
  const fit = (metrics?.table_6_llm_vs_rule as Loose | undefined)?.semantic_fit_llm_vs_rule as Loose | undefined;
  const A = multiseed?.A_value_robustness as Loose | undefined;
  const arm = A?.arm_attribution as Loose | undefined;

  /** ± 一个 std 的紧凑写法，只在这一页用。 */
  const meanStd = (d: Loose | undefined, f: (x: number) => string): string =>
    d ? `${f(Number(d.mean))} ± ${f(Number(d.std))}` : '产物未生成';

  /**
   * REGRESSIONS 里每一行的"最终值"从产物取，取不到就明确显示未生成，不回填字面量。
   * 用 finalKey 精确分派，不做字符串包含匹配 —— 加行时不会串味。
   */
  const FINAL: Record<string, () => string> = {
    rule_baseline_f1: () => (ruleF1 === null ? '产物未生成' : fixed(ruleF1, 4)),
    saved_share_multiseed: () =>
      A ? `${meanStd(A.saved_share_of_budget as Loose, (x) => pct1(x))}（n=${String((A.saved_share_of_budget as Loose).n)}）` : '产物未生成',
    arm_attribution: () =>
      arm
        ? `门禁与质量排序 ${usd0(Number((arm.saved_usd_by_gating as Loose).mean))} / 分散化 ${usd0(Number((arm.saved_usd_by_diversification as Loose).mean))}`
        : '产物未生成',
    saved_usd_single: () =>
      cfTotals ? `${usd0(Number(cfTotals.saved_usd))}（${pct1(Number(cfTotals.saved_share_of_budget))}）` : '产物未生成',
    uplift_unbounded: () => (cfTotals ? `+${pct1(Number(cfTotals.effective_view_uplift))}` : '产物未生成'),
    uplift_bounded: () =>
      bounded
        ? `${pct1(Number(bounded.baseline_effective_view_rate))} → ${pct1(Number(bounded.koxpilot_effective_view_rate))}（${fixed(Number(bounded.rate_gap_pp), 2)}pp）`
        : '产物未生成',
    ablation_split: () =>
      t4
        ? `正 ${((t4.positive_rules ?? []) as unknown[]).length} / 负 ${((t4.negative_rules ?? []) as unknown[]).length} / 死 ${((t4.dead_rules ?? []) as unknown[]).length}`
        : '产物未生成',
    decay: () =>
      decay
        ? `名单 Jaccard 最低 ${fixed(Number((decay.stability as Loose).selection_jaccard_min_vs_reference), 4)}（selection_stable=${String((decay.stability as Loose).selection_stable)}）`
        : '产物未生成',
    fit_audit: () =>
      fit
        ? `覆盖率 ${pct1(Number((fit.totals as Loose).coverage_share))} → ${String(fit.decision)}`
        : '产物未生成',
    verdict_accuracy: () => (t2 ? fixed(Number(t2.accuracy), 4) : '产物未生成'),
    sensitivity: () =>
      t5 ? `${fixed(Number(t5.max_abs_f1_shift), 4)}（stable=${String(t5.stable)}）` : '产物未生成',
    neutral_spec: () => '见迭代日志',
  };
  const finalOf = (key: string): string => (FINAL[key] ? FINAL[key]() : '见迭代日志');

  /**
   * 本轮五条自我修复的「产物现场证据」。
   * 每一格都写清字段路径 —— 这一页所有当前值都由此现读，content/notes.ts 里一个当前数字都没有。
   */
  const negRuleRow = ((t4?.by_g1_rule ?? []) as Loose[]).find(
    (v) => String(v.contribution ?? '') === 'negative',
  );
  const EVIDENCE: Record<string, React.ReactNode> = {
    'ablation-sign': t4 ? (
      <div className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-4">
        <Cell
          label="正向贡献规则"
          value={`${((t4.positive_rules ?? []) as string[]).length} 条`}
          path="table_4_ablation.positive_rules"
          tone="good"
        />
        <Cell
          label={`负贡献规则${((t4.negative_rules ?? []) as string[]).length > 0 ? `（${((t4.negative_rules ?? []) as string[]).join('、')}）` : ''}`}
          value={`${((t4.negative_rules ?? []) as string[]).length} 条`}
          path="table_4_ablation.negative_rules"
          tone="warn"
        />
        <Cell
          label={negRuleRow ? `${String(negRuleRow.variant)} 关掉后的严口径 ΔF1` : '负贡献规则的 ΔF1'}
          value={negRuleRow ? signedFixed(Number((negRuleRow.delta as Loose).d_fraud_f1_strict), 4) : '—'}
          path="table_4_ablation.by_g1_rule[].delta.d_fraud_f1_strict"
          tone="warn"
        />
        <Cell
          label="判定阈值 eps（三态共用）"
          value={fixed(Number((t4.contribution_criteria as Loose)?.eps), 3)}
          path="table_4_ablation.contribution_criteria.eps"
        />
      </div>
    ) : null,
    'uplift-denominator': bounded ? (
      <div className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-4">
        <Cell
          label="有界口径：基线 → KOXPilot 有效曝光率"
          value={`${pct1(Number(bounded.baseline_effective_view_rate))} → ${pct1(Number(bounded.koxpilot_effective_view_rate))}`}
          path="counterfactual_value_audit.totals.effective_view_uplift_bounded"
          tone="good"
        />
        <Cell
          label="有界口径：曝光率差（百分点，∈[−100,100]）"
          value={`${signedFixed(Number(bounded.rate_gap_pp), 2)}pp`}
          path="…effective_view_uplift_bounded.rate_gap_pp"
          tone="good"
        />
        <Cell
          label="对称提升（∈[−1,1]）"
          value={signedFixed(Number(bounded.symmetric_uplift), 4)}
          path="…effective_view_uplift_bounded.symmetric_uplift"
        />
        <Cell
          label="旧的无界比率（分母 = 基线有效曝光，只能单条看）"
          value={cfTotals ? `+${pct1(Number(cfTotals.effective_view_uplift))}` : '—'}
          path="counterfactual_value_audit.totals.effective_view_uplift"
          tone="warn"
        />
      </div>
    ) : null,
    'third-arm': arm ? (
      <div className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-4">
        <Cell
          label={`门禁与质量排序（${String(arm.n_seeds)} 种子 mean ± std）`}
          value={meanStd(arm.saved_usd_by_gating as Loose, usd0)}
          path="A_value_robustness.arm_attribution.saved_usd_by_gating"
          tone="good"
        />
        <Cell
          label="结构分散化（同口径）"
          value={meanStd(arm.saved_usd_by_diversification as Loose, usd0)}
          path="…arm_attribution.saved_usd_by_diversification"
          tone="warn"
        />
        <Cell
          label="为负的种子数：门禁段 / 分散化段"
          value={`${String(arm.n_seeds_gating_contribution_negative)} / ${String(arm.n_seeds_diversification_contribution_negative)}`}
          path="…arm_attribution.n_seeds_*_contribution_negative"
          tone="warn"
        />
        <Cell
          label="第三臂绝对有效曝光高于 KOXPilot 的种子数"
          value={`${String(arm.n_seeds_third_arm_more_effective_views)} / ${String(arm.n_seeds)}`}
          path="…arm_attribution.n_seeds_third_arm_more_effective_views"
          tone="warn"
        />
      </div>
    ) : null,
    'fit-audit': fit ? (
      <div className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-4">
        <Cell
          label="LLM 适配分覆盖候选池"
          value={`${pct1(Number((fit.totals as Loose).coverage_share))}（${int0(Number((fit.totals as Loose).covered_by_llm))}/${int0(Number((fit.totals as Loose).candidate_pool))}）`}
          path="…semantic_fit_llm_vs_rule.totals.coverage_share"
          tone="warn"
        />
        <Cell
          label="与规则层重复扣分（双算率）"
          value={pct1(Number((fit.totals as Loose).double_counted_share))}
          path="…totals.double_counted_share"
          tone="warn"
        />
        <Cell
          label="注入后门禁判定翻转 / 浪费金额差（gt 口径）"
          value={`${int0(Number((fit.totals as Loose).n_verdict_flips))} 条 / ${usd0(Number((fit.totals as Loose).wasted_delta_usd_llm_minus_rule))}`}
          path="…totals.n_verdict_flips / wasted_delta_usd_llm_minus_rule"
        />
        <Cell
          label="正式链路当前 fit 来源"
          value={String(fit.formal_chain_fit_source)}
          path="…semantic_fit_llm_vs_rule.formal_chain_fit_source"
        />
      </div>
    ) : null,
    'decay-scan': decay ? (
      <div className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-4">
        <Cell
          label={`三档 decay（${((decay.scan ?? []) as number[]).join(' / ')}）下的合计少浪费`}
          value={Object.entries(((decay.stability as Loose).saved_usd_total_by_decay ?? {}) as Record<string, number>)
            .map(([k, v]) => `${k}→${usd0(Number(v))}`)
            .join(' · ')}
          path="budget_decay_sensitivity.stability.saved_usd_total_by_decay"
          tone="good"
        />
        <Cell
          label="两段归因符号是否逐项稳定"
          value={
            Object.values(((decay.stability as Loose).sign_stable ?? {}) as Record<string, boolean>).length > 0
              ? `${Object.values(((decay.stability as Loose).sign_stable ?? {}) as Record<string, boolean>).filter(Boolean).length}/${Object.values(((decay.stability as Loose).sign_stable ?? {}) as Record<string, boolean>).length} 项 true`
              : '—'
          }
          path="…stability.sign_stable（逐项：total / 分散化 / 门禁与质量排序）"
          tone="good"
        />
        <Cell
          label={`选中名单 Jaccard 最低（判据 ≥ ${fixed(Number((decay.stability as Loose).threshold), 2)}）`}
          value={fixed(Number((decay.stability as Loose).selection_jaccard_min_vs_reference), 4)}
          path="…stability.selection_jaccard_min_vs_reference"
          tone="warn"
        />
        <Cell
          label="金额加权重叠最低 / 名单稳定判定"
          value={`${fixed(Number((decay.stability as Loose).spend_overlap_share_min_vs_reference), 4)} · ${String((decay.stability as Loose).selection_stable)}`}
          path="…stability.spend_overlap_share_min_vs_reference"
          tone="warn"
        />
      </div>
    ) : null,
  };

  return (
    <div className="space-y-4">
      <Panel
        title="这一页记录我自己抓到并修掉的问题"
        subtitle="一个只展示漂亮指标的作品是可疑的。真正能证明工程能力的是发现问题的过程，以及发现之后没有粉饰"
        right={<TruthChip kind="synthetic" />}
        tone="accent"
      >
        <p className="text-[12.5px] leading-relaxed text-slate-700">
          下面每一条都可以对着源码和产物复核：<b className="text-slate-900">两次评测自证</b>、
          <b className="text-slate-900">两处会让结论方向相反的"虚假声称"</b>（一个绝对值判据、一个分母趋 0 的比率）、
          <b className="text-slate-900">一次补对照臂之后把自己的结论推翻</b>、一个会静默产生错误结论的缓存 bug、
          一处"缓存里有分但正式链路没用"的口径落差、一个长期没有证据的关键参数，
          以及一整张<b className="text-slate-900">"修完之后数字变差、但照实采用"</b>的对照表。
        </p>
        <Note tone="warn">
          本轮新增的五条自我修复（消融符号判定、有界 uplift、三臂归因、A4 口径审计、decay 敏感性）都带一块
          <b className="text-amber-800">「产物现场证据」</b>：展开卡片就能看到修完之后的当前值和它对应的 JSON 字段路径。
          这一页的历史值是字面量（产物里查不到），当前值一律现读。
        </Note>
      </Panel>

      {/* ============ 三个最重要的数字 ============ */}
      <div>
        <div className="mb-2 flex items-center gap-2">
          <h3 className="text-[15px] font-semibold text-slate-900">三个最重要的口径，都是我自己把它改难看的</h3>
          <Badge className="border-rose-200 bg-rose-50 text-rose-700">对外口径取修完之后那个更保守的值</Badge>
        </div>
        <div className="grid gap-3 lg:grid-cols-3">
          <SpotlightSwap
            label="① 标签错配任务上的规则基线 F1"
            early="1.000"
            earlyTag="修复前（评测自证）"
            final={ruleF1 === null ? '产物未生成' : fixed(ruleF1, 4)}
            finalSub="prompt_bench.json 里 rule_baseline_f1 的实测值"
            why="precision / recall / F1 全是 1.000，是因为数据生成器让 gt 严格等价于「declared 与 observed 两个集合是否相交」，而规则算的正是这件事 —— 拿答案去考答案。把「自称 / 真实 / 带噪观测」拆成三个字段、ground truth 改按业务判据计算之后，规则基线掉到现在这个值。它难看，但它是真的。"
            source="prompt_bench.json → rule_baseline_f1（历史值 1.000 来自 koxpilot-build/02-ITERATION-LOG.md）"
          />
          <SpotlightSwap
            label="② 少浪费占预算比：从招牌单种子退成 12 种子分布"
            early={cfTotals ? pct1(Number(cfTotals.saved_share_of_budget)) : '产物未生成'}
            earlyTag="单种子（定稿那一次，读 audit.json）"
            final={A ? pct1(Number((A.saved_share_of_budget as Loose).mean)) : '产物未生成'}
            finalSub={
              A
                ? `± ${pct1(Number((A.saved_share_of_budget as Loose).std))}（n=${String((A.saved_share_of_budget as Loose).n)}，95% CI ${pct1(Number((A.saved_share_of_budget as Loose).ci95_low))}~${pct1(Number((A.saved_share_of_budget as Loose).ci95_high))}，最差种子 ${pct1(Number((A.saved_share_of_budget as Loose).min))}）`
                : undefined
            }
            why="那个招牌数字本身没算错，它只是一个种子上的结果。跑到 12 个种子之后：均值明显更低、离散度很大、还有种子是负的（跑输基线）。对外口径改成 12 种子分布，单种子降级成「其中一次」。"
            source="multiseed.json → A_value_robustness.saved_share_of_budget（左侧单种子值读 audit.json → counterfactual_value_audit.totals）"
          />
          <SpotlightSwap
            label="③ 价值主要来自哪一段：结论被自己的实验推翻"
            early="结构分散化"
            earlyTag={
              va
                ? `单种子看（占总差额 ${pct1(Number((va.waste_reduction_usd as Loose).share_of_total_by_diversification))}）`
                : '单种子看'
            }
            final={arm ? '门禁与质量排序' : '产物未生成'}
            finalSub={
              arm
                ? `12 种子 ${usd0(Number((arm.saved_usd_by_gating as Loose).mean))} ± ${usd0(Number((arm.saved_usd_by_gating as Loose).std))}（CV ${fixed(Number((arm.saved_usd_by_gating as Loose).cv), 2)}，${String(arm.n_seeds_gating_contribution_negative)}/${String(arm.n_seeds)} 为负）；分散化 ${usd0(Number((arm.saved_usd_by_diversification as Loose).mean))} ± ${usd0(Number((arm.saved_usd_by_diversification as Loose).std))}（CV ${fixed(Number((arm.saved_usd_by_diversification as Loose).cv), 2)}，${String(arm.n_seeds_diversification_contribution_negative)}/${String(arm.n_seeds)} 为负）`
                : undefined
            }
            why="补了只做分散、不看门禁的第三臂之后，定稿种子上分散化那一段更大 —— 但 12 个种子把它翻了过来：分散化那段标准差是均值的三倍多、有种子为负；门禁与质量排序那段每个种子都为正、离散度小得多。稳定可对外承诺的是后者。"
            source="multiseed.json → A_value_robustness.arm_attribution（左侧单种子占比读 audit.json → counterfactual_value_audit.value_attribution）"
          />
        </div>
      </div>

      {/* ============ 完整回退表 ============ */}
      <Panel
        title={`${REGRESSIONS.length} 处「口径改过之后照实采用」`}
        subtitle="左列是历史值（迭代日志记录），右列由本页从产物 JSON 现读 —— 你可以打开对应文件核对"
        tone="warn"
      >
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px]">
            <thead>
              <tr>
                <th className="th">指标</th>
                <th className="th">早期版本</th>
                <th className="th">最终采用（读 JSON）</th>
                <th className="th">为什么变了</th>
                <th className="th">产物字段</th>
              </tr>
            </thead>
            <tbody>
              {REGRESSIONS.map((r) => (
                <tr key={r.finalKey} className={`hairline ${r.spotlight ? 'bg-rose-50' : ''}`}>
                  <td className="td text-[12px] text-slate-800">
                    {r.metric}
                    {r.spotlight && <Badge className="ml-1.5 border-rose-200 bg-rose-50 text-rose-700">重点</Badge>}
                  </td>
                  <td className="td num text-slate-500 line-through decoration-rose-400">{r.early}</td>
                  <td className={`td num text-[13px] font-semibold ${r.worse ? 'text-rose-700' : 'text-emerald-700'}`}>
                    {finalOf(r.finalKey)}
                  </td>
                  <td className="td text-[11.5px] text-slate-600">{r.why}</td>
                  <td className="td num text-[10px] text-slate-500">{r.finalSource}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <Note tone="warn">
          关于阈值敏感性那一行：±20% 扰动下最大 F1 偏移{' '}
          {t5 ? fixed(Number(t5.max_abs_f1_shift), 4) : '—'}，按我自己设的判据（≤{t5 ? fixed(Number(t5.stability_tolerance), 2) : '0.05'} 才算稳健）
          <b className="text-amber-700">是不达标的</b>。我没有把判据放宽到 0.06 让它变绿，而是保留 stable = false 并写进弱项 ——
          改判据就等于改考卷。
        </Note>
      </Panel>

      {/* ============ 迭代日志 ============ */}
      <div>
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h3 className="text-[15px] font-semibold text-slate-900">迭代日志（按严重程度排序）</h3>
          <Badge className="border-rose-200 bg-rose-50 text-rose-700">
            {ITERATION_LOG.filter((e) => e.weight === 'critical').length} 条 critical（含 2 处虚假声称）
          </Badge>
          <Badge className="border-amber-200 bg-amber-50 text-amber-700">
            {ITERATION_LOG.filter((e) => e.weight === 'high').length} 条 high
          </Badge>
          <Badge className="border-live-200 bg-live-50 text-live-700">
            {ITERATION_LOG.filter((e) => e.evidenceId && EVIDENCE[e.evidenceId]).length} 条带产物现场证据
          </Badge>
          <Badge className="border-slate-300 text-slate-600">{KNOWN_DEFECTS.length} 项刻意保留的缺陷</Badge>
          <span className="muted">critical 默认展开</span>
        </div>
        <div className="space-y-2.5">
          {ITERATION_LOG.map((e) => (
            <LogCard
              key={e.id}
              e={e}
              defaultOpen={e.weight === 'critical'}
              evidence={e.evidenceId ? EVIDENCE[e.evidenceId] : undefined}
            />
          ))}
        </div>
      </div>

      {/* ============ 方法论 + 守卫测试 ============ */}
      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="方法论小结：这几次问题有同一个模式" subtitle="指标「好得不合理」或「整齐得不合理」，都是数据或链路在送答案">
          <ol className="space-y-2">
            {METHOD_RULES.map((m, i) => (
              <li key={m} className="flex gap-2">
                <span className="num mt-[1px] flex h-4.5 w-4.5 shrink-0 items-center justify-center rounded bg-live-100 px-1 text-[9.5px] text-live-700">
                  {i + 1}
                </span>
                <span className="text-[12px] leading-relaxed text-slate-700">{m}</span>
              </li>
            ))}
          </ol>
        </Panel>
        <Panel title="代码级保证：即便以后有人改坏了，测试会先红" subtitle="不靠自觉，靠断言" tone="accent">
          <ul className="space-y-2">
            {GUARD_TESTS.map((g) => (
              <li key={g} className="flex gap-2">
                <ShieldCheck size={13} className="mt-[2px] shrink-0 text-emerald-600" />
                <span className="text-[12px] leading-relaxed text-slate-700">{g}</span>
              </li>
            ))}
          </ul>
          <Note tone="good">
            其中最关键的是那条「泄漏哨兵」：它把「朴素规则 F1 必须 &lt; 0.95」写成断言。第一次自证之所以能被抓到靠的是人眼警觉，
            第二次靠的是逐条翻正例分布 —— 而现在同类问题会在 CI 里直接失败。
          </Note>
        </Panel>
      </div>

      {/* ============ 工程边界 ============ */}
      <Panel
        title="工程边界声明"
        subtitle="哪些是真的、哪些是模拟的、哪些是已知做不到的 —— 一次说清"
        right={<BookOpen size={13} className="text-slate-500" />}
      >
        <div className="grid gap-2 lg:grid-cols-2">
          {BOUNDARIES.map((b) => (
            <div
              key={b.title}
              className={`rounded-xl border px-3 py-2.5 ${
                b.tone === 'warn'
                  ? 'border-amber-200 bg-amber-50'
                  : b.tone === 'good'
                    ? 'border-emerald-200 bg-emerald-50'
                    : 'border-slate-200 bg-slate-50'
              }`}
            >
              <div className="text-[12px] font-medium text-slate-900">{b.title}</div>
              <p className="mt-1 text-[11.5px] leading-relaxed text-slate-600">{b.body}</p>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="刻意保留的已知缺陷" subtitle="能改但没改的，写清为什么" tone="warn">
        <ul className="space-y-2">
          {KNOWN_DEFECTS.map((d) => (
            <li key={d} className="flex gap-2">
              <AlertTriangle size={12} className="mt-[3px] shrink-0 text-amber-600" />
              <span className="text-[12px] leading-relaxed text-slate-700">{d}</span>
            </li>
          ))}
        </ul>
      </Panel>

      <Note>
        本页文字来自仓库里的 <code className="rounded bg-slate-100 px-1 font-mono text-[10px] text-live-700">koxpilot-build/02-ITERATION-LOG.md</code>
        ，历史值以字面量记录并标注；所有"最终采用"的数字都是本页运行时从 public/data 下的产物 JSON 读出来的。
      </Note>
    </div>
  );
}
