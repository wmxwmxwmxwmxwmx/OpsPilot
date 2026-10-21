"""Semantic evaluation uses an explicitly configured real provider, never fake vectors."""
import os
from pathlib import Path

import httpx
import pytest

from app.knowledge.embeddings import APIEmbedding
from app.knowledge.loader import load_documents


@pytest.mark.embedding_integration
async def test_real_embedding_redis_relevance():
    names = ['OPSPILOT_TEST_EMBEDDING_URL', 'OPSPILOT_TEST_EMBEDDING_KEY', 'OPSPILOT_TEST_EMBEDDING_MODEL',
             'OPSPILOT_TEST_EMBEDDING_DIMENSIONS']
    if not all(os.getenv(name) for name in names):
        pytest.skip('Requires explicit real embedding API URL/key/model/dimensions; fake is not semantic validation')
    async with httpx.AsyncClient(follow_redirects=False) as client:
        provider = APIEmbedding(client, base_url=os.environ[names[0]], api_key=os.environ[names[1]],
            model=os.environ[names[2]], dimensions=int(os.environ[names[3]]))
        documents = load_documents(Path('runbooks'))
        vectors = await provider.embed(['Redis 缓存命中率突然下降，回源压力增大，如何排查？'] +
                                       [d.content for d in documents])
    import math
    query = vectors[0]
    def cosine(vector):
        return sum(a*b for a,b in zip(query,vector,strict=True)) / math.sqrt(sum(v*v for v in query)*sum(v*v for v in vector))
    ranked = sorted(zip(documents,vectors[1:],strict=True),key=lambda row:cosine(row[1]),reverse=True)
    assert ranked[0][0].document_id == 'redis/low_cache_hit_rate'
