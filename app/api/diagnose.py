from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.agent.events import ErrorEvent
from app.agent.loop import Agent
from app.agent.schemas import AgentExecutionError, DiagnoseRequest, DiagnoseResponse
from app.api.sse import SSEResponse, encode_sse
from app.core.logging import log_event

router = APIRouter(prefix="/api/v1")


async def get_agent(request: Request) -> Agent:
    return request.app.state.agent


@router.post("/diagnose", response_model=DiagnoseResponse, response_model_exclude_none=True)
async def diagnose(payload: DiagnoseRequest, agent: Annotated[Agent, Depends(get_agent)]) -> DiagnoseResponse:
    return await agent.run(payload.message)


async def stream_diagnosis(agent: Agent, message: str) -> AsyncIterator[str]:
    """Convert safe business failures to one terminal event after HTTP starts."""
    async with aclosing(agent.stream(message)) as events:
        try:
            async for event in events:
                yield encode_sse(event)
                if event.type in {"final", "error"}:
                    return
        except AgentExecutionError as error:
            yield encode_sse(ErrorEvent(code=error.code, message=error.message))
        except Exception:
            log_event("stream_failure", code="internal_error")
            yield encode_sse(ErrorEvent(code="internal_error", message="服务暂时不可用。"))
        # CancelledError and GeneratorExit are BaseExceptions: never turn them
        # into a success/error event or continue executing tools after closure.


@router.post("/diagnose/stream", response_class=SSEResponse, status_code=200,
             responses={200: {"content": {"text/event-stream": {}}}})
async def diagnose_stream(
    payload: DiagnoseRequest, agent: Annotated[Agent, Depends(get_agent)],
) -> SSEResponse:
    return SSEResponse(stream_diagnosis(agent, payload.message))
