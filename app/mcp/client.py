from app.observability.telemetry import observed
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.mcp.schemas import CATALOG, MAX_RESPONSE_BYTES


class MCPFailure(Exception):
    """Safe boundary: never expose remote exception text."""


def validate_server_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != '/mcp' or not parsed.port):
        raise ValueError('MCP requires an explicit loopback IP, port and /mcp path')
    return url


class BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream): self.stream = stream

    async def __aiter__(self):
        count = 0
        async for part in self.stream:
            count += len(part)
            if count > MAX_RESPONSE_BYTES:
                raise MCPFailure('mcp_response_too_large')
            yield part

    async def aclose(self): await self.stream.aclose()


class ControlledTransport(httpx2.AsyncBaseTransport):
    def __init__(self, url):
        self.url = httpx2.URL(url)
        self.inner = httpx2.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if request.url != self.url:
            raise MCPFailure('mcp_redirect_denied')
        request.headers['accept-encoding'] = 'identity'
        response = await self.inner.handle_async_request(request)
        # Bound decoded memory as well as wire bytes: reject compressed bodies
        # rather than accepting an arbitrarily large decompression expansion.
        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
            await response.aclose()
            raise MCPFailure('mcp_encoded_response_denied')
        response.stream = BoundedStream(response.stream)
        return response

    async def aclose(self): await self.inner.aclose()


class MCPClient:
    def __init__(self, url, timeout=5):
        self.url, self.timeout = validate_server_url(url), timeout

    @asynccontextmanager
    async def connection(self):
        # A bounded session per operation avoids cross-task AnyIO cancel-scope
        # ownership and stale trace parents on workflow resume. No hidden retries.
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
        headers = {}
        TraceContextTextMapPropagator().inject(headers)
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx2.AsyncClient(transport=ControlledTransport(self.url), trust_env=False,
                    timeout=self.timeout, follow_redirects=False, headers=headers) as http:
                    async with Client(streamable_http_client(self.url, http_client=http,
                        max_sse_event_size=MAX_RESPONSE_BYTES), cache=None,
                        read_timeout_seconds=self.timeout) as client:
                        yield client
        except asyncio.CancelledError:
            raise
        except Exception:
            raise MCPFailure('mcp_unavailable_or_invalid_response') from None

    @observed("mcp", "discovery")
    async def discover(self):
        async with self.connection() as client:
            result = await client.list_tools()
            if result.next_cursor or len(result.tools) > 16:
                raise MCPFailure('mcp_catalog_too_large')
            return result.tools

    @observed("mcp", "call")
    async def call(self, name, arguments):
        if name not in CATALOG or arguments:
            raise MCPFailure('mcp_tool_or_arguments_denied')
        async with self.connection() as client:
            result = await client.call_tool(name, arguments)
            if result.is_error:
                raise MCPFailure('mcp_tool_failed')
            if len(result.model_dump_json().encode()) > MAX_RESPONSE_BYTES:
                raise MCPFailure('mcp_response_too_large')
            data = result.structured_content
            if not isinstance(data, dict):
                raise MCPFailure('mcp_invalid_result')
            return data

    async def register_tools(self, registry):
        from app.mcp.adapter import MCPTool
        seen = set()
        for definition in await self.discover():
            if definition.name not in CATALOG:
                continue
            if definition.name in seen:
                raise MCPFailure('mcp_duplicate_tool')
            seen.add(definition.name)
            registry.register(MCPTool(self, definition))
