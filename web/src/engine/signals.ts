/**
 * 可观测信号抽取（对应 Python `gates/signals.py`）。
 *
 * 这是"防自证"的第一道物理隔离：门禁不直接摸达人原始对象，而是先过这份白名单。
 * 想读 ground truth 也读不到，且标定与判定共用同一个抽取函数，
 * 不可能出现"标定用 A 口径、判定用 B 口径"的经典 bug。
 */

import type { GatePolicy } from './policy.ts';
import { safeDiv } from './stats.ts';
import { followerBucket, groupKey } from './taxonomy.ts';
import type { Kox } from './types.ts';

/** 参与分位数标定的信号名（与 thresholds.json 的信号 key 一致）。 */
export const OBSERVABLE_SIGNALS = [
  'engagement_rate',
  'comment_like_ratio',
  'view_follower_ratio',
  'followers_per_day',
  'comment_dup_rate',
  'comment_emoji_only_rate',
  'max_monthly_growth',
  'risk_severity_score',
  'avg_cpm_usd',
] as const;

export type SignalName = (typeof OBSERVABLE_SIGNALS)[number];

/** 安全取数：null / 非数值 / 布尔一律视为缺失（与 Python `_num` 同语义）。 */
export function numOrNull(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === 'boolean') return null;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  return null;
}

/** 分位数分组键 `platform|bucket`；followers 缺失时落入 nano 组（最保守）。 */
export function koxGroupKey(kox: Partial<Kox>): string {
  return groupKey(String(kox.platform ?? 'unknown'), followerBucket(numOrNull(kox.followers)));
}

/**
 * 累计风险载荷分（只统计 low/medium；high 走 G3.1 硬阻断，不参与累加）。
 * 单条 flag 的 hits 在 `severity_hits_cap` 处截断：命中 10 次和 3 次对决策含义差别不大，
 * 不截断会让分数被单条 flag 主导。
 */
export function riskSeverityScore(kox: Partial<Kox>, policy: GatePolicy): number {
  let total = 0.0;
  for (const flag of kox.content_flags ?? []) {
    if (!flag || typeof flag !== 'object') continue;
    const severity = String(flag.severity ?? 'low');
    if (severity === 'high') continue;
    const hits = numOrNull(flag.hits) ?? 1.0;
    total += (policy.severity_points[severity] ?? 1.0) * Math.min(hits, policy.severity_hits_cap);
  }
  const controversy = kox.controversy;
  if (controversy && typeof controversy === 'object') {
    total += policy.severity_points[String(controversy.severity ?? 'low')] ?? 1.0;
  }
  return total;
}

/** 粉丝历史 -> 月度环比增速序列（长度 = len(history) - 1）。 */
export function monthlyGrowthRates(history: readonly unknown[] | null | undefined): number[] {
  if (!history || history.length < 2) return [];
  const rates: number[] = [];
  for (let i = 0; i + 1 < history.length; i += 1) {
    const p = numOrNull(history[i]);
    const c = numOrNull(history[i + 1]);
    if (p === null || c === null || p <= 0) rates.push(0.0);
    else rates.push((c - p) / p);
  }
  return rates;
}

export type SignalBag = Record<SignalName, number | null>;

/**
 * 抽出全部可观测标量信号。缺失一律 null，绝不用 0 冒充
 * （否则 P05 阈值会被一堆假 0 拉到地板上）。
 */
export function extractSignals(kox: Partial<Kox>, policy: GatePolicy): SignalBag {
  const followers = numOrNull(kox.followers);
  const ageDays = numOrNull(kox.account_age_days);
  let fpd: number | null = null;
  if (followers !== null && ageDays !== null && ageDays > 0) fpd = safeDiv(followers, ageDays);
  const growth = monthlyGrowthRates(kox.follower_history);
  return {
    engagement_rate: numOrNull(kox.engagement_rate),
    comment_like_ratio: numOrNull(kox.comment_like_ratio),
    view_follower_ratio: numOrNull(kox.view_follower_ratio),
    followers_per_day: fpd,
    comment_dup_rate: numOrNull(kox.comment_dup_rate),
    comment_emoji_only_rate: numOrNull(kox.comment_emoji_only_rate),
    max_monthly_growth: growth.length > 0 ? Math.max(...growth) : null,
    risk_severity_score: riskSeverityScore(kox, policy),
    avg_cpm_usd: numOrNull(kox.avg_cpm_usd),
  };
}
