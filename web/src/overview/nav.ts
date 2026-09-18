import { BarChart3, Coins, Target, Wrench } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

/**
 * 导航定义。
 *
 * 为什么需要它：改版前 6 个 tab 的命名是「一篇报告的章节」
 *（决策台 / 决策与预算 / 评测 / 成本与价值 / 架构 / 工程日志），
 * 读者看不出这是个产品能干什么。这里改成 4 个**功能导向**的页签，
 * 每个页签自带「它回答什么问题」的一句话说明。
 *
 * 旧 hash 必须继续可用（README 与 PDF 里已经放出去了），
 * 所以 LEGACY_HASH 把 6 个旧 hash 映射到「新页签 + 页内段落 anchor」。
 */
export interface TabDef {
  id: TabId;
  label: string;
  /** 一句话说明：它回答什么问题 */
  desc: string;
  icon: LucideIcon;
}

export type TabId = 'run' | 'proof' | 'cost' | 'build';

export const TABS: TabDef[] = [
  { id: 'run', label: '投放决策', desc: '输入 brief，出名单和预算，每条都能点开看证据', icon: Target },
  { id: 'proof', label: '效果验证', desc: '它准不准、稳不稳、弱在哪', icon: BarChart3 },
  { id: 'cost', label: '成本账', desc: '花了多少 token、值不值、Prompt 怎么迭代的', icon: Coins },
  { id: 'build', label: '工程实现', desc: '怎么编排、双实现怎么校验、踩过哪些坑', icon: Wrench },
];

export const isTabId = (v: string): v is TabId => TABS.some((t) => t.id === v);

/** 页内段落 anchor，合并后的页签靠它承接旧 hash 的直达语义。 */
export const SECTION = {
  briefInput: 'sec-brief',
  trace: 'sec-trace',
  roster: 'sec-roster',
  baseline: 'sec-baseline',
  arch: 'sec-arch',
  notes: 'sec-notes',
} as const;

/** 旧 hash → { 新页签, 段落 anchor }。README / PDF 里的 6 个直达链接靠这张表继续生效。 */
export const LEGACY_HASH: Record<string, { tab: TabId; anchor?: string }> = {
  console: { tab: 'run', anchor: SECTION.briefInput },
  decision: { tab: 'run', anchor: SECTION.roster },
  eval: { tab: 'proof' },
  arch: { tab: 'build', anchor: SECTION.arch },
  notes: { tab: 'build', anchor: SECTION.notes },
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
