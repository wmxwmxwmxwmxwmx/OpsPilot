"""Completed diagnosis records (not confirmed production incidents)."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_incident_records"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Keep this revision self-contained; never import mutable application models.
    op.create_table(
        "incident_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("step_summary", postgresql.JSONB(), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status = 'completed'", name="ck_incident_status"),
        sa.CheckConstraint("char_length(question) BETWEEN 1 AND 4000", name="ck_incident_question"),
        sa.CheckConstraint("char_length(answer) BETWEEN 1 AND 16000", name="ck_incident_answer"),
        sa.CheckConstraint("duration_ms >= 0", name="ck_incident_duration"),
        sa.CheckConstraint("finished_at >= created_at", name="ck_incident_times"),
        sa.CheckConstraint(
            "(jsonb_typeof(step_summary) = 'object' AND "
            "step_summary->'schema_version' = '1'::jsonb AND "
            "jsonb_typeof(step_summary->'tools') = 'array' AND "
            "jsonb_typeof(step_summary->'tool_count') = 'number' AND "
            "jsonb_typeof(step_summary->'truncated') = 'boolean') IS TRUE",
            name="ck_incident_summary_shape",
        ),
        sa.CheckConstraint("octet_length(step_summary::text) <= 16384", name="ck_incident_summary_size"),
        sa.CheckConstraint(
            "octet_length(question) + octet_length(answer) + "
            "octet_length(step_summary::text) <= 131072", name="ck_incident_payload_size",
        ),
    )
    op.create_index("ix_incident_created_at", "incident_records", ["created_at"])
    op.create_index("ix_incident_session_created", "incident_records", ["session_id", "created_at"],
                    postgresql_where=sa.text("session_id IS NOT NULL"))


def downgrade():
    # Destructive: run only on a disposable database or with a reviewed backup plan.
    op.drop_index("ix_incident_session_created", table_name="incident_records")
    op.drop_index("ix_incident_created_at", table_name="incident_records")
    op.drop_table("incident_records")
