"""Separate idempotent workflow adapter; legacy WATCH/MULTI/EXEC is untouched."""
from datetime import datetime, timezone
from uuid import UUID

import anyio
from redis.exceptions import RedisError, WatchError

from app.session.errors import SessionError
from app.session.privacy import redact_content
from app.session.schemas import ConversationSession, ConversationTurn


def receipt_key(workflow_id: str):
    return f"opspilot:workflow:session-commit:{workflow_id}"


async def commit_session(repository, state: dict) -> str:
    if state["session_id"] is None:
        return "not_requested"
    if repository is None:
        raise SessionError("session_disabled")
    sid = UUID(state["session_id"])
    key, receipt = repository.key(sid), receipt_key(state["workflow_id"])
    pipe = repository.client.pipeline(transaction=True)
    try:
        await pipe.watch(key, receipt)
        if await pipe.get(receipt) is not None:
            return "committed"
        try:
            current = repository._decode(await pipe.get(key), sid)
        except SessionError as error:
            if error.code == "session_not_found":
                return "expired"
            raise
        if current.version != state["session_version"]:
            return "conflict"
        pair = (ConversationTurn(role="user", content=redact_content(state["user_message"], repository._secrets)),
                ConversationTurn(role="assistant", content=redact_content(state["final_answer"], repository._secrets)))
        updated = ConversationSession(session_id=sid, version=current.version + 1,
            messages=(*current.messages, *pair)[-2 * repository.max_turns:], created_at=current.created_at,
            updated_at=max(datetime.now(timezone.utc), current.updated_at))
        payload = repository._encode(updated)
        pipe.multi()
        pipe.set(key, payload, ex=repository.ttl_seconds, xx=True)
        # Same Redis transaction, no TTL: reconciliation survives session expiry.
        # Removed only by terminal workflow checkpoint cleanup.
        pipe.set(receipt, "committed")
        results = await pipe.execute()
        if not results[0]:
            raise SessionError("session_storage_error")
        return "committed"
    except WatchError:
        # An explicit resume will reread the receipt/version; never rerun the graph.
        raise SessionError("session_conflict") from None
    except RedisError:
        raise SessionError("session_storage_error") from None
    finally:
        with anyio.CancelScope(shield=True):
            await pipe.reset()
