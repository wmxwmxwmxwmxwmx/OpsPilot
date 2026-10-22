from typing import Literal

from pydantic import BaseModel


class WorkflowEvent(BaseModel):
    type: Literal["workflow_start", "workflow_resumed", "approval_required", "workflow_failed", "workflow_completed"]
    workflow_id: str
    status: str
    approval: dict | None = None
    code: str | None = None
    session_status: str | None = None


class NodeProgressEvent(BaseModel):
    """Allowlisted execution metadata, with no graph state or model reasoning."""
    type: Literal["node_progress"] = "node_progress"
    node: Literal["retrieve_runbook", "plan", "execute_tool"]
    phase: Literal["started", "completed", "failed"]
    retrieval_status: Literal["found", "empty", "unavailable", "disabled_or_not_needed"] | None = None
    chunk_count: int | None = None
