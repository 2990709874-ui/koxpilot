/**
 * G1 真实性门禁：水号识别 7 条信号（对应 Python `gates/g1.py`，SPEC 4.G1）。
 *
 * 阈值全部来自 `(platform, follower_bucket)` 分组的分位数（thresholds.json），
 * 本文件里没有任何判定用的裸数字（唯一常量 z>2.5 来自 policy 快照，带 SPEC 出处）。
 *
 * 两个输出必须分清：
 * - `reasons` / `hardHits`：**离散判定**，用于 pass/review/reject 合成；
 * - `fraudScore`：**连续异常分**，各信号在同组分布中"越界深度"的加权和（从 P80 起坡），
 *   用于 PR 曲线 / AUC。若直接用命中条数当分数，AUC 会退化成几个台阶，失去意义。
 */

import { QUANTILE_SIGNALS, SPIKE_GROWTH_FLOOR_Q } from './policy.ts';
import type { GatePolicy } from './policy.ts';
import { groupLabel, num, pct, ratio, sourceLabel } from './humanize.ts';
import { pyRound, robustZscores } from './stats.ts';
import { extractSignals, koxGroupKey, monthlyGrowthRates, numOrNull } from './signals.ts';
import type { Thresholds } from './thresholds.ts';
import type { Kox, Reason } from './types.ts';

/**
 * 幅度不足时对连续分的折扣系数（Python `g1.py` 模块内联常量 0.35）。
 * 作用：z 高但幅度平庸的月份不该拿满分，否则大量温和波动会污染 AUC。
 */
const SPIKE_WEAK_AMPLITUDE_DISCOUNT = 0.35;

export interface G1Outcome {
  reasons: Reason[];
  hard_hits: string[];
  /** 加权命中扣分（未打置信度折扣、未取整）。真正参与分数合成的是 reason.weight。 */
  penalty: number;
  fraud_score: number;
  signal_ranks: Record<string, number>;
}

export interface SpikeEvidence {
  month: number;
  growth: number;
  zscore: number;
  max_unsupported_z?: number;
}

/**
 * G1.5 的证据计算：找出"最像买粉"的那个月。
 * 1) 12 个月粉丝历史 -> 11 个月度环比增速；
 * 2) 用**鲁棒 z-score**（median + MAD）衡量异常度——用 MAD 而非标准差，
 *    因为突刺本身会把标准差撑大造成掩蔽效应；
 * 3) 只保留 viral_months 里没有内容支撑的月份（允许 1 个月滞后）；
 * 4) 取其中 z 最大的月份作为证据。
 */
export function spikeEvidence(kox: Partial<Kox>, policy: GatePolicy): SpikeEvidence | null {
  const history = kox.follower_history;
  if (!Array.isArray(history) || history.length < policy.min_history_months) return null;
  const rates = monthlyGrowthRates(history);
  if (rates.length < policy.min_history_months - 1) return null;
  const zs = robustZscores(rates);
  const viral = new Set<number>(
    (kox.viral_months ?? []).filter((m) => typeof m === 'number').map((m) => Math.trunc(m as number)),
  );
  let best: SpikeEvidence | null = null;
  let maxZ = 0.0;
  for (let i = 0; i < zs.length; i += 1) {
    const z = zs[i];
    const month = i + 1; // rates[i] 是 history[i] -> history[i+1] 的增长，落在第 i+1 个月
    let supported = viral.has(month);
    for (let lag = 0; lag <= policy.viral_support_lag_months; lag += 1) {
      if (viral.has(month - lag)) supported = true;
    }
    maxZ = Math.max(maxZ, supported ? 0.0 : z);
    if (supported || z <= 0) continue;
    if (best === null || z > best.zscore) best = { month, growth: rates[i], zscore: z };
  }
  if (best !== null) best.max_unsupported_z = maxZ;
  return best;
}

