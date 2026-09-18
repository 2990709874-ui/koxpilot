import React from 'react';
import { ArrowRight, FileText, ListChecks, Sparkles } from 'lucide-react';
import { Badge, TruthChip } from '../components/ui';
import { useComputeSource } from '../lib/computeSource';
import { fixed, int0 } from '../lib/format';
import type { Artifacts } from '../lib/artifacts';
import type { TabId } from './nav';
import { SECTION } from './nav';

/**
 * 首屏「这是什么」区块：定位、输入→处理→输出、四个核心数字、阅读引导。
 *
 * 数字全部取自已加载的产物（multiseed / metrics / manifest / consistency），
 * 取不到显示「—」。「少浪费」采用 12 种子稳健区间（mean ± std）。
 */

type Loose = Record<string, unknown>;

function num(x: unknown): number | null {
  return typeof x === 'number' && Number.isFinite(x) ? x : null;
}

/** 12 种子稳健区间：少浪费占预算 mean ± std。 */
export function robustSaved(art: Artifacts): { mean: number; std: number } | null {
  const a = (art.multiseed as Loose | null)?.['A_value_robustness'] as Loose | undefined;
  const s = a?.['saved_share_of_budget'] as Loose | undefined;
  const mean = num(s?.['mean']);
  const std = num(s?.['std']);
  if (mean === null || std === null) return null;
  return { mean, std };
}

function Step({
  n,
  title,
  body,
  tone,
}: {
  n: string;
  title: string;
  body: string;
  tone: 'in' | 'mid' | 'out';
}) {
  const cls =
    tone === 'in'
      ? 'border-slate-300 bg-white'
      : tone === 'mid'
        ? 'border-brand-200 bg-brand-50'
        : 'border-emerald-200 bg-emerald-50';
  return (
    <div className={`flex-1 rounded-xl border px-3 py-2.5 ${cls}`}>
      <div className="flex items-center gap-1.5">
        <span className="num chip border-transparent bg-white/80 text-slate-500">{n}</span>
        <span className="text-[13px] font-semibold text-slate-900">{title}</span>
      </div>
      <div className="muted mt-1 leading-snug">{body}</div>
    </div>
  );
}

function BigStat({
  value,
  unit,
  label,
  note,
  tone = 'brand',
}: {
  value: string;
  unit?: string;
  label: string;
  note?: string;
  tone?: 'brand' | 'live' | 'emerald' | 'slate';
}) {
  const color =
    tone === 'live'
      ? 'text-live-700'
      : tone === 'emerald'
        ? 'text-emerald-600'
        : tone === 'slate'
          ? 'text-slate-900'
          : 'text-brand-700';
  return (
    <div className="subcard px-3 py-2.5">
      <div className="flex items-baseline gap-1.5">
        <div className={`num text-[30px] font-semibold leading-none ${color}`}>{value}</div>
        {unit && <div className="num text-[12px] font-medium text-slate-600">{unit}</div>}
      </div>
      <div className="mt-1 text-[12px] font-medium text-slate-800">{label}</div>
      {note && <div className="muted leading-snug">{note}</div>}
    </div>
  );
}

