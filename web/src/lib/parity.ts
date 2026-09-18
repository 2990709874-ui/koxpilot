/**
 * 实时一致性比对：把 Python 服务返回的 `parity_payload.verdicts` 与浏览器 TS 引擎
 * 对**同一批 kox_id** 现算出来的判定逐条对撞。
 *
 * 这一层只做比较，不做任何「对不上就放宽判据」的操作：差异条数如实返回，
 * 带上前几条 kox_id 供界面点名。
 */

import type { VerdictRow } from './api';
import type { GateResult } from '../engine/types';

export interface ParityReport {
  /** 两侧都有的条数 */
  compared: number;
  matched: number;
  diff: number;
  /** 前几条差异的 kox_id 与两侧判定，界面直接点名 */
  diffRows: Array<{ kox_id: string; service: string; browser: string }>;
  /** 服务返回、但浏览器侧召回集合里没有的条数 */
  onlyService: number;
  /** 浏览器侧召回、服务没返回的条数 */
  onlyBrowser: number;
  /** 服务侧本次计算耗时（meta.elapsed_ms） */
  serviceMs: number | null;
  /** 浏览器侧本次重算耗时（performance.now 实测） */
  browserMs: number;
}

const MAX_NAMED_DIFFS = 5;

export function compareVerdicts(
  serviceVerdicts: VerdictRow[],
  browserResults: Map<string, GateResult>,
  timings: { serviceMs: number | null; browserMs: number },
): ParityReport {
  let compared = 0;
  let matched = 0;
  const diffRows: ParityReport['diffRows'] = [];
  let onlyService = 0;
  const seen = new Set<string>();

  for (const row of serviceVerdicts) {
    const id = String(row.kox_id);
    seen.add(id);
    const local = browserResults.get(id);
    if (!local) {
      onlyService += 1;
      continue;
    }
    compared += 1;
    if (local.verdict === row.verdict) matched += 1;
    else if (diffRows.length < MAX_NAMED_DIFFS) {
      diffRows.push({ kox_id: id, service: String(row.verdict), browser: local.verdict });
    }
  }

  let onlyBrowser = 0;
  for (const id of browserResults.keys()) if (!seen.has(id)) onlyBrowser += 1;

  return {
    compared,
    matched,
    diff: compared - matched,
    diffRows,
    onlyService,
    onlyBrowser,
    serviceMs: timings.serviceMs,
    browserMs: timings.browserMs,
  };
}
