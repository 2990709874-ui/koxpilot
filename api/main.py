"""KOXPilot HTTP 服务入口。

对外接口严格按 ``api/CONTRACT.md``（v1，冻结）：

    GET  /api/health
    POST /api/plan
    GET  /api/kox/{kox_id}/explain
    POST /api/gate/batch

三条与脚手架不同的决定，都是契约明文要求的：

1. **默认公开可访问**（契约 §0.3）。脚手架自带的 JWT 中间件会让每个请求都去换
   userinfo，外部评审拿不到这个头，接口对他们等于不存在。所以默认不鉴权；
   需要时用 ``REQUIRE_JWT=1`` 把原来那套鉴权原样开回来（代码保留，未删除）。
2. **CORS 全开**（契约 §1）。静态站与服务不同域，没有 CORS 前端连 health 都探不到。
3. **业务错误一律 HTTP 200 + ``ok:false``**（契约 §1）。只有真崩溃才 5xx，
   并且崩溃也被兜成 ``ok:false`` + ``code:"internal"``，让前端只需判断一次 ``ok``。

另外：进程一起来就**后台预热**数据集（约 30MB JSON）。预热期间 health 如实返回
``status:"warming"``，这样平台探活不会因为 800ms 超时而反复杀实例。
"""

import logging
import os
import time
from typing import Any, Dict, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from koxpilot_service.service import ServiceError, explain, gate_batch, plan
from koxpilot_service.store import STORE, health_payload

LOGGER = logging.getLogger(__name__)
JWT_HEADER = "x-jwt-token"
DEFAULT_JWT_SERVER = "cloud.bytedance.net"

#: 契约要求默认公开；置 1/true/yes 时恢复脚手架的 JWT 鉴权
REQUIRE_JWT = os.environ.get("REQUIRE_JWT", "").strip().lower() in ("1", "true", "yes")


class UserResponse(BaseModel):
    username: str
    region: str
    name: str
    avatar_url: str
    terminated: bool


def get_jwt_userinfo_url() -> str:
    configured_url = os.environ.get("JWT_USERINFO_URL")
    if configured_url:
        return configured_url

    jwt_server = os.environ.get("JWT_SERVER", DEFAULT_JWT_SERVER).strip().rstrip("/")
    return f"https://{jwt_server}/auth/api/v1/userinfo"


async def fetch_user_info(token: str) -> Optional[UserResponse]:
    timeout_seconds = float(os.environ.get("JWT_USERINFO_TIMEOUT_SECONDS", "5"))

    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
        ) as client:
            response = await client.get(
                get_jwt_userinfo_url(),
                headers={JWT_HEADER: token},
            )
            response.raise_for_status()
            return UserResponse.parse_obj(response.json())
    except Exception as error:
        LOGGER.warning("Unable to resolve current user from JWT: %s", error)
        return None


async def auth_middleware(request: Request, call_next):
    """Require a verified ByteCloud user for every handler."""

    token = request.headers.get(JWT_HEADER)
    if not token:
        return JSONResponse(
            status_code=403,
            content={"detail": "unauthorized: missing or invalid jwt token"},
        )

    current_user = await fetch_user_info(token)
    if current_user is None:
        return JSONResponse(
            status_code=403,
            content={"detail": "unauthorized: missing or invalid jwt token"},
        )

    request.state.current_user = current_user
    return await call_next(request)


def setup_permissions(app: FastAPI) -> None:
    """Wire the permission system into the FastAPI app.

    契约 §0.3 要求默认公开可访问，所以这里默认**不挂**鉴权中间件；
    ``REQUIRE_JWT=1`` 时挂回去（鉴权实现本身一行没改）。
    """

    if REQUIRE_JWT:
        app.middleware("http")(auth_middleware)
        LOGGER.info("JWT auth enabled (REQUIRE_JWT=1)")
    else:
        LOGGER.info("JWT auth disabled: public access per api/CONTRACT.md")


