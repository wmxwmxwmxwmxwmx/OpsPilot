"""Workflow mutex + business commits. Never holds a row transaction over LLM IO."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

import anyio
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.agent.schemas import ToolResultStep
from app.db.models import IncidentRow
from app.incident.schemas import IncidentRecord
from app.incident.summary import SummaryBuilder
from app.workflow.errors import WorkflowError
from app.workflow.models import MockActionRow, WorkflowRow


def now():
    return datetime.now(timezone.utc)


class WorkflowRepository:
    def __init__(self, engine):
        self.engine = engine

    @asynccontextmanager
    async def lock(self, workflow_id: str):
        # Session-level advisory lock on a dedicated connection. No lease expiry
        # can admit a second live owner; process/socket exit releases the lock.
        key = int.from_bytes(UUID(workflow_id).bytes[:8], "big", signed=True)
        connection = await self.engine.connect()
        locked = False
        try:
            locked = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            await connection.commit()
            if not locked:
                raise WorkflowError("workflow_busy")
            yield
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    if locked:
                        await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                        await connection.commit()
                except BaseException:
                    await connection.invalidate()
                    raise
                finally:
                    await connection.close()

    async def create(self, state: dict):
        async with self.engine.begin() as connection:
            await connection.execute(insert(WorkflowRow).values(
                id=UUID(state["workflow_id"]), incident_id=UUID(state["incident_id"]),
                status="created", initial_state=state, error_code=None, resumable=True,
                session_status="pending" if state["session_id"] else "not_requested",
                created_at=now(), updated_at=now(), checkpoint_pruned=False))

    async def get(self, workflow_id: str) -> dict:
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(WorkflowRow.__table__).where(
                WorkflowRow.id == UUID(workflow_id)))).mappings().one_or_none()
        if row is None:
            raise WorkflowError("workflow_not_found", status_code=404)
        return dict(row)

    async def update(self, workflow_id: str, **values):
        async with self.engine.begin() as connection:
            await connection.execute(update(WorkflowRow).where(WorkflowRow.id == UUID(workflow_id))
                                     .values(**values, updated_at=now()))

    async def record_action(self, workflow_id: str, operation_id: str, arguments: dict):
        result = {"operation_id": operation_id, "simulated": True, **arguments}
        async with self.engine.begin() as connection:
            await connection.execute(insert(MockActionRow).values(operation_id=operation_id,
                workflow_id=UUID(workflow_id), arguments=arguments, result=result).on_conflict_do_nothing())
            row = (await connection.execute(select(MockActionRow.__table__).where(
                MockActionRow.operation_id == operation_id))).mappings().one()
            if row["workflow_id"] != UUID(workflow_id) or row["arguments"] != arguments:
                raise WorkflowError("operation_conflict")
            return row["result"]

    async def complete(self, workflow_id: str, state: dict, session_status: str):
        summary = SummaryBuilder()
        for result in state["tool_results"]:
            summary.add(ToolResultStep.model_validate(result))
        async with self.engine.begin() as connection:
            row = (await connection.execute(select(WorkflowRow.__table__).where(
                WorkflowRow.id == UUID(workflow_id)).with_for_update())).mappings().one()
            if row["status"] == "completed":
                return
            finished = max(now(), row["created_at"])
            record = IncidentRecord(id=row["incident_id"], session_id=state["session_id"],
                question=state["user_message"], answer=state["final_answer"], step_summary=summary.build(),
                created_at=row["created_at"], finished_at=finished,
                duration_ms=int((finished - row["created_at"]).total_seconds() * 1000))
            await connection.execute(insert(IncidentRow).values(
                **record.model_dump(exclude={"step_summary"}), step_summary=summary.build().model_dump(mode="json"))
                .on_conflict_do_nothing(index_elements=[IncidentRow.id]))
            await connection.execute(update(WorkflowRow).where(WorkflowRow.id == UUID(workflow_id)).values(
                status="completed", error_code=None, resumable=False, session_status=session_status,
                initial_state={}, updated_at=finished))

    async def expired_completed(self, days: int):
        async with self.engine.connect() as connection:
            return (await connection.execute(select(WorkflowRow.id).where(
                WorkflowRow.status == "completed", WorkflowRow.checkpoint_pruned.is_(False),
                WorkflowRow.updated_at < now() - timedelta(days=days)).limit(1000))).scalars().all()
