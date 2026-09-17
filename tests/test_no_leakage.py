"""反数据泄漏 / 反自证（这套测试是整个仓库可信度的地基）。

为什么需要它
------------
机器学习项目里最容易骗人的一层不是模型，而是**评测边界**：只要生产代码偷看一眼
``gt``，所有指标就变成自证，而且从指标数字上完全看不出来（甚至会显得"很合理"）。
所以这里用三张互补的网：

1. **静态 AST 扫描**（`test_static_*`）：源码层面证明 gates / budget / prompt 构造
   根本没有触碰裁判字段的语句。优点是覆盖所有分支（包括测试跑不到的 if）。
2. **运行期泄漏哨兵**（`test_runtime_*`）：把记录包成 ``LeakGuard``，一旦被
   ``r["gt"]`` / ``r.get("gt")`` 读取就立刻抛错。优点是能抓到静态扫描绕不过的
   动态取键（``r[key_from_config]``）。
3. **信号面白名单**（`test_signal_surface_*`）：证明进入阈值标定和 prompt 的字段集合
   本身就不含裁判字段——即"想泄漏也没有通道"。

允许读 ``gt`` 的边界（唯一白名单）
--------------------------------
- ``koxpilot.eval.*``：评测与审计，本来就是裁判席。
- ``koxpilot.llm.runner.bench_tag`` / ``promptbench.run_arm`` / ``promptbench.main``：
  给 LLM 打分的部分。**注意**它们打分可以看 gt，但同一模块里构造 prompt 与
  cache key 的函数不可以——`test_static_llm_prompt_path_is_clean` 卡的就是这条线。
- ``koxpilot.gates.engine.observable_view``：唯一提到裁判字段名的地方，且用途是**剥掉它**。

第 4 组 `test_jaccard_*` 是数据质量守卫，见该组 docstring。
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from koxpilot.budget.value import build_candidates, targeting_reason
from koxpilot.gates.engine import GROUND_TRUTH_FIELD, evaluate, observable_view
from koxpilot.gates.g0 import evaluate_g0
from koxpilot.gates.g1 import evaluate_g1
from koxpilot.gates.g2 import evaluate_g2
from koxpilot.gates.g3 import evaluate_g3
from koxpilot.gates.signals import extract_signals
from koxpilot.gates.thresholds import calibrate
from koxpilot.llm import prompts as prompts_mod
from koxpilot.llm import prompt_variants
from koxpilot.stats import jaccard
from koxpilot.types import CampaignSpec

import dataclasses

SRC = Path(__file__).resolve().parents[1] / "src" / "koxpilot"

#: 裁判字段名。生产路径出现任何一个（作为字符串键或属性）即视为泄漏。
JUDGE_KEYS: frozenset[str] = frozenset(
    {
        GROUND_TRUTH_FIELD,  # "gt"
        "true_categories",
        "is_fraud",
        "fraud_type",
        "mismatch_injected",
    }
)

#: 必须 100% 干净的生产模块（相对 src/koxpilot）。
CLEAN_MODULES: tuple[str, ...] = (
    "gates/g0.py",
    "gates/g1.py",
    "gates/g2.py",
    "gates/g3.py",
    "gates/signals.py",
    "gates/policy.py",
    "gates/thresholds.py",
    "gates/humanize.py",
    "budget/value.py",
    "budget/policy.py",
    "budget/allocator.py",
    "budget/planner.py",
    "llm/prompts.py",
    "llm/prompt_variants.py",
    "llm/provider.py",
    "taxonomy.py",
    "stats.py",
)

#: `llm/` 下允许看 gt 的函数（给模型打分用）。除此之外一律不许。
LLM_SCORING_ALLOWLIST: dict[str, frozenset[str]] = {
    "llm/runner.py": frozenset({"bench_tag"}),
    "llm/promptbench.py": frozenset({"run_arm", "main"}),
}


# ---------------------------------------------------------------------------
# AST 工具
# ---------------------------------------------------------------------------
def _judge_refs(node: ast.AST) -> list[tuple[int, str]]:
    """返回 node 子树里所有对裁判字段的引用 ``(行号, 描述)``。"""
    hits: list[tuple[int, str]] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value in JUDGE_KEYS:
            hits.append((sub.lineno, f'字符串常量 "{sub.value}"'))
        elif isinstance(sub, ast.Attribute) and sub.attr in JUDGE_KEYS:
            hits.append((sub.lineno, f"属性访问 .{sub.attr}"))
        elif isinstance(sub, ast.Name) and sub.id in JUDGE_KEYS - {GROUND_TRUTH_FIELD}:
            # 局部变量名叫 gt 是无害的（打分函数常见）；叫 is_fraud/true_categories 则可疑
            hits.append((sub.lineno, f"标识符 {sub.id}"))
    return hits


def _own_body(func: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    """遍历函数体但**跳过嵌套函数**，以便按最小作用域定位泄漏。"""
    nested = {
        n for n in ast.walk(func) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    } - {func}
    skip: set[int] = set()
    for nf in nested:
        for sub in ast.walk(nf):
            skip.add(id(sub))
    for stmt in func.body:
        for sub in ast.walk(stmt):
            if id(sub) not in skip:
                yield sub


def _functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _parse(rel: str) -> ast.Module:
    return ast.parse((SRC / rel).read_text("utf-8"), filename=rel)


# ---------------------------------------------------------------------------
# 1. 静态扫描
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rel", CLEAN_MODULES)
def test_static_production_module_never_names_judge_fields(rel: str) -> None:
    """gates / budget / prompt 模块的**可执行代码**里不得出现裁判字段名。

    注释与 docstring 不算（它们本来就要解释"为什么不读 gt"），
    所以扫的是 AST 而不是文本 grep。
    """
    tree = _parse(rel)
    hits = [h for h in _judge_refs(tree) if not _is_docstring_line(tree, h[0])]
    assert not hits, f"{rel} 出现裁判字段引用：" + "; ".join(f"L{ln} {d}" for ln, d in hits)


def _is_docstring_line(tree: ast.Module, lineno: int) -> bool:
    """判断某行是否落在某个 docstring 内（docstring 里出现 "gt" 字样是允许的）。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = node.body[0] if node.body else None
            if (
                isinstance(doc, ast.Expr)
                and isinstance(doc.value, ast.Constant)
                and isinstance(doc.value.value, str)
                and doc.value.lineno <= lineno <= doc.value.end_lineno
            ):
                return True
    return False


