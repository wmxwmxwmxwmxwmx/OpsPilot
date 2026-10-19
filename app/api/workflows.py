from contextlib import aclosing
from secrets import compare_digest
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, UUID4

from app.agent.events import ErrorEvent
from app.agent.schemas import DiagnoseRequest
from app.api.sse import SSEResponse, encode_sse
from app.workflow.errors import WorkflowError

router = APIRouter(prefix="/api/v1/workflows", tags=["local workflows"])


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: UUID
    decision: Literal["approve", "reject"]


async def get_workflows(request: Request):
    service = getattr(request.app.state, "workflows", None)
    if service is None:
        raise WorkflowError("workflow_disabled", status_code=404)
    token = request.headers.get("Authorization", "")
    expected = "Bearer " + request.app.state.workflow_token
    if not compare_digest(token.encode(), expected.encode()):
        raise WorkflowError("workflow_unauthorized", status_code=403)
    return service


Service = Annotated[object, Depends(get_workflows)]


async def stream_events(service, workflow_id, **kwargs):
    try:
        async with aclosing(service.stream(workflow_id, **kwargs)) as events:
            async for event in events:
                yield encode_sse(event)
    except WorkflowError as error:
        yield encode_sse(ErrorEvent(code=error.code, message=error.code))
    except Exception:
        yield encode_sse(ErrorEvent(code="workflow_storage_error", message="工作流存储暂时不可用。"))


@router.post("")
async def create(payload: DiagnoseRequest, service: Service, stream: bool = False):
    workflow_id = await service.create(payload.message, payload.session_id)
    if stream:
        return SSEResponse(stream_events(service, workflow_id, action="start"))
    return await service.run(workflow_id, action="start")


@router.get("/{workflow_id}")
async def status(workflow_id: UUID4, service: Service):
    return await service.status(str(workflow_id))


@router.post("/{workflow_id}/resume")
async def resume(workflow_id: UUID4, service: Service, stream: bool = False):
    if stream:
        return SSEResponse(stream_events(service, str(workflow_id)))
    return await service.run(str(workflow_id))


@router.post("/{workflow_id}/approval")
async def approve(workflow_id: UUID4, payload: ApprovalRequest, service: Service, stream: bool = False):
    kwargs = {"action": "approve", "approval": payload.model_dump(mode="json")}
    if stream:
        return SSEResponse(stream_events(service, str(workflow_id), **kwargs))
    return await service.run(str(workflow_id), **kwargs)
