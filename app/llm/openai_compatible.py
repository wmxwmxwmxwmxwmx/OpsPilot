from typing import Any

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.llm.base import BaseLLM, LLMError, LLMResponse, Message, ToolCall, ToolDefinition


class OpenAICompatibleLLM(BaseLLM):
    """Chat Completions wire protocol; client lifecycle belongs to the application."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    @staticmethod
    def _serialize_message(message: Message) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {"id": call.id, "type": "function",
                 "function": {"name": call.name, "arguments": call.arguments}}
                for call in message.tool_calls
            ]
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        return payload

    async def generate(
        self, messages: list[Message], tools: list[ToolDefinition]
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._settings.openai_model,
            "messages": [self._serialize_message(message) for message in messages],
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": tool.model_dump()} for tool in tools]
            payload["tool_choice"] = "auto"
        try:
            response = await self._client.post(
                self._settings.openai_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + self._settings.openai_api_key.get_secret_value()},
                json=payload, timeout=self._settings.llm_timeout_seconds,
            )
            response.raise_for_status()
            choice = response.json()["choices"][0]
            message = choice["message"]
            if choice.get("finish_reason") not in {"stop", "tool_calls"} or message.get("refusal"):
                raise ValueError("Incomplete or refused response")
            calls = []
            for call in message.get("tool_calls") or []:
                if call.get("type") != "function":
                    raise ValueError("Unsupported tool type")
                calls.append(ToolCall(id=call["id"], name=call["function"]["name"],
                                      arguments=call["function"]["arguments"]))
            if len({call.id for call in calls}) != len(calls):
                raise ValueError("Duplicate call IDs")
            if choice["finish_reason"] == "tool_calls" and not calls:
                raise ValueError("Missing tool calls")
            # Explicit field selection discards reasoning/reasoning_content and metadata.
            parsed = LLMResponse(content=message.get("content"), tool_calls=calls)
            if not parsed.tool_calls and not (parsed.content or "").strip():
                raise ValueError("Empty answer")
            return parsed
        except httpx.TimeoutException:
            raise TimeoutError("Model request timed out") from None
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, ValidationError):
            raise LLMError("Model service returned an invalid response") from None