def test_static_engine_touches_gt_only_to_strip_it() -> None:
    """``gates/engine.py`` 是唯一提到裁判字段的门禁文件，且只能用于剥离。"""
    tree = _parse("gates/engine.py")
    offenders: list[str] = []
    for func in _functions(tree):
        hits = [h for h in _judge_refs(ast.Module(body=list(_own_body(func)), type_ignores=[]))]
        if hits and func.name != "observable_view":
            offenders.append(f"{func.name}: {hits}")
    assert not offenders, f"engine.py 除 observable_view 外不应触碰 gt：{offenders}"

    # 且 observable_view 必须是"排除"语义（出现 != 或 not in），而不是"读取"
    src = inspect.getsource(observable_view)
    assert "!=" in src or "not in" in src, "observable_view 看起来不是在剥离 gt，请人工复核"


@pytest.mark.parametrize("rel", sorted(LLM_SCORING_ALLOWLIST))
def test_static_llm_prompt_path_is_clean(rel: str) -> None:
    """LLM 模块里能看 gt 的函数必须**恰好**是打分函数白名单。

    这是防"泄漏面悄悄扩张"的测试：如果有人以后在 ``run_tag`` / ``run_fit`` /
    ``run_arm`` 的推理闭包里加了 ``r["gt"]``，或者新增一个偷看 gt 的函数，
    这里就会红——而不是等到指标虚高时才靠人眼发现。
    """
    tree = _parse(rel)
    allowed = LLM_SCORING_ALLOWLIST[rel]
    dirty: set[str] = set()
    for func in _functions(tree):
        body = ast.Module(body=list(_own_body(func)), type_ignores=[])
        if _judge_refs(body):
            dirty.add(func.name)
    assert dirty == allowed, (
        f"{rel} 中触碰 gt 的函数集合 {sorted(dirty)} 与白名单 {sorted(allowed)} 不一致："
        "新增的属于泄漏，减少的请同步更新白名单"
    )


