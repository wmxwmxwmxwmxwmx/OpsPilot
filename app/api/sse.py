"""SSE framing and a response whose disconnect listener owns execution lifetime."""

from collections.abc import AsyncIterator
from contextlib import aclosing

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.agent.events import AgentEvent


def encode_sse(event: AgentEvent) -> str:
    # JSON escapes embedded CR/LF, quotes and backslashes; data stays on one line.
    data = event.model_dump_json(exclude={"type"}, exclude_none=True)
    return f"event: {event.type}\ndata: {data}\n\n"


class SSEResponse(StreamingResponse):
    def __init__(self, content: AsyncIterator[str]) -> None:
        super().__init__(content, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })
        self._content = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Listen even on ASGI >= 2.4: a failed send alone cannot cancel a model
        # currently waiting for IO. Both tasks are joined before closing content.
        async with aclosing(self._content):
            async with anyio.create_task_group() as tasks:
                async def send_events() -> None:
                    try:
                        await self.stream_response(send)
                    except OSError:
                        # ASGI servers may signal disconnect through send().
                        pass
                    finally:
                        tasks.cancel_scope.cancel()

                tasks.start_soon(send_events)
                await self.listen_for_disconnect(receive)
                tasks.cancel_scope.cancel()