export function Hero({
  art,
  onGo,
}: {
  art: Artifacts;
  onGo: (tab: TabId, anchor?: string) => void;
}): React.ReactElement {
  const cs = useComputeSource();
  const saved = robustSaved(art);
  const m = (art.metrics as Loose | null) ?? null;
  const t1 = (m?.['table_1_fraud_detection'] as Loose | undefined) ?? undefined;
  const strict = (t1?.['strict'] as Loose | undefined) ?? undefined;
  const f1 = num(strict?.['f1']);
  const auc = num(t1?.['auc']);
  const diff = art.consistency ? art.consistency.verdict.diff_count : null;
  const n = art.manifest.dataset.n;

  const guides: Array<{ tab: TabId; anchor?: string; k: string; text: string }> = [
    { tab: 'run', anchor: SECTION.briefInput, k: '先看这个', text: '投放决策：换个 brief，看名单和预算怎么变' },
    { tab: 'proof', k: '再看这个', text: '效果验证：识别准确率与适用边界' },
    { tab: 'build', anchor: SECTION.arch, k: '想抠实现', text: '技术实现：六 Agent 编排与双实现一致性' },
  ];

  return (
    <section className="card overflow-hidden">
      <div className="flex flex-col gap-4 px-5 py-4 xl:flex-row xl:gap-6">
        {/* 左：定位 + 输入→处理→输出 */}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="chip border-brand-200 bg-brand-50 text-brand-700">
              <Sparkles size={11} /> 出海达人营销 · 投前决策智能体
            </span>
            {cs.source === 'backend' ? (
              <Badge className="border-emerald-300 bg-emerald-50 text-emerald-700">Python 服务实时计算</Badge>
            ) : cs.source === 'browser' ? (
              <TruthChip kind="rule" />
            ) : null}
            <TruthChip kind="synthetic" />
          </div>

          <h2 className="mt-2.5 text-[22px] font-semibold leading-tight tracking-tight text-slate-900">
            把<b className="text-brand-700">一句话投放 brief</b>，变成一份可执行、可追责、
            <br className="hidden lg:block" />
            带预算分配和风险证据链的<b className="text-brand-700">达人投放清单</b>。
          </h2>
          <p className="body-text mt-2 max-w-[62ch]">
            并量化这份清单相比「凭粉丝量选人」少浪费多少预算，以及该结论的稳定区间。
          </p>

          {/* 三步：吃什么、做什么、吐什么 */}
          <div className="mt-3 flex flex-col items-stretch gap-2 lg:flex-row lg:items-center">
            <Step n="输入" title="一句话 brief" body="“$80k 投中东 3C，要 TikTok 微中腰”——自然语言，不填表单" tone="in" />
            <ArrowRight size={16} className="mx-auto shrink-0 rotate-90 text-slate-400 lg:rotate-0" />
            <Step n="处理" title="四层门禁筛人 + 预算分配" body="G0→G3 逐层拒绝 + 带硬约束的预算贪心分配" tone="mid" />
            <ArrowRight size={16} className="mx-auto shrink-0 rotate-90 text-slate-400 lg:rotate-0" />
            <Step n="输出" title="可追责的投放清单" body="每人一条：投多少钱、为什么能投 / 为什么被拒，证据可点开" tone="out" />
          </div>

        </div>

        {/* 右：四个核心数字 */}
        <div className="shrink-0 xl:w-[430px]">
          <div className="grid grid-cols-2 gap-2.5">
            <BigStat
              value={saved ? `−${(saved.mean * 100).toFixed(1)}%` : '—'}
              unit={saved ? `± ${(saved.std * 100).toFixed(1)}%` : undefined}
              label="少浪费占预算（12 种子稳健区间）"
              note="12 个种子聚合口径，95% CI 12.8%~30.2%"
              tone="brand"
            />
            <BigStat
              value={f1 === null ? '—' : fixed(f1, 2)}
              unit={auc === null ? undefined : `AUC ${fixed(auc, 2)}`}
              label="水号识别 F1（严口径）"
              note="以标注结果为裁判，严口径判定"
              tone="emerald"
            />
            <BigStat
              value={int0(n)}
              label="达人逐条参与每次重算"
              note={cs.source === 'backend' ? '服务与浏览器读同一份数据集' : '改参数即重跑，耗时实时计量'}
              tone="live"
            />
            <BigStat
              value={diff === null ? '—' : int0(diff)}
              unit="条"
              label="Python / TS 双实现一致性差异"
              note="判定级、证据链、预算三层逐条比对"
              tone="slate"
            />
          </div>

        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 border-t border-slate-200 px-5 py-2.5">
        <ListChecks size={13} className="text-brand-600" />
        <span className="text-[12px] font-semibold text-slate-900">30 秒看懂，按这个顺序点：</span>
        {guides.map((g) => (
          <button
            key={g.k + g.tab}
            onClick={() => onGo(g.tab, g.anchor)}
            className="focusable group flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2 py-1 transition-colors hover:border-brand-300 hover:bg-brand-50"
          >
            <span className="text-[12px] text-slate-700 group-hover:text-slate-900">{g.text}</span>
            <ArrowRight size={12} className="shrink-0 text-slate-400 group-hover:text-brand-600" />
          </button>
        ))}
      </div>
    </section>
  );
}

/** 非默认页签下的收窄版：一行内保留定位与核心口径，不占用页签首屏。 */
export function HeroSlimBar({ art, onBack }: { art: Artifacts; onBack: () => void }): React.ReactElement {
  const saved = robustSaved(art);
  return (
    <div className="card flex flex-wrap items-center gap-x-4 gap-y-1.5 px-4 py-2">
      <FileText size={13} className="text-brand-600" />
      <span className="text-[12.5px] text-slate-700">
        一句话 brief → 四层门禁筛人 + 预算分配 → 可追责的投放清单
      </span>
      {saved && (
        <span className="chip border-brand-200 bg-brand-50 text-brand-700">
          少浪费占预算 {(saved.mean * 100).toFixed(1)}% ± {(saved.std * 100).toFixed(1)}%（12 种子）
        </span>
      )}
      <button onClick={onBack} className="focusable muted ml-auto underline decoration-dotted hover:text-brand-700">
        回到「这是什么」
      </button>
    </div>
  );
}
