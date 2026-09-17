"""多种子稳健性实验：把单点结论升级为带波动范围的结论。

为什么需要这个模块
------------------
``output/metrics.json`` 里的核心价值主张（"合计 $245,000 预算少浪费 $79,719 / 32.5%，
有效曝光 +98.6%"）是在**单一随机种子** ``SEED=20270919`` 下跑出来的**一个样本点**。
单点数字回答不了三个必然被追问的问题：

1. 32.5% 的波动范围是多少？换个种子还成立吗？
2. BRIEF-002 上 KOXPilot 输给基线，是"能力不行"还是"基线运气好"（高方差赌博）？
3. 表 3 报出的三个弱项（``follower_bucket=mega`` / ``country=SA`` /
   ``platform×bucket=youtube|macro``）是**系统性短板**还是**单次采样噪声**？

本模块对 N 个种子各跑一遍完整确定性链路，给出 mean / std / min / max / median /
95% CI，并对上面第 2、3 个问题做**可被否证的统计检验**。

口径与纪律
----------
- **纯确定性、零 LLM**：只调 datagen / gates / budget / eval 里的纯函数，不碰 ``llm`` 包。
- **全程内存**：不落任何中间盘、不读写 ``data/``，唯一产物是 ``output/multiseed.json``。
  因此也**不存在**跨进程共享缓存被互相覆盖的风险（``Cache`` 类无跨进程锁）。
- **复用而非重写**：门禁+阈值走 :func:`..eval.harness.build_context`，预算两臂走
  :func:`..eval.harness.budget_section`（保留其"消融/敏感性用中性 spec、预算用真实
  ``ctx.specs``"的刻意区分），价值账走 :func:`..eval.audit.counterfactual_report`。
  多种子实验绝不自己拼一套平行逻辑，否则两套实现漂移后谁也说不清哪个数才对。
- **统计量手算、零第三方依赖**：t 分位数与 t/F 检验 p 值用正则化不完全 Beta 函数实现
  （见下方 ``_betainc``），与项目"零运行依赖、TS 侧可逐行复算"的约束一致。
- **结论不利也照报**：本模块只负责把数字算出来，判断句在 ``docs/06-robustness.md``。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from ..datagen.briefs import build_briefs
from ..datagen.config import DATASET_VERSION, N_KOX, SEED
from ..datagen.generator import generate_dataset
from ..eval.audit import counterfactual_report
from ..eval.harness import EVAL_VERSION, budget_section, build_context
from ..eval.metrics import fraud_report, verdict_report
from ..eval.strata import MIN_SUPPORT_POSITIVE, strata_report
from ..gates.thresholds import THRESHOLDS_VERSION

__all__ = [
    "MULTISEED_VERSION",
    "DEFAULT_N_SEEDS",
    "SEED_STEP",
    "KNOWN_WEAK_SPOTS",
    "WEAK_MARGIN",
    "LOW_WASTE_SHARE",
    "HIGH_WASTE_SHARE",
    "seed_list",
    "summarize",
    "run_seed",
    "run_multiseed",
    "format_summary",
]

MULTISEED_VERSION = "1.0.0"

#: 默认种子数。单个种子约 2.3s（datagen 0.3 + 门禁 0.5 + 预算 1.5），12 个约 30s。
DEFAULT_N_SEEDS = 12

#: 派生步长：seed_i = SEED + i * SEED_STEP。7919 是素数，避免与 datagen 内部
#: 按 index 派生子种子的步长产生公因子共振（那会让"不同种子"其实高度相关）。
SEED_STEP = 7919

#: 单种子下 metrics.json 报出的三个弱项，本实验要检验它们是否稳定复现。
KNOWN_WEAK_SPOTS: tuple[tuple[str, str, float], ...] = (
    ("follower_bucket", "mega", 0.4667),
    ("country", "SA", 0.4762),
    ("platform×bucket", "youtube|macro", 0.4762),
)

#: 判定"该分层明显弱于全局"的 F1 落差阈值。0.15 的来源：单种子下三个弱项与全局 F1
#: (0.7200) 的落差为 0.24~0.25，而 MIN_SUPPORT_POSITIVE=10 附近的格子仅靠采样噪声
#: 就能抖动 ±0.10 量级，故取两者之间的 0.15 作为"幅度显著"的门槛。
WEAK_MARGIN = 0.15

#: 基线"恰好没踩坑"的判定线（浪费率 < 5%）
LOW_WASTE_SHARE = 0.05

#: 基线"踩大坑"的判定线（浪费率 > 50%）
HIGH_WASTE_SHARE = 0.50


# ---------------------------------------------------------------------------
# 统计工具（手写，零第三方依赖）
# ---------------------------------------------------------------------------
def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: Sequence[float]) -> float:
    """样本标准差（ddof=1）。种子是从"所有可能种子"里抽的样本，不是总体，故用 n-1。"""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _betacf(a: float, b: float, x: float) -> float:
    """连分数展开（Lentz 算法），供 :func:`_betainc` 使用。"""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """正则化不完全 Beta 函数 I_x(a, b)，t / F 分布的 CDF 都由它给出。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta + b * math.log1p(-x) + a * math.log(x)) * _betacf(b, a, 1.0 - x) / b


def _t_sf(t: float, df: float) -> float:
    """Student-t 单侧尾概率 P(T > t)（t >= 0 时）。"""
    if df <= 0:
        return float("nan")
    x = df / (df + t * t)
    tail = 0.5 * _betainc(df / 2.0, 0.5, x)
    return tail if t >= 0 else 1.0 - tail


def _t_ppf975(df: float) -> float:
    """t 分布 97.5% 分位数（二分求解，避免硬编码查表）。"""
    if df <= 0:
        return float("nan")
    lo, hi = 0.0, 100.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _t_sf(mid, df) > 0.025:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _sig(x: float, digits: int = 3) -> float:
    """按有效数字保留（p 值用）。``round(1e-9, 6)`` 会变成 0.0，读起来像「恰好为 0」，不诚实。"""
    if x == 0.0 or not math.isfinite(x):
        return x
    return float(f"{x:.{digits}g}")


def t_test_one_sample(xs: Sequence[float], mu0: float = 0.0) -> dict[str, Any]:
    """单样本 t 检验（也用于配对差值检验）。返回 t / df / 双侧 p。"""
    n = len(xs)
    if n < 2:
        return {"n": n, "t": None, "df": None, "p_two_sided": None}
    sd = _std(xs)
    # 相对容差而不是 sd == 0.0：常数序列经浮点运算后 sd 可能是 1e-17 而非严格 0，
    # 那会让 t 爆到 1e16 并报出 p=1e-33 这种假精度数字。
    if sd <= 1e-12 * max(1.0, abs(_mean(xs))):
        return {
            "n": n,
            "t": None,
            "df": n - 1,
            "p_two_sided": 0.0 if _mean(xs) != mu0 else 1.0,
            "note": "样本方差为 0（跨种子完全一致），t 统计量无定义",
        }
    t = (_mean(xs) - mu0) / (sd / math.sqrt(n))
    return {
        "n": n,
        "t": round(t, 4),
        "df": n - 1,
        "p_two_sided": _sig(2.0 * _t_sf(abs(t), n - 1)),
    }


