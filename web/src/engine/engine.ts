/**
 * 门禁引擎：四层判定合成（对应 Python `gates/engine.py`，SPEC 第 4 节末尾）。
 *
 * 对外只有一个入口（纯函数、无 IO、无全局状态）：
 *   `evaluate(kox, spec, thresholds, options) -> GateResult`
 *
 * 三条架构纪律与 Python 侧完全一致：
 * 1. 四层各自返回 reason 列表，**分数由 engine 统一从 reason 权重合成**
 *    —— 消融实验（关层）与口径对照（屏蔽单条规则）都只需过滤 reason 列表，
 *    不会出现"关了层但分数没跟着变"的 bug；
 * 2. **防自证**：入口处物理剥掉 `gt` 字段，引擎内部拿到的对象里根本没有 ground truth；
 * 3. **纯函数**：TS 读同一份 thresholds.json 就能逐条复算出同样的 verdict，
 *    任何隐式状态（缓存/时间/随机）都会让双实现一致性永远对不齐。
 */

import { ALL_GATES } from './policy.ts';
import { evaluateG0 } from './g0.ts';
import { evaluateG1 } from './g1.ts';
import { evaluateG2 } from './g2.ts';
import { evaluateG3 } from './g3.ts';
import { koxGroupKey } from './signals.ts';
import { Thresholds } from './thresholds.ts';
import { NEUTRAL_SPEC, type CampaignSpec, type GateResult, type Kox, type Reason, type Verdict } from './types.ts';

/** ground truth 字段名。本模块唯一用到它的地方就是"把它排除掉"。 */
export const GROUND_TRUTH_FIELD = ['g', 't'].join('');

/** 返回剥掉 ground truth 的可观测视图（防自证的物理隔离层）。 */
export function observableView(kox: Partial<Kox>): Partial<Kox> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(kox)) {
    if (k === GROUND_TRUTH_FIELD) continue;
    out[k] = v;
  }
  return out as Partial<Kox>;
}

export interface SynthesizeInput {
  blockedBy: string | null;
  authenticity: number;
  nHardHits: number;
  completeness: number;
  nReasons: number;
  authenticityFloorReject: number;
  hardHitsRejectMin: number;
  completenessReviewMax: number;
}

/**
 * SPEC 第 4 节的判定合成：
 *   任一硬阻断 → reject；authenticity < floor → reject；硬信号 ≥2 条 → reject
 *   （后两条互为交叉验证）；任一命中或 completeness < 0.8 → review；否则 pass。
 */
export function synthesizeVerdict(input: SynthesizeInput): Verdict {
  if (input.blockedBy !== null) return 'reject';
  if (input.authenticity < input.authenticityFloorReject) return 'reject';
  if (input.nHardHits >= input.hardHitsRejectMin) return 'reject';
  if (input.nReasons > 0 || input.completeness < input.completenessReviewMax) return 'review';
  return 'pass';
}

export interface EvaluateOptions {
  /** G2.3 语义适配分的外部注入点（LLM 算好后喂进来）；null 走规则版。 */
  fitScore?: number | null;
  /** 参与判定的层，消融实验用。 */
  enabledGates?: readonly string[];
  /** 屏蔽的规则号（如 ['G2.2']），口径对照 / 单规则消融用。 */
  disabledRules?: readonly string[];
}

/** 对单个达人跑四层门禁。传入的 kox 含或不含 `gt` 都可以——引擎读不到它。 */
export function evaluate(
  kox: Partial<Kox>,
  spec: CampaignSpec = NEUTRAL_SPEC,
  thresholds: Thresholds = new Thresholds(),
  options: EvaluateOptions = {},
): GateResult {
  const policy = thresholds.policy;
  const gates = new Set(options.enabledGates ?? ALL_GATES);
  const blocked = new Set(options.disabledRules ?? []);
  const view = observableView(kox);

  const raw: Reason[] = [];
  let completeness = 1.0;
  let fraudScore = 0.0;
  let fitValue = 1.0;
  let fitSource = 'disabled';
  let signalRanks: Record<string, number> = {};
  let g2Details: Record<string, unknown> = {};
  let g3Details: Record<string, unknown> = {};
  let missing: string[] = [];

  if (gates.has('G0')) {
    const g0 = evaluateG0(view, policy);
    completeness = g0.completeness;
    missing = g0.missing;
    raw.push(...g0.reasons);
  }
  if (gates.has('G1')) {
    const g1 = evaluateG1(view, thresholds);
    raw.push(...g1.reasons);
    fraudScore = g1.fraud_score;
    signalRanks = g1.signal_ranks;
  }
  if (gates.has('G2')) {
    const g2 = evaluateG2(view, spec, policy, options.fitScore ?? null);
    raw.push(...g2.reasons);
    fitValue = g2.fit_score;
    fitSource = g2.fit_source;
    g2Details = g2.details;
  }
  if (gates.has('G3')) {
    const g3 = evaluateG3(view, spec, thresholds);
    raw.push(...g3.reasons);
    g3Details = g3.details;
  }

  const reasons = raw.filter((r) => !blocked.has(r.rule_id));

  // G0.1 命中 => 后续层置信度打折（SPEC 4.G0）
  const lowConfidence = reasons.some((r) => r.rule_id === 'G0.1');
  const discount = lowConfidence ? policy.low_confidence_discount : 1.0;

  const layerScore = (gate: string): number => {
    let penalty = 0.0;
    for (const r of reasons) {
      if (r.gate === gate && r.severity !== 'block') penalty += r.weight;
    }
    return Math.max(0.0, Math.min(1.0, 1.0 - penalty * discount));
  };

  const authenticity = gates.has('G1') ? layerScore('G1') : 1.0;
  const consistency = gates.has('G2') ? layerScore('G2') : 1.0;
  const brandSafety = gates.has('G3') ? layerScore('G3') : 1.0;
  const hardHits = reasons.filter((r) => r.gate === 'G1' && r.severity === 'hard').map((r) => r.rule_id);
  const blockedBy = reasons.find((r) => r.severity === 'block')?.rule_id ?? null;

  const verdict = synthesizeVerdict({
    blockedBy,
    authenticity,
    nHardHits: hardHits.length,
    completeness,
    nReasons: reasons.length,
    authenticityFloorReject: policy.authenticity_floor_reject,
    hardHitsRejectMin: policy.hard_hits_reject_min,
    completenessReviewMax: policy.completeness_review_max,
  });

  return {
    kox_id: String(view.kox_id ?? ''),
    verdict,
    reasons,
    completeness,
    authenticity_score: authenticity,
    consistency_score: consistency,
    brand_safety_score: brandSafety,
    fraud_score: fraudScore,
    fit_score: fitValue,
    fit_source: fitSource,
    hard_hits: hardHits,
    blocked_by: blockedBy,
    group_key: koxGroupKey(view),
    signal_ranks: signalRanks,
    missing_fields: missing,
    g2_details: g2Details,
    g3_details: g3Details,
  };
}

/** 批量评估；`fitScores` 按 kox_id 索引（LLM 离线固化结果的注入方式）。 */
export function evaluateAll(
  records: readonly Partial<Kox>[],
  spec: CampaignSpec = NEUTRAL_SPEC,
  thresholds: Thresholds = new Thresholds(),
  options: EvaluateOptions & { fitScores?: Record<string, number> } = {},
): GateResult[] {
  const { fitScores, ...rest } = options;
  return records.map((kox) =>
    evaluate(kox, spec, thresholds, {
      ...rest,
      fitScore: fitScores ? fitScores[String(kox.kox_id)] ?? null : rest.fitScore ?? null,
    }),
  );
}
