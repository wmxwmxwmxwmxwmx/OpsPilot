from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WorkflowRow(Base):
    __tablename__ = "workflows"
    __table_args__ = (
        CheckConstraint("status IN ('created','running','pending_approval','failed','completed','cancelled')",
                        name="ck_workflow_status"),
        CheckConstraint("octet_length(initial_state::text) <= 262144", name="ck_workflow_input_size"),
    )
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    initial_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    resumable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    session_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checkpoint_pruned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class MockActionRow(Base):
    __tablename__ = "workflow_mock_actions"
    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[UUID] = mapped_column(ForeignKey("workflows.id"), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                               server_default=text("CURRENT_TIMESTAMP"))
