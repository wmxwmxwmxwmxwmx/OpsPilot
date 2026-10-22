from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "opspilot"}


@router.get("/api/v1/capabilities")
async def capabilities(request: Request):
    """Public demo feature flags only; never expose credentials or connection URLs."""
    settings = request.app.state.telemetry_settings
    return {
        "llm_mode": settings.llm_mode,
        "sessions": settings.session_enabled,
        "workflows": settings.workflow_enabled,
        "rag": settings.rag_enabled,
        "mcp": settings.mcp_enabled,
        "tracing": settings.tracing_enabled,
        "tool_data_source": "mock_tools",
    }
