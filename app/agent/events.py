"""Transport-independent execution events; tool events reuse the public steps."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, UUID4

from app.agent.schemas import ToolCallStep, ToolResultStep


class AgentStartEvent(BaseModel):
    type: Literal["agent_start"] = "agent_start"
    max_steps: int


class FinalAnswerEvent(BaseModel):
    type: Literal["final"] = "final"
    answer: str
    incident_id: UUID4 | None = None


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    incident_id: UUID4 | None = None
    session_committed: bool | None = None


AgentEvent = Annotated[
    AgentStartEvent | ToolCallStep | ToolResultStep | FinalAnswerEvent | ErrorEvent,
    Field(discriminator="type"),
]
