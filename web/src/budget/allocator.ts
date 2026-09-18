/**
 * 预算分配器（对应 Python `budget/allocator.py`，SPEC 第 5 节）：
 * 边际性价比贪心 + 分层配额修正到不动点。
 *
 * 采购模型是**按条买**：决策变量不是"选不选这个人"，而是"在这个人身上买几条内容"。
 * 第 n 条的边际价值按 decay^(n-1) 衰减（受众重叠 → 净增触达递减），
 * 于是同一达人的边际性价比随 n 严格递减，全局按边际性价比降序取名额时
 * 第 n 条一定排在第 n-1 条之后 —— 凹效用下贪心的经典结构，不必额外维护顺序约束。
 *
 * 两条算法纪律：
 * 1. 确定性：排序键一律带 kox_id 与名额序号做最终 tie-break（浮点相等时也不会抖）；
 * 2. 可终止：加名额只发生在"还有未取名额"，退名额只发生在"加不进去"，另有步数兜底。
 */

import { HEAD_BUCKETS, LONGTAIL_BUCKETS } from '../engine/taxonomy.ts';
import { pyRound } from '../engine/stats.ts';
import type { CampaignSpec } from '../engine/types.ts';
import {
  COUNTRY_MAX_SHARE,
  HEAD_MAX_SHARE,
  LONGTAIL_MIN_SHARE,
  MAX_POSTS_PER_KOX,
  MAX_REPAIR_ROUNDS,
  MAX_REPAIR_STEPS,
  MIN_UTILIZATION_TARGET,
  POST_MARGINAL_DECAY,
  SINGLE_KOX_MAX_SHARE,
  constraintSnapshot,
  effectivePosts,
} from './policy.ts';
import { estEngagements, type Candidate, type SkippedRecord } from './value.ts';

export interface Allocation {
  kox_id: string;
  handle: string;
  platform: string;
  country: string;
  bucket: string;
  amount_usd: number;
  posts: number;
  effective_posts: number;
  price_estimated: boolean;
  est_views: number;
  est_effective_views: number;
  est_engagements: number;
  value_score: number;
  efficiency: number;
  fit_score: number;
  authenticity_discount: number;
  audience_match: number;
  kpi_weight: number;
  verdict: string;
  picked_by: string;
}

export interface ConstraintCheck {
  name: string;
  desc: string;
  actual: number;
  limit: number;
  satisfied: boolean;
  enforced: boolean;
}

export interface ConstraintReport {
  policy: Record<string, number>;
  checks: ConstraintCheck[];
  all_enforced_satisfied: boolean;
  violations: string[];
  utilization: number;
  utilization_ok: boolean;
}

export interface BudgetPlan {
  campaign_id: string;
  strategy: string;
  budget_usd: number;
  spent_usd: number;
  utilization: number;
  n_selected: number;
  n_posts: number;
  purchase_model: { max_posts_per_kox: number; post_marginal_decay: number };
  candidate_pool: number;
  n_price_estimated: number;
  est_total_views: number;
  est_effective_views: number;
  est_total_engagements: number;
  est_cpm_usd: number;
  est_cpe_usd: number;
  tier_mix: Record<string, number>;
  country_mix: Record<string, number>;
  platform_mix: Record<string, number>;
  constraints: ConstraintReport;
  trace: string[];
  selected: Allocation[];
  skipped: SkippedRecord[];
}

/**
 * 排序依据（对应 Python `allocator.BASIS_VALUE` / `BASIS_VIEWS`）：
 * - `value`：引擎的质量加权价值（含真实性折扣 / 语义适配 / KPI 权重）；
 * - `views`：只用名义曝光 avg_views，**不读任何门禁与质量信号**。第三臂
 *   `diversified_no_gate` 用它，这样"分散化贡献"里不会混进门禁的功劳。
 */
export type Basis = 'value' | 'views';
export const BASIS_VALUE: Basis = 'value';
export const BASIS_VIEWS: Basis = 'views';

/** 三条臂的采购规则：strategy -> [是否强制结构约束, 排序依据]。 */
export const STRATEGIES: Record<Strategy, [boolean, Basis]> = {
  followers: [false, BASIS_VIEWS],
  diversified_no_gate: [true, BASIS_VIEWS],
  koxpilot: [true, BASIS_VALUE],
};

export type Strategy = 'koxpilot' | 'followers' | 'diversified_no_gate';

/** 一个采购名额 = 在某达人身上买的第 index 条内容（index 从 1 开始）。 */
export interface Slot {
  candidate: Candidate;
  index: number;
  decay: number;
  basis: Basis;
}

