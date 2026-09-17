import React from 'react';
import { catColor, compact, fixed, int0, pct1 } from '../lib/format';

/** 通用坐标换算。所有图都用 viewBox + 百分比宽度，保证在窄屏也不糊。 */
function useScale(w: number, h: number, pad: { l: number; r: number; t: number; b: number }) {
  const iw = w - pad.l - pad.r;
  const ih = h - pad.t - pad.b;
  return {
    iw,
    ih,
    x: (t: number): number => pad.l + t * iw,
    y: (t: number): number => pad.t + (1 - t) * ih,
  };
}

// ---------------------------------------------------------------------------
// 漏斗：逐层"无命中存活"
// ---------------------------------------------------------------------------
export function Funnel({
  steps,
  colors,
}: {
  steps: Array<{ key: string; label: string; value: number; note: string }>;
  colors?: Record<string, string>;
}) {
  const max = Math.max(...steps.map((s) => s.value), 1);
  return (
    <div className="space-y-1.5">
      {steps.map((s, i) => {
        const prev = i === 0 ? null : steps[i - 1].value;
        const drop = prev === null ? null : prev - s.value;
        const w = (s.value / max) * 100;
        const color = colors?.[s.key] ?? (i === 0 ? '#64748b' : i === 1 ? '#38bdf8' : '#22d3ee');
        return (
          <div key={s.key} className="group" title={s.note}>
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-[11px] text-slate-400 group-hover:text-slate-200">{s.label}</span>
              <span className="num text-[11px] text-slate-300">
                {int0(s.value)}
                {drop !== null && drop > 0 && <span className="ml-1.5 text-rose-300/80">−{int0(drop)}</span>}
              </span>
            </div>
            <div className="mt-1 h-2.5 overflow-hidden rounded-[3px] bg-white/[0.05]">
              <div
                className="h-full rounded-[3px] transition-all duration-700"
                style={{ width: `${w}%`, background: `linear-gradient(90deg, ${color}, ${color}88)` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 三分类混淆矩阵：行=gt，列=预测
// ---------------------------------------------------------------------------
const V = ['pass', 'review', 'reject'] as const;
const VL: Record<string, string> = { pass: '可投', review: '人核', reject: '拒绝' };

export function ConfusionMatrix({
  matrix,
  onCell,
  highlight,
}: {
  matrix: Record<string, Record<string, number>>;
  onCell?: (gt: string, pred: string, n: number) => void;
  highlight?: [string, string] | null;
}) {
  const all = V.flatMap((g) => V.map((p) => matrix[g]?.[p] ?? 0));
  const max = Math.max(...all, 1);
  const total = all.reduce((a, b) => a + b, 0);
  return (
    <div>
      <div className="grid grid-cols-[52px_repeat(3,1fr)] gap-1 text-center">
        <div />
        {V.map((p) => (
          <div key={p} className="text-[10px] text-slate-500">
            判 {VL[p]}
          </div>
        ))}
        {V.map((g) => (
          <React.Fragment key={g}>
            <div className="flex items-center justify-end pr-1 text-[10px] text-slate-500">gt {VL[g]}</div>
            {V.map((p) => {
              const n = matrix[g]?.[p] ?? 0;
              const diag = g === p;
              const a = 0.1 + 0.75 * (n / max);
              const isHi = highlight && highlight[0] === g && highlight[1] === p;
              return (
                <button
                  key={p}
                  onClick={() => onCell?.(g, p, n)}
                  className={`rounded-md border px-1 py-2.5 transition-all ${
                    isHi ? 'border-cyan-300/70 ring-1 ring-cyan-300/40' : 'border-white/10 hover:border-white/30'
                  }`}
                  style={{
                    background: diag ? `rgba(52,211,153,${a})` : `rgba(248,113,113,${a * 0.85})`,
                  }}
                >
                  <div className="num text-[13px] font-semibold text-white/95">{int0(n)}</div>
                  <div className="num text-[9px] text-white/60">{pct1(total ? n / total : 0)}</div>
                </button>
              );
            })}
          </React.Fragment>
        ))}
      </div>
      <p className="muted mt-2">
        绿=一致格，红=错判格。<b className="text-slate-400">点任意格</b>可看这批人被哪些规则打中（归因来自 metrics.json）。
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 折线图（敏感性 / PR 曲线通用）
// ---------------------------------------------------------------------------
export interface Series {
  name: string;
  color: string;
  points: Array<{ x: number; y: number; label?: string }>;
  dashed?: boolean;
  dots?: boolean;
}

export function LineChart({
  series,
  xTicks,
  yDomain,
  yFormat = (v: number) => fixed(v, 2),
  xFormat = (v: number) => fixed(v, 2),
  refLines = [],
  height = 190,
  xLabel,
  yLabel,
}: {
  series: Series[];
  xTicks?: number[];
  yDomain?: [number, number];
  yFormat?: (v: number) => string;
  xFormat?: (v: number) => string;
  refLines?: Array<{ y: number; label: string; color: string }>;
  height?: number;
  xLabel?: string;
  yLabel?: string;
}) {
  const W = 620;
  const H = height;
  const pad = { l: 42, r: 12, t: 12, b: 26 };
  const xs = series.flatMap((s) => s.points.map((p) => p.x));
  const ys = series.flatMap((s) => s.points.map((p) => p.y)).concat(refLines.map((r) => r.y));
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  const [y0, y1] = yDomain ?? [Math.min(...ys) * 0.96, Math.max(...ys) * 1.04];
  const sc = useScale(W, H, pad);
  const nx = (v: number): number => sc.x(x1 === x0 ? 0.5 : (v - x0) / (x1 - x0));
  const ny = (v: number): number => sc.y(y1 === y0 ? 0.5 : (v - y0) / (y1 - y0));
  const yGrid = [0, 0.25, 0.5, 0.75, 1].map((t) => y0 + t * (y1 - y0));

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
        {yGrid.map((v) => (
          <g key={v}>
            <line x1={pad.l} x2={W - pad.r} y1={ny(v)} y2={ny(v)} stroke="rgba(255,255,255,0.07)" strokeWidth={1} />
            <text x={pad.l - 6} y={ny(v) + 3} textAnchor="end" className="fill-slate-500" fontSize={9}>
              {yFormat(v)}
            </text>
          </g>
        ))}
        {(xTicks ?? series[0]?.points.map((p) => p.x) ?? []).map((v) => (
          <text key={v} x={nx(v)} y={H - 8} textAnchor="middle" className="fill-slate-500" fontSize={9}>
            {xFormat(v)}
          </text>
        ))}
        {refLines.map((r) => (
          <g key={r.label}>
            <line
              x1={pad.l}
              x2={W - pad.r}
              y1={ny(r.y)}
              y2={ny(r.y)}
              stroke={r.color}
              strokeWidth={1.25}
              strokeDasharray="5 4"
            />
            <text x={W - pad.r} y={ny(r.y) - 4} textAnchor="end" fill={r.color} fontSize={9}>
              {r.label}
            </text>
          </g>
        ))}
        {series.map((s) => (
          <g key={s.name}>
            <path
              d={s.points.map((p, i) => `${i === 0 ? 'M' : 'L'}${nx(p.x)},${ny(p.y)}`).join(' ')}
              fill="none"
              stroke={s.color}
              strokeWidth={1.8}
              strokeDasharray={s.dashed ? '4 3' : undefined}
              strokeLinejoin="round"
            />
            {(s.dots ?? true) &&
              s.points.map((p) => (
                <circle key={`${p.x}-${p.y}`} cx={nx(p.x)} cy={ny(p.y)} r={2.6} fill={s.color}>
                  <title>{`${s.name} · ${xFormat(p.x)} → ${yFormat(p.y)}${p.label ? `\n${p.label}` : ''}`}</title>
                </circle>
              ))}
          </g>
        ))}
        {yLabel && (
          <text x={10} y={pad.t + 4} className="fill-slate-500" fontSize={9}>
            {yLabel}
          </text>
        )}
        {xLabel && (
          <text x={W - pad.r} y={pad.t + 4} textAnchor="end" className="fill-slate-500" fontSize={9}>
            {xLabel}
          </text>
        )}
      </svg>
      <div className="mt-1 flex flex-wrap gap-3">
        {series.map((s) => (
          <span key={s.name} className="flex items-center gap-1.5 text-[10px] text-slate-400">
            <span className="inline-block h-0.5 w-4 rounded" style={{ background: s.color }} />
            {s.name}
          </span>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Prompt Bench 主图：规则基线是一条横贯全图的参考线，柱子跨不过去就是跨不过去
// ---------------------------------------------------------------------------
export interface BenchBar {
  key: string;
  group: string;
  label: string;
  f1: number;
  precision: number;
  recall: number;
  tokens: number;
  color: string;
  caution?: string;
  isRule?: boolean;
  sub?: string;
}

export function BenchChart({
  bars,
  baseline,
  baselineLabel,
  onPick,
  active,
}: {
  bars: BenchBar[];
  baseline: number;
  baselineLabel: string;
  onPick?: (key: string) => void;
  active?: string | null;
}) {
  const W = 660;
  const H = 260;
  const pad = { l: 38, r: 14, t: 16, b: 52 };
  const sc = useScale(W, H, pad);
  const yMax = Math.max(0.8, Math.max(...bars.map((b) => b.f1), baseline) * 1.15);
  const ny = (v: number): number => sc.y(v / yMax);
  const slot = sc.iw / bars.length;
  const bw = Math.min(52, slot * 0.56);
  const yGrid = [0, 0.2, 0.4, 0.6, 0.8].filter((v) => v <= yMax);

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
        <defs>
          <pattern id="caution" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
            <rect width="6" height="6" fill="transparent" />
            <line x1="0" y1="0" x2="0" y2="6" stroke="rgba(255,255,255,0.45)" strokeWidth="1.6" />
          </pattern>
        </defs>
        {yGrid.map((v) => (
          <g key={v}>
            <line x1={pad.l} x2={W - pad.r} y1={ny(v)} y2={ny(v)} stroke="rgba(255,255,255,0.06)" />
            <text x={pad.l - 6} y={ny(v) + 3} textAnchor="end" className="fill-slate-500" fontSize={9}>
              {v.toFixed(1)}
            </text>
          </g>
        ))}

        {/* 规则基线：横贯全图，且画在柱子之上，不让柱子盖住它 */}
        <rect x={pad.l} y={ny(baseline)} width={sc.iw} height={Math.max(0, sc.y(0) - ny(baseline))} fill="rgba(148,163,184,0.05)" />

        {bars.map((b, i) => {
          const cx = pad.l + slot * (i + 0.5);
          const top = ny(b.f1);
          const above = b.f1 >= baseline;
          const isActive = active === b.key;
          return (
            <g
              key={b.key}
              className="cursor-pointer"
              onClick={() => onPick?.(b.key)}
              opacity={active && !isActive ? 0.55 : 1}
            >
              <title>
                {`${b.label}\nF1 ${b.f1.toFixed(4)}｜精确率 ${b.precision.toFixed(4)}｜召回 ${b.recall.toFixed(4)}\ntoken ${int0(b.tokens)}${b.caution ? `\n⚠ ${b.caution}` : ''}`}
              </title>
              <rect
                x={cx - bw / 2}
                y={top}
                width={bw}
                height={Math.max(1, sc.y(0) - top)}
                rx={3}
                fill={b.color}
                opacity={above ? 0.95 : 0.5}
                stroke={isActive ? '#fff' : above ? 'rgba(255,255,255,0.35)' : 'rgba(255,255,255,0.12)'}
                strokeWidth={isActive ? 1.5 : 1}
                style={{ transition: 'all .5s ease' }}
              />
              {b.caution && (
                <rect x={cx - bw / 2} y={top} width={bw} height={Math.max(1, sc.y(0) - top)} rx={3} fill="url(#caution)" opacity={0.28} />
              )}
              <text x={cx} y={top - 5} textAnchor="middle" fontSize={10} className="fill-slate-100 font-mono">
                {b.f1.toFixed(3)}
              </text>
              {!above && !b.isRule && (
                <text x={cx} y={ny(baseline) - 5} textAnchor="middle" fontSize={9} className="fill-rose-300">
                  ↓输给规则
                </text>
              )}
              <text x={cx} y={sc.y(0) + 13} textAnchor="middle" fontSize={9.5} className="fill-slate-300">
                {b.group}
              </text>
              <text x={cx} y={sc.y(0) + 25} textAnchor="middle" fontSize={8.5} className="fill-slate-500">
                {b.sub ?? ''}
              </text>
              {b.caution && (
                <g>
                  <circle cx={cx + bw / 2 - 2} cy={top + 9} r={7} fill="#fbbf24" />
                  <text x={cx + bw / 2 - 2} y={top + 12.5} textAnchor="middle" fontSize={10} fontWeight={700} fill="#1c1917">
                    !
                  </text>
                </g>
              )}
            </g>
          );
        })}

        <line
          x1={pad.l}
          x2={W - pad.r}
          y1={ny(baseline)}
          y2={ny(baseline)}
          stroke="#e2e8f0"
          strokeWidth={1.6}
          strokeDasharray="7 4"
        />
        <g>
          <rect x={pad.l + 4} y={ny(baseline) - 17} width={224} height={14} rx={3} fill="rgba(5,7,13,0.82)" />
          <text x={pad.l + 8} y={ny(baseline) - 6} fontSize={9.5} className="fill-slate-200">
            {baselineLabel}
          </text>
        </g>
        <line x1={pad.l} x2={W - pad.r} y1={sc.y(0)} y2={sc.y(0)} stroke="rgba(255,255,255,0.18)" />
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 分层热力图
// ---------------------------------------------------------------------------
export function Heatmap({
  rows,
  onPick,
}: {
  rows: Array<{ cell: string; n: number; positives: number; f1: number; precision: number; recall: number; lowSupport?: boolean }>;
  onPick?: (cell: string) => void;
}) {
  const color = (f1: number): string => {
    // 0.4 红 -> 0.6 琥珀 -> 0.8 绿；刻意用发散配色，弱项一眼跳出来
    const t = Math.max(0, Math.min(1, (f1 - 0.4) / 0.4));
    const hue = 0 + t * 145;
    return `hsl(${hue} 70% ${28 + t * 12}%)`;
  };
  return (
    <div className="flex flex-wrap gap-1.5">
      {rows.map((r) => (
        <button
          key={r.cell}
          onClick={() => onPick?.(r.cell)}
          title={`${r.cell}\nF1 ${fixed(r.f1)}｜精确率 ${fixed(r.precision)}｜召回 ${fixed(r.recall)}\n该格 ${int0(r.n)} 人，含 ${int0(r.positives)} 个真水号${r.lowSupport ? '\n⚠ 低支撑格（真水号 < 10），不参与弱项排序' : ''}`}
          className={`min-w-[74px] rounded-md border px-2 py-1.5 text-left transition-transform hover:scale-[1.03] ${
            r.lowSupport ? 'border-dashed border-white/25' : 'border-white/10'
          }`}
          style={{ background: color(r.f1) }}
        >
          <div className="truncate text-[10px] text-white/85">{r.cell}</div>
          <div className="num text-[13px] font-semibold text-white">{fixed(r.f1, 3)}</div>
          <div className="num text-[9px] text-white/60">n={int0(r.n)} · pos={int0(r.positives)}</div>
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 结构占比（层级 / 国家 / 平台）
// ---------------------------------------------------------------------------
export function MixBar({
  mix,
  order,
  labels,
  limits,
}: {
  mix: Record<string, number>;
  order?: string[];
  labels?: Record<string, string>;
  limits?: Array<{ keys: string[]; max?: number; min?: number; name: string }>;
}) {
  const keys = (order ?? Object.keys(mix)).filter((k) => (mix[k] ?? 0) > 0);
  return (
    <div>
      <div className="flex h-4 w-full overflow-hidden rounded-md border border-white/10">
        {keys.map((k) => (
          <div
            key={k}
            className="group relative h-full transition-all"
            style={{ width: `${(mix[k] ?? 0) * 100}%`, background: catColor(k, 58, 52) }}
            title={`${labels?.[k] ?? k}：${pct1(mix[k] ?? 0)}`}
          />
        ))}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
        {keys.map((k) => (
          <span key={k} className="flex items-center gap-1 text-[10px] text-slate-400">
            <span className="inline-block h-2 w-2 rounded-sm" style={{ background: catColor(k, 58, 52) }} />
            {labels?.[k] ?? k}
            <span className="num text-slate-300">{pct1(mix[k] ?? 0)}</span>
          </span>
        ))}
      </div>
      {limits?.map((l) => {
        const v = l.keys.reduce((a, k) => a + (mix[k] ?? 0), 0);
        const ok = l.max !== undefined ? v <= l.max + 1e-9 : v >= (l.min ?? 0) - 1e-9;
        return (
          <div key={l.name} className="mt-1 flex items-center gap-2 text-[10px]">
            <span className={ok ? 'text-emerald-300' : 'text-rose-300'}>{ok ? '✓' : '✕'}</span>
            <span className="text-slate-400">{l.name}</span>
            <span className="num text-slate-300">
              {pct1(v)} {l.max !== undefined ? `≤ ${pct1(l.max, 0)}` : `≥ ${pct1(l.min ?? 0, 0)}`}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 单个信号的同组分布位置（证据链抽屉用）
// ---------------------------------------------------------------------------
export function RankStrip({
  rank,
  actual,
  threshold,
  signal,
  direction,
}: {
  rank: number | null;
  actual: number | string | null;
  threshold: number | string | null;
  signal: string;
  direction: 'upper' | 'lower' | 'abs';
}) {
  const pos = rank === null ? null : Math.max(0, Math.min(1, rank));
  return (
    <div>
      <div className="flex items-baseline justify-between text-[10px]">
        <span className="text-slate-500">{signal}</span>
        <span className="num text-slate-300">
          实际 {typeof actual === 'number' ? fixed(actual, 4) : (actual ?? '—')}
          <span className="mx-1 text-slate-600">vs</span>
          阈值 {typeof threshold === 'number' ? fixed(threshold, 4) : (threshold ?? '—')}
        </span>
      </div>
      <div className="relative mt-1 h-6 overflow-hidden rounded-md border border-white/10 bg-[linear-gradient(90deg,rgba(52,211,153,.16),rgba(251,191,36,.16),rgba(248,113,113,.22))]">
        {[0.25, 0.5, 0.75].map((t) => (
          <div key={t} className="absolute top-0 h-full w-px bg-white/10" style={{ left: `${t * 100}%` }} />
        ))}
        {pos !== null && (
          <div className="absolute top-0 h-full" style={{ left: `${pos * 100}%` }}>
            <div className="h-full w-[2px] bg-white shadow-[0_0_8px_rgba(255,255,255,0.8)]" />
          </div>
        )}
        <div className="absolute inset-0 flex items-center justify-between px-1.5 text-[9px] text-white/55">
          <span>同组分布 p0</span>
          <span>
            {pos === null ? '该信号无分位记录' : `百分位 ${pct1(pos, 1)}`}
            <span className="ml-1 text-white/40">
              （{direction === 'upper' ? '越靠右越异常' : direction === 'lower' ? '越靠左越异常' : '双侧'}）
            </span>
          </span>
          <span>p100</span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 横向条形对比（成本/浪费/曝光两臂对照）
// ---------------------------------------------------------------------------
export function DuoBars({
  rows,
  fmt = int0,
  leftName,
  rightName,
  leftColor = '#f87171',
  rightColor = '#22d3ee',
}: {
  rows: Array<{ label: string; left: number; right: number; note?: string; goodIsLow?: boolean }>;
  fmt?: (x: number) => string;
  leftName: string;
  rightName: string;
  leftColor?: string;
  rightColor?: string;
}) {
  return (
    <div className="space-y-3">
      <div className="flex gap-4 text-[10px] text-slate-400">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-sm" style={{ background: leftColor }} />
          {leftName}
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-sm" style={{ background: rightColor }} />
          {rightName}
        </span>
      </div>
      {rows.map((r) => {
        const max = Math.max(r.left, r.right, 1);
        return (
          <div key={r.label} title={r.note}>
            <div className="flex items-baseline justify-between text-[11px]">
              <span className="text-slate-400">{r.label}</span>
              <span className="num text-slate-500">
                <span style={{ color: leftColor }}>{fmt(r.left)}</span>
                <span className="mx-1">→</span>
                <span style={{ color: rightColor }}>{fmt(r.right)}</span>
              </span>
            </div>
            <div className="mt-1 space-y-1">
              <div className="h-2 overflow-hidden rounded-sm bg-white/[0.05]">
                <div className="h-full rounded-sm" style={{ width: `${(r.left / max) * 100}%`, background: leftColor }} />
              </div>
              <div className="h-2 overflow-hidden rounded-sm bg-white/[0.05]">
                <div className="h-full rounded-sm" style={{ width: `${(r.right / max) * 100}%`, background: rightColor }} />
              </div>
            </div>
            {r.note && <div className="muted mt-1">{r.note}</div>}
          </div>
        );
      })}
    </div>
  );
}

/** token 账：prompt / completion(含 reasoning) 堆叠。 */
export function TokenBars({
  rows,
}: {
  rows: Array<{ label: string; prompt: number; completion: number; reasoning: number; calls: number; failed: number; model: string }>;
}) {
  const max = Math.max(...rows.map((r) => r.prompt + r.completion), 1);
  return (
    <div className="space-y-2.5">
      {rows.map((r) => (
        <div key={r.label}>
          <div className="flex items-baseline justify-between text-[11px]">
            <span className="text-slate-300">
              {r.label}
              <span className="ml-1.5 text-[10px] text-slate-500">{r.model}</span>
            </span>
            <span className="num text-[11px] text-slate-400">
              {int0(r.prompt + r.completion)} token · {int0(r.calls)} 次调用
              {r.failed > 0 && <span className="ml-1 text-rose-300">（失败 {r.failed}）</span>}
            </span>
          </div>
          <div className="mt-1 flex h-3 overflow-hidden rounded-sm bg-white/[0.05]">
            <div
              className="h-full"
              style={{ width: `${(r.prompt / max) * 100}%`, background: '#38bdf8' }}
              title={`prompt ${int0(r.prompt)} token`}
            />
            <div
              className="h-full"
              style={{ width: `${((r.completion - r.reasoning) / max) * 100}%`, background: '#818cf8' }}
              title={`completion（不含 reasoning）${int0(r.completion - r.reasoning)} token`}
            />
            <div
              className="h-full"
              style={{ width: `${(r.reasoning / max) * 100}%`, background: '#c084fc' }}
              title={`reasoning ${int0(r.reasoning)} token`}
            />
          </div>
        </div>
      ))}
      <div className="flex gap-3 text-[10px] text-slate-500">
        {[
          ['prompt', '#38bdf8'],
          ['completion', '#818cf8'],
          ['reasoning（思维链）', '#c084fc'],
        ].map(([n, c]) => (
          <span key={n} className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-sm" style={{ background: c }} />
            {n}
          </span>
        ))}
      </div>
    </div>
  );
}

/** 迷你柱：一行里放几十个小竖条（规则命中分布）。 */
export function SparkBars({
  items,
  color = '#22d3ee',
  onPick,
}: {
  items: Array<{ key: string; label: string; value: number }>;
  color?: string;
  onPick?: (key: string) => void;
}) {
  const max = Math.max(...items.map((i) => i.value), 1);
  return (
    <div className="flex items-end gap-1" style={{ height: 56 }}>
      {items.map((i) => (
        <button
          key={i.key}
          onClick={() => onPick?.(i.key)}
          title={`${i.label}：${int0(i.value)} 人`}
          className="group flex-1 rounded-t-sm transition-all hover:opacity-100"
          style={{ height: `${Math.max(4, (i.value / max) * 100)}%`, background: color, opacity: 0.55 }}
        />
      ))}
    </div>
  );
}

export function DonutRing({
  segments,
  size = 128,
  center,
  sub,
}: {
  segments: Array<{ key: string; label: string; value: number; color: string }>;
  size?: number;
  center: React.ReactNode;
  sub?: React.ReactNode;
}) {
  const total = segments.reduce((a, s) => a + s.value, 0) || 1;
  const r = size / 2 - 10;
  const c = 2 * Math.PI * r;
  let acc = 0;
  return (
    <div className="flex items-center gap-4">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0 -rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth={12} />
        {segments.map((s) => {
          const len = (s.value / total) * c;
          const el = (
            <circle
              key={s.key}
              cx={size / 2}
              cy={size / 2}
              r={r}
              fill="none"
              stroke={s.color}
              strokeWidth={12}
              strokeDasharray={`${len} ${c - len}`}
              strokeDashoffset={-acc}
              style={{ transition: 'stroke-dasharray .6s ease' }}
            >
              <title>{`${s.label}：${int0(s.value)}（${pct1(s.value / total)}）`}</title>
            </circle>
          );
          acc += len;
          return el;
        })}
      </svg>
      <div className="min-w-0">
        <div className="num text-[20px] font-semibold text-slate-100">{center}</div>
        {sub && <div className="muted">{sub}</div>}
        <div className="mt-1.5 space-y-1">
          {segments.map((s) => (
            <div key={s.key} className="flex items-center gap-1.5 text-[11px]">
              <span className="inline-block h-2 w-2 rounded-sm" style={{ background: s.color }} />
              <span className="text-slate-400">{s.label}</span>
              <span className="num text-slate-200">{int0(s.value)}</span>
              <span className="num text-slate-500">{pct1(s.value / total)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 三臂链式差分：把「基线 → 第三臂 → KOXPilot」的浪费金额与两段贡献画在一起
// ---------------------------------------------------------------------------
export interface ArmBar {
  key: string;
  label: string;
  sub?: string;
  /** 该臂的浪费金额（越低越好）。 */
  wasted: number;
  color: string;
  /** 相对上一臂的贡献额（第一臂为 null）。 */
  contribution?: number | null;
  contributionLabel?: string;
  /** 附加的对照口径（有效曝光率、选中人数等），照实展示，不隐藏不利项。 */
  chips?: Array<{ k: string; v: string; tone?: 'good' | 'bad' | 'muted' }>;
}

export function ArmWaterfall({
  arms,
  fmt = (x: number) => `$${int0(x)}`,
}: {
  arms: ArmBar[];
  fmt?: (x: number) => string;
}) {
  const max = Math.max(...arms.map((a) => a.wasted), 1);
  return (
    <div className="space-y-2.5">
      {arms.map((a, i) => {
        const contrib = a.contribution ?? null;
        return (
          <div key={a.key}>
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
              <span className="text-[11.5px] text-slate-300">
                <span className="num mr-1.5 text-[10px] text-slate-600">{i + 1}</span>
                {a.label}
              </span>
              <span className="num text-[11.5px]" style={{ color: a.color }}>
                浪费 {fmt(a.wasted)}
              </span>
            </div>
            {a.sub && <div className="muted mt-0.5 leading-relaxed">{a.sub}</div>}
            <div className="mt-1 h-2.5 overflow-hidden rounded-sm bg-white/[0.05]">
              <div
                className="h-full rounded-sm transition-all duration-500"
                style={{ width: `${(a.wasted / max) * 100}%`, background: a.color }}
              />
            </div>
            {a.chips && a.chips.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                {a.chips.map((c) => (
                  <span key={c.k} className="text-[10.5px] text-slate-500">
                    {c.k}{' '}
                    <b
                      className={`num ${
                        c.tone === 'good' ? 'text-emerald-300' : c.tone === 'bad' ? 'text-rose-300' : 'text-slate-300'
                      }`}
                    >
                      {c.v}
                    </b>
                  </span>
                ))}
              </div>
            )}
            {contrib !== null && Number.isFinite(contrib) && (
              <div className="mt-1.5 flex items-center gap-1.5 pl-3">
                <span className="text-[10px] text-slate-600">↳</span>
                <span
                  className={`num rounded px-1.5 py-0.5 text-[10.5px] ${
                    contrib >= 0
                      ? 'border border-emerald-400/25 bg-emerald-400/10 text-emerald-200'
                      : 'border border-rose-400/25 bg-rose-400/10 text-rose-200'
                  }`}
                >
                  {a.contributionLabel ?? '相对上一臂'} {contrib >= 0 ? '−' : '+'}
                  {fmt(Math.abs(contrib))} 浪费
                </span>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 逐种子散点带：把「均值 ± 标准差 / 95% CI / 每个种子的点」一次画完
// 目的：让「4/12 个种子为负」这种事在图上直接看得见，而不是只写在文字里
// ---------------------------------------------------------------------------
export function SeedStrip({
  points,
  mean,
  ciLow,
  ciHigh,
  zeroLine = true,
  fmt = (x: number) => fixed(x, 0),
  color = '#22d3ee',
  negColor = '#fb7185',
  height = 74,
  label,
}: {
  points: Array<{ seed: number | string; value: number }>;
  mean: number;
  ciLow?: number | null;
  ciHigh?: number | null;
  zeroLine?: boolean;
  fmt?: (x: number) => string;
  color?: string;
  negColor?: string;
  height?: number;
  label?: React.ReactNode;
}) {
  const vals = points.map((p) => p.value);
  const cand = [...vals, mean, ...(zeroLine ? [0] : []), ...(ciLow != null ? [ciLow] : []), ...(ciHigh != null ? [ciHigh] : [])];
  const lo = Math.min(...cand);
  const hi = Math.max(...cand);
  const span = hi - lo || 1;
  const pad = span * 0.08;
  const min = lo - pad;
  const max = hi + pad;
  const xOf = (v: number): number => ((v - min) / (max - min)) * 100;
  const showZero = zeroLine && min < 0 && max > 0;
  return (
    <div>
      {label && <div className="muted mb-1">{label}</div>}
      {/* 用绝对定位的 HTML 元素而不是 SVG：避免 viewBox 拉伸把点画成椭圆 */}
      <div className="relative w-full" style={{ height }}>
        {/* 95% CI 区间 */}
        {ciLow != null && ciHigh != null && (
          <div
            className="absolute top-1/2 -translate-y-1/2 rounded-sm"
            style={{
              left: `${xOf(ciLow)}%`,
              width: `${Math.max(0.3, xOf(ciHigh) - xOf(ciLow))}%`,
              height: 22,
              background: color,
              opacity: 0.15,
            }}
          />
        )}
        {/* 0 线：跨 0 就是"符号不稳定" */}
        {showZero && (
          <div
            className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2 border-l border-dashed"
            style={{ left: `${xOf(0)}%`, height: 40, borderColor: negColor }}
          />
        )}
        {/* 均值线 */}
        <div
          className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2"
          style={{ left: `${xOf(mean)}%`, height: 32, width: 1.5, background: color }}
        />
        {/* 每个种子一个点（纵向轻微错开，避免重叠遮挡） */}
        {points.map((p, i) => (
          <span
            key={String(p.seed)}
            title={`seed ${p.seed}：${fmt(p.value)}`}
            className="absolute h-[6px] w-[6px] -translate-x-1/2 -translate-y-1/2 rounded-full"
            style={{
              left: `${xOf(p.value)}%`,
              top: `calc(50% + ${((i % 3) - 1) * 7}px)`,
              background: p.value < 0 ? negColor : color,
              opacity: 0.9,
            }}
          />
        ))}
        {showZero && (
          <span
            className="num absolute text-[9px]"
            style={{ left: `${xOf(0)}%`, top: 0, color: negColor, transform: 'translateX(-50%)' }}
          >
            0
          </span>
        )}
      </div>
      <div className="mt-0.5 flex flex-wrap items-center justify-between gap-x-3 text-[10px]">
        <span className="num text-slate-500">min {fmt(Math.min(...vals))}</span>
        {ciLow != null && ciHigh != null && (
          <span className="num" style={{ color }}>
            95% CI [{fmt(ciLow)}, {fmt(ciHigh)}]
            {ciLow < 0 && ciHigh > 0 && <span className="ml-1 text-rose-300">跨 0</span>}
          </span>
        )}
        <span className="num text-slate-500">max {fmt(Math.max(...vals))}</span>
      </div>
    </div>
  );
}

export { compact };
