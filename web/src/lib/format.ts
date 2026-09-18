/**
 * 展示层格式化工具。刻意集中在一个文件里：
 * 页面上任何"$79,719 / 32.5% / 4.7M"这类字样都必须经过这里，
 * 避免各处自己拼字符串时把口径写歪（例如一处按预算算占比、另一处按花费算）。
 */

/** −0 归一成 0：`Math.round(-0.4)` 会给 -0，直接格式化会印出 "$-0" 这种没人看得懂的数。 */
const noNegZero = (x: number): number => (x === 0 ? 0 : x);

/** 四舍五入后全是 0 的负数（-0.04% → "-0.0%"）同样按 0 印：页面上"负的零"只会让人以为算错了。 */
const dropZeroSign = (s: string): string => (/^-0(\.0+)?$/.test(s) ? s.slice(1) : s);

export const usd0 = (x: number | null | undefined): string =>
  x === null || x === undefined || !Number.isFinite(x) ? '—' : `$${noNegZero(Math.round(x)).toLocaleString('en-US')}`;

export const usd2 = (x: number | null | undefined): string =>
  x === null || x === undefined || !Number.isFinite(x)
    ? '—'
    : `$${noNegZero(x).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const pct1 = (x: number | null | undefined, digits = 1): string =>
  x === null || x === undefined || !Number.isFinite(x) ? '—' : `${dropZeroSign((noNegZero(x) * 100).toFixed(digits))}%`;

export const signedPct1 = (x: number | null | undefined, digits = 1): string =>
  x === null || x === undefined || !Number.isFinite(x)
    ? '—'
    : `${x >= 0 ? '+' : ''}${dropZeroSign((noNegZero(x) * 100).toFixed(digits))}%`;

export const int0 = (x: number | null | undefined): string =>
  x === null || x === undefined || !Number.isFinite(x) ? '—' : noNegZero(Math.round(x)).toLocaleString('en-US');

/** 1.2万/4.7M 这类紧凑写法，只用于图表轴与徽标，正文一律给完整数字。 */
export function compact(x: number | null | undefined, digits = 1): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '—';
  const abs = Math.abs(x);
  if (abs >= 1e9) return `${(x / 1e9).toFixed(digits)}B`;
  if (abs >= 1e6) return `${(x / 1e6).toFixed(digits)}M`;
  if (abs >= 1e3) return `${(x / 1e3).toFixed(digits)}K`;
  return `${Math.round(x)}`;
}

export const fixed = (x: number | null | undefined, digits = 3): string =>
  x === null || x === undefined || !Number.isFinite(x) ? '—' : x.toFixed(digits);

export const ms = (x: number | null | undefined): string =>
  x === null || x === undefined || !Number.isFinite(x) ? '—' : x < 10 ? `${x.toFixed(1)}ms` : `${Math.round(x)}ms`;

export const VERDICT_LABEL: Record<string, string> = { pass: '直接可投', review: '待人核', reject: '拒绝' };
export const VERDICT_COLOR: Record<string, string> = {
  pass: 'text-emerald-700 border-emerald-300 bg-emerald-50',
  review: 'text-amber-700 border-amber-300 bg-amber-50',
  reject: 'text-rose-700 border-rose-300 bg-rose-50',
};
/** 判定色的 hex 版（图表用）。亮色改版后与 palette.VERDICT 保持一致，白底上对比度足够。 */
export const VERDICT_HEX: Record<string, string> = { pass: '#059669', review: '#d97706', reject: '#e11d48' };

export const BUCKET_LABEL: Record<string, string> = {
  nano: 'nano 1K–10K',
  micro: 'micro 10K–100K',
  mid: 'mid 100K–500K',
  macro: 'macro 500K–2M',
  mega: 'mega 2M+',
};

export const SKIP_REASON_LABEL: Record<string, string> = {
  platform_off_target: '平台不在投放清单',
  category_off_target: '品类不沾目标及相邻品类',
  market_off_target: '既非目标市场本地号，受众里也没有目标市场',
  gate_review: '门禁判 review（待人核，默认不自动分配）',
  gate_reject: '门禁判 reject',
  avg_views_missing: '平均播放缺失（不当 0 参与排序）',
  price_unavailable: '报价缺失且同组 CPM 也无法估算',
  no_gate_result: '没有对应门禁结果',
};

export const PICKED_BY_LABEL: Record<string, string> = {
  greedy: '边际性价比贪心',
  fill: '约束内补位',
  repair_longtail: '长尾配额修正',
  baseline: '基线按粉丝量',
};

/** 稳定的确定性配色（按字符串哈希取色），用于国家/平台等无序类别。 */
export function hashHue(key: string): number {
  let h = 0;
  for (let i = 0; i < key.length; i += 1) h = (h * 31 + key.charCodeAt(i)) % 360;
  return h;
}

export function catColor(key: string, sat = 55, light = 58): string {
  return `hsl(${hashHue(key)} ${sat}% ${light}%)`;
}
