import React from 'react';
import { CheckCircle2, Database, FileJson, GitCompare, Hash, ShieldCheck, XCircle } from 'lucide-react';
import { Badge, Hint, KV, Note, Panel, Stat, TruthChip } from '../components/ui';
import type { ConsistencyReport, Loose, Manifest } from '../lib/artifacts';
import { compact, fixed, int0, ms, pct1 } from '../lib/format';
import type { PipelineResult } from '../lib/pipeline';

const AGENTS = [
  {
    id: 'A1',
    name: 'BriefAgent',
    kind: 'llm-offline' as const,
    io: ['输入：自然语言 brief', '输出：CampaignSpec'],
    why: '把"看重转化不是曝光"这种人话翻译成 kpi=conversion、target_age_buckets 等可执行字段。构建期真调模型，结果固化。',
  },
  {
    id: 'A2',
    name: 'RecallAgent',
    kind: 'rule' as const,
    io: ['输入：全库 5,000 人', '输出：本 campaign 候选池'],
    why: '平台 / 品类（含相邻）/ 市场三条硬筛选。只判断"在不在射程内"，不做质量判断 —— 那是门禁的活。',
  },
  {
    id: 'A3',
    name: 'GateAgent',
    kind: 'rule' as const,
    io: ['输入：候选池', '输出：verdict + 证据链'],
    why: '四层 20 条规则，逐条给出信号、实际值、阈值、同组分位与阈值来源。入口物理剥离 gt。',
  },
  {
    id: 'A4',
    name: 'FitAgent',
    kind: 'llm-offline' as const,
    io: ['输入：内容调性 + brief', '输出：fit_score'],
    why: '语义适配打分。LLM 版在构建期真调，线上按缺省走规则兜底，两者差距在「成本与价值」页有实测。',
  },
  {
    id: 'A5',
    name: 'BudgetAgent',
    kind: 'rule' as const,
    io: ['输入：pass(+review) 候选', '输出：预算方案'],
    why: '边际性价比贪心 + 分层配额修正，四条硬约束在分配过程中强制生效。',
  },
  {
    id: 'A6',
    name: 'AuditAgent',
    kind: 'audit' as const,
    io: ['输入：两臂方案 + gt', '输出：反事实价值账'],
    why: '对照"按粉丝量买"，浪费金额与有效曝光全部按 gt 计算 —— 唯一允许读 gt 的地方。',
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
  note,
}: {
  ok: boolean;
  title: string;
  main: React.ReactNode;
  rows: Array<[string, React.ReactNode]>;
  note?: React.ReactNode;
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
      {note && <div className="muted mt-1">{note}</div>}
    </div>
  );
}

