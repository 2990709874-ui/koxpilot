"""预算分配器（SPEC 第 5 节）：value/cost 贪心 + 分层配额修正。

采购模型：**按条买**
--------------------
决策变量不是"选不选这个人"，而是"在这个人身上买几条内容"（0..MAX_POSTS_PER_KOX）。
第 n 条的边际价值按 ``POST_MARGINAL_DECAY^(n-1)`` 衰减（受众重叠导致净增触达递减）。

由此得到一个**性质很好**的贪心：把所有 (达人, 第 n 条) 展开成"名额"（slot），
每个名额的边际性价比 = 边际价值 / 单条报价。由于同一达人的边际性价比随 n 严格递减，
按边际性价比全局降序取名额时，**第 n 条一定排在第 n-1 条之后**，
所以不需要额外维护"必须先买第一条"的约束——这是凹效用下贪心最优的经典结构。
（若价值函数非凹，这个贪心就没有这个保证，所以衰减系数必须 ≤1，见 policy 的断言。）

为什么不用求解器
----------------
可解释性优先：采购要能对每个"为什么选它/为什么没选它/为什么只买一条"给出人话，
每一步都会落进 ``plan.trace``。代价是不保证全局最优，这一点在报告里明说，不含糊。

两条算法纪律
------------
1. **确定性**：排序键一律带 ``kox_id`` 与名额序号做最终 tie-break。
2. **修正阶段可终止**：加名额只发生在"还有未取名额"，减名额只发生在"加不进去了"，
   两个方向都单调消耗有限集合，另有 MAX_REPAIR_STEPS 兜底。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..taxonomy import HEAD_BUCKETS, LONGTAIL_BUCKETS
from ..types import Allocation, BudgetPlan, CampaignSpec
from .policy import (
    COUNTRY_MAX_SHARE,
    HEAD_MAX_SHARE,
    LONGTAIL_MIN_SHARE,
    MAX_POSTS_PER_KOX,
    MAX_REPAIR_ROUNDS,
    MAX_REPAIR_STEPS,
    MIN_UTILIZATION_TARGET,
    POST_MARGINAL_DECAY,
    SINGLE_KOX_MAX_SHARE,
    constraint_snapshot,
    effective_posts,
)
from .value import Candidate

__all__ = [
    "BASIS_VALUE",
    "BASIS_VIEWS",
    "STRATEGIES",
    "Slot",
    "Selection",
    "allocate",
    "validate_plan",
    "build_slots",
]

#: 排序依据："质量加权价值"（引擎自己的 value(k)，含真实性折扣/语义适配/KPI 权重）
BASIS_VALUE = "value"
#: 排序依据："名义曝光"（只用 avg_views，**不含任何门禁/质量信号**）。
#: 第三臂 diversified_no_gate 用它，这样"分散化"这条对照臂不会偷用门禁的判断。
BASIS_VIEWS = "views"

#: 三条臂的采购规则：strategy -> (是否强制结构约束, 排序依据)
#:
#: - ``followers``：反事实基线，粉丝量降序、只受总预算约束（行业最朴素做法）；
#: - ``diversified_no_gate``：**只加结构分散化**（分层/地域/单人配额），排序依据是
#:   "每美元买到的名义曝光"，**不看门禁判定、不看真实性折扣**；
#: - ``koxpilot``：结构约束 + 质量加权价值排序 + 门禁过滤（过滤在 planner 层完成）。
STRATEGIES: dict[str, tuple[bool, str]] = {
    "followers": (False, BASIS_VIEWS),
    "diversified_no_gate": (True, BASIS_VIEWS),
    "koxpilot": (True, BASIS_VALUE),
}


@dataclass(frozen=True, slots=True)
class Slot:
    """一个采购名额 = 在某达人身上买的第 ``index`` 条内容（index 从 1 开始）。

    ``basis`` 决定边际价值用哪把尺子量：``BASIS_VALUE`` 是引擎的质量加权价值，
    ``BASIS_VIEWS`` 只用名义曝光。两把尺子共用同一套衰减与贪心结构，
    所以"换尺子"这件事本身不会改变算法性质，只改变排序依据——
    这正是第三臂要隔离的变量。
    """

    candidate: Candidate
    index: int
    decay: float = POST_MARGINAL_DECAY
    basis: str = BASIS_VALUE

    @property
    def kox_id(self) -> str:
        return self.candidate.kox_id

    @property
    def cost_usd(self) -> float:
        return self.candidate.cost_usd

    @property
    def base_value(self) -> float:
        """第 1 条内容的价值（未衰减），按 ``basis`` 取质量加权价值或名义曝光。"""
        if self.basis == BASIS_VIEWS:
            return self.candidate.avg_views
        return self.candidate.value

    @property
    def marginal_value(self) -> float:
        return self.base_value * (self.decay ** (self.index - 1))

    @property
    def marginal_efficiency(self) -> float:
        return self.marginal_value / self.candidate.cost_usd


def build_slots(
    pool: Iterable[Candidate],
    max_posts: int = MAX_POSTS_PER_KOX,
    decay: float = POST_MARGINAL_DECAY,
    basis: str = BASIS_VALUE,
) -> list[Slot]:
    """把候选池展开成名额列表。"""
    if not 0.0 < decay <= 1.0:
        raise ValueError("POST_MARGINAL_DECAY 必须落在 (0,1]，否则贪心的凹性前提不成立")
    if basis not in (BASIS_VALUE, BASIS_VIEWS):
        raise ValueError(f"未知的排序依据 basis={basis!r}")
    return [Slot(c, i, decay, basis) for c in pool for i in range(1, max(1, max_posts) + 1)]


@dataclass(slots=True)
class Selection:
    """选择集的增量状态。所有份额 O(1) 可读，避免每步重算全表。"""

    budget: float
    decay: float = POST_MARGINAL_DECAY
    basis: str = BASIS_VALUE
    posts: dict[str, int] = field(default_factory=dict)
    cand_by_id: dict[str, Candidate] = field(default_factory=dict)
    picked_by: dict[str, str] = field(default_factory=dict)
    spent: float = 0.0
    head_usd: float = 0.0
    longtail_usd: float = 0.0
    country_usd: dict[str, float] = field(default_factory=dict)

    # -- 变更 ---------------------------------------------------------------
    def take(self, slot: Slot, tag: str) -> None:
        cand = slot.candidate
        kid = cand.kox_id
        self.posts[kid] = self.posts.get(kid, 0) + 1
        self.cand_by_id[kid] = cand
        self.picked_by.setdefault(kid, tag)
        cost = slot.cost_usd
        self.spent += cost
        if cand.bucket in HEAD_BUCKETS:
            self.head_usd += cost
        if cand.bucket in LONGTAIL_BUCKETS:
            self.longtail_usd += cost
        self.country_usd[cand.country] = self.country_usd.get(cand.country, 0.0) + cost

    def give_back(self, kox_id: str) -> Candidate:
        """退掉该达人的**最后一条**内容（边际价值最低的那条）。"""
        cand = self.cand_by_id[kox_id]
        cost = cand.cost_usd
        self.posts[kox_id] -= 1
        if self.posts[kox_id] <= 0:
            self.posts.pop(kox_id)
            self.cand_by_id.pop(kox_id)
            self.picked_by.pop(kox_id, None)
        self.spent -= cost
        if cand.bucket in HEAD_BUCKETS:
            self.head_usd -= cost
        if cand.bucket in LONGTAIL_BUCKETS:
            self.longtail_usd -= cost
        self.country_usd[cand.country] = self.country_usd.get(cand.country, 0.0) - cost
        return cand

    # -- 读取 ---------------------------------------------------------------
    def n_posts(self, kox_id: str) -> int:
        return self.posts.get(kox_id, 0)

    def amount_of(self, kox_id: str) -> float:
        return self.posts.get(kox_id, 0) * self.cand_by_id[kox_id].cost_usd

    @property
    def chosen(self) -> dict[str, Candidate]:
        return self.cand_by_id

    @property
    def remaining(self) -> float:
        return self.budget - self.spent

    def share(self, amount: float) -> float:
        return amount / self.spent if self.spent > 0 else 0.0

    @property
    def head_share(self) -> float:
        return self.share(self.head_usd)

    @property
    def longtail_share(self) -> float:
        return self.share(self.longtail_usd)

    @property
    def max_country_share(self) -> float:
        return max((self.share(v) for v in self.country_usd.values()), default=0.0)

    def marginal_efficiency_of_last(self, kox_id: str) -> float:
        """该达人当前最后一条内容的边际性价比（退让时优先退这个值最低的）。

        用的是本臂自己的 ``basis``：第三臂不看质量加权价值，退让时也不许偷看，
        否则"分散化贡献"里就混进了门禁的判断力。
        """
        n = self.posts.get(kox_id, 0)
        if n <= 0:
            return 0.0
        return Slot(self.cand_by_id[kox_id], n, self.decay, self.basis).marginal_efficiency

    # -- 快照 / 回滚 ---------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """浅拷贝全部增量状态，供修正阶段试探性退让后回滚。"""
        return {
            "posts": dict(self.posts),
            "cand_by_id": dict(self.cand_by_id),
            "picked_by": dict(self.picked_by),
            "spent": self.spent,
            "head_usd": self.head_usd,
            "longtail_usd": self.longtail_usd,
            "country_usd": dict(self.country_usd),
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.posts = dict(snap["posts"])
        self.cand_by_id = dict(snap["cand_by_id"])
        self.picked_by = dict(snap["picked_by"])
        self.spent = float(snap["spent"])
        self.head_usd = float(snap["head_usd"])
        self.longtail_usd = float(snap["longtail_usd"])
        self.country_usd = dict(snap["country_usd"])


def _slot_key_efficiency(slot: Slot) -> tuple[float, float, str, int]:
    """边际性价比降序；同值时先要边际价值大的，最后按 id/序号定序保证确定性。"""
    return (-slot.marginal_efficiency, -slot.marginal_value, slot.kox_id, slot.index)


def _slot_key_followers(slot: Slot) -> tuple[float, int, str]:
    """基线：粉丝量降序（同一人的多条按序号，行业最朴素做法）。"""
    return (-slot.candidate.followers, slot.index, slot.kox_id)


def _fits_caps_vs_budget(sel: Selection, slot: Slot) -> str | None:
    """贪心阶段可行性检查（分母 = 总预算，单调、与选择顺序无关）。"""
    cand = slot.candidate
    cost = slot.cost_usd
    if cost > sel.remaining:
        return "over_remaining_budget"
    if sel.n_posts(cand.kox_id) * cost + cost > SINGLE_KOX_MAX_SHARE * sel.budget:
        return "over_single_kox_cap"
    if cand.bucket in HEAD_BUCKETS and sel.head_usd + cost > HEAD_MAX_SHARE * sel.budget:
        return "over_head_cap"
    if sel.country_usd.get(cand.country, 0.0) + cost > COUNTRY_MAX_SHARE * sel.budget:
        return "over_country_cap"
    return None


def _longtail_target(sel: Selection) -> float:
    """长尾下限的**单调化**目标份额。

    直接用固定下限做"加入后必须 ≥25%"的检查有个陷阱：一旦当前份额已经低于 25%
    （例如刚被地域修正退掉了一批长尾内容），任何加入都不满足该式，补位阶段会被整体冻死，
    预算利用率白掉十几个百分点。正确语义是"**不许把情况变得更糟**"：
    已达标时守住 25%，未达标时至少不能让份额继续下降。
    """
    if sel.spent <= 0:
        return 0.0
    return min(LONGTAIL_MIN_SHARE, sel.longtail_share)


def _fits_caps_vs_spend(sel: Selection, slot: Slot) -> str | None:
    """补位阶段可行性检查（分母 = 加入后的实际花费，即最终报告口径）。"""
    cand = slot.candidate
    cost = slot.cost_usd
    if cost > sel.remaining:
        return "over_remaining_budget"
    if sel.n_posts(cand.kox_id) * cost + cost > SINGLE_KOX_MAX_SHARE * sel.budget:
        return "over_single_kox_cap"
    after = sel.spent + cost
    head = sel.head_usd + (cost if cand.bucket in HEAD_BUCKETS else 0.0)
    if head > HEAD_MAX_SHARE * after:
        return "over_head_cap"
    longtail = sel.longtail_usd + (cost if cand.bucket in LONGTAIL_BUCKETS else 0.0)
    if longtail < _longtail_target(sel) * after - 1e-9:
        return "would_worsen_longtail_share"
    country = sel.country_usd.get(cand.country, 0.0) + cost
    if country > COUNTRY_MAX_SHARE * after:
        return "over_country_cap"
    return None


def _fits_for_longtail_repair(sel: Selection, slot: Slot) -> str | None:
    """长尾补足阶段的检查：只看预算、单人上限、地域上限。

    刻意**不看**长尾下限本身（我们正是在往下限爬），也不看头部上限（长尾不影响头部）。
    地域上限按加入后的实际花费算，避免"补长尾把国家占比重新顶破"的反复。
    """
    cand = slot.candidate
    cost = slot.cost_usd
    if cost > sel.remaining:
        return "over_remaining_budget"
    if sel.n_posts(cand.kox_id) * cost + cost > SINGLE_KOX_MAX_SHARE * sel.budget:
        return "over_single_kox_cap"
    if sel.country_usd.get(cand.country, 0.0) + cost > COUNTRY_MAX_SHARE * (sel.spent + cost):
        return "over_country_cap"
    return None


def _repair_longtail(sel: Selection, slots: Sequence[Slot], trace: list[str]) -> None:
    """分层配额修正：把长尾金额占比抬到 LONGTAIL_MIN_SHARE 以上。

    优先"加长尾名额"（不损失已选价值），加不动了才"退掉性价比最低的非长尾名额"腾预算。
    """
    longtail_slots = [s for s in slots if s.candidate.bucket in LONGTAIL_BUCKETS]
    if not longtail_slots:
        # 池子里根本没有长尾候选：退掉非长尾内容永远不可能把长尾占比抬起来，
        # 只会把预算白扔掉（极端情况下把整盘方案退成 $0，还让报告里的比例型约束
        # 因为"分母归零"而显示为满足）。这里直接停手并如实记账。
        if sel.longtail_share < LONGTAIL_MIN_SHARE:
            trace.append(
                f"分层修正：该定向下候选池里没有任何长尾（nano/micro）达人，"
                f"长尾下限 {LONGTAIL_MIN_SHARE:.0%} 在物理上不可满足；"
                f"不做无意义的退让（退让只会损失预算而不改善结构），如实记录违规"
            )
        return
    steps = 0
    added_total = 0
    while sel.spent > 0 and sel.longtail_share < LONGTAIL_MIN_SHARE and steps < MAX_REPAIR_STEPS:
        steps += 1
        added = False
        for slot in longtail_slots:
            cand = slot.candidate
            if sel.n_posts(cand.kox_id) >= slot.index:  # 该名额已被取走
                continue
            if sel.n_posts(cand.kox_id) + 1 != slot.index:  # 保持"先买第 n-1 条"
                continue
            if _fits_for_longtail_repair(sel, slot) is not None:
                continue
            sel.take(slot, "repair_longtail")
            added = True
            added_total += 1
            break
        if added:
            continue
        droppable = [k for k, c in sel.chosen.items() if c.bucket not in LONGTAIL_BUCKETS]
        if not droppable:
            trace.append(
                f"分层修正：长尾候选与可退让名额都已用尽，长尾占比停在 {sel.longtail_share:.1%}"
                f"（低于 {LONGTAIL_MIN_SHARE:.0%} 下限，如实记录，不假装满足）"
            )
            break
        worst = min(droppable, key=lambda k: (sel.marginal_efficiency_of_last(k), k))
        before_share = sel.longtail_share
        snap = sel.snapshot()
        cand = sel.give_back(worst)
        if sel.spent <= 0 or sel.longtail_share <= before_share + 1e-12:
            # 退让没有换来任何结构改善（腾出的预算加不进长尾），继续退下去就是纯毁灭价值。
            sel.restore(snap)
            trace.append(
                f"分层修正：退让已无法改善长尾占比（停在 {sel.longtail_share:.1%}，"
                f"下限 {LONGTAIL_MIN_SHARE:.0%}），停止退让并如实记录违规"
            )
            break
        trace.append(
            f"分层修正：退掉非长尾达人 {cand.handle}（{cand.bucket}）的 1 条内容"
            f"（${cand.cost_usd:,.0f}）以腾出长尾名额"
        )
    if added_total:
        trace.append(
            f"分层修正：追加 {added_total} 条长尾内容，长尾金额占比抬到 {sel.longtail_share:.1%}"
            f"（下限 {LONGTAIL_MIN_SHARE:.0%}）"
        )


def _repair_head(sel: Selection, trace: list[str]) -> None:
    """头部占比修正（分母换成实际花费后可能越线）：退掉边际性价比最低的头部内容。"""
    steps = 0
    dropped = 0
    while sel.spent > 0 and sel.head_share > HEAD_MAX_SHARE and steps < MAX_REPAIR_STEPS:
        steps += 1
        heads = [k for k, c in sel.chosen.items() if c.bucket in HEAD_BUCKETS]
        if not heads:
            return
        worst = min(heads, key=lambda k: (sel.marginal_efficiency_of_last(k), k))
        before_share = sel.head_share
        snap = sel.snapshot()
        sel.give_back(worst)
        if sel.spent <= 0 or sel.head_share >= before_share - 1e-12:
            # 头部占比 = 头部金额 / 总花费；当在选内容**全是头部**时，
            # 退让不改变这个比值（H/T 恒为 1），循环只会一路退到 $0，
            # 然后因为分母归零让 head_max_share 在报告里"满足"——那是用不投放伪装合规。
            sel.restore(snap)
            trace.append(
                f"头部配额修正：在选内容全部来自头部（macro/mega），"
                f"退让无法降低头部占比（停在 {sel.head_share:.1%}，上限 {HEAD_MAX_SHARE:.0%}）；"
                f"停止退让并如实记录违规"
            )
            break
        dropped += 1
    if dropped:
        trace.append(
            f"头部配额修正：退掉 {dropped} 条头部内容，头部金额占比降至 {sel.head_share:.1%}"
            f"（上限 {HEAD_MAX_SHARE:.0%}）"
        )


def _repair_country(sel: Selection, trace: list[str]) -> None:
    """地域分散修正：退掉超配国家里边际性价比最低的内容。"""
    steps = 0
    dropped: dict[str, int] = {}
    while sel.spent > 0 and sel.max_country_share > COUNTRY_MAX_SHARE and steps < MAX_REPAIR_STEPS:
        steps += 1
        country = max(sel.country_usd.items(), key=lambda kv: (kv[1], kv[0]))[0]
        pool = [k for k, c in sel.chosen.items() if c.country == country]
        if not pool:
            return
        worst = min(pool, key=lambda k: (sel.marginal_efficiency_of_last(k), k))
        before_share = sel.max_country_share
        snap = sel.snapshot()
        sel.give_back(worst)
        if sel.spent <= 0 or sel.max_country_share >= before_share - 1e-12:
            # 单一国家占比 = 该国金额 / 总花费；若在选内容只来自一个国家，
            # 这个比值恒为 1，退让改善不了任何东西，退到最后是整盘清零 + 报告假合规。
            # 单国候选池（例如只投美国的 brief）会稳定命中这一支。
            sel.restore(snap)
            trace.append(
                f"地域分散修正：在选内容集中在单一国家，退让无法降低占比"
                f"（停在 {sel.max_country_share:.1%}，上限 {COUNTRY_MAX_SHARE:.0%}）；"
                f"停止退让并如实记录违规"
            )
            break
        dropped[country] = dropped.get(country, 0) + 1
    for country, n in dropped.items():
        trace.append(
            f"地域分散修正：退掉 {country} 的 {n} 条内容，单一国家占比降至 {sel.max_country_share:.1%}"
            f"（上限 {COUNTRY_MAX_SHARE:.0%}）"
        )


def _structure_ok(sel: Selection) -> bool:
    return not _structure_violations(sel)


def _structure_violations(sel: Selection) -> list[str]:
    """当前状态下未满足的结构约束名（与 validate_plan 的判定口径完全一致）。"""
    bad: list[str] = []
    if sel.head_share > HEAD_MAX_SHARE + 1e-6:
        bad.append("head_max_share")
    if sel.longtail_share < LONGTAIL_MIN_SHARE - 1e-6:
        bad.append("longtail_min_share")
    if sel.max_country_share > COUNTRY_MAX_SHARE + 1e-6:
        bad.append("country_max_share")
    return bad


def _repair_score(sel: Selection) -> tuple[int, float]:
    """修正效果打分，**越小越好**：(未满足的结构约束数, -已花费)。

    关键的一条特例：空方案（花费 0）永远是最差的。
    因为比例型约束的分母是"实际花费"，把方案退成 $0 会让 head/country 占比变成 0/0 → 0.0，
    在报告里显示为"满足"。那不是合规，那是**用不投放伪装合规**，
    还会把违规指向错误的约束（只剩长尾下限报红），让运营看不到真正卡住的是哪一条。
    """
    if sel.spent <= 0:
        return (len(_structure_violations(sel)) + 99, 0.0)
    return (len(_structure_violations(sel)), -sel.spent)


def _repair_to_fixpoint(sel: Selection, ordered: Sequence[Slot], trace: list[str]) -> bool:
    """多轮迭代修正到不动点。

    为什么必须多轮：三条结构约束会互相打架 —— 退掉超配国家的内容时很可能同时退掉了长尾，
    把刚补好的长尾占比又打下去。单趟顺序执行只能保证最后一条约束成立。
    这里的顺序是"先做上限类（只减钱，单调收敛），再做下限类（补钱）"，
    并在整体不再变化时停止，未达标就如实记账。

    另有一条安全网：**修正不允许把方案变得更差**。若某条配额在该候选池下不可满足，
    退让会一路毁灭价值（最坏退成 $0 并让报告假合规），此时回滚到修正前的方案，
    如实登记违规——宁可交付一个"结构不达标但说清楚了"的方案，
    也不交付一个"什么都没投所以没违规"的空方案。
    """
    baseline = sel.snapshot()
    base_score = _repair_score(sel)
    local: list[str] = []
    for _ in range(MAX_REPAIR_ROUNDS):
        if _structure_ok(sel):
            trace.extend(local)
            return True
        before = (round(sel.spent, 6), tuple(sorted(sel.posts.items())))
        _repair_country(sel, local)
        _repair_head(sel, local)
        _repair_longtail(sel, ordered, local)
        after = (round(sel.spent, 6), tuple(sorted(sel.posts.items())))
        if after == before:
            break
    ok = _structure_ok(sel)
    if ok:
        trace.extend(local)
        return True
    if _repair_score(sel) > base_score:
        sel.restore(baseline)
        trace.append(
            "结构修正在该候选池下无法达标，且退让反而在毁灭已选价值；"
            "已回滚到修正前的方案，并在 constraints.violations 中如实标出未满足的配额"
            "（绝不用「退成空方案」来让比例型约束的分母归零、伪装成合规）"
        )
        return False
    trace.extend(local)
    trace.append(
        "结构修正未能同时满足全部配额（候选池在该定向下的结构性不足），"
        "已在 constraints.violations 中如实标出，未做任何掩盖"
    )
    return False


def validate_plan(sel: Selection, strict: bool) -> dict[str, Any]:
    """把每条约束的实际值/上下限/是否满足写成可断言的报告。

    Args:
        strict: True = KOXPilot 臂（全部结构约束生效）；
                False = 基线臂（只有总预算约束，其余仅**记录**不判定——
                这正是"基线为什么会翻车"的证据）。
    """
    max_single = max(
        (sel.amount_of(k) for k in sel.chosen),
        default=0.0,
    )
    checks: list[dict[str, Any]] = [
        {
            "name": "total_budget",
            "desc": "总额 ≤ 预算",
            "actual": round(sel.spent, 2),
            "limit": round(sel.budget, 2),
            "satisfied": sel.spent <= sel.budget + 1e-6,
            "enforced": True,
        },
        {
            "name": "single_kox_max_share",
            "desc": f"单达人金额 ≤ 预算的 {SINGLE_KOX_MAX_SHARE:.0%}",
            "actual": round(max_single / sel.budget, 4) if sel.budget else 0.0,
            "limit": SINGLE_KOX_MAX_SHARE,
            "satisfied": max_single <= SINGLE_KOX_MAX_SHARE * sel.budget + 1e-6,
            "enforced": strict,
        },
        {
            "name": "head_max_share",
            "desc": f"macro+mega 金额占比 ≤ {HEAD_MAX_SHARE:.0%}",
            "actual": round(sel.head_share, 4),
            "limit": HEAD_MAX_SHARE,
            "satisfied": sel.head_share <= HEAD_MAX_SHARE + 1e-6,
            "enforced": strict,
        },
        {
            "name": "longtail_min_share",
            "desc": f"nano+micro 金额占比 ≥ {LONGTAIL_MIN_SHARE:.0%}",
            "actual": round(sel.longtail_share, 4),
            "limit": LONGTAIL_MIN_SHARE,
            "satisfied": sel.longtail_share >= LONGTAIL_MIN_SHARE - 1e-6,
            "enforced": strict,
        },
        {
            "name": "country_max_share",
            "desc": f"单一国家金额占比 ≤ {COUNTRY_MAX_SHARE:.0%}",
            "actual": round(sel.max_country_share, 4),
            "limit": COUNTRY_MAX_SHARE,
            "satisfied": sel.max_country_share <= COUNTRY_MAX_SHARE + 1e-6,
            "enforced": strict,
        },
    ]
    utilization = sel.spent / sel.budget if sel.budget else 0.0
    return {
        "policy": constraint_snapshot(),
        "checks": checks,
        "all_enforced_satisfied": all(c["satisfied"] for c in checks if c["enforced"]),
        "violations": [c["name"] for c in checks if c["enforced"] and not c["satisfied"]],
        "utilization": round(utilization, 4),
        "utilization_ok": utilization >= MIN_UTILIZATION_TARGET,
    }


def _mix(sel: Selection, key: str) -> dict[str, float]:
    agg: dict[str, float] = {}
    for kid, c in sel.chosen.items():
        agg[getattr(c, key)] = agg.get(getattr(c, key), 0.0) + sel.amount_of(kid)
    return {k: round(sel.share(v), 6) for k, v in sorted(agg.items(), key=lambda kv: -kv[1])}


def allocate(
    candidates: Iterable[Candidate],
    spec: CampaignSpec,
    strategy: str = "koxpilot",
    budget_usd: float | None = None,
    skipped: Sequence[dict[str, Any]] = (),
    max_posts: int = MAX_POSTS_PER_KOX,
    decay: float = POST_MARGINAL_DECAY,
) -> BudgetPlan:
    """把候选池分配成预算方案。

    Args:
        strategy: 见 ``STRATEGIES``——
                  ``"koxpilot"`` = 质量加权价值的边际性价比贪心 + 结构约束修正；
                  ``"diversified_no_gate"`` = 第三臂，同一套结构约束，但排序依据换成
                  "每美元名义曝光"，且候选池不做门禁过滤（过滤由 planner 决定）；
                  ``"followers"`` = 反事实基线（粉丝量降序、只受总预算约束、不看门禁）。
        max_posts / decay: 采购模型参数（见 budget/policy.py 的推导与敏感性说明）。
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"未知的分配策略 strategy={strategy!r}，可选：{sorted(STRATEGIES)}")
    strict, basis = STRATEGIES[strategy]
    pool = list(candidates)
    budget = float(budget_usd if budget_usd is not None else spec.budget_usd)
    sel = Selection(budget=budget, decay=decay, basis=basis)
    trace: list[str] = []
    slots = build_slots(pool, max_posts, decay, basis)

    if budget <= 0 or not pool:
        return BudgetPlan(
            campaign_id=spec.campaign_id,
            budget_usd=budget,
            spent_usd=0.0,
            candidate_pool=len(pool),
            constraints=validate_plan(sel, strict),
            skipped=list(skipped),
            trace=["预算为 0 或候选池为空，未做任何分配"],
            strategy=strategy,
        )

    if strict:
        ordered = sorted(slots, key=_slot_key_efficiency)
        ruler = (
            "质量加权价值/报价"
            if basis == BASIS_VALUE
            else "名义曝光/报价（不含门禁与真实性信号）"
        )
        for slot in ordered:
            if _fits_caps_vs_budget(sel, slot) is None:
                sel.take(slot, "greedy")
        trace.append(
            f"边际性价比贪心（排序依据：{ruler}）：候选 {len(pool)} 人 / {len(slots)} 个内容名额，"
            f"选入 {len(sel.chosen)} 人共 {sum(sel.posts.values())} 条，花费 ${sel.spent:,.0f}"
            f"（长尾 {sel.longtail_share:.1%}，头部 {sel.head_share:.1%}）"
        )
        _repair_to_fixpoint(sel, ordered, trace)
        filled = 0
        for slot in ordered:  # 修正后可能腾出预算，按边际性价比补位（补位不得破坏任何约束）
            if sel.n_posts(slot.kox_id) + 1 != slot.index:
                continue
            if _fits_caps_vs_spend(sel, slot) is None:
                sel.take(slot, "fill")
                filled += 1
        if filled:
            trace.append(
                f"约束内补位：追加 {filled} 条内容，预算利用率 {sel.spent / budget:.1%}"
            )
        _repair_to_fixpoint(sel, ordered, trace)  # 补位后再确认一次不动点
    else:
        for slot in sorted(slots, key=_slot_key_followers):
            if sel.n_posts(slot.kox_id) + 1 != slot.index:
                continue
            if slot.cost_usd <= sel.remaining:
                sel.take(slot, "baseline")
        trace.append(
            f"基线策略：按粉丝量降序买内容直到花完预算，共 {len(sel.chosen)} 人 "
            f"{sum(sel.posts.values())} 条，花费 ${sel.spent:,.0f}"
            f"（不做门禁过滤、不做结构约束）"
        )

    allocations: list[Allocation] = []
    for kid, c in sel.chosen.items():
        n = sel.posts[kid]
        eq = effective_posts(n, decay)
        allocations.append(
            Allocation(
                kox_id=c.kox_id,
                handle=c.handle,
                platform=c.platform,
                country=c.country,
                bucket=c.bucket,
                amount_usd=n * c.cost_usd,
                posts=n,
                effective_posts=round(eq, 4),
                price_estimated=c.price_estimated,
                est_views=c.avg_views * eq,
                est_effective_views=c.avg_views * eq * c.authenticity_discount,
                est_engagements=c.est_engagements * eq,
                value_score=c.value * eq,
                efficiency=c.efficiency,
                fit_score=c.fit_score,
                authenticity_discount=c.authenticity_discount,
                audience_match=c.audience_match,
                kpi_weight=c.kpi_weight,
                verdict=c.verdict,
                picked_by=sel.picked_by.get(kid, "greedy"),
            )
        )
    allocations.sort(key=lambda a: (-a.value_score, a.kox_id))

    views = sum(a.est_views for a in allocations)
    eff_views = sum(a.est_effective_views for a in allocations)
    engagements = sum(a.est_engagements for a in allocations)
    return BudgetPlan(
        campaign_id=spec.campaign_id,
        budget_usd=budget,
        spent_usd=sel.spent,
        selected=allocations,
        est_total_views=views,
        est_effective_views=eff_views,
        est_total_engagements=engagements,
        est_cpm_usd=(sel.spent / (views / 1000.0)) if views > 0 else 0.0,
        est_cpe_usd=(sel.spent / engagements) if engagements > 0 else 0.0,
        tier_mix=_mix(sel, "bucket"),
        country_mix=_mix(sel, "country"),
        platform_mix=_mix(sel, "platform"),
        constraints=validate_plan(sel, strict),
        n_price_estimated=sum(1 for a in allocations if a.price_estimated),
        candidate_pool=len(pool),
        skipped=list(skipped),
        trace=trace,
        strategy=strategy,
        n_posts=sum(sel.posts.values()),
        purchase_model={"max_posts_per_kox": max_posts, "post_marginal_decay": decay},
    )
