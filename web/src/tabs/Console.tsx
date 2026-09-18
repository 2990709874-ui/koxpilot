import React from 'react';
import { AlertTriangle, ChevronRight, Cpu, Filter, Gauge, Play, Search, Sparkles, Wand2 } from 'lucide-react';
import { DonutRing, Funnel, SparkBars } from '../components/charts';
import { Collapse, Verdict } from '../components/Collapse';
import { EvidenceBody } from '../components/EvidenceDrawer';
import { Badge, CountUp, Drawer, Hint, Note, Panel, Segmented, Toggle, TruthChip } from '../components/ui';
import { CATEGORY_ZH, PLATFORM_LABEL } from '../engine/taxonomy';
import {
  EXAMPLE_BRIEFS,
  diffSpec,
  diffSummary,
  parseBrief,
  type BriefParseResult,
  type SpecDiffRow,
} from '../engine/briefParse';
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
 * 单个 Agent 的执行轨迹卡。
 *
 * 改版要点：耗时 / 真实性标签 / 逐层筛掉多少人这三样是「真在算」的核心证据，留在卡面上；
 * 原来平铺的 3~5 行小灰字明细收进折叠，折叠标题写明里面是什么、有几条。
 */
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
        <span className="text-[12.5px] font-semibold text-slate-900">{s.agent}</span>
        <span className="num ml-auto text-[11px] text-slate-600">{ms(s.elapsedMs)}</span>
      </div>
      <div className="muted mt-0.5 truncate" title={s.title}>
        {s.title}
      </div>
      <div className="num mt-1.5 text-[13px] font-medium text-live-700">{s.headline}</div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <TruthChip kind={s.kind} />
        {s.kind === 'llm-offline' && (
          <Hint text={s.tokenNote ?? ''}>
            <Badge className="border-indigo-200 bg-indigo-50 text-indigo-700">
              {s.tokens === null ? 'token 账未生成' : `${int0(s.tokens)} token`}
            </Badge>
          </Hint>
        )}
        <span className="num text-[11px] text-slate-600">
          {int0(s.items)} 条输入 · {(s.elapsedMs / Math.max(s.items, 1)).toFixed(4)} ms/条
        </span>
      </div>
      {s.detail.length > 0 && (
        <div className="mt-2">
          <Collapse
            flag="detail"
            count={s.detail.length}
            title={`展开：${s.id} 这一步的口径与逐项数字`}
          >
            <ul className="space-y-1">
              {s.detail.map((d) => (
                <li key={d} className="flex gap-1.5 text-[12px] leading-relaxed text-slate-600">
                  <span className="mt-[6px] inline-block h-1 w-1 shrink-0 rounded-full bg-slate-400" />
                  <span>{d}</span>
                </li>
              ))}
            </ul>
          </Collapse>
        </div>
      )}
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

function EvidenceTable({ parse }: { parse: BriefParseResult }): React.ReactElement {
  return (
    <table className="w-full">
      <thead>
        <tr>
          <th className="th">字段</th>
          <th className="th">来源</th>
          <th className="th">命中原文片段</th>
          <th className="th">规则</th>
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
                <span className="text-slate-500">—（不来自原文）</span>
              )}
            </td>
            <td className="td">
              <Hint text={e.ruleText}>
                <span className="num text-[11px] text-slate-600 underline decoration-dotted">{e.rule}</span>
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

