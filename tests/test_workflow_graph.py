import asyncio
import json
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.llm.base import LLMResponse, ToolCall
from app.llm.mock import MockLLM
from app.main import build_registry
from app.tools.mock_remediation import MockRemediationTool
from app.workflow.checkpoint import strict_serializer
from app.workflow.errors import WorkflowError
from app.workflow.graph import build_graph
from app.workflow.mock import WorkflowMockLLM
from app.workflow.nodes import WorkflowNodes
from app.workflow.privacy import CheckpointPrivacy
from app.workflow.state import checked_update, merge_results


def initial(message="订单慢", max_steps=6):
    return dict(schema_version=1, workflow_id=str(uuid4()), session_id=None, session_version=None,
        incident_id=str(uuid4()), user_message=message, llm_messages=[{"role": "user", "content": message}],
        pending_tool_calls=[], current_tool=None, tool_results=[], step_count=0, max_steps=max_steps,
        status="running", final_answer=None, pending_approval=None, error_code=None)


def config(state):
    return {"configurable": {"thread_id": state["workflow_id"]}, "recursion_limit": 512}


def graph(llm=None, registry=None, saver=None, **kwargs):
    return build_graph(WorkflowNodes(llm or MockLLM(), registry or build_registry(),
        CheckpointPrivacy(("configured-secret",)), **kwargs), saver or InMemorySaver(serde=strict_serializer()))


async def test_normal_graph_and_isolated_threads():
    g, state, other = graph(), initial("Redis慢"), initial()
    result = await g.ainvoke(state, config(state), durability="sync")
    assert result["status"] == "graph_completed" and len(result["tool_results"]) == 3
    assert "Mock Data" in result["final_answer"]
    assert not (await g.aget_state(config(state))).next
    assert not (await g.aget_state(config(other))).values
    await g.ainvoke(other, config(other), durability="sync")
    assert len((await g.aget_state(config(other))).values["tool_results"]) == 2


@pytest.mark.parametrize("name,args,code", [
    ("unknown", "{}", "tool_not_found"),
    ("get_system_metrics", '{"password":"configured-secret"}', "invalid_tool_arguments"),
    ("get_system_metrics", "not json secret", "invalid_tool_arguments"),
])
async def test_invalid_tool_permanent_failure_and_private_checkpoint(name, args, code):
    class LLM:
        async def generate(self, *args_):
            return LLMResponse(tool_calls=[ToolCall(id="configured-secret", name=name, arguments=args)])
    state, g = initial(), graph(LLM())
    with pytest.raises(WorkflowError, match=code):
        await g.ainvoke(state, config(state), durability="sync")
    saved = await g.aget_state(config(state))
    assert saved.next == ("validate_tool",)
    assert "configured-secret" not in json.dumps(saved.values)


async def test_max_steps():
    state = initial(max_steps=1)
    with pytest.raises(WorkflowError, match="max_steps_exceeded"):
        await graph().ainvoke(state, config(state))


async def test_failed_resume_preserves_finished_tool_and_plan():
    registry, state = build_registry(), initial()
    tool = registry.get("get_mysql_slow_queries")
    original, calls = tool.execute, []
    async def fail(args):
        calls.append(1)
        raise RuntimeError("configured-secret")
    tool.execute = fail
    saver = InMemorySaver(serde=strict_serializer())
    g = graph(registry=registry, saver=saver)
    with pytest.raises(WorkflowError, match="tool_execution_error"):
        await g.ainvoke(state, config(state), durability="sync")
    saved = await g.aget_state(config(state))
    assert len(saved.values["tool_results"]) == 1 and saved.values["step_count"] == 2
    assert len(calls) == 1 and saved.next == ("execute_tool",)
    tool.execute = original
    async def never(args):
        pytest.fail("Completed first tool must not execute again")
    registry.get("get_system_metrics").execute = never
    result = await graph(registry=registry, saver=saver).ainvoke(None, config(state), durability="sync")
    assert result["status"] == "graph_completed"


@pytest.mark.parametrize("failure", [TimeoutError, ConnectionError])
async def test_transient_retry_is_bounded(failure):
    registry, state, calls = build_registry(), initial(), []
    async def fail(args):
        calls.append(1)
        raise failure("private")
    registry.get("get_system_metrics").execute = fail
    with pytest.raises(WorkflowError, match="tool_timeout"):
        await graph(registry=registry).ainvoke(state, config(state))
    assert len(calls) == 3


