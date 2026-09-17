import React from 'react';
import { AlertTriangle, Check, Coins, TrendingUp, X } from 'lucide-react';
import { DuoBars, MixBar } from '../components/charts';
import { EvidenceBody } from '../components/EvidenceDrawer';
import { Badge, CountUp, Drawer, Hint, Note, Panel, Segmented, Stat, TruthChip } from '../components/ui';
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

function ConstraintList({ plan }: { plan: BudgetPlan }): React.ReactElement {
  return (
    <div className="space-y-1.5">
      {plan.constraints.checks.map((c) => (
        <div key={c.name} className="flex items-center gap-2">
          {c.satisfied ? (
            <Check size={12} className="shrink-0 text-emerald-400" />
          ) : (
            <X size={12} className="shrink-0 text-rose-400" />
          )}
          <span className="text-[11px] text-slate-300">{c.desc}</span>
          {!c.enforced && (
            <Hint text="基线臂刻意不施加结构约束（那就是行业最朴素做法），所以这些检查只做记录、不强制。这样两臂的差异才来自选人依据本身。">
              <Badge className="border-slate-400/25 bg-slate-400/10 text-slate-400">仅记录</Badge>
            </Hint>
          )}
          <span className="num ml-auto text-[11px] text-slate-400">
            {c.name === 'total_budget' ? usd0(c.actual) : c.actual <= 1.05 ? pct1(c.actual) : fixed(c.actual, 2)}
            <span className="mx-1 text-slate-600">/</span>
            {c.name === 'total_budget' ? usd0(c.limit) : c.limit <= 1.05 ? pct1(c.limit, 0) : fixed(c.limit, 2)}
          </span>
        </div>
      ))}
      <div
        className={`mt-1 rounded-lg border px-2.5 py-1.5 text-[11px] ${
          plan.constraints.all_enforced_satisfied
            ? 'border-emerald-400/25 bg-emerald-400/[0.06] text-emerald-200'
            : 'border-rose-400/25 bg-rose-400/[0.06] text-rose-200'
        }`}
      >
        {plan.constraints.all_enforced_satisfied
          ? '强制约束全部满足'
          : `未满足：${plan.constraints.violations.join('、')}`}
        <span className="ml-2 text-slate-400">
          预算利用率 {pct1(plan.constraints.utilization)}
          {plan.constraints.utilization_ok ? '（达标）' : '（低于目标）'}
        </span>
      </div>
    </div>
  );
}

