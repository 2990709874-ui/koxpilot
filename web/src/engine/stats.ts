/**
 * 统计原语（对应 Python `src/koxpilot/stats.py`）。
 *
 * 双实现一致性的关键在这一层：Python 与 TS 必须做**逐位相同**的浮点运算，
 * 否则 authenticity_score 在 0.5 的判定边界上会出现零星错判，
 * 而这种差异排查起来非常昂贵。因此这里刻意做了三件事：
 *  1. `pyRound` 复现 CPython `round(x, n)` 的"精确值 + 银行家进位"语义
 *     （JS 原生 `toFixed` 在恰好压线时是 half-up，与 Python 不同）；
 *  2. 求和一律保持与 Python 相同的**累加顺序**（builtin sum = 从左到右）；
 *  3. 需要 `math.fsum`（精确求和）的地方用 Neumaier 补偿求和近似，并在注释里标注。
 */

const EPS = 1e-12;

/** 除零保护除法（对应 `safe_div`）。 */
export function safeDiv(numerator: number, denominator: number, dflt = 0.0): number {
  if (denominator === null || denominator === undefined || Math.abs(denominator) < EPS) return dflt;
  return numerator / denominator;
}

/**
 * CPython `round(x, ndigits)` 的等价实现。
 *
 * 非压线情况下 `Number(x.toFixed(n))` 与 Python 结果同为"最接近该十进制值的 double"，二者一致；
 * 唯一分歧是**精确压线**（如 round(0.03125, 4)：Python→0.0312 取偶，toFixed→0.0313 进位）。
 * 双精度是二进制小数，十进制展开有限，因此用 20 位定点串即可判定是否精确压线。
 */
export function pyRound(x: number, n = 0): number {
  if (!Number.isFinite(x)) return x;
  const neg = x < 0;
  const a = Math.abs(x);
  if (a >= 1e15) return x; // 该量级下 double 已无小数精度，无需处理
  const s = a.toFixed(20);
  const dot = s.indexOf('.');
  const intPart = s.slice(0, dot);
  const dec = s.slice(dot + 1);
  const tail = dec.slice(n);
  let out: number;
  const isTie = tail.charAt(0) === '5' && /^0*$/.test(tail.slice(1));
  if (isTie) {
    const keep = dec.slice(0, n);
    const truncated = Number(n > 0 ? `${intPart}.${keep}` : intPart);
    const lastDigit = Number(n > 0 ? keep.charAt(n - 1) || '0' : intPart.charAt(intPart.length - 1));
    if (lastDigit % 2 === 0) {
      out = truncated; // 已是偶数 -> 舍
    } else {
      out = Number((truncated + Math.pow(10, -n)).toFixed(n)); // 进位到偶数
    }
  } else {
    out = Number(a.toFixed(n));
  }
  return neg ? -out : out;
}

/** Neumaier 补偿求和，近似 Python `math.fsum`（只用于 mean 的退化分支）。 */
export function fsum(values: readonly number[]): number {
  let s = 0.0;
  let c = 0.0;
  for (const v of values) {
    const t = s + v;
    if (Math.abs(s) >= Math.abs(v)) c += s - t + v;
    else c += v - t + s;
    s = t;
  }
  return s + c;
}

export function mean(values: readonly number[]): number {
  if (values.length === 0) return 0.0;
  return fsum(values) / values.length;
}

/**
 * 线性插值分位数（等价 numpy 'linear' / R type-7），与 Python `quantile` 逐步对应。
 * 入参不要求有序，函数内部排序且不修改入参。
 */
export function quantile(values: readonly number[], q: number): number {
  if (!(q >= 0.0 && q <= 1.0)) throw new Error(`q must be in [0, 1], got ${q}`);
  if (values.length === 0) return 0.0;
  const ordered = [...values].sort((a, b) => a - b);
  const n = ordered.length;
  if (n === 1) return ordered[0];
  const h = (n - 1) * q;
  const lo = Math.floor(h);
  const hi = Math.ceil(h);
  if (lo === hi) return ordered[lo];
  const frac = h - lo;
  return ordered[lo] * (1.0 - frac) + ordered[hi] * frac;
}

export function median(values: readonly number[]): number {
  return quantile(values, 0.5);
}

/** 中位数绝对偏差（G1.5 突刺检测的鲁棒尺度）。 */
export function mad(values: readonly number[]): number {
  if (values.length === 0) return 0.0;
  const med = median(values);
  return median(values.map((v) => Math.abs(v - med)));
}

/**
 * 鲁棒 z-score：`z = (x - median) / (1.4826 · MAD)`。
 * 1.4826 = 1/Φ⁻¹(0.75)，作用是让 MAD 在正态下与 σ 同尺度，
 * 这样 SPEC 的 "z > 2.5" 与常规 σ 语义可比。
 */
export function robustZscores(values: readonly number[]): number[] {
  if (values.length === 0) return [];
  const med = median(values);
  let scale = 1.4826 * mad(values);
  if (scale < EPS) scale = mean(values.map((v) => Math.abs(v - med)));
  if (scale < EPS) return values.map(() => 0.0);
  return values.map((v) => (v - med) / scale);
}

/** 集合 Jaccard；两个空集定义为 1.0（都没声明品类不算冲突）。 */
export function jaccard(a: Iterable<string>, b: Iterable<string>): number {
  const sa = new Set(a);
  const sb = new Set(b);
  if (sa.size === 0 && sb.size === 0) return 1.0;
  const union = new Set([...sa, ...sb]);
  if (union.size === 0) return 1.0;
  let inter = 0;
  for (const v of sa) if (sb.has(v)) inter += 1;
  return inter / union.size;
}

/** ROC-AUC（Mann–Whitney U 秩和 + 并列秩平均修正），供评测页在浏览器里重算。 */
export function rocAuc(scores: readonly number[], labels: readonly number[]): number {
  if (scores.length !== labels.length) throw new Error('scores/labels length mismatch');
  const n = scores.length;
  const nPos = labels.reduce((acc, y) => acc + (y === 1 ? 1 : 0), 0);
  const nNeg = n - nPos;
  if (nPos === 0 || nNeg === 0) return 0.5;
  const order = Array.from({ length: n }, (_, i) => i).sort((i, j) => scores[i] - scores[j]);
  const ranks = new Array<number>(n).fill(0);
  let i = 0;
  while (i < n) {
    let j = i;
    while (j + 1 < n && scores[order[j + 1]] === scores[order[i]]) j += 1;
    const avgRank = (i + j) / 2.0 + 1.0;
    for (let k = i; k <= j; k += 1) ranks[order[k]] = avgRank;
    i = j + 1;
  }
  const rPos = fsum(ranks.filter((_, idx) => labels[idx] === 1));
  return (rPos - (nPos * (nPos + 1)) / 2.0) / (nPos * nNeg);
}

export interface PRF1 {
  tp: number;
  fp: number;
  fn: number;
  precision: number;
  recall: number;
  f1: number;
}

/** 由混淆计数算 P/R/F1，分母为 0 的格子按 0 报（不隐藏）。 */
export function prf1(tp: number, fp: number, fn: number): PRF1 {
  const precision = safeDiv(tp, tp + fp);
  const recall = safeDiv(tp, tp + fn);
  const f1 = safeDiv(2 * precision * recall, precision + recall);
  return { tp, fp, fn, precision: pyRound(precision, 4), recall: pyRound(recall, 4), f1: pyRound(f1, 4) };
}
