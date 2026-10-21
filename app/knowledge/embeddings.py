from app.observability.telemetry import observed
"""No model downloads. Fake vectors test plumbing, not semantic relevance."""
from abc import ABC, abstractmethod
import hashlib
import math
import re


class EmbeddingProvider(ABC):
    def __init__(self, model: str, dimensions: int):
        self.model, self.dimensions = model, dimensions

    def validate(self, vectors, count):
        if len(vectors) != count:
            raise ValueError('embedding_count_mismatch')
        for vector in vectors:
            if (len(vector) != self.dimensions or not all(type(v) in (int, float) and math.isfinite(v) for v in vector)
                    or not any(vector)):
                raise ValueError('embedding_dimension_or_value_mismatch')
        return vectors

    @abstractmethod
    @observed("embedding", "generate")
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedding(EmbeddingProvider):
    def __init__(self, dimensions=64):
        super().__init__('fake-hash-v1', dimensions)

    @observed("embedding", "generate")
    async def embed(self, texts):
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]', text.lower()):
                digest = hashlib.sha256(token.encode()).digest()
                vector[int.from_bytes(digest[:4], 'big') % self.dimensions] += 1
            norm = math.sqrt(sum(v * v for v in vector)) or 1
            vectors.append([v / norm for v in vector])
        return self.validate(vectors, len(texts))


class APIEmbedding(EmbeddingProvider):
    def __init__(self, client, *, base_url, api_key, model, dimensions):
        super().__init__(model, dimensions)
        self.client, self.url, self.key = client, base_url.rstrip('/') + '/embeddings', api_key

    @observed("embedding", "generate")
    async def embed(self, texts):
        response = await self.client.post(self.url, headers={'Authorization': f'Bearer {self.key}'},
            json={'model': self.model, 'input': texts, 'dimensions': self.dimensions}, timeout=30)
        response.raise_for_status()
        rows = sorted(response.json()['data'], key=lambda row: row['index'])
        if [r['index'] for r in rows] != list(range(len(texts))):
            raise ValueError('embedding_index_mismatch')
        return self.validate([row['embedding'] for row in rows], len(texts))


def build_embedding(settings, client):
    if settings.rag_embedding_provider == 'fake':
        return FakeEmbedding(settings.rag_embedding_dimensions)
    return APIEmbedding(client, base_url=settings.rag_embedding_base_url,
        api_key=settings.rag_embedding_api_key.get_secret_value(), model=settings.rag_embedding_model,
        dimensions=settings.rag_embedding_dimensions)
