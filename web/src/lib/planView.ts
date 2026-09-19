/**
 * 「这次的钱花得值不值、为什么没花完」的展示口径（契约 §4.1 / §4.2）。
 *
 * 为什么单独一个文件：同一套结论要在两条通道上出现——
 * Python 服务可达时字段直接来自 `/api/plan`，服务未连接时由浏览器引擎现算。
 * 两边的**定义**只允许写一次，否则「每美元有效曝光」在一处按有效曝光算、
 * 在另一处按名义曝光算，读者比出来的结论就是错的。
 *
 * 所以这里做两件事：
 * 1. 给出契约形状的构造函数（`armRow` / `browserArms` / `browserAdvice`），
 *    浏览器侧算出来的对象与服务返回的 JSON **同名同义**；
 * 2. 给出展示层要用的派生量（每美元有效曝光、预算利用率），供两条通道共用。
 *
 * 计算本身一律调用 `src/budget/` 与 `src/engine/` 里既有的函数，本文件不重写任何业务逻辑。
 */

import { REVIEW_SPEND_DISCOUNT } from '../budget/policy.ts';
import {
  JUDGE_GROUND_TRUTH,
  LENIENT_VIEW_ASSUMPTION,
  MAIN_VIEW_ASSUMPTION,
  effectiveViewCalibers,
  gateResultsFor,
  gtIndex,
  planAudit,
  planCampaign,
  planDiversifiedNoGate,
  targetingReason,
  type BudgetPlan,
  type GroundTruth,
  type PlanAudit,
} from '../budget/index.ts';
import { neighborMarkets } from '../engine/taxonomy.ts';
import { pyRound } from '../engine/stats.ts';
import type { Thresholds } from '../engine/thresholds.ts';
import type { CampaignSpec, GateResult, Kox } from '../engine/types.ts';
import type { AdviceRow, AllocationArm, AllocationBlock, JudgeBlock } from './api.ts';
import { usd0 } from './format.ts';

/**
 * 预算利用率低于这条线，才把「钱花不出去」当成本次的主结论并给放宽建议。
 * 与服务端 `LOW_UTILIZATION_LINE` 是同一个数（契约 §4.2 写明 60%）。
 */
export const LOW_UTILIZATION_LINE = 0.6;

/** 三条臂的中文名（与服务端 `ARM_LABEL` 一致）。 */
export const ARM_LABEL: Record<string, string> = {
  koxpilot: 'KOXPilot 决策',
  follower_rank: '按粉丝量排序',
  diversified_no_gate: '只做结构分散、不做质量门禁',
};

const round2 = (x: number): number => Math.round(x * 100) / 100;

/* ------------------------------------------------------------------ */
/* 展示层派生量                                                        */
/* ------------------------------------------------------------------ */

/**
 * 每美元买到的有效曝光（**主口径**，标注为水号的曝光按 0 计）——这是本对比的判优字段。
 * 老版本服务只有 `value_per_dollar` / `expected_value`（与主口径同数同义）时按序兜底，
 * 花费为 0 一律给 `null`（"没花钱"不等于"花了钱没效果"）。
 */
export function valuePerDollar(arm: AllocationArm): number | null {
  const gt = arm.effective_views_gt_per_dollar;
  if (gt !== undefined && gt !== null) return gt;
  if (arm.value_per_dollar !== undefined && arm.value_per_dollar !== null) return arm.value_per_dollar;
  return arm.spend > 0 ? arm.expected_value / arm.spend : null;
}

/**
 * 每美元买到的有效曝光（**宽松口径**，水号曝光按 50% 计）。
 * 给它的唯一理由是让读者看出结论是否依赖"水号曝光全部作废"这个假设；
 * 老版本服务没有这个字段时返回 `null`，展示层写"—"而不是拿主口径的数字顶上。
 */
export function lenientPerDollar(arm: AllocationArm): number | null {
  const v = arm.effective_views_gt_lenient_per_dollar;
  return v === undefined ? null : v;
}

/** 每美元的引擎**事前估分**（次级信息，不参与判优）。 */
export function enginePerDollar(arm: AllocationArm): number | null {
  const v = arm.engine_value_per_dollar;
  return v === undefined ? null : v;
}

/** 有效曝光合计（主口径）。 */
export function effectiveViews(arm: AllocationArm): number {
  return arm.effective_views_gt ?? arm.expected_value;
}

/**
 * 在给定口径下每美元买得最多的那条臂。并列时取第一条。
 * 用来在表里如实标出"这次谁更划算"——包含标出领先方不是 KOXPilot 的那次。
 */
