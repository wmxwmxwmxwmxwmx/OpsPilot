import asyncio
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import fakeredis.aioredis
import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError, TimeoutError as RedisTimeoutError

from app.cache.keys import incident_summary_key
from app.cache.repository import CacheRepository
from app.cache.schemas import IncidentSummary
from app.core.config import Settings
from app.core.logging import JsonFormatter, logger
from app.incident.errors import IncidentError
from app.incident.schemas import IncidentRecord, StepSummary, ToolSummary
from app.main import create_app
from app.services.incident_query import IncidentQueryService
from app.session.errors import SessionError
from app.session.repository import SessionRepository


@pytest.fixture
async def redis():
    async with fakeredis.aioredis.FakeRedis() as client:
        yield client


@pytest.fixture
def record():
    now = datetime.now(timezone.utc)
    return IncidentRecord(id=uuid4(), session_id=uuid4(), question="private question",
                          answer="private answer", step_summary=StepSummary(tool_count=1, tools=(
                              ToolSummary(name="get_system_metrics", success=True),)),
                          duration_ms=12, created_at=now, finished_at=now)


def test_versioned_keys_are_canonical_and_disjoint_from_sessions():
    incident_id = uuid4()
    key = incident_summary_key(incident_id)
    assert key == f"opspilot:cache:incident_summary:v1:{incident_id}"
    assert incident_summary_key(UUID(str(incident_id).upper())) == key
    assert incident_summary_key(uuid4()) != key
    assert SessionRepository.key(incident_id) != key
    assert incident_summary_key(incident_id, "opspilot:cache:test") == f"opspilot:cache:test:incident_summary:v1:{incident_id}"


@pytest.mark.parametrize("prefix", ["", "opspilot:session", "opspilot", "opspilot:cache:",
                                   "opspilot:cache:*", "opspilot:cache\n", "opspilot:cache:has space",
                                   "opspilot:cache:" + "x" * 129])
def test_prefix_rejects_unsafe_or_overlapping_namespace(prefix):
    with pytest.raises(ValueError):
        incident_summary_key(uuid4(), prefix)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cache_key_prefix=prefix)


@pytest.mark.parametrize("incident_id", ["password=secret", UUID(int=0), None])
def test_key_does_not_accept_arbitrary_user_text(incident_id):
    with pytest.raises(ValueError):
        incident_summary_key(incident_id)


@pytest.mark.parametrize("field,value", [("cache_ttl_seconds", 0), ("cache_ttl_seconds", 604801),
                                        ("cache_max_bytes", 0), ("cache_max_bytes", 1048577),
                                        ("cache_io_timeout_seconds", 0), ("cache_io_timeout_seconds", 11)])
def test_cache_settings_bounds(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_cache_requires_authoritative_storage_and_defaults_off():
    settings = Settings(_env_file=None)
    assert not settings.cache_enabled and settings.cache_ttl_seconds == 300
    with pytest.raises(ValidationError, match="requires"):
        Settings(_env_file=None, cache_enabled=True)
    settings = Settings(_env_file=None, cache_enabled=True, session_enabled=False,
                        incident_persistence_enabled=True, database_url="postgresql+asyncpg://user:secret@host/db")
    assert settings.cache_enabled and "secret" not in repr(settings)


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("schema_version", True), ("schema_version", 1.0), ("schema_version", "1"),
    ("status", "running"), ("tool_count", -1), ("tool_count", 1601), ("tool_count", True),
    ("duration_ms", -1), ("duration_ms", "12"), ("tool_names", ["password-secret"]),
    ("truncated", True), ("tool_names", []), ("question", "secret"), ("answer", "secret"),
    ("result", {"api_key": "secret"}), ("session_id", str(uuid4())), ("data_source", "production"),
    ("created_at", datetime(2026, 1, 1)),
    ("created_at", datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8)))),
])
def test_safe_summary_schema_rejects_corruption(record, field, value):
    data = IncidentSummary.from_record(record).model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        IncidentSummary(**data)


async def test_cache_aside_miss_hit_invalidate_reduces_queries(redis, record, monkeypatch):
    cache = CacheRepository(redis)
    source = MagicMock(get=AsyncMock(return_value=record))
    service = IncidentQueryService(source, cache)
    calls = []
    for invalidate in [False, False, True]:
        if invalidate:
            assert await service.invalidate(record.id)
        before = source.get.await_count
        result = await service.get_summary(record.id)
        calls.append(source.get.await_count - before)
        assert result == IncidentSummary.from_record(record)
    assert calls == [1, 0, 1]
    raw = await redis.get(cache.key(record.id))
    payload = json.loads(raw)
    assert set(payload) == {"schema_version", "incident_id", "status", "tool_count", "tool_names",
                            "truncated", "duration_ms", "created_at", "data_source"}
    assert all(word not in raw for word in [b"private", b"question", b"answer", b"session_id", b"result"])
    assert 0 < await redis.ttl(cache.key(record.id)) <= 300


