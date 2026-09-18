"""路径解析：同一份代码在「本地开发」和「线上部署」两种布局下都能找到真产物。

两种布局
--------
部署（``deploy_backend`` 只上传 ``api/``）::

    api/_bundle/src/koxpilot/...      <- 由 build_bundle.py 生成
    api/_bundle/data/kox_5000.json
    api/_bundle/output/thresholds.json

本地开发（直接读仓库里的真产物，改了源码立刻生效，不用重跑打包）::

    <repo>/src/koxpilot/...
    <repo>/data/kox_5000.json
    <repo>/output/thresholds.json

解析顺序刻意是「bundle 优先」：部署环境里 ``../data`` 根本不存在，而本地开发时
bundle 可能是旧的。但本地开发者如果**已经**跑过 `make api-bundle`，读 bundle 也是对的
（它和源码逐字节等价，见 verify_bundle.py），所以这个优先级不会带来口径分歧。

本模块不 import ``koxpilot``——它的职责恰恰是「让 koxpilot 能被 import」。
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Tuple

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE_DIR = os.path.join(API_DIR, "_bundle")
REPO_ROOT = os.path.dirname(API_DIR)

#: 数据根的候选顺序：(根目录, 这个根的人话名字)
_ROOT_CANDIDATES: Tuple[Tuple[str, str], ...] = (
    (BUNDLE_DIR, "部署包 api/_bundle"),
    (REPO_ROOT, "仓库根"),
)

#: 服务必需的数据产物（相对数据根）
REQUIRED_FILES: Tuple[str, ...] = (
    "data/kox_5000.json",
    "data/briefs.json",
    "output/thresholds.json",
)

#: 可选产物：缺了服务照常跑，只是相关信息如实报「未提供」
OPTIONAL_FILES: Tuple[str, ...] = ("output/llm_cache.json",)


def _has_all_required(root: str) -> bool:
    return all(os.path.isfile(os.path.join(root, rel)) for rel in REQUIRED_FILES)


def resolve_data_root() -> Tuple[Optional[str], str]:
    """返回 ``(数据根绝对路径, 人话来源说明)``；一个都找不到时返回 ``(None, 说明)``。"""
    tried: List[str] = []
    for root, label in _ROOT_CANDIDATES:
        if _has_all_required(root):
            return root, label
        tried.append(label)
    return None, "均未找到完整数据产物（依次找过：%s）" % "、".join(tried)


def resolve_package_path() -> Tuple[Optional[str], str]:
    """返回可放进 ``sys.path`` 的 ``src`` 目录（其中含 ``koxpilot/`` 包）。"""
    for root, label in _ROOT_CANDIDATES:
        candidate = os.path.join(root, "src")
        if os.path.isdir(os.path.join(candidate, "koxpilot")):
            return candidate, label
    return None, "未找到 koxpilot 包（既不在 _bundle/src 也不在 <repo>/src）"


def ensure_importable() -> str:
    """把 koxpilot 所在的 ``src`` 目录插到 ``sys.path`` 最前面，返回来源说明。

    必须在任何 ``import koxpilot`` **之前**调用。
    """
    path, label = resolve_package_path()
    if path is None:
        raise RuntimeError(label)
    if path not in sys.path:
        sys.path.insert(0, path)
    return label


def data_file(rel: str) -> Optional[str]:
    """按候选顺序解析单个数据文件；找不到返回 None（由调用方决定怎么如实上报）。"""
    for root, _label in _ROOT_CANDIDATES:
        candidate = os.path.join(root, rel)
        if os.path.isfile(candidate):
            return candidate
    return None