const slotCost = (s: Slot): number => s.candidate.cost_usd;
/** 第 1 条内容的价值（未衰减），按 basis 取质量加权价值或名义曝光。 */
const baseValue = (s: Slot): number => (s.basis === BASIS_VIEWS ? s.candidate.avg_views : s.candidate.value);
const marginalValue = (s: Slot): number => baseValue(s) * s.decay ** (s.index - 1);
const marginalEfficiency = (s: Slot): number => marginalValue(s) / s.candidate.cost_usd;

export function buildSlots(
  pool: readonly Candidate[],
  maxPosts: number = MAX_POSTS_PER_KOX,
  decay: number = POST_MARGINAL_DECAY,
  basis: Basis = BASIS_VALUE,
): Slot[] {
  if (!(decay > 0.0 && decay <= 1.0)) {
    throw new Error('POST_MARGINAL_DECAY 必须落在 (0,1]，否则贪心的凹性前提不成立');
  }
  const out: Slot[] = [];
  for (const c of pool) {
    for (let i = 1; i <= Math.max(1, maxPosts); i += 1) out.push({ candidate: c, index: i, decay, basis });
  }
  return out;
}

/** 选择集的增量状态：所有份额 O(1) 可读，避免每步重算全表。 */
interface SelectionSnapshot {
  posts: Map<string, number>;
  candById: Map<string, Candidate>;
  pickedBy: Map<string, string>;
  spent: number;
  headUsd: number;
  longtailUsd: number;
  countryUsd: Map<string, number>;
}

class Selection {
  readonly budget: number;
  readonly decay: number;
  readonly basis: Basis;
  posts = new Map<string, number>();
  candById = new Map<string, Candidate>();
  pickedBy = new Map<string, string>();
  spent = 0.0;
  headUsd = 0.0;
  longtailUsd = 0.0;
  countryUsd = new Map<string, number>();

  constructor(budget: number, decay: number, basis: Basis = BASIS_VALUE) {
    this.budget = budget;
    this.decay = decay;
    this.basis = basis;
  }

  take(slot: Slot, tag: string): void {
    const cand = slot.candidate;
    const kid = cand.kox_id;
    this.posts.set(kid, (this.posts.get(kid) ?? 0) + 1);
    this.candById.set(kid, cand);
    if (!this.pickedBy.has(kid)) this.pickedBy.set(kid, tag);
    const cost = slotCost(slot);
    this.spent += cost;
    if (HEAD_BUCKETS.has(cand.bucket)) this.headUsd += cost;
    if (LONGTAIL_BUCKETS.has(cand.bucket)) this.longtailUsd += cost;
    this.countryUsd.set(cand.country, (this.countryUsd.get(cand.country) ?? 0.0) + cost);
  }

  /** 退掉该达人的最后一条内容（边际价值最低的那条）。 */
  giveBack(koxId: string): Candidate {
    const cand = this.candById.get(koxId) as Candidate;
    const cost = cand.cost_usd;
    const left = (this.posts.get(koxId) ?? 0) - 1;
    this.posts.set(koxId, left);
    if (left <= 0) {
      this.posts.delete(koxId);
      this.candById.delete(koxId);
      this.pickedBy.delete(koxId);
    }
    this.spent -= cost;
    if (HEAD_BUCKETS.has(cand.bucket)) this.headUsd -= cost;
    if (LONGTAIL_BUCKETS.has(cand.bucket)) this.longtailUsd -= cost;
    this.countryUsd.set(cand.country, (this.countryUsd.get(cand.country) ?? 0.0) - cost);
    return cand;
  }

  nPosts(koxId: string): number {
    return this.posts.get(koxId) ?? 0;
  }

  /** 浅拷贝全部增量状态，供修正阶段试探性退让后回滚（与 Python `Selection.snapshot` 同口径）。 */
  snapshot(): SelectionSnapshot {
    return {
      posts: new Map(this.posts),
      candById: new Map(this.candById),
      pickedBy: new Map(this.pickedBy),
      spent: this.spent,
      headUsd: this.headUsd,
      longtailUsd: this.longtailUsd,
      countryUsd: new Map(this.countryUsd),
    };
  }

  restore(snap: SelectionSnapshot): void {
    this.posts = new Map(snap.posts);
    this.candById = new Map(snap.candById);
    this.pickedBy = new Map(snap.pickedBy);
    this.spent = snap.spent;
    this.headUsd = snap.headUsd;
    this.longtailUsd = snap.longtailUsd;
    this.countryUsd = new Map(snap.countryUsd);
  }

  amountOf(koxId: string): number {
    return (this.posts.get(koxId) ?? 0) * (this.candById.get(koxId) as Candidate).cost_usd;
  }

