import React from 'react';
import { CheckCircle2, Cpu, GitCompare, ShieldCheck, XCircle } from 'lucide-react';
import { Boundaries, Collapse, Verdict } from '../components/Collapse';
import { Badge, KV, Note, Panel, TruthChip } from '../components/ui';
import type { ConsistencyReport, Loose, Manifest } from '../lib/artifacts';
import { fixed, int0, ms, pct1 } from '../lib/format';
import type { PipelineResult } from '../lib/pipeline';

const AGENTS = [
  {
    id: 'A1',
    name: 'BriefAgent',
    kind: 'llm-offline' as const,
    io: ['输入：自然语言 brief', '输出：投放规格'],
    why: '把"看重转化不是曝光"这类人话翻成 kpi=conversion、目标年龄段等可执行字段。',
  },
  {
    id: 'A2',
    name: 'RecallAgent',
    kind: 'rule' as const,
    io: ['输入：全库 5,000 人', '输出：本 campaign 候选池'],
    why: '平台 / 品类（含相邻）/ 市场三条硬筛选，只判断是否在射程内，不做质量判断。',
  },
  {
    id: 'A3',
    name: 'GateAgent',
    kind: 'rule' as const,
    io: ['输入：候选池', '输出：判定 + 证据链'],
    why: '四层 20 条规则，逐条给出信号、实际值、阈值、同组分位与阈值来源。',
  },
  {
    id: 'A4',
    name: 'FitAgent',
    kind: 'llm-offline' as const,
    io: ['输入：内容调性 + brief', '输出：适配分'],
    why: '语义适配打分：判断达人的内容风格与这次投放的诉求合不合。',
  },
  {
    id: 'A5',
    name: 'BudgetAgent',
    kind: 'rule' as const,
    io: ['输入：通过（可含待复核）候选', '输出：预算方案'],
    why: '边际性价比贪心 + 分层配额修正，四条硬约束在分配过程中强制生效。',
  },
  {
    id: 'A6',
    name: 'AuditAgent',
    kind: 'audit' as const,
    io: ['输入：两臂方案 + 真值', '输出：反事实价值账'],
    why: '对照"按粉丝量买"，浪费金额与有效曝光按真值结算 —— 唯一使用真值的环节。',
  },
];

const KIND_STYLE: Record<string, string> = {
  rule: 'border-live-300 bg-live-50',
  'llm-offline': 'border-indigo-300 bg-indigo-50',
  audit: 'border-fuchsia-300 bg-fuchsia-50',
};

function ParityCard({
  ok,
  title,
  main,
  rows,
}: {
  ok: boolean;
  title: string;
  main: React.ReactNode;
  rows: Array<[string, React.ReactNode]>;
}): React.ReactElement {
  return (
    <div className={`rounded-xl border px-3 py-2.5 ${ok ? 'border-emerald-200 bg-emerald-50' : 'border-rose-200 bg-rose-50'}`}>
      <div className="flex items-center gap-1.5">
        {ok ? <CheckCircle2 size={13} className="text-emerald-600" /> : <XCircle size={13} className="text-rose-600" />}
        <span className="text-[12px] font-medium text-slate-900">{title}</span>
      </div>
      <div className={`num mt-1 text-[20px] font-semibold ${ok ? 'text-emerald-600' : 'text-rose-600'}`}>{main}</div>
      <div className="mt-1">
        {rows.map(([k, v]) => (
          <KV key={k} k={k} v={v} />
        ))}
      </div>
    </div>
  );
}

/** 臂名的中文标签；未知臂名原样显示（不猜、不写死数量）。 */
const ARM_LABEL: Record<string, string> = {
  koxpilot: 'KOXPilot',
  baseline_followers: '按粉丝量基线',
  diversified_no_gate: '不设门禁的分散臂',
};

type HybridFacts = {
  reduction: number;
  ruleF1: number;
  llmF1: number | null;
  f1Drop: number;
  metric: string;
  schemes: Array<{ name: string; desc: string; calls: number; ours: boolean }>;
};

/**
 * 混合架构（规则 + LLM）的调用量与质量代价，读 data/audit.json：
 *   cost_audit.call_reduction_vs_full_llm
 *   cost_audit.accuracy_cost_of_all_rules.{rule_arm, llm_arm, f1_drop_if_all_rules, metric}
 *   cost_audit.schemes[*].{scheme, calls_total}
 * 组件 props 由 App 统一传入且不含 audit，这里独立读取产物，数字一律不写死。
 */
