from app.observability.telemetry import observed
import asyncio
from datetime import datetime
from uuid import UUID, uuid4

from asyncpg import PostgresError
from pydantic import ValidationError
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import log_event
from app.db.models import IncidentRow
from app.db.session import database_session
from app.incident.errors import IncidentError
from app.incident.schemas import IncidentRecord, StepSummary
from app.session.privacy import redact_content


class IncidentRepository:
    """One short transaction per operation, no shared AsyncSession or retry."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], *,
                 secrets: tuple[str, ...] = ()) -> None:
        self._sessions = sessions
        self._secrets = secrets

    @observed("persistence", "incident")
    async def create_completed(
        self, *, session_id: UUID | None, question: str, answer: str,
        step_summary: StepSummary, duration_ms: int,
        created_at: datetime, finished_at: datetime,
    ) -> IncidentRecord:
        incident_id = uuid4()
        if len(question) > 4000 or len(answer) > 16000:
            raise IncidentError("incident_too_large", incident_id=incident_id)
        try:
            record = IncidentRecord(
                id=incident_id, session_id=session_id,
                question=redact_content(question, self._secrets),
                answer=redact_content(answer, self._secrets),
                step_summary=step_summary, duration_ms=duration_ms,
                created_at=created_at, finished_at=finished_at,
            )
        except (ValidationError, ValueError):
            raise IncidentError("incident_data_error", incident_id=incident_id) from None

        commit_started = False
        try:
            async with database_session(self._sessions) as session:
                async with session.begin():
                    row = IncidentRow(**record.model_dump(exclude={"step_summary"}),
                                      step_summary=record.step_summary.model_dump(mode="json"))
                    session.add(row)
                    await session.flush()  # INSERT and constraints; still uncommitted.
                    # A transport failure from here can lose the COMMIT response.
                    commit_started = True
                # Exiting begin() committed; do not await LLM/tools in this block.
        except (IntegrityError, DataError):
            raise IncidentError("incident_data_error", incident_id=incident_id) from None
        except (SQLAlchemyError, PostgresError, OSError, TimeoutError):
            code = "incident_commit_unknown" if commit_started else "incident_storage_error"
            raise IncidentError(code, incident_id=incident_id) from None
        except asyncio.CancelledError:
            log_event("incident_cancelled", incident_id=str(incident_id),
                      code="incident_commit_unknown" if commit_started else "incident_not_committed")
            raise
        log_event("incident_committed", incident_id=str(incident_id))
        return record

    async def get(self, incident_id: UUID) -> IncidentRecord | None:
        try:
            async with database_session(self._sessions) as session:
                async with session.begin():
                    row = await session.get(IncidentRow, incident_id)
                    return IncidentRecord.model_validate(row) if row is not None else None
        except (ValidationError, ValueError):
            raise IncidentError("incident_data_error", incident_id=incident_id) from None
        except (SQLAlchemyError, PostgresError, OSError, TimeoutError):
            raise IncidentError("incident_storage_error", incident_id=incident_id) from None
