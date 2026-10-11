import asyncio
import json
from contextlib import aclosing
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter

from app.agent.events import AgentEvent, FinalAnswerEvent
from app.agent.loop import Agent
from app.api.diagnose import stream_diagnosis
from app.api.sse import encode_sse
from app.core.config import Settings
from app.core.logging import request_id_context
from app.llm.base import BaseLLM, LLMResponse, Message, ToolCall, ToolDefinition
from app.llm.mock import MockLLM
from app.main import build_registry, create_app
from app.tools.registry import EmptyInput, Tool, ToolRegistry
from app.tools.system_metrics import SystemMetricsTool


def parse_sse(body: str) -> list[dict[str, Any]]:
    assert body.endswith("\n\n")
    events = []
    for frame in body[:-2].split("\n\n"):
        name, data = frame.split("\n")
        assert name.startswith("event: ") and data.startswith("data: ")
        event = {"type": name[7:], **json.loads(data[6:])}
        TypeAdapter(AgentEvent).validate_python(event)
        events.append(event)
    assert sum(e["type"] in {"final", "error"} for e in events) == 1
    assert events[-1]["type"] in {"final", "error"}
    return events


class BatchLLM(BaseLLM):
    def __init__(self, *, arguments: str = "{}", name: str = "get_system_metrics") -> None:
        self.arguments, self.name = arguments, name
        self.observations: list[dict[str, Any]] = []
        self.calls = 0

    async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
        self.calls += 1
        if messages[-1].role == "tool":
            self.observations = [json.loads(m.content) for m in messages if m.role == "tool"]
            assert [m.tool_call_id for m in messages if m.role == "tool"] == ["a", "b"]
            return LLMResponse(content="证据已收到。")
        return LLMResponse(tool_calls=[
            ToolCall(id="a", name=self.name, arguments=self.arguments),
            ToolCall(id="b", name=self.name, arguments=self.arguments),
        ])


async def post_stream(agent: Agent) -> httpx.Response:
    app = create_app(Settings(_env_file=None))
    async with app.router.lifespan_context(app):
        app.state.agent = agent
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/v1/diagnose/stream", json={"message": "订单慢"})


async def test_stream_matches_original_json_api(client: httpx.AsyncClient) -> None:
    payload = {"message": "订单接口突然变慢"}
    response = await client.post("/api/v1/diagnose/stream", json=payload)
    original = await client.post("/api/v1/diagnose", json=payload)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert len(response.headers["x-request-id"]) == 32
    events = parse_sse(response.text)
    assert [e["type"] for e in events] == ["agent_start", "tool_call", "tool_result", "tool_call", "tool_result", "final"]
    assert events[0] == {"type": "agent_start", "max_steps": 6}
    assert events[1:-1] == original.json()["steps"]
    assert events[-1]["answer"] == original.json()["answer"]
    for call, result in zip(events[1:-1:2], events[2:-1:2]):
        assert (call["call_id"], call["tool"]) == (result["call_id"], result["tool"])


async def test_concurrent_streams_are_isolated(client: httpx.AsyncClient) -> None:
    order, redis = await asyncio.gather(*[
        client.post("/api/v1/diagnose/stream", json={"message": message})
        for message in ["订单慢", "Redis缓存出现异常，请帮我诊断"]
    ])
    order_events, redis_events = parse_sse(order.text), parse_sse(redis.text)
    assert len(order_events) == 6 and len(redis_events) == 8
    assert "get_redis_metrics" not in order.text
    assert redis_events[-3]["tool"] == "get_redis_metrics"
    assert "96%" in redis_events[-1]["answer"]
    assert order.headers["x-request-id"] != redis.headers["x-request-id"]