export function leadingArm(
  arms: readonly AllocationArm[],
  metric: (arm: AllocationArm) => number | null,
): AllocationArm | null {
  let best: AllocationArm | null = null;
  let bestVal = -Infinity;
  for (const a of arms) {
    const v = metric(a);
    if (v === null || v <= bestVal) continue;
    best = a;
    bestVal = v;
  }
  return best;
}

/** 合约数：这条臂要签、要沟通、要交付的人数。老版本服务没有此字段时给 `null`。 */
export function contractCount(arm: AllocationArm): number | null {
  return arm.n_selected ?? null;
}

/** 浪费占这条臂花费的比例（花费为 0 时给 `null`）。 */
export function wasteShare(arm: AllocationArm): number | null {
  if (arm.waste_usd === undefined || arm.spend <= 0) return null;
  return arm.waste_usd / arm.spend;
}

/**
 * 表下那句取舍结论：把"每美元领先"与"这份领先的代价"放在同一句里。
 *
 * 全部由当次数据现算，不硬编任何数字与任何臂名——用户会用自由文本 brief 现场跑。
 * 三种情况都必须说得出口：
 * 1. 领先方不是 KOXPilot：说它每美元买得更多，同时说它把多少钱花在水号 / 高风险号上、要签多少份合约；
 * 2. 领先方是 KOXPilot：说它赢在哪，也说对照臂输在哪，不只报喜；
 * 3. KOXPilot 既不是每美元最高、浪费占比也不是最低：如实说这条 brief 上门禁没起到应有作用。
 */
export function tradeoffLine(arms: readonly AllocationArm[]): string | null {
  const us = arms.find((a) => a.arm === 'koxpilot') ?? null;
  const lead = leadingArm(arms, valuePerDollar);
  if (!us || !lead) return null;
  const vpd = (a: AllocationArm): number | null => valuePerDollar(a);
  const cost = (a: AllocationArm): string => {
    const share = wasteShare(a);
    const n = contractCount(a);
    const money = `${usd0(a.waste_usd ?? 0)}${share === null ? '' : `（占其花费 ${(share * 100).toFixed(1)}%）`}`;
    return `${money}花在水号与高风险号上${n === null ? '' : `，要签 ${n} 份合约`}`;
  };
  // 浪费占比最低的那条臂：按占比比，不按绝对金额比——各臂花费差几十倍，绝对金额不可比
  let lowest: AllocationArm | null = null;
  for (const a of arms) {
    const s = wasteShare(a);
    if (s === null) continue;
    const cur = lowest === null ? null : wasteShare(lowest);
    if (cur === null || s < cur) lowest = a;
  }
  const usShare = wasteShare(us);
  const usN = contractCount(us);
  const ours = `KOXPilot${usN === null ? '' : ` ${usN} 份合约`}，浪费 ${usd0(us.waste_usd ?? 0)}${
    usShare === null ? '' : `（${(usShare * 100).toFixed(1)}%）`
  }`;

  if (lead.arm === us.arm) {
    const others = arms.filter((a) => a.arm !== us.arm && vpd(a) !== null);
    // 对照对象优先取「只分散不门禁」那条臂：它是这张表上最值得说清的一条，
    // 没有这条臂时退回每美元第二高的那条。
    const rival =
      others.find((a) => a.arm === 'diversified_no_gate') ??
      (others.length > 0 ? others.reduce((m, a) => ((vpd(a) ?? 0) > (vpd(m) ?? 0) ? a : m)) : null);
    const gap =
      rival && (vpd(rival) ?? 0) > 0 && vpd(us) !== null
        ? `，每美元比它多 ${(((vpd(us) as number) / (vpd(rival) as number) - 1) * 100).toFixed(1)}%`
        : '';
    const tail = rival ? `「${rival.label}」把 ${cost(rival)}${gap}。` : '';
    return `这条 brief 上每美元有效曝光最高的是 KOXPilot：${ours.replace(/^KOXPilot\s*/, '')}，买的都是过门禁的库存。${tail}`;
  }

  const lift =
    vpd(us) !== null && (vpd(us) as number) > 0 && vpd(lead) !== null
      ? `多 ${(((vpd(lead) as number) / (vpd(us) as number) - 1) * 100).toFixed(1)}%`
      : '更多';
  const head = `「${lead.label}」每美元比 KOXPilot ${lift}，代价是把 ${cost(lead)}；${ours}，贵在只买过门禁的库存。`;
  if (lowest && lowest.arm !== us.arm && usShare !== null) {
    const ls = wasteShare(lowest) as number;
    return `${head}这条 brief 上 KOXPilot 既不是每美元最高，浪费占比也不是最低（KOXPilot ${(usShare * 100).toFixed(1)}%，最低是「${lowest.label}」的 ${(ls * 100).toFixed(1)}%），门禁在这条 brief 上没起到应有作用。`;
  }
  return head;
}

