"""把真链路打包进 ``api/_bundle/``，让部署产物自带 koxpilot 包与数据。

为什么需要这一步
----------------
``deploy_backend`` 只上传 ``api/`` 这一个目录，跑不到仓库里的 ``../src`` 与 ``../data``。
所以部署前把「代码 + 数据产物」复制进 ``api/_bundle/``，并让服务的路径解析器
优先读 bundle、回退仓库根（见 ``koxpilot_service/paths.py``）：

    本地开发  ->  直接读仓库里的 src/ + data/ + output/（改了源码立刻生效）
    线上部署  ->  读 _bundle/（自包含）

bundle 的目录形状刻意做成「仓库的缩小版」：

    _bundle/src/koxpilot/**.py      <- src/koxpilot
    _bundle/data/kox_5000.json      <- data/
    _bundle/data/briefs.json
    _bundle/output/thresholds.json  <- output/
    _bundle/output/llm_cache.json   （只保留 _meta + fit_scores，见下）

这样 ``koxpilot.io_utils.repo_root()``（= ``src/koxpilot/io_utils.py`` 上溯三层）
在 bundle 里自然算成 ``_bundle``，``data_dir()`` / ``output_dir()`` 不打补丁就能用。

Python 3.8 降级改写（本脚本的第二个职责）
----------------------------------------
运行时是 Python 3.8，而 ``src/koxpilot/`` 里有三类**运行期**（注解位置不算，
因为每个模块都有 ``from __future__ import annotations``）的 3.9+/3.10+ 用法。
三类都由本脚本做**机械的、保行为的**改写，源码一行不改：

1. ``@dataclass(slots=True)`` / ``kw_only=`` —— 3.10+ 才有的参数，直接摘掉。
   ``slots`` 只影响内存布局与「能否动态加属性」，不影响字段、比较、``asdict``、
   ``frozen`` 语义，本仓库也没有任何代码依赖「设不上属性会报错」。
2. 运行期的内建泛型下标（如 ``Kox = dict[str, Any]`` 这种模块级类型别名）——
   3.9+ 才支持，改写成注入的 ``typing`` 别名。
3. ``zip(a, b, strict=True)`` —— 3.10+ 参数，摘掉；它只是长度一致性断言，
   摘掉不改变正常路径的结果（仅在 3.8 上少一层保护，已在交付说明里写明）。

改写后本脚本会自我校验：重新 AST 扫一遍 bundle，确认上述三类**一处不剩**，
并用 ``compile()`` 过一遍语法。逐字节对照与 pytest 复跑见 ``verify_bundle.py``。

用法：``make api-bundle``（或 ``python3 api/build_bundle.py``）
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

API_DIR = Path(__file__).resolve().parent
REPO_ROOT = API_DIR.parent
BUNDLE = API_DIR / "_bundle"

#: 由本脚本生成的文件都带这个抬头，避免有人手改 bundle 然后奇怪「怎么又被改回去了」
GENERATED_HEADER = (
    "# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。\n"
    "# 真实源码在仓库的 {origin}；要改就改那边，然后重跑 `make api-bundle`。\n"
    "# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。\n"
)

#: 注入的 typing 别名：只用于替换运行期的内建泛型下标
COMPAT_IMPORT = (
    "\n# --- build_bundle.py 注入：Python 3.8 运行期泛型兼容别名（注解位置无需替换）---\n"
    "from typing import (  # noqa: E402\n"
    "    Dict as _py38_dict,\n"
    "    FrozenSet as _py38_frozenset,\n"
    "    List as _py38_list,\n"
    "    Set as _py38_set,\n"
    "    Tuple as _py38_tuple,\n"
    "    Type as _py38_type,\n"
    ")\n"
)

BUILTIN_GENERICS = {"dict", "list", "tuple", "set", "frozenset", "type"}

#: 需要随包一起发的数据产物：(仓库相对路径, bundle 相对路径, 处理方式)
DATA_FILES: Tuple[Tuple[str, str, str], ...] = (
    ("data/kox_5000.json", "data/kox_5000.json", "copy"),
    ("data/briefs.json", "data/briefs.json", "copy"),
    ("output/thresholds.json", "output/thresholds.json", "copy"),
    # llm_cache.json 完整版 1.86MB，其中 95% 是构建期 prompt 响应缓存（entries 段），
    # 服务侧只读 _meta + fit_scores 两段（见 eval/llm_fit.load_llm_fit_scores），
    # 所以这里瘦身到 ~160KB。瘦身只删服务读不到的段，不改任何一个分数。
    ("output/llm_cache.json", "output/llm_cache.json", "slim_llm_cache"),
)


# ---------------------------------------------------------------------------
# 源码改写
# ---------------------------------------------------------------------------
def _line_offsets(src: str) -> List[int]:
    """每行起始的绝对字符下标，用于把 AST 的 (lineno, col) 换成偏移量。"""
    offsets = [0]
    for line in src.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _pos(offsets: List[int], lineno: int, col: int) -> int:
    return offsets[lineno - 1] + col


class _AnnotationMarker(ast.NodeVisitor):
    """标记所有「注解子树」里的节点：它们因 future import 而不在运行期求值。"""

    def __init__(self) -> None:
        self.marked: set = set()

    def _mark(self, node: Optional[ast.AST]) -> None:
        if node is None:
            return
        for sub in ast.walk(node):
            self.marked.add(id(sub))

    def visit_FunctionDef(self, node: Any) -> None:
        args = node.args
        for arg in list(args.args) + list(args.kwonlyargs) + list(getattr(args, "posonlyargs", [])):
            self._mark(arg.annotation)
        if args.vararg:
            self._mark(args.vararg.annotation)
        if args.kwarg:
            self._mark(args.kwarg.annotation)
        self._mark(node.returns)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._mark(node.annotation)
        self.generic_visit(node)


def _strip_dataclass_kwargs(src: str) -> Tuple[str, int]:
    """摘掉 ``@dataclass(...)`` 里的 3.10+ 专属参数（slots / kw_only 等）。

    用 AST 定位装饰器关键字参数再按偏移量删除，比正则稳：既不会漏掉换行写法，
    也不会误伤字符串里恰好出现 ``slots=True`` 的地方。
    """
    drop = {"slots", "kw_only", "match_args", "weakref_slot"}
    tree = ast.parse(src)
    cuts: List[Tuple[int, int]] = []
    offsets = _line_offsets(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            name = dec.func.id if isinstance(dec.func, ast.Name) else getattr(dec.func, "attr", "")
            if name != "dataclass":
                continue
            for kw in dec.keywords:
                if kw.arg not in drop:
                    continue
                start = _pos(offsets, kw.lineno, kw.col_offset)
                end = _pos(offsets, kw.value.end_lineno, kw.value.end_col_offset)
                # 连带吃掉前面的逗号与空白；没有前置逗号（它是唯一参数）时吃掉后面的
                back = start
                while back > 0 and src[back - 1] in " \t\n":
                    back -= 1
                if back > 0 and src[back - 1] == ",":
                    start = back - 1
                else:
                    fwd = end
                    while fwd < len(src) and src[fwd] in " \t\n":
                        fwd += 1
                    if fwd < len(src) and src[fwd] == ",":
                        end = fwd + 1
                cuts.append((start, end))
    return _apply_cuts(src, cuts), len(cuts)


def _apply_cuts(src: str, cuts: List[Tuple[int, int]]) -> str:
    out = src
    for start, end in sorted(cuts, reverse=True):
        out = out[:start] + out[end:]
    return out


def _fix_runtime_generics(src: str) -> Tuple[str, int]:
    """把运行期的 ``dict[...]`` / ``list[...]`` 等换成注入的 typing 别名。"""
    tree = ast.parse(src)
    marker = _AnnotationMarker()
    marker.visit(tree)
    offsets = _line_offsets(src)
    edits: List[Tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript) or id(node) in marker.marked:
            continue
        value = node.value
        if not isinstance(value, ast.Name) or value.id not in BUILTIN_GENERICS:
            continue
        start = _pos(offsets, value.lineno, value.col_offset)
        end = _pos(offsets, value.end_lineno, value.end_col_offset)
        edits.append((start, end, "_py38_%s" % value.id))
    if not edits:
        return src, 0
    out = src
    for start, end, text in sorted(edits, reverse=True):
        out = out[:start] + text + out[end:]
    return _inject_compat_import(out), len(edits)


def _inject_compat_import(src: str) -> str:
    """把兼容别名的 import 插到 ``from __future__ import ...`` 之后。"""
    tree = ast.parse(src)
    anchor_line = None
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            anchor_line = node.end_lineno
            break
    lines = src.splitlines(keepends=True)
    if anchor_line is None:  # 没有 future import：插在 docstring 之后
        first = tree.body[0]
        anchor_line = first.end_lineno if isinstance(first, ast.Expr) else 0
    at = sum(len(line) for line in lines[:anchor_line])
    return src[:at] + COMPAT_IMPORT + src[at:]


def _fix_zip_strict(src: str) -> Tuple[str, int]:
    """摘掉 ``zip(..., strict=True)`` 的 3.10+ 参数。"""
    tree = ast.parse(src)
    offsets = _line_offsets(src)
    cuts: List[Tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "zip"):
            continue
        for kw in node.keywords:
            if kw.arg != "strict":
                continue
            start = _pos(offsets, kw.lineno, kw.col_offset)
            end = _pos(offsets, kw.value.end_lineno, kw.value.end_col_offset)
            back = start
            while back > 0 and src[back - 1] in " \t\n":
                back -= 1
            if back > 0 and src[back - 1] == ",":
                start = back - 1
            cuts.append((start, end))
    return _apply_cuts(src, cuts), len(cuts)


def transform_source(src: str, origin: str) -> Tuple[str, Dict[str, int]]:
    """对单个 .py 做全部降级改写，返回 (新源码, 各类改写计数)。"""
    stats: Dict[str, int] = {}
    src, stats["dataclass_kwargs"] = _strip_dataclass_kwargs(src)
    src, stats["zip_strict"] = _fix_zip_strict(src)
    src, stats["runtime_generics"] = _fix_runtime_generics(src)
    header = GENERATED_HEADER.format(origin=origin)
    return header + src, stats


# ---------------------------------------------------------------------------
# 自我校验
# ---------------------------------------------------------------------------
def audit_py38(root: Path) -> List[str]:
    """重新扫一遍产物，列出仍然存在的 3.9+/3.10+ 运行期用法（应当为空）。"""
    problems: List[str] = []
    for path in sorted(root.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        try:
            compile(src, str(path), "exec")
        except SyntaxError as exc:  # pragma: no cover - 改写出错时才会走到
            problems.append("%s 语法错误：%s" % (path, exc))
            continue
        tree = ast.parse(src)
        marker = _AnnotationMarker()
        marker.visit(tree)
        rel = path.relative_to(root)
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and id(node) not in marker.marked:
                v = node.value
                if isinstance(v, ast.Name) and v.id in BUILTIN_GENERICS:
                    problems.append("%s:%d 运行期内建泛型 %s[...]" % (rel, node.lineno, v.id))
            if isinstance(node, ast.ClassDef):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call):
                        for kw in dec.keywords:
                            if kw.arg in {"slots", "kw_only", "match_args", "weakref_slot"}:
                                problems.append(
                                    "%s:%d dataclass(%s=)" % (rel, dec.lineno, kw.arg)
                                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "zip":
                if any(kw.arg == "strict" for kw in node.keywords):
                    problems.append("%s:%d zip(strict=)" % (rel, node.lineno))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"removeprefix", "removesuffix", "lcm", "pairwise"}:
                    problems.append("%s:%d 3.9+ 方法 %s" % (rel, node.lineno, node.func.attr))
            if isinstance(node, ast.Attribute) and node.attr == "cache":
                if isinstance(node.value, ast.Name) and node.value.id == "functools":
                    problems.append("%s:%d functools.cache（3.9+）" % (rel, node.lineno))
    return problems


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _slim_llm_cache(src: Path, dst: Path) -> None:
    bundle = json.loads(src.read_text(encoding="utf-8"))
    slim = {"_meta": bundle.get("_meta", {}), "fit_scores": bundle.get("fit_scores", {})}
    slim["_meta"] = dict(slim["_meta"])
    slim["_meta"]["_bundle_note"] = (
        "由 api/build_bundle.py 生成：只保留服务侧真正会读的 _meta 与 fit_scores 两段，"
        "构建期 prompt 响应缓存（entries / tag_judge / brief_specs 等）未随包发布。"
        "分数一个没改，完整版在仓库的 output/llm_cache.json。"
    )
    dst.write_text(json.dumps(slim, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def build() -> int:
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    (BUNDLE / "src").mkdir(parents=True)

    src_root = REPO_ROOT / "src" / "koxpilot"
    if not src_root.is_dir():
        print("找不到 %s" % src_root, file=sys.stderr)
        return 1

    totals: Dict[str, int] = {"dataclass_kwargs": 0, "zip_strict": 0, "runtime_generics": 0}
    n_files = 0
    for path in sorted(src_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO_ROOT)
        out_path = BUNDLE / "src" / path.relative_to(REPO_ROOT / "src")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        new_src, stats = transform_source(path.read_text(encoding="utf-8"), str(rel))
        out_path.write_text(new_src, encoding="utf-8")
        for key, value in stats.items():
            totals[key] += value
        n_files += 1

    data_manifest: Dict[str, Any] = {}
    for rel_src, rel_dst, mode in DATA_FILES:
        source = REPO_ROOT / rel_src
        if not source.exists():
            print("缺少数据产物 %s，请先跑 `make all`" % rel_src, file=sys.stderr)
            return 1
        target = BUNDLE / rel_dst
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "slim_llm_cache":
            _slim_llm_cache(source, target)
        else:
            shutil.copy2(source, target)
        data_manifest[rel_dst] = {
            "from": rel_src,
            "mode": mode,
            "bytes": target.stat().st_size,
            "sha256": _sha256(target),
            # 数据集指纹必须报**源文件**的 sha256：它是「前端看到的数据 == 后端算的数据」
            # 这条证据链的锚点，瘦身过的文件不参与这条断言。
            "source_sha256": _sha256(source),
        }

    problems = audit_py38(BUNDLE / "src")
    if problems:
        print("Python 3.8 兼容自检未通过：", file=sys.stderr)
        for item in problems:
            print("  - %s" % item, file=sys.stderr)
        return 2

    (BUNDLE / "BUNDLE_INFO.json").write_text(
        json.dumps(
            {
                "generated_by": "api/build_bundle.py",
                "note": "本目录整体为生成产物，勿手改；改源码后重跑 `make api-bundle`。",
                "python_source_files": n_files,
                "py38_rewrites": totals,
                "data": data_manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("[bundle] koxpilot 源码 %d 个文件 -> _bundle/src/koxpilot" % n_files)
    print(
        "[bundle] 3.8 降级改写：dataclass 参数 %d 处 / zip(strict=) %d 处 / 运行期泛型 %d 处"
        % (totals["dataclass_kwargs"], totals["zip_strict"], totals["runtime_generics"])
    )
    for rel_dst, info in data_manifest.items():
        print(
            "[bundle] %-28s %8.1f KB  sha256=%s…"
            % (rel_dst, info["bytes"] / 1024.0, info["sha256"][:12])
        )
    print("[bundle] Python 3.8 兼容自检：通过（运行期 3.9+/3.10+ 用法 0 处）")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
