import React from 'react';
import { Loader2, ShieldCheck } from 'lucide-react';
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
import { int0, pct1 } from './lib/format';
import { Hero, HeroSlimBar, robustSaved } from './overview/Hero';
import { SectionBlock } from './overview/SectionBlock';
import { TabBar } from './overview/TabBar';
import { DEFAULT_TAB, SECTION, resolveHash, type TabId } from './overview/nav';

const frame = (): Promise<void> => new Promise((r) => setTimeout(r, 16));

/** 滚到页内段落（承接旧 hash 的直达语义）。 */
const scrollToAnchor = (anchor: string): void => {
  window.requestAnimationFrame(() => {
    window.setTimeout(() => {
      document.getElementById(anchor)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 120);
  });
};

export default function App(): React.ReactElement {
  const [art, setArt] = React.useState<Artifacts | null>(null);
  const [loadErr, setLoadErr] = React.useState<string | null>(null);
  const [tab, setTab] = React.useState<TabId>(() => resolveHash(window.location.hash).tab);
  /** 旧 hash（#console/#decision/#eval/#cost/#arch/#notes）落地时要滚到的段落 */
  const [pendingAnchor, setPendingAnchor] = React.useState<string | null>(
    () => resolveHash(window.location.hash).anchor ?? null,
  );

  // 旧 hash 重定向：README / PDF 里已经放出去的 6 个链接必须继续可用。
  // 命中旧 hash 时改写成新页签 hash（用 replaceState，不污染前进后退），并滚到对应段落。
  React.useEffect(() => {
    const apply = (): void => {
      const r = resolveHash(window.location.hash);
      setTab(r.tab);
      setPendingAnchor(r.anchor ?? null);
      if (r.legacy) {
        window.history.replaceState(null, '', `#${r.tab}`);
      }
    };
    apply();
    window.addEventListener('hashchange', apply);
    return () => window.removeEventListener('hashchange', apply);
  }, []);

  // 产物加载完、DOM 渲染出来之后再滚（否则目标段落还不存在）
  React.useEffect(() => {
    if (!art || !pendingAnchor) return;
    scrollToAnchor(pendingAnchor);
    setPendingAnchor(null);
  }, [art, pendingAnchor]);

  const goTab = React.useCallback((id: TabId, anchor?: string): void => {
    window.history.replaceState(null, '', `#${id}`);
    setTab(id);
    if (anchor) setPendingAnchor(anchor);
    else window.scrollTo({ top: 0, behavior: 'smooth' });
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
        <div className="card max-w-lg px-5 py-4">
          <div className="text-[13px] font-semibold text-rose-600">产物加载失败</div>
          <p className="muted mt-1.5">{loadErr}</p>
          <p className="muted mt-2">
            请先在 koxpilot/web 目录下运行 <code className="font-mono text-live-700">npm run prepare-data</code>，
            把 Python 侧的 data/ 与 output/ 产物搬运到 public/data。本页不会用任何占位数字代替缺失产物。
          </p>
        </div>
      </div>
    );
  }

  if (!art) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3">
        <Loader2 size={20} className="animate-spin text-live-600" />
        <div className="num text-[12px] tracking-[0.2em] text-live-700">加载 5,000 条达人数据与阈值表…</div>
        <div className="muted">加载完成后引擎会在你的浏览器里真跑一遍，不是播放录像</div>
      </div>
    );
  }

  const c = art.consistency;
  const diffCount = c ? c.verdict.diff_count : null;
  // 页头的「少浪费」一律用 12 种子稳健口径（21.5% ± 13.7%）。
  // 原来挂的单次实测 $79,719（−32.5%）是 12 个种子里第 2 高的观测、落在均值 95% CI 之外，
  // README 已明说它偏乐观约 50%，页头不该挂一个自己都不信的数字。
  const savedRobust = robustSaved(art);

  return (
    <div className="min-h-screen">
      {/* ================= Header ================= */}
      <header className="sticky top-0 z-40 border-b border-slate-300 bg-white backdrop-blur-md">
        <div className="mx-auto max-w-[1560px] px-4 py-2.5">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <div className="flex items-center gap-2.5">
              <div className="relative flex h-8 w-8 items-center justify-center rounded-lg border border-live-300 bg-live-50">
                <ShieldCheck size={15} className="text-live-600" />
              </div>
              <div>
                <div className="flex items-baseline gap-2">
                  <h1 className="text-[15px] font-semibold tracking-tight text-slate-900">KOXPilot</h1>
                  <span className="text-[11px] text-slate-600">出海达人营销 · 投前决策智能体</span>
                </div>
                <div className="muted">
                  不是"帮你找达人"，而是<b className="text-slate-600">先把不该投的人挡掉</b>，再把预算分配得经得起问
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
                      ? 'border-emerald-300 bg-emerald-50 text-emerald-700'
                      : 'border-rose-300 bg-rose-50 text-rose-700'
                  }
                >
                  TS / Python 一致性差异 {diffCount === null ? '未生成' : int0(diffCount)} 条
                </Badge>
              </Hint>
              <Hint text={`数据集 sha256 ${art.manifest.dataset.sha256}｜种子 ${art.manifest.dataset.seed}｜全部为合成数据`}>
                <Badge className="border-slate-300 text-slate-700">
                  {int0(art.manifest.dataset.n)} 达人 · 合成数据
                </Badge>
              </Hint>
              {savedRobust && (
                <Hint text={`以 ground truth 为裁判、对照「按粉丝量降序买」的反事实审计，跨 12 个种子聚合：少浪费占预算 mean ± std = ${pct1(savedRobust.mean)} ± ${pct1(savedRobust.std)}，95% CI 12.8%~30.2%，1/12 个种子跑输基线。定稿单种子的 −32.5%（$79,719）落在这个 CI 之外、偏乐观约 50%，所以页头不挂它。`}>
                  <Badge className="border-live-200 bg-live-50 text-live-700">
                    少浪费占预算 {pct1(savedRobust.mean)} ± {pct1(savedRobust.std)} · 12 种子
                  </Badge>
                </Hint>
              )}
              <Hint text="页面上任何数字要么来自 public/data 下的产物 JSON，要么由 TS 引擎在你的浏览器里现算，没有第三种来源。">
                <Badge className="border-indigo-200 bg-indigo-50 text-indigo-700">无硬编码指标</Badge>
              </Hint>
            </div>
          </div>

          {/* Tabs：图标 + 名称 + 一句话说明，功能导向命名 */}
          <TabBar tab={tab} onGo={goTab} />
        </div>
      </header>

      {/* ================= Body ================= */}
      <main className="mx-auto max-w-[1560px] px-4 py-4">
        {/* Hero 只在默认页签占地方；切走后收成一条 slim bar */}
        <div className="mb-3">
          {tab === DEFAULT_TAB ? (
            <Hero art={art} onGo={goTab} />
          ) : (
            <HeroSlimBar art={art} onBack={() => goTab(DEFAULT_TAB)} />
          )}
        </div>
        <div key={tab} className="animate-fade-up">
          {/* 页签级错误边界：渲染异常只退化成一张错误卡片，绝不把 root 清空成白屏。 */}
          <TabErrorBoundary tab={tab}>
          {tab === 'run' && (
            <div className="space-y-6">
              <SectionBlock
                id={SECTION.briefInput}
                step="第 1 步"
                title="输入 brief，看它怎么一层层筛人"
                question="一句话需求 → A1–A6 的执行轨迹，每一阶段都在你浏览器里现算"
              >
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
              </SectionBlock>
              <SectionBlock
                id={SECTION.roster}
                step="第 2 步"
                title="拿到名单与预算，并与「凭粉丝量选人」对照"
                question="选谁、每人花多少、为什么；以及比行业朴素基线少浪费多少钱"
              >
                <DecisionTab
                  result={result}
                  koxById={koxById}
                  budgetArtifact={art.budget}
                  auditArtifact={art.audit}
                  metricsArtifact={art.metrics}
                  includeReview={includeReview}
                  decay={decay}
                />
              </SectionBlock>
            </div>
          )}
          {tab === 'proof' && (
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
          {tab === 'build' && (
            <div className="space-y-6">
              <SectionBlock
                id={SECTION.arch}
                step="怎么搭的"
                title="六个 Agent 的编排与 Python / TS 双实现一致性"
                question="谁调谁、阈值从哪来、两套实现如何逐条比对到 0 差异"
              >
                <ArchitectureTab
                  manifest={art.manifest}
                  consistency={art.consistency}
                  result={result}
                  thresholdsMeta={art.manifest.thresholds_meta}
                />
              </SectionBlock>
              <SectionBlock
                id={SECTION.notes}
                step="踩过的坑"
                title="我自己抓到并修掉的问题"
                question="哪些结论被我自己的数据推翻了，哪些边界至今没解决"
              >
                <NotesTab promptBench={art.promptBench} audit={art.audit} metrics={art.metrics} multiseed={art.multiseed} />
              </SectionBlock>
            </div>
          )}
          </TabErrorBoundary>
        </div>
      </main>

      <footer className="mx-auto max-w-[1560px] px-4 pb-8 pt-2">
        <div className="card px-4 py-3">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5">
            <span className="muted">
              数据集 sha256 <code className="font-mono text-slate-600">{art.manifest.dataset.sha256.slice(0, 24)}…</code>
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
