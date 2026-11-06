"""Cross-worker, nonblocking conversation locks held for the whole SSE response."""
from __future__ import annotations

import hashlib
import logging

import anyio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

log = logging.getLogger(__name__)


class ConversationLease:
    def __init__(self, connection: Connection, key: int):
        self.connection: Connection | None = connection
        self.key = key

    @classmethod
    def acquire(cls, engine: Engine, conversation_id: str) -> ConversationLease:
        # Session locks survive the business session's commits, but must never
        # be returned to a pool still locked. Each lease owns a separate connection.
        key = int.from_bytes(hashlib.sha256(f"knoventra:conversation:{conversation_id}".encode()).digest()[:8],
                             "big", signed=True)
        connection = engine.connect()
        try:
            if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
                raise HTTPException(409, "CONVERSATION_BUSY")
            connection.commit()  # Session lock survives; avoid a long idle transaction during SSE.
            return cls(connection, key)
        except HTTPException:
            connection.close()
            raise
        except BaseException:
            # A failed round trip/commit can leave the session lock held even
            # if its result never reached us. Do not recycle that DB session.
            try:
                connection.invalidate()
            finally:
                connection.close()
            raise

    def close(self) -> None:
        connection, self.connection = self.connection, None
        if connection is None:
            return
        try:
            if not connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": self.key}):
                raise RuntimeError("conversation lease was not held")
        except Exception:
            # Discard the DB session rather than let an advisory lock escape
            # into the pool if explicit unlocking fails.
            log.exception("Failed to release conversation lease; discarding connection")
            connection.invalidate()
        finally:
            connection.close()


class ConversationStreamingResponse(StreamingResponse):
    def __init__(self, content, lease: ConversationLease):
        super().__init__(content, media_type="text/event-stream")
        self.lease = lease
        self.content = content

    def _close_content(self) -> None:
        try:
            close = getattr(self.content, "close", None)
            if close is not None:
                close()
        finally:
            self.lease.close()

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Response-level cleanup also runs when send fails or the client
            # disconnects before the body iterator ever starts.
            with anyio.CancelScope(shield=True):
                try:
                    close = getattr(self.body_iterator, "aclose", None)
                    if close is not None:
                        await close()
                finally:
                    await run_in_threadpool(self._close_content)
