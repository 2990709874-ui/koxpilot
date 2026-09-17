/**
 * 数据装载层：把 `public/data/*.json`（由 `scripts/prepare-data.mjs` 从 Python 产物搬运）
 * 读进内存，并对**缺失产物如实降级**。
 *
 * 纪律：
 * 1. 页面上任何数字要么来自这些 JSON，要么由 TS 引擎在浏览器内现算，没有第三种来源；
 * 2. 可选产物（prompt_bench / llm_compare / llm_bench）缺失时返回 null，
 *    UI 必须显示"未生成"而不是给一个看起来合理的占位数字；
 * 3. manifest.json 记录了每个产物的来源文件、字节数与 sha256，页面直接展示，可核对。
 */

import { Thresholds, type ThresholdsPayload } from '../engine/thresholds.ts';
import { specFromDict, type CampaignSpec, type Kox } from '../engine/types.ts';

const asset = (rel: string): string => `${import.meta.env.BASE_URL}${rel}`;

export interface ManifestArtifact {
  key: string;
  file: string | null;
  source: string;
  present: boolean;
  source_bytes?: number;
  shipped_bytes?: number;
  source_sha256?: string | null;
  dataset_sha256?: string | null;
  n?: number;
  n_groups?: number;
  note?: string;
  reason?: string;
  top_level_keys?: string[];
}

/** prepare-data 的关键字段在位自检结果（缺字段时页面顶部亮条，面板降级）。 */
export interface FieldAuditRow {
  artifact: string;
  path: string;
  what: string;
  present: boolean;
}

export interface Manifest {
  schema: string;
  generated_at_unix: number;
  source_root: string;
  dataset: {
    sha256: string;
    version: string;
    seed: number;
    n: number;
    gt_verdict_counts: Record<string, number>;
    injection_actual_rates: Record<string, number>;
  };
  thresholds_meta: Record<string, unknown>;
  artifacts: ManifestArtifact[];
  field_audit?: FieldAuditRow[];
  warnings?: string[];
  notes?: string[];
  note?: string;
}

export interface BriefEntry {
  brief_id: string;
  name: string;
  raw_text: string;
  spec: CampaignSpec;
}

export interface ConsistencyReport {
  schema: string;
  status: 'pass' | 'fail';
  generated_at_unix: number;
  elapsed_ms: number;
  engine: { implementation: string; entry: string; runtime: string };
  reference: { implementation: string; file: string; scope: string; dataset_sha256: string | null; n: number | null; counts: Record<string, number> | null };
  thresholds: { file: string; version: string | null; n_groups: number; n_fallback_cells: number | null; note: string };
  campaign_scope: string;
  dataset: { sha256_from_reference: string | null; version: string | null; seed: number | null; n: number };
  verdict: {
    total: number;
    matched: number;
    diff_count: number;
    missing_in_reference: number;
    match_rate: number;
    fields_compared: string[];
    field_mismatch: Record<string, number>;
    first_diffs: unknown[];
    counts_ts: Record<string, number>;
    counts_python: Record<string, number>;
  };
  evidence: {
    status: string;
    source?: string;
    compared_records?: number;
    compared_reason_rows?: number;
    matched_records?: number;
    diff_records?: number;
    fields_compared?: string[];
    field_mismatch?: Record<string, number>;
    first_diffs?: unknown[];
    note?: string;
    reason?: string;
  };
  budget: {
    status: string;
    source?: string[];
    compared_arms?: number;
    matched_arms?: number;
    compared_audits?: number;
    matched_audits?: number;
    arms?: Array<{ campaign_id: string; arm: string; matched: boolean; per_person_compared?: boolean; n_selected: number; n_posts: number; spent_usd: number; utilization: number; est_cpm_usd: number }>;
    audits?: Array<{ campaign_id: string; matched: boolean; saved_usd: number; effective_view_uplift: number }>;
    fields_compared?: string[];
    first_diffs?: unknown[];
    note?: string;
    reason?: string;
  };
  honesty_notes: string[];
}

/** metrics.json / budget.json / audit.json / llm_bench.json 结构较深，按需取值，用宽松类型。 */
export type Loose = Record<string, any>;

