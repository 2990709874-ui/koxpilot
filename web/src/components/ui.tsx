import React from 'react';
import { CHROME, SERIES, onLight } from './palette';

/** 基础面板。所有内容区块都用它，保证留白与描边一致。 */
export function Panel({
  title,
  subtitle,
  right,
  children,
  className = '',
  bodyClass = '',
  tone = 'default',
}: {
  title?: React.ReactNode;
  subtitle?: React.ReactNode;
  right?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
  bodyClass?: string;
  tone?: 'default' | 'accent' | 'warn' | 'danger';
}) {
  const ring =
    tone === 'accent'
      ? 'border-live-300 bg-live-50/80 shadow-[inset_4px_0_0_0_#0ea5e9]'
      : tone === 'warn'
        ? 'border-amber-300 bg-amber-50/80 shadow-[inset_4px_0_0_0_#d97706]'
        : tone === 'danger'
          ? 'border-rose-300 bg-rose-50/80 shadow-[inset_4px_0_0_0_#e11d48]'
          : '';
  return (
    <section className={`card ${ring} ${className}`}>
      {(title || right) && (
        <header className="flex items-start justify-between gap-3 border-b border-slate-300 px-4 py-3">
          <div className="min-w-0">
            {title && <h3 className="section-title truncate">{title}</h3>}
            {subtitle && <p className="muted mt-0.5">{subtitle}</p>}
          </div>
          {right && <div className="shrink-0">{right}</div>}
        </header>
      )}
      <div className={`px-4 py-3 ${bodyClass}`}>{children}</div>
    </section>
  );
}

export function Badge({
  children,
  className = '',
  title,
}: {
  children: React.ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span className={`chip ${className}`} title={title}>
      {children}
    </span>
  );
}

/** 真实性标签：整个产品的诚实性靠它到处出现。 */
export function TruthChip({ kind }: { kind: 'rule' | 'llm-offline' | 'audit' | 'synthetic' | 'python' }) {
  const map: Record<string, [string, string]> = {
    rule: ['浏览器内真算', 'border-live-300 bg-live-50 text-live-700'],
    'llm-offline': ['构建期真调 LLM · 结果固化', 'border-indigo-300 bg-indigo-50 text-indigo-700'],
    audit: ['以 gt 为裁判的审计', 'border-fuchsia-300 bg-fuchsia-50 text-fuchsia-700'],
    synthetic: ['合成数据（固定种子）', 'border-slate-400 bg-slate-100 text-slate-700'],
    python: ['Python 离线全量', 'border-emerald-300 bg-emerald-50 text-emerald-700'],
  };
  const [label, cls] = map[kind];
  return <Badge className={cls}>{label}</Badge>;
}

export function Stat({
  label,
  value,
  hint,
  tone = 'default',
  icon,
  className = '',
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  hint?: React.ReactNode;
  tone?: 'default' | 'good' | 'warn' | 'bad' | 'accent';
  icon?: React.ReactNode;
  className?: string;
}) {
  const color =
    tone === 'good'
      ? 'text-emerald-700'
      : tone === 'warn'
        ? 'text-amber-700'
        : tone === 'bad'
          ? 'text-rose-700'
          : tone === 'accent'
            ? 'text-live-700'
            : 'text-slate-900';
  return (
    <div className={`card px-3.5 py-3 ${className}`}>
      <div className="flex items-center gap-1.5 text-[12px] text-slate-600">
        {icon}
        <span className="truncate">{label}</span>
      </div>
      <div className={`num mt-1 text-[22px] font-semibold leading-tight ${color}`}>{value}</div>
      {hint && <div className="muted mt-1">{hint}</div>}
    </div>
  );
}

