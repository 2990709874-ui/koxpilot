import React from 'react';
import { AlertTriangle, ChevronRight, Cpu, Filter, Gauge, Play, Search, Sparkles, Wand2 } from 'lucide-react';
import { DonutRing, Funnel, SparkBars } from '../components/charts';
import { Collapse, Verdict } from '../components/Collapse';
import { EvidenceBody } from '../components/EvidenceDrawer';
import { Badge, CountUp, Drawer, Hint, Note, Panel, Segmented, Toggle, TruthChip } from '../components/ui';
import { CATEGORY_ZH, PLATFORM_LABEL } from '../engine/taxonomy';
import { EXAMPLE_BRIEFS, parseBrief, type BriefParseResult } from '../engine/briefParse';
import { Thresholds, type ThresholdsPayload } from '../engine/thresholds';
import type { CampaignSpec, GateResult, Kox } from '../engine/types';
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
import { runPipeline, type PipelineResult, type StageReport } from '../lib/pipeline';

/* ------------------------------------------------------------------ */
/* 自由输入 brief 的运行结果共享                                        */
/* ------------------------------------------------------------------ */

/**
 * 自定义 brief 跑出来的流水线结果。
 *
 * 为什么要一个模块级小仓库：App.tsx 由他人并行改动（要把 Console 与 Decision 合成
 * 一个「投放决策」tab），两个组件的 props 签名不能变，所以「读者自己敲的那条 brief」
 * 的运行结果没法从 App 往下传。这里用一个最小的订阅仓库把它共享给 Decision，
 * 保证同一页里上下两半看到的是同一次运行，而不是上半自定义、下半还是预置 brief。
 */
export interface LiveRun {
  result: PipelineResult;
  parse: BriefParseResult;
  /** 这次运行的输入原文（Decision 用它标注「当前看的是自定义 brief」） */
  text: string;
}

let liveRun: LiveRun | null = null;
const liveSubs = new Set<(v: LiveRun | null) => void>();

function setLiveRun(v: LiveRun | null): void {
  liveRun = v;
  for (const f of liveSubs) f(v);
}

/** Decision 用它拿到「读者自己敲的那条 brief」的运行结果；没有则返回 null。 */
export function useLiveRun(): LiveRun | null {
  const [v, setV] = React.useState<LiveRun | null>(liveRun);
  React.useEffect(() => {
    liveSubs.add(setV);
    setV(liveRun);
    return () => {
      liveSubs.delete(setV);
    };
  }, []);
  return v;
}

/* ------------------------------------------------------------------ */
/* Agent 轨迹卡                                                        */
/* ------------------------------------------------------------------ */

const KIND_ICON: Record<StageReport['kind'], React.ReactElement> = {
  rule: <Cpu size={13} className="text-live-600" />,
  'llm-offline': <Sparkles size={13} className="text-indigo-600" />,
  audit: <Gauge size={13} className="text-fuchsia-600" />,
};

/**
 * 单个 Agent 的执行轨迹（紧凑行卡）。
 * 卡面只留三样：这一步做了什么、结果、耗时；逐项数字统一收进本板块底部的一个折叠。
 */
