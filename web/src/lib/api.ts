/**
 * KOXPilot HTTP API 客户端。
 *
 * 契约见 `koxpilot/api/CONTRACT.md`（v1，冻结）。本文件只做三件事：
 * 1. 按契约的字段名收发数据，不做任何字段改写；
 * 2. 给每个端点配一个明确的超时（health 800ms / plan 20s / 其余 10s）；
 * 3. **把所有失败收敛成结构化结果**（`{ ok: false, error }`），不向组件抛异常。
 *
 * 契约规定业务错误是 HTTP 200 + `ok:false`，但网络层（DNS / CORS / 断网 / 超时）
 * 会直接失败，这里统一翻成同一个 `ApiError` 形状，让调用方只需要判断一次 `ok`。
 */

/* ------------------------------------------------------------------ */
/* base URL                                                           */
/* ------------------------------------------------------------------ */

/**
 * 构建期默认 base URL。
 *
 * 静态站部署出去之后拿不到 `.env`（env 只在构建期注入），所以必须有一个编译进产物的
 * 默认值：`VITE_API_BASE` 存在时优先用它，否则回落到**同源**。
 *
 * 回落到同源的理由：服务与静态站同域部署（反向代理 `/api/*`）时开箱可用；
 * 没有服务时同源请求会立刻拿到静态站的 HTML 而不是 JSON，客户端把它判成不可达，
 * 800ms 内即切到浏览器引擎 —— 比指向一个不存在的域名更快、也不会产生 DNS 长等待。
 */
const BUILTIN_API_BASE = typeof window === 'undefined' ? '' : window.location.origin;

const rawBase = ((import.meta.env.VITE_API_BASE as string | undefined) ?? '').trim();

/** 最终生效的 base URL（去掉尾部斜杠，便于拼 `/api/...`）。 */
export const API_BASE: string = (rawBase || BUILTIN_API_BASE).replace(/\/+$/, '');

/** base URL 来自 env 还是构建期默认值（只用于技术实现页的链路说明）。 */
export const API_BASE_SOURCE: 'env' | 'builtin' = rawBase ? 'env' : 'builtin';

/* ------------------------------------------------------------------ */
/* 契约类型（字段名逐字照抄 CONTRACT.md）                              */
/* ------------------------------------------------------------------ */

export interface LlmRuntime {
  available: boolean;
  provider: string | null;
  reason: string | null;
}

export interface ApiMeta {
  engine: string;
  engine_version: string;
  dataset_sha256: string;
  kox_count: number;
  thresholds_source: string;
  llm_runtime: LlmRuntime;
  served_at: string;
  elapsed_ms: number;
}

export type HealthStatus = 'ready' | 'warming' | string;

export interface HealthPayload {
  ok: true;
  status: HealthStatus;
  meta: ApiMeta;
}

export interface BriefField {
  key: string;
  label: string;
  value: unknown;
  display: string;
  /** 契约给的三态：原文命中 / 规则推导 / 未识别用默认值 */
  status: 'hit' | 'derived' | 'default' | string;
  how: string;
  matched: string;
}

export interface BriefBlock {
  source: string;
  parse_path: 'rule' | 'llm' | 'preset' | string;
  fields: BriefField[];
  /**
   * 解析过程中的说明：歧义、brief 里的硬性要求，以及 **A1 这次走模型还是规则、为什么**。
   * 契约允许为空数组；老版本服务可能整个字段都没有，所以这里是可选的。
   */
  notes?: string[];
}

export interface FunnelRow {
  stage: string;
  label: string;
  count: number;
}

export interface GateHit {
  gate: string;
  gate_label: string;
  signal_label: string;
  detail: string;
}

export interface CandidateRow {
  kox_id: string;
  handle: string;
  platform: string;
  market: string;
  followers: number;
  verdict: 'pass' | 'review' | 'reject' | string;
  gate_hits: GateHit[];
  fit_score: number;
  fit_source: 'cached_llm' | 'rule_fallback' | string;
  reason_human: string;
  allocated_usd: number;
  expected_reach: number;
}

