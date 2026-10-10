import asyncio
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.llm.base import BaseLLM, LLMResponse, Message, ToolDefinition
from app.main import create_app


async def test_diagnose_api(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/diagnose", json={"message": "订单接口突然变慢，请帮我排查"})
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"answer", "steps"}
    assert [s["type"] for s in data["steps"]] == ["tool_call", "tool_result"] * 2
    assert data["steps"][0]["tool"] == "get_system_metrics"
    assert data["steps"][2]["tool"] == "get_mysql_slow_queries"
    assert data["steps"][3]["result"]["slow_queries"][0]["duration_ms"] == 2310
    assert "Mock Data" in data["answer"]
    assert "reasoning" not in data and "chain_of_thought" not in data


@pytest.mark.parametrize("payload", [{}, {"message": ""}, {"message": "   "},
    {"message": None}, {"message": 123}, {"message": "x" * 4001},
    {"message": "test", "password": "secret-sentinel"}])
async def test_invalid_request(client: httpx.AsyncClient, payload: dict[str, Any]) -> None:
    response = await client.post("/api/v1/diagnose", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "secret-sentinel" not in response.text


async def test_malformed_json(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/diagnose", content="{bad", headers={"Content-Type": "application/json"})
    assert response.status_code == 422


async def test_max_steps_http_error_keeps_observable_steps() -> None:
    app = create_app(Settings(_env_file=None, max_steps=1))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/diagnose", json={"message": "订单慢"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "max_steps_exceeded"
    assert len(response.json()["steps"]) == 2


@pytest.mark.parametrize("failure,status,code", [("error", 502, "llm_error"), ("timeout", 504, "llm_timeout")])
async def test_llm_http_errors_are_safe(failure: str, status: int, code: str) -> None:
    class FailingLLM(BaseLLM):
        async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
            if failure == "timeout":
                await asyncio.sleep(1)
            raise RuntimeError("secret-provider-payload")

    app = create_app(Settings(_env_file=None, llm_timeout_seconds=0.01), llm=FailingLLM())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/diagnose", json={"message": "test"})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "secret-provider-payload" not in response.text and "Traceback" not in response.text
