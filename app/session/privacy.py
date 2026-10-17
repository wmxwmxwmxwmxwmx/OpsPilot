"""Conservative credential redaction at the conversation storage boundary.

This covers configured credentials and common explicit credential forms, not
arbitrary confidential prose. Real tool integrations still need domain policies.
"""

import re

_CREDENTIAL = re.compile(
    r'''(?ix)(["']?(?:password|passwd|pwd|api[_-]?key|access[_-]?token|secret)["']?\s*[:=]\s*)'''
    r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}&]+)'''
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_URL_AUTH = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+@")
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S)
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")


def redact_content(content: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        content = content.replace(secret, "[REDACTED]")
    content = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", content)
    content = _CREDENTIAL.sub(r"\1[REDACTED]", content)
    content = _BEARER.sub("Bearer [REDACTED]", content)
    content = _URL_AUTH.sub(r"\1[REDACTED]@", content)
    return _API_KEY.sub("[REDACTED]", content)
