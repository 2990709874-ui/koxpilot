"""LLM 层测试：全程假 provider，一次真实网络请求都不许发生。

这一章要证明的四件事
--------------------
1. **不真调模型**：所有 provider 都是假的；本模块用 autouse fixture 把
   ``urllib.request.urlopen`` 和 ``socket.socket.connect`` 双双堵死——
   任何一处偷偷发请求都会立刻炸，而不是"在 CI 上偶发超时"。
2. **不泄漏凭据**：key 只能来自环境变量；provider 的 ``repr``、异常文本、
   缓存文件、账本里都不许出现 key 字面量。
3. **缓存 key 必须诚实**：cache key 由**真正发给模型的完整消息**决定。
   这是本章的核心——如果 key 只 hash 了 kox_id，那么 prompt 改版后
   评测会静默命中旧答案，指标看起来完全正常，实际上是在拿上一版的答案交卷。
   这种作弊没有任何外部症状，只能靠测试钉死。
4. **不许把答案喂给模型**：三个 prompt 构造函数的输出里不得出现 ``gt`` 及其任何
   字段名/取值。模型必须在看不见答案的前提下判定，否则 LLM vs 规则的横评毫无意义。

与 ``test_no_leakage.py`` 的分工：那边管确定性引擎读不到 gt，这边管 prompt 里
不出现 gt。两者合起来才叫"系统没看过答案"。

写盘纪律：本模块所有 ``Cache`` 都指向 ``tmp_path``；模块级 autouse fixture 会在
进出时对 ``output/`` 做指纹比对，任何改动都会让测试失败（``output/`` 是真实构建产物）。
"""

from __future__ import annotations

import copy
import email.message
import io
import json
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import pytest

from koxpilot.llm import prompts as prompts_mod
from koxpilot.llm import promptbench as pb_mod
from koxpilot.llm import runner as runner_mod
from koxpilot.llm.prompt_variants import TAG_VARIANTS, build_messages
from koxpilot.llm.prompts import (
    CAMPAIGN_SPEC_FIELDS,
    KPI_VOCAB,
    adjacency_block,
    brief_parse_messages,
    category_block,
    fit_score_messages,
    tag_judge_messages,
)
from koxpilot.llm.provider import (
    LLMError,
    LLMProvider,
    LLMResponse,
    Usage,
    available_providers,
    build_provider,
)
from koxpilot.llm.provider import _clean, _loads_loose
from koxpilot.llm.runner import (
    Cache,
    CallLog,
    Ledger,
    _batched,
    _parse_array,
    _sha1,
    bench_tag,
    build_pool,
    pmap,
    rule_tag_mismatch,
    run_brief,
    run_fit,
    run_tag,
)
from koxpilot.taxonomy import AGE_BUCKETS, CATEGORY_ZH, PLATFORMS
from koxpilot.types import CampaignSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "output"

#: 所有 provider 相关环境变量。测试前必须全部清空：
#: 否则在配了真 key 的机器上跑，``available_providers()`` 的结果会随机器而变，
#: 更糟的是可能真的把请求打出去。
PROVIDER_ENV = (
    "ARK_API_KEY",
    "ARK_BASE_URL",
    "ARK_MODEL",
    "ARK_THINKING",
    "ARK_REASONING_EFFORT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_REASONING_EFFORT",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
)

#: gt 的全部字段名。任何一个出现在 prompt 里都是把答案递给模型。
GT_FIELDS = (
    "gt",
    "is_fraud",
    "fraud_type",
    "tag_mismatch",
    "brand_safety",
    "verdict",
    "true_categories",
    "mismatch_injected",
)

FRAUD_TYPE_VALUES = ("bought_followers", "engagement_pod", "bot_comments", "view_inflation")


# ---------------------------------------------------------------------------
# 网络与环境隔离（autouse，覆盖本模块每一个测试）
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """把出网的两条路都堵死。

    只 patch ``urlopen`` 不够：将来若有人换 requests/httpx，urlopen 的哨兵就失效了。
    因此连 ``socket.socket.connect`` 一起堵——本模块任何测试都不该建立连接。
    """

    def boom_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("测试期禁止真实 HTTP 调用：请用 fake provider")

    def boom_connect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("测试期禁止建立网络连接")

    monkeypatch.setattr(urllib.request, "urlopen", boom_urlopen)
    monkeypatch.setattr(socket.socket, "connect", boom_connect)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空全部 provider 环境变量，让 provider 相关断言与本机配置无关。"""
    for key in PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="module", autouse=True)
def _output_untouched() -> Any:
    """模块进出时对 ``output/`` 做指纹比对：测试绝不允许改动真实构建产物。"""

    def fingerprint() -> dict[str, tuple[int, int]]:
        if not OUTPUT_DIR.exists():
            return {}
        return {
            str(p.relative_to(OUTPUT_DIR)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(OUTPUT_DIR.rglob("*"))
            if p.is_file()
        }

    before = fingerprint()
    yield
    after = fingerprint()
    # 注意：不比较 mtime 之外的内容 hash，因为 output/ 可能有几十 MB；
    # size + mtime_ns 已足够抓住"测试把产物写脏了"。
    assert before == after, "测试改动了 output/ 下的构建产物"


# ---------------------------------------------------------------------------
# 假 HTTP / 假 provider
# ---------------------------------------------------------------------------
class FakeHTTPResponse:
    """最小可用的 urlopen 返回值：支持 with 语句和 read()。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class UrlopenRecorder:
    """可编程的假 urlopen：记录每次请求，按脚本返回响应或抛异常。

    ``script`` 里每一项要么是 dict（当作 JSON 响应体），要么是 Exception 实例（抛出）。
    脚本用尽后重复最后一项——方便写"一直 500"的场景。
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    @property
    def n_calls(self) -> int:
        return len(self.requests)

    def __call__(self, req: Any, timeout: Any = None) -> FakeHTTPResponse:
        self.requests.append(
            {
                "url": req.full_url,
                "method": req.method,
                "headers": {k.lower(): v for k, v in req.headers.items()},
                "body": json.loads(req.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        item = self.script[0] if len(self.script) == 1 else self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeHTTPResponse(json.dumps(item, ensure_ascii=False).encode("utf-8"))


def http_error(code: int, body: str = '{"error":"boom"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://fake/chat/completions",
        code,
        f"status {code}",
        email.message.Message(),
        io.BytesIO(body.encode("utf-8")),
    )


def chat_payload(text: str, **usage: int) -> dict[str, Any]:
    """构造一个 OpenAI 兼容的响应体。"""
    u = {"prompt_tokens": 11, "completion_tokens": 7, **usage}
    return {
        "id": "chatcmpl-fake",
        "model": "fake-model-1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        "usage": u,
    }


class FakeProvider:
    """假 provider：接口与 ``LLMProvider.chat`` 一致，但不碰网络。

    ``responder(messages) -> str | list | dict | Exception``：
    返回 str 当原样文本，返回结构则序列化成 JSON；返回/抛出异常用于模拟失败。
    """

    def __init__(
        self,
        responder: Callable[[list[dict[str, str]]], Any],
        *,
        name: str = "fake",
        model: str = "fake-model-1",
        usage: Usage | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._responder = responder
        self._usage = usage or Usage(prompt_tokens=100, completion_tokens=20, reasoning_tokens=5, cached_tokens=1)
        self.calls: list[list[dict[str, str]]] = []

    @property
    def n_calls(self) -> int:
        return len(self.calls)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        self.calls.append(copy.deepcopy(messages))
        out = self._responder(messages)
        if isinstance(out, BaseException):
            raise out
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return LLMResponse(
            text=text,
            usage=self._usage,
            model=self.model,
            provider=self.name,
            latency_ms=42,
            raw={"fake": True},
        )


def user_payload(messages: list[dict[str, str]]) -> Any:
    """取 user message 尾部那个 JSON 数组（假 provider 用来"读题"）。

    注意不能图省事用 ``_loads_loose``：fit 任务的 user message 里**先**是 CampaignSpec
    对象、**后**才是候选数组，松散解析会拿到前者。这里按提示词里的固定锚点定位，
    锚点没了就直接 KeyError——比静默拿到半份数据强。
    """
    content = messages[-1]["content"]
    marker = "JSON 数组：\n"
    idx = content.rindex(marker) + len(marker)
    return json.loads(content[idx:])


def echo_tag_responder(mismatch: bool = False, severity: str = "none", confidence: float = 0.9) -> Callable[[list[dict[str, str]]], Any]:
    """按输入 kox_id 原样回填的标签判定 responder。"""

    def responder(messages: list[dict[str, str]]) -> Any:
        return [
            {
                "kox_id": item["kox_id"],
                "mismatch": mismatch,
                "confidence": confidence,
                "severity": severity,
                "evidence": "假模型",
            }
            for item in user_payload(messages)
        ]

    return responder


def fit_responder(score: float = 0.7) -> Callable[[list[dict[str, str]]], Any]:
    def responder(messages: list[dict[str, str]]) -> Any:
        return [
            {
                "kox_id": item["kox_id"],
                "fit_score": score,
                "audience_fit": score,
                "content_fit": score,
                "angle": "假模型",
            }
            for item in user_payload(messages)
        ]

    return responder


def serialize(messages: list[dict[str, str]]) -> str:
    """把消息拼成"模型真正看到的那串字符"。

    这里刻意**不用** ``json.dumps``：那样会把 content 里的引号转义成 ``\\"``，
    于是 ``'"gt"' not in blob`` 这类断言永远为真——一台专门生产假绿灯的机器。
    这个坑是本模块自己踩出来的（阳性对照测试当场红掉才发现）。
    """
    return "\n".join(m["content"] for m in messages)


# ===========================================================================
# 1. 凭据与工厂：只从环境变量取，缺配置就报错，绝不静默降级
# ===========================================================================
class TestCleanEnvValue:
    def test_strips_whitespace_and_quotes(self) -> None:
        assert _clean('  "https://x/v1" ') == "https://x/v1"
        assert _clean("'sk-abc'") == "sk-abc"

    def test_strips_invisible_characters(self) -> None:
        """零宽字符是从聊天窗口粘贴 endpoint 时的经典坑：肉眼看不见，URL 直接 404。"""
        dirty = "https://x/v1\u200b\u200c\u200d\ufeff"
        assert _clean(dirty) == "https://x/v1"

    def test_none_and_empty(self) -> None:
        assert _clean(None) == ""
        assert _clean("") == ""


class TestBuildProvider:
    def test_openai_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-unit-test")
        p = build_provider("openai")
        assert p.name == "openai"
        assert p.url == "https://api.openai.com/v1/chat/completions"
        assert p.model == "gpt-4o-mini"

    def test_openai_base_url_trailing_slash_does_not_double(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-unit-test")
        monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1/")
        assert build_provider("openai").url == "http://127.0.0.1:11434/v1/chat/completions"

    def test_ark_optional_thinking_and_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARK_API_KEY", "ark-unit-test")
        monkeypatch.setenv("ARK_MODEL", "ep-2026")
        monkeypatch.setenv("ARK_THINKING", "enabled")
        monkeypatch.setenv("ARK_REASONING_EFFORT", "medium")
        p = build_provider("ark")
        assert p.url.endswith("/api/v3/chat/completions")
        assert p._extra_body == {"thinking": {"type": "enabled"}, "reasoning_effort": "medium"}

    def test_ark_thinking_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARK_API_KEY", "ark-unit-test")
        monkeypatch.setenv("ARK_MODEL", "ep-2026")
        assert build_provider("ark")._extra_body == {}

    def test_azure_gateway_endpoint_is_used_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """网关型 endpoint 直接受 POST，不许自作聪明拼 deployment 路径。"""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-unit-test")
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://gw.internal/llm/chat/completions/")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5")
        assert build_provider("azure").url == "https://gw.internal/llm/chat/completions"

    def test_azure_resource_endpoint_gets_deployment_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-unit-test")
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://demo.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5")
        url = build_provider("azure").url
        assert url == (
            "https://demo.openai.azure.com/openai/deployments/gpt-5/chat/completions"
            "?api-version=2024-12-01-preview"
        )

    def test_azure_api_version_is_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-unit-test")
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://demo.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5")
        monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2099-01-01")
        assert build_provider("azure").url.endswith("api-version=2099-01-01")

    @pytest.mark.parametrize("name", ["ark", "azure", "openai"])
    def test_missing_config_raises_instead_of_degrading(self, name: str) -> None:
        """缺 key 必须抛错。静默返回一个"能用但没鉴权"的 provider 会在构建期才炸。"""
        with pytest.raises(LLMError) as ei:
            build_provider(name)
        assert name in str(ei.value)

    def test_error_message_names_the_env_vars(self) -> None:
        """报错要能自解释：直接说出该配哪个环境变量，而不是 KeyError。"""
        with pytest.raises(LLMError, match="ARK_API_KEY"):
            build_provider("ark")
        with pytest.raises(LLMError, match="AZURE_OPENAI_ENDPOINT"):
            build_provider("azure")
        with pytest.raises(LLMError, match="OPENAI_API_KEY"):
            build_provider("openai")

    def test_unknown_provider(self) -> None:
        with pytest.raises(LLMError, match="未知 provider"):
            build_provider("gemini")


class TestAvailableProviders:
    def test_empty_env_means_nothing_available(self) -> None:
        assert available_providers() == []

    def test_detects_only_fully_configured_providers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-unit-test")
        monkeypatch.setenv("ARK_API_KEY", "ark-unit-test")  # 缺 ARK_MODEL → 不算可用
        assert available_providers() == ["openai"]

    def test_order_is_stable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """探测顺序决定 primary 模型，必须固定，否则同一份配置会跑出不同的产物。"""
        monkeypatch.setenv("ARK_API_KEY", "k")
        monkeypatch.setenv("ARK_MODEL", "ep")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://gw/x")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "d")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        assert available_providers() == ["ark", "azure", "openai"]


class TestSecretHygiene:
    SECRET = "sk-DO-NOT-LEAK-0123456789"

    def test_key_is_not_in_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", self.SECRET)
        p = build_provider("openai")
        assert self.SECRET not in repr(p)
        assert self.SECRET not in str(p)

    def test_key_is_not_in_http_error_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """4xx 会把响应体拼进异常。要确认拼进去的是服务端文本，不是请求头里的 key。"""
        monkeypatch.setenv("OPENAI_API_KEY", self.SECRET)
        p = build_provider("openai")
        monkeypatch.setattr(urllib.request, "urlopen", UrlopenRecorder([http_error(401, '{"error":"bad key"}')]))
        with pytest.raises(LLMError) as ei:
            p.chat([{"role": "user", "content": "hi"}])
        assert self.SECRET not in str(ei.value)
        assert "bad key" in str(ei.value)

    def test_key_never_reaches_disk_cache_or_ledger(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, briefs: list[dict[str, Any]]
    ) -> None:
        """构建期落盘的 cache/bench 是要进仓库的，key 一旦混进去就是永久泄漏。"""
        monkeypatch.setenv("OPENAI_API_KEY", self.SECRET)
        prov = FakeProvider(lambda m: {"kpi": "reach"}, name="openai", model="gpt-4o-mini")
        cache = Cache(tmp_path / "c.json")
        ledger = Ledger()
        run_brief({"openai": prov}, briefs[:1], cache, ledger)
        blob = json.dumps(cache.data, ensure_ascii=False) + json.dumps(ledger.summarize(), ensure_ascii=False)
        assert self.SECRET not in blob

    def test_repo_source_contains_no_key_literals(self) -> None:
        """元测试：源码里只许出现环境变量名，不许出现 key 字面量。"""
        suspicious = ("sk-proj-", "sk-or-v1-", "AKIA")
        for path in sorted((REPO_ROOT / "src").rglob("*.py")):
            text = path.read_text("utf-8")
            for token in suspicious:
                assert token not in text, f"{path} 疑似硬编码凭据：{token}"

    def test_env_example_ships_placeholders_only(self) -> None:
        env_example = REPO_ROOT / ".env.example"
        assert env_example.exists(), "缺少 .env.example，别人无法复现配置"
        text = env_example.read_text("utf-8")
        for name in ("OPENAI_API_KEY", "ARK_API_KEY", "AZURE_OPENAI_API_KEY"):
            assert name in text
        assert "your-key-here" in text or "your-ark-key" in text
        # .env 必须在 .gitignore 里，否则"填好就提交"是必然发生的事故
        gitignore = (REPO_ROOT / ".gitignore").read_text("utf-8")
        assert any(line.strip() in {".env", "*.env", ".env*"} for line in gitignore.splitlines())


# ===========================================================================
# 2. chat()：请求体、鉴权头、重试语义
# ===========================================================================
@pytest.fixture()
def openai_provider(monkeypatch: pytest.MonkeyPatch) -> LLMProvider:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-unit-test")
    monkeypatch.setenv("OPENAI_MODEL", "fake-model-1")
    return build_provider("openai")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试退避是 1/2/4/8 秒。测试里必须掐掉，否则一条重试用例要跑 15 秒。"""
    monkeypatch.setattr("time.sleep", lambda *_: None)


