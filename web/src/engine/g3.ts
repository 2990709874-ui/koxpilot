/**
 * G3 品牌安全门禁 5 条（对应 Python `gates/g3.py`，SPEC 4.G3）。
 *
 * 阈值来源分两类：
 * - G3.2 的"累计 severity 分"门槛来自达人库 `risk_severity_score` 的**同组 P90**
 *   （让门槛自己长在数据里，而不是我拍一个 "> 2.5 分"）；
 * - 其余是 SPEC 明写的语义门槛（high severity 硬阻断、竞品 6 个月），常量在 policy 快照里带出处。
 */

import type { GatePolicy } from './policy.ts';
import { gFormat, num } from './humanize.ts';
import { pyRound } from './stats.ts';
import { REGULATED_CATEGORY_FLAGS, competitorsOf } from './taxonomy.ts';
import { koxGroupKey, numOrNull, riskSeverityScore } from './signals.ts';
import type { Thresholds } from './thresholds.ts';
import type { CampaignSpec, Kox, Reason } from './types.ts';

const FLAG_ZH: Record<string, string> = {
  political_content: '政治敏感内容',
  adult_content: '成人内容',
  gambling_promo: '赌博推广',
  medical_claim_strong: '强医疗功效宣称',
  medical_claim: '医疗功效宣称',
  profanity: '粗俗用语',
  alcohol_mention: '酒类提及',
  shock_humor: '猎奇/冒犯性幽默',
  unverified_claim: '未经证实的宣称',
};
const CONTROVERSY_ZH: Record<string, string> = {
  public_dispute: '公开争执',
  refund_scandal: '退款纠纷',
  plagiarism_claim: '抄袭指控',
  sponsorship_undisclosed: '未披露广告合作',
};
const REGULATED_ZH: Record<string, string> = { medical: '医疗健康', finance: '金融', kids: '儿童向' };

export { FLAG_ZH, CONTROVERSY_ZH, REGULATED_ZH };

export interface G3Outcome {
  reasons: Reason[];
  penalty: number;
  blocked_by: string | null;
  severity_score: number;
  details: Record<string, unknown>;
}

export function highSeverityFlags(kox: Partial<Kox>): Array<Record<string, unknown>> {
  return (kox.content_flags ?? []).filter(
    (f) => f && typeof f === 'object' && String(f.severity) === 'high',
  ) as unknown as Array<Record<string, unknown>>;
}