def test_static_cache_keys_are_derived_from_actual_prompt_content() -> None:
    """LLM 缓存 key 必须由**实际发给模型的消息**决定，不能只 hash ``kox_id``。

    只 hash id 的后果：改了 prompt 文案或数据重生成后同 id 内容变了，仍然命中旧缓存
    → 实验结果是"上一版的答案"，而 bench 数字看起来完全正常。
    这是典型的"评测作弊但无人察觉"型缺陷，靠看指标永远发现不了。

    断言方式是**正向**的：每个参与 cache key 的 ``_sha1(...)`` 都必须把
    prompt 消息（或等价的内容字段集合）喂进去。只写"不许出现 kox_id"是不够的——
    有人换成 hash 一个无关的 batch 序号照样能骗过测试。
    """
    id_only: list[str] = []
    not_content_derived: list[str] = []
    checked = 0
    for rel in ("llm/runner.py", "llm/promptbench.py"):
        text = (SRC / rel).read_text("utf-8")
        tree = _parse(rel)
        for node in ast.walk(tree):
            # 只看被拼进 cache key 的 hash 调用：ck = f"...{_sha1(x)}..."
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id in {"ck", "cache_key"} for t in node.targets
            ):
                continue
            calls = [
                n
                for n in ast.walk(node.value)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_sha1"
            ]
            assert calls, f"{rel}:L{node.lineno} cache key 没有经过 _sha1，无法判定其输入"
            for call in calls:
                checked += 1
                arg = ast.get_source_segment(text, call.args[0]) if call.args else ""
                arg = arg or ""
                where = f"{rel}:L{call.lineno} _sha1({arg})"
                content_tokens = (
                    "msgs",
                    "messages",
                    "declared_categories",
                    "observed_categories",
                    "source_tags",
                )
                if not any(tok in arg for tok in content_tokens):
                    not_content_derived.append(where)
                    if "kox_id" in arg:
                        id_only.append(where)
    assert checked >= 3, f"只找到 {checked} 处 cache key hash，扫描逻辑可能失效"
    assert not id_only, "以下 cache key 只由 kox_id 决定，内容变化不会失效：\n  " + "\n  ".join(
        id_only
    )
    assert not not_content_derived, (
        "以下 cache key 未从实际 prompt 消息/内容字段派生：\n  " + "\n  ".join(not_content_derived)
    )


# ---------------------------------------------------------------------------
# 2. 运行期泄漏哨兵
# ---------------------------------------------------------------------------
class LeakAccess(AssertionError):
    """生产代码读取了裁判字段。"""


class LeakGuard(dict):
    """读到裁判字段就爆炸的 dict。

    只拦截"按键取值"（``__getitem__`` / ``get`` / ``pop`` / ``in``-后取值），
    **不**拦截 ``items()`` / ``keys()`` 遍历——因为 ``observable_view`` 正是靠遍历
    键名把 gt 剔掉的，那是合法行为。这个边界正好把"枚举字段"与"读取裁判值"分开。
    """

    def __getitem__(self, key: Any) -> Any:  # noqa: D105
        if key in JUDGE_KEYS:
            raise LeakAccess(f"生产代码读取了裁判字段 {key!r}")
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:  # noqa: D102
        if key in JUDGE_KEYS:
            raise LeakAccess(f"生产代码读取了裁判字段 {key!r}")
        return super().get(key, default)

    def pop(self, key: Any, *args: Any) -> Any:  # noqa: D102
        if key in JUDGE_KEYS:
            raise LeakAccess(f"生产代码读取了裁判字段 {key!r}")
        return super().pop(key, *args)


