/**
 * A5 预算 + A6 审计的 TS 实现出口。
 * 浏览器与 `scripts/verify-parity.mjs` 用的是同一份代码：
 * 页面上的分配方案 = 校验脚本里与 Python `output/budget.json` 逐字段比对过的那一份。
 */

export * from './policy.ts';
export * from './value.ts';
export * from './allocator.ts';
export * from './planner.ts';
export * from './audit.ts';
