import React from 'react';
import { RankStrip } from './charts';
import { Badge, KV, Note, Panel } from './ui';
import { QUANTILE_SIGNALS } from '../engine/policy';
import type { GateResult, Kox, Reason } from '../engine/types';
import { CATEGORY_ZH, PLATFORM_LABEL } from '../engine/taxonomy';
import { ruleLabel } from '../lib/pipeline';
import { BUCKET_LABEL, VERDICT_COLOR, VERDICT_LABEL, fixed, int0, pct1, usd0 } from '../lib/format';

const SEVERITY_LABEL: Record<string, string> = { hard: '硬信号', soft: '软信号', block: '硬阻断' };
const SEVERITY_CLS: Record<string, string> = {
  hard: 'border-rose-200 bg-rose-50 text-rose-700',
  soft: 'border-amber-200 bg-amber-50 text-amber-700',
  block: 'border-rose-300 bg-rose-100 text-rose-800',
};

const GATE_TITLE: Record<string, string> = {
  G0: 'G0 数据完整性',
  G1: 'G1 真实性（水号信号）',
  G2: 'G2 一致性（人货场匹配）',
  G3: 'G3 品牌安全',
};

function directionOf(signal: string): 'upper' | 'lower' | 'abs' {
  if (signal.startsWith('comment_dup') || signal.startsWith('comment_emoji')) return 'upper';
  if (signal === 'comment_like_ratio' || signal === 'view_follower_ratio') return 'abs';
  return 'upper';
}

function ReasonRow({ r, rank }: { r: Reason; rank: number | null }): React.ReactElement {
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50 p-2.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge className="border-live-200 bg-live-50 font-mono text-live-700">{r.rule_id}</Badge>
        <Badge className={SEVERITY_CLS[r.severity] ?? 'border-slate-300 text-slate-700'}>
          {SEVERITY_LABEL[r.severity] ?? r.severity}
        </Badge>
        <span className="text-[12px] text-slate-800">{ruleLabel(r.rule_id)}</span>
        <span className="num ml-auto text-[10px] text-slate-500">
          权重 {fixed(r.weight, 2)} · 越界深度 {fixed(r.depth, 3)}
        </span>
      </div>
      <p className="mt-1.5 text-[12px] leading-relaxed text-slate-700">{r.human_text}</p>
      <div className="mt-2">
        {QUANTILE_SIGNALS.has(r.signal) ? (
          <RankStrip
            rank={rank}
            actual={r.actual}
            threshold={r.threshold}
            signal={r.signal}
            direction={directionOf(r.signal)}
          />
        ) : (
          <div className="flex items-baseline justify-between text-[10px]">
            <span className="text-slate-500">{r.signal}</span>
            <span className="num text-slate-700">
              实际 {typeof r.actual === 'number' ? fixed(r.actual, 4) : (r.actual ?? '—')}
              <span className="mx-1 text-slate-500">vs</span>
              阈值 {typeof r.threshold === 'number' ? fixed(r.threshold, 4) : (r.threshold ?? '—')}
            </span>
          </div>
        )}
      </div>
      <div className="muted mt-1.5">阈值来源：{r.source}</div>
    </div>
  );
}

