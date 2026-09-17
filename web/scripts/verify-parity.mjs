#!/usr/bin/env node
/**
 * verify-parity：TS 门禁引擎 vs Python 门禁引擎的**逐条一致性校验**。
 *
 * 为什么必须有这一步
 * ------------------
 * 前端如果自己写一套"看起来像"的规则，Demo 上的判定就只是演示动画。
 * 本项目的前端引擎与 Python 引擎共读同一份 `thresholds.json`，
 * 因此可以做一件更硬的事：把 5000 条达人**逐条**跑一遍，与 Python 落盘的
 * `output/verdicts.json` 比对 verdict / 三层分数 / 命中规则列表 / 分组键；
 * 再对带完整证据链的样本（`output/gate_results_sample.json`）比对每条 reason 的
 * 实际值、阈值、权重、越界深度、阈值来源、以及那句中文 human_text。
 *
 * 纪律
 * ----
 * - **结果只写 JSON，不写进 UI 代码**：产物 `public/data/consistency.json`，
 *   页面读它来渲染，数据重生成后数字自动跟着变，绝不会出现写死的 "5000/5000"；
 * - 有差异时**照实写进 JSON** 并以非 0 退出码中断 `npm run refresh`
 *   （加 `--allow-diff` 可以在明知有差异时仍继续构建，此时页面会显示红色失败状态）；
 * - 校验脚本 import 的就是页面用的那份 TS 源码（Node 原生类型擦除，不经二次编译），
 *   不存在"校验一套、页面另一套"。
 *
 * 用法： node scripts/verify-parity.mjs [--root ..] [--allow-diff]
 */

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import { NEUTRAL_SPEC, Thresholds, evaluate, pyRound, specFromDict } from '../src/engine/index.ts';
import { counterfactualRow, gateResultsFor, gtIndex, planBaseline, planCampaign } from '../src/budget/index.ts';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(HERE, '..');
const argRoot = process.argv.includes('--root') ? process.argv[process.argv.indexOf('--root') + 1] : null;
const ROOT = path.resolve(WEB_DIR, argRoot ?? process.env.KOXPILOT_ROOT ?? '..');
const ALLOW_DIFF = process.argv.includes('--allow-diff');
const OUT_DIR = path.join(WEB_DIR, 'public', 'data');
const OUT_FILE = path.join(OUT_DIR, 'consistency.json');

const MAX_DIFF_SAMPLES = 12;
const SCORE_FIELDS = [
  ['completeness', 'completeness'],
  ['authenticity', 'authenticity_score'],
  ['consistency', 'consistency_score'],
  ['brand_safety', 'brand_safety_score'],
  ['fraud_score', 'fraud_score'],
];
const REASON_FIELDS = ['gate', 'rule_id', 'signal', 'actual', 'threshold', 'weight', 'severity', 'source', 'depth', 'human_text'];

function loadJson(rel, required = true) {
  const abs = path.join(ROOT, rel);
  if (!fs.existsSync(abs)) {
    if (required) {
      console.error(`[verify] 缺少必需输入：${abs}`);
      process.exit(2);
    }
    return null;
  }
  return JSON.parse(fs.readFileSync(abs, 'utf8'));
}

