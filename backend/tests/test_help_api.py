"""Expert recommendation, auto-assignment and the help request HTTP API."""
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select

from fakes import ScriptedModel, call, session_factory_for, record_decision
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.api.deps import get_db
from knoventra.auth.security import issue_token
from knoventra.db.models import HelpRequest, Skill, User
from knoventra.help.recommend import recommend
from knoventra.help.service import assign_by_system, create_request, events, transition
from knoventra.main import app

SKILL = "widget calibration"


@pytest.fixture
def skills(db):
    rows = [("Zhou Lan", "proficient", 5), ("Chen Lixing", "proficient", 3), ("Tan Wei Ming", "proficient", 1),
            ("Lin Boyu", "familiar", 9)]
    for name, level, count in rows:
        db.add(Skill(machine_id="610101", engineer_name=name, skill_key=SKILL, skill_item=SKILL, op_count=count,
                     level=level, note="", company="NRS", source_file="t.xlsx", source_row=1))
    db.flush()


def engineer(db, name):
    return db.scalar(select(User).where(User.engineer_name == name))


def new_request(db, requester, **kw):
    return create_request(db, request_id=uuid.uuid4().hex, requester_id=requester.id, operation_id=uuid.uuid4().hex,
                          kind="field_help", summary=f"{SKILL} 偏差", evidence_refs=[],
                          slots={"machine_id": "610101", "symptom": f"{SKILL} 偏差", "tried_steps": "重启"}, **kw)


# ------------------------------------------------------------------ recommendation

def test_same_region_first_then_lower_load(db, users, skills):
    rec = recommend(db, users["cn-ops"], f"{SKILL} 偏差")
    assert [c.engineer for c in rec.candidates] == ["Zhou Lan", "Chen Lixing", "Tan Wei Ming"]  # familiar excluded
    assert rec.chosen.engineer == "Zhou Lan" and "同区域" in rec.reason and "作业 5 次" in rec.reason

    busy = new_request(db, users["sg-ops"])
    assign_by_system(db, busy.id, engineer(db, "Zhou Lan").id, "test")
    assert recommend(db, users["cn-ops"], SKILL).chosen.engineer == "Chen Lixing"  # Zhou now has 1 open
    assert recommend(db, users["sg-ops"], SKILL).chosen.engineer == "Tan Wei Ming"


def test_no_proficient_engineer_means_dispatch_queue(db, users, skills):
    rec = recommend(db, users["cn-ops"], "完全无关的问题")
    assert rec.chosen is None and "待派单" in rec.reason


def test_system_assignment_only_touches_open_unassigned_requests(db, users):
    req = new_request(db, users["sg-ops"])
    transition(db, req.id, users["admin"], "assign", assignee_id=users["lin.boyu"].id)
    assign_by_system(db, req.id, engineer(db, "Zhou Lan").id, "late replay")
    assert req.assignee_id == users["lin.boyu"].id and [e.action for e in events(db, req.id)] == ["created", "assign"]


def test_confirmed_card_shows_reason_and_auto_assigns(db, enc, users, skills):
    def agent(messages):
        last = messages[-1]
        if isinstance(last, ToolMessage):
            return AIMessage("已处理。")
        return call("request_help", {"machine_id": "610101", "symptom": f"{SKILL} 偏差", "tried_steps": "重启过"}, "h1")
    graph = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=agent), InMemorySaver()))
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": users["cn-ops"].id}}
    pending = graph.invoke({"messages": [HumanMessage("610101 校准偏差，帮我提求助单")], "evidence": {}, "sources": [],
                            "error_code": None}, cfg)
    assert "推荐专家：Zhou Lan" in pending["__interrupt__"][0].value["summary"]
    record_decision(db, graph, cfg, True)
    graph.invoke(Command(resume={"approve": True}), cfg)
    req = db.scalar(select(HelpRequest).where(HelpRequest.user_id == users["cn-ops"].id,
                                              HelpRequest.kind == "field_help"))
    assert req.status == "ASSIGNED" and req.assignee_id == engineer(db, "Zhou Lan").id
    trail = events(db, req.id)
    assert [(e.action, e.actor_id) for e in trail] == [("created", users["cn-ops"].id), ("assign", None)]
    assert trail[1].note.startswith("系统推荐：")


# ------------------------------------------------------------------ HTTP

@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def h(user):
    return {"Authorization": "Bearer " + issue_token(user.id)}


def act(client, user, rid, action, **body):
    return client.post(f"/api/v1/help-requests/{rid}/actions", json={"action": action, **body}, headers=h(user))