function useHybridFacts(): HybridFacts | null | 'missing' {
  const [v, setV] = React.useState<HybridFacts | null | 'missing'>(null);
  React.useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await fetch(`${import.meta.env.BASE_URL}data/audit.json`, { cache: 'force-cache' });
        if (!res.ok) throw new Error(String(res.status));
        const j = (await res.json()) as Loose;
        const ca = j?.cost_audit as Loose | undefined;
        const acc = ca?.accuracy_cost_of_all_rules as Loose | undefined;
        if (!alive) return;
        if (!ca || !acc || ca.status !== 'ok' || !Number.isFinite(Number(ca.call_reduction_vs_full_llm))) {
          setV('missing');
          return;
        }
        setV({
          reduction: Number(ca.call_reduction_vs_full_llm),
          ruleF1: Number(acc.rule_arm),
          llmF1: Number.isFinite(Number(acc.llm_arm)) ? Number(acc.llm_arm) : null,
          f1Drop: Number(acc.f1_drop_if_all_rules),
          metric: String(acc.metric ?? ''),
          schemes: ((ca.schemes ?? []) as Loose[]).map((s) => ({
            name: String(s.scheme),
            desc: String(s.desc ?? ''),
            calls: Number(s.calls_total),
            ours: String(s.scheme).includes('KOXPilot'),
          })),
        });
      } catch {
        if (alive) setV('missing');
      }
    })();
    return () => {
      alive = false;
    };
  }, []);
  return v;
}

function HybridCard(): React.ReactElement {
  const h = useHybridFacts();
  return (
    <Panel
      title="没有全用大模型：规则 + LLM 的混合架构"
      subtitle="分工：G0 / G1 纯规则，G2 部分规则，仅 brief 解析与语义适配调用模型"
      right={<TruthChip kind="python" />}
    >
      {h && h !== 'missing' ? (
        <div className="grid gap-3 lg:grid-cols-[1fr_1.1fr]">
          <div className="grid grid-cols-2 gap-2">
            <div className="rounded-xl border border-live-200 bg-live-50 px-3 py-2.5">
              <div className="flex items-center gap-1.5 text-[11px] text-slate-600">
                <Cpu size={11} className="text-live-600" />
                LLM 调用量
              </div>
              <div className="num mt-1 text-[22px] font-semibold text-live-700">1 / {fixed(h.reduction, 2)}</div>
              <div className="muted mt-1">相对「每层判定都问模型」的全 LLM 方案</div>
            </div>
            <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5">
              <div className="text-[11px] text-slate-600">质量代价</div>
              <div className="num mt-1 text-[22px] font-semibold text-amber-700">{fixed(h.f1Drop, 2)} F1</div>
              <div className="muted mt-1">
                若完全不用 LLM、语义适配退化为关键词匹配
                {h.llmF1 !== null ? `：${fixed(h.llmF1, 4)} → ${fixed(h.ruleF1, 4)}` : `（全规则臂 ${fixed(h.ruleF1, 4)}）`}
              </div>
            </div>
          </div>
          <div className="space-y-2">
            {h.schemes.map((s) => {
              const max = Math.max(...h.schemes.map((x) => x.calls), 1);
              return (
                <div key={s.name}>
                  <div className="flex items-baseline justify-between">
                    <span className={`text-[12px] ${s.ours ? 'text-live-700' : 'text-slate-700'}`}>
                      {s.name}
                      {s.ours && <Badge className="ml-1.5 border-live-200 bg-live-50 text-live-700">本实现</Badge>}
                    </span>
                    <span className="num text-[12px] text-slate-700">{int0(s.calls)} 次调用</span>
                  </div>
                  <div className="muted">{s.desc}</div>
                  <div className="mt-1 h-2.5 overflow-hidden rounded-sm bg-slate-100">
                    <div
                      className={`h-full rounded-sm ${s.ours ? 'bg-live-500' : s.calls === 0 ? 'bg-slate-300' : 'bg-rose-400'}`}
                      style={{ width: `${(s.calls / max) * 100}%` }}
                    />
                  </div>
                </div>
              );
            })}
            <div className="muted">质量口径：{h.metric}</div>
          </div>
        </div>
      ) : h === 'missing' ? (
        <Note tone="warn">该项未生成（audit.json 的 cost_audit 不可用）。</Note>
      ) : (
        <div className="muted">读取 audit.json…</div>
      )}
    </Panel>
  );
}

