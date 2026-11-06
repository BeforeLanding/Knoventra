import collections
import json
import sys
from pathlib import Path

import pytest

import run_eval

MANIFEST = Path(__file__).resolve().parents[2] / "data" / "demo" / "manifest.json"


def test_case_set_shape():
    cases = run_eval.load_cases("all")
    assert len(cases) == 60 and len({c["id"] for c in cases}) == 60
    assert collections.Counter(c["category"] for c in cases) == {"ops": 20, "cross": 10, "access": 15, "no_basis": 10, "escalation": 5}
    assert len(run_eval.load_cases("dev")) == 20 and len(run_eval.load_cases("holdout")) == 40
    titles = {d["title"] for d in json.loads(MANIFEST.read_text(encoding="utf-8"))["documents"]}
    for c in cases:
        e = c["expect"]
        for t in [e.get("title"), *e.get("forbidden_titles", [])]:
            assert t is None or t in titles, (c["id"], t)


@pytest.mark.parametrize("split", ["holdout", "all", "val3", "val4", "val5"])
def test_holdout_requires_confirmation(monkeypatch, split):
    monkeypatch.setattr(sys, "argv", ["run_eval", "--split", split])
    with pytest.raises(SystemExit):
        run_eval.main()


def test_identifier_regression_set_shape():
    cases = run_eval.load_cases("val4")
    assert len(cases) == 28 and len({c["id"] for c in cases}) == 28
    assert collections.Counter(c["category"] for c in cases) == {"ops": 10, "cross": 4, "no_basis": 10, "access": 4}
    assert all(c["split"] == "val4" for c in cases)


def test_expected_support_text_disambiguates_chunks_on_the_same_page():
    from types import SimpleNamespace

    exp = {"title": "冷卻系統維護手冊", "pages": [1], "revision": 1, "support_text": "清潔散熱片"}
    wrong = SimpleNamespace(document_title=exp["title"], page=1, revision=1, text="冷卻液每六個月更換一次。")
    right = SimpleNamespace(document_title=exp["title"], page=1, revision=1, text="檢查散熱風扇是否運轉，清潔散熱片。")
    assert not run_eval.matches(wrong, exp) and run_eval.matches(right, exp)


def test_rerank_comparison_labels_have_real_support_in_published_chunks(db):
    from sqlalchemy import select

    from knoventra.db.models import Chunk, Document, Revision

    cases = run_eval.load_cases("val5")
    assert len(cases) == 28 and len({c["id"] for c in cases}) == 28
    assert collections.Counter(c["category"] for c in cases) == {"ops": 14, "cross": 4, "no_basis": 6, "access": 4}
    rows = db.execute(select(Document.title, Revision.version, Chunk.page, Chunk.text)
                      .join(Revision, Revision.document_id == Document.id)
                      .join(Chunk, Chunk.revision_id == Revision.id).where(Revision.status == "PUBLISHED")).all()
    for c in cases:
        if c["category"] in ("ops", "cross"):
            exp = c["expect"]
            assert exp["support_text"] and any(t == exp["title"] and v == exp["revision"] and p in exp["pages"]
                                              and exp["support_text"] in body for t, v, p, body in rows), c["id"]


@pytest.mark.parametrize("args", [["--rerank-model", "model"], ["--mode", "agent", "--threshold", "0.5"],
                                  ["--mode", "agent", "--no-sparse"], ["--mode", "agent", "--no-opencc"]])
def test_unsupported_flags_fail_before_loading_models(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["run_eval", *args])
    with pytest.raises(SystemExit) as error:
        run_eval.main()
    assert error.value.code == 2


def test_retrieval_eval_runs_and_has_zero_leaks(db, enc, users):
    cases = run_eval.load_cases("all")
    res = run_eval.eval_retrieval(db, enc, None, users, cases, use_sparse=True, use_opencc=True, threshold=0.6)
    m = run_eval.summarize(res)
    assert m["access"]["leaks"] == 0 and m["access"]["n"] == 15
    assert m["ops"]["n"] == 20 and m["cross"]["n"] == 10 and m["no_basis"]["n"] == 10


