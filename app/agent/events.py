"""Transport-independent execution events; tool events reuse the public steps."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.agent.schemas import ToolCallStep, ToolResultStep


class AgentStartEvent(BaseModel):
    type: Literal["agent_start"] = "agent_start"
    max_steps: int


class FinalAnswerEvent(BaseModel):
    type: Literal["final"] = "final"
    answer: str


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


AgentEvent = Annotated[
    AgentStartEvent | ToolCallStep | ToolResultStep | FinalAnswerEvent | ErrorEvent,
    Field(discriminator="type"),
]
