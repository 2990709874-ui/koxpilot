"""表 6：LLM vs 规则对比（SPEC 第 7 节第 6 项 + 6.1 成本审计的准确率侧）。

诚实性处理（本表最重要的一点）
------------------------------
LLM 那一列只能来自**真实调用**产出的 ``output/llm_bench.json``。
本仓库的运行环境没有可用 endpoint 时，本表会明确输出
``status="llm_not_run"``，并给出复跑命令，**绝不填任何估算或想象的数字**。

规则那一列是真算的，永远有值：
- 标签错配（G2.1 的 Jaccard 规则）对 ``gt.tag_mismatch`` 的 P/R/F1；
- 语义适配（G2.3 规则版）没有 ground truth，因此只报**分布**与
  "被判低适配的比例"，不伪造准确率——有 LLM 结果时再补一致率对照。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..gates.g2 import rule_fit_score
from ..gates.policy import DECLARED_OBSERVED_JACCARD_MIN, FIT_SCORE_REVIEW_MAX
from ..io_utils import load_json
from ..llm.identity import identity_from_bench, model_display_map
from ..stats import mean, quantiles
from ..types import CampaignSpec, GateResult
from .metrics import binary_scores, gt_of

__all__ = ["llm_vs_rule_report", "rule_fit_distribution", "rule_tag_mismatch_metrics"]


def rule_tag_mismatch_metrics(
    records: Sequence[Mapping[str, Any]], results: Mapping[str, GateResult]
) -> dict[str, Any]:
    """规则版标签一致性（G2.1）对 ``gt.tag_mismatch`` 的检出能力。"""
    tp = fp = fn = tn = 0
    for kox in records:
        res = results.get(str(kox.get("kox_id")))
        if res is None:
            continue
        truth = bool(gt_of(kox).get("tag_mismatch"))
        pred = res.has_rule("G2.1")
        if pred and truth:
            tp += 1
        elif pred:
            fp += 1
        elif truth:
            fn += 1
        else:
            tn += 1
    out = binary_scores(tp, fp, fn, tn)
    out["threshold"] = DECLARED_OBSERVED_JACCARD_MIN
    out["rule"] = "G2.1：declared/observed 品类 Jaccard < 阈值即判错配"
    return out


def rule_fit_distribution(
    records: Sequence[Mapping[str, Any]], spec: CampaignSpec
) -> dict[str, Any]:
    """规则版语义适配分的分布（无 gt，只报分布，不报准确率）。

    ⚠️ 必须传**真实 campaign 的 spec**，不能传中性 spec。
    :class:`CampaignSpec` 的默认实例是"库级中性画像"（``target_categories`` 为空），
    而 :func:`rule_fit_score` 对空目标品类会直接返回 1.0（语义是"该维度不做约束"）。
    早期版本这里误传了 ``CampaignSpec()``，导致 5000 条的分位数全是 1.0、
    ``share_below_review_threshold`` 恒为 0，整格数据没有任何信息量。
    """
    if spec.is_neutral:
        return {
            "n": len(records),
            "status": "skipped_neutral_spec",
            "note": (
                "中性 spec 不做品类约束，rule_fit_score 恒为 1.0，"
                "报分布无意义。语义适配分布请看 by_campaign。"
            ),
        }
    scores = [rule_fit_score(kox, spec) for kox in records]
    if not scores:
        return {"n": 0}
    q = quantiles(scores, (0.1, 0.25, 0.5, 0.75, 0.9))
    return {
        "campaign_id": spec.campaign_id,
        "n": len(scores),
        "mean": round(mean(scores), 4),
        "quantiles": q,
        "share_below_review_threshold": round(
            sum(1 for s in scores if s < FIT_SCORE_REVIEW_MAX) / len(scores), 4
        ),
        "review_threshold": FIT_SCORE_REVIEW_MAX,
        "note": "语义适配没有 ground truth，只报分布；准确率对照需 LLM 真调后由人工抽检补齐",
    }


def llm_vs_rule_report(
    records: Sequence[Mapping[str, Any]],
    results: Mapping[str, GateResult],
    spec: CampaignSpec,
    bench_path: Path | str,
    campaign_specs: Sequence[CampaignSpec] = (),
) -> dict[str, Any]:
    """汇总表 6。``bench_path`` 指向 ``output/llm_bench.json``（构建期真调产物）。

    ``campaign_specs`` 传真实 campaign 的 spec 列表：语义适配分只有在具体
    campaign 下才有意义（中性 spec 恒为 1.0），所以按 campaign 分别报分布。
    """
    rule_tag = rule_tag_mismatch_metrics(records, results)
    rule_fit = rule_fit_distribution(records, spec)
    fit_by_campaign = [
        rule_fit_distribution(records, s) for s in campaign_specs if not s.is_neutral
    ]
    path = Path(bench_path)
    payload: dict[str, Any] = {
        "rule_arm": {
            "tag_mismatch": rule_tag,
            "semantic_fit": rule_fit,
            "semantic_fit_by_campaign": fit_by_campaign,
        },
        "llm_bench_path": str(path.name),
    }
    if not path.exists():
        payload.update(
            {
                "status": "llm_not_run",
                "llm_arm": None,
                "explanation": (
                    "本次评测运行环境未配置可用的模型 endpoint，因此 LLM 一列为空。"
                    "规则一列是真算的。配置 .env 后执行 "
                    "`python -m koxpilot.llm.runner --tasks brief,tag,fit` 即可生成 "
                    "output/llm_bench.json，本表会自动补齐 LLM 侧数字与真实 token 账。"
                ),
            }
        )
        return payload

    bench = load_json(path)
    arms = ((bench.get("tag_bench") or {}).get("arms")) or {}
    llm_arms = {k: v for k, v in arms.items() if v.get("kind") == "llm"}
    rule_arm_in_bench = next((v for v in arms.values() if v.get("kind") == "rule"), None)
    best = max(llm_arms.items(), key=lambda kv: float(kv[1].get("f1", 0.0)), default=None)
    delta = None
    if best is not None and rule_arm_in_bench is not None:
        delta = round(float(best[1]["f1"]) - float(rule_arm_in_bench["f1"]), 4)
    # 模型标识统一：`models` 一律给**展示名**（服务端回报优先），
    # 请求用的 endpoint id 放在 `model_identity` 里。旧产物里 `models` 写的是
    # endpoint id、`per_task[*].model` 写的是型号，直接透传会让同一份 metrics
    # 里出现两个名字，读者只能猜哪个是真的。
    identity = identity_from_bench(bench)
    payload.update(
        {
            "status": "ok",
            "models": model_display_map(identity) or bench.get("models"),
            "model_identity": identity,
            "sample_n": (bench.get("tag_bench") or {}).get("n_samples"),
            "llm_arm": llm_arms,
            "rule_arm_same_sample": rule_arm_in_bench,
            "best_llm_model": best[0] if best else None,
            "f1_gain_llm_over_rule": delta,
            "explanation": (
                "同一抽样、同一 ground truth 下比较；规则列在 rule_arm_same_sample，"
                "全量规则指标在 rule_arm.tag_mismatch。"
                "models 给的是对外展示名（服务端回报的型号优先），"
                "我们发请求时用的 id（ARK 是 endpoint id）见 model_identity。"
            ),
        }
    )
    return payload
