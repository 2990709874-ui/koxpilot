import React from 'react';
import { AlertTriangle, ArrowRight, Ban, Coins, Cpu, Scale, Sliders, TriangleAlert, Users, Zap } from 'lucide-react';
import { ArmWaterfall, BenchChart, DuoBars, SeedStrip, TokenBars, type ArmBar, type BenchBar } from '../components/charts';
import { Badge, MissingArtifact, Note, Panel, Segmented, Stat, TruthChip } from '../components/ui';
import type { Loose } from '../lib/artifacts';
import { compact, fixed, int0, pct1, usd0 } from '../lib/format';

const MODEL_LABEL: Record<string, string> = { ark: 'ARK', azure: 'Azure', none: '无模型' };

/** multiseed 里每个统计块的形状：{n, mean, std, min, max, median, ci95_low, ci95_high, cv}。 */
interface SeedStat {
  n: number;
  mean: number;
  std: number;
  min: number;
  max: number;
  median: number;
  ci95_low: number;
  ci95_high: number;
  cv: number;
}
const asStat = (o: Loose | undefined | null): SeedStat | null =>
  o && Number.isFinite(Number(o.mean))
    ? {
        n: Number(o.n),
        mean: Number(o.mean),
        std: Number(o.std),
        min: Number(o.min),
        max: Number(o.max),
        median: Number(o.median),
        ci95_low: Number(o.ci95_low),
        ci95_high: Number(o.ci95_high),
        cv: Number(o.cv),
      }
    : null;

/**
 * 24 种子复核：**离线跑的，产物没有入库**。
 *
 * 为什么不入库：`make multiseed SEEDS=24` 会覆盖仓库里那份 12 种子的 output/multiseed.json，
 * 而 12 种子那份是页面上其它所有稳健性数字的来源。所以这一列在页面上明确标注为
 * "离线复核结论、产物未入库、可复现"，**不伪装成读产物得来的**。
 * 页面上任何带 `provenance: 'artifact'` 的数字都能在 public/data 里逐字段核到；这一条不能，故单列。
 */
const OFFLINE_24_SEED = {
  provenance: 'offline_rerun_not_in_repo' as const,
  n_seeds: 24,
  saved_share_mean: 0.191,
  saved_share_std: 0.186,
  n_loses: 3,
  repro: 'make multiseed SEEDS=24',
  why_not_committed: '重跑会覆盖仓库里那份 12 种子的 output/multiseed.json（页面其余稳健性数字的来源）',
};

const pp = (x: number, digits = 2): string => (Number.isFinite(x) ? `${x >= 0 ? '+' : ''}${x.toFixed(digits)}pp` : '—');

