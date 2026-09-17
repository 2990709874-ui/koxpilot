/**
 * 阈值表读取器（对应 Python `gates/thresholds.py` 的查询侧）。
 *
 * **TS 侧刻意不实现标定**（没有 `calibrate`）：阈值只有一个权威来源 —— Python 落盘的
 * `output/thresholds.json`。前端重新标定一遍等于制造第二套真值，双实现一致性就失去意义。
 * 因此本类只做三件事：三级回退查询、分位数网格反查、敏感性扫描用的等比缩放。
 */

import { policyFromSnapshot } from './policy.ts';
import type { GatePolicy } from './policy.ts';

export interface SignalEntry {
  n?: number;
  source?: string;
  method?: string;
  median?: number;
  grid?: number[];
  /** pXX（鲁棒估计，判定用）与 pXX_empirical（经验值，仅对照）。 */
  [key: string]: number | string | number[] | undefined;
}

export interface ThresholdsPayload {
  meta?: Record<string, unknown>;
  policy?: Partial<GatePolicy>;
  groups?: Record<string, Record<string, SignalEntry>>;
  platform?: Record<string, Record<string, SignalEntry>>;
  global?: Record<string, SignalEntry>;
}

const MISSING: SignalEntry = { n: 0, source: 'missing', grid: [] };

export class Thresholds {
  readonly groups: Record<string, Record<string, SignalEntry>>;
  readonly platform: Record<string, Record<string, SignalEntry>>;
  readonly global: Record<string, SignalEntry>;
  readonly meta: Record<string, unknown>;
  readonly policy: GatePolicy;
  readonly rawPolicy: Partial<GatePolicy>;

  constructor(payload: ThresholdsPayload = {}) {
    this.groups = payload.groups ?? {};
    this.platform = payload.platform ?? {};
    this.global = payload.global ?? {};
    this.meta = payload.meta ?? {};
    this.rawPolicy = payload.policy ?? {};
    this.policy = policyFromSnapshot(payload.policy);
  }

  static fromDict(payload: ThresholdsPayload): Thresholds {
    return new Thresholds(payload);
  }

  /** 取某组某信号的阈值块；组不存在时按 platform -> global 回退（与 Python 同序）。 */
  signal(group: string, name: string): SignalEntry {
    const g = this.groups[group]?.[name];
    if (g !== undefined) return g;
    const plat = group.split('|', 1)[0];
    const p = this.platform[plat]?.[name];
    if (p !== undefined) return p;
    return this.global[name] ?? MISSING;
  }

  /** 取具体分位数值，如 value('tiktok|micro', 'engagement_rate', 'p95')。 */
  value(group: string, name: string, qLabel: string): number | null {
    const v = this.signal(group, name)[qLabel];
    return typeof v === 'number' && Number.isFinite(v) ? v : null;
  }

  source(group: string, name: string): string {
    return String(this.signal(group, name).source ?? 'missing');
  }

  /**
   * 用 p0..p100 分位数网格反查 x 的经验百分位（线性插值）。
   * 这是连续异常分 fraud_score 与"实际值 vs 同组分布"迷你图的共同基础：
   * 把绝对值换成"在同组里排第几"，无量纲、跨组可比，也不必把 5000 条原始值打进前端 bundle。
   */
  percentileRank(group: string, name: string, x: number): number {
    const grid = (this.signal(group, name).grid as number[] | undefined) ?? [];
    if (grid.length === 0) return 0.5;
    if (x <= grid[0]) return 0.0;
    if (x >= grid[grid.length - 1]) return 1.0;
    const step = 1.0 / (grid.length - 1);
    for (let i = 0; i < grid.length - 1; i += 1) {
      const lo = grid[i];
      const hi = grid[i + 1];
      if (lo <= x && x <= hi) {
        const frac = hi === lo ? 0.0 : (x - lo) / (hi - lo);
        return Math.min(1.0, Math.max(0.0, (i + frac) * step));
      }
    }
    return 1.0;
  }

  /** 同组分布网格（前端抽屉画迷你图直接读它）。 */
  grid(group: string, name: string): number[] {
    return ((this.signal(group, name).grid as number[] | undefined) ?? []).slice();
  }

  /**
   * 把指定信号的判定阈值统一乘以 factor，返回新对象（敏感性扫描用）。
   * 方向性是刻意的：对上尾规则（> P95）放大 = 放松，对下尾规则（< P05）放大 = 收紧，
   * 所以单个 factor 就能同时考察两个方向的稳健性。`grid` 不缩放（它不参与判定）。
   */
  scaled(factor: number, signals?: Iterable<string>): Thresholds {
    const target = new Set(signals ?? Object.keys(this.global));
    const scaleBlock = (block: Record<string, SignalEntry>): Record<string, SignalEntry> => {
      const out: Record<string, SignalEntry> = {};
      for (const [name, entry] of Object.entries(block)) {
        const next: SignalEntry = { ...entry };
        if (target.has(name)) {
          for (const [key, val] of Object.entries(entry)) {
            if (key.startsWith('p') && typeof val === 'number') next[key] = val * factor;
          }
        }
        out[name] = next;
      }
      return out;
    };
    return new Thresholds({
      groups: Object.fromEntries(Object.entries(this.groups).map(([g, b]) => [g, scaleBlock(b)])),
      platform: Object.fromEntries(Object.entries(this.platform).map(([p, b]) => [p, scaleBlock(b)])),
      global: scaleBlock(this.global),
      meta: { ...this.meta, scaled_by: factor, scaled_signals: [...target].sort() },
      policy: this.rawPolicy,
    });
  }
}
