"""Help requests: idempotent creation, state machine, append-only events, slot collection."""
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from fakes import DictWorkingMemory, ScriptedModel, call, session_factory_for
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.memory.drafts import checkpoint_draft
from knoventra.db.models import HelpEvent, HelpRequest
from knoventra.help.service import HelpError, create_request, events, transition


def _new(db, users, op=None, kind="field_help"):
    return create_request(db, request_id=uuid.uuid4().hex, requester_id=users["sg-ops"].id,
                          operation_id=op or uuid.uuid4().hex, kind=kind, summary="WX-38 pump 不转",
                          evidence_refs=[], slots={"machine_id": "630301", "symptom": "pump 不转",
                                                   "tried_steps": "重插 LS-2", "alarm_codes": ["A-1301"]})


def test_creation_is_idempotent_and_writes_one_event(db, users):
    op = uuid.uuid4().hex
    a, b = _new(db, users, op), _new(db, users, op)
    assert a.id == b.id and a.status == "OPEN" and a.alarm_codes == ["A-1301"] and a.machine_id == "630301"
    assert [e.action for e in events(db, a.id)] == ["created"]


def test_full_lifecycle_is_audited(db, users):
    req = _new(db, users)
    admin, expert, requester = users["admin"], users["lin.boyu"], users["sg-ops"]
    transition(db, req.id, admin, "assign", assignee_id=expert.id)
    transition(db, req.id, expert, "start")
    transition(db, req.id, expert, "progress", note="已远程查看 alarm log")
    transition(db, req.id, expert, "resolve", resolution="更换 LS-2 传感器后恢复")
    assert req.status == "RESOLVED" and req.resolved_at is not None and req.resolution == "更换 LS-2 传感器后恢复"
    transition(db, req.id, requester, "close")
    trail = [(e.action, e.from_status, e.to_status) for e in events(db, req.id)]
    assert trail == [("created", None, "OPEN"), ("assign", "OPEN", "ASSIGNED"), ("start", "ASSIGNED", "IN_PROGRESS"),
                     ("progress", "IN_PROGRESS", "IN_PROGRESS"), ("resolve", "IN_PROGRESS", "RESOLVED"),
                     ("close", "RESOLVED", "CLOSED")]
    assert req.closed_at is not None


@pytest.mark.parametrize("steps, actor, action, kwargs, code", [
    ([], "cn-ops", "start", {}, "NOT_FOUND"),             # not requester, assignee or admin
    ([], "sg-ops", "start", {}, "FORBIDDEN"),             # requester cannot act as the expert
    ([], "admin", "start", {}, "FORBIDDEN"),
    ([], "admin", "resolve", {}, "FORBIDDEN"),
    ([("assign",)], "lin.boyu", "resolve", {"resolution": "x"}, "INVALID_TRANSITION"),  # must start first
    ([("assign",), ("start",)], "lin.boyu", "resolve", {"resolution": "  "}, "INVALID_INPUT"),
    ([("assign",), ("start",)], "lin.boyu", "progress", {}, "INVALID_INPUT"),
    ([], "admin", "assign", {"assignee_id": "nobody"}, "INVALID_INPUT"),
    ([], "sg-ops", "teleport", {}, "INVALID_INPUT"),
    ([("cancel",)], "sg-ops", "cancel", {}, "INVALID_TRANSITION"),                   # terminal state
])
def test_invalid_moves_are_refused(db, users, steps, actor, action, kwargs, code):
    req = _new(db, users)
    actors = {"assign": users["admin"], "start": users["lin.boyu"], "cancel": users["sg-ops"]}
    for (step,) in steps:
        transition(db, req.id, actors[step], step, assignee_id=users["lin.boyu"].id if step == "assign" else None)
    before = len(events(db, req.id))
    with pytest.raises(HelpError) as exc:
        transition(db, req.id, users[actor], action, **kwargs)
    assert exc.value.code == code and len(events(db, req.id)) == before


def test_reopen_clears_resolution_time(db, users):
    req = _new(db, users)
    transition(db, req.id, users["admin"], "assign", assignee_id=users["lin.boyu"].id)
    transition(db, req.id, users["lin.boyu"], "start")
    transition(db, req.id, users["lin.boyu"], "resolve", resolution="重启")
    transition(db, req.id, users["sg-ops"], "reopen", note="又出现了")
    assert req.status == "IN_PROGRESS" and req.resolved_at is None


def test_events_are_append_only_while_the_request_exists(db, users):
    req = _new(db, users)
    event_id = events(db, req.id)[0].id
    for stmt in ("UPDATE knv_help_event SET note = 'x' WHERE id = :id", "DELETE FROM knv_help_event WHERE id = :id"):
        with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
            db.execute(text(stmt), {"id": event_id})
    db.execute(text("DELETE FROM knv_help_request WHERE id = :id"), {"id": req.id})  # purge cascades
    assert db.scalar(select(func.count()).select_from(HelpEvent).where(HelpEvent.request_id == req.id)) == 0


# ------------------------------------------------------------------ slot collection in the graph

def open_draft(out):
    """The slot draft as recorded in the checkpoint (None when no draft is open)."""
    return checkpoint_draft(out["messages"], out.get("pending"))[1]