@pytest.fixture()
def guarded(records: list[dict[str, Any]]) -> list[LeakGuard]:
    """取一小批真实记录（含 gt）包成哨兵；覆盖各种 verdict 与缺失情况。"""
    picked: list[dict[str, Any]] = []
    seen: set[tuple[str, bool]] = set()
    for rec in records:
        key = (rec["gt"]["verdict"], bool(rec["_missing"]))
        if key not in seen:
            seen.add(key)
            picked.append(rec)
        if len(picked) >= 6 and len(seen) >= 6:
            break
    picked.extend(records[:120])
    return [LeakGuard(r) for r in picked]


def test_leakguard_itself_actually_fires() -> None:
    """先证明哨兵不是哑弹——否则下面所有"没抛错"的测试都毫无意义。"""
    guard = LeakGuard({"kox_id": "X", GROUND_TRUTH_FIELD: {"is_fraud": True}})
    with pytest.raises(LeakAccess):
        _ = guard[GROUND_TRUTH_FIELD]
    with pytest.raises(LeakAccess):
        _ = guard.get(GROUND_TRUTH_FIELD)
    with pytest.raises(LeakAccess):
        _ = guard["true_categories"]
    assert guard["kox_id"] == "X"  # 非裁判字段照常可读
    assert GROUND_TRUTH_FIELD in dict(guard.items()), "哨兵不应改变字段枚举行为"


def test_runtime_signals_and_calibration_do_not_read_gt(
    guarded: list[LeakGuard], dataset: dict[str, Any]
) -> None:
    for g in guarded:
        extract_signals(g)
    calibrate(guarded, dataset["meta"])  # 阈值标定必须是无监督的


def test_runtime_each_gate_layer_does_not_read_gt(
    guarded: list[LeakGuard], thresholds: Any, spec: CampaignSpec
) -> None:
    """逐层调用四个门禁，参数直接传含 gt 的哨兵记录（不经 observable_view 剥离）。

    这比只测 ``evaluate()`` 更严：``evaluate()`` 会先剥 gt，
    即使某个子门禁写了 ``kox["gt"]`` 也只会 KeyError 而不是"读到答案"，
    静默降级的风险恰恰在子层。
    """
    for g in guarded:
        evaluate_g0(g)
        evaluate_g1(g, thresholds)
        evaluate_g2(g, spec)
        evaluate_g3(g, spec, thresholds)


def test_runtime_full_engine_does_not_read_gt(
    guarded: list[LeakGuard], thresholds: Any, specs: list[CampaignSpec]
) -> None:
    for g in guarded:
        for sp in specs[:2]:
            evaluate(g, sp, thresholds)


def test_runtime_budget_path_does_not_read_gt(
    guarded: list[LeakGuard], thresholds: Any, spec: CampaignSpec
) -> None:
    for g in guarded:
        targeting_reason(g, spec)
    gate_results = {g["kox_id"]: evaluate(dict(g), spec, thresholds) for g in guarded}
    build_candidates(guarded, gate_results, spec, thresholds, include_verdicts=("pass", "review", "reject"))


def test_runtime_prompt_builders_do_not_read_gt(
    guarded: list[LeakGuard], spec: CampaignSpec
) -> None:
    batch = list(guarded[:8])
    prompts_mod.tag_judge_messages(batch)
    prompts_mod.fit_score_messages(dataclasses.asdict(spec), batch)
    for variant in prompt_variants.TAG_VARIANTS:
        prompt_variants.build_messages(variant, batch)


