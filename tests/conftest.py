from collections.abc import AsyncIterator

import httpx
import pytest

from app.core.config import Settings
from app.main import create_app


@pytest.fixture(autouse=True)
def isolate_environment_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in Settings.model_fields:
        monkeypatch.delenv(key.upper(), raising=False)

    async def deny_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("Tests must not use real HTTP transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_network)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(_env_file=None))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client