/** 预算利用率：这次真正花得出去的钱占预算多少。 */
export function utilizationOf(alloc: Pick<AllocationBlock, 'budget_usd' | 'allocated_usd'>): number {
  return alloc.budget_usd > 0 ? alloc.allocated_usd / alloc.budget_usd : 0;
}

/** 是否要把「预算没花完」当成本次的主结论。 */
export function isLowUtilization(alloc: Pick<AllocationBlock, 'budget_usd' | 'allocated_usd'>): boolean {
  return alloc.budget_usd > 0 && utilizationOf(alloc) < LOW_UTILIZATION_LINE;
}

/* ------------------------------------------------------------------ */
/* 契约形状：arms[]                                                    */
/* ------------------------------------------------------------------ */

/**
 * 一条臂的对外口径：花费、按标注结算的有效曝光（主口径 + 宽松口径）、浪费金额。
 *
 * 判优字段全部取 A6 审计（`planAudit`）的产出，换算成"每美元"复用
 * `effectiveViewCalibers`——这里不写第二套除法，否则同一句结论会在服务与浏览器各算一遍。
 * `engine_*` 是引擎的事前估分（`value(k)` 按等效条数加总），作为次级信息给出，不判优。
 */
export function armRow(arm: string, plan: BudgetPlan, audit: PlanAudit): AllocationArm {
  const spend = round2(plan.spent_usd);
  const caliber = effectiveViewCalibers(audit, spend);
  const engineValue = pyRound(
    plan.selected.reduce((sum, a) => sum + a.value_score, 0),
    1,
  );
  return {
    arm,
    label: ARM_LABEL[arm] ?? arm,
    spend,
    // 要签、要沟通、要交付的合约数：与服务 `_arm_row` 的 `n_selected` 同名同义。
    // 它是成本不是战绩，判优指标里不含这笔，所以表上单列一列。
    n_selected: plan.selected.length,
    ...caliber,
    waste_usd: round2(audit.wasted_spend_usd),
    engine_expected_value: engineValue,
    engine_value_per_dollar: spend > 0 ? pyRound(engineValue / spend, 2) : null,
    // 兼容字段：与 effective_views_gt / effective_views_gt_per_dollar 同数同义
    expected_value: caliber.effective_views_gt,
    value_per_dollar: caliber.effective_views_gt_per_dollar,
  };
}

/**
 * 这组对比的口径声明，与服务端 `_judge_block()` 同名同义、措辞一致。
 * 降级态不能少这一块：读者必须能看出判优字段与裁判来源，不能只在服务态才说清楚。
 */
export function judgeBlock(): JudgeBlock {
  return {
    metric: 'effective_views_gt_per_dollar',
    judge: JUDGE_GROUND_TRUTH,
    main_assumption: MAIN_VIEW_ASSUMPTION,
    lenient_assumption: LENIENT_VIEW_ASSUMPTION,
    engine_score_role:
      'engine_value_per_dollar 是引擎的事前估分（选人排序依据），只作次级信息展示，不参与判优',
    pipeline_separation:
      '选人与分钱（A1–A5）只读可观测字段，读不到标注；只有 A6 审计这一组对比读标注（静态扫描 + 运行期哨兵在守这条边界）',
  };
}

export interface BrowserArmsInput {
  records: readonly Partial<Kox>[];
  spec: CampaignSpec;
  thresholds: Thresholds;
  results: Map<string, GateResult>;
  plan: BudgetPlan;
  baseline: BudgetPlan;
  gtById: Map<string, GroundTruth>;
  decay: number;
}

/** 浏览器侧的三条臂，字段与服务返回的 `allocation.arms[]` 同名同义。 */
export function browserArms(input: BrowserArmsInput): AllocationArm[] {
  const { records, spec, thresholds, results, plan, baseline, gtById, decay } = input;
  const diversified = planDiversifiedNoGate(records, spec, thresholds, results, { decay });
  return [
    armRow('koxpilot', plan, planAudit(plan, gtById)),
    armRow('follower_rank', baseline, planAudit(baseline, gtById)),
    armRow('diversified_no_gate', diversified, planAudit(diversified, gtById)),
  ];
}

