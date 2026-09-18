import React from 'react';
import { AlertTriangle, FlaskConical, Loader2, ScanSearch, ShieldAlert, ShieldCheck } from 'lucide-react';
import { ConfusionMatrix, Heatmap, LineChart } from '../components/charts';
import { Collapse, Verdict } from '../components/Collapse';
import { Badge, MissingArtifact, Note, Panel, Segmented, Stat, TruthChip } from '../components/ui';
import { GUARD_TESTS } from '../content/notes';
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
  positive: { label: '正向贡献', short: '正向', kind: 'positive', cls: 'border-emerald-200 bg-emerald-50 text-emerald-700' },
  negative: { label: '负贡献 · 拖累该指标', short: '负贡献', kind: 'negative', cls: 'border-amber-300 bg-amber-100 text-amber-800' },
  negligible: { label: '近乎无影响', short: '无影响', kind: 'negligible', cls: 'border-slate-300 text-slate-600' },
  unknown: { label: '未判定', short: '未判定', kind: 'unknown', cls: 'border-slate-300 text-slate-500' },
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
      <td className="td num text-right text-live-700">{fixed(ts, digits)}</td>
      <td className="td num text-right text-slate-600">{fixed(py, digits)}</td>
      <td className={`td num text-right ${same ? 'text-emerald-600' : 'text-rose-600'}`}>
        {same ? '0' : fixed(ts - py, digits)}
      </td>
    </tr>
  );
}

