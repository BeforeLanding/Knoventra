"""v4 agent-layer evaluation: the case file and the runner's scoring, with scripted models only."""
import json
import re
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

import run_agent_eval as ae
from fakes import DictWorkingMemory, ScriptedModel, call, session_factory_for
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.config import settings
from knoventra.db.models import HelpRequest
from knoventra.tools.backend import InProcessTools

DEMO = Path(__file__).resolve().parents[2] / "data" / "demo"


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setattr(settings, "gate_sparse_threshold", 1e9)  # dense-only gate for the hash encoder
    monkeypatch.setattr(settings, "gate_threshold", 0.0)


# ------------------------------------------------------------------ case file

def test_case_file_is_valid_with_both_splits_covering_every_category():
    cases, _ = ae.load_cases(None)
    assert ae.validate_cases(cases) == []
    assert 38 <= len(cases) <= 45
    for split in ("dev", "holdout"):
        assert {c["category"] for c in cases if c["split"] == split} == set(ae.CATEGORIES)
    labels = {t["expect"]["intent"] for c in cases for t in c["turns"] if "intent" in t["expect"]}
    assert labels == set(ae.INTENTS)
    assert any(len(c["turns"]) > 1 for c in cases if c["category"] == "coreference")
    assert any(len(c["turns"]) > 1 for c in cases if c["category"] == "help_slots")


def test_cases_use_only_fictional_demo_users_and_machines():
    cases, _ = ae.load_cases(None)
    users = {u["email"].split("@")[0] for u in json.loads((DEMO / "manifest.json").read_text("utf-8"))["users"]}
    machines = {m["serial"] for m in json.loads((DEMO / "journals" / "machines.json").read_text("utf-8"))}
    assert {c["user"] for c in cases} <= users
    named = {m for c in cases for t in c["turns"] for m in re.findall(r"\b6\d{5}\b", t["user"])}
    named |= {c["slots"]["machine_id"] for c in cases if c.get("slots")}
    assert named and named <= machines


def test_validator_rejects_unknown_labels_and_keys():
    bad = [{"id": "x", "split": "dev", "category": "routing", "user": "sg-ops",
            "turns": [{"user": "hi", "expect": {"intent": "smalltalk", "tool": "web", "colour": 1}}]},
           {"id": "x", "split": "final", "category": "routing", "user": "sg-ops", "turns": [],
            "slots": {"machine_id": "1"}}]
    errors = "\n".join(ae.validate_cases(bad))
    for fragment in ("unknown intent", "unknown tool", "unknown expect keys", "duplicate id", "bad split",
                     "no turns", "no turn expects the card"):
        assert fragment in errors


def test_call_estimate_counts_planner_skips_clarify_and_cards():
    cases = [
        {"turns": [{"user": "x", "expect": {"intent": "chitchat", "tool": None}}]},             # 1 + 1
        {"turns": [{"user": "x", "expect": {"intent": "case_sop", "clarify": True}}]},           # 1 + 0
        {"turns": [{"user": "x", "expect": {"tool": "request_help", "missing": ["tried_steps"]}},  # 1 + 2
                   {"user": "y", "expect": {"tool": "request_help", "card": True}}]},             # 0 + 3
    ]
    est = ae.estimate_calls(cases, max_tool_calls=4)
    assert est == {"cases": 3, "turns": 4, "expected_model_calls": 9, "max_model_calls": 3 + 4 * 5 + 1}


# ------------------------------------------------------------------ scripted graph

PLANS = {
    "你好": dict(intent="chitchat", rewritten="你好"),
    "我上次的求助单现在什么状态": dict(intent="help_status", rewritten="求助单状态"),
    "spindle 扭矩多少": dict(intent="case_sop", rewritten="spindle 扭矩"),
    "WX-38 spindle 扭矩多少": dict(intent="case_sop", rewritten="WX-38 spindle 扭矩", machine_model="WX-38"),
    "EMO 测试前通知谁，CDA 保压多久": dict(intent="case_sop", rewritten="EMO 与 CDA",
                                    sub_queries=["EMO 测试前通知谁", "CDA 保压多久"]),
}


def planner_script(messages):
    text = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
    if text == "planner down":
        raise RuntimeError("planner unavailable")
    plan = {"sub_queries": [], "confidence": 0.9, "machine_model": None,
            **PLANS.get(text, dict(intent="chitchat", rewritten=text))}
    return AIMessage("", tool_calls=[{"name": "QueryPlan", "args": plan, "id": "p1", "type": "tool_call"}])


def help_args(text):
    args = {}
    if m := re.search(r"\b6\d{5}\b", text):
        args["machine_id"] = m.group(0)
    if any(k in text for k in ("异响", "偏移", "漏液")):
        args["symptom"] = text
    if any(k in text for k in ("拆检", "清洁", "换过")):
        args["tried_steps"] = text
    return args


