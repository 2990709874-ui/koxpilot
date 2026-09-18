import React from 'react';
import {
  ArrowRight,
  Check,
  ChevronDown,
  Loader2,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { loadArtifacts, type Artifacts } from '../lib/artifacts';
import { runPipeline, type PipelineResult } from '../lib/pipeline';
import { browserArms, contractCount, effectiveViews, leadingArm, valuePerDollar, wasteShare } from '../lib/planView';
import type { AllocationArm } from '../lib/api';
import { plan as callPlan } from '../lib/api';
import { startProbe, useComputeSource } from '../lib/computeSource';
import { compact, int0, pct1, usd0 } from '../lib/format';
import { parseBrief } from '../engine/briefParse';
import type { CampaignSpec, GateResult, Kox } from '../engine/types';
import {
  GATE_ASK,
  GATE_NAME,
  KPI_ZH,
  TIER_RANGE,
  ageText,
  armHow,
  armName,
  category,
  country,
  funnelLabel,
  gender,
  platform,
  reasonPlain,
  rulePlain,
  tier,
} from './plain';

const GATES = ['G0', 'G1', 'G2', 'G3'] as const;
const REPO = 'https://github.com/';

/* ------------------------------------------------------------------ */
/* 小组件                                                             */
/* ------------------------------------------------------------------ */

function Card({
  children,
  className = '',
}: {
  children: React.ReactNode;
  className?: string;
}): React.ReactElement {
  return <section className={`rounded-2xl border border-slate-200 bg-white shadow-card ${className}`}>{children}</section>;
}

function StepTitle({
  n,
  title,
  sub,
}: {
  n: number;
  title: string;
  sub?: string;
}): React.ReactElement {
  return (
    <div className="flex items-start gap-3">
      <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-brand-600 text-[12px] font-semibold text-white">
        {n}
      </span>
      <div>
        <h2 className="text-[16px] font-semibold tracking-tight text-slate-900">{title}</h2>
        {sub && <p className="mt-0.5 text-[13px] leading-relaxed text-slate-500">{sub}</p>}
      </div>
    </div>
  );
}

function Chip({ children, tone = 'slate' }: { children: React.ReactNode; tone?: 'slate' | 'brand' }): React.ReactElement {
  const cls =
    tone === 'brand'
      ? 'border-brand-200 bg-brand-50 text-brand-700'
      : 'border-slate-200 bg-slate-50 text-slate-700';
  return <span className={`inline-flex items-center rounded-lg border px-2 py-1 text-[12.5px] font-medium ${cls}`}>{children}</span>;
}

/** 首屏四个大数字。单位与对照都写在数字下面，不需要读者自己换算。 */
function BigNumber({
  label,
  value,
  sub,
  tone = 'slate',
}: {
  label: string;
  value: string;
  sub: React.ReactNode;
  tone?: 'slate' | 'green' | 'amber' | 'brand';
}): React.ReactElement {
  const color =
    tone === 'green' ? 'text-emerald-600' : tone === 'amber' ? 'text-amber-600' : tone === 'brand' ? 'text-brand-600' : 'text-slate-900';
  return (
    <div className="px-1">
      <div className="text-[13px] text-slate-500">{label}</div>
      <div className={`num mt-1 text-[30px] font-semibold leading-none tracking-tight ${color}`}>{value}</div>
      <div className="mt-1.5 text-[12.5px] leading-relaxed text-slate-500">{sub}</div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 页面                                                               */
/* ------------------------------------------------------------------ */

type Stage = 'idle' | 'running' | 'done';

export default function Page(): React.ReactElement {
  const [art, setArt] = React.useState<Artifacts | null>(null);
  const [loadErr, setLoadErr] = React.useState<string | null>(null);
  const cs = useComputeSource();

  const [briefIdx, setBriefIdx] = React.useState(0);
  const [custom, setCustom] = React.useState('');
  const [useCustom, setUseCustom] = React.useState(false);
  const [spec, setSpec] = React.useState<CampaignSpec | null>(null);
  const [result, setResult] = React.useState<PipelineResult | null>(null);
  const [stage, setStage] = React.useState<Stage>('idle');
  const [serviceLine, setServiceLine] = React.useState<string | null>(null);
  const [openGate, setOpenGate] = React.useState<string | null>(null);
  const [showAll, setShowAll] = React.useState(false);
  const [showHow, setShowHow] = React.useState(false);

  React.useEffect(() => {
    startProbe();
    loadArtifacts()
      .then(setArt)
      .catch((e: Error) => setLoadErr(e.message));
  }, []);

  /** 跑一次：浏览器引擎出结果（不依赖服务是否在线），同时让服务对同一条需求跑一遍做核对。 */
  const run = React.useCallback(
    async (nextSpec: CampaignSpec, briefId: string | null) => {
      if (!art) return;
      setStage('running');
      setResult(null);
      setOpenGate(null);
      setShowAll(false);
      setServiceLine(null);
      await new Promise((r) => setTimeout(r, 30));
      const r = await runPipeline(art.records, nextSpec, art.thresholds, {
        llmPerTask: (art.llmBench?.per_task ?? null) as never,
        primaryModelKey: (art.llmBench?.primary_model_key as string | undefined) ?? null,
      });
      setResult(r);
      setStage('done');

      // 服务在线时让 Python 侧跑同一条需求，逐人核对判定，只报一句结论。
      const res = await callPlan(briefId ? { brief_id: briefId } : { brief_text: nextSpec.raw_text });
      if (!res.ok) {
        setServiceLine(null);
        return;
      }
      const rows = res.data.parity_payload?.verdicts ?? [];
      let diff = 0;
      for (const row of rows) {
        const mine = r.results.get(String(row.kox_id));
        if (mine && mine.verdict !== row.verdict) diff += 1;
      }
      setServiceLine(
        diff === 0
          ? `服务端已就同一需求独立执行，${int0(rows.length)} 位达人的判定结果一致`
          : `服务端就同一需求独立执行，${int0(diff)} 位达人的判定结果存在差异，已记录待查`,
      );
    },
    [art],
  );

  // 首次加载完自动跑第一条，首屏就有结论，不让人先看一个空壳。
  React.useEffect(() => {
    if (!art || stage !== 'idle') return;
    const b = art.briefs[0];
    if (!b) return;
    setSpec(b.spec);
    void run(b.spec, b.brief_id);
  }, [art, stage, run]);

  const pickBrief = (i: number): void => {
    if (!art) return;
    const b = art.briefs[i];
    if (!b) return;
    setBriefIdx(i);
    setUseCustom(false);
    setSpec(b.spec);
    void run(b.spec, b.brief_id);
  };

  const runCustom = (): void => {
    const text = custom.trim();
    if (!text || !art) return;
    const parsed = parseBrief(text, { campaignId: 'CUSTOM', name: '自定义需求' });
    setUseCustom(true);
    setSpec(parsed.spec);
    void run(parsed.spec, null);
  };

  if (loadErr) {
    return (
      <div className="flex min-h-screen items-center justify-center p-6">
        <Card className="max-w-md px-5 py-4">
          <div className="text-[14px] font-semibold text-rose-600">数据加载失败</div>
          <p className="mt-1.5 text-[13px] text-slate-600">{loadErr}</p>
        </Card>
      </div>
    );
  }

  if (!art) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3">
        <Loader2 size={20} className="animate-spin text-brand-600" />
        <div className="text-[13px] text-slate-600">正在加载 5,000 位海外达人档案…</div>
      </div>
    );
  }

  const arms = result
    ? browserArms({
        records: art.records,
        spec: result.spec,
        thresholds: art.thresholds,
        results: result.results,
        plan: result.plan,
        baseline: result.baseline,
        gtById: result.gtById,
        decay: result.decay,
      })
    : [];

  return (
    <div className="min-h-screen">
      {/* ---------------- 顶栏：一句话说明这是什么 ---------------- */}
      <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-[1120px] items-center gap-3 px-5 py-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-600">
            <ShieldCheck size={16} className="text-white" />
          </div>
          <div className="leading-tight">
            <div className="text-[15px] font-semibold tracking-tight text-slate-900">KOXPilot</div>
            <div className="text-[12px] text-slate-500">出海达人营销投前决策 · 资质筛查与预算分配</div>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <span className="hidden items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[12px] text-slate-600 sm:inline-flex">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  cs.source === 'backend' ? 'bg-emerald-500' : cs.source === 'browser' ? 'bg-slate-400' : 'animate-pulse bg-brand-500'
                }`}
              />
              {cs.source === 'backend' ? '服务端执行' : cs.source === 'browser' ? '本地执行' : '连接中'}
            </span>
            <a
              className="rounded-lg border border-slate-200 px-2.5 py-1 text-[12px] font-medium text-slate-600 transition hover:border-brand-300 hover:text-brand-700"
              href={REPO}
              target="_blank"
              rel="noreferrer"
            >
              源码与文档
            </a>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1120px] space-y-4 px-5 pb-16 pt-5">
        {/* ---------------- 1 说需求 ---------------- */}
        <Card className="px-5 py-4">
          <StepTitle n={1} title="投放需求" sub="需求变更后，以下全部结论重新计算" />
          <div className="mt-3 flex flex-wrap gap-2">
            {art.briefs.map((b, i) => (
              <button
                key={b.brief_id}
                onClick={() => pickBrief(i)}
                className={`rounded-xl border px-3 py-2 text-left text-[13px] transition ${
                  !useCustom && briefIdx === i
                    ? 'border-brand-400 bg-brand-50 text-brand-800 shadow-sm'
                    : 'border-slate-200 bg-white text-slate-600 hover:border-brand-300'
                }`}
              >
                {b.name}
              </button>
            ))}
          </div>

          <div className="mt-3 rounded-xl bg-slate-50 px-4 py-3">
            <p className="text-[14px] leading-relaxed text-slate-800">
              “{useCustom ? custom : art.briefs[briefIdx]?.raw_text}”
            </p>
            {spec && (
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                <Chip tone="brand">预算 {usd0(spec.budget_usd)}</Chip>
                {spec.platforms.length > 0 && <Chip>{spec.platforms.map(platform).join(' / ')}</Chip>}
                {spec.target_markets.length > 0 && <Chip>{spec.target_markets.map(country).join('、')}</Chip>}
                {spec.target_categories.length > 0 && <Chip>{spec.target_categories.map(category).join('、')}</Chip>}
                {ageText(spec.target_age_buckets) && <Chip>{ageText(spec.target_age_buckets)}</Chip>}
                {gender(spec.target_gender) && <Chip>{gender(spec.target_gender)}</Chip>}
                <Chip>{KPI_ZH[spec.kpi] ?? spec.kpi}</Chip>
                {spec.competitor_brands.length > 0 && <Chip>规避竞品 {spec.competitor_brands.join('、')}</Chip>}
              </div>
            )}
          </div>

          <div className="mt-3 flex flex-col gap-2 sm:flex-row">
            <input
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') runCustom();
              }}
              placeholder="也可输入自定义需求，例如：家用按摩仪投放沙特与阿联酋，预算 3 万美元，目标人群 30–45 岁男性，以 TikTok 为主，考核转化"
              className="flex-1 rounded-xl border border-slate-200 px-3.5 py-2.5 text-[13px] text-slate-800 outline-none transition placeholder:text-slate-400 focus:border-brand-400"
            />
            <button
              onClick={runCustom}
              disabled={!custom.trim() || stage === 'running'}
              className="inline-flex items-center justify-center gap-1.5 rounded-xl bg-brand-600 px-4 py-2.5 text-[13px] font-medium text-white transition hover:bg-brand-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              <Sparkles size={14} />
              生成投放方案
            </button>
          </div>
        </Card>

        {/* ---------------- 2 结论 ---------------- */}
        {stage === 'running' || !result ? (
          <Card className="px-5 py-8">
            <div className="flex items-center justify-center gap-2 text-[13px] text-slate-500">
              <Loader2 size={16} className="animate-spin text-brand-600" />
              正在完成候选筛查与预算分配…
            </div>
          </Card>
        ) : (
          <Conclusion result={result} arms={arms} serviceLine={serviceLine} />
        )}

        {/* ---------------- 3 怎么筛的 ---------------- */}
        {result && <GateSection result={result} openGate={openGate} onOpen={setOpenGate} records={art.records} />}

        {/* ---------------- 4 买了谁 ---------------- */}
        {result && <Roster result={result} showAll={showAll} onToggle={() => setShowAll((v) => !v)} records={art.records} />}

        {/* ---------------- 5 跟常见做法比 ---------------- */}
        {result && arms.length > 0 && <Compare arms={arms} />}

        {/* ---------------- 6 这东西怎么搭的（默认收起） ---------------- */}
        <Card className="px-5 py-4">
          <button className="flex w-full items-center gap-2 text-left" onClick={() => setShowHow((v) => !v)}>
            <ChevronDown size={16} className={`text-slate-400 transition ${showHow ? 'rotate-0' : '-rotate-90'}`} />
            <span className="text-[14px] font-semibold text-slate-900">实现说明</span>
            <span className="text-[12.5px] text-slate-500">面向技术评估，不影响上述结论</span>
          </button>
          {showHow && <HowItWorks art={art} serviceOnline={cs.source === 'backend'} />}
        </Card>
      </main>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 2 结论                                                             */
/* ------------------------------------------------------------------ */

function Conclusion({
  result,
  arms,
  serviceLine,
}: {
  result: PipelineResult;
  arms: AllocationArm[];
  serviceLine: string | null;
}): React.ReactElement {
  const plan = result.plan;
  const rejectN = result.verdictCounts.reject ?? 0;
  const saved = result.audit.saved_usd;
  const util = plan.budget_usd > 0 ? plan.spent_usd / plan.budget_usd : 0;
  const reviewN = result.verdictCounts.review ?? 0;

  return (
    <Card className="px-5 py-5">
      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
        <BigNumber
          label="建议签约"
          value={`${int0(plan.n_selected)} 位`}
          sub={`自 ${int0(result.pool.length)} 位符合定向条件的达人中选出`}
          tone="brand"
        />
        <BigNumber
          label="预算投入"
          value={usd0(plan.spent_usd)}
          sub={`预算 ${usd0(plan.budget_usd)}，使用率 ${pct1(util)}`}
        />
        <BigNumber
          label="判定不予投放"
          value={`${int0(rejectN)} 位`}
          sub={
            reviewN > 0
              ? `另有 ${int0(reviewN)} 位数据存疑，建议人工复核后再定，本次未进入名单`
              : '数据异常、人群不符或存在品牌风险'
          }
          tone="amber"
        />
        <BigNumber
          label={saved >= 0 ? '相较「按粉丝量投放」减少的无效支出' : '相较「按粉丝量投放」增加的无效支出'}
          value={usd0(Math.abs(saved))}
          sub={`事后核算：该部分预算原本将投向刷量账号或高风险账号，占总预算 ${pct1(Math.abs(result.audit.saved_share_of_budget))}`}
          tone={saved >= 0 ? 'green' : 'amber'}
        />
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-slate-100 pt-3">
        <span className="text-[12.5px] text-slate-500">
          预计曝光 {compact(plan.est_total_views)} 次，千次曝光成本 ${plan.est_cpm_usd.toFixed(2)}
        </span>
        {serviceLine && (
          <span className="inline-flex items-center gap-1.5 text-[12.5px] text-emerald-700">
            <Check size={13} />
            {serviceLine}
          </span>
        )}
        {arms.length > 0 && <TopLine arms={arms} />}
      </div>
    </Card>
  );
}

/** 一句人话结论：这次谁最划算，以及代价是什么。数字全部现算。 */
function TopLine({ arms }: { arms: AllocationArm[] }): React.ReactElement | null {
  const us = arms.find((a) => a.arm === 'koxpilot');
  const lead = leadingArm(arms, valuePerDollar);
  if (!us || !lead) return null;
  if (lead.arm === 'koxpilot') {
    return (
      <span className="text-[12.5px] text-slate-500">三种投放方式中，本方案的单位预算效率最高。</span>
    );
  }
  const leadWaste = wasteShare(lead);
  const ourWaste = wasteShare(us);
  return (
    <span className="text-[12.5px] text-slate-500">
      本次「{armName(lead.arm)}」的单位预算效率更高，代价是需签约 {int0(contractCount(lead) ?? 0)} 份合约
      {leadWaste !== null && ourWaste !== null
        ? `，且 ${pct1(leadWaste)} 的预算投向刷量账号（本方案为 ${pct1(ourWaste)}）`
        : ''}
      。
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* 3 怎么筛的                                                         */
/* ------------------------------------------------------------------ */

function GateSection({
  result,
  openGate,
  onOpen,
  records,
}: {
  result: PipelineResult;
  openGate: string | null;
  onOpen: (g: string | null) => void;
  records: Partial<Kox>[];
}): React.ReactElement {
  const steps = result.funnel;
  // 5,000 人的库和筛完的几百人差两个量级，同一把尺子会让后面几步全挤成一小截，
  // 所以第一行单独按满格画（它就是 100%），后面几步用「硬条件筛完的人数」当基准。
  const top = steps[1]?.value ?? steps[0]?.value ?? 1;

  return (
    <Card className="px-5 py-4">
      <StepTitle
        n={2}
        title="候选筛查与拦截过程"
        sub="自 5,000 位达人库逐环节筛选至最终名单；点击任一环节，查看该环节的拦截对象与判定依据"
      />
      <div className="mt-3 space-y-1.5">
        {steps.map((s, i) => {
          const prev = i === 0 ? s.value : (steps[i - 1]?.value ?? s.value);
          const cut = prev - s.value;
          const isGate = (GATES as readonly string[]).includes(s.key);
          const open = openGate === s.key;
          return (
            <div key={s.key}>
              <button
                disabled={!isGate}
                onClick={() => onOpen(open ? null : s.key)}
                className={`flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left transition ${
                  isGate ? 'hover:bg-slate-50' : 'cursor-default'
                } ${open ? 'bg-brand-50' : ''}`}
              >
                <div className="w-[112px] shrink-0 text-[13px] font-medium leading-snug text-slate-800 sm:w-[158px]">
                  {isGate ? GATE_NAME[s.key] : funnelLabel(s.key)}
                </div>
                <div className="h-6 flex-1 overflow-hidden rounded-md bg-slate-100">
                  <div
                    className={`flex h-full items-center justify-end rounded-md pr-2 text-[11px] font-medium transition-all duration-700 ${
                      i === 0 ? 'bg-slate-300 text-slate-700' : 'bg-gradient-to-r from-brand-400 to-brand-600 text-white'
                    }`}
                    style={{ width: i === 0 ? '100%' : `${Math.max(8, Math.min(100, (s.value / top) * 100))}%` }}
                  >
                    {int0(s.value)} 位
                  </div>
                </div>
                <div className="w-[88px] shrink-0 text-right text-[12.5px] text-slate-500 sm:w-[104px]">
                  {cut > 0 ? `拦截 ${int0(cut)} 位` : i === 0 ? '基准' : '无拦截'}
                </div>
              </button>
              {open && <GateDetail result={result} gate={s.key} records={records} />}
            </div>
          );
        })}
      </div>
      <p className="mt-2.5 text-[12.5px] leading-relaxed text-slate-500">
        四个环节依次执行：{GATES.map((g) => `${GATE_NAME[g]}`).join(' → ')}。被拦截的账号分两类 —— 判定不予投放，以及数据存疑需人工复核；两类均不自动进入名单。
      </p>
    </Card>
  );
}

/** 某一步挡掉的人：3 个例子 + 这一步的高频原因。 */
function GateDetail({
  result,
  gate,
  records,
}: {
  result: PipelineResult;
  gate: string;
  records: Partial<Kox>[];
}): React.ReactElement {
  const handleOf = React.useMemo(() => {
    const m = new Map<string, string>();
    for (const r of records) m.set(String(r.kox_id), String(r.handle ?? r.kox_id));
    return m;
  }, [records]);
  const hits: Array<{ id: string; r: GateResult }> = [];
  for (const [id, r] of result.results) {
    if (r.reasons.some((x) => x.gate === gate)) hits.push({ id, r });
    if (hits.length >= 200) break;
  }
  const examples = hits.slice(0, 3);
  const byRule = new Map<string, number>();
  for (const h of hits) {
    for (const reason of h.r.reasons) {
      if (reason.gate !== gate) continue;
      byRule.set(reason.rule_id, (byRule.get(reason.rule_id) ?? 0) + 1);
    }
  }
  const topRules = [...byRule.entries()].sort((a, b) => b[1] - a[1]).slice(0, 4);

  return (
    <div className="mx-3 mb-2 rounded-xl border border-brand-100 bg-brand-50/50 px-4 py-3">
      <div className="text-[12.5px] text-slate-600">{GATE_ASK[gate]}</div>
      {topRules.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {topRules.map(([rule, n]) => (
            <span key={rule} className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-[12px] text-slate-700">
              {rulePlain(rule)} · {int0(n)} 位
            </span>
          ))}
        </div>
      )}
      {examples.length > 0 && (
        <div className="mt-2.5 space-y-1.5">
          {examples.map(({ id, r }) => {
            const first = r.reasons.find((x) => x.gate === gate);
            return (
              <div key={id} className="flex flex-wrap items-baseline gap-2 text-[12.5px]">
                <span className="font-medium text-slate-800">{handleOf.get(id) ?? id}</span>
                <span className="text-slate-500">{rulePlain(String(first?.rule_id ?? ''))}</span>
                <span className="text-slate-400">·</span>
                <span className="text-slate-500">{first ? reasonPlain(first) : ''}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 4 名单                                                             */
/* ------------------------------------------------------------------ */

function Roster({
  result,
  showAll,
  onToggle,
  records,
}: {
  result: PipelineResult;
  showAll: boolean;
  onToggle: () => void;
  records: Partial<Kox>[];
}): React.ReactElement {
  const rows = showAll ? result.plan.selected : result.plan.selected.slice(0, 8);
  const byId = React.useMemo(() => {
    const m = new Map<string, Partial<Kox>>();
    for (const r of records) m.set(String(r.kox_id), r);
    return m;
  }, [records]);

  return (
    <Card className="px-5 py-4">
      <StepTitle
        n={3}
        title="投放名单与预算分配"
        sub={`共 ${int0(result.plan.n_selected)} 位，合计 ${usd0(result.plan.spent_usd)}；单个达人最多采买 ${result.plan.purchase_model.max_posts_per_kox} 条内容`}
      />
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[820px]">
          <thead>
            <tr className="border-b border-slate-200">
              <th className="th">达人</th>
              <th className="th">平台与市场</th>
              <th className="th text-right">粉丝量</th>
              <th className="th text-right">报价</th>
              <th className="th text-right">分配预算</th>
              <th className="th text-right">采买条数</th>
              <th className="th">入选依据</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => {
              const kox = byId.get(String(a.kox_id));
              const gate = result.results.get(String(a.kox_id));
              return (
                <tr key={a.kox_id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50/60">
                  <td className="td font-medium text-slate-800">
                    {a.handle}
                    {a.verdict === 'review' && (
                      <span className="ml-1.5 rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[11px] text-amber-700">
                        需人工复核
                      </span>
                    )}
                  </td>
                  <td className="td text-slate-600">
                    {platform(a.platform)} · {country(a.country)}
                    <span className="ml-1.5 text-slate-400" title={TIER_RANGE[a.bucket]}>
                      {tier(a.bucket)}
                    </span>
                  </td>
                  <td className="td num text-right text-slate-700">{compact(kox?.followers ?? 0)}</td>
                  <td className="td num text-right text-slate-600">
                    {usd0(kox?.quoted_price_usd ?? null)}
                    {a.price_estimated && <span className="ml-1 text-[11px] text-slate-400">估</span>}
                  </td>
                  <td className="td num text-right font-medium text-slate-900">{usd0(a.amount_usd)}</td>
                  <td className="td num text-right text-slate-600">{a.posts}</td>
                  <td className="td text-slate-600">
                    画像匹配 {pct1(a.audience_match)} · 数据{gate && gate.reasons.length > 0 ? '存在轻微异常，未越线' : '无异常'} · 预计曝光{' '}
                    {compact(a.est_views)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {result.plan.selected.length > 8 && (
        <button
          onClick={onToggle}
          className="mt-3 inline-flex items-center gap-1 text-[12.5px] font-medium text-brand-700 hover:text-brand-800"
        >
          {showAll ? '收起，仅显示前 8 位' : `查看全部 ${int0(result.plan.selected.length)} 位`}
          <ArrowRight size={13} />
        </button>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* 5 三种买法对比                                                      */
/* ------------------------------------------------------------------ */

function Compare({ arms }: { arms: AllocationArm[] }): React.ReactElement {
  const order = ['follower_rank', 'diversified_no_gate', 'koxpilot'];
  const sorted = [...arms].sort((a, b) => order.indexOf(a.arm) - order.indexOf(b.arm));
  const lead = leadingArm(arms, valuePerDollar);

  return (
    <Card className="px-5 py-4">
      <StepTitle
        n={4}
        title="与两种常见投放方式的对照"
        sub="同一笔预算，事后按标注结果核算；投向刷量账号的曝光不计入有效曝光"
      />
      <div className="mt-3 grid gap-3 lg:grid-cols-3">
        {sorted.map((a) => {
          const vpd = valuePerDollar(a);
          const ws = wasteShare(a);
          const isUs = a.arm === 'koxpilot';
          const isLead = lead?.arm === a.arm;
          return (
            <div
              key={a.arm}
              className={`rounded-xl border px-4 py-3.5 ${isUs ? 'border-brand-300 bg-brand-50/50' : 'border-slate-200 bg-white'}`}
            >
              <div className="flex items-center justify-between">
                <div className="text-[14px] font-semibold text-slate-900">{armName(a.arm)}</div>
                {isLead && (
                  <span className="rounded border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 text-[11px] font-medium text-emerald-700">
                    单位预算效率最高
                  </span>
                )}
              </div>
              <p className="mt-1 text-[12px] leading-relaxed text-slate-500">{armHow(a.arm)}</p>
              <dl className="mt-3 space-y-2">
                <Row label="预算投入" value={usd0(a.spend)} />
                <Row label="有效曝光" value={compact(effectiveViews(a))} />
                <Row label="每美元有效曝光" value={vpd === null ? '—' : `${vpd.toFixed(1)} 次`} strong />
                <Row
                  label="投向刷量账号的支出"
                  value={a.waste_usd === undefined ? '—' : `${usd0(a.waste_usd)}${ws === null ? '' : `（${pct1(ws)}）`}`}
                  tone={a.waste_usd && a.waste_usd > 0 ? 'bad' : 'good'}
                />
                <Row label="签约数量" value={`${int0(contractCount(a) ?? 0)} 份`} />
              </dl>
            </div>
          );
        })}
      </div>
      <p className="mt-3 border-t border-slate-100 pt-3 text-[12.5px] leading-relaxed text-slate-500">
        判优口径为「每美元有效曝光」。签约数量属于执行成本：预算越分散，单位曝光越便宜，但报价、寄样、审稿与结算的人力成本相应增加，该部分未计入上述指标。
      </p>
    </Card>
  );
}

function Row({
  label,
  value,
  strong = false,
  tone,
}: {
  label: string;
  value: string;
  strong?: boolean;
  tone?: 'good' | 'bad';
}): React.ReactElement {
  const color = tone === 'bad' ? 'text-rose-600' : tone === 'good' ? 'text-emerald-600' : 'text-slate-900';
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="text-[12.5px] text-slate-500">{label}</dt>
      <dd className={`num text-[13.5px] ${strong ? 'font-semibold' : ''} ${color}`}>{value}</dd>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 6 怎么搭的（折叠）                                                  */
/* ------------------------------------------------------------------ */

function HowItWorks({ art, serviceOnline }: { art: Artifacts; serviceOnline: boolean }): React.ReactElement {
  const steps = [
    ['需求解析', '将一句话需求拆解为预算、平台、市场、人群与考核目标'],
    ['候选召回', '按平台、品类、市场三项硬性条件筛选'],
    ['四道筛查', '资料完整性 / 数据真实性 / 人群匹配度 / 品牌安全'],
    ['匹配评分', '评估内容与本次投放的贴合度'],
    ['预算分配', '在结构约束下，按边际效率分配预算'],
    ['事后核算', '与另两种投放方式对照，核算减少的无效支出'],
  ];
  const strict = (art.metrics?.table_1_fraud_detection?.strict ?? null) as { recall?: number; precision?: number } | null;
  const recall = Number(strict?.recall ?? 0);
  const precision = Number(strict?.precision ?? 0);

  return (
    <div className="mt-3 space-y-3">
      <div className="flex flex-wrap gap-2">
        {steps.map(([name, what], i) => (
          <div key={name} className="flex items-center gap-2">
            <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
              <div className="text-[12.5px] font-medium text-slate-800">{name}</div>
              <div className="text-[11.5px] text-slate-500">{what}</div>
            </div>
            {i < steps.length - 1 && <ArrowRight size={13} className="text-slate-300" />}
          </div>
        ))}
      </div>
      <ul className="space-y-1.5 text-[12.5px] leading-relaxed text-slate-600">
        <li>
          · 前端提交需求后，六个环节在 Python 服务端执行并返回结果；
          {serviceOnline ? '当前服务端在线。' : '服务未连接时，由浏览器内同一套规则实现接续执行，功能不缺项。'}
        </li>
        <li>
          · 评测口径：{int0(Number(art.manifest.dataset.n))} 位达人全部带人工标注。刷量账号召回率 {pct1(recall)}；判定为刷量的账号中{' '}
          {pct1(precision)} 与标注一致。完整口径与已知短板见仓库文档。
        </li>
        <li>· 数据为按平台真实分布生成的合成数据，不含任何真实达人信息。</li>
      </ul>
      <a
        className="inline-flex items-center gap-1 text-[12.5px] font-medium text-brand-700 hover:text-brand-800"
        href={REPO}
        target="_blank"
        rel="noreferrer"
      >
        查看源码、评测报告与技术文档
        <ArrowRight size={13} />
      </a>
    </div>
  );
}
