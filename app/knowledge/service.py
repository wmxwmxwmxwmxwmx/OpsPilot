import hashlib

from app.knowledge.chunker import split_document
from app.knowledge.loader import load_documents


async def import_runbooks(root, repository, provider, *, size=1000, overlap=120):
    imported = unchanged = 0
    for document in load_documents(root):
        fingerprint = hashlib.sha256(f'{size}:{overlap}:{document.source_path}:{document.content}'.encode()).hexdigest()
        if await repository.signature(document.document_id) == (fingerprint, provider.model, provider.dimensions):
            unchanged += 1
            continue
        chunks = split_document(document, size, overlap)
        vectors = []
        for offset in range(0, len(chunks), 32):
            batch = chunks[offset:offset + 32]
            vectors.extend(await provider.embed([c.title + '\n' + c.section + '\n' + c.content for c in batch]))
        await repository.replace(document, chunks, vectors, provider, fingerprint)
        imported += 1
    return {'imported': imported, 'unchanged': unchanged}