def test_requester_expert_and_dispatcher_flow(client, db, users):
    requester, expert, admin = users["sg-ops"], engineer(db, "Tan Wei Ming"), users["admin"]
    req = new_request(db, requester)
    queue = client.get("/api/v1/help-requests?scope=queue", headers=h(admin)).json()["items"]
    assert req.id in [i["id"] for i in queue]
    assert act(client, admin, req.id, "assign", assignee_id=expert.id).json()["status"] == "ASSIGNED"
    assigned = client.get("/api/v1/help-requests?scope=assigned", headers=h(expert)).json()["items"]
    assert [i["id"] for i in assigned] == [req.id] and assigned[0]["my_roles"] == ["assignee"]
    assert act(client, expert, req.id, "start").status_code == 200
    assert act(client, expert, req.id, "progress", note="远程查看日志").status_code == 200
    done = act(client, expert, req.id, "resolve", resolution="更换传感器").json()
    assert done["status"] == "RESOLVED" and done["resolution"] == "更换传感器"
    mine = client.get("/api/v1/help-requests", headers=h(requester)).json()["items"]
    assert mine[0]["id"] == req.id and mine[0]["assignee"] == "Tan Wei Ming"
    detail = act(client, requester, req.id, "close").json()
    assert [e["action"] for e in detail["events"]] == ["created", "assign", "start", "progress", "resolve",
                                                       "case_draft", "close"]
    assert detail["events"][-1]["actor"] == requester.email


@pytest.mark.parametrize("who, action, body, status", [
    ("hr", "start", {}, 404),                       # unrelated user: existence not revealed
    ("sg-ops", "start", {}, 403),                   # requester cannot act as the expert
    ("admin", "close", {}, 409),                    # not resolved yet
    ("admin", "assign", {"assignee_id": "x"}, 422),
    ("admin", "fly", {}, 422),                      # unknown action fails validation
])
def test_errors_map_to_http_codes(client, db, users, who, action, body, status):
    req = new_request(db, users["sg-ops"])
    db.commit()  # the API rolls back on errors; only committed setup survives (outer test transaction still undoes it)
    r = act(client, users[who], req.id, action, **body)
    assert r.status_code == status
    assert [e.action for e in events(db, req.id)] == ["created"]


def test_visibility_and_queue_permissions(client, db, users):
    req = new_request(db, users["sg-ops"])
    assert client.get(f"/api/v1/help-requests/{req.id}", headers=h(users["hr"])).status_code == 404
    assert client.get("/api/v1/help-requests?scope=queue", headers=h(users["sg-ops"])).status_code == 403
    assert client.get(f"/api/v1/help-requests/{req.id}/recommendation", headers=h(users["sg-ops"])).status_code == 403
    assert client.get(f"/api/v1/help-requests/{req.id}", headers=h(users["admin"])).status_code == 200


def test_returning_to_the_conversation_shows_request_updates(client, db, enc, users):
    from knoventra.api.chat import get_graph
    from knoventra.db.models import Conversation, Message

    requester, expert = users["sg-ops"], users["lin.boyu"]

    def agent(messages):
        last = messages[-1]
        if isinstance(last, ToolMessage):
            return AIMessage("已处理。")
        return call("request_help", {"machine_id": "610101", "symptom": "pump 不转", "tried_steps": "重启过"}, "h1")
    graph = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=agent), InMemorySaver()))
    conv = Conversation(user_id=requester.id, title="现场求助")
    db.add(conv)
    db.flush()
    cfg = {"configurable": {"thread_id": conv.thread_id, "user_id": requester.id}}
    graph.invoke({"messages": [HumanMessage("610101 pump 不转，帮我提单")], "evidence": {}, "sources": [],
                  "error_code": None}, cfg)
    record_decision(db, graph, cfg, True)
    graph.invoke(Command(resume={"approve": True}), cfg)
    db.add(Message(conversation_id=conv.id, role="assistant", content="已创建求助单。"))
    db.flush()
    req = db.scalar(select(HelpRequest).where(HelpRequest.user_id == requester.id, HelpRequest.kind == "field_help"))
    app.dependency_overrides[get_graph] = lambda: graph

    def updates():
        state = client.get(f"/api/v1/conversations/{conv.id}/state", headers=h(requester)).json()
        return state["help_updates"]
    assert updates() == []  # nothing changed since the last message
    transition(db, req.id, users["admin"], "assign", assignee_id=expert.id)
    transition(db, req.id, expert, "start")
    transition(db, req.id, expert, "progress", note="已远程查看 alarm log")
    got = updates()
    assert [(u["id"], u["status"], u["action"], u["note"]) for u in got] == [
        (req.id, "IN_PROGRESS", "progress", "已远程查看 alarm log")]

    db.add(Message(conversation_id=conv.id, role="user", content="好的"))  # the user has seen it
    db.flush()
    assert updates() == []
    transition(db, req.id, requester, "cancel")  # the requester's own change is not news to them
    assert updates() == []