/* ------------------------------------------------------------------ */
/* 契约形状：advice[]                                                  */
/* ------------------------------------------------------------------ */

function adviceRow(
  key: AdviceRow['key'],
  title: string,
  action: string,
  budget: number,
  baseSpend: number,
  spendable: number,
  extraPicked: number,
  qualityNote: string,
): AdviceRow {
  return {
    key,
    title,
    action,
    spendable_usd: round2(spendable),
    extra_spendable_usd: round2(spendable - baseSpend),
    utilization_after: budget ? Math.round((spendable / budget) * 10000) / 10000 : 0,
    extra_picked: extraPicked,
    quality_note: qualityNote,
  };
}

export interface BrowserAdviceInput {
  records: readonly Partial<Kox>[];
  spec: CampaignSpec;
  thresholds: Thresholds;
  results: Map<string, GateResult>;
  plan: BudgetPlan;
  includeReview: boolean;
  decay: number;
}

/**
 * 预算没花完时的三条放宽建议，口径与服务端 `_advice` 逐条对齐：
 * 每条只改一处，改完在同一份数据上真跑一遍召回→门禁→分配再报数，不做估算。
 */
export function browserAdvice(input: BrowserAdviceInput): AdviceRow[] {
  const { records, spec, thresholds, results, plan, includeReview, decay } = input;
  const budget = plan.budget_usd || 0;
  const baseSpend = plan.spent_usd;
  if (budget <= 0 || baseSpend >= budget * LOW_UTILIZATION_LINE) return [];

  const pickedIds = new Set(plan.selected.map((a) => a.kox_id));
  const rows: AdviceRow[] = [];

  /** 在放宽后的投放规格上重跑召回 → 门禁 → 分配（与主链路同一批函数）。 */
  const replan = (relaxed: CampaignSpec): BudgetPlan => {
    const pool = records.filter((k) => targetingReason(k, relaxed) === null);
    const res = gateResultsFor(pool, relaxed, thresholds);
    return planCampaign(pool, relaxed, thresholds, res, { includeReview, decay });
  };

  // ---- 1. 去掉年龄限定（放宽人群定向，不动风控）----
  const ages = spec.target_age_buckets ?? [];
  if (ages.length > 0) {
    const relaxed = replan({ ...spec, target_age_buckets: [] });
    const newcomers = relaxed.selected.filter((a) => !pickedIds.has(a.kox_id));
    rows.push(
      adviceRow(
        'relax_age',
        '去掉年龄段限定',
        `把人群里的「${ages.join('、')}」这条限定去掉，平台 / 品类 / 市场与预算都不动`,
        budget,
        baseSpend,
        relaxed.spent_usd,
        newcomers.length,
        newcomers.length > 0
          ? `新进清单的 ${newcomers.length} 人都是门禁判「可投」的人：这条放宽动的是人群定向，风控口径一条没改。`
          : '清单不会变：卡住这笔预算的不是年龄限定。',
      ),
    );
  }

  // ---- 2. 扩到相邻市场（放宽地域定向）----
  const markets = spec.target_markets ?? [];
  const expanded = markets.length > 0 ? neighborMarkets(markets) : [];
  const added = expanded.filter((m) => !markets.includes(m));
  if (added.length > 0) {
    const relaxed = replan({ ...spec, target_markets: expanded });
    const newcomers = relaxed.selected.filter((a) => !pickedIds.has(a.kox_id));
    const outside = newcomers.filter((a) => !markets.includes(a.country));
    rows.push(
      adviceRow(
        'expand_markets',
        `加投相邻市场：${added.join('、')}`,
        `目标市场从 ${markets.join('、')} 扩到 ${expanded.join('、')}（相邻市场，语言与受众重叠度较高），其余定向不动`,
        budget,
        baseSpend,
        relaxed.spent_usd,
        newcomers.length,
        `新进清单的 ${newcomers.length} 人同样逐条过了四层门禁，其中 ${outside.length} 人的主市场在新增市场里——` +
          '这批人触达的是相邻市场受众，是否算本次目标人群需要业务确认。',
      ),
    );
  }

  // ---- 3. 需人核档带折扣进清单（放宽的是采购动作，门禁判定不变）----
  const nReview = [...results.values()].filter((r) => r.verdict === 'review').length;
  if (nReview > 0 && !includeReview) {
    const pool = records.filter((k) => targetingReason(k, spec) === null);
    const withReview = planCampaign(pool, spec, thresholds, results, { includeReview: true, decay });
    const reviewSpend = withReview.selected
      .filter((a) => a.verdict === 'review')
      .reduce((sum, a) => sum + a.amount_usd, 0);
    const otherSpend = withReview.selected
      .filter((a) => a.verdict !== 'review')
      .reduce((sum, a) => sum + a.amount_usd, 0);
    const reviewPicked = withReview.selected.filter((a) => a.verdict === 'review').length;
    rows.push(
      adviceRow(
        'discount_review',
        '让「需人核」的人带折扣进清单',
        `本次有 ${nReview} 人判为需人核；按单人金额 ${Math.round(REVIEW_SPEND_DISCOUNT * 100)}% 先给试投位，人核通过后再追加到全额`,
        budget,
        baseSpend,
        otherSpend + reviewSpend * REVIEW_SPEND_DISCOUNT,
        reviewPicked,
        `新增的 ${reviewPicked} 人是需人核档：必须先人工核过再下单，${usd0(reviewSpend * REVIEW_SPEND_DISCOUNT)} 是打折后的试投金额，` +
          '也就是这条建议的风险敞口上限。',
      ),
    );
  }

  rows.sort((a, b) => b.extra_spendable_usd - a.extra_spendable_usd);
  return rows;
}

