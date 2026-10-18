"""Opt-in real Redis + PostgreSQL Cache-Aside, using the shared isolated PG fixture."""
import asyncio
import json
import os
import socket
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cache.repository import CacheRepository
from app.core.config import Settings
from app.incident.repository import IncidentRepository
from app.incident.schemas import StepSummary, ToolSummary
from app.llm.mock import MockLLM
from app.main import create_app
from app.services.incident_query import IncidentQueryService
from postgres_support import ROOT

pytestmark = [pytest.mark.redis_integration, pytest.mark.postgres_integration]


@pytest.fixture
async def cache(monkeypatch):
    url = os.environ.get("OPSPILOT_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set OPSPILOT_TEST_REDIS_URL for real cache tests")
    client = Redis.from_url(url, decode_responses=False, socket_timeout=1, socket_connect_timeout=1,
                            retry=Retry(NoBackoff(), 0))
    cache = CacheRepository(client, key_prefix="opspilot:cache:m4:" + uuid4().hex, ttl_seconds=30)
    owned_keys = set()
    key = cache.key
    def tracked_key(incident_id):
        value = key(incident_id)
        owned_keys.add(value)
        return value
    monkeypatch.setattr(cache, "key", tracked_key)
    try:
        await client.ping()
        yield cache
    finally:
        if owned_keys:
            await client.delete(*owned_keys)
        await client.aclose()


@pytest.fixture
async def origin(database):
    engine, _ = database
    repo = IncidentRepository(async_sessionmaker(engine, expire_on_commit=False))
    now = datetime.now(timezone.utc)
    record = await repo.create_completed(
        session_id=None, question="private question password=hidden", answer="private answer",
        step_summary=StepSummary(tool_count=1, tools=(ToolSummary(name="get_system_metrics", success=True),)),
        duration_ms=12, created_at=now, finished_at=now,
    )
    selects = []
    def count_select(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "incident_records" in statement:
            selects.append(1)
    event.listen(engine.sync_engine, "before_cursor_execute", count_select)
    try:
        yield repo, record, selects
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_select)


async def test_real_cache_miss_hit_invalidate_and_ttl_have_measured_sql_counts(cache, origin):
    repo, record, selects = origin
    service = IncidentQueryService(repo, cache)
    key = cache.key(record.id)
    assert not await cache.client.exists(key)
    counts = []
    for invalidate in [False, False, True]:
        if invalidate:
            assert await service.invalidate(record.id)
        before = len(selects)
        summary = await service.get_summary(record.id)
        counts.append(len(selects) - before)
        assert summary.incident_id == record.id and summary.tool_count == 1
    assert counts == [1, 0, 1]
    assert 0 < await cache.client.ttl(key) <= 30
    raw = await cache.client.get(key)
    assert b"private" not in raw and b"hidden" not in raw and b"question" not in raw and b"answer" not in raw
    assert json.loads(raw)["data_source"] == "mock_tools"
    await cache.client.pexpire(key, 10000)
    await service.get_summary(record.id)
    assert 0 < await cache.client.pttl(key) <= 10000 and len(selects) == 2
    await cache.client.pexpireat(key, 1)
    assert not await cache.client.exists(key)
    assert await service.get_summary(record.id) == summary
    assert len(selects) == 3 and await cache.client.ttl(key) > 0
    assert await repo.get(record.id) == record  # Expiry never deletes PostgreSQL data.
    print("Cache SQL evidence: first miss=1, second hit=0, after invalidate=1, after expiry=1")


@pytest.mark.parametrize("payload", [b"bad-json", b'{"schema_version":2}', b"x" * 65537])
async def test_real_corrupt_value_is_rebuilt(cache, origin, payload):
    repo, record, selects = origin
    key = cache.key(record.id)
    await cache.client.set(key, payload, ex=30)
    summary = await IncidentQueryService(repo, cache).get_summary(record.id)
    assert summary.incident_id == record.id and len(selects) == 1
    assert await cache.get(record.id) == summary
    assert 0 < await cache.client.ttl(key) <= 30


async def test_real_not_found_is_not_cached(cache, origin):
    repo, _, selects = origin
    service = IncidentQueryService(repo, cache)
    missing = uuid4()
    assert await service.get_summary(missing) is None
    assert await service.get_summary(missing) is None
    assert len(selects) == 2 and not await cache.client.exists(cache.key(missing))


async def test_real_unavailable_redis_falls_back_to_postgres(origin):
    repo, record, selects = origin
    # Reserve a local TCP port without listening: real Redis connection refusal,
    # without stopping or reconfiguring any existing Redis service.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        client = Redis(host="127.0.0.1", port=reserved.getsockname()[1],
                       socket_timeout=0.1, socket_connect_timeout=0.1, retry=Retry(NoBackoff(), 0))
        try:
            cache = CacheRepository(client, io_timeout_seconds=0.2)
            summary = await IncidentQueryService(repo, cache).get_summary(record.id)
            assert summary.incident_id == record.id and len(selects) == 1
            assert not await cache.invalidate(record.id)
        finally:
            await client.aclose()


