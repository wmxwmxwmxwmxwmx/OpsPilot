import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError

from app.core.config import Settings
from app.session.errors import SessionError
from app.session.repository import SessionRepository
from app.session.schemas import ConversationSession


@pytest.fixture
async def redis():
    async with fakeredis.aioredis.FakeRedis() as client:
        yield client


async def test_create_json_key_ttl_and_round_trip(redis):
    repo = SessionRepository(redis)
    session = await repo.create()
    assert session.session_id.version == 4 and session.version == 0
    key = f"opspilot:session:{session.session_id}"
    raw = await redis.get(key)
    assert ConversationSession.model_validate_json(raw) == session
    assert await repo.load(session.session_id) == session
    assert 1798 <= await redis.ttl(key) <= 1800
    assert session.created_at.utcoffset().total_seconds() == 0
    assert json.loads(raw)["messages"] == []


async def test_success_refreshes_ttl_read_does_not_and_trims_complete_pairs(redis):
    repo = SessionRepository(redis, max_turns=2)
    snapshot = await repo.create()
    key = repo.key(snapshot.session_id)
    await redis.expire(key, 100)
    for _ in range(3):
        assert (await repo.load(snapshot.session_id)).version == 0
    assert 98 <= await redis.ttl(key) <= 100
    for i in range(3):
        snapshot = await repo.commit(snapshot, f"question {i}", f"answer {i}")
    assert 1798 <= await redis.ttl(key) <= 1800
    assert snapshot.version == 3
    assert [m.content for m in snapshot.messages] == ["question 1", "answer 1", "question 2", "answer 2"]
    assert [m.role for m in snapshot.messages] == ["user", "assistant"] * 2


async def test_expired_session_never_revives(redis):
    repo = SessionRepository(redis)
    snapshot = await repo.create()
    key = repo.key(snapshot.session_id)
    await redis.pexpire(key, 0)
    for operation in [repo.load(snapshot.session_id), repo.commit(snapshot, "user", "answer")]:
        with pytest.raises(SessionError, match="会话不存在") as exc:
            await operation
        assert exc.value.code == "session_not_found"
    assert await redis.get(key) is None


async def test_competing_snapshots_do_not_lose_updates(redis):
    repo = SessionRepository(redis)
    snapshot = await repo.create()
    a, b = await asyncio.gather(repo.load(snapshot.session_id), repo.load(snapshot.session_id))
    results = await asyncio.gather(repo.commit(a, "A", "answer A"), repo.commit(b, "B", "answer B"), return_exceptions=True)
    assert sum(isinstance(r, ConversationSession) for r in results) == 1
    assert next(r for r in results if isinstance(r, SessionError)).code == "session_conflict"
    saved = await repo.load(snapshot.session_id)
    assert saved.version == 1 and len(saved.messages) == 2
    assert saved.messages[0].content in {"A", "B"}


@pytest.mark.parametrize("change", ["concurrent", "expire", "storage"])
async def test_watch_conflict_or_failed_exec_has_no_partial_write(redis, monkeypatch, change):
    repo = SessionRepository(redis)
    snapshot = await repo.create()
    key = repo.key(snapshot.session_id)
    pipe = redis.pipeline()
    original_execute = pipe.execute
    before = await redis.get(key)

    async def execute():
        if change == "storage":
            raise ConnectionError("redis://:secret-password@private-host")
        if change == "expire":
            await redis.pexpire(key, 0)
        else:
            await redis.set(key, before, ex=100)
        return await original_execute()

    monkeypatch.setattr(pipe, "execute", execute)
    monkeypatch.setattr(redis, "pipeline", lambda **kw: pipe)
    with pytest.raises(SessionError) as exc:
        await repo.commit(snapshot, "question", "answer")
    assert exc.value.code == {"concurrent": "session_conflict", "expire": "session_not_found", "storage": "session_storage_error"}[change]
    assert "secret-password" not in str(exc.value)
    assert await redis.get(key) == (None if change == "expire" else before)
    assert pipe.connection is None and not pipe.watching
    if change == "concurrent":
        assert await redis.ttl(key) <= 100


