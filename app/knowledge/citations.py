"""Render only retrieved sources; reject model-supplied Markdown file citations."""
import re


def render_references(answer: str, chunks: list[dict]) -> str:
    allowed = {chunk['source_path'] for chunk in chunks}
    # Model prose is not authoritative citation metadata. Canonical references
    # below are generated entirely from the bounded retrieval state.
    answer = re.sub(r'(?:[\w.~-]+/)*[\w.~-]+\.md(?:#[\w-]+)?',
        lambda match: match[0] if match[0].split('#')[0] in allowed else '[未验证来源已省略]', answer)
    references = [f"{c['source_path']}（v{c['version']}，{c['section']}，chunk:{c['chunk_id']}）" for c in chunks]
    appendix = '\n\nRunbook 参考（检索来源；建议未自动执行）：\n' + (
        '\n'.join(references) if references else '未找到可靠的相关手册或检索暂不可用。')
    return answer[:16000 - len(appendix)] + appendix