export interface AllocationArm {
  arm: string;
  label: string;
  spend: number;
  expected_value: number;
}

export interface AllocationBlock {
  budget_usd: number;
  allocated_usd: number;
  unallocated_usd: number;
  unallocated_why: string;
  picked: number;
  arms: AllocationArm[];
  saved_usd: number;
  saved_share: number;
}

export interface TimingRow {
  agent: string;
  label: string;
  ms: number;
}

export interface VerdictRow {
  kox_id: string;
  verdict: string;
}

export interface PlanPayload {
  ok: true;
  brief: BriefBlock;
  funnel: FunnelRow[];
  candidates: CandidateRow[];
  allocation: AllocationBlock;
  timings: TimingRow[];
  parity_payload: { verdicts: VerdictRow[] };
  meta: ApiMeta;
}

export interface ExplainSignal {
  label: string;
  value: number;
  threshold: number | null;
  verdict: string;
}

export interface ExplainGate {
  gate: string;
  gate_label: string;
  passed: boolean;
  hits: GateHit[];
}

export interface ExplainPayload {
  ok: true;
  kox_id: string;
  profile: { handle: string; platform: string; market: string; followers: number };
  signals: ExplainSignal[];
  gates: ExplainGate[];
  verdict: string;
  reason_human: string;
  meta: ApiMeta;
}

export interface GateBatchPayload {
  ok: true;
  verdicts: VerdictRow[];
  meta: ApiMeta;
}

export interface PlanRequest {
  brief_text?: string | null;
  brief_id?: string | null;
  options?: { top_n?: number; explain_limit?: number };
}

/* ------------------------------------------------------------------ */
/* 统一结果与错误                                                      */
/* ------------------------------------------------------------------ */

/** 契约 §7 的错误码 + 网络层自造的三个（这三个不会与契约冲突）。 */
export type ApiErrorCode =
  | 'bad_request'
  | 'not_found'
  | 'too_many'
  | 'internal'
  | 'timeout'
  | 'unreachable'
  | 'malformed';

export interface ApiError {
  code: ApiErrorCode | string;
  /** 直接可展示的中文说明 */
  message: string;
  /** 失败发生在业务层（服务返回 ok:false）还是传输层 */
  layer: 'service' | 'transport';
  /** HTTP 状态码，传输层失败时为 null */
  status: number | null;
}

export type ApiResult<T> =
  | { ok: true; data: T; elapsedMs: number }
  | { ok: false; error: ApiError; elapsedMs: number };

const TIMEOUT = { health: 800, plan: 20_000, other: 10_000 } as const;

/** 批量门禁的上限（契约 §6），超过时自动分批。 */
export const GATE_BATCH_LIMIT = 5000;

function transportError(e: unknown, timedOut: boolean, ms: number): ApiError {
  if (timedOut) {
    return { code: 'timeout', message: `服务未在 ${Math.round(ms)} ms 内响应`, layer: 'transport', status: null };
  }
  const detail = e instanceof Error ? e.message : String(e);
  return { code: 'unreachable', message: `服务未连接（${detail}）`, layer: 'transport', status: null };
}

/**
 * 带超时的 JSON 请求。任何异常都不外抛。
 *
 * 契约要求出错时也是 HTTP 200 + `ok:false`，所以这里对 `ok === false` 与
 * 非 2xx 状态都走同一条路径，调用方看到的形状一致。
 */
