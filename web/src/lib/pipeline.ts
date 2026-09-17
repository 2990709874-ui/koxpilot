/**
 * 浏览器内的 A1..A6 流水线与评测复算。
 *
 * 这个文件是"这不是录像"的技术证据：
 * - 召回、四层门禁、预算分配、反事实审计**都在浏览器里真跑**，耗时用 performance.now() 实测；
 * - 评测口径（严/宽水号、三分类混淆、AUC）与 Python `eval/metrics.py` 逐函数对齐，
 *   所以页面上的 TS 复算值可以直接和 metrics.json 摆在一起对比，对不上就是 bug 而不是"口径不同"；
 * - 消融/敏感性也在浏览器里现算（关层 / 缩放阈值后重新评估 5,000 条），不是读死表。
 */

import { ALL_GATES } from '../engine/policy.ts';
import { evaluate } from '../engine/engine.ts';
import { prf1, pyRound, rocAuc, safeDiv } from '../engine/stats.ts';
import type { Thresholds } from '../engine/thresholds.ts';
import { NEUTRAL_SPEC, type CampaignSpec, type GateResult, type Kox } from '../engine/types.ts';
import {
  counterfactualRow,
  gtIndex,
  planBaseline,
  planCampaign,
  targetingReason,
  type BudgetPlan,
  type CounterfactualRow,
  type GroundTruth,
} from '../budget/index.ts';

export const VERDICTS = ['pass', 'review', 'reject'] as const;
export type VerdictKey = (typeof VERDICTS)[number];

export interface StageReport {
  id: string;
  agent: string;
  title: string;
  /** 真实性标签：rule = 浏览器内纯规则真算；llm-offline = 构建期真调模型、结果固化；audit = 以 gt 为裁判的审计。 */
  kind: 'rule' | 'llm-offline' | 'audit';
  elapsedMs: number;
  items: number;
  headline: string;
  detail: string[];
  /** 该阶段对应的真实 token 消耗（来自 llm_bench.json 的 usage），规则阶段为 0。 */
  tokens: number | null;
  tokenNote?: string;
}

export interface FunnelStep {
  key: string;
  label: string;
  value: number;
  note: string;
}

export interface PipelineResult {
  spec: CampaignSpec;
  stages: StageReport[];
  totalMs: number;
  /** 经定向筛选留下的达人（后续所有统计都基于它，除了库级基线）。 */
  pool: Partial<Kox>[];
  results: Map<string, GateResult>;
  skipCounts: Record<string, number>;
  verdictCounts: Record<VerdictKey, number>;
  gateHits: Record<string, number>;
  ruleHits: Array<{ rule_id: string; gate: string; n: number; label: string }>;
  funnel: FunnelStep[];
  plan: BudgetPlan;
  baseline: BudgetPlan;
  audit: CounterfactualRow;
  gtById: Map<string, GroundTruth>;
  includeReview: boolean;
  decay: number;
}

export interface PipelineOptions {
  includeReview?: boolean;
  decay?: number;
  budgetUsd?: number | null;
  /** llm_bench.json 的 per_task 块，用来给 LLM 阶段标注真实 token（缺失则显示"未生成"）。 */
  llmPerTask?: Record<string, { total_tokens?: number; calls?: number; items?: number; model?: string }> | null;
  primaryModelKey?: string | null;
  /** 让 UI 能在每个阶段之间让出主线程做动画。 */
  onStage?: (stage: StageReport, index: number) => Promise<void> | void;
}

const now = (): number => performance.now();

function tokensOf(
  perTask: PipelineOptions['llmPerTask'],
  task: string,
  modelKey: string | null | undefined,
): { tokens: number | null; note: string } {
  if (!perTask) return { tokens: null, note: 'llm_bench.json 未生成：本阶段的 token 账留空，不估算' };
  const key = `${task}::${modelKey ?? 'ark'}`;
  const entry = perTask[key] ?? Object.entries(perTask).find(([k]) => k.startsWith(`${task}::`))?.[1];
  if (!entry) return { tokens: null, note: `llm_bench.json 中没有 ${task} 任务的 usage 记录` };
  return {
    tokens: entry.total_tokens ?? null,
    note: `构建期真实 usage：${entry.calls ?? '?'} 次调用 / ${entry.items ?? '?'} 条样本（${entry.model ?? '?'}）`,
  };
}

