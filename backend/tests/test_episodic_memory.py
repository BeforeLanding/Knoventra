"""D12: episodic memory with re-validation, user profile, context assembler, memory API."""
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from fakes import ScriptedModel, call, session_factory_for
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.api.deps import get_db
from knoventra.auth.security import issue_token
from knoventra.config import settings
from knoventra.db.models import Chunk, Episode, Profile, Revision
from knoventra.help.service import create_request, transition
from knoventra.main import app
from knoventra.memory import profile as user_profile
from knoventra.memory.assembler import EPISODES_HEADER, PROFILE_HEADER, assemble
from knoventra.memory.context import SUMMARY_HEADER
from knoventra.memory.episodes import recall, remember_answer, remember_help


@pytest.fixture(autouse=True)
def lexical_floor(monkeypatch):
    # The offline hash encoder is lexical, not semantic: lower the floor so paraphrases match.
    monkeypatch.setattr(settings, "episode_min_score", 0.05)


def cited_agent(messages):
    last = messages[-1]
    if isinstance(last, ToolMessage):
        return AIMessage("先检查冷却液循环泵 [S1]。" if "[S1]" in last.content else "没有可靠依据。")
    return call("search_knowledge", {"query": "E17-42 排查"})


def graph_for(db, enc, agent=cited_agent, **kw):
    return build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=agent), InMemorySaver(), **kw))


def ask(graph, user, text, thread=None):
    cfg = {"configurable": {"thread_id": thread or uuid.uuid4().hex, "user_id": user.id}}
    return graph.invoke({"messages": [HumanMessage(text, id=uuid.uuid4().hex)], "evidence": {}, "sources": [],
                         "error_code": None}, cfg)


def recall_trace(out):
    return next(e for e in out["memory_trace"] if e["event"] == "recall")["episodes"]


def system_of(seen):
    return next(m for m in seen if isinstance(m, SystemMessage)).content


# ------------------------------------------------------------------ write and recall

def test_cited_answer_becomes_an_episode_recalled_in_another_conversation(db, enc, users):
    user = users["sg-ops"]
    first = ask(graph_for(db, enc), user, "E17-42 报警怎么排查")
    ep = db.scalar(select(Episode).where(Episode.user_id == user.id, Episode.source_kind == "answer"))
    assert ep is not None and ep.refs == [s["chunk_id"] for s in first["sources"]]
    assert "[S1]" not in ep.text and ep.text.startswith("问：E17-42 报警怎么排查")

    seen = []

    def watcher(messages):
        seen.append(messages)
        return AIMessage("你好。")
    out = ask(graph_for(db, enc, agent=watcher), user, "上次 E17-42 报警是怎么处理的")
    assert EPISODES_HEADER in system_of(seen[-1]) and "冷却液循环泵" in system_of(seen[-1])
    assert recall_trace(out)[0] == {"episode_id": ep.id, "kind": "answer", "score": recall_trace(out)[0]["score"],
                                    "kept": True, "reason": None}


def test_refusals_are_not_remembered(db, enc, users):
    ask(graph_for(db, enc, agent=lambda m: AIMessage("【无依据】资料中没有。")), users["sg-ops"], "火星基地怎么去")
    assert db.scalar(select(func.count()).select_from(Episode).where(Episode.user_id == users["sg-ops"].id)) == 0


def test_same_conversation_and_other_users_are_never_recalled(db, enc, users):
    user, thread = users["sg-ops"], uuid.uuid4().hex
    chunk = db.scalars(select(Chunk.id)).first()
    remember_answer(db, enc, user_id=user.id, thread_id=thread, turn_id="t1", question="E17-42 怎么排查",
                    answer="检查泵", chunk_ids=[chunk])
    remember_answer(db, enc, user_id=users["tw-ops"].id, thread_id="x", turn_id="t1", question="E17-42 怎么排查",
                    answer="检查泵", chunk_ids=[chunk])
    found, _ = recall(db, enc, user, "E17-42 怎么排查", thread_id=thread)
    assert found == []  # own episode is in this very conversation; tw-ops' is not ours


# ------------------------------------------------------------------ re-validation before reuse

def _episode_for(db, enc, user, chunk_id):
    remember_answer(db, enc, user_id=user.id, thread_id="old", turn_id=uuid.uuid4().hex,
                    question="E17-42 怎么排查", answer="检查冷却液循环泵", chunk_ids=[chunk_id])


def _published_chunk(db, user):
    from knoventra.access.scope import compute_scope

    readable = compute_scope(db, user).readable_kb_ids
    return db.scalars(select(Chunk).join(Revision, Revision.id == Chunk.revision_id)
                      .where(Revision.status == "PUBLISHED", Chunk.kb_id.in_(readable))).first()


