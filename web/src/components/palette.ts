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

/* ------------------------------------------------------------------ */
/* 亮色改版追加的色位（只追加，不改上面已有的导出名与值）              */
/* ------------------------------------------------------------------ */

/**
 * 图表骨架的补充色位。
 * 为什么需要：暗色版里「白色游标 + 白色斜纹 + 白描边」都靠白色做前景，
 * 亮色下白色变成背景色，这几处必须换成深色/灰色前景。
 */
export const CHROME = {
  /** 压在浅色渐变条上的游标：白底不可见，改深靛蓝 */
  cursor: '#1e293b',
  /** 危险/慎用区的斜纹填充线（原为白色半透明） */
  hatch: 'rgba(100,116,139,0.55)',
  /** 非激活柱的描边：淡灰而不是淡白 */
  separatorMuted: 'rgba(148,163,184,0.55)',
  /** 条形轨道底（原 bg-white/[0.07]） */
  track: '#e2e8f0',
} as const;

/**
 * RankStrip 的三段分位渐变（好→注意→异常）。
 * 亮色下用各色系 100 档实色，不用蒙版，避免脏。
 */
export const RANK_GRADIENT = ['rgba(5,150,105,0.16)', 'rgba(217,119,6,0.18)', 'rgba(225,29,72,0.20)'] as const;

/** 警示角标（! 圆点）：底 + 上面的字。 */
export const WARN_BADGE = { bg: '#d97706', fg: '#ffffff' } as const;

/**
 * 暗色遗留色 → 亮色等价色的映射。
 *
 * 为什么需要：颜色不只写在 charts.tsx 里，各 tab 也以 props 传入了一批
 * 暗色主题的霓虹值（`#22d3ee`、`#34d399`、`#fbbf24`、`#f87171` …）。
 * 这些值在白底上对比度普遍不足（cyan 尤其），但 tab 文件本轮由他人并行改动，
 * 不能在那边动手。所以在图表入口统一做一次「主题适配」：
 * 遇到已知的暗色遗留值就换成语义等价的亮色值，未知值原样透传。
 * 这样既不改数据与语义，也不会在图表里出现读不清的霓虹色。
 */
const LIGHT_EQUIV: Record<string, string> = {
  '#22d3ee': SERIES.secondary, // cyan-400 → live-500
  '#38bdf8': SERIES.secondary,
  '#67e8f9': SERIES.secondary,
  '#34d399': VERDICT.pass,
  '#4ade80': VERDICT.pass,
  '#fbbf24': VERDICT.review,
  '#fcd34d': VERDICT.review,
  '#f87171': VERDICT.reject,
  '#fb7185': VERDICT.reject,
  '#f472b6': '#db2777',
  '#818cf8': TOKEN.completion,
  '#c084fc': TOKEN.reasoning,
  '#a78bfa': TOKEN.reasoning,
  '#64748b': SERIES.baseline,
  '#94a3b8': SERIES.baseline,
  '#e2e8f0': AXIS.axis,
  '#fff': AXIS.focus,
  '#ffffff': AXIS.focus,
};

/** 把可能来自暗色主题的颜色值适配到亮色。未知值原样返回。 */
export function onLight(color: string | undefined | null): string {
  if (!color) return SERIES.primary;
  return LIGHT_EQUIV[color.trim().toLowerCase()] ?? color;
}

/**
 * 文字用色的可读性兜底。
 *
 * 为什么需要：参考线/图例的文字沿用线条颜色，而线条色里有 slate-400 这类
 * 在白底上对比度不足（约 2.1:1）的浅色。图形上浅一点没问题，文字不行。
 * 这里把已知的过浅色替换成同色系深两档的值，其余原样返回。
 */
const TEXT_DARKEN: Record<string, string> = {
  '#94a3b8': '#475569',
  '#cbd5e1': '#475569',
  '#bae6fd': '#0369a1',
  '#7dd3fc': '#0369a1',
  '#38bdf8': '#0284c7',
  '#a1a1aa': '#52525b',
};

/** 取一个在白底上读得清的文字色（先做亮色适配，再做过浅兜底）。 */
export function readableText(color: string | undefined | null): string {
  const c = onLight(color);
  return TEXT_DARKEN[c.trim().toLowerCase()] ?? c;
}
