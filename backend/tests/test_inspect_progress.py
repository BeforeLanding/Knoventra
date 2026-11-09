import copy
import json
import sys

import pytest

from answer_review import make_snapshot
from inspect_progress import inspect, main, read_events
from progress import ProgressJournal


def event(kind, **fields):
    return {"schema_version": 1, "event": kind, "at": "2026-11-07T00:00:00+00:00", **fields}


def fixture_events():
    preflight = {"mode": "preflight", "evaluation_mode": "agent", "case_ids": ["a", "b"],
                 "n_cases": 2, "cases_sha256": "frozen-case-digest", "split": "dev"}
    result = {"id": "a", "thread_id": "thread-a", "category": "no_basis", "answer": "无依据",
              "error_code": "INSUFFICIENT_EVIDENCE", "refused": True,
              "review_snapshot": make_snapshot("问题", "无依据", {}, [], "INSUFFICIENT_EVIDENCE")}
    return [event("run_started", preflight=preflight), event("corpus_captured", corpus={"documents": 12}),
            event("case_started", case_id="a", thread_id="thread-a"), event("case_completed", result=result),
            event("case_started", case_id="b", thread_id="thread-b")]


def completed_events():
    events = fixture_events()
    second = copy.deepcopy(events[3]["result"])
    second.update(id="b", thread_id="thread-b")
    return [*events, event("case_completed", result=second),
            event("run_completed", completed_cases=2, report_path="never-follow-this-path.json")]


def report_for(events):
    return {"mode": "agent", "preflight": events[0]["preflight"], "corpus": events[1]["corpus"],
            "cases": [e["result"] for e in events if e["event"] == "case_completed"],
            "n_cases": 2, "split": "dev", "cases_sha256": "frozen-case-digest",
            "metrics": {"refusal_correct": 1.0}}


def test_unclosed_failed_and_never_started_are_distinct():
    events = fixture_events()
    result = inspect(events)
    assert result["status"] == "unclosed" and result["completed_case_ids"] == ["a"]
    assert result["in_flight"] == {"id": "b", "thread_id": "thread-b"}
    assert result["not_started_case_ids"] == []
    assert inspect(events[:4])["not_started_case_ids"] == ["b"]
    assert inspect(events + [event("run_failed", error_type="TimeoutError")])["status"] == "failed"
    result = inspect([events[0], event("run_failed", error_type="OSError")])
    assert result["completed_case_ids"] == [] and result["not_started_case_ids"] == ["a", "b"]


def test_complete_report_binding_and_post_save_failure():
    events = completed_events()
    report = report_for(events)
    result = inspect(events, report=report)
    assert result["status"] == "completed" and result["report_verified"] and result["report_sha256"]
    assert "metrics" not in result and "cases" not in result
    events.append(event("run_failed", error_type="BrokenPipeError"))
    assert inspect(events, report=report)["status"] == "completed_with_error"


@pytest.mark.parametrize("answer_timing", [False, True])
def test_timed_report_binding_validates_new_metrics_and_keeps_legacy_reports_valid(answer_timing):
    from run_eval import AGENT_LATENCY_POLICY, summarize_agent
    from answer_timing import ANSWER_TIMING_POLICY

    events = completed_events()
    report = report_for(events)
    report["config"] = {"latency_policy": AGENT_LATENCY_POLICY}
    if answer_timing:
        report["config"]["answer_timing_policy"] = ANSWER_TIMING_POLICY
    for i, row in enumerate(report["cases"], 1):
        row["latency_ms"] = i * 100
        row["first_answer_chunk_ms"] = i * 20
        row["diagnostics"] = {"retrieval_trace": [{"latency_ms": i * 10}]}
    report["metrics"] = summarize_agent(report["cases"])
    if not answer_timing:
        report["metrics"].pop("first_answer_chunk_ms")
    assert inspect(events, report=report)["report_verified"]
    for metric in ("latency_ms", "retrieval_latency_ms", *(["first_answer_chunk_ms"] if answer_timing else [])):
        changed = copy.deepcopy(report)
        changed["metrics"][metric]["p95"] += 1
        with pytest.raises(ValueError, match="metrics"):
            inspect(events, report=changed)
        changed = copy.deepcopy(report)
        del changed["metrics"][metric]
        with pytest.raises(ValueError, match="metrics"):
            inspect(events, report=changed)
    report["config"]["latency_policy"] = "unknown"
    with pytest.raises(ValueError, match="latency policy"):
        inspect(events, report=report)
    del report["config"]
    with pytest.raises(ValueError, match="metrics"):
        inspect(events, report=report)
    assert inspect(completed_events(), report=report_for(completed_events()))["report_verified"]


