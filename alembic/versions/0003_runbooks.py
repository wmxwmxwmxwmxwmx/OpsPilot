"""Versioned Runbooks; extension installation is an explicit operator action.

Revision ID: 0003_runbooks
Revises: 0002_workflows
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

revision = '0003_runbooks'
down_revision = '0002_workflows'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if not connection.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')")):
        raise RuntimeError('pgvector is not enabled; have an authorized administrator CREATE EXTENSION vector before migration')
    op.create_table('runbook_documents',
        sa.Column('document_id', sa.String(128), primary_key=True),
        sa.Column('source_path', sa.Text, nullable=False),
        sa.Column('title', sa.Text, nullable=False),
        sa.Column('version', sa.String(64), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('embedding_model', sa.String(256), nullable=False),
        sa.Column('dimensions', sa.Integer, nullable=False),
        sa.Column('metadata', JSONB, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table('runbook_chunks',
        sa.Column('chunk_id', sa.String(64), primary_key=True),
        sa.Column('document_id', sa.String(128), sa.ForeignKey('runbook_documents.document_id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_path', sa.Text, nullable=False),
        sa.Column('title', sa.Text, nullable=False),
        sa.Column('section', sa.Text, nullable=False),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('metadata', JSONB, nullable=False),
        sa.Column('embedding', Vector(), nullable=False),
        sa.Column('embedding_model', sa.String(256), nullable=False),
        sa.Column('dimensions', sa.Integer, nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('version', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('vector_dims(embedding) = dimensions', name='ck_runbook_vector_dimensions'))
    op.create_index('ix_runbook_chunks_document_id', 'runbook_chunks', ['document_id'])


def downgrade():
    op.drop_table('runbook_chunks')
    op.drop_table('runbook_documents')
    # Never remove a shared extension.
