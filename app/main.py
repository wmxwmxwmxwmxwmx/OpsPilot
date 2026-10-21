from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.loop import Agent
from app.agent.schemas import AgentExecutionError
from app.api.diagnose import router as diagnose_router
from app.api.health import router as health_router
from app.api.sessions import router as sessions_router
from app.api.workflows import router as workflows_router
from app.cache.repository import CacheRepository
from app.core.config import Settings
from app.core.logging import RequestLoggingMiddleware, configure_logging, log_event
from app.db.session import create_database_engine
from app.incident.errors import IncidentError
from app.incident.repository import IncidentRepository
from app.llm.base import BaseLLM
from app.llm.mock import MockLLM
from app.llm.openai_compatible import OpenAICompatibleLLM
from app.session.errors import SessionError
from app.session.repository import SessionRepository
from app.services.incident_query import IncidentQueryService
from app.tools.mysql_diagnostics import MySQLDiagnosticsTool
from app.tools.redis_diagnostics import RedisDiagnosticsTool
from app.tools.registry import ToolRegistry
from app.tools.system_metrics import SystemMetricsTool
from app.workflow.errors import WorkflowError


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
        from app.observability.telemetry import Telemetry
        from app.observability.tracing import create_provider
        provider = create_provider(config) if config.tracing_enabled else None
        application.state.telemetry = Telemetry(metrics=config.metrics_enabled, provider=provider)
        application.state.telemetry_settings = config
        async with httpx.AsyncClient(follow_redirects=False) as client:
            model = llm if llm is not None else (
                MockLLM() if config.llm_mode == "mock" else OpenAICompatibleLLM(config, client)
            )
            application.state.agent = Agent(
                model, build_registry(), max_steps=config.max_steps,
                llm_timeout_seconds=config.llm_timeout_seconds,
                tool_timeout_seconds=config.tool_timeout_seconds,
            )
            redis_client = None
            engine = None
            application.state.sessions = None
            application.state.incidents = None
            application.state.incident_cache = None
            application.state.incident_queries = None
            application.state.workflows = None
            workflow_stack = AsyncExitStack()
            try:
                if config.session_enabled or config.cache_enabled:
                    # One lazy application client/pool for the same Redis URL.
                    redis_url = config.redis_url.get_secret_value()
                    redis_client = Redis.from_url(
                        redis_url, decode_responses=False, socket_connect_timeout=2,
                        socket_timeout=2, max_connections=20, retry=Retry(NoBackoff(), 0),
                    )
                if config.session_enabled:
                    application.state.sessions = SessionRepository(
                        redis_client, ttl_seconds=config.session_ttl_seconds,
                        max_turns=config.session_max_turns,
                        max_message_chars=config.session_max_message_chars,
                        max_bytes=config.session_max_bytes,
                        secrets=(config.openai_api_key.get_secret_value(),
                                 unquote(urlsplit(redis_url).password or "")),
                    )
                if config.incident_persistence_enabled:
                    database_url = config.database_url.get_secret_value()
                    engine = create_database_engine(database_url)
                    application.state.incidents = IncidentRepository(
                        async_sessionmaker(engine, expire_on_commit=False),
                        secrets=(config.openai_api_key.get_secret_value(), database_url,
                                 make_url(database_url).password or "",
                                 config.redis_url.get_secret_value(),
                                 unquote(urlsplit(config.redis_url.get_secret_value()).password or "")),
                    )
                if config.cache_enabled:
                    application.state.incident_cache = CacheRepository(
                        redis_client, ttl_seconds=config.cache_ttl_seconds,
                        key_prefix=config.cache_key_prefix, max_bytes=config.cache_max_bytes,
                        io_timeout_seconds=config.cache_io_timeout_seconds,
                    )
                if application.state.incidents is not None:
                    application.state.incident_queries = IncidentQueryService(
                        application.state.incidents, application.state.incident_cache,
                    )
                retriever = None
                if config.rag_enabled:
                    from app.knowledge.embeddings import build_embedding
                    from app.knowledge.repository import RunbookRepository
                    from app.knowledge.retriever import Retriever
                    if engine is None:
                        engine = create_database_engine(config.database_url.get_secret_value())
                    retriever = Retriever(RunbookRepository(engine), build_embedding(config, client),
                        top_k=config.rag_top_k, max_chars=config.rag_max_context_chars,
                        min_score=config.rag_min_score, timeout=config.rag_timeout_seconds)
                mcp = None
                if config.mcp_enabled:
                    from app.mcp.client import MCPClient
                    mcp = MCPClient(config.mcp_server_url, config.mcp_timeout_seconds)
                if config.workflow_enabled:
                    from app.workflow.runtime import workflow_service
                    if engine is None:
                        engine = create_database_engine(config.database_url.get_secret_value())
                    application.state.workflows = await workflow_stack.enter_async_context(
                        workflow_service(config, engine, model if config.llm_mode != "mock" or llm else None,
                                         application.state.sessions, retriever=retriever, mcp=mcp))
                    application.state.workflow_token = config.workflow_api_token.get_secret_value()
                yield
            finally:
                if provider is not None:
                    import asyncio
                    await asyncio.to_thread(provider.shutdown)
                try:
                    await workflow_stack.aclose()
                finally:
                    try:
                        if engine is not None:
                            await engine.dispose()
                    finally:
                        if redis_client is not None:
                            await redis_client.aclose()

    application = FastAPI(title="OpsPilot", version="0.4.0", lifespan=lifespan)
    application.include_router(health_router)
    application.include_router(diagnose_router)
    application.include_router(sessions_router)
    application.include_router(workflows_router)

    @application.exception_handler(WorkflowError)
    async def workflow_error_handler(request: Request, error: WorkflowError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={"error": {
            "code": error.code, "message": error.code,
        }})

    @application.exception_handler(IncidentError)
    async def incident_error_handler(request: Request, error: IncidentError) -> JSONResponse:
        content = {"code": error.code, "message": error.message,
                   "session_committed": error.session_committed}
        if error.incident_id is not None:
            content["incident_id"] = str(error.incident_id)
        status = 413 if error.code == "incident_too_large" else 503
        return JSONResponse(status_code=status, content={"error": content})

    @application.exception_handler(SessionError)
    async def session_error_handler(request: Request, error: SessionError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={"error": {
            "code": error.code, "message": error.message,
        }})

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
        session_error = any("session_id" in item["loc"] for item in error.errors())
        message = ("请求格式错误；session_id 必须为服务器生成的 UUID4。" if session_error else
                   "请求格式错误；message 必须为 1–4000 字符的非空字符串。")
        if request.scope.get("path") == "/api/v1/sessions":
            message = "创建会话无需参数，不接受客户端指定的会话标识或其他字段。"
        if request.scope.get("path", "").startswith("/api/v1/workflows"):
            message = "工作流请求格式错误；请检查 UUID、message 和审批 decision，不接受 GraphState。"
        return JSONResponse(status_code=422, content={"error": {
            "code": "invalid_request", "message": message,
        }})

    from app.observability.http import ObservabilityMiddleware
    application.add_middleware(ObservabilityMiddleware)
    application.add_middleware(RequestLoggingMiddleware)

    return application


app = create_app()