/** 数字滚动：只在值变化时跑一次 400ms 缓动，不做无意义的循环动画。 */
export function CountUp({ value, format, ms = 700 }: { value: number; format: (x: number) => string; ms?: number }) {
  const [shown, setShown] = React.useState(value);
  const fromRef = React.useRef(value);
  React.useEffect(() => {
    const from = fromRef.current;
    if (from === value) return;
    let raf = 0;
    const t0 = performance.now();
    const tick = (t: number): void => {
      const p = Math.min(1, (t - t0) / ms);
      const eased = 1 - (1 - p) ** 3;
      setShown(from + (value - from) * eased);
      if (p < 1) raf = requestAnimationFrame(tick);
      else fromRef.current = value;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, ms]);
  return <span className="tnum">{format(shown)}</span>;
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  size = 'md',
}: {
  options: Array<{ value: T; label: React.ReactNode; hint?: string }>;
  value: T;
  onChange: (v: T) => void;
  size?: 'sm' | 'md';
}) {
  return (
    <div className="inline-flex rounded-lg border border-slate-200 bg-slate-50 p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          title={o.hint}
          onClick={() => onChange(o.value)}
          className={`rounded-md ${size === 'sm' ? 'px-2 py-1 text-[11px]' : 'px-3 py-1.5 text-[12px]'} transition-colors ${
            value === o.value ? 'bg-white text-brand-700 shadow-[inset_0_0_0_1px_rgba(79,70,229,0.35)]' : 'text-slate-600 hover:text-slate-800'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: React.ReactNode;
  hint?: string;
}) {
  return (
    <button
      onClick={() => onChange(!checked)}
      title={hint}
      className="flex items-center gap-2 text-[12px] text-slate-700 transition-colors hover:text-slate-900"
    >
      <span
        className={`relative h-4 w-8 rounded-full transition-colors ${checked ? 'bg-live-500' : 'bg-slate-300'}`}
      >
        <span
          className={`absolute top-0.5 h-3 w-3 rounded-full bg-white transition-all ${checked ? 'left-4' : 'left-0.5'}`}
        />
      </span>
      {label}
    </button>
  );
}

/** 悬浮说明：用原生 title 之外再给一个可读性更好的卡片。 */
export function Hint({ text, children }: { text: React.ReactNode; children: React.ReactNode }) {
  return (
    <span className="group relative inline-flex cursor-help items-center">
      {children}
      <span className="pointer-events-none absolute bottom-full left-1/2 z-50 mb-1.5 hidden w-72 -translate-x-1/2 rounded-lg border border-slate-200 bg-white p-3 text-left text-[12.5px] leading-relaxed text-slate-700 shadow-pop group-hover:block">
        {text}
      </span>
    </span>
  );
}

export function Bar({
  value,
  max,
  color = SERIES.primary,
  height = 6,
  label,
}: {
  value: number;
  max: number;
  color?: string;
  height?: number;
  label?: React.ReactNode;
}) {
  const w = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 flex-1 overflow-hidden rounded-full" style={{ height, background: CHROME.track }}>
        <div className="h-full rounded-full transition-all duration-500" style={{ width: `${w}%`, background: onLight(color) }} />
      </div>
      {label && <span className="num shrink-0 text-[12px] text-slate-700">{label}</span>}
    </div>
  );
}

/** 缺失产物的诚实空态：绝不用占位数字冒充。 */
export function MissingArtifact({
  file,
  what,
  how,
}: {
  file: string;
  what: string;
  how?: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border-2 border-dashed border-amber-300 bg-amber-50 p-4">
      <div className="flex items-center gap-2 text-[12px] font-medium text-amber-700">
        <span className="inline-block h-1.5 w-1.5 rounded-full bg-amber-500" />
        {what}尚未生成
      </div>
      <p className="muted mt-1.5">
        缺少产物 <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[12px] text-amber-800">{file}</code>
        。本板块按缺失降级显示，<b className="text-amber-800">不会用估算值或占位数字冒充</b>。
      </p>
      {how && <p className="muted mt-1">{how}</p>}
    </div>
  );
}

export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  children,
  width = 'w-[min(680px,94vw)]',
}: {
  open: boolean;
  onClose: () => void;
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  children: React.ReactNode;
  width?: string;
}) {
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-slate-900/45 backdrop-blur-[2px]" onClick={onClose} />
      <aside className={`${width} overflow-y-auto border-l border-slate-200 bg-white shadow-2xl`}>
        <header className="sticky top-0 z-10 flex items-start justify-between gap-3 border-b border-slate-200 bg-white px-4 py-3 backdrop-blur">
          <div className="min-w-0">
            <h3 className="truncate text-[14px] font-semibold text-slate-900">{title}</h3>
            {subtitle && <div className="muted mt-0.5">{subtitle}</div>}
          </div>
          <button
            onClick={onClose}
            className="rounded-md border border-slate-200 px-2 py-1 text-[12px] text-slate-600 hover:border-slate-300 hover:text-slate-800"
          >
            关闭 Esc
          </button>
        </header>
        <div className="px-4 py-4">{children}</div>
      </aside>
    </div>
  );
}

export function KV({ k, v, mono = true }: { k: React.ReactNode; v: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="text-[12px] text-slate-600">{k}</span>
      <span className={`${mono ? 'num' : ''} text-[12.5px] text-slate-800`}>{v}</span>
    </div>
  );
}

export function Note({ children, tone = 'muted' }: { children: React.ReactNode; tone?: 'muted' | 'warn' | 'good' }) {
  const cls =
    tone === 'warn'
      ? 'border-amber-300 bg-amber-50 text-amber-800'
      : tone === 'good'
        ? 'border-emerald-300 bg-emerald-50 text-emerald-800'
        : 'border-slate-300 bg-slate-50 text-slate-700';
  return <div className={`rounded-lg border ${cls} px-3 py-2 text-[12.5px] leading-relaxed`}>{children}</div>;
}
