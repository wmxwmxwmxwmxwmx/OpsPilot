import re
from pathlib import Path

from app.knowledge.schemas import Document


def load_documents(root: Path) -> list[Document]:
    root = root.resolve(strict=True)
    documents, ids = [], set()
    for path in sorted(root.rglob('*.md')):
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise ValueError('Runbook must remain inside its root')
        if path.stat().st_size > 262144:
            raise ValueError('Runbook exceeds 256 KiB')
        content = path.read_text(encoding='utf-8')
        header = re.search(r'^# (.+)$', content, re.M)
        fields = dict(re.findall(r'^(document_id|version|component|updated): ([^\n]+)$', content, re.M))
        if not header or not all(fields.get(k) for k in ('document_id', 'version', 'component', 'updated')):
            raise ValueError('Runbook requires title, document_id, version, component, updated')
        if len(header[1]) > 200 or len(fields['version']) > 64 or len(path.relative_to(root).as_posix()) > 512:
            raise ValueError('Runbook metadata exceeds limits')
        doc_id = fields.pop('document_id')
        if not re.fullmatch(r'[a-z0-9_/-]{1,128}', doc_id) or doc_id in ids:
            raise ValueError('Invalid or duplicate document_id')
        ids.add(doc_id)
        documents.append(Document(doc_id, path.relative_to(root).as_posix(), header[1],
                                  fields.pop('version'), content, fields))
    return documents
