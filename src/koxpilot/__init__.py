"""KOXPilot — 出海达人营销投前决策智能体（Python 核心）。

模块地图：
  taxonomy   共享分类学（分层/品类/国家语言），datagen 与 gates 共用以防口径漂移
  stats      手写统计原语（分位数 / 鲁棒 z / AUC / P-R-F1）
  types      跨模块数据契约（CampaignSpec / Reason / GateResult / BudgetPlan）
  datagen    合成 5,000 达人 + ground truth + 3 个 brief（固定种子）
  gates      四层门禁 G0/G1/G2/G3（纯函数，阈值全部来自分位数标定）
  budget     预算分配（value/cost 贪心 + 分层配额修正 + 三类约束）
  eval       评测 harness（六张表 + 消融 + 敏感性 + 反事实价值审计）
"""

from __future__ import annotations

__version__ = "1.0.0"
