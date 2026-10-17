import asyncio
import io
import json
import logging
from contextlib import aclosing, asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import anyio
import fakeredis.aioredis
import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError

from app.agent.schemas import ToolResultStep
from app.api.diagnose import stream_diagnosis
from app.api.sse import SSEResponse
from app.core.config import Settings
from app.core.logging import JsonFormatter, logger
from app.db.config import MigrationSettings
from app.db.models import IncidentRow
from app.incident.errors import IncidentError
from app.incident.repository import IncidentRepository
from app.incident.schemas import IncidentRecord, MAX_RECORD_BYTES, MAX_SUMMARY_BYTES, StepSummary
from app.incident.summary import SummaryBuilder
from app.llm.base import BaseLLM, LLMResponse
from app.llm.mock import MockLLM
from app.main import create_app
from app.services.diagnosis import DiagnosisService
from app.session.errors import SessionError
from app.session.repository import SessionRepository


def completed_values(**changes):
    now = datetime.now(timezone.utc)
    return dict(session_id=None, question="订单慢", answer="模拟诊断完成", step_summary=StepSummary(),
                duration_ms=10, created_at=now, finished_at=now, **changes)


def record_values():
    return dict(id=uuid4(), **completed_values())


@pytest.mark.parametrize("field,value", [
    ("id", UUID(int=0)), ("status", "failed"), ("question", ""), ("answer", "a" * 16001),
    ("duration_ms", -1), ("duration_ms", True), ("duration_ms", 2**63),
    ("created_at", datetime(2026, 1, 1)),
    ("created_at", datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8)))),
    ("finished_at", datetime(2000, 1, 1, tzinfo=timezone.utc)), ("question", "nul\x00"),
    ("step_summary", {"schema_version": 2}), ("step_summary", {"tool_count": 1}),
    ("step_summary", {"raw_observation": "secret"}),
])
def test_incident_rejects_invalid_data(field, value):
    values = record_values()
    values[field] = value
    with pytest.raises(ValidationError):
        IncidentRecord(**values)


def test_record_serializes_uuid_utc_and_bounded_metadata():
    record = IncidentRecord(**record_values())
    assert record.id.version == 4 and record.created_at.utcoffset() == timedelta(0)
    assert IncidentRecord.model_validate_json(record.model_dump_json()) == record
    builder = SummaryBuilder()
    for i in range(1600):
        builder.add(ToolResultStep(tool="password=unknown-tool-secret", call_id="secret-id",
                                  success=False, result={"raw": "secret-observation"},
                                  error={"code": "secret-code", "message": "secret-message"}))
    summary = builder.build()
    assert summary.tool_count == 1600 and len(summary.tools) == 64 and summary.truncated
    assert summary.tools[0].name == "unregistered_tool"
    assert summary.tools[0].error_code == "tool_execution_error"
    payload = summary.model_dump_json()
    assert "secret" not in payload and "result" not in payload and "call_id" not in payload
    assert len(payload.encode()) <= MAX_SUMMARY_BYTES
    values = record_values()
    values.update(question="中" * 4000, answer="中" * 16000, step_summary=summary)
    assert len(IncidentRecord(**values).model_dump_json().encode()) <= MAX_RECORD_BYTES
    builder.add(ToolResultStep(tool="get_system_metrics", call_id="x", success=True))
    with pytest.raises(ValidationError):
        builder.build()


def mock_db():
    session = MagicMock()
    session.flush = AsyncMock()
    session.close = AsyncMock()
    session.get = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock()
    transaction.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = transaction
    return MagicMock(return_value=session), session, transaction


async def test_repository_redacts_and_converts_without_leaking_orm():
    factory, session, _ = mock_db()
    repo = IncidentRepository(factory, secrets=("configured-secret", "postgresql+asyncpg://u:db-pass@db/app"))
    values = completed_values()
    values.update(question="configured-secret password=hidden sk-testkey12345",
                  answer="Bearer abc.def postgresql+asyncpg://u:db-pass@db/app")
    saved = await repo.create_completed(**values)
    assert isinstance(saved, IncidentRecord) and saved.id.version == 4
    row = session.add.call_args.args[0]
    assert isinstance(row, IncidentRow) and row.step_summary["schema_version"] == 1
    assert all(s not in saved.model_dump_json() for s in ["configured-secret", "hidden", "db-pass", "abc.def", "testkey"])
    session.get.return_value = row
    assert await repo.get(saved.id) == saved
    session.get.return_value = None
    assert await repo.get(uuid4()) is None
    assert factory.call_count == 3 and session.close.await_count == 3