/** 一行"均值 ± 标准差 · CV · N/N 为正"的稳健性摘要。 */
function RobustRow({
  title,
  stat,
  points,
  fmt,
  nNegative,
  color,
  verdict,
  verdictTone,
}: {
  title: React.ReactNode;
  stat: SeedStat;
  points: Array<{ seed: number | string; value: number }>;
  fmt: (x: number) => string;
  nNegative: number;
  color: string;
  verdict: string;
  verdictTone: 'good' | 'bad';
}): React.ReactElement {
  const crossesZero = stat.ci95_low < 0 && stat.ci95_high > 0;
  return (
    <div
      className={`rounded-xl border px-3 py-2.5 ${
        verdictTone === 'good' ? 'border-emerald-400/30 bg-emerald-400/[0.06]' : 'border-rose-400/30 bg-rose-400/[0.06]'
      }`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="text-[12px] font-medium text-slate-100">{title}</span>
        <Badge
          className={
            verdictTone === 'good'
              ? 'border-emerald-400/35 bg-emerald-400/10 text-emerald-200'
              : 'border-rose-400/35 bg-rose-400/10 text-rose-200'
          }
        >
          {verdict}
        </Badge>
      </div>
      <div className="num mt-1 text-[19px] font-semibold" style={{ color }}>
        {fmt(stat.mean)}
        <span className="ml-1 text-[12px] font-normal text-slate-400">± {fmt(stat.std)}</span>
      </div>
      <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10.5px] text-slate-500">
        <span>
          中位数 <b className="num text-slate-300">{fmt(stat.median)}</b>
        </span>
        <span>
          变异系数 CV <b className={`num ${stat.cv <= 0.5 ? 'text-emerald-300' : 'text-rose-300'}`}>{fixed(stat.cv, 2)}</b>
        </span>
        <span>
          为正的种子{' '}
          <b className={`num ${nNegative === 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
            {stat.n - nNegative}/{stat.n}
          </b>
        </span>
        {crossesZero && <span className="text-rose-300">95% CI 跨 0 → 符号不可靠</span>}
      </div>
      <div className="mt-1.5">
        <SeedStrip points={points} mean={stat.mean} ciLow={stat.ci95_low} ciHigh={stat.ci95_high} fmt={fmt} color={color} />
      </div>
    </div>
  );
}

export function CostValueTab({
  promptBench,
  llmBench,
  llmCompare,
  audit,
  metrics,
  multiseed,
}: {
  promptBench: Loose | null;
  llmBench: Loose | null;
  llmCompare: Loose | null;
  audit: Loose | null;
  metrics: Loose | null;
  multiseed: Loose | null;
}): React.ReactElement {
  const [view, setView] = React.useState<'arms' | 'versions'>('arms');
  const [picked, setPicked] = React.useState<string | null>(null);
  /** 归因口径切换：single = 定稿单种子（招牌），multi = 12 种子（更诚实）。 */
  const [attrView, setAttrView] = React.useState<'single' | 'multi'>('multi');

  const arms = ((promptBench?.arms ?? []) as Loose[]).filter((a) => a.prompt_version !== 'rule');
  const ruleArm = ((promptBench?.arms ?? []) as Loose[]).find((a) => a.prompt_version === 'rule') ?? null;
  const ruleF1 = promptBench ? Number(promptBench.rule_baseline_f1) : null;
  const variants = (promptBench?.prompt_variants ?? []) as Loose[];
  const versionAvg = (promptBench?.version_average ?? {}) as Record<string, Loose>;

  const VERSION_COLOR: Record<string, string> = { v1: '#fb7185', v2: '#fbbf24', v3: '#22d3ee' };

  const bars: BenchBar[] = React.useMemo(() => {
    if (!promptBench) return [];
    if (view === 'versions') {
      return ['v1', 'v2', 'v3'].map((v) => {
        const a = versionAvg[v] ?? {};
        return {
          key: v,
          group: v,
          sub: `双模型平均 · ${int0(Number(a.total_tokens))} token`,
          label: `${v} 双模型平均`,
          f1: Number(a.avg_f1),
          precision: Number(a.avg_precision),
          recall: Number(a.avg_recall),
          tokens: Number(a.total_tokens),
          color: VERSION_COLOR[v],
          caution: v === 'v3' ? 'v3 同时改了两处（业务判据 + 完整邻接表），v2→v3 的增益无法在二者之间严格归因' : undefined,
        };
      });
    }
    return arms.map((a) => ({
      key: `${a.prompt_version}-${a.model_key}`,
      group: `${a.prompt_version} · ${MODEL_LABEL[String(a.model_key)] ?? a.model_key}`,
      sub: `${int0(Number(a.total_tokens))} token`,
      label: `${a.prompt_version} × ${MODEL_LABEL[String(a.model_key)] ?? a.model_key}（${a.model}）`,
      f1: Number(a.f1),
      precision: Number(a.precision),
      recall: Number(a.recall),
      tokens: Number(a.total_tokens),
      color: VERSION_COLOR[String(a.prompt_version)] ?? '#94a3b8',
      caution: a.prompt_version === 'v3' ? 'v3 同时改了两处（业务判据 + 完整邻接表），增益无法严格归因到单一改动' : undefined,
    }));
  }, [promptBench, view, arms, versionAvg]);

  const pickedBar = bars.find((b) => b.key === picked) ?? null;
  const belowCount = bars.filter((b) => ruleF1 !== null && b.f1 < ruleF1).length;

  const tagBench = (llmBench?.tag_bench ?? null) as Loose | null;
  const tagArms = (tagBench?.arms ?? {}) as Record<string, Loose>;
  const totals = (llmBench?.totals ?? null) as Loose | null;
  const perTask = (llmBench?.per_task ?? {}) as Record<string, Loose>;
  const costAudit = (audit?.cost_audit ?? null) as Loose | null;
  /** token 账：有就展示真实账目，没有才说"未配置 endpoint" —— 一律由产物驱动。 */
  const tokenAccount = (costAudit?.token_account ?? null) as Loose | null;

  // ---- 反事实价值审计：单种子（定稿）口径 ------------------------------------
  const cva = (audit?.counterfactual_value_audit ?? null) as Loose | null;
  const cvaTotals = (cva?.totals ?? null) as Loose | null;
  const bounded = (cvaTotals?.effective_view_uplift_bounded ?? null) as Loose | null;
  const attr = (cva?.value_attribution ?? null) as Loose | null;
  const wr = (attr?.waste_reduction_usd ?? null) as Loose | null;
  const rate = (attr?.effective_view_rate_pp ?? null) as Loose | null;
  const nSel = (attr?.n_selected ?? null) as Loose | null;
  const effViews = (attr?.effective_views_gt ?? null) as Loose | null;
  const cvaPer = (cva?.per_campaign ?? []) as Loose[];
  /** 为负的 campaign：从产物里找，不写死 BRIEF-002。 */
  const losers = cvaPer.filter((c) => Number(c.saved_usd) < 0);

  // ---- 12 种子稳健性：两段归因 ----------------------------------------------
  // ---- B 方差归因：伪重复口径 ------------------------------------------------
  const bVar = (multiseed?.B_variance_attribution ?? null) as Loose | null;
  const bPooled = (bVar?.pooled_all_campaigns ?? null) as Loose | null;
  const bUnit = (bVar?.independence_unit ?? null) as Loose | null;
  const bPerCampaign = React.useMemo(() => {
    const raw = bVar?.per_campaign;
    if (!raw) return [] as Array<{ cid: string; v: Loose }>;
    return Array.isArray(raw)
      ? (raw as Loose[]).map((v) => ({ cid: String(v.campaign_id), v }))
      : Object.entries(raw as Record<string, Loose>).map(([cid, v]) => ({ cid, v }));
  }, [bVar]);
  const bBands = ((bVar?.conditional_win_rate_by_baseline_luck as Loose | undefined)?.bands ?? []) as Loose[];
  const bCond = (bVar?.conditional_win_rate_by_baseline_luck ?? null) as Loose | null;
  /** 科学计数法：p 值一律这么写，避免 0.000000 看不出量级。 */
  const sci = (x: number, digits = 1): string => (Number.isFinite(x) ? x.toExponential(digits) : '—');

  const aRob = (multiseed?.A_value_robustness ?? null) as Loose | null;
  const armAttr = (aRob?.arm_attribution ?? null) as Loose | null;
  const perSeed = (multiseed?.per_seed ?? []) as Loose[];
  const gateStat = asStat(armAttr?.saved_usd_by_gating as Loose | undefined);
  const divStat = asStat(armAttr?.saved_usd_by_diversification as Loose | undefined);
  const gatePpStat = asStat(armAttr?.rate_gap_pp_by_gating as Loose | undefined);
  const divPpStat = asStat(armAttr?.rate_gap_pp_by_diversification as Loose | undefined);
  const thirdArmN = asStat(armAttr?.third_arm_n_selected as Loose | undefined);
  const seedPoints = React.useMemo(
    () =>
      perSeed.map((s) => ({
        seed: Number(s.seed),
        gating: Number((s.value as Loose)?.saved_usd_by_gating),
        div: Number((s.value as Loose)?.saved_usd_by_diversification),
        gatingPp: Number((s.value as Loose)?.rate_gap_pp_by_gating),
        divPp: Number((s.value as Loose)?.rate_gap_pp_by_diversification),
      })),
    [perSeed],
  );
  // ---- 三个口径并列：招牌 / 12 种子 / 24 种子离线复核 --------------------------
  const shareStat = asStat(aRob?.saved_share_of_budget as Loose | undefined);
  const singleShare = cvaTotals ? Number(cvaTotals.saved_share_of_budget) : null;
  const singleSaved = cvaTotals ? Number(cvaTotals.saved_usd) : null;
  /** 定稿那一次的 saved_share 在 12 个种子里排第几（降序），以及是否落在均值 95% CI 之外。 */
  const singleRank = React.useMemo(() => {
    if (singleShare === null || perSeed.length === 0) return null;
    const vals = perSeed
      .map((x) => Number((x.value as Loose)?.saved_share_of_budget))
      .filter((v) => Number.isFinite(v));
    if (vals.length === 0) return null;
    const higher = vals.filter((v) => v > singleShare + 1e-12).length;
    return { rank: higher + 1, n: vals.length };
  }, [perSeed, singleShare]);
  const singleOutsideCi =
    shareStat && singleShare !== null ? singleShare > shareStat.ci95_high || singleShare < shareStat.ci95_low : null;

  /** 单种子上"分散化占比"的印象 vs 12 种子上的均值占比 —— 反转就体现在这两个数上。 */
  const singleDivShare = wr ? Number(wr.share_of_total_by_diversification) : null;
  const multiDivShare =
    gateStat && divStat && gateStat.mean + divStat.mean !== 0 ? divStat.mean / (gateStat.mean + divStat.mean) : null;
  /** 无界 uplift 在 12 种子 per-campaign 上炸到多大：取三个 campaign 的 max 里的最大值。 */
  const ratioBlowup = React.useMemo(() => {
    const per = (aRob?.per_campaign ?? {}) as Record<string, Loose>;
    let best: { cid: string; max: number; median: number } | null = null;
    for (const [cid, v] of Object.entries(per)) {
      const r = v.effective_view_uplift_ratio_reference as Loose | undefined;
      if (!r) continue;
      const mx = Number(r.max);
      if (!Number.isFinite(mx)) continue;
      if (!best || mx > best.max) best = { cid, max: mx, median: Number(r.median) };
    }
    return best;
  }, [aRob]);

  // ---- decay 建模假设的三档敏感性 -------------------------------------------
  const decayScan = (metrics?.budget_decay_sensitivity ?? null) as Loose | null;
  const decayRows = (decayScan?.per_decay ?? []) as Loose[];
  const decayStab = (decayScan?.stability ?? null) as Loose | null;
  /** 分层交付：判据不达标时的分流规则（全部读产物，逐 campaign 明细也读产物）。 */
  const delivery = (decayScan?.delivery_policy ?? null) as Loose | null;
  const deliveryCore = (delivery?.stable_core ?? null) as Loose | null;
  const deliverySens = (delivery?.assumption_sensitive ?? null) as Loose | null;
  const decayPerCampaign = (decayScan?.per_campaign ?? []) as Loose[];
  const decayBlockers = (decayScan?.blockers ?? []) as string[];
  const decayCaveats = (decayScan?.caveats ?? []) as string[];
  /** 敏感层逐人清单：把三个 campaign 的人拼起来，按参照档金额降序（不写死人数）。 */
  const sensitiveKox = React.useMemo(
    () =>
      decayPerCampaign.flatMap((c) => {
        const t = ((c.delivery_tiers as Loose | undefined)?.assumption_sensitive as Loose | undefined)?.kox ?? [];
        return (t as Loose[]).map((k): Loose => ({ ...k, campaign_id: String(c.campaign_id) }));
      }),
    [decayPerCampaign],
  );
  /** 合计少浪费的相对离差 = 极差 / 均值（在 TS 里算，定义写在页面上，与产物三档金额可核对）。 */
  const savedSpread = React.useMemo(() => {
    const vals = decayRows.map((r) => Number(r.saved_usd_total)).filter((v) => Number.isFinite(v));
    if (vals.length < 2) return null;
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    return { lo, hi, mean, rel: mean !== 0 ? (hi - lo) / mean : null };
  }, [decayRows]);

  // ---- A4 语义适配：LLM 版 vs 规则版 ----------------------------------------
  const fitAudit = (metrics?.table_6_llm_vs_rule as Loose | undefined)?.semantic_fit_llm_vs_rule as Loose | undefined;
  const fitTotals = (fitAudit?.totals ?? null) as Loose | null;
  const fitChecks = (fitAudit?.checks ?? {}) as Record<string, boolean>;

  /** 三臂瀑布图的行（单种子口径）。 */
  const armBars: ArmBar[] = React.useMemo(() => {
    if (!wr || !rate || !nSel || !effViews) return [];
    const koxViews = Number(effViews.koxpilot);
    return [
      {
        key: 'baseline',
        label: '基线：按粉丝量降序买（不看门禁、无结构约束）',
        wasted: Number(wr.wasted_baseline),
        color: '#f87171',
        chips: [
          { k: '有效曝光率', v: pct1(Number(rate.baseline)), tone: 'bad' },
          { k: '选中', v: `${int0(Number(nSel.baseline))} 人` },
          { k: '有效曝光', v: compact(Number(effViews.baseline)) },
        ],
      },
      {
        key: 'third',
        label: '第三臂 diversified_no_gate：只做结构分散化，完全不看门禁',
        sub: String((attr?.arms as Loose | undefined)?.diversified_no_gate ?? ''),
        wasted: Number(wr.wasted_diversified_no_gate),
        color: '#fbbf24',
        contribution: Number(wr.by_diversification),
        contributionLabel: '结构分散化贡献',
        chips: [
          { k: '有效曝光率', v: pct1(Number(rate.diversified_no_gate)) },
          { k: '选中', v: `${int0(Number(nSel.diversified_no_gate))} 人`, tone: 'bad' },
          {
            k: '有效曝光',
            v: compact(Number(effViews.diversified_no_gate)),
            tone: Number(effViews.diversified_no_gate) > koxViews ? 'bad' : 'muted',
          },
        ],
      },
      {
        key: 'kox',
        label: 'KOXPilot：门禁过滤 + 结构约束 + 质量加权价值排序',
        wasted: Number(wr.wasted_koxpilot),
        color: '#22d3ee',
        contribution: Number(wr.by_gating_and_quality_ranking),
        contributionLabel: '门禁与质量排序贡献',
        chips: [
          { k: '有效曝光率', v: pct1(Number(rate.koxpilot)), tone: 'good' },
          { k: '选中', v: `${int0(Number(nSel.koxpilot))} 人`, tone: 'good' },
          { k: '有效曝光', v: compact(koxViews) },
        ],
      },
    ];
  }, [wr, rate, nSel, effViews, attr]);

  return (
    <div className="space-y-4">
      {/* ================= Prompt 迭代主图 ================= */}
      {promptBench ? (
        <Panel
          title="① Prompt 迭代：前两版都输给了一条 20 行的零成本规则"
          subtitle={`任务=${String(promptBench.task)}｜样本 ${int0(Number(promptBench.n_samples))} 条（正例 ${int0(Number(promptBench.n_positives))}，占 ${pct1(Number(promptBench.positive_rate))}）｜三版 Prompt 共用完全相同的样本与 batch 切分，唯一变量是 system prompt`}
          right={
            <div className="flex items-center gap-2">
              <Segmented
                size="sm"
                value={view}
                onChange={setView}
                options={[
                  { value: 'arms', label: '6 个 arm（版本×模型）' },
                  { value: 'versions', label: '按版本取双模型平均' },
                ]}
              />
              <TruthChip kind="llm-offline" />
            </div>
          }
        >
          <div className="grid gap-4 xl:grid-cols-[1.35fr_1fr]">
            <div>
              <BenchChart
                bars={bars}
                baseline={ruleF1 ?? 0}
                baselineLabel={`零成本规则基线 F1 = ${fixed(ruleF1, 4)}（0 token）`}
                onPick={(k) => setPicked(picked === k ? null : k)}
                active={picked}
              />
              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5">
                <span className="flex items-center gap-1.5 text-[11px] text-slate-400">
                  <span className="inline-block h-0.5 w-5 border-t border-dashed border-slate-200" />
                  规则基线横贯全图：柱子低于它就是没打赢
                </span>
                <span className="flex items-center gap-1.5 text-[11px] text-slate-400">
                  <span className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full bg-amber-400 text-[9px] font-bold text-black">!</span>
                  斜纹柱 = 存在混淆变量，见右侧
                </span>
                <span className="num text-[11px] text-rose-300">
                  {belowCount} / {bars.length} 个{view === 'arms' ? ' arm' : ' 版本'}低于规则基线
                </span>
              </div>

              {pickedBar && (
                <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <div className="glass px-2.5 py-2">
                    <div className="text-[10px] text-slate-500">F1</div>
                    <div className="num text-[15px] text-slate-100">{fixed(pickedBar.f1, 4)}</div>
                    <div className={`num text-[10px] ${pickedBar.f1 >= (ruleF1 ?? 0) ? 'text-emerald-300' : 'text-rose-300'}`}>
                      vs 规则 {pickedBar.f1 - (ruleF1 ?? 0) >= 0 ? '+' : ''}
                      {fixed(pickedBar.f1 - (ruleF1 ?? 0), 4)}
                    </div>
                  </div>
                  <div className="glass px-2.5 py-2">
                    <div className="text-[10px] text-slate-500">精确率</div>
                    <div className="num text-[15px] text-slate-100">{fixed(pickedBar.precision, 4)}</div>
                    <div className="num text-[10px] text-slate-500">规则 {fixed(Number(ruleArm?.precision), 4)}</div>
                  </div>
                  <div className="glass px-2.5 py-2">
                    <div className="text-[10px] text-slate-500">召回率</div>
                    <div className="num text-[15px] text-slate-100">{fixed(pickedBar.recall, 4)}</div>
                    <div className="num text-[10px] text-slate-500">规则 {fixed(Number(ruleArm?.recall), 4)}</div>
                  </div>
                  <div className="glass px-2.5 py-2">
                    <div className="text-[10px] text-slate-500">token 成本</div>
                    <div className="num text-[15px] text-amber-200">{int0(pickedBar.tokens)}</div>
                    <div className="num text-[10px] text-slate-500">规则 0</div>
                  </div>
                </div>
              )}

              <div className="mt-3 rounded-xl border border-rose-400/25 bg-rose-400/[0.06] px-3 py-2.5">
                <div className="flex items-center gap-1.5 text-[12px] font-medium text-rose-200">
                  <Ban size={12} />
                  结论先说难听的那半句
                </div>
                <p className="mt-1 text-[11.5px] leading-relaxed text-slate-300">
                  v1（只描述任务）双模型平均 F1 <b className="num text-rose-200">{fixed(Number(versionAvg.v1?.avg_f1), 4)}</b>、
                  v2（加负例约束）<b className="num text-rose-200">{fixed(Number(versionAvg.v2?.avg_f1), 4)}</b>，
                  都<b className="text-rose-200">低于</b>那条 20 行 Jaccard 规则的 <b className="num">{fixed(ruleF1, 4)}</b>，
                  却分别烧掉了 {int0(Number(versionAvg.v1?.total_tokens))} 和 {int0(Number(versionAvg.v2?.total_tokens))} token。
                  换句话说：<b className="text-slate-100">把任务丢给大模型并不自动比规则强</b>，前两版是花钱买了更差的结果。
                  直到 v3 把判据从「标签是否一致」改成「投放方按 declared 选人会不会选错」，才第一次越过这条线
                  （{fixed(Number(versionAvg.v3?.avg_f1), 4)}，最好单组合 v3 × Azure = {fixed(Number((promptBench.best_arm as Loose).f1), 4)}）。
                </p>
              </div>
            </div>

            {/* 右侧：三步迭代 + 混淆变量 */}
            <div className="space-y-2.5">
              {variants.map((v) => {
                const avg = versionAvg[String(v.version)] ?? {};
                const f1 = Number(avg.avg_f1);
                const beat = ruleF1 !== null && f1 >= ruleF1;
                return (
                  <div
                    key={String(v.version)}
                    className={`rounded-xl border px-3 py-2.5 ${
                      beat ? 'border-cyan-300/40 bg-cyan-400/[0.07]' : 'border-white/10 bg-white/[0.025]'
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span
                        className="num rounded px-1.5 py-0.5 text-[10px] font-semibold text-black"
                        style={{ background: VERSION_COLOR[String(v.version)] }}
                      >
                        {String(v.version)}
                      </span>
                      <span className="text-[12px] font-medium text-slate-100">{String(v.name)}</span>
                      <span className={`num ml-auto text-[12px] ${beat ? 'text-cyan-200' : 'text-rose-300'}`}>
                        F1 {fixed(f1, 4)}
                      </span>
                    </div>
                    <div className="muted mt-1">{String(v.change)}</div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                      <Badge className="border-white/15 text-slate-400">system prompt {String(v.system_prompt_chars)} 字</Badge>
                      <Badge className="border-white/15 text-slate-400">
                        精确率 {fixed(Number(avg.avg_precision), 3)} / 召回 {fixed(Number(avg.avg_recall), 3)}
                      </Badge>
                      <Badge className={beat ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200' : 'border-rose-400/30 bg-rose-400/10 text-rose-200'}>
                        {beat ? '越过规则基线' : `低于规则基线 ${fixed(f1 - (ruleF1 ?? 0), 4)}`}
                      </Badge>
                    </div>
                    <div className="muted mt-1.5">
                      <span className="text-slate-500">事前假设：</span>
                      {String(v.hypothesis)}
                    </div>
                  </div>
                );
              })}

              <div className="rounded-xl border border-amber-400/35 bg-amber-400/[0.09] px-3 py-2.5">
                <div className="flex items-center gap-1.5 text-[12px] font-semibold text-amber-100">
                  <TriangleAlert size={13} />
                  v3 的增益不能严格归因（混淆变量）
                </div>
                <p className="mt-1 text-[11.5px] leading-relaxed text-amber-50/85">
                  v3 同时做了<b>两处</b>改动：<br />
                  <span className="text-amber-100">(1)</span> 把判据从「标签是否一致」改写为「投放方按 declared 选人会不会选错」，给 severity 明确定义、要求以浪费预算为锚点；<br />
                  <span className="text-amber-100">(2)</span> 把品类邻接关系从举例改为从 taxonomy 注入的完整确定性邻接表。<br />
                  因此 v2 → v3 的 +{fixed(Number(versionAvg.v3?.avg_f1) - Number(versionAvg.v2?.avg_f1), 4)} F1{' '}
                  <b>无法在这两处之间做严格归因</b>。要归因就得再补一组只改其中一处的实验 —— 我没有跑，所以这里如实标注，
                  而不是把功劳直接算给"业务判据化"这个更好听的说法。
                </p>
              </div>

              <Note>
                <b className="text-slate-300">控制变量：</b>
                {String(promptBench.control_variables)}
              </Note>
              <Note tone="warn">
                <b className="text-amber-200">可信度边界：</b>推理类模型默认不是贪心解码，同 prompt 同模型两次跑出的 F1 会不同（实测 ARK 0.653 vs 0.681）。
                所以这里所有数字都是<b>单次实测值</b>，不是均值±方差；结论层面（v3 &gt; v2 &gt; v1）稳，小数点后第二位不稳。
              </Note>
            </div>
          </div>
        </Panel>
      ) : (
        <MissingArtifact file="data/prompt_bench.json" what="Prompt 横评" how="运行 python -m koxpilot.llm.promptbench 生成 output/prompt_bench.json 后重跑 npm run refresh" />
      )}

      {/* ================= LLM vs 规则 ================= */}
      <div className="grid gap-3 lg:grid-cols-[1.05fr_1fr]">
        <Panel
          title="② 生产任务上的 LLM vs 规则（标签错配判定）"
          subtitle={tagBench ? `同一份 ${int0(Number(tagBench.n_samples))} 条抽样、同一份 ground truth` : ''}
          right={<TruthChip kind="llm-offline" />}
        >
          {tagBench ? (
            <>
              <table className="w-full">
                <thead>
                  <tr>
                    <th className="th">arm</th>
                    <th className="th text-right">精确率</th>
                    <th className="th text-right">召回率</th>
                    <th className="th text-right">F1</th>
                    <th className="th text-right">覆盖率</th>
                    <th className="th text-right">token</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(tagArms).map(([k, v]) => {
                    const isRule = v.kind === 'rule';
                    return (
                      <tr key={k} className="hairline">
                        <td className="td">
                          <span className={isRule ? 'text-slate-300' : 'text-cyan-100'}>{k}</span>
                          <Badge className={`ml-1.5 ${isRule ? 'border-slate-400/25 text-slate-400' : 'border-indigo-400/30 bg-indigo-400/10 text-indigo-200'}`}>
                            {isRule ? '纯规则' : 'LLM'}
                          </Badge>
                        </td>
                        <td className="td num text-right">{fixed(Number(v.precision), 4)}</td>
                        <td className="td num text-right">{fixed(Number(v.recall), 4)}</td>
                        <td className="td num text-right font-semibold">{fixed(Number(v.f1), 4)}</td>
                        <td className={`td num text-right ${Number(v.coverage) < 1 ? 'text-amber-300' : 'text-slate-400'}`}>
                          {pct1(Number(v.coverage))}
                          {v.n_judged !== undefined && (
                            <span className="ml-1 text-[10px] text-slate-500">({int0(Number(v.n_judged))} 条)</span>
                          )}
                        </td>
                        <td className="td num text-right">{isRule ? '0' : '见下方 token 账'}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <Note tone="warn">
                <AlertTriangle size={11} className="mr-1 inline" />
                Azure 那一行覆盖率是 <b className="text-amber-200">{pct1(Number(tagArms.azure?.coverage ?? 0))}</b>：50 个 batch 里
                <b className="text-amber-200">失败了 1 个</b>，对应 12 条样本没有判定。这 12 条被算作<b>未覆盖</b>（n_judged = {int0(Number(tagArms.azure?.n_judged ?? 0))}），
                而不是用默认值 / 众数 / 规则结果填充 —— 填充会让指标虚高且无法追溯。
              </Note>
              <Note>
                在这个任务上 LLM 确实赢了规则（F1 {fixed(Number(tagArms.ark?.f1), 4)} / {fixed(Number(tagArms.azure?.f1), 4)} vs {fixed(Number(tagArms.rule_jaccard?.f1), 4)}），
                主要赢在精确率：规则把所有"相邻品类"都当错配，而模型能区分「美妆↔时尚」这种业务上不算错配的情况。
                但生产链路里 G2.1 仍然默认走规则 —— 因为 5,000 人全量过 LLM 的调用量与延迟不划算，模型只在需要时介入。
              </Note>
            </>
          ) : (
            <MissingArtifact file="data/llm_bench.json" what="LLM 横评" />
          )}
        </Panel>

        <Panel
          title="③ 真实 LLM 用量账（取自 API usage 字段，非估算）"
          subtitle={llmBench ? `构建期一次性跑完，总耗时 ${Math.round(Number(llmBench.elapsed_s))} s；线上 Demo 不再调用任何 endpoint` : ''}
          right={<TruthChip kind="llm-offline" />}
        >
          {totals ? (
            <>
              <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Stat label="总 token" value={compact(Number(totals.total_tokens))} hint={`${int0(Number(totals.total_tokens))} token`} tone="accent" icon={<Zap size={11} />} />
                <Stat label="其中思维链" value={compact(Number(totals.reasoning_tokens))} hint={`reasoning ${int0(Number(totals.reasoning_tokens))} token，占 completion 的 ${pct1(Number(totals.reasoning_tokens) / Number(totals.completion_tokens))}`} />
                <Stat label="调用次数" value={int0(Number(totals.calls))} hint="含 brief / tag / fit 三个任务" />
                <Stat
                  label="失败调用"
                  value={int0(Number(totals.failed_calls))}
                  hint="失败的 batch 按未覆盖处理，不用默认值填充"
                  tone={Number(totals.failed_calls) > 0 ? 'warn' : 'good'}
                />
              </div>
              <TokenBars
                rows={Object.entries(perTask).map(([k, v]) => ({
                  label: k,
                  model: String(v.model),
                  prompt: Number(v.prompt_tokens),
                  completion: Number(v.completion_tokens),
                  reasoning: Number(v.reasoning_tokens),
                  calls: Number(v.calls),
                  failed: Number(v.failed_calls),
                }))}
              />
              <Note>{String(llmBench?.note ?? '')}</Note>
            </>
          ) : (
            <MissingArtifact file="data/llm_bench.json" what="token 账" />
          )}
        </Panel>
      </div>


      {/* ================= A4 语义适配：LLM 版 vs 规则版 ================= */}
      {fitAudit && fitTotals ? (
        <Panel
          title="④ A4 语义适配：LLM 版 vs 规则版的量化审计（结论是「不升格」）"
          subtitle={`离线对照：${String((fitAudit.llm as Loose)?.model ?? '')}，抽样 ${int0(Number((fitAudit.llm as Loose)?.fit_sample_n))} 条；正式链路的 fit_score 仍是 ${String(fitAudit.formal_chain_fit_source)}`}
          right={
            <div className="flex items-center gap-1.5">
              <Badge className="border-amber-400/35 bg-amber-400/10 text-amber-200">{String(fitAudit.decision)}</Badge>
              <TruthChip kind="llm-offline" />
            </div>
          }
        >
          <div className="grid gap-3 xl:grid-cols-[1fr_1.15fr]">
            <div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Stat
                  label="覆盖率"
                  value={pct1(Number(fitTotals.coverage_share))}
                  hint={`${int0(Number(fitTotals.covered_by_llm))}/${int0(Number(fitTotals.candidate_pool))} 人；升格门槛 ${pct1(Number((fitAudit.thresholds as Loose).coverage_min_for_promotion), 0)}`}
                  tone="bad"
                />
                <Stat
                  label="口径重叠（双算）"
                  value={pct1(Number(fitTotals.double_counted_share))}
                  hint={`${int0(Number(fitTotals.n_llm_below_rule_above))} 人中已被定向/G2.4/audience_match 处理过；允许上限 ${pct1(Number((fitAudit.thresholds as Loose).double_count_max_for_promotion), 0)}`}
                  tone="bad"
                />
                <Stat
                  label="注入后判定翻转"
                  value={int0(Number(fitTotals.n_verdict_flips))}
                  hint={`阈值 ${fixed(Number(fitAudit.review_threshold), 2)}；影响几乎不在判定上，而在 value(k) 的 fit 因子`}
                />
                <Stat
                  label="浪费金额差（LLM − 规则）"
                  value={usd0(Number(fitTotals.wasted_delta_usd_llm_minus_rule))}
                  hint={`${usd0(Number(fitTotals.wasted_spend_usd_rule))} → ${usd0(Number(fitTotals.wasted_spend_usd_llm))}（gt 口径）`}
                  tone={Number(fitTotals.wasted_delta_usd_llm_minus_rule) < 0 ? 'good' : 'warn'}
                />
              </div>
              <Note tone="warn">
                <b className="text-amber-200">三条判据全部不通过</b>（
                {Object.entries(fitChecks).map(([k, v]) => (
                  <span key={k} className="num mr-2 text-[10.5px]">
                    {k}=<b className={v ? 'text-emerald-300' : 'text-rose-300'}>{String(v)}</b>
                  </span>
                ))}
                ）。注意浪费金额差是<b className="text-amber-200">负的（LLM 略好 {usd0(Math.abs(Number(fitTotals.wasted_delta_usd_llm_minus_rule)))}）</b>
                —— 数字对 LLM 有利，但我没有据此升格，因为它建立在 {pct1(Number(fitTotals.coverage_share))} 覆盖率和{' '}
                {pct1(Number(fitTotals.double_counted_share))} 双算率之上，是口径叠加的产物而不是能力提升。
              </Note>
            </div>
            <div className="space-y-2">
              {((fitAudit.blockers ?? []) as string[]).map((b, i) => (
                <div key={b} className="rounded-xl border border-rose-400/25 bg-rose-400/[0.06] px-3 py-2">
                  <div className="flex items-start gap-1.5">
                    <span className="num mt-[1px] shrink-0 rounded bg-rose-400/15 px-1.5 text-[9.5px] text-rose-200">
                      阻断 {i + 1}
                    </span>
                    <p className="text-[11.5px] leading-relaxed text-slate-300">{b}</p>
                  </div>
                </div>
              ))}
              <Note>
                {((fitAudit.caveats ?? []) as string[])[2] ?? ''}
              </Note>
            </div>
          </div>
        </Panel>
      ) : (
        <MissingArtifact
          file="data/metrics.json → table_6_llm_vs_rule.semantic_fit_llm_vs_rule"
          what="A4 语义适配的 LLM/规则量化审计"
          how="重跑 Python 侧评测生成该字段后执行 npm run refresh"
        />
      )}

      {/* ================= 成本审计 ================= */}
      <Panel
        title="⑤ 三种架构方案的调用量对比"
        subtitle="调用次数是可数真值（按候选池规模推算），不是估的"
        right={<TruthChip kind="python" />}
      >
        {costAudit ? (
          <div className="grid gap-3 lg:grid-cols-[1.3fr_1fr]">
            <div className="space-y-2.5">
              {((costAudit.schemes ?? []) as Loose[]).map((s) => {
                const maxCalls = Math.max(...((costAudit.schemes ?? []) as Loose[]).map((x) => Number(x.calls_total)), 1);
                const isOurs = String(s.scheme).includes('KOXPilot');
                return (
                  <div key={String(s.scheme)}>
                    <div className="flex items-baseline justify-between">
                      <span className={`text-[12px] ${isOurs ? 'text-cyan-100' : 'text-slate-300'}`}>
                        {String(s.scheme)}
                        {isOurs && <Badge className="ml-1.5 border-cyan-400/30 bg-cyan-400/10 text-cyan-200">本实现</Badge>}
                      </span>
                      <span className="num text-[12px] text-slate-300">{int0(Number(s.calls_total))} 次调用</span>
                    </div>
                    <div className="mt-1 h-2.5 overflow-hidden rounded-sm bg-white/[0.05]">
                      <div
                        className="h-full rounded-sm"
                        style={{
                          width: `${(Number(s.calls_total) / maxCalls) * 100}%`,
                          background: isOurs ? '#22d3ee' : Number(s.calls_total) === 0 ? '#64748b' : '#fb7185',
                        }}
                      />
                    </div>
                    <div className="muted mt-1">{String(s.desc)}</div>
                  </div>
                );
              })}
            </div>
            <div>
              <div className="grid grid-cols-2 gap-2">
                <Stat
                  label="调用量降低"
                  value={`${fixed(Number(costAudit.call_reduction_vs_full_llm), 2)}×`}
                  hint="混合架构 vs 全 LLM"
                  tone="good"
                  icon={<Cpu size={11} />}
                />
                <Stat
                  label="全规则方案的代价"
                  value={fixed(Number((costAudit.accuracy_cost_of_all_rules as Loose).rule_arm), 4)}
                  hint={
                    Number.isFinite(Number((costAudit.accuracy_cost_of_all_rules as Loose).llm_arm))
                      ? `${String((costAudit.accuracy_cost_of_all_rules as Loose).metric)}；LLM 一列 ${fixed(
                          Number((costAudit.accuracy_cost_of_all_rules as Loose).llm_arm),
                          4,
                        )}，全规则的 F1 代价 ${fixed(
                          Number((costAudit.accuracy_cost_of_all_rules as Loose).f1_drop_if_all_rules),
                          4,
                        )}；逐 arm 明细见上面第 ② 块`
                      : `${String((costAudit.accuracy_cost_of_all_rules as Loose).metric)}；这份产物里 LLM 一列尚未填充（生成时未配置 endpoint），真实对照见上面第 ② 块`
                  }
                  tone="warn"
                />
              </div>
              {tokenAccount ? (
                <Note>
                  <b className="text-slate-300">产物口径说明（本段全部读 audit.json，无写死状态）：</b>这份成本审计的{' '}
                  <code className="font-mono text-[10px]">token_account</code> 已填充：status ={' '}
                  <code className="font-mono text-[10px]">{String(costAudit.status)}</code>，实测{' '}
                  <b className="num text-slate-200">{int0(Number(tokenAccount.measured_calls))}</b> 次调用 /{' '}
                  <b className="num text-slate-200">{int0(Number(tokenAccount.total_tokens))}</b> token（平均{' '}
                  <span className="num">{fixed(Number(tokenAccount.tokens_per_call), 1)}</span> token/次），来源{' '}
                  {String(tokenAccount.source ?? '')}。它与上面第 ③ 块是同一份 usage 字段，两处数字必须相等
                  —— 页面不做任何二次估算。
                </Note>
              ) : (
                <Note tone="warn">
                  <b className="text-amber-200">产物口径说明：</b>这份 audit.json 里{' '}
                  <code className="font-mono text-[10px]">token_account</code> 尚未填充（status ={' '}
                  {String(costAudit.status)}）—— 成本审计跑在 LLM 真调之前。真实 token 账在 llm_bench.json，见上面第 ③ 块；
                  这条说明由产物状态驱动，产物一旦补齐就会自动改写。
                </Note>
              )}
            </div>
          </div>
        ) : (
          <MissingArtifact file="data/audit.json" what="成本审计" />
        )}
      </Panel>

      {/* ================= 反事实价值账：三臂 + 结论反转 ================= */}
      {cva && cvaTotals ? (
        <Panel
          title="⑥ 钱花得值不值：三臂反事实价值账 —— 以及一个被 12 种子推翻的结论"
          subtitle="以 ground truth 为裁判。第三臂 diversified_no_gate 只做结构分散化、完全不看门禁，用来把「少浪费」拆成两段：分散化贡献 + 门禁与质量排序贡献"
          right={
            <div className="flex flex-wrap items-center gap-2">
              {armAttr && (
                <Segmented
                  size="sm"
                  value={attrView}
                  onChange={setAttrView}
                  options={[
                    { value: 'single', label: '定稿单种子（招牌口径）', hint: '好看，但只有 1 个样本' },
                    { value: 'multi', label: `${int0(Number(armAttr.n_seeds))} 种子（更诚实）`, hint: '结论在这里反转' },
                  ]}
                />
              )}
              <TruthChip kind="audit" />
            </div>
          }
        >
          {/* ---- 三个口径并列：招牌 / 12 种子 / 24 种子离线复核 ---- */}
          <div className="mb-3 overflow-hidden rounded-2xl border border-amber-400/30 bg-amber-400/[0.05] px-4 py-3.5">
            <div className="flex flex-wrap items-center gap-2">
              <Badge className="border-amber-400/35 bg-amber-400/10 text-amber-200">三个口径并列</Badge>
              <h4 className="text-[13.5px] font-semibold text-slate-100">
                「少浪费多少」有三个口径，招牌那个最好看 —— 三个一起摆出来，并写清各自的取舍
              </h4>
            </div>
            <div className="mt-3 grid gap-2.5 lg:grid-cols-3">
              {/* 招牌口径 */}
              <div className="rounded-xl border border-white/12 bg-black/25 px-3 py-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] font-medium text-slate-400">定稿单种子（招牌口径 · n = 1）</span>
                  <Badge className="border-amber-400/30 bg-amber-400/10 text-amber-200">好看</Badge>
                </div>
                <div className="num mt-1.5 text-[20px] font-semibold text-amber-200">
                  {singleSaved !== null ? usd0(singleSaved) : '—'}
                  <span className="ml-1.5 text-[13px] font-normal text-slate-400">
                    {singleShare !== null ? pct1(singleShare) : '—'}
                  </span>
                </div>
                <p className="mt-1.5 text-[11px] leading-relaxed text-slate-400">
                  取舍：只有 1 个样本。
                  {singleRank && (
                    <>
                      {' '}
                      它是 {int0(singleRank.n)} 个种子里第{' '}
                      <b className="num text-rose-200">{int0(singleRank.rank)}</b> 高的观测
                    </>
                  )}
                  {singleOutsideCi && shareStat && (
                    <>
                      ，且<b className="text-rose-200">落在均值 95% CI（{pct1(shareStat.ci95_low)}~{pct1(shareStat.ci95_high)}）之外</b>
                    </>
                  )}
                  。所以它能当招牌，不能当结论。
                </p>
                <div className="muted mt-1">来源：audit.json → counterfactual_value_audit.totals</div>
              </div>

              {/* 12 种子 */}
              <div className="rounded-xl border border-emerald-400/30 bg-black/25 px-3 py-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] font-medium text-slate-400">
                    {shareStat ? `${int0(shareStat.n)} 种子` : '多种子'}（对外结论取这一列）
                  </span>
                  <Badge className="border-emerald-400/30 bg-emerald-400/10 text-emerald-200">采用</Badge>
                </div>
                {shareStat ? (
                  <>
                    <div className="num mt-1.5 text-[20px] font-semibold text-emerald-200">
                      {pct1(shareStat.mean)}
                      <span className="ml-1 text-[13px] font-normal text-slate-400">± {pct1(shareStat.std)}</span>
                    </div>
                    <p className="mt-1.5 text-[11px] leading-relaxed text-slate-300">
                      95% CI <b className="num">{pct1(shareStat.ci95_low)}~{pct1(shareStat.ci95_high)}</b>；区间跨了近 18 个百分点。
                      总口径下<b className="text-rose-200">
                        {' '}
                        {int0(Number(aRob?.n_seeds_koxpilot_loses_overall))}/{int0(Number(aRob?.n_seeds))} 个种子跑输基线
                      </b>
                      （seed {((aRob?.seeds_koxpilot_loses_overall ?? []) as number[]).join(', ')}），最差那个种子是{' '}
                      <span className="num text-rose-200">{pct1(shareStat.min)}</span>。
                    </p>
                    <div className="muted mt-1">来源：multiseed.json → A_value_robustness.saved_share_of_budget</div>
                  </>
                ) : (
                  <div className="muted mt-2">multiseed.json 未加载，这一列不显示替代数字</div>
                )}
              </div>

              {/* 24 种子离线复核 */}
              <div className="rounded-xl border border-dashed border-slate-400/35 bg-black/20 px-3 py-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] font-medium text-slate-400">
                    {OFFLINE_24_SEED.n_seeds} 种子复核（最保守口径）
                  </span>
                  <Badge className="border-slate-400/35 bg-slate-400/10 text-slate-300">离线 · 产物未入库</Badge>
                </div>
                <div className="num mt-1.5 text-[20px] font-semibold text-slate-200">
                  {pct1(OFFLINE_24_SEED.saved_share_mean)}
                  <span className="ml-1 text-[13px] font-normal text-slate-400">
                    ± {pct1(OFFLINE_24_SEED.saved_share_std)}
                  </span>
                </div>
                <p className="mt-1.5 text-[11px] leading-relaxed text-slate-300">
                  <b className="text-rose-200">{OFFLINE_24_SEED.n_loses}/{OFFLINE_24_SEED.n_seeds} 个种子跑输基线。</b>{' '}
                  <b className="text-amber-200">这一列是离线复核结论，产物未入库，页面无法读产物核对</b> ——
                  {OFFLINE_24_SEED.why_not_committed}。可用{' '}
                  <code className="font-mono text-[10px]">{OFFLINE_24_SEED.repro}</code> 复现。
                </p>
                <div className="muted mt-1">
                  来源：离线重跑（provenance = <code className="font-mono text-[10px]">{OFFLINE_24_SEED.provenance}</code>），
                  不是 public/data 里的任何一份产物
                </div>
              </div>
            </div>
            <Note tone="warn">
              <AlertTriangle size={11} className="mr-1 inline" />
              <b className="text-amber-200">对外该引哪个：</b>引{' '}
              {shareStat ? <b className="num">{pct1(shareStat.mean)} ± {pct1(shareStat.std)}</b> : '多种子均值'}
              （12 种子），并同时说明有种子跑输。招牌那个{' '}
              {singleShare !== null && <b className="num">{pct1(singleShare)}</b>} 只在"定稿那一次"成立；
              种子数从 12 加到 24 后均值还会再往下走（见右列），说明这个效应量对采样很敏感。
            </Note>
          </div>

          {/* ---- 总口径：招牌数字 + 有界口径 ---- */}
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
            <Stat
              label="合计少浪费（单种子）"
              value={usd0(Number(cvaTotals.saved_usd))}
              hint={`占 ${usd0(Number(cvaTotals.budget_usd))} 预算的 ${pct1(Number(cvaTotals.saved_share_of_budget))}`}
              tone="good"
              icon={<Coins size={11} />}
            />
            {bounded ? (
              <Stat
                label="有效曝光率（有界口径）"
                value={pp(Number(bounded.rate_gap_pp))}
                hint={`${pct1(Number(bounded.baseline_effective_view_rate))} → ${pct1(Number(bounded.koxpilot_effective_view_rate))}；symmetric_uplift ${fixed(Number(bounded.symmetric_uplift), 4)} ∈[−1,1]`}
                tone="accent"
                icon={<Scale size={11} />}
              />
            ) : (
              <Stat label="有界口径" value="未生成" hint="缺 effective_view_uplift_bounded" tone="warn" />
            )}
            <Stat
              label="有效曝光提升（无界口径）"
              value={`+${pct1(Number(cvaTotals.effective_view_uplift))}`}
              hint={`分母是基线有效曝光 ${compact(Number(cvaTotals.effective_views_baseline))}；总口径分母够大可读，但跨样本聚合一律用左边的有界口径`}
              tone="warn"
            />
            {losers.length > 0 ? (
              <Stat
                label={`${losers.length} 个 campaign 为负`}
                value={losers.map((c) => String(c.campaign_id)).join(' / ')}
                hint={losers
                  .map(
                    (c) =>
                      `少浪费 ${usd0(Number(c.saved_usd))}、有效曝光率 ${pp(Number((c.effective_view_uplift_bounded as Loose)?.rate_gap_pp))}`,
                  )
                  .join('；')}
                tone="bad"
              />
            ) : (
              <Stat label="为负的 campaign" value="0 个" hint="本轮三个 campaign 全部为正" tone="good" />
            )}
          </div>
          <Note tone="warn">
            <AlertTriangle size={11} className="mr-1 inline" />
            <b className="text-amber-200">上面第三格那个 +{pct1(Number(cvaTotals.effective_view_uplift))} 是无界比率，请不要跨样本引用。</b>
            它的分母是基线有效曝光，基线一旦几乎把钱全烧在水号上（浪费率 &gt;99%），分母趋 0，比率会爆炸。
            {ratioBlowup && (
              <>
                {' '}
                实测在 12 种子的 <span className="num">{ratioBlowup.cid}</span> 上，这个比率最大炸到{' '}
                <b className="num text-rose-200">×{fixed(ratioBlowup.max, 1)}</b>（同一格的中位数只有{' '}
                <span className="num">×{fixed(ratioBlowup.median, 2)}</span>）—— 这就是为什么现在补了有界口径
                （rate_gap_pp ∈[−100,100]、symmetric_uplift ∈[−1,1]），并规定聚合只用有界口径。
              </>
            )}
          </Note>

          {/* ---- 结论反转 ---- */}
          {armAttr && gateStat && divStat && wr && (
            <div className="mt-3 overflow-hidden rounded-2xl border border-rose-400/30 bg-gradient-to-br from-rose-400/[0.09] via-transparent to-emerald-400/[0.07] px-4 py-3.5">
              <div className="flex flex-wrap items-center gap-2">
                <Badge className="border-rose-400/35 bg-rose-400/10 text-rose-200">结论反转</Badge>
                <h4 className="text-[13.5px] font-semibold text-slate-100">
                  单种子看像"分散化占七成"；12 种子看，稳定的价值来源是<b className="text-emerald-200">门禁与质量排序</b>，不是分散投放
                </h4>
              </div>
              <div className="mt-3 grid gap-3 lg:grid-cols-2">
                <div className="rounded-xl border border-white/10 bg-black/25 px-3 py-2.5">
                  <div className="text-[11px] font-medium text-slate-400">
                    定稿单种子（招牌口径 · n = 1）
                    <Badge className="ml-1.5 border-amber-400/30 bg-amber-400/10 text-amber-200">好看但样本 = 1</Badge>
                  </div>
                  <div className="mt-2 space-y-1.5">
                    <div className="flex items-baseline justify-between">
                      <span className="text-[11.5px] text-amber-200">结构分散化</span>
                      <span className="num text-[15px] font-semibold text-amber-200">
                        {usd0(Number(wr.by_diversification))}
                        <span className="ml-1 text-[11px] font-normal text-slate-500">
                          占 {pct1(Number(wr.share_of_total_by_diversification))}
                        </span>
                      </span>
                    </div>
                    <div className="flex items-baseline justify-between">
                      <span className="text-[11.5px] text-cyan-200">门禁与质量排序</span>
                      <span className="num text-[15px] font-semibold text-cyan-200">
                        {usd0(Number(wr.by_gating_and_quality_ranking))}
                        <span className="ml-1 text-[11px] font-normal text-slate-500">
                          占 {pct1(Number(wr.share_of_total_by_gating))}
                        </span>
                      </span>
                    </div>
                  </div>
                  <p className="mt-2 text-[11px] leading-relaxed text-slate-400">
                    只看这一列，很容易得出"价值主要来自把钱摊开"的结论 —— 这是本页曾经的叙事，
                    <b className="text-rose-200">它是采样运气，不是规律</b>。
                  </p>
                </div>
                <div className="rounded-xl border border-emerald-400/25 bg-black/25 px-3 py-2.5">
                  <div className="text-[11px] font-medium text-slate-400">
                    {int0(Number(armAttr.n_seeds))} 种子（更诚实的口径 · 对外结论取这一列）
                    <Badge className="ml-1.5 border-emerald-400/30 bg-emerald-400/10 text-emerald-200">采用</Badge>
                  </div>
                  <div className="mt-2 space-y-1.5">
                    <div className="flex items-baseline justify-between">
                      <span className="text-[11.5px] text-amber-200">结构分散化</span>
                      <span className="num text-[15px] font-semibold text-amber-200">
                        {usd0(divStat.mean)}
                        <span className="ml-1 text-[11px] font-normal text-slate-500">± {usd0(divStat.std)}</span>
                      </span>
                    </div>
                    <div className="flex items-baseline justify-between">
                      <span className="text-[11.5px] text-cyan-200">门禁与质量排序</span>
                      <span className="num text-[15px] font-semibold text-cyan-200">
                        {usd0(gateStat.mean)}
                        <span className="ml-1 text-[11px] font-normal text-slate-500">± {usd0(gateStat.std)}</span>
                      </span>
                    </div>
                  </div>
                  <p className="mt-2 text-[11px] leading-relaxed text-slate-300">
                    门禁那一段 CV <b className="num text-emerald-300">{fixed(gateStat.cv, 2)}</b>、
                    <b className="num text-emerald-300">
                      {gateStat.n - Number(armAttr.n_seeds_gating_contribution_negative)}/{gateStat.n}
                    </b>{' '}
                    个种子为正；分散化那一段 CV <b className="num text-rose-300">{fixed(divStat.cv, 2)}</b>、
                    <b className="num text-rose-300">{int0(Number(armAttr.n_seeds_diversification_contribution_negative))}</b>{' '}
                    个种子为负，95% CI{' '}
                    <b className="num text-rose-300">
                      [{usd0(divStat.ci95_low)}, {usd0(divStat.ci95_high)}]
                    </b>{' '}
                    跨 0。
                    {singleDivShare !== null && multiDivShare !== null && (
                      <>
                        {' '}
                        分散化的"占比"从单种子的 <span className="num">{pct1(singleDivShare)}</span> 掉到 12 种子均值的{' '}
                        <span className="num">{pct1(multiDivShare)}</span>。
                      </>
                    )}
                  </p>
                </div>
              </div>
            </div>
          )}

          {/* ---- 主体：按口径切换 ---- */}
          <div className="mt-3">
            {attrView === 'single' || !armAttr || !gateStat || !divStat ? (
              <div className="grid gap-3 xl:grid-cols-[1.1fr_1fr]">
                <div>
                  <div className="muted mb-2">
                    三臂链式差分（定稿单种子）：{String(attr?.definition ?? '')}
                  </div>
                  {armBars.length > 0 ? (
                    <ArmWaterfall arms={armBars} />
                  ) : (
                    <MissingArtifact file="data/audit.json → counterfactual_value_audit.value_attribution" what="三臂归因" />
                  )}
                  {rate && (
                    <Note>
                      有效曝光率同向拆分：{pct1(Number(rate.baseline))} →（第三臂）{pct1(Number(rate.diversified_no_gate))} →
                      （KOXPilot）{pct1(Number(rate.koxpilot))}；分散化 {pp(Number(rate.gap_by_diversification_pp))}、
                      门禁与质量排序 {pp(Number(rate.gap_by_gating_and_quality_ranking_pp))}，合计{' '}
                      {pp(Number(rate.gap_total_pp))}。
                    </Note>
                  )}
                </div>
                <div>
                  <div className="muted mb-2">逐 campaign 浪费金额：基线 vs KOXPilot（含为负的那一个，未剔除）</div>
                  <DuoBars
                    leftName="按粉丝量买（基线）"
                    rightName="KOXPilot"
                    fmt={(x) => (x > 1e5 ? compact(x) : usd0(x))}
                    rows={cvaPer.map((c) => ({
                      label: `${String(c.campaign_id)}　预算 ${usd0(Number(c.budget_usd))}`,
                      left: Number((c.baseline as Loose).wasted_spend_usd),
                      right: Number((c.koxpilot as Loose).wasted_spend_usd),
                      note: String(c.headline),
                    }))}
                  />
                </div>
              </div>
            ) : (
              <div className="grid gap-3 lg:grid-cols-2">
                <RobustRow
                  title={<>门禁与质量排序 · 少浪费金额（{int0(gateStat.n)} 种子）</>}
                  stat={gateStat}
                  points={seedPoints.map((p) => ({ seed: p.seed, value: p.gating }))}
                  fmt={usd0}
                  nNegative={Number(armAttr.n_seeds_gating_contribution_negative)}
                  color="#22d3ee"
                  verdict="稳定为正"
                  verdictTone="good"
                />
                <RobustRow
                  title={<>结构分散化 · 少浪费金额（{int0(divStat.n)} 种子）</>}
                  stat={divStat}
                  points={seedPoints.map((p) => ({ seed: p.seed, value: p.div }))}
                  fmt={usd0}
                  nNegative={Number(armAttr.n_seeds_diversification_contribution_negative)}
                  color="#fbbf24"
                  verdict="符号不稳定"
                  verdictTone="bad"
                />
                {gatePpStat && (
                  <RobustRow
                    title={<>门禁与质量排序 · 有效曝光率差（有界口径）</>}
                    stat={gatePpStat}
                    points={seedPoints.map((p) => ({ seed: p.seed, value: p.gatingPp }))}
                    fmt={(x) => pp(x)}
                    nNegative={seedPoints.filter((p) => p.gatingPp < 0).length}
                    color="#22d3ee"
                    verdict="与金额口径同向"
                    verdictTone="good"
                  />
                )}
                {divPpStat && (
                  <RobustRow
                    title={<>结构分散化 · 有效曝光率差（有界口径）</>}
                    stat={divPpStat}
                    points={seedPoints.map((p) => ({ seed: p.seed, value: p.divPp }))}
                    fmt={(x) => pp(x)}
                    nNegative={seedPoints.filter((p) => p.divPp < 0).length}
                    color="#fbbf24"
                    verdict="与金额口径同向：也不稳"
                    verdictTone="bad"
                  />
                )}
                <div className="lg:col-span-2">
                  <Note tone="good">
                    <b className="text-emerald-200">可加性已显式校验：</b>
                    {String((armAttr.additivity_check as Loose).note ?? '')} 均值合计{' '}
                    <span className="num">{usd0(Number((armAttr.additivity_check as Loose).mean_total))}</span> vs 两段之和{' '}
                    <span className="num">
                      {usd0(Number((armAttr.additivity_check as Loose).mean_diversification_plus_gating))}
                    </span>
                    ，绝对误差 <span className="num">{fixed(Number((armAttr.additivity_check as Loose).abs_error), 4)}</span>。
                    另：总口径下 KOXPilot 跑输基线的种子有{' '}
                    <b className="num text-rose-200">
                      {int0(Number(aRob?.n_seeds_koxpilot_loses_overall))}/{int0(Number(aRob?.n_seeds))}
                    </b>{' '}
                    个（seed {((aRob?.seeds_koxpilot_loses_overall ?? []) as number[]).join(', ')}），一并写在这里。
                  </Note>
                </div>
              </div>
            )}
          </div>

          {/* ---- 三条必须一起展示的反面事实 ---- */}
          {armAttr && (
            <div className="mt-3">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <h4 className="text-[13px] font-semibold text-slate-100">三条不许只报一半的反面事实</h4>
                <Badge className="border-rose-400/30 bg-rose-400/10 text-rose-200">与上面的好结论同等重要</Badge>
              </div>
              <div className="grid gap-2 lg:grid-cols-3">
                <div className="rounded-xl border border-rose-400/25 bg-rose-400/[0.06] px-3 py-2.5">
                  <div className="flex items-center gap-1.5 text-[11.5px] font-medium text-rose-200">
                    <Ban size={12} />
                    第三臂的绝对有效曝光赢过我们
                  </div>
                  <p className="mt-1 text-[11px] leading-relaxed text-slate-300">
                    在{' '}
                    <b className="num text-rose-200">
                      {int0(Number(armAttr.n_seeds_third_arm_more_effective_views))}/{int0(Number(armAttr.n_seeds))}
                    </b>{' '}
                    个种子上，第三臂的绝对有效曝光都高于 KOXPilot
                    {effViews && (
                      <>
                        （定稿单种子：<span className="num">{compact(Number(effViews.diversified_no_gate))}</span> vs{' '}
                        <span className="num">{compact(Number(effViews.koxpilot))}</span>）
                      </>
                    )}
                    。原因是它按"每美元名义曝光"排序，专挑 CPM 最便宜的长尾；KOXPilot 优化的是质量加权价值，会
                    <b className="text-slate-100">主动放弃便宜但不对味的曝光</b>。所以三臂可比的口径是
                    <b className="text-slate-100">浪费金额与有效曝光率</b>，绝对曝光数不是 KOXPilot 的优化目标。
                  </p>
                </div>
                <div className="rounded-xl border border-amber-400/25 bg-amber-400/[0.06] px-3 py-2.5">
                  <div className="flex items-center gap-1.5 text-[11.5px] font-medium text-amber-200">
                    <Users size={12} />
                    第三臂平均选 {thirdArmN ? int0(thirdArmN.mean) : '—'} 人，真实采购不可执行
                  </div>
                  <p className="mt-1 text-[11px] leading-relaxed text-slate-300">
                    {thirdArmN && (
                      <>
                        12 种子均值 <b className="num text-amber-200">{int0(thirdArmN.mean)}</b> 人（min{' '}
                        <span className="num">{int0(thirdArmN.min)}</span> / max <span className="num">{int0(thirdArmN.max)}</span>
                        ）
                      </>
                    )}
                    {nSel && (
                      <>
                        ，定稿单种子上是 <span className="num">{int0(Number(nSel.diversified_no_gate))}</span> 人，而基线{' '}
                        <span className="num">{int0(Number(nSel.baseline))}</span> 人 / KOXPilot{' '}
                        <span className="num">{int0(Number(nSel.koxpilot))}</span> 人
                      </>
                    )}
                    。同一笔钱摊到五百多个达人身上，在真实采购里签约、寄样、排期都不可执行 ——
                    <b className="text-slate-100">它只是对照臂，不是一个方案</b>；"分散化贡献"里也天然含着"摊得更开"的大数效应。
                  </p>
                </div>
                <div className="rounded-xl border border-white/12 bg-white/[0.03] px-3 py-2.5">
                  <div className="flex items-center gap-1.5 text-[11.5px] font-medium text-slate-200">
                    <TriangleAlert size={12} className="text-slate-400" />
                    "门禁贡献"其实是"门禁过滤 + 质量排序"的合计
                  </div>
                  <p className="mt-1 text-[11px] leading-relaxed text-slate-400">
                    第三臂与 KOXPilot 的差额里同时包含两件事：门禁过滤掉 review/reject，以及排序依据从"每美元名义曝光"换成
                    "质量加权价值"。二者<b className="text-slate-200">共用同一批真实性/适配分数，在实现上无法再拆细</b>，
                    所以这里合并命名为"门禁与质量排序"，不谎报成纯门禁效果。
                  </p>
                </div>
              </div>
              <Note>{String(armAttr.caveat ?? '')}</Note>
              {((attr?.caveats ?? []) as string[]).map((c) => (
                <Note key={c.slice(0, 24)}>{c}</Note>
              ))}
            </div>
          )}

          <Note>
            方法：{String((cva.method as Loose).baseline)}；第三臂 = {String((cva.method as Loose).third_arm)}；
            {String((cva.method as Loose).judge)}
          </Note>
          {!multiseed && (
            <MissingArtifact
              file="data/multiseed.json"
              what="12 种子稳健性与两段归因"
              how="跑 PYTHONPATH=src python -m koxpilot.cli multiseed --seeds 12 生成 output/multiseed.json 后重跑 npm run refresh。缺它时本页只能展示单种子口径 —— 那个口径会给出相反的归因结论。"
            />
          )}
        </Panel>
      ) : (
        <MissingArtifact file="data/audit.json" what="反事实价值审计" />
      )}

      {/* ================= decay 建模假设敏感性 ================= */}
      {decayScan && decayRows.length > 0 ? (
        <Panel
          title="⑦ 预算模型里 decay = 0.7 这个建模假设，扛不扛得住扫描？"
          subtitle={`${String(decayScan.assumption)} · ${String(decayScan.assumption_kind)}；扫描 ${((decayScan.scan ?? []) as number[]).join(' / ')}，正式链路用 ${fixed(Number(decayScan.reference_decay), 1)}`}
          tone="warn"
          right={
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge className="border-emerald-400/30 bg-emerald-400/10 text-emerald-200">价值结论不依赖该假设</Badge>
              <Badge className="border-rose-400/30 bg-rose-400/10 text-rose-200">但选谁会变</Badge>
              <TruthChip kind="python" />
            </div>
          }
        >
          <div className="grid gap-3 xl:grid-cols-[1.25fr_1fr]">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px]">
                <thead>
                  <tr>
                    <th className="th">decay</th>
                    <th className="th text-right">选中人数</th>
                    <th className="th text-right">总条数</th>
                    <th className="th text-right">合计少浪费</th>
                    <th className="th text-right">分散化</th>
                    <th className="th text-right">门禁与质量排序</th>
                    <th className="th text-right">有效曝光率差</th>
                  </tr>
                </thead>
                <tbody>
                  {decayRows.map((r) => {
                    const isRef = Boolean(r.is_reference);
                    return (
                      <tr key={String(r.decay)} className={`hairline ${isRef ? 'bg-cyan-400/[0.07]' : ''}`}>
                        <td className="td num">
                          {fixed(Number(r.decay), 1)}
                          {isRef && (
                            <Badge className="ml-1.5 border-cyan-400/30 bg-cyan-400/10 text-cyan-200">正式链路</Badge>
                          )}
                        </td>
                        <td className="td num text-right">{int0(Number(r.n_selected_total))}</td>
                        <td className="td num text-right">{int0(Number(r.n_posts_total))}</td>
                        <td className="td num text-right font-semibold text-emerald-200">
                          {usd0(Number(r.saved_usd_total))}
                          <div className="muted">{pct1(Number(r.saved_share_of_budget))}</div>
                        </td>
                        <td className="td num text-right text-amber-200">{usd0(Number(r.saved_usd_by_diversification))}</td>
                        <td className="td num text-right text-cyan-200">
                          {usd0(Number(r.saved_usd_by_gating_and_quality_ranking))}
                        </td>
                        <td className="td num text-right">{pp(Number(r.effective_view_rate_gap_pp_total))}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <Note>
                <b className="text-slate-300">这些必然随 decay 变，不属于"应当稳定"的范畴：</b>
                {((decayScan.what_moves_with_decay ?? []) as string[]).map((w) => (
                  <span key={w.slice(0, 16)} className="mt-1 block">
                    · {w}
                  </span>
                ))}
              </Note>
            </div>
            <div className="space-y-2">
              <div className="rounded-xl border border-emerald-400/30 bg-emerald-400/[0.06] px-3 py-2.5">
                <div className="flex items-center gap-1.5 text-[12px] font-medium text-emerald-200">
                  <Sliders size={12} />
                  好的那一半：价值结论不依赖这个假设
                </div>
                <div className="mt-1.5 space-y-1 text-[11px] leading-relaxed text-slate-300">
                  {savedSpread && (
                    <div>
                      合计少浪费在 <b className="num">{usd0(savedSpread.lo)}</b> ~ <b className="num">{usd0(savedSpread.hi)}</b>{' '}
                      之间，相对离差（极差 / 均值）只有{' '}
                      <b className="num text-emerald-300">{pct1(savedSpread.rel ?? 0)}</b>。
                    </div>
                  )}
                  {decayStab && (
                    <div>
                      两段归因符号逐档均未翻转：
                      {Object.entries((decayStab.sign_stable ?? {}) as Record<string, boolean>).map(([k, v]) => (
                        <span key={k} className="num ml-1.5 text-[10.5px]">
                          {k}=<b className={v ? 'text-emerald-300' : 'text-rose-300'}>{String(v)}</b>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
              <div className="rounded-xl border border-rose-400/30 bg-rose-400/[0.06] px-3 py-2.5">
                <div className="flex items-center gap-1.5 text-[12px] font-medium text-rose-200">
                  <AlertTriangle size={12} />
                  难听的那一半：具体选谁会变，且没过我自设的判据
                </div>
                {decayStab && (
                  <div className="mt-1.5 space-y-1 text-[11px] leading-relaxed text-slate-300">
                    <div>
                      选中名单 Jaccard 最低{' '}
                      <b className="num text-rose-200">{fixed(Number(decayStab.selection_jaccard_min_vs_reference), 3)}</b>
                      、金额加权重叠最低{' '}
                      <b className="num text-rose-200">{fixed(Number(decayStab.spend_overlap_share_min_vs_reference), 3)}</b>
                      ，都低于自设判据{' '}
                      <b className="num">{fixed(Number(decayStab.threshold), 2)}</b> ——
                      <b className="text-rose-200">selection_stable = {String(decayStab.selection_stable)}</b>。
                    </div>
                    <div>
                      即：这个假设不影响"要不要用 KOXPilot"，但影响"最终名单里是哪些人、每人买几条"。
                      落到真实采购上，这是必须交代的不确定性，不能拿"结论稳"一句话盖过去。
                    </div>
                  </div>
                )}
              </div>
              {/* ---- blockers / caveats 分列：两类不许混装，空也要说明是空 ---- */}
              <div className="rounded-xl border border-white/10 bg-white/[0.025] px-3 py-2.5">
                <div className="text-[11.5px] font-medium text-slate-200">
                  阻断项 {decayBlockers.length} 条 · 保留意见 {decayCaveats.length} 条
                  <span className="muted ml-1.5">（两类分开读产物，缺一类就会让读者只看到一片绿）</span>
                </div>
                <div className="mt-1.5 space-y-1">
                  {decayBlockers.length === 0 ? (
                    <div className="text-[11px] leading-relaxed text-emerald-200/90">
                      blockers 为空：没有"必须先解决才能对外用"的阻断项。
                    </div>
                  ) : (
                    decayBlockers.map((b) => (
                      <div key={b.slice(0, 20)} className="text-[11px] leading-relaxed text-rose-200">
                        · 阻断：{b}
                      </div>
                    ))
                  )}
                  {decayCaveats.length === 0 ? (
                    <div className="muted">caveats 为空。</div>
                  ) : (
                    decayCaveats.map((cv) => (
                      <div key={cv.slice(0, 20)} className="text-[11px] leading-relaxed text-amber-100/90">
                        · 保留意见：{cv}
                      </div>
                    ))
                  )}
                </div>
              </div>
              <Note tone="warn">
                <b className="text-amber-200">判定：</b>
                <code className="font-mono text-[10px]">{String(decayScan.verdict)}</code>
                <span className="mt-1 block">{String(decayScan.headline)}</span>
              </Note>
              <Note>{String(decayScan.note ?? '')}</Note>
            </div>
          </div>

          {/* ================= 分层交付：把"名单不稳"变成分流规则 ================= */}
          {delivery && deliveryCore && deliverySens ? (
            <div className="mt-3 overflow-hidden rounded-2xl border border-cyan-300/30 bg-cyan-400/[0.05] px-4 py-3.5">
              <div className="flex flex-wrap items-center gap-2">
                <Badge className="border-cyan-400/35 bg-cyan-400/10 text-cyan-200">分层交付</Badge>
                <h4 className="text-[13.5px] font-semibold text-slate-100">
                  判据不达标不是写一句免责声明就完了：名单按"在几档 decay 上都被选中"分两层，不确定性变成分流动作
                </h4>
                <span className="muted ml-auto">
                  触发条件 <code className="font-mono text-[10px]">{String(delivery.trigger)}</code>
                </span>
              </div>
              <p className="mt-2 text-[11.5px] leading-relaxed text-slate-300">{String(delivery.rule)}</p>
              <div className="mt-3 grid gap-2.5 lg:grid-cols-2">
                <div className="rounded-xl border border-emerald-400/30 bg-black/25 px-3 py-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-medium text-slate-400">核心层（三档 decay 全部选中）</span>
                    <Badge className="border-emerald-400/30 bg-emerald-400/10 text-emerald-200">可直接下单</Badge>
                  </div>
                  <div className="num mt-1.5 text-[19px] font-semibold text-emerald-200">
                    {int0(Number(deliveryCore.n_kox))} 人
                    <span className="ml-2 text-[14px] font-normal text-slate-300">{usd0(Number(deliveryCore.amount_usd))}</span>
                    <span className="ml-1.5 text-[12px] font-normal text-slate-500">
                      占参照档支出 {pct1(Number(deliveryCore.share_of_spend))}
                    </span>
                  </div>
                  <p className="mt-1 text-[11px] leading-relaxed text-slate-400">{String(deliveryCore.action)}</p>
                </div>
                <div className="rounded-xl border border-amber-400/35 bg-black/25 px-3 py-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-medium text-slate-400">假设敏感层（只在部分档位出现）</span>
                    <Badge className="border-amber-400/35 bg-amber-400/10 text-amber-200">不自动执行</Badge>
                  </div>
                  <div className="num mt-1.5 text-[19px] font-semibold text-amber-200">
                    {int0(Number(deliverySens.n_kox))} 人
                    <span className="ml-2 text-[14px] font-normal text-slate-300">{usd0(Number(deliverySens.amount_usd))}</span>
                    <span className="ml-1.5 text-[12px] font-normal text-slate-500">
                      占参照档支出 {pct1(Number(deliverySens.share_of_spend))}
                    </span>
                  </div>
                  <p className="mt-1 text-[11px] leading-relaxed text-slate-400">{String(deliverySens.action)}</p>
                </div>
              </div>
              <Note tone="good">
                <b className="text-emerald-200">这句话才是要传达的：</b>这个建模假设的不确定性只影响{' '}
                <b className="num">{pct1(Number(deliverySens.share_of_spend))}</b> 的预算（
                {usd0(Number(deliverySens.amount_usd))}），而且已经定位到具体{' '}
                <b className="num">{int0(Number(deliverySens.n_kox))}</b> 个人 —— 剩下{' '}
                <b className="num">{pct1(Number(deliveryCore.share_of_spend))}</b> 的钱怎么花与 decay 取值无关。
                从"整份名单打个问号"变成"分流规则"。
              </Note>
              <div className="mt-2 grid gap-3 xl:grid-cols-[1fr_1.15fr]">
                <div className="overflow-x-auto">
                  <div className="muted mb-1.5">逐 campaign 分层（读 per_campaign[*].delivery_tiers）</div>
                  <table className="w-full min-w-[420px]">
                    <thead>
                      <tr>
                        <th className="th">campaign</th>
                        <th className="th text-right">参照档选中</th>
                        <th className="th text-right">核心层</th>
                        <th className="th text-right">敏感层（参照档内 / 并集）</th>
                        <th className="th text-right">敏感层金额</th>
                      </tr>
                    </thead>
                    <tbody>
                      {decayPerCampaign.map((c) => {
                        const t = (c.delivery_tiers ?? null) as Loose | null;
                        const core = (t?.stable_core ?? null) as Loose | null;
                        const sens = (t?.assumption_sensitive ?? null) as Loose | null;
                        if (!core || !sens) return null;
                        return (
                          <tr key={String(c.campaign_id)} className="hairline">
                            <td className="td num">{String(c.campaign_id)}</td>
                            <td className="td num text-right">
                              {int0(Number(t?.n_selected_reference))}
                              <div className="muted">并集 {int0(Number(t?.n_union_across_decays))}</div>
                            </td>
                            <td className="td num text-right text-emerald-200">
                              {int0(Number(core.n))}
                              <div className="muted">{pct1(Number(core.share_of_reference_spend))} 支出</div>
                            </td>
                            <td className="td num text-right text-amber-200">
                              {int0(Number(sens.n_in_reference_plan))} / {int0(Number(sens.n))}
                              <div className="muted">{pct1(Number(sens.share_of_reference_spend))} 支出</div>
                            </td>
                            <td className="td num text-right text-amber-200">
                              {usd0(Number(sens.amount_usd_in_reference_plan))}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  <Note>
                    敏感层两个人数不是同一件事，故并列：<b className="text-slate-300">参照档内</b>{' '}
                    = 在正式链路（decay ={fixed(Number(decayScan.reference_decay), 1)}）那份清单里、但换档会掉出去的人（三个 campaign 合计{' '}
                    {int0(Number(deliverySens.n_kox))} 人，与上面那格一致）；<b className="text-slate-300">并集</b>{' '}
                    = 三档里任意一档出现过、但没有三档全中的人（含参照档没选、别的档才冒出来的）。
                    顶部政策格用的是前者，因为要交出去的清单就是参照档那一份。
                  </Note>
                </div>
                <div>
                  <div className="muted mb-1.5">
                    敏感层逐人（{int0(sensitiveKox.length)} 人 · 金额区间来自三档实测，min = 0 表示该档整个没选它）
                  </div>
                  <div className="max-h-[280px] overflow-auto rounded-xl border border-white/10">
                    <table className="w-full min-w-[420px]">
                      <thead className="sticky top-0 z-10 bg-ink-800/95 backdrop-blur">
                        <tr>
                          <th className="th">达人 / campaign</th>
                          <th className="th text-right">参照档金额</th>
                          <th className="th text-right">区间 min ~ max</th>
                          <th className="th">被选中的 decay 档</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sensitiveKox.map((k) => (
                          <tr key={`${k.campaign_id}-${String(k.kox_id)}`} className="hairline">
                            <td className="td">
                              <span className="num text-[11px] text-slate-200">{String(k.kox_id)}</span>
                              <div className="muted num">{k.campaign_id}</div>
                            </td>
                            <td className="td num text-right text-amber-200">{usd0(Number(k.amount_usd_reference))}</td>
                            <td className="td num text-right text-slate-400">
                              {usd0(Number(k.amount_usd_min))} ~ {usd0(Number(k.amount_usd_max))}
                            </td>
                            <td className="td num text-[10.5px] text-slate-400">
                              {((k.selected_at_decays ?? []) as number[]).map((d) => fixed(Number(d), 1)).join(' / ')}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <Note>{String(delivery.why_not_just_a_caveat)}</Note>
                </div>
              </div>
            </div>
          ) : (
            <MissingArtifact
              file="data/metrics.json → budget_decay_sensitivity.delivery_policy"
              what="分层交付策略"
              how="重跑 Python 侧 decay 扫描生成该字段后执行 npm run refresh。缺它时页面只能承认「名单会随假设变动」，给不出分流动作。"
            />
          )}
        </Panel>
      ) : (
        <MissingArtifact
          file="data/metrics.json → budget_decay_sensitivity"
          what="decay 建模假设的敏感性扫描"
          how="重跑 Python 侧预算链路生成该字段后执行 npm run refresh。缺它时页面上 decay = 0.7 就只是一个没有证据的选择。"
        />
      )}

      {/* ================= B 方差归因：伪重复口径 ================= */}
      {bVar && bUnit ? (
        <Panel
          title={`⑧ 为什么「少浪费」来自降低方差 —— 以及为什么那个 pooled p = ${
            bPooled ? sci(Number((bPooled.variance_ratio_test as Loose).p_two_sided)) : '—'
          } 不能用`}
          subtitle={`观测单位：${int0(Number(bUnit.n_seeds))} 个种子 × ${int0(Number(bUnit.n_campaigns))} 个 campaign = ${int0(Number(bUnit.n_obs_pooled))} 个 (seed, campaign) 观测；独立单位是 ${String(bUnit.independent_unit)}`}
          tone="warn"
          right={
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge
                className={
                  bUnit.pooled_p_value_usable
                    ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200'
                    : 'border-rose-400/35 bg-rose-400/10 text-rose-200'
                }
              >
                pooled_p_value_usable = {String(bUnit.pooled_p_value_usable)}
              </Badge>
              <TruthChip kind="python" />
            </div>
          }
        >
          <div className="grid gap-3 xl:grid-cols-[1.1fr_1fr]">
            <div>
              <div className="muted mb-1.5">
                独立单位（种子）上的方差比检验：三个 campaign 各自 n = {int0(Number(bUnit.n_seeds))}，这才是可引用的口径
              </div>
              <table className="w-full min-w-[460px]">
                <thead>
                  <tr>
                    <th className="th">campaign</th>
                    <th className="th text-right">基线浪费率 std</th>
                    <th className="th text-right">KOXPilot std</th>
                    <th className="th text-right">std 比</th>
                    <th className="th text-right">F</th>
                    <th className="th text-right">p（双尾）</th>
                  </tr>
                </thead>
                <tbody>
                  {bPerCampaign.map(({ cid, v }) => {
                    const t = (v.variance_ratio_test ?? null) as Loose | null;
                    return (
                      <tr key={cid} className="hairline">
                        <td className="td num">{cid}</td>
                        <td className="td num text-right text-rose-300">{fixed(Number(t?.std_a), 4)}</td>
                        <td className="td num text-right text-emerald-300">{fixed(Number(t?.std_b), 4)}</td>
                        <td className="td num text-right">{fixed(Number(v.std_ratio_baseline_over_koxpilot), 2)}×</td>
                        <td className="td num text-right">{fixed(Number(t?.f), 1)}</td>
                        <td className="td num text-right font-semibold text-cyan-200">{sci(Number(t?.p_two_sided))}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {bPooled && (
                <Note tone="warn">
                  <AlertTriangle size={11} className="mr-1 inline" />
                  <b className="text-amber-200">
                    pooled（{int0(Number(bPooled.n_obs))} 个 (seed, campaign) 观测）的 F ={' '}
                    {fixed(Number((bPooled.variance_ratio_test as Loose).f), 1)}、p ={' '}
                    {sci(Number((bPooled.variance_ratio_test as Loose).p_two_sided))} 是伪重复，量级不可当真。
                  </b>{' '}
                  {String(bVar.independence_caveat ?? '')}
                </Note>
              )}
              <Note>{String(bVar.caveat ?? '')}</Note>
            </div>
            <div className="space-y-2">
              {bPooled && (
                <div className="rounded-xl border border-emerald-400/30 bg-emerald-400/[0.06] px-3 py-2.5">
                  <div className="text-[12px] font-medium text-emerald-200">
                    主证据一：极端频次计数（不依赖任何分布假设）
                  </div>
                  <div className="mt-1.5 grid grid-cols-2 gap-2">
                    <div className="rounded-lg border border-rose-400/25 bg-black/25 px-2.5 py-2">
                      <div className="text-[10px] text-slate-500">基线浪费率 &gt; 50%</div>
                      <div className="num text-[17px] font-semibold text-rose-200">
                        {int0(Number(bPooled.n_obs_baseline_share_above_50pct))}/{int0(Number(bPooled.n_obs))}
                      </div>
                      <div className="muted">另有 {int0(Number(bPooled.n_obs_baseline_share_below_5pct))} 个观测 &lt; 5%（运气好）</div>
                    </div>
                    <div className="rounded-lg border border-emerald-400/25 bg-black/25 px-2.5 py-2">
                      <div className="text-[10px] text-slate-500">KOXPilot 浪费率 &gt; 50%</div>
                      <div className="num text-[17px] font-semibold text-emerald-200">
                        {int0(Number(bPooled.n_obs_koxpilot_share_above_50pct))}/{int0(Number(bPooled.n_obs))}
                      </div>
                      <div className="muted">
                        最大值只有 {pct1(Number((bPooled.koxpilot_waste_share as Loose).max))}
                      </div>
                    </div>
                  </div>
                  <p className="mt-1.5 text-[11px] leading-relaxed text-slate-300">
                    基线是<b className="text-rose-200">高方差赌博</b>：同一套预算，运气好时几乎不浪费，踩坑时把钱几乎全烧掉
                    （max {pct1(Number((bPooled.baseline_waste_share as Loose).max))}）。KOXPilot 换来的主要不是"平均更好"，
                    而是<b className="text-emerald-200">把坏尾巴切掉</b>。这两个计数是 {int0(Number(bPooled.n_obs))} 个 (seed, campaign)
                    观测上的频次，不做任何正态假设。
                  </p>
                </div>
              )}
              {bBands.length > 0 && bCond && (
                <div className="rounded-xl border border-white/12 bg-white/[0.03] px-3 py-2.5">
                  <div className="text-[12px] font-medium text-slate-200">主证据二：按"基线运气"分档的条件胜率</div>
                  <div className="mt-1.5 space-y-1.5">
                    {bBands.map((b) => (
                      <div key={String(b.band)} className="rounded-lg border border-white/10 bg-black/20 px-2.5 py-1.5">
                        <div className="text-[11px] text-slate-300">{String(b.band)}</div>
                        <div className="mt-0.5 flex flex-wrap gap-x-3 text-[10.5px] text-slate-400">
                          <span>
                            观测 <b className="num text-slate-200">{int0(Number(b.n_obs))}</b>
                          </span>
                          <span>
                            胜率{' '}
                            <b className={`num ${Number(b.koxpilot_win_rate) >= 0.9 ? 'text-emerald-300' : 'text-rose-300'}`}>
                              {pct1(Number(b.koxpilot_win_rate))}
                            </b>
                          </span>
                          <span>
                            跑输 <b className="num text-rose-300">{int0(Number(b.n_koxpilot_loses))}</b>
                          </span>
                          <span>
                            平均少浪费 <b className="num text-slate-200">{usd0(Number(b.mean_saved_usd))}</b>
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                  <p className="mt-1.5 text-[11px] leading-relaxed text-slate-400">
                    难听的那半句一起报：全部{' '}
                    <b className="num text-rose-200">
                      {int0(Number(bCond.n_koxpilot_loses))}/{int0(Number(bCond.n_obs))}
                    </b>{' '}
                    个跑输的观测里，基线浪费率最高也只有{' '}
                    <b className="num">{pct1(Number(bCond.max_baseline_waste_share_among_koxpilot_losses))}</b> ——
                    即"基线本来就没踩坑"时我们赢不了，赢的是坑真的存在的那些场景。
                  </p>
                </div>
              )}
              <Note tone="warn">
                <b className="text-amber-200">措辞纪律：</b>{String(bUnit.note ?? '')}
              </Note>
            </div>
          </div>
        </Panel>
      ) : (
        <MissingArtifact
          file="data/multiseed.json → B_variance_attribution"
          what="方差归因与伪重复口径"
          how="跑 make multiseed 生成 output/multiseed.json（含 independence_unit）后执行 npm run refresh。"
        />
      )}

      {/* ================= 缺失产物 ================= */}
      {!llmCompare && (
        <MissingArtifact
          file="data/llm_compare.json"
          what="LLM / 规则逐条差异明细"
          how={
            <>
              metrics.json 的表 6 status ={' '}
              <code className="font-mono text-[10px]">{String((metrics?.table_6_llm_vs_rule as Loose | undefined)?.status ?? 'unknown')}</code>
              ，其中 A4 语义适配的量化对照已经补齐（见上面第 ④ 块）；仍然缺的是<b>逐条</b>差异明细这份单独产物。
              可用的真实 LLM 对照在 llm_bench.json 与 prompt_bench.json，已在本页第 ②③ 块展示。
            </>
          }
        />
      )}

      <Note>
        <ArrowRight size={11} className="mr-1 inline text-cyan-300" />
        本页想说两句反直觉的话。第一句：<b className="text-slate-200">大模型不是免费的准确率</b> ——
        它在标签错配这种需要业务常识的任务上确实赢过规则，但只有把业务判据写清楚之后才赢；在 G0/G1 这类纯统计信号上，
        规则又快又准还 0 token，所以生产链路是混合的。第二句更贵：
        <b className="text-slate-200">单个种子上的归因可能整个反过来</b> ——
        定稿那一次看像"钱省在分散投放上"，跑满 12 个种子才发现分散化的符号都不稳，稳定的那一段是门禁与质量排序。
        招牌数字我留着，但对外结论取的是更诚实的那一列。
      </Note>
    </div>
  );
}