export interface Artifacts {
  manifest: Manifest;
  records: Partial<Kox>[];
  datasetMeta: Loose;
  thresholds: Thresholds;
  thresholdsPayload: ThresholdsPayload;
  briefs: BriefEntry[];
  briefsMeta: Loose;
  metrics: Loose | null;
  budget: Loose | null;
  audit: Loose | null;
  /** 12 种子稳健性（含三臂两段归因）。缺失时 #cost 只展示单种子口径并明说。 */
  multiseed: Loose | null;
  consistency: ConsistencyReport | null;
  llmBench: Loose | null;
  promptBench: Loose | null;
  llmCompare: Loose | null;
  /** 加载耗时（真实测量，Console 的 A0 阶段展示它）。 */
  loadMs: number;
  bytes: { kox: number | null; thresholds: number | null };
}

async function fetchJson<T>(rel: string, required: boolean): Promise<T | null> {
  const res = await fetch(asset(rel), { cache: 'no-cache' });
  if (!res.ok) {
    if (required) throw new Error(`加载 ${rel} 失败：HTTP ${res.status}`);
    return null;
  }
  return (await res.json()) as T;
}

export async function loadArtifacts(): Promise<Artifacts> {
  const t0 = performance.now();
  const manifest = (await fetchJson<Manifest>('data/manifest.json', true)) as Manifest;
  const fileOf = (key: string): string | null => manifest.artifacts.find((a) => a.key === key)?.file ?? null;

  const [koxPayload, thrPayload, briefsPayload] = await Promise.all([
    fetchJson<Loose>(fileOf('kox') ?? 'data/kox.json', true),
    fetchJson<ThresholdsPayload>(fileOf('thresholds') ?? 'data/thresholds.json', true),
    fetchJson<Loose>(fileOf('briefs') ?? 'data/briefs.json', true),
  ]);

  const optional = async (key: string, fallback: string): Promise<Loose | null> => {
    const f = fileOf(key);
    if (f === null && !manifest.artifacts.some((a) => a.key === key && a.present)) {
      // manifest 明确说这个产物没生成 -> 直接按缺失处理，不去 404 探一次
      if (manifest.artifacts.some((a) => a.key === key)) return null;
    }
    return fetchJson<Loose>(f ?? fallback, false);
  };

  const [metrics, budget, audit, multiseed, consistency, llmBench, promptBench, llmCompare] = await Promise.all([
    optional('metrics', 'data/metrics.json'),
    optional('budget', 'data/budget.json'),
    optional('audit', 'data/audit.json'),
    optional('multiseed', 'data/multiseed.json'),
    fetchJson<ConsistencyReport>('data/consistency.json', false),
    optional('llm_bench', 'data/llm_bench.json'),
    optional('prompt_bench', 'data/prompt_bench.json'),
    optional('llm_compare', 'data/llm_compare.json'),
  ]);

  const records = ((koxPayload as Loose).kox ?? koxPayload) as Partial<Kox>[];
  const briefsRaw = ((briefsPayload as Loose).briefs ?? briefsPayload) as Loose[];
  const briefs: BriefEntry[] = briefsRaw.map((b) => ({
    brief_id: String(b.brief_id ?? b.spec?.campaign_id ?? ''),
    name: String(b.name ?? b.spec?.name ?? ''),
    raw_text: String(b.raw_text ?? b.spec?.raw_text ?? ''),
    spec: specFromDict(b.spec ?? b),
  }));

  const koxArtifact = manifest.artifacts.find((a) => a.key === 'kox');
  const thrArtifact = manifest.artifacts.find((a) => a.key === 'thresholds');

  return {
    manifest,
    records,
    datasetMeta: ((koxPayload as Loose).meta ?? {}) as Loose,
    thresholds: Thresholds.fromDict(thrPayload as ThresholdsPayload),
    thresholdsPayload: thrPayload as ThresholdsPayload,
    briefs,
    briefsMeta: ((briefsPayload as Loose).meta ?? {}) as Loose,
    metrics,
    budget,
    audit,
    multiseed,
    consistency,
    llmBench,
    promptBench,
    llmCompare,
    loadMs: performance.now() - t0,
    bytes: { kox: koxArtifact?.shipped_bytes ?? null, thresholds: thrArtifact?.shipped_bytes ?? null },
  };
}
