import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from fakes import ScriptedModel, call, session_factory_for
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.agent.language import answer_script, format_answer
from knoventra.config import settings
from knoventra.db.models import Chunk


@pytest.mark.parametrize("question,script", [
    ("泵組壓力不足 P03-11 怎麼處理", "zh-Hant"),
    ("泵组压力不足 P03-11 怎么处理", "zh-Hans"),
    ("泵組日常檢查要做什麼", "zh-Hant"),
    ("P03-11", None), ("How do I fix P03-11?", None), ("日常保养", "zh-Hans"),
    ("设备檢查", None), ("`設備ID` 怎么处理", "zh-Hans"),
])
def test_script_uses_current_question_and_does_not_guess_ambiguous_input(question, script):
    assert answer_script(question) == script


def test_machine_literals_and_citation_aliases_are_preserved():
    text = ("设备维护 [S1] E17-42 SEAL-9X\n"
            "`设备ID` https://example.test/设备?q=检查\n"
            "```json\n{\"设备\": \"检查\"}\n```\n"
            "~~~text\n设备 ID\n~~~\n检查液位。")
    result = format_answer("設備維護怎麼做", text)
    assert result.startswith("設備維護 [S1] E17-42 SEAL-9X")
    for literal in ("`设备ID`", "https://example.test/设备?q=检查", '```json\n{"设备": "检查"}\n```', "~~~text\n设备 ID\n~~~"):
        assert literal in result
    assert result.endswith("檢查液位。")
    assert format_answer("设备檢查", text) == text
    assert format_answer("设备怎么处理", "請檢查液位 [S1]。") == "请检查液位 [S1]。"


@pytest.mark.parametrize("question,raw,expected", [
    ("泵組壓力不足 P03-11 怎麼處理", "请查看资料 [S1]。", "請查看資料 [S1]。"),
    ("泵组压力不足 P03-11 怎么处理", "請查看資料 [S1]。", "请查看资料 [S1]。"),
])
def test_graph_formats_answer_but_preserves_source_text_and_message_identity(db, enc, users, monkeypatch, question, raw, expected):
    monkeypatch.setattr(settings, "gate_threshold", 0.1)
    monkeypatch.setattr(settings, "gate_sparse_threshold", 1e9)
    message_id = uuid.uuid4().hex

    def script(messages):
        return AIMessage(raw, id=message_id) if isinstance(messages[-1], ToolMessage) else call("search_knowledge", {"query": "P03-11 处理"})

    graph = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=script), InMemorySaver()))
    out = graph.invoke({"messages": [HumanMessage(question)], "evidence": {}, "sources": [], "error_code": None},
                       {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": users["tw-ops"].id}})
    assert out["messages"][-1].content == expected
    assert out["messages"][-1].id == message_id and out["error_code"] is None
    for card in out["sources"]:
        original = db.scalar(select(Chunk).where(Chunk.id == card["chunk_id"]))
        assert card["text"] == original.text
        assert card == out["evidence"][card["alias"]]


def test_refusal_keeps_error_and_no_sources_while_matching_traditional_question(db, enc, users, monkeypatch):
    monkeypatch.setattr(settings, "gate_threshold", 0.1)
    monkeypatch.setattr(settings, "gate_sparse_threshold", 1e9)

    def script(messages):
        return AIMessage("【无依据】资料没有记录 [S1]。") if isinstance(messages[-1], ToolMessage) else call("search_knowledge", {"query": "P03-11 处理"})

    graph = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=script), InMemorySaver()))
    out = graph.invoke({"messages": [HumanMessage("泵組資料有記錄嗎")], "evidence": {}, "sources": [], "error_code": None},
                       {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": users["tw-ops"].id}})
    assert out["messages"][-1].content == "資料沒有記錄 。"
    assert out["sources"] == [] and out["error_code"] == "INSUFFICIENT_EVIDENCE"
    assert out["model_declared_refusal"] is True


def test_same_conversation_uses_latest_question_script():
    graph = build_graph(AgentDeps(None, None, None, ScriptedModel(script=lambda messages: AIMessage("请查看资料。")), InMemorySaver()))
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": "unused"}}
    first = graph.invoke({"messages": [HumanMessage("處理方式")], "evidence": {}, "sources": [], "error_code": None}, cfg)
    second = graph.invoke({"messages": [HumanMessage("处理方式")], "evidence": {}, "sources": [], "error_code": None}, cfg)
    assert first["messages"][-1].content == "請查看資料。"
    assert second["messages"][-1].content == "请查看资料。"
