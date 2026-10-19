from uuid import uuid4

import fakeredis.aioredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.session.errors import SessionError
from app.session.repository import SessionRepository
from app.workflow.session_commit import commit_session, receipt_key


@pytest.fixture
async def sessions():
    client = fakeredis.aioredis.FakeRedis()
    try:
        yield SessionRepository(client)
    finally:
        await client.aclose()


async def state_for(sessions):
    snapshot = await sessions.create()
    return dict(workflow_id=str(uuid4()), session_id=str(snapshot.session_id), session_version=snapshot.version,
                user_message="question", final_answer="answer"), snapshot


async def test_workflow_commit_once_and_receipt_survives_session_expiry(sessions):
    state, snapshot = await state_for(sessions)
    assert await commit_session(sessions, state) == "committed"
    assert await commit_session(sessions, state) == "committed"
    assert (await sessions.load(snapshot.session_id)).version == 1
    await sessions.client.delete(sessions.key(snapshot.session_id))
    assert await commit_session(sessions, state) == "committed"
    assert not await sessions.client.exists(sessions.key(snapshot.session_id))


async def test_expired_session_never_revived(sessions):
    state, snapshot = await state_for(sessions)
    await sessions.client.delete(sessions.key(snapshot.session_id))
    assert await commit_session(sessions, state) == "expired"
    assert not await sessions.client.exists(sessions.key(snapshot.session_id))
    assert not await sessions.client.exists(receipt_key(state["workflow_id"]))


async def test_changed_version_does_not_append_stale_answer(sessions):
    state, snapshot = await state_for(sessions)
    await sessions.commit(snapshot, "other", "new answer")
    assert await commit_session(sessions, state) == "conflict"
    saved = await sessions.load(snapshot.session_id)
    assert saved.version == 1 and saved.messages[-1].content == "new answer"


async def test_unknown_redis_commit_reads_receipt_before_retry(sessions, monkeypatch):
    state, snapshot = await state_for(sessions)
    pipe_type = type(sessions.client.pipeline())
    execute, count = pipe_type.execute, []
    async def lose(self, *args, **kwargs):
        result = await execute(self, *args, **kwargs)
        count.append(1)
        raise RedisConnectionError("lost acknowledgement")
    with monkeypatch.context() as scope:
        scope.setattr(pipe_type, "execute", lose)
        with pytest.raises(SessionError, match="提交结果可能未知"):
            await commit_session(sessions, state)
    assert await commit_session(sessions, state) == "committed"
    assert len(count) == 1 and (await sessions.load(snapshot.session_id)).version == 1


async def test_missing_repository_and_stateless_behavior():
    assert await commit_session(None, {"session_id": None}) == "not_requested"
    with pytest.raises(SessionError) as caught:
        await commit_session(None, {"session_id": str(uuid4())})
    assert caught.value.code == "session_disabled"