class TestChatRequest:
    def _run(
        self, provider: LLMProvider, monkeypatch: pytest.MonkeyPatch, script: list[Any], **kw: Any
    ) -> tuple[LLMResponse, UrlopenRecorder]:
        rec = UrlopenRecorder(script)
        monkeypatch.setattr(urllib.request, "urlopen", rec)
        resp = provider.chat([{"role": "user", "content": "hi"}], **kw)
        return resp, rec

    def test_posts_json_with_bearer_auth(
        self, openai_provider: LLMProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, rec = self._run(openai_provider, monkeypatch, [chat_payload("ok")])
        req = rec.requests[0]
        assert req["method"] == "POST"
        assert req["url"] == "https://api.openai.com/v1/chat/completions"
        assert req["headers"]["content-type"] == "application/json"
        assert req["headers"]["authorization"] == "Bearer sk-unit-test"
        assert req["body"]["model"] == "fake-model-1"
        assert req["body"]["messages"] == [{"role": "user", "content": "hi"}]
        assert req["timeout"] == openai_provider.timeout

    def test_temperature_omitted_unless_given(
        self, openai_provider: LLMProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """推理型模型收到 temperature 会直接 400，所以默认必须不带这个字段。"""
        _, rec = self._run(openai_provider, monkeypatch, [chat_payload("ok")])
        assert "temperature" not in rec.requests[0]["body"]

        _, rec2 = self._run(openai_provider, monkeypatch, [chat_payload("ok")], temperature=0.0)
        assert rec2.requests[0]["body"]["temperature"] == 0.0

    def test_max_tokens_and_json_mode(
        self, openai_provider: LLMProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, rec = self._run(
            openai_provider, monkeypatch, [chat_payload("{}")], max_tokens=64, json_mode=True
        )
        body = rec.requests[0]["body"]
        assert body["max_tokens"] == 64
        assert body["response_format"] == {"type": "json_object"}

    def test_extra_body_is_merged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARK_API_KEY", "k")
        monkeypatch.setenv("ARK_MODEL", "ep")
        monkeypatch.setenv("ARK_THINKING", "enabled")
        p = build_provider("ark")
        rec = UrlopenRecorder([chat_payload("ok")])
        monkeypatch.setattr(urllib.request, "urlopen", rec)
        p.chat([{"role": "user", "content": "hi"}])
        assert rec.requests[0]["body"]["thinking"] == {"type": "enabled"}

    def test_no_auth_header_when_key_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """本地 Ollama 之类不需要鉴权：不能硬塞一个 "Bearer " 空头。"""
        p = LLMProvider(name="local", url="http://127.0.0.1:11434/v1/chat/completions", model="q")
        rec = UrlopenRecorder([chat_payload("ok")])
        monkeypatch.setattr(urllib.request, "urlopen", rec)
        p.chat([{"role": "user", "content": "hi"}])
        assert "authorization" not in rec.requests[0]["headers"]


class TestChatRetry:
    def test_5xx_is_retried_then_succeeds(
        self, openai_provider: LLMProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = UrlopenRecorder([http_error(503), http_error(500), chat_payload("ok")])
        monkeypatch.setattr(urllib.request, "urlopen", rec)
        resp = openai_provider.chat([{"role": "user", "content": "hi"}])
        assert resp.text == "ok"
        assert rec.n_calls == 3

    def test_429_is_retried(self, openai_provider: LLMProvider, monkeypatch: pytest.MonkeyPatch) -> None:
        """限流是"等一下就好"，必须重试；否则批量任务会被一次限流打断。"""
        rec = UrlopenRecorder([http_error(429), chat_payload("ok")])
        monkeypatch.setattr(urllib.request, "urlopen", rec)
        assert openai_provider.chat([{"role": "user", "content": "hi"}]).text == "ok"
        assert rec.n_calls == 2

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
    def test_4xx_fails_fast_without_retry(
        self, openai_provider: LLMProvider, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        """请求本身错了，重试 4 次只是把同一个错误发 4 遍——纯浪费时间和额度。"""
        rec = UrlopenRecorder([http_error(code)])
        monkeypatch.setattr(urllib.request, "urlopen", rec)
        with pytest.raises(LLMError) as ei:
            openai_provider.chat([{"role": "user", "content": "hi"}])
        assert rec.n_calls == 1
        assert ei.value.status == code

    def test_network_exception_is_retried(
        self, openai_provider: LLMProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = UrlopenRecorder([TimeoutError("read timeout"), chat_payload("ok")])
        monkeypatch.setattr(urllib.request, "urlopen", rec)
        assert openai_provider.chat([{"role": "user", "content": "hi"}]).text == "ok"
        assert rec.n_calls == 2

    def test_exhausted_retries_raise_with_context(
        self, openai_provider: LLMProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = UrlopenRecorder([http_error(500)])
        monkeypatch.setattr(urllib.request, "urlopen", rec)
        with pytest.raises(LLMError, match="重试"):
            openai_provider.chat([{"role": "user", "content": "hi"}])
        assert rec.n_calls == openai_provider.max_retries


class TestChatParse:
    def _resp(self, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> LLMResponse:
        p = LLMProvider(name="fake", url="http://fake/v1/chat/completions", model="declared-model")
        monkeypatch.setattr(urllib.request, "urlopen", UrlopenRecorder([payload]))
        return p.chat([{"role": "user", "content": "hi"}])

    def test_reads_first_choice_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        r = self._resp(monkeypatch, chat_payload("hello"))
        assert r.text == "hello"
        assert r.provider == "fake"
        assert r.latency_ms >= 0

    def test_falls_back_to_reasoning_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """少数推理模型只吐 reasoning_content。丢掉它等于把这次调用的钱白花。"""
        payload = chat_payload("")
        payload["choices"][0]["message"]["reasoning_content"] = "思考出来的答案"
        assert self._resp(monkeypatch, payload).text == "思考出来的答案"

    def test_model_name_comes_from_response_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """记账要用服务端实际用的模型名，而不是我们请求时写的那个。"""
        payload = chat_payload("ok")
        payload["model"] = "served-model-9"
        assert self._resp(monkeypatch, payload).model == "served-model-9"

    def test_model_falls_back_to_declared(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = chat_payload("ok")
        payload.pop("model")
        assert self._resp(monkeypatch, payload).model == "declared-model"

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"choices": []},
            {"choices": [None]},
            {"choices": [{"message": "不是对象"}]},
            {"choices": "不是数组"},
        ],
    )
    def test_broken_structure_raises(self, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
        with pytest.raises(LLMError, match="响应结构异常"):
            self._resp(monkeypatch, payload)

    def test_structural_error_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """结构性错误必须 fail fast。

        坏响应是确定性的：重发同一个请求只会拿到同一个坏响应，
        白烧 4 次额度，还把真正的错误埋进"重试 4 次后仍失败"里。
        """
        p = LLMProvider(name="fake", url="http://fake/v1/chat/completions", model="m")
        rec = UrlopenRecorder([{"choices": [None]}])
        monkeypatch.setattr(urllib.request, "urlopen", rec)
        with pytest.raises(LLMError, match="响应结构异常"):
            p.chat([{"role": "user", "content": "hi"}])
        assert rec.n_calls == 1, "结构性错误被当成网络抖动重试了"

    def test_truncated_body_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """响应被截断（JSON 破损）反而**应该**重试——那是传输问题，重发有意义。"""

        class Truncated(FakeHTTPResponse):
            def read(self) -> bytes:
                return b'{"choices": [{"mess'

        calls = {"n": 0}

        def fake_urlopen(req: Any, timeout: Any = None) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                return Truncated(b"")
            return FakeHTTPResponse(json.dumps(chat_payload("ok")).encode("utf-8"))

        p = LLMProvider(name="fake", url="http://fake/v1/chat/completions", model="m")
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert p.chat([{"role": "user", "content": "hi"}]).text == "ok"
        assert calls["n"] == 2

    def test_usage_is_taken_from_api_not_estimated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = chat_payload("ok")
        payload["usage"] = {
            "prompt_tokens": 1234,
            "completion_tokens": 56,
            "completion_tokens_details": {"reasoning_tokens": 40},
            "prompt_tokens_details": {"cached_tokens": 1000},
        }
        u = self._resp(monkeypatch, payload).usage
        assert (u.prompt_tokens, u.completion_tokens, u.reasoning_tokens, u.cached_tokens) == (1234, 56, 40, 1000)
        assert u.total_tokens == 1290


class TestUsage:
    def test_missing_payload_is_zeros_not_none(self) -> None:
        u = Usage.from_payload(None)
        assert u.to_dict() == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        }

    def test_total_excludes_reasoning_double_count(self) -> None:
        """reasoning token 已包含在 completion_tokens 里，total 不能再加一遍。"""
        u = Usage(prompt_tokens=10, completion_tokens=5, reasoning_tokens=4)
        assert u.total_tokens == 15

    def test_addition_is_field_wise(self) -> None:
        a = Usage(1, 2, 3, 4)
        b = Usage(10, 20, 30, 40)
        assert (a + b).to_dict()["prompt_tokens"] == 11
        assert (a + b).to_dict()["cached_tokens"] == 44
        # 不可变：相加不许改坏原对象（账本会重复累加同一个 Usage）
        assert a.to_dict()["prompt_tokens"] == 1

    def test_string_numbers_are_coerced(self) -> None:
        u = Usage.from_payload({"prompt_tokens": "7", "completion_tokens": None})
        assert u.prompt_tokens == 7 and u.completion_tokens == 0


class TestLooseJsonParsing:
    def test_plain_json(self) -> None:
        assert _loads_loose('{"a": 1}') == {"a": 1}

    def test_fenced_block(self) -> None:
        assert _loads_loose('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fence_without_language_tag(self) -> None:
        assert _loads_loose('```\n[1, 2]\n```') == [1, 2]

    def test_prose_before_and_after(self) -> None:
        text = '好的，这是结果：\n{"a": [1, {"b": 2}]}\n希望有帮助！'
        assert _loads_loose(text) == {"a": [1, {"b": 2}]}

    def test_braces_inside_strings_do_not_break_balance(self) -> None:
        assert _loads_loose('note: {"a": "}{ not a brace"}') == {"a": "}{ not a brace"}

    def test_array_payload(self) -> None:
        assert _loads_loose('前言\n[{"kox_id": "k1"}]') == [{"kox_id": "k1"}]

    def test_prose_plus_array_is_not_truncated_to_first_element(self) -> None:
        """回归：给数组加前言时，不许只截到数组的第一个元素。

        原实现固定先扫 ``{``，于是 ``好的：[{a},{b},{c}]`` 会被解析成 ``{a}``——
        整批 12 条判定被砍成 1 条。下游 ``_parse_array`` 拿到 dict 会抛错，
        所以不会算错数字，但这一批的 token 就白花了，覆盖率也无声地掉。
        """
        text = '好的，结果如下：\n[{"kox_id": "k1"}, {"kox_id": "k2"}, {"kox_id": "k3"}]\n以上。'
        assert _loads_loose(text) == [{"kox_id": "k1"}, {"kox_id": "k2"}, {"kox_id": "k3"}]

    def test_object_still_wins_when_it_comes_first(self) -> None:
        """反向控制：对象在前时仍按对象解析（brief 任务返回的就是对象）。"""
        assert _loads_loose('说明：{"kpi": "reach", "platforms": ["tiktok"]}') == {
            "kpi": "reach",
            "platforms": ["tiktok"],
        }

    @pytest.mark.parametrize("text", ["", "   ", "完全没有 JSON", "{不是合法 JSON"])
    def test_unparseable_raises_instead_of_returning_default(self, text: str) -> None:
        """静默返回 {} 会让"模型答崩了"变成"模型答了个空"，指标会被悄悄洗白。"""
        with pytest.raises(LLMError):
            _loads_loose(text)

    def test_response_json_payload_uses_the_same_path(self) -> None:
        r = LLMResponse(text='```json\n{"ok": true}\n```', usage=Usage(), model="m", provider="p", latency_ms=1)
        assert r.json_payload() == {"ok": True}


# ===========================================================================
# 3. Prompt：受控词表单一来源、输出契约、以及"绝不把答案喂给模型"
# ===========================================================================
@pytest.fixture(scope="module")
def fraud_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    """一条 gt 最"脏"的记录：造假 + 有 verdict，用来检查 prompt 是否泄漏答案。"""
    for r in records:
        gt = r.get("gt") or {}
        if gt.get("is_fraud") and gt.get("fraud_type"):
            return r
    pytest.skip("数据集里没有造假样本")


@pytest.fixture(scope="module")
def mismatch_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    for r in records:
        if (r.get("gt") or {}).get("tag_mismatch"):
            return r
    pytest.skip("数据集里没有标签错配样本")


class TestPromptVocabularyIsSingleSourced:
    def test_category_block_covers_taxonomy_exactly(self) -> None:
        block = category_block()
        assert len(block.strip().splitlines()) == len(CATEGORY_ZH)
        for key, zh in CATEGORY_ZH.items():
            assert f"- {key}: {zh}" in block

    def test_adjacency_block_only_references_known_categories(self) -> None:
        from koxpilot.taxonomy import CATEGORY_ADJACENCY

        block = adjacency_block()
        assert len(block.strip().splitlines()) == len(CATEGORY_ADJACENCY)
        for key, neighbours in CATEGORY_ADJACENCY.items():
            assert f"（{key}）" in block
            for n in neighbours:
                assert n in CATEGORY_ZH, f"邻接表引用了词表外的品类：{n}"

    def test_brief_prompt_ships_every_controlled_vocabulary(self) -> None:
        """词表必须从 taxonomy 注入，不能手抄——手抄的那一份迟早和数据集漂移。"""
        system = brief_parse_messages("随便一句 brief")[0]["content"]
        for bucket in AGE_BUCKETS:
            assert bucket in system
        for plat in PLATFORMS:
            assert plat in system
        for cat in CATEGORY_ZH:
            assert cat in system
        for kpi in KPI_VOCAB:
            assert kpi in system

    def test_every_spec_field_appears_in_the_output_contract(self) -> None:
        system = brief_parse_messages("x")[0]["content"]
        for field in CAMPAIGN_SPEC_FIELDS:
            assert f'"{field}"' in system, f"输出契约漏了 {field}，下游会拿到默认值"

    def test_kpi_vocab_is_a_subset_of_what_the_allocator_understands(self) -> None:
        """给模型的 KPI 取值域必须是预算侧真正认识的。

        ``balanced`` 故意不给模型：它是内部兜底档（解析失败/未提及时用），
        不是客户会说的话。反过来若 KPI_VOCAB 里出现预算侧不认识的值，
        ``KPI_EXPONENTS.get(kpi, balanced)`` 会静默兜底，
        于是"客户要转化"被当成"平衡"来算 value，且无任何报错。
        """
        from koxpilot.budget.policy import KPI_EXPONENTS

        assert set(KPI_VOCAB) <= set(KPI_EXPONENTS)
        assert set(KPI_EXPONENTS) - set(KPI_VOCAB) == {"balanced"}

    def test_regulated_category_enum_is_backed_by_taxonomy(self) -> None:
        """回归（真实 bug）：prompt 允许的每个受管制品类都必须在 G3.5 的映射表里。

        修复前 prompt 允许 ``alcohol``，而 ``REGULATED_CATEGORY_FLAGS`` 里没有它：
        ``.get("alcohol", ())`` 拿到空集合 → G3.5 对酒类**整体失效**，
        但严重度阈值那边照样按受管制品类收紧 1 倍多——
        "规则跑了、什么都没查"，且不报错。
        """
        from koxpilot.taxonomy import REGULATED_CATEGORY_FLAGS

        system = brief_parse_messages("x")[0]["content"]
        line = next(ln for ln in system.splitlines() if '"regulated_category"' in ln)
        # 契约写成 <"kids"|"medical"|...|null，说明文字>，只取带引号的枚举值
        enum_values = set(re.findall(r'"([a-z_]+)"', line.split("<", 1)[1]))
        assert enum_values, "没能从契约里解析出 regulated_category 取值域"
        assert enum_values <= set(REGULATED_CATEGORY_FLAGS), (
            f"prompt 允许但 G3.5 不认识：{enum_values - set(REGULATED_CATEGORY_FLAGS)}"
        )

    def test_gender_enum_never_produces_a_silently_dead_constraint(
        self, records: list[dict[str, Any]]
    ) -> None:
        """回归（真实 bug）：prompt 允许 ``all``，而达人侧 ``audience_gender`` 只有 f/m。

        修复前 ``target_gender="all"`` 会让 G2.6 去取一个不存在的 key，
        稳定拿到 0.0 —— "不限性别"被算成"受众完全不匹配"，达人白扣分且无报错。
        现在 CampaignSpec 会把它归一成 None（= 不做约束）。
        """
        system = brief_parse_messages("x")[0]["content"]
        line = next(ln for ln in system.splitlines() if '"target_gender"' in ln)
        enum_values = [t.strip() for t in line.split("<")[1].split(">")[0].split("|")]
        profile_keys = set().union(*(set((r.get("audience_gender") or {})) for r in records[:200]))
        for value in enum_values:
            norm = CampaignSpec.from_dict({"target_gender": value}).target_gender
            assert norm is None or norm in profile_keys, f"{value} → {norm} 不在 audience_gender 的 key 里"

    @pytest.mark.parametrize("raw,expected", [("all", None), ("ALL", None), ("Female", "f"), (" m ", "m"), ("x", None)])
    def test_gender_normalisation_table(self, raw: str, expected: str | None) -> None:
        assert CampaignSpec.from_dict({"target_gender": raw}).target_gender == expected


class TestBriefContractRoundTrip:
    def model_output(self) -> dict[str, Any]:
        """一份完全符合 prompt 契约的假模型输出。"""
        return {
            "target_categories": ["beauty_care", "fashion"],
            "target_markets": ["US", "GB"],
            "target_languages": ["en"],
            "target_age_buckets": ["25-34", "35-44"],
            "target_gender": "f",
            "budget_usd": 120000,
            "kpi": "conversion",
            "platforms": ["tiktok", "youtube"],
            "competitor_brands": ["BrandX"],
            "regulated_category": None,
            "hard_requirements": ["多用腰尾部达人"],
            "evidence": "原文说了 25-40 岁女性",
            "ambiguities": ["语言是推断的"],
        }

    def test_downstream_consumes_the_contract_without_loss(self) -> None:
        """契约里的字段必须**真的**落进 CampaignSpec，否则 prompt 白写。"""
        spec = CampaignSpec.from_dict(self.model_output())
        assert spec.target_categories == ("beauty_care", "fashion")
        assert spec.target_markets == ("US", "GB")
        assert spec.target_age_buckets == ("25-34", "35-44")
        assert spec.budget_usd == 120000.0
        assert spec.kpi == "conversion"
        assert spec.platforms == ("tiktok", "youtube")
        assert spec.competitor_brands == ("BrandX",)

    def test_extra_contract_fields_are_ignored_not_fatal(self) -> None:
        """``hard_requirements``/``evidence``/``ambiguities`` 是给人看的，不进 spec。"""
        spec = CampaignSpec.from_dict(self.model_output())
        assert not hasattr(spec, "hard_requirements")
        assert spec.raw_text == ""

    def test_null_budget_means_no_budget_constraint(self) -> None:
        assert CampaignSpec.from_dict({"budget_usd": None}).budget_usd == 0.0

    def test_non_numeric_budget_fails_loudly(self) -> None:
        """"50,000" 这种带千分位的输出必须炸，而不是静默变 0。

        静默变 0 会让预算分配器直接早退、给出一个空方案，
        看起来像"约束太紧"，实际是解析层吞了错误。
        """
        with pytest.raises(ValueError):
            CampaignSpec.from_dict({"budget_usd": "50,000"})

    def test_missing_fields_degrade_to_neutral(self) -> None:
        spec = CampaignSpec.from_dict({})
        assert spec.is_neutral and spec.kpi == "balanced" and spec.budget_usd == 0.0


class TestPromptsDoNotLeakAnswers:
    """三个 prompt 都不许把 gt 递给模型。

    这是 ``test_no_leakage.py`` 的 LLM 侧对偶：那边保证确定性引擎读不到答案，
    这边保证模型也看不到答案。少了任何一半，"LLM vs 规则"的横评都不成立。
    """

    def assert_no_gt(self, messages: list[dict[str, str]]) -> None:
        blob = serialize(messages)
        for field in GT_FIELDS:
            assert f'"{field}"' not in blob, f"prompt 里出现了 gt 字段名：{field}"
        for value in FRAUD_TYPE_VALUES:
            assert value not in blob, f"prompt 里出现了造假类型取值：{value}"

    def test_the_detector_itself_can_fail(self, fraud_record: dict[str, Any]) -> None:
        """阳性对照：故意泄漏一次，检测器必须红。

        没有这条，上面所有"没泄漏"的断言都可能只是因为检测器写坏了。
        本模块第一版就正好栽在这里：blob 用 ``json.dumps`` 拼，引号被转义成 ``\\"``，
        于是 ``'"gt"' not in blob`` 恒成立——一整组安全断言全是假绿灯。
        """
        leaky = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": json.dumps({"gt": fraud_record["gt"]}, ensure_ascii=False)},
        ]
        with pytest.raises(AssertionError, match="gt 字段名"):
            self.assert_no_gt(leaky)

    def test_the_detector_catches_leaked_fraud_type_values(self) -> None:
        leaky = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "这个号是 bought_followers"},
        ]
        with pytest.raises(AssertionError, match="造假类型取值"):
            self.assert_no_gt(leaky)

    def test_tag_prompt_has_no_gt(self, mismatch_record: dict[str, Any], fraud_record: dict[str, Any]) -> None:
        self.assert_no_gt(tag_judge_messages([mismatch_record, fraud_record]))

    def test_fit_prompt_has_no_gt(self, fraud_record: dict[str, Any], spec: CampaignSpec) -> None:
        from dataclasses import asdict

        self.assert_no_gt(fit_score_messages(asdict(spec), [fraud_record]))

    def test_brief_prompt_carries_only_the_brief_text(self, briefs: list[dict[str, Any]]) -> None:
        msgs = brief_parse_messages(briefs[0]["raw_text"])
        self.assert_no_gt(msgs)
        # brief 任务不该夹带任何达人数据
        assert "kox_id" not in serialize(msgs)

    def test_tag_payload_keys_are_an_exact_whitelist(self, fraud_record: dict[str, Any]) -> None:
        """白名单式断言：新增字段必须显式改测试，不能"顺手多喂一点"。"""
        payload = user_payload(tag_judge_messages([fraud_record]))
        assert [set(item) for item in payload] == [
            {"kox_id", "declared_categories", "observed_categories", "source_tags"}
        ]

    @pytest.mark.parametrize(
        "signal", ["followers", "engagement_rate", "avg_views", "quoted_price_usd", "verified"]
    )
    def test_tag_prompt_prunes_irrelevant_signals(self, fraud_record: dict[str, Any], signal: str) -> None:
        """字段裁剪本身是 Prompt 工程的一部分。

        喂粉丝量会把模型带偏成"大号应该没问题"；喂报价会引入与判定无关的锚定效应。
        """
        assert f'"{signal}"' not in serialize(tag_judge_messages([fraud_record]))

    def test_fit_payload_keys_are_an_exact_whitelist(self, fraud_record: dict[str, Any]) -> None:
        payload = user_payload(fit_score_messages({"kpi": "reach"}, [fraud_record]))
        assert [set(item) for item in payload] == [
            {
                "kox_id",
                "platform",
                "creator_country",
                "language",
                "declared_categories",
                "observed_categories",
                "audience_geo",
                "audience_age",
                "audience_gender",
            }
        ]

    @pytest.mark.parametrize(
        "signal", ["followers", "quoted_price_usd", "content_flags", "comment_dup_rate", "engagement_rate"]
    )
    def test_fit_prompt_excludes_what_it_forbids_itself_to_judge(
        self, fraud_record: dict[str, Any], signal: str
    ) -> None:
        """prompt 明确说"水号/报价/品牌安全不在你的评估范围"，那就不该把这些信号喂进去。

        写"请不要评估 X"却把 X 递过去，是最常见的 prompt 自相矛盾。
        """
        assert f'"{signal}"' not in serialize(fit_score_messages({"kpi": "reach"}, [fraud_record]))


class TestPromptOutputContracts:
    def test_all_prompts_demand_strict_json_without_fences(self) -> None:
        for msgs in (
            brief_parse_messages("x"),
            tag_judge_messages([{"kox_id": "k1"}]),
            fit_score_messages({}, [{"kox_id": "k1"}]),
        ):
            system = msgs[0]["content"]
            assert "严格输出" in system
            assert "代码围栏" in system or "不要用代码围栏" in system

    def test_batch_prompts_pin_array_length_and_id_echo(self) -> None:
        """批量任务必须要求"长度一致 + 原样回填 id"，否则结果无法与输入对齐。"""
        for msgs in (
            tag_judge_messages([{"kox_id": "k1"}, {"kox_id": "k2"}]),
            fit_score_messages({}, [{"kox_id": "k1"}, {"kox_id": "k2"}]),
        ):
            system = msgs[0]["content"]
            assert "数组长度必须与输入达人数一致" in system
            assert "原样回填" in system
            assert "长度为 2 的 JSON 数组" in msgs[1]["content"]

    def test_tag_prompt_requires_low_confidence_on_thin_evidence(self) -> None:
        """"信息不足就说不知道"必须写进 prompt：投放场景里瞎猜比沉默更贵。"""
        system = tag_judge_messages([{"kox_id": "k1"}])[0]["content"]
        assert "信息不足" in system and "不要靠猜" in system

    def test_fit_prompt_declares_its_boundaries(self) -> None:
        system = fit_score_messages({}, [{"kox_id": "k1"}])[0]["content"]
        for boundary in ("水号", "报价", "品牌安全"):
            assert boundary in system
        assert "不要因为达人粉丝多就给高分" in system

    def test_no_unresolved_placeholders(self) -> None:
        """f-string 拼错会留下 ``{}``/``None`` 这类残渣，模型会照着残渣理解。"""
        for msgs in (
            brief_parse_messages("x"),
            tag_judge_messages([{"kox_id": "k1"}]),
            fit_score_messages({"kpi": "reach"}, [{"kox_id": "k1"}]),
        ):
            system = msgs[0]["content"]
            assert "{}" not in system
            assert "None" not in system


class TestPromptMessageShape:
    @pytest.mark.parametrize(
        "builder",
        [
            lambda recs: brief_parse_messages("投 25-34 岁女性美妆，预算 10 万美金"),
            lambda recs: tag_judge_messages(recs),
            lambda recs: fit_score_messages({"kpi": "reach"}, recs),
        ],
    )
    def test_shape_is_system_then_user(self, builder: Any, small_records: list[dict[str, Any]]) -> None:
        msgs = builder(small_records[:3])
        assert [m["role"] for m in msgs] == ["system", "user"]
        assert all(m["content"].strip() for m in msgs)

    def test_builders_are_deterministic(self, small_records: list[dict[str, Any]]) -> None:
        """这是缓存正确性的前提：同一输入必须逐字节生成同一份消息。

        若 prompt 里混进时间戳/集合遍历序，cache key 每次都变，
        构建期会把同一批 token 反复烧掉，而且"重跑结果一致"也不再成立。
        """
        batch = small_records[:5]
        assert tag_judge_messages(batch) == tag_judge_messages(batch)
        assert fit_score_messages({"kpi": "reach"}, batch) == fit_score_messages({"kpi": "reach"}, batch)
        assert brief_parse_messages("同一句话") == brief_parse_messages("同一句话")

    def test_payload_order_follows_input_order(self, small_records: list[dict[str, Any]]) -> None:
        batch = small_records[:6]
        ids = [r["kox_id"] for r in batch]
        assert [i["kox_id"] for i in user_payload(tag_judge_messages(batch))] == ids
        assert [i["kox_id"] for i in user_payload(fit_score_messages({}, batch))] == ids

    def test_brief_text_is_stripped_but_preserved(self) -> None:
        msgs = brief_parse_messages("  投 GenZ 男性游戏，预算 6 万美金  ")
        assert "投 GenZ 男性游戏，预算 6 万美金" in msgs[1]["content"]

    def test_empty_batch_does_not_crash(self) -> None:
        assert "共 0 个达人" in tag_judge_messages([])[1]["content"]


# ===========================================================================
# 4. Prompt 版本谱系：控制变量、与生产版的漂移、以及与产物的对账
# ===========================================================================
DOC_BOUNDARIES = REPO_ROOT / "docs" / "05-boundaries.md"
PROMPT_BENCH = OUTPUT_DIR / "prompt_bench.json"


@pytest.fixture(scope="module")
def prompt_bench() -> dict[str, Any]:
    if not PROMPT_BENCH.exists():
        pytest.skip(f"缺少 {PROMPT_BENCH}")
    return json.loads(PROMPT_BENCH.read_text("utf-8"))


class TestPromptVariants:
    def test_three_versions_in_order(self) -> None:
        assert [v.version for v in TAG_VARIANTS] == ["v1", "v2", "v3"]
        assert len({v.system for v in TAG_VARIANTS}) == 3, "有两版 system 完全相同，实验不成立"

    def test_every_variant_states_its_change_and_hypothesis(self) -> None:
        """事前假设必须写在代码里。

        没有事前假设的"实验"无法证伪：跑完再编一个解释，永远是对的。
        """
        for v in TAG_VARIANTS:
            assert v.change.strip() and v.hypothesis.strip()
            assert v.name.strip()

    def test_meta_char_count_matches_the_prompt(self) -> None:
        for v in TAG_VARIANTS:
            assert v.to_meta()["system_prompt_chars"] == str(len(v.system))

    def test_user_message_is_identical_across_variants(self, small_records: list[dict[str, Any]]) -> None:
        """控制变量的硬要求：只许 system 变。

        user 侧任何差异都会让"指标差异归因于 Prompt"这句话失效。
        """
        batch = small_records[:4]
        users = {build_messages(v, batch)[1]["content"] for v in TAG_VARIANTS}
        assert len(users) == 1
        systems = {build_messages(v, batch)[0]["content"] for v in TAG_VARIANTS}
        assert len(systems) == 3

    def test_variant_user_message_equals_production_user_message(
        self, small_records: list[dict[str, Any]]
    ) -> None:
        """横评与生产共用同一份 user 构造，否则横评结论无法外推到生产。"""
        batch = small_records[:4]
        assert build_messages(TAG_VARIANTS[-1], batch)[1] == tag_judge_messages(batch)[1]

    def test_variant_payload_has_no_gt(self, fraud_record: dict[str, Any]) -> None:
        blob = serialize(build_messages(TAG_VARIANTS[0], [fraud_record]))
        for field in GT_FIELDS:
            assert f'"{field}"' not in blob

    def test_v1_has_no_judgement_criteria(self) -> None:
        """v1 是基线：必须**真的**没有判据，否则 v1→v2 的增益是假的。"""
        v1 = TAG_VARIANTS[0].system
        assert "不算" not in v1 and "豁免" not in v1
        assert adjacency_block() not in v1

    def test_v2_adds_exemptions_but_not_the_adjacency_table(self) -> None:
        v2 = TAG_VARIANTS[1].system
        assert "不算" in v2
        assert adjacency_block() not in v2, "v2 若已含完整邻接表，v2→v3 的归因就更混了"

    def test_v3_injects_the_deterministic_adjacency_table(self) -> None:
        v3 = TAG_VARIANTS[2].system
        assert adjacency_block() in v3
        assert "浪费预算" in v3, "v3 的核心改动是把判据改成业务后果"

    def test_v3_confounding_is_disclosed(self) -> None:
        """v3 一次改了两处，增益无法严格归因——这件事必须写在 change 里。"""
        assert "无法" in TAG_VARIANTS[2].change and "归因" in TAG_VARIANTS[2].change

    def test_length_grows_monotonically(self) -> None:
        lens = [len(v.system) for v in TAG_VARIANTS]
        assert lens == sorted(lens)

    def test_variant_is_immutable(self) -> None:
        with pytest.raises(Exception):
            TAG_VARIANTS[0].system = "改不了"  # type: ignore[misc]


class TestProductionDriftIsDisclosed:
    """v3 号称"生产版"，但与生产 prompt 并非逐字相同。

    这不是我发现的新问题——作者已经把它登记在 docs/05 §10 第 4 条。
    这里做的是把"登记"变成"可执行的约束"：只要代码和文档任一侧改动，
    这条测试就会红，逼着两边同时更新。
    """

    def prod_and_v3(self) -> tuple[str, str]:
        return prompts_mod._tag_system(), TAG_VARIANTS[2].system

    def test_semantic_core_is_shared(self) -> None:
        prod, v3 = self.prod_and_v3()
        for anchor in ("实质性的标签错配", "浪费预算", "severity", "信息不足"):
            assert anchor in prod and anchor in v3
        assert adjacency_block() in prod and adjacency_block() in v3

    def test_drift_and_documentation_agree_in_both_directions(self) -> None:
        prod, v3 = self.prod_and_v3()
        doc = DOC_BOUNDARIES.read_text("utf-8")
        if prod == v3:
            assert "v3 与生产 prompt" not in doc, (
                "两者已统一，docs/05 §10 第 4 条应当撤掉——否则文档在自认一个不存在的问题"
            )
        else:
            assert f"{len(v3):,}" in doc and f"{len(prod):,}" in doc, (
                f"v3({len(v3)} 字) 与生产({len(prod)} 字) 不一致，"
                "必须在 docs/05 §10 里按真实字符数登记"
            )

    def test_the_substantive_difference_is_only_about_wording(self) -> None:
        """逐行 diff：允许措辞差异，但两侧的 severity 档位必须仍是同一套。"""
        prod, v3 = self.prod_and_v3()
        for level in ("major", "minor", "none"):
            assert f"- {level}：" in prod and f"- {level}：" in v3


class TestPromptBenchProvenance:
    """产物对账：仓库里发表的数字，必须是当前这份代码能产生的数字。"""

    def test_recorded_prompt_sizes_match_the_shipped_prompts(self, prompt_bench: dict[str, Any]) -> None:
        """若有人改了 prompt 却没重跑 bench，这条会红。

        这正是"评测作弊"最常见的无声形态：文案改了、数字还是旧的。
        """
        recorded = {v["version"]: int(v["system_prompt_chars"]) for v in prompt_bench["prompt_variants"]}
        for v in TAG_VARIANTS:
            assert recorded[v.version] == len(v.system), (
                f"{v.version} 的 prompt 已改动（现在 {len(v.system)} 字，产物里记的是 "
                f"{recorded[v.version]} 字），需要重跑 promptbench 或如实登记"
            )

    def test_hypotheses_in_artifact_match_the_code(self, prompt_bench: dict[str, Any]) -> None:
        recorded = {v["version"]: v for v in prompt_bench["prompt_variants"]}
        for v in TAG_VARIANTS:
            assert recorded[v.version]["hypothesis"] == v.hypothesis
            assert recorded[v.version]["change"] == v.change

    def test_version_average_is_recomputable_from_arms(self, prompt_bench: dict[str, Any]) -> None:
        arms = [a for a in prompt_bench["arms"] if a["prompt_version"] != "rule"]
        for version, agg in prompt_bench["version_average"].items():
            xs = [a for a in arms if a["prompt_version"] == version]
            assert xs
            assert agg["avg_f1"] == pytest.approx(round(sum(x["f1"] for x in xs) / len(xs), 4), abs=1e-4)
            assert agg["avg_precision"] == pytest.approx(
                round(sum(x["precision"] for x in xs) / len(xs), 4), abs=1e-4
            )
            assert agg["total_tokens"] == sum(x["total_tokens"] for x in xs)

    def test_each_arm_metrics_are_recomputable_from_its_confusion(self, prompt_bench: dict[str, Any]) -> None:
        for arm in prompt_bench["arms"]:
            c = arm["confusion"]
            tp, fp, fn, tn = c["tp"], c["fp"], c["fn"], c["tn"]
            p = tp / (tp + fp) if tp + fp else 0.0
            r = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * p * r / (p + r) if p + r else 0.0
            assert arm["precision"] == pytest.approx(round(p, 4), abs=1e-4), arm["prompt_version"]
            assert arm["recall"] == pytest.approx(round(r, 4), abs=1e-4), arm["prompt_version"]
            assert arm["f1"] == pytest.approx(round(f1, 4), abs=1e-4), arm["prompt_version"]
            assert tp + fp + fn + tn == arm["n_covered"], "混淆矩阵没盖住被判定的样本数"

    def test_coverage_is_reported_and_never_backfilled(self, prompt_bench: dict[str, Any]) -> None:
        """漏判必须以 coverage < 1 的形式暴露，不许当成"判对"或"判错"填进矩阵。"""
        for arm in prompt_bench["arms"]:
            assert arm["n_covered"] <= arm["n_samples"]
            assert arm["coverage"] == pytest.approx(
                round(arm["n_covered"] / arm["n_samples"], 4), abs=1e-4
            )

    def test_best_arm_is_the_actual_argmax(self, prompt_bench: dict[str, Any]) -> None:
        arms = [a for a in prompt_bench["arms"] if a["prompt_version"] != "rule"]
        best = max(arms, key=lambda a: a["f1"])
        assert prompt_bench["best_arm"]["f1"] == best["f1"]
        assert prompt_bench["best_arm"]["prompt_version"] == best["prompt_version"]

    def test_rule_baseline_is_zero_token_and_reproducible(
        self, prompt_bench: dict[str, Any], records: list[dict[str, Any]]
    ) -> None:
        """规则对照组必须能在测试里**当场重算**出来，而不是只能相信产物。"""
        import random

        rule_arm = next(a for a in prompt_bench["arms"] if a["prompt_version"] == "rule")
        assert rule_arm["total_tokens"] == 0 and rule_arm["calls"] == 0
        rng = random.Random(20270919)
        sample = sorted(
            rng.sample(records, min(prompt_bench["n_samples"], len(records))), key=lambda r: r["kox_id"]
        )
        tp = fp = fn = tn = 0
        for r in sample:
            gt = bool((r.get("gt") or {}).get("tag_mismatch"))
            pred = rule_tag_mismatch(r)
            tp += gt and pred
            fp += (not gt) and pred
            fn += gt and (not pred)
            tn += (not gt) and (not pred)
        assert rule_arm["confusion"] == {"tp": tp, "fp": fp, "fn": fn, "tn": tn}
        assert prompt_bench["n_positives"] == tp + fn

    def test_llm_beats_the_rule_on_precision(self, prompt_bench: dict[str, Any]) -> None:
        """结论型断言用**关系**而不是常数：最优 arm 的精确率应高于规则对照组。

        若哪天不成立，说明"这个环节值得花 token"的论证需要重写——正是该红的时候。
        """
        rule_arm = next(a for a in prompt_bench["arms"] if a["prompt_version"] == "rule")
        best = max(
            (a for a in prompt_bench["arms"] if a["prompt_version"] != "rule"), key=lambda a: a["f1"]
        )
        assert best["precision"] > rule_arm["precision"]
        assert best["f1"] > prompt_bench["rule_baseline_f1"]

    def test_control_variables_are_declared(self, prompt_bench: dict[str, Any]) -> None:
        assert "唯一变量是 system prompt" in prompt_bench["control_variables"]


# ===========================================================================
# 5. 磁盘缓存：幂等、原子、坏文件可恢复
# ===========================================================================
class TestCache:
    def test_fresh_cache_has_the_expected_skeleton(self, tmp_path: Path) -> None:
        c = Cache(tmp_path / "c.json")
        assert c.data["entries"] == {} and c.data["_meta"] == {}
        assert c.get("nope") is None

    def test_roundtrip_through_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        c = Cache(path)
        c.put("k", {"v": 1}, flush_every=1)
        assert json.loads(path.read_text("utf-8"))["entries"]["k"] == {"v": 1}
        assert Cache(path).get("k") == {"v": 1}

    def test_flush_every_batches_writes(self, tmp_path: Path) -> None:
        """并发写场景下每条都整文件落盘会成为瓶颈，所以按批 flush。

        代价是"最多丢最后几条"——可接受，因为重跑会命中缓存补齐。
        这里钉住这个折中：默认 8 条才落盘。
        """
        path = tmp_path / "c.json"
        c = Cache(path)
        for i in range(7):
            c.put(f"k{i}", i)
        assert not path.exists(), "默认 flush_every=8，7 条不该落盘"
        c.put("k7", 7)
        assert path.exists()
        assert len(json.loads(path.read_text("utf-8"))["entries"]) == 8

    def test_explicit_flush_persists_the_tail(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        c = Cache(path)
        c.put("only", 1)
        c.flush()
        assert Cache(path).get("only") == 1

    def test_flush_is_atomic_and_leaves_no_tmp(self, tmp_path: Path) -> None:
        """原子替换：中途被杀不能留下半个文件或 .tmp 垃圾。"""
        path = tmp_path / "c.json"
        c = Cache(path)
        c.put("k", "v", flush_every=1)
        assert not (tmp_path / "c.tmp").exists()
        assert list(tmp_path.iterdir()) == [path]

    def test_corrupt_file_is_rebuilt_not_fatal(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """构建期跑几十分钟，缓存被写坏就重建——但必须**出声**，不能静默丢结果。"""
        path = tmp_path / "c.json"
        path.write_text("{不是 JSON", "utf-8")
        c = Cache(path)
        assert c.data["entries"] == {}
        assert "损坏" in capsys.readouterr().err
        c.put("k", 1, flush_every=1)
        assert Cache(path).get("k") == 1

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        c = Cache(tmp_path / "deep" / "nested" / "c.json")
        c.put("k", 1, flush_every=1)
        assert (tmp_path / "deep" / "nested" / "c.json").exists()

    def test_bundle_keys_survive_flush(self, tmp_path: Path) -> None:
        """runner 会把 brief_specs / tag_bench 等"包裹"塞进同一个文件，不能被 flush 抹掉。"""
        path = tmp_path / "c.json"
        c = Cache(path)
        c.data["campaign_specs"] = {"BRIEF-001": {"kpi": "reach"}}
        c.flush()
        assert json.loads(path.read_text("utf-8"))["campaign_specs"] == {"BRIEF-001": {"kpi": "reach"}}


# ===========================================================================
# 6. 缓存 key：本章的核心。key 必须由"真正发出去的消息"决定
# ===========================================================================
class TestSha1Key:
    def test_shape(self) -> None:
        h = _sha1({"a": 1})
        assert len(h) == 16 and all(ch in "0123456789abcdef" for ch in h)

    def test_insensitive_to_dict_order(self) -> None:
        """key 不能随字典书写顺序变，否则同一份输入会重复付费。"""
        assert _sha1({"a": 1, "b": 2}) == _sha1({"b": 2, "a": 1})

    def test_sensitive_to_values_and_types(self) -> None:
        assert _sha1({"a": 1}) != _sha1({"a": 2})
        assert _sha1({"a": 1}) != _sha1({"a": "1"})
        assert _sha1([1, 2]) != _sha1([2, 1])

    def test_non_ascii_is_stable(self) -> None:
        assert _sha1({"品类": "美妆个护"}) == _sha1({"品类": "美妆个护"})


def tag_key(model: str, batch: list[dict[str, Any]]) -> str:
    """复刻 runner 里 tag 任务的 key 组装方式（测试侧独立重算）。"""
    return f"tag::{model}::{_sha1(tag_judge_messages(batch))}"


class TestCacheKeyHonesty:
    """key 只 hash kox_id 会造成**无声的评测作弊**：

    prompt 改版后仍命中旧缓存，bench 出来的是上一版的答案，
    而指标、覆盖率、token 账全都看起来正常。没有任何外部症状，
    只能靠这一组测试钉住。
    """

    def test_key_changes_when_the_system_prompt_changes(
        self, monkeypatch: pytest.MonkeyPatch, small_records: list[dict[str, Any]]
    ) -> None:
        batch = small_records[:3]
        before = tag_key("m1", batch)
        monkeypatch.setattr(prompts_mod, "_tag_system", lambda: "改版后的 system prompt")
        after = tag_key("m1", batch)
        assert before != after, "system prompt 改版没让缓存失效"

    @pytest.mark.parametrize(
        "field,new_value",
        [
            ("declared_categories", ["gaming_app"]),
            ("observed_categories", ["auto_travel"]),
            ("source_tags", {"vendor_a": ["fashion"]}),
        ],
    )
    def test_key_changes_when_prompt_visible_content_changes(
        self, small_records: list[dict[str, Any]], field: str, new_value: Any
    ) -> None:
        """只 hash kox_id 的实现会在这里露馅：数据重生成后仍命中旧答案。"""
        batch = [dict(small_records[0])]
        before = tag_key("m1", batch)
        batch[0][field] = new_value
        assert tag_key("m1", batch) != before

    def test_key_ignores_fields_that_never_reach_the_model(
        self, small_records: list[dict[str, Any]]
    ) -> None:
        """反向控制：不进 prompt 的字段改了，key 不该变。

        这不是漏洞而是设计——粉丝量变化不影响标签错配的判定输入，
        重新付费判一遍纯属浪费。前提是"不进 prompt"这件事被上面的白名单测试锁住。
        """
        batch = [dict(small_records[0])]
        before = tag_key("m1", batch)
        batch[0]["followers"] = 999_999_999
        batch[0]["quoted_price_usd"] = 1.0
        assert tag_key("m1", batch) == before

    def test_key_separates_models_and_tasks(self, small_records: list[dict[str, Any]]) -> None:
        batch = small_records[:3]
        assert tag_key("ark", batch) != tag_key("azure", batch)
        msgs_hash = _sha1(tag_judge_messages(batch))
        assert f"tag::ark::{msgs_hash}" != f"fit::ark::{msgs_hash}"

    def test_key_changes_with_batch_composition_and_order(
        self, small_records: list[dict[str, Any]]
    ) -> None:
        a, b, c = small_records[0], small_records[1], small_records[2]
        assert tag_key("m", [a, b]) != tag_key("m", [a, b, c])
        assert tag_key("m", [a, b]) != tag_key("m", [b, a]), "批内顺序影响回填对齐，必须进 key"

    def test_fit_key_covers_spec_brief_and_candidates(self, small_records: list[dict[str, Any]]) -> None:
        batch = small_records[:3]
        spec_a = {"kpi": "reach", "target_categories": ["beauty_care"]}
        spec_b = {"kpi": "conversion", "target_categories": ["beauty_care"]}

        def key(model: str, bid: str, spec: dict[str, Any], recs: list[dict[str, Any]]) -> str:
            return f"fit::{model}::{bid}::{_sha1(fit_score_messages(spec, recs))}"

        base = key("m", "BRIEF-001", spec_a, batch)
        assert key("m", "BRIEF-001", spec_b, batch) != base, "spec 改了必须换 key"
        assert key("m", "BRIEF-002", spec_a, batch) != base, "brief 不同必须换 key"
        assert key("m2", "BRIEF-001", spec_a, batch) != base, "模型不同必须换 key"
        assert key("m", "BRIEF-001", spec_a, batch) == base

    def test_promptbench_keys_never_collide_across_variants(
        self, small_records: list[dict[str, Any]]
    ) -> None:
        """三版 prompt 必须各有各的 key。

        撞 key 是最恶劣的一种：v3 会直接吃到 v1 的答案，
        于是"Prompt 迭代带来提升"这个结论完全是缓存造出来的。
        """
        batch = small_records[:4]
        keys = {
            f"pbench::{v.version}::m::{_sha1(build_messages(v, batch))}" for v in TAG_VARIANTS
        }
        assert len(keys) == 3
        hashes = {_sha1(build_messages(v, batch)) for v in TAG_VARIANTS}
        assert len(hashes) == 3, "不同 prompt 版本 hash 出了同一个值"


# ===========================================================================
# 7. 账本
# ===========================================================================
class TestLedger:
    def test_groups_by_task_and_model(self) -> None:
        led = Ledger()
        led.add(CallLog("tag", "ark", "m1", 12, Usage(100, 20, 5, 1), 500, True))
        led.add(CallLog("tag", "ark", "m1", 12, Usage(100, 20, 5, 1), 700, True))
        led.add(CallLog("tag", "azure", "m2", 12, Usage(10, 2, 0, 0), 100, True))
        s = led.summarize()
        assert set(s) == {"tag::ark", "tag::azure"}
        assert s["tag::ark"]["calls"] == 2 and s["tag::ark"]["items"] == 24
        assert s["tag::ark"]["total_tokens"] == 240
        assert s["tag::ark"]["avg_latency_ms"] == 600.0
        assert s["tag::ark"]["tokens_per_item"] == 10.0

    def test_failed_calls_are_counted_and_cost_nothing(self) -> None:
        """失败调用必须单独计数：拿它冒充成功会让 coverage 与 token 账都失真。"""
        led = Ledger()
        led.add(CallLog("tag", "ark", "m1", 12, Usage(), 0, False, "boom"))
        s = led.summarize()["tag::ark"]
        assert s["calls"] == 1 and s["failed_calls"] == 1 and s["total_tokens"] == 0

    def test_tokens_per_item_is_none_when_no_items(self) -> None:
        led = Ledger()
        led.add(CallLog("brief", "ark", "m1", 0, Usage(5, 5), 10, True))
        assert led.summarize()["brief::ark"]["tokens_per_item"] is None

    def test_call_log_to_dict_carries_usage(self) -> None:
        d = CallLog("fit", "ark", "m1", 3, Usage(1, 2, 3, 4), 9, True).to_dict()
        assert d["task"] == "fit" and d["total_tokens"] == 3
        assert {"prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens"} <= set(d)


# ===========================================================================
# 8. 并发与切批
# ===========================================================================
class TestPmapAndBatching:
    def test_pmap_preserves_input_order(self) -> None:
        """批内 kox_id 是按序回填的，结果顺序乱了就会把答案配错人。"""
        items = list(range(50))
        assert pmap(lambda i, it: it * 2, items, workers=8) == [i * 2 for i in items]

    def test_pmap_single_worker_path(self) -> None:
        assert pmap(lambda i, it: (i, it), ["a", "b"], workers=1) == [(0, "a"), (1, "b")]

    def test_pmap_empty_and_single(self) -> None:
        assert pmap(lambda i, it: it, [], workers=4) == []
        assert pmap(lambda i, it: it, ["x"], workers=4) == ["x"]

    def test_pmap_propagates_exceptions(self) -> None:
        """单批失败要能被上层 try 住并记账；吞掉异常会让失败静默变成空结果。"""

        def boom(i: int, it: Any) -> Any:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            pmap(boom, [1, 2, 3], workers=4)

    def test_batched_partitions_exactly(self) -> None:
        items = list(range(25))
        batches = list(_batched(items, 12))
        assert [len(b) for b in batches] == [12, 12, 1]
        assert [x for b in batches for x in b] == items

    def test_batched_empty(self) -> None:
        assert list(_batched([], 12)) == []


# ===========================================================================
# 9. 三个构建期任务：幂等、失败隔离、绝不用默认值冒充成功
# ===========================================================================
class TestParseArray:
    """``_parse_array``：把模型输出规整成数组，规整不了就抛错。"""

    def test_plain_array(self) -> None:
        assert _parse_array([{"kox_id": "k1"}], "m") == [{"kox_id": "k1"}]

    def test_unwraps_a_single_wrapper_layer(self) -> None:
        """``{"results": [...]}`` 这种包装很常见，剥掉它比让整批失败划算。"""
        assert _parse_array({"results": [1, 2]}, "m") == [1, 2]
        assert _parse_array({"note": "ok", "items": [1]}, "m") == [1]

    @pytest.mark.parametrize("payload", [{"note": "没有数组"}, "字符串", 42, None])
    def test_non_array_raises_instead_of_returning_empty(self, payload: Any) -> None:
        """静默返回 [] 会让"模型答崩了"变成"这批没人错配"，直接洗白指标。"""
        with pytest.raises(LLMError, match="期望 JSON 数组"):
            _parse_array(payload, "m")

    def test_error_names_the_model(self) -> None:
        with pytest.raises(LLMError, match="ark"):
            _parse_array({"a": 1}, "ark")


class TestRunBrief:
    def test_happy_path_records_usage_and_caches(
        self, tmp_path: Path, briefs: list[dict[str, Any]]
    ) -> None:
        prov = FakeProvider(lambda m: {"kpi": "reach", "target_categories": ["beauty_care"]})
        cache, led = Cache(tmp_path / "c.json"), Ledger()
        out = run_brief({"fake": prov}, briefs[:2], cache, led, workers=1)
        assert set(out) == {b["brief_id"] for b in briefs[:2]}
        assert out[briefs[0]["brief_id"]]["fake"]["kpi"] == "reach"
        assert prov.n_calls == 2
        assert led.summarize()["brief::fake"]["calls"] == 2
        assert len(cache.data["entries"]) == 2

    def test_second_run_is_free(self, tmp_path: Path, briefs: list[dict[str, Any]]) -> None:
        """幂等：中断后重跑不许重复花 token，也不许产生不同结果。"""
        prov = FakeProvider(lambda m: {"kpi": "reach"})
        cache, led = Cache(tmp_path / "c.json"), Ledger()
        first = run_brief({"fake": prov}, briefs, cache, led, workers=1)
        prov.calls.clear()
        second = run_brief({"fake": prov}, briefs, cache, Ledger(), workers=1)
        assert prov.n_calls == 0, "第二次跑又调了模型，缓存没生效"
        assert first == second

    def test_prompt_version_change_invalidates_the_cache(
        self, tmp_path: Path, briefs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """system 文案改版后必须重新调用，否则产物是"上一版 prompt 的答案"。"""
        prov = FakeProvider(lambda m: {"kpi": "reach"})
        cache = Cache(tmp_path / "c.json")
        run_brief({"fake": prov}, briefs[:1], cache, Ledger(), workers=1)
        prov.calls.clear()
        monkeypatch.setattr(prompts_mod, "_brief_system", lambda: "新版 system prompt")
        run_brief({"fake": prov}, briefs[:1], cache, Ledger(), workers=1)
        assert prov.n_calls == 1

    def test_brief_text_change_invalidates_the_cache(
        self, tmp_path: Path, briefs: list[dict[str, Any]]
    ) -> None:
        prov = FakeProvider(lambda m: {"kpi": "reach"})
        cache = Cache(tmp_path / "c.json")
        run_brief({"fake": prov}, briefs[:1], cache, Ledger(), workers=1)
        edited = [dict(briefs[0], raw_text=briefs[0]["raw_text"] + " 追加一句需求")]
        prov.calls.clear()
        run_brief({"fake": prov}, edited, cache, Ledger(), workers=1)
        assert prov.n_calls == 1

    def test_failure_is_isolated_and_never_backfilled(
        self, tmp_path: Path, briefs: list[dict[str, Any]]
    ) -> None:
        """失败的 brief 必须**缺席**，不能拿默认 spec 顶上。

        顶上去的后果是：下游拿到一个"看起来合法"的中性 spec，
        整条 campaign 的判定全都建立在一个从未发生过的解析结果上。
        """
        bad_id = briefs[0]["brief_id"]

        def responder(messages: list[dict[str, str]]) -> Any:
            if bad_id in messages[1]["content"] or briefs[0]["raw_text"][:12] in messages[1]["content"]:
                return LLMError("fake", "模型抽风")
            return {"kpi": "reach"}

        prov = FakeProvider(responder)
        cache, led = Cache(tmp_path / "c.json"), Ledger()
        out = run_brief({"fake": prov}, briefs, cache, led, workers=1)
        assert bad_id not in out
        assert len(out) == len(briefs) - 1
        s = led.summarize()["brief::fake"]
        assert s["failed_calls"] == 1 and s["calls"] == len(briefs)
        assert len(cache.data["entries"]) == len(briefs) - 1, "失败的调用不该写缓存"

    def test_unparseable_output_counts_as_failure(
        self, tmp_path: Path, briefs: list[dict[str, Any]]
    ) -> None:
        prov = FakeProvider(lambda m: "我觉得这个 brief 挺好的，不输出 JSON 了")
        out = run_brief({"fake": prov}, briefs[:1], Cache(tmp_path / "c.json"), Ledger(), workers=1)
        assert out == {}

    def test_multiple_models_are_kept_separate(
        self, tmp_path: Path, briefs: list[dict[str, Any]]
    ) -> None:
        a = FakeProvider(lambda m: {"kpi": "reach"}, name="a")
        b = FakeProvider(lambda m: {"kpi": "conversion"}, name="b")
        out = run_brief({"a": a, "b": b}, briefs[:1], Cache(tmp_path / "c.json"), Ledger(), workers=1)
        row = out[briefs[0]["brief_id"]]
        assert row["a"]["kpi"] == "reach" and row["b"]["kpi"] == "conversion"


class TestRunTag:
    def test_batches_and_maps_by_kox_id(self, tmp_path: Path, small_records: list[dict[str, Any]]) -> None:
        recs = small_records[:25]
        prov = FakeProvider(echo_tag_responder(mismatch=True, severity="major"))
        out = run_tag({"fake": prov}, recs, Cache(tmp_path / "c.json"), Ledger(), workers=1)
        assert prov.n_calls == 3  # 25 条 / TAG_BATCH=12 → 3 批
        assert set(out) == {r["kox_id"] for r in recs}
        assert all(v["fake"]["mismatch"] is True for v in out.values())

    def test_is_idempotent(self, tmp_path: Path, small_records: list[dict[str, Any]]) -> None:
        recs = small_records[:12]
        prov = FakeProvider(echo_tag_responder())
        cache = Cache(tmp_path / "c.json")
        first = run_tag({"fake": prov}, recs, cache, Ledger(), workers=1)
        prov.calls.clear()
        second = run_tag({"fake": prov}, recs, cache, Ledger(), workers=1)
        assert prov.n_calls == 0 and first == second

    def test_failed_batch_leaves_a_coverage_hole(
        self, tmp_path: Path, small_records: list[dict[str, Any]]
    ) -> None:
        """失败批次的样本必须**缺席**，绝不能按"未错配"填默认值。

        填默认值会让 precision/recall 悄悄变好看（多数样本本来就是负例）。
        """
        recs = small_records[:24]
        state = {"n": 0}

        def responder(messages: list[dict[str, str]]) -> Any:
            state["n"] += 1
            if state["n"] == 1:
                return LLMError("fake", "第一批崩了")
            return echo_tag_responder()(messages)

        out = run_tag(
            {"fake": FakeProvider(responder)}, recs, Cache(tmp_path / "c.json"), Ledger(), workers=1
        )
        assert len(out) == 12, "失败批次被默认值填上了"
        assert {r["kox_id"] for r in recs[12:]} == set(out)

    def test_items_without_kox_id_are_dropped(
        self, tmp_path: Path, small_records: list[dict[str, Any]]
    ) -> None:
        """模型漏回填 id 的条目无法对齐到人，只能丢——但要以覆盖率下降的形式暴露。"""

        def responder(messages: list[dict[str, str]]) -> Any:
            items = echo_tag_responder()(messages)
            items[0].pop("kox_id")
            items[1]["kox_id"] = ""
            return items

        recs = small_records[:12]
        out = run_tag({"fake": FakeProvider(responder)}, recs, Cache(tmp_path / "c.json"), Ledger(), workers=1)
        assert len(out) == 10

    def test_short_array_is_not_padded(self, tmp_path: Path, small_records: list[dict[str, Any]]) -> None:
        def responder(messages: list[dict[str, str]]) -> Any:
            return echo_tag_responder()(messages)[:5]

        recs = small_records[:12]
        out = run_tag({"fake": FakeProvider(responder)}, recs, Cache(tmp_path / "c.json"), Ledger(), workers=1)
        assert len(out) == 5

    def test_dict_wrapped_array_is_unwrapped(
        self, tmp_path: Path, small_records: list[dict[str, Any]]
    ) -> None:
        """部分模型爱包一层 {"results": [...]}，这层必须剥掉而不是当失败。"""

        def responder(messages: list[dict[str, str]]) -> Any:
            return {"results": echo_tag_responder()(messages)}

        recs = small_records[:12]
        out = run_tag({"fake": FakeProvider(responder)}, recs, Cache(tmp_path / "c.json"), Ledger(), workers=1)
        assert len(out) == 12

    def test_two_models_are_recorded_side_by_side(
        self, tmp_path: Path, small_records: list[dict[str, Any]]
    ) -> None:
        recs = small_records[:12]
        providers = {
            "a": FakeProvider(echo_tag_responder(mismatch=True), name="a"),
            "b": FakeProvider(echo_tag_responder(mismatch=False), name="b"),
        }
        out = run_tag(providers, recs, Cache(tmp_path / "c.json"), Ledger(), workers=1)
        first = out[recs[0]["kox_id"]]
        assert first["a"]["mismatch"] is True and first["b"]["mismatch"] is False


class TestRunFit:
    def test_scores_per_brief(self, tmp_path: Path, small_records: list[dict[str, Any]]) -> None:
        pools = {"BRIEF-001": small_records[:12], "BRIEF-002": small_records[12:20]}
        specs = {"BRIEF-001": {"kpi": "reach"}, "BRIEF-002": {"kpi": "conversion"}}
        prov = FakeProvider(fit_responder(0.66))
        out = run_fit(prov, "fake", specs, pools, Cache(tmp_path / "c.json"), Ledger(), workers=1)
        assert set(out) == {"BRIEF-001", "BRIEF-002"}
        assert len(out["BRIEF-001"]) == 12 and len(out["BRIEF-002"]) == 8
        assert all(v["fit_score"] == 0.66 for v in out["BRIEF-001"].values())

    def test_empty_pool_yields_empty_dict_not_missing_key(self, tmp_path: Path) -> None:
        out = run_fit(
            FakeProvider(fit_responder()),
            "fake",
            {"BRIEF-001": {"kpi": "reach"}},
            {"BRIEF-001": []},
            Cache(tmp_path / "c.json"),
            Ledger(),
            workers=1,
        )
        assert out == {"BRIEF-001": {}}

    def test_same_candidates_under_different_specs_are_scored_twice(
        self, tmp_path: Path, small_records: list[dict[str, Any]]
    ) -> None:
        """不同 brief 的适配分不能共享缓存——同一个人对不同投放的适配度当然不同。"""
        prov = FakeProvider(fit_responder())
        cache = Cache(tmp_path / "c.json")
        pool = small_records[:12]
        run_fit(prov, "fake", {"A": {"kpi": "reach"}}, {"A": pool}, cache, Ledger(), workers=1)
        run_fit(prov, "fake", {"B": {"kpi": "conversion"}}, {"B": pool}, cache, Ledger(), workers=1)
        assert prov.n_calls == 2

    def test_failure_is_isolated(self, tmp_path: Path, small_records: list[dict[str, Any]]) -> None:
        prov = FakeProvider(lambda m: LLMError("fake", "崩了"))
        led = Ledger()
        out = run_fit(
            prov, "fake", {"A": {"kpi": "reach"}}, {"A": small_records[:12]}, Cache(tmp_path / "c.json"), led, workers=1
        )
        assert out == {"A": {}}
        assert led.summarize()["fit::fake"]["failed_calls"] == 1


# ===========================================================================
# 10. 候选池：不看答案、可复现
# ===========================================================================
class TestBuildPool:
    def test_is_deterministic_for_a_given_seed(self, small_records: list[dict[str, Any]]) -> None:
        spec = {"target_markets": ["US"], "platforms": ["tiktok"], "target_categories": ["beauty_care"]}
        a = build_pool(small_records, spec, 40, seed=7)
        b = build_pool(small_records, spec, 40, seed=7)
        assert [r["kox_id"] for r in a] == [r["kox_id"] for r in b]

    def test_seed_actually_changes_the_sample(self, small_records: list[dict[str, Any]]) -> None:
        spec = {"target_markets": ["US"], "platforms": ["tiktok"], "target_categories": ["beauty_care"]}
        a = {r["kox_id"] for r in build_pool(small_records, spec, 40, seed=1)}
        b = {r["kox_id"] for r in build_pool(small_records, spec, 40, seed=2)}
        assert a != b, "换种子结果不变，说明抽样其实没随机"

    def test_returns_sorted_unique_subset(self, small_records: list[dict[str, Any]]) -> None:
        pool = build_pool(small_records, {"target_markets": ["US"]}, 30, seed=3)
        ids = [r["kox_id"] for r in pool]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        assert set(ids) <= {r["kox_id"] for r in small_records}
        assert len(pool) == 30

    def test_prefers_records_that_match_the_spec(self, small_records: list[dict[str, Any]]) -> None:
        spec = {"target_markets": ["JP"], "platforms": ["youtube"], "target_categories": ["gaming_app"]}
        pool = build_pool(small_records, spec, 20, seed=5)
        hit = sum(
            1
            for r in pool
            if r.get("country") == "JP"
            or r.get("platform") == "youtube"
            or {"gaming_app"} & (set(r.get("declared_categories") or []) | set(r.get("observed_categories") or []))
        )
        assert hit == len(pool), "池子里混进了完全不相关的候选"

    def test_pool_is_independent_of_gt(self, small_records: list[dict[str, Any]]) -> None:
        """把答案全部篡改，池子必须一模一样——否则候选池本身就是泄漏通道。"""
        spec = {"target_markets": ["US"], "target_categories": ["beauty_care"]}
        before = [r["kox_id"] for r in build_pool(small_records, spec, 30, seed=9)]
        poisoned = copy.deepcopy(small_records)
        for r in poisoned:
            r["gt"] = {"is_fraud": True, "verdict": "reject", "tag_mismatch": True}
        after = [r["kox_id"] for r in build_pool(poisoned, spec, 30, seed=9)]
        assert before == after

    def test_falls_back_when_nothing_matches(self, small_records: list[dict[str, Any]]) -> None:
        """一个都匹配不上时要退回全库抽样，而不是返回空池（空池会让 fit 任务整体消失）。"""
        pool = build_pool(small_records, {"target_markets": ["ZZ"]}, 15, seed=4)
        assert len(pool) == 15


# ===========================================================================
# 11. 横评：规则 vs 模型，同一份 ground truth
# ===========================================================================
class TestRuleBaseline:
    @pytest.mark.parametrize(
        "declared,observed,expected",
        [
            (["beauty_care"], ["gaming_app"], True),            # Jaccard 0.0
            (["beauty_care", "fashion"], ["beauty_care"], False),  # 0.5 ≥ 0.34
            (["a", "b", "c"], ["a", "d", "e"], True),           # 1/5 = 0.2 < 0.34
            (["a", "b"], ["a", "c"], True),                     # 1/3 ≈ 0.333 < 0.34
            (["a"], ["a"], False),                              # 1.0
        ],
    )
    def test_jaccard_threshold_boundary(self, declared: list[str], observed: list[str], expected: bool) -> None:
        rec = {"declared_categories": declared, "observed_categories": observed}
        assert rule_tag_mismatch(rec) is expected

    def test_missing_side_is_not_a_mismatch(self) -> None:
        """信息不足时规则版**弃权**（判 false），与 prompt 里给模型的纪律一致。"""
        assert rule_tag_mismatch({"declared_categories": [], "observed_categories": ["x"]}) is False
        assert rule_tag_mismatch({"declared_categories": ["x"], "observed_categories": []}) is False
        assert rule_tag_mismatch({}) is False

    def test_threshold_is_a_parameter_not_a_magic_number(self) -> None:
        rec = {"declared_categories": ["a", "b"], "observed_categories": ["a", "c"]}
        assert rule_tag_mismatch(rec, threshold=0.3) is False
        assert rule_tag_mismatch(rec, threshold=0.4) is True


class TestBenchTag:
    def perfect(self, recs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """"作弊器"：直接抄 gt。用来验证指标**能**打到 1.0（所以 0 不是指标 bug）。"""
        return {
            r["kox_id"]: {"m": {"kox_id": r["kox_id"], "mismatch": bool((r["gt"] or {}).get("tag_mismatch"))}}
            for r in recs
        }

    def test_rule_arm_is_reproducible_by_hand(self, small_records: list[dict[str, Any]]) -> None:
        recs = small_records[:120]
        rep = bench_tag(recs, {}, [])
        arm = rep["arms"]["rule_jaccard"]
        tp = sum(1 for r in recs if (r["gt"] or {}).get("tag_mismatch") and rule_tag_mismatch(r))
        fp = sum(1 for r in recs if not (r["gt"] or {}).get("tag_mismatch") and rule_tag_mismatch(r))
        assert arm["confusion"]["tp"] == tp and arm["confusion"]["fp"] == fp
        assert arm["tokens"] == 0 and arm["coverage"] == 1.0
        assert rep["n_samples"] == len(recs)

    def test_metrics_are_recomputable_from_the_matrix(self, small_records: list[dict[str, Any]]) -> None:
        recs = small_records[:120]
        rep = bench_tag(recs, self.perfect(recs), ["m"])
        for arm in rep["arms"].values():
            c = arm["confusion"]
            p = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 0.0
            r = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 0.0
            assert arm["precision"] == pytest.approx(round(p, 4), abs=1e-4)
            assert arm["recall"] == pytest.approx(round(r, 4), abs=1e-4)

    def test_a_cheating_arm_can_reach_one(self, small_records: list[dict[str, Any]]) -> None:
        """抄答案的 arm F1 = 1.0。

        这条不是在夸作弊器，而是在证明"指标有能力打到满分"——
        于是后面那些 0.6~0.7 的真实数字不是被指标压住的。
        """
        recs = small_records[:120]
        arm = bench_tag(recs, self.perfect(recs), ["m"])["arms"]["m"]
        assert arm["f1"] == 1.0 and arm["coverage"] == 1.0

    def test_missing_predictions_lower_coverage_not_accuracy(
        self, small_records: list[dict[str, Any]]
    ) -> None:
        """漏判必须只体现在 coverage 上；把漏判当"判对"是最常见的洗指标手法。"""
        recs = small_records[:120]
        partial = self.perfect(recs)
        for kid in list(partial)[:60]:
            del partial[kid]
        arm = bench_tag(recs, partial, ["m"])["arms"]["m"]
        assert arm["n_judged"] == 60
        assert arm["coverage"] == pytest.approx(0.5, abs=1e-4)
        assert arm["accuracy"] == 1.0, "被判定的那 60 条本身是全对的"
        assert sum(arm["confusion"].values()) == 60

    def test_all_negative_arm_gets_zero_recall(self, small_records: list[dict[str, Any]]) -> None:
        recs = small_records[:120]
        out = {r["kox_id"]: {"m": {"kox_id": r["kox_id"], "mismatch": False}} for r in recs}
        arm = bench_tag(recs, out, ["m"])["arms"]["m"]
        assert arm["recall"] == 0.0 and arm["f1"] == 0.0
        assert arm["accuracy"] < 1.0, "全判负却拿到满分准确率 → 样本里没有正例，横评无意义"

    def test_only_the_mismatch_field_is_scored(self, small_records: list[dict[str, Any]]) -> None:
        """模型若在返回里夹带 ``gt`` 字段，评测必须**无视**它。

        评测口径只有两个来源：``record.gt``（答案）与 ``item.mismatch``（预测）。
        任何第三个来源都是作弊入口。
        """
        recs = small_records[:120]
        out = {
            r["kox_id"]: {
                "m": {
                    "kox_id": r["kox_id"],
                    "mismatch": False,
                    "gt": {"tag_mismatch": True},
                    "tag_mismatch": True,
                }
            }
            for r in recs
        }
        arm = bench_tag(recs, out, ["m"])["arms"]["m"]
        assert arm["recall"] == 0.0

    def test_unknown_model_key_yields_zero_coverage(self, small_records: list[dict[str, Any]]) -> None:
        rep = bench_tag(small_records[:50], {}, ["never_ran"])
        assert rep["arms"]["never_ran"]["coverage"] == 0.0
        assert rep["arms"]["never_ran"]["f1"] == 0.0

    def test_reports_do_not_mutate_inputs(self, small_records: list[dict[str, Any]]) -> None:
        recs = small_records[:50]
        snapshot = copy.deepcopy(recs)
        bench_tag(recs, self.perfect(recs), ["m"])
        assert recs == snapshot


class TestAdversaryCannotReadTheAnswerFromThePrompt:
    """终极一问：模型有没有可能"从题面里读到答案"？

    做法是造一个**专门想作弊**的假模型：它只要在 prompt 里看到任何与答案
    相关的字样，就照抄。跑完横评发现它一分也偷不到——因为 prompt 里确实没有答案。
    这比"我检查过 prompt 里没有 gt"更有说服力：它是端到端的反证。
    """

    def cheater(self) -> Callable[[list[dict[str, str]]], Any]:
        def responder(messages: list[dict[str, str]]) -> Any:
            blob = serialize(messages)
            leaked = any(f'"{field}"' in blob for field in GT_FIELDS)
            return [
                {
                    "kox_id": item["kox_id"],
                    "mismatch": leaked,
                    "confidence": 1.0,
                    "severity": "major" if leaked else "none",
                    "evidence": "从题面里抄的" if leaked else "题面里没有答案",
                }
                for item in user_payload(messages)
            ]

        return responder

    def test_cheater_gets_nothing(self, tmp_path: Path, small_records: list[dict[str, Any]]) -> None:
        recs = small_records[:120]
        prov = FakeProvider(self.cheater())
        out = run_tag({"cheat": prov}, recs, Cache(tmp_path / "c.json"), Ledger(), workers=1)
        arm = bench_tag(recs, out, ["cheat"])["arms"]["cheat"]
        assert arm["coverage"] == 1.0, "作弊器确实跑完了全部样本"
        assert arm["recall"] == 0.0, "题面里能读到答案 → prompt 泄漏了 gt"
        assert all(v["cheat"]["evidence"] == "题面里没有答案" for v in out.values())

    def test_the_same_cheater_wins_if_we_deliberately_leak(
        self, tmp_path: Path, small_records: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """阳性对照：故意把 gt 塞进 prompt，作弊器立刻满召回。

        没有这条，上面那条"作弊器拿不到分"可能只是因为作弊器本身写错了。
        """
        recs = [r for r in small_records[:200] if (r["gt"] or {}).get("tag_mismatch")][:8]
        assert recs, "小样本里没有错配正例"

        def leaky_messages(batch: list[dict[str, Any]]) -> list[dict[str, str]]:
            return [
                {"role": "system", "content": "system"},
                {
                    "role": "user",
                    "content": "共 N 个达人，请按顺序返回长度为 N 的 JSON 数组：\n"
                    + json.dumps(
                        [{"kox_id": r["kox_id"], "tag_mismatch": True} for r in batch], ensure_ascii=False
                    ),
                },
            ]

        monkeypatch.setattr(runner_mod, "tag_judge_messages", leaky_messages)
        prov = FakeProvider(self.cheater())
        out = run_tag({"cheat": prov}, recs, Cache(tmp_path / "c.json"), Ledger(), workers=1)
        arm = bench_tag(recs, out, ["cheat"])["arms"]["cheat"]
        assert arm["recall"] == 1.0


# ===========================================================================
# 12. promptbench：三版 prompt 的横评骨架
# ===========================================================================
def variant_aware_responder(
    answers: dict[str, bool]
) -> Callable[[list[dict[str, str]]], Any]:
    """按 system prompt 判断自己是哪一版，然后给出该版专属答案。

    用来验证"不同版本各走各的缓存"：若 key 撞了，某一版会吃到别版的答案。
    """

    def responder(messages: list[dict[str, str]]) -> Any:
        system = messages[0]["content"]
        version = next(v.version for v in TAG_VARIANTS if v.system == system)
        return [
            {
                "kox_id": item["kox_id"],
                "mismatch": answers[version],
                "confidence": 0.5,
                "severity": "major" if answers[version] else "none",
                "evidence": version,
            }
            for item in user_payload(messages)
        ]

    return responder


class TestPromptBenchArm:
    def test_metrics_are_recomputable(self, tmp_path: Path, small_records: list[dict[str, Any]]) -> None:
        recs = small_records[:36]
        prov = FakeProvider(echo_tag_responder(mismatch=True, severity="major", confidence=0.8))
        arm = pb_mod.run_arm(TAG_VARIANTS[2], prov, "fake", recs, Cache(tmp_path / "c.json"), workers=1)
        c = arm["confusion"]
        assert c["tp"] + c["fp"] + c["fn"] + c["tn"] == arm["n_covered"] == len(recs)
        assert c["fn"] == 0 and c["tn"] == 0, "全判错配 → 不应有 fn/tn"
        assert arm["precision"] == pytest.approx(round(c["tp"] / (c["tp"] + c["fp"]), 4), abs=1e-4)
        assert arm["recall"] == 1.0
        assert arm["avg_confidence"] == pytest.approx(0.8, abs=1e-4)
        assert arm["coverage"] == 1.0
        assert arm["calls"] == 3 and arm["failed_calls"] == 0

    def test_token_and_latency_accounting(self, tmp_path: Path, small_records: list[dict[str, Any]]) -> None:
        recs = small_records[:24]
        prov = FakeProvider(
            echo_tag_responder(), usage=Usage(prompt_tokens=1000, completion_tokens=100, reasoning_tokens=90)
        )
        arm = pb_mod.run_arm(TAG_VARIANTS[0], prov, "fake", recs, Cache(tmp_path / "c.json"), workers=1)
        assert arm["prompt_tokens"] == 2000 and arm["completion_tokens"] == 200
        assert arm["reasoning_tokens"] == 180 and arm["total_tokens"] == 2200
        assert arm["avg_latency_ms"] == 42.0

    def test_failed_batches_are_excluded_from_latency_and_coverage(
        self, tmp_path: Path, small_records: list[dict[str, Any]]
    ) -> None:
        recs = small_records[:24]
        state = {"n": 0}

        def responder(messages: list[dict[str, str]]) -> Any:
            state["n"] += 1
            if state["n"] == 1:
                return LLMError("fake", "崩")
            return echo_tag_responder()(messages)

        arm = pb_mod.run_arm(
            TAG_VARIANTS[0], FakeProvider(responder), "fake", recs, Cache(tmp_path / "c.json"), workers=1
        )
        assert arm["calls"] == 2 and arm["failed_calls"] == 1
        assert arm["n_covered"] == 12 and arm["coverage"] == 0.5
        assert arm["avg_latency_ms"] == 42.0, "失败批次不该被算进平均延迟"

    def test_severity_counter_only_counts_true_positives_side(
        self, tmp_path: Path, small_records: list[dict[str, Any]]
    ) -> None:
        """``major_severity_on_true_mismatch`` 只统计**真错配**上的 major，
        否则它会退化成"模型有多爱说 major"。
        """
        recs = small_records[:60]
        prov = FakeProvider(echo_tag_responder(mismatch=True, severity="major"))
        arm = pb_mod.run_arm(TAG_VARIANTS[2], prov, "fake", recs, Cache(tmp_path / "c.json"), workers=1)
        n_pos = sum(1 for r in recs if (r["gt"] or {}).get("tag_mismatch"))
        assert arm["major_severity_on_true_mismatch"] == n_pos

    def test_arm_is_cached(self, tmp_path: Path, small_records: list[dict[str, Any]]) -> None:
        recs = small_records[:24]
        prov = FakeProvider(echo_tag_responder())
        cache = Cache(tmp_path / "c.json")
        first = pb_mod.run_arm(TAG_VARIANTS[0], prov, "fake", recs, cache, workers=1)
        prov.calls.clear()
        second = pb_mod.run_arm(TAG_VARIANTS[0], prov, "fake", recs, cache, workers=1)
        assert prov.n_calls == 0
        assert {k: v for k, v in first.items() if k != "avg_latency_ms"} == {
            k: v for k, v in second.items() if k != "avg_latency_ms"
        }

    def test_variants_never_share_cached_answers(
        self, tmp_path: Path, small_records: list[dict[str, Any]]
    ) -> None:
        """回归：三版 prompt 共用一个缓存文件，但绝不许互相命中。

        撞 key 的后果是"Prompt 迭代带来提升"这个结论完全由缓存伪造出来，
        而产物里的 token 账、覆盖率、延迟都看不出任何异常。
        """
        recs = small_records[:24]
        cache = Cache(tmp_path / "c.json")
        prov = FakeProvider(variant_aware_responder({"v1": True, "v2": False, "v3": True}))
        arms = {
            v.version: pb_mod.run_arm(v, prov, "fake", recs, cache, workers=1) for v in TAG_VARIANTS
        }
        assert prov.n_calls == 6, "三版 × 2 批 = 6 次调用；少了就是串了缓存"
        assert arms["v1"]["recall"] == 1.0
        assert arms["v2"]["recall"] == 0.0, "v2 吃到了别版的答案"
        assert arms["v3"]["recall"] == 1.0

    def test_prf_handles_empty_denominators(self) -> None:
        assert pb_mod._prf(0, 0, 0, 0, 0) == {
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "accuracy": 0.0,
            "confusion": {"tp": 0, "fp": 0, "fn": 0, "tn": 0},
        }

    def test_batch_size_matches_the_production_task(self) -> None:
        """横评与正式 tag 任务必须同一个 batch 尺寸，否则"只改了 prompt"不成立。"""
        assert pb_mod.BATCH == runner_mod.TAG_BATCH


# ===========================================================================
# 13. 构建期产物的自洽性（只读，不重跑）
# ===========================================================================
LLM_BENCH = OUTPUT_DIR / "llm_bench.json"


@pytest.fixture(scope="module")
def llm_bench() -> dict[str, Any]:
    if not LLM_BENCH.exists():
        pytest.skip(f"缺少 {LLM_BENCH}")
    return json.loads(LLM_BENCH.read_text("utf-8"))


class TestLlmBenchProvenance:
    def test_totals_equal_the_sum_of_per_task(self, llm_bench: dict[str, Any]) -> None:
        per = llm_bench["per_task"].values()
        for field in ("calls", "failed_calls", "prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"):
            assert llm_bench["totals"][field] == sum(s[field] for s in per), field

    def test_each_slot_is_internally_consistent(self, llm_bench: dict[str, Any]) -> None:
        for key, slot in llm_bench["per_task"].items():
            assert slot["total_tokens"] == slot["prompt_tokens"] + slot["completion_tokens"], key
            assert slot["reasoning_tokens"] <= slot["completion_tokens"], (
                f"{key}: reasoning 已包含在 completion 里，不该更大"
            )
            assert slot["failed_calls"] <= slot["calls"]
            if slot["items"]:
                assert slot["tokens_per_item"] == pytest.approx(
                    round(slot["total_tokens"] / slot["items"], 2), abs=1e-2
                )

    def test_task_and_model_keys_are_well_formed(self, llm_bench: dict[str, Any]) -> None:
        models = set(llm_bench["models"])
        for key, slot in llm_bench["per_task"].items():
            task, _, model_key = key.partition("::")
            assert task in {"brief", "tag", "fit"}
            assert model_key in models
            assert slot["task"] == task and slot["model_key"] == model_key

    def test_primary_model_is_one_of_the_models(self, llm_bench: dict[str, Any]) -> None:
        assert llm_bench["primary_model_key"] in llm_bench["models"]

    def test_failures_are_disclosed_not_hidden(self, llm_bench: dict[str, Any]) -> None:
        """产物里必须能看见"有没有失败过"。真实构建里失败是常态，藏起来才是问题。"""
        assert "failed_calls" in llm_bench["totals"]
        assert llm_bench["totals"]["failed_calls"] >= 0

    def test_note_states_that_runtime_makes_no_model_calls(self, llm_bench: dict[str, Any]) -> None:
        assert "非估算" in llm_bench["note"]
        assert "不再调用" in llm_bench["note"]

    def test_tag_bench_arms_are_recomputable(self, llm_bench: dict[str, Any]) -> None:
        bench = llm_bench.get("tag_bench")
        if not bench:
            pytest.skip("产物里没有 tag_bench")
        for name, arm in bench["arms"].items():
            c = arm["confusion"]
            denom = c["tp"] + c["fp"]
            expected_p = round(c["tp"] / denom, 4) if denom else 0.0
            assert arm["precision"] == pytest.approx(expected_p, abs=1e-4), name
            covered = arm.get("n_judged", bench["n_samples"])
            assert sum(c.values()) == covered, name

    def test_rule_arm_costs_zero_tokens(self, llm_bench: dict[str, Any]) -> None:
        bench = llm_bench.get("tag_bench")
        if not bench:
            pytest.skip("产物里没有 tag_bench")
        assert bench["arms"]["rule_jaccard"]["tokens"] == 0
