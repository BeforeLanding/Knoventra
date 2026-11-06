import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import func, select

from fakes import ScriptedModel, call, session_factory_for
from knoventra.agent.checkpoint import make_checkpointer
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.api.chat import get_graph
from knoventra.api.deps import get_db
from knoventra.config import settings
from knoventra.db.models import Confirmation, Conversation, Escalation, Message
from knoventra.api.run_lock import ConversationLease
from knoventra.main import app


def script(msgs):
    last = msgs[-1]
    text = " ".join(getattr(m, "content", "") for m in msgs if m.type == "human")
    if isinstance(last, ToolMessage):
        if "转交记录" in last.content or "取消" in last.content:
            return AIMessage(last.content)
        return AIMessage("先检查冷却液循环泵 [S1]。" if "S1" in last.content else "没有可靠依据，请联系 Ops 团队。")
    if "转人工" in text:
        return call("escalate_to_expert", {"summary": "E17-42 复位后仍报警"}, cid="esc1")
    return call("search_knowledge", {"query": "E17-42 排查"})


@pytest.fixture
def client(db, enc, engine, monkeypatch):
    monkeypatch.setattr(settings, "gate_sparse_threshold", 1e9)  # dense-only gate for the hash encoder
    monkeypatch.setattr(settings, "gate_threshold", 0.1)  # hash-encoder cosine scale; BGE-M3 default is in config
    saver, pool = make_checkpointer(settings.database_url)
    g = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=script), saver))
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_graph] = lambda: g
    yield TestClient(app)
    app.dependency_overrides.clear()
    pool.close()


def auth(c, who):
    r = c.post("/api/v1/auth/login", json={"email": f"{who}@example.test", "password": "demo-pass-1"})
    return {"Authorization": "Bearer " + r.json()["token"]}


def events(resp):
    out, ev = [], None
    for line in resp.text.splitlines():
        if line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: "):
            out.append((ev, json.loads(line[6:])))
    return out


def test_chat_streams_answer_with_sources(client):
    r = client.post("/api/v1/chat", json={"message": "E17 设备报代码 42 先查什么"}, headers=auth(client, "sg-ops"))
    ev = events(r)
    names = [n for n, _ in ev]
    assert names[0] == "meta" and names[-1] == "done" and "sources" in names and "error" not in names
    src = dict(ev)["sources"][0]
    assert src["document_title"].startswith("E17") and src["page"] == 2 and src["snippet"]
    assert "冷却液" in dict(ev)["done"]["answer"]


def test_no_basis_refusal_emits_error_code(client):
    r = client.post("/api/v1/chat", json={"message": "E17 报警"}, headers=auth(client, "hr"))
    ev = events(r)
    assert ("error", {"code": "INSUFFICIENT_EVIDENCE"}) in ev
    assert dict(ev)["sources"] == []


def test_near_miss_refuses_even_when_model_searches_a_real_code(client):
    # The fixture's model always searches E17-42, regardless of user wording.
    r = client.post("/api/v1/chat", json={"message": "E17-44 怎么处理"}, headers=auth(client, "sg-ops"))
    ev = events(r)
    assert ("error", {"code": "INSUFFICIENT_EVIDENCE"}) in ev
    assert dict(ev)["sources"] == [] and "冷却液" not in dict(ev)["done"]["answer"]


def test_traditional_refusal_marker_produces_refusal_state_without_sources(client, db, enc, monkeypatch):
    def refuse(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage("【無依據】資料沒有保修期限 [S1]，請聯絡 Ops。")
        return call("search_knowledge", {"query": "E17-42 排查"})

    saver, pool = make_checkpointer(settings.database_url)
    try:
        g = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=refuse), saver))
        monkeypatch.setitem(app.dependency_overrides, get_graph, lambda: g)
        r = client.post("/api/v1/chat", json={"message": "E17 保修期限多長"}, headers=auth(client, "sg-ops"))
        ev = events(r)
        assert ("error", {"code": "INSUFFICIENT_EVIDENCE"}) in ev and dict(ev)["sources"] == []
        assert dict(ev)["done"]["answer"] == "資料沒有保修期限 ，請聯絡 Ops。"
    finally:
        pool.close()