function StageCard({ s, index, live }: { s: StageReport; index: number; live: boolean }): React.ReactElement {
  return (
    <div
      className={`card relative overflow-hidden px-3 py-1.5 ${live ? 'border-live-300' : ''}`}
      style={{ animation: `fade-up .35s ease-out ${index * 0.05}s both` }}
    >
      {live && (
        <div className="pointer-events-none absolute inset-0 overflow-hidden">
          <div className="h-full w-1/3 animate-sweep bg-gradient-to-r from-transparent via-live-100 to-transparent" />
        </div>
      )}
      <div className="flex items-center gap-1.5">
        {KIND_ICON[s.kind]}
        <span className="truncate text-[12px] font-semibold text-slate-900" title={s.title}>
          {s.agent}
        </span>
        <span className="num ml-auto shrink-0 text-[11px] text-slate-600">{ms(s.elapsedMs)}</span>
      </div>
      <div className="num mt-1 truncate text-[12px] font-medium text-live-700" title={s.headline}>
        {s.headline}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 解析证据表                                                          */
/* ------------------------------------------------------------------ */

const STATUS_STYLE: Record<string, { label: string; cls: string }> = {
  hit: { label: '原文命中', cls: 'border-live-200 bg-live-50 text-live-700' },
  derived: { label: '规则推导', cls: 'border-indigo-200 bg-indigo-50 text-indigo-700' },
  default: { label: '未识别·用默认值', cls: 'border-amber-300 bg-amber-50 text-amber-700' },
};

/** 词表/常量名等代码标识符 → 业务说法：证据表里只出现人话。 */
const IDENT_ZH: Array<[RegExp, string]> = [
  [/CATEGORY_ADJACENCY/g, '相邻品类表'],
  [/COUNTRY_LANG/g, '国家语言表'],
  [/AGE_BUCKETS/g, '年龄分桶'],
  [/COMPETITOR_GROUPS/g, '竞品品牌库'],
  [/taxonomy/gi, '品类体系'],
];

function humanRule(text: string): string {
  let out = text;
  for (const [re, zh] of IDENT_ZH) out = out.replace(re, zh);
  return out.replace(/\b[A-Z][A-Z0-9_]{3,}\b/g, '内置词表');
}

function truncate(text: string, n: number): string {
  return text.length > n ? `${text.slice(0, n)}…` : text;
}

function EvidenceTable({ parse }: { parse: BriefParseResult }): React.ReactElement {
  return (
    <table className="w-full">
      <thead>
        <tr>
          <th className="th">字段</th>
          <th className="th">来源</th>
          <th className="th">命中原文片段</th>
          <th className="th">识别方式</th>
          <th className="th">解析值</th>
        </tr>
      </thead>
      <tbody>
        {parse.evidence.map((e, i) => (
          <tr key={`${String(e.field)}-${i}`} className="hairline">
            <td className="td text-slate-700">{e.label}</td>
            <td className="td">
              <Badge className={STATUS_STYLE[e.status].cls}>{STATUS_STYLE[e.status].label}</Badge>
            </td>
            <td className="td text-slate-700">
              {e.matched ? (
                <span className="rounded bg-live-50 px-1 py-0.5">{e.matched}</span>
              ) : (
                <span className="text-slate-500">—</span>
              )}
            </td>
            <td className="td">
              <Hint text={humanRule(e.ruleText)}>
                <span className="text-[11.5px] text-slate-600 underline decoration-dotted">
                  {truncate(humanRule(e.ruleText), 22)}
                </span>
              </Hint>
            </td>
            <td className="td num text-slate-800">{e.value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SpecChips({ spec }: { spec: CampaignSpec }): React.ReactElement {
  return (
    <div className="flex flex-wrap gap-1">
      <Badge className="border-slate-300 bg-white text-slate-700">预算 {usd0(spec.budget_usd ?? 0)}</Badge>
      {(spec.target_categories ?? []).map((c) => (
        <Badge key={c} className="border-live-200 bg-live-50 text-live-700">
          品类 {CATEGORY_ZH[c] ?? c}
        </Badge>
      ))}
      {(spec.platforms ?? []).map((p) => (
        <Badge key={p} className="border-slate-300 bg-white text-slate-700">
          {PLATFORM_LABEL[p] ?? p}
        </Badge>
      ))}
      {(spec.target_markets ?? []).map((m) => (
        <Badge key={m} className="border-slate-300 bg-white text-slate-700">
          市场 {m}
        </Badge>
      ))}
      <Badge className="border-indigo-200 bg-indigo-50 text-indigo-700">KPI {spec.kpi ?? '—'}</Badge>
      {spec.target_gender && (
        <Badge className="border-slate-300 bg-white text-slate-700">
          人群 {spec.target_gender === 'f' ? '女' : '男'}
          {(spec.target_age_buckets ?? []).length > 0 ? ` · ${spec.target_age_buckets.join('/')}` : ''}
        </Badge>
      )}
      {(spec.competitor_brands ?? []).map((c) => (
        <Badge key={c} className="border-rose-200 bg-rose-50 text-rose-700">
          回避 {c}
        </Badge>
      ))}
      {spec.regulated_category && (
        <Badge className="border-amber-300 bg-amber-50 text-amber-700">受管制口径 {spec.regulated_category}</Badge>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Console                                                            */
/* ------------------------------------------------------------------ */

export function ConsoleTab({
  briefs,
  briefIdx,
  onBrief,
  result: appResult,
  stages: appStages,
  running: appRunning,
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

  // ---- 自由输入 brief ----
  const [text, setText] = React.useState<string>(brief?.raw_text ?? '');
  const [custom, setCustom] = React.useState<LiveRun | null>(null);
  const [customRunning, setCustomRunning] = React.useState(false);
  const [customErr, setCustomErr] = React.useState<string | null>(null);
  const thrRef = React.useRef<Thresholds | null>(null);

  // 切换预置 brief / 改参数 → 回到预置口径，避免同一页上下两半看的不是同一次运行
  React.useEffect(() => {
    setText(briefs[briefIdx]?.raw_text ?? '');
    setCustom(null);
    setCustomErr(null);
    setLiveRun(null);
  }, [briefIdx, briefs, includeReview, decay]);

  const records = React.useMemo(() => [...koxById.values()], [koxById]);

  /** 线上解析 → A2~A6 在浏览器里真重算一遍。 */
  const runCustom = React.useCallback(async (): Promise<void> => {
    if (customRunning) return;
    setCustomRunning(true);
    setCustomErr(null);
    try {
      const parse = parseBrief(text, { campaignId: 'CUSTOM', name: '自定义 brief' });
      if (!thrRef.current) {
        const res = await fetch(`${import.meta.env.BASE_URL}data/thresholds.json`, { cache: 'force-cache' });
        if (!res.ok) throw new Error(`thresholds.json 加载失败：HTTP ${res.status}`);
        thrRef.current = Thresholds.fromDict((await res.json()) as ThresholdsPayload);
      }
      const r = await runPipeline(records, parse.spec, thrRef.current, {
        includeReview,
        decay,
        // 自定义运行不附带预置 brief 的 token 账
        llmPerTask: null,
        primaryModelKey: null,
      });
      // 自定义模式下 A1 的输出即刚跑完的规则解析结果
      const hit = parse.evidence.filter((e) => e.status === 'hit').length;
      const derived = parse.evidence.filter((e) => e.status === 'derived').length;
      const fallback = parse.evidence.filter((e) => e.status === 'default').length;
      r.stages[0] = {
        ...r.stages[0],
        agent: 'A1 BriefAgent',
        title: '自由输入原文 → 投放需求结构（规则解析）',
        kind: 'rule',
        elapsedMs: parse.elapsedMs,
        items: Math.max(text.length, 1),
        headline: `${hit} 字段原文命中 / ${derived} 规则推导 / ${fallback} 降级默认`,
        detail: [
          '解析方式：正则与词表匹配，在浏览器内完成',
          `解析耗时 ${parse.elapsedMs.toFixed(3)} ms，输入 ${text.length} 字`,
          ...parse.warnings,
        ],
        tokens: 0,
        tokenNote: undefined,
      };
      // token 账只随预置 brief 提供；自定义运行如实标注为「本次不附带」
      for (const st of r.stages) {
        if (st.kind === 'llm-offline' && st.tokens === null) {
          st.tokenNote = '本次自定义运行不附带 token 账；切回预置 brief 可查看模型调用账目';
        }
      }
      const run: LiveRun = { result: r, parse, text };
      setCustom(run);
      setLiveRun(run);
    } catch (e) {
      setCustom(null);
      setLiveRun(null);
      setCustomErr(e instanceof Error ? e.message : String(e));
    } finally {
      setCustomRunning(false);
    }
  }, [customRunning, text, records, includeReview, decay]);

  const result = custom?.result ?? appResult;
  const stages = custom ? custom.result.stages : appStages;
  const running = custom ? customRunning : appRunning;
  const isCustom = Boolean(custom);

  /** 预置 brief 的字段解析证据（与自定义运行同一套解析器）。 */
  const presetParse = React.useMemo(() => {
    const b = briefs[briefIdx];
    return b ? parseBrief(b.raw_text, { campaignId: b.spec.campaign_id, name: b.name }) : null;
  }, [briefs, briefIdx]);

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
  const gateMsPerPerson =
    result && result.pool.length > 0
      ? (result.stages.find((s) => s.id === 'A3')?.elapsedMs ?? 0) / result.pool.length
      : null;
  const activeParse = custom?.parse ?? presetParse;

  return (
    <div className="space-y-2">
      {/* ---- 本页结论 ---- */}
      <Verdict
        what="第一步：一句话需求 → 可投名单与预算"
        conclusion={
          result ? (
            <>
              {isCustom ? '你刚敲的这条 brief' : `预置 ${brief?.brief_id ?? ''}`}：全库 <b>{int0(datasetN)}</b> 条 → 召回{' '}
              <b>{int0(result.pool.length)}</b> 人 → 判可投 <b>{int0(result.verdictCounts.pass)}</b> 人 → 预算落到{' '}
              <b>{int0(result.plan.n_selected)}</b> 人 / {int0(result.plan.n_posts)} 条，全链路浏览器内实时计算。
            </>
          ) : (
            '流水线正在运行…'
          )
        }
        stats={[
          { label: '候选池', value: result ? int0(result.pool.length) : '—' },
          { label: '门禁 ms/人', value: gateMsPerPerson === null ? '—' : gateMsPerPerson.toFixed(3) },
          { label: 'pass 率', value: result ? pct1(result.verdictCounts.pass / Math.max(result.pool.length, 1)) : '—' },
          { label: '端到端', value: result ? ms(result.totalMs) : '—', tone: 'good' },
        ]}
        right={<TruthChip kind="rule" />}
      />

      {/* ---- ① 投放需求：预置 + 自由输入 ---- */}
      <Panel
        title="① 投放需求（brief）：可以改预置原文，也可以自己写一条"
        subtitle="敲完点「解析并重跑」→ 浏览器内规则解析出投放需求 → A2–A6 实时重算；原文未写明的竞品品牌不会被补全"
        right={
          <div className="flex items-center gap-2">
            <button
              onClick={onRerun}
              disabled={appRunning}
              className="focusable flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-[12px] text-slate-700 transition-colors hover:bg-slate-50 disabled:opacity-50"
            >
              <Play size={12} />
              {appRunning ? '运行中…' : '重跑预置'}
            </button>
            <button
              onClick={() => void runCustom()}
              disabled={customRunning}
              className="focusable flex items-center gap-1.5 rounded-lg border border-brand-600 bg-brand-600 px-3 py-1.5 text-[12px] font-medium text-white transition-colors hover:bg-brand-500 disabled:opacity-50"
            >
              <Wand2 size={12} />
              {customRunning ? '解析并重算中…' : '解析并重跑'}
            </button>
          </div>
        }
      >
        <div className="grid gap-2 lg:grid-cols-[1.05fr_1fr]">
          <div>
            <div className="flex flex-wrap items-center gap-1.5">
              {briefs.map((b, i) => (
                <button
                  key={b.brief_id}
                  onClick={() => onBrief(i)}
                  className={`focusable rounded-lg border px-2.5 py-1 text-[12px] transition-colors ${
                    i === briefIdx && !isCustom
                      ? 'border-brand-300 bg-brand-50 text-brand-700'
                      : 'border-slate-300 bg-white text-slate-700 hover:bg-slate-50'
                  }`}
                  title={b.raw_text}
                >
                  {b.brief_id} · {b.name}
                </button>
              ))}
            </div>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={2}
              spellCheck={false}
              placeholder="例：彩妆新品投巴西和墨西哥，预算 5 万美元，Instagram 为主，目标 18-24 岁女性，看互动。"
              className="focusable mt-2 w-full resize-y rounded-xl border border-slate-300 bg-white px-3 py-2 text-[13px] leading-relaxed text-slate-800 outline-none placeholder:text-slate-500 focus:border-brand-400"
            />
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <span className="muted">试试这个：</span>
              {EXAMPLE_BRIEFS.map((ex) => (
                <button
                  key={ex.label}
                  onClick={() => setText(ex.text)}
                  className="focusable rounded-md border border-slate-300 bg-slate-50 px-2 py-0.5 text-[11.5px] text-slate-700 hover:bg-white"
                  title={ex.text}
                >
                  {ex.label}
                </button>
              ))}
            </div>
            {customErr && (
              <Note tone="warn">
                <AlertTriangle size={11} className="mr-1 inline" />
                这条 brief 运行失败：{customErr}。页面保留上一次的结果。
              </Note>
            )}
            {custom && custom.parse.warnings.length > 0 && (
              <Note tone="warn">
                {custom.parse.warnings.map((w) => (
                  <div key={w}>· {w}</div>
                ))}
              </Note>
            )}
            {custom && custom.result.pool.length === 0 && (
              <Note tone="warn">
                这条 brief 的候选池是 <b>0 人</b>：平台 / 品类 / 市场三条定向条件把全库筛空，下游名单与预算随之为空。放宽市场或去掉平台限定再试。
              </Note>
            )}
          </div>

          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[12.5px] font-medium text-slate-800">
                {isCustom ? '解析出的投放需求' : `${brief?.brief_id ?? ''} 的投放需求`}
              </span>
              <Badge className="border-live-300 bg-live-50 text-live-700">
                {isCustom ? '刚刚解析' : '预置产物'}
              </Badge>
            </div>
            <div className="mt-2">
              <SpecChips spec={(result?.spec ?? brief?.spec) as CampaignSpec} />
            </div>
            <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-2">
              <Toggle
                checked={includeReview}
                onChange={onIncludeReview}
                label="把 review 也纳入预算候选"
                hint="默认只买 pass。打开后 review（待人核）也参与分配，可以看到风险与规模的取舍"
              />
              <div className="flex items-center gap-1.5">
                <span className="text-[12px] text-slate-600">第 n 条内容衰减</span>
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

        {/* ---- 字段解析证据 ---- */}
        <div className="mt-2">
          {activeParse && (
            <Collapse
              title={`展开：${activeParse.evidence.length} 个字段各自命中了原文的哪个片段、以什么方式识别`}
              hint={`原文命中 ${activeParse.evidence.filter((e) => e.status === 'hit').length} · 规则推导 ${
                activeParse.evidence.filter((e) => e.status === 'derived').length
              } · 未识别用默认值 ${activeParse.evidence.filter((e) => e.status === 'default').length}`}
            >
              <EvidenceTable parse={activeParse} />
            </Collapse>
          )}
        </div>
      </Panel>

      {/* ---- ② 执行轨迹 + 逐层筛人结果（合并成一块，控制信息密度） ---- */}
      <Panel
        title="② 六个 Agent 的执行轨迹与逐层筛人结果"
        subtitle={
          result
            ? `耗时为实测值；全库 ${int0(datasetN)} 条 → 候选 ${int0(result.pool.length)} 人 → 四层门禁判定分布`
            : '耗时为实测值；每一步都在当前浏览器内计算'
        }
        bodyClass="space-y-2"
        right={
          result && (
            <span className="num text-[12px] text-slate-600">
              端到端 <CountUp value={result.totalMs} format={(v) => ms(v)} /> · 数据加载 {ms(loadMs)}
              {koxBytes !== null && ` · 数据集 ${(koxBytes / 1048576).toFixed(2)} MB`}
            </span>
          )
        }
      >
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
          {stages.map((s, i) => (
            <StageCard key={s.id} s={s} index={i} live={running && !isCustom && i === liveStage} />
          ))}
          {running &&
            Array.from({ length: Math.max(0, 6 - stages.length) }).map((_, i) => (
              <div key={`ph-${i}`} className="card flex h-[58px] items-center justify-center px-3 py-2">
                <span className="num text-[11px] text-slate-600">等待前序阶段…</span>
              </div>
            ))}
        </div>
        {result && (
          <>
            <div className="grid gap-3 lg:grid-cols-[1fr_0.85fr_1fr]">
              <Funnel steps={result.funnel} />
              <div>
                <DonutRing
                  segments={(['pass', 'review', 'reject'] as const).map((v) => ({
                    key: v,
                    label: VERDICT_LABEL[v],
                    value: result.verdictCounts[v],
                    color: VERDICT_HEX[v],
                  }))}
                  size={96}
                  center={<CountUp value={result.pool.length} format={(v) => int0(v)} />}
                  sub="候选人数"
                />
              </div>
              <div>
                <div className="grid grid-cols-2 gap-1.5">
                  {(
                    [
                      ['G0', '资料完整性'],
                      ['G1', '真实性信号'],
                      ['G2', '匹配一致性'],
                      ['G3', '内容合规'],
                    ] as const
                  ).map(([g, zh]) => (
                    <div key={g} className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-1">
                      <div className="text-[11px] text-slate-600">{zh}命中</div>
                      <div className="num text-[14px] text-slate-900">{int0(result.gateHits[g] ?? 0)}</div>
                    </div>
                  ))}
                </div>
                <div className="mt-1.5">
                  <SparkBars items={result.ruleHits.slice(0, 6).map((r) => ({ key: r.rule_id, label: r.label, value: r.n }))} />
                </div>
              </div>
            </div>
          </>
        )}
        <Collapse title="展开：六个 Agent 的计算口径与逐项数字、定向筛选原因分布、风控规则逐条命中人数">
          <div className="space-y-3">
            <div className="grid gap-2.5 lg:grid-cols-2">
                    {stages.map((st) => (
                      <div key={st.id} className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2">
                        <div className="flex items-center gap-1.5 text-[12px] font-medium text-slate-800">
                          {st.agent}
                          <span className="num ml-auto text-[11px] text-slate-600">
                            {int0(st.items)} 条输入 · {(st.elapsedMs / Math.max(st.items, 1)).toFixed(4)} ms/条
                          </span>
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-1.5">
                          <TruthChip kind={st.kind} />
                          {st.kind === 'llm-offline' && (
                            <Hint text={st.tokenNote ?? ''}>
                              <Badge className="border-indigo-200 bg-indigo-50 text-indigo-700">
                                {st.tokens === null ? 'token 账本次未附带' : `${int0(st.tokens)} token`}
                              </Badge>
                            </Hint>
                          )}
                        </div>
                        <ul className="mt-1 space-y-0.5">
                          {st.detail.map((d) => (
                            <li key={d} className="flex gap-1.5 text-[12px] leading-relaxed text-slate-600">
                              <span className="mt-[6px] inline-block h-1 w-1 shrink-0 rounded-full bg-slate-400" />
                              <span>{d}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    ))}
            </div>
            {result && (
              <div className="grid gap-3 lg:grid-cols-2">
                  <div>
                    <div className="muted mb-1">定向筛选（平台 / 品类 / 市场）</div>
                    <div className="space-y-1">
                      {Object.entries(result.skipCounts)
                        .sort((a, b) => b[1] - a[1])
                        .map(([k, v]) => (
                          <div key={k} className="flex items-baseline justify-between gap-2">
                            <span className="text-[12px] text-slate-600">{SKIP_REASON_LABEL[k] ?? k}</span>
                            <span className="num text-[12px] text-slate-800">{int0(v)}</span>
                          </div>
                        ))}
                      {Object.keys(result.skipCounts).length === 0 && (
                        <div className="muted">本次没有人被定向筛掉（需求中没有平台 / 品类 / 市场限定）。</div>
                      )}
                    </div>
                  </div>
                  <div>
                    <div className="muted mb-1">风控规则命中人数</div>
                    <div className="max-h-[200px] space-y-1 overflow-y-auto pr-1">
                      {result.ruleHits.map((r) => (
                        <div key={r.rule_id} className="flex items-center gap-2">
                          <span className="truncate text-[12px] text-slate-600">{r.label}</span>
                          <span className="num ml-auto text-[12px] text-slate-800">{int0(r.n)}</span>
                        </div>
                      ))}
                      {result.ruleHits.length === 0 && <div className="muted">本候选池没有任何规则命中。</div>}
                    </div>
                  </div>
              </div>
            )}
          </div>
        </Collapse>
        {/* ---- ⑥ 候选清单（折叠，接住下半页的名单与预算） ---- */}
        {result && (
          <Collapse
            title={`展开：${int0(result.pool.length)} 人的逐人判定表与证据链（点任意一行看信号、实际值、阈值、同组分位）`}
            hint="下半页「名单与预算」只列被买下的人；这张表是他们被判成什么、为什么的完整底稿"
          >
            <div className="mb-2 flex flex-wrap items-center justify-end gap-2">
              <div className="flex items-center gap-1 rounded-lg border border-slate-300 bg-white px-2 py-1">
                <Search size={11} className="text-slate-500" />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="搜 handle / ID"
                  className="focusable w-28 bg-transparent text-[12px] text-slate-800 outline-none placeholder:text-slate-500"
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
            <div className="max-h-[420px] overflow-auto">
              <table className="w-full border-collapse">
                <thead className="sticky top-0 z-10 bg-white">
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
                        <td className="td text-[12px] text-slate-600">
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
                            {r.reasons.length === 0 && <span className="text-[11px] text-emerald-600">无</span>}
                            {[...new Set(r.reasons.map((x) => x.rule_id))].slice(0, 4).map((id) => (
                              <span key={id} className="num rounded border border-slate-200 bg-slate-50 px-1 text-[10px] text-slate-600">
                                {id}
                              </span>
                            ))}
                            {r.reasons.length > 4 && <span className="text-[10px] text-slate-500">+{r.reasons.length - 4}</span>}
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
                <div className="px-4 py-6 text-center text-[12px] text-slate-600">
                  <Filter size={14} className="mx-auto mb-1.5 opacity-60" />
                  没有符合筛选条件的达人
                </div>
              )}
            </div>
          </Collapse>
        )}
      </Panel>

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
