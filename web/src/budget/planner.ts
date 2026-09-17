/**
 * A5 预算 Agent 的编排层（对应 Python `budget/planner.py`）：门禁 → 候选池 → 分配 → 基线对照。
 *
 * 两臂共享同一批 GateResult 与同一套定向筛选，唯一差异是**选人依据**：
 * 基线臂拿到完全一样的候选池（含被门禁判 review/reject 的号），只按粉丝量降序花钱。
 * 这样反事实审计的差额能干净地归因到"质量门禁 + 价值排序"，而不是归因到定向口径不同。
 */

import { evaluateAll } from '../engine/engine.ts';
import type { Thresholds } from '../engine/thresholds.ts';
import type { CampaignSpec, GateResult, Kox } from '../engine/types.ts';
import { allocate, type BudgetPlan } from './allocator.ts';
import { INCLUDE_REVIEW_BY_DEFAULT } from './policy.ts';
import { buildCandidates } from './value.ts';

export const ALL_VERDICTS = ['pass', 'review', 'reject'] as const;

/** 按 campaign 口径跑一遍四层门禁，返回 kox_id -> GateResult。 */
export function gateResultsFor(
  records: readonly Partial<Kox>[],
  spec: CampaignSpec,
  thresholds: Thresholds,
  fitScores?: Record<string, number>,
): Map<string, GateResult> {
  const out = new Map<string, GateResult>();
  for (const r of evaluateAll(records, spec, thresholds, { fitScores })) out.set(r.kox_id, r);
  return out;
}

export interface PlanOptions {
  includeReview?: boolean;
  budgetUsd?: number | null;
  decay?: number;
  maxPosts?: number;
}

/** KOXPilot 决策臂。 */
export function planCampaign(
  records: readonly Partial<Kox>[],
  spec: CampaignSpec,
  thresholds: Thresholds,
  results: Map<string, GateResult>,
  options: PlanOptions = {},
): BudgetPlan {
  const includeReview = options.includeReview ?? INCLUDE_REVIEW_BY_DEFAULT;
  const verdicts = includeReview ? ['pass', 'review'] : ['pass'];
  const { candidates, skipped } = buildCandidates(records, results, spec, thresholds, verdicts);
  return allocate(candidates, spec, {
    strategy: 'koxpilot',
    budgetUsd: options.budgetUsd ?? null,
    skipped,
    decay: options.decay,
    maxPosts: options.maxPosts,
  });
}

/** 反事实基线臂：不看门禁，按粉丝量降序花完预算。 */
export function planBaseline(
  records: readonly Partial<Kox>[],
  spec: CampaignSpec,
  thresholds: Thresholds,
  results: Map<string, GateResult>,
  options: PlanOptions = {},
): BudgetPlan {
  const { candidates, skipped } = buildCandidates(records, results, spec, thresholds, ALL_VERDICTS);
  return allocate(candidates, spec, {
    strategy: 'followers',
    budgetUsd: options.budgetUsd ?? null,
    skipped,
    decay: options.decay,
    maxPosts: options.maxPosts,
  });
}
