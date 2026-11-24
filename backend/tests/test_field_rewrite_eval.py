"""Rewrite / planning on-off ablation runner: plan cache, three query modes, scoring (offline, no model)."""
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

import run_field_eval as rf
import run_field_rewrite_eval as rw
from fakes import ScriptedModel
from knoventra.config import settings
from knoventra.db.models import KnowledgeBase


@pytest.fixture(autouse=True)
def open_gate(monkeypatch):
    monkeypatch.setattr(settings, "gate_threshold", 0.0)
    monkeypatch.setattr(settings, "rrf_weighting", settings.rrf_weighting)  # run_mode switches it; restore afterwards


@pytest.fixture
def kb_names(db):
    return dict(db.execute(select(KnowledgeBase.id, KnowledgeBase.name)).all())


def plan_entry(rewritten, *, intent="case_sop", queries=None, route=None, model=None, status="ok", confidence=0.9):
    plan = {"rewritten": rewritten, "queries": queries or [rewritten], "intent": intent, "confidence": confidence,
            "machine_model": model, "route_kbs": route, "skill_item": None, "notes": []}
    return {"status": status, "reason": None, "latency_ms": 12.5, "plan": plan}


def fallback_entry():
    return {"status": "fallback", "reason": "timeout", "latency_ms": 8000.0, "plan": None}


ANSWERABLE = {"id": "t1", "split": "dev", "category": "identifier", "user": "sg-ops",
              "question": "zzqq xxww kkjj vvbb",
              "expect": {"title_prefix": "SOP-GEN-200420-001", "support_text": "小于 0.3 mm"}}
ACCESS = {"id": "t2", "split": "dev", "category": "access", "user": "hr", "question": "mix tank A-1301 怎么处理",
          "expect": {"forbidden_kbs": ["field-sop", "field-cases"]}}


# ------------------------------------------------------------------ plan cache

def _planner(intent="case_sop"):
    def script(messages):
        return AIMessage("", tool_calls=[{"name": "QueryPlan", "id": "p", "type": "tool_call", "args": {
            "rewritten": "SOP-GEN-200420-001 偏心要求是多少", "sub_queries": [], "intent": intent, "confidence": 0.9}}])
    return ScriptedModel(script=script)


def test_plan_cache_is_bound_to_cases_split_and_prompt_and_never_overwritten(tmp_path):
    cases, sha = rf.load_cases("dev")
    plans = rw.make_plans(_planner(), cases[:2])
    assert set(plans) == {c["id"] for c in cases[:2]} and plans[cases[0]["id"]]["status"] == "ok"
    cache = rw.build_cache(split="dev", cases=cases[:2], cases_sha=sha, model="m", thinking="disabled", plans=plans)
    rw.check_cache(cache, split="dev", cases=cases[:2], cases_sha=sha)
    with pytest.raises(ValueError, match="cases_sha256"):
        rw.check_cache({**cache, "cases_sha256": "0" * 64}, split="dev", cases=cases[:2], cases_sha=sha)
    with pytest.raises(ValueError, match="split"):
        rw.check_cache(cache, split="holdout", cases=cases[:2], cases_sha=sha)
    with pytest.raises(ValueError, match="cover exactly"):
        rw.check_cache(cache, split="dev", cases=cases[:3], cases_sha=sha)
    with pytest.raises(ValueError, match="prompt"):
        rw.check_cache({**cache, "planner_prompt_sha256": "x"}, split="dev", cases=cases[:2], cases_sha=sha)
    path = tmp_path / "plans.json"
    rw.write_new(path, cache)
    assert json.loads(path.read_text(encoding="utf-8"))["cases_sha256"] == sha
    with pytest.raises(FileExistsError):
        rw.write_new(path, cache)


def test_planner_failure_is_cached_as_a_fallback():
    def broken(messages):
        raise RuntimeError("provider down")
    plans = rw.make_plans(ScriptedModel(script=broken), [ANSWERABLE])
    assert plans["t1"]["status"] == "fallback" and plans["t1"]["plan"] is None and plans["t1"]["reason"]


def test_the_frozen_case_file_still_has_its_recorded_size():
    dev, _ = rf.load_cases("dev")
    holdout, _ = rf.load_cases("holdout")
    assert (len(dev), len(holdout)) == (24, 15)


# ------------------------------------------------------------------ modes

def test_off_mode_scores_exactly_like_the_original_runner(db, enc, users, kb_names):
    cases = [c for c in rf.load_cases("dev")[0] if c["category"] in ("identifier", "access")][:6]
    plans = {c["id"]: plan_entry("irrelevant") for c in cases}
    base = rf.run(db, enc, users, cases, "pg", kb_names)
    mine = rw.run_mode(db, enc, users, cases, plans, "pg", "off", kb_names)
    keep = ("id", "category", "hit", "rank", "source_hit", "source_rank", "leaked", "top")
    assert [{k: r[k] for k in keep if k in r} for r in mine] == [{k: r[k] for k in keep if k in r} for r in base]


