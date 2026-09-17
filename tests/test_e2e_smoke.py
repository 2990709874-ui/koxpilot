"""端到端 smoke：`python -m koxpilot.cli …` 这条真实入口，从零目录跑到全套产物。

为什么单独有这一层
------------------
前六个测试文件全部**绕开 CLI 直接调库**——这是对的（逻辑该在库里测），但它留下一个
谁都不查的缝：**"仓库里 committed 的那些 `data/*.json` / `output/*.json`，真的是这份代码
按 `make all` 跑出来的吗？"** 这条缝恰恰是简历型项目最容易出问题的地方：
库函数全绿，但产物是三周前另一版代码（或手改过一次）留下的，指标便再也无法追溯。

所以这一层只做四件别处做不到的事：

1. **产物溯源（逐字节）**：在临时目录里用默认参数跑一遍 `cli all`，
   把 9 个落盘文件与仓库里 committed 的版本**逐字节比**。
   不比"差不多"，不比几个抽样字段——比字节。
2. **日志 ↔ 产物对账**：`cli.py` 的 docstring 声称"构建期日志本身就是交付物"。
   那日志里印的每个数字都必须能在产物里找到同一个值；否则终端输出就只是安慰剂。
   （这一组专抓"打印用 A 算、落盘用 B 算"这种最难看出的分叉。）
3. **落盘的确实是库算出来的那份**：从沙箱磁盘把数据集 + `thresholds.json` 读回来，
   **把 `gt` 物理删掉**再独立重跑 `evaluate_all`，逐条对 5,000 个判定与四位小数分数。
   既验证"文件=计算结果"，也验证"这个计算根本不需要裁判字段"。
4. **确定性的真实边界**：跑两遍，实测**哪些文件变了**（而不是靠猜字段名）。
   结论：唯一不可逐字节复现的是 `output/thresholds.json`，唯一变化的叶子是
   `meta.calibrated_at_unix`（墙上时钟）。这条已按 docs/05 §10 第 8 条登记，
   本文件把它钉成可执行约束——包括"`metrics.json` 里不许出现这个字段"。

自我约束（与其余测试文件一致，且本文件额外加了三道机械保险）
------------------------------------------------------------
- **绝不写仓库**：所有 CLI 落盘都被重定向到 `tmp_path`；重定向手法是 monkeypatch
  `io_utils.repo_root`（全仓所有路径都由它派生，见 `data_dir()` / `output_dir()`）。
  另有 autouse 哨兵在**每个测试前后**给真实 `data/` 与 `output/` 拍指纹，
  一旦有文件被改/新增/删除立即失败；`TestGuardsThemselves` 里还有阳性对照，
  证明这个哨兵**抓得住**（含"改了内容但 size 与 mtime 都被伪装成原样"的情况）。
- **绝不联网**：autouse 把 `urlopen` 与 `socket.connect` 全部换成抛异常，
  端到端路径若偷偷调模型会当场炸（`Makefile` 里的 `llm` / `promptbench` 目标不在本文件范围内）。
- **绝不绑常量**：跨产物比的是**关系与一致性**（同一个数字在两处必须相等），
  唯一"绑死"的是与 committed 产物的逐字节相等——那本来就是本文件要证明的命题。

运行代价：`TestPipelineProvenance` 依赖的 `official_run` fixture 会真跑一遍 5,000 人
全流程（约 16s，其中 `eval` 占 13s，因为消融 + 敏感性要重跑十几遍门禁）。
它是 session 级、只跑一次，且被本文件十几个断言复用。
慢，但换来的是"产物可溯源"这个别的测试给不了的结论。
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import shutil
import socket
import time
import urllib.request
from collections.abc import Iterator
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from koxpilot import cli, io_utils
from koxpilot.budget.planner import plan_baseline, plan_campaign
from koxpilot.cli import GATE_SAMPLE_N
from koxpilot.datagen.config import FRAUD_TYPES, N_KOX, SAMPLE_SIZE, SEED
from koxpilot.eval.multiseed import format_summary
from koxpilot.gates.engine import GROUND_TRUTH_FIELD, evaluate, evaluate_all
from koxpilot.gates.thresholds import Thresholds, calibrate
from koxpilot.io_utils import file_sha256
from koxpilot.types import CampaignSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DATA = REPO_ROOT / "data"
REAL_OUTPUT = REPO_ROOT / "output"
MAKEFILE = REPO_ROOT / "Makefile"
DOC_BOUNDARIES = REPO_ROOT / "docs" / "05-boundaries.md"

#: 小规模端到端用的参数：够跑通全部代码路径，又不至于每个测试都等 16 秒。
SMOKE_N = 600
SMOKE_SEED = 777

#: `cli all` 会落盘的全部文件（相对仓库根），顺序即产生顺序。
PIPELINE_ARTIFACTS: tuple[str, ...] = (
    "data/kox_5000.json",
    f"data/kox_sample_{SAMPLE_SIZE}.json",
    "data/briefs.json",
    "output/thresholds.json",
    "output/verdicts.json",
    "output/gate_results_sample.json",
    "output/budget.json",
    "output/metrics.json",
    "output/audit.json",
)

#: 门禁/预算产物里**一个字符都不许出现**的裁判侧词汇（键名 + 只有 gt 才知道的取值）。
JUDGE_TOKENS: tuple[str, ...] = (
    f'"{GROUND_TRUTH_FIELD}"',
    "true_categories",
    "is_fraud",
    "fraud_type",
    "mismatch_injected",
    *FRAUD_TYPES,
)

#: 决策侧产物（由门禁/预算写出，代表"生产决定"）。裁判词汇必须为 0。
DECISION_ARTIFACTS: tuple[str, ...] = (
    "output/thresholds.json",
    "output/verdicts.json",
    "output/gate_results_sample.json",
    "output/budget.json",
)


# ---------------------------------------------------------------------------
# 工具：目录指纹 / JSON 叶子对比 / 日志解析
# ---------------------------------------------------------------------------
def stat_fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    """``{相对路径: (size, mtime_ns)}``。只 stat 不读盘，可以每个测试都拍一次。"""
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def hash_fingerprint(root: Path) -> dict[str, str]:
    """``{相对路径: sha256}``。比 stat 版慢得多，只在 session 首尾各拍一次。"""
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def leaf_diffs(a: Any, b: Any, path: str = "") -> list[str]:
    """两个 JSON 结构的**叶子级**差异路径列表（结构不同也报在路径上）。"""
    out: list[str] = []
    if type(a) is not type(b) and not (
        isinstance(a, (int, float)) and isinstance(b, (int, float))
    ):
        return [f"{path}<type:{type(a).__name__}!={type(b).__name__}>"]
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                out.append(f"{path}/{key}<missing>")
            else:
                out.extend(leaf_diffs(a[key], b[key], f"{path}/{key}"))
    elif isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}<len:{len(a)}!={len(b)}>"]
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(leaf_diffs(x, y, f"{path}[{i}]"))
    elif a != b:
        out.append(path)
    return out


def judge_tokens_in(text: str) -> dict[str, int]:
    """文本里出现的裁判词汇及次数（空 dict = 干净）。"""
    return {tok: text.count(tok) for tok in JUDGE_TOKENS if tok in text}


def literal_dict(text: str) -> dict[str, Any]:
    """从日志行里抽出 ``{...}``（Python repr 形式）并解析。"""
    start = text.index("{")
    end = text.rindex("}")
    value = ast.literal_eval(text[start : end + 1])
    assert isinstance(value, dict)
    return value


def log_line(log: str, prefix: str, contains: str = "") -> str:
    """取日志里第一条以 ``prefix`` 开头且含 ``contains`` 的行（找不到直接失败）。"""
    for line in log.splitlines():
        if line.startswith(prefix) and contains in line:
            return line
    raise AssertionError(f"日志里找不到 {prefix!r} + {contains!r} 的行：\n{log}")


def strip_gt(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """物理删掉 ``gt``：喂给门禁的记录里连键都不存在。"""
    return [{k: v for k, v in r.items() if k != GROUND_TRUTH_FIELD} for r in records]


def make_recipe(target: str) -> str:
    """取 Makefile 里某个目标的**配方行**（只要 tab 开头的那几行，注释与相邻目标一律不算）。

    这个精度是必要的：按"下一个目标名"切块会把紧跟其后的注释算进上一个目标，
    于是"clean 里不许出现 llm_cache.json"这条会被一段解释性注释误伤（我第一版就踩了）。
    """
    lines = MAKEFILE.read_text("utf-8").splitlines()
    recipe: list[str] = []
    inside = False
    for line in lines:
        if re.match(rf"^{re.escape(target)}:", line):
            inside = True
            continue
        if inside:
            if line.startswith("\t"):
                recipe.append(line)
            elif line.strip() == "" and not recipe:
                continue
            else:
                break
    return "\n".join(recipe)


# ---------------------------------------------------------------------------
# 沙箱：把 io_utils.repo_root 重定向到 tmp 目录后跑 CLI
# ---------------------------------------------------------------------------
@dataclass
class CliRun:
    argv: list[str]
    code: int
    stdout: str
    stderr: str


@dataclass
class Sandbox:
    """一个假仓库根：CLI 的一切读写都落在这里面。"""

    root: Path
    runs: list[CliRun] = field(default_factory=list)

    def run(self, *argv: str, expect: int | None = 0) -> CliRun:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        run = CliRun(list(argv), int(code), out.getvalue(), err.getvalue())
        self.runs.append(run)
        if expect is not None:
            assert run.code == expect, f"`cli {' '.join(argv)}` 退出码 {run.code}\n{run.stderr}"
        return run

    def path(self, rel: str) -> Path:
        return self.root / rel

    def json(self, rel: str) -> Any:
        return json.loads(self.path(rel).read_text("utf-8"))

    def files(self) -> list[str]:
        return sorted(
            p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()
        )

    def bring_llm_artifacts(self) -> None:
        """把 committed 的 LLM 构建期产物拷进沙箱（只读拷贝）。

        两份都要：``llm_bench.json`` 让表 6 的 LLM 列走 ok 分支，
        ``llm_cache.json`` 让表 6 附的"A4 适配分口径对照"能真跑注入反事实。
        少拷一份，沙箱产出的 metrics.json 就会和 committed 版本对不上——
        那不是产物漂移，只是沙箱缺料，很容易把人引到错误的结论上。
        """
        dst = self.path("output")
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("llm_bench.json", "llm_cache.json"):
            src = REAL_OUTPUT / name
            if src.exists():
                shutil.copy2(src, dst / name)


def make_sandbox(root: Path, mp: pytest.MonkeyPatch) -> Sandbox:
    root.mkdir(parents=True, exist_ok=True)
    mp.setattr(io_utils, "repo_root", lambda: root)
    return Sandbox(root=root)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path / "fake_repo", monkeypatch)


@pytest.fixture
def ready_sandbox(sandbox: Sandbox) -> Sandbox:
    """已经跑完 `data`（小规模）的沙箱。"""
    sandbox.run("data", "--n", str(SMOKE_N), "--seed", str(SMOKE_SEED))
    return sandbox


# ---------------------------------------------------------------------------
# 官方口径的一次真跑（session 级，约 16s，被本文件大量断言复用）
# ---------------------------------------------------------------------------
@dataclass
class OfficialRun:
    sandbox: Sandbox
    log: str
    code: int


@pytest.fixture(scope="session")
def official_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[OfficialRun]:
    root = tmp_path_factory.mktemp("official") / "fake_repo"
    before = hash_fingerprint(REAL_DATA), hash_fingerprint(REAL_OUTPUT)
    with pytest.MonkeyPatch.context() as mp:
        box = make_sandbox(root, mp)
        box.bring_llm_artifacts()  # 表 6 与其附表需要构建期真调产物，否则会走"降级"分支
        run = box.run("all")  # 默认参数 = 官方 n / seed，正是 committed 产物的口径
        yield OfficialRun(sandbox=box, log=run.stdout, code=run.code)
    assert (hash_fingerprint(REAL_DATA), hash_fingerprint(REAL_OUTPUT)) == before, (
        "official_run 期间仓库 data/ 或 output/ 被改动了"
    )


@pytest.fixture(scope="session")
def official_metrics(official_run: OfficialRun) -> dict[str, Any]:
    return official_run.sandbox.json("output/metrics.json")


@pytest.fixture(scope="session")
def official_records(official_run: OfficialRun) -> list[dict[str, Any]]:
    """从沙箱磁盘读回来的数据集（不是内存对象——要验的就是"落盘那份"）。"""
    return list(official_run.sandbox.json("data/kox_5000.json")["kox"])


# ---------------------------------------------------------------------------
# autouse 哨兵：不许写仓库、不许联网
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _repo_is_read_only() -> Iterator[None]:
    before = stat_fingerprint(REAL_DATA), stat_fingerprint(REAL_OUTPUT)
    yield
    after = stat_fingerprint(REAL_DATA), stat_fingerprint(REAL_OUTPUT)
    for tag, b, a in (("data/", before[0], after[0]), ("output/", before[1], after[1])):
        assert set(b) == set(a), f"{tag} 文件集合变了：新增 {set(a) - set(b)}，消失 {set(b) - set(a)}"
        changed = [k for k in b if b[k] != a[k]]
        assert not changed, f"{tag} 下这些文件被改动了：{changed}"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom_urlopen(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("端到端测试里不许发 HTTP 请求")

    def boom_connect(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("端到端测试里不许建 TCP 连接")

    monkeypatch.setattr(urllib.request, "urlopen", boom_urlopen)
    monkeypatch.setattr(socket.socket, "connect", boom_connect)


# ---------------------------------------------------------------------------
# 0. 先证明本文件的保险自己是有效的（否则下面全部结论都可能是假绿）
# ---------------------------------------------------------------------------
class TestGuardsThemselves:
    """哨兵与重定向的阳性对照：任何"没出事"的断言都要有一条能让它出事的用例作陪。"""

    def test_patch_target_is_the_single_source_of_all_paths(self, sandbox: Sandbox) -> None:
        # 所有路径都由 repo_root() 派生，因此只 patch 它一处就够；
        # 若哪天有人在别处硬编码路径，这条会先红。
        assert io_utils.data_dir() == sandbox.root / "data"
        assert io_utils.output_dir() == sandbox.root / "output"
        assert REPO_ROOT not in io_utils.data_dir().parents

    def test_unpatched_repo_root_really_points_at_this_repo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 没有这条，上面那条可能只是"patch 了一个本来就无关的函数"。
        monkeypatch.undo()
        assert io_utils.repo_root() == REPO_ROOT
        assert (io_utils.repo_root() / "src" / "koxpilot" / "cli.py").exists()

    def test_stat_fingerprint_catches_write_add_and_delete(self, tmp_path: Path) -> None:
        d = tmp_path / "probe"
        d.mkdir()
        (d / "a.json").write_text("{}", "utf-8")
        base = stat_fingerprint(d)
        (d / "b.json").write_text("{}", "utf-8")
        assert set(stat_fingerprint(d)) - set(base) == {"b.json"}
        (d / "b.json").unlink()
        assert stat_fingerprint(d) == base
        time.sleep(0.01)
        (d / "a.json").write_text("{ }", "utf-8")  # size 变了
        assert stat_fingerprint(d) != base

    def test_hash_fingerprint_catches_a_disguised_rewrite(self, tmp_path: Path) -> None:
        """内容被改、但 size 与 mtime 都伪装成原样——stat 版会漏，hash 版必须抓到。"""
        d = tmp_path / "probe"
        d.mkdir()
        f = d / "a.json"
        f.write_text('{"x":1}', "utf-8")
        st = f.stat()
        stat_base, hash_base = stat_fingerprint(d), hash_fingerprint(d)
        f.write_text('{"x":2}', "utf-8")  # 同长度
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # 把 mtime 伪装回去
        assert stat_fingerprint(d) == stat_base, "构造失败：这条对照本该模拟 stat 版的盲区"
        assert hash_fingerprint(d) != hash_base

    def test_network_guard_is_armed(self) -> None:
        with pytest.raises(AssertionError):
            urllib.request.urlopen("http://127.0.0.1:1/")
        with pytest.raises(AssertionError):
            socket.socket().connect(("127.0.0.1", 1))

    def test_sandbox_starts_empty(self, sandbox: Sandbox) -> None:
        # 沙箱不许"继承"仓库产物，否则 gate/budget 可能读到 committed 的 thresholds.json，
        # 于是"从零跑通"这个命题就没被验证。
        assert sandbox.files() == []

    def test_judge_token_scanner_can_fail(self, tmp_path: Path) -> None:
        """给泄漏扫描器一个阳性对照：伪造一份带 gt 的产物，必须被抓。"""
        clean = json.dumps({"verdicts": [{"kox_id": "K1", "verdict": "pass"}]}, ensure_ascii=False)
        dirty = json.dumps(
            {"verdicts": [{"kox_id": "K1", "verdict": "pass", "gt": {"is_fraud": True}}]},
            ensure_ascii=False,
        )
        assert judge_tokens_in(clean) == {}
        assert set(judge_tokens_in(dirty)) >= {'"gt"', "is_fraud"}

    def test_leaf_diff_helper_is_not_vacuous(self) -> None:
        a = {"m": {"x": 1, "y": [1, 2]}}
        assert leaf_diffs(a, a) == []
        assert leaf_diffs(a, {"m": {"x": 2, "y": [1, 2]}}) == ["/m/x"]
        assert leaf_diffs(a, {"m": {"x": 1, "y": [1, 3]}}) == ["/m/y[1]"]
        assert leaf_diffs(a, {"m": {"x": 1}}) == ["/m/y<missing>"]
        assert leaf_diffs(a, {"m": {"x": 1, "y": [1]}}) == ["/m/y<len:2!=1>"]


# ---------------------------------------------------------------------------
# 1. 产物溯源：committed 的 data/ + output/ 就是这份代码跑出来的
# ---------------------------------------------------------------------------
class TestPipelineProvenance:
    """`cli all`（默认参数）→ 与仓库里 committed 的产物逐字节对比。

    这是整套测试里唯一"绑死实测值"的地方，而且绑的方式是最硬的一种：比字节。
    命题不是"指标好看"，而是**"仓库里发表的产物可以由当前代码原地重建"**。
    """

    def test_all_exits_clean_and_writes_every_artifact(self, official_run: OfficialRun) -> None:
        assert official_run.code == 0
        missing = [rel for rel in PIPELINE_ARTIFACTS if not official_run.sandbox.path(rel).exists()]
        assert missing == []

    def test_steps_run_in_declared_order(self, official_run: OfficialRun) -> None:
        # `all` 的顺序不是装饰性的：gate 写的 thresholds.json 会被 budget / eval 读。
        tags = [
            line.split("]")[0] + "]"
            for line in official_run.log.splitlines()
            if line.startswith("[")
        ]
        first = {tag: tags.index(tag) for tag in ("[data]", "[gate]", "[budget]", "[eval]")}
        assert first["[data]"] < first["[gate]"] < first["[budget]"] < first["[eval]"]

    @pytest.mark.parametrize(
        "rel",
        [rel for rel in PIPELINE_ARTIFACTS if rel != "output/thresholds.json"],
    )
    def test_artifact_is_byte_identical_to_committed(
        self, official_run: OfficialRun, rel: str
    ) -> None:
        committed = REPO_ROOT / rel
        if not committed.exists():
            pytest.skip(f"仓库里没有 {rel}")
        assert official_run.sandbox.path(rel).read_bytes() == committed.read_bytes(), (
            f"{rel} 与 committed 版本不一致：产物不是当前代码跑出来的（或数据/口径已漂移）"
        )

    def test_thresholds_differ_only_by_the_wall_clock_field(
        self, official_run: OfficialRun
    ) -> None:
        """thresholds.json 是唯一不可逐字节复现的产物，差异必须**只有**那一个叶子。"""
        mine = official_run.sandbox.json("output/thresholds.json")
        committed = json.loads((REAL_OUTPUT / "thresholds.json").read_text("utf-8"))
        assert leaf_diffs(mine, committed) == ["/meta/calibrated_at_unix"]
        ts = int(mine["meta"]["calibrated_at_unix"])
        assert 1_700_000_000 < ts <= int(time.time()) + 60, "calibrated_at_unix 不是一个合理的墙上时钟"
        assert int(committed["meta"]["calibrated_at_unix"]) <= ts

    def test_this_determinism_exception_is_disclosed_in_docs(self) -> None:
        """代码与文档双向对账：字段还在 → 文档必须登记；字段被删 → 登记必须撤掉。"""
        src = (REPO_ROOT / "src" / "koxpilot" / "gates" / "thresholds.py").read_text("utf-8")
        doc = DOC_BOUNDARIES.read_text("utf-8")
        has_wall_clock = "calibrated_at_unix" in src and "time.time()" in src
        disclosed = "calibrated_at_unix" in doc
        assert has_wall_clock == disclosed, (
            "thresholds.json 的墙上时钟字段与 docs/05 §10 的登记不一致"
            "（要么补登记，要么撤掉已经不存在的问题）"
        )

    def test_metrics_carries_no_wall_clock_field(self, official_run: OfficialRun) -> None:
        # harness 的 docstring 明确承诺 metrics.json 不含生成时间；
        # 阈值那边的时间戳不许顺着 meta 渗进来，否则回归对比也就没了。
        text = official_run.sandbox.path("output/metrics.json").read_text("utf-8")
        assert "calibrated_at_unix" not in text
        assert "no_timestamp_note" in text

    def test_dataset_sha256_is_the_same_string_everywhere(self, official_run: OfficialRun) -> None:
        sha = file_sha256(official_run.sandbox.path("data/kox_5000.json"))
        box = official_run.sandbox
        assert box.json("output/verdicts.json")["meta"]["dataset_sha256"] == sha
        assert box.json("output/budget.json")["meta"]["dataset_sha256"] == sha
        assert box.json("output/metrics.json")["meta"]["dataset_sha256"] == sha
        assert box.json("output/audit.json")["meta"]["dataset_sha256"] == sha
        assert log_line(official_run.log, "[data]", "sha256=").endswith(f"sha256={sha[:16]}…")

    def test_audit_is_an_exact_subset_of_metrics(self, official_run: OfficialRun) -> None:
        """audit.json 是 metrics.json 的切片，不是"另算一遍"——两份数字不许出现分叉。"""
        metrics = official_run.sandbox.json("output/metrics.json")
        audit = official_run.sandbox.json("output/audit.json")
        assert set(audit) == {"meta", "counterfactual_value_audit", "cost_audit", "budget"}
        for key in audit:
            assert leaf_diffs(audit[key], metrics[key]) == [], f"audit.{key} 与 metrics 不一致"


# ---------------------------------------------------------------------------
# 2. 日志 ↔ 产物对账（"日志也是交付物"这句话得能兑现）
# ---------------------------------------------------------------------------
class TestLogMatchesArtifacts:
    """终端每印一个数字，产物里必须有同一个数字。

    这一组专抓一种很隐蔽的分叉：打印走一条计算路径、落盘走另一条
    （比如打印用未四舍五入的中间量，落盘用重算过的值）。
    面试现场拿终端输出对 metrics.json 的用法，只有在这组全绿时才成立。
    """

    def test_data_log_matches_dataset_meta(self, official_run: OfficialRun) -> None:
        meta = official_run.sandbox.json("data/kox_5000.json")["meta"]
        head = log_line(official_run.log, "[data]", "seed=")
        assert f"seed={meta['seed']}" in head
        assert f"n={meta['n']}" in head
        assert meta["seed"] == SEED and meta["n"] == N_KOX
        rates = literal_dict(log_line(official_run.log, "[data]", "注入实际比例"))
        assert rates == meta["injection_actual_rates"]
        counts = literal_dict(log_line(official_run.log, "[data]", "gt 三档分布"))
        assert counts == meta["verdict_counts"]

    def test_gate_log_matches_thresholds_and_verdicts(self, official_run: OfficialRun) -> None:
        box = official_run.sandbox
        thr = box.json("output/thresholds.json")
        verdicts = box.json("output/verdicts.json")
        groups_line = log_line(official_run.log, "[gate]", "分组")
        assert re.search(r"标定 (\d+) 个", groups_line)
        n_groups = int(re.search(r"标定 (\d+) 个", groups_line).group(1))
        assert n_groups == len(thr["groups"]) == thr["meta"]["n_groups"]

        dist_line = log_line(official_run.log, "[gate]", "判定分布")
        assert literal_dict(dist_line) == verdicts["meta"]["counts"]
        assert f"（共 {verdicts['meta']['n']} 人）" in dist_line
        # 计数不是自报的：从明细逐条数一遍必须一致。
        recount: dict[str, int] = {"pass": 0, "review": 0, "reject": 0}
        for row in verdicts["verdicts"]:
            recount[row["verdict"]] += 1
        assert recount == verdicts["meta"]["counts"]
        assert sum(recount.values()) == verdicts["meta"]["n"] == N_KOX

    def test_budget_log_matches_every_plan(self, official_run: OfficialRun) -> None:
        plans = official_run.sandbox.json("output/budget.json")["plans"]
        assert len(plans) == official_run.sandbox.json("output/budget.json")["meta"]["n_briefs"] >= 1
        for row in plans:
            plan = row["koxpilot"]
            line = log_line(official_run.log, "[budget]", row["campaign_id"])
            assert f"候选 {plan['candidate_pool']} 人" in line
            assert f"选中 {plan['n_selected']} 人" in line
            assert f"${plan['spent_usd']:,.0f}/{plan['budget_usd']:,.0f}" in line
            assert f"（利用率 {plan['spent_usd'] / plan['budget_usd']:.1%}）" in line
            assert f"约束全部满足={plan['constraints']['all_enforced_satisfied']}" in line
            assert plan["n_selected"] == len(plan["selected"])

    def test_eval_log_matches_table_1_and_2(self, official_metrics: dict[str, Any],
                                            official_run: OfficialRun) -> None:
        t1 = official_metrics["table_1_fraud_detection"]
        line = log_line(official_run.log, "[eval]", "水号识别")
        assert f"P={t1['strict']['precision']:.4f}" in line
        assert f"R={t1['strict']['recall']:.4f}" in line
        assert f"F1={t1['strict']['f1']:.4f}" in line
        assert f"宽口径 F1={t1['loose']['f1']:.4f}" in line
        assert f"AUC={t1['auc']:.4f}" in line

        for ftype, row in t1["per_fraud_type"].items():
            per = log_line(official_run.log, "[eval]", ftype)
            assert f"n={row['n']:4d}" in per
            assert f"严召回={row['recall_strict']:.3f}" in per
            assert f"宽召回={row['recall_loose']:.3f}" in per
            assert f"AUC={row['auc_vs_clean']:.3f}" in per

        t2 = official_metrics["table_2_verdict_confusion"]
        line2 = log_line(official_run.log, "[eval]", "三分类")
        assert f"准确率={t2['accuracy']:.4f}" in line2 and f"macroF1={t2['macro_f1']:.4f}" in line2
        for verdict, row in t2["per_class"].items():
            per = log_line(official_run.log, "[eval]", f"{verdict:6s} P=")
            assert f"P={row['precision']:.4f} R={row['recall']:.4f} F1={row['f1']:.4f}" in per

    def test_eval_log_reports_weak_spots_verbatim(self, official_metrics: dict[str, Any],
                                                  official_run: OfficialRun) -> None:
        """弱项是"如实呈现"的核心：日志里的三条必须是产物里的前三条原文，不许挑好看的。"""
        printed = [
            line.split("- ", 1)[1]
            for line in official_run.log.splitlines()
            if line.startswith("[eval]   - ")
        ]
        expected = [spot["note"] for spot in official_metrics["weak_spots"][:3]]
        assert printed == expected
        assert len(official_metrics["weak_spots"]) >= len(printed)

    def test_eval_log_matches_sensitivity_and_counterfactual(
        self, official_metrics: dict[str, Any], official_run: OfficialRun
    ) -> None:
        t5 = official_metrics["table_5_sensitivity"]
        line = log_line(official_run.log, "[eval]", "敏感性")
        assert f"偏移={t5['max_abs_f1_shift']:.4f}" in line
        assert f"稳健={t5['stable']}" in line
        head = official_metrics["counterfactual_value_audit"]["headline"]
        assert log_line(official_run.log, "[eval]", "反事实价值").endswith(head)

    def test_log_paths_are_repo_relative_not_absolute(self, official_run: OfficialRun) -> None:
        # `_p()` 的作用就是把路径印成仓库相对；印成绝对路径会把跑构建的机器路径泄进日志。
        for rel in PIPELINE_ARTIFACTS:
            assert rel in official_run.log
        assert str(official_run.sandbox.root) not in official_run.log


# ---------------------------------------------------------------------------
# 3. 落盘的确实是库算出来的那份（而且算它根本不需要 gt）
# ---------------------------------------------------------------------------
class TestArtifactsAreRecomputable:
    """把产物读回来，用库函数独立重算，逐条对。

    `cli.py` 的 docstring 说"CLI 唯一需要保证的是落盘的文件确实是库算出来的那份"——
    这一组就是那句话的执行体。重算时**把 gt 物理删掉**，于是它同时是一条
    端到端级别的反自证控制：真要偷看裁判字段，重算结果就会和产物对不上（或直接崩）。
    """

    def test_all_5000_verdicts_recompute_from_disk_artifacts(
        self, official_run: OfficialRun, official_records: list[dict[str, Any]]
    ) -> None:
        box = official_run.sandbox
        thresholds = Thresholds.from_dict(box.json("output/thresholds.json"))
        observable = strip_gt(official_records)
        assert all(GROUND_TRUTH_FIELD not in r for r in observable)

        recomputed = {r.kox_id: r for r in evaluate_all(observable, thresholds=thresholds)}
        rows = box.json("output/verdicts.json")["verdicts"]
        assert len(rows) == len(recomputed) == N_KOX
        for row in rows:
            res = recomputed[row["kox_id"]]
            assert row["verdict"] == res.verdict
            assert row["group_key"] == res.group_key
            assert row["completeness"] == round(res.completeness, 4)
            assert row["authenticity"] == round(res.authenticity_score, 4)
            assert row["consistency"] == round(res.consistency_score, 4)
            assert row["brand_safety"] == round(res.brand_safety_score, 4)
            assert row["fraud_score"] == round(res.fraud_score, 4)
            assert row["rules"] == list(res.rule_ids)

    def test_verdicts_are_sorted_by_kox_id(self, official_run: OfficialRun) -> None:
        # 顺序稳定是"逐字节可比"的前提之一（dict 顺序不稳的产物没法做回归 diff）。
        ids = [row["kox_id"] for row in official_run.sandbox.json("output/verdicts.json")["verdicts"]]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)

    def test_thresholds_round_trip_through_disk_is_lossless(
        self, official_run: OfficialRun, official_records: list[dict[str, Any]]
    ) -> None:
        """磁盘上的 thresholds.json 与内存标定结果必须等价（除墙上时钟）。

        这条卡的是一个很贵的坑：如果 `from_dict` 丢了精度或漏了某个块，
        `budget`/`eval`（读盘）与 `gate`（内存）就会给出不同判定，而两边各自都"自洽"。
        """
        disk = official_run.sandbox.json("output/thresholds.json")
        fresh = calibrate(
            strip_gt(official_records),
            official_run.sandbox.json("data/kox_5000.json")["meta"],
        ).to_dict()
        assert leaf_diffs(json.loads(json.dumps(fresh, ensure_ascii=False)), disk) == [
            "/meta/calibrated_at_unix"
        ]

    def test_gate_sample_is_the_first_N_records_with_full_evidence(
        self, official_run: OfficialRun, official_records: list[dict[str, Any]]
    ) -> None:
        box = official_run.sandbox
        sample = box.json("output/gate_results_sample.json")
        assert sample["meta"]["n"] == GATE_SAMPLE_N == len(sample["results"])
        assert [r["kox_id"] for r in sample["results"]] == [
            k["kox_id"] for k in official_records[:GATE_SAMPLE_N]
        ]
        thresholds = Thresholds.from_dict(box.json("output/thresholds.json"))
        recomputed = {
            r.kox_id: json.loads(json.dumps(r.to_dict(), ensure_ascii=False))
            for r in evaluate_all(strip_gt(official_records[:GATE_SAMPLE_N]), thresholds=thresholds)
        }
        for row in sample["results"]:
            assert leaf_diffs(row, recomputed[row["kox_id"]]) == []
        # 证据链样本必须真的带证据（否则"可核对"是空话）
        assert any(row["reasons"] for row in sample["results"])

    def test_every_budget_plan_recomputes_bit_for_bit(
        self, official_run: OfficialRun, official_records: list[dict[str, Any]]
    ) -> None:
        box = official_run.sandbox
        thresholds = Thresholds.from_dict(box.json("output/thresholds.json"))
        briefs = box.json("data/briefs.json")["briefs"]
        observable = strip_gt(official_records)
        artifact = {row["campaign_id"]: row for row in box.json("output/budget.json")["plans"]}
        assert set(artifact) == {b["spec"]["campaign_id"] for b in briefs}

        for brief in briefs:
            spec = CampaignSpec.from_dict(brief["spec"])
            plan, results = plan_campaign(observable, spec, thresholds)
            base = plan_baseline(observable, spec, thresholds, results)
            row = artifact[spec.campaign_id]
            assert row["name"] == spec.name
            for key, computed in (("koxpilot", plan), ("baseline_followers", base)):
                dumped = json.loads(json.dumps(computed.to_dict(), ensure_ascii=False))
                assert leaf_diffs(row[key], dumped) == [], f"{spec.campaign_id}.{key} 与重算不一致"

    def test_budget_never_overspends_and_reports_real_utilization(
        self, official_run: OfficialRun
    ) -> None:
        for row in official_run.sandbox.json("output/budget.json")["plans"]:
            for key in ("koxpilot", "baseline_followers"):
                plan = row[key]
                assert plan["spent_usd"] <= plan["budget_usd"] + 1e-6, f"{key} 超预算"
                assert abs(plan["utilization"] - plan["spent_usd"] / plan["budget_usd"]) < 1e-4
                assert plan["spent_usd"] == pytest.approx(
                    sum(a["amount_usd"] for a in plan["selected"]), abs=0.02
                )
                assert plan["n_selected"] == len(plan["selected"])
                assert len({a["kox_id"] for a in plan["selected"]}) == plan["n_selected"]

    def test_table_6_uses_the_real_llm_bench_that_was_copied_in(
        self, official_metrics: dict[str, Any]
    ) -> None:
        # 表 6 的 LLM 那一列只能来自构建期真调产物；这里确认走的是 ok 分支
        # （而不是被降级分支静静吞掉——降级分支的分支覆盖在下面单独测）。
        t6 = official_metrics["table_6_llm_vs_rule"]
        assert t6["status"] == "ok"
        assert t6["llm_bench_path"] == "llm_bench.json"
        assert t6["best_llm_model"] in t6["llm_arm"]
        bench = json.loads((REAL_OUTPUT / "llm_bench.json").read_text("utf-8"))
        assert set(t6["models"].values()) <= set(bench["models"].values()) or set(
            t6["models"]
        ) <= set(bench["models"])


# ---------------------------------------------------------------------------
# 4. 决策产物里不许有裁判字段（端到端口径）
# ---------------------------------------------------------------------------
class TestNoJudgeLeakInArtifacts:
    """静态/运行期泄漏在 test_no_leakage.py 已经查过；这里查的是**落盘产物**这一层。

    差别很实际：代码可以完全干净，但只要有人在 CLI 里顺手把 gt 塞进 verdicts.json，
    前端/评测就能间接读到裁判字段，"离线固化"这层封装也就漏了。
    """

    @pytest.mark.parametrize("rel", DECISION_ARTIFACTS)
    def test_decision_artifacts_are_clean(self, official_run: OfficialRun, rel: str) -> None:
        found = judge_tokens_in(official_run.sandbox.path(rel).read_text("utf-8"))
        assert found == {}, f"{rel} 里出现了裁判词汇：{found}"

    def test_dataset_on_disk_does_carry_gt(self, official_run: OfficialRun) -> None:
        # 阳性对照 + 事实说明：数据集本身**必须**带 gt（评测要用），
        # 所以上面那组"干净"不是因为整个仓库压根没有 gt 这个词。
        found = judge_tokens_in(official_run.sandbox.path("data/kox_5000.json").read_text("utf-8"))
        assert set(found) >= {'"gt"', "is_fraud", "fraud_type", "true_categories"}
        assert found['"gt"'] == N_KOX

    def test_metrics_is_allowed_and_required_to_be_judge_side(
        self, official_metrics: dict[str, Any]
    ) -> None:
        # 反向约束：metrics.json 是裁判报告，per_fraud_type 这张表必须在。
        # 哪天它"变干净"了，说明分类型召回被删了——那是指标缩水，不是安全改进。
        per_type = official_metrics["table_1_fraud_detection"]["per_fraud_type"]
        assert set(per_type) == set(FRAUD_TYPES)
        assert all(row["n"] > 0 for row in per_type.values())

    def test_explain_hides_gt_unless_asked(self, ready_sandbox: Sandbox) -> None:
        box = ready_sandbox
        box.run("gate")  # 让 explain 走读盘阈值那条路径
        records = box.json("data/kox_5000.json")["kox"]
        frauds = [r for r in records if r["gt"]["is_fraud"]][:2]
        clean = [r for r in records if not r["gt"]["is_fraud"]][:3]
        assert frauds and clean, "样本里既要有水号也要有干净号，否则这条测试是空转"

        for kox in frauds + clean:
            default = box.run("explain", kox["kox_id"])
            found = judge_tokens_in(default.stdout)
            assert found == {}, f"explain 默认输出泄漏了裁判字段：{found}"
            assert kox["kox_id"] in default.stdout

        shown = box.run("explain", frauds[0]["kox_id"], "--show-gt")
        assert "ground truth" in shown.stdout
        assert set(judge_tokens_in(shown.stdout)) >= {"is_fraud", "true_categories"}, (
            "--show-gt 必须真的打印裁判字段，否则上面那条'默认不泄漏'可能只是命令没生效"
        )


# ---------------------------------------------------------------------------
# 5. data 子命令
# ---------------------------------------------------------------------------
class TestDataCommand:
    def test_respects_n_and_seed_and_writes_three_files(self, sandbox: Sandbox) -> None:
        run = sandbox.run("data", "--n", str(SMOKE_N), "--seed", str(SMOKE_SEED))
        assert sandbox.files() == [
            "data/briefs.json",
            "data/kox_5000.json",
            f"data/kox_sample_{SAMPLE_SIZE}.json",
        ], "data 阶段不许写 output/，也不许多写文件"
        meta = sandbox.json("data/kox_5000.json")["meta"]
        assert meta["seed"] == SMOKE_SEED and meta["n"] == SMOKE_N
        assert f"seed={SMOKE_SEED}" in run.stdout and f"n={SMOKE_N}" in run.stdout

    def test_two_runs_are_byte_identical(self, sandbox: Sandbox) -> None:
        sandbox.run("data", "--n", str(SMOKE_N), "--seed", str(SMOKE_SEED))
        first = {rel: sandbox.path(rel).read_bytes() for rel in sandbox.files()}
        sandbox.run("data", "--n", str(SMOKE_N), "--seed", str(SMOKE_SEED))
        second = {rel: sandbox.path(rel).read_bytes() for rel in sandbox.files()}
        assert first == second

    def test_different_seed_gives_different_dataset(self, sandbox: Sandbox) -> None:
        # 阳性对照：上一条"两次相同"必须不是因为 --seed 根本没被读。
        sandbox.run("data", "--n", str(SMOKE_N), "--seed", str(SMOKE_SEED))
        first = sandbox.path("data/kox_5000.json").read_bytes()
        sandbox.run("data", "--n", str(SMOKE_N), "--seed", str(SMOKE_SEED + 1))
        assert sandbox.path("data/kox_5000.json").read_bytes() != first

    def test_sample_is_a_prefix_and_meta_does_not_lie(self, sandbox: Sandbox) -> None:
        sandbox.run("data", "--n", str(SMOKE_N), "--seed", str(SMOKE_SEED))
        full = sandbox.json("data/kox_5000.json")
        sample = sandbox.json(f"data/kox_sample_{SAMPLE_SIZE}.json")
        assert sample["kox"] == full["kox"][:SAMPLE_SIZE]
        assert sample["meta"]["n"] == len(sample["kox"]) == SAMPLE_SIZE

    def test_sample_meta_stays_honest_when_n_is_tiny(self, sandbox: Sandbox) -> None:
        """`--n` 小于样本量时，样本文件的自报条数必须等于实际条数。

        这不是假想：原实现把 `meta.n` 写死成 SAMPLE_SIZE，`--n 20` 时样本里只有 20 条
        却自报 50 条。数字对不上内容，是本仓库最不该出现的那类错误。
        """
        tiny = 20
        sandbox.run("data", "--n", str(tiny), "--seed", str(SMOKE_SEED))
        sample = sandbox.json(f"data/kox_sample_{SAMPLE_SIZE}.json")
        assert len(sample["kox"]) == tiny
        assert sample["meta"]["n"] == tiny
        assert str(tiny) in sample["meta"]["note"]

    def test_injection_rates_in_log_are_actual_not_configured(self, sandbox: Sandbox) -> None:
        """日志印的是**实际**注入比例（有限样本下不会正好等于配置值）。"""
        from koxpilot.datagen.config import INJECTION_RATES

        run = sandbox.run("data", "--n", str(SMOKE_N), "--seed", str(SMOKE_SEED))
        actual = literal_dict(log_line(run.stdout, "[data]", "注入实际比例"))
        meta = sandbox.json("data/kox_5000.json")["meta"]
        assert actual == meta["injection_actual_rates"]
        for key, configured in INJECTION_RATES.items():
            assert abs(actual[key] - configured) < 0.05, f"{key} 实际比例偏离配置过远"

    def test_briefs_are_consumable_by_campaign_spec(self, sandbox: Sandbox) -> None:
        sandbox.run("data", "--n", str(SMOKE_N), "--seed", str(SMOKE_SEED))
        briefs = sandbox.json("data/briefs.json")["briefs"]
        assert len(briefs) >= 1
        specs = [CampaignSpec.from_dict(b["spec"]) for b in briefs]
        assert all(s.budget_usd > 0 for s in specs)
        assert len({s.campaign_id for s in specs}) == len(specs)


# ---------------------------------------------------------------------------
# 6. gate / budget / eval 的阶段契约与相互依赖
# ---------------------------------------------------------------------------
class TestStageContracts:
    def test_gate_writes_only_its_three_artifacts(self, ready_sandbox: Sandbox) -> None:
        before = set(ready_sandbox.files())
        ready_sandbox.run("gate")
        assert set(ready_sandbox.files()) - before == {
            "output/thresholds.json",
            "output/verdicts.json",
            "output/gate_results_sample.json",
        }

    def test_gate_sample_shrinks_with_the_dataset(self, sandbox: Sandbox) -> None:
        # n < GATE_SAMPLE_N 时不许自报 200 条（同一类"自报数字要对得上内容"的约束）。
        sandbox.run("data", "--n", "120", "--seed", str(SMOKE_SEED))
        sandbox.run("gate")
        sample = sandbox.json("output/gate_results_sample.json")
        assert len(sample["results"]) == 120 == sample["meta"]["n"]

    def test_budget_without_a_prior_gate_gives_identical_plans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`budget` 单跑（内存标定）与 `gate` 之后跑（读盘阈值）必须逐字节一致。

        两条路径分叉过一次就意味着：`make budget` 的结果取决于你之前有没有跑过 `make gate`。
        那种"看跑法而定"的产物是不能发表的。
        """
        with pytest.MonkeyPatch.context() as mp_a:
            box_a = make_sandbox(tmp_path / "with_gate", mp_a)
            box_a.run("data", "--n", str(SMOKE_N), "--seed", str(SMOKE_SEED))
            box_a.run("gate")
            box_a.run("budget")
        with pytest.MonkeyPatch.context() as mp_b:
            box_b = make_sandbox(tmp_path / "solo", mp_b)
            box_b.run("data", "--n", str(SMOKE_N), "--seed", str(SMOKE_SEED))
            box_b.run("budget")
        assert not box_b.path("output/thresholds.json").exists(), "budget 不该写阈值文件"
        assert (
            box_a.path("output/budget.json").read_bytes()
            == box_b.path("output/budget.json").read_bytes()
        )

    def test_eval_degrades_honestly_without_llm_bench(self, ready_sandbox: Sandbox) -> None:
        """没有构建期真调产物时，表 6 必须**说自己没有**，而不是编一列出来。"""
        ready_sandbox.run("gate")
        ready_sandbox.run("eval")
        assert not ready_sandbox.path("output/llm_bench.json").exists()
        metrics = ready_sandbox.json("output/metrics.json")
        t6 = metrics["table_6_llm_vs_rule"]
        assert t6["status"] != "ok"
        assert "llm_bench.json" in str(t6["explanation"])
        assert any("llm_bench.json" in note for note in metrics["honesty_notes"]), (
            "降级必须进 honesty_notes，否则读 metrics 的人不会注意到这一列是空的"
        )

    def test_eval_reads_thresholds_from_disk_unless_recalibrating(
        self, ready_sandbox: Sandbox
    ) -> None:
        """篡改磁盘阈值 → eval 必须受影响；加 `--recalibrate` → 必须完全不受影响。

        这条同时证明两件事：(1) 默认口径真的以 `output/thresholds.json` 为准
        （因此发表的 metrics 与发表的阈值是同一套）；(2) `--recalibrate` 不是装饰性开关。
        """
        box = ready_sandbox
        box.run("gate")
        box.run("eval")
        honest = box.json("output/metrics.json")

        pristine = box.path("output/thresholds.json").read_text("utf-8")
        tampered = json.loads(pristine)

        def halve(node: dict[str, Any]) -> None:
            for key, value in node.items():
                if isinstance(value, dict):
                    halve(value)
                elif isinstance(value, (int, float)) and re.fullmatch(r"p\d+", key):
                    node[key] = value * 0.5

        for block in ("groups", "platform", "global"):
            halve(tampered[block])
        box.path("output/thresholds.json").write_text(
            json.dumps(tampered, ensure_ascii=False), "utf-8"
        )

        box.run("eval")
        poisoned = box.json("output/metrics.json")
        assert (
            poisoned["table_1_fraud_detection"]["strict"]["f1"]
            != honest["table_1_fraud_detection"]["strict"]["f1"]
        ), "篡改阈值后指标没变 —— 说明 eval 根本没在用磁盘上的阈值"

        box.run("eval", "--recalibrate")
        recal = box.json("output/metrics.json")
        assert leaf_diffs(recal, honest) == [], "--recalibrate 没有绕开被篡改的阈值文件"
        assert json.loads(box.path("output/thresholds.json").read_text("utf-8")) == tampered, (
            "eval 不该回写 thresholds.json（回写会把探索性重标定悄悄变成正式口径）"
        )

    def test_eval_writes_only_metrics_and_audit(self, ready_sandbox: Sandbox) -> None:
        ready_sandbox.run("gate")
        before = set(ready_sandbox.files())
        ready_sandbox.run("eval")
        assert set(ready_sandbox.files()) - before == {
            "output/metrics.json",
            "output/audit.json",
        }

    @pytest.mark.parametrize("cmd", ["gate", "budget", "eval"])
    def test_stages_fail_loudly_without_data(self, sandbox: Sandbox, cmd: str) -> None:
        # 缺数据时必须抛错并给出下一步动作，而不是写出一份"0 条记录"的空产物。
        with pytest.raises(FileNotFoundError) as exc:
            sandbox.run(cmd)
        assert "make data" in str(exc.value)
        assert sandbox.files() == []