@pytest.mark.parametrize("mutation", ["json", "extra", "system", "tool_call", "orphan", "wrong_id", "naive_time", "wrong_version", "non_string", "oversize"])
async def test_corrupt_redis_payload_is_rejected(redis, mutation):
    repo = SessionRepository(redis, max_bytes=1024)
    snapshot = await repo.create()
    data = snapshot.model_dump(mode="json")
    if mutation == "extra":
        data["api_key"] = "secret-sentinel"
    elif mutation == "system":
        data["messages"] = [{"role": "system", "content": "secret"}]
    elif mutation == "tool_call":
        data["messages"] = [{"role": "assistant", "content": "secret", "tool_calls": []}]
    elif mutation == "orphan":
        data["messages"] = [{"role": "user", "content": "alone"}]
    elif mutation == "wrong_id":
        data["session_id"] = str(uuid4())
    elif mutation == "naive_time":
        data["created_at"] = "2026-01-01T00:00:00"
    elif mutation == "wrong_version":
        data["version"] = "0"
    elif mutation == "non_string":
        data.update(version=1, messages=[{"role": "user", "content": 1}, {"role": "assistant", "content": "hi"}])
    payload = "secret-not-json" if mutation == "json" else "x" * 1025 if mutation == "oversize" else json.dumps(data)
    await redis.set(repo.key(snapshot.session_id), payload, ex=100)
    with pytest.raises(SessionError) as exc:
        await repo.load(snapshot.session_id)
    assert exc.value.code == "session_data_error"
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("limit", ["message", "bytes"])
async def test_size_limits_reject_without_refresh_or_partial_write(redis, limit):
    repo = SessionRepository(redis, max_message_chars=100 if limit == "message" else 16000, max_bytes=1024)
    snapshot = await repo.create()
    key = repo.key(snapshot.session_id)
    before = await redis.get(key)
    await redis.expire(key, 100)
    with pytest.raises(SessionError) as exc:
        await repo.commit(snapshot, "问题", "中" * (101 if limit == "message" else 400))
    assert exc.value.code == "session_too_large"
    assert await redis.get(key) == before and await redis.ttl(key) <= 100


async def test_redacts_credentials_and_stores_only_dialogue(redis):
    repo = SessionRepository(redis, secrets=("configured-provider-key", "configured-redis-password"))
    snapshot = await repo.create()
    saved = await repo.commit(snapshot,
        'password=hunter2 api_key="key-sentinel" Bearer bearer-sentinel configured-redis-password',
        'configured-provider-key redis://u:uri-sentinel@host sk-provider-sentinel')
    raw = (await redis.get(repo.key(saved.session_id))).decode()
    assert "[REDACTED]" in raw
    for secret in ["hunter2", "key-sentinel", "bearer-sentinel", "configured-redis-password", "configured-provider-key", "uri-sentinel", "sk-provider-sentinel"]:
        assert secret not in raw
    assert all(set(m) == {"role", "content"} for m in json.loads(raw)["messages"])


@pytest.mark.parametrize("operation", ["get", "set"])
async def test_redis_outage_is_safe(redis, monkeypatch, operation):
    monkeypatch.setattr(redis, operation, AsyncMock(side_effect=ConnectionError("password=secret-sentinel")))
    repo = SessionRepository(redis)
    with pytest.raises(SessionError) as exc:
        await (repo.create() if operation == "set" else repo.load(uuid4()))
    assert exc.value.code == "session_storage_error" and "secret-sentinel" not in str(exc.value)


@pytest.mark.parametrize("field,value", [("session_ttl_seconds", 0), ("session_max_turns", 0), ("session_max_turns", 101), ("session_max_bytes", 1), ("session_max_message_chars", 1), ("redis_url", "https://host"), ("redis_url", "redis://host/0?socket_timeout=999"), ("redis_url", "redis://host:bad/0")])
def test_session_settings_validation(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_session_defaults_and_secret_url_repr():
    settings = Settings(_env_file=None, redis_url="redis://:secret-sentinel@localhost/0")
    assert not settings.session_enabled
    assert settings.session_ttl_seconds == 1800 and settings.session_max_turns == 10
    assert "secret-sentinel" not in repr(settings)


async def test_lowered_retention_applies_on_read_without_refresh(redis):
    repo = SessionRepository(redis, max_turns=3)
    session = await repo.create()
    for i in range(3):
        session = await repo.commit(session, str(i), f"answer {i}")
    key = repo.key(session.session_id)
    raw = await redis.get(key)
    await redis.expire(key, 100)
    smaller = SessionRepository(redis, max_turns=1)
    loaded = await smaller.load(session.session_id)
    assert [m.content for m in loaded.messages] == ["2", "answer 2"]
    assert await redis.get(key) == raw and await redis.ttl(key) <= 100
