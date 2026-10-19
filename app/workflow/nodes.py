import asyncio
import hashlib
import json
from uuid import UUID, uuid5

from langgraph.types import interrupt
from pydantic import ValidationError

from app.agent.prompts import SYSTEM_PROMPT
from app.llm.base import Message, ToolDefinition
from app.tools.mock_remediation import MockRemediationTool
from app.workflow.errors import TransientWorkflowError, WorkflowError
from app.workflow.state import GraphState, checked_update


class WorkflowNodes:
    def __init__(self, llm, registry, privacy, *, llm_timeout=30, tool_timeout=10):
        self.llm, self.registry, self.privacy = llm, registry, privacy
        self.llm_timeout, self.tool_timeout = llm_timeout, tool_timeout

    async def plan(self, state: GraphState):
        if state["step_count"] >= state["max_steps"]:
            raise WorkflowError("max_steps_exceeded")
        messages = [Message(role="system", content=SYSTEM_PROMPT)]
        messages += [Message.model_validate(m) for m in state["llm_messages"]]
        definitions = [ToolDefinition(name=t.name, description=t.description,
                       parameters=t.input_schema.model_json_schema()) for t in self.registry.list_tools()]
        try:
            async with asyncio.timeout(self.llm_timeout):
                response = await self.llm.generate(messages, definitions)
        except (TimeoutError, ConnectionError):
            raise TransientWorkflowError("llm_timeout") from None
        except Exception:
            raise WorkflowError("llm_error", resumable=True) from None
        count = state["step_count"] + 1
        if not response.tool_calls:
            if not response.content or not response.content.strip():
                raise WorkflowError("invalid_llm_response")
            answer = self.privacy.text(response.content.strip(), 16000)
            if not answer.strip():
                raise WorkflowError("invalid_llm_response")
            return checked_update(state, step_count=count,
                                  final_answer=answer)
        if len(response.tool_calls) > 8 or len(state["tool_results"]) + len(response.tool_calls) > 64:
            raise WorkflowError("workflow_too_large")
        calls = []
        # Sanitize BEFORE the planner output becomes a checkpoint. Raw LLM IDs,
        # names, malformed arguments and intermediate reasoning are never stored.
        for index, call in enumerate(response.tool_calls):
            tool = self.registry.get(call.name)
            error, args = None, {}
            if tool is None:
                error = "tool_not_found"
            else:
                try:
                    if len(call.arguments) > 2048:
                        raise ValueError
                    args = tool.input_schema.model_validate_json(call.arguments, strict=True).model_dump(mode="json")
                    # Only explicitly safe inputs are durable in this version.
                    if not isinstance(tool, MockRemediationTool) and args:
                        raise ValueError
                except (ValidationError, ValueError):
                    error = "invalid_tool_arguments"
            calls.append({"id": f"call_{count}_{index}", "name": tool.name if tool else "unregistered_tool",
                          "arguments": json.dumps(args if error is None else {}), "validation_error": error})
        message = {"role": "assistant", "tool_calls": [
            {k: c[k] for k in ("id", "name", "arguments")} for c in calls]}
        return checked_update(state, step_count=count, pending_tool_calls=calls,
                              llm_messages=[*state["llm_messages"], message])

    async def validate_tool(self, state: GraphState):
        call = dict(state["pending_tool_calls"][0])
        tool = self.registry.get(call["name"])
        if call["validation_error"]:
            raise WorkflowError(call["validation_error"])
        if tool is None:
            raise WorkflowError("tool_not_found")
        try:
            args = tool.input_schema.model_validate_json(call["arguments"], strict=True)
        except ValidationError:
            raise WorkflowError("invalid_tool_arguments") from None
        if tool.safety not in {"read_only", "approval_required"}:
            raise WorkflowError("unsafe_tool")
        if tool.safety == "approval_required" and not isinstance(tool, MockRemediationTool):
            raise WorkflowError("unsafe_tool")
        canonical = json.dumps([state["workflow_id"], call["id"], tool.name, args.model_dump()], sort_keys=True)
        call.update(operation_id=hashlib.sha256(canonical.encode()).hexdigest(), safety=tool.safety,
                    approved=False)
        approval = None
        if tool.safety == "approval_required":
            approval = {"workflow_id": state["workflow_id"],
                        "approval_id": str(uuid5(UUID(state["workflow_id"]), call["operation_id"])),
                        "tool": tool.name, "arguments": args.model_dump(), "risk": "simulated_write",
                        "description": "只登记模拟动作，不操作真实服务。", "status": "pending_approval"}
        return checked_update(state, current_tool=call, pending_approval=approval,
                              status="pending_approval" if approval else "running")

    async def human_approval(self, state: GraphState):
        decision = interrupt(state["pending_approval"])
        if (not isinstance(decision, dict) or set(decision) != {"approval_id", "decision"}
                or decision["approval_id"] != state["pending_approval"]["approval_id"]
                or decision["decision"] not in {"approve", "reject"}):
            raise WorkflowError("invalid_approval")
        call = {**state["current_tool"], "approved": decision["decision"] == "approve"}
        if not call["approved"]:
            return self.observation(state, call, success=False, error="approval_rejected")
        return checked_update(state, current_tool=call, pending_approval=None, status="running")

    def observation(self, state, call, *, success, result=None, error=None):
        record = {"type": "tool_result", "tool": call["name"], "call_id": call["id"],
                  "success": success, "result": result,
                  "error": {"code": error, "message": error} if error else None}
        return checked_update(state, tool_results=[record], current_tool=None,
            pending_tool_calls=state["pending_tool_calls"][1:], pending_approval=None, status="running",
            llm_messages=[*state["llm_messages"], {"role": "tool", "tool_call_id": call["id"],
                                                 "content": json.dumps(record, ensure_ascii=False)}])

    async def execute_tool(self, state: GraphState):
        call = state["current_tool"]
        tool = self.registry.get(call["name"])
        if tool is None or tool.safety != call["safety"]:
            raise WorkflowError("tool_policy_changed")
        try:
            args = tool.input_schema.model_validate_json(call["arguments"], strict=True)
        except ValidationError:
            raise WorkflowError("invalid_tool_arguments") from None
        if tool.safety != "read_only" and (not call["approved"] or not isinstance(tool, MockRemediationTool)):
            raise WorkflowError("approval_required")
        try:
            async with asyncio.timeout(self.tool_timeout):
                raw = (await tool.execute_idempotent(args, workflow_id=state["workflow_id"],
                                                     operation_id=call["operation_id"])
                       if isinstance(tool, MockRemediationTool) else await tool.execute(args))
            result = self.privacy.result(tool, raw)
        except (TimeoutError, ConnectionError):
            if tool.safety == "read_only":
                raise TransientWorkflowError("tool_timeout") from None
            raise WorkflowError("action_outcome_unknown", resumable=True) from None
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("tool_execution_error", resumable=True) from None
        return self.observation(state, call, success=True, result=result)

    async def finalize(self, state: GraphState):
        if not state["final_answer"]:
            raise WorkflowError("missing_final_answer")
        # Business completion is a separate idempotent service operation.
        return {"status": "graph_completed", "error_code": None}