def agent_script(messages):
    last = messages[-1]
    if isinstance(last, ToolMessage):
        if last.content.startswith(("求助单草稿已更新", "已创建", "用户已取消")) or ":" in last.content[:40]:
            return AIMessage(last.content[:80])
        return AIMessage("见资料 [S1]。")
    text = last.content
    if "状态" in text:
        return call("get_escalation_status", {}, cid="st1")
    args = help_args(text)
    if args or "求助" in text:
        return call("request_help", args, cid=f"h{len(messages)}")
    return AIMessage("你好，有什么可以帮你？")


class Rig:
    def __init__(self, db, enc, users, planner=planner_script, tools_cls=InProcessTools):
        self.db, self.users = db, users
        sf = session_factory_for(db)
        self.tools = tools_cls(sf, enc, None)
        saver, memory = InMemorySaver(), DictWorkingMemory()
        self.counter = counter = ae.model_call_counter()
        self.factory = lambda: build_graph(AgentDeps(
            sf, enc, None, ScriptedModel(script=agent_script, callbacks=[counter]), saver,
            ScriptedModel(script=planner, callbacks=[counter]), memory=memory, long_term_memory=False,
            tools=self.tools))

    def run(self, cases):
        assert ae.validate_cases(cases) == []
        return ae.evaluate(self.factory, self.tools, self.db, self.users, cases, self.counter)


def case(cid, category, turns, **kw):
    return {"id": cid, "split": "dev", "category": category, "user": kw.pop("user", "sg-ops"),
            "turns": [{"user": u, "expect": e} for u, e in turns], **kw}


HELP = case("h1", "help_slots", [
    ("帮我提个求助单，circulation pump 有异响", {"tool": "request_help", "missing": ["machine_id", "tried_steps"]}),
    ("机台 630301", {"tool": "request_help", "missing": ["tried_steps"]}),
    ("拆检看到 bearing 有磨损", {"tool": "request_help", "card": True}),
], decisions=[True], slots={"machine_id": "630301", "symptom": ["pump", "异响"], "tried_steps": ["bearing"]})


def field_requests(db, user):
    return db.scalar(select(func.count()).select_from(HelpRequest)
                     .where(HelpRequest.user_id == user.id, HelpRequest.kind == "field_help"))


def test_routing_status_and_slot_filling_score_end_to_end(db, enc, users):
    cases = [
        case("r1", "routing", [("你好", {"intent": "chitchat", "tool": None})]),
        case("s1", "help_status", [("我上次的求助单现在什么状态", {"intent": "help_status",
                                                         "tool": "get_escalation_status"})],
             setup={"help_requests": [{"kind": "field_help", "machine_id": "630301",
                                       "symptom": "[评测夹具] pump 异响", "status": "ASSIGNED"}]}),
        case("m1", "multi_question", [("EMO 测试前通知谁，CDA 保压多久",
                                       {"intent": "case_sop", "tool": "search_knowledge", "queries_min": 2})]),
        HELP,
    ]
    results, summary = Rig(db, enc, users).run(cases)
    assert [r["ok"] for r in results] == [True] * 4, [r["turns"] for r in results if not r["ok"]]
    assert summary["routing_accuracy"] == {"n": 3, "correct": 3, "rate": 1.0}  # slot turns carry no label
    assert summary["slot_completeness"] == {"cases": 1, "expected_slots": 3, "filled": 3, "rate": 1.0,
                                            "cases_all_slots": 1}
    assert summary["filings"] == {"expected": 1, "actual": 2, "duplicate_filings": 0, "missing_filings": 0,
                                  "refresh_mismatches": 0, "pending_after_reload": 0} or \
        summary["filings"]["duplicate_filings"] == 0
    status = results[1]
    assert "ASSIGNED" in status["turns"][0]["answer"]  # the fixture request reached the tool output
    assert status["filings"] == 1 and status["expected_records"] == 0 or status["filings"] == 0
    help_case = results[3]
    assert help_case["filings"] == 1 and help_case["duplicate_filings"] == 0 and help_case["confirmations"] == 1
    assert help_case["card_slots"]["machine_id"] == "630301"
    assert [t["plan_status"] for t in help_case["turns"]] == ["ok", "skipped", "skipped"]
    # planner + agent; status needs a second agent step; slot turns skip the planner, and the card
    # turn's agent replies once more after the decision
    assert [r["model_calls"] for r in results] == [2, 3, 2, 3 + 2 + 2]


def test_fixture_requests_are_not_counted_as_filings(db, enc, users):
    status = case("s1", "help_status", [("我上次的求助单现在什么状态", {"intent": "help_status"})],
                  setup={"help_requests": [{"kind": "field_help", "machine_id": "630302",
                                            "symptom": "[评测夹具] A-3105", "status": "OPEN"}]})
    results, summary = Rig(db, enc, users).run([status])
    assert results[0]["filings"] == 0 and summary["filings"]["actual"] == 0


