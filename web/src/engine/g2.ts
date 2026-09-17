/**
 * G2 一致性门禁：标签/口径/campaign 适配 6 条（对应 Python `gates/g2.py`，SPEC 4.G2）。
 *
 * 与 G1 的关键区别：**G2 是 campaign 条件的**。G2.1/G2.2 只看达人自身（库级恒定），
 * G2.3–G2.6 依赖 campaign spec；spec 相应字段为空时对应规则自动跳过。
 * 这不是偷懒，而是评测口径的必要条件：gt.verdict 是 campaign 无关的库级判定，
 * 拿"带 campaign 条件的判定"去比它，混淆矩阵会被口径差异污染。
 * （唯一例外是 G2.5：markets 为空时退化为"以达人自称所属国为目标市场"。）
 *
 * G2.3 是 LLM 注入点：`fitScore` 传入 number 用外部语义分，传 undefined/null 走规则版，
 * 两条路径可以逐条 diff。
 */

import {
  FIT_ADJACENT,
  FIT_DECLARED_DISCOUNT,
  FIT_EXACT,
  FIT_KEYWORD,
  FIT_MISS,
} from './policy.ts';
import type { GatePolicy } from './policy.ts';
import { cats, pct, ratio } from './humanize.ts';
import { jaccard, pyRound } from './stats.ts';
import { CATEGORY_ADJACENCY, CATEGORY_KEYWORDS, marketLanguages } from './taxonomy.ts';
import type { CampaignSpec, Kox, Reason } from './types.ts';

export interface G2Outcome {
  reasons: Reason[];
  penalty: number;
  fit_score: number;
  fit_source: string;
  audience_match: number;
  geo_share: number;
  details: Record<string, unknown>;
}

/**
 * 规则版语义适配分（无 LLM 兜底），刻意做成三档 + 关键词兜底，便于口头解释：
 * 观测品类命中目标 → 1.0；只命中相邻品类 → 0.65；仅自称品类命中 → ×0.9（自称不如观测可信）；
 * 都不沾但 handle 含目标品类关键词 → 0.70；完全不沾 → 0.20。
 */
export function ruleFitScore(kox: Partial<Kox>, spec: CampaignSpec): number {
  if (spec.target_categories.length === 0) return 1.0;
  const observed = (kox.observed_categories ?? []).map((c) => String(c));
  const declared = (kox.declared_categories ?? []).map((c) => String(c));
  let best = FIT_MISS;
  for (const target of spec.target_categories) {
    const adjacency = new Set(CATEGORY_ADJACENCY[target] ?? []);
    for (const cat of observed) {
      if (cat === target) best = Math.max(best, FIT_EXACT);
      else if (adjacency.has(cat)) best = Math.max(best, FIT_ADJACENT);
    }
    for (const cat of declared) {
      if (cat === target) best = Math.max(best, FIT_EXACT * FIT_DECLARED_DISCOUNT);
      else if (adjacency.has(cat)) best = Math.max(best, FIT_ADJACENT * FIT_DECLARED_DISCOUNT);
    }
  }
  const handle = String(kox.handle ?? '').toLowerCase();
  if (best <= FIT_MISS) {
    for (const target of spec.target_categories) {
      if ((CATEGORY_KEYWORDS[target] ?? []).some((kw) => handle.includes(kw))) {
        best = Math.max(best, FIT_KEYWORD);
      }
    }
  }
  return pyRound(best, 4);
}

/** 目标市场在受众地域里的合计占比；audience_geo 缺失返回 null（交给 G0）。 */
export function audienceGeoShare(kox: Partial<Kox>, markets: readonly string[]): number | null {
  const geo = kox.audience_geo;
  if (!geo || typeof geo !== 'object' || Object.keys(geo).length === 0) return null;
  if (markets.length === 0) return null;
  let total = 0.0;
  for (const m of markets) total += Number(geo[m] ?? 0.0) || 0.0;
  return total;
}

/**
 * 年龄+性别加权重叠度（SPEC 4.G2.6）。权重来自 policy（年龄 0.6/性别 0.4），
 * 只给一维时权重归一到该维；两维都没给（neutral 画像）返回 1.0——
 * "没有人群要求"不该被判成"人群不匹配"。
 */
