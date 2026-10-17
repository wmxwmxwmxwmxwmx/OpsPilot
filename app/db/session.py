from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_database_engine(url: str) -> AsyncEngine:
    # Lazy pool: startup neither connects nor applies migrations. No SQL/payload logs.
    return create_async_engine(
        url, echo=False, hide_parameters=True, pool_pre_ping=True,
        pool_size=5, max_overflow=5, pool_timeout=2,
        connect_args={"timeout": 2, "command_timeout": 5,
                      "server_settings": {"timezone": "UTC"}},
    )


@asynccontextmanager
async def database_session(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    session = factory()
    try:
        yield session
    finally:
        # AnyIO's repeated SSE cancellation must not interrupt rollback/checkin.
        # Execution and COMMIT remain cancellable; only cleanup is shielded.
        with anyio.CancelScope(shield=True):
            await session.close()
