import React from 'react';

/**
 * 页签内部的「段落分区」标题：带序号的标题把同一页签内的多个区块串成一条动线，
 * 同时这个 id 承接旧 hash 的直达锚点。
 */
export function SectionBlock({
  id,
  step,
  title,
  question,
  children,
}: {
  id: string;
  /** 叙事序号，例如「第 1 步」 */
  step: string;
  title: string;
  /** 这一段回答什么问题 */
  question: string;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <section id={id} className="scroll-mt-28">
      <div className="mb-2.5 flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-slate-200 pb-2">
        <span className="num chip border-brand-200 bg-brand-50 text-brand-700">{step}</span>
        <h3 className="text-[16px] font-semibold tracking-tight text-slate-900">{title}</h3>
        <span className="muted">{question}</span>
      </div>
      <div className="space-y-3">{children}</div>
    </section>
  );
}
