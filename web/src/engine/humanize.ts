/**
 * human_text 格式化工具（对应 Python `gates/humanize.py`）。
 *
 * 证据链的价值在于"客户问为什么不选他，能当场答"，所以每条 reason 的文案都必须同时含
 * **实际值 + 同组阈值 + 业务含义**。本模块只做格式化，不做判定。
 *
 * 注意 `fmt`：Python 的 `format(x, '.1f')` 在精确压线时是银行家进位，而 JS `toFixed` 是
 * 半数进位。为了让 TS 与 Python 生成**逐字符相同**的文案（便于 diff 证据链），这里统一走 pyRound。
 */

import { pyRound } from './stats.ts';
import { BUCKET_BOUNDS, CATEGORY_ZH, PLATFORM_LABEL } from './taxonomy.ts';
import type { Bucket } from './taxonomy.ts';

export const BUCKET_LABEL: Record<string, string> = {
  nano: 'nano（1k–10k 粉）',
  micro: 'micro（10k–100k 粉）',
  mid: 'mid（100k–500k 粉）',
  macro: 'macro（500k–2M 粉）',
  mega: 'mega（2M+ 粉）',
};

function fmt(x: number, digits: number): string {
  return pyRound(x, digits).toFixed(digits);
}

/** `tiktok|micro` -> `TikTok · micro（10k–100k 粉）`。 */
export function groupLabel(groupKey: string): string {
  const idx = groupKey.indexOf('|');
  const platform = idx < 0 ? groupKey : groupKey.slice(0, idx);
  const bucket = idx < 0 ? '' : groupKey.slice(idx + 1);
  return `${PLATFORM_LABEL[platform] ?? platform} · ${BUCKET_LABEL[bucket] ?? bucket}`;
}

export function pct(x: number | null | undefined, digits = 1): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '缺失';
  return `${fmt(x * 100, digits)}%`;
}

export function num(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '缺失';
  if (Math.abs(x) >= 10000) {
    // Python f"{x:,.0f}"：千分位 + 0 位小数
    return pyRound(x, 0).toLocaleString('en-US', { maximumFractionDigits: 0 });
  }
  return fmt(x, digits);
}

export function ratio(x: number | null | undefined, digits = 3): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '缺失';
  return fmt(x, digits);
}

export function cats(items: readonly string[] | null | undefined): string {
  if (!items || items.length === 0) return '（无）';
  return items.map((c) => CATEGORY_ZH[c] ?? c).join('/');
}

/** 阈值来源的中文说明，直接进 human_text 尾巴，便于追问时定位到哪一级分位数。 */
export function sourceLabel(source: string): string {
  if (source.startsWith('group:')) return `阈值来源：同组样本分位数（${source.slice(6)}）`;
  if (source.startsWith('platform:')) return `阈值来源：平台级分位数回退（${source.slice(9)}，该细分组样本不足）`;
  if (source.startsWith('fallback:')) return `阈值来源：回退（${source.slice(9)}，该细分组样本不足 60）`;
  if (source === 'global' || source.startsWith('global')) return '阈值来源：全库分位数回退';
  return `阈值来源：${source}`;
}

export function bucketSpan(bucket: string): string {
  const [lo, hi] = BUCKET_BOUNDS[bucket as Bucket] ?? [0, 0];
  return `${lo.toLocaleString('en-US')}–${hi.toLocaleString('en-US')}`;
}

/** Python `f"{x:g}"` 的常用子集（用于 G3 文案里的倍率显示）。 */
export function gFormat(x: number): string {
  return String(Number(x.toPrecision(6)));
}