  get remaining(): number {
    return this.budget - this.spent;
  }

  share(amount: number): number {
    return this.spent > 0 ? amount / this.spent : 0.0;
  }

  get headShare(): number {
    return this.share(this.headUsd);
  }

  get longtailShare(): number {
    return this.share(this.longtailUsd);
  }

  get maxCountryShare(): number {
    let best = 0.0;
    let seen = false;
    for (const v of this.countryUsd.values()) {
      const s = this.share(v);
      if (!seen || s > best) best = s;
      seen = true;
    }
    return seen ? best : 0.0;
  }

  /** 该达人当前最后一条内容的边际性价比（退让时优先退这个值最低的）。 */
  marginalEfficiencyOfLast(koxId: string): number {
    const n = this.posts.get(koxId) ?? 0;
    if (n <= 0) return 0.0;
    // 用的是本臂自己的 basis：第三臂不看质量加权价值，退让时也不许偷看。
    return marginalEfficiency({
      candidate: this.candById.get(koxId) as Candidate,
      index: n,
      decay: this.decay,
      basis: this.basis,
    });
  }
}

/** 边际性价比降序；同值先要边际价值大的，最后按 id/序号定序保证确定性。 */
function cmpEfficiency(a: Slot, b: Slot): number {
  const ea = -marginalEfficiency(a);
  const eb = -marginalEfficiency(b);
  if (ea !== eb) return ea - eb;
  const va = -marginalValue(a);
  const vb = -marginalValue(b);
  if (va !== vb) return va - vb;
  if (a.candidate.kox_id !== b.candidate.kox_id) return a.candidate.kox_id < b.candidate.kox_id ? -1 : 1;
  return a.index - b.index;
}

/** 基线：粉丝量降序（同一人多条按序号，行业最朴素做法）。 */
function cmpFollowers(a: Slot, b: Slot): number {
  const fa = -a.candidate.followers;
  const fb = -b.candidate.followers;
  if (fa !== fb) return fa - fb;
  if (a.index !== b.index) return a.index - b.index;
  return a.candidate.kox_id < b.candidate.kox_id ? -1 : a.candidate.kox_id > b.candidate.kox_id ? 1 : 0;
}

/** 贪心阶段可行性检查（分母 = 总预算，单调、与选择顺序无关）。 */
function fitsCapsVsBudget(sel: Selection, slot: Slot): string | null {
  const cand = slot.candidate;
  const cost = slotCost(slot);
  if (cost > sel.remaining) return 'over_remaining_budget';
  if (sel.nPosts(cand.kox_id) * cost + cost > SINGLE_KOX_MAX_SHARE * sel.budget) return 'over_single_kox_cap';
  if (HEAD_BUCKETS.has(cand.bucket) && sel.headUsd + cost > HEAD_MAX_SHARE * sel.budget) return 'over_head_cap';
  if ((sel.countryUsd.get(cand.country) ?? 0.0) + cost > COUNTRY_MAX_SHARE * sel.budget) return 'over_country_cap';
  return null;
}

/**
 * 长尾下限的**单调化**目标份额：已达标时守住 25%，未达标时至少不许更糟。
 * 直接用固定下限做"加入后必须 ≥25%"会在份额已低于下限时把补位阶段整体冻死。
 */
function longtailTarget(sel: Selection): number {
  if (sel.spent <= 0) return 0.0;
  return Math.min(LONGTAIL_MIN_SHARE, sel.longtailShare);
}

/** 补位阶段可行性检查（分母 = 加入后的实际花费，即最终报告口径）。 */
function fitsCapsVsSpend(sel: Selection, slot: Slot): string | null {
  const cand = slot.candidate;
  const cost = slotCost(slot);
  if (cost > sel.remaining) return 'over_remaining_budget';
  if (sel.nPosts(cand.kox_id) * cost + cost > SINGLE_KOX_MAX_SHARE * sel.budget) return 'over_single_kox_cap';
  const after = sel.spent + cost;
  const head = sel.headUsd + (HEAD_BUCKETS.has(cand.bucket) ? cost : 0.0);
  if (head > HEAD_MAX_SHARE * after) return 'over_head_cap';
  const longtail = sel.longtailUsd + (LONGTAIL_BUCKETS.has(cand.bucket) ? cost : 0.0);
  if (longtail < longtailTarget(sel) * after - 1e-9) return 'would_worsen_longtail_share';
  const country = (sel.countryUsd.get(cand.country) ?? 0.0) + cost;
  if (country > COUNTRY_MAX_SHARE * after) return 'over_country_cap';
  return null;
}

