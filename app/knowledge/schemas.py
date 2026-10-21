from dataclasses import dataclass, field


@dataclass(frozen=True)
class Document:
    document_id: str
    source_path: str
    title: str
    version: str
    content: str
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    source_path: str
    title: str
    section: str
    content: str
    content_hash: str
    version: str
    metadata: dict
