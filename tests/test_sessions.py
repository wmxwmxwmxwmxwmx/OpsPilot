import asyncio
import io
import json
import logging
from contextlib import aclosing, asynccontextmanager
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import fakeredis.aioredis
import httpx
import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError

from app.agent.loop import Agent
from app.api.diagnose import stream_diagnosis
from app.api.sse import SSEResponse
from app.core.config import Settings
from app.core.logging import JsonFormatter, logger
from app.llm.base import BaseLLM, LLMResponse
from app.llm.mock import MockLLM
from app.main import build_registry, create_app
from app.services.diagnosis import DiagnosisService
from app.session.errors import SessionError
from app.session.repository import SessionRepository
from app.session.schemas import ConversationTurn


class CapturingLLM(MockLLM):
    def __init__(self):
        self.histories = []

    async def generate(self, messages, tools):
        self.histories.append([m.model_copy(deep=True) for m in messages])
        return await super().generate(messages, tools)


@asynccontextmanager
async def session_app(llm=None, **options):
    async with fakeredis.aioredis.FakeRedis() as redis:
        repo = SessionRepository(redis, **options)
        app = create_app(Settings(_env_file=None), llm=llm)
        async with app.router.lifespan_context(app):
            app.state.sessions = repo
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                yield client, repo, app


def sse_events(response):
    events = []
    for frame in response.text.strip().split("\n\n"):
        name, data = frame.split("\n")
        events.append({"type": name[7:], **json.loads(data[6:])})
    assert sum(e["type"] in {"final", "error"} for e in events) == 1
    return events


async def test_two_rounds_and_sse_restore_only_dialogue_and_current_mock_plan():
    llm = CapturingLLM()
    async with session_app(llm) as (client, repo, _):
        created = await client.post("/api/v1/sessions")
        assert created.status_code == 201
        assert created.json()["expires_in_seconds"] == 1800
        sid = created.json()["session_id"]
        assert UUID(sid).version == 4
        first = await client.post("/api/v1/diagnose", json={"session_id": sid, "message": "订单慢"})
        assert first.status_code == 200 and set(first.json()) == {"answer", "steps"}
        assert len(first.json()["steps"]) == 4
        second = await client.post("/api/v1/diagnose", json={"session_id": sid, "message": "那 Redis 缓存方面呢？"})
        assert len(second.json()["steps"]) == 6
        first_second_round = llm.histories[3]
        assert [m.role for m in first_second_round] == ["system", "user", "assistant", "user"]
        assert first_second_round[1].content == "订单慢"
        assert first_second_round[2].content == first.json()["answer"]
        assert first_second_round[3].content == "那 Redis 缓存方面呢？"
        assert all(not m.tool_calls and m.tool_call_id is None for m in first_second_round)
        third = await client.post("/api/v1/diagnose/stream", json={"session_id": sid, "message": "继续检查订单"})
        events = sse_events(third)
        assert len(events) == 6 and events[-1]["type"] == "final"
        assert "get_redis_metrics" not in third.text  # Latest question drives MockLLM.
        saved = await repo.load(UUID(sid))
        assert saved.version == 3 and len(saved.messages) == 6
        assert saved.messages[-1].content == events[-1]["answer"]
        assert len(llm.histories) == 10  # Exactly one agent execution per request.
        assert [m.role for m in llm.histories[7]] == ["system", "user", "assistant", "user", "assistant", "user"]
        raw = await repo.client.get(repo.key(UUID(sid)))
        assert all(key not in raw for key in [b'"tool_calls"', b'"tool_call_id"', b'"result"', b'"system"', b'"reasoning"'])


