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
