from datetime import timedelta
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, UUID4, model_validator


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")
    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=32000)]


class ConversationSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")
    session_id: UUID4
    version: int = Field(default=0, strict=True, ge=0)
    messages: tuple[ConversationTurn, ...] = Field(default=(), max_length=200)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        if len(self.messages) % 2 or any(
            turn.role != ("user" if index % 2 == 0 else "assistant")
            for index, turn in enumerate(self.messages)
        ):
            raise ValueError("History must contain complete user/assistant pairs")
        if any(t.utcoffset() != timedelta(0) for t in (self.created_at, self.updated_at)):
            raise ValueError("Session timestamps must be UTC")
        if self.updated_at < self.created_at or self.version < len(self.messages) // 2:
            raise ValueError("Inconsistent session metadata")
        return self


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSessionResponse(BaseModel):
    session_id: UUID4
    expires_in_seconds: int