def test_rewrite_only_uses_the_rewrite_and_falls_back_to_the_question(db, enc, users, kb_names, monkeypatch):
    seen = []
    import knoventra.retrieval.search as module
    real = module.search

    def spy(*a, **kw):
        seen.append({"query": a[3], **{k: kw.get(k) for k in ("original_query", "machine_model")}})
        return real(*a, **kw)
    monkeypatch.setattr(module, "search", spy)
    plans = {"t1": plan_entry("SOP-GEN-200420-001 偏心要求是多少", model="WX-38", route=["field-sop"])}
    off = rw.run_mode(db, enc, users, [ANSWERABLE], plans, "pg", "off", kb_names)[0]
    only = rw.run_mode(db, enc, users, [ANSWERABLE], plans, "pg", "rewrite_only", kb_names)[0]
    assert seen[0] == {"query": ANSWERABLE["question"], "original_query": "", "machine_model": None}
    assert seen[1] == {"query": "SOP-GEN-200420-001 偏心要求是多少", "original_query": ANSWERABLE["question"],
                       "machine_model": None}  # no route, no machine model in this mode
    assert only["source_hit"] and not off["source_hit"]
    assert "route" not in only
    seen.clear()
    fb = rw.run_mode(db, enc, users, [ANSWERABLE], {"t1": fallback_entry()}, "pg", "rewrite_only", kb_names)[0]
    assert seen[0]["query"] == ANSWERABLE["question"] and fb["planner_status"] == "fallback"


def test_full_plan_narrows_the_scope_filters_the_model_and_merges_sub_queries(db, enc, users, kb_names, monkeypatch):
    calls = []
    import knoventra.retrieval.search as module
    real = module.search

    def spy(db_, enc_, scope, query, **kw):
        calls.append({"query": query, "kbs": len(scope.readable_kb_ids), "model": kw.get("machine_model")})
        return real(db_, enc_, scope, query, **kw)
    monkeypatch.setattr(module, "search", spy)
    case = {**ANSWERABLE, "question": "偏心要求和 golden check"}
    plans = {"t1": plan_entry("偏心要求", queries=["SOP-GEN-200420-001 偏心要求是多少", "golden check 偏心"],
                              route=["field-sop"], model="WX-38")}
    r = rw.run_mode(db, enc, users, [case], plans, "pg", "full_plan", kb_names)[0]
    assert r["route"] == {"requested": ["field-sop"], "applied": True}
    assert [c["kbs"] for c in calls] == [1, 1] and [c["model"] for c in calls] == ["WX-38", "WX-38"]
    assert len(r["queries"]) == 2 and {k.split("|")[0] for k in r["top"]} == {"field-sop"}


def test_non_retrieval_intent_is_recorded_and_searches_the_full_readable_scope(db, enc, users, kb_names):
    plans = {"t1": plan_entry("提个求助单", intent="request_help", route=None)}
    r = rw.run_mode(db, enc, users, [ANSWERABLE], plans, "pg", "full_plan", kb_names)[0]
    assert r["non_retrieval_intent"] == "request_help" and "route" not in r


def test_routing_never_widens_access_and_leaks_nothing(db, enc, users, kb_names):
    # An HR user routed to the field KBs: the intersection is empty, so the scope is unchanged.
    plans = {"t2": plan_entry("mix tank A-1301 处理", route=["ops-global", "field-sop", "field-cases"])}
    for mode in rw.MODES:
        r = rw.run_mode(db, enc, users, [ACCESS], plans, "pg", mode, kb_names)[0]
        assert r["leaked"] == 0
    full = rw.run_mode(db, enc, users, [ACCESS], plans, "pg", "full_plan", kb_names)[0]
    assert full["route"]["applied"] is False


# ------------------------------------------------------------------ helpers and statistics

def test_round_robin_merge_interleaves_by_rank_and_drops_repeats():
    def ev(*ids):
        return [SimpleNamespace(chunk_id=i) for i in ids]
    merged = rw.merge_round_robin([ev("a", "b", "c"), ev("x", "a", "y")], 4)
    assert [e.chunk_id for e in merged] == ["a", "x", "b", "c"]


def test_distribution_uses_nearest_rank_and_keeps_empty_as_null():
    assert rw.distribution([]) == {"n": 0, "p50": None, "p95": None, "max": None}
    assert rw.distribution([4, 1, 3, 2]) == {"n": 4, "p50": 2, "p95": 4, "max": 4}


def test_changes_and_planner_summary():
    cases = [{"id": "a", "category": "identifier", "question": "q1"}, {"id": "b", "category": "access", "question": "q2"},
             {"id": "c", "category": "case", "question": "q3"}]
    off = [{"id": "a", "category": "identifier", "rank": 2, "source_rank": 2},
           {"id": "b", "category": "access", "leaked": 0},
           {"id": "c", "category": "case", "rank": 1, "source_rank": 1}]
    other = [{"id": "a", "category": "identifier", "rank": 1, "source_rank": 1, "queries": ["x"]},
             {"id": "b", "category": "access", "leaked": 0},
             {"id": "c", "category": "case", "rank": None, "source_rank": None, "non_retrieval_intent": "chitchat"}]
    changes = rw.diff_vs_off(cases, off, other)
    assert [(c["id"], c["rank"]) for c in changes] == [("a", [2, 1]), ("c", [1, None])]
    plans = {"a": plan_entry("x"), "b": fallback_entry(), "c": plan_entry("y", intent="chitchat")}
    summary = rw.planner_summary(cases, plans)
    assert summary["ok"] == 2 and summary["fallback"] == 1 and summary["fallback_reasons"] == ["timeout"]
    assert summary["intents"] == {"case_sop": 1, "chitchat": 1}
    assert summary["answerable_intents"] == {"case_sop": ["a"], "chitchat": ["c"]}
