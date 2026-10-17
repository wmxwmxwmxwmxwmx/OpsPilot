from datetime import datetime, timezone
from uuid import UUID, uuid4

import anyio
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

from app.core.logging import log_event
from app.session.errors import SessionError
from app.session.privacy import redact_content
from app.session.schemas import ConversationSession, ConversationTurn


class SessionRepository:
    """Redis JSON snapshots with success-based TTL and optimistic commits."""

    def __init__(
        self, client: Redis, *, ttl_seconds: int = 1800, max_turns: int = 10,
        max_message_chars: int = 16000, max_bytes: int = 262144,
        secrets: tuple[str, ...] = (),
    ) -> None:
        if (not 1 <= max_turns <= 100 or not 1 <= max_message_chars <= 32000
                or min(ttl_seconds, max_bytes) <= 0):
            raise ValueError("Session limits must be positive and within schema bounds")
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.max_turns = max_turns
        self.max_message_chars = max_message_chars
        self.max_bytes = max_bytes
        self._secrets = secrets

    @staticmethod
    def key(session_id: UUID) -> str:
        return f"opspilot:session:{session_id}"

    def _encode(self, session: ConversationSession) -> str:
        if any(len(m.content) > self.max_message_chars for m in session.messages):
            raise SessionError("session_too_large")
        payload = session.model_dump_json()
        if len(payload.encode("utf-8")) > self.max_bytes:
            raise SessionError("session_too_large")
        return payload

    def _decode(self, payload: bytes | str | None, session_id: UUID) -> ConversationSession:
        if payload is None:
            raise SessionError("session_not_found")
        try:
            size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
            if size > self.max_bytes:
                raise ValueError("Oversized session")
            session = ConversationSession.model_validate_json(payload)
            if session.session_id != session_id or any(
                len(m.content) > self.max_message_chars for m in session.messages
            ):
                raise ValueError("Invalid session")
            # A lower retention setting takes effect on the next read without
            # writing or refreshing TTL; only complete pairs are removed.
            return session.model_copy(update={"messages": session.messages[-2 * self.max_turns:]})
        except (ValidationError, ValueError, TypeError):
            raise SessionError("session_data_error") from None

    async def create(self) -> ConversationSession:
        now = datetime.now(timezone.utc)
        session = ConversationSession(session_id=uuid4(), created_at=now, updated_at=now)
        try:
            created = await self.client.set(self.key(session.session_id), self._encode(session),
                                            ex=self.ttl_seconds, nx=True)
            if not created:
                raise SessionError("session_conflict")
        except RedisError:
            raise SessionError("session_storage_error") from None
        log_event("session_created")
        return session

    async def load(self, session_id: UUID) -> ConversationSession:
        try:
            payload = await self.client.get(self.key(session_id))
        except RedisError:
            raise SessionError("session_storage_error") from None
        return self._decode(payload, session_id)

    async def commit(
        self, snapshot: ConversationSession, user: str, answer: str,
    ) -> ConversationSession:
        # Validate before any write. Never persist system/tool/reasoning fields.
        if max(len(user), len(answer)) > self.max_message_chars:
            raise SessionError("session_too_large")
        try:
            pair = (ConversationTurn(role="user", content=redact_content(user, self._secrets)),
                    ConversationTurn(role="assistant", content=redact_content(answer, self._secrets)))
            updated = ConversationSession(
                session_id=snapshot.session_id, version=snapshot.version + 1,
                messages=(*snapshot.messages, *pair)[-2 * self.max_turns:],
                created_at=snapshot.created_at,
                updated_at=max(datetime.now(timezone.utc), snapshot.updated_at),
            )
            payload = self._encode(updated)
        except ValidationError:
            raise SessionError("session_data_error") from None
        key = self.key(snapshot.session_id)
        pipe = self.client.pipeline(transaction=True)
        try:
            await pipe.watch(key)
            current = self._decode(await pipe.get(key), snapshot.session_id)
            if current.version != snapshot.version:
                raise SessionError("session_conflict")
            pipe.multi()
            # One atomic SET updates JSON + TTL; XX additionally prevents revival.
            pipe.set(key, payload, ex=self.ttl_seconds, xx=True)
            result = await pipe.execute()
            if not result[0]:
                raise SessionError("session_not_found")
        except WatchError:
            # Do not retry a result computed from stale history.
            await self.load(snapshot.session_id)  # Expiry maps to not_found.
            raise SessionError("session_conflict") from None
        except RedisError:
            raise SessionError("session_storage_error") from None
        finally:
            # AnyIO SSE cancellation is level-triggered: shield only bounded
            # transaction cleanup, never the commit itself or the agent work.
            with anyio.CancelScope(shield=True):
                await pipe.reset()
        log_event("session_committed")
        return updated