export function audienceMatchScore(kox: Partial<Kox>, spec: CampaignSpec, policy: GatePolicy): number {
  const ageProfile = kox.audience_age;
  const genderProfile = kox.audience_gender;
  const parts: Array<[number, number]> = [];
  if (spec.target_age_buckets.length > 0 && ageProfile && typeof ageProfile === 'object') {
    let share = 0.0;
    for (const b of spec.target_age_buckets) share += Number(ageProfile[b] ?? 0.0) || 0.0;
    parts.push([policy.audience_age_weight, Math.min(1.0, share)]);
  }
  if (spec.target_gender && genderProfile && typeof genderProfile === 'object') {
    const share = Number(genderProfile[spec.target_gender] ?? 0.0) || 0.0;
    parts.push([policy.audience_gender_weight, Math.min(1.0, share)]);
  }
  if (parts.length === 0) return 1.0;
  let totalW = 0.0;
  for (const [w] of parts) totalW += w;
  let acc = 0.0;
  for (const [w, v] of parts) acc += w * v;
  return pyRound(acc / totalW, 4);
}

/** 三源标签两两 Jaccard 的平均值（SPEC 4.G2.2）；少于 2 源返回 null。 */
export function sourceAgreement(kox: Partial<Kox>): number | null {
  const tags = kox.source_tags;
  if (!tags || typeof tags !== 'object') return null;
  const sets = Object.values(tags).map((v) => (Array.isArray(v) ? v.map(String) : []));
  if (sets.length < 2) return null;
  const pairs: number[] = [];
  for (let i = 0; i < sets.length; i += 1) {
    for (let j = i + 1; j < sets.length; j += 1) pairs.push(jaccard(sets[i], sets[j]));
  }
  if (pairs.length === 0) return null;
  let acc = 0.0;
  for (const p of pairs) acc += p;
  return pyRound(acc / pairs.length, 4);
}