@pytest.mark.parametrize("phase,exception,code", [
    ("flush", OperationalError("insert", {}, Exception("db-password")), "incident_storage_error"),
    ("flush", IntegrityError("insert", {}, Exception("db-password")), "incident_data_error"),
    ("commit", OperationalError("commit", {}, Exception("db-password")), "incident_commit_unknown"),
    ("commit", IntegrityError("commit", {}, Exception("db-password")), "incident_data_error"),
    ("flush", OSError("db-password"), "incident_storage_error"),
    ("commit", TimeoutError("db-password"), "incident_commit_unknown"),
])
async def test_repository_error_phase_mapping_and_cleanup(phase, exception, code):
    factory, session, transaction = mock_db()
    if phase == "flush":
        session.flush.side_effect = exception
    else:
        transaction.__aexit__.side_effect = exception
    with pytest.raises(IncidentError) as caught:
        await IncidentRepository(factory).create_completed(**completed_values())
    assert caught.value.code == code and caught.value.incident_id.version == 4
    assert "db-password" not in str(caught.value)
    session.close.assert_awaited_once()
    if phase == "flush":
        assert transaction.__aexit__.call_args.args[0] is type(exception)


async def test_repository_rejects_large_or_corrupt_data_without_opening_session():
    factory, session, _ = mock_db()
    repo = IncidentRepository(factory)
    values = completed_values()
    values["answer"] = "a" * 16001
    with pytest.raises(IncidentError, match="大小") as caught:
        await repo.create_completed(**values)
    assert caught.value.code == "incident_too_large"
    factory.assert_not_called()
    session.get.return_value = IncidentRow(**{**record_values(), "step_summary": {"schema_version": 3}})
    with pytest.raises(IncidentError) as caught:
        await repo.get(uuid4())
    assert caught.value.code == "incident_data_error"


async def test_repository_anyio_cancel_closes_session_without_committing():
    factory, session, transaction = mock_db()
    entered, cleaned = asyncio.Event(), asyncio.Event()
    async def flush():
        entered.set()
        await asyncio.Event().wait()
    async def close():
        await anyio.lowlevel.checkpoint()
        cleaned.set()
    session.flush.side_effect, session.close.side_effect = flush, close
    async with anyio.create_task_group() as group:
        async def write():
            await IncidentRepository(factory).create_completed(**completed_values())
        group.start_soon(write)
        await entered.wait()
        group.cancel_scope.cancel()
    assert cleaned.is_set()
    assert issubclass(transaction.__aexit__.call_args.args[0], asyncio.CancelledError)


class CapturingLLM(MockLLM):
    def __init__(self):
        self.histories = []

    async def generate(self, messages, tools):
        self.histories.append([m.model_copy(deep=True) for m in messages])
        return await super().generate(messages, tools)


@asynccontextmanager
async def incident_app(llm=None):
    factory, session, _ = mock_db()
    incidents = IncidentRepository(factory)
    async with fakeredis.aioredis.FakeRedis() as redis:
        sessions = SessionRepository(redis)
        app = create_app(Settings(_env_file=None), llm=llm)
        async with app.router.lifespan_context(app):
            app.state.sessions, app.state.incidents = sessions, incidents
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                yield client, app, session


def terminal(response):
    frames = [dict(type=f.splitlines()[0][7:], **json.loads(f.splitlines()[1][6:]))
              for f in response.text.strip().split("\n\n")]
    assert sum(f["type"] in {"final", "error"} for f in frames) == 1
    return frames[-1]


