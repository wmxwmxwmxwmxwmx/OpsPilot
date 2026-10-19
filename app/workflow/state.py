"""Only bounded JSON primitives enter durable state, never live clients."""
import json
from typing import Annotated, TypedDict

from app.workflow.errors import WorkflowError

MAX_STATE_BYTES = 262144
MAX_MESSAGES = 128
MAX_RESULTS = 64


def merge_results(left: list[dict], right: list[dict]) -> list[dict]:
    merged = {item["call_id"]: item for item in left}
    merged.update({item["call_id"]: item for item in right})
    if len(merged) > MAX_RESULTS:
        raise WorkflowError("workflow_too_large")
    return list(merged.values())


class GraphState(TypedDict):
    schema_version: int
    workflow_id: str
    session_id: str | None
    session_version: int | None
    incident_id: str
    user_message: str
    llm_messages: list[dict]
    pending_tool_calls: list[dict]
    current_tool: dict | None
    tool_results: Annotated[list[dict], merge_results]
    step_count: int
    max_steps: int
    status: str
    final_answer: str | None
    pending_approval: dict | None
    error_code: str | None


def checked_update(state: dict, **updates) -> dict:
    candidate = {**state, **updates}
    if "tool_results" in updates:
        candidate["tool_results"] = merge_results(state.get("tool_results", []), updates["tool_results"])
    if (len(candidate.get("llm_messages", [])) > MAX_MESSAGES
            or len(json.dumps(candidate, ensure_ascii=False, allow_nan=False).encode()) > MAX_STATE_BYTES):
        raise WorkflowError("workflow_too_large")
    return updates
