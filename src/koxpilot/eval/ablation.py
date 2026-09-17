"""消融实验（SPEC 第 7 节表 4）：逐层关掉 G0/G1/G2/G3，以及逐条关掉 G1 规则。

为什么要做两级消融
------------------
- **层级消融**回答"这四层是不是都在干活"；
- **规则级消融**回答"G1 里那 7 条信号是不是都在干活"——
  这是防止"堆规则凑数"的自查：如果关掉某条规则指标一点都不动，那条规则就应该删掉，
  而不是留在文档里充数。

实现依赖 ``evaluate(..., enabled_gates=, disabled_rules=)``：
判定分数由 engine 统一从 reason 列表合成，所以屏蔽规则后分数会**同步**变化，
不会出现"规则关了但扣分还在"的假消融。

贡献的符号必须分开看
--------------------
``delta = 变体 − 全量``。**取绝对值就会把结论弄反**：
delta 为负是"关掉后指标下降 = 有正向贡献"，delta 为正是"关掉后指标上升 = 该规则拖累这个指标"。
本模块因此把结论分成 ``positive`` / ``negative`` / ``negligible`` 三档（:func:`contribution_sign`），
``contributes`` 只在 positive 档为真，``dead_rules`` 只收 negligible 档。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..gates.engine import evaluate_all
from ..gates.policy import ALL_GATES, G1_WEIGHTS
from ..gates.thresholds import Thresholds
from ..types import CampaignSpec, GateResult, Kox
from .metrics import fraud_report, verdict_report

__all__ = ["CONTRIBUTION_EPS", "GATE_ROLES", "ablation_report", "contribution_sign"]

#: 判定"这条规则对严口径 F1 有可测量影响"的最小 |delta|。
#: 低于它的差异在 5,000 条样本上等价于几条样本的抖动，不该被叫作贡献。
CONTRIBUTION_EPS: float = 0.001

#: 每层的职责说明，直接进报告（数字要能配一句话，否则读者不知道该期待什么）
GATE_ROLES: dict[str, str] = {
    "G0": "数据完整性：缺关键字段的号降置信度并进人核，防止用残缺数据下判定",
    "G1": "真实性：7 条统计信号识别水号，是水号指标的唯一来源",
    "G2": "一致性：品类/多源/语言/受众匹配，campaign 相关的错配拦截",
    "G3": "品牌安全：高危内容硬阻断、竞品冲突与争议历史",
}


def _score(
    records: Sequence[Kox],
    results: Mapping[str, GateResult],
) -> dict[str, float]:
    fraud = fraud_report(records, results)
    verdict = verdict_report(records, results)
    return {
        "fraud_f1_strict": float(fraud["strict"]["f1"]),
        "fraud_precision_strict": float(fraud["strict"]["precision"]),
        "fraud_recall_strict": float(fraud["strict"]["recall"]),
        "fraud_auc": float(fraud["auc"]),
        "verdict_accuracy": float(verdict["accuracy"]),
        "verdict_macro_f1": float(verdict["macro_f1"]),
        "n_reject": sum(1 for r in results.values() if r.verdict == "reject"),
        "n_review": sum(1 for r in results.values() if r.verdict == "review"),
        "n_pass": sum(1 for r in results.values() if r.verdict == "pass"),
    }


def _run(
    records: Sequence[Kox],
    spec: CampaignSpec,
    thresholds: Thresholds,
    gates: tuple[str, ...],
    disabled_rules: tuple[str, ...] = (),
) -> dict[str, float]:
    results = {
        r.kox_id: r
        for r in evaluate_all(records, spec, thresholds, None, gates, disabled_rules)
    }
    return _score(records, results)


def _delta(base: Mapping[str, float], variant: Mapping[str, float]) -> dict[str, float]:
    keys = ("fraud_f1_strict", "fraud_auc", "verdict_accuracy", "verdict_macro_f1")
    return {f"d_{k}": round(float(variant[k]) - float(base[k]), 4) for k in keys}


def contribution_sign(delta: float, eps: float = CONTRIBUTION_EPS) -> str:
    """把 ``delta = 变体 - 全量`` 翻译成**带符号**的贡献结论。

    这里必须分三档而不是取绝对值：``delta < 0`` 表示关掉它指标下降（正向贡献），
    ``delta > 0`` 表示关掉它指标反而上升（该规则在这个指标上是**负担**）。
    早期实现用 ``abs(delta) >= eps`` 当 ``contributes``，于是 G1.6 这种
    "关掉后 F1 涨 0.0149" 的规则也被标成"有贡献"——一个符号错误足以让整张表的结论反过来。
    """
    if delta <= -eps:
        return "positive"
    if delta >= eps:
        return "negative"
    return "negligible"


#: 三档贡献各自的人话解释（直接落进产物，避免读者拿 contributes 布尔值自己编语义）
_CONTRIBUTION_NOTES: dict[str, str] = {
    "positive": "关掉它严口径 F1 下降：对该指标有正向贡献",
    "negative": "关掉它严口径 F1 反而上升：该规则在严口径上是净负担（保留与否见 docs/03 §表 4）",
    "negligible": f"|ΔF1| < {CONTRIBUTION_EPS}：对本数据集的严口径 F1 近乎无影响",
}


def _build_note(
    positive: Sequence[str],
    negative: Sequence[str],
    dead: Sequence[str],
    rules: Sequence[Mapping[str, Any]],
) -> str:
    """表 4 的 note。**只允许写产物里能核对的句子**。

    这里刻意不写"所有 G1 规则都有贡献"这类总括句：只要有一条规则的 delta 为正，
    这句话就是假的，而它恰好是最容易被现场问穿的一句。
    """
    by_variant = {str(r["variant"]): r for r in rules}
    parts = [
        "delta 为「变体 − 全量」：负值 = 关掉后指标下降（该规则有正向贡献），"
        f"正值 = 关掉后指标上升（该规则拖累该指标），|delta| < {CONTRIBUTION_EPS} 视为无影响。",
        f"严口径 F1 上：{len(positive)}/{len(rules)} 条为正向贡献"
        f"（{', '.join(positive) if positive else '无'}）。",
    ]
    if negative:
        detail = "、".join(
            f"{v}（ΔF1 {float(by_variant[v]['delta']['d_fraud_f1_strict']):+.4f}）" for v in negative
        )
        parts.append(
            f"如实记录 {len(negative)} 条**负贡献**规则：{detail}——"
            "关掉后严口径 F1 反而上升，因此不属于"
            "「对 F1 有正向贡献」的规则；保留理由（软信号、只推 review、宽口径召回来源）"
            "写在 docs/03-evaluation.md 表 4 一节，不靠这条 note 一句话带过。"
        )
    else:
        parts.append("没有任何 G1 规则的严口径 F1 delta 为正（即没有规则在该指标上是净负担）。")
    if dead:
        parts.append(
            f"另有 {len(dead)} 条对严口径 F1 无可测量影响（|ΔF1| < {CONTRIBUTION_EPS}）："
            f"{', '.join(dead)}，属于对本数据集近乎无效的规则。"
        )
    else:
        parts.append(f"没有 |ΔF1| < {CONTRIBUTION_EPS} 的死规则（dead_rules 为空）。")
    return "".join(parts)


def ablation_report(
    records: Sequence[Kox],
    spec: CampaignSpec,
    thresholds: Thresholds,
    baseline: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """跑层级消融 + G1 规则级消融，返回带 delta 的表。"""
    full = dict(baseline) if baseline is not None else _run(records, spec, thresholds, ALL_GATES)

    layers: list[dict[str, Any]] = []
    for gate in ALL_GATES:
        remaining = tuple(g for g in ALL_GATES if g != gate)
        metrics = _run(records, spec, thresholds, remaining)
        delta = _delta(full, metrics)
        layers.append(
            {
                "variant": f"-{gate}",
                "enabled_gates": list(remaining),
                "role": GATE_ROLES[gate],
                "metrics": metrics,
                "delta": delta,
                # 层级同样要分符号：−G2 的 d_verdict_accuracy 是 +0.0764，
                # 也就是"关掉一致性层三分类反而更准"。这必须能在产物里一眼看出来。
                "contribution": {
                    "fraud_f1_strict": contribution_sign(float(delta["d_fraud_f1_strict"])),
                    "verdict_accuracy": contribution_sign(float(delta["d_verdict_accuracy"])),
                },
            }
        )
    only_g1 = _run(records, spec, thresholds, ("G1",))
    only_g1_delta = _delta(full, only_g1)
    layers.append(
        {
            "variant": "only G1",
            "enabled_gates": ["G1"],
            "role": "只留真实性层：看其余三层对三分类到底贡献多少",
            "metrics": only_g1,
            "delta": only_g1_delta,
            "contribution": {
                "fraud_f1_strict": contribution_sign(float(only_g1_delta["d_fraud_f1_strict"])),
                "verdict_accuracy": contribution_sign(float(only_g1_delta["d_verdict_accuracy"])),
            },
        }
    )

    rules: list[dict[str, Any]] = []
    for rule_id in sorted(G1_WEIGHTS):
        metrics = _run(records, spec, thresholds, ALL_GATES, (rule_id,))
        d = _delta(full, metrics)
        sign = contribution_sign(float(d["d_fraud_f1_strict"]))
        rules.append(
            {
                "variant": f"-{rule_id}",
                "disabled_rules": [rule_id],
                "weight": G1_WEIGHTS[rule_id],
                "metrics": metrics,
                "delta": d,
                # contributes 只在"关掉它 F1 下降"时为真（= 正向贡献）。
                # 前端那一列表头写的就是"有贡献"，所以它必须是带符号的判定。
                "contributes": sign == "positive",
                "contribution": sign,
                "contribution_note": _CONTRIBUTION_NOTES[sign],
            }
        )

    dead_rules = [r["variant"] for r in rules if r["contribution"] == "negligible"]
    negative_rules = [r["variant"] for r in rules if r["contribution"] == "negative"]
    positive_rules = [r["variant"] for r in rules if r["contribution"] == "positive"]
    return {
        "full": full,
        "by_layer": layers,
        "by_g1_rule": rules,
        "dead_rules": dead_rules,
        "negative_rules": negative_rules,
        "positive_rules": positive_rules,
        "contribution_criteria": {
            "eps": CONTRIBUTION_EPS,
            "metric": "d_fraud_f1_strict（严口径 F1 的「变体 − 全量」）",
            "positive": f"delta ≤ −{CONTRIBUTION_EPS}：关掉它 F1 下降，该规则有正向贡献",
            "negative": f"delta ≥ +{CONTRIBUTION_EPS}：关掉它 F1 反而上升，该规则拖累该指标",
            "negligible": f"|delta| < {CONTRIBUTION_EPS}：对该指标近乎无影响（= dead_rules）",
            "note": (
                "contributes 字段只在 positive 档为真。"
                "早期实现用 abs(delta) 判定，会把负贡献规则也标成「有贡献」。"
            ),
        },
        "note": _build_note(positive_rules, negative_rules, dead_rules, rules),
    }