@pytest.mark.parametrize("stateful", [False, True])
async def test_json_and_sse_save_with_history_and_no_repeat(stateful):
    llm = CapturingLLM()
    async with incident_app(llm) as (client, app, session):
        sid = (await app.state.sessions.create()).session_id if stateful else None
        for path in ["/api/v1/diagnose", "/api/v1/diagnose/stream"]:
            response = await client.post(path, json={"message": "订单慢", "session_id": str(sid) if sid else None})
            assert response.status_code == 200
            body = terminal(response) if path.endswith("stream") else response.json()
            assert UUID(body["incident_id"]).version == 4
            row = session.add.call_args.args[0]
            assert str(row.id) == body["incident_id"] and row.session_id == sid
            assert row.step_summary["tool_count"] == 2
            assert all(t["success"] for t in row.step_summary["tools"])
        assert session.add.call_count == 2 and len(llm.histories) == 6
        if stateful:
            assert [m.role for m in llm.histories[3]] == ["system", "user", "assistant", "user"]
            assert (await app.state.sessions.load(sid)).version == 2


@pytest.mark.parametrize("code", ["incident_storage_error", "incident_commit_unknown"])
@pytest.mark.parametrize("stateful", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_persistence_failure_never_returns_success_or_retries(code, stateful, stream, monkeypatch):
    llm = CapturingLLM()
    async with incident_app(llm) as (client, app, _):
        sid = (await app.state.sessions.create()).session_id if stateful else None
        incident_id = uuid4()
        create = AsyncMock(side_effect=IncidentError(code, incident_id=incident_id))
        monkeypatch.setattr(app.state.incidents, "create_completed", create)
        response = await client.post("/api/v1/diagnose" + ("/stream" if stream else ""),
                                     json={"message": "订单慢", "session_id": str(sid) if sid else None})
        error = terminal(response) if stream else response.json()["error"]
        expected = (("incident_partial_unknown" if code.endswith("unknown") else "incident_partial_failure")
                    if stateful else code)
        assert error["code"] == expected and error["session_committed"] is stateful
        assert error["incident_id"] == str(incident_id)
        assert "answer" not in error and response.status_code == (200 if stream else 503)
        assert len(llm.histories) == 3 and create.await_count == 1
        if stateful:
            assert (await app.state.sessions.load(sid)).version == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_redis_conflict_prevents_postgres_write(stream, monkeypatch):
    async with incident_app() as (client, app, session):
        sid = (await app.state.sessions.create()).session_id
        monkeypatch.setattr(app.state.sessions, "commit", AsyncMock(side_effect=SessionError("session_conflict")))
        response = await client.post("/api/v1/diagnose" + ("/stream" if stream else ""),
                                     json={"message": "订单慢", "session_id": str(sid)})
        error = terminal(response) if stream else response.json()["error"]
        assert error["code"] == "session_conflict"
        session.add.assert_not_called()


async def test_sse_tools_flow_before_both_commits_and_final_waits(monkeypatch):
    async with incident_app() as (_, app, session):
        sid = (await app.state.sessions.create()).session_id
        entered, release = asyncio.Event(), asyncio.Event()
        original = app.state.incidents.create_completed
        async def gated(**kwargs):
            assert (await app.state.sessions.load(sid)).version == 1
            entered.set()
            await release.wait()
            return await original(**kwargs)
        monkeypatch.setattr(app.state.incidents, "create_completed", gated)
        emitted = []
        async def consume():
            service = DiagnosisService(app.state.agent, app.state.sessions, app.state.incidents)
            async with aclosing(service.stream("订单慢", sid)) as events:
                async for event in events:
                    emitted.append(event.type)
                    if event.type == "final":
                        session.flush.assert_awaited_once()
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert emitted == ["agent_start", "tool_call", "tool_result", "tool_call", "tool_result"]
            session.add.assert_not_called()
            release.set()
            await asyncio.wait_for(task, 2)
            assert emitted[-1] == "final"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["agent_failed", "llm_cancel", "postgres_cancel"])
