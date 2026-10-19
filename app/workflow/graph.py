from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.workflow.errors import TransientWorkflowError
from app.workflow.state import GraphState


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
    for name in ("plan", "validate_tool", "human_approval", "execute_tool", "finalize"):
        builder.add_node(name, getattr(nodes, name), retry_policy=retry if name in {"plan", "execute_tool"} else None)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges("plan", after_plan, ["finalize", "validate_tool"])
    builder.add_conditional_edges("validate_tool", after_validation, ["human_approval", "execute_tool"])
    builder.add_conditional_edges("human_approval", after_approval, ["execute_tool", "validate_tool", "plan"])
    builder.add_conditional_edges("execute_tool", after_tool, ["validate_tool", "plan"])
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)
