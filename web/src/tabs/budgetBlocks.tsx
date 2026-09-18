import React from 'react';
import { Badge, Note } from '../components/ui';
import type { AdviceRow, AllocationArm, AllocationBlock, JudgeBlock } from '../lib/api';
import { compact, fixed, int0, pct1, usd0 } from '../lib/format';
import {
  effectiveViews,
  enginePerDollar,
  leadingArm,
  lenientPerDollar,
  contractCount,
  tradeoffLine,
  utilizationOf,
  valuePerDollar,
} from '../lib/planView';

/**
 * 预算结论块：**三条臂的可比对照**与**预算没花完时该怎么办**。
 *
 * 这两块在 Python 服务可达与未连接两种情况下渲染的都是同一套字段
 * （契约 §4 的 `allocation.arms[]` 与 `advice[]`），所以放在这里共用，
 * 而不是各写一份——各写一份就会出现「服务态和降级态给的结论不一样」。
 */

/* ------------------------------------------------------------------ */
/* 三条臂对照                                                          */
/* ------------------------------------------------------------------ */

/**
 * 三条臂的对照表。
 *
 * 判优口径：**每美元买到多少「按标注仍然算数」的有效曝光**，加上**花在水号 / 高风险号的钱**。
 * 三条臂的花费能差几十倍（门禁只买得起过关的那几个人），把绝对曝光并排摆出来会把
 * "花得少"读成"做得差"，所以花费与绝对曝光只作同一行里的次级信息。
 *
 * 两个假设都摆在表里：主口径把标注为水号的曝光按 0 计，宽松口径按 50% 计。
 * 表下一行如实写出两个口径的最高项是否为同一条——不同就说不同，这决定结论稳不稳。
 * 引擎自己的事前估分收进折叠区，并写明它不参与本表判优。
 */
