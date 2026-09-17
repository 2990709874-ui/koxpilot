import React from 'react';
import { AlertTriangle, ArrowRight, Ban, Coins, Cpu, TriangleAlert, Zap } from 'lucide-react';
import { BenchChart, DuoBars, TokenBars, type BenchBar } from '../components/charts';
import { Badge, MissingArtifact, Note, Panel, Segmented, Stat, TruthChip } from '../components/ui';
import type { Loose } from '../lib/artifacts';
import { compact, fixed, int0, pct1, usd0 } from '../lib/format';

const MODEL_LABEL: Record<string, string> = { ark: 'ARK', azure: 'Azure', none: '无模型' };

export function CostValueTab({
  promptBench,
  llmBench,
  llmCompare,
  audit,
  metrics,
}: {
  promptBench: Loose | null;
  llmBench: Loose | null;
  llmCompare: Loose | null;
  audit: Loose | null;
  metrics: Loose | null;
}): React.ReactElement {
  const [view, setView] = React.useState<'arms' | 'versions'>('arms');
  const [picked, setPicked] = React.useState<string | null>(null);

  const arms = ((promptBench?.arms ?? []) as Loose[]).filter((a) => a.prompt_version !== 'rule');
  const ruleArm = ((promptBench?.arms ?? []) as Loose[]).find((a) => a.prompt_version === 'rule') ?? null;
  const ruleF1 = promptBench ? Number(promptBench.rule_baseline_f1) : null;
  const variants = (promptBench?.prompt_variants ?? []) as Loose[];
  const versionAvg = (promptBench?.version_average ?? {}) as Record<string, Loose>;

  const VERSION_COLOR: Record<string, string> = { v1: '#fb7185', v2: '#fbbf24', v3: '#22d3ee' };

  const bars: BenchBar[] = React.useMemo(() => {
    if (!promptBench) return [];
    if (view === 'versions') {
      return ['v1', 'v2', 'v3'].map((v) => {
        const a = versionAvg[v] ?? {};
        return {
          key: v,
          group: v,
          sub: `双模型平均 · ${int0(Number(a.total_tokens))} token`,
          label: `${v} 双模型平均`,
          f1: Number(a.avg_f1),
          precision: Number(a.avg_precision),
          recall: Number(a.avg_recall),
          tokens: Number(a.total_tokens),
          color: VERSION_COLOR[v],
          caution: v === 'v3' ? 'v3 同时改了两处（业务判据 + 完整邻接表），v2→v3 的增益无法在二者之间严格归因' : undefined,
        };
      });
    }
    return arms.map((a) => ({
      key: `${a.prompt_version}-${a.model_key}`,
      group: `${a.prompt_version} · ${MODEL_LABEL[String(a.model_key)] ?? a.model_key}`,
      sub: `${int0(Number(a.total_tokens))} token`,
      label: `${a.prompt_version} × ${MODEL_LABEL[String(a.model_key)] ?? a.model_key}（${a.model}）`,
      f1: Number(a.f1),
      precision: Number(a.precision),
      recall: Number(a.recall),
      tokens: Number(a.total_tokens),
      color: VERSION_COLOR[String(a.prompt_version)] ?? '#94a3b8',
      caution: a.prompt_version === 'v3' ? 'v3 同时改了两处（业务判据 + 完整邻接表），增益无法严格归因到单一改动' : undefined,
    }));
  }, [promptBench, view, arms, versionAvg]);

  const pickedBar = bars.find((b) => b.key === picked) ?? null;
  const belowCount = bars.filter((b) => ruleF1 !== null && b.f1 < ruleF1).length;

  const tagBench = (llmBench?.tag_bench ?? null) as Loose | null;
  const tagArms = (tagBench?.arms ?? {}) as Record<string, Loose>;
  const totals = (llmBench?.totals ?? null) as Loose | null;
  const perTask = (llmBench?.per_task ?? {}) as Record<string, Loose>;
  const costAudit = (audit?.cost_audit ?? null) as Loose | null;

  return (
    <div className="space-y-4">
      {/* ================= Prompt 迭代主图 ================= */}
      {promptBench ? (
        <Panel
          title="① Prompt 迭代：前两版都输给了一条 20 行的零成本规则"
          subtitle={`任务=${String(promptBench.task)}｜样本 ${int0(Number(promptBench.n_samples))} 条（正例 ${int0(Number(promptBench.n_positives))}，占 ${pct1(Number(promptBench.positive_rate))}）｜三版 Prompt 共用完全相同的样本与 batch 切分，唯一变量是 system prompt`}
          right={
            <div className="flex items-center gap-2">
              <Segmented
                size="sm"
                value={view}
                onChange={setView}
                options={[
                  { value: 'arms', label: '6 个 arm（版本×模型）' },
                  { value: 'versions', label: '按版本取双模型平均' },
                ]}
              />
              <TruthChip kind="llm-offline" />
            </div>
          }
        >
          <div className="grid gap-4 xl:grid-cols-[1.35fr_1fr]">
            <div>
              <BenchChart
                bars={bars}
                baseline={ruleF1 ?? 0}
                baselineLabel={`零成本规则基线 F1 = ${fixed(ruleF1, 4)}（0 token）`}
                onPick={(k) => setPicked(picked === k ? null : k)}
                active={picked}
              />
              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5">
                <span className="flex items-center gap-1.5 text-[11px] text-slate-400">
                  <span className="inline-block h-0.5 w-5 border-t border-dashed border-slate-200" />
                  规则基线横贯全图：柱子低于它就是没打赢
                </span>
                <span className="flex items-center gap-1.5 text-[11px] text-slate-400">
                  <span className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full bg-amber-400 text-[9px] font-bold text-black">!</span>
                  斜纹柱 = 存在混淆变量，见右侧
                </span>
                <span className="num text-[11px] text-rose-300">
                  {belowCount} / {bars.length} 个{view === 'arms' ? ' arm' : ' 版本'}低于规则基线
                </span>
              </div>

              {pickedBar && (
                <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <div className="glass px-2.5 py-2">
                    <div className="text-[10px] text-slate-500">F1</div>
                    <div className="num text-[15px] text-slate-100">{fixed(pickedBar.f1, 4)}</div>
                    <div className={`num text-[10px] ${pickedBar.f1 >= (ruleF1 ?? 0) ? 'text-emerald-300' : 'text-rose-300'}`}>
                      vs 规则 {pickedBar.f1 - (ruleF1 ?? 0) >= 0 ? '+' : ''}
                      {fixed(pickedBar.f1 - (ruleF1 ?? 0), 4)}
                    </div>
                  </div>
                  <div className="glass px-2.5 py-2">
                    <div className="text-[10px] text-slate-500">精确率</div>
                    <div className="num text-[15px] text-slate-100">{fixed(pickedBar.precision, 4)}</div>
                    <div className="num text-[10px] text-slate-500">规则 {fixed(Number(ruleArm?.precision), 4)}</div>
                  </div>
                  <div className="glass px-2.5 py-2">
                    <div className="text-[10px] text-slate-500">召回率</div>
                    <div className="num text-[15px] text-slate-100">{fixed(pickedBar.recall, 4)}</div>
                    <div className="num text-[10px] text-slate-500">规则 {fixed(Number(ruleArm?.recall), 4)}</div>
                  </div>
                  <div className="glass px-2.5 py-2">
                    <div className="text-[10px] text-slate-500">token 成本</div>
                    <div className="num text-[15px] text-amber-200">{int0(pickedBar.tokens)}</div>
                    <div className="num text-[10px] text-slate-500">规则 0</div>
                  </div>
                </div>
              )}

              <div className="mt-3 rounded-xl border border-rose-400/25 bg-rose-400/[0.06] px-3 py-2.5">
                <div className="flex items-center gap-1.5 text-[12px] font-medium text-rose-200">
                  <Ban size={12} />
                  结论先说难听的那半句
                </div>
                <p className="mt-1 text-[11.5px] leading-relaxed text-slate-300">
                  v1（只描述任务）双模型平均 F1 <b className="num text-rose-200">{fixed(Number(versionAvg.v1?.avg_f1), 4)}</b>、
                  v2（加负例约束）<b className="num text-rose-200">{fixed(Number(versionAvg.v2?.avg_f1), 4)}</b>，
                  都<b className="text-rose-200">低于</b>那条 20 行 Jaccard 规则的 <b className="num">{fixed(ruleF1, 4)}</b>，
                  却分别烧掉了 {int0(Number(versionAvg.v1?.total_tokens))} 和 {int0(Number(versionAvg.v2?.total_tokens))} token。
                  换句话说：<b className="text-slate-100">把任务丢给大模型并不自动比规则强</b>，前两版是花钱买了更差的结果。
                  直到 v3 把判据从「标签是否一致」改成「投放方按 declared 选人会不会选错」，才第一次越过这条线
                  （{fixed(Number(versionAvg.v3?.avg_f1), 4)}，最好单组合 v3 × Azure = {fixed(Number((promptBench.best_arm as Loose).f1), 4)}）。
                </p>
              </div>
            </div>

            {/* 右侧：三步迭代 + 混淆变量 */}
            <div className="space-y-2.5">
              {variants.map((v) => {
                const avg = versionAvg[String(v.version)] ?? {};
                const f1 = Number(avg.avg_f1);
                const beat = ruleF1 !== null && f1 >= ruleF1;
                return (
                  <div
                    key={String(v.version)}
                    className={`rounded-xl border px-3 py-2.5 ${
                      beat ? 'border-cyan-300/40 bg-cyan-400/[0.07]' : 'border-white/10 bg-white/[0.025]'
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span
                        className="num rounded px-1.5 py-0.5 text-[10px] font-semibold text-black"
                        style={{ background: VERSION_COLOR[String(v.version)] }}
                      >
                        {String(v.version)}
                      </span>
                      <span className="text-[12px] font-medium text-slate-100">{String(v.name)}</span>
                      <span className={`num ml-auto text-[12px] ${beat ? 'text-cyan-200' : 'text-rose-300'}`}>
                        F1 {fixed(f1, 4)}
                      </span>
                    </div>
                    <div className="muted mt-1">{String(v.change)}</div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                      <Badge className="border-white/15 text-slate-400">system prompt {String(v.system_prompt_chars)} 字</Badge>
                      <Badge className="border-white/15 text-slate-400">
                        精确率 {fixed(Number(avg.avg_precision), 3)} / 召回 {fixed(Number(avg.avg_recall), 3)}
                      </Badge>
                      <Badge className={beat ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200' : 'border-rose-400/30 bg-rose-400/10 text-rose-200'}>
                        {beat ? '越过规则基线' : `低于规则基线 ${fixed(f1 - (ruleF1 ?? 0), 4)}`}
                      </Badge>
                    </div>
                    <div className="muted mt-1.5">
                      <span className="text-slate-500">事前假设：</span>
                      {String(v.hypothesis)}
                    </div>
                  </div>
                );
              })}

              <div className="rounded-xl border border-amber-400/35 bg-amber-400/[0.09] px-3 py-2.5">
                <div className="flex items-center gap-1.5 text-[12px] font-semibold text-amber-100">
                  <TriangleAlert size={13} />
                  v3 的增益不能严格归因（混淆变量）
                </div>
                <p className="mt-1 text-[11.5px] leading-relaxed text-amber-50/85">
                  v3 同时做了<b>两处</b>改动：<br />
                  <span className="text-amber-100">(1)</span> 把判据从「标签是否一致」改写为「投放方按 declared 选人会不会选错」，给 severity 明确定义、要求以浪费预算为锚点；<br />
                  <span className="text-amber-100">(2)</span> 把品类邻接关系从举例改为从 taxonomy 注入的完整确定性邻接表。<br />
                  因此 v2 → v3 的 +{fixed(Number(versionAvg.v3?.avg_f1) - Number(versionAvg.v2?.avg_f1), 4)} F1{' '}
                  <b>无法在这两处之间做严格归因</b>。要归因就得再补一组只改其中一处的实验 —— 我没有跑，所以这里如实标注，
                  而不是把功劳直接算给"业务判据化"这个更好听的说法。
                </p>
              </div>

              <Note>
                <b className="text-slate-300">控制变量：</b>
                {String(promptBench.control_variables)}
              </Note>
              <Note tone="warn">
                <b className="text-amber-200">可信度边界：</b>推理类模型默认不是贪心解码，同 prompt 同模型两次跑出的 F1 会不同（实测 ARK 0.653 vs 0.681）。
                所以这里所有数字都是<b>单次实测值</b>，不是均值±方差；结论层面（v3 &gt; v2 &gt; v1）稳，小数点后第二位不稳。
              </Note>
            </div>
          </div>
        </Panel>
      ) : (
        <MissingArtifact file="data/prompt_bench.json" what="Prompt 横评" how="运行 python -m koxpilot.llm.promptbench 生成 output/prompt_bench.json 后重跑 npm run refresh" />
      )}

      {/* ================= LLM vs 规则 ================= */}
      <div className="grid gap-3 lg:grid-cols-[1.05fr_1fr]">
        <Panel
          title="② 生产任务上的 LLM vs 规则（标签错配判定）"
          subtitle={tagBench ? `同一份 ${int0(Number(tagBench.n_samples))} 条抽样、同一份 ground truth` : ''}
          right={<TruthChip kind="llm-offline" />}
        >
          {tagBench ? (
            <>
              <table className="w-full">
                <thead>
                  <tr>
                    <th className="th">arm</th>
                    <th className="th text-right">精确率</th>
                    <th className="th text-right">召回率</th>
                    <th className="th text-right">F1</th>
                    <th className="th text-right">覆盖率</th>
                    <th className="th text-right">token</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(tagArms).map(([k, v]) => {
                    const isRule = v.kind === 'rule';
                    return (
                      <tr key={k} className="hairline">
                        <td className="td">
                          <span className={isRule ? 'text-slate-300' : 'text-cyan-100'}>{k}</span>
                          <Badge className={`ml-1.5 ${isRule ? 'border-slate-400/25 text-slate-400' : 'border-indigo-400/30 bg-indigo-400/10 text-indigo-200'}`}>
                            {isRule ? '纯规则' : 'LLM'}
                          </Badge>
                        </td>
                        <td className="td num text-right">{fixed(Number(v.precision), 4)}</td>
                        <td className="td num text-right">{fixed(Number(v.recall), 4)}</td>
                        <td className="td num text-right font-semibold">{fixed(Number(v.f1), 4)}</td>
                        <td className={`td num text-right ${Number(v.coverage) < 1 ? 'text-amber-300' : 'text-slate-400'}`}>
                          {pct1(Number(v.coverage))}
                          {v.n_judged !== undefined && (
                            <span className="ml-1 text-[10px] text-slate-500">({int0(Number(v.n_judged))} 条)</span>
                          )}
                        </td>
                        <td className="td num text-right">{isRule ? '0' : '见下方 token 账'}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <Note tone="warn">
                <AlertTriangle size={11} className="mr-1 inline" />
                Azure 那一行覆盖率是 <b className="text-amber-200">{pct1(Number(tagArms.azure?.coverage ?? 0))}</b>：50 个 batch 里
                <b className="text-amber-200">失败了 1 个</b>，对应 12 条样本没有判定。这 12 条被算作<b>未覆盖</b>（n_judged = {int0(Number(tagArms.azure?.n_judged ?? 0))}），
                而不是用默认值 / 众数 / 规则结果填充 —— 填充会让指标虚高且无法追溯。
              </Note>
              <Note>
                在这个任务上 LLM 确实赢了规则（F1 {fixed(Number(tagArms.ark?.f1), 4)} / {fixed(Number(tagArms.azure?.f1), 4)} vs {fixed(Number(tagArms.rule_jaccard?.f1), 4)}），
                主要赢在精确率：规则把所有"相邻品类"都当错配，而模型能区分「美妆↔时尚」这种业务上不算错配的情况。
                但生产链路里 G2.1 仍然默认走规则 —— 因为 5,000 人全量过 LLM 的调用量与延迟不划算，模型只在需要时介入。
              </Note>
            </>
          ) : (
            <MissingArtifact file="data/llm_bench.json" what="LLM 横评" />
          )}
        </Panel>

        <Panel
          title="③ 真实 LLM 用量账（取自 API usage 字段，非估算）"
          subtitle={llmBench ? `构建期一次性跑完，总耗时 ${Math.round(Number(llmBench.elapsed_s))} s；线上 Demo 不再调用任何 endpoint` : ''}
          right={<TruthChip kind="llm-offline" />}
        >
          {totals ? (
            <>
              <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Stat label="总 token" value={compact(Number(totals.total_tokens))} hint={`${int0(Number(totals.total_tokens))} token`} tone="accent" icon={<Zap size={11} />} />
                <Stat label="其中思维链" value={compact(Number(totals.reasoning_tokens))} hint={`reasoning ${int0(Number(totals.reasoning_tokens))} token，占 completion 的 ${pct1(Number(totals.reasoning_tokens) / Number(totals.completion_tokens))}`} />
                <Stat label="调用次数" value={int0(Number(totals.calls))} hint="含 brief / tag / fit 三个任务" />
                <Stat
                  label="失败调用"
                  value={int0(Number(totals.failed_calls))}
                  hint="失败的 batch 按未覆盖处理，不用默认值填充"
                  tone={Number(totals.failed_calls) > 0 ? 'warn' : 'good'}
                />
              </div>
              <TokenBars
                rows={Object.entries(perTask).map(([k, v]) => ({
                  label: k,
                  model: String(v.model),
                  prompt: Number(v.prompt_tokens),
                  completion: Number(v.completion_tokens),
                  reasoning: Number(v.reasoning_tokens),
                  calls: Number(v.calls),
                  failed: Number(v.failed_calls),
                }))}
              />
              <Note>{String(llmBench?.note ?? '')}</Note>
            </>
          ) : (
            <MissingArtifact file="data/llm_bench.json" what="token 账" />
          )}
        </Panel>
      </div>

      {/* ================= 成本审计 ================= */}
      <div className="grid gap-3 lg:grid-cols-[1fr_1fr]">
        <Panel
          title="④ 三种架构方案的调用量对比"
          subtitle="调用次数是可数真值（按候选池规模推算），不是估的"
          right={<TruthChip kind="python" />}
        >
          {costAudit ? (
            <>
              <div className="space-y-2.5">
                {((costAudit.schemes ?? []) as Loose[]).map((s) => {
                  const maxCalls = Math.max(...((costAudit.schemes ?? []) as Loose[]).map((x) => Number(x.calls_total)), 1);
                  const isOurs = String(s.scheme).includes('KOXPilot');
                  return (
                    <div key={String(s.scheme)}>
                      <div className="flex items-baseline justify-between">
                        <span className={`text-[12px] ${isOurs ? 'text-cyan-100' : 'text-slate-300'}`}>
                          {String(s.scheme)}
                          {isOurs && <Badge className="ml-1.5 border-cyan-400/30 bg-cyan-400/10 text-cyan-200">本实现</Badge>}
                        </span>
                        <span className="num text-[12px] text-slate-300">{int0(Number(s.calls_total))} 次调用</span>
                      </div>
                      <div className="mt-1 h-2.5 overflow-hidden rounded-sm bg-white/[0.05]">
                        <div
                          className="h-full rounded-sm"
                          style={{
                            width: `${(Number(s.calls_total) / maxCalls) * 100}%`,
                            background: isOurs ? '#22d3ee' : Number(s.calls_total) === 0 ? '#64748b' : '#fb7185',
                          }}
                        />
                      </div>
                      <div className="muted mt-1">{String(s.desc)}</div>
                    </div>
                  );
                })}
              </div>
              <div className="mt-3 grid grid-cols-2 gap-2">
                <Stat
                  label="调用量降低"
                  value={`${fixed(Number(costAudit.call_reduction_vs_full_llm), 2)}×`}
                  hint="混合架构 vs 全 LLM（888 → 225 次）"
                  tone="good"
                  icon={<Cpu size={11} />}
                />
                <Stat
                  label="全规则方案的代价"
                  value={fixed(Number((costAudit.accuracy_cost_of_all_rules as Loose).rule_arm), 4)}
                  hint={`${String((costAudit.accuracy_cost_of_all_rules as Loose).metric)}；LLM 一列在 audit.json 里是 null（生成时未配置 endpoint），真实对照见上面第 ② 块`}
                  tone="warn"
                />
              </div>
              <Note tone="warn">
                <b className="text-amber-200">产物口径说明：</b>audit.json 的 <code className="font-mono text-[10px]">token_account</code> 是{' '}
                <code className="font-mono text-[10px]">null</code>、status = {String(costAudit.status)} ——
                因为成本审计跑在 LLM 真调之前。我没有回头改这份产物去"补齐"，而是把真实 token 账放在 llm_bench.json 并在上面第 ③ 块展示。
                两份产物的时序差异如实呈现。
              </Note>
            </>
          ) : (
            <MissingArtifact file="data/audit.json" what="成本审计" />
          )}
        </Panel>

        <Panel
          title="⑤ 钱花得值不值：反事实价值账"
          subtitle="以 gt 为裁判对照「按粉丝量买」，三个 campaign 合计"
          right={<TruthChip kind="audit" />}
        >
          {audit?.counterfactual_value_audit ? (
            <>
              <div className="mb-3 grid grid-cols-3 gap-2">
                <Stat
                  label="合计少浪费"
                  value={usd0(Number((audit.counterfactual_value_audit as Loose).totals.saved_usd))}
                  hint={`占 ${usd0(Number((audit.counterfactual_value_audit as Loose).totals.budget_usd))} 预算的 ${pct1(Number((audit.counterfactual_value_audit as Loose).totals.saved_share_of_budget))}`}
                  tone="good"
                  icon={<Coins size={11} />}
                />
                <Stat
                  label="有效曝光提升"
                  value={`+${pct1(Number((audit.counterfactual_value_audit as Loose).totals.effective_view_uplift))}`}
                  hint={`${compact(Number((audit.counterfactual_value_audit as Loose).totals.effective_views_baseline))} → ${compact(Number((audit.counterfactual_value_audit as Loose).totals.effective_views_koxpilot))}（水号曝光按 0 计）`}
                  tone="accent"
                />
                <Stat
                  label="其中一个 campaign 为负"
                  value="BRIEF-002"
                  hint="少浪费 −$5,118、有效曝光 −2.3%；照实保留，不从汇总里剔除"
                  tone="bad"
                />
              </div>
              <DuoBars
                leftName="按粉丝量买（基线）"
                rightName="KOXPilot"
                fmt={(x) => (x > 1e5 ? compact(x) : usd0(x))}
                rows={((audit.counterfactual_value_audit as Loose).per_campaign as Loose[]).map((c) => ({
                  label: `${String(c.campaign_id)}　预算 ${usd0(Number(c.budget_usd))}`,
                  left: Number((c.baseline as Loose).wasted_spend_usd),
                  right: Number((c.koxpilot as Loose).wasted_spend_usd),
                  note: String(c.headline),
                }))}
              />
              <Note>
                方法：{String((audit.counterfactual_value_audit as Loose).method.baseline)}；
                {String((audit.counterfactual_value_audit as Loose).method.judge)}
              </Note>
            </>
          ) : (
            <MissingArtifact file="data/audit.json" what="反事实价值审计" />
          )}
        </Panel>
      </div>

      {/* ================= 缺失产物 ================= */}
      {!llmCompare && (
        <MissingArtifact
          file="data/llm_compare.json"
          what="LLM / 规则逐条差异明细"
          how={
            <>
              metrics.json 的表 6 里 LLM 一列也是空的（status ={' '}
              <code className="font-mono text-[10px]">{String((metrics?.table_6_llm_vs_rule as Loose | undefined)?.status ?? 'unknown')}</code>
              ），原因同上：评测跑在 LLM 真调之前。可用的真实 LLM 对照在 llm_bench.json 与 prompt_bench.json，已在本页第 ②③ 块展示。
            </>
          }
        />
      )}

      <Note>
        <ArrowRight size={11} className="mr-1 inline text-cyan-300" />
        本页想说的其实是一句反直觉的话：<b className="text-slate-200">大模型不是免费的准确率</b>。
        它在标签错配这种"需要业务常识判断相邻品类"的任务上确实赢过规则；但只有当 Prompt 把业务判据写清楚之后才赢，
        而在 G0/G1 这类纯统计信号的任务上，规则又快又准还 0 token —— 所以生产链路是混合的，不是"全都上模型"。
      </Note>
    </div>
  );
}
