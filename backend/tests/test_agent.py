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
    monkeypatch.setattr(settings, "gate_sparse_threshold", 1e9)  # dense-only gate for the hash encoder
    monkeypatch.setattr(settings, "gate_threshold", 0.1)  # hash-encoder cosine scale; BGE-M3 default is in config


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
    assert out["model_declared_refusal"] is False
    assert out["retrieval_trace"][0]["status"] == "OK"
    assert out["sources"][0]["chunk_id"] in out["retrieval_trace"][0]["returned_chunk_ids"]


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
    assert out2["retrieval_trace"] == out["retrieval_trace"] == []
    assert out2["model_declared_refusal"] is False
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


@pytest.mark.parametrize("marker", ["【无依据】", "【無依據】"])
def test_model_declared_refusal_hides_sources(db, enc, saver, users, marker):
    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage(f"  {marker}资料里没有保修期信息 [S1]，请联系 Ops 团队。")
        return call("search_knowledge", {"query": "E17-42 排查"})

    out, _ = run(make_graph(db, enc, saver, script), users["sg-ops"], "E17 保修期多长")
    assert out["sources"] == [] and out["error_code"] == "INSUFFICIENT_EVIDENCE"
    assert out["model_declared_refusal"] is True
    assert out["retrieval_trace"][0]["status"] == "OK"
    final = out["messages"][-1].content
    assert marker not in final and "[S1]" not in final and "保修期" in final


@pytest.mark.parametrize("query", ["E17-42 排查", "主轴温度异常", "E17-44"])
def test_query_rewriting_cannot_replace_or_drop_original_code(db, enc, saver, users, query):
    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage("【无依据】没有该编码的可靠依据 [S1]，请联系 Ops。")
        return call("search_knowledge", {"query": query})

    out, _ = run(make_graph(db, enc, saver, script), users["sg-ops"], "E17-44 怎么恢复")
    tool = next(m for m in out["messages"] if isinstance(m, ToolMessage))
    assert "INSUFFICIENT_EVIDENCE" in tool.content and "冷却液" not in tool.content
    assert out["evidence"] == {} and out["sources"] == []


def test_retry_cannot_bypass_original_code_and_next_turn_can_ask_a_real_code(db, enc, saver, users):
    def script(msgs):
        human = next(m for m in reversed(msgs) if isinstance(m, HumanMessage))
        if isinstance(msgs[-1], ToolMessage):
            if "E17-44" in human.content and msgs[-1].tool_call_id == "first":
                return call("search_knowledge", {"query": "主轴温度异常"}, cid="retry")
            return AIMessage("按手册处理 [S1]" if "[S1]" in msgs[-1].content else "【无依据】请联系 Ops。")
        return call("search_knowledge", {"query": "E17-42 排查"}, cid="first")

    g = make_graph(db, enc, saver, script)
    first, cfg = run(g, users["sg-ops"], "E17-44 如何排查")
    assert len([m for m in first["messages"] if isinstance(m, ToolMessage)]) == 2
    assert first["evidence"] == {} and first["error_code"] == "INSUFFICIENT_EVIDENCE"
    assert len(first["retrieval_trace"]) == 2
    assert all(t["reason"] == "IDENTIFIER_MISMATCH" and not t["returned_chunk_ids"] for t in first["retrieval_trace"])
    second, _ = run(g, users["sg-ops"], "那 E17-42 呢", thread=cfg["configurable"]["thread_id"])
    assert second["sources"] and second["error_code"] is None
    assert len(second["retrieval_trace"]) == 1 and second["model_declared_refusal"] is False
