import React from 'react';
import { AlertTriangle, ArrowDown, BookOpen, Bug, ChevronDown, FileWarning, ShieldCheck, Wrench } from 'lucide-react';
import { Badge, Note, Panel, TruthChip } from '../components/ui';
import { BOUNDARIES, GUARD_TESTS, ITERATION_LOG, KNOWN_DEFECTS, METHOD_RULES, REGRESSIONS, type LogEntry } from '../content/notes';
import type { Loose } from '../lib/artifacts';
import { fixed, pct1, usd0 } from '../lib/format';

const WEIGHT_STYLE: Record<LogEntry['weight'], { border: string; kicker: string; icon: React.ReactElement }> = {
  critical: {
    border: 'border-rose-400/35 bg-rose-400/[0.055]',
    kicker: 'bg-rose-400/15 text-rose-200',
    icon: <AlertTriangle size={13} className="text-rose-300" />,
  },
  high: {
    border: 'border-amber-400/30 bg-amber-400/[0.05]',
    kicker: 'bg-amber-400/15 text-amber-200',
    icon: <Bug size={13} className="text-amber-300" />,
  },
  normal: {
    border: 'border-white/10 bg-white/[0.025]',
    kicker: 'bg-slate-400/15 text-slate-300',
    icon: <FileWarning size={13} className="text-slate-400" />,
  },
};

