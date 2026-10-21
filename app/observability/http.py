import ipaddress
import secrets
from time import perf_counter

from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.trace import SpanKind, StatusCode
import asyncio
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from app.observability.telemetry import current


class ObservabilityMiddleware:
    def __init__(self, app): self.app = app

    async def __call__(self, scope, receive, send):
        obs = getattr(scope.get('app').state, 'telemetry', None) if scope.get('app') else None
        if scope['type'] != 'http' or obs is None:
            return await self.app(scope, receive, send)
        if scope['path'] == '/metrics':
            settings = scope['app'].state.telemetry_settings
            if not settings.metrics_enabled:
                return await Response(status_code=404)(scope, receive, send)
            host = (scope.get('client') or ('', 0))[0]
            try:
                allowed = any(ipaddress.ip_address(host) in ipaddress.ip_network(cidr)
                              for cidr in settings.metrics_allowed_cidrs)
            except ValueError:
                allowed = False
            headers = dict(scope['headers'])
            token = settings.metrics_token.get_secret_value()
            if not allowed or (token and not secrets.compare_digest(headers.get(b'authorization', b''),
                                                                    ('Bearer ' + token).encode())):
                return await Response(status_code=403)(scope, receive, send)
            return await Response(generate_latest(obs.registry), headers={'Content-Type': CONTENT_TYPE_LATEST})(scope, receive, send)
        method = scope['method'] if scope['method'] in {'GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'} else 'OTHER'
        status, started = 500, perf_counter()
        async def tracked_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                if span.get_span_context().is_valid:
                    message = {**message, 'headers': [*message.get('headers', []),
                        (b'x-trace-id', format(span.get_span_context().trace_id, '032x').encode())]}
            await send(message)
        carrier = {key.decode(): value.decode() for key, value in scope['headers'] if key in {b'traceparent', b'tracestate'}}
        context = TraceContextTextMapPropagator().extract(carrier)
        token = current.set(obs)
        try:
            with obs.tracer.start_as_current_span('HTTP request', context=context, kind=SpanKind.SERVER,
                    record_exception=False, set_status_on_exception=False) as span:
                try:
                    await self.app(scope, receive, tracked_send)
                except asyncio.CancelledError:
                    span.set_attribute('outcome', 'cancelled')
                    raise
                except Exception:
                    span.set_status(StatusCode.ERROR, 'http_failed')
                    raise
                finally:
                    if status >= 500: span.set_status(StatusCode.ERROR, 'http_failed')
                    route = getattr(scope.get('route'), 'path', 'unmatched')
                    span.update_name(f'{method} {route}')
                    span.set_attributes({'http.request.method': method, 'http.route': route,
                                         'http.response.status_code': status})
                    if obs.metrics:
                        obs.http_requests.labels(route, method, str(status)).inc()
                        obs.http_duration.labels(route, method).observe(perf_counter() - started)
        finally:
            current.reset(token)
