"""Real PostgreSQL only. Create/drop exclusively random databases owned by this fixture.

OPSPILOT_TEST_POSTGRES_URL must name a dedicated opspilot_test* bootstrap DB;
its role needs CREATEDB. No migration, TRUNCATE, or DROP touches that bootstrap DB.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models import IncidentRow
from app.db.session import create_database_engine
from app.incident.errors import IncidentError
from app.incident.repository import IncidentRepository
from app.incident.schemas import StepSummary, ToolSummary
from app.main import create_app

pytestmark = pytest.mark.postgres_integration
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


def values():
    now = datetime.now(timezone.utc)
    return dict(session_id=None, question="订单慢 password=hidden", answer="基于 Mock Data 的模拟诊断",
                step_summary=StepSummary(tools=(ToolSummary(name="get_system_metrics", success=True),),
                                         tool_count=1), duration_ms=23, created_at=now, finished_at=now)


def row_values():
    data = values()
    return dict(id=uuid4(), status="completed", **{**data, "step_summary": data["step_summary"].model_dump(mode="json")})


async def count(engine):
    async with engine.connect() as connection:
        return await connection.scalar(text("SELECT count(*) FROM incident_records"))


async def test_real_insert_select_jsonb_types_indexes_and_metadata(database):
    engine, _ = database
    repo = IncidentRepository(async_sessionmaker(engine, expire_on_commit=False))
    saved = await repo.create_completed(**values())
    assert await repo.get(saved.id) == saved
    assert await repo.get(uuid4()) is None
    assert saved.id.version == 4 and saved.finished_at.utcoffset() == timedelta(0)
    assert "hidden" not in saved.question
    async with engine.connect() as connection:
        types = (await connection.execute(text(
            "SELECT pg_typeof(id)::text, pg_typeof(created_at)::text, pg_typeof(step_summary)::text "
            "FROM incident_records"))).one()
        assert types == ("uuid", "timestamp with time zone", "jsonb")
        assert await connection.scalar(select(IncidentRow.step_summary["schema_version"].as_integer())) == 1
        assert await connection.scalar(select(IncidentRow.id).where(
            IncidentRow.step_summary.contains({"tools": [{"name": "get_system_metrics"}]}))) == saved.id
        indexes = await connection.run_sync(lambda c: inspect(c).get_indexes("incident_records"))
        assert {i["name"] for i in indexes} == {"ix_incident_created_at", "ix_incident_session_created"}
        await connection.run_sync(lambda c: migrate(c, "check"))
    assert engine.pool.checkedout() == 0


@pytest.mark.parametrize("field,value", [
    ("status", "failed"), ("question", ""), ("question", "q" * 4001), ("answer", "a" * 16001),
    ("answer", None), ("duration_ms", -1), ("finished_at", datetime(2000, 1, 1, tzinfo=timezone.utc)),
    ("step_summary", {}), ("step_summary", {"schema_version": 2, "tools": [], "tool_count": 0, "truncated": False}),
    ("step_summary", {"schema_version": 1, "tools": [], "tool_count": 0, "truncated": False, "large": "x" * 17000}),
])
async def test_real_constraints_rollback_entire_transaction(database, field, value):
    engine, _ = database
    valid, invalid = row_values(), row_values()
    invalid[field] = value
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(insert(IncidentRow).values(**valid))
            await connection.execute(insert(IncidentRow).values(**invalid))
    assert await count(engine) == 0  # The earlier valid INSERT rolls back too.
    repo = IncidentRepository(async_sessionmaker(engine, expire_on_commit=False))
    await repo.create_completed(**values())
    assert await count(engine) == 1 and engine.pool.checkedout() == 0


async def test_real_repository_flush_failure_rolls_back_and_connection_reusable(database, monkeypatch):
    engine, _ = database
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from sqlalchemy.ext.asyncio import AsyncSession
    original_flush = AsyncSession.flush
    async def flush_then_fail(self, *args, **kwargs):
        await original_flush(self, *args, **kwargs)
        raise OperationalError("INSERT", {}, Exception("credential must not escape"))
    with monkeypatch.context() as scoped:
        scoped.setattr(AsyncSession, "flush", flush_then_fail)
        with pytest.raises(IncidentError) as caught:
            await IncidentRepository(factory).create_completed(**values())
        assert caught.value.code == "incident_storage_error"
    assert await count(engine) == 0
    await IncidentRepository(factory).create_completed(**values())
    assert await count(engine) == 1 and engine.pool.checkedout() == 0


async def test_real_lost_commit_response_is_unknown_and_row_may_exist(database, monkeypatch):
    engine, _ = database
    repo = IncidentRepository(async_sessionmaker(engine, expire_on_commit=False))
    original = engine.sync_engine.dialect.do_commit
    def commit_then_lose_response(connection):
        original(connection)  # Actually committed to PostgreSQL.
        raise OperationalError("COMMIT", {}, OSError("lost acknowledgement"))
    with monkeypatch.context() as scoped:
        scoped.setattr(engine.sync_engine.dialect, "do_commit", commit_then_lose_response)
        with pytest.raises(IncidentError) as caught:
            await repo.create_completed(**values())
        assert caught.value.code == "incident_commit_unknown"
        incident_id = caught.value.incident_id
    assert await repo.get(incident_id) is not None
    assert await count(engine) == 1  # No automatic retry/duplicate execution.


async def test_real_cancel_during_insert_rolls_back_and_returns_connection(database, monkeypatch):
    engine, _ = database
    from sqlalchemy.ext.asyncio import AsyncSession
    original = AsyncSession.flush
    entered = asyncio.Event()
    async def flush_then_wait(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(AsyncSession, "flush", flush_then_wait)
    repo = IncidentRepository(async_sessionmaker(engine, expire_on_commit=False))
    task = asyncio.create_task(repo.create_completed(**values()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert engine.pool.checkedout() == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert engine.pool.checkedout() == 0 and await count(engine) == 0


async def test_real_cli_migration_downgrade_upgrade_and_current(database):
    engine, url = database
    env = {**os.environ, "MIGRATION_DATABASE_URL": url}
    async def cli(*args):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "alembic", *args, cwd=ROOT, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        assert proc.returncode == 0, (stdout + stderr).decode()
        return stdout.decode()
    await engine.dispose()  # No stale prepared statements across DDL changes.
    assert "0001_incident_records" in await cli("current")
    await cli("downgrade", "base")
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT to_regclass('incident_records')")) is None
    await engine.dispose()
    await cli("upgrade", "head")
    assert "0001_incident_records" in await cli("current")
    await IncidentRepository(async_sessionmaker(engine, expire_on_commit=False)).create_completed(**values())
    assert await count(engine) == 1


async def test_real_http_lifespan_pool_and_agent_does_not_borrow_connection(database, monkeypatch):
    engine, url = database
    from app.llm.mock import MockLLM
    class CheckedLLM(MockLLM):
        async def generate(self, messages, tools):
            assert engine.pool.checkedout() == 0
            return await super().generate(messages, tools)
    monkeypatch.setattr("app.main.create_database_engine", lambda url: engine)
    app = create_app(Settings(_env_file=None, incident_persistence_enabled=True, database_url=url), llm=CheckedLLM())
    closed = []
    event.listen(engine.sync_engine, "close", lambda *args: closed.append(True))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/diagnose", json={"message": "订单慢"})
            assert response.status_code == 200
            incident = await app.state.incidents.get(UUID(response.json()["incident_id"]))
            assert incident is not None and incident.step_summary.tool_count == 2
            response = await client.post("/api/v1/diagnose/stream", json={"message": "Redis慢"})
            assert "event: final" in response.text and '"incident_id"' in response.text
            assert engine.pool.checkedout() == 0
            assert (await client.get(f"/api/v1/incidents/{incident.id}")).status_code == 404
    assert closed and engine.pool.checkedout() == 0


@pytest.mark.redis_integration
async def test_real_redis_and_postgres_survive_session_expiry(database):
    redis_url = os.environ.get("OPSPILOT_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("Set OPSPILOT_TEST_REDIS_URL as well for the dual-store test")
    engine, url = database
    app = create_app(Settings(_env_file=None, incident_persistence_enabled=True, database_url=url,
                              session_enabled=True, redis_url=redis_url))
    async with app.router.lifespan_context(app):
        key = None
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                sid = UUID((await client.post("/api/v1/sessions")).json()["session_id"])
                key = app.state.sessions.key(sid)
                result = await client.post("/api/v1/diagnose", json={"message": "订单慢", "session_id": str(sid)})
                assert result.status_code == 200
                incident_id = UUID(result.json()["incident_id"])
                stream = await client.post("/api/v1/diagnose/stream", json={"message": "Redis呢", "session_id": str(sid)})
                assert "event: final" in stream.text and "event: error" not in stream.text
                assert (await app.state.sessions.load(sid)).version == 2
                await app.state.sessions.client.pexpire(key, 0)
                assert not await app.state.sessions.client.exists(key)
                assert (await app.state.incidents.get(incident_id)).session_id == sid
                assert await count(engine) == 2
        finally:
            if key:
                await app.state.sessions.client.delete(key)


async def test_real_unavailable_database_http_and_sse_are_safe(database):
    _, url = database
    # This database is deliberately absent; do not create or alter it.
    missing = make_url(url).set(database="opspilot_test_absent_" + uuid4().hex)
    app = create_app(Settings(_env_file=None, incident_persistence_enabled=True,
                              database_url=missing.render_as_string(hide_password=False)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/diagnose", json={"message": "订单慢"})
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "incident_storage_error"
            stream = await client.post("/api/v1/diagnose/stream", json={"message": "订单慢"})
            assert "event: error" in stream.text and "event: final" not in stream.text
            assert "incident_storage_error" in stream.text and missing.database not in stream.text


async def test_real_anyio_cancellation_releases_transaction(database, monkeypatch):
    import anyio
    from sqlalchemy.ext.asyncio import AsyncSession
    engine, _ = database
    entered = asyncio.Event()
    original = AsyncSession.flush
    async def flush(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(AsyncSession, "flush", flush)
    repo = IncidentRepository(async_sessionmaker(engine, expire_on_commit=False))
    async def write():
        await repo.create_completed(**values())
    async with anyio.create_task_group() as group:
        group.start_soon(write)
        await asyncio.wait_for(entered.wait(), 3)
        group.cancel_scope.cancel()
    assert engine.pool.checkedout() == 0
    assert await count(engine) == 0
