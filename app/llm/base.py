from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    # Keep the raw JSON string so malformed arguments become a tool observation.
    arguments: str


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class LLMResponse(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=16)


class LLMError(Exception):
    """Provider failure with no provider payload or credentials attached."""


class BaseLLM(ABC):
    @abstractmethod
    async def generate(
        self, messages: list[Message], tools: list[ToolDefinition]
    ) -> LLMResponse:
        """Choose tool calls or produce a final answer using accumulated evidence."""