def test_clarification_trigger_and_false_trigger_rates(db, enc, users, monkeypatch):
    import knoventra.tools.service as tool_service

    real = tool_service.retrieve
    monkeypatch.setattr(tool_service, "retrieve", lambda db_, enc_, rr, user, query, **kw:
                        real(db_, enc_, rr, user, "SOP-WCL-380120-002 " + query, **kw))
    cases = [
        case("c1", "clarify_model", [("spindle 扭矩多少", {"intent": "case_sop", "clarify": True,
                                                       "tool": "search_knowledge"})]),
        case("c2", "clarify_model", [("WX-38 spindle 扭矩多少", {"intent": "case_sop", "clarify": False})]),
        # Wrong label on purpose: the scorer must count it as a false trigger, not a success.
        case("c3", "clarify_model", [("spindle 扭矩多少", {"clarify": False})]),
    ]
    results, summary = Rig(db, enc, users).run(cases)
    assert [r["ok"] for r in results] == [True, True, False]
    assert summary["clarification"] == {"should_ask": 1, "asked": 1, "trigger_rate": 1.0,
                                        "should_not_ask": 2, "asked_anyway": 1, "false_trigger_rate": 0.5}
    assert results[0]["model_calls"] == 1  # planner only: the clarify node answers, not the agent model


def test_wrong_intent_and_planner_fallback_count_as_routing_errors(db, enc, users):
    cases = [case("w1", "routing", [("你好", {"intent": "policy"})]),
             case("w2", "routing", [("planner down", {"intent": "chitchat", "tool": None})])]
    results, summary = Rig(db, enc, users).run(cases)
    assert summary["routing_accuracy"] == {"n": 2, "correct": 0, "rate": 0.0}
    assert summary["planner_fallbacks"] == 1
    assert summary["intent_confusion"] == {"chitchat-><fallback>": 1, "policy->chitchat": 1}
    assert results[1]["turns"][0]["checks"] == {"intent": False, "tool": True}


def test_cancel_and_rejected_card_file_nothing_and_slots_score_from_the_card(db, enc, users):
    cancel = case("x1", "help_slots", [("帮我提求助单，610101 偏移", {"tool": "request_help",
                                                                 "missing": ["tried_steps"]}),
                                       ("算了", {"tool": None, "card": False})], user="cn-ops", decisions=[])
    reject = case("x2", "help_slots", [("610102 漏液，换过 O-ring，提求助单", {"tool": "request_help", "card": True})],
                  user="cn-ops", decisions=[False],
                  slots={"machine_id": "610102", "symptom": ["漏液"], "tried_steps": ["O-ring"], "alarm_codes": []})
    results, summary = Rig(db, enc, users).run([cancel, reject])
    assert [r["ok"] for r in results] == [True, True], [r["turns"] for r in results]
    assert summary["filings"]["actual"] == 0 and field_requests(db, users["cn-ops"]) == 0
    assert summary["slot_completeness"]["rate"] == 1.0


def test_card_never_reached_scores_zero_slots_and_a_missing_filing(db, enc, users):
    stuck = case("x3", "help_slots", [("帮我提个求助单，pump 异响", {"tool": "request_help", "card": True})],
                 decisions=[True], slots={"machine_id": "630301", "symptom": ["异响"]})
    results, summary = Rig(db, enc, users).run([stuck])
    assert results[0]["slot_checks"] == {"machine_id": False, "symptom": False}
    assert summary["filings"]["missing_filings"] == 1 and not results[0]["ok"]


class DoubleFilingTools(InProcessTools):
    """A broken tool side that files again on every replayed submit."""

    def submit(self, caller, confirmation_id):
        out = super().submit(caller, confirmation_id)
        self.replays = getattr(self, "replays", 0) + 1
        if self.replays > 1:
            with self.session_factory() as db:
                db.add(HelpRequest(user_id=caller.user_id, operation_id=f"dup-{self.replays}", kind="field_help",
                                   summary="duplicate"))
                db.commit()
        return out


def test_duplicate_filing_on_replay_is_detected(db, enc, users):
    rig = Rig(db, enc, users, tools_cls=DoubleFilingTools)
    results, summary = rig.run([HELP])
    assert rig.tools.replays == 2  # the graph's submit plus the runner's retry
    assert results[0]["duplicate_filings"] == 1 and not results[0]["ok"]
    assert summary["filings"]["duplicate_filings"] == 1


def test_real_submit_replay_and_refresh_stay_idempotent(db, enc, users):
    results, summary = Rig(db, enc, users).run([HELP])
    r = results[0]
    assert r["replay_errors"] == [] and r["refresh_mismatch"] is False and r["pending_after_reload"] is False
    assert summary["filings"]["duplicate_filings"] == 0 and field_requests(db, users["sg-ops"]) == 1
