import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_context: ContextVar[str] = ContextVar("request_id", default="-")
logger = logging.getLogger("opspilot")

# Only operational metadata can enter application logs. Never serialize requests,
# provider bodies, headers, configuration objects, exception text or model output.
ALLOWED_FIELDS = {"request_id", "method", "route", "status_code", "step", "tool",
                  "latency_ms", "success", "code", "max_steps", "tool_call_count",
                  "incident_id", "session_committed"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        payload.update({key: value for key, value in getattr(record, "fields", {}).items()
                        if key in ALLOWED_FIELDS})
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # HTTP client INFO logs can include credential-bearing URLs from providers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def log_event(event: str, **fields: Any) -> None:
    logger.info(event, extra={"request_id": request_id_context.get(), "fields": fields})


class RequestLoggingMiddleware:
    """Keep request context alive until the body finishes or is disconnected."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        token = request_id_context.set(request_id)
        started = perf_counter()
        status_code = 500
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        log_event("request_start", method=scope["method"])
        try:
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                log_event("request_failure", code="internal_error")
                if response_started:
                    raise  # A second HTTP response is invalid once headers are sent.
                response = JSONResponse(status_code=500, content={"error": {
                    "code": "internal_error", "message": "服务暂时不可用。"
                }})
                await response(scope, receive, send_with_id)
        finally:
            log_event("request_finish", status_code=status_code,
                      route=getattr(scope.get("route"), "path", "unmatched"),
                      latency_ms=round((perf_counter() - started) * 1000, 3))
            request_id_context.reset(token)
