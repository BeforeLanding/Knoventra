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
from knoventra.db.models import Confirmation, Escalation
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
    monkeypatch.setattr(settings, "gate_threshold", 0.6)
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