/** 臂名的中文标签；未知臂名原样显示（不猜、不写死数量）。 */
const ARM_LABEL: Record<string, string> = {
  koxpilot: 'KOXPilot',
  baseline_followers: '按粉丝量基线',
  diversified_no_gate: '第三臂 diversified_no_gate',
};

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
      {/* ---- 编排图 ---- */}
      <Panel
        title="六个 Agent 的编排与职责边界"
        subtitle="每个 Agent 的耗时是本次运行的实测值；颜色代表真实性类别，不是装饰"
        right={
          <div className="flex flex-wrap items-center gap-1.5">
            <TruthChip kind="rule" />
            <TruthChip kind="llm-offline" />
            <TruthChip kind="audit" />
          </div>
        }
      >
        <div className="grid gap-2 md:grid-cols-3 xl:grid-cols-6">
          {AGENTS.map((a, i) => (
            <div key={a.id} className="relative">
              <div className={`h-full rounded-xl border px-3 py-2.5 ${KIND_STYLE[a.kind]}`}>
                <div className="flex items-center gap-1.5">
                  <span className="num text-[11px] font-semibold text-slate-900">{a.id}</span>
                  <span className="text-[11px] text-slate-700">{a.name}</span>
                  <span className="num ml-auto text-[9.5px] text-slate-500">
                    {stageMs.has(a.id) ? ms(stageMs.get(a.id) as number) : '—'}
                  </span>
                </div>
                <div className="mt-1.5 space-y-0.5">
                  {a.io.map((x) => (
                    <div key={x} className="num text-[9.5px] text-slate-500">
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

        <div className="mt-3 rounded-xl border border-slate-200 bg-slate-100 p-3">
          <div className="mb-2 text-[11px] text-slate-600">A3 内部：四层门禁按顺序执行，任一层给出 block 即短路</div>
          <div className="grid gap-2 sm:grid-cols-4">
            {[
              ['G0', '数据完整性', '关键字段缺失 / 低置信度', 2, 'from-slate-100'],
              ['G1', '真实性', '7 条统计信号识别水号', 7, 'from-rose-100'],
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
                  <div className="num mt-1 text-[10px] text-live-700">
                    本次命中 {int0(result.gateHits[String(g)] ?? 0)} 人
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      </Panel>

      {/* ---- 一致性面板 ---- */}
      <Panel
        title="双实现一致性：同一份阈值下，TS 与 Python 判定完全相同"
        subtitle={
          c
            ? `由 scripts/verify-parity.mjs 在每次 npm run refresh 时重新生成并写入 public/data/consistency.json（耗时 ${c.elapsed_ms} ms，运行时 ${c.engine.runtime}）`
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
                title="判定级（全量 5,000 条）"
                main={`${int0(c.verdict.diff_count)} 条差异`}
                rows={[
                  ['比对条数', `${int0(c.verdict.matched)} / ${int0(c.verdict.total)}`],
                  ['一致率', pct1(c.verdict.match_rate, 2)],
                  ['TS 判定分布', `${c.verdict.counts_ts.pass} / ${c.verdict.counts_ts.review} / ${c.verdict.counts_ts.reject}`],
                  ['Python 判定分布', `${c.verdict.counts_python.pass} / ${c.verdict.counts_python.review} / ${c.verdict.counts_python.reject}`],
                ]}
                note={`比对字段：${c.verdict.fields_compared.join('、')}`}
              />
              <ParityCard
                ok={c.evidence.status === 'pass'}
                title="证据链级（逐条 reason）"
                main={`${int0(c.evidence.diff_records ?? 0)} 条差异`}
                rows={[
                  ['抽样记录', int0(c.evidence.compared_records ?? 0)],
                  ['比对 reason 行', int0(c.evidence.compared_reason_rows ?? 0)],
                  ['来源', c.evidence.source ?? '—'],
                ]}
                note={c.evidence.note}
              />
              <ParityCard
                ok={c.budget.status === 'pass'}
                title="预算 / 审计级"
                main={`${int0(c.budget.matched_arms ?? 0)} / ${int0(c.budget.compared_arms ?? 0)} 臂一致`}
                rows={[
                  ['反事实审计', `${int0(c.budget.matched_audits ?? 0)} / ${int0(c.budget.compared_audits ?? 0)} 一致`],
                  ['来源', (c.budget.source ?? []).join('、')],
                ]}
                note={`覆盖的臂（读 consistency.json 的 arms 数组）：${armNames.join('、')}。${
                  summaryOnlyArms.length > 0
                    ? `其中 ${summaryOnlyArms.join('、')} 在 Python 侧只落摘要（无逐人明细），故只比摘要标量、约束检查与 trace；`
                    : ''
                }其余臂含 selected[] 逐条金额/posts/顺序、约束检查与 trace 中文措辞逐字符。`}
              />
            </div>

            <div className="mt-3 grid gap-3 lg:grid-cols-[1.1fr_1fr]">
              <div>
                <div className="muted mb-1.5">
                  {int0((c.budget.arms ?? []).length)} 个分配臂逐项核对（读 consistency.json 的 arms 数组）
                </div>
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
                        <td className="td text-[11px] text-slate-600">
                          {ARM_LABEL[a.arm] ?? a.arm}
                          {a.per_person_compared === false && (
                            <span className="ml-1 text-[10px] text-amber-600/90" title="Python 侧只落摘要（无逐人明细），本臂只比摘要标量、约束检查与 trace">
                              仅摘要
                            </span>
                          )}
                        </td>
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
                <div className="muted mb-1.5">阈值与口径</div>
                <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
                  <KV k="阈值文件" v={c.thresholds.file} />
                  <KV k="阈值版本" v={c.thresholds.version ?? '—'} />
                  <KV k="分组数" v={`${c.thresholds.n_groups} 组`} />
                  <KV k="回退格子数" v={`${c.thresholds.n_fallback_cells ?? '—'} 个`} />
                  <KV k="数据集 sha256" v={<span title={c.dataset.sha256_from_reference ?? ''}>{(c.dataset.sha256_from_reference ?? '').slice(0, 16)}…</span>} />
                  <KV k="数据集种子" v={String(c.dataset.seed ?? '—')} />
                  <div className="muted mt-1.5">{c.thresholds.note}</div>
                  <div className="muted mt-1">{c.campaign_scope}</div>
                </div>
                <div className="mt-2 space-y-1.5">
                  {c.honesty_notes.map((n) => (
                    <Note key={n}>{n}</Note>
                  ))}
                </div>
              </div>
            </div>

            <Note tone="good">
              <GitCompare size={11} className="mr-1 inline" />
              为什么要做双实现？因为"前端把后端算过的结果画出来"证明不了引擎正确。TS 侧只读 Python 落盘的 thresholds.json、
              不做任何再标定，然后对 5,000 条数据逐字段（含中文 human_text 逐字符）比对 —— 任何一处口径写歪都会立刻变成非零差异。
            </Note>
          </>
        ) : (
          <Note tone="warn">consistency.json 缺失：请运行 npm run verify 生成。本板块不显示任何替代数字。</Note>
        )}
      </Panel>

      {/* ---- 数据流 & 产物清单 ---- */}
      <div className="grid gap-3 lg:grid-cols-[1fr_1.15fr]">
        <Panel title="数据流与真值边界" subtitle="谁能读 gt、谁不能，在架构上就分开了">
          <div className="space-y-2">
            {[
              ['datagen（固定种子）', 'data/kox_5000.json', '合成 5,000 达人 + gt 块；问题注入只改可观测信号的统计偏移', 'border-slate-200'],
              ['Python 标定', 'output/thresholds.json', '按 platform×follower_bucket 分组标定分位阈值，25 组 + 三级回退', 'border-emerald-200'],
              ['Python 判定/预算/审计', 'output/verdicts.json · budget.json · audit.json · metrics.json', '全量离线跑，作为参考实现与产物来源', 'border-emerald-200'],
              ['构建期 LLM runner', 'output/llm_bench.json · prompt_bench.json', '真调模型、记录真实 usage，结果固化；线上不再调用', 'border-indigo-200'],
              ['prepare-data.mjs', 'public/data/*.json', '字段白名单瘦身 + 记录 sha256/字节数到 manifest', 'border-live-200'],
              ['浏览器 TS 引擎', '本页所有实时数字', '读同一份 thresholds.json，现场跑门禁/预算/审计/评测', 'border-live-200'],
            ].map(([stage, out, desc, border]) => (
              <div key={String(stage)} className={`rounded-lg border ${border} bg-slate-50 px-2.5 py-2`}>
                <div className="flex flex-wrap items-baseline gap-1.5">
                  <span className="text-[11.5px] font-medium text-slate-800">{stage}</span>
                  <span className="num text-[10px] text-live-600">→ {out}</span>
                </div>
                <div className="muted mt-0.5">{desc}</div>
              </div>
            ))}
          </div>
          <Note tone="good">
            <ShieldCheck size={11} className="mr-1 inline" />
            门禁 / 预算 / LLM 三层源码里不允许出现对 gt、true_categories、is_fraud 的任何访问，由一条 AST 静态扫描测试守着。
            只有评测与审计可以读 gt，且必须用它当裁判而不是当特征。
          </Note>
        </Panel>

        <Panel
          title="前端消费的产物清单"
          subtitle="每个产物都记录了来源文件、字节数与 sha256，可逐个核对"
          right={<Badge className="border-slate-300 text-slate-600"><Hash size={10} className="mr-1" />manifest.json</Badge>}
          bodyClass="px-0 py-0"
        >
          <div className="max-h-[420px] overflow-auto">
            <table className="w-full">
              <thead className="sticky top-0 bg-white backdrop-blur">
                <tr className="hairline">
                  <th className="th">产物</th>
                  <th className="th">来源</th>
                  <th className="th text-right">源 / 上线字节</th>
                  <th className="th">sha256</th>
                  <th className="th text-center">状态</th>
                </tr>
              </thead>
              <tbody>
                {manifest.artifacts.map((a) => (
                  <tr key={a.key} className="hairline">
                    <td className="td num text-[11px]">
                      {a.key}
                      {a.note && (
                        <Hint text={a.note}>
                          <span className="ml-1 text-[9px] text-live-600">ⓘ</span>
                        </Hint>
                      )}
                    </td>
                    <td className="td num text-[10px] text-slate-500">{a.source}</td>
                    <td className="td num text-right text-[10.5px]">
                      {a.source_bytes ? `${(a.source_bytes / 1024).toFixed(0)}K` : '—'}
                      <span className="mx-1 text-slate-500">→</span>
                      {a.shipped_bytes ? `${(a.shipped_bytes / 1024).toFixed(0)}K` : '—'}
                    </td>
                    <td className="td num text-[10px] text-slate-500" title={a.source_sha256 ?? ''}>
                      {(a.source_sha256 ?? '').slice(0, 10) || '—'}
                    </td>
                    <td className="td text-center">
                      {a.present ? (
                        <CheckCircle2 size={12} className="mx-auto text-emerald-600" />
                      ) : (
                        <Hint text={a.reason ?? '未生成'}>
                          <XCircle size={12} className="mx-auto text-amber-600" />
                        </Hint>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(manifest.field_audit ?? []).length > 0 && (
            <div className="border-t border-slate-300 px-4 py-2.5">
              <div className="muted mb-1.5">
                关键字段在位自检（prepare-data 对可选产物<b className="text-slate-600">整份原样搬运、不做字段白名单</b>，
                所以 Python 侧新增字段会自动到前端；这里只核对页面上重口径结论依赖的字段是否真的在）
              </div>
              <div className="grid gap-1 lg:grid-cols-2">
                {(manifest.field_audit ?? []).map((f) => (
                  <div
                    key={`${f.artifact}.${f.path}`}
                    className={`flex items-start gap-1.5 rounded-lg border px-2 py-1.5 ${
                      f.present ? 'border-emerald-200 bg-emerald-50' : 'border-rose-200 bg-rose-50'
                    }`}
                  >
                    {f.present ? (
                      <CheckCircle2 size={11} className="mt-[2px] shrink-0 text-emerald-600" />
                    ) : (
                      <XCircle size={11} className="mt-[2px] shrink-0 text-rose-600" />
                    )}
                    <div className="min-w-0">
                      <div className="num truncate text-[10px] text-slate-600">
                        {f.artifact}.{f.path}
                      </div>
                      <div className="muted">{f.what}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
          {(manifest.warnings ?? []).length > 0 && (
            <div className="border-t border-slate-300 px-4 py-2">
              {(manifest.warnings ?? []).map((w) => (
                <Note key={w} tone="warn">
                  {w}
                </Note>
              ))}
            </div>
          )}
        </Panel>
      </div>

      {/* ---- 数据集与阈值元信息 ---- */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="数据集"
          value={`${int0(manifest.dataset.n)} 人`}
          hint={`版本 ${manifest.dataset.version} · 种子 ${manifest.dataset.seed} · sha256 ${manifest.dataset.sha256.slice(0, 10)}…`}
          icon={<Database size={11} />}
        />
        <Stat
          label="gt 判定分布"
          value={`${manifest.dataset.gt_verdict_counts.pass} / ${manifest.dataset.gt_verdict_counts.review} / ${manifest.dataset.gt_verdict_counts.reject}`}
          hint="pass / review / reject（库级判定，与门禁输出对比见评测页）"
        />
        <Stat
          label="阈值分组"
          value={`${int0(Number(thresholdsMeta.n_groups ?? 0))} 组`}
          hint={`platform × follower_bucket；样本不足的格子回退 ${int0(Number(thresholdsMeta.n_fallback_cells ?? 0))} 个（platform → global）`}
          icon={<FileJson size={11} />}
        />
        <Stat
          label="上线数据体积"
          value={`${(manifest.artifacts.reduce((a, x) => a + (x.shipped_bytes ?? 0), 0) / 1048576).toFixed(2)} MB`}
          hint="全部产物合计（含 5,000 条达人明细）；前端把它们全量读进内存后现算"
          tone="accent"
        />
      </div>

      <Panel title="注入率核对：数据生成器声明 vs 实际落盘" subtitle="连「注入了多少」都不靠记忆，直接读 manifest">
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(manifest.dataset.injection_actual_rates).map(([k, v]) => (
            <Badge key={k} className="border-slate-300 text-slate-700">
              {k}
              <span className="num ml-1 text-slate-900">{pct1(Number(v), 1)}</span>
            </Badge>
          ))}
        </div>
        <Note>
          水号相关注入合计 {pct1(
            ['bought_followers', 'engagement_pod', 'bot_comments', 'view_inflation'].reduce(
              (a, k) => a + Number(manifest.dataset.injection_actual_rates[k] ?? 0),
              0,
            ),
          )}
          ，与评测页的 gt 水号占比一致；标签错配 {pct1(Number(manifest.dataset.injection_actual_rates.tag_mismatch ?? 0))}、
          多源冲突 {pct1(Number(manifest.dataset.injection_actual_rates.source_conflict ?? 0))} 是 G2 的主要压力来源。
        </Note>
      </Panel>

      {result && (
        <Panel title="本次运行的性能账" subtitle="全部为 performance.now() 实测，不是估算">
          <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
            {result.stages.map((s) => (
              <div key={s.id} className="card px-2.5 py-2">
                <div className="num text-[10px] text-slate-500">{s.id}</div>
                <div className="num text-[15px] text-slate-900">{ms(s.elapsedMs)}</div>
                <div className="muted">{int0(s.items)} 条</div>
              </div>
            ))}
          </div>
          <Note>
            端到端 {ms(result.totalMs)}：其中门禁 {ms(result.stages.find((s) => s.id === 'A3')?.elapsedMs ?? 0)}（
            {int0(result.pool.length)} 人 × 20 条规则），预算分配 {ms(result.stages.find((s) => s.id === 'A5')?.elapsedMs ?? 0)}
            （含两臂 + 约束修正）。整份数据集 {compact(manifest.dataset.n)} 条全量在浏览器内存里，没有分页请求后端。
          </Note>
        </Panel>
      )}
    </div>
  );
}