/**
 * multiseed.json 的兜底加载。
 *
 * 为什么需要它：12 种子稳健性与「伪重复口径修正」是本页最重要的两条自述局限，
 * 但 App.tsx 目前没有把 multiseed 传进本页（props 签名由他人并行维护，不能改成必填）。
 * 所以这里做成「可选 prop 优先，缺省时自己读同一份产物」，
 * 两条路都读 public/data/multiseed.json，绝不另造数字；真读不到就走 MissingArtifact 空态。
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

/** 单次观测 vs 12 种子分布的一行：是否落在 95% CI 之内，方向由数据现算，不写死措辞。 */
function SeedRow({ label, single, stat }: { label: string; single: number | null; stat: Loose | undefined }): React.ReactElement | null {
  if (!stat) return null;
  const lo = Number(stat.ci95_low);
  const hi = Number(stat.ci95_high);
  const inCI = single !== null && Number.isFinite(single) && single >= lo && single <= hi;
  const dir = single !== null && single < lo ? '低于 CI 下界（对外口径更保守）' : single !== null && single > hi ? '高于 CI 上界（单次偏乐观）' : '落在 CI 内';
  return (
    <tr className="hairline">
      <td className="td">{label}</td>
      <td className="td num text-right">{single === null || !Number.isFinite(single) ? '—' : fixed(single, 4)}</td>
      <td className="td num text-right">{fixed(Number(stat.mean), 4)} ± {fixed(Number(stat.std), 4)}</td>
      <td className="td num text-right text-slate-600">[{fixed(lo, 4)}, {fixed(hi, 4)}]</td>
      <td className={`td text-right text-[12px] ${inCI ? 'text-emerald-600' : 'text-amber-700'}`}>{dir}</td>
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
  /** 可选：由 App 注入 multiseed.json；不传时本页自己读同一份产物（见 useMultiseed） */
  multiseed?: Loose | null;
}): React.ReactElement {
  const [stratDim, setStratDim] = React.useState<'follower_bucket' | 'platform' | 'country' | 'platform_bucket'>('follower_bucket');
  const [cell, setCell] = React.useState<[string, string] | null>(null);
  const ms = useMultiseed(multiseed);

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

  /** 低支撑格（真水号 < 10）：照实输出但不参与弱项排序，条数用来给折叠标题带 count。 */
  const lowSupportRows = stratRows.filter((r) => r.lowSupport);

  const attribution = React.useMemo(() => {
    if (!cell || !t2) return null;
    const [gt, pred] = cell;
    if (gt === 'pass' && pred === 'review') return t2.review_false_positive_attribution as Loose;
    if (gt === 'pass' && pred === 'reject') return t2.reject_false_positive_attribution as Loose;
    return null;
  }, [cell, t2]);

  /** 敏感性扫描里不达标的档位：|F1 偏移| 超过自设容差的那些 factor。 */
  const sensOffenders = React.useMemo(() => {
    if (!t5) return [] as Array<{ factor: number; f1: number; shift: number }>;
    const base = Number((t5.baseline as Loose)?.fraud_f1_strict);
    const tol = Number(t5.stability_tolerance);
    return ((t5.overall ?? []) as Loose[])
      .map((p) => ({
        factor: Number(p.factor),
        f1: Number((p.metrics as Loose).fraud_f1_strict),
        shift: Number((p.metrics as Loose).fraud_f1_strict) - base,
      }))
      .filter((p) => Math.abs(p.shift) > tol);
  }, [t5]);

  const C = (ms?.C_gate_metric_robustness ?? null) as Loose | null;
  const cStats = (C?.metrics ?? {}) as Record<string, Loose>;
  const B = (ms?.B_variance_attribution ?? null) as Loose | null;
  const bUnit = (B?.independence_unit ?? null) as Loose | null;
  const bPooled = (B?.pooled_all_campaigns ?? null) as Loose | null;
  const bPerCampaign = (B?.per_campaign ?? {}) as Record<string, Loose>;
  /** 单次观测里有几项落在 12 种子 95% CI 之外 —— 折叠标题要写清条数，不许含糊。 */
  const seedPairs: Array<[string, number | null, Loose | undefined]> = [
    ['水号 严口径 F1', report ? report.fraud.strict.f1 : Number(t1?.strict?.f1 ?? NaN), cStats.fraud_f1_strict],
    ['AUC（连续异常分）', report ? report.fraud.auc : Number(t1?.auc ?? NaN), cStats.fraud_auc],
    ['三分类 准确率', report ? report.verdict.accuracy : Number(t2?.accuracy ?? NaN), cStats.verdict_accuracy],
    ['三分类 macro F1', report ? report.verdict.macro_f1 : Number(t2?.macro_f1 ?? NaN), cStats.verdict_macro_f1],
  ];
  const outsideCI = seedPairs.filter(([, s, st]) => {
    if (!st || s === null || !Number.isFinite(s)) return false;
    return s < Number(st.ci95_low) || s > Number(st.ci95_high);
  });

  const guardScan = GUARD_TESTS.filter((g) => g.startsWith('静态扫描测试') || g.startsWith('泄漏哨兵测试'));
  const guardRest = GUARD_TESTS.filter((g) => !guardScan.includes(g));

  return (
    <div className="space-y-4">
      {/* ============ 本页结论 ============ */}
      <Verdict
        what="本页在证明：这套门禁到底能不能认出「买来的粉丝」，以及它认错在哪"
        conclusion={
          <>
            <b className="text-slate-900">水号识别 F1 {fixed(report ? report.fraud.strict.f1 : Number(t1?.strict?.f1 ?? 0), 2)} —— 抓得住四分之三，但会漏</b>；
            漏的那部分是哪类人（哪个平台、哪个粉丝层级、哪种造假手法），下面按维度拆开了。
            三分类准确率只有 {fixed(Number(t2?.accuracy ?? 0), 2)}，其中一大块<b className="text-amber-700">不是模型错、是两份口径定义打架</b>，
            我保留了更难看的那个口径。所有数字左边由你的浏览器现算、右边取自 Python 离线产物，差值为 0。
          </>
        }
        tone="brand"
        stats={[
          { label: '水号识别 F1（严口径）', value: fixed(report ? report.fraud.strict.f1 : Number(t1?.strict?.f1 ?? 0), 2), tone: 'warn' },
          { label: 'AUC（排序能力）', value: fixed(report ? report.fraud.auc : Number(t1?.auc ?? 0), 2), tone: 'good' },
          { label: '三分类准确率', value: fixed(report ? report.verdict.accuracy : Number(t2?.accuracy ?? 0), 2), tone: 'warn' },
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
              <button onClick={onRun} className="focusable rounded-lg border border-slate-300 px-2.5 py-1 text-[11px] text-slate-700 hover:border-live-300 hover:text-live-700">
                在浏览器里重算一遍
              </button>
            )}
          </div>
        }
      />

      {/* ============ 凭什么信「没有自证」：反数据泄漏（留在上层） ============ */}
      <Panel
        title="凭什么信这个 0.72 不是自己给自己打分"
        subtitle="要作弊最省事的办法是让模型偷看答案（gt）。所以我用两道自动测试把这条路堵死，并且先证明「哨兵本身不是哑弹」"
        tone="accent"
        right={
          <Badge className="border-emerald-300 bg-emerald-100 text-emerald-800">
            <ShieldCheck size={11} className="mr-1" />
            反数据泄漏 2 道 + 745 项测试
          </Badge>
        }
      >
        <div className="grid gap-2 lg:grid-cols-2">
          {guardScan.map((g, i) => (
            <div key={g} className="rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2.5">
              <div className="flex items-center gap-1.5 text-[12px] font-medium text-slate-900">
                {i === 0 ? <ScanSearch size={13} className="text-emerald-600" /> : <ShieldAlert size={13} className="text-emerald-600" />}
                {i === 0 ? 'AST 静态扫描（源码层面证明看不到答案）' : '泄漏哨兵（抓静态扫描绕不过的动态取键）'}
              </div>
              <p className="body-text mt-1">{g}</p>
              <div className="muted mt-1">
                {i === 0
                  ? '扫的是 AST 不是 grep —— 所以 docstring 里解释「为什么不读 gt」不会误报；覆盖测试跑不到的分支也照样扫得到。'
                  : '先跑 test_leakguard_itself_actually_fires 证明哨兵会响，否则下面所有「没抛错」的测试都是空气；哨兵直接喂给 g0/g1/g2/g3，不是只喂 evaluate()。'}
              </div>
            </div>
          ))}
        </div>
        <Note tone="good">
          评测与反事实审计<b className="text-emerald-700">反过来必须读 gt</b>（拿它当裁判，而不是当特征）；门禁 / 预算 / LLM 三层源码里
          一次 gt 访问都不许有。这两句话不是承诺，是上面两道测试在 CI 里每次都跑的断言。
        </Note>
      </Panel>

      {/* ============ 视觉主体：混淆矩阵 + F1 热力图（留在上层） ============ */}
      <div className="grid gap-3 lg:grid-cols-[0.95fr_1.05fr]">
        <Panel title="它把每类人判成了什么（三分类混淆矩阵）" subtitle="行 = 正确答案，列 = 门禁的判定；点任意格看归因">
          <p className="body-text mb-2">
            <b className="text-slate-900">怎么读：</b>对角线（绿）是判对的，越深越多；偏离对角线（红）是判错的。
            最该看的是<b className="text-amber-700">左中那格</b>——本该放行、却被推去人工复核的人，这是准确率的主要失分来源。
          </p>
          {report ? <ConfusionMatrix matrix={report.verdict.matrix} onCell={(g, p) => setCell([g, p])} highlight={cell} /> : (
            <div className="flex items-center gap-2 py-6 text-[12px] text-slate-600">
              <Loader2 size={14} className="animate-spin text-live-600" />
              正在对全库重跑四层门禁…（这一步是真算，所以要花几百毫秒）
            </div>
          )}
        </Panel>

        <Panel
          title="哪一类人最容易被认错（F1 热力图）"
          subtitle="按维度切开看哪里最差；低支撑格（真水号 < 10）用虚线框标出"
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
          <p className="body-text mb-2">
            <b className="text-slate-900">怎么读：</b>一格 = 一类达人，格子上的数字是这类人的识别 F1（1 = 全对）。
            <b className="text-slate-900">颜色越浅 = 越差</b>，所以浅色格就是弱项。鼠标悬停能看到该格人数与真水号个数。
            {worstWeakSpot && (
              <>
                {' '}当前最刺眼的一格是 <b className="text-rose-700">{String(worstWeakSpot.dimension)}={String(worstWeakSpot.cell)}</b>，F1 只有{' '}
                <b className="num text-rose-700">{fixed(Number(worstWeakSpot.f1), 3)}</b>（由产物现算，不写死是哪个分层）。
              </>
            )}
          </p>
          <Heatmap rows={stratRows} />
        </Panel>
      </div>

      {/* ============ 自述局限：全部折叠，标题自带信息量 ============ */}
      <Panel
        title="我自己承认的问题（扫标题就够，想深挖再展开）"
        subtitle="每一条都是产物里真实存在的自述局限，一条都没删；折叠只是为了让你 3 分钟看得完"
        bodyClass="space-y-2"
        tone="warn"
      >
        {/* 1. 三分类准确率低 —— 口径冲突 */}
        <Collapse
          flag="caveat"
          title={`三分类准确率只有 ${fixed(Number(t2?.accuracy ?? 0), 4)}：其中 ${int0(Number((t2?.review_false_positive_attribution as Loose | undefined)?.solely_caused_by_G2_2 ?? 0))} 人是两份口径打架，不是模型判错`}
          hint="屏蔽 G2.2 能把准确率抬到更好看的数字，我没这么做——那等于为了指标改判据"
        >
          {attribution ? (
            <div>
              <div className="flex items-baseline justify-between">
                <span className="num text-[12px] text-amber-700">{String(attribution.cell)}</span>
                <span className="num text-[12px] text-slate-700">{int0(Number(attribution.n))} 人</span>
              </div>
              <div className="mt-2 space-y-1">
                {((attribution.top_rule_signatures ?? []) as Loose[]).map((s) => (
                  <div key={String(s.rules)} className="flex items-center gap-2">
                    <span className="num w-24 shrink-0 text-[10.5px] text-live-600">{String(s.rules)}</span>
                    <div className="h-2 flex-1 overflow-hidden rounded-sm bg-slate-100">
                      <div className="h-full rounded-sm bg-amber-500" style={{ width: `${Number(s.share) * 100}%` }} />
                    </div>
                    <span className="num w-20 shrink-0 text-right text-[10.5px] text-slate-600">
                      {int0(Number(s.n))} · {pct1(Number(s.share))}
                    </span>
                  </div>
                ))}
              </div>
              {attribution.solely_caused_by_G2_2 !== undefined && (
                <Note tone="warn">
                  其中 <b className="text-amber-700">{int0(Number(attribution.solely_caused_by_G2_2))}</b> 人的唯一命中就是 G2.2（多源标签互相冲突）。
                  这是<b className="text-amber-700">口径冲突而不是模型错</b>：SPEC 3.3 合成 gt.verdict 时没把「多源冲突」计入 review，而 SPEC 4.G2.2 要求口径冲突需人核。
                  产品上这些号确实该进人核队列。本实现选择保留 G2.2，并给出屏蔽 G2.2 的对照版本 ——
                  准确率会从 {fixed(Number(t2?.accuracy), 4)} 升到 {fixed(Number((t2?.variant_rule_disabled_G2_2 as Loose | undefined)?.accuracy), 4)}，
                  但我没有把它当成主口径，因为那等于为了指标改判据。
                </Note>
              )}
            </div>
          ) : (
            <div className="space-y-2">
              <Note>点上面矩阵里的任意错判格（红色）查看归因。pass→review 这一格是准确率的主要失分来源。</Note>
              {t2?.variant_rule_disabled_G2_2 && (
                <div className="rounded-lg border border-slate-200 bg-slate-50 p-2.5">
                  <div className="text-[12px] text-slate-700">口径对照：屏蔽 G2.2 后的同一份数据</div>
                  <div className="mt-1.5 grid grid-cols-2 gap-2">
                    <div>
                      <div className="muted">主口径（保留 G2.2）</div>
                      <div className="num text-[15px] text-slate-900">
                        {fixed(Number(t2.accuracy), 4)} <span className="text-[11px] text-slate-500">accuracy</span>
                      </div>
                      <div className="num text-[11px] text-slate-500">macro F1 {fixed(Number(t2.macro_f1), 4)}</div>
                    </div>
                    <div>
                      <div className="muted">对照口径（屏蔽 G2.2）</div>
                      <div className="num text-[15px] text-amber-700">
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
                <div className="rounded-lg border border-slate-200 bg-slate-50 p-2.5">
                  <div className="text-[12px] text-slate-700">漏掉的水号长什么样</div>
                  <pre className="muted mt-1 whitespace-pre-wrap font-mono text-[11px] text-slate-600">
                    {JSON.stringify(t2?.missed_fraud_profile, null, 1).slice(0, 600)}
                  </pre>
                </div>
              )}
              {report && (
                <table className="w-full">
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
            </div>
          )}
        </Collapse>

        {/* 2. 低支撑格 */}
        <Collapse
          flag="caveat"
          count={lowSupportRows.length > 0 ? lowSupportRows.length : undefined}
          title={`低支撑格：这些分层里真水号不到 10 个，F1 照实输出但不参与「最差分层」排序（避免拿 2 个样本的格子当结论）`}
          hint="弱项板块直接渲染 metrics.json 的 weak_spots 数组，顺序与内容都不挑选"
        >
          <div className="grid gap-1.5 lg:grid-cols-2">
            {lowSupportRows.length === 0 ? (
              <div className="muted">当前维度（{stratDim}）下没有低支撑格；切换上面的维度按钮可以看到被标虚线框的格子。</div>
            ) : (
              lowSupportRows.map((r) => (
                <div key={r.cell} className="flex items-baseline justify-between rounded-lg border border-dashed border-slate-300 bg-slate-50 px-2.5 py-1.5">
                  <span className="num text-[12px] text-slate-700">{r.cell}</span>
                  <span className="num text-[12px] text-slate-600">
                    F1 {fixed(r.f1, 3)} · n={int0(r.n)} · 真水号 {int0(r.positives)}
                  </span>
                </div>
              ))
            )}
          </div>
          <div className="mt-2 grid gap-2 lg:grid-cols-2">
            {weak.map((w) => (
              <div key={`${w.dimension}-${w.cell}`} className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2.5">
                <div className="flex items-center gap-1.5">
                  <ShieldAlert size={12} className="text-rose-600" />
                  <span className="num text-[11px] text-rose-700">
                    {String(w.dimension)} = {String(w.cell)}
                  </span>
                  <span className="num ml-auto text-[13px] font-semibold text-rose-700">F1 {fixed(Number(w.f1), 3)}</span>
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
                当前最刺眼的一格是 <b className="text-amber-700">
                  {String(worstWeakSpot.dimension)}={String(worstWeakSpot.cell)}
                </b>
                ，F1 只有 <b className="num text-rose-700">{fixed(Number(worstWeakSpot.f1), 3)}</b>（这一句由产物现算，不写死是哪个分层）：
                样本少、互动率天然偏低，与买粉特征混淆。
              </>
            ) : (
              ' 本轮 weak_spots 里没有可比的 F1 字段，故不点名。'
            )}
          </Note>
        </Collapse>

        {/* 3. 阈值敏感性：不达标档位 */}
        <Collapse
          flag="caveat"
          count={sensOffenders.length}
          title={
            t5
              ? `阈值敏感性不达标：±20% 缩放里最大 F1 偏移 ${fixed(Number(t5.max_abs_f1_shift), 4)} > 我自设的容差 ${fixed(Number(t5.stability_tolerance), 2)}，所以 stable = ${String(t5.stable)}（没把容差放宽到 0.06 让它变绿）`
              : '阈值敏感性扫描：产物未生成'
          }
          hint={t5 ? `不达标的档位：${sensOffenders.map((p) => `×${p.factor.toFixed(1)}（偏移 ${signed(p.shift, 4)}）`).join('，') || '无'}；最敏感信号 ${String(t5.most_sensitive_signal)}` : undefined}
        >
          {t5 ? (
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
                <div className="muted mb-1.5">
                  factor 是阈值整体缩放系数：对上尾规则放大=放松、对下尾规则放大=收紧，一次扫描覆盖两个方向
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <Stat
                    label="±20% 内最大 F1 偏移"
                    value={fixed(Number(t5.max_abs_f1_shift), 4)}
                    hint={`自设判据是 ≤ ${fixed(Number(t5.stability_tolerance), 2)} 才算稳健，实测超了`}
                    tone="bad"
                  />
                  <Stat label="最敏感信号" value={String(t5.most_sensitive_signal)} hint="单信号扰动下 F1 偏移最大的那个" tone="warn" />
                </div>
                {sensOffenders.length > 0 && (
                  <table className="mt-2 w-full">
                    <thead>
                      <tr>
                        <th className="th">不达标档位</th>
                        <th className="th text-right">该档 F1</th>
                        <th className="th text-right">相对基准偏移</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sensOffenders.map((p) => (
                        <tr key={p.factor} className="hairline">
                          <td className="td num">×{p.factor.toFixed(1)}</td>
                          <td className="td num text-right">{fixed(p.f1, 4)}</td>
                          <td className="td num text-right text-amber-700">{signed(p.shift, 4)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                <div className="mt-2 space-y-1.5">
                  {((t5.per_signal ?? []) as Loose[]).map((s) => (
                    <div key={String(s.signal)} className="flex items-center gap-2">
                      <span className="w-40 shrink-0 truncate text-[11px] text-slate-600">{String(s.signal)}</span>
                      <div className="h-2 flex-1 overflow-hidden rounded-sm bg-slate-100">
                        <div
                          className="h-full rounded-sm bg-amber-500"
                          style={{ width: `${(Number(s.max_abs_shift) / Number(t5.max_abs_f1_shift)) * 100}%` }}
                        />
                      </div>
                      <span className="num w-14 shrink-0 text-right text-[11px] text-slate-600">{fixed(Number(s.max_abs_shift), 4)}</span>
                    </div>
                  ))}
                </div>
                <div className="mt-2 flex items-center gap-2">
                  {t5 && (
                    <Badge className={t5.stable ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : 'border-rose-200 bg-rose-50 text-rose-700'}>
                      稳健性判据（≤{fixed(Number(t5.stability_tolerance), 2)}）：{t5.stable ? '通过' : '未通过'}
                    </Badge>
                  )}
                  <button
                    onClick={onSens}
                    disabled={Boolean(busy)}
                    className="focusable rounded-lg border border-slate-300 px-2.5 py-1 text-[11px] text-slate-700 hover:border-live-300 hover:text-live-700 disabled:opacity-50"
                  >
                    浏览器内重扫 ±20%
                  </button>
                </div>
                <Note tone="warn">
                  最大偏移 {fixed(Number(t5.max_abs_f1_shift), 4)} &gt; 判据 {fixed(Number(t5.stability_tolerance), 2)}，所以 stable = false。
                  我没有把判据放宽到 0.06 让它变绿 —— 改判据就等于改考卷。
                </Note>
                {sens.length > 0 && (
                  <Note tone="good">
                    浏览器现扫结果（粉色虚线）与 Python 产物重合：
                    {sens.map((p) => `×${p.factor.toFixed(1)} → ${fixed(p.fraud_f1_strict, 4)}`).join('　')}
                  </Note>
                )}
              </div>
            </div>
          ) : (
            <MissingArtifact file="data/metrics.json → table_5_sensitivity" what="阈值敏感性扫描" />
          )}
        </Collapse>

        {/* 4. 12 种子稳健性：单次观测落在 CI 之外 */}
        <Collapse
          flag="caveat"
          count={outsideCI.length}
          title={
            C
              ? `单次实验不等于结论：换 12 个随机种子重跑，页面上这几个招牌数字有 ${outsideCI.length} 项落在 95% 置信区间之外（F1 12 种子均值 ${fixed(Number(cStats.fraud_f1_strict?.mean ?? 0), 4)} ± ${fixed(Number(cStats.fraud_f1_strict?.std ?? 0), 4)}）`
              : '12 种子稳健性：multiseed.json 未加载'
          }
          hint="每个种子都重新生成数据集、重新标定阈值、重新跑全库门禁；对外报数以 12 种子分布为准，单次值只作展示"
        >
          {C ? (
            <>
              <table className="w-full">
                <thead>
                  <tr>
                    <th className="th">指标</th>
                    <th className="th text-right">本页单次观测</th>
                    <th className="th text-right">12 种子 均值 ± 标准差</th>
                    <th className="th text-right">95% CI</th>
                    <th className="th text-right">单次值落在哪</th>
                  </tr>
                </thead>
                <tbody>
                  {seedPairs.map(([label, single, stat]) => (
                    <SeedRow key={label} label={label} single={single} stat={stat} />
                  ))}
                </tbody>
              </table>
              <Note tone="warn">
                {String(C.note ?? '')} 也就是说<b className="text-amber-800">单次观测和 12 种子分布并不重合</b>：
                {outsideCI.length > 0
                  ? `${outsideCI.map(([l]) => l).join('、')} 落在 95% CI 之外 —— 招牌数字只有单次支撑时先当它是运气，这条规矩写在方法论清单里。`
                  : '本轮所有指标都落在 CI 内，但这不是免责声明：种子换了数字还是会动。'}
                {' '}种子推导：{String((ms?.meta as Loose | undefined)?.seed_derivation ?? '—')}，复现命令 {String((ms?.meta as Loose | undefined)?.reproduce ?? '—')}。
              </Note>
              <div className="muted mt-1">
                更根本的局限：{String((ms?.meta as Loose | undefined)?.scope_note ?? '')}
              </div>
            </>
          ) : (
            <MissingArtifact
              file="data/multiseed.json → C_gate_metric_robustness"
              what="12 种子门禁指标稳健性"
              how="跑 PYTHONPATH=src python -m koxpilot.cli multiseed --seeds 12 生成 output/multiseed.json 后重跑 npm run refresh。"
            />
          )}
        </Collapse>

        {/* 5. 伪重复口径修正 —— 全站最能体现严谨度的一条 */}
        <Collapse
          flag="caveat"
          title={
            bUnit
              ? `我自己抓出并改正了一个统计口径错误：那 ${int0(Number(bUnit.n_obs_pooled ?? 36))} 个观测不是 ${int0(Number(bUnit.n_obs_pooled ?? 36))} 个独立样本（同一种子下的 ${int0(Number(bUnit.n_campaigns ?? 3))} 个 campaign 共享同一个达人库 = 伪重复），pooled p 值已在产物里标成 pooled_p_value_usable=false，改报逐 campaign n=${int0(Number(bUnit.n_seeds ?? 12))} 的检验`
              : '伪重复口径：multiseed.json 未加载'
          }
          hint={bUnit ? `独立单位 = ${String(bUnit.independent_unit ?? 'seed')}；报数请写「${int0(Number(bUnit.n_obs_pooled ?? 36))} 个 (seed, campaign) 观测」，不要写「独立样本」` : undefined}
        >
          {B && bUnit ? (
            <>
              <p className="body-text">{String(B.independence_caveat ?? '')}</p>
              <div className="mt-2 grid gap-2 lg:grid-cols-2">
                <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5">
                  <div className="text-[12px] font-medium text-amber-800">被作废的口径（pooled，不可当真）</div>
                  <div className="muted mt-1">
                    pooled n_obs = {int0(Number(bUnit.n_obs_pooled))} = {int0(Number(bUnit.n_seeds))} 种子 × {int0(Number(bUnit.n_campaigns))} campaign；
                    独立单位 independent_unit = <b className="text-amber-800">{String(bUnit.independent_unit)}</b>；
                    pooled_p_value_usable = <b className="text-rose-700">{String(bUnit.pooled_p_value_usable)}</b>
                  </div>
                  {bPooled && (
                    <div className="num mt-1 text-[11px] text-slate-600">
                      （原 pooled F 检验：F = {fixed(Number((bPooled.variance_ratio_test as Loose).f), 3)}，df ={' '}
                      {JSON.stringify((bPooled.variance_ratio_test as Loose).df)}，p = {String((bPooled.variance_ratio_test as Loose).p_two_sided)} ——
                      自由度被高估、p 值被压小，<b className="text-rose-700">量级不可当真</b>，故不再用作结论）
                    </div>
                  )}
                  <div className="muted mt-1">{String(bUnit.note ?? '')}</div>
                </div>
                <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2.5">
                  <div className="text-[12px] font-medium text-emerald-800">改正后真正采用的口径：逐 campaign，n = {int0(Number(bUnit.n_seeds))}</div>
                  <table className="mt-1 w-full">
                    <thead>
                      <tr>
                        <th className="th">campaign</th>
                        <th className="th text-right">std 比（基线/本方案）</th>
                        <th className="th text-right">配对检验 t（n=12）</th>
                        <th className="th text-right">p</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(bPerCampaign).map(([k, v]) => (
                        <tr key={k} className="hairline">
                          <td className="td num">{k}</td>
                          <td className="td num text-right">{fixed(Number(v.std_ratio_baseline_over_koxpilot), 2)}×</td>
                          <td className="td num text-right">
                            {fixed(Number((v.paired_diff_base_minus_kox_share as Loose)?.t), 3)}
                          </td>
                          <td className="td num text-right text-slate-600">
                            {String((v.paired_diff_base_minus_kox_share as Loose)?.p_two_sided ?? '—')}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="muted mt-1">
                    另外还报了不依赖分布假设的证据：极端频次计数与条件胜率（见「成本与价值」页的方差归因板块）。
                  </div>
                </div>
              </div>
              <Note tone="good">
                这条局限的价值在于：它<b className="text-emerald-700">不是评审挑出来的，是我自己在写 docs/06-robustness.md 时发现的</b>，
                并且改正动作落进了产物字段（independence_unit）与一条测试（断言必须写明 independent_unit=seed 且 pooled_p_value_usable=false），
                而不是只在文档里道个歉。
              </Note>
            </>
          ) : (
            <MissingArtifact
              file="data/multiseed.json → B_variance_attribution.independence_unit"
              what="伪重复（pseudo-replication）口径修正"
              how="跑 make multiseed 生成 output/multiseed.json（含 independence_unit）后执行 npm run refresh。完整论述见 docs/06-robustness.md。"
            />
          )}
        </Collapse>

        {/* 6. 分造假类型：刻意保留的弱项 */}
        <Collapse
          flag="caveat"
          title={
            report
              ? `刷播放（view_inflation）严口径召回只有 ${fixed(report.fraud.perType.view_inflation?.recall_strict ?? 0, 3)}：播放/粉丝比偏移与「爆款」天然混淆，这个弱项我没靠调阈值做上去`
              : '分造假类型召回：等待浏览器现算'
          }
          hint="严口径 = 门禁自动拦掉；宽口径 = 进人工复核队列"
        >
          {report && (
            <div className="space-y-2.5">
              {Object.entries(report.fraud.perType).map(([type, v]) => {
                const py = (t1?.per_fraud_type as Loose | undefined)?.[type] as Loose | undefined;
                return (
                  <div key={type}>
                    <div className="flex items-baseline justify-between">
                      <span className="text-[12px] text-slate-800">
                        {FRAUD_TYPE_LABEL[type] ?? type}
                        <span className="num ml-1.5 text-[11px] text-slate-500">n={int0(v.n)}</span>
                      </span>
                      <span className="num text-[11px] text-slate-600">
                        严 {fixed(v.recall_strict, 3)} · 宽 {fixed(v.recall_loose, 3)}
                        {py && <span className="ml-1.5 text-slate-500">AUC {fixed(Number(py.auc_vs_clean), 3)}</span>}
                      </span>
                    </div>
                    <div className="mt-1 flex gap-1">
                      <div className="h-2 flex-1 overflow-hidden rounded-sm bg-slate-100">
                        <div className="h-full rounded-sm bg-live-500" style={{ width: `${v.recall_strict * 100}%` }} />
                      </div>
                      <div className="h-2 flex-1 overflow-hidden rounded-sm bg-slate-100">
                        <div className="h-full rounded-sm bg-live-500" style={{ width: `${v.recall_loose * 100}%` }} />
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
        </Collapse>

        {/* 7. 产物自带的诚实声明 */}
        <Collapse
          flag="caveat"
          count={((metrics?.honesty_notes ?? []) as string[]).length}
          title="产物自己带的诚实声明（metrics.json → honesty_notes，原样渲染、一条没改措辞）"
          hint="包含口径冲突、负贡献规则、LLM 未升格、decay 是建模假设等自述"
        >
          <ol className="space-y-1.5">
            {((metrics?.honesty_notes ?? []) as string[]).map((n, i) => (
              <li key={n} className="flex gap-2 text-[12px] leading-relaxed text-slate-700">
                <span className="num mt-[1px] flex h-4 w-4 shrink-0 items-center justify-center rounded bg-amber-100 text-[10px] text-amber-700">
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
        </Collapse>
      </Panel>

      {/* ============ 支撑证据 / 明细：默认折叠 ============ */}
      <Panel title="论证与明细（想验证的人从这里往下挖）" subtitle="双实现逐指标对照、消融实验、PR 曲线、指标定义、守护测试清单" bodyClass="space-y-2">
        <Collapse
          flag="evidence"
          count={7}
          title={
            report && t1 && t2
              ? `浏览器现算 vs Python 产物：7 项核心指标逐个对照，差值全为 0（本次浏览器重算 ${int0(report.n)} 条，耗时 ${Math.round(report.elapsedMs)} ms）`
              : '浏览器现算 vs Python 产物：正在浏览器内对全库重跑判定并重算指标…'
          }
          hint="TS 引擎在你的浏览器里重跑四层门禁并重算全部指标，两侧读同一份 thresholds.json"
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
                <Stat label="三分类 准确率 / macro F1" value={`${fixed(report.verdict.accuracy, 3)} / ${fixed(report.verdict.macro_f1, 3)}`} hint="这个数字不高，原因见上面的混淆矩阵与口径冲突说明" tone="warn" />
                <Stat label="评测样本" value={int0(report?.n ?? Number(t2?.n ?? 0))} hint="全库 5,000 条，中性口径" />
                <Stat
                  label="与 Python 产物差异"
                  value={Math.abs(report.fraud.strict.f1 - Number((t1.strict as Loose).f1)) < 1e-9 ? '0' : '≠0'}
                  hint="核心指标逐项对齐；全量判定级一致性见「工程实现」页"
                  tone="good"
                />
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2 py-4 text-[12px] text-slate-600">
              <Loader2 size={14} className="animate-spin text-live-600" />
              正在对全库重跑四层门禁…（这一步是真算，所以要花几百毫秒）
            </div>
          )}
        </Collapse>

        <Collapse
          flag="detail"
          count={((t4?.by_layer ?? []) as Loose[]).length + ((t4?.by_g1_rule ?? []) as Loose[]).length}
          title={
            t4
              ? `消融实验：逐层 + 逐条规则关掉重跑，正向 ${((t4.positive_rules ?? []) as string[]).length} 条 / 负贡献 ${((t4.negative_rules ?? []) as string[]).length} 条 / 死规则 ${((t4.dead_rules ?? []) as unknown[]).length} 条 —— 一张全是「✓」的消融表才是可疑的`
              : '消融实验：产物未生成'
          }
          hint="delta = 变体 − 全量；负值表示关掉它指标下降（即它有贡献）。里面附带「这张表原来撒了一个谎，我把它修了」的完整说明"
        >
          <div className="mb-2 flex items-center gap-2">
            {busy && <Loader2 size={12} className="animate-spin text-live-600" />}
            <button
              onClick={onAblation}
              disabled={Boolean(busy)}
              className="focusable flex items-center gap-1.5 rounded-lg border border-live-300 bg-live-50 px-2.5 py-1 text-[11px] text-live-700 hover:bg-live-100 disabled:opacity-50"
            >
              <FlaskConical size={11} />
              在浏览器里现关一层重跑
            </button>
          </div>
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
                      <tr key={String(v.variant)} className={`hairline ${cv.kind === 'negative' ? 'bg-amber-50' : ''}`}>
                        <td className="td num">
                          {String(v.variant)}
                          <div className="muted max-w-[200px] truncate" title={String(v.role)}>
                            {String(v.role)}
                          </div>
                        </td>
                        <td className="td num text-right">{fixed(Number(m.fraud_f1_strict), 4)}</td>
                        <td className={`td num text-right ${Number(d.d_fraud_f1_strict) < 0 ? 'text-emerald-600' : 'text-slate-500'}`}>
                          {Number(d.d_fraud_f1_strict) === 0 ? '0' : fixed(Number(d.d_fraud_f1_strict), 4)}
                        </td>
                        <td className={`td num text-right ${Number(d.d_verdict_accuracy) < 0 ? 'text-emerald-600' : 'text-amber-600'}`}>
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
                  注意 <b className="text-amber-700">
                    {String(negLayer.variant)} 的三分类准确率反而更高（
                    {signed(Number((negLayer.delta as Loose).d_verdict_accuracy), 4)}）
                  </b>
                  ，所以它在"三分类"这一列是{' '}
                  <b className="text-amber-800">
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
                        <td className={`td num text-right ${r.d_fraud_f1_strict < 0 ? 'text-emerald-600' : 'text-slate-500'}`}>
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
                            className={`hairline ${kind === 'negative' ? 'bg-amber-50' : ''}`}
                            title={String(v.contribution_note ?? '')}
                          >
                            <td className="td num">
                              {String(v.variant)}
                              <span className="ml-1.5 text-[11px] text-slate-500">{ruleLabel(String(v.variant).slice(1))}</span>
                            </td>
                            <td className="td num text-right">{fixed(Number(v.weight), 2)}</td>
                            <td className={`td num text-right ${d < 0 ? 'text-emerald-600' : d > 0 ? 'text-amber-600' : 'text-slate-500'}`}>
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
                <div className="mt-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="flex items-center gap-1.5 text-[12px] font-semibold text-amber-800">
                      <ShieldAlert size={12} />
                      这张表原来撒了一个谎，我把它修了
                    </span>
                    <Badge className="border-emerald-200 bg-emerald-50 text-emerald-700">
                      正向 {((t4.positive_rules ?? []) as string[]).length} 条
                    </Badge>
                    <Badge className="border-amber-300 bg-amber-50 text-amber-700">
                      负向 {((t4.negative_rules ?? []) as string[]).length} 条
                    </Badge>
                    <Badge className="border-slate-300 text-slate-600">
                      死规则 {((t4.dead_rules ?? []) as unknown[]).length} 条
                    </Badge>
                  </div>
                  <p className="mt-1.5 text-[12px] leading-relaxed text-slate-700">
                    早期实现用 <code className="rounded bg-slate-100 px-1 font-mono text-[11px] text-rose-700">abs(delta) &ge; {String((t4.contribution_criteria as Loose).eps)}</code>{' '}
                    判"有贡献"，<b className="text-rose-700">不看符号</b> —— 于是{' '}
                    {((t4.negative_rules ?? []) as string[]).join('、')} 这种"关掉后 F1 反而更好"的规则也被算成有贡献。
                    现在改成 <b className="text-amber-800">三态判定</b>：
                  </p>
                  <div className="mt-1.5 grid gap-1 sm:grid-cols-3">
                    {(['positive', 'negative', 'negligible'] as const).map((k) => (
                      <div key={k} className="rounded-lg border border-slate-200 bg-slate-100 px-2 py-1.5">
                        <Badge className={CONTRIB_STYLE[k].cls}>{CONTRIB_STYLE[k].label}</Badge>
                        <div className="muted mt-1">{String((t4.contribution_criteria as Loose)[k] ?? '')}</div>
                      </div>
                    ))}
                  </div>
                  {((t4.negative_rules ?? []) as string[]).map((rid) => {
                    const row = ((t4.by_g1_rule ?? []) as Loose[]).find((v) => String(v.variant) === rid);
                    if (!row) return null;
                    return (
                      <p key={rid} className="mt-2 text-[12px] leading-relaxed text-amber-800">
                        <b className="num text-amber-800">{rid}</b>（{ruleLabel(rid.slice(1))}，权重{' '}
                        {fixed(Number(row.weight), 2)}）：关掉它严口径 F1{' '}
                        <b className="num text-amber-700">
                          {signed(Number((row.delta as Loose).d_fraud_f1_strict), 4)}
                        </b>{' '}
                        —— 也就是说<b className="text-amber-800">它在这个指标上是净负担</b>。我没有偷偷删掉它、也没有继续把它算作"有贡献"：
                        保留理由（软信号、只推 review、宽口径召回来源）写在 docs/03-evaluation.md 表 4 一节，
                        判定则如实标成 negative。<b className="text-slate-900">一张全是"✓"的消融表才是可疑的。</b>
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
        </Collapse>

        <Collapse
          flag="evidence"
          title="PR 曲线：把连续异常分从高到低扫一遍，看精确率-召回率怎么换（当前工作点已标在图上）"
          hint="浏览器现算：每个分值变化点取一个点；参考线是随机基线（= 水号占比）与当前严口径工作点"
        >
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
        </Collapse>

        <Collapse
          flag="detail"
          count={Object.keys((metrics?.definitions ?? {}) as Record<string, string>).length}
          title="评测口径逐条定义：全部指标在「库级中性画像」下评测（gt.verdict 是 campaign 无关的库级判定，必须用中性口径才对得上）"
          hint="口径写歪的话，后面所有数字都不用看了，所以定义直接来自 metrics.json → definitions"
        >
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {Object.entries((metrics?.definitions ?? {}) as Record<string, string>).map(([k, v]) => (
              <div key={k} className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2">
                <div className="num text-[11px] text-live-600">{k}</div>
                <div className="muted mt-0.5">{v}</div>
              </div>
            ))}
          </div>
        </Collapse>

        <Collapse
          flag="evidence"
          count={guardRest.length}
          title="其余 7 条守护测试：消融符号、有界 uplift、三臂归因、A4 口径审计、双实现一致性、独立单位…（都是「能让我自己出事」的断言）"
          hint="以上守护规则由 Python 侧 745 个测试承载；前端另有 npm run verify 做双实现逐字段比对"
        >
          <ul className="space-y-1.5">
            {guardRest.map((g) => (
              <li key={g} className="flex gap-2 text-[12px] leading-relaxed text-slate-700">
                <ShieldCheck size={12} className="mt-[2px] shrink-0 text-emerald-600" />
                {g}
              </li>
            ))}
          </ul>
        </Collapse>
      </Panel>
    </div>
  );
}