/** 逐层"无命中存活"人数，用于漏斗图。定义写在 note 里，避免被理解成"被该层拒绝". */
function survivalFunnel(pool: Partial<Kox>[], results: Map<string, GateResult>, libraryN: number): FunnelStep[] {
  let alive = pool.map((k) => results.get(String(k.kox_id))).filter((r): r is GateResult => Boolean(r));
  const steps: FunnelStep[] = [
    { key: 'library', label: '达人库', value: libraryN, note: '合成数据集全量（固定种子可复现）' },
    { key: 'recall', label: 'A2 定向召回', value: alive.length, note: '平台 / 品类（含相邻）/ 市场三条硬筛选，纯规则' },
  ];
  for (const gate of ALL_GATES) {
    alive = alive.filter((r) => !r.reasons.some((x) => x.gate === gate));
    steps.push({
      key: gate,
      label: `${gate} 无命中`,
      value: alive.length,
      note: `累计通过 ${ALL_GATES.slice(0, ALL_GATES.indexOf(gate) + 1).join('→')} 且一条规则都没触发的人数`,
    });
  }
  return steps;
}

const RULE_LABEL: Record<string, string> = {
  'G0.1': '关键字段缺失',
  'G0.2': '低置信度（缺失后仍判定）',
  'G1.1': '互动率异常高（同组上尾 P95）',
  'G1.2': '互动率异常低（同组下尾 P05）',
  'G1.3': '评论/点赞比越界 [P02, P98]',
  'G1.4': '播放/粉丝比异常高（P98）',
  'G1.5': '粉丝断层式突增',
  'G1.6': '日均涨粉超同组 P97（新号巨量粉）',
  'G1.7': '评论重复率 / 纯 emoji 率高',
  'G2.1': '自称品类 vs 实际内容错配',
  'G2.2': '多源标签互相冲突',
  'G2.3': '语义适配分过低',
  'G2.4': '语言与目标市场不符',
  'G2.5': '受众地域不在目标市场',
  'G2.6': '受众人群（年龄/性别）不匹配',
  'G3.1': '高危内容硬阻断',
  'G3.2': '风险内容载荷超限',
  'G3.3': '近期竞品合作冲突',
  'G3.4': '争议历史',
  'G3.5': '受管制品类需额外审',
};

export function ruleLabel(ruleId: string): string {
  return RULE_LABEL[ruleId] ?? ruleId;
}