/** 纯函数。G3.3/G3.5 依赖 campaign（竞品清单、受管制品类），为空时跳过。 */
export function evaluateG3(
  kox: Partial<Kox>,
  spec: CampaignSpec,
  thresholds: Thresholds,
): G3Outcome {
  const policy: GatePolicy = thresholds.policy;
  const reasons: Reason[] = [];
  const details: Record<string, unknown> = {};
  let penalty = 0.0;
  let blockedBy: string | null = null;
  const gkey = koxGroupKey(kox);

  const hit = (
    ruleId: string,
    signal: string,
    actual: number | string | null,
    threshold: number | string | null,
    text: string,
    source: string,
    severity: 'soft' | 'block' = 'soft',
  ): void => {
    const weight = policy.g3_weights[ruleId];
    reasons.push({
      gate: 'G3',
      rule_id: ruleId,
      signal,
      actual,
      threshold,
      weight,
      severity,
      source,
      depth: 0.0,
      human_text: text,
    });
    penalty += weight;
    if (severity === 'block' && blockedBy === null) blockedBy = ruleId;
  };

  // ---- G3.1 高危内容 → 硬阻断 -------------------------------------------
  const highs = highSeverityFlags(kox);
  details.high_flags = highs.map((f) => String(f.type));
  if (highs.length > 0) {
    const names = highs.map((f) => FLAG_ZH[String(f.type)] ?? String(f.type)).join('、');
    let hits = 0;
    for (const f of highs) hits += Math.trunc(Number(f.hits ?? 0) || 1);
    hit(
      'G3.1',
      'content_flags.severity',
      `high×${highs.length}`,
      'high',
      `内容审核命中高危标记：${names}（累计 ${hits} 次）。` +
        `出海投放里这类内容一旦上线就是品牌事故，按品牌安全策略**硬阻断**，不进入预算分配。`,
      'policy:spec_4.G3.1',
      'block',
    );
  }

  // ---- G3.2 中低危累计载荷 ----------------------------------------------
  const score = riskSeverityScore(kox, policy);
  details.severity_score = pyRound(score, 3);
  const baseThr = thresholds.value(gkey, 'risk_severity_score', 'p90');
  let thr = baseThr;
  const regulated = spec.regulated_category;
  if (thr !== null && regulated) thr = thr * policy.regulated_severity_multiplier;
  details.severity_threshold = thr === null ? null : pyRound(thr, 3);
  if (thr !== null && score > thr) {
    const flags = (kox.content_flags ?? [])
      .filter((f) => f && typeof f === 'object' && String(f.severity) !== 'high')
      .map((f) => `${FLAG_ZH[String(f.type)] ?? String(f.type)}（${f.severity}×${f.hits}）`);
    const extra = regulated
      ? `，且本次是${REGULATED_ZH[regulated] ?? regulated}品类、门槛按 ` +
        `${gFormat(policy.regulated_severity_multiplier)} 倍收紧`
      : '';
    hit(
      'G3.2',
      'risk_severity_score',
      pyRound(score, 3),
      pyRound(thr, 3),
      `中低危内容累计风险载荷 ${num(score, 1)} 分，高于同组 P90 门槛 ${num(thr, 1)} 分` +
        `${extra}。命中项：${flags.join('、') || '争议记录'}。建议人工过一遍内容再决定。`,
      `quantile:${thresholds.source(gkey, 'risk_severity_score')}`,
    );
  }

  // ---- G3.3 竞品合作冲突 ------------------------------------------------
  if (spec.competitor_brands.length > 0) {
    const competitorSet = new Set<string>(spec.competitor_brands);
    for (const brand of spec.competitor_brands) {
      for (const c of competitorsOf(brand)) competitorSet.add(c);
    }
    const recent: Array<{ brand: string; months_ago: number }> = [];
    for (const collab of kox.past_collabs ?? []) {
      if (!collab || typeof collab !== 'object') continue;
      const brand = String(collab.brand);
      const months = numOrNull(collab.months_ago);
      if (competitorSet.has(brand) && months !== null && months < policy.competitor_recent_months) {
        recent.push({ brand, months_ago: Math.trunc(months) });
      }
    }
    details.competitor_collabs = recent;
    if (recent.length > 0) {
      const nearest = recent.reduce((a, b) => (b.months_ago < a.months_ago ? b : a));
      const hard = nearest.months_ago < policy.competitor_hard_months;
      hit(
        'G3.3',
        'past_collabs',
        `${nearest.brand}@${nearest.months_ago}mo`,
        `<${policy.competitor_recent_months}mo`,
        `${nearest.months_ago} 个月前刚合作过竞品 ${nearest.brand}` +
          (hard
            ? `，落在通常 ${policy.competitor_hard_months} 个月的排他期内，` +
              `直接排除（既有法务风险也会被观众吐槽）。`
            : `，虽超出 ${policy.competitor_hard_months} 个月排他期但仍在 ` +
              `${policy.competitor_recent_months} 个月内，需客户确认是否接受。`),
        'policy:spec_4.G3.3',
        hard ? 'block' : 'soft',
      );
    }
  }

  // ---- G3.4 争议历史 ----------------------------------------------------
  const controversy = kox.controversy;
  if (controversy && typeof controversy === 'object' && Object.keys(controversy).length > 0) {
    const ctype = CONTROVERSY_ZH[String(controversy.type)] ?? String(controversy.type);
    hit(
      'G3.4',
      'controversy',
      String(controversy.type),
      'null',
      `存在争议历史：${ctype}（${controversy.months_ago} 个月前，` +
        `严重度 ${controversy.severity ?? 'low'}）。不必然否决，但要人工判断舆情是否已冷却。`,
      'policy:spec_4.G3.4',
    );
    details.controversy = String(controversy.type);
  }

  // ---- G3.5 受管制品类叠加更严阈值 ---------------------------------------
  if (regulated) {
    const sensitive = new Set(REGULATED_CATEGORY_FLAGS[regulated] ?? []);
    const matched = (kox.content_flags ?? [])
      .filter((f) => f && typeof f === 'object' && sensitive.has(String(f.type)))
      .map((f) => String(f.type));
    details.regulated_matched_flags = matched;
    if (matched.length > 0) {
      const names = matched.map((m) => FLAG_ZH[m] ?? m).join('、');
      hit(
        'G3.5',
        'content_flags.type',
        matched.join('/'),
        `regulated:${regulated}`,
        `本次是${REGULATED_ZH[regulated] ?? regulated}品类（合规要求更高），` +
          `而该达人内容含 ${names}，按更严阈值需人工合规审核后才能投。`,
        'policy:spec_4.G3.5',
      );
    }
  }

  return { reasons, penalty, blocked_by: blockedBy, severity_score: score, details };
}
