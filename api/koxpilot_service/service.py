"""三个业务用例的编排：plan / explain / gate_batch。

本模块的唯一职责是**编排 + 翻译**：
- 编排：按 A1→A2→A3→A5→A6 的顺序调用 ``koxpilot`` 包里既有的函数；
- 翻译：把 dataclass 与内部代码标识符翻成 ``api/CONTRACT.md`` 规定的 JSON 形状与中文人话。

一条纪律：**判定/打分/分配的逻辑一行都不在这里**。
门禁走 ``gates.engine.evaluate``，召回走 ``budget.value.targeting_reason``，
预算走 ``budget.planner.plan_*``，反事实账走 ``eval.audit.counterfactual_report``——
与 CLI 调的是同一批函数。否则接口与 CLI 必然漂移，这个作品最值钱的"口径一致"也就没了。

与浏览器 TS 引擎的对齐点（决定实时一致性比对能不能是 0 差异）
------------------------------------------------------------
1. **不注入 LLM 适配分**：``evaluate(kox, spec, thresholds)`` 让 G2.3 走规则版 fit，
   前端 ``runPipeline`` 也是这么调的（它不传 fitScores）。注入了就会两侧不一致。
2. **预置 brief 直接用固化 spec**：前端对预置 brief 用的是 ``briefs.json`` 里的 ``spec``，
   服务再拿原文重解析一遍反而会引入差异，所以 ``brief_id`` 路径不重新解析。
3. **默认参数取包里的默认值**：include_review / decay 都用 ``budget.policy`` 的默认值，
   与前端默认值一致。
4. **召回不截断**：命中定向的全部候选都进入门禁与预算计算，``options.top_n`` 只裁
   返回给前端的明细条数。截断计算会让服务与离线 CLI 变成两道题（预算利用率能差十几倍），
   而 ``parity_payload`` 覆盖的正是这批参与计算的候选，前端才能用同一批 id 重算出同一份金额。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

from .briefparse import MAX_BRIEF_CHARS, parse_brief
from .labels import (
    AGENT_LABEL,
    ARM_LABEL,
    FIT_SOURCE_CACHED_LLM,
    FIT_SOURCE_RULE_FALLBACK,
    GATE_LABEL,
    SIGNAL_ABOVE_REFERENCE,
    SIGNAL_IN_RANGE,
    SIGNAL_MISSING,
    SIGNAL_NO_THRESHOLD,
    SIGNAL_OUT_OF_RANGE,
    gate_label,
    rule_label,
    signal_label,
)
from .store import STORE, Loaded, warmup_timeout

#: 契约 §6 的批量上限
GATE_BATCH_LIMIT = 5000

#: 契约 §4 的 options 口径
#:
#: ``top_n`` 只截断**返回给前端的候选明细条数**，不截断进入计算的候选池。
#: v1.0 里它同时截断了召回集合，于是同一条 brief 在 CLI 与服务上算出的预算方案能差一个
#: 数量级（BRIEF-001：离线 69 人 / $79,855，服务 top_n=200 时 8 人 / $5,693）——
#: 分配器的结构配额（长尾下限 / 单一国家上限）是在候选池上求解的，池被截掉之后
#: 「买得起且能满足配额的库存」也跟着消失，钱就花不出去。截断展示是合理的，截断计算是错的。
TOP_N_DEFAULT, TOP_N_MAX = 200, 500
EXPLAIN_LIMIT_DEFAULT, EXPLAIN_LIMIT_MAX = 60, 200

#: 判定 -> 中文前缀（reason_human 的第一个词）
VERDICT_PREFIX: Dict[str, str] = {"pass": "可投", "review": "需人核", "reject": "不可投"}

#: 判定的严重度排序（候选列表排序用，与界面「先看能投的」直觉一致）
VERDICT_RANK: Dict[str, int] = {"pass": 0, "review": 1, "reject": 2}

#: 四层门禁的固定顺序
ALL_GATES: Tuple[str, ...] = ("G0", "G1", "G2", "G3")


class ServiceError(Exception):
    """业务错误。契约 §1：一律 HTTP 200 + ``ok:false``，不占用 5xx。"""

    def __init__(self, code: str, message: str) -> None:
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def _snapshot() -> Loaded:
    """取数据快照；还没预热完就如实报错，不返回半成品。"""
    STORE.wait_ready(warmup_timeout())
    snap = STORE.snapshot()
    if snap is None:
        raise ServiceError(
            "internal",
            "数据集与阈值尚未就绪（当前状态：%s%s）"
            % (STORE.status, "，原因：%s" % STORE.error if STORE.error else ""),
        )
    return snap


def _clamp_int(value: Any, default: int, maximum: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    if num <= 0:
        return default
    return min(num, maximum)


# ---------------------------------------------------------------------------
# 证据链翻译
# ---------------------------------------------------------------------------
def _fit_source(raw: str) -> str:
    """门禁内部的 fit_source -> 契约 §4 只允许的两个取值。

    ``injected:llm_fit_score`` 才是真用了构建期 LLM 打的分；其余（规则版、
    无目标品类而跳过、整层被关掉）本质都是规则兜底，如实归到 ``rule_fallback``。
    """
    return FIT_SOURCE_CACHED_LLM if str(raw).startswith("injected") else FIT_SOURCE_RULE_FALLBACK


def _gate_hits(result: Any) -> List[Dict[str, Any]]:
    """GateResult.reasons -> 契约的 gate_hits（全中文，不泄露规则内部字段名）。"""
    hits: List[Dict[str, Any]] = []
    for reason in result.reasons:
        hits.append(
            {
                "gate": reason.gate,
                "gate_label": gate_label(reason.gate),
                # 契约示例里 signal_label 写的是「多源标签互相冲突」——即规则的中文名，
                # 而不是 kox JSON 里的字段名。字段名只作为可下钻信息留在 detail 里。
                "signal_label": rule_label(reason.rule_id),
                "detail": reason.human_text,
            }
        )
    return hits


def _reason_human(result: Any) -> str:
    """整条判定的人话说明。

    直接把 ``gates/humanize.py`` 产出的 ``human_text`` 原样拼起来，
    只在最前面加一个判定词——所以它与 CLI ``koxpilot explain`` 打印的逐条理由**逐字一致**。
    """
    prefix = VERDICT_PREFIX.get(result.verdict, result.verdict)
    if not result.reasons:
        return "%s：四层门禁（数据完整性 / 真实性 / 一致性 / 品牌安全）逐条核过，没有任何规则命中。" % prefix
    return "%s：%s" % (prefix, "".join(r.human_text for r in result.reasons))


# ---------------------------------------------------------------------------
# A1：brief -> spec
# ---------------------------------------------------------------------------
def _resolve_brief(body: Dict[str, Any], snap: Loaded) -> Tuple[Any, Dict[str, Any], float, List[str]]:
    """返回 ``(CampaignSpec, brief 响应块, A1 耗时 ms, 提示)``。"""
    from koxpilot.types import CampaignSpec

    brief_id = body.get("brief_id")
    if brief_id:
        preset = snap.briefs_by_id.get(str(brief_id))
        if preset is None:
            raise ServiceError("not_found", "没有这个预置 brief：%s" % brief_id)
        started = time.time()
        spec = CampaignSpec.from_dict(preset["spec"])
        elapsed = (time.time() - started) * 1000.0
        block = {
            "source": "preset",
            # 预置 brief 的结构化结果是构建期真调模型跑出来、固化进 data/briefs.json 的产物，
            # 服务这里只做读取与校验，不重新解析（重新解析会与前端用的固化 spec 打架）。
            "parse_path": "preset",
            "fields": _preset_fields(preset, spec),
            "notes": [
                "预置 brief 用的是构建期真调模型产出、已固化进 data/briefs.json 的投放规格，"
                "服务不重新解析——这样它与前端读的那份 spec 逐字段相同。"
            ],
        }
        return spec, block, elapsed, []

    raw = body.get("brief_text")
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise ServiceError("bad_request", "brief 是空的：请写一句投放需求，或改用 brief_id 取预置 brief。")
    if len(text) > MAX_BRIEF_CHARS:
        raise ServiceError(
            "bad_request",
            "brief 太长了（%d 字，上限 %d 字）：请精简到一段话。" % (len(text), MAX_BRIEF_CHARS),
        )

    parsed = parse_brief(text, "CUSTOM")
    spec = CampaignSpec.from_dict(parsed.spec)
    # notes 里装两类话：① 解析时发现的歧义/硬性要求（来自规则或模型）；
    # ② A1 这次到底走了模型还是规则、以及为什么。两类都如实回给前端，
    # 因为"为什么没用上模型"正是最容易被含糊掉的一句。
    notes = list(parsed.warnings)
    if parsed.llm_note:
        notes.append(parsed.llm_note)
    block = {
        "source": "free_text",
        "parse_path": parsed.parse_path,
        "fields": parsed.fields,
        "notes": notes,
    }
    return spec, block, parsed.elapsed_ms, notes


def _preset_fields(preset: Dict[str, Any], spec: Any) -> List[Dict[str, Any]]:
    """把固化 spec 翻成字段证据。``matched`` 给的是原文本身（它确实来自这条 brief）。"""
    from .briefparse import _category_zh, _money, _platform_zh  # 复用同一套中文化
    from .labels import GENDER_LABEL, KPI_LABEL, REGULATED_LABEL

    raw = str(preset.get("raw_text") or "")
    how = "预置 brief 的结构化结果由构建期真调模型产出并固化在数据集中，服务直接读取"
    rows: List[Dict[str, Any]] = []

    def add(key: str, label: str, value: Any, display: str) -> None:
        rows.append(
            {
                "key": key,
                "label": label,
                "value": value,
                "display": display,
                "status": "hit",
                "how": how,
                "matched": raw[:120] + ("…" if len(raw) > 120 else ""),
            }
        )

    budget = int(round(float(spec.budget_usd)))
    add("budget_usd", "预算", budget, _money(budget))
    cats = list(spec.target_categories)
    add("target_categories", "品类", cats, "/".join(_category_zh(c) for c in cats) if cats else "不限品类")
    plats = list(spec.platforms)
    add("platforms", "平台", plats, "/".join(_platform_zh(p) for p in plats) if plats else "不限平台")
    add("target_markets", "市场", list(spec.target_markets), "/".join(spec.target_markets) or "不限市场")
    add("target_languages", "语言", list(spec.target_languages), "/".join(spec.target_languages) or "不限语言")
    add("kpi", "KPI", spec.kpi, KPI_LABEL.get(spec.kpi, spec.kpi))
    gender = spec.target_gender
    add("target_gender", "性别", gender, GENDER_LABEL.get(str(gender), "不限性别") if gender else "不限性别")
    ages = list(spec.target_age_buckets)
    add("target_age_buckets", "年龄", ages, "/".join(ages) if ages else "不限年龄")
    brands = list(spec.competitor_brands)
    add("competitor_brands", "竞品回避", brands, "、".join(brands) if brands else "无")
    reg = spec.regulated_category
    add("regulated_category", "受管制口径", reg, REGULATED_LABEL.get(str(reg), "无") if reg else "无")
    return rows


# ---------------------------------------------------------------------------
# A2：召回
# ---------------------------------------------------------------------------
def _recall(snap: Loaded, spec: Any) -> List[Dict[str, Any]]:
    """定向召回：命中定向的**全部**达人，不做任何截断。

    为什么不截断
    ------------
    召回集合就是分配器的可行域。分配器要在这个池子上同时满足「长尾金额 ≥25%」、
    「单一国家 ≤60%」、「单达人 ≤预算的 X%」这几条结构配额；把池子截掉一半，
    被截掉的往往正是用来满足配额的那批库存（例如 BRIEF-001 里的 CA 长尾），
    于是可行域塌缩，预算利用率从 99.8% 掉到 7.1%。这不是「展示少一点」，
    而是**换了一道题**：同一条 brief 在离线 CLI 与线上服务给出的方案会差一个数量级。

    展示侧的收敛由 ``options.explain_limit`` / ``options.top_n`` 负责（只裁明细条数），
    计算侧一条不裁——这样服务与 ``koxpilot.cli`` 的 ``cmd_budget``（对全库跑
    ``plan_campaign``）是同一道题，结果可以逐条对上 ``output/budget.json``。
    """
    from koxpilot.budget.value import targeting_reason

    return [kox for kox in snap.records if targeting_reason(kox, spec) is None]


# ---------------------------------------------------------------------------
# A5/A6：预算与反事实账
# ---------------------------------------------------------------------------
#: 这条约束是**下限**（越低越危险），判断"卡住了没"时方向与其它上限型约束相反
_MIN_TYPE_CHECKS = ("longtail_min_share",)


def _unallocated_why(plan: Any, candidates: List[Any], remaining: float) -> str:
    """把「为什么没花完」说成人话，依据全部来自 allocator 自己的约束报告。"""
    budget = float(plan.budget_usd or 0.0)
    utilization = (float(plan.spent_usd) / budget) if budget else 0.0
    if remaining <= max(1.0, budget * 0.005):
        return "预算基本投尽（利用率 %.1f%%），剩下的是按条报价取整后的零头。" % (utilization * 100.0)

    parts: List[str] = []
    if utilization < 0.9:
        parts.append(
            "本次通过门禁、且报价与曝光齐备的候选只有 %d 人，其中 %d 人进了清单"
            % (plan.candidate_pool, len(plan.selected))
        )
    costs = [float(c.cost_usd) for c in candidates if float(c.cost_usd) > 0]
    if costs and remaining < min(costs):
        parts.append(
            "剩余 $%s 已低于候选里最便宜的一条内容报价 $%s，再买不到任何一条"
            % ("{:,.0f}".format(remaining), "{:,.0f}".format(min(costs)))
        )

    binding: List[str] = []
    broken: List[str] = []
    for check in (plan.constraints or {}).get("checks") or []:
        name = str(check.get("name"))
        actual, limit = check.get("actual"), check.get("limit")
        if not check.get("enforced") or name == "total_budget":
            continue
        if not isinstance(actual, (int, float)) or not isinstance(limit, (int, float)) or limit == 0:
            continue
        item = "%s（当前 %.1f%%）" % (check.get("desc"), float(actual) * 100.0)
        # 已经不满足的约束不能和"贴住上限"混在一句里说：把 74.1% 摆在"≤ 60%"后面
        # 当作没花完的理由，看的人只会以为页面算错了。这两种情况的动作也不同。
        if not check.get("satisfied", True):
            broken.append(item)
            continue
        near = actual <= limit * 1.02 if name in _MIN_TYPE_CHECKS else actual >= limit * 0.98
        if near:
            binding.append(item)
    if binding:
        parts.append("剩余额度已经贴住结构配额，再加钱就会破：%s" % "；".join(binding))
    if broken:
        parts.append(
            "候选池在这个定向下本身就偏，这几条配额已经守不住，继续加钱只会更偏：%s" % "；".join(broken)
        )

    if not parts:
        parts.append(
            "候选名额已用尽（候选 %d 人 × 每人最多 %d 条内容）"
            % (plan.candidate_pool, int(((plan.constraints or {}).get("policy") or {}).get("max_posts_per_kox", 3)))
        )
    return "；".join(parts) + "。"


def _arm_row(arm: str, plan: Any, audit: Dict[str, Any]) -> Dict[str, Any]:
    """一条臂的对外口径：花费、按标注结算的有效曝光（两个口径）、浪费金额。

    **判优字段一律以标注结果为裁判。** ``effective_views_gt*`` 与 ``waste_usd``
    全部取 A6 审计（``eval.audit.plan_audit``）的产出，换算成"每美元"也复用
    ``eval.audit.effective_view_calibers``——接口这里不写第二套除法，
    否则同一句结论会在服务、CLI、前端各算一遍，早晚漂移。

    为什么必须给"每美元"：三条臂花的钱可以差几十倍（KOXPilot 只买得起门禁过关的
    那几个人），把绝对有效曝光并排摆出来，读者会把"花得少"读成"做得差"。
    花费为 0 时如实给 ``null``——写 0 是错的。

    为什么主口径与宽松口径都给：主口径把标注为水号的曝光按 0 计，宽松口径按 50% 计。
    两个口径同向，结论才不依赖这个假设；只给对自己有利的那个不算结论。

    ``engine_*`` 是引擎的**事前估分**（``budget/value.py`` 的
    ``value(k) = avg_views × 真实性折扣 × 语义适配 × 受众匹配 × KPI 权重``，
    按等效条数加总）。它是选人排序的依据，作为次级信息给出，不参与本对比的判优——
    用引擎自己的分给引擎打分，数字再漂亮也不能当结论。
    """
    from koxpilot.eval.audit import effective_view_calibers

    spend = round(float(plan.spent_usd), 2)
    caliber = effective_view_calibers(audit, spend)
    engine_value = round(sum(float(a.value_score) for a in plan.selected), 1)
    row = {"arm": arm, "label": ARM_LABEL[arm], "spend": spend}
    # 这条臂实际拿到钱的达人数 = 要签、要沟通、要交付的合约数。
    # 它是**成本**不是战绩：摊得越薄，每美元越便宜，但真实采购里排不进档期。
    # 这笔成本完全不在判优指标（effective_views_gt_per_dollar）里，所以必须单独摆出来。
    row["n_selected"] = len(plan.selected)
    row.update(caliber)  # judge + 主口径/宽松口径的有效曝光与每美元有效曝光
    row["waste_usd"] = round(float(audit["wasted_spend_usd"]), 2)
    # 引擎事前估分：次级信息，不判优
    row["engine_expected_value"] = engine_value
    row["engine_value_per_dollar"] = round(engine_value / spend, 2) if spend > 0 else None
    # 兼容字段：与 effective_views_gt / effective_views_gt_per_dollar 同数同义，
    # 保留是因为契约 v1 已经放出去了；新调用方请直接用 effective_views_gt*。
    row["expected_value"] = caliber["effective_views_gt"]
    row["value_per_dollar"] = caliber["effective_views_gt_per_dollar"]
    return row


def _arms(plan: Any, baseline: Any, diversified: Any, row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """三条臂的花费与有效曝光（有效曝光按标注结果结算，两个口径都给）。"""
    out = [
        _arm_row("koxpilot", plan, row["koxpilot"]),
        _arm_row("follower_rank", baseline, row["baseline"]),
    ]
    if diversified is not None and row.get("diversified_no_gate"):
        out.append(_arm_row("diversified_no_gate", diversified, row["diversified_no_gate"]))
    return out


def _judge_block() -> Dict[str, Any]:
    """这组对比的口径声明：谁在判、按什么假设判、决策链路与裁判席怎么分开。

    为什么要把它放进响应体：``arms[]`` 里那几个数字是"谁更划算"的全部依据，
    读者必须能在同一份 JSON 里看出①判优用的是哪个字段、②裁判是标注还是引擎自评、
    ③读了标注的到底是哪一段代码。写在文档里不算——文档会和代码走散。
    """
    from koxpilot.eval.audit import (
        JUDGE_GROUND_TRUTH,
        LENIENT_VIEW_ASSUMPTION,
        MAIN_VIEW_ASSUMPTION,
    )

    return {
        "metric": "effective_views_gt_per_dollar",
        "judge": JUDGE_GROUND_TRUTH,
        "main_assumption": MAIN_VIEW_ASSUMPTION,
        "lenient_assumption": LENIENT_VIEW_ASSUMPTION,
        "engine_score_role": (
            "engine_value_per_dollar 是引擎的事前估分（选人排序依据），"
            "只作次级信息展示，不参与判优"
        ),
        "pipeline_separation": (
            "选人与分钱（A1–A5）只读可观测字段，读不到标注；"
            "只有 A6 审计这一组对比读标注（静态扫描 + 运行期哨兵在守这条边界）"
        ),
    }


# ---------------------------------------------------------------------------
# 预算放宽建议（契约 §4 的 advice）
# ---------------------------------------------------------------------------
#: 预算利用率低于这条线，才算「钱明显花不出去」，才需要给放宽建议。
#: 与前端结论块用的是同一条线（契约 §4 写明 60%），两边不允许各写一个数。
LOW_UTILIZATION_LINE = 0.60


def _usd(amount: float) -> str:
    return "$%s" % "{:,.0f}".format(amount)


def _advice_row(
    key: str,
    title: str,
    action: str,
    budget: float,
    base_spend: float,
    spendable: float,
    extra_picked: int,
    quality_note: str,
) -> Dict[str, Any]:
    """一条放宽建议：改什么、这次能多花出多少、名单质量会怎么变。

    ``spendable_usd`` 是**在这条放宽下重跑一遍 A2→A3→A5 得到的花费**，不是估算的上限；
    ``extra_spendable_usd`` 允许为负或 0（放宽了也没多花出钱），如实给出，不做截断——
    "这条放宽帮不上忙"本身就是一个有用的结论。
    """
    return {
        "key": key,
        "title": title,
        "action": action,
        "spendable_usd": round(spendable, 2),
        "extra_spendable_usd": round(spendable - base_spend, 2),
        "utilization_after": round(spendable / budget, 4) if budget else 0.0,
        "extra_picked": extra_picked,
        "quality_note": quality_note,
    }


def _advice(
    snap: Loaded,
    spec: Any,
    base_plan: Any,
    base_pool: List[Dict[str, Any]],
    base_results: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """预算没花完时，给三条**各自独立**的放宽建议，每条都真跑一遍再报数。

    三条建议对应三种不同性质的放宽，刻意分开而不是打包成一个"自动放宽"：
    1. 去掉年龄限定 —— 放宽人群定向（不动风控）；
    2. 扩到相邻市场 —— 放宽地域定向（相邻关系取 ``taxonomy.neighbor_markets``）；
    3. 让需人核档带折扣进清单 —— 放宽的是**采购动作**（折扣系数取
       ``budget.policy.REVIEW_SPEND_DISCOUNT``），门禁判定本身一个字都没改。

    利用率已经达到 :data:`LOW_UTILIZATION_LINE` 时返回空列表：钱花得出去就不需要建议。
    """
    import dataclasses

    from koxpilot.budget.planner import gate_results_for, plan_campaign
    from koxpilot.budget.policy import REVIEW_SPEND_DISCOUNT
    from koxpilot.taxonomy import neighbor_markets

    budget = float(base_plan.budget_usd or 0.0)
    base_spend = float(base_plan.spent_usd)
    if budget <= 0.0 or base_spend >= budget * LOW_UTILIZATION_LINE:
        return []

    picked_ids = set(a.kox_id for a in base_plan.selected)
    rows: List[Dict[str, Any]] = []

    def replan(relaxed_spec: Any) -> Any:
        """在放宽后的投放规格上重跑召回→门禁→分配（与主链路同一批函数、同样不截断召回）。"""
        pool = _recall(snap, relaxed_spec)
        results = gate_results_for(pool, relaxed_spec, snap.thresholds)
        plan, _ = plan_campaign(pool, relaxed_spec, snap.thresholds, results)
        return plan

    # ---- 1. 去掉年龄限定 ----
    ages = tuple(spec.target_age_buckets or ())
    if ages:
        plan_age = replan(dataclasses.replace(spec, target_age_buckets=()))
        newcomers = [a for a in plan_age.selected if a.kox_id not in picked_ids]
        rows.append(
            _advice_row(
                "relax_age",
                "去掉年龄段限定",
                "把人群里的「%s」这条限定去掉，平台 / 品类 / 市场与预算都不动" % "、".join(ages),
                budget,
                base_spend,
                float(plan_age.spent_usd),
                len(newcomers),
                (
                    "新进清单的 %d 人都是门禁判「可投」的人：这条放宽动的是人群定向，风控口径一条没改。"
                    % len(newcomers)
                )
                if newcomers
                else "清单不会变：卡住这笔预算的不是年龄限定。",
            )
        )

    # ---- 2. 扩到相邻市场 ----
    markets = tuple(spec.target_markets or ())
    expanded = neighbor_markets(markets) if markets else ()
    added = [m for m in expanded if m not in markets]
    if added:
        plan_mkt = replan(dataclasses.replace(spec, target_markets=expanded))
        newcomers = [a for a in plan_mkt.selected if a.kox_id not in picked_ids]
        outside = [a for a in newcomers if a.country not in markets]
        rows.append(
            _advice_row(
                "expand_markets",
                "加投相邻市场：%s" % "、".join(added),
                "目标市场从 %s 扩到 %s（相邻市场，语言与受众重叠度较高），其余定向不动"
                % ("、".join(markets), "、".join(expanded)),
                budget,
                base_spend,
                float(plan_mkt.spent_usd),
                len(newcomers),
                "新进清单的 %d 人同样逐条过了四层门禁，其中 %d 人的主市场在新增市场里——"
                "这批人触达的是相邻市场受众，是否算本次目标人群需要业务确认。"
                % (len(newcomers), len(outside)),
            )
        )

    # ---- 3. 需人核档带折扣进清单 ----
    n_review = sum(1 for r in base_results.values() if r.verdict == "review")
    if n_review:
        plan_rev, _ = plan_campaign(base_pool, spec, snap.thresholds, base_results, include_review=True)
        review_spend = sum(float(a.amount_usd) for a in plan_rev.selected if a.verdict == "review")
        other_spend = sum(float(a.amount_usd) for a in plan_rev.selected if a.verdict != "review")
        discounted = other_spend + review_spend * REVIEW_SPEND_DISCOUNT
        review_picked = sum(1 for a in plan_rev.selected if a.verdict == "review")
        rows.append(
            _advice_row(
                "discount_review",
                "让「需人核」的人带折扣进清单",
                "本次有 %d 人判为需人核；按单人金额 %d%% 先给试投位，人核通过后再追加到全额"
                % (n_review, int(round(REVIEW_SPEND_DISCOUNT * 100))),
                budget,
                base_spend,
                discounted,
                review_picked,
                "新增的 %d 人是需人核档：必须先人工核过再下单，%s 是打折后的试投金额，"
                "也就是这条建议的风险敞口上限。"
                % (review_picked, _usd(review_spend * REVIEW_SPEND_DISCOUNT)),
            )
        )

    rows.sort(key=lambda r: -float(r["extra_spendable_usd"]))
    return rows


# ---------------------------------------------------------------------------
# POST /api/plan
# ---------------------------------------------------------------------------
def plan(body: Dict[str, Any]) -> Dict[str, Any]:
    """契约 §4。全流程真算：A1 解析 → A2 召回 → A3 门禁 → A5 预算 → A6 反事实账。"""
    snap = _snapshot()
    options = body.get("options") or {}
    if not isinstance(options, dict):
        options = {}
    top_n = _clamp_int(options.get("top_n"), TOP_N_DEFAULT, TOP_N_MAX)
    explain_limit = _clamp_int(options.get("explain_limit"), EXPLAIN_LIMIT_DEFAULT, EXPLAIN_LIMIT_MAX)

    spec, brief_block, a1_ms, _notes = _resolve_brief(body, snap)  # notes 已随 brief_block 一起回

    from koxpilot.budget.planner import (
        gate_results_for,
        plan_baseline,
        plan_campaign,
        plan_diversified_no_gate,
    )
    from koxpilot.budget.policy import INCLUDE_REVIEW_BY_DEFAULT
    from koxpilot.budget.value import build_candidates
    from koxpilot.eval.audit import counterfactual_report

    # ---- A2 召回：命中定向的全部候选都进入计算，top_n 只管明细条数 ----
    t = time.time()
    pool = _recall(snap, spec)
    a2_ms = (time.time() - t) * 1000.0

    # ---- A3 门禁（不注入 LLM 适配分，与 CLI explain、浏览器引擎同口径）----
    t = time.time()
    results = gate_results_for(pool, spec, snap.thresholds)
    a3_ms = (time.time() - t) * 1000.0

    # ---- A4 语义适配：分值在 G2 里算出，这里只做归档与对外口径翻译 ----
    t = time.time()
    fit_by_id: Dict[str, Tuple[float, str]] = {}
    for kox_id, res in results.items():
        fit_by_id[kox_id] = (round(float(res.fit_score), 4), _fit_source(res.fit_source))
    a4_ms = (time.time() - t) * 1000.0

    # ---- A5 预算：三条臂共用同一批门禁结果 ----
    t = time.time()
    campaign_plan, _ = plan_campaign(pool, spec, snap.thresholds, results)
    baseline = plan_baseline(pool, spec, snap.thresholds, results)
    diversified = plan_diversified_no_gate(pool, spec, snap.thresholds, results)
    a5_ms = (time.time() - t) * 1000.0

    # ---- A6 反事实账：以 gt 为裁判，不用引擎自己的分 ----
    t = time.time()
    cid = spec.campaign_id
    report = counterfactual_report(pool, {cid: campaign_plan}, {cid: baseline}, {cid: diversified})
    a6_ms = (time.time() - t) * 1000.0
    row = report["per_campaign"][0]

    verdicts = ("pass", "review") if INCLUDE_REVIEW_BY_DEFAULT else ("pass",)
    candidates_raw, _skipped = build_candidates(pool, results, spec, snap.thresholds, verdicts)

    allocated: Dict[str, Any] = {a.kox_id: a for a in campaign_plan.selected}

    # ---- 候选行：先给进了清单的（按金额降序），再按判定档位与适配分排 ----
    rows: List[Dict[str, Any]] = []
    for kox_id in sorted(results.keys()):
        res = results[kox_id]
        kox = snap.by_id.get(kox_id) or {}
        alloc = allocated.get(kox_id)
        fit_score, fit_src = fit_by_id[kox_id]
        rows.append(
            {
                "kox_id": kox_id,
                "handle": str(kox.get("handle") or ""),
                "platform": str(kox.get("platform") or ""),
                "market": str(kox.get("country") or ""),
                "followers": int(kox.get("followers") or 0),
                "verdict": res.verdict,
                "gate_hits": _gate_hits(res),
                "fit_score": fit_score,
                "fit_source": fit_src,
                "reason_human": _reason_human(res),
                "allocated_usd": round(float(alloc.amount_usd), 2) if alloc else 0.0,
                "expected_reach": round(float(alloc.est_effective_views), 0) if alloc else 0.0,
            }
        )
    rows.sort(
        key=lambda r: (
            0 if r["allocated_usd"] > 0 else 1,
            -r["allocated_usd"],
            VERDICT_RANK.get(r["verdict"], 9),
            -r["fit_score"],
            r["kox_id"],
        )
    )

    n_pass = sum(1 for r in results.values() if r.verdict in verdicts)
    funnel = [
        {"stage": "recall", "label": "召回（命中定向的全部候选，计算不截断）", "count": len(pool)},
        {"stage": "gate", "label": "通过门禁", "count": n_pass},
        {"stage": "allocated", "label": "进入清单", "count": len(campaign_plan.selected)},
    ]

    remaining = max(0.0, float(campaign_plan.budget_usd) - float(campaign_plan.spent_usd))
    allocation = {
        "budget_usd": round(float(campaign_plan.budget_usd), 2),
        "allocated_usd": round(float(campaign_plan.spent_usd), 2),
        "unallocated_usd": round(remaining, 2),
        "unallocated_why": _unallocated_why(campaign_plan, candidates_raw, remaining),
        "picked": len(campaign_plan.selected),
        "arms": _arms(campaign_plan, baseline, diversified, row),
        # 契约 §4.1：这组对比的口径声明（判优字段 / 裁判来源 / 两个假设 / 链路分离）
        "judge": _judge_block(),
        "saved_usd": round(float(row["saved_usd"]), 2),
        "saved_share": round(float(row["saved_share_of_budget"]), 4),
    }

    # ---- 预算明显花不出去时，同一次请求里把三条放宽建议算出来 ----
    # 放在 plan 里而不是单开一个端点：读者看到"只花掉 2%"的下一个动作一定是"那怎么办"，
    # 让他为此再点一次、再等一次，是把工程分层的成本转嫁给使用者。
    # 前提是这个"花不完"必须是真的：召回不截断之后，低利用率只会来自定向本身太窄
    # （候选池里买不到能满足结构配额的库存），而不是被接口自己截出来的假象。
    advice = _advice(snap, spec, campaign_plan, pool, results)

    timings = [
        {"agent": "A1", "label": AGENT_LABEL["A1"], "ms": int(round(a1_ms))},
        {"agent": "A2", "label": AGENT_LABEL["A2"], "ms": int(round(a2_ms))},
        {"agent": "A3", "label": AGENT_LABEL["A3"], "ms": int(round(a3_ms))},
        {"agent": "A4", "label": "%s（分值在 G2 内算出）" % AGENT_LABEL["A4"], "ms": int(round(a4_ms))},
        {"agent": "A5", "label": AGENT_LABEL["A5"], "ms": int(round(a5_ms))},
        {"agent": "A6", "label": AGENT_LABEL["A6"], "ms": int(round(a6_ms))},
    ]

    # 明细条数 = min(explain_limit, top_n)：两者都只裁"看多少条"，一条都不裁计算。
    detail_rows = min(explain_limit, top_n)

    return {
        "ok": True,
        "brief": brief_block,
        "funnel": funnel,
        # 契约 §4.2：这次计算到底用了多少人、返回了多少条明细，写在响应体里而不是文档里。
        # 有了它，"服务与离线 CLI 是同一道题"这句话是可核对的，而不是一句承诺。
        "scope": {
            "recall_total": len(pool),
            "computed_on": len(pool),
            "detail_rows": min(detail_rows, len(rows)),
            "truncated_for_compute": False,
            "note": (
                "命中定向的 %d 人全部进入门禁与预算计算（召回不截断），"
                "与离线命令行对同一条需求跑出的方案是同一道题；"
                "请求里的两个条数上限只决定返回多少条候选明细，不影响分配结果。"
                % len(pool)
            ),
        },
        "candidates": rows[:detail_rows],
        "allocation": allocation,
        # 契约 §4：预算利用率低于 60% 时给出的放宽建议；花得出去时为空数组
        "advice": advice,
        "timings": timings,
        # 契约 §4：本次召回集合内**每一条**的判定，按 kox_id 升序，供前端逐条比对。
        # 必须覆盖**参与计算的全部候选**：前端拿这批 id 交给浏览器 TS 引擎重算，
        # 少给一条，两侧算的就不是同一道题，页面上的金额也会和服务返回的对不上。
        "parity_payload": {
            "verdicts": [
                {"kox_id": kox_id, "verdict": results[kox_id].verdict} for kox_id in sorted(results.keys())
            ]
        },
    }


# ---------------------------------------------------------------------------
# GET /api/kox/{kox_id}/explain
# ---------------------------------------------------------------------------
def explain(kox_id: str) -> Dict[str, Any]:
    """契约 §5。与 CLI ``koxpilot explain``（无 --campaign）同口径：默认中性 spec。"""
    snap = _snapshot()
    kox = snap.by_id.get(str(kox_id))
    if kox is None:
        raise ServiceError("not_found", "达人库里没有这个 id：%s" % kox_id)

    from koxpilot.gates.engine import evaluate
    from koxpilot.gates.policy import SIGNAL_QUANTILES
    from koxpilot.gates.signals import OBSERVABLE_SIGNALS, extract_signals
    from koxpilot.types import CampaignSpec

    result = evaluate(kox, CampaignSpec(), snap.thresholds)
    view = extract_signals(kox)

    # 每条信号被哪条规则判过：有命中就用那条规则给出的阈值，口径与证据链完全一致
    hit_by_signal: Dict[str, Any] = {}
    for reason in result.reasons:
        if reason.signal not in hit_by_signal:
            hit_by_signal[reason.signal] = reason

    signals: List[Dict[str, Any]] = []
    for name in OBSERVABLE_SIGNALS:
        raw = view.get(name)
        value = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
        reason = hit_by_signal.get(name)
        if reason is not None and isinstance(reason.threshold, (int, float)):
            threshold = float(reason.threshold)
            verdict = SIGNAL_OUT_OF_RANGE
        else:
            # 没命中时给出同组参考阈值（该信号标定的最高分位，即上尾判定线）
            quantiles = SIGNAL_QUANTILES.get(name, (0.5,))
            label = "p%02d" % int(round(max(quantiles) * 100))
            threshold = snap.thresholds.value(result.group_key, name, label)
            if threshold is None:
                verdict = SIGNAL_NO_THRESHOLD
            elif value is not None and value > threshold:
                # 值在尾部但规则没判它：该规则还要求别的条件（例如 G1.5 要求增长断层）
                verdict = SIGNAL_ABOVE_REFERENCE
            else:
                verdict = SIGNAL_IN_RANGE
        if value is None:
            verdict = SIGNAL_MISSING
        signals.append(
            {
                "label": signal_label(name),
                "value": round(value, 6) if value is not None else None,
                "threshold": round(threshold, 6) if threshold is not None else None,
                "verdict": verdict,
            }
        )

    hits = _gate_hits(result)
    gates = []
    for gate in ALL_GATES:
        gate_hits_rows = [h for h in hits if h["gate"] == gate]
        gates.append(
            {
                "gate": gate,
                "gate_label": GATE_LABEL[gate],
                "passed": not gate_hits_rows,
                "hits": gate_hits_rows,
            }
        )

    return {
        "ok": True,
        "kox_id": result.kox_id,
        "profile": {
            "handle": str(kox.get("handle") or ""),
            "platform": str(kox.get("platform") or ""),
            "market": str(kox.get("country") or ""),
            "followers": int(kox.get("followers") or 0),
        },
        "signals": signals,
        "gates": gates,
        "verdict": result.verdict,
        "reason_human": _reason_human(result),
    }


# ---------------------------------------------------------------------------
# POST /api/gate/batch
# ---------------------------------------------------------------------------
def gate_batch(body: Dict[str, Any]) -> Dict[str, Any]:
    """契约 §6：给一批 id 跑门禁，供前端把「它拿到的记录」交回后端复判。"""
    snap = _snapshot()
    raw = body.get("kox_ids")
    if raw is None or not isinstance(raw, list):
        raise ServiceError("bad_request", "kox_ids 必须是一个数组，例如 {\"kox_ids\": [\"KOX-000001\"]}。")
    if len(raw) > GATE_BATCH_LIMIT:
        raise ServiceError(
            "too_many",
            "一次最多判 %d 人，本次收到 %d 人：请分批。" % (GATE_BATCH_LIMIT, len(raw)),
        )

    from koxpilot.gates.engine import evaluate
    from koxpilot.types import CampaignSpec

    spec = CampaignSpec()
    verdicts: List[Dict[str, Any]] = []
    missing: List[str] = []
    for item in raw:
        kox_id = str(item)
        kox = snap.by_id.get(kox_id)
        if kox is None:
            missing.append(kox_id)
            continue
        verdicts.append({"kox_id": kox_id, "verdict": evaluate(kox, spec, snap.thresholds).verdict})
    if missing and not verdicts:
        raise ServiceError(
            "not_found",
            "这些 id 在达人库里都不存在：%s%s"
            % ("、".join(missing[:5]), " 等 %d 个" % len(missing) if len(missing) > 5 else ""),
        )
    verdicts.sort(key=lambda v: v["kox_id"])
    return {"ok": True, "verdicts": verdicts}


__all__ = [
    "GATE_BATCH_LIMIT",
    "ServiceError",
    "explain",
    "gate_batch",
    "plan",
]
