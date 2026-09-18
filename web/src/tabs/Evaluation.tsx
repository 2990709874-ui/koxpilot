import React from 'react';
import { Loader2, ShieldCheck } from 'lucide-react';
import { ConfusionMatrix, Heatmap } from '../components/charts';
import { Boundaries, Collapse, Verdict } from '../components/Collapse';
import { Badge, MissingArtifact, Panel, Segmented, Stat, TruthChip } from '../components/ui';
import type { Loose } from '../lib/artifacts';
import { fixed, int0, pct1 } from '../lib/format';
import type { AblationRow, EvalReport, SensitivityPoint } from '../lib/pipeline';
import { ruleLabel } from '../lib/pipeline';

/** 分层维度的中文名：产品页只显示业务口径，不显示产物字段名。 */
const DIM_ZH: Record<string, string> = {
  follower_bucket: '粉丝层级',
  platform: '平台',
  country: '国家',
  'platform×bucket': '平台 × 粉丝层级',
  platform_bucket: '平台 × 粉丝层级',
};

/** 粉丝层级的业务中文名。 */
const BUCKET_ZH: Record<string, string> = {
  nano: '万粉以下',
  micro: '1 万–10 万粉',
  mid: '10 万–50 万粉',
  macro: '50 万–200 万粉',
  mega: '200 万粉以上（头部）',
};

/** 规则签名（可能是多条规则拼接）翻成中文规则名。 */
function signatureZh(sig: string): string {
  return sig
    .split(/[+,|]/)
    .map((x) => ruleLabel(x.trim()))
    .join(' + ');
}

/** 信号的业务中文名：页面不显示产物字段名。 */
const SIGNAL_ZH: Record<string, string> = {
  engagement_rate: '互动率',
  follower_growth_spike: '涨粉突变',
  view_follower_ratio: '播放/粉丝比',
  comment_like_ratio: '评论/点赞比',
  authenticity_score: '真实性分',
};

/** TS 现算 vs Python 产物的一行对照。 */
function ParityRow({ label, ts, py, digits = 4 }: { label: string; ts: number; py: number; digits?: number }): React.ReactElement {
  const same = Math.abs(ts - py) < 5 * 10 ** -(digits + 1);
  return (
    <tr className="hairline">
      <td className="td">{label}</td>
      <td className="td num text-right text-live-700">{fixed(ts, digits)}</td>
      <td className="td num text-right text-slate-600">{fixed(py, digits)}</td>
      <td className={`td num text-right ${same ? 'text-emerald-600' : 'text-rose-600'}`}>{same ? '0' : fixed(ts - py, digits)}</td>
    </tr>
  );
}

/**
 * multiseed.json 的兜底加载：可选 prop 优先，缺省时本页自己读同一份产物。
 * 多种子区间是本页主指标口径，数字一律来自产物。
 */