/** 跑完整流水线。stages 里的耗时全部是真实测量值。 */
export async function runPipeline(
  records: Partial<Kox>[],
  spec: CampaignSpec,
  thresholds: Thresholds,
  options: PipelineOptions = {},
): Promise<PipelineResult> {
  const includeReview = options.includeReview ?? false;
  const decay = options.decay ?? 0.7;
  const stages: StageReport[] = [];
  const t0 = now();

  const emit = async (stage: StageReport): Promise<void> => {
    stages.push(stage);
    if (options.onStage) await options.onStage(stage, stages.length - 1);
  };

  // ---- A1 BriefAgent：自然语言 -> CampaignSpec（构建期真调 LLM，结果固化） ----
  const tA1 = now();
  const briefTokens = tokensOf(options.llmPerTask, 'brief', options.primaryModelKey);
  await emit({
    id: 'A1',
    agent: 'A1 BriefAgent',
    title: '自然语言 brief → 结构化 CampaignSpec',
    kind: 'llm-offline',
    elapsedMs: now() - tA1,
    items: 1,
    headline: `${spec.target_categories.length} 品类 / ${spec.target_markets.length} 市场 / KPI=${spec.kpi}`,
    detail: [
      `预算 $${spec.budget_usd.toLocaleString('en-US')}，平台 ${spec.platforms.join(' + ') || '不限'}`,
      `竞品回避 ${spec.competitor_brands.join('、') || '无'}；受管制口径 ${spec.regulated_category ?? '无'}`,
      'Demo 内解析结果为构建期固化产物：线上不调任何模型 endpoint（原因见工程边界）',
    ],
    tokens: briefTokens.tokens,
    tokenNote: briefTokens.note,
  });

  // ---- A2 RecallAgent：定向硬筛选（纯规则，浏览器内真算） ----
  const tA2 = now();
  const pool: Partial<Kox>[] = [];
  const skipCounts: Record<string, number> = {};
  for (const kox of records) {
    const reason = targetingReason(kox, spec);
    if (reason === null) pool.push(kox);
    else skipCounts[reason] = (skipCounts[reason] ?? 0) + 1;
  }
  const a2Ms = now() - tA2;
  await emit({
    id: 'A2',
    agent: 'A2 RecallAgent',
    title: '从全库召回候选（平台 / 品类 / 市场硬筛选）',
    kind: 'rule',
    elapsedMs: a2Ms,
    items: records.length,
    headline: `${records.length.toLocaleString('en-US')} → ${pool.length.toLocaleString('en-US')} 人`,
    detail: Object.entries(skipCounts)
      .sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `${k}：${v.toLocaleString('en-US')} 人被筛掉`),
    tokens: 0,
  });

  // ---- A3 GateAgent：四层门禁（纯规则，浏览器内真算） ----
  const tA3 = now();
  const results = new Map<string, GateResult>();
  for (const kox of pool) {
    const r = evaluate(kox, spec, thresholds);
    results.set(r.kox_id, r);
  }
  const a3Ms = now() - tA3;
  const verdictCounts: Record<VerdictKey, number> = { pass: 0, review: 0, reject: 0 };
  const gateHits: Record<string, number> = { G0: 0, G1: 0, G2: 0, G3: 0 };
  const ruleHitMap = new Map<string, number>();
  for (const r of results.values()) {
    verdictCounts[r.verdict] += 1;
    const gatesHit = new Set(r.reasons.map((x) => x.gate));
    for (const g of gatesHit) gateHits[g] = (gateHits[g] ?? 0) + 1;
    for (const reason of r.reasons) ruleHitMap.set(reason.rule_id, (ruleHitMap.get(reason.rule_id) ?? 0) + 1);
  }
  const ruleHits = [...ruleHitMap.entries()]
    .map(([rule_id, n]) => ({ rule_id, gate: rule_id.split('.')[0], n, label: ruleLabel(rule_id) }))
    .sort((a, b) => b.n - a.n || (a.rule_id < b.rule_id ? -1 : 1));
  await emit({
    id: 'A3',
    agent: 'A3 GateAgent',
    title: '四层质量门禁 G0 → G1 → G2 → G3',
    kind: 'rule',
    elapsedMs: a3Ms,
    items: pool.length,
    headline: `pass ${verdictCounts.pass} / review ${verdictCounts.review} / reject ${verdictCounts.reject}`,
    detail: [
      `平均 ${(a3Ms / Math.max(pool.length, 1)).toFixed(3)} ms/人，全部在浏览器主线程内完成`,
      `命中人数：G0 ${gateHits.G0}｜G1 ${gateHits.G1}｜G2 ${gateHits.G2}｜G3 ${gateHits.G3}`,
      '引擎入口物理剥离 gt 字段：判定过程读不到 ground truth',
    ],
    tokens: 0,
  });

  // ---- A4 FitAgent：语义适配（LLM 离线固化 + 规则兜底；本次走规则兜底） ----
  const tA4 = now();
  let ruleFit = 0;
  let injectedFit = 0;
  for (const r of results.values()) {
    if (r.fit_source === 'llm') injectedFit += 1;
    else ruleFit += 1;
  }
  const fitTokens = tokensOf(options.llmPerTask, 'fit', options.primaryModelKey);
  await emit({
    id: 'A4',
    agent: 'A4 FitAgent',
    title: '语义适配打分（内容调性 vs brief）',
    kind: 'llm-offline',
    elapsedMs: now() - tA4,
    items: results.size,
    headline: injectedFit > 0 ? `${injectedFit} 人用 LLM 固化分` : `${ruleHitsFitLabel(ruleFit)}`,
    detail: [
      '规则版口径：观测品类命中目标 1.0，仅相邻 0.65，仅自称打 0.9 折，handle 关键词兜底 0.70，完全不沾 0.20',
      'LLM 版在构建期真调（tag/fit 任务），线上按缺省走规则兜底，两者的 F1 差距见 Cost & Value 页',
    ],
    tokens: fitTokens.tokens,
    tokenNote: fitTokens.note,
  });

  // ---- A5 BudgetAgent：约束优化（浏览器内真算） ----
  const tA5 = now();
  const plan = planCampaign(records, spec, thresholds, results, {
    includeReview,
    decay,
    budgetUsd: options.budgetUsd ?? null,
  });
  const baseline = planBaseline(records, spec, thresholds, results, {
    decay,
    budgetUsd: options.budgetUsd ?? null,
  });
  const a5Ms = now() - tA5;
  await emit({
    id: 'A5',
    agent: 'A5 BudgetAgent',
    title: '预算分配：边际性价比贪心 + 分层配额修正',
    kind: 'rule',
    elapsedMs: a5Ms,
    items: plan.candidate_pool,
    headline: `选中 ${plan.n_selected} 人 / ${plan.n_posts} 条，花掉 ${(plan.utilization * 100).toFixed(1)}% 预算`,
    detail: [
      ...plan.trace,
      `约束校验：${plan.constraints.all_enforced_satisfied ? '全部满足' : `未满足 ${plan.constraints.violations.join('、')}`}`,
    ],
    tokens: 0,
  });

  // ---- A6 AuditAgent：反事实价值审计（以 gt 为裁判） ----
  const tA6 = now();
  const gtById = gtIndex(records);
  const audit = counterfactualRow(spec.campaign_id, plan, baseline, gtById);
  await emit({
    id: 'A6',
    agent: 'A6 AuditAgent',
    title: '成本账 + 反事实价值账（对照"按粉丝量选人"）',
    kind: 'audit',
    elapsedMs: now() - tA6,
    items: plan.n_selected + baseline.n_selected,
    headline: `少浪费 $${Math.round(audit.saved_usd).toLocaleString('en-US')}（有效曝光 ${audit.effective_view_uplift >= 0 ? '+' : ''}${(audit.effective_view_uplift * 100).toFixed(1)}%）`,
    detail: [
      `基线臂：${baseline.n_selected} 人，浪费 $${Math.round(audit.baseline.wasted_spend_usd).toLocaleString('en-US')}（占花费 ${(audit.baseline.wasted_spend_share * 100).toFixed(1)}%）`,
      `KOXPilot 臂：${plan.n_selected} 人，浪费 $${Math.round(audit.koxpilot.wasted_spend_usd).toLocaleString('en-US')}（占花费 ${(audit.koxpilot.wasted_spend_share * 100).toFixed(1)}%）`,
      '浪费金额与有效曝光一律按 gt 计算，不使用引擎自身分数（防自证）',
    ],
    tokens: 0,
  });

  return {
    spec,
    stages,
    totalMs: now() - t0,
    pool,
    results,
    skipCounts,
    verdictCounts,
    gateHits,
    ruleHits,
    funnel: survivalFunnel(pool, results, records.length),
    plan,
    baseline,
    audit,
    gtById,
    includeReview,
    decay,
  };
}