# ---------------------------------------------------------------------------
# 3. 信号面白名单：想泄漏也没有通道
# ---------------------------------------------------------------------------
def test_extract_signals_output_is_flat_numeric_whitelist(records: list[dict[str, Any]]) -> None:
    sig = extract_signals(records[0])
    assert JUDGE_KEYS.isdisjoint(sig)
    for key, val in sig.items():
        assert isinstance(key, str)
        assert val is None or isinstance(val, (int, float)), f"{key} 不是标量：可能夹带了结构化 gt"


def test_observable_view_strips_gt_and_keeps_everything_else(
    records: list[dict[str, Any]],
) -> None:
    rec = records[0]
    view = observable_view(rec)
    assert GROUND_TRUTH_FIELD not in view
    assert set(view) == set(rec) - {GROUND_TRUTH_FIELD}
    for key in view:
        assert view[key] == rec[key], "剥离视图不应顺手改动其他字段"


def test_gate_result_serialization_never_carries_gt(
    records: list[dict[str, Any]], thresholds: Any, spec: CampaignSpec
) -> None:
    """门禁结果落盘时不能夹带 gt——否则 web/ 或下游会"顺便"拿到答案。"""
    for rec in records[:50]:
        payload = evaluate(rec, spec, thresholds).to_dict()
        blob = repr(payload)
        for key in JUDGE_KEYS:
            assert f"'{key}'" not in blob, f"GateResult.to_dict() 夹带了 {key}"


def test_prompt_text_never_contains_judge_answers(
    records: list[dict[str, Any]], spec: CampaignSpec
) -> None:
    """把答案写进 prompt 是最隐蔽的泄漏：模型"猜得准"其实是抄的。"""
    batch = records[:24]
    texts = [
        m["content"]
        for msgs in (
            prompts_mod.tag_judge_messages(batch),
            prompts_mod.fit_score_messages(dataclasses.asdict(spec), batch),
            *[prompt_variants.build_messages(v, batch) for v in prompt_variants.TAG_VARIANTS],
        )
        for m in msgs
    ]
    joined = "\n".join(texts)
    for key in ("true_categories", "is_fraud", "fraud_type", "mismatch_injected"):
        assert key not in joined, f"prompt 里出现了裁判字段名 {key}"
    # 更强的检查：逐条比对，prompt 里不得出现该达人的隐藏真实品类组合
    for rec in batch:
        hidden = rec["gt"]["true_categories"]
        declared = set(rec["declared_categories"]) | set(rec["observed_categories"])
        exclusive = [c for c in hidden if c not in declared]
        for cat in exclusive:
            assert cat not in joined, (
                f"{rec['kox_id']} 的隐藏真实品类 {cat} 出现在 prompt 中（既非声明也非观测）"
            )


# ---------------------------------------------------------------------------
# 4. Jaccard 分布守卫：标签不匹配任务不能是"送分题"
# ---------------------------------------------------------------------------
def _positive_jaccards(records: list[dict[str, Any]]) -> list[float]:
    return [
        jaccard(set(r["declared_categories"]), set(r["observed_categories"]))
        for r in records
        if r["gt"]["tag_mismatch"]
    ]