# ---------------------------------------------------------------------------
# 7. 确定性的真实边界：跑两遍，实测哪些文件会变
# ---------------------------------------------------------------------------
class TestDeterminismBoundary:
    """不靠猜字段名，靠**重跑一遍**来界定"哪些产物可以逐字节回归对比"。

    结论（本仓库现状）：唯一变的是 `output/thresholds.json`，唯一变的叶子是
    `meta.calibrated_at_unix`。这条已按 docs/05 §10 第 8 条登记。
    如果哪天有人往产物里塞了 `generated_at`，这一组会立刻红——
    而不是等到半年后有人想做回归 diff 时才发现"两次跑出来的文件永远不一样"。
    """

    def test_only_the_threshold_timestamp_changes_across_runs(self, sandbox: Sandbox) -> None:
        def full_pipeline() -> dict[str, bytes]:
            sandbox.run("data", "--n", str(SMOKE_N), "--seed", str(SMOKE_SEED))
            sandbox.run("gate")
            sandbox.run("budget")
            sandbox.run("eval")
            return {rel: sandbox.path(rel).read_bytes() for rel in sandbox.files()}

        first = full_pipeline()
        second = full_pipeline()
        assert set(first) == set(second)
        changed = sorted(rel for rel in first if first[rel] != second[rel])
        assert changed == ["output/thresholds.json"], (
            f"除阈值文件外还有产物在两次运行间变化：{changed}（确定性回归对比会因此失效）"
        )
        assert leaf_diffs(
            json.loads(first["output/thresholds.json"]),
            json.loads(second["output/thresholds.json"]),
        ) == ["/meta/calibrated_at_unix"]

    def test_multiseed_is_reproducible_and_worker_count_invariant(self, sandbox: Sandbox) -> None:
        """多种子实验：同参数两次一致，且**并行度不影响结果**。

        后半句是并行实现最容易破的性质（结果顺序依赖 / 共享 RNG）。
        Makefile 把 WORKERS 做成可调参数，那"调它不改变结论"就必须是被测过的。
        """
        sandbox.run("multiseed", "--seeds", "2", "--n", "300")
        serial = sandbox.path("output/multiseed.json").read_bytes()
        sandbox.run("multiseed", "--seeds", "2", "--n", "300")
        assert sandbox.path("output/multiseed.json").read_bytes() == serial
        sandbox.run("multiseed", "--seeds", "2", "--n", "300", "--workers", "2")
        assert sandbox.path("output/multiseed.json").read_bytes() == serial


