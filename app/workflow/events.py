from typing import Literal

from pydantic import BaseModel


class WorkflowEvent(BaseModel):
    type: Literal["workflow_start", "workflow_resumed", "approval_required", "workflow_failed", "workflow_completed"]
    workflow_id: str
    status: str
    approval: dict | None = None
    code: str | None = None
    session_status: str | None = None