def f_test_variance_ratio(a: Sequence[float], b: Sequence[float]) -> dict[str, Any]:
    """方差比 F 检验（H0: var(a) == var(b)）。

    ⚠️ F 检验假设两组都近似正态；浪费率是有界比例，尾部并不正态，故本文件同时给出
    **分布无关**的对照证据（极端频次计数），不把结论只押在这个 p 值上。
    """
    va, vb = _std(a) ** 2, _std(b) ** 2
    if len(a) < 2 or len(b) < 2 or vb == 0.0 or va == 0.0:
        return {
            "var_a": round(va, 8),
            "var_b": round(vb, 8),
            "f": None,
            "p_two_sided": None,
            "note": "一侧方差为 0 或样本不足，F 检验不适用",
        }
    f = va / vb
    d1, d2 = len(a) - 1, len(b) - 1
    x = d2 / (d2 + d1 * f)
    sf = _betainc(d2 / 2.0, d1 / 2.0, x)  # P(F > f)
    p = 2.0 * min(sf, 1.0 - sf)
    return {
        "var_a": round(va, 8),
        "var_b": round(vb, 8),
        "std_a": round(math.sqrt(va), 6),
        "std_b": round(math.sqrt(vb), 6),
        "f": round(f, 4),
        "df": [d1, d2],
        "p_two_sided": _sig(min(1.0, p)),
    }


def _opt_float(value: Any) -> float | None:
    """``None`` 透传，其余转 float。

    审计里"分母为 0 的相对提升"现在是 ``None``（无定义）而不是 0.0，
    这里必须把这个"无定义"原样带到产物里——用 0.0 顶替会让"基线全打水漂"
    的种子在跨种子统计里变成"零提升"，把最有利的样本记成中性。
    """
    return None if value is None else float(value)


def _defined(values: Sequence[Any]) -> list[float]:
    """只保留有定义的取值（丢掉 ``None``）。

    统计前必须显式丢掉"无定义"，而不是把它当 0 参与平均；
    丢了多少条会在产物里以 ``n_undefined_denominator`` 报出来，不做静默处理。
    """
    return [float(v) for v in values if v is not None]


def summarize(values: Sequence[float], digits: int = 4) -> dict[str, Any]:
    """一组跨种子取值的描述统计 + 均值的 95% CI（t 分布，小样本不用正态近似）。"""
    xs = [float(v) for v in values]
    n = len(xs)
    if n == 0:
        return {"n": 0}
    m, sd = _mean(xs), _std(xs)
    half = _t_ppf975(n - 1) * sd / math.sqrt(n) if n >= 2 and sd > 0 else 0.0
    return {
        "n": n,
        "mean": round(m, digits),
        "std": round(sd, digits),
        "min": round(min(xs), digits),
        "max": round(max(xs), digits),
        "median": round(_median(xs), digits),
        "ci95_low": round(m - half, digits),
        "ci95_high": round(m + half, digits),
        "cv": round(sd / abs(m), digits) if m else None,
    }


def seed_list(n_seeds: int = DEFAULT_N_SEEDS, base: int = SEED, step: int = SEED_STEP) -> list[int]:
    """确定性派生种子列表：``[base, base+step, base+2*step, ...]``，首个必为原始种子。"""
    if n_seeds < 1:
        raise ValueError("n_seeds 必须 >= 1")
    return [base + i * step for i in range(n_seeds)]


# ---------------------------------------------------------------------------
# 单个种子：完整确定性链路
# ---------------------------------------------------------------------------
#: 表 3 四种切法在 payload 里的统一命名（``dim::cell``），与 weak_spots 的 dimension 对齐
_STRATA_DIMS: tuple[tuple[str, str], ...] = (
    ("platform", "by_platform"),
    ("follower_bucket", "by_follower_bucket"),
    ("country", "by_country"),
    ("platform×bucket", "by_platform_bucket"),
)


