from datetime import timedelta
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator

from app.incident.schemas import MAX_SUMMARY_TOOLS, MAX_TOOL_COUNT, IncidentRecord, ToolName


class IncidentSummary(BaseModel):
    """A strict projection: no question, answer, session ID or observation fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always",
                              hide_input_in_errors=True)
    schema_version: Literal[1]
    incident_id: UUID4
    status: Literal["completed"]
    tool_count: int = Field(strict=True, ge=0, le=MAX_TOOL_COUNT)
    # Preserve order/repetitions of M3's first 64 calls, not unique tool names.
    tool_names: tuple[ToolName, ...] = Field(max_length=MAX_SUMMARY_TOOLS)
    truncated: bool = Field(strict=True)
    duration_ms: int = Field(strict=True, ge=0, le=9223372036854775807)
    created_at: AwareDatetime
    data_source: Literal["mock_tools"] = "mock_tools"

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Cache schema version must be an integer")
        return value

    @model_validator(mode="after")
    def consistent_summary(self) -> Self:
        if self.created_at.utcoffset() != timedelta(0):
            raise ValueError("Summary timestamps must be UTC")
        if len(self.tool_names) != min(self.tool_count, MAX_SUMMARY_TOOLS):
            raise ValueError("Inconsistent tool count")
        if self.truncated != (self.tool_count > len(self.tool_names)):
            raise ValueError("Inconsistent truncation flag")
        return self

    @classmethod
    def from_record(cls, record: IncidentRecord) -> "IncidentSummary":
        # Explicit allowlist. Never serialize the full ORM/record into Redis.
        return cls(schema_version=1, incident_id=record.id, status=record.status,
                   tool_count=record.step_summary.tool_count,
                   tool_names=tuple(tool.name for tool in record.step_summary.tools),
                   truncated=record.step_summary.truncated, duration_ms=record.duration_ms,
                   created_at=record.created_at)
