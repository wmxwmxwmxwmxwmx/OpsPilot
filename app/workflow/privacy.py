import json
import math

from app.session.privacy import redact_content
from app.workflow.errors import WorkflowError


class CheckpointPrivacy:
    def __init__(self, secrets: tuple[str, ...] = ()):
        self.secrets = secrets

    def text(self, value: str, limit: int = 4000) -> str:
        return redact_content(value, self.secrets).replace("\x00", "")[:limit]

    def result(self, tool, raw: dict) -> dict:
        # Default deny: a newly registered tool persists no arbitrary result fields.
        if not isinstance(raw, dict):
            raise WorkflowError("invalid_tool_result")
        safe = {key: raw[key] for key in tool.checkpoint_fields if key in raw}
        def number(value):
            if type(value) not in {int, float} or not math.isfinite(value):
                raise WorkflowError("invalid_tool_result")
            return value
        if tool.name in {"get_system_metrics", "get_redis_metrics"}:
            safe = {key: number(value) for key, value in safe.items()}
        if "slow_queries" in safe:
            safe["slow_queries"] = [
                {"sql": "[SQL omitted]", **{k: number(row[k]) for k in
                 ("duration_ms", "rows_examined", "rows_returned") if k in row}}
                for row in safe["slow_queries"][:10]
            ]
        def clean(value):
            if isinstance(value, str):
                return self.text(value, 1000)
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, list):
                return [clean(v) for v in value[:16]]
            return value
        safe = clean(safe)
        if len(json.dumps(safe, allow_nan=False).encode()) > 8192:
            raise WorkflowError("tool_result_too_large")
        return safe