function LogCard({ e, defaultOpen }: { e: LogEntry; defaultOpen: boolean }): React.ReactElement {
  const [open, setOpen] = React.useState(defaultOpen);
  const st = WEIGHT_STYLE[e.weight];
  return (
    <div className={`rounded-2xl border ${st.border}`}>
      <button onClick={() => setOpen(!open)} className="flex w-full items-start gap-2.5 px-4 py-3 text-left">
        {st.icon}
        <div className="min-w-0 flex-1">
          <span className={`num inline-block rounded px-1.5 py-0.5 text-[9.5px] ${st.kicker}`}>{e.kicker}</span>
          <h4 className="mt-1.5 text-[14px] font-semibold leading-snug text-slate-100">{e.title}</h4>
          <p className="mt-1 text-[12px] leading-relaxed text-slate-300">{e.punchline}</p>
        </div>
        <ChevronDown size={14} className={`mt-1 shrink-0 text-slate-500 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="grid gap-3 border-t border-white/[0.07] px-4 py-3 md:grid-cols-3">
          <div>
            <div className="mb-1 text-[10.5px] font-medium uppercase tracking-wider text-slate-500">怎么发现的</div>
            <p className="text-[11.5px] leading-relaxed text-slate-400">{e.found}</p>
          </div>
          <div>
            <div className="mb-1 text-[10.5px] font-medium uppercase tracking-wider text-slate-500">根因</div>
            <p className="text-[11.5px] leading-relaxed text-slate-400">{e.cause}</p>
          </div>
          <div>
            <div className="mb-1 text-[10.5px] font-medium uppercase tracking-wider text-slate-500">修法</div>
            <ul className="space-y-1">
              {e.fix.map((f) => (
                <li key={f} className="flex gap-1.5 text-[11.5px] leading-relaxed text-slate-400">
                  <Wrench size={10} className="mt-[3px] shrink-0 text-cyan-400/70" />
                  {f}
                </li>
              ))}
            </ul>
          </div>
          {e.cost && (
            <div className="md:col-span-3">
              <div className="rounded-lg border border-white/10 bg-black/25 px-3 py-2">
                <span className="text-[10.5px] font-medium uppercase tracking-wider text-slate-500">修完的代价 </span>
                <span className="text-[12px] leading-relaxed text-slate-200">{e.cost}</span>
              </div>
            </div>
          )}
          <div className="md:col-span-3 flex flex-wrap gap-1.5">
            {e.files.map((f) => (
              <Badge key={f} className="border-white/15 font-mono text-slate-500">
                {f}
              </Badge>
            ))}
          </div>
        </div>
      )}
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
    <div className="relative overflow-hidden rounded-2xl border border-rose-400/25 bg-gradient-to-br from-rose-400/[0.09] via-transparent to-cyan-400/[0.07] px-4 py-4">
      <div className="text-[11px] font-medium text-slate-400">{label}</div>
      <div className="mt-3 flex flex-wrap items-end gap-x-5 gap-y-3">
        <div>
          <div className="text-[10px] text-rose-300/80">{earlyTag}</div>
          <div className="num relative mt-0.5 text-[30px] font-semibold leading-none text-rose-300/55">
            <span className="relative">
              {early}
              <span className="absolute left-[-4%] top-1/2 h-[2px] w-[108%] -translate-y-1/2 rotate-[-8deg] bg-rose-300/70" />
            </span>
          </div>
          <div className="muted mt-1">历史值 · 来自迭代日志</div>
        </div>
        <ArrowDown size={18} className="mb-3 -rotate-90 text-slate-500" />
        <div>
          <div className="text-[10px] text-cyan-300/90">最终采用（本页从 JSON 读）</div>
          <div className="num mt-0.5 text-[34px] font-semibold leading-none text-cyan-100">{final}</div>
          {finalSub && <div className="muted mt-1">{finalSub}</div>}
        </div>
      </div>
      <p className="mt-3 text-[12px] leading-relaxed text-slate-300">{why}</p>
      <div className="num mt-2 text-[10px] text-slate-500">数据来源：{source}</div>
    </div>
  );
}

export function NotesTab({
  promptBench,
  audit,
  metrics,
}: {
  promptBench: Loose | null;
  audit: Loose | null;
  metrics: Loose | null;
}): React.ReactElement {
  const ruleF1 = promptBench ? Number(promptBench.rule_baseline_f1) : null;
  const cfTotals = (audit?.counterfactual_value_audit?.totals ?? null) as Loose | null;
  const t2 = metrics?.table_2_verdict_confusion as Loose | undefined;
  const t5 = metrics?.table_5_sensitivity as Loose | undefined;

  /** REGRESSIONS 里每一行的"最终值"从产物取，取不到就明确显示未生成，不回填字面量。 */
  const finalOf = (metric: string): string => {
    if (metric.startsWith('规则基线 F1')) return ruleF1 === null ? '产物未生成' : fixed(ruleF1, 4);
    if (metric.includes('少浪费金额'))
      return cfTotals ? `${usd0(Number(cfTotals.saved_usd))}（${pct1(Number(cfTotals.saved_share_of_budget))}）` : '产物未生成';
    if (metric.includes('有效曝光提升'))
      return cfTotals ? `+${pct1(Number(cfTotals.effective_view_uplift))}` : '产物未生成';
    if (metric.includes('三分类')) return t2 ? fixed(Number(t2.accuracy), 4) : '产物未生成';
    if (metric.includes('敏感性'))
      return t5 ? `${fixed(Number(t5.max_abs_f1_shift), 4)}（stable=${String(t5.stable)}）` : '产物未生成';
    return '见迭代日志';
  };

  return (
    <div className="space-y-4">
      <Panel
        title="这一页记录我自己抓到并修掉的问题"
        subtitle="一个只展示漂亮指标的作品是可疑的。真正能证明工程能力的是发现问题的过程，以及发现之后没有粉饰"
        right={<TruthChip kind="synthetic" />}
        tone="accent"
      >
        <p className="text-[12.5px] leading-relaxed text-slate-300">
          下面每一条都可以对着源码和产物复核：<b className="text-slate-100">两次评测自证</b>、
          <b className="text-slate-100">一个会静默产生错误结论的缓存 bug</b>、一次并发写冲突、一格"恒等于 1.0"的无意义数据，
          以及四处<b className="text-slate-100">"修完之后数字变差、但照实采用"</b>的取舍。
        </p>
      </Panel>

      {/* ============ 两个最重要的数字 ============ */}
      <div>
        <div className="mb-2 flex items-center gap-2">
          <h3 className="text-[15px] font-semibold text-slate-100">两个最重要的数字，都是我自己把它改小的</h3>
          <Badge className="border-rose-400/30 bg-rose-400/10 text-rose-200">对外口径取修完之后那个更难看的值</Badge>
        </div>
        <div className="grid gap-3 lg:grid-cols-2">
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
            label="② 反事实价值：相比「按粉丝量买」少浪费的钱"
            early="$104,239"
            earlyTag="早期版本（42.5%）"
            final={cfTotals ? usd0(Number(cfTotals.saved_usd)) : '产物未生成'}
            finalSub={
              cfTotals
                ? `占 ${usd0(Number(cfTotals.budget_usd))} 预算的 ${pct1(Number(cfTotals.saved_share_of_budget))}；有效曝光 +${pct1(Number(cfTotals.effective_view_uplift))}（早期是 +133.6%）`
                : undefined
            }
            why="数据集重新生成后候选池结构变了，价值数字整体缩水。我没有保留那份更好看的旧结果，也没有去挑一个更有利的 baseline 口径 —— 全站展示的一律是最终产物里的数字。"
            source="audit.json → counterfactual_value_audit.totals（历史值 $104,239 / +133.6% 来自迭代日志）"
          />
        </div>
      </div>

      {/* ============ 完整回退表 ============ */}
      <Panel
        title="四处「修完之后数字变差，但照实采用」"
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
                <tr key={r.metric} className={`hairline ${r.spotlight ? 'bg-rose-400/[0.05]' : ''}`}>
                  <td className="td text-[12px] text-slate-200">
                    {r.metric}
                    {r.spotlight && <Badge className="ml-1.5 border-rose-400/30 bg-rose-400/10 text-rose-200">重点</Badge>}
                  </td>
                  <td className="td num text-slate-500 line-through decoration-rose-400/60">{r.early}</td>
                  <td className={`td num text-[13px] font-semibold ${r.worse ? 'text-rose-200' : 'text-emerald-200'}`}>
                    {finalOf(r.metric)}
                  </td>
                  <td className="td text-[11.5px] text-slate-400">{r.why}</td>
                  <td className="td num text-[10px] text-slate-500">{r.finalSource}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <Note tone="warn">
          关于阈值敏感性那一行：±20% 扰动下最大 F1 偏移{' '}
          {t5 ? fixed(Number(t5.max_abs_f1_shift), 4) : '—'}，按我自己设的判据（≤{t5 ? fixed(Number(t5.stability_tolerance), 2) : '0.05'} 才算稳健）
          <b className="text-amber-200">是不达标的</b>。我没有把判据放宽到 0.06 让它变绿，而是保留 stable = false 并写进弱项 ——
          改判据就等于改考卷。
        </Note>
      </Panel>

      {/* ============ 迭代日志 ============ */}
      <div>
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h3 className="text-[15px] font-semibold text-slate-100">迭代日志（按严重程度排序）</h3>
          <Badge className="border-rose-400/30 bg-rose-400/10 text-rose-200">2 次评测自证</Badge>
          <Badge className="border-amber-400/30 bg-amber-400/10 text-amber-200">1 个静默错误结论的缓存 bug</Badge>
          <Badge className="border-amber-400/30 bg-amber-400/10 text-amber-200">1 格无意义数据</Badge>
          <Badge className="border-white/15 text-slate-400">2 项已知局限</Badge>
          <span className="muted">critical 两条默认展开</span>
        </div>
        <div className="space-y-2.5">
          {ITERATION_LOG.map((e) => (
            <LogCard key={e.id} e={e} defaultOpen={e.weight === 'critical'} />
          ))}
        </div>
      </div>

      {/* ============ 方法论 + 守卫测试 ============ */}
      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="方法论小结：这几次问题有同一个模式" subtitle="指标「好得不合理」或「整齐得不合理」，都是数据或链路在送答案">
          <ol className="space-y-2">
            {METHOD_RULES.map((m, i) => (
              <li key={m} className="flex gap-2">
                <span className="num mt-[1px] flex h-4.5 w-4.5 shrink-0 items-center justify-center rounded bg-cyan-400/15 px-1 text-[9.5px] text-cyan-200">
                  {i + 1}
                </span>
                <span className="text-[12px] leading-relaxed text-slate-300">{m}</span>
              </li>
            ))}
          </ol>
        </Panel>
        <Panel title="代码级保证：即便以后有人改坏了，测试会先红" subtitle="不靠自觉，靠断言" tone="accent">
          <ul className="space-y-2">
            {GUARD_TESTS.map((g) => (
              <li key={g} className="flex gap-2">
                <ShieldCheck size={13} className="mt-[2px] shrink-0 text-emerald-400" />
                <span className="text-[12px] leading-relaxed text-slate-300">{g}</span>
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
                  ? 'border-amber-400/25 bg-amber-400/[0.05]'
                  : b.tone === 'good'
                    ? 'border-emerald-400/25 bg-emerald-400/[0.05]'
                    : 'border-white/10 bg-white/[0.025]'
              }`}
            >
              <div className="text-[12px] font-medium text-slate-100">{b.title}</div>
              <p className="mt-1 text-[11.5px] leading-relaxed text-slate-400">{b.body}</p>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="刻意保留的已知缺陷" subtitle="能改但没改的，写清为什么" tone="warn">
        <ul className="space-y-2">
          {KNOWN_DEFECTS.map((d) => (
            <li key={d} className="flex gap-2">
              <AlertTriangle size={12} className="mt-[3px] shrink-0 text-amber-300" />
              <span className="text-[12px] leading-relaxed text-slate-300">{d}</span>
            </li>
          ))}
        </ul>
      </Panel>

      <Note>
        本页文字来自仓库里的 <code className="rounded bg-black/30 px-1 font-mono text-[10px] text-cyan-200">koxpilot-build/02-ITERATION-LOG.md</code>
        ，历史值以字面量记录并标注；所有"最终采用"的数字都是本页运行时从 public/data 下的产物 JSON 读出来的。
      </Note>
    </div>
  );
}
