"""路径与确定性 JSON 落盘工具。

设计意图
--------
"两次生成逐字节相同"这条硬要求，除了随机种子，还依赖**序列化本身确定性**：
- 固定 ``ensure_ascii=False`` 与 ``separators``；
- 不使用 ``sort_keys``（字段顺序即 SPEC 3.2 的声明顺序，前端按顺序读更直观）；
- 每条达人记录压成一行、记录之间换行 —— 既保证字节确定，又让 git diff 可读。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

__all__ = [
    "data_dir",
    "dump_dataset",
    "dump_json",
    "file_sha256",
    "load_json",
    "output_dir",
    "repo_root",
]

_COMPACT = (",", ":")


def repo_root() -> Path:
    """仓库根目录（src/koxpilot/io_utils.py -> 上溯三层）。"""
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    d = repo_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_dir() -> Path:
    d = repo_root() / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def dump_json(path: Path | str, payload: Any, indent: int | None = 2) -> Path:
    """确定性写 JSON。indent=None 时用紧凑分隔符。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if indent is None:
        text = json.dumps(payload, ensure_ascii=False, separators=_COMPACT)
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=indent)
    p.write_text(text + "\n", encoding="utf-8")
    return p


def dump_dataset(path: Path | str, dataset: dict[str, Any]) -> Path:
    """写达人数据集：meta 缩进可读，kox 每条一行（确定性 + 可 diff）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    meta_text = json.dumps(dataset["meta"], ensure_ascii=False, indent=2)
    lines = [json.dumps(k, ensure_ascii=False, separators=_COMPACT) for k in dataset["kox"]]
    body = ",\n".join(lines)
    p.write_text('{\n"meta": ' + meta_text + ',\n"kox": [\n' + body + "\n]}\n", encoding="utf-8")
    return p


def load_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
