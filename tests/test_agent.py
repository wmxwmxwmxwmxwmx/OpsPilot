import asyncio
import json
from typing import Any

import pytest

from app.agent.loop import Agent
from app.agent.schemas import AgentExecutionError
from app.llm.base import BaseLLM, LLMResponse, Message, ToolCall, ToolDefinition
from app.llm.mock import MockLLM
from app.main import build_registry
from app.tools.registry import EmptyInput, ToolRegistry
from app.tools.system_metrics import SystemMetricsTool


class RecordingMock(MockLLM):
    def __init__(self) -> None:
        self.histories: list[list[Message]] = []

    async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
        self.histories.append([m.model_copy(deep=True) for m in messages])
        return await super().generate(messages, tools)


class CallThenAnswer(BaseLLM):
    def __init__(self, name: str = "get_system_metrics", arguments: str = "{}") -> None:
        self.name, self.arguments = name, arguments
        self.observation: dict[str, Any] | None = None

    async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
        if messages[-1].role == "tool":
            self.observation = json.loads(messages[-1].content or "{}")
            return LLMResponse(content="证据已收到。")
        return LLMResponse(tool_calls=[ToolCall(id="call_1", name=self.name, arguments=self.arguments)])


class ForeverLLM(BaseLLM):
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
        self.calls += 1
        return LLMResponse(tool_calls=[ToolCall(
            id=f"call_{self.calls}", name="get_system_metrics", arguments="{}"
        )])


async def test_mock_full_flow_and_observation_history() -> None:
    model = RecordingMock()
    result = await Agent(model, build_registry()).run("订单接口突然变慢")
    assert len(model.histories) == 3
    assert [s.tool for s in result.steps if s.type == "tool_call"] == [
        "get_system_metrics", "get_mysql_slow_queries",
    ]
    second_round = model.histories[1]
    assert [m.role for m in second_round] == ["system", "user", "assistant", "tool"]
    assert second_round[2].tool_calls[0].id == second_round[3].tool_call_id
    assert json.loads(second_round[3].content)["result"]["cpu_percent"] == 35.2
    assert "1700000" in result.answer and "2310" in result.answer
    assert "Mock Data" in result.answer


async def test_mock_uses_observation_values_not_canned_diagnosis() -> None:
    class ChangedMetrics(SystemMetricsTool):
        async def execute(self, arguments: EmptyInput) -> dict[str, Any]:
            return {"cpu_percent": 99.1, "memory_percent": 95.2, "load_average": 12.3}

    registry = ToolRegistry()
    registry.register(ChangedMetrics())
    for tool in build_registry().list_tools()[1:]:
        registry.register(tool)
    result = await Agent(MockLLM(), registry).run("订单接口慢")
    assert "99.1" in result.answer and "35.2" not in result.answer


async def test_concurrent_runs_do_not_share_history() -> None:
    agent = Agent(MockLLM(), build_registry())
    order, redis = await asyncio.gather(agent.run("订单慢"), agent.run("Redis 缓存慢"))
    assert len(order.steps) == 4
    assert len(redis.steps) == 6
    assert "96%" in redis.answer


async def test_max_steps_is_exact_and_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr("app.agent.loop.log_event", lambda event, **kw: events.append(event))
    model = ForeverLLM()
    with pytest.raises(AgentExecutionError) as raised:
        await Agent(model, build_registry(), max_steps=6).run("订单慢")
    assert raised.value.code == "max_steps_exceeded"
    assert model.calls == 6 and len(raised.value.steps) == 12
    assert "max_steps_exceeded" in events
    assert events[-1] == "agent_finish"


async def test_unknown_tool_observation() -> None:
    model = CallThenAnswer("does_not_exist")
    result = await Agent(model, build_registry()).run("test")
    assert not result.steps[1].success
    assert model.observation["error"]["code"] == "tool_not_found"


@pytest.mark.parametrize("arguments", ['{"extra":1}', "not-json", "[]", "null"])
async def test_invalid_tool_arguments_are_observed(arguments: str) -> None:
    model = CallThenAnswer(arguments=arguments)
    result = await Agent(model, build_registry()).run("test")
    assert model.observation["error"]["code"] == "invalid_tool_arguments"
    assert result.steps[0].arguments == {}


async def test_tool_exception_is_sanitized_and_returned_to_llm() -> None:
    class BrokenTool(SystemMetricsTool):
        async def execute(self, arguments: EmptyInput) -> dict[str, Any]:
            raise RuntimeError("password=never-expose-this")

    registry = ToolRegistry()
    registry.register(BrokenTool())
    model = CallThenAnswer()
    result = await Agent(model, registry).run("test")
    assert model.observation["error"]["code"] == "tool_execution_error"
    assert "never-expose-this" not in result.model_dump_json()


async def test_mock_reports_failed_evidence() -> None:
    class BrokenTool(SystemMetricsTool):
        async def execute(self, arguments: EmptyInput) -> dict[str, Any]:
            raise RuntimeError("broken")

    registry = ToolRegistry()
    registry.register(BrokenTool())
    result = await Agent(MockLLM(), registry).run("订单慢")
    assert "证据不足" in result.answer
    assert "2310" not in result.answer


async def test_tool_timeout_is_observed() -> None:
    class SlowTool(SystemMetricsTool):
        async def execute(self, arguments: EmptyInput) -> dict[str, Any]:
            await asyncio.sleep(1)
            return {}

    registry = ToolRegistry()
    registry.register(SlowTool())
    model = CallThenAnswer()
    await Agent(model, registry, tool_timeout_seconds=0.01).run("test")
    assert model.observation["error"]["code"] == "tool_timeout"


async def test_llm_exception_is_business_error() -> None:
    class BrokenLLM(BaseLLM):
        async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
            raise RuntimeError("Authorization: secret-value")

    with pytest.raises(AgentExecutionError) as raised:
        await Agent(BrokenLLM(), build_registry()).run("test")
    assert raised.value.code == "llm_error"
    assert "secret-value" not in str(raised.value)


async def test_llm_timeout() -> None:
    class SlowLLM(BaseLLM):
        async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
            await asyncio.sleep(1)
            return LLMResponse(content="late")

    with pytest.raises(AgentExecutionError) as raised:
        await Agent(SlowLLM(), build_registry(), llm_timeout_seconds=0.01).run("test")
    assert raised.value.code == "llm_timeout"


async def test_empty_answer_is_not_success() -> None:
    class EmptyLLM(BaseLLM):
        async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
            return LLMResponse(content=" ")

    with pytest.raises(AgentExecutionError) as raised:
        await Agent(EmptyLLM(), build_registry()).run("test")
    assert raised.value.code == "invalid_llm_response"


async def test_multiple_tool_calls_in_one_round() -> None:
    class BatchLLM(BaseLLM):
        async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
            if messages[-1].role == "tool":
                assert [m.tool_call_id for m in messages if m.role == "tool"] == ["a", "b"]
                return LLMResponse(content="完成")
            return LLMResponse(tool_calls=[
                ToolCall(id="a", name="get_system_metrics", arguments="{}"),
                ToolCall(id="b", name="get_redis_metrics", arguments="{}"),
            ])

    result = await Agent(BatchLLM(), build_registry()).run("test")
    assert result.answer == "完成" and len(result.steps) == 4


async def test_cancellation_is_not_swallowed() -> None:
    class CancelledLLM(BaseLLM):
        async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await Agent(CancelledLLM(), build_registry()).run("test")
