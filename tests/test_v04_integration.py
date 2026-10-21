"""Opt-in real pgvector and Streamable HTTP; fake vectors explicitly test plumbing."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import socket

import httpx
import pytest
from sqlalchemy import func, select
import uvicorn

from app.core.config import Settings
from app.knowledge.embeddings import FakeEmbedding
from app.knowledge.models import RunbookChunkRow, RunbookDocumentRow
from app.knowledge.repository import RunbookRepository
from app.knowledge.service import import_runbooks
from app.mcp.client import MCPClient, MCPFailure
from app.mcp.server import create_app as mcp_app
from app.observability.telemetry import Telemetry, current, operation
from test_observability import setup_traces


@pytest.mark.postgres_integration
async def test_real_pgvector_import_search_versions_and_dimensions(database, tmp_path):
    engine, _ = database
    repo, provider = RunbookRepository(engine), FakeEmbedding()
    assert (await import_runbooks(Path('runbooks'), repo, provider))['imported'] == 6
    async with engine.connect() as c:
        before = await c.scalar(select(func.count()).select_from(RunbookChunkRow))
        assert await c.scalar(select(func.count()).select_from(RunbookDocumentRow)) == 6
    assert (await import_runbooks(Path('runbooks'), repo, provider))['unchanged'] == 6
    vector = (await provider.embed(['Redis 缓存命中率下降']))[0]
    rows = await repo.search(vector, provider, 4, -1)
    assert len(rows) == 4 and rows[0]['score'] >= rows[-1]['score']
    assert all(r['source_path'] in {str(p.relative_to('runbooks')) for p in Path('runbooks').rglob('*.md')} for r in rows)
    assert await repo.search(vector, provider, 4, 1) == []
    with pytest.raises(ValueError): await repo.search([1], provider, 4, 0)
    # Model/dimension replacement removes obsolete chunks atomically.
    upgraded = FakeEmbedding(32)
    await import_runbooks(Path('runbooks'), repo, upgraded)
    assert await repo.search(vector, provider, 4, -1) == []
    async with engine.connect() as c:
        assert await c.scalar(select(func.count()).select_from(RunbookChunkRow)) == before
        assert (await c.execute(select(RunbookChunkRow.dimensions).distinct())).scalars().all() == [32]
    source = Path('runbooks/redis/low_cache_hit_rate.md').read_text()
    (tmp_path / 'redis.md').write_text(source.replace('version: 1.0', 'version: 2.0'))
    await import_runbooks(tmp_path, repo, upgraded)
    async with engine.connect() as c:
        assert (await c.execute(select(RunbookChunkRow.version).where(
            RunbookChunkRow.document_id == 'redis/low_cache_hit_rate'))).scalars().unique().all() == ['2.0']


@asynccontextmanager
async def serve(app):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done(): await task
                await asyncio.sleep(.01)
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


@pytest.mark.mcp_integration
async def test_real_mcp_http_discovery_call_timeout_and_cross_service_trace(monkeypatch):
    provider, exporter = setup_traces()
    monkeypatch.setattr('app.observability.tracing.create_provider', lambda settings: provider)
    obs = Telemetry(metrics=True, provider=provider)
    token = current.set(obs)
    try:
        async with serve(mcp_app(Settings(_env_file=None, tracing_enabled=True))) as url:
            client = MCPClient(url + '/mcp')
            assert len(await client.discover()) == 3
            with operation('workflow', 'execution'):
                assert (await client.call('get_mock_redis_metrics', {}))['hit_rate'] == .42
        spans = exporter.get_finished_spans()
        remote = next(s for s in spans if s.name == 'remote_tool.get_mock_redis_metrics')
        mcp = next(s for s in spans if s.name == 'mcp.call')
        assert remote.context.trace_id == mcp.context.trace_id
        # SDK W3C _meta may parent the tool directly to the client span.
        by_id = {s.context.span_id: s for s in spans}
        ancestor = remote
        while ancestor.parent and ancestor.parent.span_id in by_id:
            ancestor = by_id[ancestor.parent.span_id]
            if ancestor.context.span_id == mcp.context.span_id: break
        assert ancestor.context.span_id == mcp.context.span_id
        assert any(s.name == 'POST /mcp' and s.parent.span_id == mcp.context.span_id for s in spans)
        assert obs.operations.labels('mcp', 'call', 'success')._value.get() == 1
        # Now the same port has no server: safe error, no uncaught transport text.
        with pytest.raises(MCPFailure, match='mcp_unavailable'):
            await client.call('get_mock_redis_metrics', {})
    finally:
        current.reset(token)


@pytest.mark.mcp_integration
@pytest.mark.postgres_integration
async def test_real_redis_diagnosis_rag_mcp_incident_metrics_trace(database, monkeypatch):
    from app.main import create_app
    from app.workflow.checkpoint import open_checkpointer
    from app.db.models import IncidentRow
    engine, url = database
    checkpoint_url = url.replace('postgresql+asyncpg:', 'postgresql:')
    async with open_checkpointer(checkpoint_url) as saver: await saver.setup()
    await import_runbooks(Path('runbooks'), RunbookRepository(engine), FakeEmbedding())
    provider, exporter = setup_traces()
    monkeypatch.setattr('app.observability.tracing.create_provider', lambda settings: provider)
    async with serve(mcp_app()) as remote:
        settings = Settings(_env_file=None, workflow_enabled=True, workflow_local_demo=True,
            workflow_api_token='x' * 32, database_url=url, checkpoint_database_url=checkpoint_url,
            rag_enabled=True, rag_min_score=0, mcp_enabled=True, mcp_server_url=remote + '/mcp',
            metrics_enabled=True, tracing_enabled=True)
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post('/api/v1/workflows?stream=true',
                    headers={'Authorization': 'Bearer ' + 'x' * 32},
                    json={'message': 'Redis 缓存命中率突然下降，请帮我排查原因，并参考运维 Runbook 给出建议。'})
                assert response.status_code == 200
                assert 'event: workflow_completed' in response.text
                assert 'mcp_get_mock_redis_metrics' in response.text and '42%' in response.text
                assert 'redis/' in response.text and 'chunk:' in response.text
                metrics = (await client.get('/metrics')).text
                for component in ['rag', 'mcp', 'tool', 'workflow']:
                    assert f'component="{component}"' in metrics
    async with engine.connect() as c:
        assert await c.scalar(select(func.count()).select_from(IncidentRow)) == 1
    names = {s.name for s in exporter.get_finished_spans()}
    assert {'rag.retrieve', 'embedding.generate', 'vector.search', 'llm.generate', 'mcp.call',
            'workflow.execution', 'persistence.incident'} <= names
