import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import datetime, timezone
from time import perf_counter
from uuid import UUID

from app.agent.events import AgentEvent, FinalAnswerEvent
from app.agent.loop import Agent
from app.agent.schemas import DiagnoseResponse, ToolResultStep
from app.core.logging import log_event
from app.incident.errors import IncidentError
from app.incident.repository import IncidentRepository
from app.incident.summary import SummaryBuilder
from app.session.errors import SessionError
from app.session.repository import SessionRepository
from app.session.schemas import ConversationSession


class DiagnosisService:
    """One execution; Redis acceptance then optional PostgreSQL persistence."""

    def __init__(self, agent: Agent, sessions: SessionRepository | None = None,
                 incidents: IncidentRepository | None = None) -> None:
        self.agent = agent
        self.sessions = sessions
        self.incidents = incidents

    def require_sessions(self) -> SessionRepository:
        if self.sessions is None:
            raise SessionError("session_disabled")
        return self.sessions

    async def _complete(
        self, snapshot: ConversationSession | None, message: str, answer: str,
        summary: SummaryBuilder, created_at: datetime, started: float,
    ) -> UUID | None:
        finished_at = max(datetime.now(timezone.utc), created_at)
        duration_ms = max(0, int((perf_counter() - started) * 1000))
        session_committed = False
        if snapshot is not None:
            await self.require_sessions().commit(snapshot, message, answer)
            session_committed = True
        if self.incidents is None:
            return None
        try:
            incident = await self.incidents.create_completed(
                session_id=snapshot.session_id if snapshot else None,
                question=message, answer=answer, step_summary=summary.build(),
                created_at=created_at, finished_at=finished_at, duration_ms=duration_ms,
            )
        except IncidentError as error:
            failure = error.after_session_commit() if session_committed else error
            log_event("incident_failure", code=failure.code,
                      incident_id=str(failure.incident_id) if failure.incident_id else None,
                      session_committed=session_committed)
            raise failure from None
        except asyncio.CancelledError:
            # COMMIT may have reached PostgreSQL. Never promise rollback or retry.
            log_event("diagnosis_persistence_cancelled", code="incident_commit_unknown",
                      session_committed=session_committed)
            raise
        return incident.id

    async def run(self, message: str, session_id: UUID | None = None) -> DiagnoseResponse:
        snapshot = await self.require_sessions().load(session_id) if session_id is not None else None
        created_at, started = datetime.now(timezone.utc), perf_counter()
        result = (await self.agent.run(message, history=snapshot.messages) if snapshot is not None
                  else await self.agent.run(message))
        summary = SummaryBuilder()
        if self.incidents is not None:
            for step in result.steps:
                if isinstance(step, ToolResultStep):
                    summary.add(step)
        incident_id = await self._complete(snapshot, message, result.answer, summary, created_at, started)
        return result.model_copy(update={"incident_id": incident_id}) if incident_id else result

    async def stream(self, message: str, session_id: UUID | None = None) -> AsyncIterator[AgentEvent]:
        snapshot = await self.require_sessions().load(session_id) if session_id is not None else None
        created_at, started = datetime.now(timezone.utc), perf_counter()
        summary = SummaryBuilder()
        async with aclosing(self.agent.stream(
            message, history=snapshot.messages if snapshot is not None else None,
        )) as events:
            async for event in events:
                if self.incidents is not None and isinstance(event, ToolResultStep):
                    summary.add(event)
                if isinstance(event, FinalAnswerEvent):
                    incident_id = await self._complete(
                        snapshot, message, event.answer, summary, created_at, started,
                    )
                    if incident_id is not None:
                        event = event.model_copy(update={"incident_id": incident_id})
                yield event
