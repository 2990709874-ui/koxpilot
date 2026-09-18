#!/usr/bin/env node
/**
 * 契约形状的本地服务替身（Node 原生 http，零依赖）。
 *
 * 用途：真 Python 服务还没上线时，用它把前端的三态切换、实时一致性比对、
 * 错误路径全部测通。**严格按 `koxpilot/api/CONTRACT.md` 返回字段**，字段名不做任何改写。
 *
 * 数据取材于 `web/public/data/`（真实产物）；判定、预算、审计直接调用仓库里的
 * TS 引擎（Node 22 的类型剥离可以直接 import .ts），所以返回的数字不是编的。
 *
 * 环境变量（只影响这个替身，用来构造测试场景）：
 *   PORT=8000          监听端口
 *   MOCK_WARMING=2     前 N 次 /api/health 返回 status=warming
 *   MOCK_ERROR=internal  /api/plan 返回 HTTP 200 + ok:false + 该错误码
 *   MOCK_FLIP=3        把 parity_payload 里前 N 条判定改掉，用来验证「差异条数」显示
 *   MOCK_DELAY=0       每个请求额外延迟毫秒数
 */

import http from 'node:http';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import { Thresholds } from '../src/engine/thresholds.ts';
import { evaluate } from '../src/engine/engine.ts';
import { parseBrief } from '../src/engine/briefParse.ts';
import { specFromDict, NEUTRAL_SPEC } from '../src/engine/types.ts';
import { counterfactualRow, gtIndex, planBaseline, planCampaign, targetingReason } from '../src/budget/index.ts';
import { ruleLabel } from '../src/lib/pipeline.ts';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DATA = path.join(HERE, '..', 'public', 'data');

const PORT = Number(process.env.PORT ?? 8000);
const WARMING = Number(process.env.MOCK_WARMING ?? 0);
const ERROR_CODE = (process.env.MOCK_ERROR ?? '').trim();
const FLIP = Number(process.env.MOCK_FLIP ?? 0);
const DELAY = Number(process.env.MOCK_DELAY ?? 0);

const readJson = (name) => JSON.parse(readFileSync(path.join(DATA, name), 'utf8'));

/* ---------------- 数据集：首个请求时懒加载（和真服务的冷启动一致） ---------------- */

let store = null;
function load() {
  if (store) return store;
  const koxPayload = readJson('kox.json');
  const records = koxPayload.kox ?? koxPayload;
  const thresholdsPayload = readJson('thresholds.json');
  const briefs = readJson('briefs.json').briefs ?? [];
  const manifest = readJson('manifest.json');
  store = {
    records,
    thresholds: Thresholds.fromDict(thresholdsPayload),
    briefs,
    datasetSha256: manifest.dataset.sha256,
    thresholdsVersion: String(manifest.thresholds_meta?.thresholds_version ?? ''),
  };
  return store;
}

let healthCalls = 0;

const GATE_LABEL = { G0: '资料完整性', G1: '真实性', G2: '一致性', G3: '品牌安全' };
const VERDICT_ZH = { pass: '可投', review: '需人工复核', reject: '不建议投放' };

function meta(elapsedMs, extra = {}) {
  const s = load();
  return {
    engine: 'python',
    engine_version: '1.0.0',
    dataset_sha256: s.datasetSha256,
    kox_count: s.records.length,
    thresholds_source: 'output/thresholds.json',
    llm_runtime: {
      available: false,
      provider: null,
      reason: '未配置模型凭据，A1 使用确定性规则解析',
    },
    served_at: new Date().toISOString(),
    elapsed_ms: Math.round(elapsedMs),
    ...extra,
  };
}

/* ---------------- /api/plan ---------------- */

function humanReason(result) {
  if (result.reasons.length === 0) {
    return `${VERDICT_ZH[result.verdict]}：四层门禁逐条检查未发现越界信号，真实性 ${result.authenticity_score.toFixed(
      3,
    )}，语义适配 ${result.fit_score.toFixed(2)}。`;
  }
  const head = result.reasons
    .slice(0, 3)
    .map((r) => r.human_text)
    .join(' ');
  return `${VERDICT_ZH[result.verdict]}：${head}`;
}

