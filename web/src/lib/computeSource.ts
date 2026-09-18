/**
 * 计算源三态（契约 §8）：`probing` → `backend` / `browser`。
 *
 * 设计要点：
 * - 启动时探活一次 `/api/health`（800ms 超时）。`status = "warming"` 按「尚不可用」处理，
 *   但**停留在 probing 骨架态**、3 秒后重试一次 —— 不先显示一个结论再跳变。
 * - 传输层直接失败（断网 / CORS / 超时）时立即定为 `browser`，不再重试，避免界面长时间空转。
 * - `plan` 返回 `ok:false` 时由调用方 `reportServiceFailure()` 切到 `browser`。
 *
 * 状态放在模块级 store 而不是 React context：页头、投放决策页、技术实现页都要读它，
 * 而这三处分属不同子树，store + 订阅是最小改动的共享方式。
 */

import React from 'react';
import { health, type ApiMeta } from './api';

export type ComputeSource = 'probing' | 'backend' | 'browser';

export interface ComputeSourceState {
  source: ComputeSource;
  /** 服务侧 meta（数据集校验值、达人条数、LLM 路径），仅 backend 态有值 */
  meta: ApiMeta | null;
  /** 状态成因的中性说明，直接可展示 */
  note: string | null;
  /** 最近一次探活往返耗时 */
  probeMs: number | null;
  /** 探活次数（warming 会重试一次） */
  attempts: number;
  /** 是否处于「服务正在加载数据集」的等待窗口 */
  warming: boolean;
}

const initial: ComputeSourceState = {
  source: 'probing',
  meta: null,
  note: null,
  probeMs: null,
  attempts: 0,
  warming: false,
};

let state: ComputeSourceState = initial;
const subs = new Set<(s: ComputeSourceState) => void>();

function set(patch: Partial<ComputeSourceState>): void {
  state = { ...state, ...patch };
  for (const f of subs) f(state);
}

/** 顶部展示用的中性标签。不写「降级」「兜底」这类词。 */
export const SOURCE_LABEL: Record<ComputeSource, string> = {
  probing: '计算源：连接中',
  backend: '计算源：Python 服务',
  browser: '计算源：浏览器引擎（服务未连接）',
};

/** warming 重试的等待时间：后端 6.7MB 数据集懒加载，冷启动确实需要几秒。 */
const WARMING_RETRY_MS = 3000;

let started = false;

async function probeOnce(): Promise<'ready' | 'warming' | 'down'> {
  const res = await health();
  set({ attempts: state.attempts + 1, probeMs: res.elapsedMs });
  if (!res.ok) {
    set({ note: res.error.message, meta: null });
    return 'down';
  }
  set({ meta: res.data.meta });
  if (res.data.status === 'ready') {
    set({ note: null });
    return 'ready';
  }
  set({ note: '服务正在加载数据集' });
  return 'warming';
}

/** 启动探活（幂等，App 挂载时调一次）。 */
export function startProbe(): void {
  if (started) return;
  started = true;
  void (async () => {
    const first = await probeOnce();
    if (first === 'ready') {
      set({ source: 'backend', warming: false });
      return;
    }
    if (first === 'down') {
      set({ source: 'browser', warming: false });
      return;
    }
    // warming：留在骨架态，3 秒后重试一次，成功即用服务，仍未就绪则由浏览器引擎完成
    set({ warming: true });
    await new Promise<void>((r) => setTimeout(r, WARMING_RETRY_MS));
    const second = await probeOnce();
    set({ source: second === 'ready' ? 'backend' : 'browser', warming: false });
  })();
}

/** 调用过程中服务给出错误 / 不可达：本次及后续计算改由浏览器引擎完成。 */
export function reportServiceFailure(note: string): void {
  set({ source: 'browser', note, warming: false });
}

export function useComputeSource(): ComputeSourceState {
  const [s, setS] = React.useState<ComputeSourceState>(state);
  React.useEffect(() => {
    subs.add(setS);
    setS(state);
    return () => {
      subs.delete(setS);
    };
  }, []);
  return s;
}

/** 供测试与脚本读取当前态（不订阅）。 */
export function currentComputeSource(): ComputeSourceState {
  return state;
}
