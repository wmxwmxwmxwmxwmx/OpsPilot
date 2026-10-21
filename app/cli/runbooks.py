import asyncio
from pathlib import Path

import httpx

from app.core.config import Settings
from app.db.session import create_database_engine
from app.knowledge.embeddings import build_embedding
from app.knowledge.repository import RunbookRepository
from app.knowledge.service import import_runbooks


async def run():
    settings = Settings()
    engine = create_database_engine(settings.database_url.get_secret_value())
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            print(await import_runbooks(Path(settings.rag_runbook_path), RunbookRepository(engine),
                build_embedding(settings, client), size=settings.rag_chunk_size, overlap=settings.rag_chunk_overlap))
    finally:
        await engine.dispose()


def main():
    asyncio.run(run())


if __name__ == '__main__':
    main()