export function ArchitectureTab({
  manifest,
  consistency,
  result,
  thresholdsMeta,
}: {
  manifest: Manifest;
  consistency: ConsistencyReport | null;
  result: PipelineResult | null;
  thresholdsMeta: Loose;
}): React.ReactElement {
  const stageMs = new Map((result?.stages ?? []).map((s) => [s.id, s.elapsedMs]));
  const c = consistency;
  /** 一致性面板里"覆盖了哪些臂"一律从产物读，不写死臂数与臂名。 */
  const parityArms = c?.budget.arms ?? [];
  const armNames = [...new Set(parityArms.map((a) => ARM_LABEL[a.arm] ?? a.arm))];
  const summaryOnlyArms = [...new Set(parityArms.filter((a) => a.per_person_compared === false).map((a) => ARM_LABEL[a.arm] ?? a.arm))];

  return (
    <div className="space-y-4">
      {/* ============ 本页结论 ============ */}
      <Verdict
        what="系统构成与一致性校验"
        conclusion={
          <>
            一句 brief 依次经过<b className="text-slate-900">六个 Agent</b>：读需求 → 拉候选 → 四层门禁筛人 → 语义适配打分 → 分预算 → 以真值结算价值，
            每一步耗时为本次运行的实测值。同一套判定规则<b className="text-slate-900">用 Python 与 TypeScript 各实现一遍</b>，
            对同一份 {int0(manifest.dataset.n)} 人数据逐字段比对（含中文理由逐字符）—— 差异 {c ? int0(c.verdict.diff_count) : '—'} 条。
          </>
        }
        tone={c?.status === 'pass' ? 'good' : 'warn'}
        stats={[
          { label: 'Agent 编排', value: `${AGENTS.length} 个` },
          { label: '双实现判定差异', value: c ? `${int0(c.verdict.diff_count)} 条` : '—', tone: c && c.verdict.diff_count === 0 ? 'good' : 'bad' },
          { label: '逐条比对规模', value: c ? `${int0(c.verdict.matched)} / ${int0(c.verdict.total)}` : '—' },
          { label: '阈值分组', value: `${int0(Number(thresholdsMeta.n_groups ?? 0))} 组` },
        ]}
      />

      {/* ============ 视觉主体：六 Agent 编排图 ============ */}
      <Panel
        title="六个 Agent 的编排与职责边界"
        subtitle="耗时为本次运行实测；颜色区分实现方式：蓝=规则计算，靛=语义理解（LLM），紫=以真值结算的审计"
      >
        <div className="grid gap-2 md:grid-cols-3 xl:grid-cols-6">
          {AGENTS.map((a, i) => (
            <div key={a.id} className="relative">
              <div className={`h-full rounded-xl border px-3 py-2.5 ${KIND_STYLE[a.kind]}`}>
                <div className="flex items-center gap-1.5">
                  <span className="num text-[11px] font-semibold text-slate-900">{a.id}</span>
                  <span className="text-[11px] text-slate-700">{a.name}</span>
                  <span className="num ml-auto text-[10px] text-slate-500">
                    {stageMs.has(a.id) ? ms(stageMs.get(a.id) as number) : '—'}
                  </span>
                </div>
                <div className="mt-1.5 space-y-0.5">
                  {a.io.map((x) => (
                    <div key={x} className="num text-[10px] text-slate-500">
                      {x}
                    </div>
                  ))}
                </div>
                <p className="muted mt-1.5">{a.why}</p>
              </div>
              {i < AGENTS.length - 1 && (
                <div className="pointer-events-none absolute -right-1.5 top-1/2 hidden h-3 w-3 -translate-y-1/2 rotate-45 border-r border-t border-slate-300 xl:block" />
              )}
            </div>
          ))}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {[
            ['G0', '数据完整性', 2],
            ['G1', '真实性', 7],
            ['G2', '一致性', 6],
            ['G3', '品牌安全', 5],
          ].map(([g, name, n]) => (
            <Badge key={String(g)} className="border-slate-300 text-slate-700">
              <span className="num mr-1 font-semibold text-slate-900">{g}</span>
              {name} · {n} 条
              {result && <span className="num ml-1 text-live-700">命中 {int0(result.gateHits[String(g)] ?? 0)} 人</span>}
            </Badge>
          ))}
          <span className="muted">A3 内部四层按顺序执行，任一层给出阻断即短路</span>
        </div>
        <div className="mt-2">
          <Collapse
            title="四层门禁各自管什么，以及本次运行每层筛掉多少人"
            hint="G0 数据完整性 · G1 真实性 · G2 一致性 · G3 品牌安全"
          >
            <div className="grid gap-2 sm:grid-cols-4">
              {[
                ['G0', '数据完整性', '关键字段缺失 / 低置信度', 2, 'from-slate-100'],
                ['G1', '真实性', '7 条统计信号识别刷量账号', 7, 'from-rose-100'],
                ['G2', '一致性', '品类 / 多源 / 语言 / 受众', 6, 'from-amber-100'],
                ['G3', '品牌安全', '高危硬阻断 / 竞品 / 争议', 5, 'from-fuchsia-100'],
              ].map(([g, name, desc, n, grad]) => (
                <div key={String(g)} className={`rounded-lg border border-slate-200 bg-gradient-to-br ${grad} to-transparent px-2.5 py-2`}>
                  <div className="flex items-center gap-1.5">
                    <span className="num text-[12px] font-semibold text-slate-900">{g}</span>
                    <span className="text-[11px] text-slate-700">{name}</span>
                    <span className="num ml-auto text-[10px] text-slate-500">{n} 条规则</span>
                  </div>
                  <div className="muted mt-1">{desc}</div>
                  {result && (
                    <div className="num mt-1 text-[10px] text-live-700">本次命中 {int0(result.gateHits[String(g)] ?? 0)} 人</div>
                  )}
                </div>
              ))}
            </div>
          </Collapse>
        </div>
      </Panel>

      {/* ============ 关键技术选择：混合架构 ============ */}
      <HybridCard />

      {/* ============ 双实现一致性 ============ */}
      <Panel
        title="双实现一致性：同一份阈值下，TS 与 Python 判定完全相同"
        subtitle={
          c
            ? `TS 侧只读 Python 落盘的 thresholds.json，不做二次标定；比对由 scripts/verify-parity.mjs 每次刷新数据时重新生成（耗时 ${c.elapsed_ms} ms）`
            : 'consistency.json 未生成'
        }
        tone={c?.status === 'pass' ? 'accent' : 'danger'}
        right={
          c && (
            <Badge
              className={
                c.status === 'pass'
                  ? 'border-emerald-300 bg-emerald-100 text-emerald-800'
                  : 'border-rose-300 bg-rose-100 text-rose-800'
              }
            >
              <ShieldCheck size={11} className="mr-1" />
              {c.status === 'pass' ? '全部通过' : '存在差异'}
            </Badge>
          )
        }
      >
        {c ? (
          <>
            <div className="grid gap-2 lg:grid-cols-3">
              <ParityCard
                ok={c.verdict.diff_count === 0}
                title={`判定级（全量 ${int0(c.verdict.total)} 条）`}
                main={`${int0(c.verdict.diff_count)} 条差异`}
                rows={[
                  ['比对条数', `${int0(c.verdict.matched)} / ${int0(c.verdict.total)}`],
                  ['一致率', pct1(c.verdict.match_rate, 2)],
                  ['TS 判定分布', `${c.verdict.counts_ts.pass} / ${c.verdict.counts_ts.review} / ${c.verdict.counts_ts.reject}`],
                  ['Python 判定分布', `${c.verdict.counts_python.pass} / ${c.verdict.counts_python.review} / ${c.verdict.counts_python.reject}`],
                ]}
              />
              <ParityCard
                ok={c.evidence.status === 'pass'}
                title="证据链级（逐条理由）"
                main={`${int0(c.evidence.diff_records ?? 0)} 条差异`}
                rows={[
                  ['抽样记录', int0(c.evidence.compared_records ?? 0)],
                  ['比对理由行', int0(c.evidence.compared_reason_rows ?? 0)],
                ]}
              />
              <ParityCard
                ok={c.budget.status === 'pass'}
                title="预算 / 审计级"
                main={`${int0(c.budget.matched_arms ?? 0)} / ${int0(c.budget.compared_arms ?? 0)} 臂一致`}
                rows={[
                  ['反事实审计', `${int0(c.budget.matched_audits ?? 0)} / ${int0(c.budget.compared_audits ?? 0)} 一致`],
                  ['覆盖的臂', armNames.join('、') || '—'],
                ]}
              />
            </div>
            <div className="mt-2">
              <Collapse
                title="逐臂核对明细：人数 / 条数、花费、CPM 与阈值口径"
                hint="数据读自 consistency.json"
              >
                <div className="grid gap-3 lg:grid-cols-[1.1fr_1fr]">
                  <div>
                    <table className="w-full">
                      <thead>
                        <tr>
                          <th className="th">campaign</th>
                          <th className="th">臂</th>
                          <th className="th text-right">人 / 条</th>
                          <th className="th text-right">花费</th>
                          <th className="th text-right">CPM</th>
                          <th className="th text-center">一致</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(c.budget.arms ?? []).map((a) => (
                          <tr key={`${a.campaign_id}-${a.arm}`} className="hairline">
                            <td className="td num">{a.campaign_id}</td>
                            <td className="td text-[11px] text-slate-600">{ARM_LABEL[a.arm] ?? a.arm}</td>
                            <td className="td num text-right">
                              {a.n_selected} / {a.n_posts}
                            </td>
                            <td className="td num text-right">${int0(a.spent_usd)}</td>
                            <td className="td num text-right">${fixed(a.est_cpm_usd, 2)}</td>
                            <td className="td text-center">
                              {a.matched ? (
                                <CheckCircle2 size={13} className="mx-auto text-emerald-600" />
                              ) : (
                                <XCircle size={13} className="mx-auto text-rose-600" />
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div>
                    <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
                      <KV k="阈值文件" v={c.thresholds.file} />
                      <KV k="阈值版本" v={c.thresholds.version ?? '—'} />
                      <KV k="分组数" v={`${c.thresholds.n_groups} 组`} />
                      <KV k="数据集种子" v={String(c.dataset.seed ?? '—')} />
                      {summaryOnlyArms.length > 0 && (
                        <div className="muted mt-1.5">
                          {summaryOnlyArms.join('、')}：Python 侧仅落摘要，故只比摘要标量、约束检查与分配日志。
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              </Collapse>
            </div>
          </>
        ) : (
          <Note tone="warn">consistency.json 未生成，该项不显示。</Note>
        )}
      </Panel>

      {/* ============ 数据链路 ============ */}
      <Panel title="数据链路：从合成数据到浏览器内实时重算" subtitle="离线一次成型的产物 + 浏览器内的 TypeScript 引擎">
        <div className="grid gap-2 lg:grid-cols-2">
          {[
            ['合成数据集（固定种子）', `data/kox_5000.json · ${int0(manifest.dataset.n)} 人`, '含真值块，仅用于评测与审计', 'border-slate-200'],
            ['Python 阈值标定', 'output/thresholds.json', `按平台 × 粉丝量级分组标定分位阈值，${int0(Number(thresholdsMeta.n_groups ?? 0))} 组`, 'border-emerald-200'],
            ['Python 判定 / 预算 / 审计', 'output/verdicts · budget · audit · metrics.json', '全量离线跑，作为参考实现', 'border-emerald-200'],
            ['LLM 结果固化', 'output/llm_bench.json · prompt_bench.json', 'A1 / A4 的模型输出与真实用量落盘', 'border-indigo-200'],
            ['产物瘦身', 'public/data/*.json', '字段白名单 + 体积与校验值记录', 'border-live-200'],
            ['浏览器 TS 引擎', '本页与决策页的全部实时数字', '读同一份阈值，现场跑门禁 / 预算 / 审计 / 评测', 'border-live-200'],
          ].map(([stage, out, desc, border]) => (
            <div key={String(stage)} className={`rounded-lg border ${border} bg-slate-50 px-2.5 py-2`}>
              <div className="flex flex-wrap items-baseline gap-1.5">
                <span className="text-[12px] font-medium text-slate-800">{stage}</span>
                <span className="num text-[11px] text-live-600">→ {out}</span>
              </div>
              <div className="muted mt-0.5">{desc}</div>
            </div>
          ))}
        </div>
        <Note tone="good">
          <GitCompare size={11} className="mr-1 inline" />
          门禁、预算与 LLM 三层不读取真值字段，由静态扫描测试约束；真值只在评测与审计中作为裁判使用。
        </Note>
      </Panel>

      <Boundaries
        items={[
          '线上无后端服务：全部判定、预算与审计由浏览器内的 TypeScript 引擎对静态产物实时重算。',
          'A1 brief 解析与 A4 语义适配的 LLM 结果为离线固化产物；线上不调用模型 endpoint，缺省路径走规则实现。',
          `数据为合成数据集（${int0(manifest.dataset.n)} 条，固定种子 ${manifest.dataset.seed}），绝对数值仅在该生成假设内成立。`,
          '阈值按平台 × 粉丝量级分组标定，样本不足的分组回退至平台级或全局分位。',
        ]}
      />
    </div>
  );
}
