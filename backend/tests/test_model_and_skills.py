"""Machine-model clarification and the skill lookup tool (scripted models only)."""
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from fakes import ScriptedModel, session_factory_for
from knoventra.access.scope import compute_scope
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.agent.model_variants import chunk_models, sectioned_documents, stated_models
from knoventra.config import settings
from knoventra.db.models import Chunk, Document, Revision
from knoventra.ingestion.journal import import_workbooks
from knoventra.retrieval.search import search
from knoventra.skills import lookup_skills

JOURNALS = Path(__file__).resolve().parents[2] / "data" / "demo" / "journals"


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setattr(settings, "gate_sparse_threshold", 1e9)
    monkeypatch.setattr(settings, "gate_threshold", 0.0)


def _doc(db, prefix):
    return db.scalar(select(Document).where(Document.title.like(f"{prefix}%")))


# ------------------------------------------------------------------ model detection

def test_latest_message_naming_a_model_wins():
    msgs = [HumanMessage("我们是单面机"), AIMessage("好的"), HumanMessage("其实是 WX-P"), HumanMessage("扭矩多少")]
    assert stated_models(msgs) == ["WX-P"]
    assert stated_models([HumanMessage("WX-38 和 WX-P 的区别")]) == ["WX-38", "WX-P"]
    assert stated_models([HumanMessage("扭矩多少")]) == []


def test_only_documents_with_model_sections_count(db):
    spindle, door = _doc(db, "SOP-WCL-380120-002"), _doc(db, "SOP-GEN-200310-001")
    mixtank, emo = _doc(db, "SOP-WCL-380110-001"), _doc(db, "SOP-SAF-900110-001")
    found = sectioned_documents(db, {spindle.id, door.id, mixtank.id, emo.id})
    assert found == {spindle.id: ["WX-38", "WX-P"], door.id: ["ST-250", "WX-38", "WX-P"]}


def test_stated_model_filters_out_other_model_sections(db, enc, users):
    res = search(db, enc, compute_scope(db, users["sg-ops"]), "spindle 更换 扭矩", top_k=20, keyword=None,
                 machine_model="WX-P")
    assert res.stages["model_filter"] and res.evidence
    assert all(not (m := chunk_models(e.section_path, e.text)) or "WX-P" in m for e in res.evidence)


# ------------------------------------------------------------------ graph

def plan(intent="case_sop", **extra):
    args = {"rewritten": "spindle 扭矩", "sub_queries": [], "intent": intent, "confidence": 0.9,
            "machine_model": None, **extra}
    return AIMessage("", tool_calls=[{"name": "QueryPlan", "args": args, "id": "p", "type": "tool_call"}])


def graph(db, enc, plans, agent=None):
    it = iter(plans)
    agent = agent or (lambda m: AIMessage("见资料 [S1]。") if isinstance(m[-1], ToolMessage) else AIMessage("好的"))
    deps = AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=agent), InMemorySaver(),
                     ScriptedModel(script=lambda m: next(it)))
    return build_graph(deps)


def ask(g, user, text, thread):
    cfg = {"configurable": {"thread_id": thread, "user_id": user.id}}
    return g.invoke({"messages": [HumanMessage(text)], "evidence": {}, "sources": [], "error_code": None}, cfg)


def test_unstated_model_gets_a_question_then_the_answer_is_filtered(db, enc, users, monkeypatch):
    # Force the spindle SOP into the evidence so the test does not depend on hash-encoder ranking.
    import knoventra.agent.graph as graph_module

    spindle = _doc(db, "SOP-WCL-380120-002")
    real = graph_module.retrieve

    def retrieve(db_, enc_, rr, user, query, **kw):
        out = real(db_, enc_, rr, user, "SOP-WCL-380120-002 " + query, **kw)
        assert all(e.document_id == spindle.id for e in out.evidence)
        return out
    monkeypatch.setattr(graph_module, "retrieve", retrieve)

    calls = []

    def agent(messages):
        calls.append(messages[-1])
        return AIMessage("扭矩见资料 [S1]。")
    g, thread = graph(db, enc, [plan(), plan(rewritten="WX-P spindle 扭矩", machine_model="WX-P")], agent), "t1"
    first = ask(g, users["sg-ops"], "spindle 扭矩多少", thread)
    text = first["messages"][-1].content
    assert "哪个机型" in text and "WX-38" in text and "WX-P" in text
    assert first["sources"] == [] and first["clarification"]["models"] == ["WX-38", "WX-P"]
    assert calls == []  # the answering model is never asked to answer across models

    second = ask(g, users["sg-ops"], "WX-P", thread)
    assert second["clarification"] is None and second["retrieval_trace"][0]["machine_model"] == "WX-P"
    texts = [db.get(Chunk, cid) for cid in second["retrieval_trace"][0]["returned_chunk_ids"]]
    assert texts and all("12 N·m" not in c.text for c in texts)
    assert second["sources"]


