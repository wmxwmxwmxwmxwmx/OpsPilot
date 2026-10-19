import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base import Base
from app.db.config import MigrationSettings
from app.db.models import IncidentRow  # noqa: F401 -- register metadata
from app.workflow.models import WorkflowRow, MockActionRow  # noqa: F401

config = context.config
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    # Official saver migrations own these tables, never Alembic autogenerate.
    return not (type_ == "table" and name in {
        "checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations",
    })


def run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                      include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    settings = MigrationSettings()
    engine = create_async_engine(settings.migration_database_url.get_secret_value(),
                                 poolclass=pool.NullPool, echo=False, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=MigrationSettings().migration_database_url.get_secret_value(),
                      target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    # Allows isolated integration tests to migrate an explicitly supplied connection.
    connection = config.attributes.get("connection")
    if connection is not None:
        run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())