function ruleHitsFitLabel(n: number): string {
  return `${n} 人走规则兜底`;
}

// ---------------------------------------------------------------------------
// 评测复算（口径与 Python eval/metrics.py 对齐）
// ---------------------------------------------------------------------------

export interface BinaryScores {
  tp: number;
  fp: number;
  fn: number;
  tn: number;
  precision: number;
  recall: number;
  f1: number;
  specificity: number;
  accuracy: number;
  n: number;
}

function binaryScores(tp: number, fp: number, fn: number, tn: number): BinaryScores {
  const base = prf1(tp, fp, fn);
  const total = tp + fp + fn + tn;
  return {
    ...base,
    tn,
    specificity: tn + fp ? pyRound(tn / (tn + fp), 4) : 0.0,
    accuracy: total ? pyRound((tp + tn) / total, 4) : 0.0,
    n: total,
  };
}

/** 严口径：门禁会自动拦掉的"真实性不过关"（authenticity < 地板 或 硬信号 ≥2 条）。 */
export function fraudPredStrict(r: GateResult, floor: number, hardMin: number): boolean {
  return r.authenticity_score < floor || r.hard_hits.length >= hardMin;
}

/** 宽口径：G1 命中任意一条即"值得人核"。 */
export function fraudPredLoose(r: GateResult): boolean {
  return r.reasons.some((x) => x.gate === 'G1');
}

