# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/datagen/__init__.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
"""datagen：合成达人数据集与 campaign brief 生成器（固定种子、可复现）。"""

from __future__ import annotations

from .briefs import BRIEFS, build_briefs
from .config import INJECTION_RATES, N_KOX, SEED
from .generator import InjectionPlan, build_kox, generate_dataset, plan_injections

__all__ = [
    "BRIEFS",
    "INJECTION_RATES",
    "N_KOX",
    "SEED",
    "InjectionPlan",
    "build_briefs",
    "build_kox",
    "generate_dataset",
    "plan_injections",
]