export function DecisionTab({
  result,
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

  const other = arm === 'koxpilot' ? result.baseline : result.plan;
  const audit = result.audit;
  const armAudit = arm === 'koxpilot' ? audit.koxpilot : audit.baseline;

  // 产物口径下（不含 review、衰减 0.7）可以和 Python 产物直接对数；参数一改就不可比，如实说明
  const comparable = !includeReview && decay === 0.7;
  /** 分层标记只有在参数与产物口径一致（不纳入 review、decay = 参照档）时才敢往行上打。 */
  const tierComparable = !includeReview && referenceDecay !== null && Math.abs(decay - referenceDecay) < 1e-9;
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
    <div className="space-y-4">
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
        <span className="muted">
          切换后下面所有数字由浏览器重算（两臂共用同一份定向筛选与报价/估价口径，唯一差异是选人依据）
        </span>
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
        <Stat label="预估 CPM" value={usd2(plan.est_cpm_usd)} hint="按方案自身预估曝光算（不含 gt 修正）" />
        <Stat label="预估 CPE" value={usd2(plan.est_cpe_usd)} hint="按互动量算的单次互动成本" />
        <Stat
          label="有效曝光（gt 裁判）"
          value={compact(armAudit.effective_views_gt)}
          hint={`名义曝光 ${int0(armAudit.nominal_views)}；水号曝光按 0 计（宽松口径按 50% 计为 ${compact(armAudit.effective_views_gt_lenient)}）`}
          tone={arm === 'koxpilot' ? 'good' : 'bad'}
        />
        <Stat
          label="浪费金额（gt 裁判）"
          value={usd0(armAudit.wasted_spend_usd)}
          hint={`占花费 ${pct1(armAudit.wasted_spend_share)}；买中真水号 ${armAudit.n_fraud_selected} 人、高危 ${armAudit.n_high_risk_selected} 人`}
          tone={armAudit.wasted_spend_share > 0.2 ? 'bad' : 'good'}
        />
      </div>

      <div className="grid gap-3 lg:grid-cols-[1.05fr_1fr]">
        <Panel
          title="两臂对照：同预算、同候选口径，只换选人依据"
          subtitle="浪费金额与有效曝光一律按 ground truth 计算，不使用引擎自身分数（防自证）"
          right={<TruthChip kind="audit" />}
        >
          <DuoBars
            leftName="按粉丝量基线"
            rightName="KOXPilot"
            rows={[
              { label: '选中达人数', left: result.baseline.n_selected, right: result.plan.n_selected, note: '基线把预算堆在少数头部达人身上' },
              { label: '花费（美元）', left: result.baseline.spent_usd, right: result.plan.spent_usd },
              { label: '名义曝光', left: audit.baseline.nominal_views, right: audit.koxpilot.nominal_views, note: '名义曝光基线并不吃亏 —— 差距全在「有多少是真的」' },
              { label: '有效曝光（gt）', left: audit.baseline.effective_views_gt, right: audit.koxpilot.effective_views_gt },
              { label: '浪费金额（gt）', left: audit.baseline.wasted_spend_usd, right: audit.koxpilot.wasted_spend_usd },
            ]}
            fmt={(x) => (x > 1e5 ? compact(x) : int0(x))}
          />
          <div className="mt-3 grid grid-cols-3 gap-2">
            <div className="rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-2">
              <div className="text-[10px] text-slate-500">少浪费</div>
              <div className={`num text-[16px] ${audit.saved_usd >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
                {audit.saved_usd >= 0 ? '' : '−'}
                {usd0(Math.abs(audit.saved_usd))}
              </div>
              <div className="muted">占预算 {signedPct1(audit.saved_share_of_budget)}</div>
            </div>
            <div className="rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-2">
              <div className="text-[10px] text-slate-500">有效曝光提升</div>
              <div className={`num text-[16px] ${audit.effective_view_uplift >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
                {signedPct1(audit.effective_view_uplift)}
              </div>
              <div className="muted">宽松口径 {signedPct1(audit.effective_view_uplift_lenient)}</div>
            </div>
            <div className="rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-2">
              <div className="text-[10px] text-slate-500">每千美元有效曝光</div>
              <div className="num text-[16px] text-cyan-200">{compact(audit.effective_views_per_1k_usd.koxpilot)}</div>
              <div className="muted">基线 {compact(audit.effective_views_per_1k_usd.baseline)}</div>
            </div>
          </div>
          {audit.saved_usd < 0 && (
            <Note tone="warn">
              本 campaign 的对照结果是<b className="text-amber-200">负的</b>：KOXPilot 反而多花了浪费钱。原因是这个候选池里基线恰好没买到多少水号，
              而 KOXPilot 为了满足长尾配额买入了更多中小达人。三个 campaign 合计仍然是正收益，但单个 campaign 的失败案例照实展示，不挑好看的看板。
            </Note>
          )}
        </Panel>

        <Panel title={`结构约束校验 · ${arm === 'koxpilot' ? 'KOXPilot 臂' : '基线臂'}`} subtitle="约束在分配过程中强制生效，不是事后检查">
          <ConstraintList plan={plan} />
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

      <Panel title="分配过程日志（trace）" subtitle="这些中文说明由分配器在运行时生成，双实现一致性校验会逐字比对它们">
        <ol className="space-y-1.5">
          {plan.trace.map((t, i) => (
            <li key={t} className="flex gap-2 text-[12px] leading-relaxed text-slate-300">
              <span className="num mt-[1px] flex h-4 w-4 shrink-0 items-center justify-center rounded bg-cyan-400/15 text-[9px] text-cyan-200">
                {i + 1}
              </span>
              {t}
            </li>
          ))}
        </ol>
        {other.trace.length > 0 && (
          <details className="mt-2">
            <summary className="cursor-pointer text-[11px] text-slate-500 hover:text-slate-300">
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
      </Panel>

      <Panel
        title={`选中清单 · ${plan.n_selected} 人 / ${plan.n_posts} 条`}
        subtitle="value = 平均播放 × 真实性折扣 × 适配 × 受众匹配 × KPI 权重；效率 = 边际 value / 报价"
        right={
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
        }
        bodyClass="px-0 py-0"
      >
        <div className="max-h-[480px] overflow-auto">
          <table className="w-full border-collapse">
            <thead className="sticky top-0 z-10 bg-ink-800/95 backdrop-blur">
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
                <th className="th">gt</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((a) => {
                const gt = koxById.get(a.kox_id)?.gt;
                return (
                  <tr
                    key={a.kox_id}
                    onClick={() => setOpenId(a.kox_id)}
                    className="hairline cursor-pointer transition-colors hover:bg-cyan-400/[0.06]"
                  >
                    <td className="td">
                      <div className="font-medium text-slate-200">{a.handle}</div>
                      <div className="num text-[10px] text-slate-600">{a.kox_id}</div>
                    </td>
                    <td className="td text-[11px] text-slate-400">
                      {a.bucket}
                      <span className="mx-1 text-slate-600">/</span>
                      {a.country} · {PLATFORM_LABEL[a.platform] ?? a.platform}
                    </td>
                    <td className="td num text-right text-cyan-100">
                      {usd0(a.amount_usd)}
                      {a.price_estimated && (
                        <Hint text="该达人未给报价，按同组 avg_cpm 的 P50 估算，产物里用 price_estimated 标注，不假装是真实报价">
                          <span className="ml-1 text-[9px] text-amber-300">估</span>
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
                    <td className="td text-[11px] text-slate-400">{PICKED_BY_LABEL[a.picked_by] ?? a.picked_by}</td>
                    <td className="td">
                      {(() => {
                        const t = tierByKox.get(a.kox_id);
                        if (!tierComparable) {
                          return (
                            <Hint text={`分层来自 metrics.json 的 decay 三档扫描，产物口径是「不纳入 review、decay = ${referenceDecay ?? '?'}」。当前参数与该口径不一致，标记会误导，所以这里不显示。`}>
                              <span className="text-[10px] text-slate-600">口径不符</span>
                            </Hint>
                          );
                        }
                        if (arm !== 'koxpilot') return <span className="text-[10px] text-slate-600">—</span>;
                        if (!t) return <span className="text-[10px] text-slate-600">未在扫描内</span>;
                        return t.tier === 'core' ? (
                          <Hint text={`三档 decay（${((decaySens?.scan ?? []) as number[]).join(' / ')}）都选中这个人，入选不依赖该假设，可直接下单。金额区间 $${Math.round(t.min)} ~ $${Math.round(t.max)}。`}>
                            <Badge className="border-emerald-400/30 bg-emerald-400/10 text-emerald-200">核心</Badge>
                          </Hint>
                        ) : (
                          <Hint text={`只在 decay = ${t.decays.map((d) => d.toFixed(1)).join(' / ')} 时被选中，换档会掉出名单；金额区间 $${Math.round(t.min)} ~ $${Math.round(t.max)}（min = 0 表示某一档整个没选它）。按分层交付规则：标记「重复触达折扣假设敏感」，转人工确认或改按单条采购，不自动执行。`}>
                            <Badge className="border-amber-400/35 bg-amber-400/10 text-amber-200">假设敏感</Badge>
                          </Hint>
                        );
                      })()}
                    </td>
                    <td className="td">
                      <Badge className={VERDICT_COLOR[a.verdict]}>{VERDICT_LABEL[a.verdict] ?? a.verdict}</Badge>
                    </td>
                    <td className="td">
                      {gt?.is_fraud ? (
                        <Badge className="border-rose-400/30 bg-rose-400/10 text-rose-200">真水号 {gt.fraud_type}</Badge>
                      ) : gt?.brand_safety === 'high' ? (
                        <Badge className="border-rose-400/30 bg-rose-400/10 text-rose-200">高危</Badge>
                      ) : (
                        <span className="text-[10px] text-slate-600">正常</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="border-t border-white/[0.06] px-4 py-2">
          {tierComparable && arm === 'koxpilot' && tierByKox.size > 0 && (
            <div className="mb-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
              <span className="text-slate-400">
                本 campaign 交付分层（读 metrics.json → budget_decay_sensitivity.per_campaign[*].delivery_tiers）：
              </span>
              <Badge className="border-emerald-400/30 bg-emerald-400/10 text-emerald-200">
                核心 {int0(tierRowStats.core)} 人 · {usd0(tierRowStats.coreUsd)}
              </Badge>
              <Badge className="border-amber-400/35 bg-amber-400/10 text-amber-200">
                假设敏感 {int0(tierRowStats.sensitive)} 人 · {usd0(tierRowStats.sensitiveUsd)}
                {plan.spent_usd > 0 && <>（{pct1(tierRowStats.sensitiveUsd / plan.spent_usd)} 支出）</>}
              </Badge>
              <span className="muted">
                敏感层不自动执行：转人工确认或改按单条采购 —— 换一档"重复触达折扣"假设，这些人就会掉出名单。
              </span>
            </div>
          )}
          <span className="muted">
            gt 一列只用于事后审计展示 —— 分配过程完全读不到它。基线臂里那些标红的人，就是「按粉丝量买」实际会把钱交给谁。
          </span>
        </div>
      </Panel>

      <div className="grid gap-3 lg:grid-cols-[1fr_1.1fr]">
        <Panel
          title="浏览器现算 vs Python 产物"
          subtitle={
            comparable
              ? '当前参数与产物口径一致（不含 review、衰减 0.7），因此可以直接对数'
              : '你改过参数（纳入 review 或改了衰减系数），与产物口径不可比 —— 这里如实标注，不做假对齐'
          }
          tone={comparable && allParity ? 'accent' : 'default'}
          right={
            comparable ? (
              <Badge className={allParity ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200' : 'border-rose-400/30 bg-rose-400/10 text-rose-200'}>
                {allParity ? '逐项一致' : '存在差异'}
              </Badge>
            ) : (
              <Badge className="border-amber-400/30 bg-amber-400/10 text-amber-200">参数已改，不可比</Badge>
            )
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
                    <td className="td num text-right text-cyan-100">{ts}</td>
                    <td className="td num text-right text-slate-400">{py}</td>
                    <td className="td text-center">
                      {ok ? <Check size={13} className="mx-auto text-emerald-400" /> : <X size={13} className="mx-auto text-rose-400" />}
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
        </Panel>

        <Panel
          title="三个 campaign 的反事实价值汇总"
          subtitle="来自 audit.json（Python 全量产物）；单个 campaign 可能为负，照实列出"
          right={<TruthChip kind="python" />}
        >
          {cfTotals ? (
            <>
              <div className="mb-2 grid grid-cols-3 gap-2">
                <div className="rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-2">
                  <div className="text-[10px] text-slate-500">合计预算</div>
                  <div className="num text-[15px] text-slate-100">{usd0(Number(cfTotals.budget_usd))}</div>
                </div>
                <div className="rounded-lg border border-emerald-400/25 bg-emerald-400/[0.06] px-2.5 py-2">
                  <div className="text-[10px] text-slate-500">合计少浪费</div>
                  <div className="num text-[15px] text-emerald-300">{usd0(Number(cfTotals.saved_usd))}</div>
                  <div className="muted">占预算 {pct1(Number(cfTotals.saved_share_of_budget))}</div>
                </div>
                <div className="rounded-lg border border-cyan-400/25 bg-cyan-400/[0.06] px-2.5 py-2">
                  <div className="text-[10px] text-slate-500">有效曝光提升</div>
                  <div className="num text-[15px] text-cyan-200">{signedPct1(Number(cfTotals.effective_view_uplift))}</div>
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
                      <td className="td num text-right text-rose-300">{usd0(Number((c.baseline as Loose).wasted_spend_usd))}</td>
                      <td className="td num text-right text-emerald-300">{usd0(Number((c.koxpilot as Loose).wasted_spend_usd))}</td>
                      <td className={`td num text-right ${Number(c.saved_usd) >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
                        {Number(c.saved_usd) >= 0 ? '' : '−'}
                        {usd0(Math.abs(Number(c.saved_usd)))}
                      </td>
                      <td className={`td num text-right ${Number(c.effective_view_uplift) >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
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
        </Panel>
      </div>

      <Note>
        <TrendingUp size={11} className="mr-1 inline text-cyan-300" />
        采购模型：按内容条数买，单达人最多 3 条，第 n 条的边际有效曝光按 {plan.purchase_model.post_marginal_decay}^(n−1) 衰减 ——
        这样"把钱堆在一个人身上"会自然失去性价比，而不是靠一条硬规则拍死。
      </Note>

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