/** 长尾补足阶段：只看预算、单人上限、地域上限（正在往长尾下限爬，故不看它本身）。 */
function fitsForLongtailRepair(sel: Selection, slot: Slot): string | null {
  const cand = slot.candidate;
  const cost = slotCost(slot);
  if (cost > sel.remaining) return 'over_remaining_budget';
  if (sel.nPosts(cand.kox_id) * cost + cost > SINGLE_KOX_MAX_SHARE * sel.budget) return 'over_single_kox_cap';
  if ((sel.countryUsd.get(cand.country) ?? 0.0) + cost > COUNTRY_MAX_SHARE * (sel.spent + cost)) {
    return 'over_country_cap';
  }
  return null;
}

const fmtUsd0 = (x: number): string => `$${Math.round(x).toLocaleString('en-US')}`;
const fmtPct1 = (x: number): string => `${(x * 100).toFixed(1)}%`;
const fmtPct0 = (x: number): string => `${Math.round(x * 100)}%`;

/** 分层配额修正：把长尾金额占比抬到下限以上。优先加长尾，加不动才退非长尾腾预算。 */
function repairLongtail(sel: Selection, slots: readonly Slot[], trace: string[]): void {
  const longtailSlots = slots.filter((s) => LONGTAIL_BUCKETS.has(s.candidate.bucket));
  if (longtailSlots.length === 0) {
    // 池子里根本没有长尾候选：退掉非长尾内容永远不可能把长尾占比抬起来，
    // 只会把预算白扔掉（极端情况下把整盘方案退成 $0，还让报告里的比例型约束
    // 因为"分母归零"而显示为满足）。这里直接停手并如实记账。
    if (sel.longtailShare < LONGTAIL_MIN_SHARE) {
      trace.push(
        `分层修正：该定向下候选池里没有任何长尾（nano/micro）达人，` +
          `长尾下限 ${fmtPct0(LONGTAIL_MIN_SHARE)} 在物理上不可满足；` +
          `不做无意义的退让（退让只会损失预算而不改善结构），如实记录违规`,
      );
    }
    return;
  }
  let steps = 0;
  let addedTotal = 0;
  while (sel.spent > 0 && sel.longtailShare < LONGTAIL_MIN_SHARE && steps < MAX_REPAIR_STEPS) {
    steps += 1;
    let added = false;
    for (const slot of longtailSlots) {
      const cand = slot.candidate;
      if (sel.nPosts(cand.kox_id) >= slot.index) continue; // 该名额已被取走
      if (sel.nPosts(cand.kox_id) + 1 !== slot.index) continue; // 保持"先买第 n-1 条"
      if (fitsForLongtailRepair(sel, slot) !== null) continue;
      sel.take(slot, 'repair_longtail');
      added = true;
      addedTotal += 1;
      break;
    }
    if (added) continue;
    const droppable = [...sel.candById.entries()].filter(([, c]) => !LONGTAIL_BUCKETS.has(c.bucket)).map(([k]) => k);
    if (droppable.length === 0) {
      trace.push(
        `分层修正：长尾候选与可退让名额都已用尽，长尾占比停在 ${fmtPct1(sel.longtailShare)}` +
          `（低于 ${fmtPct0(LONGTAIL_MIN_SHARE)} 下限，如实记录，不假装满足）`,
      );
      return;
    }
    const worst = argMinByEfficiency(sel, droppable);
    const beforeShare = sel.longtailShare;
    const snap = sel.snapshot();
    const cand = sel.giveBack(worst);
    if (sel.spent <= 0 || sel.longtailShare <= beforeShare + 1e-12) {
      // 退让没有换来任何结构改善（腾出的预算加不进长尾），继续退下去就是纯毁灭价值。
      sel.restore(snap);
      trace.push(
        `分层修正：退让已无法改善长尾占比（停在 ${fmtPct1(sel.longtailShare)}，` +
          `下限 ${fmtPct0(LONGTAIL_MIN_SHARE)}），停止退让并如实记录违规`,
      );
      break;
    }
    trace.push(
      `分层修正：退掉非长尾达人 ${cand.handle}（${cand.bucket}）的 1 条内容` +
        `（${fmtUsd0(cand.cost_usd)}）以腾出长尾名额`,
    );
  }
  if (addedTotal) {
    trace.push(
      `分层修正：追加 ${addedTotal} 条长尾内容，长尾金额占比抬到 ${fmtPct1(sel.longtailShare)}` +
        `（下限 ${fmtPct0(LONGTAIL_MIN_SHARE)}）`,
    );
  }
}