@pytest.mark.parametrize("path", ["/api/v1/diagnose", "/api/v1/diagnose/stream"])
async def test_stateless_never_touches_redis_even_when_unavailable(path, monkeypatch):
    async with session_app() as (client, repo, _):
        get = AsyncMock(side_effect=AssertionError("Redis must not be accessed"))
        set_ = AsyncMock(side_effect=AssertionError("Redis must not be accessed"))
        monkeypatch.setattr(repo.client, "get", get)
        monkeypatch.setattr(repo.client, "set", set_)
        response = await client.post(path, json={"message": "订单慢"})
        assert response.status_code == 200
        if path.endswith("stream"):
            assert sse_events(response)[-1]["type"] == "final"
        else:
            assert "answer" in response.json()
        get.assert_not_awaited()
        set_.assert_not_awaited()


async def test_disabled_sessions_are_explicit(client):
    created = await client.post("/api/v1/sessions")
    assert created.status_code == 503 and created.json()["error"]["code"] == "session_disabled"
    response = await client.post("/api/v1/diagnose", json={"message": "hi", "session_id": str(uuid4())})
    assert response.status_code == 503
    stream = await client.post("/api/v1/diagnose/stream", json={"message": "hi", "session_id": str(uuid4())})
    assert sse_events(stream)[-1]["code"] == "session_disabled"


