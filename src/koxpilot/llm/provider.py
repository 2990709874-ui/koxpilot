"""Provider-agnostic LLM 适配层。

设计意图
--------
KOXPilot 的语义类判定（brief 解析、标签错配判定、语义适配打分）需要大模型，
但作品要交付给外部评审，因此有两条硬约束：

1. **凭据零泄漏**：任何 API key / 私有 endpoint 都只能来自环境变量，
   不得出现在源码、配置文件或文档中。仓库只提供 ``.env.example`` 占位。
2. **运行期零依赖**：线上 Demo 面向公网访问者，不能依赖任何需要鉴权的 endpoint。
   因此本模块只在**构建期**运行，产物固化为 ``output/llm_cache.json``，
   前端读固化结果。生产环境中语义类判定同样适合离线批处理 + 缓存
   （成本与延迟约束），这是工程实践而非取巧。

支持三种 provider，全部走 OpenAI Chat Completions 兼容协议：

- ``ark``    : 火山方舟风格 endpoint（``{base}/chat/completions``，Bearer 鉴权）
- ``azure``  : Azure OpenAI 风格网关（endpoint 直接受 POST，Bearer 鉴权）
- ``openai`` : 任意 OpenAI 兼容服务（含 openai.com 本身、vLLM、Ollama 等）

三者的差异被 :class:`LLMProvider` 收敛掉，上层代码只见到 ``chat()``。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "Usage",
    "available_providers",
    "build_provider",
]

ProviderName = Literal["ark", "azure", "openai"]


class LLMError(RuntimeError):
    """模型调用失败。带上 provider 与 HTTP 状态，便于排障。"""

    def __init__(self, provider: str, message: str, status: int | None = None) -> None:
        self.provider = provider
        self.status = status
        super().__init__(f"[{provider}] {message}" + (f" (HTTP {status})" if status else ""))


@dataclass(frozen=True)
class Usage:
    """一次调用的真实 token 用量。

    成本审计（SPEC 6.1）里的所有 token 数字都来自这里，
    取自 API 返回的 ``usage`` 字段，**不是估算**。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> Usage:
        if not payload:
            return cls()
        ctd = payload.get("completion_tokens_details") or {}
        ptd = payload.get("prompt_tokens_details") or {}
        return cls(
            prompt_tokens=int(payload.get("prompt_tokens") or 0),
            completion_tokens=int(payload.get("completion_tokens") or 0),
            reasoning_tokens=int(ctd.get("reasoning_tokens") or 0),
            cached_tokens=int(ptd.get("cached_tokens") or 0),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class LLMResponse:
    text: str
    usage: Usage
    #: 展示用模型名：服务端回报的优先，没有则退回我们请求时用的 id。
    model: str
    provider: str
    latency_ms: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    #: **仅**服务端在响应里明确回报的模型名；响应没带 ``model`` 字段时为空串。
    #: 与 ``model`` 分开存是因为二者含义不同：ARK 用 endpoint id 发请求
    #: （``ep-...``），服务端回报的却是真实型号（``doubao-...``）。
    #: 合成一个字段就会出现"models 里写 endpoint、per_task 里写型号"那种
    #: 同名不同义的产物，读的人只能靠猜。
    served_model: str = ""

    def json_payload(self) -> Any:
        """把模型输出解析成 JSON。

        模型经常把 JSON 包在 ```json 围栏里，或在前后加解释性文字。
        这里做三级降级：直接 parse → 剥围栏 → 截取首个平衡的 {...} / [...]。
        解析不出来就抛错，绝不静默返回默认值——静默默认值会让评测结果失真。
        """
        return _loads_loose(self.text)


def _loads_loose(text: str) -> Any:
    s = (text or "").strip()
    if not s:
        raise LLMError("parse", "模型返回空文本")
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    if "```" in s:
        chunks = s.split("```")
        for chunk in chunks[1:]:
            body = chunk
            if "\n" in body:
                head, rest = body.split("\n", 1)
                if head.strip().lower() in {"json", "json5", ""}:
                    body = rest
            body = body.strip()
            if not body:
                continue
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                continue

    # 按「谁先出现」决定先试对象还是数组。
    # 固定先试 "{" 是有害的：模型给数组加前言时（"好的，结果如下：[{...},{...}]"），
    # 先扫 "{" 只会截到**数组里的第一个元素**，整批判定被悄悄砍成一条。
    # 下游 _parse_array 拿到 dict 会抛错（不会静默算错），但这一批的 token 就白花了。
    openers = sorted(
        (("{", "}"), ("[", "]")),
        key=lambda pair: (s.find(pair[0]) if s.find(pair[0]) >= 0 else len(s) + 1),
    )
    for opener, closer in openers:
        start = s.find(opener)
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise LLMError("parse", f"无法从模型输出中解析 JSON：{s[:200]!r}")


class LLMProvider:
    """一个 OpenAI 兼容的 chat 客户端。

    只用标准库 ``urllib``，不引入 openai/httpx 依赖——
    这样 clone 仓库的人不需要为了看懂调用链去读第三方库源码。
    """

    def __init__(
        self,
        *,
        name: str,
        url: str,
        model: str,
        auth_header: str = "Authorization",
        auth_prefix: str = "Bearer ",
        api_key: str = "",
        extra_body: dict[str, Any] | None = None,
        timeout: int = 180,
        max_retries: int = 4,
    ) -> None:
        self.name = name
        self.url = url
        self.model = model
        self._auth_header = auth_header
        self._auth_prefix = auth_prefix
        self._api_key = api_key
        self._extra_body = extra_body or {}
        self.timeout = timeout
        self.max_retries = max_retries

    # -- 公开接口 ---------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        body.update(self._extra_body)
        # 推理型模型（Seed 2.0 thinking / GPT-5.5 reasoning）不接受 temperature，
        # 传了会直接 400，因此仅在显式指定时才带上。
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers[self._auth_header] = f"{self._auth_prefix}{self._api_key}"

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            started = time.time()
            try:
                req = urllib.request.Request(
                    self.url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                latency_ms = int((time.time() - started) * 1000)
                return self._parse(payload, latency_ms)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:300]
                except Exception:  # pragma: no cover - 读 body 失败无关紧要
                    pass
                last_err = LLMError(self.name, f"{exc.reason} {detail}", status=exc.code)
                # 4xx（除 429）是请求本身的问题，重试没意义
                if exc.code < 500 and exc.code != 429:
                    raise last_err from exc
            except LLMError:
                # 响应结构异常（choices 缺失 / message 不是对象）是**确定性**错误：
                # 同一个请求再发 4 遍只会拿到同一个坏响应，纯烧额度还把真正的
                # 错误信息埋进"重试 N 次后仍失败"里。所以立刻上抛。
                raise
            except Exception as exc:  # 网络抖动 / 超时 / JSON 破损
                last_err = exc
            sleep_s = min(2 ** attempt, 16)
            time.sleep(sleep_s)
        raise LLMError(self.name, f"重试 {self.max_retries} 次后仍失败：{last_err}")

    # -- 内部 -------------------------------------------------------------
    def _parse(self, payload: dict[str, Any], latency_ms: int) -> LLMResponse:
        try:
            choice = payload["choices"][0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            if not text and msg.get("reasoning_content"):
                # 极少数情况下模型只吐了 reasoning，没有 content
                text = msg["reasoning_content"]
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            # AttributeError 也要收：choices[0] 是 null、message 是字符串这类
            # "结构对了一半"的响应真实存在。漏掉它会让确定性的结构错误
            # 冒充成网络异常，被重试 4 次。
            raise LLMError(self.name, f"响应结构异常：{json.dumps(payload)[:300]}") from exc
        served = str(payload.get("model") or "")
        return LLMResponse(
            text=text,
            usage=Usage.from_payload(payload.get("usage")),
            model=served or self.model,
            provider=self.name,
            latency_ms=latency_ms,
            raw=payload,
            served_model=served,
        )


# -- 工厂 -----------------------------------------------------------------
def _clean(value: str | None) -> str:
    """清洗环境变量。

    从聊天窗口复制粘贴的 endpoint 经常尾随零宽字符（U+200B/U+200C/U+FEFF 等），
    肉眼不可见但会让 URL 直接 404。这里主动剥掉——真踩过这个坑。
    """
    if not value:
        return ""
    out = value.strip().strip('"').strip("'")
    return "".join(ch for ch in out if ch.isprintable() and ch not in "\u200b\u200c\u200d\ufeff")


def build_provider(name: ProviderName | str) -> LLMProvider:
    """按名字从环境变量装配 provider。缺配置就抛错，不静默降级。"""
    key = str(name).lower()

    if key == "ark":
        api_key = _clean(os.getenv("ARK_API_KEY"))
        base = _clean(os.getenv("ARK_BASE_URL")) or "https://ark.cn-beijing.volces.com/api/v3"
        model = _clean(os.getenv("ARK_MODEL"))
        if not api_key or not model:
            raise LLMError("ark", "缺少 ARK_API_KEY 或 ARK_MODEL，请参考 .env.example 配置")
        extra: dict[str, Any] = {}
        if _clean(os.getenv("ARK_THINKING")).lower() in {"enabled", "true", "1"}:
            extra["thinking"] = {"type": "enabled"}
        effort = _clean(os.getenv("ARK_REASONING_EFFORT"))
        if effort:
            extra["reasoning_effort"] = effort
        return LLMProvider(
            name="ark",
            url=f"{base.rstrip('/')}/chat/completions",
            model=model,
            api_key=api_key,
            extra_body=extra,
        )

    if key == "azure":
        api_key = _clean(os.getenv("AZURE_OPENAI_API_KEY"))
        endpoint = _clean(os.getenv("AZURE_OPENAI_ENDPOINT"))
        model = _clean(os.getenv("AZURE_OPENAI_DEPLOYMENT"))
        if not (api_key and endpoint and model):
            raise LLMError(
                "azure",
                "缺少 AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_DEPLOYMENT",
            )
        extra = {}
        effort = _clean(os.getenv("AZURE_OPENAI_REASONING_EFFORT"))
        if effort:
            extra["reasoning_effort"] = effort
        # 网关型 endpoint 直接受 POST；标准 Azure 资源地址需要拼 deployment 路径。
        url = endpoint.rstrip("/")
        if ".openai.azure.com" in url and "/chat/completions" not in url:
            version = _clean(os.getenv("AZURE_OPENAI_API_VERSION")) or "2024-12-01-preview"
            url = f"{url}/openai/deployments/{model}/chat/completions?api-version={version}"
        return LLMProvider(name="azure", url=url, model=model, api_key=api_key, extra_body=extra)

    if key == "openai":
        api_key = _clean(os.getenv("OPENAI_API_KEY"))
        base = _clean(os.getenv("OPENAI_BASE_URL")) or "https://api.openai.com/v1"
        model = _clean(os.getenv("OPENAI_MODEL")) or "gpt-4o-mini"
        if not api_key:
            raise LLMError("openai", "缺少 OPENAI_API_KEY")
        return LLMProvider(
            name="openai",
            url=f"{base.rstrip('/')}/chat/completions",
            model=model,
            api_key=api_key,
        )

    raise LLMError("factory", f"未知 provider：{name}（支持 ark / azure / openai）")


def available_providers() -> list[str]:
    """探测当前环境下哪些 provider 的配置是齐的。"""
    out: list[str] = []
    for name in ("ark", "azure", "openai"):
        try:
            build_provider(name)
        except LLMError:
            continue
        out.append(name)
    return out
