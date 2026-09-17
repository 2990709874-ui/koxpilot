"""datagen：合成达人数据集与 campaign brief 生成器（固定种子、可复现）。"""

from __future__ import annotations

from .briefs import BRIEFS, build_briefs
from .config import INJECTION_RATES, N_KOX, SEED
from .generator import InjectionPlan, build_kox, generate_dataset, plan_injections

__all__ = [
    "BRIEFS",
    "build_briefs",
    "INJECTION_RATES",
    "N_KOX",
    "SEED",
    "InjectionPlan",
    "build_kox",
    "generate_dataset",
    "plan_injections",
]