@pytest.mark.parametrize("sid", ["invalid-id-secret", "../key", 123, "00000000-0000-0000-0000-000000000000"])
@pytest.mark.parametrize("path", ["/api/v1/diagnose", "/api/v1/diagnose/stream"])
async def test_session_id_validation(client, sid, path):
    response = await client.post(path, json={"session_id": sid, "message": "hi"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "invalid-id-secret" not in response.text


async def test_cannot_create_client_chosen_session_id():
    async with session_app() as (client, repo, _):
        response = await client.post("/api/v1/sessions", json={"session_id": str(uuid4())})
        assert response.status_code == 422
        assert await repo.client.dbsize() == 0


@pytest.mark.parametrize("failure,status,code", [("missing", 404, "session_not_found"), ("corrupt", 503, "session_data_error"), ("storage", 503, "session_storage_error")])
async def test_load_errors_map_to_http_and_sse(failure, status, code, monkeypatch):
    llm = CapturingLLM()
    async with session_app(llm) as (client, repo, _):
        sid = uuid4()
        if failure == "corrupt":
            await repo.client.set(repo.key(sid), "password=corrupt-secret", ex=100)
        if failure == "storage":
            monkeypatch.setattr(repo.client, "get", AsyncMock(side_effect=ConnectionError("password=redis-secret")))
        for path in ["/api/v1/diagnose", "/api/v1/diagnose/stream"]:
            response = await client.post(path, json={"session_id": str(sid), "message": "hi"})
            if path.endswith("stream"):
                assert response.status_code == 200
                assert sse_events(response) == [{"type": "error", "code": code, "message": SessionError(code).message}]
            else:
                assert response.status_code == status and response.json()["error"]["code"] == code
            assert "redis-secret" not in response.text and "corrupt-secret" not in response.text
        assert llm.histories == []


@pytest.mark.parametrize("path", ["/api/v1/diagnose", "/api/v1/diagnose/stream"])
async def test_failed_agent_does_not_save_or_refresh(path):
    class BrokenLLM(BaseLLM):
        async def generate(self, messages, tools):
            raise RuntimeError("secret-provider-payload")

    async with session_app(BrokenLLM()) as (client, repo, _):
        session = await repo.create()
        key = repo.key(session.session_id)
        await repo.client.expire(key, 100)
        before = await repo.client.get(key)
        response = await client.post(path, json={"message": "hi", "session_id": str(session.session_id)})
        if path.endswith("stream"):
            assert sse_events(response)[-1]["code"] == "llm_error"
        else:
            assert response.status_code == 502
        assert await repo.client.get(key) == before
        assert await repo.client.ttl(key) <= 100


@pytest.mark.parametrize("failure,status", [("session_storage_error", 503), ("session_conflict", 409), ("session_not_found", 404)])
async def test_commit_errors_never_expose_final(failure, status, monkeypatch):
    async with session_app() as (client, repo, _):
        session = await repo.create()
        monkeypatch.setattr(repo, "commit", AsyncMock(side_effect=SessionError(failure)))
        for path in ["/api/v1/diagnose", "/api/v1/diagnose/stream"]:
            response = await client.post(path, json={"message": "订单慢", "session_id": str(session.session_id)})
            if path.endswith("stream"):
                events = sse_events(response)
                assert events[-1]["code"] == failure
                assert not any(e["type"] == "final" for e in events)
            else:
                assert response.status_code == status
        assert (await repo.load(session.session_id)).version == 0


class BarrierLLM(BaseLLM):
    def __init__(self):
        self.entered = 0
        self.ready = asyncio.Event()
        self.histories = []

    async def generate(self, messages, tools):
        self.histories.append([m.model_copy(deep=True) for m in messages])
        self.entered += 1
        if self.entered == 2:
            self.ready.set()
        await self.ready.wait()
        return LLMResponse(content="完成 " + messages[-1].content)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("same_session", [False, True])
async def test_concurrent_requests_are_isolated_or_conflict(stream, same_session):
    llm = BarrierLLM()
    async with session_app(llm) as (client, repo, _):
        first, second = await repo.create(), await repo.create()
        ids = [first.session_id, first.session_id if same_session else second.session_id]
        path = "/api/v1/diagnose" + ("/stream" if stream else "")
        responses = await asyncio.wait_for(asyncio.gather(*[
            client.post(path, json={"session_id": str(sid), "message": message})
            for sid, message in zip(ids, ["first", "second"])
        ]), 2)
        if stream:
            terminals = [sse_events(r)[-1] for r in responses]
            assert sum(e["type"] == "final" for e in terminals) == (1 if same_session else 2)
            if same_session:
                assert next(e for e in terminals if e["type"] == "error")["code"] == "session_conflict"
        else:
            assert sorted(r.status_code for r in responses) == ([200, 409] if same_session else [200, 200])
        assert (await repo.load(first.session_id)).version == 1
        assert (await repo.load(second.session_id)).version == (0 if same_session else 1)
        assert all(len(h) == 2 for h in llm.histories)


async def test_stream_final_is_blocked_until_commit(monkeypatch):
    async with session_app() as (_, repo, app):
        session = await repo.create()
        entered, release = asyncio.Event(), asyncio.Event()
        real_commit = repo.commit
        emitted = []

        async def gated_commit(*args):
            entered.set()
            await release.wait()
            return await real_commit(*args)

        monkeypatch.setattr(repo, "commit", gated_commit)
        service = DiagnosisService(app.state.agent, repo)

        async def consume():
            async with aclosing(service.stream("订单慢", session.session_id)) as events:
                async for event in events:
                    if event.type == "final":
                        assert (await repo.load(session.session_id)).version == 1
                    emitted.append(event.type)

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert emitted == ["agent_start", "tool_call", "tool_result", "tool_call", "tool_result"]
            assert (await repo.load(session.session_id)).version == 0
            release.set()
            await asyncio.wait_for(task, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert emitted[-1] == "final"


@pytest.mark.parametrize("phase", ["llm", "watch", "committed"])
async def test_session_sse_disconnect_cleans_up_and_commit_boundary(phase, monkeypatch):
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class WaitingLLM(MockLLM):
        async def generate(self, messages, tools):
            if phase == "llm":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            return await super().generate(messages, tools)

    async with session_app(WaitingLLM()) as (_, repo, app):
        session = await repo.create()
        pipe = repo.client.pipeline()
        if phase == "watch":
            async def blocked_get(*args):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            monkeypatch.setattr(pipe, "get", blocked_get)
            monkeypatch.setattr(repo.client, "pipeline", lambda **kwargs: pipe)
        if phase == "committed":
            real_commit = repo.commit
            async def commit_then_disconnect(*args):
                await real_commit(*args)
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            monkeypatch.setattr(repo, "commit", commit_then_disconnect)

        service = DiagnosisService(app.state.agent, repo)
        response = SSEResponse(stream_diagnosis(service, "订单慢", session.session_id))
        sent = []
        async def receive():
            await entered.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            sent.append(message)
        await asyncio.wait_for(response({"type": "http"}, receive, send), 2)
        assert cleaned.is_set()
        saved = await repo.load(session.session_id)
        assert saved.version == (1 if phase == "committed" else 0)
        assert len(saved.messages) == (2 if phase == "committed" else 0)
        assert pipe.connection is None and not pipe.watching
        body = b"".join(m.get("body", b"") for m in sent)
        assert b"event: final" not in body and b"event: error" not in body


@pytest.mark.parametrize("bad", [{"role": "system", "content": "secret"}, {"role": "tool", "content": "observation"}, {"role": "assistant", "content": "hi", "tool_calls": []}])
async def test_agent_rejects_non_conversation_history(bad):
    llm = CapturingLLM()
    with pytest.raises(ValidationError):
        await Agent(llm, build_registry()).run("hi", history=[bad])
    assert llm.histories == []


async def test_agent_history_messages_are_request_local():
    history = [ConversationTurn(role="user", content="first"), ConversationTurn(role="assistant", content="answer")]
    class MutatingLLM(BaseLLM):
        async def generate(self, messages, tools):
            assert messages[1].content == "first"
            messages[1].content = "mutated"
            return LLMResponse(content="done")
    agent = Agent(MutatingLLM(), build_registry())
    await asyncio.gather(agent.run("one", history=history), agent.run("two", history=history))
    assert history[0].content == "first"


async def test_redis_client_lifespan_is_lazy_reused_and_closed(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis()
    factory_calls = []
    close = AsyncMock(wraps=redis.aclose)
    monkeypatch.setattr(redis, "aclose", close)
    def factory(*args, **kwargs):
        factory_calls.append(kwargs)
        return redis
    monkeypatch.setattr("app.main.Redis.from_url", factory)
    app = create_app(Settings(_env_file=None, session_enabled=True))
    async with app.router.lifespan_context(app):
        assert len(factory_calls) == 1
        assert not redis.connection_pool._in_use_connections
        repository = app.state.sessions
        await repository.create()
        await repository.create()
        assert app.state.sessions.client is redis and len(factory_calls) == 1
    close.assert_awaited_once()


async def test_session_logs_do_not_contain_secrets(monkeypatch):
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    try:
        async with session_app() as (client, repo, _):
            sid = (await repo.create()).session_id
            response = await client.post("/api/v1/diagnose", json={"session_id": str(sid), "message": "password=message-secret"})
            assert response.status_code == 200
            monkeypatch.setattr(repo.client, "get", AsyncMock(side_effect=ConnectionError("redis://:redis-password@host")))
            response = await client.post("/api/v1/diagnose", json={"session_id": str(sid), "message": "message-secret"})
            assert response.status_code == 503
    finally:
        logger.removeHandler(handler)
    assert all(s not in buffer.getvalue() for s in [str(sid), "message-secret", "redis-password"])
    assert "session_committed" in buffer.getvalue() and "session_storage_error" in buffer.getvalue()


async def test_enabled_but_unavailable_redis_does_not_block_startup_or_stateless(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(redis, "execute_command", AsyncMock(side_effect=ConnectionError("redis-secret")))
    monkeypatch.setattr("app.main.Redis.from_url", lambda *args, **kwargs: redis)
    app = create_app(Settings(_env_file=None, session_enabled=True))
    async with app.router.lifespan_context(app):
        redis.execute_command.assert_not_awaited()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for path in ["/api/v1/diagnose", "/api/v1/diagnose/stream"]:
                response = await client.post(path, json={"message": "订单慢"})
                assert response.status_code == 200
            redis.execute_command.assert_not_awaited()
            response = await client.post("/api/v1/sessions")
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "session_storage_error"
            assert "redis-secret" not in response.text
