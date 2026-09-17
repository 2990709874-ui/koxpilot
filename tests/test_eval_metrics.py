"""评测指标：统计原语 / 口径定义 / 报表结构 / 反自证守卫（SPEC 第 7 节）。

测试策略
--------
指标是这个项目里**最容易骗人**的一层：数字只要"看起来合理"，几乎没人会去验它算得对不对。
所以这一章分四段，且刻意不去绑 `output/metrics.json` 里的任何具体数值：

1. **原语可复算**（`TestStats*`）：项目不用 sklearn，那就必须自己证明 AUC/P/R/F1 算对了。
   `roc_auc` 用**独立写的 O(n²) 定义式**（含并列算 0.5）对拍，而不是"跟自己比"。
2. **口径固定**（`TestPredicates` / `TestReportArithmetic`）：严/宽口径与混淆矩阵的边界打表，
   报表里每个派生数字都从矩阵手算复现一遍——各表各算一套是指标注水的头号来源。
3. **反自证**（`TestAntiSelfProof`）：这一段是本章的核心。
   打乱标签后 AUC 必须回到 0.5、常数分预测器必须拿不到分、
   缺失预测不许被算成正确。**如果一套指标在标签被打乱后还很好看，那它测的就不是模型。**
4. **区间断言**（`TestRealData*`）：真实数据只断言区间与**关系**（宽口径召回 > 严口径召回等），
   不写死 F1=0.72 这类常量——绑常量的测试只会变成记账本，代码一改就红，还掩盖真实回归。

消融/敏感性会反复整表重跑（各 12~20 次门禁），因此用 `small_records`（另一个种子的 400 条）跑，
只验证**结构与算术**，分布性结论留给全量表。
"""

from __future__ import annotations

import copy
import math
import random
from typing import Any

import pytest

from koxpilot.eval.ablation import CONTRIBUTION_EPS, ablation_report
from koxpilot.eval.metrics import (
    FRAUD_TYPES,
    VERDICTS,
    binary_scores,
    confusion_matrix,
    fraud_pred_loose,
    fraud_pred_strict,
    fraud_report,
    gt_of,
    macro_f1,
    verdict_report,
)
from koxpilot.eval.sensitivity import SCAN_FACTORS, STABILITY_TOLERANCE, sensitivity_report
from koxpilot.eval.strata import MIN_SUPPORT_POSITIVE, WEAK_SPOT_TOP_N, strata_report
from koxpilot.gates.policy import AUTHENTICITY_FLOOR_REJECT, G1_WEIGHTS, HARD_HITS_REJECT_MIN
from koxpilot.gates.thresholds import Thresholds, calibrate
from koxpilot.stats import (
    jaccard,
    mad,
    mean,
    median,
    percentile_rank,
    prf1,
    quantile,
    quantiles,
    robust_zscores,
    roc_auc,
    safe_div,
)
from koxpilot.types import CampaignSpec, GateResult, Reason


# ---------------------------------------------------------------------------
# 参考实现（**独立**于被测代码，用来对拍）
# ---------------------------------------------------------------------------
def auc_by_definition(scores: list[float], labels: list[int]) -> float:
    """AUC 的定义式：随机取一正一负，正样本分更高的概率（并列算 0.5）。

    O(n²) 双循环，慢但一眼能看懂对不对。被测实现用的是 Mann–Whitney 秩和 O(n log n)，
    两者必须给出同样的数——这才叫"验证过"，而不是"跑通了"。
    """
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return 0.5
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else (0.5 if p == n else 0.0)
    return wins / (len(pos) * len(neg))


def result_of(
    kox_id: str = "K",
    *,
    verdict: str = "pass",
    authenticity: float = 1.0,
    hard_hits: tuple[str, ...] = (),
    gates: tuple[str, ...] = (),
    fraud_score: float = 0.0,
) -> GateResult:
    reasons = tuple(
        Reason(
            gate=g,
            rule_id=f"{g}.1",
            signal="s",
            actual=1.0,
            threshold=0.5,
            weight=0.1,
            human_text="x",
        )
        for g in gates
    )
    return GateResult(
        kox_id=kox_id,
        verdict=verdict,  # type: ignore[arg-type]
        reasons=reasons,
        completeness=1.0,
        authenticity_score=authenticity,
        consistency_score=1.0,
        brand_safety_score=1.0,
        fraud_score=fraud_score,
        fit_score=1.0,
        fit_source="rule",
        hard_hits=hard_hits,
    )


def rec_of(kox_id: str, *, verdict: str = "pass", is_fraud: bool = False, ftype: Any = None) -> dict[str, Any]:
    return {
        "kox_id": kox_id,
        "platform": "tiktok",
        "country": "US",
        "followers": 50_000,
        "gt": {"verdict": verdict, "is_fraud": is_fraud, "fraud_type": ftype},
    }


