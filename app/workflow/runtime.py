from contextlib import asynccontextmanager
from urllib.parse import unquote, urlsplit

from app.tools.mock_remediation import MockRemediationTool
from app.workflow.checkpoint import open_checkpointer
from app.workflow.graph import build_graph
from app.workflow.mock import WorkflowMockLLM
from app.workflow.nodes import WorkflowNodes
from app.workflow.privacy import CheckpointPrivacy
from app.workflow.repository import WorkflowRepository
from app.workflow.service import WorkflowService


@asynccontextmanager
async def workflow_service(settings, engine, llm=None, sessions=None):
    from app.main import build_registry
    repository = WorkflowRepository(engine)
    registry = build_registry()
    registry.register(MockRemediationTool(repository))
    secrets = [settings.openai_api_key.get_secret_value(), settings.workflow_api_token.get_secret_value()]
    for value in (settings.database_url, settings.checkpoint_database_url, settings.redis_url):
        secret = value.get_secret_value()
        secrets.extend([secret, unquote(urlsplit(secret).password or "")])
    privacy = CheckpointPrivacy(tuple(secrets))
    async with open_checkpointer(settings.checkpoint_database_url.get_secret_value()) as saver:
        nodes = WorkflowNodes(llm or WorkflowMockLLM(), registry, privacy,
                              llm_timeout=settings.llm_timeout_seconds, tool_timeout=settings.tool_timeout_seconds)
        yield WorkflowService(build_graph(nodes, saver), repository, privacy, sessions=sessions,
                              max_steps=settings.max_steps)
