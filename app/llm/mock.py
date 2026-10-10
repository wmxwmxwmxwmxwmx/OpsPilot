import json

from app.llm.base import BaseLLM, LLMResponse, Message, ToolCall, ToolDefinition


class MockLLM(BaseLLM):
    """Deterministic, stateless tool selection; every request has its own history."""

    async def generate(
        self, messages: list[Message], tools: list[ToolDefinition]
    ) -> LLMResponse:
        observations = [json.loads(m.content or "{}") for m in messages if m.role == "tool"]
        user_message = next(m.content or "" for m in messages if m.role == "user")
        plan = ["get_system_metrics", "get_mysql_slow_queries"]
        if "redis" in user_message.lower() or "缓存" in user_message:
            plan.append("get_redis_metrics")

        # Failed tool observations are evidence too; do not invent successful results.
        if any(not obs["success"] for obs in observations):
            return LLMResponse(content="模拟诊断：工具执行失败，证据不足，无法确定故障原因。请检查工具状态后重试。")

        if len(observations) < len(plan):
            name = plan[len(observations)]
            if name not in {tool.name for tool in tools}:
                return LLMResponse(content="模拟诊断：所需工具未注册，无法完成分析。")
            return LLMResponse(tool_calls=[ToolCall(
                id=f"mock_call_{len(observations) + 1}", name=name, arguments="{}"
            )])

        system = observations[0]["result"]
        queries = observations[1]["result"]["slow_queries"]
        summary = (f"基于 Mock Data 的模拟诊断：CPU {system['cpu_percent']}%，"
                   f"内存 {system['memory_percent']}%，Load {system['load_average']}。")
        if queries:
            slowest = max(queries, key=lambda query: query["duration_ms"])
            summary += (f"最慢 SQL：{slowest['sql']}，耗时 {slowest['duration_ms']} ms，"
                        f"扫描 {slowest['rows_examined']} 行，仅返回 {slowest['rows_returned']} 行。"
                        "该查询是接口延迟的候选原因；需结合真实请求链路、EXPLAIN 和现有索引验证。"
                        "建议检查 orders.user_id 的索引与查询计划，评估按需选列，勿直接修改生产索引。")
        else:
            summary += "未发现模拟慢 SQL，现有证据不足以确定原因。"
        if len(observations) > 2:
            redis = observations[2]["result"]
            summary += f" Redis 命中率 {redis['hit_rate']:.0%}，阻塞连接 {redis['blocked_clients']}。"
        return LLMResponse(content=summary + "以上不是生产环境结论。")