export function EvidenceBody({
  kox,
  result,
  allocation,
}: {
  kox: Partial<Kox>;
  result: GateResult;
  allocation?: { amount_usd: number; posts: number; picked_by: string; value_score: number; efficiency: number } | null;
}): React.ReactElement {
  const byGate = new Map<string, Reason[]>();
  for (const r of result.reasons) byGate.set(r.gate, [...(byGate.get(r.gate) ?? []), r]);
  const gt = kox.gt;
  const scoreRows: Array<[string, number, string]> = [
    ['完整性 completeness', result.completeness, 'G0：关键字段齐不齐'],
    ['真实性 authenticity', result.authenticity_score, 'G1：1 − 加权异常信号'],
    ['一致性 consistency', result.consistency_score, 'G2：人货场匹配度'],
    ['品牌安全 brand_safety', result.brand_safety_score, 'G3：内容与合作风险'],
    ['连续异常分 fraud_score', result.fraud_score, '按同组分位数算的连续分（AUC 用它，与离散判定无关）'],
    ['语义适配 fit_score', result.fit_score, `来源：${result.fit_source}`],
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge className={VERDICT_COLOR[result.verdict]}>判定 {VERDICT_LABEL[result.verdict]}</Badge>
        {result.blocked_by && <Badge className="border-rose-300 bg-rose-100 text-rose-800">硬阻断 {result.blocked_by}</Badge>}
        <Badge className="border-slate-300 text-slate-700">{PLATFORM_LABEL[String(kox.platform)] ?? kox.platform}</Badge>
        <Badge className="border-slate-300 text-slate-700">{kox.country}</Badge>
        <Badge className="border-slate-300 text-slate-700">{BUCKET_LABEL[result.group_key.split('|')[1]] ?? result.group_key}</Badge>
        <Badge className="border-slate-300 font-mono text-slate-600">同组 {result.group_key}</Badge>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {scoreRows.map(([label, v, hint]) => (
          <div key={label} className="card px-2.5 py-2" title={hint}>
            <div className="text-[10px] text-slate-500">{label}</div>
            <div className="num mt-0.5 text-[15px] text-slate-900">{fixed(v, 4)}</div>
            <div className="mt-1 h-1 overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-full rounded-full"
                style={{ width: `${Math.max(0, Math.min(1, v)) * 100}%`, background: '#22d3ee' }}
              />
            </div>
          </div>
        ))}
      </div>

      {allocation && (
        <Panel title="本次预算里分到了什么" tone="accent">
          <div className="grid grid-cols-2 gap-x-4 sm:grid-cols-4">
            <KV k="分配金额" v={usd0(allocation.amount_usd)} />
            <KV k="内容条数" v={`${allocation.posts} 条`} />
            <KV k="入选方式" v={allocation.picked_by} />
            <KV k="边际性价比" v={fixed(allocation.efficiency, 2)} />
          </div>
        </Panel>
      )}

      <div>
        <h4 className="section-title mb-2">
          证据链 · 共 {result.reasons.length} 条规则命中
          {result.reasons.length === 0 && <span className="ml-2 text-[11px] font-normal text-emerald-600">四层全部无命中</span>}
        </h4>
        <div className="space-y-3">
          {['G0', 'G1', 'G2', 'G3'].map((gate) => {
            const rows = byGate.get(gate) ?? [];
            return (
              <div key={gate}>
                <div className="mb-1.5 flex items-center gap-2">
                  <span className="text-[11px] font-medium text-slate-700">{GATE_TITLE[gate]}</span>
                  <span className={`num text-[10px] ${rows.length ? 'text-rose-600' : 'text-emerald-600'}`}>
                    {rows.length ? `${rows.length} 条命中` : '无命中'}
                  </span>
                </div>
                {rows.length > 0 && (
                  <div className="space-y-2">
                    {rows.map((r) => (
                      <ReasonRow key={`${r.rule_id}-${r.signal}`} r={r} rank={result.signal_ranks[r.signal] ?? null} />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {result.missing_fields && result.missing_fields.length > 0 && (
        <Note tone="warn">缺失关键字段：{result.missing_fields.join('、')}（G0 会据此降低置信度）</Note>
      )}

      <Panel title="原始事实（引擎读到的输入）" subtitle="下面这些字段就是判定的全部输入，gt 块在入口已被物理剥离">
        <div className="grid gap-x-6 sm:grid-cols-2">
          <KV k="handle" v={kox.handle ?? '—'} />
          <KV k="粉丝数" v={int0(kox.followers)} />
          <KV k="平均播放" v={int0(kox.avg_views)} />
          <KV k="互动率" v={fixed(kox.engagement_rate ?? null, 4)} />
          <KV k="播放/粉丝" v={fixed(kox.view_follower_ratio ?? null, 4)} />
          <KV k="评论/点赞" v={fixed(kox.comment_like_ratio ?? null, 4)} />
          <KV k="评论重复率" v={fixed(kox.comment_dup_rate ?? null, 4)} />
          <KV k="纯 emoji 评论率" v={fixed(kox.comment_emoji_only_rate ?? null, 4)} />
          <KV k="账号天数" v={int0(kox.account_age_days)} />
          <KV k="报价" v={kox.quoted_price_usd ? usd0(kox.quoted_price_usd) : `缺失（同组 CPM ${fixed(kox.avg_cpm_usd ?? null, 2)} 估算）`} />
          <KV
            k="自称品类"
            v={(kox.declared_categories ?? []).map((c) => CATEGORY_ZH[c] ?? c).join('、') || '—'}
            mono={false}
          />
          <KV
            k="观测品类"
            v={(kox.observed_categories ?? []).map((c) => CATEGORY_ZH[c] ?? c).join('、') || '—'}
            mono={false}
          />
        </div>
        {kox.audience_geo && (
          <div className="mt-2">
            <div className="text-[10px] text-slate-500">受众地域</div>
            <div className="mt-1 flex flex-wrap gap-1">
              {Object.entries(kox.audience_geo)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 6)
                .map(([c, v]) => (
                  <Badge key={c} className="border-slate-300 text-slate-700">
                    {c} {pct1(v, 0)}
                  </Badge>
                ))}
            </div>
          </div>
        )}
        {(kox.content_flags ?? []).length > 0 && (
          <div className="mt-2">
            <div className="text-[10px] text-slate-500">内容风险标记</div>
            <div className="mt-1 flex flex-wrap gap-1">
              {(kox.content_flags ?? []).map((f) => (
                <Badge key={`${f.type}-${f.severity}`} className="border-rose-200 bg-rose-50 text-rose-700">
                  {f.type} · {f.severity} × {f.hits ?? 1}
                </Badge>
              ))}
            </div>
          </div>
        )}
        {(kox.past_collabs ?? []).length > 0 && (
          <div className="mt-2">
            <div className="text-[10px] text-slate-500">历史合作</div>
            <div className="mt-1 flex flex-wrap gap-1">
              {(kox.past_collabs ?? []).map((c) => (
                <Badge key={`${c.brand}-${c.months_ago}`} className="border-slate-300 text-slate-700">
                  {c.brand} · {c.months_ago} 个月前
                </Badge>
              ))}
            </div>
          </div>
        )}
      </Panel>

      {gt && (
        <Panel
          title="ground truth（仅评测/审计可见）"
          subtitle="判定过程读不到这里的任何字段；放在抽屉底部只为让你核对上面的结论对不对"
          tone="warn"
        >
          <div className="grid gap-x-6 sm:grid-cols-2">
            <KV k="是否水号" v={gt.is_fraud ? `是（${gt.fraud_type ?? '未标注类型'}）` : '否'} />
            <KV k="标签错配" v={gt.tag_mismatch ? '是' : '否'} />
            <KV k="品牌安全" v={gt.brand_safety} />
            <KV k="库级 gt 判定" v={VERDICT_LABEL[gt.verdict] ?? gt.verdict} />
          </div>
          <Note tone="warn">
            {gt.verdict === result.verdict
              ? '本条与 gt 判定一致。'
              : `本条与 gt 不一致：gt=${VERDICT_LABEL[gt.verdict]}，门禁给出 ${VERDICT_LABEL[result.verdict]}。整体错判结构见「评测」页混淆矩阵，不在这里挑好看的样本。`}
          </Note>
        </Panel>
      )}
    </div>
  );
}
