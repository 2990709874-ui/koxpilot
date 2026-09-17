/**
 * G0 完整性门禁（对应 Python `gates/g0.py`，SPEC 4.G0）。
 *
 * - **G0.1（SPEC 原文）**：completeness = 1 - 缺失关键字段数/关键字段数，< 0.8 → review，
 *   且后续层置信度打折。
 * - **G0.2（本实现新增，已在 Engineering Notes 声明）**：只缺 1 个关键字段时 completeness 恰为 0.8，
 *   按 SPEC 的严格不等式不会命中 G0.1；但 gt 合成规则里"关键字段缺失 → review"无论缺几个都成立，
 *   不补这条会出现一批"gt=review 而门禁 pass"的系统性漏检。
 *   G0.2 权重刻意低于 G0.1，只触发 review，不参与 reject。
 */

import type { GatePolicy } from './policy.ts';
import { pct } from './humanize.ts';
import { pyRound } from './stats.ts';
import type { Kox, Reason } from './types.ts';

const FIELD_ZH: Record<string, string> = {
  followers: '粉丝数',
  avg_views: '平均播放',
  engagement_rate: '互动率',
  audience_geo: '受众地域',
  quoted_price_usd: '报价',
};

export interface G0Outcome {
  completeness: number;
  missing: string[];
  reasons: Reason[];
  low_confidence: boolean;
}

function isEmptyValue(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === 'string' || Array.isArray(value)) return value.length === 0;
  if (typeof value === 'object') return Object.keys(value as object).length === 0;
  return false;
}

/** 关键字段完整度 + 缺失列表。判缺口径：不存在 / null / 空 dict|list|str 都算缺。 */
export function completenessOf(kox: Partial<Kox>, policy: GatePolicy): [number, string[]] {
  const missing: string[] = [];
  for (const field of policy.critical_fields) {
    if (isEmptyValue((kox as Record<string, unknown>)[field])) missing.push(field);
  }
  const completeness = 1.0 - missing.length / policy.critical_fields.length;
  return [completeness, missing];
}

/** 纯函数：只看字段存在性，不需要阈值表也不需要 campaign。 */
export function evaluateG0(kox: Partial<Kox>, policy: GatePolicy): G0Outcome {
  const [completeness, missing] = completenessOf(kox, policy);
  const reasons: Reason[] = [];
  const missingZh = missing.map((m) => FIELD_ZH[m] ?? m).join('、');

  if (completeness < policy.completeness_review_max) {
    reasons.push({
      gate: 'G0',
      rule_id: 'G0.1',
      signal: 'completeness',
      actual: pyRound(completeness, 4),
      threshold: policy.completeness_review_max,
      weight: 1.0,
      severity: 'soft',
      source: 'policy:spec_4.G0',
      depth: 0.0,
      human_text:
        `关键字段完整度仅 ${pct(completeness)}（缺 ${missingZh}），` +
        `低于门禁要求的 ${pct(policy.completeness_review_max)}，` +
        `该达人转人工复核，且真实性/一致性判定的置信度按 15% 折损计。`,
    });
  } else if (missing.length > 0) {
    reasons.push({
      gate: 'G0',
      rule_id: 'G0.2',
      signal: 'missing_critical_fields',
      actual: missing.length,
      threshold: 0,
      weight: 0.4,
      severity: 'soft',
      source: 'policy:derived_from_spec_3.3_missing_review',
      depth: 0.0,
      human_text:
        `缺 1 个关键字段（${missingZh}），完整度 ${pct(completeness)} 恰好卡在门槛上，` +
        `不足以判 reject，但报价/受众缺失会直接影响预算分配，先转人工补数。`,
    });
  }

  return {
    completeness,
    missing,
    reasons,
    low_confidence: completeness < policy.completeness_review_max,
  };
}
