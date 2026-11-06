"""Offline refusal triage using captured agent evidence and search diagnostics.

    python eval/analyze_refusals.py agent-report.json --out triage.json

Never calls a model, reconnects to a database, or reconstructs historical evidence.
Target-span matches identify review candidates, not proven over-refusals.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from answer_review import digest, validate_snapshot

REFUSALS = {"NO_ACCESS", "INSUFFICIENT_EVIDENCE"}


def _validate(row: dict) -> tuple[dict, dict]:
    snapshot = row.get("review_snapshot")
    if snapshot is None:
        raise ValueError(f"{row['id']}: no captured source snapshot; cannot reconstruct historical evidence")
    registry = validate_snapshot(snapshot)
    if row.get("answer") != snapshot["answer"] or row.get("error_code") != snapshot["error_code"]:
        raise ValueError("report answer/error differs from captured snapshot")
    diagnostic = row.get("diagnostics")
    if not isinstance(diagnostic, dict) or diagnostic.get("schema_version") != 1:
        raise ValueError(f"{row['id']}: missing or unsupported refusal diagnostics")
    if type(diagnostic.get("model_declared_refusal")) is not bool:
        raise ValueError("model_declared_refusal must be a boolean")
    trace = diagnostic.get("retrieval_trace")
    if not isinstance(trace, list):
        raise ValueError("retrieval_trace must be a list")
    returned, call_ids = set(), set()
    for step in trace:
        if not isinstance(step, dict) or step.get("status") not in {"OK", *REFUSALS}:
            raise ValueError("invalid retrieval diagnostic status")
        call_id = step.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id or call_id in call_ids:
            raise ValueError("retrieval tool call ids must be unique nonempty strings")
        call_ids.add(call_id)
        if not isinstance(step.get("query"), str):
            raise ValueError("retrieval diagnostic query must be text")
        for field in ("returned_chunk_ids", "required_identifiers", "missing_identifiers"):
            if not isinstance(step.get(field), list) or any(not isinstance(v, str) for v in step[field]):
                raise ValueError(f"retrieval diagnostic {field} must be a string list")
        if step.get("reason") not in {None, "LOW_SCORE", "IDENTIFIER_MISMATCH"}:
            raise ValueError("unknown retrieval diagnostic reason")
        if step["status"] != "OK" and step["returned_chunk_ids"]:
            raise ValueError("blocked retrieval must not return evidence")
        if step["status"] == "OK" and not step["returned_chunk_ids"]:
            raise ValueError("successful retrieval must return evidence")
        returned.update(step["returned_chunk_ids"])
    if returned != {card["chunk_id"] for card in registry.values()}:
        raise ValueError("retrieval trace differs from captured evidence")
    if diagnostic["model_declared_refusal"] and (
            snapshot["error_code"] not in REFUSALS or snapshot["sources"]):
        raise ValueError("model refusal must carry a refusal code and no displayed sources")
    return snapshot, diagnostic


def _target_aliases(snapshot: dict, expect: dict) -> tuple[list[str], list[str]]:
    if not isinstance(expect, dict) or not isinstance(expect.get("title"), str):
        raise ValueError("answerable cases require captured target expectations")
    if (not isinstance(expect.get("pages"), list) or not expect["pages"]
            or any(type(p) is not int for p in expect["pages"])
            or type(expect.get("revision")) is not int):
        raise ValueError("target expectations require pages and revision")
    span = expect.get("support_text")
    if span is not None and (not isinstance(span, str) or not span.strip()):
        raise ValueError("support_text must be nonempty text when supplied")
    location = [card for card in snapshot["evidence"] if card["document_title"] == expect["title"]
                and card["revision"] == expect["revision"] and card["page"] in expect["pages"]]
    return ([card["alias"] for card in location],
            [card["alias"] for card in location if span and span in card["text"]])


def analyze(report: dict) -> dict:
    if report.get("mode") != "agent":
        raise ValueError("refusal triage requires an agent evaluation report")
    rows = report.get("cases")
    if not isinstance(rows, list) or not rows:
        raise ValueError("report must contain cases")
    ids = [row["id"] for row in rows]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("case ids must be unique nonempty strings")
    cases, buckets, reasons = [], Counter(), Counter()
    refused = candidates = operational = answerable = 0
    for row in rows:
        snapshot, diagnostic = _validate(row)
        if row.get("category") not in {"ops", "cross"}:
            continue  # Access/no-basis/escalation cases are not answerable denominators.
        answerable += 1
        location, spans = _target_aliases(snapshot, row.get("expect"))
        trace = diagnostic["retrieval_trace"]
        success = any(step["status"] == "OK" for step in trace)
        error = snapshot["error_code"]
        is_refused = error in REFUSALS
        if error is not None and error not in REFUSALS:
            bucket = "operational_failure"
            operational += 1
        elif is_refused:
            refused += 1
            if not trace:
                bucket = "refusal_without_search"
            elif not success:
                bucket = "retrieval_blocked"
            elif diagnostic["model_declared_refusal"]:
                if spans:
                    bucket = "model_refusal_with_target_span"
                    candidates += 1
                elif location:
                    bucket = "model_refusal_with_target_location_only"
                else:
                    bucket = "model_refusal_without_target"
            else:
                bucket = "refusal_after_successful_retrieval"
        else:
            bucket = "answer_with_sources" if snapshot["sources"] else "answer_without_sources"
        buckets[bucket] += 1
        for step in trace:
            if step["status"] != "OK":
                reasons[step["reason"] or step["status"]] += 1
        cases.append({"id": row["id"], "category": row["category"], "bucket": bucket,
                      "refused": is_refused, "error_code": error,
                      "model_declared_refusal": diagnostic["model_declared_refusal"],
                      "search_calls": len(trace), "successful_search_calls": sum(s["status"] == "OK" for s in trace),
                      "target_location_aliases": location, "target_span_aliases": spans,
                      "blocked_search_reasons": [s["reason"] or s["status"] for s in trace if s["status"] != "OK"],
                      "snapshot_sha256": snapshot["sha256"]})
    return {"schema_version": 1, "report_sha256": digest(report), "report_cases": len(rows),
            "answerable_cases": answerable, "excluded_cases": len(rows) - answerable,
            "refused_cases": refused, "refusal_rate": round(refused / answerable, 4) if answerable else None,
            "operational_failures": operational, "review_candidates_with_target_span": candidates,
            "buckets": dict(sorted(buckets.items())), "blocked_search_reasons": dict(sorted(reasons.items())),
            "limitation": "Target location/span matches are lexical diagnostics, not semantic proof of over-refusal. "
                          "Refusal rate includes only explicit refusal codes; unmarked prose refusals need manual review.",
            "cases": cases}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        result = analyze(report)
        with args.out.open("x", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"triage error: {error}\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