export interface EvalReport {
  n: number;
  elapsedMs: number;
  fraud: {
    strict: BinaryScores;
    loose: BinaryScores;
    auc: number;
    prevalence: number;
    perType: Record<string, { n: number; recall_strict: number; recall_loose: number; mean_fraud_score: number }>;
  };
  verdict: {
    n: number;
    accuracy: number;
    macro_f1: number;
    matrix: Record<string, Record<string, number>>;
    per_class: Record<string, BinaryScores>;
  };
  /** PR 曲线（用连续分 fraud_score 扫阈值，真算）。 */
  pr: Array<{ threshold: number; precision: number; recall: number }>;
  counts: Record<VerdictKey, number>;
  /** 分层 F1（platform / follower_bucket），用于热力图。 */
  strata: {
    byPlatform: Array<StratumRow>;
    byBucket: Array<StratumRow>;
    byGroup: Array<StratumRow>;
  };
}

export interface StratumRow {
  cell: string;
  n: number;
  positives: number;
  precision: number;
  recall: number;
  f1: number;
  lowSupport: boolean;
}

export interface EvalOptions {
  enabledGates?: readonly string[];
  disabledRules?: readonly string[];
  minSupportPositive?: number;
}

/** 对全库跑一遍中性口径判定并算全部评测指标（浏览器内真算，可与 metrics.json 对照）。 */
export function evaluateDataset(
  records: Partial<Kox>[],
  thresholds: Thresholds,
  options: EvalOptions = {},
): EvalReport {
  const t0 = now();
  const floor = thresholds.policy.authenticity_floor_reject;
  const hardMin = thresholds.policy.hard_hits_reject_min;
  const minSupport = options.minSupportPositive ?? 10;

  let sTp = 0;
  let sFp = 0;
  let sFn = 0;
  let sTn = 0;
  let lTp = 0;
  let lFp = 0;
  let lFn = 0;
  let lTn = 0;
  const scores: number[] = [];
  const labels: number[] = [];
  const matrix: Record<string, Record<string, number>> = {};
  for (const g of VERDICTS) matrix[g] = { pass: 0, review: 0, reject: 0 };
  const counts: Record<VerdictKey, number> = { pass: 0, review: 0, reject: 0 };
  const perType = new Map<string, { n: number; strict: number; loose: number; scoreSum: number }>();
  const strata = new Map<string, { key: string; tp: number; fp: number; fn: number; tn: number; pos: number; n: number }>();

  const bump = (bucketKey: string, pred: boolean, truth: boolean): void => {
    const slot = strata.get(bucketKey) ?? { key: bucketKey, tp: 0, fp: 0, fn: 0, tn: 0, pos: 0, n: 0 };
    slot.n += 1;
    if (truth) slot.pos += 1;
    if (pred && truth) slot.tp += 1;
    else if (pred) slot.fp += 1;
    else if (truth) slot.fn += 1;
    else slot.tn += 1;
    strata.set(bucketKey, slot);
  };

  for (const kox of records) {
    const r = evaluate(kox, NEUTRAL_SPEC, thresholds, {
      enabledGates: options.enabledGates,
      disabledRules: options.disabledRules,
    });
    counts[r.verdict] += 1;
    const gt = (kox as { gt?: GroundTruth }).gt;
    const truthFraud = Boolean(gt?.is_fraud);
    const truthVerdict = String(gt?.verdict ?? 'pass');
    const strict = fraudPredStrict(r, floor, hardMin);
    const loose = fraudPredLoose(r);

    if (strict && truthFraud) sTp += 1;
    else if (strict) sFp += 1;
    else if (truthFraud) sFn += 1;
    else sTn += 1;
    if (loose && truthFraud) lTp += 1;
    else if (loose) lFp += 1;
    else if (truthFraud) lFn += 1;
    else lTn += 1;

    scores.push(r.fraud_score);
    labels.push(truthFraud ? 1 : 0);
    if (matrix[truthVerdict]) matrix[truthVerdict][r.verdict] += 1;

    if (truthFraud && typeof gt?.fraud_type === 'string') {
      const slot = perType.get(gt.fraud_type) ?? { n: 0, strict: 0, loose: 0, scoreSum: 0 };
      slot.n += 1;
      if (strict) slot.strict += 1;
      if (loose) slot.loose += 1;
      slot.scoreSum += r.fraud_score;
      perType.set(gt.fraud_type, slot);
    }

    const platform = String(kox.platform ?? 'unknown');
    const bucket = r.group_key.split('|')[1] ?? 'nano';
    bump(`platform:${platform}`, strict, truthFraud);
    bump(`bucket:${bucket}`, strict, truthFraud);
    bump(`group:${r.group_key}`, strict, truthFraud);
  }

  const total = VERDICTS.reduce((acc, g) => acc + VERDICTS.reduce((a2, p) => a2 + matrix[g][p], 0), 0);
  const correct = VERDICTS.reduce((acc, v) => acc + matrix[v][v], 0);
  const perClass: Record<string, BinaryScores> = {};
  for (const v of VERDICTS) {
    const tp = matrix[v][v];
    const fn = VERDICTS.filter((p) => p !== v).reduce((a, p) => a + matrix[v][p], 0);
    const fp = VERDICTS.filter((g) => g !== v).reduce((a, g) => a + matrix[g][v], 0);
    perClass[v] = binaryScores(tp, fp, fn, total - tp - fn - fp);
  }
  const macro = pyRound(VERDICTS.reduce((a, v) => a + perClass[v].f1, 0) / VERDICTS.length, 4);

  // PR 曲线：按连续分降序扫，只在分值变化处取点（避免上万个重复点）
  const order = scores.map((s, i) => [s, labels[i]] as const).sort((a, b) => b[0] - a[0]);
  const positives = labels.reduce((a, b) => a + b, 0);
  const pr: Array<{ threshold: number; precision: number; recall: number }> = [];
  let tpRun = 0;
  let fpRun = 0;
  for (let i = 0; i < order.length; i += 1) {
    if (order[i][1] === 1) tpRun += 1;
    else fpRun += 1;
    const nextDifferent = i === order.length - 1 || order[i + 1][0] !== order[i][0];
    if (nextDifferent && positives > 0) {
      pr.push({
        threshold: pyRound(order[i][0], 4),
        precision: pyRound(safeDiv(tpRun, tpRun + fpRun), 4),
        recall: pyRound(tpRun / positives, 4),
      });
    }
  }

  const rows = (prefix: string): StratumRow[] =>
    [...strata.values()]
      .filter((s) => s.key.startsWith(`${prefix}:`))
      .map((s) => {
        const m = prf1(s.tp, s.fp, s.fn);
        return {
          cell: s.key.slice(prefix.length + 1),
          n: s.n,
          positives: s.pos,
          precision: m.precision,
          recall: m.recall,
          f1: m.f1,
          lowSupport: s.pos < minSupport,
        };
      })
      .sort((a, b) => b.n - a.n);

  const perTypeOut: EvalReport['fraud']['perType'] = {};
  for (const [k, v] of [...perType.entries()].sort((a, b) => b[1].n - a[1].n)) {
    perTypeOut[k] = {
      n: v.n,
      recall_strict: pyRound(v.strict / v.n, 4),
      recall_loose: pyRound(v.loose / v.n, 4),
      mean_fraud_score: pyRound(v.scoreSum / v.n, 4),
    };
  }

  return {
    n: records.length,
    elapsedMs: now() - t0,
    fraud: {
      strict: binaryScores(sTp, sFp, sFn, sTn),
      loose: binaryScores(lTp, lFp, lFn, lTn),
      auc: rocAuc(scores, labels),
      prevalence: labels.length ? pyRound(positives / labels.length, 4) : 0,
      perType: perTypeOut,
    },
    verdict: {
      n: total,
      accuracy: total ? pyRound(correct / total, 4) : 0,
      macro_f1: macro,
      matrix,
      per_class: perClass,
    },
    pr,
    counts,
    strata: { byPlatform: rows('platform'), byBucket: rows('bucket'), byGroup: rows('group') },
  };
}

