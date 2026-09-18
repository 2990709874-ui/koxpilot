/**
 * KOXPilot HTTP API 客户端。
 *
 * 契约见 `koxpilot/api/CONTRACT.md`（v1.1）。本文件只做三件事：
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

/** 构建期确定的 base URL（去掉尾部斜杠，便于拼 `/api/...`）。 */
export const API_BASE: string = (rawBase || BUILTIN_API_BASE).replace(/\/+$/, '');

/**
 * 运行期覆盖的 base URL。
 *
 * 为什么需要它：静态站（GitHub Pages 之类）一旦构建完成，`VITE_API_BASE` 就被编译进
 * 产物里改不动了。但"前端发到哪、服务部署在哪"这件事是**部署期**才知道的——换一个
 * 服务地址不该需要重新构建一次前端。所以产物根目录留一个 `api-config.json`，
 * 首屏探活前读一次；改这个文件即可换服务地址。
 *
 * 优先级：`api-config.json` 里的非空 `apiBase` > 构建期 `VITE_API_BASE` > 同源。
 * 放在最前是因为它是**部署者显式写下的意图**，而 env 只是构建那台机器上的默认值。
 * 空字符串一律当作"没配"，所以仓库里可以提交一份 `apiBase: ""` 的模板而不影响本地开发。
 */
let runtimeBase: string | null = null;
let runtimeConfigLoaded = false;

/** 本次生效的 base URL。所有请求都走这里，不要直接用 `API_BASE`。 */
export function apiBase(): string {
  return runtimeBase ?? API_BASE;
}

/** base URL 的来源（只用于界面上说明这次连的是哪儿）。 */
export function apiBaseSource(): 'runtime-config' | 'env' | 'builtin' {
  if (runtimeBase !== null) return 'runtime-config';
  return rawBase ? 'env' : 'builtin';
}

/**
 * 读一次 `api-config.json`。
 *
 * 用相对路径 `./api-config.json`：站点可能被部署在任意子目录下（GitHub Pages 的
 * `/<repo>/`），写成 `/api-config.json` 在子目录部署时会 404。
 *
 * 拿不到就静默用构建期默认值——这个文件缺失是**正常情况**（本地开发、同域反代部署都
 * 不需要它），不该因此在界面上报错。超时给 600ms：它只是首屏探活前的一次小请求，
 * 不能让它拖慢"这东西到底能不能用"的判断。
 */
export async function loadRuntimeApiConfig(): Promise<void> {
  if (runtimeConfigLoaded) return;
  runtimeConfigLoaded = true;
  if (typeof window === 'undefined') return;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 600);
  try {
    const res = await fetch('./api-config.json', { signal: ctrl.signal, cache: 'no-store' });
    if (!res.ok) return;
    const body = (await res.json()) as { apiBase?: unknown };
    const next = typeof body.apiBase === 'string' ? body.apiBase.trim() : '';
    if (next) runtimeBase = next.replace(/\/+$/, '');
  } catch {
    /* 缺失 / 超时 / 不是 JSON：按"没配"处理 */
  } finally {
    clearTimeout(timer);
  }
}

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

/**
 * 一条对照臂（契约 §4.1）。
 *
 * **判优字段一律以标注结果为裁判**：`effective_views_gt*` 与 `waste_usd` 全部来自
 * A6 审计按数据集标注的结算，不使用引擎自己的分数。`engine_*` 是引擎的事前估分
 * （选人排序依据），作为次级信息给出，不参与本对比的判优。
 */
