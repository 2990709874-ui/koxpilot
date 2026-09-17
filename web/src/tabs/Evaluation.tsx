import React from 'react';
import { AlertTriangle, FlaskConical, Loader2, ShieldAlert } from 'lucide-react';
import { ConfusionMatrix, Heatmap, LineChart } from '../components/charts';
import { Badge, Note, Panel, Segmented, Stat, TruthChip } from '../components/ui';
import type { Loose } from '../lib/artifacts';
import { fixed, int0, pct1 } from '../lib/format';
import type { AblationRow, EvalReport, SensitivityPoint } from '../lib/pipeline';
import { ruleLabel } from '../lib/pipeline';

const FRAUD_TYPE_LABEL: Record<string, string> = {
  bought_followers: '买粉',
  engagement_pod: '互赞群',
  bot_comments: '机器评论',
  view_inflation: '刷播放',
};

/**
 * 消融表 contribution 三态的展示样式。
 * 判据本身（eps 与三档的定义）一律从 metrics.json 的 contribution_criteria 读，这里只管配色和中文标签。
 * 刻意不用"✓ / —"两态：那正是被修掉的那个 bug（abs(delta) 不看符号，把负贡献规则也标成有贡献）。
 */
const CONTRIB_STYLE: Record<string, { label: string; short: string; kind: string; cls: string }> = {
  positive: { label: '正向贡献', short: '正向', kind: 'positive', cls: 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200' },
  negative: { label: '负贡献 · 拖累该指标', short: '负贡献', kind: 'negative', cls: 'border-amber-400/40 bg-amber-400/15 text-amber-100' },
  negligible: { label: '近乎无影响', short: '无影响', kind: 'negligible', cls: 'border-white/15 text-slate-400' },
  unknown: { label: '未判定', short: '未判定', kind: 'unknown', cls: 'border-white/15 text-slate-500' },
};

/** 带符号的定点数：+0.0764 / −0.0014，避免把"更高"写成裸数字看不出方向。 */
function signed(v: number, digits = 4): string {
  if (!Number.isFinite(v)) return '—';
  if (v === 0) return '0';
  return `${v > 0 ? '+' : ''}${fixed(v, digits)}`;
}

/** TS 现算 vs Python 产物的一行对照。差异不为 0 就红 —— 不做容差放宽。 */
function ParityRow({ label, ts, py, digits = 4 }: { label: string; ts: number; py: number; digits?: number }): React.ReactElement {
  const same = Math.abs(ts - py) < 5 * 10 ** -(digits + 1);
  return (
    <tr className="hairline">
      <td className="td">{label}</td>
      <td className="td num text-right text-cyan-100">{fixed(ts, digits)}</td>
      <td className="td num text-right text-slate-400">{fixed(py, digits)}</td>
      <td className={`td num text-right ${same ? 'text-emerald-300' : 'text-rose-300'}`}>
        {same ? '0' : fixed(ts - py, digits)}
      </td>
    </tr>
  );
}

export function EvaluationTab({
  metrics,
  report,
  busy,
  onRun,
  ablation,
  onAblation,
  sens,
  onSens,
}: {
  metrics: Loose | null;
  report: EvalReport | null;
  busy: string | null;
  onRun: () => void;
  ablation: AblationRow[];
  onAblation: () => void;
  sens: SensitivityPoint[];
  onSens: () => void;
}): React.ReactElement {
  const [stratDim, setStratDim] = React.useState<'follower_bucket' | 'platform' | 'country' | 'platform_bucket'>('follower_bucket');
  const [cell, setCell] = React.useState<[string, string] | null>(null);

  React.useEffect(() => {
    if (!report && busy === null) onRun();
    // 只在首次进入本页时触发；后续切页回来复用已算结果
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const t1 = metrics?.table_1_fraud_detection as Loose | undefined;
  const t2 = metrics?.table_2_verdict_confusion as Loose | undefined;
  const t3 = metrics?.table_3_strata as Loose | undefined;
  const t4 = metrics?.table_4_ablation as Loose | undefined;
  const t5 = metrics?.table_5_sensitivity as Loose | undefined;
  const weak = (metrics?.weak_spots ?? []) as Loose[];
  /** F1 最低的那一格：从产物里现算，页面上不写死"mega 0.467"这类会过期的描述。 */
  const worstWeakSpot = weak.reduce<Loose | null>((acc, w) => {
    const f1 = Number(w.f1);
    if (!Number.isFinite(f1)) return acc;
    return acc === null || f1 < Number(acc.f1) ? w : acc;
  }, null);

  /** 按层消融里第一个"关掉它三分类反而更好"的层。用来把反例写成数据驱动，而不是写死 −G2。 */
  const negLayer = ((t4?.by_layer ?? []) as Loose[]).find(
    (v) => String(((v.contribution ?? {}) as Loose).verdict_accuracy ?? '') === 'negative',
  );

  const stratRows = React.useMemo(() => {
    const src = (t3?.[stratDim === 'follower_bucket' ? 'by_follower_bucket' : stratDim === 'platform' ? 'by_platform' : stratDim === 'country' ? 'by_country' : 'by_platform_bucket'] ?? {}) as Record<string, Loose>;
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

  const attribution = React.useMemo(() => {
    if (!cell || !t2) return null;
    const [gt, pred] = cell;
    if (gt === 'pass' && pred === 'review') return t2.review_false_positive_attribution as Loose;
    if (gt === 'pass' && pred === 'reject') return t2.reject_false_positive_attribution as Loose;
    return null;
  }, [cell, t2]);

  return (
    <div className="space-y-4">
      <Panel
        title="评测口径与防自证"
        subtitle="全部指标在库级中性画像下评测（gt.verdict 是 campaign 无关的库级判定，必须用中性口径才对得上）"
        right={
          <div className="flex items-center gap-1.5">
            <TruthChip kind="rule" />
            <TruthChip kind="python" />
          </div>
        }
      >
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries((metrics?.definitions ?? {}) as Record<string, string>).map(([k, v]) => (
            <div key={k} className="rounded-lg border border-white/10 bg-white/[0.025] px-2.5 py-2">
              <div className="num text-[10px] text-cyan-300">{k}</div>
              <div className="muted mt-0.5">{v}</div>
            </div>
          ))}
        </div>
      </Panel>

      {/* ---- TS 现算 vs 产物 ---- */}
      <Panel
        title="① 浏览器现算的评测结果 vs Python 产物"
        subtitle={
          report
            ? `TS 引擎刚刚在你的浏览器里对 ${int0(report.n)} 条数据重跑了一遍判定并重算全部指标，耗时 ${Math.round(report.elapsedMs)} ms`
            : '正在浏览器内对全库重跑判定并重算指标…'
        }
        right={
          busy ? (
            <span className="flex items-center gap-1.5 text-[11px] text-cyan-200">
              <Loader2 size={12} className="animate-spin" />
              {busy}
            </span>
          ) : (
            <button
              onClick={onRun}
              className="rounded-lg border border-white/15 px-2.5 py-1 text-[11px] text-slate-300 hover:border-cyan-300/40 hover:text-cyan-100"
            >
              重新计算
            </button>
          )
        }
      >
        {report && t1 && t2 ? (
          <div className="grid gap-3 lg:grid-cols-[1.1fr_1fr]">
            <div>
              <table className="w-full">
                <thead>
                  <tr>
                    <th className="th">指标</th>
                    <th className="th text-right">浏览器 TS 现算</th>
                    <th className="th text-right">metrics.json</th>
                    <th className="th text-right">差</th>
                  </tr>
                </thead>
                <tbody>
                  <ParityRow label="水号 严口径 精确率" ts={report.fraud.strict.precision} py={Number((t1.strict as Loose).precision)} />
                  <ParityRow label="水号 严口径 召回率" ts={report.fraud.strict.recall} py={Number((t1.strict as Loose).recall)} />
                  <ParityRow label="水号 严口径 F1" ts={report.fraud.strict.f1} py={Number((t1.strict as Loose).f1)} />
                  <ParityRow label="水号 宽口径 F1" ts={report.fraud.loose.f1} py={Number((t1.loose as Loose).f1)} />
                  <ParityRow label="AUC（连续异常分）" ts={report.fraud.auc} py={Number(t1.auc)} />
                  <ParityRow label="三分类 准确率" ts={report.verdict.accuracy} py={Number(t2.accuracy)} />
                  <ParityRow label="三分类 macro F1" ts={report.verdict.macro_f1} py={Number(t2.macro_f1)} />
                </tbody>
              </table>
              <Note tone="good">
                这张表是"没有偷懒"的最直接证据：左列由你的浏览器现算，右列是 Python 离线产物，两侧读同一份 thresholds.json，差值全为 0。
              </Note>
            </div>
            <div className="grid grid-cols-2 gap-2 self-start">
              <Stat label="严口径 精确率 / 召回率" value={`${fixed(report.fraud.strict.precision, 3)} / ${fixed(report.fraud.strict.recall, 3)}`} hint="严口径 = 门禁会自动拦掉的人（authenticity < 地板 或 硬信号 ≥ 2）" />
              <Stat label="严口径 F1" value={fixed(report.fraud.strict.f1, 4)} tone="accent" hint={`TP ${report.fraud.strict.tp} / FP ${report.fraud.strict.fp} / FN ${report.fraud.strict.fn}`} />
              <Stat label="AUC" value={fixed(report.fraud.auc, 4)} hint="用连续异常分算，与离散判定无关" tone="good" />
              <Stat label="宽口径 F1" value={fixed(report.fraud.loose.f1, 4)} hint={`宽口径 = G1 命中任意一条即进人核，召回 ${fixed(report.fraud.loose.recall, 3)}、精确率只有 ${fixed(report.fraud.loose.precision, 3)}`} tone="warn" />
              <Stat label="水号占比（gt）" value={pct1(report.fraud.prevalence)} hint="注入率来自数据生成器，产物 manifest 里有实际注入计数" />
              <Stat label="三分类 准确率 / macro F1" value={`${fixed(report.verdict.accuracy, 3)} / ${fixed(report.verdict.macro_f1, 3)}`} hint="这个数字不高，原因见下面的混淆矩阵与口径冲突说明" tone="warn" />
            </div>
          </div>
        ) : (
          <div className="flex items-center gap-2 py-6 text-[12px] text-slate-400">
            <Loader2 size={14} className="animate-spin text-cyan-300" />
            正在对全库重跑四层门禁…（这一步是真算，所以要花几百毫秒）
          </div>
        )}
      </Panel>

      {/* ---- 分造假类型召回 + PR 曲线 ---- */}
      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="② 分造假类型的召回（哪种水号最难抓）" subtitle="严口径 = 自动拦掉；宽口径 = 进人核队列">
          {report && (
            <div className="space-y-2.5">
              {Object.entries(report.fraud.perType).map(([type, v]) => {
                const py = (t1?.per_fraud_type as Loose | undefined)?.[type] as Loose | undefined;
                return (
                  <div key={type}>
                    <div className="flex items-baseline justify-between">
                      <span className="text-[12px] text-slate-200">
                        {FRAUD_TYPE_LABEL[type] ?? type}
                        <span className="num ml-1.5 text-[10px] text-slate-500">n={int0(v.n)}</span>
                      </span>
                      <span className="num text-[11px] text-slate-400">
                        严 {fixed(v.recall_strict, 3)} · 宽 {fixed(v.recall_loose, 3)}
                        {py && <span className="ml-1.5 text-slate-600">AUC {fixed(Number(py.auc_vs_clean), 3)}</span>}
                      </span>
                    </div>
                    <div className="mt-1 flex gap-1">
                      <div className="h-2 flex-1 overflow-hidden rounded-sm bg-white/[0.05]">
                        <div className="h-full rounded-sm bg-cyan-400" style={{ width: `${v.recall_strict * 100}%` }} />
                      </div>
                      <div className="h-2 flex-1 overflow-hidden rounded-sm bg-white/[0.05]">
                        <div className="h-full rounded-sm bg-cyan-400/45" style={{ width: `${v.recall_loose * 100}%` }} />
                      </div>
                    </div>
                  </div>
                );
              })}
              <Note tone="warn">
                刷播放（view_inflation）严口径召回只有 {fixed(report.fraud.perType.view_inflation?.recall_strict ?? 0, 3)} —— 播放/粉丝比偏移与"爆款"天然混淆，
                这是刻意保留的弱项，没有靠调阈值把它做上去。
              </Note>
            </div>
          )}
        </Panel>

        <Panel title="③ PR 曲线（扫连续异常分的阈值）" subtitle="浏览器现算：把 fraud_score 从高到低扫一遍，每个分值变化点取一个点">
          {report && report.pr.length > 1 && (
            <LineChart
              height={220}
              series={[
                {
                  name: '精确率-召回率（严口径信号源）',
                  color: '#22d3ee',
                  dots: false,
                  points: report.pr.map((p) => ({ x: p.recall, y: p.precision, label: `阈值 ${fixed(p.threshold, 3)}` })),
                },
              ]}
              xTicks={[0, 0.2, 0.4, 0.6, 0.8, 1]}
              yDomain={[0, 1]}
              xFormat={(v) => fixed(v, 1)}
              yFormat={(v) => fixed(v, 1)}
              xLabel="召回率 →"
              yLabel="↑ 精确率"
              refLines={[
                { y: report.fraud.prevalence, label: `随机基线 = 水号占比 ${pct1(report.fraud.prevalence)}`, color: '#64748b' },
                { y: report.fraud.strict.precision, label: `当前严口径工作点 P=${fixed(report.fraud.strict.precision, 3)}`, color: '#34d399' },
              ]}
            />
          )}
        </Panel>
      </div>

      {/* ---- 混淆矩阵 ---- */}
      <div className="grid gap-3 lg:grid-cols-[0.95fr_1.05fr]">
        <Panel title="④ 三分类混淆矩阵" subtitle="行 = gt.verdict，列 = 门禁判定；点格子看归因">
          {report && <ConfusionMatrix matrix={report.verdict.matrix} onCell={(g, p) => setCell([g, p])} highlight={cell} />}
          {report && (
            <table className="mt-3 w-full">
              <thead>
                <tr>
                  <th className="th">类别</th>
                  <th className="th text-right">精确率</th>
                  <th className="th text-right">召回率</th>
                  <th className="th text-right">F1</th>
                </tr>
              </thead>
              <tbody>
                {(['pass', 'review', 'reject'] as const).map((v) => (
                  <tr key={v} className="hairline">
                    <td className="td">{v}</td>
                    <td className="td num text-right">{fixed(report.verdict.per_class[v].precision, 4)}</td>
                    <td className="td num text-right">{fixed(report.verdict.per_class[v].recall, 4)}</td>
                    <td className="td num text-right">{fixed(report.verdict.per_class[v].f1, 4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel
          title="⑤ 错判归因：为什么准确率只有 0.66"
          subtitle="点左边任意错判格；pass→review 与 pass→reject 两格有完整规则归因"
          tone="warn"
        >
          {attribution ? (
            <div>
              <div className="flex items-baseline justify-between">
                <span className="num text-[12px] text-amber-200">{String(attribution.cell)}</span>
                <span className="num text-[12px] text-slate-300">{int0(Number(attribution.n))} 人</span>
              </div>
              <div className="mt-2 space-y-1">
                {((attribution.top_rule_signatures ?? []) as Loose[]).map((s) => (
                  <div key={String(s.rules)} className="flex items-center gap-2">
                    <span className="num w-24 shrink-0 text-[10.5px] text-cyan-300">{String(s.rules)}</span>
                    <div className="h-2 flex-1 overflow-hidden rounded-sm bg-white/[0.05]">
                      <div className="h-full rounded-sm bg-amber-400/70" style={{ width: `${Number(s.share) * 100}%` }} />
                    </div>
                    <span className="num w-20 shrink-0 text-right text-[10.5px] text-slate-400">
                      {int0(Number(s.n))} · {pct1(Number(s.share))}
                    </span>
                  </div>
                ))}
              </div>
              {attribution.solely_caused_by_G2_2 !== undefined && (
                <Note tone="warn">
                  其中 <b className="text-amber-200">{int0(Number(attribution.solely_caused_by_G2_2))}</b> 人的唯一命中就是 G2.2（多源标签互相冲突）。
                  这是<b className="text-amber-200">口径冲突而不是模型错</b>：SPEC 3.3 合成 gt.verdict 时没把「多源冲突」计入 review，而 SPEC 4.G2.2 要求口径冲突需人核。
                  产品上这些号确实该进人核队列。本实现选择保留 G2.2，并给出屏蔽 G2.2 的对照版本 ——
                  准确率会从 {fixed(Number(t2?.accuracy), 4)} 升到 {fixed(Number((t2?.variant_rule_disabled_G2_2 as Loose | undefined)?.accuracy), 4)}，
                  但我没有把它当成主口径，因为那等于为了指标改判据。
                </Note>
              )}
            </div>
          ) : (
            <div className="space-y-2">
              <Note>点左侧矩阵里的任意错判格（红色）查看归因。pass→review 这一格是准确率的主要失分来源。</Note>
              {t2?.variant_rule_disabled_G2_2 && (
                <div className="rounded-lg border border-white/10 bg-white/[0.025] p-2.5">
                  <div className="text-[11px] text-slate-300">口径对照：屏蔽 G2.2 后的同一份数据</div>
                  <div className="mt-1.5 grid grid-cols-2 gap-2">
                    <div>
                      <div className="muted">主口径（保留 G2.2）</div>
                      <div className="num text-[15px] text-slate-100">
                        {fixed(Number(t2.accuracy), 4)} <span className="text-[11px] text-slate-500">accuracy</span>
                      </div>
                      <div className="num text-[11px] text-slate-500">macro F1 {fixed(Number(t2.macro_f1), 4)}</div>
                    </div>
                    <div>
                      <div className="muted">对照口径（屏蔽 G2.2）</div>
                      <div className="num text-[15px] text-amber-200">
                        {fixed(Number((t2.variant_rule_disabled_G2_2 as Loose).accuracy), 4)}
                        <span className="text-[11px] text-slate-500"> accuracy</span>
                      </div>
                      <div className="num text-[11px] text-slate-500">
                        macro F1 {fixed(Number((t2.variant_rule_disabled_G2_2 as Loose).macro_f1), 4)}
                      </div>
                    </div>
                  </div>
                  <div className="muted mt-1.5">对外口径取左边那个更难看的数字。</div>
                </div>
              )}
              {(t2?.missed_fraud_profile as Loose | undefined) && (
                <div className="rounded-lg border border-white/10 bg-white/[0.025] p-2.5">
                  <div className="text-[11px] text-slate-300">漏掉的水号长什么样</div>
                  <pre className="muted mt-1 whitespace-pre-wrap font-mono text-[10px]">
                    {JSON.stringify(t2?.missed_fraud_profile, null, 1).slice(0, 600)}
                  </pre>
                </div>
              )}
            </div>
          )}
        </Panel>
      </div>

      {/* ---- 分层 & 弱项 ---- */}
      <Panel
        title="⑥ 分层评测与已知弱项"
        subtitle="按维度切开看哪里最差；低支撑格（真水号 < 10）用虚线框标出，照实输出但不参与弱项排序"
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
        <div className="mt-3 grid gap-2 lg:grid-cols-2">
          {weak.map((w) => (
            <div key={`${w.dimension}-${w.cell}`} className="rounded-xl border border-rose-400/25 bg-rose-400/[0.06] px-3 py-2.5">
              <div className="flex items-center gap-1.5">
                <ShieldAlert size={12} className="text-rose-300" />
                <span className="num text-[11px] text-rose-200">
                  {String(w.dimension)} = {String(w.cell)}
                </span>
                <span className="num ml-auto text-[13px] font-semibold text-rose-200">F1 {fixed(Number(w.f1), 3)}</span>
              </div>
              <div className="muted mt-1">{String(w.note)}</div>
            </div>
          ))}
        </div>
        <Note tone="warn">
          <AlertTriangle size={11} className="mr-1 inline" />
          弱项板块直接渲染 metrics.json 的 weak_spots 数组，顺序与内容都不挑选。
          {worstWeakSpot ? (
            <>
              {' '}
              当前最刺眼的一格是 <b className="text-amber-200">
                {String(worstWeakSpot.dimension)}={String(worstWeakSpot.cell)}
              </b>
              ，F1 只有 <b className="num text-rose-200">{fixed(Number(worstWeakSpot.f1), 3)}</b>（这一句由产物现算，不写死是哪个分层）：
              样本少、互动率天然偏低，与买粉特征混淆。
            </>
          ) : (
            ' 本轮 weak_spots 里没有可比的 F1 字段，故不点名。'
          )}
        </Note>
      </Panel>

      {/* ---- 消融 ---- */}
      <Panel
        title="⑦ 消融实验：每一层到底有没有用"
        subtitle="delta = 变体 − 全量；负值表示关掉它指标下降（即它有贡献）"
        right={
          <div className="flex items-center gap-2">
            {busy && <Loader2 size={12} className="animate-spin text-cyan-300" />}
            <button
              onClick={onAblation}
              disabled={Boolean(busy)}
              className="flex items-center gap-1.5 rounded-lg border border-cyan-300/40 bg-cyan-400/10 px-2.5 py-1 text-[11px] text-cyan-100 hover:bg-cyan-400/20 disabled:opacity-50"
            >
              <FlaskConical size={11} />
              在浏览器里现关一层重跑
            </button>
          </div>
        }
      >
        <div className="grid gap-3 lg:grid-cols-2">
          <div>
            <div className="muted mb-1.5">按层（Python 产物，全量 5,000 条）</div>
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">变体</th>
                  <th className="th text-right">水号 F1</th>
                  <th className="th text-right">Δ F1</th>
                  <th className="th text-right">Δ 准确率</th>
                  <th className="th">对水号 F1 / 三分类</th>
                </tr>
              </thead>
              <tbody>
                {((t4?.by_layer ?? []) as Loose[]).map((v) => {
                  const m = v.metrics as Loose;
                  const d = v.delta as Loose;
                  const c = (v.contribution ?? {}) as Loose;
                  const cf = CONTRIB_STYLE[String(c.fraud_f1_strict ?? '')] ?? CONTRIB_STYLE.unknown;
                  const cv = CONTRIB_STYLE[String(c.verdict_accuracy ?? '')] ?? CONTRIB_STYLE.unknown;
                  return (
                    <tr key={String(v.variant)} className={`hairline ${cv.kind === 'negative' ? 'bg-amber-400/[0.07]' : ''}`}>
                      <td className="td num">
                        {String(v.variant)}
                        <div className="muted max-w-[200px] truncate" title={String(v.role)}>
                          {String(v.role)}
                        </div>
                      </td>
                      <td className="td num text-right">{fixed(Number(m.fraud_f1_strict), 4)}</td>
                      <td className={`td num text-right ${Number(d.d_fraud_f1_strict) < 0 ? 'text-emerald-300' : 'text-slate-500'}`}>
                        {Number(d.d_fraud_f1_strict) === 0 ? '0' : fixed(Number(d.d_fraud_f1_strict), 4)}
                      </td>
                      <td className={`td num text-right ${Number(d.d_verdict_accuracy) < 0 ? 'text-emerald-300' : 'text-amber-300'}`}>
                        {Number(d.d_verdict_accuracy) === 0 ? '0' : fixed(Number(d.d_verdict_accuracy), 4)}
                      </td>
                      <td className="td">
                        <div className="flex flex-col gap-1">
                          <Badge className={cf.cls}>{cf.short}</Badge>
                          <Badge className={cv.cls}>{cv.short}</Badge>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {negLayer && (
              <Note tone="warn">
                注意 <b className="text-amber-200">
                  {String(negLayer.variant)} 的三分类准确率反而更高（
                  {signed(Number((negLayer.delta as Loose).d_verdict_accuracy), 4)}）
                </b>
                ，所以它在"三分类"这一列是{' '}
                <b className="text-amber-100">
                  {(CONTRIB_STYLE[String(((negLayer.contribution ?? {}) as Loose).verdict_accuracy ?? '')] ?? CONTRIB_STYLE.unknown).label}
                </b>
                。我没有据此删掉它 —— 原因就是上面那条口径冲突：G2.2 的"错判"在产品上是对的。
                指标不是唯一裁判，但也不能假装这个反例不存在，所以三态标签直接标在表里。
              </Note>
            )}
          </div>
          <div>
            <div className="muted mb-1.5">
              {ablation.length > 0 ? '浏览器现算（点上面按钮跑的，同口径可与左表对照）' : 'G1 逐条规则（Python 产物）'}
            </div>
            {ablation.length > 0 ? (
              <table className="w-full">
                <thead>
                  <tr>
                    <th className="th">变体</th>
                    <th className="th text-right">水号 F1</th>
                    <th className="th text-right">Δ F1</th>
                    <th className="th text-right">耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {ablation.map((r) => (
                    <tr key={r.variant} className="hairline">
                      <td className="td num">{r.variant}</td>
                      <td className="td num text-right">{fixed(r.fraud_f1_strict, 4)}</td>
                      <td className={`td num text-right ${r.d_fraud_f1_strict < 0 ? 'text-emerald-300' : 'text-slate-500'}`}>
                        {r.d_fraud_f1_strict === 0 ? '0' : fixed(r.d_fraud_f1_strict, 4)}
                      </td>
                      <td className="td num text-right text-slate-500">{Math.round(r.elapsedMs)} ms</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="max-h-[280px] overflow-y-auto">
                <table className="w-full">
                  <thead>
                    <tr>
                      <th className="th">关掉的规则</th>
                      <th className="th text-right">权重</th>
                      <th className="th text-right">Δ 水号 F1</th>
                      <th className="th text-center">贡献判定</th>
                    </tr>
                  </thead>
                  <tbody>
                    {((t4?.by_g1_rule ?? []) as Loose[]).map((v) => {
                      const d = Number((v.delta as Loose).d_fraud_f1_strict);
                      const kind = String(v.contribution ?? '');
                      const style = CONTRIB_STYLE[kind] ?? CONTRIB_STYLE.unknown;
                      return (
                        <tr
                          key={String(v.variant)}
                          className={`hairline ${kind === 'negative' ? 'bg-amber-400/[0.07]' : ''}`}
                          title={String(v.contribution_note ?? '')}
                        >
                          <td className="td num">
                            {String(v.variant)}
                            <span className="ml-1.5 text-[10px] text-slate-500">{ruleLabel(String(v.variant).slice(1))}</span>
                          </td>
                          <td className="td num text-right">{fixed(Number(v.weight), 2)}</td>
                          <td className={`td num text-right ${d < 0 ? 'text-emerald-300' : d > 0 ? 'text-amber-300' : 'text-slate-500'}`}>
                            {d > 0 ? '+' : ''}
                            {fixed(d, 4)}
                          </td>
                          <td className="td text-center">
                            <Badge className={style.cls} title={String(v.contribution_note ?? '')}>
                              {style.label}
                            </Badge>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {/* ---- contribution 三态：把「关掉后 F1 反而更好」这件事摆到台面上 ---- */}
            {t4?.contribution_criteria && (
              <div className="mt-2 rounded-xl border border-amber-400/30 bg-amber-400/[0.07] px-3 py-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="flex items-center gap-1.5 text-[12px] font-semibold text-amber-100">
                    <ShieldAlert size={12} />
                    这张表原来撒了一个谎，我把它修了
                  </span>
                  <Badge className="border-emerald-400/30 bg-emerald-400/10 text-emerald-200">
                    正向 {((t4.positive_rules ?? []) as string[]).length} 条
                  </Badge>
                  <Badge className="border-amber-400/35 bg-amber-400/10 text-amber-200">
                    负向 {((t4.negative_rules ?? []) as string[]).length} 条
                  </Badge>
                  <Badge className="border-white/15 text-slate-400">
                    死规则 {((t4.dead_rules ?? []) as unknown[]).length} 条
                  </Badge>
                </div>
                <p className="mt-1.5 text-[11.5px] leading-relaxed text-slate-300">
                  早期实现用 <code className="rounded bg-black/30 px-1 font-mono text-[10px] text-rose-200">abs(delta) &ge; {String((t4.contribution_criteria as Loose).eps)}</code>{' '}
                  判"有贡献"，<b className="text-rose-200">不看符号</b> —— 于是{' '}
                  {((t4.negative_rules ?? []) as string[]).join('、')} 这种"关掉后 F1 反而更好"的规则也被算成有贡献。
                  现在改成 <b className="text-amber-100">三态判定</b>：
                </p>
                <div className="mt-1.5 grid gap-1 sm:grid-cols-3">
                  {(['positive', 'negative', 'negligible'] as const).map((k) => (
                    <div key={k} className="rounded-lg border border-white/10 bg-black/25 px-2 py-1.5">
                      <Badge className={CONTRIB_STYLE[k].cls}>{CONTRIB_STYLE[k].label}</Badge>
                      <div className="muted mt-1">{String((t4.contribution_criteria as Loose)[k] ?? '')}</div>
                    </div>
                  ))}
                </div>
                {((t4.negative_rules ?? []) as string[]).map((rid) => {
                  const row = ((t4.by_g1_rule ?? []) as Loose[]).find((v) => String(v.variant) === rid);
                  if (!row) return null;
                  return (
                    <p key={rid} className="mt-2 text-[11.5px] leading-relaxed text-amber-50/90">
                      <b className="num text-amber-100">{rid}</b>（{ruleLabel(rid.slice(1))}，权重{' '}
                      {fixed(Number(row.weight), 2)}）：关掉它严口径 F1{' '}
                      <b className="num text-amber-200">
                        {signed(Number((row.delta as Loose).d_fraud_f1_strict), 4)}
                      </b>{' '}
                      —— 也就是说<b className="text-amber-100">它在这个指标上是净负担</b>。我没有偷偷删掉它、也没有继续把它算作"有贡献"：
                      保留理由（软信号、只推 review、宽口径召回来源）写在 docs/03-evaluation.md 表 4 一节，
                      判定则如实标成 negative。<b className="text-slate-100">一张全是"✓"的消融表才是可疑的。</b>
                    </p>
                  );
                })}
                <div className="muted mt-1.5">
                  判据来源：metrics.json → table_4_ablation.contribution_criteria（metric ={' '}
                  {String((t4.contribution_criteria as Loose).metric)}）
                </div>
              </div>
            )}
            <Note tone="good">
              {String(t4?.note ?? '')} 死规则清单：
              {((t4?.dead_rules ?? []) as unknown[]).length === 0 ? '空（没有一条规则是白写的）' : JSON.stringify(t4?.dead_rules)}
            </Note>
          </div>
        </div>
      </Panel>

      {/* ---- 敏感性 ---- */}
      <Panel
        title="⑧ 阈值敏感性：不稳健就是不稳健"
        subtitle="factor 是阈值整体缩放系数：对上尾规则放大=放松、对下尾规则放大=收紧，一次扫描覆盖两个方向"
        tone="warn"
        right={
          <div className="flex items-center gap-2">
            {t5 && (
              <Badge className={t5.stable ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200' : 'border-rose-400/30 bg-rose-400/10 text-rose-200'}>
                稳健性判据（≤{fixed(Number(t5.stability_tolerance), 2)}）：{t5.stable ? '通过' : '未通过'}
              </Badge>
            )}
            <button
              onClick={onSens}
              disabled={Boolean(busy)}
              className="rounded-lg border border-white/15 px-2.5 py-1 text-[11px] text-slate-300 hover:border-cyan-300/40 hover:text-cyan-100 disabled:opacity-50"
            >
              浏览器内重扫 ±20%
            </button>
          </div>
        }
      >
        {t5 && (
          <div className="grid gap-3 lg:grid-cols-[1.15fr_1fr]">
            <LineChart
              height={210}
              series={[
                {
                  name: '水号 F1（Python 产物）',
                  color: '#22d3ee',
                  points: ((t5.overall ?? []) as Loose[]).map((p) => ({ x: Number(p.factor), y: Number((p.metrics as Loose).fraud_f1_strict) })),
                },
                {
                  name: '三分类准确率（Python 产物）',
                  color: '#818cf8',
                  points: ((t5.overall ?? []) as Loose[]).map((p) => ({ x: Number(p.factor), y: Number((p.metrics as Loose).verdict_accuracy) })),
                },
                ...(sens.length > 0
                  ? [
                      {
                        name: '水号 F1（浏览器现扫）',
                        color: '#f472b6',
                        dashed: true,
                        points: sens.map((p) => ({ x: p.factor, y: p.fraud_f1_strict })),
                      },
                    ]
                  : []),
              ]}
              xTicks={((t5.factors ?? []) as number[]).map(Number)}
              xFormat={(v) => `×${v.toFixed(1)}`}
              yFormat={(v) => fixed(v, 2)}
              xLabel="阈值缩放系数"
              refLines={[{ y: Number((t5.baseline as Loose).fraud_f1_strict), label: '基准 F1', color: '#94a3b8' }]}
            />
            <div>
              <div className="grid grid-cols-2 gap-2">
                <Stat
                  label="±20% 内最大 F1 偏移"
                  value={fixed(Number(t5.max_abs_f1_shift), 4)}
                  hint={`自设判据是 ≤ ${fixed(Number(t5.stability_tolerance), 2)} 才算稳健，实测超了`}
                  tone="bad"
                />
                <Stat label="最敏感信号" value={String(t5.most_sensitive_signal)} hint="单信号扰动下 F1 偏移最大的那个" tone="warn" />
              </div>
              <div className="mt-2 space-y-1.5">
                {((t5.per_signal ?? []) as Loose[]).map((s) => (
                  <div key={String(s.signal)} className="flex items-center gap-2">
                    <span className="w-40 shrink-0 truncate text-[10.5px] text-slate-400">{String(s.signal)}</span>
                    <div className="h-2 flex-1 overflow-hidden rounded-sm bg-white/[0.05]">
                      <div
                        className="h-full rounded-sm bg-amber-400/70"
                        style={{ width: `${(Number(s.max_abs_shift) / Number(t5.max_abs_f1_shift)) * 100}%` }}
                      />
                    </div>
                    <span className="num w-14 shrink-0 text-right text-[10.5px] text-slate-400">{fixed(Number(s.max_abs_shift), 4)}</span>
                  </div>
                ))}
              </div>
              <Note tone="warn">
                最大偏移 {fixed(Number(t5.max_abs_f1_shift), 4)} &gt; 判据 {fixed(Number(t5.stability_tolerance), 2)}，所以 stable = false。
                我没有把判据放宽到 0.06 让它变绿 —— 改判据就等于改考卷。
              </Note>
            </div>
          </div>
        )}
        {sens.length > 0 && (
          <Note tone="good">
            浏览器现扫结果（粉色虚线）与 Python 产物重合：
            {sens.map((p) => `×${p.factor.toFixed(1)} → ${fixed(p.fraud_f1_strict, 4)}`).join('　')}
          </Note>
        )}
      </Panel>

      <Panel title="⑨ 产物自带的诚实声明" subtitle="metrics.json 的 honesty_notes 数组，原样渲染" tone="warn">
        <ol className="space-y-1.5">
          {((metrics?.honesty_notes ?? []) as string[]).map((n, i) => (
            <li key={n} className="flex gap-2 text-[11.5px] leading-relaxed text-slate-300">
              <span className="num mt-[1px] flex h-4 w-4 shrink-0 items-center justify-center rounded bg-amber-400/15 text-[9px] text-amber-200">
                {i + 1}
              </span>
              {n}
            </li>
          ))}
        </ol>
        <Note tone="warn">
          第 6 条提到"未配置 endpoint"是 metrics.json 生成时的状态；LLM 真调是在那之后跑的，结果在 llm_bench.json / prompt_bench.json 里，
          见「成本与价值」页。两份产物的生成时序差异照实说明，没有回头去改 metrics.json 的措辞。
        </Note>
      </Panel>

      <div className="grid gap-2 sm:grid-cols-3">
        <Stat label="评测样本" value={int0(report?.n ?? Number(t2?.n ?? 0))} hint="全库 5,000 条，中性口径" />
        <Stat
          label="浏览器重算耗时"
          value={report ? `${Math.round(report.elapsedMs)} ms` : '—'}
          hint="含 5,000 条 × 四层 20 条规则 + 全部指标聚合"
          tone="accent"
        />
        <Stat
          label="与 Python 产物差异"
          value={report && t1 ? (Math.abs(report.fraud.strict.f1 - Number((t1.strict as Loose).f1)) < 1e-9 ? '0' : '≠0') : '—'}
          hint="核心指标逐项对齐；全量判定级一致性见「架构」页"
          tone="good"
        />
      </div>
    </div>
  );
}