# ---------------------------------------------------------------------------
# 8. multiseed 子命令的独立性
# ---------------------------------------------------------------------------
class TestMultiseedIsSelfContained:
    """multiseed 的 docstring 声称"刻意不读 data/ 也不复用 output/thresholds.json"。

    这句话很重要：如果它偷偷复用定稿阈值，跨种子的"波动范围"会被人为压窄，
    于是"稳健性"结论就是自证的。所以这里用两条**物理**证据来验：
    沙箱里根本没有 data/；沙箱里的 thresholds.json 是被投毒的。
    """

    def test_runs_with_no_data_dir_at_all(self, sandbox: Sandbox) -> None:
        run = sandbox.run("multiseed", "--seeds", "2", "--n", "300")
        assert not sandbox.path("data").exists(), "multiseed 不该需要（也不该创建）data/"
        assert sandbox.files() == ["output/multiseed.json"]
        payload = sandbox.json("output/multiseed.json")
        assert payload["meta"]["n_seeds"] == 2
        assert payload["meta"]["seeds"][0] == SEED, "种子列表必须从官方种子起，才能和定稿口径对上"
        assert payload["meta"]["n_kox_per_seed"] == 300
        assert "[multiseed]" in run.stdout

    def test_ignores_a_poisoned_thresholds_file(self, sandbox: Sandbox) -> None:
        sandbox.run("multiseed", "--seeds", "2", "--n", "300")
        clean = sandbox.path("output/multiseed.json").read_bytes()

        poison = {"groups": {}, "platform": {}, "global": {}, "meta": {"poisoned": True}, "policy": {}}
        sandbox.path("output/thresholds.json").write_text(json.dumps(poison), "utf-8")
        sandbox.run("multiseed", "--seeds", "2", "--n", "300")
        assert sandbox.path("output/multiseed.json").read_bytes() == clean, (
            "投毒 thresholds.json 改变了 multiseed 结果 —— 说明它复用了定稿阈值"
        )
        assert json.loads(sandbox.path("output/thresholds.json").read_text("utf-8")) == poison, (
            "multiseed 不该改写别人的产物"
        )

    def test_summary_survives_a_clean_sweep_by_koxpilot(self, sandbox: Sandbox) -> None:
        """回归测试：一个观测都没输给基线时，摘要不许崩。

        原实现照着"总会有失败观测"写摘要：
        `mean_koxpilot_waste_share_when_losing` 在无失败观测时是 `None`，
        直接拿去 `:.1%` 格式化 → `TypeError`。
        也就是说**结果最好的那种情况会让 CLI 崩掉**（我在 `--seeds 2 --n 300` 上真踩到了）。

        构造方式刻意用"真实 payload + 定点改写"，而不是手搓一份假 payload：
        手搓的那份会在 multiseed 产物结构变动时静默失效，成为一条永远绿的死测试。
        """
        sandbox.run("multiseed", "--seeds", "2", "--n", "300")
        payload = sandbox.json("output/multiseed.json")
        assert format_summary(payload), "原样 payload 都格式化不出来的话，下面的对照没有意义"

        cw = payload["B_variance_attribution"]["conditional_win_rate_by_baseline_luck"]
        cw["n_koxpilot_loses"] = 0
        cw["mean_koxpilot_waste_share_when_losing"] = None
        cw["max_baseline_waste_share_among_koxpilot_losses"] = 0.0
        for row in cw["bands"]:
            row["n_koxpilot_loses"] = 0
        # 这一行就是当年崩掉的那个格式化动作，留在测试里当"为什么需要分支"的证据
        with pytest.raises(TypeError):
            f"{cw['mean_koxpilot_waste_share_when_losing']:.1%}"

        text = format_summary(payload)
        assert "没输给基线" in text
        assert "None" not in text, "摘要里不许把 None 直接印给读者"

    def test_summary_explains_why_a_number_is_missing(self, sandbox: Sandbox) -> None:
        """`std 比` 与 F 检验 p 在小样本下会是 `None`（KOXPilot 一侧方差恰好为 0）。

        原实现直接把它插进 f-string，于是日志里出现 `std 比 = None×，F 检验 p=None`。
        `None×` 会被读成 bug，也可能被读成 0（"方差没差别"）——两种误读都会改变结论方向。
        没有数就要写清"为什么没有"。
        """
        run = sandbox.run("multiseed", "--seeds", "2", "--n", "300")
        payload = sandbox.json("output/multiseed.json")
        pooled = payload["B_variance_attribution"]["pooled_all_campaigns"]
        undefined = pooled["std_ratio_baseline_over_koxpilot"] is None or any(
            row["std_ratio_baseline_over_koxpilot"] is None
            for row in payload["B_variance_attribution"]["per_campaign"].values()
        )
        assert undefined, (
            "这个规模下本该出现「比值无定义」的格子；如果不再出现，这条测试就成了空转，"
            "需要换一个能触发它的参数"
        )
        assert "None" not in run.stdout
        assert "不可算" in run.stdout
        # 产物里保留 null（机器可判别），但人读的摘要必须解释它 —— 两者分工不能混
        assert "null" in sandbox.path("output/multiseed.json").read_text("utf-8")