def run_seed(seed: int, n: int = N_KOX) -> dict[str, Any]:
    """跑一个种子的完整链路，返回该种子的紧凑结果（**全程内存，不落盘、不调 LLM**）。

    链路：``generate_dataset`` → ``build_context``（标定分位数阈值 + 全库四层门禁）
    → 表 1/2/3 → ``budget_section``（3 个 brief 的两臂预算）→ ``counterfactual_report``。
    brief 由 ``build_briefs()`` 现造：它是**写死的常量**（不含随机性），
    所以跨种子唯一变的是达人库，campaign 定义保持同一份，价值差额才可归因到数据随机性。
    """
    dataset = generate_dataset(n, seed)
    records = dataset["kox"]
    meta = dataset["meta"]
    briefs = list(build_briefs()["briefs"])

    ctx = build_context(records, meta, briefs)  # 阈值按本种子的库重新标定（不复用定稿阈值）
    t1 = fraud_report(records, ctx.results)
    t2 = verdict_report(records, ctx.results)
    t3 = strata_report(records, ctx.results)

    budget_rows, plans, baselines = budget_section(ctx)  # 复用 harness 的两臂编排（公开 API）
    cf = counterfactual_report(records, plans, baselines)

    pools = {str(r["campaign_id"]): int(r["candidate_pool"]) for r in budget_rows["per_campaign"]}
    constraints_ok = all(bool(r["constraints_ok"]) for r in budget_rows["per_campaign"])

    per_campaign: list[dict[str, Any]] = []
    for row in cf["per_campaign"]:
        base, kox = row["baseline"], row["koxpilot"]
        per_campaign.append(
            {
                "campaign_id": row["campaign_id"],
                "budget_usd": float(row["budget_usd"]),
                "candidate_pool": pools.get(str(row["campaign_id"])),
                "baseline_n_selected": int(base["n_selected"]),
                "baseline_wasted_usd": float(base["wasted_spend_usd"]),
                "baseline_waste_share": float(base["wasted_spend_share"]),
                "baseline_n_fraud": int(base["n_fraud_selected"]),
                "baseline_n_high_risk": int(base["n_high_risk_selected"]),
                "koxpilot_n_selected": int(kox["n_selected"]),
                "koxpilot_wasted_usd": float(kox["wasted_spend_usd"]),
                "koxpilot_waste_share": float(kox["wasted_spend_share"]),
                "koxpilot_n_fraud": int(kox["n_fraud_selected"]),
                "saved_usd": float(row["saved_usd"]),
                "saved_share_of_budget": float(row["saved_share_of_budget"]),
                # 相对提升可能为 None（基线有效曝光为 0 = 分母无定义），不能无脑 float()。
                "effective_view_uplift": _opt_float(row["effective_view_uplift"]),
                "effective_view_uplift_lenient": _opt_float(row["effective_view_uplift_lenient"]),
                # 有界口径：跨种子聚合只用这两个，避免分母趋 0 时 mean 被单个种子绑架
                "effective_view_rate_gap_pp": _opt_float(
                    row["effective_view_uplift_bounded"]["rate_gap_pp"]
                ),
                "effective_view_symmetric_uplift": _opt_float(
                    row["effective_view_uplift_bounded"]["symmetric_uplift"]
                ),
                "uplift_ratio_denominator_fragile": bool(
                    row["effective_view_uplift_bounded"]["ratio_denominator_fragile"]
                ),
                # "跑输基线"的口径：本 campaign 上 KOXPilot 的浪费金额高于基线（saved < 0）
                "koxpilot_loses": float(row["saved_usd"]) < 0.0,
            }
        )

    totals = cf["totals"]
    strata_cells: dict[str, dict[str, Any]] = {}
    for dim, key in _STRATA_DIMS:
        for cell, stats in (t3.get(key) or {}).items():
            strata_cells[f"{dim}::{cell}"] = {
                "f1": float(stats["f1"]),
                "precision": float(stats["precision"]),
                "recall": float(stats["recall"]),
                "positives": int(stats["positives"]),
                "n": int(stats["n"]),
                "low_support": bool(stats["low_support"]),
            }

    return {
        "seed": seed,
        "dataset": {
            "n": len(records),
            "injection_actual_rates": meta["injection_actual_rates"],
            "verdict_counts": meta["verdict_counts"],
        },
        "value": {
            "budget_usd": float(totals["budget_usd"]),
            "saved_usd": float(totals["saved_usd"]),
            "saved_share_of_budget": float(totals["saved_share_of_budget"]),
            "effective_view_uplift": _opt_float(totals["effective_view_uplift"]),
            # 宽松口径（水号曝光按 50% 计）：用于确认结论不依赖"水号曝光全废"这个保守假设。
            # 现在直接取 audit 的 totals（同源），不再在这里重算一遍——两处算法漂移过一次就够了。
            "effective_view_uplift_lenient": _opt_float(totals["effective_view_uplift_lenient"]),
            # 总口径的有界指标：分母是三个 campaign 汇总的名义曝光，本身不脆弱，
            # 但保留它可以让"总口径"和"per-campaign"用同一把尺子比较。
            "effective_view_rate_gap_pp": _opt_float(
                totals["effective_view_uplift_bounded"]["rate_gap_pp"]
            ),
            "effective_view_symmetric_uplift": _opt_float(
                totals["effective_view_uplift_bounded"]["symmetric_uplift"]
            ),
            "koxpilot_loses_overall": float(totals["saved_usd"]) < 0.0,
            "all_constraints_satisfied": constraints_ok,
        },
        "per_campaign": per_campaign,
        "gates": {
            "fraud_precision_strict": float(t1["strict"]["precision"]),
            "fraud_recall_strict": float(t1["strict"]["recall"]),
            "fraud_f1_strict": float(t1["strict"]["f1"]),
            "fraud_f1_loose": float(t1["loose"]["f1"]),
            "fraud_auc": float(t1["auc"]),
            "verdict_accuracy": float(t2["accuracy"]),
            "verdict_macro_f1": float(t2["macro_f1"]),
            "reject_f1": float(t2["per_class"]["reject"]["f1"]),
            "pass_precision": float(t2["per_class"]["pass"]["precision"]),
        },
        "strata": {
            "global_fraud_f1": float(t1["strict"]["f1"]),
            "cells": strata_cells,
            "top_weak": [f"{w['dimension']}::{w['cell']}" for w in t3["weak_spots"]],
        },
    }


