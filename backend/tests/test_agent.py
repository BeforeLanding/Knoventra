import uuid
import hashlib
import datetime as dt

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from sqlalchemy import func, select

from fakes import ScriptedModel, call, session_factory_for
from knoventra.agent.checkpoint import make_checkpointer
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.config import settings
from knoventra.db.models import Confirmation, Escalation


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


@pytest.mark.parametrize("account", ["sg-ops", "hr"])
def test_search_trace_times_completed_calls_including_refusals(db, enc, saver, users, monkeypatch, account):
    import knoventra.agent.graph as graph_module

    # Replace only this module's clock, preserving LangGraph's own timers.
    from types import SimpleNamespace
    readings = iter([10.0, 10.125])
    monkeypatch.setattr(graph_module, "time", SimpleNamespace(perf_counter=lambda: next(readings)))
    out, _ = run(make_graph(db, enc, saver, qa_script), users[account], "E17-42 排查")
    assert len(out["retrieval_trace"]) == 1
    assert out["retrieval_trace"][0]["latency_ms"] == 125.0
    assert out["retrieval_trace"][0]["status"] == ("OK" if account == "sg-ops" else "INSUFFICIENT_EVIDENCE")


def test_run_limit(db, enc, saver, users):
    n = [0]

    def loop(msgs):
        n[0] += 1
        return call("search_knowledge", {"query": "E17-42"}, cid=f"c{n[0]}")

    out, _ = run(make_graph(db, enc, saver, loop), users["sg-ops"], "x")
    assert out["error_code"] == "RUN_LIMIT"
    assert n[0] == settings.max_tool_calls_per_turn + 1  # the over-limit call is refused, not executed


@pytest.mark.parametrize("answer", ["直接停机并更换全部部件。", "请停机 [S99]", "", "[S0]"])
def test_retrieval_answer_requires_at_least_one_registered_citation(db, enc, saver, users, answer):
    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage(answer)
        return call("search_knowledge", {"query": "E17-42 排查"})
    out, _ = run(make_graph(db, enc, saver, script), users["sg-ops"], "E17-42 排查")
    assert out["evidence"] and out["retrieval_trace"][0]["status"] == "OK"
    assert out["error_code"] == "MODEL_CITATION_MISSING" and out["sources"] == []
    assert "缺少有效引用" in out["messages"][-1].content
    assert out["model_declared_refusal"] is False


@pytest.mark.parametrize("answer", ["直接绕过联锁并继续运行 [S1]", "【无依据】但是仍可绕过联锁 [S1]"])
def test_blocked_search_final_answer_is_fixed_even_if_model_ignores_refusal(db, enc, saver, users, answer):
    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage(answer)
        return call("search_knowledge", {"query": "E17-44 排查"})
    out, _ = run(make_graph(db, enc, saver, script), users["sg-ops"], "E17-44 排查")
    assert out["error_code"] == "INSUFFICIENT_EVIDENCE" and not out["sources"] and not out["evidence"]
    assert "绕过联锁" not in out["messages"][-1].content
    assert out["messages"][-1].content == "资料中没有可靠依据，无法回答该问题。请联系对应职能团队。"
    assert out["model_declared_refusal"] is answer.startswith("【无依据】")


def test_greeting_and_workflow_status_do_not_require_knowledge_citations(db, enc, saver, users):
    greeting, _ = run(make_graph(db, enc, saver, lambda msgs: AIMessage("你好。")), users["sg-ops"], "你好")
    assert greeting["error_code"] is None and greeting["messages"][-1].content == "你好。"
    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage(msgs[-1].content)
        return call("get_escalation_status", {})
    status, _ = run(make_graph(db, enc, saver, script), users["sg-ops"], "转交进度")
    assert status["error_code"] is None and status["sources"] == []
    assert status["messages"][-1].content == "暂无转交记录"


def test_mixed_search_and_confirmed_escalation_acknowledgement_needs_no_citation(db, enc, saver, users):
    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage("转交已处理，请查看记录。")
        return AIMessage("", tool_calls=[
            {"name": "search_knowledge", "args": {"query": "E17-42 排查"}, "id": "search", "type": "tool_call"},
            {"name": "escalate_to_expert", "args": {"summary": "E17 报警"}, "id": "escalate", "type": "tool_call"},
        ])
    graph = make_graph(db, enc, saver, script)
    out, cfg = run(graph, users["sg-ops"], "E17-42 排查并转人工")
    assert out["evidence"] and graph.get_state(cfg).next
    out = graph.invoke(Command(resume={"approve": True}), cfg)
    assert out["error_code"] is None and out["sources"] == []
    assert esc_count(db, users["sg-ops"]) == 1


@pytest.mark.parametrize("reason,invalid,expected", [
    ("length", False, "MODEL_OUTPUT_TRUNCATED"),
    ("length", True, "MODEL_OUTPUT_TRUNCATED"),
    ("tool_calls", True, "MODEL_TOOL_CALL_INVALID"),
])
def test_incomplete_response_never_executes_even_valid_sibling_tool_call(db, enc, saver, users, reason, invalid, expected):
    from unittest.mock import Mock
    model = Mock()
    response = call("escalate_to_expert", {"summary": "must not execute"})
    response.content = "不完整的输出 [S1]"
    response.response_metadata = {"finish_reason": reason}
    if invalid:
        response.invalid_tool_calls = [{"name": "search_knowledge", "args": '{"query":',
                                        "id": "broken", "error": "invalid JSON", "type": "invalid_tool_call"}]
    model.bind_tools.return_value.invoke.return_value = response
    graph = build_graph(AgentDeps(session_factory_for(db), enc, None, model, saver))
    out, cfg = run(graph, users["sg-ops"], "转人工")
    assert out["error_code"] == expected and not graph.get_state(cfg).next
    assert out["sources"] == [] and out["pending"] is None
    assert not out["messages"][-1].tool_calls and not out["messages"][-1].invalid_tool_calls
    assert "不完整的输出" not in out["messages"][-1].content
    assert out["model_finish_reason"] == reason and out["model_declared_refusal"] is False
    assert db.scalar(select(func.count()).select_from(Confirmation)) == 0
    assert esc_count(db, users["sg-ops"]) == 0
    model.bind_tools.return_value.invoke.assert_called_once()