# ---------------------------------------------------------------------------
# 9. explain 子命令：现场逐条复算的入口
# ---------------------------------------------------------------------------
class TestExplainCommand:
    def test_prints_numbers_that_match_a_fresh_evaluate(self, ready_sandbox: Sandbox) -> None:
        box = ready_sandbox
        box.run("gate")
        thresholds = Thresholds.from_dict(box.json("output/thresholds.json"))
        records = box.json("data/kox_5000.json")["kox"]
        # 挑一个真有证据链的达人，否则这条测试只验证了"无命中"这一种排版
        target = next(
            k
            for k in records
            if evaluate({kk: vv for kk, vv in k.items() if kk != GROUND_TRUTH_FIELD},
                        CampaignSpec(), thresholds).reasons
        )
        res = evaluate(
            {k: v for k, v in target.items() if k != GROUND_TRUTH_FIELD},
            CampaignSpec(),
            thresholds,
        )
        out = box.run("explain", str(target["kox_id"])).stdout

        assert f"判定：{res.verdict}（campaign=neutral）" in out
        assert f"完整度={res.completeness:.3f}" in out
        assert f"真实性={res.authenticity_score:.3f}" in out
        assert f"一致性={res.consistency_score:.3f}" in out
        assert f"品牌安全={res.brand_safety_score:.3f}" in out
        assert f"异常分={res.fraud_score:.3f}" in out
        assert f"适配={res.fit_score:.3f}({res.fit_source})" in out
        assert f"组={res.group_key}" in out
        for reason in res.reasons:
            assert f"[{reason.rule_id}]" in out
            assert reason.human_text in out
            assert f"actual={reason.actual}" in out
            assert f"threshold={reason.threshold}" in out

    def test_handle_is_printed_exactly_once(self, ready_sandbox: Sandbox) -> None:
        """datagen 生成的 handle 自带 `@`，CLI 再补一个就会印成 `@@xxx`（原实现如此）。"""
        box = ready_sandbox
        box.run("gate")
        kox = box.json("data/kox_5000.json")["kox"][0]
        out = box.run("explain", str(kox["kox_id"])).stdout
        assert str(kox["handle"]) in out
        assert "@@" not in out
        assert out.count("@") == str(kox["handle"]).count("@")

    def test_campaign_scope_is_taken_from_the_brief(self, ready_sandbox: Sandbox) -> None:
        box = ready_sandbox
        box.run("gate")
        brief = box.json("data/briefs.json")["briefs"][1]
        spec = CampaignSpec.from_dict(brief["spec"])
        thresholds = Thresholds.from_dict(box.json("output/thresholds.json"))
        records = box.json("data/kox_5000.json")["kox"]
        # 找一个"中性口径与 campaign 口径判定不同"的达人：只有这种样本能证明 --campaign 真的生效
        target = None
        for kox in records:
            observable = {k: v for k, v in kox.items() if k != GROUND_TRUTH_FIELD}
            if (
                evaluate(observable, CampaignSpec(), thresholds).verdict
                != evaluate(observable, spec, thresholds).verdict
            ):
                target = kox
                break
        assert target is not None, "样本里没有 campaign 口径与中性口径不同的达人，这条测试会空转"

        neutral = box.run("explain", str(target["kox_id"])).stdout
        scoped = box.run("explain", str(target["kox_id"]), "--campaign", spec.campaign_id).stdout
        assert f"campaign={spec.campaign_id}" in scoped
        assert "campaign=neutral" in neutral
        expected = evaluate(
            {k: v for k, v in target.items() if k != GROUND_TRUTH_FIELD}, spec, thresholds
        )
        assert f"判定：{expected.verdict}" in scoped
        assert scoped != neutral

    def test_unknown_ids_and_campaigns_exit_nonzero(self, ready_sandbox: Sandbox) -> None:
        box = ready_sandbox
        missing = box.run("explain", "KOX-999999", expect=1)
        assert "找不到 KOX-999999" in missing.stderr
        assert missing.stdout == ""
        kox_id = str(box.json("data/kox_5000.json")["kox"][0]["kox_id"])
        bad_campaign = box.run("explain", kox_id, "--campaign", "NOPE", expect=1)
        assert "找不到 campaign NOPE" in bad_campaign.stderr

    def test_explain_is_read_only(self, ready_sandbox: Sandbox) -> None:
        box = ready_sandbox
        box.run("gate")
        before = {rel: box.path(rel).read_bytes() for rel in box.files()}
        kox_id = str(box.json("data/kox_5000.json")["kox"][0]["kox_id"])
        box.run("explain", kox_id)
        box.run("explain", kox_id, "--show-gt")
        after = {rel: box.path(rel).read_bytes() for rel in box.files()}
        assert before == after, "explain 是只读命令，不该动任何文件"

    def test_explain_calibrates_on_the_fly_without_thresholds_file(
        self, ready_sandbox: Sandbox
    ) -> None:
        # 没有 thresholds.json 时也要能解释（现场排查不该被"先跑 gate"卡住），
        # 而且此时结论必须与内存标定一致。
        box = ready_sandbox
        assert not box.path("output/thresholds.json").exists()
        records = box.json("data/kox_5000.json")["kox"]
        meta = box.json("data/kox_5000.json")["meta"]
        thresholds = calibrate(strip_gt(records), meta)
        kox = records[3]
        expected = evaluate(
            {k: v for k, v in kox.items() if k != GROUND_TRUTH_FIELD}, CampaignSpec(), thresholds
        )
        out = box.run("explain", str(kox["kox_id"])).stdout
        assert f"判定：{expected.verdict}" in out
        assert box.files() == [
            "data/briefs.json",
            "data/kox_5000.json",
            f"data/kox_sample_{SAMPLE_SIZE}.json",
        ]


