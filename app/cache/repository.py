import asyncio
from time import perf_counter
from uuid import UUID

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.cache.keys import DEFAULT_KEY_PREFIX, incident_summary_key, validate_key_prefix
from app.cache.schemas import IncidentSummary
from app.core.logging import log_event


class CacheRepository:
    """Best-effort Redis IO; owns no client lifecycle and never touches PostgreSQL."""

    def __init__(self, client: Redis, *, ttl_seconds: int = 300,
                 key_prefix: str = DEFAULT_KEY_PREFIX, max_bytes: int = 65536,
                 io_timeout_seconds: float = 0.5) -> None:
        if (not 1 <= ttl_seconds <= 604800 or not 1 <= max_bytes <= 1048576
                or not 0 < io_timeout_seconds <= 10):
            raise ValueError("Cache limits must be positive and within configured bounds")
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.key_prefix = validate_key_prefix(key_prefix)
        self.max_bytes = max_bytes
        self.io_timeout_seconds = io_timeout_seconds

    def key(self, incident_id: UUID) -> str:
        return incident_summary_key(incident_id, self.key_prefix)

    @staticmethod
    def _log(event: str, operation: str, started: float, *, code: str | None = None) -> None:
        # No key, ID, prefix, URL, exception text, payload or configured credentials.
        log_event(event, cache_type="incident_summary", operation=operation, code=code,
                  latency_ms=round((perf_counter() - started) * 1000, 3))

    async def get(self, incident_id: UUID) -> IncidentSummary | None:
        key, started = self.key(incident_id), perf_counter()
        try:
            async with asyncio.timeout(self.io_timeout_seconds):
                payload = await self.client.get(key)
        except (RedisError, TimeoutError):
            self._log("cache_error", "get", started, code="cache_read_error")
            return None
        if payload is None:
            self._log("cache_miss", "get", started)
            return None
        try:
            size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
            if size > self.max_bytes:
                raise ValueError("Oversized cache entry")
            summary = IncidentSummary.model_validate_json(payload)
            if summary.incident_id != incident_id:
                raise ValueError("Cache identity mismatch")
        except (ValidationError, ValueError, UnicodeError):
            self._log("cache_error", "get", started, code="cache_data_error")
            # Best effort eviction; a concurrent valid fill may also be removed.
            # For immutable records that only causes another safe cache miss.
            await self.invalidate(incident_id)
            return None
        self._log("cache_hit", "get", started)
        return summary

    async def set(self, incident_id: UUID, summary: IncidentSummary) -> bool:
        key, started = self.key(incident_id), perf_counter()
        try:
            validated = IncidentSummary.model_validate(summary)
            if validated.incident_id != incident_id:
                raise ValueError("Cache identity mismatch")
            payload = validated.model_dump_json()
            if len(payload.encode("utf-8")) > self.max_bytes:
                self._log("cache_error", "set", started, code="cache_too_large")
                return False
        except (ValidationError, ValueError, UnicodeError):
            self._log("cache_error", "set", started, code="cache_data_error")
            return False
        try:
            async with asyncio.timeout(self.io_timeout_seconds):
                accepted = await self.client.set(key, payload, ex=self.ttl_seconds)
        except (RedisError, TimeoutError):
            self._log("cache_error", "set", started, code="cache_write_error")
            return False
        if not accepted:
            self._log("cache_error", "set", started, code="cache_write_error")
            return False
        self._log("cache_set", "set", started)
        return True

    async def invalidate(self, incident_id: UUID) -> bool:
        """True means DEL acknowledged (including absent key); False means unknown."""
        key, started = self.key(incident_id), perf_counter()
        try:
            async with asyncio.timeout(self.io_timeout_seconds):
                await self.client.delete(key)
        except (RedisError, TimeoutError):
            self._log("cache_error", "invalidate", started, code="cache_invalidate_error")
            return False
        self._log("cache_invalidate", "invalidate", started)
        return True
