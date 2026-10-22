from app.observability.telemetry import observed
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from langgraph.config import get_stream_writer
from functools import wraps

from app.workflow.errors import TransientWorkflowError
from app.workflow.state import GraphState
from app.workflow.events import NodeProgressEvent


def with_progress(name, function):
    @wraps(function)
    async def call(*args, **kwargs):
        writer = get_stream_writer()
        writer(NodeProgressEvent(node=name, phase="started").model_dump(exclude_none=True))
        try:
            result = await function(*args, **kwargs)
        except Exception:
            writer(NodeProgressEvent(node=name, phase="failed").model_dump(exclude_none=True))
            raise
        metadata = {}
        if name == "retrieve_runbook":
            metadata = {"retrieval_status": result.get("retrieval_status"),
                        "chunk_count": len(result.get("runbook_chunks", []))}
        writer(NodeProgressEvent(node=name, phase="completed", **metadata).model_dump(exclude_none=True))
        return result
    return call


async def after_tool(state):
    return "validate_tool" if state["pending_tool_calls"] else "plan"


async def after_plan(state):
    return "finalize" if state["final_answer"] else "validate_tool"


async def after_validation(state):
    return "human_approval" if state["pending_approval"] else "execute_tool"


async def after_approval(state):
    return "execute_tool" if state["current_tool"] else await after_tool(state)


def build_graph(nodes, checkpointer):
    builder = StateGraph(GraphState)
    retry = RetryPolicy(max_attempts=3, initial_interval=0.05, backoff_factor=2,
                        max_interval=0.2, jitter=False, retry_on=TransientWorkflowError)
    for name in ("retrieve_runbook", "plan", "validate_tool", "human_approval", "execute_tool", "finalize"):
        function = observed("node", name)(getattr(nodes, name))
        if name in {"retrieve_runbook", "plan", "execute_tool"}:
            function = with_progress(name, function)
        builder.add_node(name, function, retry_policy=retry if name in {"plan", "execute_tool"} else None)
    builder.add_edge(START, "retrieve_runbook")
    builder.add_edge("retrieve_runbook", "plan")
    builder.add_conditional_edges("plan", after_plan, ["finalize", "validate_tool"])
    builder.add_conditional_edges("validate_tool", after_validation, ["human_approval", "execute_tool"])
    builder.add_conditional_edges("human_approval", after_approval, ["execute_tool", "validate_tool", "plan"])
    builder.add_conditional_edges("execute_tool", after_tool, ["validate_tool", "plan"])
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)
