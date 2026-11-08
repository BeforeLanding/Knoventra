import json
import sys
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

import run_eval
from progress import ProgressJournal


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_progress_flushes_completed_results_before_later_failure(tmp_path):
    path = tmp_path / "nested" / "run.jsonl"
    with pytest.raises(RuntimeError, match="secret"):
        with ProgressJournal(path, {"case_ids": ["a", "b"]}) as journal:
            journal.start_case("a", "thread-a")
            journal.complete_case({"id": "a", "answer": "原始回答", "review_snapshot": {"text": "证据"}})
            assert rows(path)[-1]["result"]["answer"] == "原始回答"
            journal.start_case("b", "thread-b")
            raise RuntimeError("secret provider URL and key")
    result = rows(path)
    assert [r["event"] for r in result] == ["run_started", "case_started", "case_completed", "case_started", "run_failed"]
    assert result[-1]["error_type"] == "RuntimeError"
    assert "secret" not in path.read_text(encoding="utf-8")
    assert journal.stream.closed


def test_progress_never_overwrites_existing_run(tmp_path):
    path = tmp_path / "existing.jsonl"
    path.write_text("original", encoding="utf-8")
    with pytest.raises(FileExistsError):
        with ProgressJournal(path, {}):
            pytest.fail("must fail before starting")
    assert path.read_text(encoding="utf-8") == "original"


def test_journal_write_failure_preserves_original_exception(tmp_path, monkeypatch):
    with pytest.raises(KeyboardInterrupt):
        with ProgressJournal(tmp_path / "interrupt.jsonl", {}) as journal:
            monkeypatch.setattr(journal, "record", Mock(side_effect=OSError("disk unavailable")))
            raise KeyboardInterrupt()
    assert journal.stream.closed


@pytest.mark.parametrize("flags", [
    ["--progress-out", "unused.jsonl"],
    ["--mode", "agent", "--out", "same.json", "--progress-out", "same.json"],
])
def test_invalid_progress_flags_fail_before_execution(monkeypatch, flags):
    execute = Mock(side_effect=AssertionError("must not run"))
    monkeypatch.setattr(run_eval, "run_evaluation", execute)
    monkeypatch.setattr(sys, "argv", ["eval", *flags])
    with pytest.raises(SystemExit) as error:
        run_eval.main()
    assert error.value.code == 2
    execute.assert_not_called()


def test_existing_default_progress_blocks_execution_before_loading_models(tmp_path, monkeypatch):
    from knoventra.config import settings
    monkeypatch.setattr(settings, "llm_model", "offline-test")
    output = tmp_path / "report.json"
    progress = tmp_path / "report.json.progress.jsonl"
    progress.write_text("previous run", encoding="utf-8")
    execute = Mock(side_effect=AssertionError("must not run"))
    monkeypatch.setattr(run_eval, "run_evaluation", execute)
    monkeypatch.setattr(sys, "argv", ["eval", "--mode", "agent", "--allow-llm", "--out", str(output),
                                     "--input-price-per-million", "0", "--output-price-per-million", "0"])
    with pytest.raises(FileExistsError):
        run_eval.main()
    execute.assert_not_called()
    assert progress.read_text(encoding="utf-8") == "previous run"


def test_dry_run_creates_no_progress_journal(tmp_path, monkeypatch):
    output, progress = tmp_path / "plan.json", tmp_path / "progress.jsonl"
    monkeypatch.setattr(sys, "argv", ["eval", "--mode", "agent", "--dry-run", "--out", str(output),
                                     "--progress-out", str(progress)])
    run_eval.main()
    assert json.loads(output.read_text(encoding="utf-8"))["mode"] == "preflight"
    assert not progress.exists()


@pytest.mark.parametrize("fail", [False, True])
def test_cli_agent_persists_per_case_and_only_publishes_complete_report(engine, tmp_path, monkeypatch, fail):
    from knoventra.config import settings
    monkeypatch.setattr(settings, "llm_model", "offline-test-model")
    selected = [c for c in run_eval.load_cases("dev") if c["category"] == "no_basis"][:2]
    assert len(selected) == 2
    state = {"messages": [AIMessage("无依据")], "error_code": "INSUFFICIENT_EVIDENCE"}
    graph = Mock()
    graph.invoke.side_effect = [state, RuntimeError("secret") if fail else state]
    pool = Mock()
    monkeypatch.setattr(run_eval, "make_evaluation_graph", lambda *args: (graph, pool))
    output = tmp_path / "report.json"
    argv = ["eval", "--mode", "agent", "--allow-llm", "--input-price-per-million", "0",
            "--output-price-per-million", "0", "--out", str(output)]
    for case in selected:
        argv.extend(["--case-id", case["id"]])
    monkeypatch.setattr(sys, "argv", argv)
    if fail:
        with pytest.raises(RuntimeError, match="secret"):
            run_eval.main()
    else:
        run_eval.main()
    journal = rows(tmp_path / "report.json.progress.jsonl")
    from inspect_progress import inspect
    inspection = inspect(journal, report=json.loads(output.read_text(encoding="utf-8")) if not fail else None)
    assert inspection["status"] == ("failed" if fail else "completed")
    assert inspection["report_verified"] is not fail
    completed = [r["result"] for r in journal if r["event"] == "case_completed"]
    assert len(completed) == (1 if fail else 2)
    assert completed[0]["review_snapshot"]["answer"] == "无依据"
    starts = [r for r in journal if r["event"] == "case_started"]
    assert [r["case_id"] for r in starts] == [c["id"] for c in selected]
    assert starts[0]["thread_id"] == completed[0]["thread_id"]
    assert graph.invoke.call_count == 2  # no automatic re-execution
    assert journal[-1]["event"] == ("run_failed" if fail else "run_completed")
    assert output.exists() is not fail
    if not fail:
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["cases"] == completed
        assert report["metrics"]["refusal_correct"] == 1
    else:
        assert "secret" not in (tmp_path / "report.json.progress.jsonl").read_text(encoding="utf-8")
    pool.close.assert_called_once()
