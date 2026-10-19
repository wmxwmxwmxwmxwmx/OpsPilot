"""Explicit local demo trigger, leaving the legacy MockLLM unchanged."""
import json

from app.llm.base import LLMResponse, ToolCall
from app.llm.mock import MockLLM


class WorkflowMockLLM(MockLLM):
    async def generate(self, messages, tools):
        index = max(i for i, m in enumerate(messages) if m.role == "user")
        if "mock remediation" not in (messages[index].content or "").lower():
            return await super().generate(messages, tools)
        observations = [json.loads(m.content) for m in messages[index + 1:] if m.role == "tool"]
        if observations:
            return LLMResponse(content=("模拟动作已登记；没有操作真实服务。" if observations[-1]["success"]
                                        else "模拟动作已拒绝，没有执行。"))
        return LLMResponse(tool_calls=[ToolCall(id="demo", name="propose_mock_remediation",
                            arguments='{"target":"demo-api","action":"simulate_restart"}')])