export interface AllocationArm {
  arm: string;
  label: string;
  spend: number;
  /**
   * 这条臂实际拿到钱的达人数 = 要签、要沟通、要交付的合约数。是成本，不是战绩：
   * 摊得越薄每美元越便宜，但判优指标里不含这笔采购与交付成本。老版本服务没有此字段。
   */
  n_selected?: number;
  /** 裁判来源：`ground_truth` = 按数据集标注结算。老版本服务没有此字段。 */
  judge?: string;
  /** 主口径有效曝光（标注为水号的曝光按 0 计）。 */
  effective_views_gt?: number;
  /**
   * 每美元买到的有效曝光（主口径）——**本对比的判优字段**。三条臂的 `spend` 能差几十倍，
   * 所以并排比较只能用这个字段；`spend` 为 0 时契约给 `null`（"没花钱"和"花了钱没效果"
   * 不是一回事，不能写 0）。
   */
  effective_views_gt_per_dollar?: number | null;
  /** 宽松口径有效曝光（水号曝光按 50% 计），用来看结论是否依赖这个假设。 */
  effective_views_gt_lenient?: number;
  /** 每美元买到的有效曝光（宽松口径）。 */
  effective_views_gt_lenient_per_dollar?: number | null;
  /** 这条臂花在真水号 / 真高风险号上的钱，与「少浪费」同一口径。 */
  waste_usd?: number;
  /** 引擎事前估分合计（`value(k)` 按等效条数加总）。次级信息，不判优。 */
  engine_expected_value?: number;
  /** 每美元的引擎事前估分。次级信息，不判优。 */
  engine_value_per_dollar?: number | null;
  /** 兼容字段：与 `effective_views_gt` 同数同义。新代码请用 `effective_views_gt`。 */
  expected_value: number;
  /** 兼容字段：与 `effective_views_gt_per_dollar` 同数同义。 */
  value_per_dollar?: number | null;
}

/**
 * 预算花不出去时的放宽建议（契约 §4.2）。每条都是「只改这一处」后真跑一遍的结果，
 * 不是估算上限；`extra_spendable_usd` 允许为 0 或负数——「这条放宽帮不上忙」也是结论。
 */
export interface AdviceRow {
  key: 'relax_age' | 'expand_markets' | 'discount_review' | string;
  title: string;
  action: string;
  spendable_usd: number;
  extra_spendable_usd: number;
  utilization_after: number;
  extra_picked: number;
  quality_note: string;
}

/**
 * 这组对比的口径声明（契约 §4.1）：判优用哪个字段、裁判是谁、两个假设怎么写、
 * 决策链路与裁判席怎么分开。服务端与浏览器降级态给的是同一套措辞。
 */
export interface JudgeBlock {
  metric: string;
  judge: string;
  main_assumption: string;
  lenient_assumption: string;
  engine_score_role: string;
  pipeline_separation: string;
}

export interface AllocationBlock {
  budget_usd: number;
  allocated_usd: number;
  unallocated_usd: number;
  unallocated_why: string;
  picked: number;
  arms: AllocationArm[];
  /** 老版本服务没有这一块，展示层按 `undefined` 兜。 */
  judge?: JudgeBlock;
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

/**
 * 本次计算的口径块（契约 §4.2）：这一次到底把多少人送进了门禁与预算。
 *
 * `computed_on` 必须等于 `recall_total`——服务端一旦为了省时间截断计算池，
 * 同一条 brief 的线上方案就会和离线 CLI 差一个数量级（v1.0 真出过：7.1% vs 99.8%）。
 * 老服务没有这一块，展示层按 `undefined` 兜。
 */
export interface ScopeBlock {
  recall_total: number;
  computed_on: number;
  detail_rows: number;
  truncated_for_compute: boolean;
  note: string;
}

export interface PlanPayload {
  ok: true;
  brief: BriefBlock;
  funnel: FunnelRow[];
  /** 契约 v1.1 新增；老服务没有时为 `undefined`。 */
  scope?: ScopeBlock;
  candidates: CandidateRow[];
  allocation: AllocationBlock;
  /** 预算利用率低于 60% 时的放宽建议；花得出去时是空数组。老服务可能没有这个字段。 */
  advice?: AdviceRow[];
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
    const res = await fetch(`${apiBase()}${path}`, {
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
        // 契约 v1.1：这两个上限都只裁「返回多少条明细」，不裁计算池，
        // 所以这里不再替调用方塞一个 top_n 默认值去"控制召回规模"——那正是漂移的来源。
        options: {
          ...(req.options?.top_n === undefined ? {} : { top_n: req.options.top_n }),
          explain_limit: req.options?.explain_limit ?? 60,
        },
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
