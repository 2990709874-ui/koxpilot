/**
 * 候选池构建与价值/成本模型（对应 Python `budget/value.py`，SPEC 第 5 节）。
 *
 *   value(k) = avg_views × authenticity_discount × fit_score × audience_match × kpi_weight
 *   cost(k)  = quoted_price_usd，缺失则按同组 avg_cpm 中位数估算并标注 price_estimated
 *
 * 三条纪律（与 Python 逐条对齐）：
 * 1. 只读可观测字段，候选构建同样不碰 `gt`；
 * 2. 缺失不当 0：报价缺失走估算并打标，avg_views 缺失直接出池并记原因
 *    （0 成本会让缺价号排最前，是这类系统的经典事故）；
 * 3. 定向筛选与选人依据分离：两臂共用同一份 targeting，唯一差异是选人依据。
 */

import { audienceMatchScore, ruleFitScore } from '../engine/g2.ts';
import { CATEGORY_ADJACENCY, followerBucket, groupKey } from '../engine/taxonomy.ts';
import { pyRound } from '../engine/stats.ts';
import type { Thresholds } from '../engine/thresholds.ts';
import type { CampaignSpec, GateResult, Kox } from '../engine/types.ts';
import { KPI_EXPONENTS, PRICE_ESTIMATE_QUANTILE, RATIO_CLAMP } from './policy.ts';

export interface Candidate {
  kox_id: string;
  handle: string;
  platform: string;
  country: string;
  bucket: string;
  group: string;
  verdict: string;
  followers: number;
  avg_views: number;
  engagement_rate: number;
  cost_usd: number;
  price_estimated: boolean;
  authenticity_discount: number;
  fit_score: number;
  audience_match: number;
  kpi_weight: number;
  value: number;
  efficiency: number;
}

export interface SkippedRecord {
  kox_id: string;
  reason: string;
}

export function estEngagements(c: Candidate): number {
  return c.avg_views * c.engagement_rate;
}

function numOrNull(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === 'boolean') return null;
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return value;
}

/**
 * campaign 定向筛选：只判断"这个号在不在本次投放射程内"，**不做质量判断**（那是门禁的活）。
 * 三个维度都遵循"spec 没给就不约束"。返回 null 表示通过。
 */
export function targetingReason(kox: Partial<Kox>, spec: CampaignSpec): string | null {
  if (spec.platforms.length > 0 && !spec.platforms.includes(String(kox.platform))) {
    return 'platform_off_target';
  }
  if (spec.target_categories.length > 0) {
    const cats = new Set<string>();
    for (const c of kox.declared_categories ?? []) cats.add(String(c));
    for (const c of kox.observed_categories ?? []) cats.add(String(c));
    const allowed = new Set<string>();
    for (const target of spec.target_categories) {
      allowed.add(target);
      for (const adj of CATEGORY_ADJACENCY[target] ?? []) allowed.add(adj);
    }
    let hit = false;
    for (const c of cats) if (allowed.has(c)) hit = true;
    if (!hit) return 'category_off_target';
  }
  if (spec.target_markets.length > 0) {
    const geo = kox.audience_geo;
    const local = spec.target_markets.includes(String(kox.country));
    let share: number | null = null;
    if (geo && typeof geo === 'object' && Object.keys(geo).length > 0) {
      share = 0.0;
      for (const m of spec.target_markets) share += Number(geo[m] ?? 0.0) || 0.0;
    }
    if (!local && share !== null && share <= 0.0) return 'market_off_target';
  }
  return null;
}

/** 同组归一比值，落在 RATIO_CLAMP 内。任一侧缺失/非正 → 1.0（中性，不奖不罚）。 */
function clampedRatio(actual: number | null, reference: number | null): number {
  const [lo, hi] = RATIO_CLAMP;
  if (actual === null || reference === null || reference <= 0 || actual <= 0) return 1.0;
  return Math.min(hi, Math.max(lo, actual / reference));
}