def run_turns(db, enc, user, turns, agent, memory=None):
    graph = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=agent), InMemorySaver(),
                                  memory=memory))
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": user.id}}
    out = None
    for t in turns:
        inp = Command(resume=t) if isinstance(t, dict) else {"messages": [HumanMessage(t)], "evidence": {},
                                                              "sources": [], "error_code": None}
        out = graph.invoke(inp, cfg)
    return out


def slot_agent(messages):
    last = messages[-1]
    if isinstance(last, ToolMessage):
        return AIMessage("还缺已尝试的步骤，请问你已经做过哪些处理？" if "还缺" in last.content else "已处理。")
    said = last.content
    if "610101" in said:
        return call("request_help", {"machine_id": "610101", "symptom": "切换 tank 时报 A-1301，pump 不转"}, "h1")
    return call("request_help", {"tried_steps": said}, "h2")


def test_slots_are_collected_over_turns_then_confirmed(db, enc, users):
    user = users["sg-ops"]
    asked = run_turns(db, enc, user, ["WX-38 机台 610101 切换 tank 时报 A-1301，pump 不转，帮我提个求助单"], slot_agent)
    assert open_draft(asked)["machine_id"] == "610101" and open_draft(asked)["alarm_codes"] == ["A-1301"]
    assert "已尝试" in asked["messages"][-1].content and "__interrupt__" not in asked

    out = run_turns(db, enc, user, ["WX-38 机台 610101 切换 tank 时报 A-1301，pump 不转，帮我提个求助单",
                                    "已经重新插紧 LS-2，还是不行", {"approve": True}], slot_agent)
    req = db.scalar(select(HelpRequest).where(HelpRequest.user_id == user.id, HelpRequest.kind == "field_help"))
    assert (req.machine_id, req.machine_model, req.alarm_codes) == ("610101", "WX-38", ["A-1301"])
    assert req.tried_steps == "已经重新插紧 LS-2，还是不行" and "机台号：610101" in req.summary
    assert open_draft(out) is None and [e.action for e in events(db, req.id)] == ["created"]


def test_confirmation_card_shows_every_slot(db, enc, users):
    out = run_turns(db, enc, users["sg-ops"], ["WX-38 机台 610101 切换 tank 时报 A-1301，pump 不转",
                                               "已经重新插紧 LS-2"], slot_agent)
    card = out["__interrupt__"][0].value["summary"]
    for label in ("机台号：610101", "机型：WX-38", "告警码：A-1301", "故障现象：", "已尝试的步骤：已经重新插紧 LS-2"):
        assert label in card


def test_cancel_clears_the_draft_without_calling_the_model(db, enc, users):
    calls = []

    def agent(messages):
        calls.append(messages[-1])
        return slot_agent(messages)
    out = run_turns(db, enc, users["sg-ops"], ["机台 610101 pump 不转", "算了"], agent)
    assert open_draft(out) is None and "已取消求助单草稿" in out["messages"][-1].content
    assert not any(isinstance(m, HumanMessage) and m.content == "算了" for m in calls)
    assert db.scalar(select(func.count()).select_from(HelpRequest)
                     .where(HelpRequest.user_id == users["sg-ops"].id, HelpRequest.kind == "field_help")) == 0


def test_declined_card_files_nothing_and_ends_the_draft(db, enc, users):
    out = run_turns(db, enc, users["sg-ops"], ["机台 610101 pump 不转", "已经重启过", {"approve": False}], slot_agent)
    assert open_draft(out) is None
    assert db.scalar(select(func.count()).select_from(HelpRequest)
                     .where(HelpRequest.user_id == users["sg-ops"].id, HelpRequest.kind == "field_help")) == 0


@pytest.mark.parametrize("text, cancels", [
    ("算了", True), ("算了吧", True), ("取消吧", True), ("取消求助", True), ("不用了，谢谢", True), ("cancel", True),
    ("取消了 interlock 后重启还是不行", False), ("算了一下大概 30 分钟", False),
])
def test_cancel_phrases(text, cancels):
    from knoventra.help.slots import CANCEL

    assert bool(CANCEL.match(text)) is cancels


def test_symptom_contributes_only_real_alarm_codes():
    from knoventra.help.slots import merge

    draft = merge(None, {"symptom": "WX-38 pump 不转，按 SOP-WCL-380110-001 复位无效，报 alarm A-1301", "alarm": "E17-42"},
                  [HumanMessage("x")])
    assert draft["alarm_codes"] == ["A-1301", "E17-42"]


def test_assignee_must_be_an_engineer(db, users):
    req = _new(db, users)
    with pytest.raises(HelpError) as exc:
        transition(db, req.id, users["admin"], "assign", assignee_id=users["cn-ops"].id)
    assert exc.value.code == "INVALID_INPUT"


def test_abandoned_draft_expires_after_an_unrelated_turn(db, enc, users):
    def agent(messages):
        last = messages[-1]
        if isinstance(last, ToolMessage):
            return AIMessage("还缺已尝试的步骤。")
        if "610101" in last.content:
            return call("request_help", {"machine_id": "610101", "symptom": "pump 不转"}, "h1")
        return AIMessage("年假按工龄计算。")
    out = run_turns(db, enc, users["sg-ops"], ["610101 pump 不转，帮我提单", "另外问一下年假怎么算", "谢谢"], agent)
    assert open_draft(out) is None
