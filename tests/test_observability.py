import asyncio
from contextlib import aclosing

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.config import Settings
from app.main import create_app
from app.observability.telemetry import Telemetry, current, execution_events, operation
from app.observability.tracing import SafeExporter
from app.workflow.events import WorkflowEvent


def setup_traces():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@pytest.mark.parametrize('fail', [False, True])
async def test_metrics_http_sse_business_outcome_and_trace_privacy(fail):
    from app.llm.mock import MockLLM
    class LLM(MockLLM):
        async def generate(self, messages, tools):
            if fail: raise RuntimeError('api-key-private')
            return await super().generate(messages, tools)
    app = create_app(Settings(_env_file=None, metrics_enabled=True), llm=LLM())
    provider, exporter = setup_traces()
    async with app.router.lifespan_context(app):
        app.state.telemetry = Telemetry(metrics=True, provider=provider)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/v1/diagnose/stream', json={'message': 'private-user-question'})
            assert response.status_code == 200
            assert ('event: error' if fail else 'event: final') in response.text
            result = await client.get('/metrics')
            assert result.status_code == 200
            expected = 'failed' if fail else 'completed'
            assert f'opspilot_events_total{{component="agent",event="{expected}"}} 1.0' in result.text
            assert 'opspilot_http_duration_seconds_count' in result.text
            assert 'workflow_id=' not in result.text and 'private-user-question' not in result.text
            assert app.state.telemetry.active.labels('agent')._value.get() == 0
    spans = exporter.get_finished_spans()
    http = next(s for s in spans if s.name == 'POST /api/v1/diagnose/stream')
    agent = next(s for s in spans if s.name == 'agent.execution')
    llm = next(s for s in spans if s.name == 'llm.generate')
    assert agent.parent.span_id == http.context.span_id
    assert llm.parent.span_id == agent.context.span_id
    serialized = str([(s.attributes, s.events, s.status) for s in spans])
    assert 'api-key-private' not in serialized and 'private-user-question' not in serialized
    assert current.get() is None and not trace.get_current_span().get_span_context().is_valid
    provider.shutdown()


async def test_metrics_access_disabled_and_private_registry():
    for settings, expected in [(Settings(_env_file=None), 404),
        (Settings(_env_file=None, metrics_enabled=True, metrics_token='x' * 32), 403)]:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
                assert (await c.get('/metrics')).status_code == expected
    assert Telemetry(metrics=True).registry is not Telemetry(metrics=True).registry


async def test_pending_resume_new_trace_and_cancelled_generator():
    provider, exporter = setup_traces()
    obs = Telemetry(metrics=True, provider=provider)
    token = current.set(obs)
    async def source():
        yield WorkflowEvent(type='approval_required', workflow_id='id', status='pending_approval')
    try:
        async with aclosing(execution_events(source(), 'workflow', workflow_id='id')) as events:
            assert (await anext(events)).type == 'approval_required'
        assert obs.events.labels('workflow', 'pending_approval')._value.get() == 1
        assert obs.events.labels('workflow', 'completed')._value.get() == 0
        async def resume():
            yield WorkflowEvent(type='workflow_resumed', workflow_id='id', status='running')
            await asyncio.Event().wait()
        async def consume():
            async with aclosing(execution_events(resume(), 'workflow', workflow_id='id')) as events:
                async for _ in events: pass
        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    finally:
        current.reset(token)
    spans = [s for s in exporter.get_finished_spans() if s.name == 'workflow.execution']
    assert len(spans) == 2 and spans[0].context.trace_id != spans[1].context.trace_id
    assert all(s.attributes['workflow.id'] == 'id' for s in spans)
    assert spans[1].attributes['outcome'] == 'cancelled'
    assert obs.active.labels('workflow')._value.get() == 0
    provider.shutdown()


def test_export_failure_does_not_escape_or_record_exception():
    class Broken:
        def export(self, spans): raise RuntimeError('secret-key')
        def shutdown(self): raise RuntimeError('secret-key')
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(SafeExporter(Broken())))
    token = current.set(Telemetry(provider=provider))
    try:
        with operation('llm', 'generate'): pass
    finally:
        current.reset(token)
        provider.shutdown()


async def test_unknown_paths_and_untrusted_tool_names_do_not_create_metric_labels():
    from app.llm.base import LLMResponse, ToolCall
    class LLM:
        async def generate(self, messages, tools):
            if any(m.role == 'tool' for m in messages): return LLMResponse(content='工具不可用')
            return LLMResponse(tool_calls=[ToolCall(id='1', name='untrusted_secret_name', arguments='{}')])
    app = create_app(Settings(_env_file=None, metrics_enabled=True), llm=LLM())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            await c.get('/some-private-user-path')
            await c.post('/api/v1/diagnose', json={'message': 'question'})
            metrics = (await c.get('/metrics')).text
            assert 'some-private-user-path' not in metrics and 'untrusted_secret_name' not in metrics
            assert 'route="unmatched"' in metrics


async def test_metrics_reject_forwarded_ip_spoofing():
    app = create_app(Settings(_env_file=None, metrics_enabled=True))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('203.0.113.1', 5000)), base_url='http://test') as c:
            assert (await c.get('/metrics', headers={'X-Forwarded-For':'127.0.0.1'})).status_code == 403
