import React from 'react';
import { ChevronRight } from 'lucide-react';
import { DonutRing, Funnel } from '../components/charts';
import { Badge, Hint } from '../components/ui';
import { PLATFORM_LABEL } from '../engine/taxonomy';
import { explain, type BriefField, type CandidateRow, type ExplainPayload, type PlanPayload, type TimingRow } from '../lib/api';
import type { ParityReport } from '../lib/parity';
import { VERDICT_COLOR, VERDICT_HEX, VERDICT_LABEL, compact, fixed, int0, ms, pct1, usd0 } from '../lib/format';

/**
 * Python 服务返回结果的渲染块。
 *
 * 与浏览器引擎那一套渲染并存：服务可达时用这些块，服务未连接时用原有的浏览器引擎块，
 * 两条通道展示的字段口径保持一致（同一套阈值、同一套门禁、同一套分配器）。
 */

const STATUS_STYLE: Record<string, { label: string; cls: string }> = {
  hit: { label: '原文命中', cls: 'border-live-200 bg-live-50 text-live-700' },
  derived: { label: '规则推导', cls: 'border-indigo-200 bg-indigo-50 text-indigo-700' },
  default: { label: '未识别·用默认值', cls: 'border-amber-300 bg-amber-50 text-amber-700' },
};

function statusStyle(status: string): { label: string; cls: string } {
  return STATUS_STYLE[status] ?? { label: status, cls: 'border-slate-300 bg-white text-slate-700' };
}

function truncate(text: string, n: number): string {
  return text.length > n ? `${text.slice(0, n)}…` : text;
}