function useMultiseed(passed?: Loose | null): Loose | null {
  const [own, setOwn] = React.useState<Loose | null>(null);
  React.useEffect(() => {
    if (passed) return;
    let alive = true;
    fetch(`${import.meta.env.BASE_URL}data/multiseed.json`, { cache: 'no-cache' })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (alive) setOwn(j as Loose | null);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [passed]);
  return passed ?? own;
}

/** 多种子区间一行：均值 ± 标准差与 95% 区间。 */
function SeedRow({ label, stat }: { label: string; stat: Loose | undefined }): React.ReactElement | null {
  if (!stat) return null;
  return (
    <tr className="hairline">
      <td className="td">{label}</td>
      <td className="td num text-right">
        {fixed(Number(stat.mean), 3)} ± {fixed(Number(stat.std), 3)}
      </td>
      <td className="td num text-right text-slate-600">
        [{fixed(Number(stat.ci95_low), 3)}, {fixed(Number(stat.ci95_high), 3)}]
      </td>
      <td className="td num text-right text-slate-600">
        {fixed(Number(stat.min), 3)} – {fixed(Number(stat.max), 3)}
      </td>
    </tr>
  );
}

export function EvaluationTab({
  metrics,
  report,
  busy,
  onRun,
  multiseed,
}: {
  metrics: Loose | null;
  report: EvalReport | null;
  busy: string | null;
  onRun: () => void;
  ablation: AblationRow[];
  onAblation: () => void;
  sens: SensitivityPoint[];
  onSens: () => void;
  /** 可选：由 App 注入 multiseed.json；不传时本页自己读同一份产物 */
  multiseed?: Loose | null;
}): React.ReactElement {
  const [cell, setCell] = React.useState<[string, string] | null>(null);
  const [stratDim, setStratDim] = React.useState<'follower_bucket' | 'platform' | 'country' | 'platform_bucket'>('follower_bucket');
  const ms = useMultiseed(multiseed);

  React.useEffect(() => {
    if (!report && busy === null) onRun();
    // 只在首次进入本页时触发；后续切页回来复用已算结果
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const t1 = metrics?.table_1_fraud_detection as Loose | undefined;
  const t2 = metrics?.table_2_verdict_confusion as Loose | undefined;
  const t3 = metrics?.table_3_strata as Loose | undefined;
  const t5 = metrics?.table_5_sensitivity as Loose | undefined;
  const weak = (metrics?.weak_spots ?? []) as Loose[];

  const f1 = report ? report.fraud.strict.f1 : Number(t1?.strict?.f1 ?? 0);
  const auc = report ? report.fraud.auc : Number(t1?.auc ?? 0);
  const acc = report ? report.verdict.accuracy : Number(t2?.accuracy ?? 0);

  /** F1 最低的一格：由产物现算。 */
  const worst = weak.reduce<Loose | null>((a, w) => {
    const v = Number(w.f1);
    if (!Number.isFinite(v)) return a;
    return a === null || v < Number(a.f1) ? w : a;
  }, null);

  /** 口径冲突导致的人工复核假阳性人数（多源标签冲突，产品口径下确实应人核）。 */
  const conflictN = Number((t2?.review_false_positive_attribution as Loose | undefined)?.solely_caused_by_G2_2 ?? 0);

  /** 点击错判格后的规则归因（只在两类假阳性格上有产物）。 */
  const attribution = React.useMemo(() => {
    if (!cell || !t2) return null;
    const [gt, pred] = cell;
    if (gt === 'pass' && pred === 'review') return t2.review_false_positive_attribution as Loose;
    if (gt === 'pass' && pred === 'reject') return t2.reject_false_positive_attribution as Loose;
    return null;
  }, [cell, t2]);

  const stratRows = React.useMemo(() => {
    const key =
      stratDim === 'follower_bucket'
        ? 'by_follower_bucket'
        : stratDim === 'platform'
          ? 'by_platform'
          : stratDim === 'country'
            ? 'by_country'
            : 'by_platform_bucket';
    const src = (t3?.[key] ?? {}) as Record<string, Loose>;
    return Object.entries(src)
      .map(([k, v]) => ({
        cell: k,
        n: Number(v.n),
        positives: Number(v.positives),
        f1: Number(v.f1),
        precision: Number(v.precision),
        recall: Number(v.recall),
        lowSupport: Boolean(v.low_support),
      }))
      .sort((a, b) => a.f1 - b.f1);
  }, [t3, stratDim]);

  const C = (ms?.C_gate_metric_robustness ?? null) as Loose | null;
  const cStats = (C?.metrics ?? {}) as Record<string, Loose>;
  const nSeeds = Number((ms?.meta as Loose | undefined)?.n_seeds ?? 12);
  const saveShare = ((ms?.A_value_robustness as Loose | undefined)?.saved_share_of_budget ?? null) as Loose | null;

  return (
    <div className="space-y-4">
      {/* ============ 招牌指标 ============ */}
      <Verdict
        what="效果验证 · 与 ground truth 的一致性"
        conclusion={
          <>
            水号识别 F1 <b className="text-slate-900">{fixed(f1, 2)}</b>，排序能力 AUC{' '}
            <b className="text-slate-900">{fixed(auc, 2)}</b>，三分类判定准确率 <b className="text-slate-900">{fixed(acc, 2)}</b>。
            对外报数以 {int0(nSeeds)} 个随机种子的分布为准；下方指标左列由当前浏览器现算，右列取自 Python 离线产物。
          </>
        }
        tone="brand"
        stats={[
          { label: '水号识别 F1', value: fixed(f1, 2), tone: 'warn' },
          { label: 'AUC（排序能力）', value: fixed(auc, 2), tone: 'good' },
          { label: '三分类准确率', value: fixed(acc, 2), tone: 'warn' },
          { label: '评测样本', value: int0(report?.n ?? Number(t2?.n ?? 0)) },
        ]}
        right={
          <div className="flex flex-col items-end gap-1.5">
            <div className="flex items-center gap-1.5">
              <TruthChip kind="rule" />
              <TruthChip kind="python" />
            </div>
            {busy ? (
              <span className="flex items-center gap-1.5 text-[11px] text-live-700">
                <Loader2 size={12} className="animate-spin" />
                {busy}
              </span>
            ) : (
              <button
                onClick={onRun}
                className="focusable rounded-lg border border-slate-300 px-2.5 py-1 text-[11px] text-slate-700 hover:border-live-300 hover:text-live-700"
              >
                在浏览器里重算一遍
              </button>
            )}
          </div>
        }
      />

      {/* ============ 混淆矩阵 + F1 热力图 ============ */}
      <div className="grid gap-3 lg:grid-cols-[0.95fr_1.05fr]">
        <Panel title="判定分布（三分类混淆矩阵）" subtitle="行 = ground truth，列 = 系统判定">
          {report ? (
            <>
              <ConfusionMatrix matrix={report.verdict.matrix} onCell={(g, pr) => setCell([g, pr])} highlight={cell} />
              <div className="mt-2 grid grid-cols-3 gap-2">
                {(['pass', 'review', 'reject'] as const).map((v) => (
                  <Stat
                    key={v}
                    label={v === 'pass' ? '可投 · F1' : v === 'review' ? '人工复核 · F1' : '拒绝 · F1'}
                    value={fixed(report.verdict.per_class[v].f1, 3)}
                    hint={`精确率 ${fixed(report.verdict.per_class[v].precision, 3)} / 召回率 ${fixed(report.verdict.per_class[v].recall, 3)}`}
                  />
                ))}
              </div>
              {attribution ? (
                <div className="mt-2 rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2">
                  <div className="flex items-baseline justify-between">
                    <span className="text-[12px] font-medium text-slate-800">这批达人主要由哪些规则触发</span>
                    <span className="num text-[12px] text-slate-700">{int0(Number(attribution.n))} 人</span>
                  </div>
                  <div className="mt-1.5 space-y-1">
                    {((attribution.top_rule_signatures ?? []) as Loose[]).slice(0, 4).map((sig) => (
                      <div key={String(sig.rules)} className="flex items-center gap-2">
                        <span className="w-40 shrink-0 truncate text-[11.5px] text-slate-600" title={signatureZh(String(sig.rules))}>
                          {signatureZh(String(sig.rules))}
                        </span>
                        <div className="h-2 flex-1 overflow-hidden rounded-sm bg-slate-200">
                          <div className="h-full rounded-sm bg-amber-500" style={{ width: `${Number(sig.share) * 100}%` }} />
                        </div>
                        <span className="num w-20 shrink-0 text-right text-[11px] text-slate-600">
                          {int0(Number(sig.n))} · {pct1(Number(sig.share))}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                conflictN > 0 && (
                  <p className="body-text mt-2">
                    「本该放行、被推入人工复核」是准确率的主要失分来源，其中 <b className="text-amber-700">{int0(conflictN)}</b> 人由多源标签冲突触发；按投放口径这些达人确实应进人工复核队列。
                  </p>
                )
              )}
            </>
          ) : (
            <div className="flex items-center gap-2 py-6 text-[12px] text-slate-600">
              <Loader2 size={14} className="animate-spin text-live-600" />
              正在对全库重跑四层门禁…
            </div>
          )}
        </Panel>

        <Panel
          title="哪类达人判得准（识别 F1 分层视图）"
          subtitle="一格 = 一类达人，颜色越浅 = 识别越弱；虚线框为低支撑格（真水号 < 10）"
          right={
            <Segmented
              size="sm"
              value={stratDim}
              onChange={setStratDim}
              options={[
                { value: 'follower_bucket', label: '粉丝层级' },
                { value: 'platform', label: '平台' },
                { value: 'country', label: '国家' },
                { value: 'platform_bucket', label: '平台×层级' },
              ]}
            />
          }
        >
          <Heatmap rows={stratRows} />
        </Panel>
      </div>

      {/* ============ 多种子稳健区间（主指标口径） ============ */}
      <Panel
        title={`多种子稳健区间（${int0(nSeeds)} 个随机种子，主指标口径）`}
        subtitle="每个种子重新生成数据集、重新标定阈值、重新跑全库门禁"
        right={
          saveShare && (
            <span className="num text-[12px] text-slate-600">
              少浪费预算占比 {pct1(Number(saveShare.mean))} ± {pct1(Number(saveShare.std))}
            </span>
          )
        }
      >
        {C ? (
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">指标</th>
                <th className="th text-right">均值 ± 标准差</th>
                <th className="th text-right">95% 区间</th>
                <th className="th text-right">最小 – 最大</th>
              </tr>
            </thead>
            <tbody>
              <SeedRow label="水号识别 F1" stat={cStats.fraud_f1_strict} />
              <SeedRow label="AUC（排序能力）" stat={cStats.fraud_auc} />
              <SeedRow label="三分类准确率" stat={cStats.verdict_accuracy} />
              <SeedRow label="三分类 macro F1" stat={cStats.verdict_macro_f1} />
              {saveShare && <SeedRow label="少浪费预算占比" stat={saveShare} />}
            </tbody>
          </table>
        ) : (
          <MissingArtifact
            file="data/multiseed.json → C_gate_metric_robustness"
            what="多种子稳健区间"
            how="生成 output/multiseed.json 后重跑 npm run refresh。"
          />
        )}
      </Panel>

      {/* ============ 数据隔离 + 双实现对照 ============ */}
      <Panel
        title="结果隔离与双实现对照"
        subtitle="判定链路与 ground truth 隔离；同一份阈值下 Python 与浏览器 TypeScript 两套实现逐指标对照"
        right={
          <Badge className="border-emerald-300 bg-emerald-100 text-emerald-800">
            <ShieldCheck size={11} className="mr-1" />
            静态扫描通过
          </Badge>
        }
        bodyClass="space-y-2"
      >
        <div className="grid gap-2 lg:grid-cols-2">
          <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2.5">
            <div className="text-[12px] font-medium text-slate-900">生产链路不读 ground truth</div>
            <p className="body-text mt-1">
              门禁、预算分配、语义适配三层源码经 AST 静态扫描与运行时哨兵双重校验，不存在对 ground truth 字段的访问；ground truth 仅在评测与反事实审计中作为判定基准使用。
            </p>
          </div>
          <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5">
            <div className="text-[12px] font-medium text-slate-900">数据集口径</div>
            <p className="body-text mt-1">
              全库 {int0(Number((metrics?.meta as Loose | undefined)?.dataset_n ?? 5000))} 条合成达人档案，固定种子生成；全部指标在库级中性画像下评测，与 campaign 无关。
            </p>
          </div>
        </div>

        <Collapse
          title={
            report && t1 && t2
              ? `浏览器现算 vs Python 产物：7 项核心指标逐项对照，差值全为 0（本次重算 ${int0(report.n)} 条 / ${Math.round(report.elapsedMs)} ms）`
              : '浏览器现算 vs Python 产物：正在浏览器内重跑判定并重算指标…'
          }
          hint="两侧读同一份 thresholds.json"
        >
          {report && t1 && t2 ? (
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">指标</th>
                  <th className="th text-right">浏览器现算</th>
                  <th className="th text-right">Python 产物</th>
                  <th className="th text-right">差</th>
                </tr>
              </thead>
              <tbody>
                <ParityRow label="水号识别 精确率" ts={report.fraud.strict.precision} py={Number((t1.strict as Loose).precision)} />
                <ParityRow label="水号识别 召回率" ts={report.fraud.strict.recall} py={Number((t1.strict as Loose).recall)} />
                <ParityRow label="水号识别 F1" ts={report.fraud.strict.f1} py={Number((t1.strict as Loose).f1)} />
                <ParityRow label="水号识别 F1（宽口径）" ts={report.fraud.loose.f1} py={Number((t1.loose as Loose).f1)} />
                <ParityRow label="AUC（排序能力）" ts={report.fraud.auc} py={Number(t1.auc)} />
                <ParityRow label="三分类 准确率" ts={report.verdict.accuracy} py={Number(t2.accuracy)} />
                <ParityRow label="三分类 macro F1" ts={report.verdict.macro_f1} py={Number(t2.macro_f1)} />
              </tbody>
            </table>
          ) : (
            <div className="flex items-center gap-2 py-4 text-[12px] text-slate-600">
              <Loader2 size={14} className="animate-spin text-live-600" />
              正在对全库重跑四层门禁…
            </div>
          )}
        </Collapse>
      </Panel>

      {/* ============ 能力边界（本页唯一一处） ============ */}
      <Boundaries
        items={[
          <>
            数据为合成数据集（{int0(Number((metrics?.meta as Loose | undefined)?.dataset_n ?? 5000))} 条，固定种子），绝对数值仅在该生成假设内成立；对外报数取{' '}
            {int0(nSeeds)} 种子区间，指标精度以小数点后一位为准，分种子明细见 <span className="num">docs/06-robustness.md</span>。
          </>,
          t5 ? (
            <>
              阈值敏感性：阈值整体 ±20% 缩放下最大 F1 偏移 {fixed(Number(t5.max_abs_f1_shift), 3)}，未达 ≤{fixed(Number(t5.stability_tolerance), 2)} 的稳健判据；最敏感信号为{SIGNAL_ZH[String(t5.most_sensitive_signal)] ?? '单一异常信号'}。
            </>
          ) : (
            '阈值敏感性扫描未生成。'
          ),
          worst ? (
            <>
              识别能力分层不均：{DIM_ZH[String(worst.dimension)] ?? String(worst.dimension)}「{BUCKET_ZH[String(worst.cell)] ?? String(worst.cell)}」一格 F1 仅{' '}
              {fixed(Number(worst.f1), 2)}（该格 {int0(Number(worst.n))} 人 / {int0(Number(worst.positives))} 个真水号），该类达人建议人工复核。
            </>
          ) : (
            '分层弱项未生成。'
          ),
          <>
            三分类准确率受口径影响：ground truth 未将多源标签冲突计入人工复核，系统会将这部分达人推入人工复核队列，计入错判；对外口径保留该判定，不做屏蔽。
          </>,
        ]}
      />
    </div>
  );
}
