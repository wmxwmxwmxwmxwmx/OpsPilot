"""python -m app.mcp.server; independent loopback-only mock server."""
from app.observability.telemetry import observed
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from typing import Any

from app.tools.registry import EmptyInput
from app.tools.system_metrics import SystemMetricsTool
from app.tools.mysql_diagnostics import MySQLDiagnosticsTool


def create_server(*, fail_redis=False):
    server = MCPServer('OpsPilot controlled mock diagnostics', subscriptions=False)
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=readonly)
    @observed("remote_tool", "get_mock_system_metrics")
    async def get_mock_system_metrics() -> dict[str, Any]:
        """Return fixed development system metrics; no live host access."""
        return await SystemMetricsTool().execute(EmptyInput())

    @server.tool(annotations=readonly)
    @observed("remote_tool", "get_mock_redis_metrics")
    async def get_mock_redis_metrics() -> dict[str, Any]:
        """Return a simulated low cache hit rate; no Redis connection."""
        if fail_redis:
            raise RuntimeError('simulated_redis_failure')
        return {'used_memory_mb': 312, 'hit_rate': 0.42, 'connected_clients': 84, 'blocked_clients': 0}

    @server.tool(annotations=readonly)
    @observed("remote_tool", "get_mock_mysql_status")
    async def get_mock_mysql_status() -> dict[str, Any]:
        """Return fixed development query metrics; no MySQL connection."""
        return await MySQLDiagnosticsTool().execute(EmptyInput())

    return server


def create_app(settings=None, *, fail_redis=False):
    from contextlib import asynccontextmanager
    import asyncio
    from app.core.config import Settings
    from app.observability.http import ObservabilityMiddleware
    from app.observability.telemetry import Telemetry
    from app.observability.tracing import create_provider
    settings = settings or Settings()
    app = create_server(fail_redis=fail_redis).streamable_http_app(stateless_http=True, json_response=True)
    original = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        provider = create_provider(settings) if settings.tracing_enabled else None
        application.state.telemetry = Telemetry(provider=provider)
        application.state.telemetry_settings = settings
        try:
            async with original(application):
                yield
        finally:
            if provider: await asyncio.to_thread(provider.shutdown)
    app.router.lifespan_context = lifespan
    app.add_middleware(ObservabilityMiddleware)
    return app


def main():
    import uvicorn
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--fail-redis', action='store_true', help='Controlled mock failure for observability checks')
    args = parser.parse_args()
    uvicorn.run(create_app(fail_redis=args.fail_redis), host='127.0.0.1', port=8765, access_log=False)


if __name__ == '__main__':
    main()