/** min(pool, key=(marginal_efficiency_of_last, kox_id)) 的等价实现。 */
function argMinByEfficiency(sel: Selection, pool: readonly string[]): string {
  let best = pool[0];
  let bestEff = sel.marginalEfficiencyOfLast(best);
  for (const k of pool.slice(1)) {
    const eff = sel.marginalEfficiencyOfLast(k);
    if (eff < bestEff || (eff === bestEff && k < best)) {
      best = k;
      bestEff = eff;
    }
  }
  return best;
}

/** 头部占比修正（分母换成实际花费后可能越线）：退掉边际性价比最低的头部内容。 */
function repairHead(sel: Selection, trace: string[]): void {
  let steps = 0;
  let dropped = 0;
  while (sel.spent > 0 && sel.headShare > HEAD_MAX_SHARE && steps < MAX_REPAIR_STEPS) {
    steps += 1;
    const heads = [...sel.candById.entries()].filter(([, c]) => HEAD_BUCKETS.has(c.bucket)).map(([k]) => k);
    if (heads.length === 0) return;
    const beforeShare = sel.headShare;
    const snap = sel.snapshot();
    sel.giveBack(argMinByEfficiency(sel, heads));
    if (sel.spent <= 0 || sel.headShare >= beforeShare - 1e-12) {
      // 头部占比 = 头部金额 / 总花费；当在选内容**全是头部**时，
      // 退让不改变这个比值（H/T 恒为 1），循环只会一路退到 $0，
      // 然后因为分母归零让 head_max_share 在报告里"满足"——那是用不投放伪装合规。
      sel.restore(snap);
      trace.push(
        `头部配额修正：在选内容全部来自头部（macro/mega），退让无法降低头部占比` +
          `（停在 ${fmtPct1(sel.headShare)}，上限 ${fmtPct0(HEAD_MAX_SHARE)}）；停止退让并如实记录违规`,
      );
      break;
    }
    dropped += 1;
  }
  if (dropped) {
    trace.push(
      `头部配额修正：退掉 ${dropped} 条头部内容，头部金额占比降至 ${fmtPct1(sel.headShare)}` +
        `（上限 ${fmtPct0(HEAD_MAX_SHARE)}）`,
    );
  }
}

/** 地域分散修正：退掉超配国家里边际性价比最低的内容。 */
function repairCountry(sel: Selection, trace: string[]): void {
  let steps = 0;
  const dropped = new Map<string, number>();
  while (sel.spent > 0 && sel.maxCountryShare > COUNTRY_MAX_SHARE && steps < MAX_REPAIR_STEPS) {
    steps += 1;
    let country = '';
    let bestAmt = -Infinity;
    for (const [k, v] of sel.countryUsd) {
      if (v > bestAmt || (v === bestAmt && k > country)) {
        country = k;
        bestAmt = v;
      }
    }
    const pool = [...sel.candById.entries()].filter(([, c]) => c.country === country).map(([k]) => k);
    if (pool.length === 0) return;
    const beforeShare = sel.maxCountryShare;
    const snap = sel.snapshot();
    sel.giveBack(argMinByEfficiency(sel, pool));
    if (sel.spent <= 0 || sel.maxCountryShare >= beforeShare - 1e-12) {
      // 单一国家占比 = 该国金额 / 总花费；若在选内容只来自一个国家，
      // 这个比值恒为 1，退让改善不了任何东西，退到最后是整盘清零 + 报告假合规。
      // 单国候选池（例如只投美国的 brief）会稳定命中这一支。
      sel.restore(snap);
      trace.push(
        `地域分散修正：在选内容集中在单一国家，退让无法降低占比` +
          `（停在 ${fmtPct1(sel.maxCountryShare)}，上限 ${fmtPct0(COUNTRY_MAX_SHARE)}）；停止退让并如实记录违规`,
      );
      break;
    }
    dropped.set(country, (dropped.get(country) ?? 0) + 1);
  }
  for (const [country, n] of dropped) {
    trace.push(
      `地域分散修正：退掉 ${country} 的 ${n} 条内容，单一国家占比降至 ${fmtPct1(sel.maxCountryShare)}` +
        `（上限 ${fmtPct0(COUNTRY_MAX_SHARE)}）`,
    );
  }
}

function structureOk(sel: Selection): boolean {
  return (
    sel.headShare <= HEAD_MAX_SHARE + 1e-6 &&
    sel.longtailShare >= LONGTAIL_MIN_SHARE - 1e-6 &&
    sel.maxCountryShare <= COUNTRY_MAX_SHARE + 1e-6
  );
}

