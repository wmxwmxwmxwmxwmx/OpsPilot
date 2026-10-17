from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IncidentRow(Base):
    __tablename__ = "incident_records"
    __table_args__ = (
        CheckConstraint("status = 'completed'", name="ck_incident_status"),
        CheckConstraint("char_length(question) BETWEEN 1 AND 4000", name="ck_incident_question"),
        CheckConstraint("char_length(answer) BETWEEN 1 AND 16000", name="ck_incident_answer"),
        CheckConstraint("duration_ms >= 0", name="ck_incident_duration"),
        CheckConstraint("finished_at >= created_at", name="ck_incident_times"),
        CheckConstraint(
            "(jsonb_typeof(step_summary) = 'object' AND "
            "step_summary->'schema_version' = '1'::jsonb AND "
            "jsonb_typeof(step_summary->'tools') = 'array' AND "
            "jsonb_typeof(step_summary->'tool_count') = 'number' AND "
            "jsonb_typeof(step_summary->'truncated') = 'boolean') IS TRUE",
            name="ck_incident_summary_shape",
        ),
        CheckConstraint("octet_length(step_summary::text) <= 16384", name="ck_incident_summary_size"),
        CheckConstraint(
            "octet_length(question) + octet_length(answer) + "
            "octet_length(step_summary::text) <= 131072", name="ck_incident_payload_size",
        ),
        Index("ix_incident_created_at", "created_at"),
        Index("ix_incident_session_created", "session_id", "created_at",
              postgresql_where=text("session_id IS NOT NULL")),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    # Deliberately no FK: Redis expiry must never remove durable incidents.
    session_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    step_summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