async def test_reads_do_not_refresh_ttl_expiry_refills_without_changing_record(redis, record):
    cache = CacheRepository(redis, ttl_seconds=30)
    source = MagicMock(get=AsyncMock(return_value=record))
    service = IncidentQueryService(source, cache)
    await service.get_summary(record.id)
    key = cache.key(record.id)
    await redis.pexpire(key, 10000)
    await service.get_summary(record.id)
    assert 0 < await redis.pttl(key) <= 10000
    await redis.pexpireat(key, 1)
    assert not await redis.exists(key)
    assert await service.get_summary(record.id) == IncidentSummary.from_record(record)
    assert source.get.await_count == 2 and 0 < await redis.ttl(key) <= 30


async def test_not_found_is_not_cached(redis):
    cache = CacheRepository(redis)
    source = MagicMock(get=AsyncMock(return_value=None))
    service = IncidentQueryService(source, cache)
    incident_id = uuid4()
    assert await service.get_summary(incident_id) is None
    assert await service.get_summary(incident_id) is None
    assert source.get.await_count == 2 and not await redis.exists(cache.key(incident_id))


@pytest.mark.parametrize("corruption", ["json", "version", "missing_version", "identity", "secret", "utf8", "oversized"])
async def test_corrupt_cache_is_evicted_then_rebuilt(redis, record, monkeypatch, corruption):
    cache = CacheRepository(redis)
    payload = IncidentSummary.from_record(record).model_dump(mode="json")
    if corruption == "version":
        payload["schema_version"] = 2
    elif corruption == "missing_version":
        del payload["schema_version"]
    elif corruption == "identity":
        payload["incident_id"] = str(uuid4())
    elif corruption == "secret":
        payload["question"] = "api_key=secret"
    raw = {"json": b"{bad-json", "utf8": b"\xff", "oversized": b"x" * 65537}.get(corruption, json.dumps(payload).encode())
    await redis.set(cache.key(record.id), raw, ex=30)
    original_delete = redis.delete
    async def delete_key(*args):
        return await original_delete(*args)
    delete = AsyncMock(side_effect=delete_key)
    monkeypatch.setattr(redis, "delete", delete)
    source = MagicMock(get=AsyncMock(return_value=record))
    service = IncidentQueryService(source, cache)
    assert await service.get_summary(record.id) == IncidentSummary.from_record(record)
    delete.assert_awaited_once_with(cache.key(record.id))
    source.get.assert_awaited_once_with(record.id)
    assert await cache.get(record.id) == IncidentSummary.from_record(record)


async def test_oversized_write_is_optional_and_wrong_identity_is_rejected(redis, record):
    cache = CacheRepository(redis, max_bytes=1)
    service = IncidentQueryService(MagicMock(get=AsyncMock(return_value=record)), cache)
    assert await service.get_summary(record.id) == IncidentSummary.from_record(record)
    assert not await redis.exists(cache.key(record.id))
    assert not await cache.set(uuid4(), IncidentSummary.from_record(record))
    invalid = IncidentSummary.from_record(record).model_copy(update={"tool_names": ("secret",)})
    assert not await cache.set(record.id, invalid)


async def test_invalidation_only_deletes_requested_cache_key(redis, record):
    sessions = SessionRepository(redis)
    session = await sessions.create()
    cache = CacheRepository(redis)
    await cache.set(record.id, IncidentSummary.from_record(record))
    other = record.model_copy(update={"id": uuid4()})
    await cache.set(other.id, IncidentSummary.from_record(other))
    assert await cache.invalidate(record.id)
    assert await cache.invalidate(record.id)  # Already absent is an acknowledged DEL.
    assert await cache.get(other.id) is not None
    assert await sessions.load(session.session_id) == session


@pytest.mark.parametrize("operation", ["get", "set", "delete"])
@pytest.mark.parametrize("failure", [ConnectionError, RedisTimeoutError, TimeoutError])
async def test_redis_io_failures_degrade_without_database_error_suppression(redis, record, monkeypatch, operation, failure):
    cache = CacheRepository(redis)
    monkeypatch.setattr(redis, operation, AsyncMock(side_effect=failure("redis://user:secret@host")))
    source = MagicMock(get=AsyncMock(return_value=record))
    service = IncidentQueryService(source, cache)
    if operation == "delete":
        assert not await service.invalidate(record.id)
    else:
        assert await service.get_summary(record.id) == IncidentSummary.from_record(record)
        source.get.assert_awaited_once()