async def test_transient_recovers_and_real_tool_timeout():
    registry, state, calls = build_registry(), initial(), []
    tool = registry.get("get_system_metrics")
    execute = tool.execute
    async def once(args):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError
        return await execute(args)
    tool.execute = once
    assert (await graph(registry=registry).ainvoke(state, config(state)))["final_answer"]
    assert len(calls) == 2
    async def slow(args):
        await asyncio.sleep(10)
    tool.execute = slow
    state = initial()
    with pytest.raises(WorkflowError, match="tool_timeout"):
        await graph(registry=registry, tool_timeout=0.001).ainvoke(state, config(state))


async def test_cancellation_not_retried():
    registry, state, calls = build_registry(), initial(), []
    entered = asyncio.Event()
    async def cancel(args):
        calls.append(1)
        entered.set()
        await asyncio.Event().wait()
    registry.get("get_system_metrics").execute = cancel
    task = asyncio.create_task(graph(registry=registry).ainvoke(state, config(state), durability="sync"))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_interrupt_and_resume_separate_execution_node(decision):
    class Actions:
        calls = []
        async def record_action(self, workflow_id, operation_id, args):
            self.calls.append(operation_id)
            return {"operation_id": operation_id, "simulated": True, **args}
    repo, registry, state = Actions(), build_registry(), initial("mock remediation")
    registry.register(MockRemediationTool(repo))
    g = graph(WorkflowMockLLM(), registry)
    result = await g.ainvoke(state, config(state), durability="sync")
    assert "__interrupt__" in result and not repo.calls
    saved = await g.aget_state(config(state))
    assert saved.next == ("human_approval",)
    approval = saved.values["pending_approval"]
    resumed = await g.ainvoke(Command(resume={"approval_id": approval["approval_id"], "decision": decision}),
                              config(state), durability="sync")
    assert len(repo.calls) == (1 if decision == "approve" else 0)
    assert resumed["tool_results"][0]["success"] == (decision == "approve")


def test_reducer_deduplicates_and_limits_state():
    item = {"call_id": "one", "success": True}
    assert merge_results([item], [item]) == [item]
    with pytest.raises(WorkflowError, match="workflow_too_large"):
        checked_update(initial(), final_answer="a" * 262144)


def test_privacy_whitelists_fields_and_removes_sql():
    privacy, registry = CheckpointPrivacy(("configured-secret",)), build_registry()
    result = privacy.result(registry.get("get_mysql_slow_queries"), {"password": "secret", "slow_queries": [
        {"sql": "SELECT confidential", "duration_ms": 100, "rows_examined": 3, "rows_returned": 1, "secret": "x"}]})
    assert "confidential" not in json.dumps(result) and "password" not in result
    assert "configured-secret" not in privacy.text("configured-secret")


async def test_multi_call_plan_resumes_each_call_at_its_own_boundary():
    class Batch:
        async def generate(self, messages, tools):
            if messages[-1].role == "tool":
                observations = [m for m in messages if m.role == "tool"]
                assert len(observations) == 2 and len({m.tool_call_id for m in observations}) == 2
                return LLMResponse(content="done")
            return LLMResponse(tool_calls=[ToolCall(id="same", name="get_system_metrics", arguments="{}"),
                                            ToolCall(id="same", name="get_mysql_slow_queries", arguments="{}")])
    state = initial()
    result = await graph(Batch()).ainvoke(state, config(state), durability="sync")
    assert len(result["tool_results"]) == 2


async def test_unknown_safety_never_executes_or_inherits_read_permission():
    from app.tools.registry import EmptyInput, Tool
    class UnknownTool(Tool[EmptyInput]):
        name, description, input_schema = "unknown", "not trusted", EmptyInput
        async def execute(self, args):
            pytest.fail("Unclassified operation must never execute")
    class LLM:
        async def generate(self, *args):
            return LLMResponse(tool_calls=[ToolCall(id="x", name="unknown", arguments="{}")])
    registry, state = build_registry(), initial()
    registry.register(UnknownTool())
    with pytest.raises(WorkflowError, match="unsafe_tool"):
        await graph(LLM(), registry).ainvoke(state, config(state))


def test_numeric_projection_rejects_nested_private_payload():
    with pytest.raises(WorkflowError, match="invalid_tool_result"):
        CheckpointPrivacy().result(build_registry().get("get_system_metrics"),
                                   {"cpu_percent": {"password": "hidden"}})


async def test_empty_answer_after_sanitizing_is_permanent_failure():
    class LLM:
        async def generate(self, *args):
            return LLMResponse(content="\x00")
    state = initial()
    with pytest.raises(WorkflowError, match="invalid_llm_response"):
        await graph(LLM()).ainvoke(state, config(state))
