import uuid
from unittest.mock import Mock

import anyio
from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine
from starlette.requests import ClientDisconnect

from knoventra.api.run_lock import ConversationLease, ConversationStreamingResponse


@pytest.mark.parametrize("failure", ["query", "commit"])
def test_acquisition_failures_discard_connection_instead_of_pooling_possible_locks(failure):
    connection = Mock()
    connection.scalar.return_value = True
    if failure == "query":
        connection.scalar.side_effect = RuntimeError("lost query response")
    else:
        connection.commit.side_effect = RuntimeError("lost commit response")
    engine = Mock()
    engine.connect.return_value = connection
    with pytest.raises(RuntimeError):
        ConversationLease.acquire(engine, "c1")
    connection.invalidate.assert_called_once()
    connection.close.assert_called_once()


def test_unlock_failure_discards_connection_and_cleanup_stays_idempotent():
    connection = Mock()
    connection.scalar.side_effect = RuntimeError("disconnected")
    lease = ConversationLease(connection, 123)
    lease.close()
    lease.close()
    connection.invalidate.assert_called_once()
    connection.close.assert_called_once()


def test_lock_is_cross_pool_survives_commit_and_releases_for_retry(engine):
    other = create_engine(engine.url)
    cid = uuid.uuid4().hex
    lease = ConversationLease.acquire(engine, cid)
    try:
        with pytest.raises(HTTPException) as error:
            ConversationLease.acquire(other, cid)
        assert error.value.status_code == 409 and error.value.detail == "CONVERSATION_BUSY"
        independent = ConversationLease.acquire(other, cid + "-other")
        independent.close()
    finally:
        lease.close()
    lease.close()  # Cleanup is idempotent.
    retry = ConversationLease.acquire(other, cid)
    retry.close()
    other.dispose()


class FakeLease:
    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append("unlock")


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [None, "before_body", "mid_body"])
async def test_response_releases_lock_on_completion_and_send_failure(disconnect):
    events = []

    def content():
        try:
            events.append("iterate")
            yield "first"
            yield "second"
        finally:
            events.append("generator_closed")

    async def send(message):
        if ((disconnect == "before_body" and message["type"] == "http.response.start")
                or (disconnect == "mid_body" and message["type"] == "http.response.body")):
            raise OSError("client disconnected")

    async def receive():
        return {"type": "http.disconnect"}

    response = ConversationStreamingResponse(content(), FakeLease(events))
    if disconnect:
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    else:
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert events[-1] == "unlock" and events.count("unlock") == 1
    if disconnect == "before_body":
        assert "iterate" not in events
    else:
        assert events[-2] == "generator_closed"


@pytest.mark.asyncio
async def test_cancellation_closes_async_body_before_unlocking():
    events = []
    started = anyio.Event()

    async def content():
        try:
            yield "first"
            started.set()
            await anyio.sleep_forever()
        finally:
            events.append("generator_closed")

    async def send(message):
        pass

    async def receive():
        await anyio.sleep_forever()

    response = ConversationStreamingResponse(content(), FakeLease(events))
    async with anyio.create_task_group() as group:
        group.start_soon(response, {"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send)
        await started.wait()
        group.cancel_scope.cancel()
    assert events == ["generator_closed", "unlock"]
