import React from 'react';
import { AlertTriangle, Check, Coins, X } from 'lucide-react';
import { DuoBars, MixBar } from '../components/charts';
import { Collapse, Verdict } from '../components/Collapse';
import { EvidenceBody } from '../components/EvidenceDrawer';
import { Badge, CountUp, Drawer, Hint, Note, Panel, Segmented, Stat, TruthChip } from '../components/ui';
import { useLiveRun } from './Console';
import { BUCKET_ORDER, PLATFORM_LABEL } from '../engine/taxonomy';
import type { Kox } from '../engine/types';
import type { Allocation, BudgetPlan } from '../budget/allocator';
import type { Loose } from '../lib/artifacts';
import {
  BUCKET_LABEL,
  PICKED_BY_LABEL,
  VERDICT_COLOR,
  VERDICT_LABEL,
  compact,
  fixed,
  int0,
  pct1,
  signedPct1,
  usd0,
  usd2,
} from '../lib/format';
import type { PipelineResult } from '../lib/pipeline';

/** 清单默认只展开前 N 行，其余按需展开（避免整页被一张长表撑开）。 */
const TOP_N = 15;

function ConstraintList({ plan }: { plan: BudgetPlan }): React.ReactElement {
  return (
    <div className="space-y-1.5">
      {plan.constraints.checks.map((c) => (
        <div key={c.name} className="flex items-center gap-2">
          {c.satisfied ? (
            <Check size={12} className="shrink-0 text-emerald-600" />
          ) : (
            <X size={12} className="shrink-0 text-rose-600" />
          )}
          <span className="text-[11px] text-slate-700">{c.desc}</span>
          {!c.enforced && (
            <Hint text="基线臂不施加结构约束（对应行业最朴素做法），这些检查仅作记录，不参与强制。">
              <Badge className="border-slate-200 bg-slate-50 text-slate-600">仅记录</Badge>
            </Hint>
          )}
          <span className="num ml-auto text-[11px] text-slate-600">
            {c.name === 'total_budget' ? usd0(c.actual) : c.actual <= 1.05 ? pct1(c.actual) : fixed(c.actual, 2)}
            <span className="mx-1 text-slate-500">/</span>
            {c.name === 'total_budget' ? usd0(c.limit) : c.limit <= 1.05 ? pct1(c.limit, 0) : fixed(c.limit, 2)}
          </span>
        </div>
      ))}
      <div
        className={`mt-1 rounded-lg border px-2.5 py-1.5 text-[11px] ${
          plan.constraints.all_enforced_satisfied
            ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
            : 'border-rose-200 bg-rose-50 text-rose-700'
        }`}
      >
        {plan.constraints.all_enforced_satisfied
          ? '强制约束全部满足'
          : `未满足：${plan.constraints.violations.join('、')}`}
        <span className="ml-2 text-slate-600">
          预算利用率 {pct1(plan.constraints.utilization)}
          {plan.constraints.utilization_ok ? '（达标）' : '（低于目标）'}
        </span>
      </div>
    </div>
  );
}

/**
 * 12 种子稳健区间（读 multiseed.json 的 A_value_robustness.saved_share_of_budget）。
 *
 * 为什么在这里懒加载：Decision 的 props 签名不能改（App.tsx 由他人并行改动），
 * 但「少浪费 $xx」这个单次实测值旁边**必须**标清楚它只是一个种子的观测 ——
 * 12 种子的均值 ± 标准差与 95% CI 才是可对外说的口径。宁可多一次 fetch，
 * 也不把区间数字写死在组件里。
 */
