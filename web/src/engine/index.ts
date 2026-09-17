/**
 * KOXPilot TS 门禁引擎的统一出口。
 *
 * 浏览器与 Node 校验脚本（scripts/verify-parity.mjs）用的是**同一份**代码：
 * 前端展示的判定 = 一致性校验里跑的判定，不存在"页面一套、校验一套"。
 */

export * from './types.ts';
export * from './policy.ts';
export * from './stats.ts';
export * from './taxonomy.ts';
export * from './humanize.ts';
export * from './thresholds.ts';
export * from './signals.ts';
export * from './g0.ts';
export * from './g1.ts';
export * from './g2.ts';
export * from './g3.ts';
export * from './engine.ts';