/** KPI 权重：同组归一后的互动率与评论占比的幂次组合。 */
export function kpiWeightOf(
  kox: Partial<Kox>,
  spec: CampaignSpec,
  thresholds: Thresholds,
  group: string,
): number {
  const [a, b] = KPI_EXPONENTS[spec.kpi] ?? KPI_EXPONENTS.balanced;
  if (a === 0.0 && b === 0.0) return 1.0;
  const erRatio = clampedRatio(
    numOrNull(kox.engagement_rate),
    thresholds.value(group, 'engagement_rate', 'median'),
  );
  const clrRatio = clampedRatio(
    numOrNull(kox.comment_like_ratio),
    thresholds.value(group, 'comment_like_ratio', 'median'),
  );
  return pyRound(erRatio ** a * clrRatio ** b, 6);
}

/** 返回 `[cost_usd, price_estimated]`；无法定价时返回 `[null, true]`。 */
export function estimateCost(
  kox: Partial<Kox>,
  thresholds: Thresholds,
  group: string,
): [number | null, boolean] {
  const quoted = numOrNull(kox.quoted_price_usd);
  if (quoted !== null && quoted > 0) return [quoted, false];
  const views = numOrNull(kox.avg_views);
  const cpm = thresholds.value(group, 'avg_cpm_usd', PRICE_ESTIMATE_QUANTILE);
  if (views === null || views <= 0 || cpm === null || cpm <= 0) return [null, true];
  return [(cpm * views) / 1000.0, true];
}

/**
 * 构建候选池。`results` 必须是**用同一个 campaign spec 跑出来的**门禁结果，
 * 否则 fit/geo 判定与这里的 value 口径会打架。
 */
export function buildCandidates(
  records: readonly Partial<Kox>[],
  results: Map<string, GateResult> | Record<string, GateResult>,
  spec: CampaignSpec,
  thresholds: Thresholds,
  includeVerdicts: readonly string[] = ['pass'],
): { candidates: Candidate[]; skipped: SkippedRecord[] } {
  const get = (id: string): GateResult | undefined =>
    results instanceof Map ? results.get(id) : results[id];
  const candidates: Candidate[] = [];
  const skipped: SkippedRecord[] = [];

  for (const kox of records) {
    const koxId = String(kox.kox_id);
    const reason = targetingReason(kox, spec);
    if (reason !== null) {
      skipped.push({ kox_id: koxId, reason });
      continue;
    }
    const result = get(koxId);
    if (result === undefined) {
      skipped.push({ kox_id: koxId, reason: 'no_gate_result' });
      continue;
    }
    if (!includeVerdicts.includes(result.verdict)) {
      skipped.push({ kox_id: koxId, reason: `gate_${result.verdict}` });
      continue;
    }
    const views = numOrNull(kox.avg_views);
    if (views === null || views <= 0) {
      skipped.push({ kox_id: koxId, reason: 'avg_views_missing' });
      continue;
    }
    const followers = numOrNull(kox.followers) ?? 0.0;
    const bucket = followerBucket(followers > 0 ? followers : null);
    const group = groupKey(String(kox.platform ?? 'unknown'), bucket);

    const [cost, estimated] = estimateCost(kox, thresholds, group);
    if (cost === null || cost <= 0) {
      skipped.push({ kox_id: koxId, reason: 'price_unavailable' });
      continue;
    }

    const fit = result.fit_source !== 'disabled' ? result.fit_score : ruleFitScore(kox, spec);
    const audience = audienceMatchScore(kox, spec, thresholds.policy);
    const weight = kpiWeightOf(kox, spec, thresholds, group);
    const discount = result.authenticity_score;
    const value = views * discount * fit * audience * weight;
    candidates.push({
      kox_id: koxId,
      handle: String(kox.handle ?? ''),
      platform: String(kox.platform ?? 'unknown'),
      country: String(kox.country ?? '??'),
      bucket,
      group,
      verdict: result.verdict,
      followers,
      avg_views: views,
      engagement_rate: numOrNull(kox.engagement_rate) ?? 0.0,
      cost_usd: cost,
      price_estimated: estimated,
      authenticity_discount: discount,
      fit_score: fit,
      audience_match: audience,
      kpi_weight: weight,
      value,
      efficiency: value / cost,
    });
  }
  return { candidates, skipped };
}
