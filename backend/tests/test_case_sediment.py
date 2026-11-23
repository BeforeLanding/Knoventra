"""End to end: create help request -> assign -> resolve -> review draft -> publish -> searchable."""
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select

from fakes import ScriptedModel, call, session_factory_for
from knoventra.access.scope import compute_scope
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.api.deps import get_db
from knoventra.auth.security import issue_token
from knoventra.db.models import Chunk, Document, EsOutbox, HelpRequest, Revision, User
from knoventra.help.service import create_request, transition
from knoventra.main import app
from knoventra.retrieval.search import search

TOKEN = "qzvx"  # a term that exists nowhere else in the corpus


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def h(user):
    return {"Authorization": "Bearer " + issue_token(user.id)}


def act(client, user, rid, action, **body):
    r = client.post(f"/api/v1/help-requests/{rid}/actions", json={"action": action, **body}, headers=h(user))
    assert r.status_code == 200, r.text
    return r.json()


def found(db, enc, user, doc_id):
    res = search(db, enc, compute_scope(db, user), f"{TOKEN} 传感器漂移", top_k=20, keyword=None)
    return any(e.document_id == doc_id for e in res.evidence)


def file_request(db, enc, user):
    """Create the request the way users do: agent slots, confirmation card, approve."""
    def agent(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("已提交。")
        return call("request_help", {"machine_id": "630301", "symptom": f"{TOKEN} 传感器漂移，读数跳变",
                                     "tried_steps": "重新校零无效"}, "h1")
    graph = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=agent), InMemorySaver()))
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": user.id}}
    graph.invoke({"messages": [HumanMessage("630301 传感器漂移，帮我提求助单")], "evidence": {}, "sources": [],
                  "error_code": None}, cfg)
    graph.invoke(Command(resume={"approve": True}), cfg)
    return db.scalar(select(HelpRequest).where(HelpRequest.user_id == user.id, HelpRequest.kind == "field_help"))


def test_resolved_help_becomes_a_reviewed_searchable_case(client, db, enc, users):
    requester, admin = users["sg-ops"], users["admin"]
    expert = db.scalar(select(User).where(User.engineer_name == "Tan Wei Ming"))
    req = file_request(db, enc, requester)
    if req.status == "OPEN":  # no proficient match for this symptom: dispatch by hand
        act(client, admin, req.id, "assign", assignee_id=expert.id)
    db.refresh(req)
    solver = db.get(User, req.assignee_id)
    act(client, solver, req.id, "start")
    act(client, solver, req.id, "progress", note=f"现场确认 {TOKEN} 模块供电不稳")
    detail = act(client, solver, req.id, "resolve", resolution=f"更换 {TOKEN} 模块后读数稳定；备件联系 13800138000")
    assert detail["case_status"] == "DRAFT" and detail["events"][-1]["action"] == "case_draft"

    doc = db.get(Document, detail["case_document_id"])
    rev = db.scalar(select(Revision).where(Revision.document_id == doc.id))
    text = " ".join(db.scalars(select(Chunk.text).where(Chunk.revision_id == rev.id)))
    assert rev.status == "DRAFT" and doc.title.startswith("HELP-")
    for part in (f"更换 {TOKEN} 模块", "处理过程：现场确认", "机台：630301", f"求助单 {req.id}", "[PHONE]"):
        assert part in text
    assert "13800138000" not in text
    assert not found(db, enc, requester, doc.id)  # a draft is never searchable

    assert client.post(f"/api/v1/admin/revisions/{rev.id}/publish", headers=h(admin)).status_code == 200
    assert client.get(f"/api/v1/help-requests/{req.id}", headers=h(requester)).json()["case_status"] == "PUBLISHED"
    assert db.scalar(select(EsOutbox).where(EsOutbox.document_id == doc.id)) is not None  # ES sync queued
    assert found(db, enc, requester, doc.id)
    assert not found(db, enc, users["hr"], doc.id)  # field cases follow the Ops-only grant


def test_reopen_and_resolve_again_drafts_a_new_revision(client, db, enc, users):
    requester, expert = users["sg-ops"], users["lin.boyu"]
    req = create_request(db, request_id=uuid.uuid4().hex, requester_id=requester.id, operation_id=uuid.uuid4().hex,
                         kind="field_help", summary="s", evidence_refs=[],
                         slots={"machine_id": "630301", "symptom": f"{TOKEN} 偶发报警", "tried_steps": "重启"})
    transition(db, req.id, users["admin"], "assign", assignee_id=expert.id)
    transition(db, req.id, expert, "start")
    db.commit()
    first = act(client, expert, req.id, "resolve", resolution="第一次处理")
    act(client, requester, req.id, "reopen", note="又出现了")
    second = act(client, expert, req.id, "resolve", resolution="第二次处理：更换线缆")
    assert first["case_document_id"] == second["case_document_id"]
    versions = db.execute(select(Revision.version, Revision.status)
                          .where(Revision.document_id == second["case_document_id"]).order_by(Revision.version)).all()
    assert [tuple(v) for v in versions] == [(1, "DRAFT"), (2, "DRAFT")]


def test_expert_escalations_do_not_draft_cases(client, db, users):
    req = create_request(db, request_id=uuid.uuid4().hex, requester_id=users["sg-ops"].id,
                         operation_id=uuid.uuid4().hex, kind="expert_escalation", summary="请专家看看",
                         evidence_refs=[])
    transition(db, req.id, users["admin"], "assign", assignee_id=users["lin.boyu"].id)
    transition(db, req.id, users["lin.boyu"], "start")
    db.commit()
    detail = act(client, users["lin.boyu"], req.id, "resolve", resolution="已电话说明")
    assert detail["case_document_id"] is None and detail["case_status"] is None


def test_case_title_is_redacted(db, users):
    from knoventra.help.sediment import case_title

    req = create_request(db, request_id=uuid.uuid4().hex, requester_id=users["sg-ops"].id,
                         operation_id=uuid.uuid4().hex, kind="field_help", summary="s", evidence_refs=[],
                         slots={"machine_id": "630301", "symptom": "漏液，联系客户 13800138000", "tried_steps": "x"})
    assert "13800138000" not in case_title(req) and "[PHONE]" in case_title(req)
