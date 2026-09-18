import React from 'react';
import { ChevronRight } from 'lucide-react';

/**
 * 统一折叠块。
 *
 * v3 变更：移除了原来的「自述局限 / 支撑证据 / 明细」角标体系。
 *
 * 原因：那套角标本身就在页面上宣告「这是一次自我审计」，
 * 而这正是错误的框架——产品界面不是自我举证的地方。
 * 折叠块本身保留，用途收窄为「收纳次要明细」，不再承担"展示我有多诚实"的职能。
 *
 * `flag` 与 `count` 两个 prop 暂时保留但不再渲染，只为让调用方可以分批清理而不打断构建；
 * 调用方清理完后应当删除传参。
 */
export function Collapse({
  title,
  hint,
  children,
  defaultOpen = false,
}: {
  /** 写清里面是什么，禁止「详情」「更多」这类空标题 */
  title: React.ReactNode;
  hint?: React.ReactNode;
  children: React.ReactNode;
  defaultOpen?: boolean;
  /** @deprecated v3 起不再渲染角标，留着只为兼容未清理的调用方 */
  flag?: 'caveat' | 'evidence' | 'detail';
  /** @deprecated 同上 */
  count?: number;
}) {
  const [open, setOpen] = React.useState(defaultOpen);

  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/60 transition-colors">
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
          <span className="block text-[13px] font-medium text-slate-800">{title}</span>
          {hint && <span className="muted mt-0.5 block">{hint}</span>}
        </span>
      </button>
      {open && <div className="border-t border-slate-200/70 px-3 py-3">{children}</div>}
    </div>
  );
}

/**
 * 「本页结论」横幅：一句话说清这一页给出什么结论，配 2~4 个关键数字。
 *
 * v3 措辞纪律：陈述句，无第一人称，不叙述开发过程，不替作者辩解。
 */
export function Verdict({
  what,
  conclusion,
  stats,
  tone = 'brand',
  right,
}: {
  what: React.ReactNode;
  conclusion: React.ReactNode;
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

/**
 * 「能力边界」区块。
 *
 * v3 新增，用来取代原先散落全站的自述局限折叠块。
 * 纪律：每个页签最多一个，3~5 行陈述句，只写「用这份结果时要注意什么」，
 * 不写「我做了什么努力才发现这件事」。忏悔改规格。
 */
export function Boundaries({ items }: { items: React.ReactNode[] }) {
  return (
    <section className="card border-slate-300 bg-slate-50/80">
      <div className="px-4 py-3.5">
        <div className="text-[13px] font-semibold text-slate-800">能力边界</div>
        <ul className="mt-2 space-y-1.5">
          {items.map((t, i) => (
            <li key={i} className="flex gap-2 text-[13px] leading-relaxed text-slate-600">
              <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-slate-400" />
              <span>{t}</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