/** 当前状态下未满足的结构约束名（与 validatePlan 的判定口径完全一致）。 */
function structureViolations(sel: Selection): string[] {
  const bad: string[] = [];
  if (sel.headShare > HEAD_MAX_SHARE + 1e-6) bad.push('head_max_share');
  if (sel.longtailShare < LONGTAIL_MIN_SHARE - 1e-6) bad.push('longtail_min_share');
  if (sel.maxCountryShare > COUNTRY_MAX_SHARE + 1e-6) bad.push('country_max_share');
  return bad;
}

/**
 * 修正效果打分，**越小越好**：(未满足的结构约束数, -已花费)。
 *
 * 关键的一条特例：空方案（花费 0）永远是最差的。
 * 因为比例型约束的分母是"实际花费"，把方案退成 $0 会让 head/country 占比变成 0/0 → 0.0，
 * 在报告里显示为"满足"。那不是合规，那是**用不投放伪装合规**，
 * 还会把违规指向错误的约束（只剩长尾下限报红），让运营看不到真正卡住的是哪一条。
 */
function repairScore(sel: Selection): [number, number] {
  if (sel.spent <= 0) return [structureViolations(sel).length + 99, 0.0];
  return [structureViolations(sel).length, -sel.spent];
}

/** (违规数, -花费) 的字典序比较：>0 表示 a 比 b 更差。 */
function worseThan(a: [number, number], b: [number, number]): boolean {
  return a[0] !== b[0] ? a[0] > b[0] : a[1] > b[1];
}

function stateFingerprint(sel: Selection): string {
  const posts = [...sel.posts.entries()].sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return `${pyRound(sel.spent, 6)}|${posts.map(([k, v]) => `${k}:${v}`).join(',')}`;
}

/**
 * 多轮迭代修正到不动点。必须多轮：三条结构约束互相打架 ——
 * 退掉超配国家的内容时很可能同时退掉了长尾，把刚补好的占比又打下去。
 * 顺序是"先上限类（只减钱、单调收敛），再下限类（补钱）"，不再变化即停止，未达标如实记账。
 *
 * 另有一条安全网：**修正不允许把方案变得更差**。若某条配额在该候选池下不可满足，
 * 退让会一路毁灭价值（最坏退成 $0 并让报告假合规），此时回滚到修正前的方案，
 * 如实登记违规——宁可交付一个"结构不达标但说清楚了"的方案，
 * 也不交付一个"什么都没投所以没违规"的空方案。
 */
function repairToFixpoint(sel: Selection, ordered: readonly Slot[], trace: string[]): boolean {
  const baseline = sel.snapshot();
  const baseScore = repairScore(sel);
  const local: string[] = [];
  for (let round = 0; round < MAX_REPAIR_ROUNDS; round += 1) {
    if (structureOk(sel)) {
      trace.push(...local);
      return true;
    }
    const before = stateFingerprint(sel);
    repairCountry(sel, local);
    repairHead(sel, local);
    repairLongtail(sel, ordered, local);
    if (stateFingerprint(sel) === before) break;
  }
  if (structureOk(sel)) {
    trace.push(...local);
    return true;
  }
  if (worseThan(repairScore(sel), baseScore)) {
    sel.restore(baseline);
    trace.push(
      '结构修正在该候选池下无法达标，且退让反而在毁灭已选价值；' +
        '已回滚到修正前的方案，并在 constraints.violations 中如实标出未满足的配额' +
        '（绝不用「退成空方案」来让比例型约束的分母归零、伪装成合规）',
    );
    return false;
  }
  trace.push(...local);
  trace.push(
    '结构修正未能同时满足全部配额（候选池在该定向下的结构性不足），' +
      '已在 constraints.violations 中如实标出，未做任何掩盖',
  );
  return false;
}

