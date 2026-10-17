"""Allowlisted metadata only: never retain arguments, call IDs or observations."""

from typing import get_args

from app.agent.schemas import ToolResultStep
from app.incident.schemas import MAX_SUMMARY_TOOLS, StepSummary, ToolErrorCode, ToolName, ToolSummary


class SummaryBuilder:
    def __init__(self) -> None:
        self._tools: list[ToolSummary] = []
        self._count = 0

    def add(self, step: ToolResultStep) -> None:
        self._count += 1
        if len(self._tools) >= MAX_SUMMARY_TOOLS:
            return
        # Unknown model-generated tool names and arbitrary error messages could
        # contain credentials. Never accept them as persistent metadata.
        name = step.tool if step.tool in get_args(ToolName) else "unregistered_tool"
        code = None if step.success else (step.error or {}).get("code")
        if not step.success and code not in get_args(ToolErrorCode):
            code = "tool_execution_error"
        self._tools.append(ToolSummary(name=name, success=step.success, error_code=code))

    def build(self) -> StepSummary:
        return StepSummary(tools=tuple(self._tools), tool_count=self._count,
                           truncated=self._count > len(self._tools))