export interface AblationRow {
  variant: string;
  role: string;
  enabledGates: string[];
  disabledRules: string[];
  fraud_f1_strict: number;
  fraud_auc: number;
  verdict_accuracy: number;
  verdict_macro_f1: number;
  n_reject: number;
  d_fraud_f1_strict: number;
  d_verdict_accuracy: number;
  elapsedMs: number;
}

export const GATE_ROLES: Record<string, string> = {
  G0: '数据完整性：缺关键字段的号降置信度并进人核，防止用残缺数据下判定',
  G1: '真实性：7 条统计信号识别水号，是水号指标的唯一来源',
  G2: '一致性：品类/多源/语言/受众匹配，campaign 相关的错配拦截',
  G3: '品牌安全：高危内容硬阻断、竞品冲突与争议历史',
};

/** 单个消融变体：现场重跑全库。用于 Evaluation 页的"点一下关掉某层"。 */
export function ablationVariant(
  records: Partial<Kox>[],
  thresholds: Thresholds,
  full: EvalReport,
  opts: { variant: string; role: string; enabledGates?: readonly string[]; disabledRules?: readonly string[] },
): AblationRow {
  const rep = evaluateDataset(records, thresholds, {
    enabledGates: opts.enabledGates,
    disabledRules: opts.disabledRules,
  });
  return {
    variant: opts.variant,
    role: opts.role,
    enabledGates: [...(opts.enabledGates ?? ALL_GATES)],
    disabledRules: [...(opts.disabledRules ?? [])],
    fraud_f1_strict: rep.fraud.strict.f1,
    fraud_auc: rep.fraud.auc,
    verdict_accuracy: rep.verdict.accuracy,
    verdict_macro_f1: rep.verdict.macro_f1,
    n_reject: rep.counts.reject,
    d_fraud_f1_strict: pyRound(rep.fraud.strict.f1 - full.fraud.strict.f1, 4),
    d_verdict_accuracy: pyRound(rep.verdict.accuracy - full.verdict.accuracy, 4),
    elapsedMs: rep.elapsedMs,
  };
}

export interface SensitivityPoint {
  factor: number;
  fraud_f1_strict: number;
  fraud_precision_strict: number;
  fraud_recall_strict: number;
  verdict_accuracy: number;
  n_reject: number;
  d_fraud_f1_strict: number;
}

/** 阈值整体缩放扫描：对上尾规则放大=放松、对下尾规则放大=收紧，一次覆盖两个方向。 */
export function sensitivityScan(
  records: Partial<Kox>[],
  thresholds: Thresholds,
  full: EvalReport,
  factors: readonly number[],
  signals?: readonly string[],
): SensitivityPoint[] {
  return factors.map((factor) => {
    const rep =
      factor === 1.0
        ? full
        : evaluateDataset(records, signals ? thresholds.scaled(factor, signals) : thresholds.scaled(factor));
    return {
      factor,
      fraud_f1_strict: rep.fraud.strict.f1,
      fraud_precision_strict: rep.fraud.strict.precision,
      fraud_recall_strict: rep.fraud.strict.recall,
      verdict_accuracy: rep.verdict.accuracy,
      n_reject: rep.counts.reject,
      d_fraud_f1_strict: pyRound(rep.fraud.strict.f1 - full.fraud.strict.f1, 4),
    };
  });
}
