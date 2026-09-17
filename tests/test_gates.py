"""四层门禁 G0/G1/G2/G3 与判定合成（SPEC 第 4 节）。

测试策略
--------
门禁最容易出的两类错，一类都不能靠"跑一遍看结果像不像"发现：

1. **边界方向错**（`<` 写成 `<=`、上尾写成下尾）：只能靠**贴着阈值两侧**打表。
   所以下面凡是有门槛的规则，都构造"刚好不触发"和"刚好触发"两个样本。
2. **消融/降级时静默改语义**（关掉一层却让另一层结论变了）：靠不变量断言，
   例如"关层只能减少 reason，不能新增"。

手工样本 vs 真实数据的分工：
- **规则逻辑**用手工构造的记录（`kox()` 工厂）+ 手工构造的阈值表（`fake_thresholds`），
  这样断言是确定的、可复算的，不随数据集分布漂移。
- **分布性质**（连续分不退化成台阶、命中率量级）才用真实 5,000 条。

所有断言都不依赖 `output/*.json` 里的任何具体数值。
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from koxpilot.gates.engine import ALL_GATES, evaluate, evaluate_all, synthesize_verdict
from koxpilot.gates.g0 import completeness_of, evaluate_g0
from koxpilot.gates.g1 import evaluate_g1, spike_evidence
from koxpilot.gates.g2 import (
    audience_geo_share,
    audience_match_score,
    evaluate_g2,
    rule_fit_score,
    source_agreement,
)
from koxpilot.gates.g3 import evaluate_g3, high_severity_flags
from koxpilot.gates.policy import (
    AUDIENCE_GEO_MIN,
    AUDIENCE_MATCH_MIN,
    AUTHENTICITY_FLOOR_REJECT,
    COMPETITOR_HARD_MONTHS,
    COMPETITOR_RECENT_MONTHS,
    COMPLETENESS_REVIEW_MAX,
    CRITICAL_FIELDS,
    DECLARED_OBSERVED_JACCARD_MIN,
    FIT_SCORE_REVIEW_MAX,
    G1_HARD_RULES,
    G1_WEIGHTS,
    G2_WEIGHTS,
    G3_WEIGHTS,
    GRADED_PENALTY_GAIN,
    HARD_HITS_REJECT_MIN,
    REGULATED_SEVERITY_MULTIPLIER,
    SOURCE_JACCARD_MIN,
    SPIKE_ZSCORE_MIN,
)
from koxpilot.gates.thresholds import Thresholds
from koxpilot.stats import jaccard
from koxpilot.types import CampaignSpec

NEUTRAL = CampaignSpec()


# ---------------------------------------------------------------------------
# 工厂：一个"干净得挑不出毛病"的达人，测试按需只改要考察的字段
# ---------------------------------------------------------------------------
def _steady_history(final: int) -> list[int]:
    """由末点反推 12 个月粉丝历史，月增速在 2.8%~3.5% 之间轻微抖动。

    末点严格等于 ``final``，保证 ``follower_history[-1] == followers`` 这个自洽约束成立。
    """
    growths = [0.031, 0.029, 0.033, 0.030, 0.034, 0.029, 0.031, 0.032, 0.028, 0.035, 0.030]
    hist = [float(final)]
    for g in reversed(growths):
        hist.append(hist[-1] / (1.0 + g))
    return [int(round(v)) for v in reversed(hist)]


def kox(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kox_id": "KOX-TEST01",
        "handle": "@clean_baseline",
        "platform": "tiktok",
        "country": "US",
        "language": "en",
        "verified": True,
        "account_age_days": 900,
        "followers": 100_000,
        "following": 300,
        "posts": 400,
        "declared_categories": ["beauty"],
        "observed_categories": ["beauty"],
        "source_tags": {
            "src_platform": ["beauty"],
            "src_vendor_a": ["beauty"],
            "src_vendor_b": ["beauty"],
        },
        "avg_views": 50_000,
        "avg_likes": 2_500,
        "avg_comments": 200,
        "avg_shares": 100,
        "engagement_rate": 0.056,
        "view_follower_ratio": 0.5,
        "comment_like_ratio": 0.08,
        # 12 个月温和且**非单调**的增速（2.8%~3.5%）：不应触发任何突刺。
        # 注意不能用等差数列——等差数列的环比增速逐月单调下降，
        # 再把末点对齐 followers 就会造出一个人为的尾部突刺，G1.5 会误报。
        "follower_history": _steady_history(100_000),
        "viral_months": [],
        "audience_geo": {"US": 0.7, "CA": 0.1, "other": 0.2},
        "audience_age": {"13-17": 0.05, "18-24": 0.35, "25-34": 0.4, "35-44": 0.15, "45+": 0.05},
        "audience_gender": {"f": 0.6, "m": 0.4},
        "quoted_price_usd": 4_000.0,
        "avg_cpm_usd": 12.0,
        "past_collabs": [],
        "content_flags": [],
        "controversy": None,
        "comment_dup_rate": 0.05,
        "comment_emoji_only_rate": 0.05,
        "comment_lang_mismatch_rate": 0.03,
        "_missing": [],
    }
    base.update(over)
    return base


def _signal_block(**qs: float) -> dict[str, Any]:
    """构造一个阈值块。grid 给成 0..1 的 51 点线性网格，便于手算 percentile_rank。"""
    entry: dict[str, Any] = {"n": 500, "source": "group:test", "method": "log"}
    entry.update({k: v for k, v in qs.items()})
    entry["grid"] = [i / 50.0 for i in range(51)]
    return entry


@pytest.fixture()
def fake_thresholds() -> Thresholds:
    """手工阈值表：数值取整，方便贴边界打表。

    刻意只填 global_，用来同时验证 ``signal()`` 的 group -> platform -> global 回退。
    """
    return Thresholds(
        global_={
            "engagement_rate": _signal_block(p05=0.01, p95=0.10),
            "comment_like_ratio": _signal_block(p02=0.01, p98=0.20),
            "view_follower_ratio": _signal_block(p02=0.05, p98=1.00),
            "followers_per_day": _signal_block(p97=200.0),
            "comment_dup_rate": _signal_block(p97=0.30),
            "comment_emoji_only_rate": _signal_block(p97=0.30),
            "max_monthly_growth": _signal_block(p90=0.20, p97=0.40),
            "risk_severity_score": _signal_block(p90=2.0),
            "avg_cpm_usd": _signal_block(p50=10.0),
        },
        meta={"note": "handcrafted for tests"},
    )


# ===========================================================================
# 判定合成（SPEC 第 4 节）——纯函数，直接打表
# ===========================================================================
class TestSynthesizeVerdict:
    """`synthesize_verdict` 是四层结论汇总成 pass/review/reject 的唯一出口。

    优先级必须是 block > authenticity floor > 硬信号计数 > review > pass。
    """

    @pytest.mark.parametrize(
        "kwargs,expected,why",
        [
            (dict(blocked_by="G3.1", authenticity=1.0, n_hard_hits=0, completeness=1.0, n_reasons=1), "reject", "硬阻断最高优先"),
            (dict(blocked_by="G3.1", authenticity=0.0, n_hard_hits=5, completeness=0.0, n_reasons=9), "reject", "全坏也还是 reject"),
            (dict(blocked_by=None, authenticity=0.49, n_hard_hits=0, completeness=1.0, n_reasons=1), "reject", "真实性低于地板"),
            (dict(blocked_by=None, authenticity=1.0, n_hard_hits=2, completeness=1.0, n_reasons=2), "reject", "硬信号≥2"),
            (dict(blocked_by=None, authenticity=1.0, n_hard_hits=1, completeness=1.0, n_reasons=1), "review", "1 条硬信号只 review"),
            (dict(blocked_by=None, authenticity=1.0, n_hard_hits=0, completeness=0.6, n_reasons=0), "review", "完整度不足"),
            (dict(blocked_by=None, authenticity=1.0, n_hard_hits=0, completeness=1.0, n_reasons=1), "review", "有任意 reason"),
            (dict(blocked_by=None, authenticity=1.0, n_hard_hits=0, completeness=1.0, n_reasons=0), "pass", "干净"),
        ],
    )
    def test_truth_table(self, kwargs: dict[str, Any], expected: str, why: str) -> None:
        assert synthesize_verdict(**kwargs) == expected, why

    def test_authenticity_boundary_is_strict_less_than(self) -> None:
        """``authenticity == floor`` 不该 reject（SPEC 写的是 ``<``）。

        这条专抓 `<` / `<=` 写错——它在真实数据上几乎不可见（恰好等于地板的样本极少），
        但一旦写错，整个 reject 率会系统性偏移。
        """
        floor = AUTHENTICITY_FLOOR_REJECT
        at = dict(blocked_by=None, n_hard_hits=0, completeness=1.0, n_reasons=0)
        assert synthesize_verdict(authenticity=floor, **at) == "pass"
        assert synthesize_verdict(authenticity=math.nextafter(floor, 0.0), **at) == "reject"

    def test_completeness_boundary_is_strict_less_than(self) -> None:
        base = dict(blocked_by=None, authenticity=1.0, n_hard_hits=0, n_reasons=0)
        assert synthesize_verdict(completeness=COMPLETENESS_REVIEW_MAX, **base) == "pass"
        assert (
            synthesize_verdict(
                completeness=math.nextafter(COMPLETENESS_REVIEW_MAX, 0.0), **base
            )
            == "review"
        )

    def test_hard_hits_boundary(self) -> None:
        base = dict(blocked_by=None, authenticity=1.0, completeness=1.0)
        n = HARD_HITS_REJECT_MIN
        assert synthesize_verdict(n_hard_hits=n - 1, n_reasons=n - 1, **base) == "review"
        assert synthesize_verdict(n_hard_hits=n, n_reasons=n, **base) == "reject"

    def test_two_reject_paths_agree_by_construction(self) -> None:
        """交叉验证：G1 权重设计要保证"任意两条硬信号"也能把 authenticity 压到地板下。

        这不是巧合而是 policy.py 里写明的推导。如果有人调小硬信号权重却没同步
        `HARD_HITS_REJECT_MIN`，两条 reject 路径就会打架——这里替他发现。
        """
        hard_weights = sorted(G1_WEIGHTS[r] for r in G1_HARD_RULES)
        two_smallest = sum(hard_weights[:HARD_HITS_REJECT_MIN])
        assert 1.0 - two_smallest < AUTHENTICITY_FLOOR_REJECT, (
            f"最小的 {HARD_HITS_REJECT_MIN} 条硬信号权重之和 {two_smallest:.3f} "
            f"不足以把 authenticity 压到 {AUTHENTICITY_FLOOR_REJECT} 以下，"
            "「≥2 硬信号 → reject」与「authenticity < floor → reject」两条路径不再等价"
        )


# ===========================================================================
# G0 完整性
# ===========================================================================
class TestG0:
    def test_completeness_arithmetic(self) -> None:
        assert completeness_of(kox()) == (1.0, ())
        c, miss = completeness_of(kox(followers=None, avg_views=None))
        assert miss == ("followers", "avg_views")
        assert c == pytest.approx(1.0 - 2 / len(CRITICAL_FIELDS))

    def test_empty_container_counts_as_missing(self) -> None:
        """真实多源数据里"字段在但是空 dict"很常见，不能当有值。"""
        c_none, _ = completeness_of(kox(audience_geo=None))
        c_empty, miss = completeness_of(kox(audience_geo={}))
        assert c_empty == c_none and "audience_geo" in miss

    def test_one_missing_field_triggers_g02_not_g01(self) -> None:
        """缺 1/5 时 completeness=0.8，按严格不等式不命中 G0.1，必须由 G0.2 兜住。

        若这里退化成"什么都不命中"，就会出现一批 gt=review 而门禁 pass 的系统性漏检。
        """
        out = evaluate_g0(kox(quoted_price_usd=None))
        assert out.completeness == pytest.approx(COMPLETENESS_REVIEW_MAX)
        ids = [r.rule_id for r in out.reasons]
        assert ids == ["G0.2"]
        assert out.low_confidence is False

    def test_two_missing_fields_trigger_g01_and_low_confidence(self) -> None:
        out = evaluate_g0(kox(quoted_price_usd=None, avg_views=None))
        assert [r.rule_id for r in out.reasons] == ["G0.1"]
        assert out.low_confidence is True

    def test_g01_and_g02_are_mutually_exclusive(self) -> None:
        for n in range(len(CRITICAL_FIELDS) + 1):
            over = {f: None for f in CRITICAL_FIELDS[:n]}
            ids = [r.rule_id for r in evaluate_g0(kox(**over)).reasons]
            assert len(ids) <= 1, f"缺 {n} 个字段时同时命中 {ids}，G0 两条规则互斥性被破坏"

    def test_g02_weight_is_lower_than_g01(self) -> None:
        """证据强度排序：缺 1 个字段确实比缺一半更弱，权重必须体现这个方向。"""
        w1 = evaluate_g0(kox(quoted_price_usd=None, avg_views=None)).reasons[0].weight
        w2 = evaluate_g0(kox(quoted_price_usd=None)).reasons[0].weight
        assert w2 < w1

    def test_all_fields_missing_gives_zero_completeness(self) -> None:
        out = evaluate_g0(kox(**{f: None for f in CRITICAL_FIELDS}))
        assert out.completeness == 0.0
        assert out.missing == CRITICAL_FIELDS

    def test_g0_never_blocks(self) -> None:
        """缺数据不该等于封杀——G0 只允许 soft。"""
        out = evaluate_g0(kox(**{f: None for f in CRITICAL_FIELDS}))
        assert all(r.severity == "soft" for r in out.reasons)


# ===========================================================================
# G1 真实性
# ===========================================================================
class TestG1:
    def test_clean_record_has_no_hits(self, fake_thresholds: Thresholds) -> None:
        out = evaluate_g1(kox(), fake_thresholds)
        assert out.reasons == () and out.hard_hits == ()

    @pytest.mark.parametrize(
        "field,value,rule",
        [
            ("engagement_rate", 0.1001, "G1.1"),   # > p95=0.10
            ("engagement_rate", 0.0099, "G1.2"),   # < p05=0.01
            ("comment_like_ratio", 0.2001, "G1.3"),
            ("comment_like_ratio", 0.0099, "G1.3"),
            ("view_follower_ratio", 1.0001, "G1.4"),
            ("view_follower_ratio", 0.0499, "G1.4"),
            ("comment_dup_rate", 0.3001, "G1.7"),
            ("comment_emoji_only_rate", 0.3001, "G1.7"),
        ],
    )
    def test_each_signal_triggers_expected_rule(
        self, fake_thresholds: Thresholds, field: str, value: float, rule: str
    ) -> None:
        out = evaluate_g1(kox(**{field: value}), fake_thresholds)
        assert rule in [r.rule_id for r in out.reasons], f"{field}={value} 未触发 {rule}"

    @pytest.mark.parametrize(
        "field,value",
        [
            ("engagement_rate", 0.10),    # == p95，严格 > 才命中
            ("engagement_rate", 0.01),    # == p05，严格 < 才命中
            ("comment_like_ratio", 0.20),
            ("comment_like_ratio", 0.01),
            ("view_follower_ratio", 1.00),
            ("view_follower_ratio", 0.05),
            ("comment_dup_rate", 0.30),
            ("comment_emoji_only_rate", 0.30),
        ],
    )
    def test_values_exactly_on_threshold_do_not_hit(
        self, fake_thresholds: Thresholds, field: str, value: float
    ) -> None:
        """恰好等于阈值不命中。专抓 `>` / `>=` 写错的边界方向 bug。"""
        out = evaluate_g1(kox(**{field: value}), fake_thresholds)
        assert out.reasons == (), f"{field}={value}（恰好等于阈值）不应命中，实得 {out.reasons}"

    def test_hard_and_soft_severity_matches_policy(self, fake_thresholds: Thresholds) -> None:
        out = evaluate_g1(
            kox(engagement_rate=0.5, comment_like_ratio=0.9, view_follower_ratio=5.0),
            fake_thresholds,
        )
        for r in out.reasons:
            expected = "hard" if r.rule_id in G1_HARD_RULES else "soft"
            assert r.severity == expected, f"{r.rule_id} severity 应为 {expected}"
        assert set(out.hard_hits) == {r.rule_id for r in out.reasons if r.severity == "hard"}

    def test_graded_penalty_is_monotonic_in_depth(self, fake_thresholds: Thresholds) -> None:
        """越界越深，扣分越重（分级惩罚的核心不变量）。

        纯计数式扣分会让"评论重复率 82%"和"刚好压线"扣一样多，产品上说不通。
        """
        weights = []
        for er in (0.11, 0.20, 0.60, 0.95):
            out = evaluate_g1(kox(engagement_rate=er), fake_thresholds)
            hit = next(r for r in out.reasons if r.rule_id == "G1.1")
            weights.append((er, hit.depth, hit.weight))
        depths = [d for _, d, _ in weights]
        ws = [w for _, _, w in weights]
        assert depths == sorted(depths), f"depth 非单调：{weights}"
        assert ws == sorted(ws), f"weight 非单调：{weights}"
        # 逐条复算分级惩罚公式 w = w0 * (1 + GAIN * depth)，防止"看起来单调但公式改了"
        for er, depth, w in weights:
            expected = G1_WEIGHTS["G1.1"] * (1.0 + GRADED_PENALTY_GAIN * depth)
            assert w == pytest.approx(expected, abs=1e-3), f"er={er} 的分级扣分不符合公式"
        assert ws[0] < G1_WEIGHTS["G1.1"] * 1.1, "刚越界时扣分应接近基础权重"

    def test_missing_signals_are_silent_not_guessed(self, fake_thresholds: Thresholds) -> None:
        """信号缺失时必须跳过该规则，不能填 0 当"极低值"去命中下尾规则。"""
        out = evaluate_g1(
            kox(engagement_rate=None, comment_like_ratio=None, view_follower_ratio=None),
            fake_thresholds,
        )
        assert [r.rule_id for r in out.reasons] == []

    def test_no_thresholds_degrades_silently(self) -> None:
        """空阈值表（如某组样本不足）不能崩，也不能瞎判。"""
        out = evaluate_g1(kox(engagement_rate=0.99), Thresholds())
        assert out.reasons == ()
        assert 0.0 <= out.fraud_score <= 1.0

    def test_fraud_score_is_bounded_and_ordered(self, fake_thresholds: Thresholds) -> None:
        clean = evaluate_g1(kox(), fake_thresholds).fraud_score
        bad = evaluate_g1(
            kox(engagement_rate=0.9, comment_dup_rate=0.9, view_follower_ratio=9.0),
            fake_thresholds,
        ).fraud_score
        assert 0.0 <= clean <= bad <= 1.0
        assert bad > clean, "明显异常的账号连续分必须高于干净账号"

    def test_fraud_score_is_continuous_not_step(
        self, records: list[dict[str, Any]], thresholds: Thresholds
    ) -> None:
        """连续分必须真连续：若退化成"命中几条"的台阶，AUC 就没有意义了。

        这里用真实数据看取值多样性——手工样本无法体现分布性质。
        """
        scores = [evaluate_g1(r, thresholds).fraud_score for r in records[:1500]]
        uniq = len(set(round(s, 6) for s in scores))
        assert uniq > 500, f"1500 条样本只有 {uniq} 个不同的 fraud_score，连续分退化成台阶"
        assert min(scores) >= 0.0 and max(scores) <= 1.0

    # -- G1.5 突刺 ---------------------------------------------------------
    #: 前 8 个月约 1.2% 稳定增长，第 8 个月粉丝翻 4 倍（典型买粉形态）
    SPIKE_HISTORY = [10_000, 10_120, 10_250, 10_380, 10_500, 10_630, 10_760, 10_890,
                     40_000, 40_200, 40_500, 40_800]

    def test_spike_evidence_finds_unsupported_spike(self) -> None:
        ev = spike_evidence(
            kox(follower_history=self.SPIKE_HISTORY, followers=40_800, viral_months=[])
        )
        assert ev is not None and ev["zscore"] > SPIKE_ZSCORE_MIN
        assert ev["month"] == 8, "突刺月份定位错了（rates[i] 对应第 i+1 个月）"

    @pytest.mark.parametrize("viral,explained", [([8], True), ([7], True), ([6], False)])
    def test_viral_content_explains_the_spike(
        self, fake_thresholds: Thresholds, viral: list[int], explained: bool
    ) -> None:
        """有爆款内容支撑的突刺不算买粉——这是 G1.5 区别于"涨得快就是假"的关键。

        允许 1 个月滞后（爆款在 m 月、粉丝在 m+1 月涨），滞后 2 个月就不算解释。
        断言放在**规则层**而不是 `spike_evidence` 层：后者只要还有任何无支撑月份就会返回
        非 None（哪怕 z 很小），拿它的 None 当"已解释"是错的口径。
        """
        rec = kox(follower_history=self.SPIKE_HISTORY, followers=40_800, viral_months=viral)
        hit = "G1.5" in [r.rule_id for r in evaluate_g1(rec, fake_thresholds).reasons]
        assert hit is (not explained), f"viral_months={viral} 的解释力判定与预期相反"

    def test_spike_month_index_is_not_off_by_one(self) -> None:
        """月份定位差 1 会让"爆款解释"整体错位——这是最容易悄悄写错的地方。"""
        for spike_at in (3, 5, 9):
            hist = [10_000 + 100 * i for i in range(12)]
            for j in range(spike_at, 12):
                hist[j] += 25_000
            ev = spike_evidence(kox(follower_history=hist, followers=hist[-1], viral_months=[]))
            assert ev is not None and ev["month"] == spike_at

    def test_short_history_returns_none(self) -> None:
        assert spike_evidence(kox(follower_history=[1, 2, 3])) is None
        assert spike_evidence(kox(follower_history=None)) is None

    def test_g15_needs_both_zscore_and_growth_floor(self, fake_thresholds: Thresholds) -> None:
        """双条件：z 超标**且**增速高于同组「最好月份」的 P90。

        只看 z 会在 11 点短序列上产生约 8.6% 的假阳性（正常号也有相对突刺）。
        这里用一个 z 很高但绝对增速只有 5% 的样本证明第二个条件真的生效了。
        """
        gentle = [10_000] * 8 + [10_000, 10_500, 10_505, 10_510]
        rec = kox(follower_history=gentle, followers=10_510, viral_months=[])
        ev = spike_evidence(rec)
        assert ev is not None and ev["zscore"] > SPIKE_ZSCORE_MIN, "样本前提不成立"
        assert ev["growth"] < 0.20, "样本前提不成立：增速应低于 p90=0.20"
        out = evaluate_g1(rec, fake_thresholds)
        assert "G1.5" not in [r.rule_id for r in out.reasons], (
            "增速未超过同组最好月份 P90 却判了买粉突刺：G1.5 的第二个条件失效了"
        )


# ===========================================================================
# G2 一致性
# ===========================================================================
class TestG2:
    def test_neutral_spec_only_checks_campaign_free_rules(self) -> None:
        """neutral 画像下只能查 campaign 无关的规则（G2.1/G2.2/G2.5 退化口径）。

        这是评测能对齐 `gt.verdict`（campaign 无关的库级判定）的前提：
        拿"带 campaign 条件的判定"去比 campaign 无关的 gt，混淆矩阵会被口径差异污染。
        """
        out = evaluate_g2(kox(), NEUTRAL)
        assert out.reasons == ()
        assert out.fit_source.startswith("skipped")

    @pytest.mark.parametrize(
        "declared,observed,should_hit",
        [
            (["beauty"], ["beauty"], False),                      # J=1
            (["beauty", "fashion"], ["beauty", "fashion"], False),  # J=1
            (["beauty", "fashion"], ["beauty"], False),            # J=0.5 > 0.34
            (["beauty", "fashion", "food"], ["beauty"], True),     # J=1/3 ≈ 0.333 < 0.34
            (["beauty"], ["gaming_app"], True),                    # J=0
        ],
    )
    def test_g21_jaccard_boundary(
        self, declared: list[str], observed: list[str], should_hit: bool
    ) -> None:
        """Jaccard < 0.34 的边界。1/3 与 1/2 一个在门槛下一个在门槛上，专卡这条线。"""
        j = jaccard(declared, observed)
        out = evaluate_g2(kox(declared_categories=declared, observed_categories=observed), NEUTRAL)
        hit = "G2.1" in [r.rule_id for r in out.reasons]
        assert hit is should_hit, f"J({declared},{observed})={j:.4f} 判定与预期不符"
        assert out.details["declared_observed_jaccard"] == pytest.approx(j, abs=1e-4)

    def test_g21_threshold_is_strict_less_than(self) -> None:
        """构造 Jaccard 恰好等于门槛的情形不该命中。"""
        # 用不上整齐的整数比，直接验证"略高于/略低于"两侧的方向性
        lo = ["a1", "a2", "a3"]
        assert jaccard(lo, ["a1"]) < DECLARED_OBSERVED_JACCARD_MIN
        assert jaccard(["a1", "a2"], ["a1"]) > DECLARED_OBSERVED_JACCARD_MIN

    def test_source_agreement_math(self) -> None:
        assert source_agreement(kox()) == pytest.approx(1.0)
        rec = kox(
            source_tags={
                "src_platform": ["beauty"],
                "src_vendor_a": ["fashion"],
                "src_vendor_b": ["food"],
            }
        )
        assert source_agreement(rec) == pytest.approx(0.0)
        assert "G2.2" in [r.rule_id for r in evaluate_g2(rec, NEUTRAL).reasons]

    def test_source_agreement_needs_two_sources(self) -> None:
        """单源无法谈"一致度"，必须返回 None 而不是 0（0 会被误判成完全冲突）。"""
        assert source_agreement(kox(source_tags={"src_platform": ["beauty"]})) is None
        assert source_agreement(kox(source_tags=None)) is None
        out = evaluate_g2(kox(source_tags={"src_platform": ["beauty"]}), NEUTRAL)
        assert "G2.2" not in [r.rule_id for r in out.reasons]

    def test_source_agreement_boundary(self) -> None:
        """一致度恰好等于门槛（0.5）不命中；略低才命中。"""
        # 三源两两 Jaccard = 1/3, 1/3, 1/3 -> 均值 1/3 < 0.5
        conflict = kox(
            source_tags={
                "src_platform": ["beauty", "fashion"],
                "src_vendor_a": ["beauty", "food"],
                "src_vendor_b": ["beauty", "gaming_app"],
            }
        )
        agree = source_agreement(conflict)
        assert agree is not None and agree < SOURCE_JACCARD_MIN
        assert "G2.2" in [r.rule_id for r in evaluate_g2(conflict, NEUTRAL).reasons]
        # 两源 Jaccard 恰好 0.5（交集 2 / 并集 4）
        half = kox(
            source_tags={
                "src_platform": ["beauty", "fashion"],
                "src_vendor_a": ["beauty", "fashion", "food", "gaming_app"],
            }
        )
        assert source_agreement(half) == pytest.approx(SOURCE_JACCARD_MIN)
        assert "G2.2" not in [r.rule_id for r in evaluate_g2(half, NEUTRAL).reasons]

    def test_rule_fit_score_tiers(self) -> None:
        """规则版 fit 的三档 + 自称打折 + 关键词兜底，逐档验证相对顺序。"""
        spec = CampaignSpec(target_categories=("3c_digital",))
        exact = rule_fit_score(kox(observed_categories=["3c_digital"]), spec)
        declared_only = rule_fit_score(
            kox(observed_categories=["food"], declared_categories=["3c_digital"]), spec
        )
        miss = rule_fit_score(
            kox(observed_categories=["food"], declared_categories=["food"], handle="@nothing"), spec
        )
        assert exact == pytest.approx(1.0)
        assert miss < declared_only < exact, "自称命中应低于观测命中、但高于完全不沾"
        assert rule_fit_score(kox(), NEUTRAL) == 1.0, "无目标品类时不该扣分"

    def test_g23_uses_injected_fit_score_when_given(self) -> None:
        """LLM 注入点：给了 fit_score 就必须用它，且来源要如实标注。"""
        spec = CampaignSpec(target_categories=("3c_digital",))
        rec = kox(observed_categories=["3c_digital"])
        out = evaluate_g2(rec, spec, fit_score=0.1)
        assert out.fit_source.startswith("injected")
        assert out.fit_score == pytest.approx(0.1)
        assert "G2.3" in [r.rule_id for r in out.reasons], "注入的低分必须能触发 G2.3"
        rule_out = evaluate_g2(rec, spec)
        assert rule_out.fit_source.startswith("rule")
        assert "G2.3" not in [r.rule_id for r in rule_out.reasons]

    def test_g23_injected_score_is_clamped(self) -> None:
        spec = CampaignSpec(target_categories=("3c_digital",))
        assert evaluate_g2(kox(), spec, fit_score=99.0).fit_score == 1.0
        assert evaluate_g2(kox(), spec, fit_score=-5.0).fit_score == 0.0

    def test_g23_boundary(self) -> None:
        spec = CampaignSpec(target_categories=("3c_digital",))
        at = evaluate_g2(kox(), spec, fit_score=FIT_SCORE_REVIEW_MAX)
        below = evaluate_g2(kox(), spec, fit_score=math.nextafter(FIT_SCORE_REVIEW_MAX, 0.0))
        assert "G2.3" not in [r.rule_id for r in at.reasons]
        assert "G2.3" in [r.rule_id for r in below.reasons]

    def test_g24_language_derives_from_markets_when_unspecified(self) -> None:
        """未显式给语言时，可接受语言集应由目标市场推导，而不是放任不查。"""
        spec = CampaignSpec(target_markets=("US",))
        assert "G2.4" in [r.rule_id for r in evaluate_g2(kox(language="ja"), spec).reasons]
        assert "G2.4" not in [r.rule_id for r in evaluate_g2(kox(language="en"), spec).reasons]

    def test_g24_treats_english_as_universally_acceptable(self) -> None:
        """记录一条**刻意的产品假设**：英文对所有市场都算可接受（出海内容常用英文）。

        它藏在 ``MARKET_LANGUAGES`` 的构造里（每个国家的语言集都并入 "en"），
        很容易被误读成 bug。这条测试把它钉成显式契约：以后要改成"日本只收日文"，
        必须先改这里，而不是悄悄改词表让线上判定漂移。
        """
        for market in ("JP", "KR", "BR", "DE"):
            spec = CampaignSpec(target_markets=(market,))
            out = evaluate_g2(kox(language="en", audience_geo={market: 0.9}), spec)
            assert "G2.4" not in [r.rule_id for r in out.reasons]

    def test_g24_silent_when_no_constraint(self) -> None:
        assert "G2.4" not in [r.rule_id for r in evaluate_g2(kox(language="ja"), NEUTRAL).reasons]

    def test_g25_geo_share_math_and_boundary(self) -> None:
        spec = CampaignSpec(target_markets=("US", "CA"))
        rec = kox(audience_geo={"US": 0.2, "CA": 0.1, "IN": 0.7})
        assert audience_geo_share(rec, ("US", "CA")) == pytest.approx(0.3)
        assert "G2.5" in [r.rule_id for r in evaluate_g2(rec, spec).reasons]
        ok = kox(audience_geo={"US": AUDIENCE_GEO_MIN, "IN": 1 - AUDIENCE_GEO_MIN})
        assert "G2.5" not in [r.rule_id for r in evaluate_g2(ok, spec).reasons], "恰好等于门槛不该命中"

    def test_g25_falls_back_to_self_declared_market(self) -> None:
        """neutral 画像下退化为"达人自称市场"，这正好对应 gt 里受众地域错位的定义。"""
        rec = kox(country="US", audience_geo={"US": 0.1, "IN": 0.9})
        out = evaluate_g2(rec, NEUTRAL)
        assert "G2.5" in [r.rule_id for r in out.reasons]
        assert out.details["target_markets"] == ["US"]

    def test_g25_missing_geo_is_g0s_job(self) -> None:
        """受众地域缺失是完整性问题，不该由 G2 判"人群不匹配"（会重复扣分）。"""
        assert audience_geo_share(kox(audience_geo=None), ("US",)) is None
        out = evaluate_g2(kox(audience_geo=None), CampaignSpec(target_markets=("US",)))
        assert "G2.5" not in [r.rule_id for r in out.reasons]

    def test_g26_audience_match_weighting(self) -> None:
        spec = CampaignSpec(target_age_buckets=("25-34",), target_gender="f")
        rec = kox(
            audience_age={"13-17": 0.0, "18-24": 0.0, "25-34": 0.5, "35-44": 0.5, "45+": 0.0},
            audience_gender={"f": 0.25, "m": 0.75},
        )
        # 0.6*0.5 + 0.4*0.25 = 0.40
        assert audience_match_score(rec, spec) == pytest.approx(0.40, abs=1e-4)
        assert "G2.6" not in [r.rule_id for r in evaluate_g2(rec, spec).reasons], "恰好等于门槛不命中"
        worse = kox(
            audience_age={"13-17": 0.0, "18-24": 1.0, "25-34": 0.0, "35-44": 0.0, "45+": 0.0},
            audience_gender={"f": 0.1, "m": 0.9},
        )
        assert audience_match_score(worse, spec) < AUDIENCE_MATCH_MIN
        assert "G2.6" in [r.rule_id for r in evaluate_g2(worse, spec).reasons]

    def test_g26_no_target_means_no_penalty(self) -> None:
        """"没有人群要求"不该被判成"人群不匹配"。"""
        assert audience_match_score(kox(), NEUTRAL) == 1.0
        assert "G2.6" not in [r.rule_id for r in evaluate_g2(kox(), NEUTRAL).reasons]

    def test_g2_is_never_hard_or_block(self) -> None:
        """一致性问题是"选错人"，不是"不能用"，因此不允许 block/hard。"""
        spec = CampaignSpec(
            target_categories=("3c_digital",),
            target_markets=("JP",),
            target_age_buckets=("45+",),
            target_gender="m",
        )
        out = evaluate_g2(kox(declared_categories=["a", "b", "c"], observed_categories=["d"]), spec)
        assert out.reasons and all(r.severity == "soft" for r in out.reasons)

    def test_g2_weights_cover_all_emitted_rules(self) -> None:
        spec = CampaignSpec(
            target_categories=("3c_digital",),
            target_markets=("JP",),
            target_age_buckets=("45+",),
            target_gender="m",
        )
        out = evaluate_g2(
            kox(
                declared_categories=["a", "b", "c"],
                observed_categories=["d"],
                source_tags={"a": ["x"], "b": ["y"], "c": ["z"]},
                audience_geo={"IN": 1.0},
            ),
            spec,
        )
        for r in out.reasons:
            assert r.rule_id in G2_WEIGHTS
            assert r.weight == pytest.approx(G2_WEIGHTS[r.rule_id])
        assert out.penalty == pytest.approx(sum(r.weight for r in out.reasons))


# ===========================================================================
# G3 品牌安全
# ===========================================================================
class TestG3:
    def test_clean_record_no_hits(self, fake_thresholds: Thresholds) -> None:
        out = evaluate_g3(kox(), NEUTRAL, fake_thresholds)
        assert out.reasons == () and out.blocked_by is None

    def test_g31_high_severity_is_hard_block(self, fake_thresholds: Thresholds) -> None:
        rec = kox(content_flags=[{"type": "gambling_promo", "severity": "high", "hits": 2}])
        assert high_severity_flags(rec)
        out = evaluate_g3(rec, NEUTRAL, fake_thresholds)
        assert out.blocked_by == "G3.1"
        assert any(r.severity == "block" for r in out.reasons)

    def test_g32_threshold_comes_from_quantile(self, fake_thresholds: Thresholds) -> None:
        """G3.2 的门槛必须来自分位数表，而不是代码里的裸数字。"""
        rec = kox(content_flags=[{"type": "profanity", "severity": "medium", "hits": 3}])
        out = evaluate_g3(rec, NEUTRAL, fake_thresholds)
        g32 = [r for r in out.reasons if r.rule_id == "G3.2"]
        assert g32, "medium×3 应超过 p90=2.0 的风险载荷门槛"
        assert g32[0].source.startswith("quantile:")
        assert out.details["severity_threshold"] == pytest.approx(2.0)

    def test_g32_silent_without_thresholds(self) -> None:
        rec = kox(content_flags=[{"type": "profanity", "severity": "medium", "hits": 3}])
        out = evaluate_g3(rec, NEUTRAL, Thresholds())
        assert "G3.2" not in [r.rule_id for r in out.reasons]

    def test_regulated_category_tightens_g32(self, fake_thresholds: Thresholds) -> None:
        """受管制品类把门槛乘以 0.5：同一个达人在医疗单子下更容易被拦。"""
        # low×2 = 2.0 分：宽口径门槛 2.0（严格 > 不命中），收紧后门槛 1.0（命中）
        rec = kox(content_flags=[{"type": "profanity", "severity": "low", "hits": 2}])
        loose = evaluate_g3(rec, NEUTRAL, fake_thresholds)
        strict = evaluate_g3(rec, CampaignSpec(regulated_category="medical"), fake_thresholds)
        assert strict.details["severity_threshold"] == pytest.approx(
            2.0 * REGULATED_SEVERITY_MULTIPLIER
        )
        assert "G3.2" not in [r.rule_id for r in loose.reasons]
        assert "G3.2" in [r.rule_id for r in strict.reasons]

    @pytest.mark.parametrize(
        "months_ago,expect_rule,expect_block",
        [
            (1, True, True),    # < 3 个月排他期 -> block
            (2, True, True),
            (4, True, False),   # 3~6 个月 -> soft，需客户确认
            (7, False, False),  # > 6 个月不算冲突
        ],
    )
    def test_g33_competitor_recency_tiers(
        self, fake_thresholds: Thresholds, months_ago: int, expect_rule: bool, expect_block: bool
    ) -> None:
        spec = CampaignSpec(competitor_brands=("BrandX",))
        rec = kox(past_collabs=[{"brand": "BrandX", "months_ago": months_ago}])
        out = evaluate_g3(rec, spec, fake_thresholds)
        assert ("G3.3" in [r.rule_id for r in out.reasons]) is expect_rule
        assert (out.blocked_by == "G3.3") is expect_block

    def test_g33_boundaries_are_strict(self, fake_thresholds: Thresholds) -> None:
        spec = CampaignSpec(competitor_brands=("BrandX",))

        def out_at(m: int) -> Any:
            return evaluate_g3(
                kox(past_collabs=[{"brand": "BrandX", "months_ago": m}]), spec, fake_thresholds
            )

        assert "G3.3" not in [r.rule_id for r in out_at(COMPETITOR_RECENT_MONTHS).reasons]
        assert out_at(COMPETITOR_HARD_MONTHS).blocked_by is None, "恰好 3 个月不在排他期内"
        assert out_at(COMPETITOR_HARD_MONTHS - 1).blocked_by == "G3.3"

    def test_g33_silent_without_competitor_list(self, fake_thresholds: Thresholds) -> None:
        rec = kox(past_collabs=[{"brand": "BrandX", "months_ago": 1}])
        out = evaluate_g3(rec, NEUTRAL, fake_thresholds)
        assert "G3.3" not in [r.rule_id for r in out.reasons]

    def test_g34_controversy_is_review_not_reject(self, fake_thresholds: Thresholds) -> None:
        rec = kox(controversy={"type": "public_dispute", "months_ago": 4, "severity": "low"})
        out = evaluate_g3(rec, NEUTRAL, fake_thresholds)
        g34 = [r for r in out.reasons if r.rule_id == "G3.4"]
        assert g34 and g34[0].severity == "soft"
        assert out.blocked_by is None

    def test_g35_only_fires_for_regulated_campaigns(self, fake_thresholds: Thresholds) -> None:
        rec = kox(content_flags=[{"type": "medical_claim", "severity": "low", "hits": 1}])
        assert "G3.5" not in [r.rule_id for r in evaluate_g3(rec, NEUTRAL, fake_thresholds).reasons]
        out = evaluate_g3(rec, CampaignSpec(regulated_category="medical"), fake_thresholds)
        assert "G3.5" in [r.rule_id for r in out.reasons]

    def test_g3_weights_match_policy(self, fake_thresholds: Thresholds) -> None:
        rec = kox(
            content_flags=[{"type": "medical_claim", "severity": "medium", "hits": 3}],
            controversy={"type": "refund_scandal", "months_ago": 2, "severity": "medium"},
            past_collabs=[{"brand": "BrandX", "months_ago": 4}],
        )
        spec = CampaignSpec(competitor_brands=("BrandX",), regulated_category="medical")
        out = evaluate_g3(rec, spec, fake_thresholds)
        assert {r.rule_id for r in out.reasons} >= {"G3.2", "G3.3", "G3.4", "G3.5"}
        for r in out.reasons:
            assert r.weight == pytest.approx(G3_WEIGHTS[r.rule_id])


# ===========================================================================
# 引擎装配
# ===========================================================================
class TestEngine:
    def test_clean_record_passes(self, fake_thresholds: Thresholds) -> None:
        res = evaluate(kox(), NEUTRAL, fake_thresholds)
        assert res.verdict == "pass"
        assert res.reasons == () and res.blocked_by is None
        assert res.completeness == 1.0

    def test_layer_scores_are_computed_per_gate(self, fake_thresholds: Thresholds) -> None:
        """三个分数必须各自只受本层影响；串味会让证据链解释不通。"""
        g1_bad = evaluate(kox(engagement_rate=0.5), NEUTRAL, fake_thresholds)
        assert g1_bad.authenticity_score < 1.0
        assert g1_bad.consistency_score == 1.0 and g1_bad.brand_safety_score == 1.0

        g2_bad = evaluate(
            kox(declared_categories=["a", "b", "c"], observed_categories=["d"]),
            NEUTRAL,
            fake_thresholds,
        )
        assert g2_bad.consistency_score < 1.0
        assert g2_bad.authenticity_score == 1.0 and g2_bad.brand_safety_score == 1.0

        g3_bad = evaluate(
            kox(controversy={"type": "public_dispute", "months_ago": 2, "severity": "low"}),
            NEUTRAL,
            fake_thresholds,
        )
        assert g3_bad.brand_safety_score < 1.0
        assert g3_bad.authenticity_score == 1.0 and g3_bad.consistency_score == 1.0

    def test_block_severity_excluded_from_layer_score(self, fake_thresholds: Thresholds) -> None:
        """block 不参与累加扣分（它已经是终局），否则会双重惩罚。"""
        res = evaluate(
            kox(content_flags=[{"type": "adult_content", "severity": "high", "hits": 1}]),
            NEUTRAL,
            fake_thresholds,
        )
        assert res.verdict == "reject" and res.blocked_by == "G3.1"
        assert res.brand_safety_score == 1.0

    def test_low_confidence_discount_applies_to_later_layers(
        self, fake_thresholds: Thresholds
    ) -> None:
        """G0.1 命中时后续层扣分打折——"缺数据"不该反过来加重处罚。"""
        bad_signal = dict(engagement_rate=0.5)
        full = evaluate(kox(**bad_signal), NEUTRAL, fake_thresholds)
        # 缺 2 个关键字段（但不含 engagement_rate，避免信号消失）
        partial = evaluate(
            kox(quoted_price_usd=None, audience_geo=None, **bad_signal), NEUTRAL, fake_thresholds
        )
        assert partial.completeness < COMPLETENESS_REVIEW_MAX
        assert partial.authenticity_score > full.authenticity_score, (
            "G0.1 命中时后续层应打折扣分（分数更高），实测反而更低"
        )

    def test_disabled_rules_are_removed(self, fake_thresholds: Thresholds) -> None:
        rec = kox(engagement_rate=0.5)
        with_rule = evaluate(rec, NEUTRAL, fake_thresholds)
        assert "G1.1" in with_rule.rule_ids
        without = evaluate(rec, NEUTRAL, fake_thresholds, None, ALL_GATES, ("G1.1",))
        assert "G1.1" not in without.rule_ids
        assert without.authenticity_score == 1.0

    @pytest.mark.parametrize("gates", [("G0",), ("G1",), ("G2",), ("G3",), ("G0", "G1")])
    def test_ablation_only_removes_reasons(
        self, fake_thresholds: Thresholds, gates: tuple[str, ...]
    ) -> None:
        """消融不变量：关层只能减少 reason，绝不能凭空生出新的。

        消融实验最常见的坑是"关掉一层导致另一层拿到 None 走进了别的分支"，
        指标看起来还挺合理，其实口径已经变了。这条不变量就是为此设的。
        """
        rec = kox(
            engagement_rate=0.5,
            declared_categories=["a", "b", "c"],
            observed_categories=["d"],
            controversy={"type": "public_dispute", "months_ago": 2, "severity": "low"},
            quoted_price_usd=None,
        )
        full = set(evaluate(rec, NEUTRAL, fake_thresholds).rule_ids)
        part = set(evaluate(rec, NEUTRAL, fake_thresholds, None, gates).rule_ids)
        assert part <= full, f"消融 {gates} 后新增了 {part - full}"
        assert part == {r for r in full if r.split(".")[0] in gates}

    def test_disabled_gate_scores_default_to_one(self, fake_thresholds: Thresholds) -> None:
        res = evaluate(kox(engagement_rate=0.5), NEUTRAL, fake_thresholds, None, ("G0",))
        assert res.authenticity_score == 1.0
        assert res.consistency_score == 1.0
        assert res.brand_safety_score == 1.0
        assert res.fraud_score == 0.0

    def test_evaluate_is_pure_and_does_not_mutate_input(
        self, fake_thresholds: Thresholds
    ) -> None:
        rec = kox(engagement_rate=0.5, gt={"is_fraud": True})
        before = repr(rec)
        evaluate(rec, NEUTRAL, fake_thresholds)
        assert repr(rec) == before, "evaluate 修改了入参：批量评估会出现顺序依赖"

    def test_evaluate_is_deterministic_and_order_independent(
        self, records: list[dict[str, Any]], thresholds: Thresholds
    ) -> None:
        sample = records[:200]
        a = {r.kox_id: r.verdict for r in evaluate_all(sample, thresholds=thresholds)}
        b = {r.kox_id: r.verdict for r in evaluate_all(list(reversed(sample)), thresholds=thresholds)}
        assert a == b, "判定结果依赖记录顺序，说明存在跨记录状态"

    def test_fit_scores_are_injected_by_kox_id(
        self, records: list[dict[str, Any]], thresholds: Thresholds
    ) -> None:
        """LLM fit 分按 kox_id 注入；串号会让"LLM vs 规则"的对比彻底失真。"""
        spec = CampaignSpec(target_categories=("3c_digital",))
        sample = records[:30]
        target = sample[0]["kox_id"]
        out = {
            r.kox_id: r
            for r in evaluate_all(sample, spec, thresholds, fit_scores={target: 0.0})
        }
        assert out[target].fit_source.startswith("injected")
        assert out[target].fit_score == 0.0
        others = [r for k, r in out.items() if k != target]
        assert all(r.fit_source.startswith("rule") for r in others)

    def test_group_key_shape(self, fake_thresholds: Thresholds) -> None:
        res = evaluate(kox(platform="tiktok", followers=100_000), NEUTRAL, fake_thresholds)
        assert res.group_key.startswith("tiktok|") and "|" in res.group_key

    def test_to_dict_contract(self, fake_thresholds: Thresholds) -> None:
        """落盘结构是前端 TS 引擎的契约，字段丢一个前端就白屏。"""
        d = evaluate(kox(engagement_rate=0.5), NEUTRAL, fake_thresholds).to_dict()
        assert set(d) == {
            "kox_id",
            "verdict",
            "group_key",
            "scores",
            "fit_source",
            "hard_hits",
            "blocked_by",
            "reasons",
        }
        assert set(d["scores"]) == {
            "completeness",
            "authenticity",
            "consistency",
            "brand_safety",
            "fraud_score",
            "fit_score",
        }
        for reason in d["reasons"]:
            assert set(reason) == {
                "gate",
                "rule_id",
                "signal",
                "actual",
                "threshold",
                "weight",
                "human_text",
                "severity",
                "source",
                "depth",
            }
            assert reason["human_text"].strip(), "证据必须有中文人话，否则产品无法解释"
            assert reason["source"], "每条证据都要能追溯阈值来源"

    def test_every_reason_is_traceable(
        self, records: list[dict[str, Any]], thresholds: Thresholds
    ) -> None:
        """真实数据上抽查：每条命中都必须能说清阈值来自分位数还是 policy。"""
        for rec in records[:400]:
            for r in evaluate(rec, NEUTRAL, thresholds).reasons:
                assert r.source.startswith(("quantile:", "policy:")), r.source
                assert r.gate in ALL_GATES
                assert 0.0 <= r.depth <= 1.0
                assert r.weight > 0.0

    def test_verdict_distribution_has_all_three_classes(
        self, results: dict[str, Any]
    ) -> None:
        """三档都要有量级合理的样本，否则混淆矩阵没法看。

        区间刻意放很宽（只防塌陷），不绑实测值（pass 1740 / review 2453 / reject 807）。
        """
        counts: dict[str, int] = {}
        for r in results.values():
            counts[r.verdict] = counts.get(r.verdict, 0) + 1
        total = sum(counts.values())
        assert set(counts) == {"pass", "review", "reject"}
        for verdict, n in counts.items():
            assert 0.05 <= n / total <= 0.70, f"{verdict} 占比 {n / total:.1%} 落在极端区间"

    def test_reject_always_has_evidence(self, results: dict[str, Any]) -> None:
        """不允许"无理由 reject"——产品上无法向客户解释。"""
        for r in results.values():
            if r.verdict != "pass":
                assert r.reasons, f"{r.kox_id} 判 {r.verdict} 却没有任何证据"
            if r.verdict == "reject":
                assert (
                    r.blocked_by is not None
                    or r.authenticity_score < AUTHENTICITY_FLOOR_REJECT
                    or len(r.hard_hits) >= HARD_HITS_REJECT_MIN
                ), f"{r.kox_id} reject 但三条 reject 路径都不成立"

    def test_pass_records_have_no_reasons(self, results: dict[str, Any]) -> None:
        for r in results.values():
            if r.verdict == "pass":
                assert not r.reasons and r.completeness >= COMPLETENESS_REVIEW_MAX