/** 「构建期 LLM 解析 vs 线上规则解析」逐字段对照表。 */
function DiffTable({ rows }: { rows: SpecDiffRow[] }): React.ReactElement {
  return (
    <table className="w-full">
      <thead>
        <tr>
          <th className="th">字段</th>
          <th className="th">构建期 LLM 解析（固化产物）</th>
          <th className="th">线上规则解析（现在算的）</th>
          <th className="th">一致</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={String(r.field)} className="hairline">
            <td className="td text-slate-700">{r.label}</td>
            <td className="td num text-slate-700">{r.llm}</td>
            <td className="td num text-live-700">{r.rule}</td>
            <td className="td">
              {r.same ? (
                <span className="text-[12px] text-emerald-600">一致</span>
              ) : (
                <span className="text-[12px] text-rose-600">{r.note}</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
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
      const parse = parseBrief(text, { campaignId: 'CUSTOM', name: '自定义 brief（线上规则解析）' });
      if (!thrRef.current) {
        const res = await fetch(`${import.meta.env.BASE_URL}data/thresholds.json`, { cache: 'force-cache' });
        if (!res.ok) throw new Error(`thresholds.json 加载失败：HTTP ${res.status}`);
        thrRef.current = Thresholds.fromDict((await res.json()) as ThresholdsPayload);
      }
      const r = await runPipeline(records, parse.spec, thrRef.current, {
        includeReview,
        decay,
        // 线上这一跑没有任何 LLM 调用，所以不传 llm_bench 的 token 账 —— 见下面对 A1 卡的重写
        llmPerTask: null,
        primaryModelKey: null,
      });
      // A1 在自定义模式下**不是**构建期 LLM 产物，而是刚刚跑完的规则解析：如实改写这张卡
      const hit = parse.evidence.filter((e) => e.status === 'hit').length;
      const derived = parse.evidence.filter((e) => e.status === 'derived').length;
      const fallback = parse.evidence.filter((e) => e.status === 'default').length;
      r.stages[0] = {
        ...r.stages[0],
        agent: 'A1 BriefAgent（线上规则版）',
        title: '自由输入原文 → CampaignSpec（briefParse.ts，纯规则）',
        kind: 'rule',
        elapsedMs: parse.elapsedMs,
        items: Math.max(text.length, 1),
        headline: `${hit} 字段原文命中 / ${derived} 规则推导 / ${fallback} 降级默认`,
        detail: [
          '线上这一步是规则解析，不是 LLM：正则 + 词表匹配，全部在你的浏览器里跑',
          `解析耗时 ${parse.elapsedMs.toFixed(3)} ms（performance.now() 实测），输入 ${text.length} 字`,
          ...parse.warnings,
        ],
        tokens: 0,
        tokenNote: undefined,
      };
      // A4 的 token 账只在预置 brief 下有意义（llm_bench.json 记的是那三条 brief 的构建期调用）；
      // 自定义运行不附带 token 账，措辞要说清是「本次没附带」，不能让人以为产物缺失
      for (const st of r.stages) {
        if (st.kind === 'llm-offline' && st.tokens === null) {
          st.tokenNote = '自定义 brief 的这次运行不附带构建期 token 账：llm_bench.json 记录的是三条预置 brief 的真实调用，切回预置即可看到账目';
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

  /** 预置 brief 的自比对：同一条原文，构建期 LLM 解析 vs 线上规则解析。 */
  const selfCompare = React.useMemo(
    () =>
      briefs.map((b) => {
        const parsed = parseBrief(b.raw_text, { campaignId: b.spec.campaign_id, name: b.name });
        const rows = diffSpec(b.spec, parsed.spec);
        return { brief: b, parsed, rows, sum: diffSummary(rows) };
      }),
    [briefs],
  );
  const compareTotal = selfCompare.reduce(
    (acc, x) => ({ same: acc.same + x.sum.same, total: acc.total + x.sum.total }),
    { same: 0, total: 0 },
  );
  const currentCompare = selfCompare[briefIdx] ?? null;

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
  const activeParse = custom?.parse ?? currentCompare?.parsed ?? null;

  return (
    <div className="space-y-3">
      {/* ---- 本页结论 ---- */}
      <Verdict
        what="第一步：一句话需求 → 可投名单与预算"
        conclusion={
          result ? (
            <>
              {isCustom ? '你刚敲的这条 brief' : `预置 ${brief?.brief_id ?? ''}`} 已在浏览器内跑完 A1–A6：全库{' '}
              <b>{int0(datasetN)}</b> 条 → 定向召回 <b>{int0(result.pool.length)}</b> 人 → 四层门禁判可投{' '}
              <b>{int0(result.verdictCounts.pass)}</b> 人 → 预算落到 <b>{int0(result.plan.n_selected)}</b> 人 /{' '}
              {int0(result.plan.n_posts)} 条。A2–A6 全部现算；A1 线上是<b>规则解析</b>，不是 LLM（下方明说）。
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
        subtitle="敲完点「解析并重跑」→ 规则解析出 CampaignSpec → A2~A6 在你的浏览器里重算一遍"
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
        <div className="grid gap-3 lg:grid-cols-[1.05fr_1fr]">
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
              rows={4}
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
                这次自定义运行失败了：{customErr}。页面保留上一次的预置结果，不用假数字顶上。
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
                这条 brief 的候选池是 <b>0 人</b>：平台 / 品类 / 市场三条硬筛选把全库筛空了，所以下游名单与预算都是空的
                —— 这是真实结果，不是报错。放宽市场或去掉平台限定再试。
              </Note>
            )}
          </div>

          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[12.5px] font-medium text-slate-800">
                {isCustom ? '线上规则解析出的 CampaignSpec' : `${brief?.brief_id ?? ''} 的 CampaignSpec（构建期 LLM 固化）`}
              </span>
              {isCustom ? (
                <Badge className="border-live-300 bg-live-50 text-live-700">规则解析 · 刚刚现算</Badge>
              ) : (
                <TruthChip kind="llm-offline" />
              )}
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

        {/* ---- 诚实性：线上是规则，不是 LLM ---- */}
        <div className="mt-3 rounded-xl border border-amber-300 bg-amber-50 px-3 py-2.5">
          <div className="text-[13px] font-medium text-amber-900">
            说明：线上这个 brief 解析器是<b>规则实现</b>，不是大模型。
          </div>
          <p className="body-text mt-1">
            你敲进去的 brief 由 <code className="num text-[12px] text-slate-800">src/engine/briefParse.ts</code>
            （正则 + 词表）在你的浏览器里解析，<b>本页不向任何模型 endpoint 发一个请求</b>。
            A1 的 LLM 版在<b>构建期真调过</b>，真实 token usage 落在{' '}
            <code className="num text-[12px] text-slate-800">output/llm_bench.json</code> 里、账目展示在 Cost &amp; Value 页；
            线上不调的原因是三条工程约束：静态部署没有服务端、不把 API key 打进前端包、要让任何人任何时刻打开都能复现同一组数字。
            两套实现的差距不含糊过去 —— 就在下面这张对照表里逐字段量化。
          </p>
        </div>

        {/* ---- 解析证据 + LLM/规则对照 ---- */}
        <div className="mt-2 grid gap-2 lg:grid-cols-2">
          {activeParse && (
            <Collapse
              flag="evidence"
              count={activeParse.evidence.length}
              title={`展开：这 ${activeParse.evidence.length} 个字段各自命中了原文的哪个片段、用的哪条规则`}
              hint={`原文命中 ${activeParse.evidence.filter((e) => e.status === 'hit').length} · 规则推导 ${
                activeParse.evidence.filter((e) => e.status === 'derived').length
              } · 未识别降级 ${activeParse.evidence.filter((e) => e.status === 'default').length}（降级项如实标注，不假装识别到）`}
            >
              <EvidenceTable parse={activeParse} />
            </Collapse>
          )}
          {currentCompare && (
            <Collapse
              flag="evidence"
              count={currentCompare.rows.filter((r) => !r.same).length}
              title={`展开：同一条 ${currentCompare.brief.brief_id}，构建期 LLM 解析 vs 线上规则解析差在哪（${currentCompare.sum.same}/${currentCompare.sum.total} 字段一致）`}
              hint={`3 条预置 brief 合计 ${compareTotal.same}/${compareTotal.total} 字段一致（${pct1(
                compareTotal.same / Math.max(compareTotal.total, 1),
              )}）；差异全部集中在竞品回避一格 —— LLM 会按品类常识补出原文没写的竞品，规则版只认原文出现过的品牌，补不出来，如实认。`}
            >
              <DiffTable rows={currentCompare.rows} />
              <div className="mt-2 space-y-1">
                {selfCompare.map((x) => (
                  <div key={x.brief.brief_id} className="flex items-baseline justify-between gap-2">
                    <span className="text-[12px] text-slate-600">
                      {x.brief.brief_id} {x.brief.name}
                    </span>
                    <span className="num text-[12px] text-slate-800">
                      {x.sum.same}/{x.sum.total} 一致（{pct1(x.sum.rate)}）
                    </span>
                  </div>
                ))}
              </div>
            </Collapse>
          )}
        </div>
      </Panel>

      {/* ---- ② 六个 Agent 的真实执行轨迹 ---- */}
      <Panel
        title="② 六个 Agent 的真实执行轨迹"
        subtitle="耗时是 performance.now() 实测值；带「浏览器内真算」标签的阶段就在你的机器上跑。逐项明细收在每张卡的折叠里"
        right={
          result && (
            <span className="num text-[12px] text-slate-600">
              端到端 <CountUp value={result.totalMs} format={(v) => ms(v)} /> · 数据加载 {ms(loadMs)}
              {koxBytes !== null && ` · 数据集 ${(koxBytes / 1048576).toFixed(2)} MB`}
            </span>
          )
        }
      >
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {stages.map((s, i) => (
            <StageCard key={s.id} s={s} index={i} live={running && !isCustom && i === liveStage} />
          ))}
          {running &&
            Array.from({ length: Math.max(0, 6 - stages.length) }).map((_, i) => (
              <div key={`ph-${i}`} className="card flex h-[132px] items-center justify-center px-3.5 py-3">
                <span className="num text-[12px] text-slate-600">等待前序阶段…</span>
              </div>
            ))}
        </div>
      </Panel>

      {/* ---- ③④⑤ 逐层筛掉多少人 ---- */}
      {result && (
        <div className="grid gap-2 lg:grid-cols-3">
          <Panel title="③ 从全库到可投的漏斗" subtitle="每层数字 =「到这一层为止一条规则都没触发」的人数">
            <Funnel steps={result.funnel} />
            <div className="mt-2">
              <Collapse
                flag="detail"
                count={Object.keys(result.skipCounts).length}
                title="展开：A2 定向筛人的原因分布（平台 / 品类 / 市场各筛掉多少）"
              >
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
                    <div className="muted">本次没有人被定向筛掉（spec 没有平台 / 品类 / 市场限定）。</div>
                  )}
                </div>
              </Collapse>
            </div>
          </Panel>

          <Panel title="④ 候选池判定分布" subtitle={`候选 ${int0(result.pool.length)} 人（不是全库 ${int0(datasetN)} 人）`}>
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
            <div className="mt-2 grid grid-cols-4 gap-1.5">
              {(['G0', 'G1', 'G2', 'G3'] as const).map((g) => (
                <div key={g} className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-1.5">
                  <div className="text-[11px] text-slate-600">{g} 命中</div>
                  <div className="num text-[14px] text-slate-900">{int0(result.gateHits[g] ?? 0)}</div>
                </div>
              ))}
            </div>
          </Panel>

          <Panel title="⑤ 规则命中排行" subtitle="一个人可能同时命中多条规则">
            <SparkBars items={result.ruleHits.map((r) => ({ key: r.rule_id, label: `${r.rule_id} ${r.label}`, value: r.n }))} />
            <div className="mt-2">
              <Collapse
                flag="detail"
                count={result.ruleHits.length}
                title="展开：20 条规则里哪几条真的在拦人（逐条命中人数）"
              >
                <div className="max-h-[220px] space-y-1 overflow-y-auto pr-1">
                  {result.ruleHits.map((r) => (
                    <div key={r.rule_id} className="flex items-center gap-2">
                      <span className="num w-9 shrink-0 text-[11px] text-live-700">{r.rule_id}</span>
                      <span className="truncate text-[12px] text-slate-600">{r.label}</span>
                      <span className="num ml-auto text-[12px] text-slate-800">{int0(r.n)}</span>
                    </div>
                  ))}
                  {result.ruleHits.length === 0 && <div className="muted">本候选池没有任何规则命中。</div>}
                </div>
              </Collapse>
            </div>
          </Panel>
        </div>
      )}

      {/* ---- ⑥ 候选清单（折叠，接住下半页的名单与预算） ---- */}
      {result && (
        <Collapse
          flag="detail"
          count={result.pool.length}
          title={`展开：${int0(result.pool.length)} 人的逐人判定表与证据链（点任意一行看信号、实际值、阈值、同组分位、阈值来源）`}
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

      {/* ---- 接上下半页的叙事 ---- */}
      {result && (
        <div className="card px-3.5 py-2.5">
          <p className="body-text">
            这条链路的产出是 <b>{int0(result.verdictCounts.pass)}</b> 个可投达人。下面「名单与预算」把{' '}
            {usd0(result.plan.budget_usd)} 真的分配到人头上，再和「按粉丝量买」的基线做两臂对照 ——
            同一次运行、同一份候选池，唯一差异是选人依据。
          </p>
        </div>
      )}

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
