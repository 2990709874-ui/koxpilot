# ⚠️ 本文件由 api/build_bundle.py 自动生成，请勿手改。
# 真实源码在仓库的 src/koxpilot/llm/__init__.py；要改就改那边，然后重跑 `make api-bundle`。
# 本副本相对源码只做了「Python 3.8 运行期兼容」的机械改写（见 build_bundle.py 抬头）。
"""KOXPilot 模型层：provider-agnostic 的 LLM 适配与构建期批量推理。"""

from .provider import (
    LLMError,
    LLMProvider,
    LLMResponse,
    Usage,
    available_providers,
    build_provider,
)

__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "Usage",
    "available_providers",
    "build_provider",
]