/** brief 字段的解析证据（服务侧 `brief.fields`）。 */
export function ServiceFields({ fields }: { fields: BriefField[] }): React.ReactElement {
  return (
    <table className="w-full">
      <thead>
        <tr>
          <th className="th">字段</th>
          <th className="th">来源</th>
          <th className="th">命中原文片段</th>
          <th className="th">识别方式</th>
          <th className="th">解析值</th>
        </tr>
      </thead>
      <tbody>
        {fields.map((f, i) => {
          const s = statusStyle(f.status);
          return (
            <tr key={`${f.key}-${i}`} className="hairline">
              <td className="td text-slate-700">{f.label}</td>
              <td className="td">
                <Badge className={s.cls}>{s.label}</Badge>
              </td>
              <td className="td text-slate-700">
                {f.matched ? <span className="rounded bg-live-50 px-1 py-0.5">{f.matched}</span> : <span className="text-slate-500">—</span>}
              </td>
              <td className="td">
                <Hint text={f.how}>
                  <span className="text-[11.5px] text-slate-600 underline decoration-dotted">{truncate(f.how, 22)}</span>
                </Hint>
              </td>
              <td className="td num text-slate-800">{f.display}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** 服务侧解析出的投放需求（芯片形式，取 `brief.fields` 的展示值）。 */
export function ServiceSpecChips({ fields }: { fields: BriefField[] }): React.ReactElement {
  return (
    <div className="flex flex-wrap gap-1">
      {fields
        .filter((f) => f.display && f.display !== '—')
        .map((f, i) => (
          <Badge
            key={`${f.key}-${i}`}
            className={f.status === 'default' ? 'border-slate-300 bg-white text-slate-600' : 'border-live-200 bg-live-50 text-live-700'}
          >
            {f.label} {f.display}
          </Badge>
        ))}
    </div>
  );
}

/** 各 Agent 的服务侧耗时。 */
export function ServiceStages({ timings }: { timings: TimingRow[] }): React.ReactElement {
  return (
    <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
      {timings.map((t, i) => (
        <div key={`${t.agent}-${i}`} className="card px-3 py-1.5">
          <div className="flex items-center gap-1.5">
            <span className="num text-[11px] font-semibold text-slate-900">{t.agent}</span>
            <span className="num ml-auto shrink-0 text-[11px] text-slate-600">{ms(t.ms)}</span>
          </div>
          <div className="mt-1 truncate text-[12px] font-medium text-live-700" title={t.label}>
            {t.label}
          </div>
        </div>
      ))}
    </div>
  );
}

/** 漏斗 + 判定分布 + 预算分配摘要。 */
export function ServiceSummary({ data }: { data: PlanPayload }): React.ReactElement {
  const counts = { pass: 0, review: 0, reject: 0 } as Record<string, number>;
  for (const v of data.parity_payload.verdicts) counts[v.verdict] = (counts[v.verdict] ?? 0) + 1;
  const total = data.parity_payload.verdicts.length;
  const a = data.allocation;
  const other = a.arms.find((x) => x.arm !== 'koxpilot') ?? null;

  return (
    <div className="grid gap-3 lg:grid-cols-[1fr_0.7fr_1.05fr]">
      <Funnel
        steps={data.funnel.map((f) => ({
          key: f.stage,
          label: f.label,
          value: f.count,
          // 召回这一级把「参与计算的人数」摊开说：这次的方案是在多少人里解出来的，
          // 决定了预算能不能花出去，也是与离线 CLI 对数的前提。
          note:
            f.stage === 'recall' && data.scope
              ? `${f.label}：${int0(f.count)} 人；其中进入计算 ${int0(data.scope.computed_on)} 人，返回明细 ${int0(
                  data.scope.detail_rows,
                )} 条`
              : `${f.label}：${int0(f.count)} 人`,
        }))}
      />
      <DonutRing
        segments={(['pass', 'review', 'reject'] as const).map((v) => ({
          key: v,
          label: VERDICT_LABEL[v],
          value: counts[v] ?? 0,
          color: VERDICT_HEX[v],
        }))}
        size={96}
        center={int0(total)}
        sub="候选人数"
      />
      <div className="grid grid-cols-2 gap-1.5">
        <div className="rounded-lg border border-live-200 bg-live-50 px-2 py-1">
          <div className="text-[11px] text-slate-600">已分配 / 预算</div>
          <div className="num text-[13px] text-live-700">
            {usd0(a.allocated_usd)} <span className="text-[11px] text-slate-600">/ {usd0(a.budget_usd)}</span>
          </div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-1">
          <div className="text-[11px] text-slate-600">进入清单</div>
          <div className="num text-[13px] text-slate-900">{int0(a.picked)} 人</div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-1">
          <Hint text={a.unallocated_why}>
            <div className="text-[11px] text-slate-600 underline decoration-dotted">未分配</div>
          </Hint>
          <div className="num text-[13px] text-slate-900">{usd0(a.unallocated_usd)}</div>
        </div>
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-2 py-1">
          <Hint text={other ? `对照臂「${other.label}」花费 ${usd0(other.spend)}` : '对照臂未返回'}>
            <div className="text-[11px] text-slate-600 underline decoration-dotted">少浪费</div>
          </Hint>
          <div className="num text-[13px] text-emerald-700">
            {usd0(a.saved_usd)} <span className="text-[11px] text-slate-600">{pct1(a.saved_share)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

/** 实时一致性比对结论：一行给数字，不配旁白。 */
export function ParityLine({ parity, note }: { parity: ParityReport; note?: React.ReactNode }): React.ReactElement {
  const ok = parity.diff === 0;
  return (
    <div
      className={`flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border px-3 py-1.5 text-[12.5px] ${
        ok ? 'border-emerald-300 bg-emerald-50 text-emerald-800' : 'border-amber-300 bg-amber-50 text-amber-800'
      }`}
    >
      <span className="font-medium">
        {ok ? (
          <>
            Python 服务与浏览器引擎：{int0(parity.compared)} 条逐条一致，差异 0 条
          </>
        ) : (
          <>
            Python 服务与浏览器引擎：比对 {int0(parity.compared)} 条，差异 {int0(parity.diff)} 条
          </>
        )}
      </span>
      {!ok && (
        <span className="num text-[12px]">
          {parity.diffRows.map((d) => `${d.kox_id}（服务 ${VERDICT_LABEL[d.service] ?? d.service} / 浏览器 ${VERDICT_LABEL[d.browser] ?? d.browser}）`).join('，')}
          {parity.diff > parity.diffRows.length ? ` 等 ${int0(parity.diff)} 条` : ''}
        </span>
      )}
      {note && <span className="num text-[12px] text-slate-600">{note}</span>}
      <span className="num ml-auto text-[12px] text-slate-600">
        服务 {parity.serviceMs === null ? '—' : ms(parity.serviceMs)} · 浏览器 {ms(parity.browserMs)}
        {(parity.onlyService > 0 || parity.onlyBrowser > 0) && (
          <span className="ml-2 text-slate-600">
            仅一侧召回 {int0(parity.onlyService + parity.onlyBrowser)} 条
          </span>
        )}
      </span>
    </div>
  );
}

/**
 * 展开某一行时按需拉 `/api/kox/{kox_id}/explain`，把该达人的逐条信号、阈值与门禁结论补齐。
 * 结果按 kox_id 缓存；取不到就只显示 plan 已经给出的理由，不影响这一行的可读性。
 */
function useExplain(koxId: string | null): { data: ExplainPayload | null; loading: boolean; failed: string | null } {
  const cache = React.useRef(new Map<string, ExplainPayload>());
  const [state, setState] = React.useState<{ data: ExplainPayload | null; loading: boolean; failed: string | null }>({
    data: null,
    loading: false,
    failed: null,
  });
  React.useEffect(() => {
    if (!koxId) {
      setState({ data: null, loading: false, failed: null });
      return;
    }
    const hit = cache.current.get(koxId);
    if (hit) {
      setState({ data: hit, loading: false, failed: null });
      return;
    }
    let alive = true;
    setState({ data: null, loading: true, failed: null });
    void explain(koxId).then((res) => {
      if (!alive) return;
      if (res.ok) {
        cache.current.set(koxId, res.data);
        setState({ data: res.data, loading: false, failed: null });
      } else {
        setState({ data: null, loading: false, failed: res.error.message });
      }
    });
    return () => {
      alive = false;
    };
  }, [koxId]);
  return state;
}

/** 单个达人的服务侧信号明细（信号 / 实际值 / 阈值 / 结论）。 */
function ExplainSignals({ koxId }: { koxId: string }): React.ReactElement {
  const { data, loading, failed } = useExplain(koxId);
  if (loading) return <div className="muted mt-1.5">读取该达人的逐条信号…</div>;
  if (failed || !data) return <div className="muted mt-1.5">该达人的信号明细本次未取到：{failed ?? '无响应'}</div>;
  return (
    <div className="mt-1.5 flex flex-wrap gap-1.5">
      {data.signals.map((sig, i) => (
        <span
          key={`${sig.label}-${i}`}
          className={`num rounded border px-1.5 py-0.5 text-[11px] ${
            sig.verdict === 'in_range'
              ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
              : 'border-amber-300 bg-amber-50 text-amber-800'
          }`}
        >
          {sig.label} {fixed(sig.value, 3)}
          {sig.threshold !== null && <span className="text-slate-600"> / 阈值 {fixed(sig.threshold, 3)}</span>}
        </span>
      ))}
      {data.signals.length === 0 && <span className="muted">该达人无可展示的数值信号</span>}
    </div>
  );
}

/** 服务返回的候选清单（默认 Top N，逐条带证据与淘汰理由）。 */
export function ServiceCandidates({
  candidates,
  topN = 12,
}: {
  candidates: CandidateRow[];
  topN?: number;
}): React.ReactElement {
  const [all, setAll] = React.useState(false);
  const [openId, setOpenId] = React.useState<string | null>(null);
  const shown = all ? candidates : candidates.slice(0, topN);
  return (
    <div>
      <div className="max-h-[420px] overflow-auto">
        <table className="w-full border-collapse">
          <thead className="sticky top-0 z-10 bg-white">
            <tr className="hairline">
              <th className="th">达人</th>
              <th className="th">平台 / 市场</th>
              <th className="th text-right">粉丝</th>
              <th className="th text-right">适配</th>
              <th className="th">判定</th>
              <th className="th">门禁命中</th>
              <th className="th text-right">分配</th>
              <th className="th" />
            </tr>
          </thead>
          <tbody>
            {shown.map((c) => (
              <React.Fragment key={c.kox_id}>
                <tr
                  onClick={() => setOpenId((v) => (v === c.kox_id ? null : c.kox_id))}
                  className="hairline cursor-pointer transition-colors hover:bg-live-50"
                >
                  <td className="td">
                    <div className="font-medium text-slate-800">{c.handle}</div>
                    <div className="num text-[10px] text-slate-500">{c.kox_id}</div>
                  </td>
                  <td className="td text-[12px] text-slate-600">
                    {PLATFORM_LABEL[c.platform] ?? c.platform}
                    <span className="mx-1 text-slate-500">/</span>
                    {c.market}
                  </td>
                  <td className="td num text-right">{compact(c.followers)}</td>
                  <td className="td num text-right">
                    {fixed(c.fit_score, 2)}
                    <span className="ml-1 text-[10px] text-slate-500">
                      {c.fit_source === 'cached_llm' ? '模型' : '规则'}
                    </span>
                  </td>
                  <td className="td">
                    <Badge className={VERDICT_COLOR[c.verdict] ?? 'border-slate-300 text-slate-700'}>
                      {VERDICT_LABEL[c.verdict] ?? c.verdict}
                    </Badge>
                  </td>
                  <td className="td">
                    <div className="flex flex-wrap gap-1">
                      {c.gate_hits.length === 0 && <span className="text-[11px] text-emerald-600">无</span>}
                      {[...new Set(c.gate_hits.map((h) => `${h.gate} ${h.gate_label}`))].slice(0, 3).map((label) => (
                        <span key={label} className="rounded border border-slate-200 bg-slate-50 px-1 text-[10px] text-slate-600">
                          {label}
                        </span>
                      ))}
                      {c.gate_hits.length > 3 && <span className="text-[10px] text-slate-500">+{c.gate_hits.length - 3}</span>}
                    </div>
                  </td>
                  <td className="td num text-right">
                    {c.allocated_usd > 0 ? (
                      <span className="text-live-700">{usd0(c.allocated_usd)}</span>
                    ) : (
                      <span className="text-slate-500">—</span>
                    )}
                  </td>
                  <td className="td text-right">
                    <ChevronRight
                      size={13}
                      className={`text-slate-500 transition-transform ${openId === c.kox_id ? 'rotate-90' : ''}`}
                    />
                  </td>
                </tr>
                {openId === c.kox_id && (
                  <tr className="hairline bg-slate-50">
                    <td className="td" colSpan={8}>
                      <div className="text-[12.5px] leading-relaxed text-slate-700">{c.reason_human}</div>
                      {c.gate_hits.length > 0 && (
                        <ul className="mt-1.5 space-y-0.5">
                          {c.gate_hits.map((h, i) => (
                            <li key={`${h.gate}-${i}`} className="flex gap-1.5 text-[12px] leading-relaxed text-slate-600">
                              <span className="mt-[6px] inline-block h-1 w-1 shrink-0 rounded-full bg-slate-400" />
                              <span>
                                <b className="text-slate-700">
                                  {h.gate} {h.gate_label} · {h.signal_label}
                                </b>
                                ：{h.detail}
                              </span>
                            </li>
                          ))}
                        </ul>
                      )}
                      <ExplainSignals koxId={c.kox_id} />
                      {c.expected_reach > 0 && (
                        <div className="muted mt-1">预估有效曝光 {compact(c.expected_reach)}</div>
                      )}
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {candidates.length > topN && (
        <div className="mt-2 flex justify-center">
          <button
            onClick={() => setAll((v) => !v)}
            className="focusable rounded-lg border border-slate-300 px-3 py-1 text-[11px] text-slate-700 hover:bg-slate-50"
          >
            {all ? `收起（只看前 ${topN} 条）` : `展开全部 ${candidates.length} 条`}
          </button>
        </div>
      )}
    </div>
  );
}