function buildPlan(body) {
  const t0 = performance.now();
  const s = load();

  const briefId = body.brief_id ? String(body.brief_id) : null;
  let spec;
  let parse = null;
  let source;
  if (briefId) {
    const hit = s.briefs.find((b) => String(b.brief_id) === briefId);
    if (!hit) return { error: { code: 'not_found', message: `brief_id ${briefId} 不存在` } };
    spec = specFromDict(hit.spec);
    // 预置 brief 也把原文过一遍解析器，字段级证据才有东西可展示（与自由文本同一套规则）
    parse = parseBrief(String(hit.raw_text ?? ''), { campaignId: String(hit.spec.campaign_id ?? briefId), name: String(hit.name ?? briefId) });
    source = 'preset';
  } else {
    const text = String(body.brief_text ?? '').trim();
    if (!text) return { error: { code: 'bad_request', message: 'brief_text 为空' } };
    if (text.length > 2000) return { error: { code: 'bad_request', message: 'brief_text 超过 2000 字' } };
    parse = parseBrief(text, { campaignId: 'API', name: '接口请求' });
    spec = parse.spec;
    source = 'free_text';
  }

  const topN = Math.min(Number(body.options?.top_n ?? 200) || 200, 500);
  const explainLimit = Math.min(Number(body.options?.explain_limit ?? 60) || 60, 200);

  // A1
  const tA1 = performance.now();
  const fields = (parse?.evidence ?? []).map((e) => ({
    key: String(e.field),
    label: e.label,
    value: e.value,
    display: e.value,
    status: e.status,
    how: e.ruleText,
    matched: e.matched,
  }));
  const a1Ms = performance.now() - tA1;

  // A2 召回
  const tA2 = performance.now();
  const pool = [];
  for (const kox of s.records) {
    if (targetingReason(kox, spec) === null) pool.push(kox);
  }
  const recalled = pool.slice(0, topN);
  const a2Ms = performance.now() - tA2;

  // A3 门禁
  const tA3 = performance.now();
  const results = new Map();
  for (const kox of recalled) {
    const r = evaluate(kox, spec, s.thresholds);
    results.set(r.kox_id, r);
  }
  const a3Ms = performance.now() - tA3;

  // A4 语义适配（引擎内已算出 fit_score / fit_source）
  const tA4 = performance.now();
  const counts = { pass: 0, review: 0, reject: 0 };
  for (const r of results.values()) counts[r.verdict] += 1;
  const a4Ms = performance.now() - tA4;

  // A5 预算
  const tA5 = performance.now();
  const plan = planCampaign(recalled, spec, s.thresholds, results, { includeReview: false, decay: 0.7 });
  const baseline = planBaseline(recalled, spec, s.thresholds, results, { decay: 0.7 });
  const a5Ms = performance.now() - tA5;

  // A6 审计
  const tA6 = performance.now();
  const audit = counterfactualRow(spec.campaign_id, plan, baseline, gtIndex(recalled));
  const a6Ms = performance.now() - tA6;

  const allocById = new Map(plan.selected.map((a) => [a.kox_id, a]));
  const rank = { pass: 0, review: 1, reject: 2 };
  const ordered = [...results.values()].sort(
    (a, b) =>
      (allocById.get(b.kox_id)?.amount_usd ?? 0) - (allocById.get(a.kox_id)?.amount_usd ?? 0) ||
      rank[a.verdict] - rank[b.verdict] ||
      (a.kox_id < b.kox_id ? -1 : 1),
  );
  const byId = new Map(recalled.map((k) => [String(k.kox_id), k]));

  const candidates = ordered.slice(0, explainLimit).map((r) => {
    const kox = byId.get(r.kox_id) ?? {};
    const alloc = allocById.get(r.kox_id) ?? null;
    return {
      kox_id: r.kox_id,
      handle: String(kox.handle ?? ''),
      platform: String(kox.platform ?? ''),
      market: String(kox.country ?? ''),
      followers: Number(kox.followers ?? 0),
      verdict: r.verdict,
      gate_hits: r.reasons.map((x) => ({
        gate: x.gate,
        gate_label: GATE_LABEL[x.gate] ?? x.gate,
        signal_label: ruleLabel(x.rule_id),
        detail: x.human_text,
      })),
      fit_score: Number(r.fit_score.toFixed(4)),
      fit_source: r.fit_source === 'llm' ? 'cached_llm' : 'rule_fallback',
      reason_human: humanReason(r),
      allocated_usd: alloc ? Math.round(alloc.amount_usd * 100) / 100 : 0,
      expected_reach: alloc ? Math.round(alloc.est_views) : 0,
    };
  });

  const verdicts = [...results.values()]
    .map((r) => ({ kox_id: r.kox_id, verdict: r.verdict }))
    .sort((a, b) => (a.kox_id < b.kox_id ? -1 : 1));
  // 差异显示的验证开关：把前 N 条判定改成另一个值
  for (let i = 0; i < Math.min(FLIP, verdicts.length); i += 1) {
    verdicts[i] = { ...verdicts[i], verdict: verdicts[i].verdict === 'pass' ? 'review' : 'pass' };
  }

  const totalMs = performance.now() - t0;
  return {
    payload: {
      ok: true,
      brief: {
        source,
        parse_path: 'rule',
        fields,
      },
      funnel: [
        { stage: 'recall', label: '召回', count: recalled.length },
        { stage: 'gate', label: '通过门禁', count: counts.pass },
        { stage: 'allocated', label: '进入清单', count: plan.n_selected },
      ],
      candidates,
      allocation: {
        budget_usd: plan.budget_usd,
        allocated_usd: Math.round(plan.spent_usd * 100) / 100,
        unallocated_usd: Math.round((plan.budget_usd - plan.spent_usd) * 100) / 100,
        unallocated_why: '剩余额度低于单人最低起投',
        picked: plan.n_selected,
        arms: [
          {
            arm: 'koxpilot',
            label: 'KOXPilot 决策',
            spend: Math.round(plan.spent_usd * 100) / 100,
            expected_value: Math.round(plan.est_effective_views),
          },
          {
            arm: 'follower_rank',
            label: '按粉丝量排序',
            spend: Math.round(baseline.spent_usd * 100) / 100,
            expected_value: Math.round(baseline.est_effective_views),
          },
        ],
        saved_usd: Math.round(audit.saved_usd * 100) / 100,
        saved_share: Number(audit.saved_share_of_budget.toFixed(4)),
      },
      timings: [
        { agent: 'A1', label: 'brief 解析', ms: Math.round(a1Ms) },
        { agent: 'A2', label: '定向召回', ms: Math.round(a2Ms) },
        { agent: 'A3', label: '四层门禁', ms: Math.round(a3Ms) },
        { agent: 'A4', label: '语义适配', ms: Math.round(a4Ms) },
        { agent: 'A5', label: '预算分配', ms: Math.round(a5Ms) },
        { agent: 'A6', label: '成本与反事实审计', ms: Math.round(a6Ms) },
      ],
      parity_payload: { verdicts },
      meta: meta(totalMs),
    },
  };
}

