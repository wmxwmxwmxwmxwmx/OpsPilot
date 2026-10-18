from uuid import UUID

from app.cache.repository import CacheRepository
from app.cache.schemas import IncidentSummary
from app.core.logging import log_event
from app.incident.errors import IncidentError
from app.incident.repository import IncidentRepository


class IncidentQueryService:
    """Internal historical metadata queries; never used for diagnosis decisions."""

    def __init__(self, incidents: IncidentRepository, cache: CacheRepository | None = None) -> None:
        self.incidents = incidents
        self.cache = cache

    async def get_summary(self, incident_id: UUID) -> IncidentSummary | None:
        if self.cache is not None:
            cached = await self.cache.get(incident_id)
            if cached is not None:
                return cached
            log_event("cache_fallback", cache_type="incident_summary", operation="get",
                      code="postgres_lookup")
        # No cache exception handler surrounds PostgreSQL: failures must propagate.
        record = await self.incidents.get(incident_id)
        if record is None:
            return None  # No negative caching and no cached database errors.
        if record.id != incident_id:
            raise IncidentError("incident_data_error", incident_id=incident_id)
        summary = IncidentSummary.from_record(record)
        if self.cache is not None:
            await self.cache.set(incident_id, summary)
        return summary

    async def invalidate(self, incident_id: UUID) -> bool:
        # M3 has no mutation path. Future writers must call AFTER PG commit and
        # still account for the concurrent stale-fill race; DEL is not atomic with PG.
        return await self.cache.invalidate(incident_id) if self.cache is not None else True
