/**
 * A6 审计（对应 Python `eval/audit.py`，SPEC 6.2）：把方案翻译成钱。
 *
 * 两条口径纪律，与 Python 逐条对齐：
 * 1. **用 ground truth 当裁判，不用引擎自己的分数。**有效曝光与浪费金额一律按 `gt` 计算；
 *    若用引擎的 authenticity_score 去算"我省了多少钱"，那是自证，数字再漂亮也不能信。
 * 2. **保守假设要给敏感性。**主口径把水号曝光按 0 计，同时给出"按 50% 计"的宽松对照，
 *    用来证明结论不依赖这个假设。
 *
 * 注意：这是**唯一**允许读 `gt` 的模块（门禁引擎入口物理剥离 gt）。
 */

import { pyRound } from '../engine/stats.ts';
import type { Kox } from '../engine/types.ts';
import type { BudgetPlan } from './allocator.ts';

/** 敏感性假设：水号仍有多少比例的曝光算"有效"。主口径取 0，此常量仅用于宽松对照。 */
export const FRAUD_RESIDUAL_VIEW_SHARE = 0.5;

/**
 * 有效曝光与浪费金额的裁判来源（与 Python `JUDGE_GROUND_TRUTH` 同值）。
 * 写成常量是为了让对外口径能把"谁在判"标出来：`ground_truth` = 数据集标注，
 * 不是引擎自己的分数。
 */
export const JUDGE_GROUND_TRUTH = 'ground_truth';

/** 两个口径的假设原文（与 Python `MAIN_VIEW_ASSUMPTION` / `LENIENT_VIEW_ASSUMPTION` 逐字对齐）。 */
export const MAIN_VIEW_ASSUMPTION = '主口径：标注为水号的达人，其曝光按 0 计入有效曝光';
export const LENIENT_VIEW_ASSUMPTION =
  `宽松口径：水号曝光按 ${Math.round(FRAUD_RESIDUAL_VIEW_SHARE * 100)}% 计入有效曝光。` +
  '两个口径同向才说明结论不依赖该假设';

/** 对外的"每美元有效曝光"口径（两个假设各一份）。 */
export interface EffectiveViewCalibers {
  judge: string;
  effective_views_gt: number;
  effective_views_gt_per_dollar: number | null;
  effective_views_gt_lenient: number;
  effective_views_gt_lenient_per_dollar: number | null;
}

/**
 * 把 {@link planAudit} 的结果翻成"每美元有效曝光"的对外口径（主口径 + 宽松口径）。
 * 与 Python `eval/audit.py` 的 `effective_view_calibers` 是同一段算术，逐字对齐：
 * 分子只取按标注算好的有效曝光（不接受引擎自评），花费为 0 时给 `null` 而不是 0。
 *
 * @param spendUsd 分母花费。缺省用 `audit.spent_usd`；显式传入是为了让调用方能用
 *   自己四舍五入后的花费当分母，保证"每美元 × 花费"自洽。
 */
export function effectiveViewCalibers(audit: PlanAudit, spendUsd?: number): EffectiveViewCalibers {
  const eff = audit.effective_views_gt;
  const effLenient = audit.effective_views_gt_lenient;
  const money = spendUsd === undefined ? audit.spent_usd : spendUsd;
  return {
    judge: JUDGE_GROUND_TRUTH,
    effective_views_gt: pyRound(eff, 1),
    effective_views_gt_per_dollar: money > 0 ? pyRound(eff / money, 2) : null,
    effective_views_gt_lenient: pyRound(effLenient, 1),
    effective_views_gt_lenient_per_dollar: money > 0 ? pyRound(effLenient / money, 2) : null,
  };
}

export interface GroundTruth {
  is_fraud: boolean;
  fraud_type: string | null;
  tag_mismatch: boolean;
  brand_safety: string;
  verdict: string;
}

export interface PlanAudit {
  strategy: string;
  n_selected: number;
  spent_usd: number;
  utilization: number;
  n_fraud_selected: number;
  n_high_risk_selected: number;
  fraud_spend_usd: number;
  fraud_spend_share: number;
  high_risk_spend_usd: number;
  high_risk_spend_share: number;
  wasted_spend_usd: number;
  wasted_spend_share: number;
  spend_on_gate_review_usd: number;
  spend_on_gate_reject_usd: number;
  nominal_views: number;
  effective_views_gt: number;
  effective_views_gt_lenient: number;
  effective_cpm_usd: number | null;
  effective_views_per_1k_usd: number;
  fraud_examples: Array<{
    kox_id: string;
    handle: string;
    amount_usd: number;
    fraud_type: string | null;
    followers_bucket: string;
  }>;
}

export function gtIndex(records: readonly Partial<Kox>[]): Map<string, GroundTruth> {
  const out = new Map<string, GroundTruth>();
  for (const r of records) {
    const gt = (r as { gt?: GroundTruth }).gt;
    if (gt) out.set(String(r.kox_id), gt);
  }
  return out;
}

