#!/usr/bin/env node
/**
 * prepare-data：把 Python 侧产物搬成前端可直接 fetch 的数据集。
 *
 * 设计纪律（数据会被重新生成，脚本必须完全无手工步骤）：
 * 1. **只读、可重跑**：不修改 `../data`、`../output` 任何文件，重复执行结果一致；
 * 2. **产物缺失不报错，只登记**：LLM 相关实验（prompt_bench / llm_bench）可能还没跑，
 *    manifest 里如实标 `present: false`，前端据此优雅降级，绝不用假数据顶上；
 * 3. **口径一致性自检**：所有产物的 `meta.dataset_sha256` 必须相同，
 *    否则说明 output/ 里混了不同数据版本的文件 —— 写进 manifest.warnings，UI 顶部亮红条；
 * 4. **不做任何指标计算**：这里只搬运和瘦身，数字一律由 Python 产物或浏览器内 TS 引擎产生。
 *
 * 用法： node scripts/prepare-data.mjs [--root ../]
 */

import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(HERE, '..');
const argRoot = process.argv.includes('--root') ? process.argv[process.argv.indexOf('--root') + 1] : null;
const ROOT = path.resolve(WEB_DIR, argRoot ?? process.env.KOXPILOT_ROOT ?? '..');
const OUT_DIR = path.join(WEB_DIR, 'public', 'data');

/** 前端与 UI 都不需要、纯粹增加体积的字段（引擎判定不读它们）。 */
const DROP_KOX_FIELDS = new Set(['comment_lang_mismatch_rate', '_missing']);
/** ground truth 里只保留评测页真正要用的标签（引擎读不到 gt，这里只服务混淆矩阵）。 */
const KEEP_GT_FIELDS = ['is_fraud', 'fraud_type', 'tag_mismatch', 'brand_safety', 'verdict'];

const sha256 = (buf) => createHash('sha256').update(buf).digest('hex');
const fmtMB = (n) => `${(n / 1024 / 1024).toFixed(2)} MB`;

function readJson(absPath) {
  const buf = fs.readFileSync(absPath);
  return { data: JSON.parse(buf.toString('utf8')), bytes: buf.length, sha256: sha256(buf) };
}

function writeJson(name, payload) {
  const text = JSON.stringify(payload);
  fs.writeFileSync(path.join(OUT_DIR, name), text);
  return Buffer.byteLength(text);
}

/** 达人记录瘦身：保留引擎判定 + 卡片展示需要的一切，其余丢掉。 */
function slimKox(record) {
  const out = {};
  for (const [k, v] of Object.entries(record)) {
    if (DROP_KOX_FIELDS.has(k) || k === 'gt') continue;
    out[k] = v;
  }
  if (record.gt && typeof record.gt === 'object') {
    const gt = {};
    for (const key of KEEP_GT_FIELDS) if (key in record.gt) gt[key] = record.gt[key];
    out.gt = gt;
  }
  return out;
}

