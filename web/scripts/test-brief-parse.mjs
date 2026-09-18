/**
 * briefParse.ts 的自检脚本（node 直接跑，不引入测试框架）。
 *
 * 为什么单独一个脚本而不是挂进 verify：
 * `pnpm run verify` 校验的是 Python/TS 双实现的判定一致性（硬红线 0 差异），
 * briefParse 是**额外**的线上规则解析能力，不参与那条校验，
 * 混进去会污染「0 差异」这个指标的含义。所以它自己一条 script：`pnpm run test:brief`。
 *
 * 断言三类：
 * 1. 3 个预置 brief 上，规则解析与构建期 LLM 解析（data/briefs.json 的 spec）逐字段对撞，
 *    并把差异如实打出来（差异**允许存在**，但必须稳定：低于阈值就算回归）；
 * 2. 各类写法的预算/市场/年龄单元用例；
 * 3. 残缺输入必须走降级而不是抛异常，且降级要在 evidence 里标出来。
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

// 直接 import 页面用的那份 TS 源码（Node 原生类型擦除，与 verify-parity.mjs 同一手法），
// 不引入任何测试框架，也不做二次编译 —— 脚本测的就是浏览器里跑的那份代码。
import { diffSpec, diffSummary, parseBrief } from '../src/engine/briefParse.ts';

const here = dirname(fileURLToPath(import.meta.url));

const briefs = JSON.parse(readFileSync(resolve(here, '../public/data/briefs.json'), 'utf8')).briefs;

let fail = 0;
const ok = (cond, msg, extra = '') => {
  if (cond) {
    console.log(`  ✔ ${msg}`);
  } else {
    fail += 1;
    console.log(`  ✘ ${msg}${extra ? ` → ${extra}` : ''}`);
  }
};

console.log('\n[1] 3 个预置 brief：构建期 LLM 解析 vs 线上规则解析');
const perBrief = [];
for (const b of briefs) {
  const parsed = parseBrief(b.raw_text, { campaignId: b.spec.campaign_id, name: b.name });
  const rows = diffSpec(b.spec, parsed.spec);
  const sum = diffSummary(rows);
  perBrief.push({ id: b.brief_id, sum, rows });
  console.log(`\n  ${b.brief_id} ${b.name}：一致 ${sum.same}/${sum.total}（${(sum.rate * 100).toFixed(0)}%）`);
  for (const r of rows.filter((x) => !x.same)) {
    console.log(`    · ${r.label}：LLM=${r.llm} / 规则=${r.rule}  ${r.note}`);
  }
  // 硬性要求：结构性字段（品类/市场/语言/平台/预算/KPI/性别/年龄/受管制）必须完全一致，
  // 只允许 competitor_brands 有差（LLM 会凭品类常识补竞品，规则版只认原文出现的品牌）。
  const hardFields = rows.filter((r) => r.field !== 'competitor_brands');
  ok(
    hardFields.every((r) => r.same),
    `${b.brief_id} 除竞品外 9 个字段全一致`,
    hardFields.filter((r) => !r.same).map((r) => r.label).join(','),
  );
}
const totalSame = perBrief.reduce((s, x) => s + x.sum.same, 0);
const totalAll = perBrief.reduce((s, x) => s + x.sum.total, 0);
console.log(`\n  合计字段一致率：${totalSame}/${totalAll} = ${((totalSame / totalAll) * 100).toFixed(1)}%`);
ok(totalSame / totalAll >= 0.9, '合计字段一致率 ≥ 90%', `${((totalSame / totalAll) * 100).toFixed(1)}%`);

console.log('\n[2] 单字段写法用例');
const cases = [
  ['预算 8 万美元', (s) => s.budget_usd === 80000, 'budget 8万美元 → 80000'],
  ['预算 $80,000 投北美', (s) => s.budget_usd === 80000, '$80,000 → 80000'],
  ['预算 80000 美元', (s) => s.budget_usd === 80000, '80000 美元 → 80000'],
  ['预算 12 万，投美国', (s) => s.budget_usd === 120000, '12 万（无货币词）→ 120000'],
  ['预算 4.5 万美金', (s) => s.budget_usd === 45000, '4.5 万美金 → 45000'],
  ['投东南亚，预算 3 万美元', (s) => s.target_markets.join(',') === 'ID,MY,VN,TH,PH,SG', '东南亚 → 6 国'],
  ['主打北美，预算 3 万美元', (s) => s.target_markets.join(',') === 'US,CA', '北美 → US,CA'],
  ['投东南亚，重点印尼和越南，预算 3 万美元', (s) => s.target_markets.join(',') === 'ID,VN', '具体国家优先于区域词'],
  ['目标 25-40 岁女性', (s) => s.target_age_buckets.join(',') === '25-34,35-44' && s.target_gender === 'f', '25-40 岁女性'],
  ['主要人群 18-34 岁男性', (s) => s.target_age_buckets.join(',') === '18-24,25-34' && s.target_gender === 'm', '18-34 岁男性'],
  ['抖音国际版和油管为主', (s) => s.platforms.includes('tiktok') && s.platforms.includes('youtube'), '中文别名 → tiktok/youtube'],
  ['彩妆新品，看重转化', (s) => s.kpi === 'conversion' && s.target_categories[0] === 'beauty_care', '彩妆 + 转化'],
  ['休闲手游，先把曝光打出来', (s) => s.kpi === 'reach' && s.target_categories[0] === 'gaming_app', '手游 + 曝光→reach'],
  ['小家电，希望把口碑和互动做起来', (s) => s.kpi === 'engagement' && s.target_categories[0] === 'home_appliance', '小家电 + 互动'],
  ['3C 数码，避开 Anker', (s) => s.competitor_brands.includes('Anker'), '回避语境 + 品牌 → Anker'],
  ['我们是 Anker 的代理', (s) => s.competitor_brands.length === 0, '无回避语境时不把品牌当竞品'],
  ['儿童玩具投放，品牌安全按儿童向审', (s) => s.regulated_category === 'kids', '儿童向 → kids'],
];
for (const [text, assert, msg] of cases) {
  const r = parseBrief(text);
  ok(assert(r.spec), msg, JSON.stringify({ budget: r.spec.budget_usd, markets: r.spec.target_markets, age: r.spec.target_age_buckets, kpi: r.spec.kpi, cats: r.spec.target_categories, plat: r.spec.platforms, comp: r.spec.competitor_brands }));
}

console.log('\n[3] 残缺输入必须降级而不是崩');
for (const text of ['', '我们想做一波投放，尽快上线。', '？？？', 'aaaaaaa']) {
  let r;
  try {
    r = parseBrief(text);
  } catch (e) {
    ok(false, `残缺输入不抛异常：${JSON.stringify(text)}`, String(e));
    continue;
  }
  const defaults = r.evidence.filter((e) => e.status === 'default');
  ok(r.spec.budget_usd === 50000 && defaults.length > 0, `${JSON.stringify(text.slice(0, 12))} → 走默认值且 evidence 标了 ${defaults.length} 条 default`);
}

console.log('\n[4] 证据完整性：每个 spec 字段都要有证据行');
const r0 = parseBrief(briefs[0].raw_text);
const covered = new Set(r0.evidence.map((e) => e.field));
for (const f of ['budget_usd', 'target_categories', 'target_markets', 'target_languages', 'platforms', 'kpi', 'target_gender', 'target_age_buckets', 'competitor_brands', 'regulated_category']) {
  ok(covered.has(f), `字段 ${f} 有证据行`);
}
ok(r0.evidence.every((e) => e.rule && e.ruleText), '每条证据都带 rule id 与规则说明');
ok(r0.evidence.filter((e) => e.status === 'hit').every((e) => e.matched.length > 0), '每条 hit 证据都带原文片段');

console.log(`\n${fail === 0 ? '全部通过' : `${fail} 条失败`}\n`);
process.exit(fail === 0 ? 0 : 1);