# ---------------------------------------------------------------------------
# 10. CLI 契约（argparse 层）与 Makefile 对账
# ---------------------------------------------------------------------------
class TestCliContract:
    SUBCOMMANDS = ("data", "gate", "budget", "eval", "multiseed", "explain", "all")

    def test_every_documented_subcommand_exists(self) -> None:
        """子命令表直接从 `--help` 的输出里读，而不是抄一份常量对着自己比。"""
        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main(["--help"])
        assert exc.value.code == 0
        listed = re.search(r"\{([a-z,]+)\}", out.getvalue())
        assert listed, f"--help 里找不到子命令列表：\n{out.getvalue()}"
        assert set(listed.group(1).split(",")) == set(self.SUBCOMMANDS)

    def test_no_subcommand_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main([])
        assert exc.value.code == 2

    @pytest.mark.parametrize("argv", [["nope"], ["data", "--nope"], ["eval", "extra"]])
    def test_bad_invocations_are_usage_errors(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2

    def test_help_exits_zero(self) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["--help"])
        assert exc.value.code == 0

    @pytest.mark.parametrize("sub", SUBCOMMANDS)
    def test_subcommand_help_exits_zero(self, sub: str) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main([sub, "--help"])
        assert exc.value.code == 0

    def test_module_entry_point_is_wired(self) -> None:
        """`python -m koxpilot.cli` 这条真实入口必须存在且 `--help` 就能自证。

        只跑 `--help`：argparse 在做任何事之前就退出，因此**不会**在真实仓库里落盘
        （本文件的 autouse 哨兵会替这句话背书）。
        """
        import subprocess
        import sys

        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
        proc = subprocess.run(
            [sys.executable, "-m", "koxpilot.cli", "--help"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        for sub in self.SUBCOMMANDS:
            assert sub in proc.stdout
        src = (REPO_ROOT / "src" / "koxpilot" / "cli.py").read_text("utf-8")
        assert 'if __name__ == "__main__":' in src and "raise SystemExit(main())" in src

    def test_main_returns_int_exit_codes(self, ready_sandbox: Sandbox) -> None:
        # `main` 的返回值会被 `SystemExit` 吃掉，所以它必须是 int 而不是 None/True。
        code = cli.main(["gate"])
        assert isinstance(code, int) and code == 0


class TestMakefileMatchesCli:
    """Makefile 是外部读者的第一入口，它的每一句都要与 CLI 对得上。"""

    def test_every_make_target_calls_a_real_subcommand(self) -> None:
        text = MAKEFILE.read_text("utf-8")
        used = set(re.findall(r"-m koxpilot\.cli (\w+)", text))
        assert used == set(TestCliContract.SUBCOMMANDS) - {"explain"}, (
            "Makefile 调了不存在的子命令，或漏掉了某个确定性步骤"
        )
        assert "-m koxpilot.cli all" in text

    def test_llm_targets_are_the_only_ones_spending_tokens(self) -> None:
        text = MAKEFILE.read_text("utf-8")
        token_targets = {
            block.split(":", 1)[0]
            for block in re.split(r"\n(?=\w[\w-]*:)", text)
            if "-m koxpilot.llm" in block
        }
        assert token_targets == {"llm", "promptbench"}
        # 帮助文本必须把"需要 key"和"不需要 key"分开写清楚
        assert "需要 LLM API key 的部分" in text and "不需要任何 API key 的部分" in text

    def test_clean_does_not_throw_away_paid_artifacts(self) -> None:
        """`make clean` 不许删掉真花过 token 的 LLM 产物。

        原实现是 `rm -rf output/*.json`，会连 `llm_cache.json` / `llm_bench.json` /
        `prompt_bench.json` 一起删——那三个文件删了就只能再花钱重跑，
        而且删完 `make eval` 的成本账会静静退化成 `llm_not_run`。
        """
        recipe = make_recipe("clean")
        assert recipe, "Makefile 里找不到 clean 目标的配方"
        assert "output/*.json" not in recipe, "通配删除会波及 LLM 产物"
        for paid in ("llm_cache.json", "llm_bench.json", "prompt_bench.json"):
            assert paid not in recipe, f"make clean 删掉了 {paid}"
        assert make_recipe("clean-llm"), (
            "破坏性清理要有一个显式命名的目标，而不是藏在 clean 里"
        )
        assert ".PHONY" in MAKEFILE.read_text("utf-8")
        assert "clean-llm" in re.search(r"\.PHONY:.*", MAKEFILE.read_text("utf-8")).group(0)
        # clean 该删的那些必须真的在列表里（否则"改成白名单"变成了不清理）
        for regenerable in ("metrics.json", "verdicts.json", "thresholds.json", "budget.json"):
            assert regenerable in recipe

    def test_help_text_promises_match_reality(self) -> None:
        text = MAKEFILE.read_text("utf-8")
        # help 里写的 5,000 / 3 个 brief / 25 组阈值，必须和产物里的真实数字一致
        assert f"({N_KOX:,})" in text or str(N_KOX) in text
        thresholds = json.loads((REAL_OUTPUT / "thresholds.json").read_text("utf-8"))
        n_groups = len(thresholds["groups"])
        assert f"make gate        标定 {n_groups} 组" in text, (
            f"help 里写的分组数与 thresholds.json 的 {n_groups} 组不一致"
        )
        briefs = json.loads((REAL_DATA / "briefs.json").read_text("utf-8"))["briefs"]
        assert f"{len(briefs)} 个 campaign brief" in text
        assert f"{len(briefs)} 个 brief" in text
