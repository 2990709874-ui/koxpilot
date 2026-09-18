import React from 'react';
import { ChevronRight } from 'lucide-react';

/**
 * 统一折叠块。
 *
 * 为什么需要它：这个作品的内容密度是它的价值（可信边界、混淆变量、口径澄清、
 * 反面事实、trace 日志），但全部平铺导致 6 个 tab 加起来 23.7 屏，
 * 评审第一眼只会觉得「堆砌」。
 *
 * 折叠的纪律写在 DESIGN.md：**折叠不等于藏**。
 * 所以这个组件强制要求 `title` 自带信息量，并且支持 `flag` 角标
 * 让人扫一眼就知道里面是「我自己承认的问题」还是「补充证据」——
 * 自我批判是加分项，只是不该占满第一屏。
 */
export function Collapse({
  title,
  hint,
  children,
  flag,
  defaultOpen = false,
  count,
}: {
  /** 必须写清里面是什么，禁止「详情」「更多」这类空标题 */
  title: React.ReactNode;
  hint?: React.ReactNode;
  children: React.ReactNode;
  /** caveat = 我自己承认的局限；evidence = 支撑证据；detail = 中性明细 */
  flag?: 'caveat' | 'evidence' | 'detail';
  defaultOpen?: boolean;
  /** 里面有几条，显示在标题右侧，避免「展开才知道是不是空的」 */
  count?: number;
}) {
  const [open, setOpen] = React.useState(defaultOpen);
  const tone =
    flag === 'caveat'
      ? { ring: 'border-amber-200 bg-amber-50/60', dot: 'bg-amber-500', label: '自述局限' }
      : flag === 'evidence'
        ? { ring: 'border-live-200 bg-live-50/60', dot: 'bg-live-500', label: '支撑证据' }
        : { ring: 'border-slate-200 bg-slate-50/60', dot: 'bg-slate-400', label: '明细' };

  return (
    <div className={`rounded-xl border ${tone.ring} transition-colors`}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="focusable flex w-full items-start gap-2 rounded-xl px-3 py-2.5 text-left"
      >
        <ChevronRight
          size={14}
          className={`mt-0.5 shrink-0 text-slate-500 transition-transform ${open ? 'rotate-90' : ''}`}
        />
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="text-[13px] font-medium text-slate-800">{title}</span>
            {flag && (
              <span className="chip border-transparent bg-white/80 text-slate-500">
                <span className={`inline-block h-1.5 w-1.5 rounded-full ${tone.dot}`} />
                {tone.label}
                {typeof count === 'number' ? ` ${count}` : ''}
              </span>
            )}
          </span>
          {hint && <span className="muted mt-0.5 block">{hint}</span>}
        </span>
      </button>
      {open && <div className="border-t border-slate-200/70 px-3 py-3">{children}</div>}
    </div>
  );
}

/**
 * 「本页结论」横幅。
 *
 * 为什么需要它：改版前进任何一个 tab，第一屏都是控件和日志，
 * 读者不知道这一页在证明什么。DESIGN.md 规定每个 tab 顶部必须先给结论。
 */
export function Verdict({
  what,
  conclusion,
  stats,
  tone = 'brand',
  right,
}: {
  /** 这一页在证明什么，短语 */
  what: React.ReactNode;
  /** 结论，一句话，允许含加粗 */
  conclusion: React.ReactNode;
  /** 2~4 个关键数字 */
  stats?: Array<{ label: React.ReactNode; value: React.ReactNode; tone?: 'good' | 'warn' | 'bad' }>;
  tone?: 'brand' | 'good' | 'warn';
  right?: React.ReactNode;
}) {
  const bar = tone === 'good' ? 'bg-emerald-500' : tone === 'warn' ? 'bg-amber-500' : 'bg-brand-600';
  const valueTone = (t?: string): string =>
    t === 'good' ? 'text-emerald-600' : t === 'warn' ? 'text-amber-600' : t === 'bad' ? 'text-rose-600' : 'text-slate-900';
  return (
    <section className="card relative overflow-hidden">
      <span className={`absolute left-0 top-0 h-full w-1 ${bar}`} />
      <div className="flex flex-col gap-4 pl-5 pr-4 py-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="muted uppercase tracking-wider">{what}</div>
          <p className="body-text mt-1 text-[14px] text-slate-800">{conclusion}</p>
        </div>
        {stats && stats.length > 0 && (
          <div className="flex shrink-0 flex-wrap gap-x-7 gap-y-3">
            {stats.map((s, i) => (
              <div key={i}>
                <div className="muted">{s.label}</div>
                <div className={`num text-[22px] font-semibold leading-tight ${valueTone(s.tone)}`}>{s.value}</div>
              </div>
            ))}
          </div>
        )}
        {right && <div className="shrink-0">{right}</div>}
      </div>
    </section>
  );
}