/** 纯函数。`fitScore` 为 LLM 注入点（null/undefined 时走规则版）。 */
export function evaluateG2(
  kox: Partial<Kox>,
  spec: CampaignSpec,
  policy: GatePolicy,
  fitScore: number | null = null,
): G2Outcome {
  const reasons: Reason[] = [];
  const details: Record<string, unknown> = {};
  let penalty = 0.0;

  const hit = (
    ruleId: string,
    signal: string,
    actual: number | string | null,
    threshold: number | string | null,
    text: string,
    source: string,
  ): void => {
    const weight = policy.g2_weights[ruleId];
    reasons.push({
      gate: 'G2',
      rule_id: ruleId,
      signal,
      actual,
      threshold,
      weight,
      severity: 'soft',
      source,
      depth: 0.0,
      human_text: text,
    });
    penalty += weight;
  };

  // ---- G2.1 自称 vs 观测品类 -------------------------------------------
  const declared = (kox.declared_categories ?? []).map((c) => String(c));
  const observed = (kox.observed_categories ?? []).map((c) => String(c));
  const jDo = jaccard(declared, observed);
  details.declared_observed_jaccard = pyRound(jDo, 4);
  if (jDo < policy.declared_observed_jaccard_min) {
    hit(
      'G2.1',
      'declared_vs_observed_categories',
      pyRound(jDo, 4),
      policy.declared_observed_jaccard_min,
      `自称品类 ${cats(declared)} 与近 30 条内容的观测品类 ${cats(observed)} ` +
        `重叠度 Jaccard=${ratio(jDo)} 低于 ${policy.declared_observed_jaccard_min}，` +
        `标签与实际内容错配，按此标签选人会选错人群。`,
      'policy:spec_4.G2.1',
    );
  }

  // ---- G2.2 多源标签冲突 ------------------------------------------------
  const agree = sourceAgreement(kox);
  details.source_agreement = agree;
  if (agree !== null && agree < policy.source_jaccard_min) {
    const st = (kox.source_tags ?? {}) as Record<string, string[]>;
    hit(
      'G2.2',
      'source_tags',
      agree,
      policy.source_jaccard_min,
      `三个数据源给的品类两两平均一致度仅 ${ratio(agree)}（低于 ${policy.source_jaccard_min}）：` +
        `平台=${cats(st.src_platform)}，` +
        `供应商A=${cats(st.src_vendor_a)}，` +
        `供应商B=${cats(st.src_vendor_b)}，` +
        `口径打架，需人工核定主品类。`,
      'policy:spec_4.G2.2',
    );
  }

  // ---- G2.3 与 campaign 目标品类的语义适配（LLM 注入点）------------------
  let fitValue = 1.0;
  let fitSource = 'skipped:no_target_categories';
  if (spec.target_categories.length > 0) {
    if (fitScore === null || fitScore === undefined) {
      fitValue = ruleFitScore(kox, spec);
      fitSource = 'rule:category_map+keyword';
    } else {
      fitValue = Math.min(1.0, Math.max(0.0, fitScore));
      fitSource = 'injected:llm_fit_score';
    }
    details.fit_score = fitValue;
    details.fit_source = fitSource;
    if (fitValue < policy.fit_score_review_max) {
      hit(
        'G2.3',
        'campaign_fit',
        pyRound(fitValue, 4),
        policy.fit_score_review_max,
        `与本次 campaign 目标品类 ${cats(spec.target_categories)} 的语义适配分仅 ` +
          `${ratio(fitValue)}（门槛 ${policy.fit_score_review_max}）：达人观测品类为 ${cats(observed)}，` +
          `既非目标品类也非相邻品类，内容调性对不上。` +
          `（打分来源：${fitSource.startsWith('injected') ? 'LLM 语义分' : '规则版品类映射表'}）`,
        `policy:spec_4.G2.3|${fitSource}`,
      );
    }
  }

  // ---- G2.4 语言不匹配 --------------------------------------------------
  const lang = String(kox.language ?? '');
  const allowed = new Set<string>(spec.target_languages);
  if (allowed.size === 0 && spec.target_markets.length > 0) {
    for (const market of spec.target_markets) {
      for (const l of marketLanguages(market)) allowed.add(l);
    }
  }
  if (allowed.size > 0 && lang) {
    const sortedAllowed = [...allowed].sort();
    details.language_allowed = sortedAllowed;
    if (!allowed.has(lang)) {
      hit(
        'G2.4',
        'language',
        lang,
        sortedAllowed.join('/'),
        `达人内容语言为 ${lang}，不在目标市场可接受语言集 ` +
          `{${sortedAllowed.join(', ')}} 内，本地化不匹配。`,
        'policy:spec_4.G2.4',
      );
    }
  }

  // ---- G2.5 受众地域重叠（markets 为空时退化为达人自称市场）--------------
  const rawMarkets =
    spec.target_markets.length > 0 ? spec.target_markets : [String(kox.country ?? '')];
  const markets = rawMarkets.filter((m) => m);
  const geoShare = audienceGeoShare(kox, markets);
  details.target_markets = markets;
  details.geo_share = geoShare;
  if (geoShare !== null && geoShare < policy.audience_geo_min) {
    const scope = spec.target_markets.length > 0 ? '本次目标市场' : '该达人自称所属市场';
    hit(
      'G2.5',
      'audience_geo',
      pyRound(geoShare, 4),
      policy.audience_geo_min,
      `${scope}（${markets.join('/')}）在其受众地域中只占 ${pct(geoShare)}，` +
        `低于 ${pct(policy.audience_geo_min)} 门槛，钱会花给不在目标市场的观众。`,
      'policy:spec_4.G2.5',
    );
  }

  // ---- G2.6 受众人群重叠 ------------------------------------------------
  const match = audienceMatchScore(kox, spec, policy);
  details.audience_match = match;
  if ((spec.target_age_buckets.length > 0 || spec.target_gender) && match < policy.audience_match_min) {
    const want: string[] = [];
    if (spec.target_age_buckets.length > 0) want.push(`${spec.target_age_buckets.join('/')} 岁`);
    if (spec.target_gender) want.push(spec.target_gender === 'f' ? '女性' : '男性');
    hit(
      'G2.6',
      'audience_age+gender',
      pyRound(match, 4),
      policy.audience_match_min,
      `目标人群（${want.join('、')}）与该达人受众画像的加权重叠度只有 ${ratio(match)}，` +
        `低于 ${policy.audience_match_min}（年龄权重 ${policy.audience_age_weight}/性别权重 ` +
        `${policy.audience_gender_weight}），人群偏差过大。`,
      'policy:spec_4.G2.6',
    );
  }

  return {
    reasons,
    penalty,
    fit_score: fitValue,
    fit_source: fitSource,
    audience_match: match,
    geo_share: geoShare === null ? 1.0 : geoShare,
    details,
  };
}
