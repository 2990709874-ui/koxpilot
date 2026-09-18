#!/usr/bin/env node
/**
 * verify-service-parity：**真 Python 服务** vs 浏览器 TS 引擎的逐条一致性校验（命令行版）。
 *
 * 与 `verify-parity.mjs` 的区别：
 * - `verify-parity.mjs` 比的是 TS 引擎 vs Python **落盘产物**（构建期）；
 * - 本脚本比的是 TS 引擎 vs Python **HTTP 服务**（运行期），也就是页面上那条
 *   「Python 服务与浏览器引擎：N 条逐条一致」到底能不能站得住。
 *
 * 做的事和前端一模一样（见 `src/tabs/Console.tsx` 的 runService）：
 * 1. 调 `POST /api/plan`；
 * 2. 取 `parity_payload.verdicts` 里的 kox_id 作为同一批候选；
 * 3. 用 TS 引擎对同一批 kox_id 重算，逐条比对 verdict；
 * 4. 再抽样调 `GET /api/kox/{id}/explain`，比对 verdict 与每条证据的 human_text。
 *
 * 用法::
 *
 *     node scripts/verify-service-parity.mjs [--base http://127.0.0.1:8399]
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import { Thresholds } from '../src/engine/thresholds.ts';
import { evaluate } from '../src/engine/engine.ts';
import { parseBrief } from '../src/engine/briefParse.ts';
import { specFromDict, NEUTRAL_SPEC } from '../src/engine/types.ts';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DATA = path.join(HERE, '..', 'public', 'data');
const readJson = (name) => JSON.parse(readFileSync(path.join(DATA, name), 'utf8'));

const argIdx = process.argv.indexOf('--base');
const BASE = (argIdx >= 0 ? process.argv[argIdx + 1] : 'http://127.0.0.1:8399').replace(/\/+$/, '');

const koxPayload = readJson('kox.json');
const records = koxPayload.kox ?? koxPayload;
const byId = new Map(records.map((r) => [String(r.kox_id), r]));
const thresholds = Thresholds.fromDict(readJson('thresholds.json'));
const briefs = readJson('briefs.json').briefs ?? [];

async function post(pathname, body) {
  const res = await fetch(`${BASE}${pathname}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return res.json();
}

let failures = 0;

/** 一次 plan 的逐条比对。 */
async function checkPlan(label, body, spec) {
  const data = await post('/api/plan', body);
  if (!data.ok) {
    failures += 1;
    console.log(`✗ ${label}：服务返回 ok:false（${data.error?.code} ${data.error?.message}）`);
    return;
  }
  const rows = data.parity_payload.verdicts;
  let diff = 0;
  let missing = 0;
  const samples = [];
  for (const row of rows) {
    const kox = byId.get(row.kox_id);
    if (!kox) {
      missing += 1;
      continue;
    }
    const mine = evaluate(kox, spec, thresholds).verdict;
    if (mine !== row.verdict) {
      diff += 1;
      if (samples.length < 5) samples.push(`${row.kox_id}（服务 ${row.verdict} / TS ${mine}）`);
    }
  }
  const tag = diff === 0 && missing === 0 ? '✓' : '✗';
  if (diff !== 0 || missing !== 0) failures += 1;
  console.log(
    `${tag} ${label}：比对 ${rows.length} 条，差异 ${diff} 条` +
      (missing ? `，本地数据缺 ${missing} 条` : '') +
      (samples.length ? `｜${samples.join('，')}` : ''),
  );
}

/** explain 的证据链比对。 */
async function checkExplain(koxId) {
  const res = await fetch(`${BASE}/api/kox/${encodeURIComponent(koxId)}/explain`);
  const data = await res.json();
  if (!data.ok) {
    failures += 1;
    console.log(`✗ explain ${koxId}：${data.error?.code}`);
    return;
  }
  const mine = evaluate(byId.get(koxId), NEUTRAL_SPEC, thresholds);
  const problems = [];
  if (mine.verdict !== data.verdict) problems.push(`verdict ${data.verdict} != ${mine.verdict}`);
  const serviceTexts = data.gates.flatMap((g) => g.hits.map((h) => h.detail));
  const tsTexts = mine.reasons.map((r) => r.human_text);
  if (serviceTexts.length !== tsTexts.length) {
    problems.push(`证据条数 ${serviceTexts.length} != ${tsTexts.length}`);
  } else {
    for (let i = 0; i < serviceTexts.length; i += 1) {
      if (serviceTexts[i] !== tsTexts[i]) problems.push(`第 ${i + 1} 条理由文案不一致`);
    }
  }
  if (problems.length) {
    failures += 1;
    console.log(`✗ explain ${koxId}：${problems.join('；')}`);
  } else {
    console.log(`✓ explain ${koxId}：判定 ${data.verdict}，证据 ${serviceTexts.length} 条逐字一致`);
  }
}

const health = await (await fetch(`${BASE}/api/health`)).json();
console.log(`服务状态：${health.status}｜数据 ${health.meta.kox_count} 条｜sha256 ${health.meta.dataset_sha256.slice(0, 12)}…`);
if (health.meta.dataset_sha256 !== readJson('manifest.json').dataset.sha256) {
  failures += 1;
  console.log('✗ 服务与前端不是同一份数据集（dataset_sha256 不一致）');
} else {
  console.log('✓ 服务与前端读的是同一份数据集（dataset_sha256 一致）');
}

for (const b of briefs) {
  await checkPlan(`${b.brief_id} 预置`, { brief_id: b.brief_id, options: { top_n: 500, explain_limit: 60 } }, specFromDict(b.spec));
}

const freeText = '巴西和墨西哥的 Instagram 彩妆，18-24 女性，预算 8 万美金，避开 Focallure';
await checkPlan(
  '自由文本',
  { brief_text: freeText, options: { top_n: 500, explain_limit: 60 } },
  parseBrief(freeText, { campaignId: 'CUSTOM', name: '自定义 brief' }).spec,
);

for (const id of ['KOX-000002', 'KOX-000004', 'KOX-000005', 'KOX-000007', 'KOX-001146']) {
  await checkExplain(id);
}

console.log('');
console.log(failures === 0 ? '全部一致：0 处差异' : `发现 ${failures} 处差异`);
process.exit(failures === 0 ? 0 : 1);