@pytest.mark.parametrize("operation", ["get", "set", "delete"])
async def test_cache_deadlines_cancel_pending_io(redis, record, monkeypatch, operation):
    finished = asyncio.Event()
    async def stuck(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
    monkeypatch.setattr(redis, operation, stuck)
    cache = CacheRepository(redis, io_timeout_seconds=0.01)
    service = IncidentQueryService(MagicMock(get=AsyncMock(return_value=record)), cache)
    async with asyncio.timeout(1):
        if operation == "delete":
            assert not await service.invalidate(record.id)
        else:
            assert await service.get_summary(record.id) == IncidentSummary.from_record(record)
    assert finished.is_set()


async def test_corrupt_cache_delete_failure_still_falls_back(redis, record, monkeypatch):
    cache = CacheRepository(redis)
    await redis.set(cache.key(record.id), "bad-json", ex=30)
    monkeypatch.setattr(redis, "delete", AsyncMock(side_effect=ConnectionError("secret")))
    service = IncidentQueryService(MagicMock(get=AsyncMock(return_value=record)), cache)
    assert await service.get_summary(record.id) == IncidentSummary.from_record(record)


async def test_pg_failure_propagates_and_never_fills_cache(redis, record, monkeypatch):
    failure = IncidentError("incident_storage_error")
    source = MagicMock(get=AsyncMock(side_effect=failure))
    cache = CacheRepository(redis)
    set_ = AsyncMock(wraps=cache.set)
    monkeypatch.setattr(cache, "set", set_)
    with pytest.raises(IncidentError) as caught:
        await IncidentQueryService(source, cache).get_summary(record.id)
    assert caught.value is failure
    set_.assert_not_awaited()


@pytest.mark.parametrize("phase", ["get", "postgres", "set", "invalidate"])
async def test_cancel_propagates_without_retry_or_later_work(redis, record, monkeypatch, phase):
    cache = CacheRepository(redis)
    source = MagicMock(get=AsyncMock(return_value=record))
    target, name = (source, "get") if phase == "postgres" else (cache, phase)
    cancelled = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(target, name, cancelled)
    service = IncidentQueryService(source, cache)
    with pytest.raises(asyncio.CancelledError):
        if phase == "invalidate":
            await service.invalidate(record.id)
        else:
            await service.get_summary(record.id)
    cancelled.assert_awaited_once()
    if phase in {"get", "invalidate"}:
        source.get.assert_not_awaited()
    assert not await redis.exists(cache.key(record.id))


async def test_unexpected_cache_programming_error_is_not_swallowed(redis, record, monkeypatch):
    monkeypatch.setattr(redis, "get", AsyncMock(side_effect=RuntimeError("bug")))
    source = MagicMock(get=AsyncMock(return_value=record))
    with pytest.raises(RuntimeError, match="bug"):
        await IncidentQueryService(source, CacheRepository(redis)).get_summary(record.id)
    source.get.assert_not_awaited()


async def test_disabled_cache_always_uses_postgres(record):
    source = MagicMock(get=AsyncMock(return_value=record))
    service = IncidentQueryService(source)
    assert await service.get_summary(record.id) == await service.get_summary(record.id)
    assert source.get.await_count == 2 and await service.invalidate(record.id)


async def test_concurrent_misses_allow_duplicate_origin_reads_but_complete_cache(redis, record):
    entered, reads = asyncio.Event(), 0
    async def get(incident_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            entered.set()
        await entered.wait()
        return record
    cache = CacheRepository(redis)
    service = IncidentQueryService(MagicMock(get=AsyncMock(side_effect=get)), cache)
    results = await asyncio.wait_for(asyncio.gather(service.get_summary(record.id), service.get_summary(record.id)), 2)
    assert results[0] == results[1] == await cache.get(record.id)
    assert reads == 2 and await redis.ttl(cache.key(record.id)) > 0


async def test_out_of_band_update_can_be_stale_until_invalidation(redis, record):
    source = MagicMock(get=AsyncMock(return_value=record))
    service = IncidentQueryService(source, CacheRepository(redis))
    assert (await service.get_summary(record.id)).duration_ms == 12
    source.get.return_value = record.model_copy(update={"duration_ms": 99})
    assert (await service.get_summary(record.id)).duration_ms == 12
    assert await service.invalidate(record.id)
    assert (await service.get_summary(record.id)).duration_ms == 99


async def test_stale_fill_race_is_explicitly_not_strong_consistency(redis, record):
    read, release = asyncio.Event(), asyncio.Event()
    old = record
    async def get(incident_id):
        read.set()
        await release.wait()
        return old
    source = MagicMock(get=AsyncMock(side_effect=get))
    cache = CacheRepository(redis)
    service = IncidentQueryService(source, cache)
    task = asyncio.create_task(service.get_summary(record.id))
    try:
        await asyncio.wait_for(read.wait(), 1)
        # Simulate B committing a newer value while A holds the older snapshot.
        source.get.side_effect = None
        source.get.return_value = record.model_copy(update={"duration_ms": 99})
        assert await service.invalidate(record.id)
        release.set()
        assert (await task).duration_ms == 12
        assert (await service.get_summary(record.id)).duration_ms == 12
        await cache.invalidate(record.id)
        assert (await service.get_summary(record.id)).duration_ms == 99
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("sessions_enabled,cache_enabled", [(False, False), (True, False), (False, True), (True, True)])
async def test_lifespan_reuses_one_lazy_client_and_closes_once(redis, record, monkeypatch, sessions_enabled, cache_enabled):
    factory = MagicMock(return_value=redis)
    close = AsyncMock(wraps=redis.aclose)
    monkeypatch.setattr(redis, "aclose", close)
    monkeypatch.setattr("app.main.Redis.from_url", factory)
    config = Settings(_env_file=None, session_enabled=sessions_enabled, cache_enabled=cache_enabled,
                      incident_persistence_enabled=True, database_url="postgresql+asyncpg://user@host/db")
    app = create_app(config)
    async with app.router.lifespan_context(app):
        assert not redis.connection_pool._in_use_connections
        assert factory.call_count == int(sessions_enabled or cache_enabled)
        assert (app.state.sessions is not None) == sessions_enabled
        if sessions_enabled and cache_enabled:
            assert app.state.sessions.client is app.state.incident_cache.client
        assert (app.state.incident_cache is not None) == cache_enabled
        monkeypatch.setattr(app.state.incidents, "get", AsyncMock(return_value=record))
        service = app.state.incident_queries
        assert await service.get_summary(record.id) == await service.get_summary(record.id)
        assert app.state.incidents.get.await_count == (1 if cache_enabled else 2)
    assert close.await_count == int(sessions_enabled or cache_enabled)


async def test_cache_outage_does_not_change_session_commit_errors(redis, record, monkeypatch):
    cache = CacheRepository(redis)
    sessions = SessionRepository(redis)
    snapshot = await sessions.create()
    monkeypatch.setattr(redis, "get", AsyncMock(side_effect=ConnectionError("secret")))
    service = IncidentQueryService(MagicMock(get=AsyncMock(return_value=record)), cache)
    assert await service.get_summary(record.id) == IncidentSummary.from_record(record)
    # Session pipeline uses its own commands; fail its WATCH to check M2's boundary.
    pipe = redis.pipeline()
    monkeypatch.setattr(pipe, "watch", AsyncMock(side_effect=ConnectionError("secret")))
    monkeypatch.setattr(redis, "pipeline", lambda **kwargs: pipe)
    with pytest.raises(SessionError) as caught:
        await sessions.commit(snapshot, "question", "answer")
    assert caught.value.code == "session_storage_error"


async def test_cache_logs_only_safe_operational_metadata(redis, record, monkeypatch):
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    try:
        cache = CacheRepository(redis)
        service = IncidentQueryService(MagicMock(get=AsyncMock(return_value=record)), cache)
        await service.get_summary(record.id)
        await service.get_summary(record.id)
        await service.invalidate(record.id)
        monkeypatch.setattr(redis, "get", AsyncMock(side_effect=ConnectionError("redis://secret-url")))
        await service.get_summary(record.id)
        monkeypatch.setattr(redis, "delete", AsyncMock(side_effect=ConnectionError("secret-password")))
        assert not await service.invalidate(record.id)
    finally:
        logger.removeHandler(handler)
    payload = buffer.getvalue()
    for secret in ["private question", "private answer", "secret-url", "secret-password", str(record.session_id)]:
        assert secret not in payload
    logs = [json.loads(line) for line in payload.splitlines()]
    assert {entry["event"] for entry in logs} >= {"cache_hit", "cache_miss", "cache_set", "cache_invalidate", "cache_error", "cache_fallback"}
    assert all(entry["cache_type"] == "incident_summary" for entry in logs)
    assert any(entry.get("code") == "cache_invalidate_error" for entry in logs)


@pytest.mark.parametrize("operation", ["get", "set", "delete"])
async def test_external_cancellation_inside_redis_io_is_not_a_cache_timeout(redis, record, monkeypatch, operation):
    entered, cleaned = asyncio.Event(), asyncio.Event()
    async def pending(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    monkeypatch.setattr(redis, operation, pending)
    cache = CacheRepository(redis)
    if operation == "get":
        work = cache.get(record.id)
    elif operation == "set":
        work = cache.set(record.id, IncidentSummary.from_record(record))
    else:
        work = cache.invalidate(record.id)
    task = asyncio.create_task(work)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