# ===========================================================================
# 1. 统计原语
# ===========================================================================
class TestQuantile:
    def test_known_values_by_hand(self) -> None:
        v = [1.0, 2.0, 3.0, 4.0]
        assert quantile(v, 0.0) == 1.0
        assert quantile(v, 1.0) == 4.0
        assert quantile(v, 0.5) == 2.5
        # h = (n-1)*q = 3*0.25 = 0.75 → 在 v[0] 与 v[1] 之间插值
        assert quantile(v, 0.25) == pytest.approx(1.75)

    def test_matches_linear_interpolation_definition(self) -> None:
        rng = random.Random(7)
        v = [rng.uniform(-10, 10) for _ in range(37)]
        ordered = sorted(v)
        for q in (0.05, 0.1, 0.33, 0.5, 0.9, 0.95, 0.99):
            h = (len(v) - 1) * q
            lo, hi = math.floor(h), math.ceil(h)
            expected = ordered[lo] + (h - lo) * (ordered[hi] - ordered[lo])
            assert quantile(v, q) == pytest.approx(expected)

    def test_does_not_mutate_input(self) -> None:
        v = [3.0, 1.0, 2.0]
        quantile(v, 0.5)
        assert v == [3.0, 1.0, 2.0], "分位数计算把调用方的数组排序了，会引发远处的诡异 bug"

    def test_edge_cases(self) -> None:
        assert quantile([], 0.5) == 0.0
        assert quantile([42.0], 0.99) == 42.0
        for bad in (-0.01, 1.01):
            with pytest.raises(ValueError):
                quantile([1.0, 2.0], bad)

    def test_quantiles_labels(self) -> None:
        out = quantiles([float(i) for i in range(101)], (0.0, 0.05, 0.5, 0.95, 1.0))
        assert set(out) == {"p00", "p05", "p50", "p95", "p100"}
        assert out["p50"] == pytest.approx(50.0)


class TestRobustStats:
    def test_median_and_mad_by_hand(self) -> None:
        assert median([1.0, 3.0, 2.0]) == 2.0
        # 偏差 = [1,0,1,2] → 中位数 1.0
        assert mad([1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)
        assert mad([]) == 0.0

    def test_robust_z_is_scaled_to_sigma(self) -> None:
        v = [10.0, 11.0, 12.0, 13.0, 100.0]
        z = robust_zscores(v)
        assert z[-1] > 2.5, "明显的突刺必须被识别出来"
        expected = (v[-1] - median(v)) / (1.4826 * mad(v))
        assert z[-1] == pytest.approx(expected)

    def test_constant_series_returns_zeros_not_nan(self) -> None:
        """MAD 为 0 时不能除零变成 inf/NaN——那会顺着 fraud_score 污染整张表。"""
        assert robust_zscores([5.0] * 6) == [0.0] * 6
        assert robust_zscores([]) == []
        assert all(math.isfinite(x) for x in robust_zscores([1.0, 1.0, 1.0, 9.0]))

    def test_percentile_rank_uses_mid_rank_for_ties(self) -> None:
        s = [1.0, 2.0, 2.0, 3.0]
        assert percentile_rank(s, 0.0) == 0.0
        assert percentile_rank(s, 4.0) == 1.0
        assert percentile_rank(s, 2.0) == pytest.approx((1 + 3) / 8.0)
        assert percentile_rank([], 1.0) == 0.5, "空样本应返回「无信息」的 0.5"

    def test_safe_div_and_mean(self) -> None:
        assert safe_div(1.0, 0.0) == 0.0
        assert safe_div(1.0, 0.0, default=-1.0) == -1.0
        assert safe_div(3.0, 2.0) == 1.5
        assert mean([]) == 0.0

    def test_jaccard_definition_and_empty_convention(self) -> None:
        assert jaccard([], []) == 1.0, "都没声明品类不算冲突"
        assert jaccard(["a"], []) == 0.0
        assert jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)
        assert jaccard(["a", "b"], ["a", "b"]) == 1.0


class TestRocAuc:
    def test_matches_pairwise_definition_with_ties(self) -> None:
        """与独立写的 O(n²) 定义式对拍（刻意用小整数分制造大量并列秩）。"""
        rng = random.Random(11)
        for _ in range(25):
            n = rng.randint(4, 60)
            scores = [float(rng.randint(0, 5)) for _ in range(n)]
            labels = [rng.randint(0, 1) for _ in range(n)]
            if len(set(labels)) < 2:
                continue
            assert roc_auc(scores, labels) == pytest.approx(auc_by_definition(scores, labels))

    def test_perfect_and_inverted_separation(self) -> None:
        scores = [0.1, 0.2, 0.8, 0.9]
        assert roc_auc(scores, [0, 0, 1, 1]) == 1.0
        assert roc_auc(scores, [1, 1, 0, 0]) == 0.0

    def test_constant_scores_give_exactly_half(self) -> None:
        """常数分预测器必须拿 0.5——不能靠并列秩的实现细节偷到 0.6。"""
        assert roc_auc([0.5] * 10, [1, 0] * 5) == 0.5

    def test_single_class_returns_half_not_crash(self) -> None:
        assert roc_auc([0.1, 0.9], [1, 1]) == 0.5
        assert roc_auc([0.1, 0.9], [0, 0]) == 0.5

    def test_invariant_under_monotone_rescaling(self) -> None:
        """AUC 是纯秩统计量：分数做任何单调变换都不该改变它。

        这条同时是"实现真的基于秩"的证明——若哪天有人偷偷用绝对分做加权，这里会红。
        """
        rng = random.Random(3)
        scores = [rng.random() for _ in range(200)]
        labels = [1 if s + rng.gauss(0, 0.3) > 0.5 else 0 for s in scores]
        base = roc_auc(scores, labels)
        for f in (lambda x: 100 * x, lambda x: x**3, lambda x: math.log1p(x)):
            assert roc_auc([f(s) for s in scores], labels) == pytest.approx(base)

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            roc_auc([0.1, 0.2], [1])


