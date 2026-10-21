from datetime import timedelta
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, UUID4, model_validator

MAX_SUMMARY_TOOLS = 64
MAX_TOOL_COUNT = 1600  # MAX_STEPS <= 100, <= 16 tool calls per LLM response.
MAX_SUMMARY_BYTES = 16384
MAX_RECORD_BYTES = 131072

ToolName = Literal[
    "get_system_metrics", "get_mysql_slow_queries", "get_redis_metrics", "unregistered_tool",
    "propose_mock_remediation",
    "mcp_get_mock_system_metrics", "mcp_get_mock_redis_metrics", "mcp_get_mock_mysql_status",
]
ToolErrorCode = Literal[
    "tool_not_found", "invalid_tool_arguments", "tool_timeout", "tool_execution_error",
    "approval_rejected",
]


class StoredModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always",
                              from_attributes=True, hide_input_in_errors=True)


class ToolSummary(StoredModel):
    name: ToolName
    success: bool = Field(strict=True)
    error_code: ToolErrorCode | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        if self.success != (self.error_code is None):
            raise ValueError("Inconsistent tool outcome")
        return self


class StepSummary(StoredModel):
    schema_version: Literal[1] = 1
    tools: tuple[ToolSummary, ...] = Field(default=(), max_length=MAX_SUMMARY_TOOLS)
    tool_count: int = Field(default=0, strict=True, ge=0, le=MAX_TOOL_COUNT)
    truncated: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def consistent_count(self) -> Self:
        if len(self.tools) != min(self.tool_count, MAX_SUMMARY_TOOLS):
            raise ValueError("Inconsistent summary count")
        if self.truncated != (self.tool_count > len(self.tools)):
            raise ValueError("Inconsistent truncation flag")
        if len(self.model_dump_json().encode()) > MAX_SUMMARY_BYTES:
            raise ValueError("Oversized summary")
        return self


class IncidentRecord(StoredModel):
    id: UUID4
    session_id: UUID4 | None = None
    # M3 stores only completed executions, never in-progress/failed/cancelled rows.
    status: Literal["completed"] = "completed"
    question: str = Field(strict=True, min_length=1, max_length=4000)
    answer: str = Field(strict=True, min_length=1, max_length=16000)
    step_summary: StepSummary
    duration_ms: int = Field(strict=True, ge=0, le=9223372036854775807)
    created_at: AwareDatetime
    finished_at: AwareDatetime

    @model_validator(mode="after")
    def consistent_record(self) -> Self:
        if any(t.utcoffset() != timedelta(0) for t in (self.created_at, self.finished_at)):
            raise ValueError("Incident timestamps must be UTC")
        if self.finished_at < self.created_at:
            raise ValueError("Invalid execution timestamps")
        if "\x00" in self.question or "\x00" in self.answer:
            raise ValueError("PostgreSQL text cannot contain NUL")
        if len(self.model_dump_json().encode()) > MAX_RECORD_BYTES:
            raise ValueError("Oversized incident")
        return self
