"""``POST_MARGINAL_DECAY`` 的敏感性扫描：把一句"这个假设不影响结论"变成三档实测。

为什么需要这个模块
------------------
采购模型里"同一达人第 n 条内容的边际曝光按 ``decay^(n-1)`` 递减"是一个**建模假设**，
不是从数据里拟合出来的。``budget/policy.py`` 早就定义了 ``POST_DECAY_SCAN = (0.5, 0.7, 0.9)``，
注释还写着"metrics.json 的 budget 段会同时给出三档结果，用来证明选谁的结论不依赖这个数字"——
**但产物里从来没有这个扫描**。也就是说，这条最关键的假设当时既没有敏感性证据，
文档里还有一句站不住的声称：注释在替一段不存在的输出背书。

本模块就是补上那段输出。三条口径纪律：

1. **换档要在同一批门禁结果上换。** 门禁判定与 decay 无关，因此每个 campaign 只跑一次门禁，
   三档共用。否则三档之间会掺进无关波动，扫描出来的差异不能归因到 decay。
2. **区分"会变的数字"和"不该变的结论"。** ``effective_posts`` 本身就是 decay 的函数，
   所以等效曝光、CPM、单人买几条**必然**随 decay 变——这类数字照实报，不假装稳定。
   需要稳定的是**选谁**（选中集合）与**结论方向**（少浪费为正、两段归因的符号）。
3. **稳不稳由判据算，不由我说。** 见 :data:`SELECTION_OVERLAP_STABLE_MIN`：
   重叠度不达标或任何一档上结论符号翻转时，``verdict`` 会如实变成"结论依赖该假设"，
   而不是留一句"我们扫过了，没问题"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..budget.planner import (
    gate_results_for,
    plan_baseline,
    plan_campaign,
    plan_diversified_no_gate,
)
from ..budget.policy import POST_DECAY_SCAN, POST_MARGINAL_DECAY
from ..gates.thresholds import Thresholds
from ..types import BudgetPlan, CampaignSpec, Kox
from .audit import plan_audit
from .metrics import gt_of

__all__ = [
    "SELECTION_OVERLAP_STABLE_MIN",
    "VALUE_SPREAD_STABLE_MAX",
    "VERDICT_ROBUST",
    "VERDICT_SELECTION_MOVES",
    "VERDICT_SENSITIVE",
    "decay_sensitivity_report",
]

#: 选人重叠度（Jaccard 与按金额加权的重叠份额）的下限。
#:
#: 取 0.80 的理由：这条门槛要判定的是"换个 decay 假设，钱还是花在同一批人身上"。
#: 重叠 0.80 意味着最多五分之一的名单/金额发生变动——这在"多买一条还是少买一条"的边际上
#: 是可以接受的抖动；再低就不能说"同一批人"了，那时正确的写法是承认
#: "decay 会改变选人与配额"，而不是把门槛调低让结论变绿。
SELECTION_OVERLAP_STABLE_MIN = 0.80

#: 价值结论（合计少浪费金额）在各档之间的相对离差上限：``(max − min) / max(|值|)``。
#: 0.20 的含义是"换档最多让招牌金额浮动两成"。这不是显著性检验——跨种子的方差另有
#: ``eval/multiseed.py`` 负责；这里只回答"同一份数据下，换这个假设会不会把结论说反或说飘"。
VALUE_SPREAD_STABLE_MAX = 0.20

#: 三种结论，按"哪些站得住"分开命名。刻意不合并成一个布尔：
#: 本次实测正是"价值结论稳、但选人会挪"的中间情形，合并只会逼着人二选一地夸大或抹平。
VERDICT_ROBUST = "value_conclusions_hold_and_selection_stable"
VERDICT_SELECTION_MOVES = "value_conclusions_hold_but_selection_shifts"
VERDICT_SENSITIVE = "value_conclusions_depend_on_decay_assumption"


def _selected_amounts(plan: BudgetPlan) -> dict[str, float]:
    return {a.kox_id: float(a.amount_usd) for a in plan.selected}


def _jaccard(a: Mapping[str, float], b: Mapping[str, float]) -> float | None:
    """选中集合的 Jaccard。两边都为空时返回 ``None``（没选人 ≠ 完全一致）。"""
    union = set(a) | set(b)
    if not union:
        return None
    return round(len(set(a) & set(b)) / len(union), 4)


def _spend_overlap(a: Mapping[str, float], b: Mapping[str, float]) -> float | None:
    """按金额加权的重叠份额：``Σ min(金额_a, 金额_b) / Σ 金额_b``（分母是参照档）。

    只看名单会高估稳定性——同一批人但钱挪走了一半，"选谁"其实已经变了。
    """
    total = sum(b.values())
    if total <= 0:
        return None
    shared = sum(min(a.get(k, 0.0), v) for k, v in b.items())
    return round(shared / total, 4)


def _sign(value: float | None) -> int | None:
    if value is None:
        return None
    return 0 if value == 0 else (1 if value > 0 else -1)


def _spread_share(values: Sequence[float]) -> float | None:
    """一组数的相对离差 ``(max − min) / max(|值|)``。

    分母取绝对值的最大值而不是均值：均值在正负混杂时会趋近 0，把离差放大成天文数字。
    全为 0 时返回 ``None``——"没有差异"和"离差为零"在这里应该区分开。
    """
    if not values:
        return None
    scale = max(abs(float(v)) for v in values)
    if scale == 0:
        return None
    return round((max(values) - min(values)) / scale, 4)


def _delivery_tiers(
    amounts_by_level: Mapping[float, Mapping[str, float]],
    levels: Sequence[float],
    ref: float,
) -> dict[str, Any]:
    """把"名单会随假设变动"从一句认输的 caveat 变成可交付的分层名单。

    敏感性扫描判定 ``selection_stable=False`` 之后，只写"名单不唯一"是没有产品动作的：
    投手拿到的仍是一份不知道哪里靠不住的清单。这里按"在几档上被选中"把名单切两层：

    - **核心层**：三档全选中。这批人的入选不依赖 decay 取值，可以直接下单。
    - **假设敏感层**：只在部分档位被选中。金额照给参照档的值，但必须带着
      "换个重复触达折扣假设就可能换人"的标记走到人工确认环节。

    这样"假设不确定"就从一个免责声明变成了**分流规则**：不确定性被定位到具体的人和
    具体的金额上，而不是笼统地打折整份名单的可信度。
    """
    ref_amounts = dict(amounts_by_level[ref])
    selected_sets = {lvl: set(amounts_by_level[lvl]) for lvl in levels}
    core_ids = set.intersection(*selected_sets.values()) if selected_sets else set()
    union_ids = set.union(*selected_sets.values()) if selected_sets else set()
    sensitive_ids = union_ids - core_ids

    def _amount_range(kox_id: str) -> dict[str, Any]:
        vals = [float(amounts_by_level[lvl].get(kox_id, 0.0)) for lvl in levels]
        return {
            "kox_id": kox_id,
            "amount_usd_reference": round(float(ref_amounts.get(kox_id, 0.0)), 2),
            "amount_usd_min": round(min(vals), 2),
            "amount_usd_max": round(max(vals), 2),
            "selected_at_decays": [lvl for lvl in levels if kox_id in selected_sets[lvl]],
        }

    ref_total = sum(ref_amounts.values())
    core_in_ref = sorted(core_ids & set(ref_amounts))
    sens_in_ref = sorted(sensitive_ids & set(ref_amounts))
    core_amount = sum(ref_amounts[k] for k in core_in_ref)
    sens_amount = sum(ref_amounts[k] for k in sens_in_ref)
    return {
        "n_selected_reference": len(ref_amounts),
        "n_union_across_decays": len(union_ids),
        "stable_core": {
            "n": len(core_ids),
            "n_in_reference_plan": len(core_in_ref),
            "amount_usd_in_reference_plan": round(core_amount, 2),
            "share_of_reference_spend": round(core_amount / ref_total, 4) if ref_total else None,
            "kox": [_amount_range(k) for k in core_in_ref],
        },
        "assumption_sensitive": {
            "n": len(sensitive_ids),
            "n_in_reference_plan": len(sens_in_ref),
            "amount_usd_in_reference_plan": round(sens_amount, 2),
            "share_of_reference_spend": round(sens_amount / ref_total, 4) if ref_total else None,
            "kox": [_amount_range(k) for k in sorted(sensitive_ids)],
        },
    }


def decay_sensitivity_report(
    records: Sequence[Kox],
    specs: Sequence[CampaignSpec],
    thresholds: Thresholds,
    decays: Sequence[float] = POST_DECAY_SCAN,
    reference_decay: float = POST_MARGINAL_DECAY,
    results_by_campaign: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """在 ``decays`` 各档上重跑三臂预算，报选人重叠度与结论符号的稳定性。

    Args:
        records: 达人库（含 gt；gt 只用于事后审计浪费金额与有效曝光）。
        specs: campaign spec 列表。
        thresholds: 与正式链路同一套阈值。
        decays: 要扫的档位，默认 ``POST_DECAY_SCAN``。
        reference_decay: 参照档（正式链路用的那个值），会被自动并入扫描集合。
        results_by_campaign: ``campaign_id -> {kox_id: GateResult}``，正式链路已经跑过时传进来复用。

    Returns:
        含 ``per_decay`` / ``per_campaign`` / ``stability`` / ``verdict`` 的报告；
        没有真实 campaign 时报 ``status="no_campaign_spec"``，不编任何数字。
    """
    real_specs = [s for s in specs if not s.is_neutral]
    if not real_specs:
        return {
            "status": "no_campaign_spec",
            "note": "没有真实 campaign spec，预算分配无从谈起，因此不做 decay 扫描。",
        }
    levels = sorted({round(float(d), 6) for d in list(decays) + [float(reference_decay)]})
    ref = round(float(reference_decay), 6)
    gt_by_id = {str(r.get("kox_id")): gt_of(r) for r in records}

    # 门禁与 decay 无关：每个 campaign 只跑一次，三档共用同一批判定。
    gates: dict[str, Mapping[str, Any]] = {}
    for spec in real_specs:
        cached = (results_by_campaign or {}).get(spec.campaign_id)
        gates[spec.campaign_id] = (
            cached if cached is not None else gate_results_for(records, spec, thresholds)
        )

    # ---- 逐 campaign × 逐档实跑 -------------------------------------------
    rows: dict[str, dict[float, dict[str, Any]]] = {s.campaign_id: {} for s in real_specs}
    amounts: dict[str, dict[float, dict[str, float]]] = {s.campaign_id: {} for s in real_specs}
    audits: dict[str, dict[float, dict[str, Mapping[str, Any]]]] = {
        s.campaign_id: {} for s in real_specs
    }
    for spec in real_specs:
        res = gates[spec.campaign_id]
        for level in levels:
            plan, _ = plan_campaign(records, spec, thresholds, res, decay=level)  # type: ignore[arg-type]
            base = plan_baseline(records, spec, thresholds, res, decay=level)  # type: ignore[arg-type]
            div = plan_diversified_no_gate(records, spec, thresholds, res, decay=level)  # type: ignore[arg-type]
            kox_audit = plan_audit(plan, gt_by_id)
            base_audit = plan_audit(base, gt_by_id)
            div_audit = plan_audit(div, gt_by_id)
            amounts[spec.campaign_id][level] = _selected_amounts(plan)
            audits[spec.campaign_id][level] = {
                "koxpilot": kox_audit,
                "baseline": base_audit,
                "diversified_no_gate": div_audit,
            }
            rows[spec.campaign_id][level] = {
                "decay": level,
                "n_selected": len(plan.selected),
                "n_posts": plan.n_posts,
                "spent_usd": round(plan.spent_usd, 2),
                "utilization": round(plan.spent_usd / plan.budget_usd, 4)
                if plan.budget_usd
                else 0.0,
                # 下面两个**本来就该随 decay 变**（等效条数是 decay 的函数），照实报。
                "est_effective_posts_total": round(
                    sum(a.effective_posts for a in plan.selected), 4
                ),
                "est_cpm_usd": round(plan.est_cpm_usd, 3),
                "wasted_spend_usd_koxpilot": kox_audit["wasted_spend_usd"],
                "wasted_spend_usd_baseline": base_audit["wasted_spend_usd"],
                "wasted_spend_usd_diversified_no_gate": div_audit["wasted_spend_usd"],
                "saved_usd_vs_baseline": round(
                    float(base_audit["wasted_spend_usd"]) - float(kox_audit["wasted_spend_usd"]), 2
                ),
                "effective_view_rate_koxpilot": kox_audit["effective_view_rate"],
                "constraints_ok": plan.constraints.get("all_enforced_satisfied"),
            }

    # ---- 汇总每一档 -------------------------------------------------------
    arms = ("baseline", "diversified_no_gate", "koxpilot")
    per_decay: list[dict[str, Any]] = []
    for level in levels:
        waste = dict.fromkeys(arms, 0.0)
        nominal = dict.fromkeys(arms, 0.0)
        effective = dict.fromkeys(arms, 0.0)
        budget = n_selected = n_posts = 0.0
        for spec in real_specs:
            row = rows[spec.campaign_id][level]
            budget += spec.budget_usd
            n_selected += row["n_selected"]
            n_posts += row["n_posts"]
            for arm in arms:
                audit = audits[spec.campaign_id][level][arm]
                waste[arm] += float(audit["wasted_spend_usd"])
                nominal[arm] += float(audit["nominal_views"])
                effective[arm] += float(audit["effective_views_gt"])
        # 有效曝光率用"合计有效/合计名义"，不对三个 campaign 的比率求平均
        # （那会给小预算 campaign 同等权重，是另一个口径）。
        rates = {
            arm: (round(effective[arm] / nominal[arm], 4) if nominal[arm] > 0 else None)
            for arm in arms
        }
        saved_total = waste["baseline"] - waste["koxpilot"]
        saved_div = waste["baseline"] - waste["diversified_no_gate"]
        saved_gate = waste["diversified_no_gate"] - waste["koxpilot"]
        per_decay.append(
            {
                "decay": level,
                "is_reference": level == ref,
                "n_selected_total": int(n_selected),
                "n_posts_total": int(n_posts),
                "wasted_spend_usd": {k: round(v, 2) for k, v in waste.items()},
                "saved_usd_total": round(saved_total, 2),
                "saved_share_of_budget": round(saved_total / budget, 4) if budget else None,
                "saved_usd_by_diversification": round(saved_div, 2),
                "saved_usd_by_gating_and_quality_ranking": round(saved_gate, 2),
                "effective_view_rate": rates,
                "effective_view_rate_gap_pp_total": (
                    round((rates["koxpilot"] - rates["baseline"]) * 100, 2)
                    if rates["koxpilot"] is not None and rates["baseline"] is not None
                    else None
                ),
            }
        )

    # ---- 稳定性判据 -------------------------------------------------------
    campaign_blocks: list[dict[str, Any]] = []
    jaccards: list[float] = []
    overlaps: list[float] = []
    for spec in real_specs:
        block_rows = []
        for level in levels:
            row = dict(rows[spec.campaign_id][level])
            jac = _jaccard(amounts[spec.campaign_id][level], amounts[spec.campaign_id][ref])
            ovl = _spend_overlap(amounts[spec.campaign_id][level], amounts[spec.campaign_id][ref])
            row["selection_jaccard_vs_reference"] = jac
            row["spend_overlap_share_vs_reference"] = ovl
            if level != ref:
                if jac is not None:
                    jaccards.append(jac)
                if ovl is not None:
                    overlaps.append(ovl)
            block_rows.append(row)
        tiers = _delivery_tiers(amounts[spec.campaign_id], levels, ref)
        campaign_blocks.append(
            {
                "campaign_id": spec.campaign_id,
                "budget_usd": spec.budget_usd,
                "by_decay": block_rows,
                "saved_usd_sign_by_decay": {
                    str(r["decay"]): _sign(float(r["saved_usd_vs_baseline"])) for r in block_rows
                },
                "delivery_tiers": tiers,
            }
        )

    saved_by_decay = {str(d["decay"]): d["saved_usd_total"] for d in per_decay}
    div_by_decay = {str(d["decay"]): d["saved_usd_by_diversification"] for d in per_decay}
    gate_by_decay = {
        str(d["decay"]): d["saved_usd_by_gating_and_quality_ranking"] for d in per_decay
    }
    series = {
        "saved_usd_total": saved_by_decay,
        "saved_usd_by_diversification": div_by_decay,
        "saved_usd_by_gating_and_quality_ranking": gate_by_decay,
    }
    sign_stable = {k: len({_sign(v) for v in vals.values()}) == 1 for k, vals in series.items()}
    spread = {k: _spread_share(list(vals.values())) for k, vals in series.items()}
    # 逐 campaign 的符号也要看：合计为正完全可能盖住某个 campaign 上的负差额，
    # 而"某个 campaign 换档就从赚变亏"同样是"结论依赖假设"。
    per_campaign_sign_stable = {
        block["campaign_id"]: len(set(block["saved_usd_sign_by_decay"].values())) == 1
        for block in campaign_blocks
    }
    jac_min = min(jaccards) if jaccards else None
    ovl_min = min(overlaps) if overlaps else None
    selection_stable = (
        jac_min is not None
        and ovl_min is not None
        and jac_min >= SELECTION_OVERLAP_STABLE_MIN
        and ovl_min >= SELECTION_OVERLAP_STABLE_MIN
    )
    signs_ok = all(sign_stable.values()) and all(per_campaign_sign_stable.values())
    spreads_ok = all(
        v is not None and v <= VALUE_SPREAD_STABLE_MAX for v in spread.values()
    )
    value_conclusions_stable = signs_ok and spreads_ok
    if value_conclusions_stable and selection_stable:
        verdict = VERDICT_ROBUST
    elif value_conclusions_stable:
        verdict = VERDICT_SELECTION_MOVES
    else:
        verdict = VERDICT_SENSITIVE

    # 这里刻意分两类记录：``blockers`` 是"价值结论站不住"的理由（会把 verdict 打成
    # SENSITIVE）；``caveats`` 是"结论站得住，但有句话不能说"的限定（不改 verdict，
    # 但必须跟着结论一起被读到）。把两者混成一个列表，就会出现"扫过了没问题"这种
    # 听起来通过、实际掩盖了限定条件的说法。
    blockers: list[str] = []
    caveats: list[str] = []
    for key, stable in sign_stable.items():
        if not stable:
            blockers.append(f"{key} 在各档之间符号翻转：该结论依赖 decay 假设，不能当成稳定结论。")
    for cid, stable in per_campaign_sign_stable.items():
        if not stable:
            blockers.append(f"{cid} 的'相对基线少浪费'符号随档位翻转，单 campaign 结论不稳。")
    for key, value in spread.items():
        if value is not None and value > VALUE_SPREAD_STABLE_MAX:
            blockers.append(
                f"{key} 在各档之间的相对离差 {value:.1%} 超过 {VALUE_SPREAD_STABLE_MAX:.0%}，"
                "招牌金额对该假设敏感。"
            )
    if jac_min is not None and jac_min < SELECTION_OVERLAP_STABLE_MIN:
        caveats.append(
            f"选中名单重叠不足：非参照档与参照档的 Jaccard 最低 {jac_min:.3f}，"
            f"低于 {SELECTION_OVERLAP_STABLE_MIN:.2f}，**不能**说'换档买的还是同一批人'。"
        )
    if ovl_min is not None and ovl_min < SELECTION_OVERLAP_STABLE_MIN:
        caveats.append(
            f"金额加权重叠最低 {ovl_min:.3f}，低于 {SELECTION_OVERLAP_STABLE_MIN:.2f}——"
            "名单大体还是那批人，但钱在人与内容条数之间挪了位置。"
            "所以本扫描支持的说法是'价值结论不依赖 decay'，"
            "**不是**'具体买谁、每人买几条不依赖 decay'。"
        )

    saved_values = [float(v) for v in saved_by_decay.values()]
    core_n = sum(b["delivery_tiers"]["stable_core"]["n_in_reference_plan"] for b in campaign_blocks)
    sens_n = sum(
        b["delivery_tiers"]["assumption_sensitive"]["n_in_reference_plan"] for b in campaign_blocks
    )
    core_amt = sum(
        b["delivery_tiers"]["stable_core"]["amount_usd_in_reference_plan"] for b in campaign_blocks
    )
    sens_amt = sum(
        b["delivery_tiers"]["assumption_sensitive"]["amount_usd_in_reference_plan"]
        for b in campaign_blocks
    )
    tier_total = core_amt + sens_amt
    delivery_policy = {
        "trigger": "selection_stable == false",
        "rule": (
            "名单按'在几档 decay 上都被选中'分两层交付：三档全选中的进核心层，"
            "只在部分档位出现的进假设敏感层，带标记走人工确认，不与核心层混在一份清单里发出。"
        ),
        "stable_core": {
            "n_kox": core_n,
            "amount_usd": round(core_amt, 2),
            "share_of_spend": round(core_amt / tier_total, 4) if tier_total else None,
            "action": "可直接下单：入选不依赖 decay 取值。",
        },
        "assumption_sensitive": {
            "n_kox": sens_n,
            "amount_usd": round(sens_amt, 2),
            "share_of_spend": round(sens_amt / tier_total, 4) if tier_total else None,
            "action": "标记'重复触达折扣假设敏感'，交人工确认或改按单条采购，不自动执行。",
        },
        "why_not_just_a_caveat": (
            "判据不达标时若只写一句'名单不唯一'，投手拿到的仍是一份不知道哪里靠不住的清单。"
            "分层把不确定性定位到具体的人和金额上，让'假设不确定'产生一个分流动作，"
            "而不是笼统地打折整份名单的可信度。"
        ),
    }
    headline = (
        f"decay ∈ {tuple(levels)}（正式链路用 {ref}）："
        f"合计少浪费 ${min(saved_values):,.0f}~${max(saved_values):,.0f}"
        f"（相对离差 {spread['saved_usd_total']:.1%}）"
        f"，两段归因（分散化 / 门禁与质量排序）符号"
        f"{'均未翻转' if signs_ok else '发生翻转'}；"
        f"但选中名单 Jaccard 最低 {jac_min if jac_min is not None else float('nan'):.3f}、"
        f"金额加权重叠最低 {ovl_min if ovl_min is not None else float('nan'):.3f}，"
        f"说明{'选谁与买几条会随该假设变动' if not selection_stable else '选人也基本不变'}。"
    )

    return {
        "status": "ok",
        "assumption": "post_marginal_decay",
        "assumption_kind": "建模假设（受众重叠导致的重复触达折扣），不是从数据拟合的观测值",
        "reference_decay": ref,
        "scan": list(levels),
        "policy_constant": "budget.policy.POST_DECAY_SCAN",
        "method": (
            "每个 campaign 只跑一次门禁（门禁与 decay 无关），三档共用同一批 GateResult；"
            "每档重跑三臂预算分配，再用 gt 审计浪费金额与有效曝光。"
            "参照档的数字必须与 metrics.json 的正式预算/反事实结果一致（同一套代码路径）。"
        ),
        "per_decay": per_decay,
        "per_campaign": campaign_blocks,
        "stability": {
            "selection_jaccard_min_vs_reference": jac_min,
            "spend_overlap_share_min_vs_reference": ovl_min,
            "threshold": SELECTION_OVERLAP_STABLE_MIN,
            "selection_stable": selection_stable,
            "saved_usd_total_by_decay": saved_by_decay,
            "saved_usd_by_diversification_by_decay": div_by_decay,
            "saved_usd_by_gating_and_quality_ranking_by_decay": gate_by_decay,
            "sign_stable": sign_stable,
        },
        "what_moves_with_decay": [
            "每人买几条 / 总条数：decay 越高，第 2、3 条的边际价值越接近第 1 条，"
            "贪心就更愿意在同一个人身上加买（本次实测总条数随档位变化）。",
            "等效条数、估算曝光与 CPM：``effective_posts`` 本身就是 decay 的函数，"
            "这些数字**必然**随档位变，不属于'应当稳定'的范畴，照实报出。",
        ],
        "blockers": blockers,
        "caveats": caveats,
        "delivery_policy": delivery_policy,
        "verdict": verdict,
        "headline": headline,
        "note": (
            "这条扫描回答的是'选谁与结论方向是否依赖该假设'，"
            "不回答'哪个 decay 更接近真实世界'——后者需要真实投放的重复触达数据，本项目没有。"
        ),
    }