async function request<T extends { ok: true }>(
  path: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<ApiResult<T>> {
  const t0 = performance.now();
  const ctrl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    ctrl.abort();
  }, timeoutMs);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: ctrl.signal,
      headers: { Accept: 'application/json', ...(init.headers ?? {}) },
    });
    const elapsedMs = performance.now() - t0;
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      return {
        ok: false,
        elapsedMs,
        error: { code: 'malformed', message: '服务响应不是 JSON', layer: 'transport', status: res.status },
      };
    }
    const obj = (body ?? {}) as Record<string, unknown>;
    if (obj.ok === true) return { ok: true, data: obj as unknown as T, elapsedMs };
    if (obj.ok === false) {
      const err = (obj.error ?? {}) as Record<string, unknown>;
      return {
        ok: false,
        elapsedMs,
        error: {
          code: String(err.code ?? 'internal'),
          message: String(err.message ?? '服务未给出说明'),
          layer: 'service',
          status: res.status,
        },
      };
    }
    return {
      ok: false,
      elapsedMs,
      error: {
        code: res.ok ? 'malformed' : 'internal',
        message: res.ok ? '服务响应缺少 ok 字段' : `服务返回 HTTP ${res.status}`,
        layer: res.ok ? 'transport' : 'service',
        status: res.status,
      },
    };
  } catch (e) {
    const elapsedMs = performance.now() - t0;
    return { ok: false, elapsedMs, error: transportError(e, timedOut, timeoutMs) };
  } finally {
    clearTimeout(timer);
  }
}

/* ------------------------------------------------------------------ */
/* 端点                                                               */
/* ------------------------------------------------------------------ */

/** 探活。契约要求 800ms 内返回，超时按不可用处理。 */
export function health(): Promise<ApiResult<HealthPayload>> {
  return request<HealthPayload>('/api/health', { method: 'GET', cache: 'no-store' }, TIMEOUT.health);
}

/** 核心接口：一条 brief → 名单 / 预算 / 逐条证据 / 一致性比对载荷。 */
export function plan(req: PlanRequest): Promise<ApiResult<PlanPayload>> {
  return request<PlanPayload>(
    '/api/plan',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        brief_text: req.brief_text ?? null,
        brief_id: req.brief_id ?? null,
        options: { top_n: req.options?.top_n ?? 200, explain_limit: req.options?.explain_limit ?? 60 },
      }),
    },
    TIMEOUT.plan,
  );
}

/** 单个达人的完整证据链。 */
export function explain(koxId: string): Promise<ApiResult<ExplainPayload>> {
  return request<ExplainPayload>(
    `/api/kox/${encodeURIComponent(koxId)}/explain`,
    { method: 'GET' },
    TIMEOUT.other,
  );
}

/**
 * 批量门禁判定。超过契约上限时自动分批并合并结果；
 * 服务若仍回 `too_many`，把该批再对半拆一次重试（契约 §7 要求自动分批重试）。
 */
export async function gateBatch(koxIds: string[]): Promise<ApiResult<GateBatchPayload>> {
  const t0 = performance.now();
  const chunks: string[][] = [];
  for (let i = 0; i < koxIds.length; i += GATE_BATCH_LIMIT) {
    chunks.push(koxIds.slice(i, i + GATE_BATCH_LIMIT));
  }
  const verdicts: VerdictRow[] = [];
  let meta: ApiMeta | null = null;

  const send = async (ids: string[], depth: number): Promise<ApiError | null> => {
    const res = await request<GateBatchPayload>(
      '/api/gate/batch',
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kox_ids: ids }) },
      TIMEOUT.other,
    );
    if (res.ok) {
      verdicts.push(...res.data.verdicts);
      meta = res.data.meta;
      return null;
    }
    if (res.error.code === 'too_many' && ids.length > 1 && depth < 6) {
      const mid = Math.ceil(ids.length / 2);
      return (await send(ids.slice(0, mid), depth + 1)) ?? (await send(ids.slice(mid), depth + 1));
    }
    return res.error;
  };

  for (const chunk of chunks) {
    const err = await send(chunk, 0);
    if (err) return { ok: false, error: err, elapsedMs: performance.now() - t0 };
  }
  if (!meta) {
    return {
      ok: false,
      elapsedMs: performance.now() - t0,
      error: { code: 'malformed', message: '批量判定没有返回任何结果', layer: 'transport', status: null },
    };
  }
  return { ok: true, data: { ok: true, verdicts, meta }, elapsedMs: performance.now() - t0 };
}