def test_naming_two_models_is_a_comparison_not_a_question(db, enc, users, monkeypatch):
    import knoventra.agent.graph as graph_module

    real = graph_module.retrieve
    monkeypatch.setattr(graph_module, "retrieve",
                        lambda db_, enc_, rr, user, query, **kw: real(db_, enc_, rr, user, "SOP-WCL-380120-002 " + query,
                                                                       **kw))
    out = ask(graph(db, enc, [plan()]), users["sg-ops"], "WX-38 和 WX-P 的 spindle 扭矩有什么区别", "t2")
    assert out["clarification"] is None and out["retrieval_trace"][0]["machine_model"] is None


# ------------------------------------------------------------------ skills

@pytest.fixture
def skills(db):
    import_workbooks(db, sorted(JOURNALS.glob("*.xlsx")), JOURNALS / "machines.json")


def test_skill_lookup_matches_tokens_and_synonyms(db, skills):
    golden = lookup_skills(db, "谁会做 golden check")
    assert golden and {h.skill_item for h in golden} == {"golden check"}
    assert [h.level for h in golden] == sorted((h.level for h in golden), key={"proficient": 0, "familiar": 1}.get)
    assert {h.skill_item for h in lookup_skills(db, "谁会 chamber 示教")} == {"chamber teaching"}
    assert {h.skill_item for h in lookup_skills(db, "chamical flow 谁熟")} == {"chemical flow 调试"}
    assert lookup_skills(db, "报销流程") == []


@pytest.mark.parametrize("account, expect", [("sg-ops", "golden check："), ("hr", "NO_ACCESS")])
def test_skill_intent_calls_the_tool_and_checks_department(db, enc, users, skills, account, expect):
    seen = []

    def agent(messages):
        seen.append(messages[-1])
        return AIMessage("已查询技能表。")
    out = ask(graph(db, enc, [plan("skill_lookup", rewritten="谁会 golden check", skill_item="golden check")], agent),
              users[account], "谁会做 golden check", f"t-{account}")
    tool = seen[0]
    assert isinstance(tool, ToolMessage) and tool.tool_call_id == "plan-skill" and expect in tool.content
    assert out["query_plan"]["skill_item"] == "golden check" and out["error_code"] is None


def test_low_ranked_sectioned_chunk_does_not_trigger_a_question(db, enc, users, monkeypatch):
    import knoventra.agent.graph as graph_module
    from knoventra.retrieval.pipeline import OK, Outcome
    from knoventra.retrieval.search import Evidence

    def ev(prefix, section_like):
        doc = _doc(db, prefix)
        c = db.scalar(select(Chunk).join(Revision, Revision.id == Chunk.revision_id)
                      .where(Revision.document_id == doc.id, Revision.status == "PUBLISHED",
                             Chunk.section_path.like(f"%{section_like}%")))
        return Evidence(c.id, doc.id, doc.title, 1, None, c.section_path, c.text, "zh-Hans", 0.1, dense_sim=0.9)
    top = ev("SOP-GEN-200410-001", "示教")
    filler = ev("SOP-WCL-380120-002", "WX-38")
    monkeypatch.setattr(graph_module, "retrieve", lambda *a, **kw: Outcome(OK, [top, filler]))
    out = ask(graph(db, enc, [plan(rewritten="chamber teaching 速度")]), users["sg-ops"], "chamber teaching 速度", "t9")
    assert out["clarification"] is None and out["sources"]


def test_planner_route_does_not_narrow_the_model_follow_up(db, enc, users):
    calls = []

    def agent(messages):
        last = messages[-1]
        calls.append(last)
        if isinstance(last, ToolMessage) and last.tool_call_id == "plan-1":
            return AIMessage("", tool_calls=[{"name": "search_knowledge", "args": {"query": "VPN 报修"},
                                              "id": "c1", "type": "tool_call"}])
        return AIMessage("见资料 [S1]。")
    out = ask(graph(db, enc, [plan("policy", rewritten="VPN 报修")], agent), users["sg-ops"], "VPN 连不上去哪报修", "t10")
    routes = {t["tool_call_id"]: t["route"] for t in out["retrieval_trace"]}
    assert routes["plan-1"]["requested"] == settings.intent_kbs["policy"] and routes["c1"] is None