function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const warnings = [];
  const artifacts = [];

  // ---- 必需输入 ---------------------------------------------------------
  const koxPath = path.join(ROOT, 'data', 'kox_5000.json');
  const briefsPath = path.join(ROOT, 'data', 'briefs.json');
  const thrPath = path.join(ROOT, 'output', 'thresholds.json');
  for (const p of [koxPath, briefsPath, thrPath]) {
    if (!fs.existsSync(p)) {
      console.error(`[prepare-data] 缺少必需输入：${p}\n  先在 ${ROOT} 跑 make data && make gate`);
      process.exit(2);
    }
  }

  const kox = readJson(koxPath);
  const briefs = readJson(briefsPath);
  const thresholds = readJson(thrPath);

  const slim = kox.data.kox.map(slimKox);
  const koxBytes = writeJson('kox.json', { meta: kox.data.meta, kox: slim });
  artifacts.push({
    key: 'kox', file: 'data/kox.json', source: 'data/kox_5000.json', present: true,
    source_bytes: kox.bytes, shipped_bytes: koxBytes, source_sha256: kox.sha256,
    n: slim.length,
    note: `按字段白名单瘦身（丢弃 ${[...DROP_KOX_FIELDS].join('/')} 与 gt 的非评测字段），引擎判定所需字段全部保留`,
  });

  const briefsBytes = writeJson('briefs.json', briefs.data);
  artifacts.push({
    key: 'briefs', file: 'data/briefs.json', source: 'data/briefs.json', present: true,
    source_bytes: briefs.bytes, shipped_bytes: briefsBytes, source_sha256: briefs.sha256,
    n: (briefs.data.briefs ?? []).length,
  });

  const thrBytes = writeJson('thresholds.json', thresholds.data);
  artifacts.push({
    key: 'thresholds', file: 'data/thresholds.json', source: 'output/thresholds.json', present: true,
    source_bytes: thresholds.bytes, shipped_bytes: thrBytes, source_sha256: thresholds.sha256,
    n_groups: Object.keys(thresholds.data.groups ?? {}).length,
    note: '原样搬运：TS 引擎读的阈值与 Python 判定用的是同一份文件，不做任何再标定',
  });

  // ---- 可选产物：缺了就如实登记 present:false，前端降级显示 -------------
  const OPTIONAL = [
    ['metrics', 'output/metrics.json', 'metrics.json'],
    ['budget', 'output/budget.json', 'budget.json'],
    ['audit', 'output/audit.json', 'audit.json'],
    // multiseed：12 种子稳健性 + 三臂两段归因的唯一来源。它是"结论反转"那一页的证据，
    // 缺了 #cost 页只能展示单种子口径，因此必须搬进 bundle（整份搬，不挑字段）。
    ['multiseed', 'output/multiseed.json', 'multiseed.json'],
    ['prompt_bench', 'output/prompt_bench.json', 'prompt_bench.json'],
    ['llm_bench', 'output/llm_bench.json', 'llm_bench.json'],
    ['llm_compare', 'output/llm_compare.json', 'llm_compare.json'],
  ];
  const datasetShas = new Map([['verdicts_or_inputs:kox_5000.json', null]]);
  /** 产物整份 JSON.stringify 原样搬运，不做字段白名单 —— 这里登记以便核对。 */
  const shipped = new Map();

  for (const [key, rel, outName] of OPTIONAL) {
    const abs = path.join(ROOT, rel);
    if (!fs.existsSync(abs)) {
      artifacts.push({ key, file: null, source: rel, present: false, reason: '该实验尚未产出（前端按缺失降级）' });
      warnings.push(`产物缺失：${rel} —— 相关 Tab 会显示"未生成"，不会用占位数字冒充`);
      continue;
    }
    const j = readJson(abs);
    const bytes = writeJson(outName, j.data);
    const ds = j.data?.meta?.dataset_sha256 ?? null;
    if (ds) datasetShas.set(rel, ds);
    shipped.set(key, j.data);
    artifacts.push({
      key, file: `data/${outName}`, source: rel, present: true,
      source_bytes: j.bytes, shipped_bytes: bytes, source_sha256: j.sha256, dataset_sha256: ds,
      top_level_keys: Object.keys(j.data ?? {}),
      note: '原样整份搬运（不做字段白名单）：Python 侧新增的字段会自动出现在前端，不需要改本脚本',
    });
  }

  // ---- 关键字段在位自检：页面上的重口径结论依赖它们，缺一个就该在 UI 顶部亮条 ----
  const at = (obj, dotted) => dotted.split('.').reduce((o, k) => (o === null || o === undefined ? undefined : o[k]), obj);
  const REQUIRED_FIELDS = [
    ['audit', 'counterfactual_value_audit.value_attribution', '三臂链式差分归因（#cost 的两段归因面板）'],
    ['audit', 'counterfactual_value_audit.totals.effective_view_uplift_bounded', '有界 uplift 口径（rate_gap_pp / symmetric_uplift）'],
    ['metrics', 'budget_decay_sensitivity', 'decay 建模假设的三档敏感性扫描'],
    ['metrics', 'table_4_ablation.contribution_criteria', '消融 contribution 三态判据'],
    ['metrics', 'table_4_ablation.negative_rules', '负贡献规则清单（G1.6）'],
    ['metrics', 'table_6_llm_vs_rule.semantic_fit_llm_vs_rule', 'A4 语义适配 LLM vs 规则量化审计'],
    ['multiseed', 'A_value_robustness.arm_attribution', '12 种子两段归因统计（结论反转的证据）'],
    ['metrics', 'budget_decay_sensitivity.delivery_policy', '分层交付策略（selection_stable=false 的分流动作，#cost/#decision 都读它）'],
    ['metrics', 'budget_decay_sensitivity.caveats', 'decay 扫描的 caveats（与 blockers 分列，缺它读者只会看到一片绿）'],
    ['multiseed', 'B_variance_attribution.independence_unit', '方差归因的独立单位口径（pooled p 值是否可用）'],
  ];
  const fieldAudit = REQUIRED_FIELDS.map(([key, dotted, what]) => {
    const present = shipped.has(key) && at(shipped.get(key), dotted) !== undefined;
    if (!present) {
      warnings.push(`字段缺失：${key}.${dotted}（${what}）—— 对应面板会降级为"未生成"，不会用旧口径的数字顶上`);
    }
    return { artifact: key, path: dotted, what, present };
  });

  // ---- 口径一致性自检：所有产物必须指向同一份达人库 ----------------------
  const verdictsPath = path.join(ROOT, 'output', 'verdicts.json');
  let verdictsDatasetSha = null;
  if (fs.existsSync(verdictsPath)) {
    // verdicts.json 不进 bundle（前端用 TS 引擎实时重算），只读它的 meta 做口径校验
    const head = JSON.parse(fs.readFileSync(verdictsPath, 'utf8'));
    verdictsDatasetSha = head?.meta?.dataset_sha256 ?? null;
    if (verdictsDatasetSha) datasetShas.set('output/verdicts.json', verdictsDatasetSha);
    artifacts.push({
      key: 'verdicts_reference', file: null, source: 'output/verdicts.json', present: true,
      source_bytes: fs.statSync(verdictsPath).size, dataset_sha256: verdictsDatasetSha,
      note: '刻意不进 bundle：前端一致性面板读 data/consistency.json（由 verify-parity 生成），判定本身由浏览器内 TS 引擎实时重算',
    });
  } else {
    warnings.push('缺少 output/verdicts.json，无法做双实现一致性校验');
  }

  const koxSha = kox.sha256;
  const distinct = new Set([...datasetShas.values()].filter(Boolean));
  for (const [rel, sha] of datasetShas) {
    if (sha && sha !== koxSha) {
      warnings.push(`口径不一致：${rel} 的 dataset_sha256=${sha.slice(0, 12)}… 与当前 data/kox_5000.json 的 ${koxSha.slice(0, 12)}… 不同，请重跑 make all`);
    }
  }
  if (distinct.size > 1) warnings.push(`output/ 下混入了 ${distinct.size} 个不同数据版本的产物`);

  const manifest = {
    schema: 'koxpilot-web-manifest/1',
    generated_at_unix: Math.floor(Date.now() / 1000),
    source_root: path.relative(WEB_DIR, ROOT) || '.',
    dataset: {
      sha256: koxSha,
      version: kox.data.meta?.dataset_version ?? null,
      seed: kox.data.meta?.seed ?? null,
      n: slim.length,
      gt_verdict_counts: kox.data.meta?.verdict_counts ?? null,
      injection_actual_rates: kox.data.meta?.injection_actual_rates ?? null,
    },
    thresholds_meta: thresholds.data.meta ?? null,
    artifacts,
    field_audit: fieldAudit,
    warnings,
    note: '本文件由 scripts/prepare-data.mjs 生成；前端所有数字来自 artifacts 列出的产物或浏览器内 TS 引擎实时计算，无硬编码。可选产物一律整份搬运，不做字段白名单，因此 Python 侧新增字段无需改本脚本；field_audit 只做「在位与否」的自检。',
  };
  writeJson('manifest.json', manifest);

  // ---- 控制台报告 -------------------------------------------------------
  console.log(`[prepare-data] 源目录 ${ROOT}`);
  console.log(`[prepare-data] 达人库 ${slim.length} 条：${fmtMB(kox.bytes)} -> ${fmtMB(koxBytes)}（字段白名单瘦身）`);
  console.log(`[prepare-data] 阈值表 ${Object.keys(thresholds.data.groups ?? {}).length} 个分组，dataset=${koxSha.slice(0, 12)}…`);
  for (const a of artifacts) {
    const state = a.present ? `ok   ${a.file ?? '(不进 bundle)'}` : 'miss (前端降级)';
    console.log(`[prepare-data]   ${a.key.padEnd(18)} ${state}`);
  }
  const okFields = fieldAudit.filter((f) => f.present).length;
  console.log(`[prepare-data] 关键字段自检 ${okFields}/${fieldAudit.length} 在位：`);
  for (const f of fieldAudit) {
    console.log(`[prepare-data]   ${f.present ? 'ok  ' : 'MISS'} ${f.artifact}.${f.path}  — ${f.what}`);
  }
  if (warnings.length) {
    console.log('[prepare-data] 警告：');
    for (const w of warnings) console.log(`  - ${w}`);
  }
  console.log(`[prepare-data] 输出目录 ${path.relative(WEB_DIR, OUT_DIR)}`);
}

main();
