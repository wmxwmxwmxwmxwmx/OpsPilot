"""Console metadata and opt-in progress exercise the real graph/SSE boundary."""
from contextlib import asynccontextmanager
import json
from uuid import uuid4

import httpx
import pytest

from app.api.workflows import get_workflows
from app.core.config import Settings
from app.main import create_app
from app.workflow.service import WorkflowService
from app.workflow.privacy import CheckpointPrivacy
from test_workflow_graph import graph


class MemoryRepository:
    def __init__(self):
        self.rows = {}

    async def create(self, state):
        self.rows[state['workflow_id']] = dict(initial_state=state, status='created',
            checkpoint_pruned=False, resumable=True, error_code=None, session_status='not_requested',
            incident_id=state['incident_id'])

    async def get(self, workflow_id):
        return dict(self.rows[workflow_id])

    async def update(self, workflow_id, **values):
        self.rows[workflow_id].update(values)

    async def complete(self, workflow_id, state, session_status):
        await self.update(workflow_id, status='completed', resumable=False, session_status=session_status)

    @asynccontextmanager
    async def lock(self, workflow_id):
        yield


async def test_console_capabilities_are_only_public_flags():
    app = create_app(Settings(_env_file=None, openai_api_key='private-key-sentinel'))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/api/v1/capabilities')
    assert response.json() == dict(llm_mode='mock', sessions=False, workflows=False,
                                  rag=False, mcp=False, tracing=False, tool_data_source='mock_tools')
    assert 'private-key-sentinel' not in response.text


@pytest.mark.parametrize('progress', [False, True])
async def test_opt_in_progress_over_real_workflow_sse(progress):
    class Retriever:
        async def retrieve(self, query):
            return []
    service = WorkflowService(graph(retriever=Retriever()), MemoryRepository(), CheckpointPrivacy(()))
    app = create_app(Settings(_env_file=None))
    async def get_service():
        return service
    app.dependency_overrides[get_workflows] = get_service
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/v1/workflows?stream=true&progress={str(progress).lower()}',
                                         json={'message': 'Redis 缓存排查 secret-user-question'})
    assert response.status_code == 200
    frames = [frame.splitlines() for frame in response.text.strip().split('\n\n')]
    types = [frame[0].removeprefix('event: ') for frame in frames]
    assert types[0] == 'workflow_start'
    assert types[-2:] == ['final', 'workflow_completed']
    updates = [json.loads(frame[1].removeprefix('data: ')) for frame in frames if frame[0] == 'event: node_progress']
    assert bool(updates) == progress
    if progress:
        assert updates[0] == {'node': 'retrieve_runbook', 'phase': 'started'}
        assert updates[1] == {'node': 'retrieve_runbook', 'phase': 'completed',
                              'retrieval_status': 'empty', 'chunk_count': 0}
        assert {'node': 'plan', 'phase': 'started'} in updates
        assert {'node': 'execute_tool', 'phase': 'completed'} in updates
        assert 'secret-user-question' not in json.dumps(updates)
        assert all(set(event) <= {'node', 'phase', 'retrieval_status', 'chunk_count'} for event in updates)


async def test_progress_reports_retrieval_degradation_without_leaking_exception():
    class BrokenRetriever:
        async def retrieve(self, query):
            raise RuntimeError('private-retrieval-credentials')
    service = WorkflowService(graph(retriever=BrokenRetriever()), MemoryRepository(), CheckpointPrivacy(()))
    wid = await service.create('Redis 排查')
    events = [event async for event in service.stream(wid, action='start', include_progress=True)]
    assert any(getattr(event, 'retrieval_status', None) == 'unavailable' for event in events)
    assert events[-1].type == 'workflow_completed'
    assert 'private-retrieval-credentials' not in str(events)


@pytest.mark.parametrize('path,body,action', [
    ('resume', {}, 'resume'),
    ('approval', {'approval_id': str(uuid4()), 'decision': 'reject'}, 'approve'),
])
async def test_approval_and_resume_forward_progress_flag(path, body, action):
    from app.workflow.events import WorkflowEvent
    calls = []
    wid = str(uuid4())
    class Service:
        async def stream(self, workflow_id, **kwargs):
            calls.append((workflow_id, kwargs))
            yield WorkflowEvent(type='workflow_completed', workflow_id=workflow_id, status='completed')
    app = create_app(Settings(_env_file=None))
    async def get_service():
        return Service()
    app.dependency_overrides[get_workflows] = get_service
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = await client.post(f'/api/v1/workflows/{wid}/{path}?stream=true&progress=true', json=body)
    assert 'event: workflow_completed' in result.text
    assert calls[0][0] == wid and calls[0][1]['include_progress'] is True
    assert calls[0][1].get('action', 'resume') == action
    if path == 'approval':
        assert calls[0][1]['approval'] == body