def test_truncated_answer_keeps_retrieval_diagnostics_without_citations_and_next_turn_can_complete(db, enc, saver, users):
    responses = iter([AIMessage("半句 [S1]", response_metadata={"finish_reason": "length"}),
                      AIMessage("完整回答 [S1]", response_metadata={"finish_reason": "stop"})])
    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return next(responses)
        return call("search_knowledge", {"query": "E17-42 排查"})
    graph = make_graph(db, enc, saver, script)
    out, cfg = run(graph, users["sg-ops"], "E17-42 排查")
    assert out["evidence"] and out["retrieval_trace"][0]["status"] == "OK"
    assert out["sources"] == [] and out["error_code"] == "MODEL_OUTPUT_TRUNCATED"
    assert out["model_declared_refusal"] is False
    out, _ = run(graph, users["sg-ops"], "E17-42 排查", thread=cfg["configurable"]["thread_id"])
    assert out["sources"] and out["error_code"] is None and out["model_finish_reason"] == "stop"


def test_truncated_acknowledgement_does_not_repeat_or_undo_confirmed_escalation(db, enc, saver, users):
    invocations = []
    def script(msgs):
        invocations.append(1)
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage("已创建转交记", response_metadata={"finish_reason": "length"})
        return call("escalate_to_expert", {"summary": "E17 仍报警"}, cid="esc-truncated")
    graph = make_graph(db, enc, saver, script)
    _, cfg = run(graph, users["sg-ops"], "转人工")
    assert esc_count(db, users["sg-ops"]) == 0
    out = graph.invoke(Command(resume={"approve": True}), cfg)
    assert out["error_code"] == "MODEL_OUTPUT_TRUNCATED" and out["sources"] == []
    assert not graph.get_state(cfg).next and len(invocations) == 2
    assert esc_count(db, users["sg-ops"]) == 1


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


def test_legacy_pending_checkpoint_preserves_operation_on_restart(db, enc, saver, users):
    user = users["sg-ops"]
    graph = make_graph(db, enc, saver, esc_script)
    thread = uuid.uuid4().hex
    cfg = {"configurable": {"thread_id": thread, "user_id": user.id}}
    pending = {"tool_call_id": "legacy-tool", "args": {"summary": "旧检查点摘要"}}
    # Reconstruct the saved tools-node output from before operation_id was added.
    graph.update_state(cfg, {"messages": [HumanMessage("转人工"),
                                           call("escalate_to_expert", pending["args"], cid="legacy-tool")],
                             "pending": pending, "evidence": {}, "sources": [], "error_code": None}, as_node="tools")
    graph.invoke(None, cfg)
    conf_id = graph.get_state(cfg).tasks[0].interrupts[0].value["confirmation_id"]
    expected_op = hashlib.sha256(f"{thread}:legacy-tool".encode()).hexdigest()[:32]
    assert db.get(Confirmation, conf_id).operation_id == expected_op
    restarted = make_graph(db, enc, saver, esc_script)
    restarted.invoke(Command(resume={"approve": True}), cfg)
    rows = db.scalars(select(Confirmation).where(Confirmation.thread_id == thread)).all()
    assert [r.id for r in rows] == [conf_id] and esc_count(db, user) == 1


def test_status_tool_returns_latest_by_time_instead_of_random_uuid(db, enc, saver, users):
    now = dt.datetime(2026, 11, 7, tzinfo=dt.UTC)
    for n in range(6):
        db.add(Escalation(id=f"{100 - n:032x}", user_id=users["sg-ops"].id, operation_id=uuid.uuid4().hex,
                          summary=f"按时间排序{n}", evidence_refs=[], created_at=now + dt.timedelta(minutes=n)))
    db.flush()

    def script(msgs):
        return AIMessage(msgs[-1].content) if isinstance(msgs[-1], ToolMessage) else call("get_escalation_status", {})

    out, _ = run(make_graph(db, enc, saver, script), users["sg-ops"], "查询转交进度")
    answer = out["messages"][-1].content
    assert "按时间排序0" not in answer
    assert [line.split(" - ")[-1] for line in answer.splitlines()] == [f"按时间排序{n}" for n in (5, 4, 3, 2, 1)]


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


@pytest.mark.parametrize("query", ["E17-42 排查", "主轴温度异常", "E17设备代码42"])
def test_split_original_code_survives_model_rewriting(db, enc, saver, users, query):
    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage("【无依据】没有该编码的资料。")
        return call("search_knowledge", {"query": query})
    out, _ = run(make_graph(db, enc, saver, script), users["sg-ops"], "E17設備報代碼45 该怎么恢复")
    assert out["evidence"] == {} and out["sources"] == []
    assert out["retrieval_trace"][0]["missing_identifiers"] == ["E17-45"]