def test_withdrawn_source_drops_the_whole_episode(db, enc, users):
    user = users["sg-ops"]
    chunk = _published_chunk(db, user)
    _episode_for(db, enc, user, chunk.id)
    assert recall(db, enc, user, "E17-42 怎么排查", thread_id="now")[0]
    db.get(Revision, chunk.revision_id).status = "RETIRED"  # the manual was withdrawn
    db.flush()
    found, trace = recall(db, enc, user, "E17-42 怎么排查", thread_id="now")
    assert found == [] and trace[0]["reason"] == "source_not_published" and trace[0]["kept"] is False


def test_lost_read_access_drops_the_episode(db, enc, users):
    user = users["sg-ops"]
    chunk = _published_chunk(db, user)
    _episode_for(db, enc, user, chunk.id)
    from knoventra.db.models import KnowledgeBase
    hr = db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == "hr"))
    chunk.kb_id = hr  # the source moved to a KB this user cannot read
    db.flush()
    found, trace = recall(db, enc, user, "E17-42 怎么排查", thread_id="now")
    assert found == [] and trace[0]["reason"] == "source_not_readable"


def _resolved_request(db, enc, users):
    requester, expert, admin = users["sg-ops"], users["lin.boyu"], users["admin"]
    req = create_request(db, request_id=uuid.uuid4().hex, requester_id=requester.id, operation_id=uuid.uuid4().hex,
                         kind="field_help", summary="WX-38 pump 不转", evidence_refs=[],
                         slots={"machine_id": "630301", "machine_model": "WX-38", "symptom": "切换 tank 时 pump 不转",
                                "tried_steps": "重插 LS-2", "alarm_codes": ["A-1301"]})
    transition(db, req.id, admin, "assign", assignee_id=expert.id)
    transition(db, req.id, expert, "start")
    transition(db, req.id, expert, "resolve", resolution="更换 LS-2 液位传感器后恢复")
    remember_help(db, enc, req)
    return req


def test_resolved_help_request_is_recalled_until_reopened(db, enc, users):
    user = users["sg-ops"]
    req = _resolved_request(db, enc, users)
    found, _ = recall(db, enc, user, "上次那个 A-1301 alarm 后来怎么解决的", thread_id="now")
    assert found and found[0].kind == "help_request" and "更换 LS-2 液位传感器" in found[0].text
    assert "630301" in found[0].text and "A-1301" in found[0].text
    transition(db, req.id, user, "reopen", note="又出现了")
    found, trace = recall(db, enc, user, "上次那个 A-1301 alarm 后来怎么解决的", thread_id="now")
    assert found == [] and trace[0]["reason"] == "help_request_reopened"


def test_resolve_api_writes_the_requesters_episode(db, users):
    requester, expert, admin = users["sg-ops"], users["lin.boyu"], users["admin"]
    req = create_request(db, request_id=uuid.uuid4().hex, requester_id=requester.id, operation_id=uuid.uuid4().hex,
                         kind="field_help", summary="door 无法关闭", evidence_refs=[],
                         slots={"machine_id": "620202", "symptom": "door 无法关闭", "tried_steps": "复位",
                                "alarm_codes": []})
    transition(db, req.id, admin, "assign", assignee_id=expert.id)
    transition(db, req.id, expert, "start")
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        r = client.post(f"/api/v1/help-requests/{req.id}/actions", json={"action": "resolve", "resolution": "调整门锁"},
                        headers={"Authorization": "Bearer " + issue_token(expert.id)})
        assert r.status_code == 200
    finally:
        app.dependency_overrides.clear()
    ep = db.scalar(select(Episode).where(Episode.source_id == req.id))
    assert ep.user_id == requester.id and "调整门锁" in ep.text  # the requester's memory, not the expert's


# ------------------------------------------------------------------ profile

def test_profile_sets_reply_script_only_when_the_question_gives_none(db, enc, users):
    user = users["sg-ops"]
    for _ in range(3):
        user_profile.bump(db, user.id, lang="zh-Hant")
    answer = ScriptedModel(script=lambda m: AIMessage("请检查泵。"))
    graph = build_graph(AgentDeps(session_factory_for(db), enc, None, answer, InMemorySaver()))
    assert ask(graph, user, "pump check?")["messages"][-1].content == "請檢查泵。"
    assert ask(graph, user, "泵怎么检查")["messages"][-1].content == "请检查泵。"  # the question wins


def test_profile_counts_questions_and_filed_machines(db, enc, users):
    user = users["sg-ops"]
    ask(graph_for(db, enc), user, "WX-38 的 E17-42 报警怎么排查")
    prof = db.get(Profile, user.id)
    assert prof.lang_counts == {"zh-Hans": 1} and prof.model_counts == {"WX-38": 1}
    snap = user_profile.snapshot(db, user)
    assert snap["department"] == user.department and snap["preferred_script"] is None  # too few signals


def test_usual_machine_is_a_hint_never_a_filled_slot(db, enc, users):
    user = users["sg-ops"]
    user_profile.bump(db, user.id, machines=["610101", "610101", "620202"])

    def agent(messages):
        last = messages[-1]
        if isinstance(last, ToolMessage):
            return AIMessage("请问是哪台机台？")
        return call("request_help", {"symptom": "pump 不转", "tried_steps": "重启"}, "h1")
    out = ask(graph_for(db, enc, agent=agent), user, "pump 不转，帮我提单")
    tool = next(m for m in out["messages"] if isinstance(m, ToolMessage))
    assert "610101（2 次）" in tool.content and "不要直接填写" in tool.content
    assert "machine_id" not in tool.artifact["help_draft"] and "__interrupt__" not in out


