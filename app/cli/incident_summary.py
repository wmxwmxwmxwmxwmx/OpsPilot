"""python -m app.cli.incident_summary UUID [--invalidate]

Reads an existing incident through the same service wired in FastAPI lifespan.
It does not execute an Agent, create incidents, or expose full question/answer data.
"""
import argparse
import asyncio
import json
import sys
from uuid import UUID

from app.core.config import Settings
from app.incident.errors import IncidentError
from app.main import create_app


async def query(incident_id: UUID, *, invalidate: bool = False) -> int:
    app = create_app(Settings())
    async with app.router.lifespan_context(app):
        service = app.state.incident_queries
        if service is None:
            print("Incident queries require INCIDENT_PERSISTENCE_ENABLED=true.", file=sys.stderr)
            return 2
        if invalidate and not await service.invalidate(incident_id):
            # Do not claim a fresh read after a failed DEL. Operator can disable
            # CACHE_ENABLED and run again to explicitly bypass caching.
            print("Cache invalidation unconfirmed; disable cache for an authoritative read.", file=sys.stderr)
            return 1
        try:
            summary = await service.get_summary(incident_id)
        except IncidentError as error:
            print(json.dumps({"error": {"code": error.code, "message": error.message}},
                             ensure_ascii=False), file=sys.stderr)
            return 1
        print(summary.model_dump_json() if summary is not None else "null")
        return 0 if summary is not None else 3


def main() -> None:
    parser = argparse.ArgumentParser(description="Read a safe historical Incident summary using Cache-Aside")
    parser.add_argument("incident_id", type=UUID)
    parser.add_argument("--invalidate", action="store_true", help="Delete this summary's cache before reading")
    args = parser.parse_args()
    if args.incident_id.version != 4:
        parser.error("incident_id must be a server-generated UUID4")
    raise SystemExit(asyncio.run(query(args.incident_id, invalidate=args.invalidate)))


if __name__ == "__main__":
    main()