def test_positive_jaccard_distribution_is_not_bimodal(records: list[dict[str, Any]]) -> None:
    """正例的 declared vs observed Jaccard 不能退化成 0/1 双峰。

    历史缺陷记录（已修，故本测试应直接通过，不需要 xfail）：
    早期生成器把"不匹配"实现成整组替换品类，导致正例 Jaccard 几乎全为 0、
    负例几乎全为 1。后果非常隐蔽——一条 ``jaccard < 阈值`` 的朴素规则就能拿到接近
    完美的分数，各消融 arm 的召回/精确率会**完全相同或呈阶跃**，看起来像"模型很强"，
    实际上是数据集把答案写在了输入里。

    现在的判据（阈值按"能否用单一阈值一刀切"来定，不绑实测值）：
    - 中间带 (0.15, 0.85) 占比 ≥ 20%：必须存在"部分重叠"的难样本。
    - 恰好 0 的占比 ≤ 75%：不能大多数正例都是完全无交集的送分题。
    """
    vals = _positive_jaccards(records)
    assert len(vals) > 200, "正例太少，分布断言没有统计意义"
    n = len(vals)
    middle = sum(1 for v in vals if 0.15 < v < 0.85) / n
    exact_zero = sum(1 for v in vals if v == 0.0) / n
    exact_one = sum(1 for v in vals if v == 1.0) / n
    assert middle >= 0.20, (
        f"正例 Jaccard 中间带仅 {middle:.1%}（要求 ≥20%）：难样本不足，"
        "标签不匹配退化为可用单阈值一刀切的送分题"
    )
    assert exact_zero <= 0.75, f"正例 Jaccard 恰好 0 占比 {exact_zero:.1%} 过高（要求 ≤75%）"
    assert exact_one <= 0.20, f"正例 Jaccard 恰好 1 占比 {exact_one:.1%} 过高：正例定义可疑"


def test_naive_jaccard_rule_is_far_from_perfect(records: list[dict[str, Any]]) -> None:
    """朴素单阈值规则的 F1 必须明显低于 1——否则这个任务不需要模型。

    同时要求最佳阈值不落在扫描区间端点：落端点说明单调可分，仍是退化分布。
    """
    pos = [r["gt"]["tag_mismatch"] for r in records]
    js = [
        jaccard(set(r["declared_categories"]), set(r["observed_categories"])) for r in records
    ]
    grid = [round(0.02 * i, 2) for i in range(1, 50)]
    best_f1, best_t = 0.0, grid[0]
    for t in grid:
        tp = sum(1 for j, p in zip(js, pos) if j < t and p)
        fp = sum(1 for j, p in zip(js, pos) if j < t and not p)
        fn = sum(1 for j, p in zip(js, pos) if j >= t and p)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec_ = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec_ / (prec + rec_) if prec + rec_ else 0.0
        if f1 > best_f1:
            best_f1, best_t = f1, t
    assert best_f1 < 0.90, (
        f"朴素 Jaccard 阈值规则 F1={best_f1:.3f} @t={best_t} 过高："
        "标签不匹配任务被数据集写死成规则可解"
    )
    assert best_f1 > 0.30, f"朴素规则 F1 仅 {best_f1:.3f}：Jaccard 与标签几乎无关，标注口径可疑"


def test_negative_jaccard_is_not_all_ones(records: list[dict[str, Any]]) -> None:
    """负例也要有噪声：全 1 的负例意味着"匹配"被实现成了完全相等。"""
    neg = [
        jaccard(set(r["declared_categories"]), set(r["observed_categories"]))
        for r in records
        if not r["gt"]["tag_mismatch"]
    ]
    assert neg
    ones = sum(1 for v in neg if v == 1.0) / len(neg)
    assert ones <= 0.85, f"负例 Jaccard 恰好 1 占比 {ones:.1%} 过高：观测噪声缺失"


def test_positive_and_negative_distributions_overlap(records: list[dict[str, Any]]) -> None:
    """两类分布必须有重叠区间；完全不重叠 = 存在可分离的泄漏特征。"""
    pos = _positive_jaccards(records)
    neg = [
        jaccard(set(r["declared_categories"]), set(r["observed_categories"]))
        for r in records
        if not r["gt"]["tag_mismatch"]
    ]
    lo, hi = 0.15, 0.85
    pos_mid = sum(1 for v in pos if lo < v < hi)
    neg_mid = sum(1 for v in neg if lo < v < hi)
    assert pos_mid > 0 and neg_mid > 0, "正负例在中间带没有共存，任务可被单特征完全分离"
