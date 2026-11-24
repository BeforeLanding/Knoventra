"""Query planning: deterministic guards, fallbacks, routing and graph wiring (scripted models only)."""
import time
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from fakes import ScriptedModel, session_factory_for
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.agent.query_plan import QueryPlan, fix_spelling, plan_query, postprocess
from knoventra.config import settings
from knoventra.db.models import Chunk, KnowledgeBase


def plan_call(**args):
    args = {"sub_queries": [], "confidence": 0.9, "machine_model": None, **args}
    return AIMessage("", tool_calls=[{"name": "QueryPlan", "args": args, "id": "p1", "type": "tool_call"}])


def planner(*responses):
    it = iter(responses)

    def script(messages):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r
    return ScriptedModel(script=script)


def human(*texts):
    return [HumanMessage(t) for t in texts]


# ------------------------------------------------------------------ deterministic guards

def test_spelling_uses_the_es_typo_table():
    assert fix_spelling("mix tank trouble shotting, chamical flow") == "mix tank trouble shooting, chemical flow"
    assert fix_spelling("Shotting") == "shooting" and fix_spelling("shottings") == "shottings"


def test_rewrite_keeps_codes_and_never_invents_a_model():
    plan = QueryPlan(rewritten="mix tank 液位报警怎么复位", sub_queries=[], intent="case_sop", confidence=0.9,
                     machine_model="WX-P")
    r = postprocess(plan, human("A-1301 报警后怎么复位"))
    assert r.queries == ["mix tank 液位报警怎么复位 A-1301"] and "restored_identifiers:A-1301" in r.notes
    assert r.machine_model is None and "dropped_unstated_model:WX-P" in r.notes


def test_model_comes_from_the_conversation_and_routes_by_intent():
    plan = QueryPlan(rewritten="spindle 扭矩", intent="case_sop", confidence=0.9)
    r = postprocess(plan, human("我们是单面机", "spindle 扭矩多少"))
    assert r.machine_model == "WX-38" and r.route_kbs == settings.intent_kbs["case_sop"]


def test_low_confidence_searches_every_readable_kb():
    plan = QueryPlan(rewritten="报销", intent="policy", confidence=0.3)
    r = postprocess(plan, human("报销"))
    assert r.route_kbs is None and "low_confidence_all_readable" in r.notes


def test_split_is_capped_and_typos_fixed_per_sub_query():
    plan = QueryPlan(rewritten="x", intent="case_sop", confidence=0.9, sub_queries=["chamical flow", "EMO 测试"])
    assert postprocess(plan, human("两个问题")).queries == ["chemical flow", "EMO 测试"]


# ------------------------------------------------------------------ failures fall back

@pytest.mark.parametrize("response, reason", [
    (RuntimeError("provider down"), "RuntimeError"),
    (AIMessage("I think the intent is case_sop"), "no_plan"),
    (plan_call(rewritten="x", intent="unknown_intent"), "ValidationError"),
])
def test_planner_failures_return_none_with_a_reason(response, reason):
    plan, trace = plan_query(planner(response), human("EMO 测试"))
    assert plan is None and trace["status"] == "fallback" and trace["reason"] in (reason, "OutputParserException")


def test_planner_timeout_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "query_plan_timeout_s", 0.05)

    def slow(messages):
        time.sleep(0.5)
        return plan_call(rewritten="x", intent="case_sop")
    plan, trace = plan_query(ScriptedModel(script=slow), human("EMO 测试"))
    assert plan is None and trace["reason"] == "timeout"


# ------------------------------------------------------------------ graph

@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setattr(settings, "gate_sparse_threshold", 1e9)
    monkeypatch.setattr(settings, "gate_threshold", 0.0)


def answer_from_tools(messages):
    last = messages[-1]
    if isinstance(last, ToolMessage):
        return AIMessage("见资料 [S1]。")
    return AIMessage("你好，有什么可以帮你？")


def run(db, enc, user, text, plan_response, agent_script=answer_from_tools):
    deps = AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=agent_script), InMemorySaver(),
                     planner(plan_response))
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": user.id}}
    return build_graph(deps).invoke({"messages": [HumanMessage(text)], "evidence": {}, "sources": [],
                                     "error_code": None}, cfg)


def _kb_names(db, chunk_ids):
    rows = db.execute(select(KnowledgeBase.name).join(Chunk, Chunk.kb_id == KnowledgeBase.id)
                      .where(Chunk.id.in_(chunk_ids))).scalars()
    return set(rows)


