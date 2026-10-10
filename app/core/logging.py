import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

request_id_context: ContextVar[str] = ContextVar("request_id", default="-")
logger = logging.getLogger("opspilot")

# Only operational metadata can enter application logs. Never serialize requests,
# provider bodies, headers, configuration objects, exception text or model output.
ALLOWED_FIELDS = {"request_id", "method", "route", "status_code", "step", "tool",
                  "latency_ms", "success", "code", "max_steps", "tool_call_count"}


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
