import asyncio
import json
from contextlib import aclosing
from uuid import UUID, uuid4

import anyio
from langgraph.types import Command
from langsmith import tracing_context

from app.agent.events import FinalAnswerEvent
from app.agent.schemas import ToolCallStep, ToolResultStep
from app.core.logging import log_event
from app.session.errors import SessionError
from app.workflow.errors import WorkflowError
from app.workflow.events import NodeProgressEvent, WorkflowEvent
from app.workflow.session_commit import commit_session
from app.workflow.state import checked_update


class WorkflowService:
    def __init__(self, graph, repository, privacy, *, sessions=None, max_steps=6):
        self.graph, self.repository, self.privacy = graph, repository, privacy
        self.sessions, self.max_steps = sessions, min(max_steps, 32)

    @staticmethod
    def config(workflow_id):
        return {"configurable": {"thread_id": workflow_id}, "recursion_limit": 512,
                "callbacks": []}

    async def create(self, message, session_id=None):
        snapshot = None
        if session_id is not None:
            if self.sessions is None:
                raise SessionError("session_disabled")
            snapshot = await self.sessions.load(session_id)
        message = self.privacy.text(message)
        if not message.strip():
            raise WorkflowError("invalid_message", status_code=422)
        history = [{"role": m.role, "content": self.privacy.text(m.content, 2000)}
                   for m in snapshot.messages[-20:]] if snapshot else []
        state = dict(schema_version=1, workflow_id=str(uuid4()), session_id=str(session_id) if session_id else None,
            session_version=snapshot.version if snapshot else None, incident_id=str(uuid4()), user_message=message,
            llm_messages=[*history, {"role": "user", "content": message}], pending_tool_calls=[], current_tool=None,
            tool_results=[], step_count=0, max_steps=self.max_steps, status="running", final_answer=None,
            pending_approval=None, error_code=None)
        checked_update({}, **state)
        await self.repository.create(state)
        return state["workflow_id"]

    async def snapshot(self, workflow_id):
        # Explicitly disable automatic external tracing, including checkpoint reads.
        with tracing_context(enabled=False):
            return await self.graph.aget_state(self.config(workflow_id))

    async def status(self, workflow_id):
        row = await self.repository.get(workflow_id)
        saved = await self.snapshot(workflow_id) if not row["checkpoint_pruned"] else None
        values = saved.values if saved else {}
        interrupted = bool(saved and any(task.interrupts for task in saved.tasks))
        status = "pending_approval" if interrupted and row["status"] != "completed" else row["status"]
        return {"workflow_id": workflow_id, "status": status,
                "current_step": saved.next[0] if saved and saved.next else None,
                "pending_approval": values.get("pending_approval") if interrupted else None,
                "error_code": row["error_code"], "resumable": row["resumable"],
                "session_status": row["session_status"],
                "incident_id": str(row["incident_id"]) if row["status"] == "completed" else None,
                "checkpoint_pruned": row["checkpoint_pruned"]}

    async def run(self, workflow_id, *, action="resume", approval=None):
        answer = None
        async with aclosing(self.stream(workflow_id, action=action, approval=approval)) as events:
            async for event in events:
                if event.type == "final":
                    answer = event.answer
        result = await self.status(workflow_id)
        if answer is not None:
            result["answer"] = answer
        return result

    async def stream(self, workflow_id, *, action="resume", approval=None, include_progress=False):
        from app.observability.telemetry import execution_events
        async with aclosing(execution_events(self._stream(workflow_id, action=action, approval=approval,
                                                         include_progress=include_progress),
                                            "workflow", workflow_id=workflow_id)) as events:
            async for event in events:
                yield event

    async def _stream(self, workflow_id, *, action="resume", approval=None, include_progress=False):
        async with self.repository.lock(workflow_id):
            row = await self.repository.get(workflow_id)
            if row["status"] in {"completed", "cancelled"}:
                raise WorkflowError("workflow_terminal")
            saved = await self.snapshot(workflow_id)
            has_checkpoint = saved.metadata is not None
            if saved.values and saved.values.get("schema_version") != 1:
                raise WorkflowError("checkpoint_version_unsupported")
            interrupted = bool(saved.values and any(task.interrupts for task in saved.tasks))
            if action == "approve":
                if not interrupted:
                    raise WorkflowError("workflow_not_pending_approval")
                pending = saved.values["pending_approval"]
                if not approval or approval["approval_id"] != pending["approval_id"]:
                    raise WorkflowError("invalid_approval")
                graph_input = Command(resume=approval)
            else:
                if interrupted:
                    raise WorkflowError("approval_required")
                if row["status"] == "failed" and not row["resumable"]:
                    raise WorkflowError("workflow_not_resumable")
                # Only a workflow without ANY checkpoint may initialize. All failed
                # node / completed graph recovery uses None with the original thread.
                graph_input = None if has_checkpoint else row["initial_state"]
            await self.repository.update(workflow_id, status="running", error_code=None)
            log_event("workflow_running", workflow_id=workflow_id)
            try:
                yield WorkflowEvent(type="workflow_resumed" if has_checkpoint else "workflow_start",
                                    workflow_id=workflow_id, status="running")
                if not has_checkpoint or saved.next:
                    with tracing_context(enabled=False):
                        source = self.graph.astream(graph_input, self.config(workflow_id),
                                                    stream_mode=["updates", "custom"] if include_progress else "updates",
                                                    durability="sync")
                        async with aclosing(source) as updates:
                            async for update in updates:
                                if include_progress:
                                    mode, update = update
                                    if mode == "custom":
                                        yield NodeProgressEvent.model_validate(update)
                                        continue
                                # Only updates from THIS invocation become events.
                                # Historical state/results are never replayed as SSE.
                                for node, delta in update.items():
                                    if node == "__interrupt__" or not isinstance(delta, dict):
                                        continue
                                    if node == "validate_tool" and delta.get("current_tool"):
                                        call = delta["current_tool"]
                                        yield ToolCallStep(tool=call["name"], call_id=call["id"],
                                                           arguments=json.loads(call["arguments"]))
                                    for result in delta.get("tool_results", []):
                                        yield ToolResultStep.model_validate(result)
                saved = await self.snapshot(workflow_id)
                if any(task.interrupts for task in saved.tasks):
                    await self.repository.update(workflow_id, status="pending_approval", resumable=True)
                    log_event("workflow_paused", workflow_id=workflow_id)
                    yield WorkflowEvent(type="approval_required", workflow_id=workflow_id,
                        status="pending_approval", approval=saved.values["pending_approval"])
                    return
                if saved.next or saved.values.get("status") != "graph_completed":
                    raise WorkflowError("workflow_incomplete", resumable=True)
                row = await self.repository.get(workflow_id)
                session_status = row["session_status"]
                if session_status not in {"committed", "expired", "conflict", "not_requested"}:
                    session_status = await commit_session(self.sessions, saved.values)
                    await self.repository.update(workflow_id, session_status=session_status)
                await self.repository.complete(workflow_id, saved.values, session_status)
                log_event("workflow_completed", workflow_id=workflow_id)
                yield FinalAnswerEvent(answer=saved.values["final_answer"], incident_id=UUID(saved.values["incident_id"]))
                yield WorkflowEvent(type="workflow_completed", workflow_id=workflow_id,
                                    status="completed", session_status=session_status)
            except (asyncio.CancelledError, GeneratorExit):
                # A disconnect cancels current IO, not the durable workflow. A hard
                # process kill leaves running; the next lock owner can resume it.
                with anyio.move_on_after(3, shield=True):
                    latest = await self.repository.get(workflow_id)
                    if latest["status"] != "completed":
                        await self.repository.update(workflow_id, status="failed", error_code="connection_interrupted",
                                                     resumable=True)
                raise
            except Exception as error:
                code = error.code if isinstance(error, (WorkflowError, SessionError)) else "workflow_storage_error"
                resumable = error.resumable if isinstance(error, WorkflowError) else True
                # A lost business COMMIT response can already have completed the row.
                latest = await self.repository.get(workflow_id)
                if latest["status"] == "completed":
                    # Stable ID + atomic row status proves a lost COMMIT succeeded.
                    yield FinalAnswerEvent(answer=saved.values["final_answer"], incident_id=latest["incident_id"])
                    yield WorkflowEvent(type="workflow_completed", workflow_id=workflow_id, status="completed",
                                        session_status=latest["session_status"])
                else:
                    await self.repository.update(workflow_id, status="failed", error_code=code, resumable=resumable)
                    log_event("workflow_failed", workflow_id=workflow_id, code=code)
                    yield WorkflowEvent(type="workflow_failed", workflow_id=workflow_id, status="failed", code=code)