/** 纯函数：只吃可观测信号 + 阈值表，不吃 campaign、不吃 ground truth。 */
export function evaluateG1(kox: Partial<Kox>, thresholds: Thresholds): G1Outcome {
  const policy = thresholds.policy;
  const sig = extractSignals(kox, policy);
  const gkey = koxGroupKey(kox);
  const label = groupLabel(gkey);
  const reasons: Reason[] = [];
  const hard: string[] = [];
  const ranks: Record<string, number> = {};
  const scoreTerms: Array<[string, number]> = [];
  let penalty = 0.0;

  const rampUpper = (rank: number): number => {
    const span = 1.0 - policy.fraud_score_ramp_upper;
    return Math.min(1.0, Math.max(0.0, (rank - policy.fraud_score_ramp_upper) / span));
  };
  const rampLower = (rank: number): number =>
    Math.min(1.0, Math.max(0.0, (policy.fraud_score_ramp_lower - rank) / policy.fraud_score_ramp_lower));

  /**
   * 越界深度 ∈[0,1]：0 = 刚好压线，1 = 深入尾部（证据饱和）。
   * 把实际值与阈值都映射成同组经验百分位再按剩余尾部空间归一化——
   * 用百分位差而非绝对差，是为了跨信号可比（互动率的 0.01 和播放比的 0.01 不是一回事）。
   */
  const depthOf = (signal: string, actual: number, threshold: number, upper: boolean): number => {
    const rankA = thresholds.percentileRank(gkey, signal, actual);
    const rankT = thresholds.percentileRank(gkey, signal, threshold);
    if (upper) {
      const room = Math.max(1e-6, 1.0 - rankT);
      return Math.min(1.0, Math.max(0.0, (rankA - rankT) / room));
    }
    const room = Math.max(1e-6, rankT);
    return Math.min(1.0, Math.max(0.0, (rankT - rankA) / room));
  };

  const hit = (
    ruleId: string,
    signal: string,
    actual: number | string | null,
    threshold: number | string | null,
    text: string,
    depth = 0.0,
  ): void => {
    // 分级惩罚：越界越深，扣分越重（policy.graded_penalty_gain 的推导见 SPEC/Notes）
    const weight = policy.g1_weights[ruleId] * (1.0 + policy.graded_penalty_gain * depth);
    const severity = policy.g1_hard_rules.includes(ruleId) ? 'hard' : 'soft';
    reasons.push({
      gate: 'G1',
      rule_id: ruleId,
      signal,
      actual,
      threshold,
      weight: pyRound(weight, 4),
      depth: pyRound(depth, 4),
      severity,
      source: QUANTILE_SIGNALS.has(signal)
        ? `quantile:${thresholds.source(gkey, signal)}`
        : 'policy:spec_4.G1.5',
      human_text: text,
    });
    penalty += weight;
    if (severity === 'hard') hard.push(ruleId);
  };

  // ---- G1.1 / G1.2 互动率双尾 ------------------------------------------
  const er = sig.engagement_rate;
  if (er !== null) {
    const p95 = thresholds.value(gkey, 'engagement_rate', 'p95');
    const p05 = thresholds.value(gkey, 'engagement_rate', 'p05');
    const rank = thresholds.percentileRank(gkey, 'engagement_rate', er);
    ranks.engagement_rate = rank;
    scoreTerms.push(['G1.1', rampUpper(rank)]);
    scoreTerms.push(['G1.2', rampLower(rank)]);
    const src = sourceLabel(thresholds.source(gkey, 'engagement_rate'));
    if (p95 !== null && er > p95) {
      hit(
        'G1.1',
        'engagement_rate',
        pyRound(er, 6),
        pyRound(p95, 6),
        `互动率 ${pct(er)} 高于 ${label} 同行的 P95（${pct(p95)}），` +
          `互动量与粉丝规模不成比例，疑似互动农场/互赞群刷量。${src}。`,
        depthOf('engagement_rate', er, p95, true),
      );
    } else if (p05 !== null && er < p05) {
      hit(
        'G1.2',
        'engagement_rate',
        pyRound(er, 6),
        pyRound(p05, 6),
        `互动率仅 ${pct(er)}，低于 ${label} 同行的 P05（${pct(p05)}），` +
          `有粉丝没互动，疑似买粉或僵尸粉稀释。${src}。`,
        depthOf('engagement_rate', er, p05, false),
      );
    }
  }

  // ---- G1.3 评论/点赞比越界 --------------------------------------------
  const clr = sig.comment_like_ratio;
  if (clr !== null) {
    const lo = thresholds.value(gkey, 'comment_like_ratio', 'p02');
    const hi = thresholds.value(gkey, 'comment_like_ratio', 'p98');
    const rank = thresholds.percentileRank(gkey, 'comment_like_ratio', clr);
    ranks.comment_like_ratio = rank;
    scoreTerms.push(['G1.3', Math.max(rampUpper(rank), rampLower(rank))]);
    if (lo !== null && hi !== null && (clr < lo || clr > hi)) {
      const side = clr > hi ? '高于上界' : '低于下界';
      const thr = clr > hi ? hi : lo;
      hit(
        'G1.3',
        'comment_like_ratio',
        pyRound(clr, 6),
        pyRound(thr, 6),
        `评论/点赞比 ${ratio(clr)} ${side}，超出 ${label} 同行的正常区间 ` +
          `[${ratio(lo)}, ${ratio(hi)}]（P02–P98），评论区结构异常。` +
          `${sourceLabel(thresholds.source(gkey, 'comment_like_ratio'))}。`,
        depthOf('comment_like_ratio', clr, thr, clr > hi),
      );
    }
  }

  // ---- G1.4 播放/粉丝比越界 --------------------------------------------
  const vfr = sig.view_follower_ratio;
  if (vfr !== null) {
    const lo = thresholds.value(gkey, 'view_follower_ratio', 'p02');
    const hi = thresholds.value(gkey, 'view_follower_ratio', 'p98');
    const rank = thresholds.percentileRank(gkey, 'view_follower_ratio', vfr);
    ranks.view_follower_ratio = rank;
    scoreTerms.push(['G1.4', Math.max(rampUpper(rank), rampLower(rank))]);
    if (lo !== null && hi !== null && (vfr < lo || vfr > hi)) {
      const text =
        vfr > hi
          ? `播放/粉丝比 ${ratio(vfr)} 高于 ${label} 同行的 P98（${ratio(hi)}），` +
            `播放量远超粉丝盘能解释的范围，疑似播放注水。`
          : `播放/粉丝比仅 ${ratio(vfr)}，低于 ${label} 同行的 P02（${ratio(lo)}），` +
            `粉丝规模撑不起播放量，疑似粉丝虚高（买粉）。`;
      const thr = vfr > hi ? hi : lo;
      hit(
        'G1.4',
        'view_follower_ratio',
        pyRound(vfr, 6),
        pyRound(thr, 6),
        text + sourceLabel(thresholds.source(gkey, 'view_follower_ratio')) + '。',
        depthOf('view_follower_ratio', vfr, thr, vfr > hi),
      );
    }
  }

  // ---- G1.5 粉丝突刺无内容支撑 -----------------------------------------
  // 双重条件（第二条是本实现对 SPEC 的修正）：
  //   ① 该月增速的鲁棒 z-score > 2.5（SPEC 原文）
  //   ② 该月增速 > 同组"最好月份增速"分布的 P90（数据标定，压掉短序列 z 的固有假阳性）
  const spike = spikeEvidence(kox, policy);
  const growthFloor = thresholds.value(gkey, 'max_monthly_growth', SPIKE_GROWTH_FLOOR_Q);
  if (spike !== null) {
    const z = spike.zscore;
    const growth = spike.growth;
    const [zLo, zHi] = policy.fraud_score_z_ramp;
    let zTerm = Math.min(1.0, Math.max(0.0, (z - zLo) / (zHi - zLo)));
    if (growthFloor !== null && growth <= growthFloor) zTerm *= SPIKE_WEAK_AMPLITUDE_DISCOUNT;
    scoreTerms.push(['G1.5', zTerm]);
    ranks.spike_zscore = z;
    if (z > policy.spike_zscore_min && (growthFloor === null || growth > growthFloor)) {
      hit(
        'G1.5',
        'follower_history',
        pyRound(z, 3),
        policy.spike_zscore_min,
        `第 ${spike.month} 个月粉丝环比 ${pct(growth, 0)}，` +
          `鲁棒 z-score=${num(z)} 超过 ${policy.spike_zscore_min}，` +
          `且该月增速高于 ${label} 同行「最好月份」增速的 P90` +
          `（${growthFloor !== null ? pct(growthFloor, 0) : '—'}），` +
          `但该月及前一个月都没有爆款内容记录（viral_months 不含该月），` +
          `典型的买粉突刺形态。阈值来源：SPEC 4.G1.5 的 z>2.5 + 同组增速分位数下限。`,
        Math.min(1.0, Math.max(0.0, (z - policy.spike_zscore_min) / (policy.fraud_score_z_ramp[1] * 2.0))),
      );
    }
  }

  // ---- G1.6 新号巨量粉 -------------------------------------------------
  const fpd = sig.followers_per_day;
  if (fpd !== null) {
    const p97 = thresholds.value(gkey, 'followers_per_day', 'p97');
    const rank = thresholds.percentileRank(gkey, 'followers_per_day', fpd);
    ranks.followers_per_day = rank;
    scoreTerms.push(['G1.6', rampUpper(rank)]);
    if (p97 !== null && fpd > p97) {
      hit(
        'G1.6',
        'followers_per_day',
        pyRound(fpd, 3),
        pyRound(p97, 3),
        `日均涨粉 ${num(fpd, 1)} 人，高于 ${label} 同行的 P97（${num(p97, 1)} 人）` +
          `——账号只有 ${num(numOrNull(kox.account_age_days), 0)} 天就攒到 ` +
          `${num(numOrNull(kox.followers), 0)} 粉，增长曲线不自然（也可能是 MCN 起号，故只计软信号）。` +
          `${sourceLabel(thresholds.source(gkey, 'followers_per_day'))}。`,
        depthOf('followers_per_day', fpd, p97, true),
      );
    }
  }

  // ---- G1.7 评论质量异常 ------------------------------------------------
  const dup = sig.comment_dup_rate;
  const emo = sig.comment_emoji_only_rate;
  const dupP97 = thresholds.value(gkey, 'comment_dup_rate', 'p97');
  const emoP97 = thresholds.value(gkey, 'comment_emoji_only_rate', 'p97');
  let term = 0.0;
  if (dup !== null) {
    const r = thresholds.percentileRank(gkey, 'comment_dup_rate', dup);
    ranks.comment_dup_rate = r;
    term = Math.max(term, rampUpper(r));
  }
  if (emo !== null) {
    const r = thresholds.percentileRank(gkey, 'comment_emoji_only_rate', emo);
    ranks.comment_emoji_only_rate = r;
    term = Math.max(term, rampUpper(r));
  }
  scoreTerms.push(['G1.7', term]);
  const dupHit = dup !== null && dupP97 !== null && dup > dupP97;
  const emoHit = emo !== null && emoP97 !== null && emo > emoP97;
  if (dupHit || emoHit) {
    const signalName = dupHit ? 'comment_dup_rate' : 'comment_emoji_only_rate';
    const actual = (dupHit ? dup : emo) as number;
    const thr = (dupHit ? dupP97 : emoP97) as number;
    const detail = dupHit
      ? `评论重复率 ${pct(dup)} 高于 ${label} 同行的 P97（${pct(dupP97)}）`
      : `纯 emoji 评论占比 ${pct(emo)} 高于 ${label} 同行的 P97（${pct(emoP97)}）`;
    hit(
      'G1.7',
      signalName,
      pyRound(actual, 6),
      pyRound(thr, 6),
      `${detail}，评论区疑似机器批量生成（模板化/无语义）。` +
        `${sourceLabel(thresholds.source(gkey, signalName))}。`,
      depthOf(signalName, actual, thr, true),
    );
  }

  // ---- 连续异常分：按 G1 权重归一加权 -----------------------------------
  let totalW = 0.0;
  for (const [r] of scoreTerms) totalW += policy.g1_weights[r];
  if (totalW === 0.0) totalW = 1.0;
  let acc = 0.0;
  for (const [r, s] of scoreTerms) acc += policy.g1_weights[r] * s;
  const fraudScore = acc / totalW;

  return {
    reasons,
    hard_hits: hard,
    penalty,
    fraud_score: Math.min(1.0, Math.max(0.0, fraudScore)),
    signal_ranks: ranks,
  };
}
