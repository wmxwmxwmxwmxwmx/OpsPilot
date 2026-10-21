from app.observability.telemetry import observed
import asyncio


class Retriever:
    def __init__(self, repository, provider, *, top_k=4, max_chars=4000, min_score=0.35, timeout=10):
        self.repository, self.provider = repository, provider
        self.top_k, self.max_chars, self.min_score, self.timeout = top_k, max_chars, min_score, timeout

    @observed("rag", "retrieve")
    async def retrieve(self, query):
        async with asyncio.timeout(self.timeout):
            vector = (await self.provider.embed([query]))[0]
            rows = await self.repository.search(vector, self.provider, self.top_k, self.min_score)
        result, seen, remaining = [], set(), self.max_chars
        for row in rows:
            if len(result) >= self.top_k: break
            if row['chunk_id'] in seen or remaining <= 0:
                continue
            seen.add(row['chunk_id'])
            content = row['content'][:remaining]
            remaining -= len(content)
            result.append({**row, 'content': content})
        from app.observability.telemetry import telemetry
        obs = telemetry()
        if obs.metrics: obs.chunks.observe(len(result))
        if not result: obs.event("rag", "empty")
        return result
