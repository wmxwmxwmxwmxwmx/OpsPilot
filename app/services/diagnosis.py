from collections.abc import AsyncIterator
from contextlib import aclosing
from uuid import UUID

from app.agent.events import AgentEvent, FinalAnswerEvent
from app.agent.loop import Agent
from app.agent.schemas import DiagnoseResponse
from app.session.errors import SessionError
from app.session.repository import SessionRepository


class DiagnosisService:
    """Coordinate one agent execution with an optional conversation commit."""

    def __init__(self, agent: Agent, sessions: SessionRepository | None = None) -> None:
        self.agent = agent
        self.sessions = sessions

    def require_sessions(self) -> SessionRepository:
        if self.sessions is None:
            raise SessionError("session_disabled")
        return self.sessions

    async def run(self, message: str, session_id: UUID | None = None) -> DiagnoseResponse:
        if session_id is None:
            return await self.agent.run(message)
        sessions = self.require_sessions()
        snapshot = await sessions.load(session_id)
        result = await self.agent.run(message, history=snapshot.messages)
        await sessions.commit(snapshot, message, result.answer)
        return result

    async def stream(self, message: str, session_id: UUID | None = None) -> AsyncIterator[AgentEvent]:
        sessions = self.require_sessions() if session_id is not None else None
        snapshot = await sessions.load(session_id) if sessions is not None else None
        async with aclosing(self.agent.stream(
            message, history=snapshot.messages if snapshot is not None else None,
        )) as events:
            async for event in events:
                if isinstance(event, FinalAnswerEvent) and snapshot is not None and sessions is not None:
                    # Do not expose success before durable session acceptance.
                    await sessions.commit(snapshot, message, event.answer)
                yield event