async def test_real_out_of_band_update_stays_stale_until_post_commit_invalidation(database, cache, origin):
    engine, _ = database
    repo, record, selects = origin
    service = IncidentQueryService(repo, cache)
    assert (await service.get_summary(record.id)).duration_ms == 12
    # Test-only SQL update: the production M3 repository has no UPDATE method.
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE incident_records SET duration_ms=99 WHERE id=:id"), {"id": record.id})
    assert (await service.get_summary(record.id)).duration_ms == 12
    assert len(selects) == 1  # Still an unexpired hit, despite newer authoritative data.
    assert await service.invalidate(record.id)
    assert (await service.get_summary(record.id)).duration_ms == 99
    assert len(selects) == 2


async def test_real_concurrent_misses_return_complete_summary(cache, origin, monkeypatch):
    repo, record, selects = origin
    original = repo.get
    ready, readers = asyncio.Event(), 0
    async def simultaneous_get(incident_id):
        nonlocal readers
        result = await original(incident_id)
        readers += 1
        if readers == 2:
            ready.set()
        await ready.wait()
        return result
    monkeypatch.setattr(repo, "get", simultaneous_get)
    service = IncidentQueryService(repo, cache)
    summaries = await asyncio.wait_for(asyncio.gather(service.get_summary(record.id), service.get_summary(record.id)), 3)
    assert summaries[0] == summaries[1] == await cache.get(record.id)
    assert len(selects) == 2 and await cache.client.ttl(cache.key(record.id)) > 0


@pytest.mark.parametrize("sessions_enabled", [False, True])
async def test_real_application_reads_preserve_diagnosis_semantics(database, cache, monkeypatch, sessions_enabled):
    engine, url = database
    class CountingLLM(MockLLM):
        calls = 0
        async def generate(self, messages, tools):
            self.calls += 1
            return await super().generate(messages, tools)
    llm = CountingLLM()
    settings = Settings(_env_file=None, incident_persistence_enabled=True, database_url=url,
                        session_enabled=sessions_enabled, cache_enabled=True, cache_key_prefix=cache.key_prefix,
                        redis_url=os.environ["OPSPILOT_TEST_REDIS_URL"])
    app = create_app(settings, llm=llm)
    async with app.router.lifespan_context(app):
        assert (app.state.sessions is not None) == sessions_enabled
        service = app.state.incident_queries
        get = AsyncMock(wraps=app.state.incidents.get)
        monkeypatch.setattr(app.state.incidents, "get", get)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            sid = (await client.post("/api/v1/sessions")).json()["session_id"] if sessions_enabled else None
            try:
                payload = {"message": "订单慢", "session_id": sid}
                first = await client.post("/api/v1/diagnose", json=payload)
                assert first.status_code == 200
                incident_id = UUID(first.json()["incident_id"])
                key = cache.key(incident_id)  # Register for fixture cleanup.
                assert not await cache.client.exists(key)  # Diagnosis does not prefill cache.
                assert await service.get_summary(incident_id) == await service.get_summary(incident_id)
                assert get.await_count == 1
                second = await client.post("/api/v1/diagnose/stream", json=payload)
                assert "event: final" in second.text and "event: error" not in second.text
                assert llm.calls == 6  # Each request independently executes three LLM rounds.
                assert (await client.get(f"/api/v1/incidents/{incident_id}")).status_code == 404
                if sessions_enabled:
                    assert app.state.sessions.client is app.state.incident_cache.client
                    assert (await app.state.sessions.load(UUID(sid))).version == 2
                    await service.invalidate(incident_id)
                    assert (await app.state.sessions.load(UUID(sid))).version == 2
            finally:
                if sid is not None:
                    await app.state.sessions.client.delete(app.state.sessions.key(UUID(sid)))


async def test_real_cli_uses_application_query_service(database, cache, origin):
    _, url = database
    _, record, _ = origin
    key = cache.key(record.id)
    env = {**os.environ, "INCIDENT_PERSISTENCE_ENABLED": "true", "DATABASE_URL": url,
           "CACHE_ENABLED": "true", "CACHE_KEY_PREFIX": cache.key_prefix,
           "REDIS_URL": os.environ["OPSPILOT_TEST_REDIS_URL"], "SESSION_ENABLED": "false",
           "LLM_MODE": "mock"}
    async def cli(*extra):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.cli.incident_summary", str(record.id), *extra,
            cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert b"private question" not in stdout and b"private answer" not in stdout
        return stdout.decode()
    assert '"event": "cache_miss"' in await cli()
    assert '"event": "cache_hit"' in await cli()
    invalidated = await cli("--invalidate")
    assert '"event": "cache_invalidate"' in invalidated and '"event": "cache_miss"' in invalidated
    assert await cache.client.ttl(key) > 0