class TestPrf1:
    def test_hand_table(self) -> None:
        out = prf1(tp=8, fp=2, fn=4)
        assert out["precision"] == pytest.approx(0.8)
        assert out["recall"] == pytest.approx(8 / 12, abs=1e-4)
        assert out["f1"] == pytest.approx(2 * 0.8 * (8 / 12) / (0.8 + 8 / 12), abs=1e-4)

    def test_zero_denominators_report_zero_not_nan(self) -> None:
        """空格子按 0 报且不隐藏——用 NaN 或直接省略会让报表读者以为"没这个问题"。"""
        assert prf1(0, 0, 0) == {"tp": 0, "fp": 0, "fn": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0}
        assert prf1(0, 5, 5)["f1"] == 0.0

    def test_binary_scores_adds_specificity_and_support(self) -> None:
        out = binary_scores(tp=8, fp=2, fn=4, tn=86)
        assert out["specificity"] == pytest.approx(86 / 88, abs=1e-4)
        assert out["accuracy"] == pytest.approx(94 / 100, abs=1e-4)
        assert out["n"] == 100

    def test_macro_f1_is_unweighted_mean(self) -> None:
        per = {"a": {"f1": 0.2}, "b": {"f1": 0.4}, "c": {"f1": 0.9}}
        assert macro_f1(per) == pytest.approx(0.5)
        assert macro_f1({}) == 0.0


