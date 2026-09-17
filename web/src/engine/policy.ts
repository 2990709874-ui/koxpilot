/**
 * 门禁 policy 常量（对应 Python `gates/policy.py`）。
 *
 * **数据无关纪律**：本文件不是"第二份阈值来源"。
 * 运行时的权威值一律来自 `thresholds.json` 的 `policy` 块（Python `policy_snapshot()` 落盘），
 * 下面的 `DEFAULT_POLICY` 只是类型骨架 + 离线单测兜底，
 * 一旦数据重新标定，前端不需要改任何一行代码。
 *
 * 少数常量 Python 侧写在 g1/g2 模块内、没有进 snapshot（如 SPIKE_GROWTH_FLOOR_Q、
 * 规则版 fit 的三档分值），这里如实标注来源，并在 Engineering Notes Tab 里列出。
 */

export interface GatePolicy {
  critical_fields: string[];
  completeness_review_max: number;
  low_confidence_discount: number;
  spike_zscore_min: number;
  viral_support_lag_months: number;
  min_history_months: number;
  spec_reference_comment_dup_abs: number;
  spec_reference_comment_emoji_abs: number;
  g1_weights: Record<string, number>;
  graded_penalty_gain: number;
  g1_hard_rules: string[];
  authenticity_floor_reject: number;
  hard_hits_reject_min: number;
  fraud_score_ramp_upper: number;
  fraud_score_ramp_lower: number;
  fraud_score_z_ramp: [number, number];
  declared_observed_jaccard_min: number;
  source_jaccard_min: number;
  fit_score_review_max: number;
  audience_geo_min: number;
  audience_match_min: number;
  audience_age_weight: number;
  audience_gender_weight: number;
  g2_weights: Record<string, number>;
  severity_points: Record<string, number>;
  severity_hits_cap: number;
  competitor_recent_months: number;
  competitor_hard_months: number;
  regulated_severity_multiplier: number;
  g3_weights: Record<string, number>;
  min_group_samples: number;
  grid_step_pct: number;
}

/** SPEC 第 4 节的语义门槛骨架。运行时会被 thresholds.json 的 policy 块整体覆盖。 */
export const DEFAULT_POLICY: GatePolicy = {
  critical_fields: ['followers', 'avg_views', 'engagement_rate', 'audience_geo', 'quoted_price_usd'],
  completeness_review_max: 0.8,
  low_confidence_discount: 0.85,
  spike_zscore_min: 2.5,
  viral_support_lag_months: 1,
  min_history_months: 6,
  spec_reference_comment_dup_abs: 0.35,
  spec_reference_comment_emoji_abs: 0.4,
  g1_weights: { 'G1.1': 0.3, 'G1.2': 0.28, 'G1.3': 0.12, 'G1.4': 0.26, 'G1.5': 0.3, 'G1.6': 0.14, 'G1.7': 0.26 },
  graded_penalty_gain: 1.2,
  g1_hard_rules: ['G1.1', 'G1.2', 'G1.4', 'G1.5', 'G1.7'],
  authenticity_floor_reject: 0.5,
  hard_hits_reject_min: 2,
  fraud_score_ramp_upper: 0.8,
  fraud_score_ramp_lower: 0.2,
  fraud_score_z_ramp: [1.0, 3.5],
  declared_observed_jaccard_min: 0.34,
  source_jaccard_min: 0.5,
  fit_score_review_max: 0.5,
  audience_geo_min: 0.35,
  audience_match_min: 0.4,
  audience_age_weight: 0.6,
  audience_gender_weight: 0.4,
  g2_weights: { 'G2.1': 0.3, 'G2.2': 0.2, 'G2.3': 0.25, 'G2.4': 0.2, 'G2.5': 0.25, 'G2.6': 0.2 },
  severity_points: { low: 1.0, medium: 2.5, high: 10.0 },
  severity_hits_cap: 3,
  competitor_recent_months: 6,
  competitor_hard_months: 3,
  regulated_severity_multiplier: 0.5,
  g3_weights: { 'G3.1': 1.0, 'G3.2': 0.3, 'G3.3': 0.35, 'G3.4': 0.3, 'G3.5': 0.45 },
  min_group_samples: 60,
  grid_step_pct: 2,
};

/** 用 thresholds.json 的 policy 块覆盖骨架（缺字段则保留骨架值，并可被 UI 提示）。 */
export function policyFromSnapshot(snapshot: Partial<GatePolicy> | undefined | null): GatePolicy {
  if (!snapshot) return { ...DEFAULT_POLICY };
  return { ...DEFAULT_POLICY, ...snapshot } as GatePolicy;
}

/** 计算 policy 快照与 TS 骨架的差异，用于 Engineering Notes 页如实展示"是否有字段没对齐"。 */
export function policyDrift(snapshot: Partial<GatePolicy> | undefined | null): string[] {
  if (!snapshot) return ['thresholds.json 缺少 policy 块，TS 侧退化为 SPEC 默认骨架'];
  const out: string[] = [];
  for (const key of Object.keys(DEFAULT_POLICY) as Array<keyof GatePolicy>) {
    if (!(key in snapshot)) {
      out.push(`policy.${key} 未在 thresholds.json 中出现，使用 SPEC 默认值`);
      continue;
    }
    const a = JSON.stringify((DEFAULT_POLICY as unknown as Record<string, unknown>)[key]);
    const b = JSON.stringify((snapshot as unknown as Record<string, unknown>)[key]);
    if (a !== b) out.push(`policy.${key}：产物=${b}，TS 骨架默认=${a}（已按产物为准）`);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Python 侧写在模块内、未进 policy_snapshot 的常量（如实标注来源）
// ---------------------------------------------------------------------------

/** G1.5 第二必要条件用的分位标签（Python `policy.SPIKE_GROWTH_FLOOR_Q`）。 */
export const SPIKE_GROWTH_FLOOR_Q = 'p90';

/** 规则版语义适配分的三档 + 关键词兜底（Python `g2.py` 模块常量）。 */
export const FIT_EXACT = 1.0;
export const FIT_ADJACENT = 0.65;
export const FIT_MISS = 0.2;
export const FIT_KEYWORD = 0.7;
export const FIT_DECLARED_DISCOUNT = 0.9;

/** 参与判定的四层（Python `policy.ALL_GATES`）。 */
export const ALL_GATES = ['G0', 'G1', 'G2', 'G3'] as const;

/** G1 里"阈值来自分位数"的信号白名单（决定 reason.source 前缀，Python g1.hit 内联判断）。 */
export const QUANTILE_SIGNALS = new Set([
  'engagement_rate',
  'comment_like_ratio',
  'view_follower_ratio',
  'followers_per_day',
  'comment_dup_rate',
  'comment_emoji_only_rate',
]);