def test_filing_a_request_counts_its_machine(db, enc, users):
    from test_help_requests import run_turns, slot_agent

    user = users["sg-ops"]
    run_turns(db, enc, user, ["WX-38 机台 610101 切换 tank 时报 A-1301，pump 不转", "已经重新插紧 LS-2",
                              {"approve": True}], slot_agent)
    prof = db.get(Profile, user.id)
    assert prof.machine_counts == {"610101": 1} and prof.model_counts.get("WX-38", 0) >= 1


def test_memory_off_writes_and_reads_nothing(db, enc, users):
    user = users["sg-ops"]
    out = ask(graph_for(db, enc, long_term_memory=False), user, "E17-42 报警怎么排查")
    assert out["memory_context"] is None
    assert db.scalar(select(func.count()).select_from(Episode).where(Episode.user_id == user.id)) == 0
    assert db.get(Profile, user.id) is None


# ------------------------------------------------------------------ assembler

def turn(i, chars=120):
    return [HumanMessage(f"问题{i}", id=f"h{i}"), AIMessage("答" * chars, id=f"a{i}")]


def test_assembler_trims_lowest_priority_first_and_keeps_pinned_parts():
    msgs = turn(0) + turn(1) + [HumanMessage("当前问题", id="cur")]
    full = assemble("SYS", msgs, budget=10_000, draft="机台号：610101", profile="部门 Ops", episodes=["e1", "e2"],
                    summary="摘要")
    assert full.trace["dropped"] == [] and [m.id for m in full.messages[1:]] == ["h0", "a0", "h1", "a1", "cur"]
    system = full.messages[0].content
    assert system.index("机台号") < system.index(PROFILE_HEADER) < system.index(EPISODES_HEADER) < system.index(SUMMARY_HEADER)

    tight = assemble("SYS", msgs, budget=60, draft="机台号：610101", profile="部门 Ops", episodes=["e1", "e2"],
                     summary="摘要")
    assert tight.trace["dropped"][:2] == ["turn", "turn"]
    assert [m.id for m in tight.messages[1:]] == ["cur"] and "机台号：610101" in tight.messages[0].content

    nothing_left = assemble("SYS", msgs, budget=1, draft="机台号：610101", profile="部门 Ops", episodes=["e1", "e2"],
                            summary="摘要")
    assert nothing_left.trace["dropped"] == ["turn", "turn", "summary", "episode", "episode", "profile"]
    assert nothing_left.trace["over_budget"] is True  # pinned parts are sent rather than cut
    assert "机台号：610101" in nothing_left.messages[0].content and nothing_left.messages[-1].id == "cur"


def test_assembler_never_splits_the_current_turns_tool_traffic():
    msgs = turn(0) + [HumanMessage("当前", id="cur"),
                      AIMessage("", tool_calls=[{"name": "search_knowledge", "args": {"query": "q"}, "id": "c",
                                                 "type": "tool_call"}], id="call"),
                      ToolMessage("证据" * 200, tool_call_id="c", id="res")]
    out = assemble("SYS", msgs, budget=10, profile="部门 Ops")
    assert [m.id for m in out.messages[1:]] == ["cur", "call", "res"]


# ------------------------------------------------------------------ API

@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def h(user):
    return {"Authorization": "Bearer " + issue_token(user.id)}


def test_memory_api_shows_and_deletes_only_your_own(client, db, enc, users):
    me, other = users["sg-ops"], users["tw-ops"]
    chunk = db.scalars(select(Chunk.id)).first()
    for who in (me, other):
        remember_answer(db, enc, user_id=who.id, thread_id="t", turn_id="turn", question="q", answer="a",
                        chunk_ids=[chunk])
    user_profile.bump(db, me.id, lang="zh-Hans")
    mine = client.get("/api/v1/memory", headers=h(me)).json()
    assert len(mine["episodes"]) == 1 and mine["profile"]["department"] == me.department
    theirs = db.scalar(select(Episode.id).where(Episode.user_id == other.id))
    assert client.delete(f"/api/v1/memory/episodes/{theirs}", headers=h(me)).status_code == 404
    assert client.delete(f"/api/v1/memory/episodes/{mine['episodes'][0]['id']}", headers=h(me)).status_code == 204
    assert client.get("/api/v1/memory", headers=h(me)).json()["episodes"] == []
    user_profile.bump(db, me.id, lang="zh-Hans")
    assert client.delete("/api/v1/memory", headers=h(me)).status_code == 204
    assert db.get(Profile, me.id) is None
    assert db.scalar(select(func.count()).select_from(Episode).where(Episode.user_id == other.id)) == 1
    assert client.get("/api/v1/memory").status_code == 401