def test_unknown_answer_timing_policy_is_rejected():
    events = completed_events()
    report = report_for(events)
    report["config"] = {"answer_timing_policy": "unknown"}
    with pytest.raises(ValueError, match="answer timing policy"):
        inspect(events, report=report)


@pytest.mark.parametrize("option,value", [("llm_thinking", "disabled"), ("llm_streaming", True)])
def test_logged_model_options_must_match_report(option, value):
    events = completed_events()
    events[0]["preflight"][option] = value
    report = report_for(events)
    with pytest.raises(ValueError, match="model options"):
        inspect(events, report=report)
    report["config"] = {option: value}
    assert inspect(events, report=report)["report_verified"]


@pytest.mark.parametrize("option,value", [("llm_thinking", "enabled"), ("llm_streaming", "true")])
def test_unknown_logged_model_options_are_rejected(option, value):
    events = completed_events()
    events[0]["preflight"][option] = value
    with pytest.raises(ValueError, match="model options"):
        inspect(events)


@pytest.mark.parametrize("field,value", [
    ("mode", "retrieval"), ("n_cases", 1), ("cases_sha256", "other"), ("split", "holdout"),
    ("preflight", {}), ("corpus", {}), ("cases", []), ("metrics", {"refusal_correct": 0}),
])
def test_report_drift_is_rejected(field, value):
    events = completed_events()
    report = copy.deepcopy(report_for(events))
    report[field] = value
    with pytest.raises(ValueError, match="report"):
        inspect(events, report=report)


@pytest.mark.parametrize("mutate", [
    lambda e: e.pop(0),
    lambda e: e[0]["preflight"].update(case_ids=["a", "a"]),
    lambda e: e[0].update(schema_version=True),
    lambda e: e[1].update(event="unknown"),
    lambda e: e.insert(2, copy.deepcopy(e[1])),
    lambda e: e[2].update(case_id="b"),
    lambda e: e[3]["result"].update(thread_id="other"),
    lambda e: e[3]["result"].update(answer="changed"),
    lambda e: e[3]["result"]["review_snapshot"].update(answer="changed"),
    lambda e: e[4].update(thread_id="thread-a"),
    lambda e: e.insert(3, copy.deepcopy(e[4])),
    lambda e: e.append(event("run_completed", completed_cases=2, report_path="report.json")),
    lambda e: e.extend([event("run_failed", error_type="Error"), copy.deepcopy(e[4])]),
])
def test_invalid_event_sequences_and_snapshots_are_rejected(mutate):
    events = fixture_events()
    mutate(events)
    with pytest.raises(ValueError):
        inspect(events)


def test_incomplete_run_cannot_verify_report():
    with pytest.raises(ValueError, match="without run_completed"):
        inspect(fixture_events(), report={})


def test_truncated_tail_requires_opt_in_and_only_last_line_may_be_ignored(tmp_path):
    path = tmp_path / "progress.jsonl"
    header = json.dumps(fixture_events()[0]).encode() + b"\n"
    path.write_bytes(header + b'{"event":"case_started", "text":"\xe4\xb8')
    with pytest.raises(ValueError, match="unterminated"):
        read_events(path)
    events, ignored = read_events(path, allow_truncated_tail=True)
    assert ignored and len(events) == 1
    assert inspect(events, ignored_tail=ignored)["ignored_truncated_tail"]
    path.write_bytes(header + b'{bad json}\n' + b'{truncated')
    with pytest.raises(ValueError, match="line 2"):
        read_events(path, allow_truncated_tail=True)


@pytest.mark.parametrize("data", [b'null\n', b'{"score":NaN}\n', b'{"text":"\xff"}\n'])
def test_invalid_complete_json_lines_are_not_silently_skipped(tmp_path, data):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        read_events(path, allow_truncated_tail=True)


def test_real_journal_cli_is_offline_and_output_cannot_be_overwritten(tmp_path, monkeypatch):
    path, output = tmp_path / "progress.jsonl", tmp_path / "inspection.json"
    events = fixture_events()
    with ProgressJournal(path, events[0]["preflight"]) as journal:
        journal.record("corpus_captured", corpus=events[1]["corpus"])
        journal.start_case("a", "thread-a")
        journal.complete_case(events[3]["result"])
    from knoventra.db import session
    from knoventra.ingestion import encoder
    def forbidden(*args, **kwargs):
        pytest.fail("inspection must not access DB or model")
    monkeypatch.setattr(session, "SessionLocal", forbidden)
    monkeypatch.setattr(encoder, "get_encoder", forbidden)
    monkeypatch.setattr(sys, "argv", ["inspect", str(path), "--out", str(output)])
    main()
    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["status"] == "unclosed" and summary["not_started_case_ids"] == ["b"]
    before = output.read_bytes()
    with pytest.raises(SystemExit):
        main()
    assert output.read_bytes() == before