export function ArmsCompare({
  arms,
  judge,
}: {
  arms: AllocationArm[];
  judge?: JudgeBlock;
}): React.ReactElement | null {
  if (arms.length === 0) return null;
  const best = Math.max(...arms.map((a) => valuePerDollar(a) ?? 0), 0);
  const mainLead = leadingArm(arms, valuePerDollar);
  const lenientLead = leadingArm(arms, lenientPerDollar);
  const bestLenient = lenientLead ? lenientPerDollar(lenientLead) : null;
  const spends = arms.map((a) => usd0(a.spend)).join(' / ');
  const engineArms = arms.filter((a) => enginePerDollar(a) !== null);
  // 老版本服务没有 n_selected：整列直接不出，不显示 undefined、也不拿 0 顶上
  const showContracts = arms.some((a) => contractCount(a) !== null);
  // 合约数越少越省事，这一列的"最少"用中性色标出，避免被读成战绩
  const fewest = showContracts
    ? Math.min(...arms.map((a) => contractCount(a)).filter((n): n is number => n !== null))
    : null;
  const tradeoff = tradeoffLine(arms);
  // 标题里的条数按实际行数写：这张表在两臂视角与三臂视角都用，写死"三种"会和眼前的行数打架
  const nWord = ['零', '一', '两', '三', '四'][arms.length] ?? String(arms.length);
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-3 py-2">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[12.5px] font-medium text-slate-800">{nWord}种选人方式，同一份候选与报价</span>
        <span className="text-[11.5px] text-slate-600">
          花费（{spends}）不可比，故按每美元有效曝光判优；主口径水号曝光按 0 计，宽松口径按 50% 计
        </span>
      </div>
      <table className="mt-1.5 w-full">
        <thead>
          <tr>
            <th className="th">选人方式</th>
            <th className="th text-right">每美元有效曝光</th>
            <th className="th text-right">宽松口径</th>
            <th className="th text-right">花在水号 / 高风险号的钱</th>
            {showContracts ? (
              <th className="th text-right" title="这条臂实际拿到钱的达人数：报价、寄样、审稿、发布确认、结算都要逐人做，这笔成本不在判优指标里">
                选中人数（要签的合约数）
              </th>
            ) : null}
            <th className="th text-right">本次花费</th>
            <th className="th text-right">有效曝光合计</th>
          </tr>
        </thead>
        <tbody>
          {arms.map((a) => {
            const vpd = valuePerDollar(a);
            const share = best > 0 && vpd !== null ? Math.max(0.04, vpd / best) : 0;
            const primary = a.arm === 'koxpilot';
            // 高亮给"每美元买得最多"的那一条，而不是固定给我们自己那一条：
            // 这张表要能读出"这次谁更划算"，包含读出我们更贵的那次。
            const leads = vpd !== null && best > 0 && vpd >= best;
            const lenient = lenientPerDollar(a);
            const lenientLeads = lenient !== null && bestLenient !== null && lenient >= bestLenient;
            const waste = a.waste_usd ?? 0;
            return (
              <tr key={a.arm} className="hairline">
                <td className="td">
                  <span className={primary ? 'font-medium text-slate-900' : 'text-slate-700'}>{a.label}</span>
                </td>
                <td className="td">
                  <div className="flex items-center justify-end gap-2">
                    <div className="h-1.5 w-16 overflow-hidden rounded bg-slate-100">
                      <div
                        className={`h-full rounded ${leads ? 'bg-live-500' : 'bg-slate-400'}`}
                        style={{ width: `${share * 100}%` }}
                      />
                    </div>
                    <span className={`num text-[13px] ${leads ? 'text-live-700' : 'text-slate-700'}`}>
                      {vpd === null ? '—' : fixed(vpd, 1)}
                    </span>
                  </div>
                </td>
                <td className={`td num text-right ${lenientLeads ? 'font-medium text-slate-900' : 'text-slate-600'}`}>
                  {lenient === null ? '—' : fixed(lenient, 1)}
                </td>
                <td className={`td num text-right ${waste > 0 ? 'text-rose-600' : 'text-emerald-700'}`}>
                  {usd0(waste)}
                </td>
                {showContracts ? (
                  <td
                    className={`td num text-right ${
                      contractCount(a) !== null && contractCount(a) === fewest ? 'text-slate-900' : 'text-slate-600'
                    }`}
                  >
                    {contractCount(a) === null ? '—' : int0(contractCount(a))}
                  </td>
                ) : null}
                <td className="td num text-right text-slate-600">{usd0(a.spend)}</td>
                <td className="td num text-right text-slate-600">{compact(effectiveViews(a))}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {/* 稳健性结论与引擎事前估分并排放一行：都是这张表的脚注，不值得各占一行版面 */}
      <div className="mt-1 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-[11.5px]">
        {mainLead && lenientLead ? (
          <span className="leading-snug text-slate-600">
            {/* 取舍结论：每美元领先是用什么换来的。全部由当次数据现算，不硬编任何数字 */}
            {tradeoff ? <span className="text-slate-700">{tradeoff} </span> : null}
            {mainLead.arm === lenientLead.arm
              ? `两个口径下每美元最高的都是「${mainLead.label}」，排序不依赖水号曝光是否完全作废。`
              : `主口径最高是「${mainLead.label}」，水号按 50% 计后换成「${lenientLead.label}」，排序依赖该假设。`}
          </span>
        ) : (
          <span />
        )}
        {engineArms.length > 0 ? (
          <details className="text-slate-600">
            <summary className="cursor-pointer text-slate-500">引擎事前估分（不参与判优）</summary>
            <div className="mt-1 leading-snug">
              每美元事前估分：
              {engineArms.map((a, i) => (
                <span key={a.arm}>
                  {i > 0 ? '；' : ''}
                  {a.label} <span className="num">{fixed(enginePerDollar(a) ?? 0, 1)}</span>
                </span>
              ))}
              。事前估分 = 历史均曝光 × 真实性折扣 × 语义适配 × 受众匹配 × KPI 权重，按等效条数加总，
              用于给候选人排序；本表的判优只看上面按标注结算的有效曝光。
            </div>
            {judge ? <div className="mt-1 leading-snug text-slate-500">{judge.pipeline_separation}</div> : null}
          </details>
        ) : null}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 预算没花完                                                          */
/* ------------------------------------------------------------------ */

/** 一条放宽建议算不算「有用」：能多花出去的钱至少要到 1 美元，否则只是重跑后的抖动。 */
const HELPS_LINE = 1;

/**
 * 一条放宽建议：改哪一处、这次能多花出多少、名单质量会怎么变。
 *
 * 增量不为正的那条也照实列出来，但不写成「可多花出 -$677」——负数金额在投放页面上
 * 读不出任何动作。它的意思是"这条放宽打不开这笔预算"，就按这句话写。
 */
function AdviceItem({ row }: { row: AdviceRow }): React.ReactElement {
  const helps = row.extra_spendable_usd >= HELPS_LINE;
  return (
    <li className={`rounded-lg border px-2.5 py-1 ${helps ? 'border-slate-200 bg-white' : 'border-slate-200 bg-slate-50'}`}>
      <div className="text-[12.5px] font-medium leading-snug">
        <span className={helps ? 'text-slate-800' : 'text-slate-600'}>{row.title}</span>
      </div>
      <div className="flex flex-wrap items-baseline gap-x-2 text-[11.5px]">
        {helps ? (
          <span className="num text-[12px] text-live-700">可多花出 {usd0(row.extra_spendable_usd)}</span>
        ) : (
          <span className="text-slate-500">这条打不开预算：能花出去的钱没有变多</span>
        )}
        {/* 只留"改完这次能花出去多少"这一个口径：预算总额上面那句已经写过了 */}
        <span className="num text-slate-600">
          花得出去 {pct1(row.utilization_after)} · 名单 +{int0(row.extra_picked)} 人
        </span>
      </div>
      <div className={`text-[11.5px] leading-snug ${helps ? 'text-slate-600' : 'text-slate-500'}`}>{row.action}</div>
      <div className="text-[11.5px] leading-snug text-slate-500">{row.quality_note}</div>
    </li>
  );
}

/**
 * 「预算没花完」结论块。
 *
 * 触发条件与契约 §4.2 同一条线（利用率 < 60%）。三段话按投放视角排：
 * 这次花得出去多少 → 为什么卡住 → 下一步可以怎么放宽。
 * 建议为空时也照实说明：这一步是投放决策，不是失败通知。
 */
export function BudgetShortfall({
  allocation,
  advice,
  pending = false,
}: {
  allocation: AllocationBlock;
  advice: AdviceRow[];
  pending?: boolean;
}): React.ReactElement {
  const u = utilizationOf(allocation);
  const helpful = advice.filter((r) => r.extra_spendable_usd >= HELPS_LINE).length;
  const lead =
    helpful > 0
      ? `下一步可以${helpful >= 2 ? `${helpful === 3 ? '三' : '二'}选一` : '这样做'}（每条只放宽一处，金额是按这条放宽重跑一遍得到的）：`
      : '三种放宽都各重跑了一遍，没有一条能把这笔预算打开：';
  return (
    <div className="rounded-xl border border-amber-300 bg-amber-50 px-3 py-2.5">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <Badge className="border-amber-400 bg-white text-amber-800">预算没花完</Badge>
        <span className="text-[13px] font-medium text-slate-900">
          {usd0(allocation.budget_usd)} 预算里，这次只投得出去 <span className="num">{usd0(allocation.allocated_usd)}</span>
          （{pct1(u)}），落到 {int0(allocation.picked)} 人；余下 <span className="num">{usd0(allocation.unallocated_usd)}</span> 没有可投的人接
        </span>
      </div>
      <div className="mt-1 text-[12px] leading-relaxed text-slate-700">
        <span className="text-slate-600">卡在哪里：</span>
        {allocation.unallocated_why}
      </div>
      <div className="mt-1.5">
        <div className="text-[12px] text-slate-600">{lead}</div>
        {advice.length > 0 ? (
          // 三条建议是"三选一"，横着摆才读得出并列关系，也把这块压在一屏之内
          <ul className="mt-1 grid gap-1 lg:grid-cols-3">
            {advice.map((row) => (
              <AdviceItem key={row.key} row={row} />
            ))}
          </ul>
        ) : pending ? (
          <div className="muted mt-1">正在按三种放宽方式各重跑一遍…</div>
        ) : (
          <Note tone="warn">
            本次没有可用的放宽方式：这条需求没有年龄限定、目标市场也没有可扩的相邻市场，且候选里没有需人核的人。
            调高单人条数上限或换一批品类 / 平台后再试。
          </Note>
        )}
      </div>
    </div>
  );
}

/** 利用率达标时不额外占版面：摘要里的「已分配 / 预算」已经把这件事说清了。 */
