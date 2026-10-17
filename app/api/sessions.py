from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.diagnose import get_diagnosis_service
from app.services.diagnosis import DiagnosisService
from app.session.schemas import CreateSessionRequest, CreateSessionResponse

router = APIRouter(prefix="/api/v1")


@router.post("/sessions", status_code=201, response_model=CreateSessionResponse)
async def create_session(
    service: Annotated[DiagnosisService, Depends(get_diagnosis_service)],
    payload: CreateSessionRequest | None = None,
) -> CreateSessionResponse:
    repository = service.require_sessions()
    session = await repository.create()
    return CreateSessionResponse(session_id=session.session_id,
                                 expires_in_seconds=repository.ttl_seconds)
