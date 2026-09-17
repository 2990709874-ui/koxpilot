"""pytest 公共 fixture。

设计纪律（这套测试的立场）
--------------------------
1. **不许自证**：测试可以读 ``gt``（测试自己就是裁判台），但被测的门禁/预算代码
   必须在物理上读不到 ``gt``。相关的元测试见 ``test_no_gt_leak.py``。
2. **不许绑常量**：数据集与产物里的具体指标（F1/AUC/准确率/金额）一律用**区间**断言。
   用等号绑住实测值只会让测试变成"记账本"，代码一改就红，且掩盖真实回归。
3. **不许真调模型**：LLM 层全部用假 provider（见 ``test_llm_layer.py``），
   不读环境变量里的 key，不发网络请求。
4. **不许写仓库产物**：测试只读 ``data/`` 与 ``output/``，写盘一律用 ``tmp_path``。
   ``output/`` 下是真实构建产物（还有正在跑的实验在写 llm_cache.json），任何测试都不得改动。

fixture 都是 session 级：全量 5,000 条的生成 + 标定 + 判定加起来不到 1 秒，
但如果每个测试各跑一遍，整套会慢 20 倍。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from koxpilot.datagen.config import N_KOX, SEED
from koxpilot.datagen.generator import generate_dataset
from koxpilot.gates.engine import evaluate_all
from koxpilot.gates.thresholds import Thresholds, calibrate
from koxpilot.types import CampaignSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "koxpilot"
DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = REPO_ROOT / "output"

#: 小样本量：用于只关心"逻辑是否成立"而不关心分布的测试，跑得快。
SMALL_N = 400
SMALL_SEED = 424242


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def dataset() -> dict[str, Any]:
    """当前代码 + 官方种子生成的全量数据集（不落盘）。"""
    return generate_dataset(N_KOX, SEED)


@pytest.fixture(scope="session")
def records(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    return list(dataset["kox"])


@pytest.fixture(scope="session")
def small_records() -> list[dict[str, Any]]:
    """另一个种子的小样本：用来验证结论不是只在官方种子上成立。"""
    return list(generate_dataset(SMALL_N, SMALL_SEED)["kox"])


@pytest.fixture(scope="session")
def thresholds(records: list[dict[str, Any]], dataset: dict[str, Any]) -> Thresholds:
    return calibrate(records, dataset["meta"])


@pytest.fixture(scope="session")
def results(records: list[dict[str, Any]], thresholds: Thresholds) -> dict[str, Any]:
    """库级中性口径（campaign 无关）的门禁结果，与 gt.verdict 同口径。"""
    return {r.kox_id: r for r in evaluate_all(records, thresholds=thresholds)}


@pytest.fixture(scope="session")
def disk_dataset() -> dict[str, Any]:
    """仓库里落盘的 data/kox_5000.json。缺失则跳过依赖它的测试。"""
    path = DATA_DIR / "kox_5000.json"
    if not path.exists():
        pytest.skip(f"缺少 {path}（先跑 `python -m koxpilot.cli data`）")
    return json.loads(path.read_text("utf-8"))


@pytest.fixture(scope="session")
def briefs() -> list[dict[str, Any]]:
    path = DATA_DIR / "briefs.json"
    if not path.exists():
        pytest.skip(f"缺少 {path}")
    payload = json.loads(path.read_text("utf-8"))
    return list(payload["briefs"] if isinstance(payload, dict) else payload)


@pytest.fixture(scope="session")
def specs(briefs: list[dict[str, Any]]) -> list[CampaignSpec]:
    """briefs.json 里的结构化 campaign 画像（门禁/预算的唯一 campaign 输入）。

    注意 ``from_dict`` 吃的是 brief 里的 ``spec`` 子块，不是整条 brief。
    传错一层不会报错，只会静默退化成"中性 spec + 预算 0"，
    于是所有 campaign 相关的断言都会变成空转——所以这里顺手把它锁住。
    """
    out = [CampaignSpec.from_dict(b["spec"]) for b in briefs]
    assert all(s.budget_usd > 0 for s in out), "specs fixture 取错层级，预算全为 0"
    return out


@pytest.fixture(scope="session")
def spec(specs: list[CampaignSpec]) -> CampaignSpec:
    return specs[0]