/* ---------------- /api/kox/{id}/explain ---------------- */

function buildExplain(koxId) {
  const t0 = performance.now();
  const s = load();
  const kox = s.records.find((k) => String(k.kox_id) === koxId);
  if (!kox) return { error: { code: 'not_found', message: `kox_id ${koxId} 不存在` } };
  const r = evaluate(kox, NEUTRAL_SPEC, s.thresholds);
  const gates = ['G0', 'G1', 'G2', 'G3'].map((g) => {
    const hits = r.reasons.filter((x) => x.gate === g);
    return {
      gate: g,
      gate_label: GATE_LABEL[g],
      passed: hits.length === 0,
      hits: hits.map((x) => ({
        gate: g,
        gate_label: GATE_LABEL[g],
        signal_label: ruleLabel(x.rule_id),
        detail: x.human_text,
      })),
    };
  });
  return {
    payload: {
      ok: true,
      kox_id: r.kox_id,
      profile: {
        handle: String(kox.handle ?? ''),
        platform: String(kox.platform ?? ''),
        market: String(kox.country ?? ''),
        followers: Number(kox.followers ?? 0),
      },
      signals: r.reasons.map((x) => ({
        label: ruleLabel(x.rule_id),
        value: typeof x.actual === 'number' ? x.actual : 0,
        threshold: typeof x.threshold === 'number' ? x.threshold : null,
        verdict: 'out_of_range',
      })),
      gates,
      verdict: r.verdict,
      reason_human: humanReason(r),
      meta: meta(performance.now() - t0),
    },
  };
}

