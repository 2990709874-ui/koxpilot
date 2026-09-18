import { BarChart3, Target, Wrench } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

/**
 * 导航定义：3 个功能导向页签，每个页签自带「这一页给你什么」的一句话说明。
 *
 * 旧 hash 必须继续可用（README 与 PDF 里已经放出去了），
 * 所以 LEGACY_HASH 把旧 hash 映射到「新页签 + 页内段落 anchor」。
 */
export interface TabDef {
  id: TabId;
  label: string;
  /** 一句话说明：这一页给你什么 */
  desc: string;
  icon: LucideIcon;
}

export type TabId = 'run' | 'proof' | 'build';

export const TABS: TabDef[] = [
  { id: 'run', label: '投放决策', desc: '输入 brief，拿到达人名单、预算分配与逐条证据', icon: Target },
  { id: 'proof', label: '效果验证', desc: '识别准确率、混淆矩阵与适用边界', icon: BarChart3 },
  { id: 'build', label: '技术实现', desc: '六 Agent 编排与 Python / TS 双实现一致性', icon: Wrench },
];

export const isTabId = (v: string): v is TabId => TABS.some((t) => t.id === v);

/** 页内段落 anchor，合并后的页签靠它承接旧 hash 的直达语义。 */
export const SECTION = {
  briefInput: 'sec-brief',
  trace: 'sec-trace',
  roster: 'sec-roster',
  baseline: 'sec-baseline',
  arch: 'sec-arch',
} as const;

/** 旧 hash → { 新页签, 段落 anchor }。README / PDF 里放出过的直达链接靠这张表继续生效。 */
export const LEGACY_HASH: Record<string, { tab: TabId; anchor?: string }> = {
  console: { tab: 'run', anchor: SECTION.briefInput },
  decision: { tab: 'run', anchor: SECTION.roster },
  eval: { tab: 'proof' },
  arch: { tab: 'build', anchor: SECTION.arch },
  notes: { tab: 'build', anchor: SECTION.arch },
  cost: { tab: 'build', anchor: SECTION.arch },
};

export const DEFAULT_TAB: TabId = 'run';

/** 解析 hash：返回新页签 id 与（若来自旧 hash）需要滚动到的段落。 */
export function resolveHash(hash: string): { tab: TabId; anchor?: string; legacy?: string } {
  const raw = hash.replace(/^#\/?/, '').trim();
  if (isTabId(raw)) return { tab: raw };
  const hit = LEGACY_HASH[raw];
  if (hit) return { tab: hit.tab, anchor: hit.anchor, legacy: raw };
  return { tab: DEFAULT_TAB };
}
