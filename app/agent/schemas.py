from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class DiagnoseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Annotated[str, StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=4000
    )]


class ToolCallStep(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    tool: str
    call_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultStep(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool: str
    call_id: str
    success: bool
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None


Step = Annotated[ToolCallStep | ToolResultStep, Field(discriminator="type")]


class DiagnoseResponse(BaseModel):
    answer: str
    steps: list[Step]


class AgentExecutionError(Exception):
    def __init__(self, code: str, message: str, steps: list[Step]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.steps = list(steps)
