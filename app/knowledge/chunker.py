import hashlib
import re

from app.knowledge.schemas import Chunk, Document


def split_document(document: Document, size: int = 1000, overlap: int = 120) -> list[Chunk]:
    if not 128 <= size <= 8000 or not 0 <= overlap < size // 2:
        raise ValueError('Invalid chunk size or overlap')
    sections, headings, lines = [], [], []
    for line in document.content.splitlines():
        match = re.match(r'^(#{1,6}) (.+)$', line)
        if match:
            if lines:
                sections.append((' / '.join(headings), '\n'.join(lines).strip()))
            headings = headings[:len(match[1]) - 1] + [match[2]]
            lines = [line]
        else:
            lines.append(line)
    if lines:
        sections.append((' / '.join(headings), '\n'.join(lines).strip()))
    chunks = []
    for section, text in sections:
        offset = 0
        while offset < len(text):
            end = min(offset + size, len(text))
            if end < len(text):
                boundary = text.rfind('\n\n', offset + size // 2, end)
                if boundary > offset:
                    end = boundary
            content = text[offset:end].strip()
            if content:
                digest = hashlib.sha256(content.encode()).hexdigest()
                identity = f'{document.document_id}\0{document.version}\0{section}\0{offset}\0{digest}'
                chunks.append(Chunk(hashlib.sha256(identity.encode()).hexdigest(), document.document_id,
                    document.source_path, document.title, section, content, digest, document.version,
                    document.metadata))
            if end == len(text):
                break
            offset = max(offset + 1, end - overlap)
    return chunks