/* ---------------- /api/gate/batch ---------------- */

function buildGateBatch(body) {
  const t0 = performance.now();
  const s = load();
  const ids = Array.isArray(body.kox_ids) ? body.kox_ids.map(String) : [];
  if (ids.length > 5000) return { error: { code: 'too_many', message: '单批上限 5000 条' } };
  const wanted = new Set(ids);
  const verdicts = [];
  for (const kox of s.records) {
    if (!wanted.has(String(kox.kox_id))) continue;
    const r = evaluate(kox, NEUTRAL_SPEC, s.thresholds);
    verdicts.push({ kox_id: r.kox_id, verdict: r.verdict });
  }
  verdicts.sort((a, b) => (a.kox_id < b.kox_id ? -1 : 1));
  return { payload: { ok: true, verdicts, meta: meta(performance.now() - t0) } };
}

/* ---------------- HTTP ---------------- */

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

function send(res, payload, status = 200) {
  const body = JSON.stringify(payload);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', ...CORS });
  res.end(body);
}

function fail(res, code, message, elapsedMs = 0) {
  send(res, { ok: false, error: { code, message }, meta: meta(elapsedMs) });
}

function readBody(req) {
  return new Promise((resolve) => {
    let buf = '';
    req.on('data', (c) => {
      buf += c;
    });
    req.on('end', () => {
      try {
        resolve(buf ? JSON.parse(buf) : {});
      } catch {
        resolve(null);
      }
    });
  });
}

const server = http.createServer(async (req, res) => {
  if (DELAY > 0) await new Promise((r) => setTimeout(r, DELAY));
  const url = new URL(req.url ?? '/', `http://${req.headers.host ?? 'localhost'}`);
  const p = url.pathname;

  if (req.method === 'OPTIONS') {
    res.writeHead(204, CORS);
    res.end();
    return;
  }

  try {
    if (req.method === 'GET' && p === '/api/health') {
      const t0 = performance.now();
      healthCalls += 1;
      if (healthCalls <= WARMING) {
        send(res, { ok: true, status: 'warming', meta: meta(performance.now() - t0) });
        return;
      }
      load();
      send(res, { ok: true, status: 'ready', meta: meta(performance.now() - t0) });
      return;
    }

    if (req.method === 'POST' && p === '/api/plan') {
      const body = await readBody(req);
      if (body === null) {
        fail(res, 'bad_request', '请求体不是合法 JSON');
        return;
      }
      if (ERROR_CODE) {
        fail(res, ERROR_CODE, `服务侧构造的测试错误（${ERROR_CODE}）`);
        return;
      }
      const out = buildPlan(body);
      if (out.error) {
        fail(res, out.error.code, out.error.message);
        return;
      }
      send(res, out.payload);
      return;
    }

    const explainMatch = /^\/api\/kox\/([^/]+)\/explain$/.exec(p);
    if (req.method === 'GET' && explainMatch) {
      const out = buildExplain(decodeURIComponent(explainMatch[1]));
      if (out.error) {
        fail(res, out.error.code, out.error.message);
        return;
      }
      send(res, out.payload);
      return;
    }

    if (req.method === 'POST' && p === '/api/gate/batch') {
      const body = await readBody(req);
      if (body === null) {
        fail(res, 'bad_request', '请求体不是合法 JSON');
        return;
      }
      const out = buildGateBatch(body);
      if (out.error) {
        fail(res, out.error.code, out.error.message);
        return;
      }
      send(res, out.payload);
      return;
    }

    fail(res, 'not_found', `未定义的路径 ${p}`);
  } catch (e) {
    // 契约：任何情况下不返回 5xx
    fail(res, 'internal', `服务侧异常：${e instanceof Error ? e.message : String(e)}`);
  }
});

server.listen(PORT, () => {
  const flags = [
    WARMING ? `warming=${WARMING}` : null,
    ERROR_CODE ? `error=${ERROR_CODE}` : null,
    FLIP ? `flip=${FLIP}` : null,
    DELAY ? `delay=${DELAY}ms` : null,
  ].filter(Boolean);
  process.stdout.write(
    `mock-api 监听 http://127.0.0.1:${PORT}${flags.length ? `（${flags.join(' ')}）` : ''}\n`,
  );
});
