from app.observability.telemetry import observed
import hashlib
from dataclasses import asdict

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.knowledge.models import RunbookChunkRow as C, RunbookDocumentRow as D


class RunbookRepository:
    def __init__(self, engine):
        self.engine = engine

    async def signature(self, document_id):
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(D.content_hash, D.embedding_model, D.dimensions)
                   .where(D.document_id == document_id))).first()
            return tuple(row) if row else None

    @observed("vector", "import")
    async def replace(self, document, chunks, vectors, provider, fingerprint):
        provider.validate(vectors, len(chunks))
        async with self.engine.begin() as connection:
            # Serializes concurrent imports of the same document, including first import.
            key = int.from_bytes(hashlib.sha256(document.document_id.encode()).digest()[:8], 'big', signed=True)
            await connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
            values = dict(document_id=document.document_id, source_path=document.source_path,
                title=document.title, version=document.version, content_hash=fingerprint,
                embedding_model=provider.model, dimensions=provider.dimensions, metadata=document.metadata)
            statement = insert(D.__table__).values(**values)
            await connection.execute(statement.on_conflict_do_update(index_elements=['document_id'],
                set_={**values, 'updated_at': func.now()}))
            await connection.execute(delete(C).where(C.document_id == document.document_id))
            if chunks:
                await connection.execute(insert(C.__table__), [dict(**asdict(chunk), embedding=vector,
                    embedding_model=provider.model, dimensions=provider.dimensions)
                    for chunk, vector in zip(chunks, vectors, strict=True)])

    @observed("vector", "search")
    async def search(self, vector, provider, top_k, min_score):
        provider.validate([vector], 1)
        # Materialized CTE prevents mixed-dimension vectors from reaching cosine_distance.
        compatible = select(C.__table__).where(C.embedding_model == provider.model,
            C.dimensions == provider.dimensions).cte('compatible').prefix_with('MATERIALIZED')
        score = (1 - compatible.c.embedding.cosine_distance(vector)).label('score')
        query = select(compatible, score).where(score >= min_score).order_by(score.desc(), compatible.c.chunk_id).limit(top_k)
        async with self.engine.connect() as connection:
            rows = (await connection.execute(query)).mappings().all()
        return [{k: row[k] for k in ('chunk_id', 'document_id', 'source_path', 'title', 'section',
                                    'content', 'version', 'score')} for row in rows]
