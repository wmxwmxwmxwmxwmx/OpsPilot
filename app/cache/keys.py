import re
from uuid import UUID

DEFAULT_KEY_PREFIX = "opspilot:cache"
_PREFIX = re.compile(r"opspilot:cache(?::[A-Za-z0-9_-]{1,64})*")


def validate_key_prefix(prefix: str) -> str:
    # Keep every configured prefix inside the cache namespace, never session:*.
    if len(prefix) > 128 or _PREFIX.fullmatch(prefix) is None:
        raise ValueError("Cache prefix must be opspilot:cache with optional safe colon-separated segments")
    return prefix


def incident_summary_key(incident_id: UUID, prefix: str = DEFAULT_KEY_PREFIX) -> str:
    if not isinstance(incident_id, UUID) or incident_id.version != 4:
        raise ValueError("Incident cache keys require a UUID4")
    return f"{validate_key_prefix(prefix)}:incident_summary:v1:{incident_id}"
