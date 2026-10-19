"""Only effect is an idempotent PostgreSQL demo row; no shell or infrastructure IO."""
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.tools.registry import Tool


class MockRemediationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target: Literal["demo-api", "demo-worker"]
    action: Literal["simulate_restart"]


class MockRemediationTool(Tool[MockRemediationInput]):
    name = "propose_mock_remediation"
    description = "仅模拟恢复：审批后登记一条数据库记录，不执行真实操作。"
    safety = "approval_required"
    checkpoint_fields = ("operation_id", "simulated", "target", "action")
    input_schema = MockRemediationInput

    def __init__(self, repository):
        self.repository = repository

    async def execute(self, arguments):
        raise RuntimeError("Durable operation_id and approval are required")

    async def execute_idempotent(self, arguments, *, workflow_id: str, operation_id: str):
        return await self.repository.record_action(workflow_id, operation_id, arguments.model_dump())
