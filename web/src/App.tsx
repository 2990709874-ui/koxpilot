import React from 'react';
import {
  Activity,
  BarChart3,
  Coins,
  Layers,
  Loader2,
  NotebookPen,
  ShieldCheck,
  Terminal,
} from 'lucide-react';
import { Badge, Hint } from './components/ui';
import { ArchitectureTab } from './tabs/Architecture';
import { ConsoleTab } from './tabs/Console';
import { CostValueTab } from './tabs/CostValue';
import { DecisionTab } from './tabs/Decision';
import { EvaluationTab } from './tabs/Evaluation';
import { NotesTab } from './tabs/Notes';
import { TabErrorBoundary } from './components/TabErrorBoundary';
import { GATE_ROLES, ablationVariant, evaluateDataset, runPipeline, sensitivityScan } from './lib/pipeline';
import type { AblationRow, EvalReport, PipelineResult, SensitivityPoint, StageReport } from './lib/pipeline';
import { loadArtifacts, type Artifacts } from './lib/artifacts';
import type { Kox } from './engine/types';
import { ALL_GATES } from './engine/policy';
import { int0, usd0 } from './lib/format';

const TABS = [
  { id: 'console', label: '决策台', icon: Terminal, desc: '真跑一遍 A1–A6' },
  { id: 'decision', label: '决策与预算', icon: Coins, desc: '选谁、花多少、为什么' },
  { id: 'eval', label: '评测', icon: BarChart3, desc: '好在哪、差在哪' },
  { id: 'cost', label: '成本与价值', icon: Activity, desc: 'Prompt 迭代与 token 账' },
  { id: 'arch', label: '架构', icon: Layers, desc: '编排与双实现一致性' },
  { id: 'notes', label: '工程日志', icon: NotebookPen, desc: '我自己抓到的问题' },
] as const;

type TabId = (typeof TABS)[number]['id'];

const frame = (): Promise<void> => new Promise((r) => setTimeout(r, 16));