def test_cross_user_ids_return_404(client):
    h, o = auth(client, "sg-ops"), auth(client, "hr")
    ev = events(client.post("/api/v1/chat", json={"message": "E17 设备报代码 42"}, headers=h))
    cid = dict(ev)["meta"]["conversation_id"]
    chunk = dict(ev)["sources"][0]["chunk_id"]
    assert client.get(f"/api/v1/conversations/{cid}/messages", headers=h).status_code == 200
    assert client.get(f"/api/v1/conversations/{cid}/messages", headers=o).status_code == 404
    assert client.get(f"/api/v1/evidence/{chunk}", headers=h).json()["text"]
    assert client.get(f"/api/v1/evidence/{chunk}", headers=o).status_code == 404
    assert client.post("/api/v1/chat", json={"conversation_id": cid, "message": "x"}, headers=o).status_code == 404
    assert [c["id"] for c in client.get("/api/v1/conversations", headers=o).json()] == []


def _start_escalation(client, who="sg-ops"):
    ev = events(client.post("/api/v1/chat", json={"message": "转人工"}, headers=auth(client, who)))
    assert ev[-1][0] == "confirm"
    return ev[-1][1]["confirmation_id"]


def count(db):
    return db.scalar(select(func.count()).select_from(Escalation))


def test_confirm_twice_creates_one_record(client, db):
    cid, h = _start_escalation(client), auth(client, "sg-ops")
    assert count(db) == 0
    first = events(client.post("/api/v1/chat/confirm", json={"confirmation_id": cid, "approve": True}, headers=h))
    assert "已创建转交记录" in dict(first)["done"]["answer"] and count(db) == 1
    again = events(client.post("/api/v1/chat/confirm", json={"confirmation_id": cid, "approve": True}, headers=h))
    flip = events(client.post("/api/v1/chat/confirm", json={"confirmation_id": cid, "approve": False}, headers=h))
    assert count(db) == 1 and dict(again)["meta"]["status"] == "APPROVED" and dict(flip)["meta"]["status"] == "APPROVED"
    eid = db.scalar(select(Escalation.id))
    assert client.get(f"/api/v1/escalations/{eid}", headers=h).status_code == 200
    assert client.get(f"/api/v1/escalations/{eid}", headers=auth(client, "hr")).status_code == 404


def test_confirm_reject_and_foreign_user(client, db):
    cid = _start_escalation(client)
    assert client.post("/api/v1/chat/confirm", json={"confirmation_id": cid, "approve": True},
                       headers=auth(client, "hr")).status_code == 404
    ev = events(client.post("/api/v1/chat/confirm", json={"confirmation_id": cid, "approve": False},
                            headers=auth(client, "sg-ops")))
    assert count(db) == 0 and "取消" in dict(ev)["done"]["answer"]


def test_confirm_expired(client, db):
    cid = _start_escalation(client)
    db.execute(Confirmation.__table__.update().where(Confirmation.id == cid)
               .values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)))
    ev = events(client.post("/api/v1/chat/confirm", json={"confirmation_id": cid, "approve": True},
                            headers=auth(client, "sg-ops")))
    assert ("error", {"code": "CONFIRMATION_EXPIRED"}) in ev and count(db) == 0


def test_state_restores_pending_confirmation_and_is_owner_only(client, db):
    h = auth(client, "sg-ops")
    ev = dict(events(client.post("/api/v1/chat", json={"message": "转人工"}, headers=h)))
    cid = ev["meta"]["conversation_id"]
    url = f"/api/v1/conversations/{cid}/state"
    assert client.get(url, headers=auth(client, "hr")).status_code == 404
    for _ in range(2):  # Refresh reads must neither resume nor create records.
        state = client.get(url, headers=h).json()
        assert state["blocked"] is True
        card = state["pending_confirmation"]
        assert card["confirmation_id"] == ev["confirm"]["confirmation_id"]
        assert card["status"] == "PENDING" and not card["expired"]
        assert card["expires_at"] and state["messages"][-1]["content"] == "转人工"
        assert count(db) == 0
    client.post("/api/v1/chat/confirm", headers=h,
                json={"confirmation_id": card["confirmation_id"], "approve": True})
    restored = client.get(url, headers=h).json()
    assert restored["blocked"] is False and restored["pending_confirmation"] is None
    assert "已创建转交记录" in restored["messages"][-1]["content"] and count(db) == 1