function main() {
  const t0 = Date.now();
  const dataset = loadJson('data/kox_5000.json');
  const thrPayload = loadJson('output/thresholds.json');
  const reference = loadJson('output/verdicts.json');
  const evidenceRef = loadJson('output/gate_results_sample.json', false);

  const thresholds = Thresholds.fromDict(thrPayload);
  const records = dataset.kox;
  const refById = new Map((reference.verdicts ?? []).map((v) => [v.kox_id, v]));

  const fieldMismatch = { verdict: 0, group_key: 0, rules: 0, completeness: 0, authenticity: 0, consistency: 0, brand_safety: 0, fraud_score: 0 };
  const diffSamples = [];
  const tsCounts = { pass: 0, review: 0, reject: 0 };
  const refCounts = { pass: 0, review: 0, reject: 0 };
  let matched = 0;
  let missingInRef = 0;

  for (const kox of records) {
    // 库级中性口径（campaign 无关），与 Python `koxpilot gate` 落盘时的口径完全一致
    const res = evaluate(kox, NEUTRAL_SPEC, thresholds);
    tsCounts[res.verdict] += 1;
    const ref = refById.get(res.kox_id);
    if (!ref) {
      missingInRef += 1;
      continue;
    }
    refCounts[ref.verdict] = (refCounts[ref.verdict] ?? 0) + 1;

    const diffs = [];
    if (res.verdict !== ref.verdict) {
      diffs.push({ field: 'verdict', ts: res.verdict, py: ref.verdict });
      fieldMismatch.verdict += 1;
    }
    if (res.group_key !== ref.group_key) {
      diffs.push({ field: 'group_key', ts: res.group_key, py: ref.group_key });
      fieldMismatch.group_key += 1;
    }
    for (const [refKey, tsKey] of SCORE_FIELDS) {
      const tsVal = pyRound(res[tsKey], 4);
      const pyVal = ref[refKey];
      if (tsVal !== pyVal) {
        diffs.push({ field: refKey, ts: tsVal, py: pyVal });
        fieldMismatch[refKey] += 1;
      }
    }
    const tsRules = res.reasons.map((r) => r.rule_id);
    const pyRules = ref.rules ?? [];
    if (tsRules.length !== pyRules.length || tsRules.some((r, i) => r !== pyRules[i])) {
      diffs.push({ field: 'rules', ts: tsRules, py: pyRules });
      fieldMismatch.rules += 1;
    }

    if (diffs.length === 0) matched += 1;
    else if (diffSamples.length < MAX_DIFF_SAMPLES) diffSamples.push({ kox_id: res.kox_id, group_key: res.group_key, diffs });
  }

  // ---- 证据链（reason 级）比对：连 human_text 都要逐字符相同 --------------
  let evidence = { status: 'skipped', reason: 'output/gate_results_sample.json 不存在' };
  if (evidenceRef?.results) {
    const byId = new Map(records.map((k) => [String(k.kox_id), k]));
    let compared = 0;
    let reasonRows = 0;
    let evMatched = 0;
    const evDiffs = [];
    const evFieldMismatch = {};
    for (const refRes of evidenceRef.results) {
      const kox = byId.get(String(refRes.kox_id));
      if (!kox) continue;
      compared += 1;
      const res = evaluate(kox, NEUTRAL_SPEC, thresholds);
      const refReasons = refRes.reasons ?? [];
      let ok = res.reasons.length === refReasons.length;
      const localDiffs = [];
      for (let i = 0; i < Math.max(res.reasons.length, refReasons.length); i += 1) {
        const a = res.reasons[i];
        const b = refReasons[i];
        reasonRows += 1;
        if (!a || !b) {
          ok = false;
          localDiffs.push({ index: i, field: 'presence', ts: a?.rule_id ?? null, py: b?.rule_id ?? null });
          continue;
        }
        for (const f of REASON_FIELDS) {
          const av = a[f];
          const bv = b[f];
          if (JSON.stringify(av) !== JSON.stringify(bv)) {
            ok = false;
            evFieldMismatch[f] = (evFieldMismatch[f] ?? 0) + 1;
            localDiffs.push({ index: i, rule_id: a.rule_id, field: f, ts: av, py: bv });
          }
        }
      }
      if (ok) evMatched += 1;
      else if (evDiffs.length < MAX_DIFF_SAMPLES) evDiffs.push({ kox_id: refRes.kox_id, diffs: localDiffs.slice(0, 6) });
    }
    evidence = {
      status: compared > 0 && evMatched === compared ? 'pass' : 'fail',
      source: 'output/gate_results_sample.json',
      compared_records: compared,
      compared_reason_rows: reasonRows,
      matched_records: evMatched,
      diff_records: compared - evMatched,
      fields_compared: REASON_FIELDS,
      field_mismatch: evFieldMismatch,
      first_diffs: evDiffs,
      note: '逐条比对每个 reason 的信号、实际值、阈值、权重、越界深度、阈值来源与中文说明（human_text 要求逐字符相同）',
    };
  }

  // ---- 预算分配（A5）与反事实审计（A6）级比对 ------------------------------
  // 门禁一致只证明"判谁"一致；页面上"买谁、买多少、省多少钱"是另一套算法（贪心 + 配额修正 + gt 审计），
  // 所以这里对 3 个 campaign 的两条臂（KOXPilot / 按粉丝量基线）逐字段比对 Python 的 budget.json 与 audit.json。
  const budgetRef = loadJson('output/budget.json', false);
  const auditRef = loadJson('output/audit.json', false);
  const briefsRef = loadJson('data/briefs.json', false);
  let budgetParity = { status: 'skipped', reason: 'output/budget.json 或 data/briefs.json 不存在' };
  if (budgetRef?.plans && briefsRef) {
    const briefs = briefsRef.briefs ?? briefsRef;
    const specById = new Map(briefs.map((b) => [String(b.spec?.campaign_id ?? b.brief_id), specFromDict(b.spec ?? b)]));
    const gtById = gtIndex(records);
    const auditByCampaign = new Map(
      (auditRef?.counterfactual_value_audit?.per_campaign ?? []).map((r) => [String(r.campaign_id), r]),
    );
    const PLAN_SCALARS = [
      'spent_usd', 'utilization', 'n_selected', 'n_posts', 'candidate_pool', 'n_price_estimated',
      'est_total_views', 'est_effective_views', 'est_total_engagements', 'est_cpm_usd', 'est_cpe_usd',
    ];
    const AUDIT_SCALARS = [
      'n_fraud_selected', 'n_high_risk_selected', 'fraud_spend_usd', 'high_risk_spend_usd',
      'wasted_spend_usd', 'wasted_spend_share', 'spend_on_gate_review_usd', 'spend_on_gate_reject_usd',
      'nominal_views', 'effective_views_gt', 'effective_views_gt_lenient', 'effective_views_per_1k_usd',
    ];
    const arms = [];
    const auditRows = [];
    const budgetDiffs = [];
    let armMatched = 0;
    let auditMatched = 0;

    for (const refPlan of budgetRef.plans) {
      const cid = String(refPlan.campaign_id);
      const spec = specById.get(cid);
      if (!spec) continue;
      const results = gateResultsFor(records, spec, thresholds);
      const tsPlan = planCampaign(records, spec, thresholds, results);
      const tsBase = planBaseline(records, spec, thresholds, results);

      for (const [armName, tsArm, pyArm] of [
        ['koxpilot', tsPlan, refPlan.koxpilot],
        ['baseline_followers', tsBase, refPlan.baseline_followers],
      ]) {
        const diffs = [];
        for (const f of PLAN_SCALARS) {
          if (tsArm[f] !== pyArm[f]) diffs.push({ field: f, ts: tsArm[f], py: pyArm[f] });
        }
        for (const mixKey of ['tier_mix', 'country_mix', 'platform_mix']) {
          const tsMix = Object.fromEntries(Object.entries(tsArm[mixKey]).map(([k, v]) => [k, pyRound(v, 4)]));
          if (JSON.stringify(tsMix) !== JSON.stringify(pyArm[mixKey])) {
            diffs.push({ field: mixKey, ts: tsMix, py: pyArm[mixKey] });
          }
        }
        const tsIds = tsArm.selected.map((a) => a.kox_id);
        const pyIds = (pyArm.selected ?? []).map((a) => a.kox_id);
        if (JSON.stringify(tsIds) !== JSON.stringify(pyIds)) {
          const firstIdx = tsIds.findIndex((v, i) => v !== pyIds[i]);
          diffs.push({ field: 'selected[].kox_id', first_index: firstIdx, ts: tsIds[firstIdx] ?? null, py: pyIds[firstIdx] ?? null });
        } else {
          for (let i = 0; i < tsArm.selected.length; i += 1) {
            const a = tsArm.selected[i];
            const b = pyArm.selected[i];
            for (const [f, digits] of [['amount_usd', 2], ['posts', 0], ['effective_posts', 4], ['est_views', 2], ['est_effective_views', 2], ['value_score', 6], ['efficiency', 6], ['picked_by', null]]) {
              const tv = digits === null ? a[f] : pyRound(a[f], digits);
              if (tv !== b[f]) {
                diffs.push({ field: `selected[${i}].${f}`, kox_id: a.kox_id, ts: tv, py: b[f] });
                break;
              }
            }
            if (diffs.length > 0) break;
          }
        }
        const tsChecks = tsArm.constraints.checks.map((c) => [c.name, c.actual, c.satisfied, c.enforced]);
        const pyChecks = (pyArm.constraints?.checks ?? []).map((c) => [c.name, c.actual, c.satisfied, c.enforced]);
        if (JSON.stringify(tsChecks) !== JSON.stringify(pyChecks)) {
          diffs.push({ field: 'constraints.checks', ts: tsChecks, py: pyChecks });
        }
        if (JSON.stringify(tsArm.trace) !== JSON.stringify(pyArm.trace)) {
          diffs.push({ field: 'trace', ts: tsArm.trace, py: pyArm.trace });
        }
        if (diffs.length === 0) armMatched += 1;
        else if (budgetDiffs.length < MAX_DIFF_SAMPLES) budgetDiffs.push({ campaign_id: cid, arm: armName, diffs: diffs.slice(0, 4) });
        arms.push({
          campaign_id: cid,
          arm: armName,
          matched: diffs.length === 0,
          n_selected: tsArm.n_selected,
          n_posts: tsArm.n_posts,
          spent_usd: tsArm.spent_usd,
          utilization: tsArm.utilization,
          est_cpm_usd: tsArm.est_cpm_usd,
        });
      }

      // A6：用 gt 当裁判重算反事实账，与 Python audit.json 对齐
      const pyAudit = auditByCampaign.get(cid);
      if (pyAudit) {
        const tsRow = counterfactualRow(cid, tsPlan, tsBase, gtById);
        const diffs = [];
        for (const f of ['saved_usd', 'saved_share_of_budget', 'effective_view_uplift', 'effective_view_uplift_lenient']) {
          if (tsRow[f] !== pyAudit[f]) diffs.push({ field: f, ts: tsRow[f], py: pyAudit[f] });
        }
        for (const arm of ['baseline', 'koxpilot']) {
          for (const f of AUDIT_SCALARS) {
            if (tsRow[arm][f] !== pyAudit[arm][f]) diffs.push({ field: `${arm}.${f}`, ts: tsRow[arm][f], py: pyAudit[arm][f] });
          }
        }
        if (diffs.length === 0) auditMatched += 1;
        else if (budgetDiffs.length < MAX_DIFF_SAMPLES) budgetDiffs.push({ campaign_id: cid, arm: 'audit', diffs: diffs.slice(0, 6) });
        auditRows.push({
          campaign_id: cid,
          matched: diffs.length === 0,
          saved_usd: tsRow.saved_usd,
          effective_view_uplift: tsRow.effective_view_uplift,
        });
      }
    }

    const nArms = arms.length;
    const nAudits = auditRows.length;
    budgetParity = {
      status: nArms > 0 && armMatched === nArms && auditMatched === nAudits ? 'pass' : 'fail',
      source: ['output/budget.json', auditRef ? 'output/audit.json' : null].filter(Boolean),
      compared_arms: nArms,
      matched_arms: armMatched,
      compared_audits: nAudits,
      matched_audits: auditMatched,
      arms,
      audits: auditRows,
      fields_compared: [...PLAN_SCALARS, 'tier/country/platform_mix', 'selected[].kox_id 顺序', 'selected[].amount_usd/posts/value_score/picked_by', 'constraints.checks', 'trace[]（含中文措辞）', ...AUDIT_SCALARS.map((f) => `audit.${f}`)],
      first_diffs: budgetDiffs,
      note: '两臂各自跑一遍 TS 贪心分配与结构修正，逐字段对齐 Python；反事实审计一律以 gt 为裁判（不使用引擎自身分数）',
    };
  }

  const total = records.length;
  const diffCount = total - matched - missingInRef;
  const status =
    diffCount === 0 && missingInRef === 0 && evidence.status !== 'fail' && budgetParity.status !== 'fail'
      ? 'pass'
      : 'fail';

  const payload = {
    schema: 'koxpilot-consistency/1',
    status,
    generated_at_unix: Math.floor(Date.now() / 1000),
    elapsed_ms: Date.now() - t0,
    engine: { implementation: 'typescript', entry: 'src/engine/engine.ts', runtime: `node ${process.version}` },
    reference: {
      implementation: 'python',
      file: 'output/verdicts.json',
      scope: reference.meta?.note ?? '',
      dataset_sha256: reference.meta?.dataset_sha256 ?? null,
      n: reference.meta?.n ?? null,
      counts: reference.meta?.counts ?? null,
    },
    thresholds: {
      file: 'output/thresholds.json',
      version: thrPayload.meta?.thresholds_version ?? null,
      n_groups: Object.keys(thrPayload.groups ?? {}).length,
      n_fallback_cells: thrPayload.meta?.n_fallback_cells ?? null,
      note: '双实现读同一份阈值表，TS 侧不做任何再标定',
    },
    campaign_scope: '库级中性画像（campaign 无关），与 Python `koxpilot gate` 落盘口径一致',
    dataset: { sha256_from_reference: reference.meta?.dataset_sha256 ?? null, version: dataset.meta?.dataset_version ?? null, seed: dataset.meta?.seed ?? null, n: total },
    verdict: {
      total,
      matched,
      diff_count: diffCount,
      missing_in_reference: missingInRef,
      match_rate: total ? pyRound(matched / total, 6) : 0,
      fields_compared: ['verdict', 'group_key', 'rules[]', 'completeness', 'authenticity', 'consistency', 'brand_safety', 'fraud_score'],
      field_mismatch: fieldMismatch,
      first_diffs: diffSamples,
      counts_ts: tsCounts,
      counts_python: refCounts,
    },
    evidence,
    budget: budgetParity,
    honesty_notes: [
      '本文件由 scripts/verify-parity.mjs 在每次 `npm run refresh` 时重新生成，页面直接读取，不存在写死的一致性数字。',
      '比对口径：分数按 Python 落盘精度四位小数比较；命中规则列表要求顺序完全一致。',
      diffCount === 0
        ? '当前无差异：同一份 thresholds.json 下，TS 与 Python 对 5000 条数据给出完全相同的判定。'
        : `当前存在 ${diffCount} 条差异，已列出前 ${diffSamples.length} 条明细，页面会以失败状态展示。`,
    ],
  };

  fs.mkdirSync(OUT_DIR, { recursive: true });
  fs.writeFileSync(OUT_FILE, JSON.stringify(payload, null, 2));

  console.log(`[verify] 数据集 ${total} 条，dataset_sha256=${(reference.meta?.dataset_sha256 ?? '?').slice(0, 12)}…`);
  console.log(`[verify] 判定级：一致 ${matched}/${total}，差异 ${diffCount}，参考缺失 ${missingInRef}`);
  console.log(`[verify] TS 三档：${JSON.stringify(tsCounts)}｜Python 三档：${JSON.stringify(refCounts)}`);
  if (diffCount > 0) {
    console.log(`[verify] 字段差异计数：${JSON.stringify(fieldMismatch)}`);
    for (const d of diffSamples.slice(0, 5)) {
      console.log(`  - ${d.kox_id} ${JSON.stringify(d.diffs).slice(0, 400)}`);
    }
  }
  if (evidence.status !== 'skipped') {
    console.log(`[verify] 证据链级：${evidence.matched_records}/${evidence.compared_records} 条完全一致（含 human_text 逐字符），比对 ${evidence.compared_reason_rows} 行 reason`);
    if (evidence.status === 'fail') {
      console.log(`[verify] 证据链字段差异：${JSON.stringify(evidence.field_mismatch)}`);
      for (const d of evidence.first_diffs.slice(0, 3)) console.log(`  - ${d.kox_id} ${JSON.stringify(d.diffs).slice(0, 500)}`);
    }
  }
  if (budgetParity.status !== 'skipped') {
    console.log(
      `[verify] 预算/审计级：分配臂 ${budgetParity.matched_arms}/${budgetParity.compared_arms} 一致，` +
        `反事实审计 ${budgetParity.matched_audits}/${budgetParity.compared_audits} 一致`,
    );
    if (budgetParity.status === 'fail') {
      for (const d of budgetParity.first_diffs.slice(0, 4)) {
        console.log(`  - ${d.campaign_id}/${d.arm} ${JSON.stringify(d.diffs).slice(0, 500)}`);
      }
    }
  }
  console.log(`[verify] 状态 ${status}，耗时 ${payload.elapsed_ms}ms -> ${path.relative(WEB_DIR, OUT_FILE)}`);

  if (status !== 'pass' && !ALLOW_DIFF) {
    console.error('[verify] 存在差异，已中断（如需带着差异继续构建，加 --allow-diff，页面会显示失败状态）');
    process.exit(1);
  }
}

main();
