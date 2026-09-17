/**
 * A5 预算 Agent 的编排层（对应 Python `budget/planner.py`）：门禁 → 候选池 → 分配 → 基线对照。
 *
 * 三条臂（KOXPilot / 按粉丝量基线 / 第三臂 diversified_no_gate）共享同一批 GateResult
 * 与同一套定向筛选，唯一差异是**选人依据**（第三臂另加结构约束、排序换成名义曝光/报价）：
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

/**
 * 第三臂 diversified_no_gate：**只做结构分散化，不做质量门禁**（对应 Python
 * `budget/planner.plan_diversified_no_gate`）。
 *
 * 与基线臂完全同一个候选池（含 review/reject 的号），但强制执行 KOXPilot 的
 * 分层/地域/单人配额；排序依据换成"每美元买到的名义曝光"——只用 avg_views 与报价，
 * 不读门禁判定、不读真实性折扣、不读语义适配与 KPI 权重（见 allocator.BASIS_VIEWS）。
 * 如果它沿用 value(k)，"分散化贡献"里就会混进门禁的功劳，归因立刻失真。
 */
export function planDiversifiedNoGate(
  records: readonly Partial<Kox>[],
  spec: CampaignSpec,
  thresholds: Thresholds,
  results: Map<string, GateResult>,
  options: PlanOptions = {},
): BudgetPlan {
  const { candidates, skipped } = buildCandidates(records, results, spec, thresholds, ALL_VERDICTS);
  return allocate(candidates, spec, {
    strategy: 'diversified_no_gate',
    budgetUsd: options.budgetUsd ?? null,
    skipped,
    decay: options.decay,
    maxPosts: options.maxPosts,
  });
}