# ---------------------------------------------------------------------------
# 汇总 A：价值主张稳健性
# ---------------------------------------------------------------------------
def value_robustness(per_seed: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """跨种子汇总"少浪费多少钱 / 有效曝光提升多少"，并给出跑输次数。

    聚合口径的纪律：**比率不聚合，有界指标才聚合。** per-campaign 的
    ``effective_view_uplift`` 分母是基线有效曝光，某些种子它趋近 0，
    比率会放大到几百倍（实测单 campaign 出现过 +3585×），mean 直接失真。
    因此 per-campaign 的主结论改用 ``effective_view_rate_gap_pp``（∈[−100,100]）与
    ``effective_view_symmetric_uplift``（∈[−1,1]）；原比率仍照实给出，
    但明确标注"仅供参考、看 median"，并附上分母脆弱的种子数。
    """
    saved = [float(s["value"]["saved_usd"]) for s in per_seed]
    share = [float(s["value"]["saved_share_of_budget"]) for s in per_seed]
    uplift = _defined([s["value"]["effective_view_uplift"] for s in per_seed])
    uplift_l = _defined([s["value"]["effective_view_uplift_lenient"] for s in per_seed])
    gap_pp = _defined([s["value"].get("effective_view_rate_gap_pp") for s in per_seed])
    sym = _defined([s["value"].get("effective_view_symmetric_uplift") for s in per_seed])

    campaign_ids = [c["campaign_id"] for c in per_seed[0]["per_campaign"]]
    per_campaign: dict[str, Any] = {}
    for cid in campaign_ids:
        rows = [c for s in per_seed for c in s["per_campaign"] if c["campaign_id"] == cid]
        losses = [int(s["seed"]) for s in per_seed for c in s["per_campaign"]
                  if c["campaign_id"] == cid and c["koxpilot_loses"]]
        fragile = [
            int(s["seed"]) for s in per_seed for c in s["per_campaign"]
            if c["campaign_id"] == cid and c.get("uplift_ratio_denominator_fragile")
        ]
        ratio_vals = _defined([r["effective_view_uplift"] for r in rows])
        per_campaign[cid] = {
            "budget_usd": rows[0]["budget_usd"],
            "saved_usd": summarize([r["saved_usd"] for r in rows], 2),
            "saved_share_of_budget": summarize([r["saved_share_of_budget"] for r in rows]),
            # 主口径（有界，可聚合）
            "effective_view_rate_gap_pp": summarize(
                _defined([r.get("effective_view_rate_gap_pp") for r in rows]), 2
            ),
            "effective_view_symmetric_uplift": summarize(
                _defined([r.get("effective_view_symmetric_uplift") for r in rows])
            ),
            # 参考口径（无界，分母趋 0 会爆炸）——保留数字，但标清不可当结论
            "effective_view_uplift_ratio_reference": {
                **summarize(ratio_vals),
                "n_undefined_denominator": len(rows) - len(ratio_vals),
                "n_seeds_denominator_fragile": len(fragile),
                "seeds_denominator_fragile": fragile,
                "warning": (
                    "无界比率：分母是基线有效曝光。mean 会被分母趋 0 的种子绑架，"
                    "只看 median，且结论请引用 effective_view_rate_gap_pp。"
                ),
            },
            "n_seeds_koxpilot_loses": len(losses),
            "seeds_koxpilot_loses": losses,
        }

    overall_losses = [int(s["seed"]) for s in per_seed if s["value"]["koxpilot_loses_overall"]]
    n = len(per_seed)
    sig = t_test_one_sample(share, 0.0)
    return {
        "definition": {
            "saved_usd": "基线浪费金额 − KOXPilot 浪费金额（浪费 = 花在 gt 水号或 gt 高风险人身上的钱）",
            "saved_share_of_budget": "saved_usd / 总预算（$245,000）",
            "effective_view_uplift": "按 gt 计的有效曝光相对基线的提升（主口径：水号曝光按 0 计）",
            "effective_view_rate_gap_pp": (
                "两臂有效曝光率（有效/名义）之差，单位百分点，恒在 [−100,100]；"
                "不以基线为分母，可跨 campaign / 跨种子聚合"
            ),
            "effective_view_symmetric_uplift": "(kox − base)/(kox + base)，恒在 [−1,1]，符号同相对提升",
            "koxpilot_loses": "该口径下 saved_usd < 0，即 KOXPilot 比基线更浪费",
        },
        "saved_usd": summarize(saved, 2),
        "saved_share_of_budget": summarize(share),
        "effective_view_uplift": summarize(uplift),
        "effective_view_uplift_lenient": summarize(uplift_l),
        "effective_view_rate_gap_pp": summarize(gap_pp, 2),
        "effective_view_symmetric_uplift": summarize(sym),
        "significance_saved_share_vs_zero": sig,
        "n_seeds": n,
        "n_seeds_koxpilot_loses_overall": len(overall_losses),
        "seeds_koxpilot_loses_overall": overall_losses,
        "win_rate_overall": round((n - len(overall_losses)) / n, 4) if n else 0.0,
        "per_campaign": per_campaign,
        "per_campaign_uplift_caveat": (
            "per_campaign 的 effective_view_uplift 是比率，分母是**基线**的有效曝光；"
            "基线只选 4~7 人，某些种子几乎把钱全花在水号上（浪费率 >99%），"
            "分母趋近 0 会让该比率放大到几百倍，其跨种子 mean 因此没有意义。"
            "该比率已降级到 per_campaign[*].effective_view_uplift_ratio_reference（仅供参考，看 median），"
            "per-campaign 的可聚合结论请用 effective_view_rate_gap_pp（∈[−100,100] pp）"
            "或 effective_view_symmetric_uplift（∈[−1,1]）。"
            "总口径（三个 campaign 汇总）的分母足够大，其相对提升仍可直接引用。"
        ),
        "all_constraints_satisfied_every_seed": all(
            bool(s["value"]["all_constraints_satisfied"]) for s in per_seed
        ),
    }


# ---------------------------------------------------------------------------
# 汇总 B：BRIEF-002 现象的方差归因
# ---------------------------------------------------------------------------
def variance_attribution(per_seed: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """检验"基线是高方差赌博、KOXPilot 是低方差稳定"这个假设。

    检验方式三条并列（任何一条单独都不足以定论）：
    1. 浪费率的跨种子 std 对比 + 方差比 F 检验（有正态性假设，故仅作参考）；
    2. **分布无关**的极端频次：基线浪费率 < 5%（恰好没踩坑）与 > 50%（踩大坑）的次数；
    3. 选中人数量级对比（小样本才可能有大方差，这是假设的机制前提）。
    """
    campaign_ids = [c["campaign_id"] for c in per_seed[0]["per_campaign"]]
    per_campaign: dict[str, Any] = {}
    all_base_share: list[float] = []
    all_kox_share: list[float] = []

    for cid in campaign_ids:
        rows = [c for s in per_seed for c in s["per_campaign"] if c["campaign_id"] == cid]
        base_share = [r["baseline_waste_share"] for r in rows]
        kox_share = [r["koxpilot_waste_share"] for r in rows]
        all_base_share.extend(base_share)
        all_kox_share.extend(kox_share)
        base_std, kox_std = _std(base_share), _std(kox_share)
        per_campaign[cid] = {
            "budget_usd": rows[0]["budget_usd"],
            "baseline_n_selected": summarize([r["baseline_n_selected"] for r in rows], 2),
            "koxpilot_n_selected": summarize([r["koxpilot_n_selected"] for r in rows], 2),
            "baseline_waste_share": summarize(base_share),
            "koxpilot_waste_share": summarize(kox_share),
            "std_ratio_baseline_over_koxpilot": round(base_std / kox_std, 3) if kox_std else None,
            "variance_ratio_test": f_test_variance_ratio(base_share, kox_share),
            "n_seeds_baseline_share_below_5pct": sum(1 for x in base_share if x < LOW_WASTE_SHARE),
            "n_seeds_baseline_share_above_50pct": sum(1 for x in base_share if x > HIGH_WASTE_SHARE),
            "n_seeds_koxpilot_share_below_5pct": sum(1 for x in kox_share if x < LOW_WASTE_SHARE),
            "n_seeds_koxpilot_share_above_50pct": sum(1 for x in kox_share if x > HIGH_WASTE_SHARE),
            "n_seeds_koxpilot_loses": sum(1 for r in rows if r["koxpilot_loses"]),
            "paired_diff_base_minus_kox_share": t_test_one_sample(
                [b - k for b, k in zip(base_share, kox_share, strict=True)], 0.0
            ),
            "per_seed": [
                {
                    "seed": s["seed"],
                    "baseline_n": c["baseline_n_selected"],
                    "baseline_waste_share": c["baseline_waste_share"],
                    "koxpilot_n": c["koxpilot_n_selected"],
                    "koxpilot_waste_share": c["koxpilot_waste_share"],
                    "saved_usd": c["saved_usd"],
                }
                for s in per_seed
                for c in s["per_campaign"]
                if c["campaign_id"] == cid
            ],
        }

    pooled_base_std, pooled_kox_std = _std(all_base_share), _std(all_kox_share)
    all_obs = [c for s in per_seed for c in s["per_campaign"]]
    return {
        "hypothesis": (
            "基线只选个位数人 → 样本极小 → 浪费率方差极大（高方差赌博）；"
            "KOXPilot 分散到几十人 → 浪费率方差小（低方差稳定）。"
            "若成立，应看到 std(基线浪费率) ≫ std(KOXPilot 浪费率)，"
            "且基线在部分种子上浪费率 <5%（运气好）、部分种子上 >50%（踩大坑）。"
        ),
        "thresholds": {
            "low_waste_share": LOW_WASTE_SHARE,
            "high_waste_share": HIGH_WASTE_SHARE,
        },
        "pooled_all_campaigns": {
            "baseline_waste_share": summarize(all_base_share),
            "koxpilot_waste_share": summarize(all_kox_share),
            "std_ratio_baseline_over_koxpilot": round(pooled_base_std / pooled_kox_std, 3)
            if pooled_kox_std
            else None,
            "variance_ratio_test": f_test_variance_ratio(all_base_share, all_kox_share),
            "n_obs_baseline_share_below_5pct": sum(1 for x in all_base_share if x < LOW_WASTE_SHARE),
            "n_obs_baseline_share_above_50pct": sum(
                1 for x in all_base_share if x > HIGH_WASTE_SHARE
            ),
            "n_obs_koxpilot_share_below_5pct": sum(1 for x in all_kox_share if x < LOW_WASTE_SHARE),
            "n_obs_koxpilot_share_above_50pct": sum(
                1 for x in all_kox_share if x > HIGH_WASTE_SHARE
            ),
            "n_obs": len(all_base_share),
        },
        "per_campaign": per_campaign,
        "conditional_win_rate_by_baseline_luck": _conditional_win_rate(all_obs),
        "caveat": (
            "F 检验假设两组近似正态，而浪费率是 [0,1] 有界比例（KOXPilot 一侧还紧贴下界），"
            "尾部不正态会让 p 值偏乐观。因此结论以「std 量级差异 + 极端频次计数」为主，"
            "F 检验 p 值只作参考。"
        ),
    }


def _conditional_win_rate(obs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """按「基线这一把运气如何」分档，看 KOXPilot 的胜负分布。

    这是对方差假设最直接的检验：如果 KOXPilot 的失败**只**发生在
    「基线恰好没踩坑」的那些观测上，那么"输"就是基线运气好而非 KOXPilot 能力差；
    反之如果在基线踩了大坑时 KOXPilot 也会输，那才是能力问题。
    每个观测 = 一个 (种子, campaign) 组合。
    """
    bands: list[tuple[str, float, float]] = [
        (f"baseline_waste_share < {LOW_WASTE_SHARE:.0%}（基线运气好，几乎没踩坑）", -1.0, LOW_WASTE_SHARE),
        (
            f"{LOW_WASTE_SHARE:.0%} ≤ baseline_waste_share ≤ {HIGH_WASTE_SHARE:.0%}（基线中等）",
            LOW_WASTE_SHARE,
            HIGH_WASTE_SHARE,
        ),
        (f"baseline_waste_share > {HIGH_WASTE_SHARE:.0%}（基线踩大坑）", HIGH_WASTE_SHARE, 2.0),
    ]
    rows: list[dict[str, Any]] = []
    for label, lo, hi in bands:
        sel = [
            o
            for o in obs
            if (lo < float(o["baseline_waste_share"]) <= hi)
            or (lo < 0 and float(o["baseline_waste_share"]) < hi)
        ]
        losses = [o for o in sel if bool(o["koxpilot_loses"])]
        rows.append(
            {
                "band": label,
                "n_obs": len(sel),
                "n_koxpilot_loses": len(losses),
                "koxpilot_win_rate": round(1 - len(losses) / len(sel), 4) if sel else None,
                "mean_saved_usd": round(_mean([float(o["saved_usd"]) for o in sel]), 2) if sel else None,
                "mean_koxpilot_waste_share": round(
                    _mean([float(o["koxpilot_waste_share"]) for o in sel]), 4
                )
                if sel
                else None,
            }
        )
    losses_all = [o for o in obs if bool(o["koxpilot_loses"])]
    return {
        "unit": "一个观测 = 一个 (seed, campaign) 组合",
        "n_obs": len(obs),
        "n_koxpilot_loses": len(losses_all),
        "bands": rows,
        "max_baseline_waste_share_among_koxpilot_losses": round(
            max((float(o["baseline_waste_share"]) for o in losses_all), default=0.0), 4
        ),
        "mean_koxpilot_waste_share_when_losing": round(
            _mean([float(o["koxpilot_waste_share"]) for o in losses_all]), 4
        )
        if losses_all
        else None,
    }


# ---------------------------------------------------------------------------
# 汇总 C：门禁质量指标稳健性
# ---------------------------------------------------------------------------
_GATE_KEYS: tuple[str, ...] = (
    "fraud_precision_strict",
    "fraud_recall_strict",
    "fraud_f1_strict",
    "fraud_f1_loose",
    "fraud_auc",
    "verdict_accuracy",
    "verdict_macro_f1",
    "reject_f1",
    "pass_precision",
)


def gate_metric_robustness(per_seed: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """门禁能力指标的跨种子 mean ± std：用来说明"能力本身不依赖特定种子"。"""
    out = {k: summarize([float(s["gates"][k]) for s in per_seed]) for k in _GATE_KEYS}
    return {
        "note": (
            "每个种子都重新标定分位数阈值、重新跑全库四层门禁；"
            "指标口径与 metrics.json 表 1/表 2 完全一致（复用同一批函数）。"
        ),
        "metrics": out,
    }


# ---------------------------------------------------------------------------
# 汇总 D：已知弱项是否稳定复现
# ---------------------------------------------------------------------------
def weak_spot_stability(per_seed: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """区分"系统性弱点"与"单次采样噪声"。

    对每个已知弱项，逐种子取 ``delta = 该格 F1 − 该种子全局 F1``，然后：

    - ``delta`` 的 95% CI 完全在 0 以下 **且** mean(delta) ≤ −WEAK_MARGIN → ``systematic_weak``
      （稳定的系统性短板，说明模型设计确有短板）；
    - CI 完全在 0 以下但幅度不足 → ``mild_but_consistent``（方向稳定、幅度被单次采样放大了）；
    - CI 跨 0 → ``sampling_noise``（不该当成结论）。
    """
    n_seeds = len(per_seed)
    cells: list[dict[str, Any]] = []
    for dim, cell, single_seed_f1 in KNOWN_WEAK_SPOTS:
        key = f"{dim}::{cell}"
        f1s: list[float] = []
        deltas: list[float] = []
        n_present = 0
        n_low_support = 0
        n_in_top_weak = 0
        n_below_margin = 0
        positives: list[float] = []
        per_seed_rows: list[dict[str, Any]] = []
        for s in per_seed:
            stats = s["strata"]["cells"].get(key)
            if stats is None:
                continue
            n_present += 1
            g = float(s["strata"]["global_fraud_f1"])
            f1 = float(stats["f1"])
            f1s.append(f1)
            deltas.append(f1 - g)
            positives.append(float(stats["positives"]))
            n_low_support += int(stats["low_support"])
            in_top = key in s["strata"]["top_weak"]
            n_in_top_weak += int(in_top)
            n_below_margin += int((f1 - g) <= -WEAK_MARGIN)
            per_seed_rows.append(
                {
                    "seed": s["seed"],
                    "f1": f1,
                    "global_f1": g,
                    "delta": round(f1 - g, 4),
                    "positives": int(stats["positives"]),
                    "low_support": bool(stats["low_support"]),
                    "in_top_weak_list": in_top,
                }
            )

        d = summarize(deltas)
        sig = t_test_one_sample(deltas, 0.0)
        ci_below_zero = bool(d.get("ci95_high") is not None and float(d["ci95_high"]) < 0.0)
        if ci_below_zero and float(d["mean"]) <= -WEAK_MARGIN:
            verdict = "systematic_weak"
        elif ci_below_zero:
            verdict = "mild_but_consistent"
        else:
            verdict = "sampling_noise"
        cells.append(
            {
                "dimension": dim,
                "cell": cell,
                "single_seed_f1": single_seed_f1,
                "n_seeds_present": n_present,
                "n_seeds_low_support": n_low_support,
                "positives": summarize(positives, 2),
                "f1": summarize(f1s),
                "delta_vs_global_f1": d,
                "significance_delta_vs_zero": sig,
                "n_seeds_in_top_weak_list": n_in_top_weak,
                "top_weak_frequency": round(n_in_top_weak / n_seeds, 4) if n_seeds else 0.0,
                "n_seeds_below_margin": n_below_margin,
                "below_margin_frequency": round(n_below_margin / n_seeds, 4) if n_seeds else 0.0,
                "classification": verdict,
                "per_seed": per_seed_rows,
            }
        )

    # 附加视角：跨种子最常进入弱项榜的格子（回答"是这个格子稳定弱，还是这个维度总有人弱"）
    freq: dict[str, int] = {}
    for s in per_seed:
        for key in s["strata"]["top_weak"]:
            freq[key] = freq.get(key, 0) + 1
    ranking = [
        {"cell": k, "n_seeds_in_top_weak": v, "frequency": round(v / n_seeds, 4)}
        for k, v in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {
        "criteria": {
            "weak_margin": WEAK_MARGIN,
            "systematic_weak": "delta 的 95% CI 全在 0 以下，且 mean(delta) ≤ −0.15",
            "mild_but_consistent": "95% CI 全在 0 以下，但 mean(delta) 幅度不足 0.15",
            "sampling_noise": "95% CI 跨 0，方向都不稳定",
            "min_support_positive": MIN_SUPPORT_POSITIVE,
        },
        "known_weak_spots": cells,
        "top_weak_frequency_ranking": ranking,
        "systematic_weak_discovery": _discover_systematic_weak(per_seed),
        "selection_bias_note": (
            "weak_spots 是在 40+ 个分层格子里取 F1 最小的前 6 名，本质是「多个噪声变量的最小值」，"
            "因此单种子弱项榜必然被系统性低估（winner's curse / 向均值回归）："
            "一个格子出现在榜上，可能只是这一次抽样恰好最低。"
            "能在跨种子 delta 上稳住的格子才是真短板。"
        ),
        "note": (
            "弱项榜（weak_spots）本身只取 F1 最低的前 6 格且排除低支撑格，"
            "因此「没进榜」不等于「不弱」；判定一律以 delta 的 CI 为准，进榜频次只作辅证。"
        ),
    }


def _discover_systematic_weak(per_seed: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """反向问题：如果三个已知弱项里有的只是噪声，那么**真正**稳定的短板是哪些格子？

    只考察"在所有种子都出现、且没有任何一个种子低支撑"的格子（否则比较本身不公平），
    按 mean(delta) 升序给出前若干名，并标注分类。这一步不是为了多报几个指标，
    而是为了让"弱项"这件事有一个不依赖单次采样的答案。
    """
    n_seeds = len(per_seed)
    keys = set(per_seed[0]["strata"]["cells"])
    for s in per_seed[1:]:
        keys &= set(s["strata"]["cells"])
    rows: list[dict[str, Any]] = []
    for key in sorted(keys):
        cells = [s["strata"]["cells"][key] for s in per_seed]
        if any(bool(c["low_support"]) for c in cells):
            continue
        deltas = [
            float(c["f1"]) - float(s["strata"]["global_fraud_f1"])
            for c, s in zip(cells, per_seed, strict=True)
        ]
        d = summarize(deltas)
        ci_below_zero = float(d["ci95_high"]) < 0.0
        if ci_below_zero and float(d["mean"]) <= -WEAK_MARGIN:
            label = "systematic_weak"
        elif ci_below_zero:
            label = "mild_but_consistent"
        else:
            label = "sampling_noise"
        rows.append(
            {
                "cell": key,
                "f1_mean": round(_mean([float(c["f1"]) for c in cells]), 4),
                "delta_mean": d["mean"],
                "delta_std": d["std"],
                "delta_ci95": [d["ci95_low"], d["ci95_high"]],
                "p_two_sided": t_test_one_sample(deltas, 0.0)["p_two_sided"],
                "positives_mean": round(_mean([float(c["positives"]) for c in cells]), 1),
                "classification": label,
            }
        )
    rows.sort(key=lambda r: float(r["delta_mean"]))
    return {
        "eligibility": f"在全部 {n_seeds} 个种子都出现、且没有任何种子被判 low_support 的格子",
        "n_eligible_cells": len(rows),
        "n_systematic_weak": sum(1 for r in rows if r["classification"] == "systematic_weak"),
        "n_mild_but_consistent": sum(1 for r in rows if r["classification"] == "mild_but_consistent"),
        "weakest_cells": rows[:10],
    }


# ---------------------------------------------------------------------------
# 编排 + 人可读摘要
# ---------------------------------------------------------------------------
def _run_seed_job(args: tuple[int, int]) -> dict[str, Any]:
    """ProcessPoolExecutor 的 top-level 入口（lambda 不可 pickle）。"""
    seed, n = args
    return run_seed(seed, n)


def run_multiseed(
    seeds: Sequence[int] | None = None,
    n: int = N_KOX,
    workers: int = 1,
) -> dict[str, Any]:
    """跑完整多种子实验，返回 ``output/multiseed.json`` 的 payload。

    ``workers > 1`` 时用多进程并行。**每个 worker 全程内存计算、零文件写入**，
    因此不存在共享磁盘缓存被互相覆盖的风险（项目里的 ``Cache`` 无跨进程锁）。
    单种子仅约 2.3s，默认 ``workers=1`` 顺序跑即可，也让结果顺序天然稳定。
    """
    seed_values = list(seeds) if seeds is not None else seed_list()
    if workers > 1 and len(seed_values) > 1:
        with ProcessPoolExecutor(max_workers=min(workers, len(seed_values))) as pool:
            per_seed = list(pool.map(_run_seed_job, [(s, n) for s in seed_values]))
    else:
        per_seed = [run_seed(s, n) for s in seed_values]
    per_seed.sort(key=lambda r: seed_values.index(int(r["seed"])))

    value = value_robustness(per_seed)
    variance = variance_attribution(per_seed)
    gates = gate_metric_robustness(per_seed)
    weak = weak_spot_stability(per_seed)

    share = value["saved_share_of_budget"]
    up = value["effective_view_uplift"]
    pooled = variance["pooled_all_campaigns"]
    headline = (
        f"{len(seed_values)} 个种子：少浪费占预算 {share['mean']:.1%} ± {share['std']:.1%}"
        f"（min {share['min']:.1%} / max {share['max']:.1%}，95% CI "
        f"[{share['ci95_low']:.1%}, {share['ci95_high']:.1%}]），"
        f"有效曝光提升 {up['mean']:+.1%} ± {up['std']:.1%}；"
        f"总口径跑输基线 {value['n_seeds_koxpilot_loses_overall']}/{len(seed_values)} 个种子。"
    )
    return {
        "meta": {
            "multiseed_version": MULTISEED_VERSION,
            "eval_version": EVAL_VERSION,
            "thresholds_version": THRESHOLDS_VERSION,
            "dataset_version": DATASET_VERSION,
            "n_kox_per_seed": n,
            "n_seeds": len(seed_values),
            "base_seed": SEED,
            "seed_step": SEED_STEP,
            "seeds": seed_values,
            "seed_derivation": f"seed_i = {SEED} + i × {SEED_STEP}（i = 0..{len(seed_values) - 1}）",
            "reproduce": f"PYTHONPATH=src python -m koxpilot.cli multiseed --seeds {len(seed_values)}",
            "determinism_note": (
                "全链路纯确定性、零 LLM 调用；每个种子重新生成数据集、重新标定阈值、"
                "重新跑全库门禁与两臂预算。全程内存计算，不读写 data/，"
                "唯一落盘产物就是本文件（不含时间戳，便于逐字节回归对比）。"
            ),
            "scope_note": (
                "本实验检验的是「合成数据生成过程的随机性」，不检验「生成模型假设本身"
                "是否符合真实世界」——后者是更根本的局限，见 docs/06-robustness.md。"
            ),
        },
        "headline": headline,
        "A_value_robustness": value,
        "B_variance_attribution": variance,
        "C_gate_metric_robustness": gates,
        "D_weak_spot_stability": weak,
        "per_seed": per_seed,
    }


def _fmt_pm(stats: Mapping[str, Any], pct: bool = False, digits: int = 4) -> str:
    if pct:
        return (
            f"{float(stats['mean']):.1%} ± {float(stats['std']):.1%} "
            f"[min {float(stats['min']):.1%} / med {float(stats['median']):.1%} / "
            f"max {float(stats['max']):.1%}]"
        )
    return (
        f"{float(stats['mean']):.{digits}f} ± {float(stats['std']):.{digits}f} "
        f"[min {float(stats['min']):.{digits}f} / max {float(stats['max']):.{digits}f}]"
    )


def _fmt_pm_safe(stats: Mapping[str, Any], pct: bool = False, digits: int = 4) -> str:
    """``summarize`` 在全部取值都无定义时返回 ``{"n": 0}``，这里不能 KeyError 崩。"""
    if not stats or int(stats.get("n") or 0) == 0:
        return "无定义（该口径在所有种子上都没有可用取值）"
    return _fmt_pm(stats, pct=pct, digits=digits)


def _fmt_opt_pct(value: Any) -> str:
    """``None`` 写成"无定义"，不印裸 None（摘要是给人读的）。"""
    return f"{float(value):+.1%}" if value is not None else "无定义"


def _fmt_ratio(value: Any) -> str:
    """std 比可能为 ``None``：KOXPilot 一侧方差恰好为 0 时比值没有定义（小样本下会真的发生）。

    这种情况必须说清"为什么没有数"，不能把 ``None`` 原样印给读者
    （``std 比 = None×`` 会被当成 bug 或被当成 0，两种误读都很贵）。
    """
    return f"{value}×" if value is not None else "不可算（KOXPilot 方差为 0）"


def _fmt_p(test: Mapping[str, Any]) -> str:
    value = test.get("p_two_sided")
    return "不可算" if value is None else str(value)


def format_summary(payload: Mapping[str, Any]) -> str:
    """stdout 摘要：一屏之内把 A/B/C/D 四组结论说清。"""
    meta = payload["meta"]
    a = payload["A_value_robustness"]
    b = payload["B_variance_attribution"]
    c = payload["C_gate_metric_robustness"]["metrics"]
    d = payload["D_weak_spot_stability"]
    n = int(meta["n_seeds"])
    lines: list[str] = []
    add = lines.append

    add(f"[multiseed] 种子数 {n} × 每种子 {meta['n_kox_per_seed']} 人，派生规则 {meta['seed_derivation']}")
    add(f"[multiseed] 种子列表 {meta['seeds']}")

    add("")
    add("== A 价值主张稳健性（预算 $245,000，口径与 metrics.json 一致） ==")
    add(f"[A] 少浪费金额 USD    {_fmt_pm(a['saved_usd'], digits=0)}")
    add(f"[A] 少浪费占预算      {_fmt_pm(a['saved_share_of_budget'], pct=True)}")
    add(
        f"[A]   95% CI [{float(a['saved_share_of_budget']['ci95_low']):.1%}, "
        f"{float(a['saved_share_of_budget']['ci95_high']):.1%}]，"
        f"双侧显著性 p={a['significance_saved_share_vs_zero']['p_two_sided']}"
        "（H0: 少浪费占比 = 0）"
    )
    add(f"[A] 有效曝光提升      {_fmt_pm(a['effective_view_uplift'], pct=True)}")
    add(f"[A] 有效曝光（宽松口径，水号曝光按 50% 计） {_fmt_pm(a['effective_view_uplift_lenient'], pct=True)}")
    # 有界口径：比率会被分母趋 0 的种子绑架，摘要里必须把可聚合的那把尺子一起给
    add(
        f"[A] 有效曝光率差（有界，pp） {_fmt_pm(a['effective_view_rate_gap_pp'], digits=2)}"
        f"；symmetric {_fmt_pm(a['effective_view_symmetric_uplift'])}"
    )
    add(
        f"[A] 总口径跑输基线    {a['n_seeds_koxpilot_loses_overall']}/{n} 个种子"
        f"（胜率 {a['win_rate_overall']:.0%}）；硬约束每个种子都满足={a['all_constraints_satisfied_every_seed']}"
    )
    for cid, row in a["per_campaign"].items():
        ref = row["effective_view_uplift_ratio_reference"]
        add(
            f"[A]   {cid} ${row['budget_usd']:,.0f}：少浪费 {_fmt_pm(row['saved_share_of_budget'], pct=True)}"
            f"，跑输 {row['n_seeds_koxpilot_loses']}/{n} 个种子"
        )
        add(
            f"[A]     有效曝光率差 {_fmt_pm_safe(row['effective_view_rate_gap_pp'], digits=2)} pp（有界口径）"
            f"；无界比率仅参考：median {_fmt_opt_pct(ref.get('median'))}"
            f"（分母脆弱 {ref['n_seeds_denominator_fragile']}/{n} 个种子，故不引用其 mean）"
        )

    add("")
    add("== B BRIEF-002 现象归因：基线是否「高方差赌博」 ==")
    for cid, row in b["per_campaign"].items():
        base_std = float(row["baseline_waste_share"]["std"])
        kox_std = float(row["koxpilot_waste_share"]["std"])
        add(
            f"[B] {cid} 基线选中人数 {float(row['baseline_n_selected']['mean']):.1f} 人 / "
            f"KOXPilot {float(row['koxpilot_n_selected']['mean']):.1f} 人"
        )
        add(f"[B]   基线浪费率    {_fmt_pm(row['baseline_waste_share'], pct=True)}  std={base_std:.4f}")
        add(f"[B]   KOXPilot 浪费率 {_fmt_pm(row['koxpilot_waste_share'], pct=True)}  std={kox_std:.4f}")
        add(
            f"[B]   std 比 = {_fmt_ratio(row['std_ratio_baseline_over_koxpilot'])}，"
            f"F 检验 p={_fmt_p(row['variance_ratio_test'])}；"
            f"基线 <5% 的种子 {row['n_seeds_baseline_share_below_5pct']}/{n}，"
            f">50% 的种子 {row['n_seeds_baseline_share_above_50pct']}/{n}；"
            f"KOXPilot 跑输 {row['n_seeds_koxpilot_loses']}/{n}"
        )
    p = b["pooled_all_campaigns"]
    add(
        f"[B] 三 campaign 合并（{p['n_obs']} 个观测）：基线浪费率 std={float(p['baseline_waste_share']['std']):.4f}"
        f" vs KOXPilot std={float(p['koxpilot_waste_share']['std']):.4f}"
        f"（比 {_fmt_ratio(p['std_ratio_baseline_over_koxpilot'])}，"
        f"F 检验 p={_fmt_p(p['variance_ratio_test'])}）"
    )
    add(
        f"[B]   基线 <5%: {p['n_obs_baseline_share_below_5pct']}/{p['n_obs']}，"
        f"基线 >50%: {p['n_obs_baseline_share_above_50pct']}/{p['n_obs']}；"
        f"KOXPilot <5%: {p['n_obs_koxpilot_share_below_5pct']}/{p['n_obs']}，"
        f"KOXPilot >50%: {p['n_obs_koxpilot_share_above_50pct']}/{p['n_obs']}"
    )

    cw = b["conditional_win_rate_by_baseline_luck"]
    add(f"[B] 按「基线运气」分档（{cw['n_obs']} 个 (seed, campaign) 观测，KOXPilot 共输 {cw['n_koxpilot_loses']} 次）：")
    for row in cw["bands"]:
        add(
            f"[B]   {row['band']}：{row['n_obs']} 个观测，KOXPilot 输 {row['n_koxpilot_loses']} 次"
            f"（胜率 {row['koxpilot_win_rate']:.0%}），平均少浪费 ${row['mean_saved_usd']:,.0f}"
            if row["n_obs"]
            else f"[B]   {row['band']}：0 个观测"
        )
    if cw["n_koxpilot_loses"]:
        add(
            f"[B]   KOXPilot 输掉的那些观测里，基线浪费率最高只有 "
            f"{float(cw['max_baseline_waste_share_among_koxpilot_losses']):.1%}"
            f"（同时 KOXPilot 自身浪费率均值 "
            f"{float(cw['mean_koxpilot_waste_share_when_losing']):.1%}）"
        )
    else:
        # 一个观测都没输：此时 mean_koxpilot_waste_share_when_losing 为 None，
        # 不能拿去做 %.1% 格式化（照着"总会有失败观测"写摘要，会在最好的那种结果上崩）。
        add("[B]   KOXPilot 在全部观测上都没输给基线（无失败观测可归因）")

    add("")
    add("== C 门禁质量指标稳健性（每个种子独立标定阈值） ==")
    for key in _GATE_KEYS:
        add(f"[C] {key:24s} {_fmt_pm(c[key])}")

    add("")
    add("== D 已知弱项：系统性短板 or 采样噪声 ==")
    for cell in d["known_weak_spots"]:
        add(
            f"[D] {cell['dimension']}={cell['cell']}（单种子 F1 {cell['single_seed_f1']}）-> "
            f"{cell['classification']}"
        )
        add(
            f"[D]   F1 {_fmt_pm(cell['f1'])}；delta vs 全局 F1 "
            f"{float(cell['delta_vs_global_f1']['mean']):+.4f} ± {float(cell['delta_vs_global_f1']['std']):.4f}"
            f"，CI [{float(cell['delta_vs_global_f1']['ci95_low']):+.4f}, "
            f"{float(cell['delta_vs_global_f1']['ci95_high']):+.4f}]，"
            f"p={cell['significance_delta_vs_zero']['p_two_sided']}"
        )
        add(
            f"[D]   进弱项榜 {cell['n_seeds_in_top_weak_list']}/{n}，"
            f"落差 ≥{WEAK_MARGIN} 的种子 {cell['n_seeds_below_margin']}/{n}，"
            f"真水号数 {float(cell['positives']['mean']):.1f}（低支撑种子 {cell['n_seeds_low_support']} 个）"
        )
    top = d["top_weak_frequency_ranking"][:5]
    add("[D] 最常进弱项榜的格子：" + "，".join(f"{r['cell']} {r['n_seeds_in_top_weak']}/{n}" for r in top))

    add("")
    add(f"[multiseed] {payload['headline']}")
    return "\n".join(lines)
