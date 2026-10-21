import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from time import perf_counter

from opentelemetry import trace
from opentelemetry.trace import StatusCode
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

current = ContextVar('opspilot_telemetry', default=None)


class Telemetry:
    def __init__(self, *, metrics=False, provider=None):
        self.registry = CollectorRegistry()
        self.metrics = metrics
        self.provider = provider
        self.tracer = provider.get_tracer('opspilot') if provider else trace.NoOpTracer()
        self.operations = Counter('opspilot_operations_total', 'Completed operation attempts',
            ['component', 'operation', 'outcome'], registry=self.registry)
        self.duration = Histogram('opspilot_operation_duration_seconds', 'Operation wall time in seconds',
            ['component', 'operation'], registry=self.registry,
            buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60))
        self.events = Counter('opspilot_events_total', 'Business events', ['component', 'event'], registry=self.registry)
        self.active = Gauge('opspilot_active_executions', 'Currently executing segments, excludes suspended approvals',
            ['component'], registry=self.registry)
        self.chunks = Histogram('opspilot_rag_chunks', 'Chunks returned per retrieval', buckets=(0, 1, 2, 4, 8, 10), registry=self.registry)
        self.http_requests = Counter('opspilot_http_requests_total', 'HTTP requests', ['route', 'method', 'status'], registry=self.registry)
        self.http_duration = Histogram('opspilot_http_duration_seconds', 'HTTP lifetime including SSE',
            ['route', 'method'], registry=self.registry)

    def event(self, component, event):
        if self.metrics: self.events.labels(component, event).inc()

    def finish(self, component, operation, outcome, started):
        if self.metrics:
            self.operations.labels(component, operation, outcome).inc()
            self.duration.labels(component, operation).observe(perf_counter() - started)

    @contextmanager
    def span(self, name, **attributes):
        with self.tracer.start_as_current_span(name, attributes=attributes,
                record_exception=False, set_status_on_exception=False) as span:
            try:
                yield span
            except (asyncio.CancelledError, GeneratorExit):
                span.set_attribute('outcome', 'cancelled')
                raise
            except Exception:
                span.set_attribute('outcome', 'error')
                span.set_status(StatusCode.ERROR, 'operation_failed')
                raise


NOOP = Telemetry()


def telemetry():
    return current.get() or NOOP


@contextmanager
def operation(component, name, **attributes):
    obs, started, outcome = telemetry(), perf_counter(), 'success'
    with obs.span(f'{component}.{name}', **attributes) as span:
        try:
            yield span
        except (asyncio.CancelledError, GeneratorExit):
            outcome = 'cancelled'
            raise
        except Exception:
            outcome = 'error'
            raise
        finally:
            obs.finish(component, name, outcome, started)


def observed(component, name):
    def decorate(function):
        @wraps(function)
        async def call(*args, **kwargs):
            with operation(component, name):
                return await function(*args, **kwargs)
        return call
    return decorate


async def execution_events(source, component, *, workflow_id=None):
    """Attach only while advancing the generator; never carry ContextVars over yield."""
    obs, started, outcome = telemetry(), perf_counter(), 'cancelled'
    attrs = {'workflow.id': workflow_id, 'thread.id': workflow_id} if workflow_id else {}
    span = obs.tracer.start_span(f'{component}.execution', attributes=attrs)
    obs.event(component, 'started')
    if obs.metrics: obs.active.labels(component).inc()
    try:
        while True:
            with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                try:
                    event = await anext(source)
                except StopAsyncIteration:
                    break
            if event.type == 'workflow_resumed': obs.event('workflow', 'resumed')
            if event.type in {'workflow_completed', 'workflow_failed', 'approval_required'}:
                outcome = {'workflow_completed': 'completed', 'workflow_failed': 'failed',
                           'approval_required': 'pending_approval'}[event.type]
            elif component == 'agent' and event.type == 'final':
                outcome = 'completed'
            yield event
    except asyncio.CancelledError:
        outcome = 'cancelled'
        raise
    except Exception:
        outcome = 'failed'
        raise
    finally:
        try:
            with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                await source.aclose()
        finally:
            span.set_attribute('outcome', outcome)
            if outcome == 'failed': span.set_status(StatusCode.ERROR, 'execution_failed')
            span.end()
            obs.event(component, outcome)
            obs.finish(component, 'execution', outcome, started)
            if obs.metrics: obs.active.labels(component).dec()
