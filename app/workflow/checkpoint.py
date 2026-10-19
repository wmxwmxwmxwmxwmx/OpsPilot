"""psycopg lifecycle is independent of SQLAlchemy/asyncpg. No automatic DDL."""
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import anyio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


def validate_checkpoint_url(value: str):
    try:
        url = urlsplit(value)
        if (url.scheme != "postgresql" or not url.hostname or not url.path.strip("/")
                or url.fragment or url.query or (url.port is not None and not 1 <= url.port <= 65535)):
            raise ValueError
    except ValueError:
        raise ValueError("CHECKPOINT_DATABASE_URL must be a postgresql URL without query or fragment") from None


def strict_serializer():
    return JsonPlusSerializer(pickle_fallback=False, allowed_json_modules=[], allowed_msgpack_modules=[])


@asynccontextmanager
async def open_checkpointer(url: str):
    validate_checkpoint_url(url)
    pool = AsyncConnectionPool(url, open=False, min_size=0, max_size=5, timeout=3,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
                "connect_timeout": 3, "options": "-c timezone=UTC -c statement_timeout=10000"})
    await pool.open()
    try:
        yield AsyncPostgresSaver(pool, serde=strict_serializer())
    finally:
        with anyio.CancelScope(shield=True):
            await pool.close(timeout=5)
