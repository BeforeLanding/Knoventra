import copy
import json
import sys

import pytest

import analyze_refusals
from answer_review import make_snapshot


def case(*, error="INSUFFICIENT_EVIDENCE", model=True, status="OK", reason=None,
         text="复位后观察十分钟。", span="观察十分钟", category="ops", case_id="new-case-1"):
    card = {"alias": "S1", "chunk_id": "chunk1", "document_id": "doc1", "document_title": "设备手册",
            "revision": 2, "page": 3, "section_path": "排查", "text": text, "lang": "zh-Hans"}
    evidence = {"S1": card} if status == "OK" else {}
    answer = "没有依据。" if error else "复位后观察十分钟 [S1]。"
    return {"id": case_id, "category": category, "answer": answer, "error_code": error,
            "expect": {"title": "设备手册", "revision": 2, "pages": [3], "support_text": span},
            "review_snapshot": make_snapshot("如何复位", answer, evidence, [] if error else [card], error),
            "diagnostics": {"schema_version": 1, "model_declared_refusal": model,
                            "retrieval_trace": [{"tool_call_id": "t1", "query": "复位", "status": status,
                                                 "reason": reason, "required_identifiers": [],
                                                 "missing_identifiers": [],
                                                 "returned_chunk_ids": ["chunk1"] if evidence else []}]}}


def report(*rows):
    return {"mode": "agent", "cases": list(rows)}


@pytest.mark.parametrize("kwargs,bucket", [
    ({}, "model_refusal_with_target_span"),
    ({"text": "另一节的内容。"}, "model_refusal_with_target_location_only"),
    ({"status": "INSUFFICIENT_EVIDENCE", "reason": "IDENTIFIER_MISMATCH"}, "retrieval_blocked"),
    ({"status": "INSUFFICIENT_EVIDENCE", "reason": "LOW_SCORE"}, "retrieval_blocked"),
    ({"status": "NO_ACCESS", "error": "NO_ACCESS"}, "retrieval_blocked"),
    ({"error": "RUN_LIMIT", "model": False}, "operational_failure"),
    ({"error": None, "model": False}, "answer_with_sources"),
    ({"model": False}, "refusal_after_successful_retrieval"),
])
def test_triage_uses_trace_and_captured_target(kwargs, bucket):
    out = analyze_refusals.analyze(report(case(**kwargs)))
    assert out["cases"][0]["bucket"] == bucket
    assert out["review_candidates_with_target_span"] == int(bucket == "model_refusal_with_target_span")


def test_target_location_does_not_claim_semantic_support():
    row = case()
    del row["expect"]["support_text"]
    out = analyze_refusals.analyze(report(row))
    assert out["cases"][0]["bucket"] == "model_refusal_with_target_location_only"
    assert out["review_candidates_with_target_span"] == 0
    assert "not semantic proof" in out["limitation"]


def test_missing_target_and_no_search_have_distinct_diagnoses():
    row = case()
    row["expect"]["revision"] = 1
    assert analyze_refusals.analyze(report(row))["cases"][0]["bucket"] == "model_refusal_without_target"
    row = case(status="INSUFFICIENT_EVIDENCE")
    row["diagnostics"]["retrieval_trace"] = []
    assert analyze_refusals.analyze(report(row))["cases"][0]["bucket"] == "refusal_without_search"


def test_metrics_separate_answerable_cases_operational_errors_and_search_attempts():
    row = case()
    blocked = copy.deepcopy(row["diagnostics"]["retrieval_trace"][0])
    blocked.update(tool_call_id="first", status="INSUFFICIENT_EVIDENCE", reason="LOW_SCORE", returned_chunk_ids=[])
    row["diagnostics"]["retrieval_trace"].insert(0, blocked)
    out = analyze_refusals.analyze(report(row, case(case_id="2", category="no_basis"),
                                          case(case_id="3", error="RUN_LIMIT", model=False)))
    assert out["answerable_cases"] == 2 and out["excluded_cases"] == 1
    assert out["refused_cases"] == 1 and out["refusal_rate"] == 0.5 and out["operational_failures"] == 1
    assert out["blocked_search_reasons"] == {"LOW_SCORE": 1}
    assert out["cases"][0]["search_calls"] == 2 and out["cases"][0]["successful_search_calls"] == 1


def test_no_answerable_cases_has_null_rate():
    out = analyze_refusals.analyze(report(case(category="access")))
    assert out["refusal_rate"] is None and out["answerable_cases"] == 0


@pytest.mark.parametrize("mutation,message", [
    (lambda r: r.pop("review_snapshot"), "no captured source snapshot"),
    (lambda r: r.pop("diagnostics"), "missing or unsupported"),
    (lambda r: r.update(answer="changed"), "differs from captured"),
    (lambda r: r.update(error_code=None), "differs from captured"),
    (lambda r: r["review_snapshot"].update(answer="changed"), "checksum mismatch"),
    (lambda r: r["diagnostics"]["retrieval_trace"][0].update(returned_chunk_ids=[]), "successful retrieval"),
    (lambda r: r["diagnostics"]["retrieval_trace"][0].update(returned_chunk_ids=["wrong"]), "differs from captured evidence"),
    (lambda r: r["diagnostics"].update(model_declared_refusal="true"), "must be a boolean"),
    (lambda r: r["diagnostics"]["retrieval_trace"][0].update(status="NO_ACCESS"), "must not return evidence"),
])
def test_invalid_or_historical_data_is_rejected(mutation, message):
    row = case()
    mutation(row)
    with pytest.raises(ValueError, match=message):
        analyze_refusals.analyze(report(row))


def test_duplicate_cases_and_retrieval_reports_are_rejected():
    with pytest.raises(ValueError, match="unique"):
        analyze_refusals.analyze(report(case(), case()))
    with pytest.raises(ValueError, match="agent evaluation"):
        analyze_refusals.analyze({"mode": "retrieval"})


def test_cli_roundtrip_never_overwrites_report_or_existing_output(tmp_path, monkeypatch):
    source, output = tmp_path / "agent.json", tmp_path / "triage.json"
    source.write_text(json.dumps(report(case()), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["analyze_refusals", str(source), "--out", str(output)])
    analyze_refusals.main()
    assert json.loads(output.read_text(encoding="utf-8"))["review_candidates_with_target_span"] == 1
    for target in (source, output):
        before = target.read_bytes()
        monkeypatch.setattr(sys, "argv", ["analyze_refusals", str(source), "--out", str(target)])
        with pytest.raises(SystemExit) as error:
            analyze_refusals.main()
        assert error.value.code == 2 and target.read_bytes() == before
