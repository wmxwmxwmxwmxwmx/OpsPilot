"""Shared isolated PostgreSQL fixture. Drops only its own random database."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import create_database_engine

ROOT = Path(__file__).resolve().parents[1]


def migrate(connection, direction="upgrade"):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    if direction == "upgrade":
        command.upgrade(config, "head")
    elif direction == "check":
        command.check(config)
    else:
        command.downgrade(config, "base")


@pytest.fixture
async def database():
    url = os.environ.get("OPSPILOT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set OPSPILOT_TEST_POSTGRES_URL for isolated real PostgreSQL tests")
    parsed = make_url(url)
    if parsed.drivername != "postgresql+asyncpg" or not (parsed.database or "").startswith("opspilot_test"):
        pytest.fail("PostgreSQL integration URL must name a dedicated opspilot_test* database")
    # PostgreSQL CREATE DATABASE cannot run inside a transaction. This setting is
    # only for fixture provisioning; repository INSERTs always use begin/commit.
    admin = create_async_engine(parsed, isolation_level="AUTOCOMMIT", hide_parameters=True)
    name = "opspilot_test_m3_" + uuid4().hex
    owned = False
    engine = None
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
            owned = True
        test_url = parsed.set(database=name).render_as_string(hide_password=False)
        engine = create_database_engine(test_url)
        async with engine.begin() as connection:
            await connection.run_sync(migrate)
        yield engine, test_url
    finally:
        if engine is not None:
            await engine.dispose()
        if owned:
            async with admin.connect() as connection:
                # Only the UUID database created above, never the caller's DB.
                await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()