async def test_failed_or_cancelled_execution_never_emits_final(phase, monkeypatch):
    entered = asyncio.Event()
    class LLM(BaseLLM):
        async def generate(self, messages, tools):
            if phase == "agent_failed":
                raise RuntimeError("secret-provider-error")
            if phase == "llm_cancel":
                entered.set()
                await asyncio.Event().wait()
            return LLMResponse(content="done")
    async with incident_app(LLM()) as (client, app, session):
        if phase == "agent_failed":
            for path in ["/api/v1/diagnose", "/api/v1/diagnose/stream"]:
                response = await client.post(path, json={"message": "hi"})
                assert "llm_error" in response.text
            session.add.assert_not_called()
            return
        if phase == "postgres_cancel":
            async def flush():
                entered.set()
                await asyncio.Event().wait()
            session.flush.side_effect = flush
        service = DiagnosisService(app.state.agent, app.state.sessions, app.state.incidents)
        response = SSEResponse(stream_diagnosis(service, "hi"))
        sent = []
        async def receive():
            await entered.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            sent.append(message)
        await asyncio.wait_for(response({"type": "http"}, receive, send), 2)
        body = b"".join(m.get("body", b"") for m in sent)
        assert b"event: final" not in body and b"event: error" not in body
        if phase == "llm_cancel":
            session.add.assert_not_called()
        else:
            session.close.assert_awaited_once()


@pytest.mark.parametrize("url", ["", "postgresql://user:secret@host/db", "sqlite+aiosqlite:///db", "postgresql+asyncpg://host", "postgresql+asyncpg://u:secret@host:99999/db"])
def test_enabled_postgres_requires_safe_async_url(url):
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, incident_persistence_enabled=True, database_url=url)
    assert "secret" not in str(caught.value)


def test_migrations_use_independent_settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://runtime:secret@host/runtime")
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        MigrationSettings(_env_file=None)
    settings = MigrationSettings(_env_file=None, migration_database_url="postgresql+asyncpg://ddl:secret@host/ddl")
    assert "secret" not in repr(settings)


async def test_engine_lifespan_lazy_reused_disposed_and_failure_safe(monkeypatch):
    from app.db.session import create_database_engine
    engine = create_database_engine("postgresql+asyncpg://user:secret@127.0.0.1/test")
    dispose = AsyncMock(wraps=engine.dispose)
    monkeypatch.setattr(type(engine), "dispose", dispose)
    factory = MagicMock(return_value=engine)
    monkeypatch.setattr("app.main.create_database_engine", factory)
    app = create_app(Settings(_env_file=None, incident_persistence_enabled=True,
                              database_url="postgresql+asyncpg://user:secret@127.0.0.1/test"))
    async with app.router.lifespan_context(app):
        assert engine.pool.checkedout() == 0
        repository = app.state.incidents
        assert repository._sessions.kw["bind"] is engine
        assert app.state.incidents is repository
        factory.assert_called_once()
    dispose.assert_awaited_once()


async def test_incident_errors_and_logs_do_not_expose_credentials(monkeypatch):
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    try:
        async with incident_app() as (client, app, session):
            session.flush.side_effect = OperationalError("INSERT", {"question": "private-question"}, Exception("db-secret"))
            response = await client.post("/api/v1/diagnose", json={"message": "private-question"})
            assert response.status_code == 503
            assert "db-secret" not in response.text and "private-question" not in response.text
    finally:
        logger.removeHandler(handler)
    assert "incident_storage_error" in buffer.getvalue()
    assert "db-secret" not in buffer.getvalue() and "private-question" not in buffer.getvalue()


@pytest.mark.parametrize("phase", ["committing", "committed"])
async def test_cancel_around_commit_never_reports_success(phase):
    factory, session, transaction = mock_db()
    entered = asyncio.Event()
    async def exit_transaction(*args):
        if phase == "committing":
            entered.set()
            await asyncio.Event().wait()
        return False
    async def close():
        if phase == "committed":
            entered.set()
            await asyncio.Event().wait()
    transaction.__aexit__.side_effect = exit_transaction
    session.close.side_effect = close
    task = asyncio.create_task(IncidentRepository(factory).create_completed(**completed_values()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    session.flush.assert_awaited_once()


async def test_disabled_persistence_never_constructs_engine(monkeypatch):
    factory = MagicMock(side_effect=AssertionError("PostgreSQL must remain disabled"))
    monkeypatch.setattr("app.main.create_database_engine", factory)
    app = create_app(Settings(_env_file=None))
    async with app.router.lifespan_context(app):
        assert app.state.incidents is None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/diagnose", json={"message": "订单慢"})
            assert set(response.json()) == {"answer", "steps"}
    factory.assert_not_called()