def setup_cors(app: FastAPI) -> None:
    """契约 §1：允许所有来源 / POST,GET,OPTIONS / Content-Type 头。"""

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,  # 与 allow_origins=["*"] 并用时必须为 False
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Accept", JWT_HEADER],
        max_age=86400,
    )


# ---------------------------------------------------------------------------
# 响应装配
# ---------------------------------------------------------------------------
def _with_meta(payload: Dict[str, Any], started: float) -> Dict[str, Any]:
    payload["meta"] = STORE.meta((time.time() - started) * 1000.0)
    return payload


def _error(code: str, message: str, started: float) -> JSONResponse:
    """契约 §1/§7：业务错误也走 HTTP 200，前端只据 ``ok`` 判断。"""
    body = _with_meta({"ok": False, "error": {"code": code, "message": message}}, started)
    return JSONResponse(status_code=200, content=body)


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise ServiceError("bad_request", "请求体不是合法 JSON。")
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ServiceError("bad_request", "请求体必须是一个 JSON 对象。")
    return body


def _run(handler, started: float) -> JSONResponse:
    """统一的异常收敛：业务错误 -> ok:false；未预期异常 -> 记日志 + ok:false(internal)。"""
    try:
        payload = handler()
    except ServiceError as exc:
        return _error(exc.code, exc.message, started)
    except Exception as exc:  # pragma: no cover - 兜底，绝不把栈抛给前端
        LOGGER.exception("unhandled error")
        return _error("internal", "服务内部错误：%s: %s" % (type(exc).__name__, exc), started)
    return JSONResponse(status_code=200, content=_with_meta(payload, started))


def register_routes(app: FastAPI) -> None:
    """Register all application routes on the FastAPI app."""

    @app.get("/api")
    def index_handler():
        return {
            "ok": True,
            "service": "KOXPilot HTTP API",
            "contract": "api/CONTRACT.md (v1)",
            "endpoints": [
                "GET /api/health",
                "POST /api/plan",
                "GET /api/kox/{kox_id}/explain",
                "POST /api/gate/batch",
            ],
        }

    @app.get("/api/v1/ping")
    async def ping_handler():
        return "Ping healthcheck"

    @app.get("/api/v1/user", response_model=UserResponse)
    async def get_current_user(request: Request):
        user = getattr(request.state, "current_user", None)
        if user is None:
            raise HTTPException(
                status_code=403,
                detail="unauthorized: missing or invalid jwt token",
            )

        return user

    # -- 契约端点 -----------------------------------------------------------
    @app.get("/api/health")
    def health_handler():
        """契约 §3：**不等数据**，如实报 ready / warming / failed。"""
        payload, _ = health_payload()
        return JSONResponse(status_code=200, content=payload)

    @app.post("/api/plan")
    async def plan_handler(request: Request):
        started = time.time()
        try:
            body = await _json_body(request)
        except ServiceError as exc:
            return _error(exc.code, exc.message, started)
        return _run(lambda: plan(body), started)

    @app.get("/api/kox/{kox_id}/explain")
    def explain_handler(kox_id: str):
        started = time.time()
        return _run(lambda: explain(kox_id), started)

    @app.post("/api/gate/batch")
    async def gate_batch_handler(request: Request):
        started = time.time()
        try:
            body = await _json_body(request)
        except ServiceError as exc:
            return _error(exc.code, exc.message, started)
        return _run(lambda: gate_batch(body), started)


app = FastAPI(
    title="KOXPilot HTTP API",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

setup_cors(app)
setup_permissions(app)
register_routes(app)


@app.on_event("startup")
def _warmup() -> None:
    """进程起来立刻后台加载数据集：health 不阻塞，业务接口少等一次冷启动。"""
    STORE.start()


# ---------------------------DO NOT EDIT CODE BELOW THIS LINE---------------------------------
# This is the entry point for the FastAPI application.
if __name__ == "__main__":
    port = int(os.environ.get("_BYTEFAAS_RUNTIME_PORT", 8000))
    config = uvicorn.Config("main:app", port=port, log_level="info", host=None)
    server = uvicorn.Server(config)
    server.run()
# --------------------------------------------------------------------------------------------
