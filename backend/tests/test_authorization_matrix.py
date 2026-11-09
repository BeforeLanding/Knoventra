"""Plan 9.4: every identity pair and every source in the fictional corpus."""
import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from fakes import ScriptedModel, session_factory_for
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.api.chat import get_graph
from knoventra.api.deps import get_db
from knoventra.db.models import Chunk, Confirmation, Conversation, Document, Escalation, KnowledgeBase, Message, Revision
from knoventra.ingestion.encoder import get_encoder
from knoventra.ingestion.service import publish
from knoventra.main import app

# Expected policy comes from the demo specification, not compute_scope's output.
READABLE = {
    "sg-ops": {"ops-global", "it-public"}, "tw-ops": {"ops-global", "it-public"},
    "cn-ops": {"ops-global", "ops-cn", "it-public"}, "hr": {"hr", "it-public"},
    "fin": {"fin", "it-public"}, "admin": {"it-public"},
}


@pytest.fixture
def client(db, enc):
    def forbidden_model(messages):
        pytest.fail("authorization/read-only requests must never invoke a model")

    graph = build_graph(AgentDeps(session_factory_for(db), enc, None,
                                 ScriptedModel(script=forbidden_model), InMemorySaver()))
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_graph] = lambda: graph
    app.dependency_overrides[get_encoder] = lambda: enc
    try:
        with TestClient(app) as value:
            yield value
    finally:
        app.dependency_overrides.clear()


def auth(client, who):
    result = client.post("/api/v1/auth/login", json={"email": f"{who}@example.test", "password": "demo-pass-1"})
    assert result.status_code == 200
    return {"Authorization": "Bearer " + result.json()["token"]}


@pytest.mark.parametrize("owner", READABLE)
@pytest.mark.parametrize("viewer", READABLE)
def test_all_identity_pairs_enforce_history_and_record_ownership(client, db, users, owner, viewer):
    conv = Conversation(user_id=users[owner].id, title=f"private conversation for {owner}")
    db.add(conv)
    db.flush()
    message = Message(conversation_id=conv.id, role="assistant", content=f"private answer for {owner}")
    record = Escalation(user_id=users[owner].id, operation_id=uuid.uuid4().hex,
                        summary=f"private escalation for {owner}", evidence_refs=[], status="OPEN")
    confirmation = Confirmation(user_id=users[owner].id, thread_id=conv.thread_id, operation_id=uuid.uuid4().hex,
                                summary=f"private confirmation for {owner}", status="PENDING",
                                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=30))
    db.add_all([message, record, confirmation])
    db.flush()
    headers = auth(client, viewer)
    allowed = owner == viewer
    for path in (f"/conversations/{conv.id}/messages", f"/conversations/{conv.id}/state", f"/escalations/{record.id}"):
        response = client.get("/api/v1" + path, headers=headers)
        assert response.status_code == (200 if allowed else 404), (owner, viewer, path)
        if allowed:
            data = response.json()
            if path.endswith("/messages"):
                assert [row["content"] for row in data] == [message.content]
            elif path.endswith("/state"):
                assert data["conversation_id"] == conv.id
                assert [row["content"] for row in data["messages"]] == [message.content]
            else:
                assert data["id"] == record.id and data["summary"] == record.summary
        else:
            assert response.json() == {"detail": "Not Found"}
    assert [r["id"] for r in client.get("/api/v1/conversations", headers=headers).json()] == ([conv.id] if allowed else [])
    for path, record_id in (("/conversations/page", conv.id), ("/escalations", record.id)):
        response = client.get("/api/v1" + path, headers=headers)
        assert response.status_code == 200
        assert [row["id"] for row in response.json()["items"]] == ([record_id] if allowed else [])
        if not allowed:
            assert client.get("/api/v1" + path, params={"before": record_id}, headers=headers).status_code == 404
    if not allowed:
        for path, body in (
            ("/chat", {"conversation_id": conv.id, "message": "read another user's context"}),
            ("/chat/retry", {"conversation_id": conv.id, "checkpoint_id": "guessed"}),
            ("/chat/recover", {"conversation_id": conv.id, "checkpoint_id": "guessed"}),
            ("/chat/confirm", {"confirmation_id": confirmation.id, "approve": True}),
        ):
            assert client.post("/api/v1" + path, json=body, headers=headers).status_code == 404
    assert db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == conv.id)) == 1
    assert db.get(Confirmation, confirmation.id).status == "PENDING"
    assert db.scalar(select(func.count()).select_from(Escalation).where(Escalation.user_id == users[owner].id)) == 1


@pytest.mark.parametrize("who", READABLE)
@pytest.mark.parametrize("after_publish", [False, True])
def test_all_users_preview_every_chunk_before_and_after_publication(client, db, who, after_publish):
    if after_publish:
        draft = db.scalar(select(Revision).join(Document).where(Document.title == "E17 系列设备故障手册",
                                                              Revision.status == "DRAFT"))
        publish(db, draft.id)
    rows = db.execute(select(Chunk, Revision.status, KnowledgeBase.name)
                      .join(Revision, Revision.id == Chunk.revision_id)
                      .join(KnowledgeBase, KnowledgeBase.id == Chunk.kb_id)).all()
    assert len(rows) >= 23 and any(status == ("RETIRED" if after_publish else "DRAFT") for _, status, _ in rows)
    headers = auth(client, who)
    readable = denied = 0
    for chunk, status, kb in rows:
        response = client.get(f"/api/v1/evidence/{chunk.id}", headers=headers)
        allowed = kb in READABLE[who] and status in ("PUBLISHED", "RETIRED")
        assert response.status_code == (200 if allowed else 404), (who, kb, status)
        if allowed:
            readable += 1
            assert response.json()["text"] == chunk.text
            assert response.json()["revision_status"] == status
        else:
            denied += 1
            assert response.json() == {"detail": "Not Found"}
    assert readable and denied  # The matrix must exercise both sides, including admin.


@pytest.mark.parametrize("who", READABLE)
def test_unknown_identifiers_are_hidden_for_every_user(client, who):
    headers = auth(client, who)
    unknown = uuid.uuid4().hex
    for path in (f"/conversations/{unknown}/messages", f"/conversations/{unknown}/state",
                 f"/evidence/{unknown}", f"/escalations/{unknown}"):
        assert client.get("/api/v1" + path, headers=headers).status_code == 404