def test_state_restores_refusal_without_sources(client):
    h = auth(client, "hr")
    ev = dict(events(client.post("/api/v1/chat", json={"message": "E17 报警"}, headers=h)))
    cid = ev["meta"]["conversation_id"]
    state = client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()
    assert state["blocked"] is False and state["pending_confirmation"] is None
    answer = state["messages"][-1]
    assert answer["error_code"] == "INSUFFICIENT_EVIDENCE" and answer["sources"] == []
    assert answer["content"] == ev["done"]["answer"]


def test_state_exposes_expiry_without_resuming(client, db):
    confirmation_id = _start_escalation(client)
    db.execute(Confirmation.__table__.update().where(Confirmation.id == confirmation_id)
               .values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)))
    h = auth(client, "sg-ops")
    cid = client.get("/api/v1/conversations", headers=h).json()[0]["id"]
    state = client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()
    assert state["blocked"] and state["pending_confirmation"]["expired"]
    assert count(db) == 0 and db.get(Confirmation, confirmation_id).status == "PENDING"


def test_busy_chat_returns_409_without_appending_messages_and_preserves_ownership(client, db):
    h = auth(client, "sg-ops")
    ev = dict(events(client.post("/api/v1/chat", json={"message": "E17-42 排查"}, headers=h)))
    cid = ev["meta"]["conversation_id"]
    before = db.scalar(select(func.count()).select_from(Message))
    lease = ConversationLease.acquire(db.get_bind().engine, cid)
    try:
        busy = client.post("/api/v1/chat", json={"conversation_id": cid, "message": "重复问题"}, headers=h)
        assert busy.status_code == 409 and busy.json()["detail"] == "CONVERSATION_BUSY"
        assert db.scalar(select(func.count()).select_from(Message)) == before
        foreign = client.post("/api/v1/chat", json={"conversation_id": cid, "message": "猜测 ID"}, headers=auth(client, "hr"))
        assert foreign.status_code == 404
    finally:
        lease.close()
    retry = client.post("/api/v1/chat", json={"conversation_id": cid, "message": "E17-42 排查"}, headers=h)
    assert retry.status_code == 200 and "done" in dict(events(retry))


def test_busy_confirmation_does_not_claim_decision_and_can_retry(client, db):
    confirmation_id = _start_escalation(client)
    conf = db.get(Confirmation, confirmation_id)
    conv = db.scalar(select(Conversation).where(Conversation.thread_id == conf.thread_id))
    lease = ConversationLease.acquire(db.get_bind().engine, conv.id)
    try:
        h = auth(client, "sg-ops")
        body = {"confirmation_id": confirmation_id, "approve": True}
        busy = client.post("/api/v1/chat/confirm", json=body, headers=h)
        assert busy.status_code == 409 and busy.json()["detail"] == "CONVERSATION_BUSY"
        db.refresh(conf)
        assert conf.status == "PENDING" and count(db) == 0
        assert client.post("/api/v1/chat/confirm", json=body, headers=auth(client, "hr")).status_code == 404
    finally:
        lease.close()
    assert client.post("/api/v1/chat/confirm", json=body, headers=h).status_code == 200
    assert count(db) == 1


def test_pending_confirmation_error_does_not_leak_run_lock(client, db):
    confirmation_id = _start_escalation(client)
    conf = db.get(Confirmation, confirmation_id)
    conv = db.scalar(select(Conversation).where(Conversation.thread_id == conf.thread_id))
    h = auth(client, "sg-ops")
    rejected = client.post("/api/v1/chat", json={"conversation_id": conv.id, "message": "未处理确认"}, headers=h)
    assert rejected.status_code == 409 and rejected.json()["detail"] != "CONVERSATION_BUSY"
    retry = ConversationLease.acquire(db.get_bind().engine, conv.id)
    retry.close()
