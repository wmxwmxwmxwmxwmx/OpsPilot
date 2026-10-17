"""Opt-in real Redis checks; only delete UUID keys created by these tests.

OPSPILOT_TEST_REDIS_URL=redis://127.0.0.1:16379/0 pytest -v -m redis_integration
"""

import asyncio
import os
from uuid import UUID

import httpx
import pytest
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.core.config import Settings
from app.main import create_app
from app.session.errors import SessionError
from app.session.repository import SessionRepository
from app.session.schemas import ConversationSession

pytestmark = pytest.mark.redis_integration


@pytest.fixture
async def real_repository(monkeypatch):
    url = os.environ.get("OPSPILOT_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set OPSPILOT_TEST_REDIS_URL to opt into real Redis tests")
    client = Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2,
                           retry=Retry(NoBackoff(), 0))
    owned_keys = []
    repo = SessionRepository(client, ttl_seconds=30)
    create = repo.create
    async def tracked_create():
        session = await create()
        owned_keys.append(repo.key(session.session_id))
        return session
    monkeypatch.setattr(repo, "create", tracked_create)
    try:
        await client.ping()
        yield repo
    finally:
        if owned_keys:
            await client.delete(*owned_keys)
        await client.aclose()


async def test_real_ttl_slides_only_on_success_and_expiry_cannot_revive(real_repository):
    repo = real_repository
    session = await repo.create()
    key = repo.key(session.session_id)
    assert 28000 <= await repo.client.pttl(key) <= 30000
    await repo.client.pexpire(key, 10000)
    await repo.load(session.session_id)
    assert 0 < await repo.client.pttl(key) <= 10000
    updated = await repo.commit(session, "订单慢", "诊断完成")
    assert updated.version == 1
    assert 28000 <= await repo.client.pttl(key) <= 30000
    # Use Redis server time to force a real expiration, without sleep timing.
    seconds, _ = await repo.client.time()
    await repo.client.pexpireat(key, seconds * 1000 - 1)
    for operation in [repo.load(session.session_id), repo.commit(updated, "继续", "完成")]:
        with pytest.raises(SessionError) as exc:
            await operation
        assert exc.value.code == "session_not_found"
    assert await repo.client.exists(key) == 0


@pytest.mark.parametrize("mutation", ["write", "expire"])
async def test_real_watch_exec_rejects_intervening_write_or_expiry(real_repository, monkeypatch, mutation):
    repo = real_repository
    session = await repo.create()
    key = repo.key(session.session_id)
    original = await repo.client.get(key)
    pipe = repo.client.pipeline()
    execute = pipe.execute
    async def interleaved_execute():
        # This uses another pooled connection, while the pipeline owns WATCH.
        if mutation == "expire":
            await repo.client.pexpire(key, 0)
        else:
            await repo.client.set(key, original, ex=10)
        return await execute()
    monkeypatch.setattr(pipe, "execute", interleaved_execute)
    monkeypatch.setattr(repo.client, "pipeline", lambda **kwargs: pipe)
    with pytest.raises(SessionError) as exc:
        await repo.commit(session, "new question", "new answer")
    assert exc.value.code == ("session_not_found" if mutation == "expire" else "session_conflict")
    assert await repo.client.get(key) == (None if mutation == "expire" else original)
    assert not pipe.watching and pipe.connection is None


async def test_real_concurrent_transactions_have_one_winner(real_repository, monkeypatch):
    repo = real_repository
    session = await repo.create()
    ready, reads = asyncio.Event(), 0
    factory = repo.client.pipeline
    pipelines = []
    def gated_pipeline(**kwargs):
        pipe = factory(**kwargs)
        original_get = pipe.get
        async def get(*args):
            nonlocal reads
            result = await original_get(*args)
            reads += 1
            if reads == 2:
                ready.set()
            await ready.wait()
            return result
        monkeypatch.setattr(pipe, "get", get)
        pipelines.append(pipe)
        return pipe
    monkeypatch.setattr(repo.client, "pipeline", gated_pipeline)
    results = await asyncio.wait_for(asyncio.gather(
        repo.commit(session, "A", "answer A"), repo.commit(session, "B", "answer B"),
        return_exceptions=True,
    ), 3)
    assert sum(isinstance(r, ConversationSession) for r in results) == 1
    assert next(r for r in results if isinstance(r, SessionError)).code == "session_conflict"
    saved = await repo.load(session.session_id)
    assert saved.version == 1 and len(saved.messages) == 2
    assert all(pipe.connection is None and not pipe.watching for pipe in pipelines)


async def test_real_exec_queue_failure_does_not_write_or_refresh(real_repository, monkeypatch):
    repo = real_repository
    snapshot = await repo.create()
    key = repo.key(snapshot.session_id)
    original = await repo.client.get(key)
    await repo.client.expire(key, 10)
    pipe = repo.client.pipeline()
    execute = pipe.execute
    async def bad_transaction():
        # An invalid queued command causes EXECABORT before SET is applied.
        pipe.execute_command("SET")
        return await execute()
    monkeypatch.setattr(pipe, "execute", bad_transaction)
    monkeypatch.setattr(repo.client, "pipeline", lambda **kwargs: pipe)
    with pytest.raises(SessionError) as exc:
        await repo.commit(snapshot, "question", "answer")
    assert exc.value.code == "session_storage_error"
    assert await repo.client.get(key) == original and await repo.client.ttl(key) <= 10
    assert pipe.connection is None


async def test_real_http_two_rounds_and_sse_commit(real_repository):
    repo = real_repository
    app = create_app(Settings(_env_file=None))
    async with app.router.lifespan_context(app):
        app.state.sessions = repo
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/v1/sessions")
            assert created.status_code == 201
            sid = created.json()["session_id"]
            for message in ["订单慢", "那 Redis 缓存方面呢？"]:
                result = await client.post("/api/v1/diagnose", json={"session_id": sid, "message": message})
                assert result.status_code == 200
            assert len(result.json()["steps"]) == 6
            stream = await client.post("/api/v1/diagnose/stream", json={"session_id": sid, "message": "继续检查"})
            assert "event: final" in stream.text and "event: error" not in stream.text
    saved = await repo.load(UUID(sid))
    assert saved.version == 3 and len(saved.messages) == 6
    assert 28 <= await repo.client.ttl(repo.key(saved.session_id)) <= 30