function useRobustSaved(): { mean: number; std: number; low: number; high: number; n: number } | null | 'missing' {
  const [v, setV] = React.useState<{ mean: number; std: number; low: number; high: number; n: number } | null | 'missing'>(null);
  React.useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await fetch(`${import.meta.env.BASE_URL}data/multiseed.json`, { cache: 'force-cache' });
        if (!res.ok) throw new Error(String(res.status));
        const j = (await res.json()) as Loose;
        const s = j?.A_value_robustness?.saved_share_of_budget as Loose | undefined;
        if (!s || !alive) {
          if (alive) setV('missing');
          return;
        }
        setV({
          mean: Number(s.mean),
          std: Number(s.std),
          low: Number(s.ci95_low),
          high: Number(s.ci95_high),
          n: Number(s.n),
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

export function DecisionTab({
  result: appResult,
  koxById,
  budgetArtifact,
  auditArtifact,
  metricsArtifact,
  includeReview,
  decay,
}: {
  result: PipelineResult | null;
  koxById: Map<string, Partial<Kox>>;
  budgetArtifact: Loose | null;
  auditArtifact: Loose | null;
  metricsArtifact: Loose | null;
  includeReview: boolean;
  decay: number;
}): React.ReactElement {
  const [arm, setArm] = React.useState<'koxpilot' | 'baseline'>('koxpilot');
  const [sort, setSort] = React.useState<'amount' | 'efficiency' | 'value' | 'views'>('amount');
  const [openId, setOpenId] = React.useState<string | null>(null);
  const [showAll, setShowAll] = React.useState(false);
  const robust = useRobustSaved();

  /**
   * 读者在上半页自己敲的那条 brief 跑出来的结果优先。
   * 合并后的「投放决策」tab 上下同页，如果这里还显示预置 brief 的名单，
   * 就会出现「上面换了 brief、下面没动」的假联动观感 —— 那是最不该犯的错。
   */
  const live = useLiveRun();
  const result = live?.result ?? appResult;
  const isCustom = Boolean(live);

  const plan = result ? (arm === 'koxpilot' ? result.plan : result.baseline) : null;

  // hooks 必须无条件调用：排序在早退之前算好，plan 为空时给空数组
  const sorted: Allocation[] = React.useMemo(() => {
    if (!plan) return [];
    const key = (a: Allocation): number =>
      sort === 'amount' ? a.amount_usd : sort === 'efficiency' ? a.efficiency : sort === 'value' ? a.value_score : a.est_views;
    return [...plan.selected].sort((x, y) => key(y) - key(x));
  }, [plan, sort]);

  /**
   * 交付分层标记：kox_id -> 核心层 / 假设敏感层（含三档金额区间）。
   * 全部读 metrics.json → budget_decay_sensitivity.per_campaign[*].delivery_tiers，不写死任何人或人数。
   * 产物是按"正式链路那一档 decay、不纳入 review"算出来的，所以只有参数与产物口径一致时才敢往行上打标。
   */
  const decaySens = (metricsArtifact?.budget_decay_sensitivity ?? null) as Loose | null;
  const referenceDecay = decaySens ? Number(decaySens.reference_decay) : null;
  const tierByKox = React.useMemo(() => {
    const out = new Map<string, { tier: 'core' | 'sensitive'; min: number; ref: number; max: number; decays: number[] }>();
    const cid = result?.spec.campaign_id;
    if (!decaySens || !cid) return out;
    const row = ((decaySens.per_campaign ?? []) as Loose[]).find((c) => String(c.campaign_id) === cid);
    const tiers = (row?.delivery_tiers ?? null) as Loose | null;
    if (!tiers) return out;
    for (const [tier, key] of [
      ['core', 'stable_core'],
      ['sensitive', 'assumption_sensitive'],
    ] as const) {
      for (const k of (((tiers[key] as Loose | undefined)?.kox ?? []) as Loose[])) {
        out.set(String(k.kox_id), {
          tier,
          min: Number(k.amount_usd_min),
          ref: Number(k.amount_usd_reference),
          max: Number(k.amount_usd_max),
          decays: ((k.selected_at_decays ?? []) as number[]).map(Number),
        });
      }
    }
    return out;
  }, [decaySens, result?.spec.campaign_id]);

  if (!result || !plan) return <div className="muted px-1">流水线尚未运行。</div>;

  /** 当前这份清单里核心 / 敏感各占多少（用行上的实际金额汇总，不引用产物里的汇总数）。 */
  const tierRowStats = { core: 0, sensitive: 0, coreUsd: 0, sensitiveUsd: 0 };
  for (const a of plan.selected) {
    const t = tierByKox.get(a.kox_id);
    if (!t) continue;
    if (t.tier === 'core') {
      tierRowStats.core += 1;
      tierRowStats.coreUsd += a.amount_usd;
    } else {
      tierRowStats.sensitive += 1;
      tierRowStats.sensitiveUsd += a.amount_usd;
    }
  }

  const shown = showAll ? sorted : sorted.slice(0, TOP_N);
  const other = arm === 'koxpilot' ? result.baseline : result.plan;
  const audit = result.audit;
  const armAudit = arm === 'koxpilot' ? audit.koxpilot : audit.baseline;

  // 产物口径下（不含 review、衰减 0.7）可以和 Python 产物直接对数；参数一改就不可比，如实说明
  // 自定义 brief 不在 Python 产物里，天然不可比 —— 这一点必须显式排除，否则会拿别的 campaign 的数对上去
  const comparable = !includeReview && decay === 0.7 && !isCustom;
  /** 分层标记只有在参数与产物口径一致（不纳入 review、decay = 参照档）时才敢往行上打。 */
  const tierComparable = !isCustom && !includeReview && referenceDecay !== null && Math.abs(decay - referenceDecay) < 1e-9;
  const pyPlan = ((budgetArtifact?.plans ?? []) as Loose[]).find((p) => p.campaign_id === result.spec.campaign_id) ?? null;
  const pyArm = pyPlan ? (pyPlan[arm === 'koxpilot' ? 'koxpilot' : 'baseline_followers'] as Loose | undefined) : undefined;
  const parityRows: Array<[string, string, string, boolean]> = [];
  if (comparable && pyArm) {
    const push = (label: string, ts: number, py: number, fmt: (x: number) => string): void => {
      parityRows.push([label, fmt(ts), fmt(py), Math.abs(ts - py) < 0.005]);
    };
    push('选中人数', plan.n_selected, Number(pyArm.n_selected), (x) => int0(x));
    push('内容条数', plan.n_posts, Number(pyArm.n_posts), (x) => int0(x));
    push('花费', plan.spent_usd, Number(pyArm.spent_usd), (x) => usd2(x));
    push('预估 CPM', plan.est_cpm_usd, Number(pyArm.est_cpm_usd), (x) => usd2(x));
    push('预估有效曝光', plan.est_effective_views, Number(pyArm.est_effective_views), (x) => int0(x));
  }
  const allParity = parityRows.length > 0 && parityRows.every((r) => r[3]);

  const cfTotals = (auditArtifact?.counterfactual_value_audit?.totals ?? null) as Loose | null;
  const cfPer = (auditArtifact?.counterfactual_value_audit?.per_campaign ?? []) as Loose[];
  /** 为负的 campaign 从产物里现算，不写死是哪一个、也不写死金额。 */
  const cfLosers = cfPer.filter((c) => Number(c.saved_usd) < 0);

  return (
    <div className="space-y-3">
      {/* ---- 本页结论 ---- */}
      <Verdict
        what="第二步：同预算下「看门禁」比「看粉丝量」省了多少"
        tone={audit.saved_usd >= 0 ? 'good' : 'warn'}
        conclusion={
          <>
            {isCustom ? '你自己敲的那条 brief' : result.spec.campaign_id} 的 {usd0(result.plan.budget_usd)} 预算落到{' '}
            <b>{int0(result.plan.n_selected)}</b> 人 / {int0(result.plan.n_posts)} 条；与「按粉丝量降序买」的基线相比，
            这一次<b>{audit.saved_usd >= 0 ? '少' : '多'}浪费 {usd0(Math.abs(audit.saved_usd))}</b>
            （占预算 {signedPct1(audit.saved_share_of_budget)}）。
            {robust && robust !== 'missing' ? (
              <>
                {' '}
                跨 {robust.n} 组数据的稳健口径为 {signedPct1(robust.mean)} ± {pct1(robust.std)}。
              </>
            ) : null}
          </>
        }
        stats={[
          { label: '花费 / 预算', value: `${usd0(plan.spent_usd)}`, tone: 'good' },
          { label: '选中', value: `${int0(plan.n_selected)} 人` },
          {
            label: '浪费占花费',
            value: pct1(armAudit.wasted_spend_share),
            tone: armAudit.wasted_spend_share > 0.2 ? 'bad' : 'good',
          },
          {
            label: '有效曝光提升',
            value: signedPct1(audit.effective_view_uplift),
            tone: audit.effective_view_uplift >= 0 ? 'good' : 'bad',
          },
        ]}
      />

      <div className="flex flex-wrap items-center gap-2">
        <Segmented
          value={arm}
          onChange={setArm}
          options={[
            { value: 'koxpilot', label: 'KOXPilot：门禁 + 约束优化', hint: '只买 pass（可选纳入 review），按边际性价比贪心 + 分层配额修正' },
            { value: 'baseline', label: '对照：按粉丝量降序买', hint: '行业最朴素做法：不看门禁、不做结构约束，粉丝多就先买' },
          ]}
        />
        <TruthChip kind="rule" />
        {isCustom && (
          <Badge className="border-live-300 bg-live-50 text-live-700">
            当前是你自己敲的 brief（campaign_id = {result.spec.campaign_id}），与 Python 产物不可比
          </Badge>
        )}
        <span className="muted">两臂共用同一份定向筛选与报价口径，唯一差异是选人依据</span>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6">
        <Stat
          label="预算 / 花费"
          value={<CountUp value={plan.spent_usd} format={(v) => usd0(v)} />}
          hint={`预算 ${usd0(plan.budget_usd)}，利用率 ${pct1(plan.utilization)}`}
          icon={<Coins size={11} />}
          tone="accent"
        />
        <Stat
          label="选中达人 / 内容条数"
          value={
            <>
              <CountUp value={plan.n_selected} format={(v) => int0(v)} />
              <span className="text-[13px] text-slate-500"> 人 / {int0(plan.n_posts)} 条</span>
            </>
          }
          hint={`候选池 ${int0(plan.candidate_pool)} 人；单人最多 ${plan.purchase_model.max_posts_per_kox} 条，第 n 条边际按 ${plan.purchase_model.post_marginal_decay}^(n−1) 衰减`}
        />
        <Stat label="预估 CPM" value={usd2(plan.est_cpm_usd)} hint="按方案自身预估曝光计算" />
        <Stat label="预估 CPE" value={usd2(plan.est_cpe_usd)} hint="按互动量算的单次互动成本" />
        <Stat
          label="有效曝光（真实标注结算）"
          value={compact(armAudit.effective_views_gt)}
          hint={`名义曝光 ${int0(armAudit.nominal_views)}；水号曝光按 0 计（宽松口径按 50% 计为 ${compact(armAudit.effective_views_gt_lenient)}）`}
          tone={arm === 'koxpilot' ? 'good' : 'bad'}
        />
        <Stat
          label="浪费金额（真实标注结算）"
          value={usd0(armAudit.wasted_spend_usd)}
          hint={`占花费 ${pct1(armAudit.wasted_spend_share)}；买中真水号 ${armAudit.n_fraud_selected} 人、高危 ${armAudit.n_high_risk_selected} 人`}
          tone={armAudit.wasted_spend_share > 0.2 ? 'bad' : 'good'}
        />
      </div>

      <div className="grid gap-3 lg:grid-cols-[1.05fr_1fr]">
        <Panel
          title="两臂对照：同预算、同候选口径，只换选人依据"
          subtitle="浪费金额与有效曝光按真实标注结算，不使用引擎自身给出的分数"
          right={<TruthChip kind="audit" />}
        >
          <DuoBars
            leftName="按粉丝量基线"
            rightName="KOXPilot"
            rows={[
              { label: '选中达人数', left: result.baseline.n_selected, right: result.plan.n_selected, note: '基线把预算堆在少数头部达人身上' },
              { label: '花费（美元）', left: result.baseline.spent_usd, right: result.plan.spent_usd },
              { label: '名义曝光', left: audit.baseline.nominal_views, right: audit.koxpilot.nominal_views, note: '名义曝光基线并不吃亏 —— 差距全在「有多少是真的」' },
              { label: '有效曝光（真实标注）', left: audit.baseline.effective_views_gt, right: audit.koxpilot.effective_views_gt },
              { label: '浪费金额（真实标注）', left: audit.baseline.wasted_spend_usd, right: audit.koxpilot.wasted_spend_usd },
            ]}
            fmt={(x) => (x > 1e5 ? compact(x) : int0(x))}
          />
          <div className="mt-3 grid grid-cols-3 gap-2">
            <div className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2">
              <div className="text-[11px] text-slate-600">少浪费</div>
              <div className={`num text-[16px] ${audit.saved_usd >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                {audit.saved_usd >= 0 ? '' : '−'}
                {usd0(Math.abs(audit.saved_usd))}
              </div>
              <div className="muted">
                占预算 {signedPct1(audit.saved_share_of_budget)}
                {robust && robust !== 'missing'
                  ? ` · 稳健区间 [${signedPct1(robust.low)}, ${signedPct1(robust.high)}]`
                  : ''}
              </div>
            </div>
            <div className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2">
              <div className="text-[11px] text-slate-600">有效曝光提升</div>
              <div className={`num text-[16px] ${audit.effective_view_uplift >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                {signedPct1(audit.effective_view_uplift)}
              </div>
              <div className="muted">宽松口径 {signedPct1(audit.effective_view_uplift_lenient)}</div>
            </div>
            <div className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2">
              <div className="text-[11px] text-slate-600">每千美元有效曝光</div>
              <div className="num text-[16px] text-live-700">{compact(audit.effective_views_per_1k_usd.koxpilot)}</div>
              <div className="muted">基线 {compact(audit.effective_views_per_1k_usd.baseline)}</div>
            </div>
          </div>
          {audit.saved_usd < 0 && (
            <Note tone="warn">
              本 campaign 的对照结果是<b className="text-amber-700">负的</b>：KOXPilot 反而多花了浪费钱。原因是这个候选池里基线恰好没买到多少水号，
              而 KOXPilot 为了满足长尾配额买入了更多中小达人。全部 campaign 合计仍为正收益。
            </Note>
          )}
        </Panel>

        <Panel title={`结构约束校验 · ${arm === 'koxpilot' ? 'KOXPilot 臂' : '基线臂'}`} subtitle="配额约束在分配过程中强制生效">
          <div
            className={`rounded-lg border px-2.5 py-1.5 text-[12px] ${
              plan.constraints.all_enforced_satisfied
                ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                : 'border-rose-200 bg-rose-50 text-rose-700'
            }`}
          >
            {plan.constraints.all_enforced_satisfied
              ? `强制约束全部满足（${plan.constraints.checks.length} 项校验）`
              : `未满足：${plan.constraints.violations.join('、')}`}
            <span className="ml-2 text-slate-700">
              预算利用率 {pct1(plan.constraints.utilization)}
              {plan.constraints.utilization_ok ? '（达标）' : '（低于目标）'}
            </span>
          </div>
          <div className="mt-2">
            <Collapse
              title="逐条约束的实际值 / 上限"
              hint="头部金额占比 ≤45%、长尾 ≥25%、单一国家 ≤60%，这些配额在分配过程中即生效"
            >
              <ConstraintList plan={plan} />
            </Collapse>
          </div>
          <div className="mt-3 space-y-3">
            <div>
              <div className="muted mb-1">层级分布（按金额）</div>
              <MixBar
                mix={plan.tier_mix}
                order={[...BUCKET_ORDER]}
                labels={BUCKET_LABEL}
                limits={[
                  { keys: ['macro', 'mega'], max: 0.45, name: '头部（macro+mega）金额占比' },
                  { keys: ['nano', 'micro'], min: 0.25, name: '长尾（nano+micro）金额占比' },
                ]}
              />
            </div>
            <div>
              <div className="muted mb-1">国家分布（按金额）</div>
              <MixBar
                mix={plan.country_mix}
                limits={Object.keys(plan.country_mix).length > 0 ? [{ keys: [Object.entries(plan.country_mix).sort((a, b) => b[1] - a[1])[0][0]], max: 0.6, name: '单一国家金额占比' }] : []}
              />
            </div>
            <div>
              <div className="muted mb-1">平台分布（按金额）</div>
              <MixBar mix={plan.platform_mix} labels={PLATFORM_LABEL} />
            </div>
          </div>
        </Panel>
      </div>

      <Collapse
        title={`分配过程：${plan.trace.length} 步决策日志（每一步为什么加人、为什么修正配额）`}
        hint="由分配器在本次运行中生成"
      >
        <ol className="space-y-1.5">
          {plan.trace.map((t, i) => (
            <li key={t} className="flex gap-2 text-[12px] leading-relaxed text-slate-700">
              <span className="num mt-[1px] flex h-4 w-4 shrink-0 items-center justify-center rounded bg-live-100 text-[9px] text-live-700">
                {i + 1}
              </span>
              {t}
            </li>
          ))}
        </ol>
        {other.trace.length > 0 && (
          <details className="mt-2">
            <summary className="cursor-pointer text-[11px] text-slate-500 hover:text-slate-700">
              查看{arm === 'koxpilot' ? '基线臂' : 'KOXPilot 臂'}的 trace
            </summary>
            <ol className="mt-1.5 space-y-1">
              {other.trace.map((t) => (
                <li key={t} className="muted">
                  · {t}
                </li>
              ))}
            </ol>
          </details>
        )}
      </Collapse>

      <Collapse
        title={`达人清单：${plan.n_selected} 人 / ${plan.n_posts} 条的逐人金额、性价比与交付分层`}
        hint="价值分 = 平均播放 × 真实性折扣 × 内容适配 × 受众匹配 × KPI 权重；性价比 = 边际价值 / 报价。点任意一行查看该达人的门禁证据链与淘汰理由"
      >
        <div className="mb-2 flex justify-end">
          <Segmented
            size="sm"
            value={sort}
            onChange={setSort}
            options={[
              { value: 'amount', label: '按金额' },
              { value: 'efficiency', label: '按性价比' },
              { value: 'value', label: '按价值分' },
              { value: 'views', label: '按曝光' },
            ]}
          />
        </div>
        <div className="max-h-[360px] overflow-auto">
          <table className="w-full border-collapse">
            <thead className="sticky top-0 z-10 bg-white backdrop-blur">
              <tr className="hairline">
                <th className="th">达人</th>
                <th className="th">层级 / 地区</th>
                <th className="th text-right">金额</th>
                <th className="th text-right">条数</th>
                <th className="th text-right">预估曝光</th>
                <th className="th text-right">价值分</th>
                <th className="th text-right">性价比</th>
                <th className="th">入选方式</th>
                <th className="th">交付分层</th>
                <th className="th">门禁</th>
                <th className="th">真实标注</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((a) => {
                const gt = koxById.get(a.kox_id)?.gt;
                return (
                  <tr
                    key={a.kox_id}
                    onClick={() => setOpenId(a.kox_id)}
                    className="hairline cursor-pointer transition-colors hover:bg-live-50"
                  >
                    <td className="td">
                      <div className="font-medium text-slate-800">{a.handle}</div>
                      <div className="num text-[10px] text-slate-500">{a.kox_id}</div>
                    </td>
                    <td className="td text-[11px] text-slate-600">
                      {a.bucket}
                      <span className="mx-1 text-slate-500">/</span>
                      {a.country} · {PLATFORM_LABEL[a.platform] ?? a.platform}
                    </td>
                    <td className="td num text-right text-live-700">
                      {usd0(a.amount_usd)}
                      {a.price_estimated && (
                        <Hint text="该达人未提供报价，按同组 CPM 中位数估算">
                          <span className="ml-1 text-[9px] text-amber-600">估</span>
                        </Hint>
                      )}
                    </td>
                    <td className="td num text-right">
                      {a.posts}
                      <span className="ml-1 text-[10px] text-slate-500">有效 {fixed(a.effective_posts, 2)}</span>
                    </td>
                    <td className="td num text-right">{compact(a.est_views)}</td>
                    <td className="td num text-right">{compact(a.value_score)}</td>
                    <td className="td num text-right">{fixed(a.efficiency, 2)}</td>
                    <td className="td text-[11px] text-slate-600">{PICKED_BY_LABEL[a.picked_by] ?? a.picked_by}</td>
                    <td className="td">
                      {(() => {
                        const t = tierByKox.get(a.kox_id);
                        if (!tierComparable) {
                          return (
                            <Hint text={`分层依据「不纳入待复核、重复触达折扣 = ${referenceDecay ?? '?'}」这一档口径计算，当前参数与之不一致，故不展示。`}>
                              <span className="text-[10px] text-slate-500">口径不符</span>
                            </Hint>
                          );
                        }
                        if (arm !== 'koxpilot') return <span className="text-[10px] text-slate-500">—</span>;
                        if (!t) return <span className="text-[10px] text-slate-500">未在扫描内</span>;
                        return t.tier === 'core' ? (
                          <Hint text={`重复触达折扣取三档（${((decaySens?.scan ?? []) as number[]).join(' / ')}）时均入选，可直接下单。金额区间 $${Math.round(t.min)} ~ $${Math.round(t.max)}。`}>
                            <Badge className="border-emerald-200 bg-emerald-50 text-emerald-700">核心</Badge>
                          </Hint>
                        ) : (
                          <Hint text={`仅在重复触达折扣 = ${t.decays.map((d) => d.toFixed(1)).join(' / ')} 时入选，换档会掉出名单；金额区间 $${Math.round(t.min)} ~ $${Math.round(t.max)}。需人工复核或改按单条采购。`}>
                            <Badge className="border-amber-300 bg-amber-50 text-amber-700">假设敏感</Badge>
                          </Hint>
                        );
                      })()}
                    </td>
                    <td className="td">
                      <Badge className={VERDICT_COLOR[a.verdict]}>{VERDICT_LABEL[a.verdict] ?? a.verdict}</Badge>
                    </td>
                    <td className="td">
                      {gt?.is_fraud ? (
                        <Badge className="border-rose-200 bg-rose-50 text-rose-700">真水号 {gt.fraud_type}</Badge>
                      ) : gt?.brand_safety === 'high' ? (
                        <Badge className="border-rose-200 bg-rose-50 text-rose-700">高危</Badge>
                      ) : (
                        <span className="text-[10px] text-slate-500">正常</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {sorted.length > TOP_N && (
          <div className="mt-2 flex justify-center">
            <button
              onClick={() => setShowAll((v) => !v)}
              className="focusable rounded-lg border border-slate-300 px-3 py-1 text-[11px] text-slate-700 hover:bg-slate-50"
            >
              {showAll ? `收起（只看金额前 ${TOP_N} 人）` : `展开全部 ${sorted.length} 人`}
            </button>
          </div>
        )}
        <div className="mt-2 border-t border-slate-300 pt-2">
          {tierComparable && arm === 'koxpilot' && tierByKox.size > 0 && (
            <div className="mb-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
              <span className="text-slate-600">交付分层：</span>
              <Badge className="border-emerald-200 bg-emerald-50 text-emerald-700">
                核心 {int0(tierRowStats.core)} 人 · {usd0(tierRowStats.coreUsd)}
              </Badge>
              <Badge className="border-amber-300 bg-amber-50 text-amber-700">
                假设敏感 {int0(tierRowStats.sensitive)} 人 · {usd0(tierRowStats.sensitiveUsd)}
                {plan.spent_usd > 0 && <>（{pct1(tierRowStats.sensitiveUsd / plan.spent_usd)} 支出）</>}
              </Badge>
              <span className="muted">敏感层需人工复核后下单，不自动执行。</span>
            </div>
          )}
          <span className="muted">「真实标注」一列仅用于事后审计展示，分配过程不读取。</span>
        </div>
      </Collapse>

      <div className="grid gap-2 lg:grid-cols-[1fr_1.1fr]">
        <Collapse
          title={
            comparable
              ? `与 Python 离线结果逐项对数（${allParity ? `${parityRows.length} 项全部一致` : '存在差异'}）`
              : isCustom
                ? '自定义 brief 与 Python 离线结果不可比（口径说明）'
                : '当前参数与离线结果口径不同，暂不可比（口径说明）'
          }
          hint={
            comparable
              ? '当前参数与离线结果口径一致：不纳入待复核、边际衰减 0.7'
              : isCustom
                ? '自定义 brief 没有对应的离线结果可对照'
                : '已修改参数（纳入待复核或调整了边际衰减），与离线结果口径不同'
          }
        >
          {comparable && parityRows.length > 0 ? (
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">指标</th>
                  <th className="th text-right">浏览器 TS 现算</th>
                  <th className="th text-right">Python 产物</th>
                  <th className="th text-center">一致</th>
                </tr>
              </thead>
              <tbody>
                {parityRows.map(([label, ts, py, ok]) => (
                  <tr key={label} className="hairline">
                    <td className="td">{label}</td>
                    <td className="td num text-right text-live-700">{ts}</td>
                    <td className="td num text-right text-slate-600">{py}</td>
                    <td className="td text-center">
                      {ok ? <Check size={13} className="mx-auto text-emerald-600" /> : <X size={13} className="mx-auto text-rose-600" />}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Note tone="warn">
              恢复默认参数（关闭「纳入 review」、衰减取 0.7）后这张表会重新可比。
              全量一致性校验结果见「架构」页，那里读的是 verify 脚本生成的 consistency.json。
            </Note>
          )}
        </Collapse>

        <Collapse
          title={`全部 ${cfPer.length} 个 campaign 的价值汇总（Python 离线全量结果）`}
          hint="单个 campaign 的少浪费可能为负，取决于该候选池里是否真的存在刷量账号"
        >
          <div className="mb-2">
            <TruthChip kind="python" />
          </div>
          {cfTotals ? (
            <>
              <div className="mb-2 grid grid-cols-3 gap-2">
                <div className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2">
                  <div className="text-[11px] text-slate-600">合计预算</div>
                  <div className="num text-[15px] text-slate-900">{usd0(Number(cfTotals.budget_usd))}</div>
                </div>
                <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-2">
                  <div className="text-[11px] text-slate-600">合计少浪费</div>
                  <div className="num text-[15px] text-emerald-600">{usd0(Number(cfTotals.saved_usd))}</div>
                  <div className="muted">占预算 {pct1(Number(cfTotals.saved_share_of_budget))}</div>
                </div>
                <div className="rounded-lg border border-live-200 bg-live-50 px-2.5 py-2">
                  <div className="text-[11px] text-slate-600">有效曝光提升</div>
                  <div className="num text-[15px] text-live-700">{signedPct1(Number(cfTotals.effective_view_uplift))}</div>
                  <div className="muted">
                    {compact(Number(cfTotals.effective_views_baseline))} → {compact(Number(cfTotals.effective_views_koxpilot))}
                  </div>
                </div>
              </div>
              <table className="w-full">
                <thead>
                  <tr>
                    <th className="th">campaign</th>
                    <th className="th text-right">预算</th>
                    <th className="th text-right">基线浪费</th>
                    <th className="th text-right">KOXPilot 浪费</th>
                    <th className="th text-right">少浪费</th>
                    <th className="th text-right">有效曝光</th>
                  </tr>
                </thead>
                <tbody>
                  {cfPer.map((c) => (
                    <tr key={String(c.campaign_id)} className="hairline">
                      <td className="td num">{String(c.campaign_id)}</td>
                      <td className="td num text-right">{usd0(Number(c.budget_usd))}</td>
                      <td className="td num text-right text-rose-600">{usd0(Number((c.baseline as Loose).wasted_spend_usd))}</td>
                      <td className="td num text-right text-emerald-600">{usd0(Number((c.koxpilot as Loose).wasted_spend_usd))}</td>
                      <td className={`td num text-right ${Number(c.saved_usd) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                        {Number(c.saved_usd) >= 0 ? '' : '−'}
                        {usd0(Math.abs(Number(c.saved_usd)))}
                      </td>
                      <td className={`td num text-right ${Number(c.effective_view_uplift) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                        {signedPct1(Number(c.effective_view_uplift))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {cfLosers.length > 0 ? (
                <Note tone="warn">
                  <AlertTriangle size={11} className="mr-1 inline" />
                  {cfLosers
                    .map(
                      (c) =>
                        `${String(c.campaign_id)} 是负的（少浪费 −${usd0(Math.abs(Number(c.saved_usd)))}、有效曝光 ${signedPct1(
                          Number(c.effective_view_uplift),
                        )}）`,
                    )
                    .join('；')}
                  。这一格没有被藏起来（哪个 campaign 为负由 audit.json 现算，不写死）：它说明门禁的价值依赖候选池里真有水号，
                  当基线恰好没踩坑时，结构约束带来的分散反而略微拖累了单次结果。
                </Note>
              ) : (
                <Note tone="good">
                  本轮 {cfPer.length} 个 campaign 的少浪费全部为非负（这一句同样由 audit.json 现算 —— 一旦有 campaign 转负，
                  上面这行会自动变成点名说明）。
                </Note>
              )}
            </>
          ) : (
            <Note tone="warn">audit.json 未生成，本板块留空。</Note>
          )}
        </Collapse>
      </div>

      <Drawer
        open={Boolean(openId)}
        onClose={() => setOpenId(null)}
        title={`${koxById.get(openId ?? '')?.handle ?? ''} · 证据链`}
        subtitle={openId ?? ''}
      >
        {openId && result.results.get(openId) && koxById.get(openId) && (
          <EvidenceBody
            kox={koxById.get(openId) as Partial<Kox>}
            result={result.results.get(openId)!}
            allocation={plan.selected.find((s) => s.kox_id === openId) ?? null}
          />
        )}
      </Drawer>
    </div>
  );
}
