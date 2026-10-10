from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.agent.loop import Agent
from app.agent.schemas import DiagnoseRequest, DiagnoseResponse

router = APIRouter(prefix="/api/v1")


async def get_agent(request: Request) -> Agent:
    return request.app.state.agent


@router.post("/diagnose", response_model=DiagnoseResponse, response_model_exclude_none=True)
async def diagnose(payload: DiagnoseRequest, agent: Annotated[Agent, Depends(get_agent)]) -> DiagnoseResponse:
    return await agent.run(payload.message)