const isTabId = (v: string): v is TabId => TABS.some((t) => t.id === v);
const tabFromHash = (): TabId => {
  const raw = window.location.hash.replace(/^#\/?/, '').trim();
  return isTabId(raw) ? raw : 'console';
};

export default function App(): React.ReactElement {
  const [art, setArt] = React.useState<Artifacts | null>(null);
  const [loadErr, setLoadErr] = React.useState<string | null>(null);
  const [tab, setTab] = React.useState<TabId>(tabFromHash);

  React.useEffect(() => {
    const onHash = (): void => setTab(tabFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const goTab = React.useCallback((id: TabId): void => {
    window.location.hash = `#${id}`;
    setTab(id);
  }, []);

  const [briefIdx, setBriefIdx] = React.useState(0);
  const [includeReview, setIncludeReview] = React.useState(false);
  const [decay, setDecay] = React.useState(0.7);

  const [result, setResult] = React.useState<PipelineResult | null>(null);
  const [stages, setStages] = React.useState<StageReport[]>([]);
  const [running, setRunning] = React.useState(false);
  const [liveStage, setLiveStage] = React.useState(0);
  const [runToken, setRunToken] = React.useState(0);

  const [evalReport, setEvalReport] = React.useState<EvalReport | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [ablation, setAblation] = React.useState<AblationRow[]>([]);
  const [sens, setSens] = React.useState<SensitivityPoint[]>([]);

  React.useEffect(() => {
    loadArtifacts()
      .then(setArt)
      .catch((e: Error) => setLoadErr(e.message));
  }, []);

  const koxById = React.useMemo(() => {
    const m = new Map<string, Partial<Kox>>();
    for (const r of art?.records ?? []) m.set(String(r.kox_id), r);
    return m;
  }, [art]);

  // ---- 每次参数变化都真跑一遍流水线（带阶段动画）----
  React.useEffect(() => {
    if (!art) return;
    let cancelled = false;
    const spec = art.briefs[briefIdx]?.spec;
    if (!spec) return;
    setRunning(true);
    setStages([]);
    setLiveStage(0);
    void (async () => {
      const r = await runPipeline(art.records, spec, art.thresholds, {
        includeReview,
        decay,
        llmPerTask: (art.llmBench?.per_task ?? null) as never,
        primaryModelKey: (art.llmBench?.primary_model_key as string | undefined) ?? null,
        onStage: async (stage, index) => {
          if (cancelled) return;
          setStages((prev) => [...prev, stage]);
          setLiveStage(index);
          await frame();
          await new Promise<void>((res) => setTimeout(res, 90));
        },
      });
      if (cancelled) return;
      setResult(r);
      setRunning(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [art, briefIdx, includeReview, decay, runToken]);

  // ---- 评测复算（按需，浏览器内真算）----
  const runEval = React.useCallback(async () => {
    if (!art) return;
    setBusy('全库 5,000 条重跑判定并重算指标…');
    await frame();
    const rep = evaluateDataset(art.records, art.thresholds);
    setEvalReport(rep);
    setAblation([]);
    setBusy(null);
  }, [art]);

  const runAblation = React.useCallback(async () => {
    if (!art) return;
    let full = evalReport;
    if (!full) {
      setBusy('先算全量基准…');
      await frame();
      full = evaluateDataset(art.records, art.thresholds);
      setEvalReport(full);
    }
    const rows: AblationRow[] = [];
    for (const gate of ALL_GATES) {
      setBusy(`关掉 ${gate} 后重跑全库…`);
      await frame();
      rows.push(
        ablationVariant(art.records, art.thresholds, full, {
          variant: `-${gate}`,
          role: GATE_ROLES[gate],
          enabledGates: ALL_GATES.filter((g) => g !== gate),
        }),
      );
      setAblation([...rows]);
    }
    setBusy(null);
  }, [art, evalReport]);

  const runSens = React.useCallback(async () => {
    if (!art) return;
    let full = evalReport;
    if (!full) {
      setBusy('先算全量基准…');
      await frame();
      full = evaluateDataset(art.records, art.thresholds);
      setEvalReport(full);
    }
    const signals = ((art.metrics?.table_5_sensitivity?.scaled_signals ?? []) as string[]) ?? [];
    const factors = ((art.metrics?.table_5_sensitivity?.factors ?? [0.8, 0.9, 1.0, 1.1, 1.2]) as number[]).map(Number);
    const points: SensitivityPoint[] = [];
    for (const f of factors) {
      setBusy(`阈值 ×${f.toFixed(1)} 重跑全库…`);
      await frame();
      points.push(...sensitivityScan(art.records, art.thresholds, full, [f], signals.length ? signals : undefined));
      setSens([...points]);
    }
    setBusy(null);
  }, [art, evalReport]);

  if (loadErr) {
    return (
      <div className="flex min-h-screen items-center justify-center p-6">
        <div className="glass max-w-lg px-5 py-4">
          <div className="text-[13px] font-semibold text-rose-300">产物加载失败</div>
          <p className="muted mt-1.5">{loadErr}</p>
          <p className="muted mt-2">
            请先在 koxpilot/web 目录下运行 <code className="font-mono text-cyan-200">npm run prepare-data</code>，
            把 Python 侧的 data/ 与 output/ 产物搬运到 public/data。本页不会用任何占位数字代替缺失产物。
          </p>
        </div>
      </div>
    );
  }

  if (!art) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3">
        <Loader2 size={20} className="animate-spin text-cyan-300" />
        <div className="num text-[12px] tracking-[0.2em] text-cyan-200">加载 5,000 条达人数据与阈值表…</div>
        <div className="muted">加载完成后引擎会在你的浏览器里真跑一遍，不是播放录像</div>
      </div>
    );
  }

  const c = art.consistency;
  const diffCount = c ? c.verdict.diff_count : null;
  const savedTotal = (art.audit?.counterfactual_value_audit?.totals?.saved_usd ?? null) as number | null;

  return (
    <div className="min-h-screen">
      {/* ================= Header ================= */}
      <header className="sticky top-0 z-40 border-b border-white/[0.07] bg-ink-900/80 backdrop-blur-md">
        <div className="mx-auto max-w-[1560px] px-4 py-2.5">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <div className="flex items-center gap-2.5">
              <div className="relative flex h-8 w-8 items-center justify-center rounded-lg border border-cyan-300/35 bg-cyan-400/10">
                <ShieldCheck size={15} className="text-cyan-300" />
              </div>
              <div>
                <div className="flex items-baseline gap-2">
                  <h1 className="text-[15px] font-semibold tracking-tight text-slate-50">KOXPilot</h1>
                  <span className="text-[11px] text-slate-400">出海达人营销 · 投前决策智能体</span>
                </div>
                <div className="muted">
                  不是"帮你找达人"，而是<b className="text-slate-400">先把不该投的人挡掉</b>，再把预算分配得经得起问
                </div>
              </div>
            </div>

            <div className="ml-auto flex flex-wrap items-center gap-1.5">
              <Hint
                text={
                  c
                    ? `verify-parity.mjs 于每次 npm run refresh 时重新生成 consistency.json：判定级 ${int0(c.verdict.matched)}/${int0(c.verdict.total)} 一致，证据链 ${int0(c.evidence.compared_records ?? 0)} 条记录 / ${int0(c.evidence.compared_reason_rows ?? 0)} 行 reason 一致，预算与审计 ${int0(c.budget.matched_arms ?? 0)}/${int0(c.budget.compared_arms ?? 0)} 臂一致。`
                    : 'consistency.json 未生成'
                }
              >
                <Badge
                  className={
                    diffCount === 0
                      ? 'border-emerald-400/35 bg-emerald-400/10 text-emerald-200'
                      : 'border-rose-400/35 bg-rose-400/10 text-rose-200'
                  }
                >
                  TS / Python 一致性差异 {diffCount === null ? '未生成' : int0(diffCount)} 条
                </Badge>
              </Hint>
              <Hint text={`数据集 sha256 ${art.manifest.dataset.sha256}｜种子 ${art.manifest.dataset.seed}｜全部为合成数据`}>
                <Badge className="border-white/15 text-slate-300">
                  {int0(art.manifest.dataset.n)} 达人 · 合成数据
                </Badge>
              </Hint>
              {savedTotal !== null && (
                <Hint text="以 ground truth 为裁判，对照「按粉丝量降序买」的反事实审计结果（三个 campaign 合计，其中 BRIEF-002 为负，未剔除）">
                  <Badge className="border-cyan-400/30 bg-cyan-400/10 text-cyan-200">少浪费 {usd0(savedTotal)}</Badge>
                </Hint>
              )}
              <Hint text="页面上任何数字要么来自 public/data 下的产物 JSON，要么由 TS 引擎在你的浏览器里现算，没有第三种来源。">
                <Badge className="border-indigo-400/30 bg-indigo-400/10 text-indigo-200">无硬编码指标</Badge>
              </Hint>
            </div>
          </div>

          {/* Tabs */}
          <nav className="mt-2.5 flex gap-1 overflow-x-auto pb-0.5">
            {TABS.map((t, i) => {
              const Icon = t.icon;
              const active = tab === t.id;
              return (
                <button
                  key={t.id}
                  onClick={() => goTab(t.id)}
                  className={`group flex shrink-0 items-center gap-2 rounded-lg border px-3 py-1.5 transition-all ${
                    active
                      ? 'border-cyan-300/45 bg-cyan-400/[0.11] text-cyan-100 shadow-glow'
                      : 'border-transparent text-slate-400 hover:border-white/10 hover:bg-white/[0.04] hover:text-slate-200'
                  }`}
                >
                  <span className="num text-[10px] opacity-60">{['①', '②', '③', '④', '⑤', '⑥'][i]}</span>
                  <Icon size={13} />
                  <span className="text-[12px] font-medium">{t.label}</span>
                  <span className={`hidden text-[10px] xl:inline ${active ? 'text-cyan-200/60' : 'text-slate-600'}`}>
                    {t.desc}
                  </span>
                </button>
              );
            })}
          </nav>
        </div>
      </header>

      {/* ================= Body ================= */}
      <main className="mx-auto max-w-[1560px] px-4 py-4">
        <div key={tab} className="animate-fade-up">
          {/* 页签级错误边界：渲染异常只退化成一张错误卡片，绝不把 root 清空成白屏。 */}
          <TabErrorBoundary tab={tab}>
          {tab === 'console' && (
            <ConsoleTab
              briefs={art.briefs}
              briefIdx={briefIdx}
              onBrief={setBriefIdx}
              result={result}
              stages={stages}
              running={running}
              liveStage={liveStage}
              koxById={koxById}
              includeReview={includeReview}
              onIncludeReview={setIncludeReview}
              decay={decay}
              onDecay={setDecay}
              onRerun={() => setRunToken((x) => x + 1)}
              datasetN={art.manifest.dataset.n}
              loadMs={art.loadMs}
              koxBytes={art.bytes.kox}
            />
          )}
          {tab === 'decision' && (
            <DecisionTab
              result={result}
              koxById={koxById}
              budgetArtifact={art.budget}
              auditArtifact={art.audit}
              metricsArtifact={art.metrics}
              includeReview={includeReview}
              decay={decay}
            />
          )}
          {tab === 'eval' && (
            <EvaluationTab
              metrics={art.metrics}
              report={evalReport}
              busy={busy}
              onRun={runEval}
              ablation={ablation}
              onAblation={runAblation}
              sens={sens}
              onSens={runSens}
            />
          )}
          {tab === 'cost' && (
            <CostValueTab
              promptBench={art.promptBench}
              llmBench={art.llmBench}
              llmCompare={art.llmCompare}
              audit={art.audit}
              metrics={art.metrics}
              multiseed={art.multiseed}
            />
          )}
          {tab === 'arch' && (
            <ArchitectureTab
              manifest={art.manifest}
              consistency={art.consistency}
              result={result}
              thresholdsMeta={art.manifest.thresholds_meta}
            />
          )}
          {tab === 'notes' && (
            <NotesTab promptBench={art.promptBench} audit={art.audit} metrics={art.metrics} multiseed={art.multiseed} />
          )}
          </TabErrorBoundary>
        </div>
      </main>

      <footer className="mx-auto max-w-[1560px] px-4 pb-8 pt-2">
        <div className="glass px-4 py-3">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5">
            <span className="muted">
              数据集 sha256 <code className="font-mono text-slate-400">{art.manifest.dataset.sha256.slice(0, 24)}…</code>
            </span>
            <span className="muted">阈值版本 {String(art.manifest.thresholds_meta.thresholds_version ?? '—')}</span>
            <span className="muted">
              产物生成于 unix {art.manifest.generated_at_unix}（prepare-data.mjs）
            </span>
            {c && <span className="muted">一致性校验耗时 {c.elapsed_ms} ms · {c.engine.runtime}</span>}
          </div>
          <p className="muted mt-1.5">
            {art.manifest.note ?? (art.manifest.notes ?? []).join('　')}
          </p>
        </div>
      </footer>
    </div>
  );
}
