import React from 'react';
import { TABS, type TabId } from './nav';

/**
 * 页签栏：「图标 + 名称 + 一句话说明」的卡片式页签。
 * 选中态用 brand 主色，未选中态文字保持 slate-600/700，白底上读得清。
 */
export function TabBar({ tab, onGo }: { tab: TabId; onGo: (id: TabId) => void }): React.ReactElement {
  return (
    <nav className="mt-2.5 grid grid-cols-1 gap-1.5 sm:grid-cols-3" aria-label="功能页签">
      {TABS.map((t, i) => {
        const Icon = t.icon;
        const active = tab === t.id;
        return (
          <button
            key={t.id}
            onClick={() => onGo(t.id)}
            aria-current={active ? 'page' : undefined}
            className={`focusable flex items-center gap-2.5 rounded-xl border px-3 py-2 text-left transition-all ${
              active
                ? 'border-brand-300 bg-brand-50 shadow-lift'
                : 'border-slate-200 bg-white hover:border-brand-200 hover:bg-slate-50'
            }`}
          >
            <span
              className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border ${
                active ? 'border-brand-300 bg-white text-brand-700' : 'border-slate-200 bg-slate-50 text-slate-600'
              }`}
            >
              <Icon size={15} />
            </span>
            <span className="min-w-0">
              <span className="flex items-baseline gap-1.5">
                <span className="num text-[11px] text-slate-500">{i + 1}</span>
                <span className={`text-[13.5px] font-semibold ${active ? 'text-brand-700' : 'text-slate-900'}`}>
                  {t.label}
                </span>
              </span>
              <span className={`block truncate text-[11.5px] ${active ? 'text-brand-700/90' : 'text-slate-600'}`}>
                {t.desc}
              </span>
            </span>
          </button>
        );
      })}
    </nav>
  );
}
