"""Inspect agent progress offline. Never follows stored paths or resumes execution."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from answer_review import digest, validate_snapshot


def _reject_constant(value):
    raise ValueError("non-finite JSON value")


def read_events(path: Path, *, allow_truncated_tail: bool = False) -> tuple[list[dict], bool]:
    lines = path.read_bytes().splitlines(keepends=True)
    events = []
    ignored = False
    for number, line in enumerate(lines, 1):
        if not line.endswith(b"\n"):
            if allow_truncated_tail and number == len(lines):
                ignored = True
                break
            raise ValueError("unterminated final line; use --allow-truncated-tail to ignore it")
        try:
            event = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError):
            raise ValueError(f"invalid JSON/UTF-8 at line {number}") from None
        if not isinstance(event, dict):
            raise ValueError(f"event must be an object at line {number}")
        events.append(event)
    return events, ignored


def inspect(events: list[dict], *, ignored_tail: bool = False, report: dict | None = None) -> dict:
    if not events:
        raise ValueError("no complete run header")
    header = events[0]
    preflight = header.get("preflight")
    if header.get("event") != "run_started" or not isinstance(preflight, dict):
        raise ValueError("first event must be run_started with preflight")
    ids = preflight.get("case_ids")
    if (preflight.get("mode") != "preflight" or preflight.get("evaluation_mode") != "agent"
            or not isinstance(ids, list) or not ids
            or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids)
            or type(preflight.get("n_cases")) is not int or preflight["n_cases"] != len(ids)):
        raise ValueError("invalid agent preflight case selection")
    corpus, active, terminal, completion, error_type = None, None, None, None, None
    started, completed, threads = [], [], set()
    for index, event in enumerate(events):
        if type(event.get("schema_version")) is not int or event["schema_version"] != 1:
            raise ValueError("unsupported progress schema")
        if not isinstance(event.get("at"), str) or not event["at"]:
            raise ValueError("event timestamp is missing")
        kind = event.get("event")
        if index == 0:
            continue
        # A stdout/cleanup error may be recorded after the report was saved.
        if terminal is not None and not (terminal == "completed" and kind == "run_failed"):
            raise ValueError("event follows a terminal event")
        if kind == "corpus_captured":
            if corpus is not None or started or not isinstance(event.get("corpus"), dict):
                raise ValueError("duplicate or misplaced corpus event")
            corpus = event["corpus"]
        elif kind == "case_started":
            thread = event.get("thread_id")
            if (corpus is None or active is not None or len(started) >= len(ids)
                    or event.get("case_id") != ids[len(started)]
                    or not isinstance(thread, str) or not thread or thread in threads):
                raise ValueError("case start violates selection order or thread identity")
            active = {"id": event["case_id"], "thread_id": thread}
            started.append(active)
            threads.add(thread)
        elif kind == "case_completed":
            row = event.get("result")
            if (active is None or not isinstance(row, dict) or row.get("id") != active["id"]
                    or row.get("thread_id") != active["thread_id"]):
                raise ValueError("completed case does not match active case/thread")
            snapshot = row.get("review_snapshot")
            if not isinstance(snapshot, dict):
                raise ValueError("completed case has no captured snapshot")
            validate_snapshot(snapshot)
            if row.get("answer") != snapshot["answer"] or row.get("error_code") != snapshot["error_code"]:
                raise ValueError("completed answer/error differs from snapshot")
            completed.append(row)
            active = None
        elif kind == "run_completed":
            if (active is not None or len(completed) != len(ids)
                    or type(event.get("completed_cases")) is not int or event["completed_cases"] != len(ids)
                    or not isinstance(event.get("report_path"), str) or not event["report_path"]):
                raise ValueError("run completion requires all selected cases")
            terminal, completion = "completed", event
        elif kind == "run_failed":
            if not isinstance(event.get("error_type"), str) or not event["error_type"]:
                raise ValueError("failure event requires error type")
            terminal = "completed_with_error" if completion else "failed"
            error_type = event["error_type"]
        else:
            raise ValueError("unknown or misplaced progress event")
    if ignored_tail and terminal is not None:
        raise ValueError("unexpected truncated data after terminal event")
    report_digest = None
    if report is not None:
        if completion is None:
            raise ValueError("cannot verify a report without run_completed")
        if (report.get("mode") != "agent" or report.get("preflight") != preflight
                or report.get("corpus") != corpus or report.get("cases") != completed
                or type(report.get("n_cases")) is not int or report["n_cases"] != len(ids)
                or report.get("cases_sha256") != preflight.get("cases_sha256")
                or report.get("split") != preflight.get("split")):
            raise ValueError("report differs from captured progress")
        from run_eval import summarize_agent
        if report.get("metrics") != summarize_agent(completed):
            raise ValueError("report metrics differ from completed cases")
        report_digest = digest(report)
    return {"schema_version": 1, "mode": "progress_inspection", "status": terminal or "unclosed",
            "error_type": error_type,
            "selected_case_ids": ids, "completed_case_ids": [r["id"] for r in completed],
            "in_flight": active, "not_started_case_ids": ids[len(started):],
            "ignored_truncated_tail": ignored_tail, "complete_event_count": len(events),
            "events_sha256": digest(events), "report_verified": report is not None,
            "report_sha256": report_digest,
            "limitation": "Offline inspection only; checksums are not signatures. Unclosed does not mean running. "
                          "In-flight cases may have incurred charges or committed writes. No automatic resume. "
                          "Report verification covers preflight/selection, corpus, results and metrics, not unlogged configuration."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path)
    parser.add_argument("--allow-truncated-tail", action="store_true")
    parser.add_argument("--report", type=Path, help="explicit report to verify; stored paths are never followed")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("output already exists; choose a new inspection path")
    try:
        events, ignored = read_events(args.journal, allow_truncated_tail=args.allow_truncated_tail)
        report = json.loads(args.report.read_text(encoding="utf-8")) if args.report else None
        if args.report and not isinstance(report, dict):
            raise ValueError("report must be an object")
        result = inspect(events, ignored_tail=ignored, report=report)
    except (ValueError, KeyError, TypeError):
        parser.error("invalid progress/report; validate event order, snapshots and report correspondence")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as output:
        output.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
