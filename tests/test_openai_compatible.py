import json
from typing import Any

import httpx
import pytest

from app.agent.loop import Agent
from app.core.config import Settings
from app.llm.base import LLMError, Message
from app.llm.openai_compatible import OpenAICompatibleLLM
from app.main import build_registry


def provider_settings() -> Settings:
    return Settings(_env_file=None, llm_mode="openai", openai_api_key="test-only-not-a-real-key",
                    openai_base_url="https://model.invalid/v1/", openai_model="test-model")


async def test_wire_protocol_round_trip_and_reasoning_excluded() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://model.invalid/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-only-not-a-real-key"
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["model"] == "test-model" and payload["tool_choice"] == "auto"
        assert len(payload["tools"]) == 3
        assert payload["tools"][0]["function"]["parameters"]["type"] == "object"
        if len(requests) == 1:
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
                "content": None, "reasoning_content": "hidden-reasoning-sentinel",
                "tool_calls": [{"id": "call_wire", "type": "function", "function": {
                    "name": "get_system_metrics", "arguments": "{}",
                }}],
            }}]})
        messages = payload["messages"]
        assert messages[-2]["tool_calls"][0]["id"] == "call_wire"
        assert messages[-1]["role"] == "tool" and messages[-1]["tool_call_id"] == "call_wire"
        assert json.loads(messages[-1]["content"])["result"]["cpu_percent"] == 35.2
        assert "hidden-reasoning-sentinel" not in json.dumps(payload)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": "基于模拟证据的最终答案", "reasoning": "hidden-reasoning-sentinel",
        }}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await Agent(OpenAICompatibleLLM(provider_settings(), client), build_registry()).run("test")
    assert len(requests) == 2 and len(result.steps) == 2
    assert "hidden-reasoning-sentinel" not in result.model_dump_json()


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_provider_http_errors_are_sanitized(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="secret-provider-body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMError) as raised:
            await OpenAICompatibleLLM(provider_settings(), client).generate([Message(role="user", content="test")], [])
    assert "secret-provider-body" not in str(raised.value)


@pytest.mark.parametrize("body", [
    {}, {"choices": []}, {"choices": [{"finish_reason": "stop", "message": {"content": " "}}]},
    {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
    {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": []}}]},
    {"choices": [{"finish_reason": "stop", "message": {"refusal": "no", "content": "refused"}}]},
])
async def test_invalid_provider_responses(body: dict[str, Any]) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))) as client:
        with pytest.raises(LLMError):
            await OpenAICompatibleLLM(provider_settings(), client).generate([], [])


async def test_non_json_provider_response() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="bad"))) as client:
        with pytest.raises(LLMError):
            await OpenAICompatibleLLM(provider_settings(), client).generate([], [])


async def test_provider_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret URL", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TimeoutError):
            await OpenAICompatibleLLM(provider_settings(), client).generate([], [])


async def test_bad_json_arguments_are_tool_failure_not_transport_failure() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            message = {"tool_calls": [{"id": "bad_args", "type": "function", "function": {
                "name": "get_system_metrics", "arguments": "{not-json",
            }}]}
            reason = "tool_calls"
        else:
            observation = json.loads(json.loads(request.content)["messages"][-1]["content"])
            assert observation["error"]["code"] == "invalid_tool_arguments"
            message, reason = {"content": "参数错误，诊断未完成。"}, "stop"
        return httpx.Response(200, json={"choices": [{"finish_reason": reason, "message": message}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await Agent(OpenAICompatibleLLM(provider_settings(), client), build_registry()).run("test")
    assert requests == 2 and not result.steps[1].success
