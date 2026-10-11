import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from time import perf_counter

from pydantic import ValidationError

from app.agent.events import AgentEvent, AgentStartEvent, FinalAnswerEvent
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.schemas import (
    AgentExecutionError, DiagnoseResponse, Step, ToolCallStep, ToolResultStep,
)
from app.core.logging import log_event
from app.llm.base import BaseLLM, Message, ToolCall, ToolDefinition
from app.tools.registry import ToolRegistry


class Agent:
    def __init__(
        self, llm: BaseLLM, registry: ToolRegistry, *, max_steps: int = 6,
        llm_timeout_seconds: float = 30, tool_timeout_seconds: float = 10,
    ) -> None:
        if max_steps < 1 or min(llm_timeout_seconds, tool_timeout_seconds) <= 0:
            raise ValueError("Step limit and timeouts must be positive")
        self.llm = llm
        self.registry = registry
        self.max_steps = max_steps
        self.llm_timeout_seconds = llm_timeout_seconds
        self.tool_timeout_seconds = tool_timeout_seconds

    async def run(self, message: str) -> DiagnoseResponse:
        """Collect the same execution events for the original JSON API."""
        steps: list[Step] = []
        async with aclosing(self.stream(message)) as events:
            async for event in events:
                if isinstance(event, (ToolCallStep, ToolResultStep)):
                    steps.append(event)
                elif isinstance(event, FinalAnswerEvent):
                    return DiagnoseResponse(answer=event.answer, steps=steps)
        raise RuntimeError("Agent ended without a final answer")

    async def stream(self, message: str) -> AsyncIterator[AgentEvent]:
        """Yield execution events; failures raise AgentExecutionError, never HTTP.

        Consumers that stop early must close this async generator (aclosing).
        Cancellation and GeneratorExit propagate through all execution boundaries.
        """
        # All mutable conversation state is request-local, including failed calls.
        messages = [Message(role="system", content=SYSTEM_PROMPT),
                    Message(role="user", content=message)]
        steps: list[Step] = []
        definitions = [ToolDefinition(name=tool.name, description=tool.description,
                                     parameters=tool.input_schema.model_json_schema())
                       for tool in self.registry.list_tools()]
        started = perf_counter()
        success = False
        log_event("agent_start", max_steps=self.max_steps)
        try:
            yield AgentStartEvent(max_steps=self.max_steps)
            for round_number in range(1, self.max_steps + 1):
                await asyncio.sleep(0)  # Cancellation checkpoint, including MockLLM.
                log_event("agent_step", step=round_number)
                try:
                    async with asyncio.timeout(self.llm_timeout_seconds):
                        response = await self.llm.generate(messages, definitions)
                except TimeoutError:
                    log_event("llm_failure", code="llm_timeout")
                    raise AgentExecutionError("llm_timeout", "模型调用超时，请稍后重试。", steps) from None
                except Exception:
                    # Boundary conversion is deliberate: exception text may contain secrets.
                    log_event("llm_failure", code="llm_error")
                    raise AgentExecutionError("llm_error", "模型服务调用失败，请稍后重试。", steps) from None

                if not response.tool_calls:
                    if not response.content or not response.content.strip():
                        log_event("llm_failure", code="invalid_llm_response")
                        raise AgentExecutionError("invalid_llm_response", "模型未返回有效答案。", steps)
                    success = True
                    yield FinalAnswerEvent(answer=response.content.strip())
                    return

                # Preserve call IDs for matching observations. Intermediate model text is
                # neither a public step nor required for this V0.1 adapter's next round.
                messages.append(Message(role="assistant", tool_calls=response.tool_calls))
                for call in response.tool_calls:
                    async with aclosing(self._execute_tool(call, steps)) as tool_events:
                        async for event in tool_events:
                            if isinstance(event, ToolResultStep):
                                messages.append(Message(
                                    role="tool", tool_call_id=call.id,
                                    content=json.dumps(event.model_dump(exclude_none=True), ensure_ascii=False),
                                ))
                            yield event

            log_event("max_steps_exceeded", max_steps=self.max_steps)
            raise AgentExecutionError("max_steps_exceeded", "诊断已达到最大轮数，尚未得到最终答案。", steps)
        finally:
            log_event("agent_finish", success=success,
                      latency_ms=round((perf_counter() - started) * 1000, 3))

    async def _execute_tool(self, call: ToolCall, steps: list[Step]) -> AsyncIterator[Step]:
        started = perf_counter()
        call_step = ToolCallStep(tool=call.name, call_id=call.id)
        steps.append(call_step)
        result_step = ToolResultStep(tool=call.name, call_id=call.id, success=False)
        tool = self.registry.get(call.name)
        arguments = None
        log_event("tool_start", tool=call.name)
        if tool is None:
            result_step.error = {"code": "tool_not_found", "message": "请求的工具未注册。"}
        else:
            try:
                arguments = tool.input_schema.model_validate_json(call.arguments, strict=True)
            except (ValidationError, ValueError):
                # Never echo malformed/unvalidated arguments, which may contain secrets.
                result_step.error = {"code": "invalid_tool_arguments", "message": "工具参数不符合输入 schema。"}
            else:
                call_step.arguments = arguments.model_dump(mode="json")
        # Emit only validated arguments, before executing even an immediate tool.
        yield call_step
        if tool is not None and arguments is not None:
            await asyncio.sleep(0)  # Give disconnect cancellation a chance before IO.
            try:
                async with asyncio.timeout(self.tool_timeout_seconds):
                    result = await tool.execute(arguments)
                # Validate the observation before adding it to the model context.
                json.dumps(result, allow_nan=False)
                if not isinstance(result, dict):
                    raise TypeError("Tool results must be JSON objects")
                result_step.result = result
                result_step.success = True
            except TimeoutError:
                result_step.error = {"code": "tool_timeout", "message": "工具执行超时。"}
            except Exception:
                result_step.error = {"code": "tool_execution_error", "message": "工具执行失败。"}
        steps.append(result_step)
        log_event("tool_success" if result_step.success else "tool_failure", tool=call.name,
                  success=result_step.success,
                  code=result_step.error["code"] if result_step.error else "ok",
                  latency_ms=round((perf_counter() - started) * 1000, 3))
        yield result_step
