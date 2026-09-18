import React from 'react';
import { Activity, ChevronRight, Cpu, Filter, Gauge, Play, Search, Sparkles } from 'lucide-react';
import { DonutRing, Funnel, SparkBars } from '../components/charts';
import { EvidenceBody } from '../components/EvidenceDrawer';
import { Badge, CountUp, Drawer, Hint, Note, Panel, Segmented, Stat, Toggle, TruthChip } from '../components/ui';
import { CATEGORY_ZH, PLATFORM_LABEL } from '../engine/taxonomy';
import type { GateResult, Kox } from '../engine/types';
import type { BriefEntry } from '../lib/artifacts';
import {
  SKIP_REASON_LABEL,
  VERDICT_COLOR,
  VERDICT_HEX,
  VERDICT_LABEL,
  compact,
  fixed,
  int0,
  ms,
  pct1,
  usd0,
} from '../lib/format';
import type { PipelineResult, StageReport } from '../lib/pipeline';

const KIND_ICON: Record<StageReport['kind'], React.ReactElement> = {
  rule: <Cpu size={13} className="text-live-600" />,
  'llm-offline': <Sparkles size={13} className="text-indigo-600" />,
  audit: <Gauge size={13} className="text-fuchsia-600" />,
};

function StageCard({ s, index, live }: { s: StageReport; index: number; live: boolean }): React.ReactElement {
  return (
    <div
      className={`card relative overflow-hidden px-3.5 py-3 ${live ? 'border-live-300' : ''}`}
      style={{ animation: `fade-up .35s ease-out ${index * 0.05}s both` }}
    >
      {live && (
        <div className="pointer-events-none absolute inset-0 overflow-hidden">
          <div className="h-full w-1/3 animate-sweep bg-gradient-to-r from-transparent via-live-100 to-transparent" />
        </div>
      )}
      <div className="flex items-center gap-2">
        {KIND_ICON[s.kind]}
        <span className="text-[12px] font-semibold text-slate-900">{s.agent}</span>
        <span className="num ml-auto text-[10px] text-slate-500">{ms(s.elapsedMs)}</span>
      </div>
      <div className="muted mt-1">{s.title}</div>
      <div className="num mt-1.5 text-[13px] text-live-700">{s.headline}</div>
      <ul className="mt-2 space-y-1">
        {s.detail.map((d) => (
          <li key={d} className="flex gap-1.5 text-[10.5px] leading-relaxed text-slate-500">
            <span className="mt-[5px] inline-block h-1 w-1 shrink-0 rounded-full bg-slate-600" />
            <span>{d}</span>
          </li>
        ))}
      </ul>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <TruthChip kind={s.kind === 'rule' ? 'rule' : s.kind === 'audit' ? 'audit' : 'llm-offline'} />
        {s.kind === 'llm-offline' && (
          <Hint text={s.tokenNote ?? ''}>
            <Badge className="border-indigo-200 bg-indigo-50 text-indigo-700">
              {s.tokens === null ? 'token 账未生成' : `${int0(s.tokens)} token`}
            </Badge>
          </Hint>
        )}
        <span className="num text-[10px] text-slate-500">
          {int0(s.items)} 条输入 · {(s.elapsedMs / Math.max(s.items, 1)).toFixed(4)} ms/条
        </span>
      </div>
    </div>
  );
}