/** 把每条约束的实际值/上下限/是否满足写成可断言的报告。 */
export function validatePlan(sel: Selection, strict: boolean): ConstraintReport {
  let maxSingle = 0.0;
  for (const k of sel.candById.keys()) maxSingle = Math.max(maxSingle, sel.amountOf(k));
  const checks: ConstraintCheck[] = [
    {
      name: 'total_budget',
      desc: '总额 ≤ 预算',
      actual: pyRound(sel.spent, 2),
      limit: pyRound(sel.budget, 2),
      satisfied: sel.spent <= sel.budget + 1e-6,
      enforced: true,
    },
    {
      name: 'single_kox_max_share',
      desc: `单达人金额 ≤ 预算的 ${fmtPct0(SINGLE_KOX_MAX_SHARE)}`,
      actual: sel.budget ? pyRound(maxSingle / sel.budget, 4) : 0.0,
      limit: SINGLE_KOX_MAX_SHARE,
      satisfied: maxSingle <= SINGLE_KOX_MAX_SHARE * sel.budget + 1e-6,
      enforced: strict,
    },
    {
      name: 'head_max_share',
      desc: `macro+mega 金额占比 ≤ ${fmtPct0(HEAD_MAX_SHARE)}`,
      actual: pyRound(sel.headShare, 4),
      limit: HEAD_MAX_SHARE,
      satisfied: sel.headShare <= HEAD_MAX_SHARE + 1e-6,
      enforced: strict,
    },
    {
      name: 'longtail_min_share',
      desc: `nano+micro 金额占比 ≥ ${fmtPct0(LONGTAIL_MIN_SHARE)}`,
      actual: pyRound(sel.longtailShare, 4),
      limit: LONGTAIL_MIN_SHARE,
      satisfied: sel.longtailShare >= LONGTAIL_MIN_SHARE - 1e-6,
      enforced: strict,
    },
    {
      name: 'country_max_share',
      desc: `单一国家金额占比 ≤ ${fmtPct0(COUNTRY_MAX_SHARE)}`,
      actual: pyRound(sel.maxCountryShare, 4),
      limit: COUNTRY_MAX_SHARE,
      satisfied: sel.maxCountryShare <= COUNTRY_MAX_SHARE + 1e-6,
      enforced: strict,
    },
  ];
  const utilization = sel.budget ? sel.spent / sel.budget : 0.0;
  return {
    policy: constraintSnapshot(),
    checks,
    all_enforced_satisfied: checks.every((c) => !c.enforced || c.satisfied),
    violations: checks.filter((c) => c.enforced && !c.satisfied).map((c) => c.name),
    utilization: pyRound(utilization, 4),
    utilization_ok: utilization >= MIN_UTILIZATION_TARGET,
  };
}

function mix(sel: Selection, key: 'bucket' | 'country' | 'platform'): Record<string, number> {
  const agg = new Map<string, number>();
  for (const [kid, c] of sel.candById) {
    const k = c[key];
    agg.set(k, (agg.get(k) ?? 0.0) + sel.amountOf(kid));
  }
  const entries = [...agg.entries()].sort((a, b) => -a[1] - -b[1]);
  const out: Record<string, number> = {};
  for (const [k, v] of entries) out[k] = pyRound(sel.share(v), 6);
  return out;
}

export interface AllocateOptions {
  strategy?: Strategy;
  budgetUsd?: number | null;
  skipped?: readonly SkippedRecord[];
  maxPosts?: number;
  decay?: number;
}

/**
 * 把候选池分配成预算方案。
 * `koxpilot` = 边际性价比贪心 + 结构约束修正；`followers` = 反事实基线（只受总预算约束）。
 */
