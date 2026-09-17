/**
 * KOXPilot 引擎侧数据契约（与 Python `src/koxpilot/types.py` 逐字段对齐）。
 *
 * 纪律：字段名一律与 Python 侧 `to_dict()` 落盘的 key 保持一致，
 * 否则双实现一致性校验会退化成"两套字段名互相翻译"，失去意义。
 */

export type Verdict = 'pass' | 'review' | 'reject';
export type Severity = 'hard' | 'soft' | 'block';

/** SPEC 3.2 的达人 compact schema（前端只消费精简版，字段子集见 scripts/prepare-data.mjs）。 */
export interface Kox {
  kox_id: string;
  handle: string;
  platform: string;
  country: string;
  language: string | null;
  verified?: boolean;
  account_age_days: number | null;
  followers: number | null;
  posts?: number | null;
  declared_categories: string[] | null;
  observed_categories: string[] | null;
  source_tags: Record<string, string[]> | null;
  avg_views: number | null;
  engagement_rate: number | null;
  view_follower_ratio: number | null;
  comment_like_ratio: number | null;
  follower_history: number[] | null;
  viral_months: number[] | null;
  audience_geo: Record<string, number> | null;
  audience_age: Record<string, number> | null;
  audience_gender: Record<string, number> | null;
  quoted_price_usd: number | null;
  avg_cpm_usd: number | null;
  past_collabs: Array<{ brand: string; category?: string; months_ago: number }> | null;
  content_flags: Array<{ type: string; severity: string; hits?: number }> | null;
  controversy: { type: string; severity?: string; months_ago?: number } | null;
  comment_dup_rate: number | null;
  comment_emoji_only_rate: number | null;
  _missing?: string[];
  /** ground truth：引擎入口会物理剥离，只有评测页可以读。 */
  gt?: {
    is_fraud: boolean;
    fraud_type: string | null;
    tag_mismatch: boolean;
    brand_safety: string;
    verdict: Verdict;
  };
}

/** A1 BriefAgent 的结构化输出（= briefs.json 的 spec 块）。 */
export interface CampaignSpec {
  campaign_id: string;
  name: string;
  target_categories: string[];
  target_markets: string[];
  target_languages: string[];
  target_age_buckets: string[];
  target_gender: string | null;
  budget_usd: number;
  kpi: string;
  platforms: string[];
  competitor_brands: string[];
  regulated_category: string | null;
  raw_text: string;
}

export const NEUTRAL_SPEC: CampaignSpec = {
  campaign_id: 'neutral',
  name: '库级中性画像（campaign 无关）',
  target_categories: [],
  target_markets: [],
  target_languages: [],
  target_age_buckets: [],
  target_gender: null,
  budget_usd: 0,
  kpi: 'balanced',
  platforms: [],
  competitor_brands: [],
  regulated_category: null,
  raw_text: '',
};

export function specFromDict(payload: Partial<CampaignSpec> & Record<string, unknown>): CampaignSpec {
  const tup = (k: keyof CampaignSpec): string[] => {
    const v = (payload as Record<string, unknown>)[k as string];
    return Array.isArray(v) ? v.map((x) => String(x)) : [];
  };
  return {
    campaign_id: String(payload.campaign_id ?? 'neutral'),
    name: String(payload.name ?? ''),
    target_categories: tup('target_categories'),
    target_markets: tup('target_markets'),
    target_languages: tup('target_languages'),
    target_age_buckets: tup('target_age_buckets'),
    target_gender: (payload.target_gender as string | null) ?? null,
    budget_usd: Number(payload.budget_usd ?? 0),
    kpi: String(payload.kpi ?? 'balanced'),
    platforms: tup('platforms'),
    competitor_brands: tup('competitor_brands'),
    regulated_category: (payload.regulated_category as string | null) ?? null,
    raw_text: String(payload.raw_text ?? ''),
  };
}

/** 一条门禁判定证据。前端证据链抽屉直接渲染这个结构。 */
export interface Reason {
  gate: string;
  rule_id: string;
  signal: string;
  actual: number | string | null;
  threshold: number | string | null;
  weight: number;
  human_text: string;
  severity: Severity;
  source: string;
  depth: number;
}

export interface GateResult {
  kox_id: string;
  verdict: Verdict;
  reasons: Reason[];
  completeness: number;
  authenticity_score: number;
  consistency_score: number;
  brand_safety_score: number;
  fraud_score: number;
  fit_score: number;
  fit_source: string;
  hard_hits: string[];
  blocked_by: string | null;
  group_key: string;
  /** G1 各信号在同组经验分布中的百分位（抽屉里的迷你分布图要用）。 */
  signal_ranks: Record<string, number>;
  /** G0 判定出的缺失关键字段（UI 用；Python 侧在 G0Outcome 里）。 */
  missing_fields?: string[];
  /** G2 中间量（jaccard / 一致度 / geo_share / audience_match），供证据链下钻。 */
  g2_details?: Record<string, unknown>;
  /** G3 中间量（高危标记 / 风险载荷与门槛 / 竞品命中），供证据链下钻。 */
  g3_details?: Record<string, unknown>;
}