# ===========================================================================
# 2. 口径定义（严 / 宽）
# ===========================================================================
class TestPredicates:
    def test_strict_boundary_on_authenticity_floor(self) -> None:
        """地板是**严格小于**：恰好等于地板不算水号。

        这类边界在真实数据上几乎看不出来（恰好压线的样本极少），
        但写反了整个 reject 率会系统性偏移，且没有任何报错。
        """
        floor = AUTHENTICITY_FLOOR_REJECT
        assert fraud_pred_strict(result_of(authenticity=floor)) is False
        assert fraud_pred_strict(result_of(authenticity=math.nextafter(floor, 0.0))) is True

    def test_strict_boundary_on_hard_hits(self) -> None:
        n = HARD_HITS_REJECT_MIN
        assert fraud_pred_strict(result_of(hard_hits=tuple(f"G1.{i}" for i in range(n - 1)))) is False
        assert fraud_pred_strict(result_of(hard_hits=tuple(f"G1.{i}" for i in range(n)))) is True

    def test_loose_counts_only_g1(self) -> None:
        """宽口径是"真实性可疑"，不能被 G2/G3 命中污染——否则水号指标混进了别的层。"""
        assert fraud_pred_loose(result_of(gates=("G1",))) is True
        assert fraud_pred_loose(result_of(gates=("G0", "G2", "G3"))) is False
        assert fraud_pred_loose(result_of()) is False

    def test_strict_implies_loose_on_real_data(
        self, records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        """严口径必须是宽口径的子集。

        若出现"严口径命中但 G1 一条都没命中"，说明 authenticity 被非 G1 的东西压下去了，
        那两个口径就不再可比，表 1 的严/宽对照也就失去意义。
        """
        strict = {k for k, r in results.items() if fraud_pred_strict(r)}
        loose = {k for k, r in results.items() if fraud_pred_loose(r)}
        assert strict <= loose, f"{len(strict - loose)} 个严口径命中不在宽口径里"
        assert strict and loose

    def test_gt_of_never_raises_on_bad_input(self) -> None:
        assert gt_of({}) == {}
        assert gt_of({"gt": None}) == {}
        assert gt_of({"gt": "oops"}) == {}
        assert gt_of({"gt": {"verdict": "pass"}}) == {"verdict": "pass"}

    def test_gt_of_returns_a_copy(self) -> None:
        """裁判块必须是副本：评测代码手滑改了它，后面所有指标都会跟着变。"""
        rec = {"gt": {"verdict": "pass"}}
        got = gt_of(rec)
        got["verdict"] = "reject"
        assert rec["gt"]["verdict"] == "pass"


# ===========================================================================
# 3. 报表算术（每个派生数字都手算复现）
# ===========================================================================
class TestReportArithmetic:
    @pytest.fixture()
    def toy(self) -> tuple[list[dict[str, Any]], dict[str, GateResult]]:
        """9 条手工样本，混淆矩阵可以直接肉眼数出来。"""
        rows = [
            ("A1", "pass", "pass"),
            ("A2", "pass", "pass"),
            ("A3", "pass", "review"),
            ("B1", "review", "review"),
            ("B2", "review", "reject"),
            ("B3", "review", "pass"),
            ("C1", "reject", "reject"),
            ("C2", "reject", "reject"),
            ("C3", "reject", "review"),
        ]
        recs = [rec_of(k, verdict=truth) for k, truth, _ in rows]
        res = {k: result_of(k, verdict=pred) for k, _, pred in rows}
        return recs, res

    def test_confusion_matrix_counts(self, toy: Any) -> None:
        recs, res = toy
        m = confusion_matrix(recs, res)
        assert m["pass"] == {"pass": 2, "review": 1, "reject": 0}
        assert m["review"] == {"pass": 1, "review": 1, "reject": 1}
        assert m["reject"] == {"pass": 0, "review": 1, "reject": 2}

    def test_matrix_margins_match_inputs(
        self, records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        """行和 = 真值分布、列和 = 预测分布、总和 = 样本数。三者任一错位都说明有人被漏算/重算。"""
        m = confusion_matrix(records, results)
        for v in VERDICTS:
            assert sum(m[v].values()) == sum(1 for r in records if gt_of(r).get("verdict") == v)
            assert sum(m[g][v] for g in VERDICTS) == sum(1 for r in results.values() if r.verdict == v)
        assert sum(sum(row.values()) for row in m.values()) == len(records)

    def test_verdict_report_is_recomputable_from_matrix(self, toy: Any) -> None:
        recs, res = toy
        rep = verdict_report(recs, res)
        m = rep["matrix"]
        total = sum(sum(r.values()) for r in m.values())
        assert rep["n"] == total == 9
        assert rep["accuracy"] == pytest.approx(round(sum(m[v][v] for v in VERDICTS) / total, 4))
        for v in VERDICTS:
            tp = m[v][v]
            fn = sum(m[v][p] for p in VERDICTS if p != v)
            fp = sum(m[g][v] for g in VERDICTS if g != v)
            expected = binary_scores(tp, fp, fn, total - tp - fn - fp)
            assert rep["per_class"][v] == expected, v
        assert rep["macro_f1"] == macro_f1(rep["per_class"])

    def test_fraud_report_is_recomputable_by_hand(self) -> None:
        recs = [
            rec_of("F1", is_fraud=True, ftype="bought_followers"),
            rec_of("F2", is_fraud=True, ftype="bought_followers"),
            rec_of("F3", is_fraud=True, ftype="view_inflation"),
            rec_of("C1"),
            rec_of("C2"),
            rec_of("C3"),
        ]
        res = {
            "F1": result_of("F1", authenticity=0.1, gates=("G1",), fraud_score=0.9),
            "F2": result_of("F2", authenticity=1.0, gates=("G1",), fraud_score=0.6),  # 只宽口径命中
            "F3": result_of("F3", fraud_score=0.2),  # 双口径都漏
            "C1": result_of("C1", hard_hits=("a", "b"), gates=("G1",), fraud_score=0.7),  # 严口径误报
            "C2": result_of("C2", fraud_score=0.1),
            "C3": result_of("C3", fraud_score=0.05),
        }
        rep = fraud_report(recs, res)

        assert (rep["strict"]["tp"], rep["strict"]["fp"], rep["strict"]["fn"], rep["strict"]["tn"]) == (
            1,
            1,
            2,
            2,
        )
        assert (rep["loose"]["tp"], rep["loose"]["fp"], rep["loose"]["fn"], rep["loose"]["tn"]) == (
            2,
            1,
            1,
            2,
        )
        assert rep["prevalence"] == pytest.approx(0.5)
        scores = [res[r["kox_id"]].fraud_score for r in recs]
        labels = [1 if r["gt"]["is_fraud"] else 0 for r in recs]
        assert rep["auc"] == pytest.approx(auc_by_definition(scores, labels))

        bf = rep["per_fraud_type"]["bought_followers"]
        assert bf["n"] == 2
        assert bf["recall_strict"] == pytest.approx(0.5)
        assert bf["recall_loose"] == pytest.approx(1.0)
        assert bf["mean_fraud_score"] == pytest.approx((0.9 + 0.6) / 2)
        # 一对多 AUC 只拿该类型 vs 全部真实号（不含其他造假类型）
        assert bf["auc_vs_clean"] == pytest.approx(
            auc_by_definition([0.9, 0.6, 0.7, 0.1, 0.05], [1, 1, 0, 0, 0])
        )

    def test_absent_fraud_type_is_omitted_not_zero_filled(self) -> None:
        """没有样本的造假类型不该出现在表里（0 行会被误读成"这类全漏了"）。"""
        rep = fraud_report([rec_of("C1")], {"C1": result_of("C1")})
        assert rep["per_fraud_type"] == {}

    def test_reports_do_not_mutate_inputs(
        self, records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        snapshot = copy.deepcopy(records[:300])
        verdict_report(snapshot, results)
        fraud_report(snapshot, results)
        strata_report(snapshot, results)
        assert snapshot == copy.deepcopy(records[:300])

    def test_reports_are_deterministic(
        self, records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        assert fraud_report(records, results) == fraud_report(records, results)
        assert verdict_report(records, results) == verdict_report(records, results)


# ===========================================================================
# 4. 反自证守卫（本章核心）
# ===========================================================================
class TestAntiSelfProof:
    def test_shuffled_labels_collapse_auc_to_chance(
        self, records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        """把 gt 在样本间打乱后，AUC 必须回到 0.5 附近。

        这是整套评测"没有自证"的最强证据：
        如果指标计算里不小心用了引擎自己的判定当真值（或裁判字段被算进了分数），
        打乱标签**不会**让指标变差——它照样好看。
        n=5000 时 AUC 的抽样标准差约 0.008，取 ±0.05 已经很宽松。
        """
        shuffled = copy.deepcopy(records)
        gts = [r["gt"] for r in shuffled]
        random.Random(20240607).shuffle(gts)
        for rec, gt in zip(shuffled, gts):
            rec["gt"] = gt

        real = fraud_report(records, results)
        fake = fraud_report(shuffled, results)
        assert abs(fake["auc"] - 0.5) < 0.05, f"打乱标签后 AUC 仍为 {fake['auc']:.4f}"
        assert real["auc"] - fake["auc"] > 0.25, "真标签与随机标签的 AUC 差距太小，指标没有判别力"

    def test_shuffled_labels_collapse_precision_to_prevalence(
        self, records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        """打乱标签后精确率必须退化到"瞎猜"水平，即约等于水号占比。"""
        shuffled = copy.deepcopy(records)
        gts = [r["gt"] for r in shuffled]
        random.Random(99).shuffle(gts)
        for rec, gt in zip(shuffled, gts):
            rec["gt"] = gt
        fake = fraud_report(shuffled, results)
        assert fake["strict"]["precision"] == pytest.approx(fake["prevalence"], abs=0.05)
        assert fake["strict"]["f1"] < 0.35

    def test_constant_predictor_earns_nothing(
        self, records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        """常数分 + 全部判 pass 的"废物预测器"必须拿到 AUC 0.5、召回 0。

        这条防的是"指标实现里偷看了 gt"：一旦偷看，废物预测器也能拿到好看的数。
        """
        dud = {k: result_of(k, verdict="pass", fraud_score=0.42) for k in results}
        rep = fraud_report(records, dud)
        assert rep["auc"] == 0.5
        assert rep["strict"]["recall"] == 0.0
        assert rep["strict"]["tp"] == 0

    def test_all_pass_predictor_accuracy_equals_gt_pass_share(
        self, records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        """全判 pass 的准确率必须恰好等于 gt 里 pass 的占比——证明真值来自 gt 而非引擎自身。"""
        dud = {k: result_of(k, verdict="pass") for k in results}
        rep = verdict_report(records, dud)
        share = sum(1 for r in records if gt_of(r).get("verdict") == "pass") / len(records)
        assert rep["accuracy"] == pytest.approx(round(share, 4), abs=1e-4)
        assert rep["per_class"]["reject"]["recall"] == 0.0

    def test_missing_predictions_are_skipped_not_scored_as_correct(
        self, records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        """没有预测的记录必须被跳过（而不是当成猜对了）——否则少跑一半就能"提高"准确率。"""
        half = dict(list(results.items())[: len(results) // 2])
        rep = verdict_report(records, half)
        assert rep["n"] == len(half)
        full = verdict_report(records, results)
        assert rep["n"] < full["n"]

    def test_gt_verdict_is_the_only_source_of_truth(self) -> None:
        """把 gt 改掉，指标必须跟着变；若不变，说明真值不是从 gt 读的。"""
        recs = [rec_of("K1", verdict="reject", is_fraud=True, ftype="bought_followers")]
        res = {"K1": result_of("K1", verdict="reject", authenticity=0.1, fraud_score=0.9)}
        assert verdict_report(recs, res)["accuracy"] == 1.0
        flipped = copy.deepcopy(recs)
        flipped[0]["gt"]["verdict"] = "pass"
        assert verdict_report(flipped, res)["accuracy"] == 0.0


# ===========================================================================
# 5. 真实数据：区间 + 关系（不绑常量）
# ===========================================================================
@pytest.fixture(scope="class")
def fraud_rep(records: list[dict[str, Any]], results: dict[str, GateResult]) -> dict[str, Any]:
    return fraud_report(records, results)


class TestRealDataFraudMetrics:
    def test_headline_metrics_are_in_plausible_ranges(self, fraud_rep: dict[str, Any]) -> None:
        """区间刻意开得很宽：既要拦住"崩了"，也要拦住"好得不像真的"。

        上界同样重要——F1 > 0.95 在这个含 weak 偏移样本的数据集上是**不可能**的，
        真出现了，第一反应应该是怀疑泄漏，而不是庆祝。
        """
        s = fraud_rep["strict"]
        assert 0.55 < s["f1"] < 0.90, s
        assert 0.60 < s["precision"] < 0.95, s
        assert 0.45 < s["recall"] < 0.90, s
        assert 0.90 < s["specificity"] < 0.995, s
        assert 0.75 < fraud_rep["auc"] < 0.97, fraud_rep["auc"]
        assert s["n"] == 5000

    def test_prevalence_matches_injection_design(
        self, fraud_rep: dict[str, Any], records: list[dict[str, Any]]
    ) -> None:
        actual = sum(1 for r in records if gt_of(r).get("is_fraud")) / len(records)
        assert fraud_rep["prevalence"] == pytest.approx(actual, abs=1e-4)
        assert 0.10 < fraud_rep["prevalence"] < 0.25

    def test_loose_trades_precision_for_recall(self, fraud_rep: dict[str, Any]) -> None:
        """两个口径必须体现出**方向相反**的取舍，否则不构成两个口径。"""
        assert fraud_rep["loose"]["recall"] > fraud_rep["strict"]["recall"]
        assert fraud_rep["loose"]["precision"] < fraud_rep["strict"]["precision"]

    def test_confusion_counts_are_internally_consistent(self, fraud_rep: dict[str, Any]) -> None:
        for key in ("strict", "loose"):
            c = fraud_rep[key]
            assert c["tp"] + c["fp"] + c["fn"] + c["tn"] == c["n"]
            assert c["precision"] == pytest.approx(round(c["tp"] / (c["tp"] + c["fp"]), 4), abs=1e-4)
            assert c["recall"] == pytest.approx(round(c["tp"] / (c["tp"] + c["fn"]), 4), abs=1e-4)

    def test_every_fraud_type_is_partially_but_not_fully_caught(self, fraud_rep: dict[str, Any]) -> None:
        """四类造假都要有覆盖，且**都不许满召回**。

        满召回意味着该类型被造成了极端可分的样本（送分题），
        这个数据集刻意保留了 weak 偏移，所以召回不到 1 才是设计正确的表现。
        """
        per = fraud_rep["per_fraud_type"]
        assert set(per) == set(FRAUD_TYPES)
        for ftype, row in per.items():
            assert row["n"] > 0
            assert 0.2 < row["recall_strict"] < 1.0, f"{ftype}: {row}"
            assert row["recall_loose"] >= row["recall_strict"], ftype
            assert 0.6 < row["auc_vs_clean"] < 0.99, f"{ftype}: {row}"

    def test_hardest_fraud_type_is_reported_honestly(self, fraud_rep: dict[str, Any]) -> None:
        """必须存在明显的强弱差异（各类型召回不能整齐一致）。

        整齐一致往往是"用同一条规则一网打尽"的信号，那说明数据集把答案写在了输入里。
        """
        recalls = [row["recall_strict"] for row in fraud_rep["per_fraud_type"].values()]
        assert max(recalls) - min(recalls) > 0.05, recalls

    def test_definitions_are_shipped_with_the_numbers(self, fraud_rep: dict[str, Any]) -> None:
        assert {"strict", "loose", "auc", "auc_vs_clean"} <= set(fraud_rep["definitions"])


class TestRealDataVerdictMetrics:
    def test_three_class_metrics_in_range(
        self, records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        rep = verdict_report(records, results)
        assert 0.45 < rep["accuracy"] < 0.85, rep["accuracy"]
        assert 0.45 < rep["macro_f1"] < 0.85, rep["macro_f1"]
        for v in VERDICTS:
            assert 0.30 < rep["per_class"][v]["f1"] < 0.95, (v, rep["per_class"][v])

    def test_diagonal_dominates_each_row(
        self, records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        """每个真值档里，判对的那一格应当是最多的——否则就是系统性错档。"""
        m = confusion_matrix(records, results)
        for v in VERDICTS:
            assert m[v][v] == max(m[v].values()), (v, m[v])

    def test_no_catastrophic_confusion_between_pass_and_reject(
        self, records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        """跨两档的错（pass↔reject）是最贵的，占比必须很小；但**不许为 0**。

        恰好为 0 反而可疑：那通常意味着判定和 gt 用了同一套输入。
        """
        m = confusion_matrix(records, results)
        n = len(records)
        assert 0 < m["pass"]["reject"] / n < 0.05, m["pass"]["reject"]
        assert m["reject"]["pass"] / n < 0.05, m["reject"]["pass"]


@pytest.fixture(scope="class")
def strata_rep(records: list[dict[str, Any]], results: dict[str, GateResult]) -> dict[str, Any]:
    return strata_report(records, results)


class TestStrataReport:
    def test_every_slice_totals_match_full_sample(
        self, strata_rep: dict[str, Any], records: list[dict[str, Any]]
    ) -> None:
        for dim in ("by_platform", "by_follower_bucket", "by_country", "by_platform_bucket"):
            assert sum(c["n"] for c in strata_rep[dim].values()) == len(records), dim

    def test_cell_metrics_are_self_consistent(self, strata_rep: dict[str, Any]) -> None:
        for dim in ("by_platform", "by_follower_bucket", "by_country", "by_platform_bucket"):
            for key, c in strata_rep[dim].items():
                assert c["tp"] + c["fp"] + c["fn"] + c["tn"] == c["n"], (dim, key)
                assert c["positives"] == c["tp"] + c["fn"], (dim, key)
                assert c["low_support"] == (c["positives"] < MIN_SUPPORT_POSITIVE), (dim, key)
                assert 0.0 <= c["verdict_accuracy"] <= 1.0

    def test_weak_spots_are_sorted_and_exclude_low_support(self, strata_rep: dict[str, Any]) -> None:
        """弱项榜必须按 F1 升序，且**排除低支撑格**——用噪声当结论比不报还糟。"""
        weak = strata_rep["weak_spots"]
        assert 0 < len(weak) <= WEAK_SPOT_TOP_N
        assert [w["f1"] for w in weak] == sorted(w["f1"] for w in weak)
        dims = {
            "platform": "by_platform",
            "follower_bucket": "by_follower_bucket",
            "country": "by_country",
            "platform×bucket": "by_platform_bucket",
        }
        for w in weak:
            cell = strata_rep[dims[w["dimension"]]][w["cell"]]
            assert cell["low_support"] is False, w
            assert cell["positives"] >= MIN_SUPPORT_POSITIVE
            assert w["f1"] == cell["f1"] and w["note"]

    def test_weak_spots_are_actually_weaker_than_overall(
        self, strata_rep: dict[str, Any], records: list[dict[str, Any]], results: dict[str, GateResult]
    ) -> None:
        """榜首必须真的低于整体 F1，否则这张"弱项表"没有在暴露任何问题。"""
        overall = fraud_report(records, results)["strict"]["f1"]
        assert strata_rep["weak_spots"][0]["f1"] < overall


# ===========================================================================
# 6. 消融 / 敏感性（小样本，只验结构与算术）
# ===========================================================================
@pytest.fixture(scope="class")
def ablation_rep(small_records: list[dict[str, Any]]) -> dict[str, Any]:
    """消融要整表重跑 12 次门禁，故用 400 条小样本并按 class 缓存。"""
    thr = calibrate(small_records, None)
    return ablation_report(small_records, CampaignSpec(), thr)


class TestAblation:
    def test_delta_is_variant_minus_full(self, ablation_rep: dict[str, Any]) -> None:
        """delta 的方向必须是"变体 - 全量"。写反了整张表的结论会**完全颠倒**。"""
        full = ablation_rep["full"]
        for row in ablation_rep["by_layer"] + ablation_rep["by_g1_rule"]:
            for key in ("fraud_f1_strict", "fraud_auc", "verdict_accuracy", "verdict_macro_f1"):
                assert row["delta"][f"d_{key}"] == pytest.approx(
                    round(row["metrics"][key] - full[key], 4), abs=1e-4
                ), (row["variant"], key)

    def test_removing_g1_destroys_fraud_detection(self, ablation_rep: dict[str, Any]) -> None:
        """G1 是水号指标的**唯一**来源，关掉它 F1 必须塌掉。

        若关掉 G1 后 F1 还很好看，说明水号信号从别的层漏进来了（口径串味），
        表 1 就不再是在评测 G1。
        """
        row = next(r for r in ablation_rep["by_layer"] if r["variant"] == "-G1")
        assert row["metrics"]["fraud_f1_strict"] == 0.0, row["metrics"]
        assert row["delta"]["d_fraud_f1_strict"] < -0.3

    def test_each_layer_ablation_ships_a_role_note(self, ablation_rep: dict[str, Any]) -> None:
        variants = {r["variant"] for r in ablation_rep["by_layer"]}
        assert variants == {"-G0", "-G1", "-G2", "-G3", "only G1"}
        assert all(r["role"] for r in ablation_rep["by_layer"])

    def test_ablation_covers_every_g1_rule(self, ablation_rep: dict[str, Any]) -> None:
        assert {r["variant"] for r in ablation_rep["by_g1_rule"]} == {f"-{rid}" for rid in G1_WEIGHTS}
        for row in ablation_rep["by_g1_rule"]:
            assert row["weight"] > 0

    def test_dead_rules_are_consistent_and_disclosed(self, ablation_rep: dict[str, Any]) -> None:
        """「哪条规则没用」只能由 negligible 档推出，并写进 note——不许悄悄留着凑数。"""
        expected = [r["variant"] for r in ablation_rep["by_g1_rule"] if r["contribution"] == "negligible"]
        assert ablation_rep["dead_rules"] == expected
        assert ablation_rep["note"]
        if ablation_rep["dead_rules"]:
            assert all(v.lstrip("-") in ablation_rep["note"] for v in ablation_rep["dead_rules"])

    def test_contribution_is_signed_not_absolute(self, ablation_rep: dict[str, Any]) -> None:
        """回归（真实 bug）：`contributes` 曾是 `abs(delta) >= 0.001`。

        取绝对值会把"关掉它 F1 反而上升"的规则（定稿数据集上的 G1.6，ΔF1 = +0.0149）
        也标成"有贡献"，而产物 note 还跟着写「所有 G1 规则都有可测量的边际贡献」。
        这里把三档判定钉死：符号一旦被弄丢，这条测试立刻红。
        """
        eps = CONTRIBUTION_EPS
        for row in ablation_rep["by_g1_rule"]:
            d = row["delta"]["d_fraud_f1_strict"]
            expected = "positive" if d <= -eps else "negative" if d >= eps else "negligible"
            assert row["contribution"] == expected, (row["variant"], d)
            assert row["contributes"] is (expected == "positive"), row["variant"]
            assert row["contribution_note"]
        # 分类是三档的划分：并集 = 全部规则，且两两不相交
        buckets = (
            ablation_rep["positive_rules"],
            ablation_rep["negative_rules"],
            ablation_rep["dead_rules"],
        )
        all_variants = [r["variant"] for r in ablation_rep["by_g1_rule"]]
        assert sorted(v for b in buckets for v in b) == sorted(all_variants)

    def test_note_never_claims_universal_contribution_when_a_rule_is_negative(
        self, ablation_rep: dict[str, Any]
    ) -> None:
        """note 不许出现"所有 G1 规则都有贡献"这类会被负贡献规则直接推翻的总括句。"""
        note = ablation_rep["note"]
        assert "所有 G1 规则对严口径 F1 都有可测量的边际贡献" not in note
        for variant in ablation_rep["negative_rules"]:
            assert variant.lstrip("-") in note, f"{variant} 是负贡献，必须在 note 里点名"

    def test_layer_rows_also_carry_signed_contribution(self, ablation_rep: dict[str, Any]) -> None:
        """层级消融同样按符号分档（−G2 在定稿数据上让三分类准确率**上升**）。"""
        for row in ablation_rep["by_layer"]:
            for metric in ("fraud_f1_strict", "verdict_accuracy"):
                d = row["delta"][f"d_{metric}"]
                expected = (
                    "positive"
                    if d <= -CONTRIBUTION_EPS
                    else "negative"
                    if d >= CONTRIBUTION_EPS
                    else "negligible"
                )
                assert row["contribution"][metric] == expected, (row["variant"], metric)

    def test_disabling_a_rule_only_relaxes_detection(self, ablation_rep: dict[str, Any]) -> None:
        """关掉一条 G1 规则只可能让"判为水号"的人变少（reject 数不该增加）。

        若某条规则关掉后 reject 反而更多，说明规则之间存在耦合副作用，
        那消融就不是在做受控实验。
        """
        full_reject = ablation_rep["full"]["n_reject"]
        for row in ablation_rep["by_g1_rule"]:
            assert row["metrics"]["n_reject"] <= full_reject, row["variant"]


@pytest.fixture(scope="class")
def sens_rep(small_records: list[dict[str, Any]]) -> dict[str, Any]:
    """敏感性要跑 4 + 8×2 次门禁，同样用小样本并按 class 缓存。"""
    thr = calibrate(small_records, None)
    return sensitivity_report(small_records, CampaignSpec(), thr)


class TestSensitivity:
    def test_factor_one_row_equals_baseline_exactly(self, sens_rep: dict[str, Any]) -> None:
        """factor=1.0 必须与基线**逐字段相等**，否则整条曲线的参照点就是错的。"""
        row = next(r for r in sens_rep["overall"] if r["factor"] == 1.0)
        assert row["metrics"] == sens_rep["baseline"]
        assert row["d_fraud_f1_strict"] == 0.0
        assert row["d_verdict_accuracy"] == 0.0

    def test_scan_covers_required_range(self, sens_rep: dict[str, Any]) -> None:
        assert tuple(sens_rep["factors"]) == SCAN_FACTORS
        assert min(SCAN_FACTORS) <= 0.8 and max(SCAN_FACTORS) >= 1.2, "SPEC 要求至少 ±20%"
        assert [r["factor"] for r in sens_rep["overall"]] == list(SCAN_FACTORS)

    def test_stability_verdict_is_derived_not_asserted(self, sens_rep: dict[str, Any]) -> None:
        """`stable` 必须由实际最大偏移与容差比出来，而不是硬编码成 True。"""
        shifts = [abs(r["d_fraud_f1_strict"]) for r in sens_rep["overall"]]
        assert sens_rep["max_abs_f1_shift"] == pytest.approx(round(max(shifts), 4))
        assert sens_rep["stability_tolerance"] == STABILITY_TOLERANCE
        assert sens_rep["stable"] == (sens_rep["max_abs_f1_shift"] <= STABILITY_TOLERANCE)

    def test_thresholds_actually_move_the_metrics(self, sens_rep: dict[str, Any]) -> None:
        """扫描必须真的改变了判定：全 0 偏移意味着 `scaled()` 没生效，那这张表是假的。"""
        assert sens_rep["max_abs_f1_shift"] > 0.0, "±20% 缩放后指标一点没动，敏感性扫描形同虚设"

    def test_per_signal_rows_sorted_by_sensitivity(self, sens_rep: dict[str, Any]) -> None:
        shifts = [r["max_abs_shift"] for r in sens_rep["per_signal"]]
        assert shifts == sorted(shifts, reverse=True)
        assert sens_rep["most_sensitive_signal"] == sens_rep["per_signal"][0]["signal"]
        for row in sens_rep["per_signal"]:
            assert [p["factor"] for p in row["points"]] == [0.8, 1.2]
            assert row["max_abs_shift"] == pytest.approx(
                round(max(abs(p["delta"]) for p in row["points"]), 4)
            )

    def test_scaled_thresholds_do_not_mutate_the_original(
        self, small_records: list[dict[str, Any]]
    ) -> None:
        """`scaled()` 必须返回新对象。原地改会让后续所有表都跑在被污染的阈值上。"""
        thr = calibrate(small_records, None)
        before = copy.deepcopy(thr.groups)
        scaled = thr.scaled(1.2)
        assert thr.groups == before
        assert isinstance(scaled, Thresholds) and scaled is not thr