export function ConsoleTab({
  briefs,
  briefIdx,
  onBrief,
  result,
  stages,
  running,
  liveStage,
  koxById,
  includeReview,
  onIncludeReview,
  decay,
  onDecay,
  onRerun,
  datasetN,
  loadMs,
  koxBytes,
}: {
  briefs: BriefEntry[];
  briefIdx: number;
  onBrief: (i: number) => void;
  result: PipelineResult | null;
  stages: StageReport[];
  running: boolean;
  liveStage: number;
  koxById: Map<string, Partial<Kox>>;
  includeReview: boolean;
  onIncludeReview: (v: boolean) => void;
  decay: number;
  onDecay: (v: number) => void;
  onRerun: () => void;
  datasetN: number;
  loadMs: number;
  koxBytes: number | null;
}): React.ReactElement {
  const [verdictFilter, setVerdictFilter] = React.useState<'all' | 'pass' | 'review' | 'reject'>('all');
  const [q, setQ] = React.useState('');
  const [openId, setOpenId] = React.useState<string | null>(null);
  const brief = briefs[briefIdx];

  const rows = React.useMemo(() => {
    if (!result) return [];
    const out: Array<{ kox: Partial<Kox>; r: GateResult }> = [];
    for (const kox of result.pool) {
      const r = result.results.get(String(kox.kox_id));
      if (!r) continue;
      if (verdictFilter !== 'all' && r.verdict !== verdictFilter) continue;
      if (q && !String(kox.handle ?? '').toLowerCase().includes(q.toLowerCase()) && !String(kox.kox_id).includes(q)) continue;
      out.push({ kox, r });
    }
    const rank = { reject: 0, review: 1, pass: 2 };
    return out.sort((a, b) => rank[a.r.verdict] - rank[b.r.verdict] || b.r.fraud_score - a.r.fraud_score);
  }, [result, verdictFilter, q]);

  const allocById = React.useMemo(() => {
    const m = new Map<string, { amount_usd: number; posts: number; picked_by: string; value_score: number; efficiency: number }>();
    for (const a of result?.plan.selected ?? []) m.set(a.kox_id, a);
    return m;
  }, [result]);

  const open = openId ? koxById.get(openId) : null;
  const openResult = openId ? result?.results.get(openId) : null;

  return (
    <div className="space-y-4">
      {/* ---- brief 选择与控制台 ---- */}
      <Panel
        title="① 投放需求（brief）"
        subtitle="选一个预置 brief → 浏览器内立刻重跑 A1–A6。三个 brief 的 raw_text 与结构化 spec 都来自 data/briefs.json"
        right={
          <button
            onClick={onRerun}
            disabled={running}
            className="flex items-center gap-1.5 rounded-lg border border-live-300 bg-live-50 px-3 py-1.5 text-[12px] text-live-700 transition-colors hover:bg-live-100 disabled:opacity-50"
          >
            <Play size={12} />
            {running ? '运行中…' : '重跑流水线'}
          </button>
        }
      >
        <div className="grid gap-2 lg:grid-cols-3">
          {briefs.map((b, i) => (
            <button
              key={b.brief_id}
              onClick={() => onBrief(i)}
              className={`card card-hover px-3 py-2.5 text-left ${
                i === briefIdx ? 'border-live-300 bg-live-50 shadow-lift' : ''
              }`}
            >
              <div className="flex items-center gap-1.5">
                <span className="num text-[10px] text-slate-500">{b.brief_id}</span>
                {i === briefIdx && <Badge className="border-live-200 bg-live-50 text-live-700">当前</Badge>}
                <span className="num ml-auto text-[11px] text-slate-700">{usd0(b.spec.budget_usd)}</span>
              </div>
              <div className="mt-1 text-[12px] font-medium text-slate-900">{b.name}</div>
              <div className="muted mt-1 line-clamp-2">{b.raw_text}</div>
            </button>
          ))}
        </div>

        {brief && (
          <div className="mt-3 grid gap-3 lg:grid-cols-[1.15fr_1fr]">
            <div className="rounded-xl border border-slate-200 bg-slate-100 p-3">
              <div className="muted mb-1">brief 原文（A1 的输入）</div>
              <p className="text-[12px] leading-relaxed text-slate-700">{brief.raw_text}</p>
            </div>
            <div className="rounded-xl border border-slate-200 bg-slate-100 p-3">
              <div className="muted mb-1.5">A1 解析出的 CampaignSpec（判定与预算的唯一输入）</div>
              <div className="flex flex-wrap gap-1">
                {brief.spec.target_categories.map((c) => (
                  <Badge key={c} className="border-live-200 bg-live-50 text-live-700">
                    品类 {CATEGORY_ZH[c] ?? c}
                  </Badge>
                ))}
                {brief.spec.platforms.map((p) => (
                  <Badge key={p} className="border-slate-300 text-slate-700">
                    {PLATFORM_LABEL[p] ?? p}
                  </Badge>
                ))}
                {brief.spec.target_markets.map((m) => (
                  <Badge key={m} className="border-slate-300 text-slate-700">
                    市场 {m}
                  </Badge>
                ))}
                <Badge className="border-indigo-200 bg-indigo-50 text-indigo-700">KPI {brief.spec.kpi}</Badge>
                {brief.spec.target_gender && (
                  <Badge className="border-slate-300 text-slate-700">人群 {brief.spec.target_gender} · {brief.spec.target_age_buckets.join('/')}</Badge>
                )}
                {brief.spec.competitor_brands.map((c) => (
                  <Badge key={c} className="border-rose-200 bg-rose-50 text-rose-700">
                    回避 {c}
                  </Badge>
                ))}
                {brief.spec.regulated_category && (
                  <Badge className="border-amber-200 bg-amber-50 text-amber-700">
                    受管制口径 {brief.spec.regulated_category}
                  </Badge>
                )}
              </div>
              <div className="mt-2.5 flex flex-wrap items-center gap-3">
                <Toggle
                  checked={includeReview}
                  onChange={onIncludeReview}
                  label="把 review 也纳入预算候选"
                  hint="默认只买 pass。打开后 review（待人核）也参与分配，可以看到风险与规模的取舍"
                />
                <div className="flex items-center gap-1.5">
                  <span className="text-[11px] text-slate-500">第 n 条内容衰减</span>
                  <Segmented
                    size="sm"
                    value={String(decay)}
                    onChange={(v) => onDecay(Number(v))}
                    options={[
                      { value: '0.5', label: '0.5', hint: '第 n 条边际有效曝光按 0.5^(n-1) 衰减' },
                      { value: '0.7', label: '0.7', hint: '产物口径：0.7^(n-1)' },
                      { value: '0.9', label: '0.9', hint: '0.9^(n-1)' },
                    ]}
                  />
                </div>
              </div>
            </div>
          </div>
        )}
      </Panel>

      {/* ---- 流水线 ---- */}
      <Panel
        title="② 六个 Agent 的真实执行轨迹"
        subtitle="耗时是 performance.now() 实测值；带「浏览器内真算」标签的阶段现在就在你的机器上跑"
        right={
          result && (
            <span className="num text-[11px] text-slate-600">
              端到端 <CountUp value={result.totalMs} format={(v) => ms(v)} /> · 数据加载 {ms(loadMs)}
              {koxBytes !== null && ` · 数据集 ${(koxBytes / 1048576).toFixed(2)} MB`}
            </span>
          )
        }
      >
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {stages.map((s, i) => (
            <StageCard key={s.id} s={s} index={i} live={running && i === liveStage} />
          ))}
          {running &&
            Array.from({ length: Math.max(0, 6 - stages.length) }).map((_, i) => (
              <div key={`ph-${i}`} className="card flex h-[168px] items-center justify-center px-3.5 py-3">
                <span className="num text-[11px] text-slate-500">等待前序阶段…</span>
              </div>
            ))}
        </div>
      </Panel>

      {/* ---- 漏斗 + 判定分布 + 规则命中 ---- */}
      {result && (
        <div className="grid gap-3 lg:grid-cols-3">
          <Panel title="③ 从全库到可投的漏斗" subtitle="每一层的数字都是「到这一层为止一条规则都没触发」的人数">
            <Funnel steps={result.funnel} />
            <div className="mt-3 space-y-1.5">
              <div className="muted">A2 定向筛掉的原因分布：</div>
              {Object.entries(result.skipCounts)
                .sort((a, b) => b[1] - a[1])
                .map(([k, v]) => (
                  <div key={k} className="flex items-baseline justify-between gap-2">
                    <span className="text-[10.5px] text-slate-500">{SKIP_REASON_LABEL[k] ?? k}</span>
                    <span className="num text-[10.5px] text-slate-600">{int0(v)}</span>
                  </div>
                ))}
            </div>
          </Panel>

          <Panel title="④ 候选池判定分布" subtitle={`本 campaign 候选 ${int0(result.pool.length)} 人（不是全库 ${int0(datasetN)} 人）`}>
            <DonutRing
              segments={(['pass', 'review', 'reject'] as const).map((v) => ({
                key: v,
                label: VERDICT_LABEL[v],
                value: result.verdictCounts[v],
                color: VERDICT_HEX[v],
              }))}
              center={<CountUp value={result.pool.length} format={(v) => int0(v)} />}
              sub="候选人数"
            />
            <div className="mt-3 grid grid-cols-2 gap-2">
              {(['G0', 'G1', 'G2', 'G3'] as const).map((g) => (
                <div key={g} className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5">
                  <div className="text-[10px] text-slate-500">{g} 命中人数</div>
                  <div className="num text-[14px] text-slate-900">{int0(result.gateHits[g] ?? 0)}</div>
                </div>
              ))}
            </div>
          </Panel>

          <Panel title="⑤ 规则命中排行" subtitle="一个人可能同时命中多条；点柱子上方列表可下钻">
            <SparkBars items={result.ruleHits.map((r) => ({ key: r.rule_id, label: `${r.rule_id} ${r.label}`, value: r.n }))} />
            <div className="mt-2 max-h-[188px] space-y-1 overflow-y-auto pr-1">
              {result.ruleHits.map((r) => (
                <div key={r.rule_id} className="flex items-center gap-2">
                  <span className="num w-9 shrink-0 text-[10px] text-live-600">{r.rule_id}</span>
                  <span className="truncate text-[10.5px] text-slate-600">{r.label}</span>
                  <span className="num ml-auto text-[10.5px] text-slate-700">{int0(r.n)}</span>
                </div>
              ))}
              {result.ruleHits.length === 0 && <div className="muted">本候选池没有任何规则命中。</div>}
            </div>
          </Panel>
        </div>
      )}

      {/* ---- 达人清单 ---- */}
      {result && (
        <Panel
          title="⑥ 候选达人清单与证据链"
          subtitle="点任意一行打开证据链抽屉：每条命中都给出信号、实际值、阈值、同组分布位置与阈值来源"
          right={
            <div className="flex items-center gap-2">
              <div className="flex items-center gap-1 rounded-lg border border-slate-200 bg-slate-50 px-2 py-1">
                <Search size={11} className="text-slate-500" />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="搜 handle / ID"
                  className="w-28 bg-transparent text-[11px] text-slate-800 outline-none placeholder:text-slate-500"
                />
              </div>
              <Segmented
                size="sm"
                value={verdictFilter}
                onChange={setVerdictFilter}
                options={[
                  { value: 'all', label: `全部 ${result.pool.length}` },
                  { value: 'pass', label: `可投 ${result.verdictCounts.pass}` },
                  { value: 'review', label: `人核 ${result.verdictCounts.review}` },
                  { value: 'reject', label: `拒绝 ${result.verdictCounts.reject}` },
                ]}
              />
            </div>
          }
          bodyClass="px-0 py-0"
        >
          <div className="max-h-[520px] overflow-auto">
            <table className="w-full border-collapse">
              <thead className="sticky top-0 z-10 bg-white backdrop-blur">
                <tr className="hairline">
                  <th className="th">达人</th>
                  <th className="th">平台 / 地区</th>
                  <th className="th text-right">粉丝</th>
                  <th className="th text-right">平均播放</th>
                  <th className="th text-right">真实性</th>
                  <th className="th text-right">异常分</th>
                  <th className="th text-right">适配</th>
                  <th className="th">判定</th>
                  <th className="th">命中</th>
                  <th className="th text-right">分配</th>
                  <th className="th" />
                </tr>
              </thead>
              <tbody>
                {rows.map(({ kox, r }) => {
                  const alloc = allocById.get(r.kox_id);
                  return (
                    <tr
                      key={r.kox_id}
                      onClick={() => setOpenId(r.kox_id)}
                      className="hairline cursor-pointer transition-colors hover:bg-live-50"
                    >
                      <td className="td">
                        <div className="font-medium text-slate-800">{kox.handle}</div>
                        <div className="num text-[10px] text-slate-500">{r.kox_id}</div>
                      </td>
                      <td className="td text-[11px] text-slate-600">
                        {PLATFORM_LABEL[String(kox.platform)] ?? kox.platform}
                        <span className="mx-1 text-slate-500">/</span>
                        {kox.country}
                      </td>
                      <td className="td num text-right">{compact(kox.followers ?? null)}</td>
                      <td className="td num text-right">{compact(kox.avg_views ?? null)}</td>
                      <td className="td num text-right">{fixed(r.authenticity_score, 3)}</td>
                      <td className="td num text-right">
                        <span className={r.fraud_score > 0.4 ? 'text-rose-600' : r.fraud_score > 0.2 ? 'text-amber-600' : 'text-slate-600'}>
                          {fixed(r.fraud_score, 3)}
                        </span>
                      </td>
                      <td className="td num text-right">{fixed(r.fit_score, 2)}</td>
                      <td className="td">
                        <Badge className={VERDICT_COLOR[r.verdict]}>{VERDICT_LABEL[r.verdict]}</Badge>
                      </td>
                      <td className="td">
                        <div className="flex flex-wrap gap-1">
                          {r.reasons.length === 0 && <span className="text-[10px] text-emerald-600/80">无</span>}
                          {[...new Set(r.reasons.map((x) => x.rule_id))].slice(0, 4).map((id) => (
                            <span key={id} className="num rounded border border-slate-200 bg-slate-50 px-1 text-[9.5px] text-slate-600">
                              {id}
                            </span>
                          ))}
                          {r.reasons.length > 4 && <span className="text-[9.5px] text-slate-500">+{r.reasons.length - 4}</span>}
                        </div>
                      </td>
                      <td className="td num text-right">
                        {alloc ? (
                          <span className="text-live-700">
                            {usd0(alloc.amount_usd)}
                            <span className="ml-1 text-[10px] text-slate-500">{alloc.posts} 条</span>
                          </span>
                        ) : (
                          <span className="text-slate-500">—</span>
                        )}
                      </td>
                      <td className="td text-right">
                        <ChevronRight size={13} className="text-slate-500" />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {rows.length === 0 && (
              <div className="px-4 py-6 text-center text-[12px] text-slate-500">
                <Filter size={14} className="mx-auto mb-1.5 opacity-50" />
                没有符合筛选条件的达人
              </div>
            )}
          </div>
        </Panel>
      )}

      {result && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Stat
            label="本次真跑规模"
            value={<CountUp value={result.pool.length} format={(v) => int0(v)} />}
            hint={`全库 ${int0(datasetN)} 条 → 定向后 ${int0(result.pool.length)} 条进入四层门禁`}
            icon={<Activity size={11} />}
          />
          <Stat
            label="门禁单人平均耗时"
            value={`${((result.stages.find((s) => s.id === 'A3')?.elapsedMs ?? 0) / Math.max(result.pool.length, 1)).toFixed(3)} ms`}
            hint="纯 TS、单线程、无 WASM；四层 20 条规则全跑"
            tone="accent"
          />
          <Stat
            label="pass 率"
            value={pct1(result.verdictCounts.pass / Math.max(result.pool.length, 1))}
            hint={`可投 ${result.verdictCounts.pass} / 人核 ${result.verdictCounts.review} / 拒绝 ${result.verdictCounts.reject}`}
          />
          <Stat
            label="预算利用率"
            value={pct1(result.plan.utilization)}
            hint={`${usd0(result.plan.spent_usd)} / ${usd0(result.plan.budget_usd)}，选中 ${result.plan.n_selected} 人 ${result.plan.n_posts} 条`}
            tone="good"
          />
        </div>
      )}

      <Note>
        这一页没有任何预先算好的结果：切换 brief、打开 review、改衰减系数都会让下面所有数字重新算一遍。
        LLM 相关阶段（A1 brief 解析 / A4 语义适配）是构建期真调后固化的产物，线上不调 endpoint —— 原因写在「工程日志」页。
      </Note>

      <Drawer
        open={Boolean(open && openResult)}
        onClose={() => setOpenId(null)}
        title={`${open?.handle ?? ''} · 证据链`}
        subtitle={`${openId} · 同组 ${openResult?.group_key ?? ''} · 命中 ${openResult?.reasons.length ?? 0} 条规则`}
      >
        {open && openResult && (
          <EvidenceBody kox={open} result={openResult} allocation={allocById.get(openResult.kox_id) ?? null} />
        )}
      </Drawer>
    </div>
  );
}
