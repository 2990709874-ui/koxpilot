/**
 * 图表调色板。
 *
 * 为什么单独抽一个文件：原版把 `#22d3ee`、`rgba(255,255,255,0.07)` 这类暗色值
 * 直接写死在 charts.tsx 的 20 多处 SVG 属性里。亮色化时这些值全部失效
 * （白底上 rgba(255,255,255,0.07) 的网格线等于不存在），而且散落各处改不干净。
 * 所以统一收口到这里：组件只引用语义名，换主题只改这一个文件。
 */

/** 数据系列色：按「第一条是对照/基线，后面是我方」的顺序排。 */
export const SERIES = {
  /** 基线 / 对照臂：中性灰，视觉上要弱于我方 */
  baseline: '#94a3b8',
  /** 我方主系列 */
  primary: '#4f46e5',
  /** 我方次系列 */
  secondary: '#0ea5e9',
  /** 第三臂（diversified_no_gate）：紫，和前两条都区分开 */
  third: '#a855f7',
} as const;

/** 判定三态。与 tailwind verdict.* 保持一致。 */
export const VERDICT = {
  pass: '#059669',
  review: '#d97706',
  reject: '#e11d48',
} as const;

/** 语义色：好 / 警告 / 坏 / 中性。 */
export const TONE = {
  good: '#059669',
  warn: '#d97706',
  bad: '#e11d48',
  neutral: '#64748b',
} as const;

/** token 用量分解（prompt / completion / reasoning）。 */
export const TOKEN = {
  prompt: '#0ea5e9',
  completion: '#6366f1',
  reasoning: '#a855f7',
} as const;

/** 达人分层（nano → mega），单色系递进，保证打印和色盲下也能排序。 */
export const TIER = ['#bae6fd', '#7dd3fc', '#38bdf8', '#0284c7', '#075985'] as const;

/** 定性分类用的分类色（互不相邻，便于区分）。 */
export const CATEGORICAL = [
  '#4f46e5',
  '#0ea5e9',
  '#059669',
  '#d97706',
  '#e11d48',
  '#a855f7',
  '#0d9488',
  '#c2410c',
] as const;

/**
 * SVG 骨架色。亮色下网格线要「淡但可见」——
 * 太淡（<0.06）在白底上消失，太深会盖过数据。实测 slate-200 一档最稳。
 */
export const AXIS = {
  /** 水平参考网格线 */
  grid: 'rgba(148,163,184,0.28)',
  /** 坐标轴 / 零线：比网格线明显一档 */
  axis: 'rgba(100,116,139,0.55)',
  /** 刻度线 */
  tick: 'rgba(100,116,139,0.45)',
  /** 轴标签文字 */
  label: '#64748b',
  /** 数据标签文字（压在图形上方） */
  value: '#0f172a',
  /** 柱/点的描边：亮底上需要一圈白描边把相邻色块分开 */
  separator: '#ffffff',
  /** 强调描边（选中态） */
  focus: '#4f46e5',
  /** 基线区域填充（零线到基线之间） */
  baselineBand: 'rgba(148,163,184,0.10)',
  /** 压在图上的说明标签底色 */
  calloutBg: 'rgba(255,255,255,0.92)',
  calloutBorder: 'rgba(148,163,184,0.45)',
} as const;

/**
 * 混淆矩阵热力色。
 * 对角线（判对）走绿，非对角（判错）走红，透明度表示密度。
 * 亮色下透明度上限要压到 0.75，否则文字读不出来。
 */
export function heatCell(alpha: number, diagonal: boolean): string {
  const a = Math.max(0, Math.min(0.75, alpha));
  return diagonal ? `rgba(5,150,105,${a})` : `rgba(225,29,72,${a * 0.85})`;
}

/**
 * F1 热力图色标（0 → 1）。
 * 亮色下从浅灰蓝渐变到深靛蓝：低分浅、高分深，
 * 这样「哪一格是弱项」用明度就能扫出来，不依赖色相。
 */
export function f1Scale(v: number): string {
  const t = Math.max(0, Math.min(1, v));
  // 浅 #eef2ff → 深 #3730a3
  const lerp = (a: number, b: number): number => Math.round(a + (b - a) * t);
  return `rgb(${lerp(238, 55)},${lerp(242, 48)},${lerp(255, 163)})`;
}

/** 给定背景明度，返回压在上面该用深字还是白字。 */
export function textOn(bgHexOrRgb: string): string {
  const m = bgHexOrRgb.match(/\d+/g);
  if (!m || m.length < 3) return '#0f172a';
  const [r, g, b] = m.slice(0, 3).map(Number);
  // 相对亮度（sRGB 近似）
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.6 ? '#0f172a' : '#ffffff';
}
