"""Durable workflow metadata and controlled mock actions; saver owns its own DDL."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_workflows"
down_revision = "0001_incident_records"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("workflows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("initial_state", postgresql.JSONB(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("resumable", sa.Boolean(), nullable=False),
        sa.Column("session_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checkpoint_pruned", sa.Boolean(), nullable=False),
        sa.CheckConstraint("status IN ('created','running','pending_approval','failed','completed','cancelled')",
                           name="ck_workflow_status"),
        sa.CheckConstraint("octet_length(initial_state::text) <= 262144", name="ck_workflow_input_size"))
    op.create_table("workflow_mock_actions",
        sa.Column("operation_id", sa.String(64), primary_key=True),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workflows.id"), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")))


def downgrade():
    op.drop_table("workflow_mock_actions")
    op.drop_table("workflows")