export function allocate(
  candidates: readonly Candidate[],
  spec: CampaignSpec,
  options: AllocateOptions = {},
): BudgetPlan {
  const strategy: Strategy = options.strategy ?? 'koxpilot';
  const [strictFromTable, basis] = STRATEGIES[strategy];
  const maxPosts = options.maxPosts ?? MAX_POSTS_PER_KOX;
  const decay = options.decay ?? POST_MARGINAL_DECAY;
  const skipped = [...(options.skipped ?? [])];
  const pool = [...candidates];
  const budget = Number(options.budgetUsd ?? spec.budget_usd);
  const sel = new Selection(budget, decay, basis);
  const trace: string[] = [];
  const strict = strictFromTable;
  const slots = buildSlots(pool, maxPosts, decay, basis);
  const purchaseModel = { max_posts_per_kox: maxPosts, post_marginal_decay: decay };

  if (budget <= 0 || pool.length === 0) {
    return {
      campaign_id: spec.campaign_id,
      strategy,
      budget_usd: pyRound(budget, 2),
      spent_usd: 0.0,
      utilization: 0.0,
      n_selected: 0,
      n_posts: 0,
      purchase_model: purchaseModel,
      candidate_pool: pool.length,
      n_price_estimated: 0,
      est_total_views: 0,
      est_effective_views: 0,
      est_total_engagements: 0,
      est_cpm_usd: 0,
      est_cpe_usd: 0,
      tier_mix: {},
      country_mix: {},
      platform_mix: {},
      constraints: validatePlan(sel, strict),
      trace: ['预算为 0 或候选池为空，未做任何分配'],
      selected: [],
      skipped,
    };
  }

  let ordered: Slot[] = [];
  if (strict) {
    ordered = [...slots].sort(cmpEfficiency);
    for (const slot of ordered) {
      if (fitsCapsVsBudget(sel, slot) === null) sel.take(slot, 'greedy');
    }
    let totalPosts = 0;
    for (const v of sel.posts.values()) totalPosts += v;
    // 排序依据要写进 trace：本轮起浏览器引擎也实现了第三臂（同一套结构约束、排序换成
    // "名义曝光/报价"），所以这句话必须点明"这一臂按什么排"，否则两臂的 trace 长得一样。
    const ruler = basis === BASIS_VALUE ? '质量加权价值/报价' : '名义曝光/报价（不含门禁与真实性信号）';
    trace.push(
      `边际性价比贪心（排序依据：${ruler}）：候选 ${pool.length} 人 / ${slots.length} 个内容名额，` +
        `选入 ${sel.candById.size} 人共 ${totalPosts} 条，花费 ${fmtUsd0(sel.spent)}` +
        `（长尾 ${fmtPct1(sel.longtailShare)}，头部 ${fmtPct1(sel.headShare)}）`,
    );
    repairToFixpoint(sel, ordered, trace);
    let filled = 0;
    for (const slot of ordered) {
      if (sel.nPosts(slot.candidate.kox_id) + 1 !== slot.index) continue;
      if (fitsCapsVsSpend(sel, slot) === null) {
        sel.take(slot, 'fill');
        filled += 1;
      }
    }
    if (filled) {
      trace.push(`约束内补位：追加 ${filled} 条内容，预算利用率 ${fmtPct1(sel.spent / budget)}`);
    }
    repairToFixpoint(sel, ordered, trace);
  } else {
    for (const slot of [...slots].sort(cmpFollowers)) {
      if (sel.nPosts(slot.candidate.kox_id) + 1 !== slot.index) continue;
      if (slotCost(slot) <= sel.remaining) sel.take(slot, 'baseline');
    }
    let totalPosts = 0;
    for (const v of sel.posts.values()) totalPosts += v;
    trace.push(
      `基线策略：按粉丝量降序买内容直到花完预算，共 ${sel.candById.size} 人 ` +
        `${totalPosts} 条，花费 ${fmtUsd0(sel.spent)}（不做门禁过滤、不做结构约束）`,
    );
  }

  const allocations: Allocation[] = [];
  for (const [kid, c] of sel.candById) {
    const n = sel.posts.get(kid) as number;
    const eq = effectivePosts(n, decay);
    allocations.push({
      kox_id: c.kox_id,
      handle: c.handle,
      platform: c.platform,
      country: c.country,
      bucket: c.bucket,
      amount_usd: n * c.cost_usd,
      posts: n,
      effective_posts: pyRound(eq, 4),
      price_estimated: c.price_estimated,
      est_views: c.avg_views * eq,
      est_effective_views: c.avg_views * eq * c.authenticity_discount,
      est_engagements: estEngagements(c) * eq,
      value_score: c.value * eq,
      efficiency: c.efficiency,
      fit_score: c.fit_score,
      authenticity_discount: c.authenticity_discount,
      audience_match: c.audience_match,
      kpi_weight: c.kpi_weight,
      verdict: c.verdict,
      picked_by: sel.pickedBy.get(kid) ?? 'greedy',
    });
  }
  allocations.sort((a, b) => {
    if (a.value_score !== b.value_score) return -a.value_score - -b.value_score;
    return a.kox_id < b.kox_id ? -1 : a.kox_id > b.kox_id ? 1 : 0;
  });

  let views = 0.0;
  let effViews = 0.0;
  let engagements = 0.0;
  for (const a of allocations) {
    views += a.est_views;
    effViews += a.est_effective_views;
    engagements += a.est_engagements;
  }
  let nPosts = 0;
  for (const v of sel.posts.values()) nPosts += v;

  return {
    campaign_id: spec.campaign_id,
    strategy,
    budget_usd: pyRound(budget, 2),
    spent_usd: pyRound(sel.spent, 2),
    utilization: budget ? pyRound(sel.spent / budget, 4) : 0.0,
    n_selected: allocations.length,
    n_posts: nPosts,
    purchase_model: purchaseModel,
    candidate_pool: pool.length,
    n_price_estimated: allocations.filter((a) => a.price_estimated).length,
    est_total_views: pyRound(views, 0),
    est_effective_views: pyRound(effViews, 0),
    est_total_engagements: pyRound(engagements, 0),
    est_cpm_usd: views > 0 ? pyRound(sel.spent / (views / 1000.0), 3) : 0,
    est_cpe_usd: engagements > 0 ? pyRound(sel.spent / engagements, 4) : 0,
    tier_mix: mix(sel, 'bucket'),
    country_mix: mix(sel, 'country'),
    platform_mix: mix(sel, 'platform'),
    constraints: validatePlan(sel, strict),
    trace,
    selected: allocations,
    skipped,
  };
}
