import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from sqlalchemy import func, select

from fakes import ScriptedModel, call, session_factory_for
from knoventra.agent.checkpoint import make_checkpointer
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.config import settings
from knoventra.db.models import Escalation


@pytest.fixture(scope="session")
def saver(engine):
    s, pool = make_checkpointer(settings.database_url)
    yield s
    pool.close()


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setattr(settings, "gate_threshold", 0.6)


def make_graph(db, enc, saver, script):
    return build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=script), saver))


def run(graph, user, text, thread=None):
    thread = thread or uuid.uuid4().hex
    cfg = {"configurable": {"thread_id": thread, "user_id": user.id}}
    out = graph.invoke({"messages": [HumanMessage(text)], "evidence": {}, "sources": [], "error_code": None}, cfg)
    return out, cfg


def qa_script(msgs):
    last = msgs[-1]
    if isinstance(last, ToolMessage):
        return AIMessage("先检查冷却液循环泵 [S1]，再核对液位 [S9]。" if "S1" in last.content else "没有可靠依据，请联系 Ops 团队。")
    return call("search_knowledge", {"query": "E17-42 排查"})


def test_answer_has_registered_sources_and_forged_marker_dropped(db, enc, saver, users):
    out, _ = run(make_graph(db, enc, saver, qa_script), users["sg-ops"], "E17 设备报代码 42 先查什么")
    final = out["messages"][-1].content
    assert "[S1]" in final and "[S9]" not in final
    assert [s["alias"] for s in out["sources"]] == ["S1"]
    assert out["sources"][0]["document_title"].startswith("E17") and out["sources"][0]["page"] == 2
    assert out["error_code"] is None


def test_unauthorized_user_gets_refusal_and_no_restricted_text(db, enc, saver, users):
    out, _ = run(make_graph(db, enc, saver, qa_script), users["hr"], "E17-42 排查")
    assert out["error_code"] == "INSUFFICIENT_EVIDENCE"
    assert out["sources"] == []
    blob = " ".join(m.content for m in out["messages"])
    assert "冷却液" not in blob


def test_run_limit(db, enc, saver, users):
    n = [0]

    def loop(msgs):
        n[0] += 1
        return call("search_knowledge", {"query": "E17-42"}, cid=f"c{n[0]}")

    out, _ = run(make_graph(db, enc, saver, loop), users["sg-ops"], "x")
    assert out["error_code"] == "RUN_LIMIT"
    assert n[0] == settings.max_tool_calls_per_turn + 1  # the over-limit call is refused, not executed


def esc_script(msgs):
    if isinstance(msgs[-1], ToolMessage):
        return AIMessage("已处理：" + msgs[-1].content)
    return call("escalate_to_expert", {"summary": "E17-42 复位后仍报警"}, cid="esc1")


def esc_count(db, user):
    return db.scalar(select(func.count()).select_from(Escalation).where(Escalation.user_id == user.id))


def test_escalation_interrupt_approve_is_idempotent_across_restart(db, enc, saver, users):
    user = users["sg-ops"]
    g = make_graph(db, enc, saver, esc_script)
    out, cfg = run(g, user, "帮我转人工")
    assert g.get_state(cfg).next and esc_count(db, user) == 0
    payload = g.get_state(cfg).tasks[0].interrupts[0].value
    assert payload["summary"].startswith("E17-42")

    # process "restart": a brand-new graph object over the same checkpointer
    g2 = make_graph(db, enc, saver, esc_script)
    assert g2.get_state(cfg).next
    out2 = g2.invoke(Command(resume={"approve": True}), cfg)
    assert esc_count(db, user) == 1 and "已创建转交记录" in out2["messages"][-1].content
    assert not g2.get_state(cfg).next
    # the node itself is idempotent even if resumed again with the same operation id
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    e = db.scalar(select(Escalation).where(Escalation.user_id == user.id))
    db.execute(pg_insert(Escalation).values(id="dup", user_id=user.id, operation_id=e.operation_id, summary="x",
                                            status="OPEN").on_conflict_do_nothing(index_elements=["user_id", "operation_id"]))
    assert esc_count(db, user) == 1


def test_escalation_rejected_creates_nothing(db, enc, saver, users):
    user = users["sg-ops"]
    g = make_graph(db, enc, saver, esc_script)
    _, cfg = run(g, user, "转人工")
    out = g.invoke(Command(resume={"approve": False}), cfg)
    assert esc_count(db, user) == 0 and "已取消" in out["messages"][-2].content