def test_retrieval_intent_searches_planned_queries_within_the_route(db, enc, users):
    out = run(db, enc, users["sg-ops"], "EMO 测试前通知谁，CDA 保压多久",
              plan_call(rewritten="EMO 测试与 CDA 保压", intent="case_sop",
                        sub_queries=["EMO 测试前通知谁", "CDA 保压多久"]))
    assert out["query_plan"]["status"] == "ok" and out["query_plan"]["queries"] == ["EMO 测试前通知谁", "CDA 保压多久"]
    trace = out["retrieval_trace"]
    assert [t["tool_call_id"] for t in trace] == ["plan-1", "plan-2"]
    assert all(t["route"] == {"requested": settings.intent_kbs["case_sop"], "applied": True} for t in trace)
    returned = [cid for t in trace for cid in t["returned_chunk_ids"]]
    assert returned and _kb_names(db, returned) <= set(settings.intent_kbs["case_sop"])
    assert out["sources"] and out["error_code"] is None


def test_routing_never_widens_scope(db, enc, users):
    # HR cannot read any case_sop KB: the route is not applied and HR's own scope stays in force.
    out = run(db, enc, users["hr"], "spindle 更换扭矩", plan_call(rewritten="spindle 更换扭矩", intent="case_sop"))
    trace = out["retrieval_trace"][0]
    assert trace["route"]["applied"] is False
    assert _kb_names(db, trace["returned_chunk_ids"]) <= {"hr", "it-public"}


@pytest.mark.parametrize("response", [RuntimeError("down"), plan_call(rewritten="你好", intent="chitchat")])
def test_fallback_and_non_retrieval_intents_go_to_the_agent(db, enc, users, response):
    seen = []

    def agent(messages):
        seen.append(messages[-1])
        return AIMessage("你好，有什么可以帮你？")
    out = run(db, enc, users["sg-ops"], "你好", response, agent)
    assert isinstance(seen[0], HumanMessage) and out["retrieval_trace"] == []
    assert out["query_plan"]["status"] == ("fallback" if isinstance(response, Exception) else "ok")


def test_explicit_escalation_skips_planning(db, enc, users):
    out = run(db, enc, users["sg-ops"], "转人工", RuntimeError("planner must not be called"),
              lambda m: AIMessage("ok"))
    assert out["query_plan"] == {"status": "skipped", "reason": "explicit_escalation"}


def test_a_comparison_gets_one_query_per_model():
    plan = QueryPlan(rewritten="WX-38 和 WX-P 的 spindle 扭矩有什么区别", sub_queries=[], intent="case_sop",
                     confidence=0.9, machine_model=None)
    r = postprocess(plan, human("WX-38 和 WX-P 的 spindle 扭矩有什么区别"))
    assert [q.split()[0] for q in r.queries] == ["WX-38", "WX-P"] and "split_by_model" in r.notes
    assert all("spindle 扭矩" in q and q.count("WX") == 1 for q in r.queries)


def test_planner_sub_queries_already_per_model_are_kept():
    plan = QueryPlan(rewritten="对比扭矩", sub_queries=["WX-38 spindle 扭矩", "WX-P spindle 扭矩"], intent="case_sop",
                     confidence=0.9, machine_model=None)
    r = postprocess(plan, human("单面机和双面机 spindle 扭矩差多少"))
    assert r.queries == ["WX-38 spindle 扭矩", "WX-P spindle 扭矩"] and "split_by_model" not in r.notes


def test_one_model_is_not_a_comparison():
    plan = QueryPlan(rewritten="WX-38 spindle 扭矩", sub_queries=[], intent="case_sop", confidence=0.9,
                     machine_model="WX-38")
    assert postprocess(plan, human("WX-38 spindle 扭矩多少")).queries == ["WX-38 spindle 扭矩"]


def test_a_split_never_appends_the_other_model_as_a_restored_code():
    plan = QueryPlan(rewritten="spindle 扭矩", sub_queries=[], intent="case_sop", confidence=0.9, machine_model=None)
    r = postprocess(plan, human("WX-38 和 WX-P 的 spindle 扭矩有什么区别，E17-42 也有关系吗"))
    assert r.queries == ["WX-38 spindle 扭矩 E17-42", "WX-P spindle 扭矩 E17-42"]
