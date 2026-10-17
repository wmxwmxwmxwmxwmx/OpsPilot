from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.agent.events import ErrorEvent
from app.agent.loop import Agent
from app.agent.schemas import AgentExecutionError, DiagnoseRequest, DiagnoseResponse
from app.api.sse import SSEResponse, encode_sse
from app.core.logging import log_event
from app.services.diagnosis import DiagnosisService
from app.session.errors import SessionError

router = APIRouter(prefix="/api/v1")


async def get_agent(request: Request) -> Agent:
    return request.app.state.agent


async def get_diagnosis_service(
    request: Request, agent: Annotated[Agent, Depends(get_agent)],
) -> DiagnosisService:
    return DiagnosisService(agent, request.app.state.sessions)


@router.post("/diagnose", response_model=DiagnoseResponse, response_model_exclude_none=True)
async def diagnose(
    payload: DiagnoseRequest, service: Annotated[DiagnosisService, Depends(get_diagnosis_service)],
) -> DiagnoseResponse:
    return await service.run(payload.message, payload.session_id)


async def stream_diagnosis(
    agent: Agent | DiagnosisService, message: str, session_id: UUID | None = None,
) -> AsyncIterator[str]:
    """Convert safe business failures to one terminal event after HTTP starts."""
    service = agent if isinstance(agent, DiagnosisService) else DiagnosisService(agent)
    source = service.stream(message, session_id=session_id)
    async with aclosing(source) as events:
        try:
            async for event in events:
                yield encode_sse(event)
                if event.type in {"final", "error"}:
                    return
        except (AgentExecutionError, SessionError) as error:
            yield encode_sse(ErrorEvent(code=error.code, message=error.message))
        except Exception:
            log_event("stream_failure", code="internal_error")
            yield encode_sse(ErrorEvent(code="internal_error", message="服务暂时不可用。"))
        # CancelledError and GeneratorExit are BaseExceptions: never turn them
        # into a success/error event or continue executing tools after closure.


@router.post("/diagnose/stream", response_class=SSEResponse, status_code=200,
             responses={200: {"content": {"text/event-stream": {}}}})
async def diagnose_stream(
    payload: DiagnoseRequest, service: Annotated[DiagnosisService, Depends(get_diagnosis_service)],
) -> SSEResponse:
    return SSEResponse(stream_diagnosis(service, payload.message, payload.session_id))
