import pytest
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.workflow.checkpoint import strict_serializer, validate_checkpoint_url


async def test_workflow_api_is_closed_by_default(client):
    for path in ("/api/v1/workflows", "/api/v1/workflows?stream=true"):
        response = await client.post(path, json={"message": "hi"})
        assert response.status_code == 404 and response.json()["error"]["code"] == "workflow_disabled"


@pytest.mark.parametrize("kwargs", [
    {"workflow_enabled": True}, {"agent_engine": "langgraph"},
    {"workflow_enabled": True, "workflow_local_demo": True, "workflow_api_token": "short"},
])
def test_workflow_requires_explicit_local_gate_and_token(kwargs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **kwargs)


@pytest.mark.parametrize("url", ["", "postgresql+asyncpg://host/db", "postgresql://host/db?password=hidden", "file:///tmp/x"])
def test_checkpoint_url_separate_from_asyncpg(url):
    with pytest.raises(ValueError):
        validate_checkpoint_url(url)


def test_strict_serializer_rejects_arbitrary_objects():
    class UntrustedModel(BaseModel):
        value: str
    serializer = strict_serializer()
    assert serializer.pickle_fallback is False
    encoded = serializer.dumps_typed(UntrustedModel(value="test"))
    assert not isinstance(serializer.loads_typed(encoded), UntrustedModel)


def test_engine_setting_enables_separate_workflow_api():
    config = Settings(_env_file=None, agent_engine="langgraph", workflow_local_demo=True,
        workflow_api_token="x" * 32, database_url="postgresql+asyncpg://localhost/db",
        checkpoint_database_url="postgresql://localhost/db")
    assert config.workflow_enabled and not config.incident_persistence_enabled


@pytest.mark.parametrize("days", [0, -1, 91])
def test_maintenance_rejects_invalid_retention_before_deletion(days):
    from app.cli.checkpoints import CheckpointSettings
    with pytest.raises(ValidationError):
        CheckpointSettings(_env_file=None, checkpoint_database_url="postgresql://localhost/db",
                           workflow_retention_days=days)