/** 这条约束是**下限**（越低越危险），判断"卡住了没"时方向与其它上限型约束相反。 */
const MIN_TYPE_CHECKS = new Set(['longtail_min_share']);

/**
 * 「为什么没花完」的人话说明（口径与服务端 `_unallocated_why` 一致）：
 * 依据全部来自分配器自己的约束报告，不另立解释。
 */
export function browserUnallocatedWhy(plan: BudgetPlan): string {
  const budget = plan.budget_usd || 0;
  const remaining = Math.max(0, budget - plan.spent_usd);
  const utilization = budget ? plan.spent_usd / budget : 0;
  if (remaining <= Math.max(1, budget * 0.005)) {
    return `预算基本投尽（利用率 ${(utilization * 100).toFixed(1)}%），剩下的是按条报价取整后的零头。`;
  }
  const parts: string[] = [];
  if (utilization < 0.9) {
    parts.push(
      `本次通过门禁、且报价与曝光齐备的候选只有 ${plan.candidate_pool} 人，其中 ${plan.selected.length} 人进了清单`,
    );
  }
  const binding: string[] = [];
  const broken: string[] = [];
  for (const check of plan.constraints.checks) {
    if (!check.enforced || check.name === 'total_budget' || !check.limit) continue;
    const item = `${check.desc}（当前 ${(check.actual * 100).toFixed(1)}%）`;
    // 与服务端同一处理：已经守不住的约束单独说，别和"贴住上限"混成一句。
    if (!check.satisfied) {
      broken.push(item);
      continue;
    }
    const near = MIN_TYPE_CHECKS.has(check.name)
      ? check.actual <= check.limit * 1.02
      : check.actual >= check.limit * 0.98;
    if (near) binding.push(item);
  }
  if (binding.length > 0) parts.push(`剩余额度已经贴住结构配额，再加钱就会破：${binding.join('；')}`);
  if (broken.length > 0) {
    parts.push(`候选池在这个定向下本身就偏，这几条配额已经守不住，继续加钱只会更偏：${broken.join('；')}`);
  }
  if (parts.length === 0) {
    parts.push(
      `候选名额已用尽（候选 ${plan.candidate_pool} 人 × 每人最多 ${plan.purchase_model.max_posts_per_kox} 条内容）`,
    );
  }
  return `${parts.join('；')}。`;
}

/** 浏览器侧的分配摘要，字段与服务返回的 `allocation` 同名同义（只取展示要用的那几个）。 */
export function browserAllocation(plan: BudgetPlan, arms: AllocationArm[], why: string): AllocationBlock {
  const kox = arms.find((a) => a.arm === 'koxpilot');
  const follower = arms.find((a) => a.arm === 'follower_rank');
  const saved = (follower?.waste_usd ?? 0) - (kox?.waste_usd ?? 0);
  return {
    budget_usd: round2(plan.budget_usd),
    allocated_usd: round2(plan.spent_usd),
    unallocated_usd: round2(Math.max(0, plan.budget_usd - plan.spent_usd)),
    unallocated_why: why,
    picked: plan.selected.length,
    arms,
    judge: judgeBlock(),
    saved_usd: round2(saved),
    saved_share: plan.budget_usd ? Math.round((saved / plan.budget_usd) * 10000) / 10000 : 0,
  };
}

/** 使用 `gtIndex` 的转发，方便调用方一处 import。 */
export { gtIndex };