@pytest.mark.parametrize("payload", [{}, {"message": " "}, {"message": 123}, {"message": "x" * 4001}, {"message": "hi", "key": "secret-sentinel"}])
async def test_stream_validation_still_uses_http_422(client: httpx.AsyncClient, payload: dict) -> None:
    response = await client.post("/api/v1/diagnose/stream", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "secret-sentinel" not in response.text


async def test_openapi_describes_sse(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    operation = response.json()["paths"]["/api/v1/diagnose/stream"]["post"]
    assert "text/event-stream" in operation["responses"]["200"]["content"]


def test_json_framing_cannot_inject_sse_events() -> None:
    answer = '中文"反斜线\\\r\nevent: error\n\ndata: {"code":"fake"}'
    frame = encode_sse(FinalAnswerEvent(answer=answer))
    assert "中文" in frame
    assert parse_sse(frame) == [{"type": "final", "answer": answer}]


@pytest.mark.parametrize("failure,code", [("timeout", "llm_timeout"), ("provider", "llm_error"), ("empty", "invalid_llm_response"), ("unexpected", "internal_error")])
async def test_stream_errors_are_terminal_and_sanitized(failure: str, code: str) -> None:
    class FailingLLM(BaseLLM):
        async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
            if failure == "timeout":
                await asyncio.Event().wait()
            if failure == "empty":
                return LLMResponse(content=" ")
            if failure == "unexpected":
                return object()  # Deliberately violate the provider contract.
            raise RuntimeError("Authorization: sk-secret-provider-payload")

    response = await post_stream(Agent(FailingLLM(), build_registry(), llm_timeout_seconds=0.01))
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert [e["type"] for e in events] == ["agent_start", "error"]
    assert events[-1]["code"] == code
    assert all(secret not in response.text for secret in ["sk-secret", "Authorization", "Traceback", "object has no attribute"])


async def test_max_steps_is_exact_in_stream() -> None:
    class ForeverLLM(BatchLLM):
        async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
            return await super().generate(messages[:2], tools)

    model = ForeverLLM()
    response = await post_stream(Agent(model, build_registry(), max_steps=2))
    events = parse_sse(response.text)
    assert model.calls == 2
    assert len(events) == 10  # Start + two calls/results per round + error.
    assert events[-1]["code"] == "max_steps_exceeded"


@pytest.mark.parametrize("failure,code", [
    ("exception", "tool_execution_error"), ("timeout", "tool_timeout"),
    ("nan", "tool_execution_error"), ("non_json", "tool_execution_error"),
    ("array", "tool_execution_error"), ("arguments", "invalid_tool_arguments"),
    ("unknown", "tool_not_found"),
])
async def test_tool_failures_remain_observations(failure: str, code: str) -> None:
    executed = []

    class BrokenTool(SystemMetricsTool):
        async def execute(self, arguments: EmptyInput) -> dict[str, Any]:
            executed.append(True)
            if failure == "timeout":
                await asyncio.Event().wait()
            if failure == "nan":
                return {"value": float("nan")}
            if failure == "non_json":
                return {"value": object()}
            if failure == "array":
                return []
            raise RuntimeError("sk-secret-tool-exception")

    model = BatchLLM(arguments='{"password":"sk-secret-argument"}' if failure == "arguments" else "{}",
                     name="missing" if failure == "unknown" else "get_system_metrics")
    registry = ToolRegistry()
    registry.register(BrokenTool())
    response = await post_stream(Agent(model, registry, tool_timeout_seconds=0.01))
    events = parse_sse(response.text)
    assert [e["type"] for e in events] == ["agent_start", "tool_call", "tool_result", "tool_call", "tool_result", "final"]
    assert events[-1]["answer"] == "证据已收到。"
    assert len(model.observations) == 2
    for observation in model.observations:
        assert observation["error"]["code"] == code
        assert observation["success"] is False
    assert "sk-secret" not in response.text
    if failure in {"arguments", "unknown"}:
        assert executed == [] and events[1]["arguments"] == {}


async def test_call_is_yielded_before_execution_and_contains_validated_arguments() -> None:
    class Input(BaseModel):
        model_config = ConfigDict(extra="forbid")
        target: str

    executed = []

    class EchoTool(Tool[Input]):
        name, description, input_schema = "echo", "Echo a validated target", Input

        async def execute(self, arguments: Input) -> dict[str, Any]:
            executed.append(arguments.target)
            return {"target": arguments.target}

    registry = ToolRegistry()
    registry.register(EchoTool())
    agent = Agent(BatchLLM(name="echo", arguments='{"target":"中文"}'), registry)
    async with aclosing(agent.stream("test")) as events:
        assert (await anext(events)).type == "agent_start"
        call = await anext(events)
        assert call.arguments == {"target": "中文"} and executed == []
        assert (await anext(events)).type == "tool_result"
        assert executed == ["中文"]
        assert (await anext(events)).call_id == "b"
        assert executed == ["中文"]
    assert executed == ["中文"]  # Closing at second call must not run it.


@pytest.mark.parametrize("stop_after", [1, 2, 3, 4, 5, 6])
async def test_early_generator_close_finishes_agent(stop_after: int, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = []
    monkeypatch.setattr("app.agent.loop.log_event", lambda event, **fields: logs.append((event, fields)))
    agent = Agent(MockLLM(), build_registry())
    async with aclosing(stream_diagnosis(agent, "订单慢")) as stream:
        for _ in range(stop_after):
            await anext(stream)
    assert logs[-1][0] == "agent_finish"
    assert logs[-1][1]["success"] is (stop_after == 6)
    assert sum(name == "agent_finish" for name, _ in logs) == 1


@pytest.mark.parametrize("phase", ["llm", "tool"])
async def test_task_cancellation_propagates(phase: str) -> None:
    entered, cleaned = asyncio.Event(), asyncio.Event()
    executed = []

    class WaitingLLM(BatchLLM):
        async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
            if phase == "llm":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            return await super().generate(messages, tools)

    class WaitingTool(SystemMetricsTool):
        async def execute(self, arguments: EmptyInput) -> dict[str, Any]:
            executed.append(True)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    registry = ToolRegistry()
    registry.register(WaitingTool())
    chunks = []

    async def consume() -> None:
        async with aclosing(stream_diagnosis(Agent(WaitingLLM(), registry), "test")) as stream:
            async for chunk in stream:
                chunks.append(chunk)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert cleaned.is_set()
    assert len(executed) == (1 if phase == "tool" else 0)
    assert not any("event: final" in c or "event: error" in c for c in chunks)


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
@pytest.mark.parametrize("phase", ["llm", "tool", "send", "result"])
async def test_asgi_disconnect_cleans_up_without_next_tool(spec: str, phase: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the real ASGI app, with deterministic disconnect signals, not
    # ASGITransport's buffered body or timing-based sleep assertions.
    entered, cleaned = asyncio.Event(), asyncio.Event()
    logs, sent, executed = [], [], []

    def log(event: str, **fields: Any) -> None:
        logs.append((event, request_id_context.get(), fields))

    monkeypatch.setattr("app.agent.loop.log_event", log)
    monkeypatch.setattr("app.core.logging.log_event", log)

    class WaitingLLM(BatchLLM):
        async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
            if phase == "llm":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            return await super().generate(messages, tools)

    class WaitingTool(SystemMetricsTool):
        async def execute(self, arguments: EmptyInput) -> dict[str, Any]:
            executed.append(True)
            if phase == "result":
                return {"cpu_percent": 35.2}
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    registry = ToolRegistry()
    registry.register(WaitingTool())
    app = create_app(Settings(_env_file=None))
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b'{"message":"test"}', "more_body": False}
        await entered.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if phase == "send" and b"event: tool_call" in message.get("body", b""):
            raise OSError("client closed connection")
        if phase == "result" and b"event: tool_result" in message.get("body", b""):
            entered.set()

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec},
             "method": "POST", "path": "/api/v1/diagnose/stream", "raw_path": b"/api/v1/diagnose/stream",
             "query_string": b"", "headers": [(b"content-type", b"application/json")],
             "scheme": "http", "server": ("test", 80), "client": ("test", 1234), "http_version": "1.1"}
    original_context = request_id_context.get()
    async with app.router.lifespan_context(app):
        app.state.agent = Agent(WaitingLLM(), registry)
        await asyncio.wait_for(app(scope, receive, send), 1)
    assert cleaned.is_set() is (phase in {"llm", "tool"})
    assert len(executed) == (1 if phase in {"tool", "result"} else 0)
    body = b"".join(m.get("body", b"") for m in sent)
    assert b"event: final" not in body and b"event: error" not in body
    assert logs[-1][0] == "request_finish"
    assert sum(name == "agent_finish" for name, _, _ in logs) == 1
    assert next(fields for name, _, fields in logs if name == "agent_finish")["success"] is False
    headers = dict(sent[0]["headers"])
    assert all(request_id == headers[b"x-request-id"].decode() for _, request_id, _ in logs)
    assert request_id_context.get() == original_context
