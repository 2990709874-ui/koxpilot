"""KOXPilot HTTP 服务层：把真链路包装成接口，**不重写任何业务逻辑**。

模块划分
--------
``paths``        数据与包路径解析（bundle 优先，回退仓库根）
``labels``       代码标识符 -> 中文人话的翻译表（接口对外只说人话）
``briefparse``   A1：LLM 可选 + 规则兜底的 brief 解析
``store``        懒加载的数据仓（冷启动期间对外报 warming）
``service``      plan / explain / gate_batch 三个用例的编排

纪律：本包只做「取数、调 koxpilot、把 dataclass 翻成契约里的 JSON 形状」。
凡是判定、打分、分配的逻辑，一律调 ``koxpilot`` 包里既有的函数——
接口与 CLI 必须是同一套实现，否则两边一定会漂移。
"""

from __future__ import annotations

SERVICE_VERSION = "1.0.0"

__all__ = ["SERVICE_VERSION"]