def test_agent_eval_escalation_counts(db, enc, users):
    from fakes import ScriptedModel, call, session_factory_for
    from langchain_core.messages import AIMessage, ToolMessage

    from knoventra.agent.checkpoint import make_checkpointer
    from knoventra.agent.graph import AgentDeps, build_graph
    from knoventra.config import settings

    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage(msgs[-1].content)
        return call("escalate_to_expert", {"summary": "s"}, cid="e1")

    saver, pool = make_checkpointer(settings.database_url)
    try:
        g = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=script), saver))
        rs = run_eval.eval_agent(g, db, users, [c for c in run_eval.load_cases("all") if c["category"] == "escalation"])
        assert all(r["ok"] for r in rs), rs
    finally:
        pool.close()


@pytest.mark.parametrize("question,bucket,reason", [
    ("E17-42 复位后如何观察", "model_refusal_with_target_span", None),
    ("E17-44 复位后如何观察", "retrieval_blocked", "IDENTIFIER_MISMATCH"),
])
def test_refusal_triage_consumes_real_graph_snapshots(db, enc, users, monkeypatch, question, bucket, reason):
    from analyze_refusals import analyze
    from fakes import ScriptedModel, call, session_factory_for
    from langchain_core.messages import AIMessage, ToolMessage

    from knoventra.agent.checkpoint import make_checkpointer
    from knoventra.agent.graph import AgentDeps, build_graph
    from knoventra.config import settings

    monkeypatch.setattr(settings, "gate_threshold", 0.1)
    monkeypatch.setattr(settings, "gate_sparse_threshold", 1e9)

    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage("【无依据】没有复位后的观察信息。")
        return call("search_knowledge", {"query": "E17-42 排查"})

    case = {"id": "triage-new-01", "category": "ops", "user": "sg-ops", "question": question,
            "expect": {"title": "E17 系列设备故障手册", "pages": [2], "revision": 1,
                       "support_text": "复位设备并观察 10 分钟"}}
    saver, pool = make_checkpointer(settings.database_url)
    try:
        graph = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=script), saver))
        rows = run_eval.eval_agent(graph, db, users, [case])
        assert rows[0]["diagnostics"]["retrieval_trace"][0]["reason"] == reason
        triage = analyze({"mode": "agent", "cases": rows})
        assert triage["cases"][0]["bucket"] == bucket
        assert triage["refused_cases"] == 1
        assert triage["review_candidates_with_target_span"] == int(reason is None)
    finally:
        pool.close()


def test_agent_eval_captures_exact_turn_evidence_for_offline_review(db, enc, users, monkeypatch):
    from answer_review import prepare_packet, summarize_packet, validate_snapshot
    from fakes import ScriptedModel, call, session_factory_for
    from langchain_core.messages import AIMessage, ToolMessage

    from knoventra.agent.checkpoint import make_checkpointer
    from knoventra.agent.graph import AgentDeps, build_graph
    from knoventra.config import settings

    monkeypatch.setattr(settings, "gate_threshold", 0.1)
    monkeypatch.setattr(settings, "gate_sparse_threshold", 1e9)

    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage("请按手册核对排查步骤 [S1]。")
        return call("search_knowledge", {"query": "E17-42 排查"})

    case = {"id": "snapshot-new-01", "category": "ops", "user": "sg-ops", "question": "E17-42 如何排查",
            "expect": {"title": "E17 系列设备故障手册", "pages": [2], "revision": 1}}
    saver, pool = make_checkpointer(settings.database_url)
    try:
        g = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=script), saver))
        results = run_eval.eval_agent(g, db, users, [case])
        snapshot = results[0]["review_snapshot"]
        registry = validate_snapshot(snapshot)
        assert snapshot["question"] == case["question"] and snapshot["answer"] == results[0]["answer"]
        assert results[0]["thread_id"] and registry and snapshot["sources"][0] == registry["S1"]
        assert snapshot["sources"][0]["text"] and snapshot["sources"][0]["revision"] == 1
        report = {"mode": "agent", "cases": results}
        from analyze_refusals import analyze
        assert results[0]["expect"] == case["expect"]
        assert results[0]["diagnostics"]["retrieval_trace"][0]["status"] == "OK"
        assert analyze(report)["cases"][0]["bucket"] == "answer_with_sources"
        summary = summarize_packet(prepare_packet(report), report)
        assert summary["unreviewed_units"] > 0 and summary["by_reviewer_kind"]["human"]["fully_supported_rate"] is None
    finally:
        pool.close()