/** 用 gt 审计单个方案：钱花到哪些人身上、有效曝光多少。 */
export function planAudit(plan: BudgetPlan, gtById: Map<string, GroundTruth>): PlanAudit {
  let fraudSpend = 0.0;
  let highRiskSpend = 0.0;
  let wastedSpend = 0.0;
  let reviewSpend = 0.0;
  let rejectSpend = 0.0;
  let effViews = 0.0;
  let effViewsLenient = 0.0;
  let nominalViews = 0.0;
  let nFraud = 0;
  let nHighRisk = 0;
  const fraudExamples: PlanAudit['fraud_examples'] = [];

  for (const a of plan.selected) {
    const gt = gtById.get(a.kox_id);
    const isFraud = Boolean(gt?.is_fraud);
    const isHigh = String(gt?.brand_safety) === 'high';
    nominalViews += a.est_views;
    if (isFraud) {
      fraudSpend += a.amount_usd;
      nFraud += 1;
      effViewsLenient += a.est_views * FRAUD_RESIDUAL_VIEW_SHARE;
      if (fraudExamples.length < 5) {
        fraudExamples.push({
          kox_id: a.kox_id,
          handle: a.handle,
          amount_usd: pyRound(a.amount_usd, 2),
          fraud_type: gt?.fraud_type ?? null,
          followers_bucket: a.bucket,
        });
      }
    } else {
      effViews += a.est_views;
      effViewsLenient += a.est_views;
    }
    if (isHigh) {
      highRiskSpend += a.amount_usd;
      nHighRisk += 1;
    }
    if (isFraud || isHigh) wastedSpend += a.amount_usd;
    if (a.verdict === 'review') reviewSpend += a.amount_usd;
    else if (a.verdict === 'reject') rejectSpend += a.amount_usd;
  }

  const spent = plan.spent_usd;
  return {
    strategy: plan.strategy,
    n_selected: plan.selected.length,
    spent_usd: pyRound(spent, 2),
    utilization: plan.budget_usd ? pyRound(spent / plan.budget_usd, 4) : 0.0,
    n_fraud_selected: nFraud,
    n_high_risk_selected: nHighRisk,
    fraud_spend_usd: pyRound(fraudSpend, 2),
    fraud_spend_share: spent ? pyRound(fraudSpend / spent, 4) : 0.0,
    high_risk_spend_usd: pyRound(highRiskSpend, 2),
    high_risk_spend_share: spent ? pyRound(highRiskSpend / spent, 4) : 0.0,
    wasted_spend_usd: pyRound(wastedSpend, 2),
    wasted_spend_share: spent ? pyRound(wastedSpend / spent, 4) : 0.0,
    spend_on_gate_review_usd: pyRound(reviewSpend, 2),
    spend_on_gate_reject_usd: pyRound(rejectSpend, 2),
    nominal_views: pyRound(nominalViews, 0),
    effective_views_gt: pyRound(effViews, 0),
    effective_views_gt_lenient: pyRound(effViewsLenient, 0),
    effective_cpm_usd: effViews > 0 ? pyRound(spent / (effViews / 1000.0), 3) : null,
    effective_views_per_1k_usd: spent ? pyRound(effViews / (spent / 1000.0), 0) : 0.0,
    fraud_examples: fraudExamples,
  };
}

export interface CounterfactualRow {
  campaign_id: string;
  budget_usd: number;
  baseline: PlanAudit;
  koxpilot: PlanAudit;
  saved_usd: number;
  saved_share_of_budget: number;
  effective_view_uplift: number;
  effective_view_uplift_lenient: number;
  effective_views_per_1k_usd: { baseline: number; koxpilot: number; uplift: number };
}

/** SPEC 6.2：单 campaign 的两臂对比（前端顶部"避免的浪费金额"就是这里的 saved_usd）。 */
export function counterfactualRow(
  campaignId: string,
  plan: BudgetPlan,
  baseline: BudgetPlan,
  gtById: Map<string, GroundTruth>,
): CounterfactualRow {
  const koxAudit = planAudit(plan, gtById);
  const baseAudit = planAudit(baseline, gtById);
  const saved = baseAudit.wasted_spend_usd - koxAudit.wasted_spend_usd;
  const baseEff = baseAudit.effective_views_gt;
  const koxEff = koxAudit.effective_views_gt;
  const baseEffL = baseAudit.effective_views_gt_lenient;
  const koxEffL = koxAudit.effective_views_gt_lenient;
  const per1kBase = baseAudit.effective_views_per_1k_usd;
  const per1kKox = koxAudit.effective_views_per_1k_usd;
  return {
    campaign_id: campaignId,
    budget_usd: pyRound(plan.budget_usd, 2),
    baseline: baseAudit,
    koxpilot: koxAudit,
    saved_usd: pyRound(saved, 2),
    saved_share_of_budget: plan.budget_usd ? pyRound(saved / plan.budget_usd, 4) : 0.0,
    effective_view_uplift: baseEff > 0 ? pyRound((koxEff - baseEff) / baseEff, 4) : 0.0,
    effective_view_uplift_lenient: baseEffL > 0 ? pyRound((koxEffL - baseEffL) / baseEffL, 4) : 0.0,
    effective_views_per_1k_usd: {
      baseline: per1kBase,
      koxpilot: per1kKox,
      uplift: per1kBase > 0 ? pyRound((per1kKox - per1kBase) / per1kBase, 4) : 0.0,
    },
  };
}
