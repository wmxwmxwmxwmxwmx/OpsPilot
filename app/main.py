from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.agent.loop import Agent
from app.agent.schemas import AgentExecutionError
from app.api.diagnose import router as diagnose_router
from app.api.health import router as health_router
from app.core.config import Settings
from app.core.logging import configure_logging, log_event, request_id_context
from app.llm.base import BaseLLM
from app.llm.mock import MockLLM
from app.llm.openai_compatible import OpenAICompatibleLLM
from app.tools.mysql_diagnostics import MySQLDiagnosticsTool
from app.tools.redis_diagnostics import RedisDiagnosticsTool
from app.tools.registry import ToolRegistry
from app.tools.system_metrics import SystemMetricsTool


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (SystemMetricsTool(), MySQLDiagnosticsTool(), RedisDiagnosticsTool()):
        registry.register(tool)
    return registry


def create_app(settings: Settings | None = None, *, llm: BaseLLM | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        config = settings if settings is not None else Settings()
        configure_logging()
        async with httpx.AsyncClient(follow_redirects=False) as client:
            model = llm if llm is not None else (
                MockLLM() if config.llm_mode == "mock" else OpenAICompatibleLLM(config, client)
            )
            application.state.agent = Agent(
                model, build_registry(), max_steps=config.max_steps,
                llm_timeout_seconds=config.llm_timeout_seconds,
                tool_timeout_seconds=config.tool_timeout_seconds,
            )
            yield

    application = FastAPI(title="OpsPilot", version="0.1.0", lifespan=lifespan)
    application.include_router(health_router)
    application.include_router(diagnose_router)

    @application.exception_handler(AgentExecutionError)
    async def agent_error_handler(request: Request, error: AgentExecutionError) -> JSONResponse:
        status = {"max_steps_exceeded": 503, "llm_timeout": 504}.get(error.code, 502)
        return JSONResponse(status_code=status, content={
            "error": {"code": error.code, "message": error.message},
            "steps": [step.model_dump(exclude_none=True) for step in error.steps],
        })

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, error: RequestValidationError) -> JSONResponse:
        log_event("request_validation_error", code="invalid_request")
        return JSONResponse(status_code=422, content={"error": {
            "code": "invalid_request", "message": "请求格式错误；message 必须为 1–4000 字符的非空字符串。"
        }})

    @application.middleware("http")
    async def observe_request(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = uuid4().hex
        token = request_id_context.set(request_id)
        started = perf_counter()
        status_code = 500
        log_event("request_start", method=request.method)
        try:
            try:
                response = await call_next(request)
            except Exception:
                log_event("request_failure", code="internal_error")
                response = JSONResponse(status_code=500, content={"error": {
                    "code": "internal_error", "message": "服务暂时不可用。"
                }})
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            # Log the matched route template, not arbitrary paths or query strings.
            route = request.scope.get("route")
            log_event("request_finish", status_code=status_code,
                      route=getattr(route, "path", "unmatched"),
                      latency_ms=round((perf_counter() - started) * 1000, 3))
            request_id_context.reset(token)

    return application


app = create_app()
