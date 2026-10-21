"""Explicit local demo trigger, leaving the legacy MockLLM unchanged."""
import json

from app.llm.base import LLMResponse, ToolCall
from app.llm.mock import MockLLM


class WorkflowMockLLM(MockLLM):
    async def generate(self, messages, tools):
        index = max(i for i, m in enumerate(messages) if m.role == "user")
        if (any(t.name == "mcp_get_mock_redis_metrics" for t in tools)
                and any(word in (messages[index].content or "").lower() for word in ("redis", "缓存"))):
            observations = [json.loads(m.content) for m in messages[index + 1:] if m.role == "tool"]
            remote = next((o for o in observations if o.get("tool") == "mcp_get_mock_redis_metrics"), None)
            if remote is None:
                return LLMResponse(tool_calls=[ToolCall(id="mcp-demo", name="mcp_get_mock_redis_metrics", arguments="{}")])
            if not remote["success"]:
                return LLMResponse(content="MCP 模拟工具失败，证据不足。")
            rate = remote["result"]["hit_rate"]
            return LLMResponse(content=f"基于 MCP Mock Data：Redis 模拟命中率 {rate:.0%}。"
                "建议比较命中与未命中增量，检查过期、淘汰和访问模式；尚不能确定根因。"
                "仅提出排查建议，没有执行修复；这不是生产环境结论。")
        if "mock remediation" not in (messages[index].content or "").lower():
            return await super().generate(messages, tools)
        observations = [json.loads(m.content) for m in messages[index + 1:] if m.role == "tool"]
        if observations:
            return LLMResponse(content=("模拟动作已登记；没有操作真实服务。" if observations[-1]["success"]
                                        else "模拟动作已拒绝，没有执行。"))
        return LLMResponse(tool_calls=[ToolCall(id="demo", name="propose_mock_remediation",
                            arguments='{"target":"demo-api","action":"simulate_restart"}')])
