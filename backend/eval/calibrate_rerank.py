"""Measure a frozen, separate rerank gate set with cached local models only.

Run on an isolated, already seeded PostgreSQL database. This script never calls
an LLM and never changes the serving threshold.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from knoventra.config import settings
from knoventra.db.models import User
from knoventra.db.session import SessionLocal
from knoventra.ingestion.encoder import get_encoder
from knoventra.retrieval.pipeline import retrieve
from knoventra.retrieval.rerank import get_reranker
from run_eval import corpus_fingerprints


DEFAULT_CASES = Path(__file__).with_name("cases_threshold_v1.jsonl")


def load_cases(path: Path) -> tuple[list[dict], str]:
    data = path.read_bytes()
    cases = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("cases must be nonempty and have unique IDs")
    if {case["split"] for case in cases} != {"calibration", "validation"}:
        raise ValueError("both calibration and validation splits are required")
    for case in cases:
        if type(case.get("answerable")) is not bool or not case.get("question") or not case.get("user"):
            raise ValueError(f"invalid case: {case.get('id')}")
        expected = case.get("expect")
        if case["answerable"] and (not isinstance(expected, dict) or
                                   not all(expected.get(key) for key in ("title", "page", "support_text"))):
            raise ValueError(f"answerable case needs a source label: {case['id']}")
        if not case["answerable"] and expected is not None:
            raise ValueError(f"unanswerable case must not have a source label: {case['id']}")
    return cases, hashlib.sha256(data).hexdigest()


def measure(db, enc, rr, users: dict[str, User], cases: list[dict]) -> list[dict]:
    rows = []
    for case in cases:
        if case["user"] not in users:
            raise ValueError(f"unknown user: {case['user']}")
        outcome = retrieve(db, enc, rr, users[case["user"]], case["question"], threshold=0.0)
        if outcome.required_identifiers:
            raise ValueError(f"score-only set must not contain exact identifiers: {case['id']}")
        ranked = outcome.ranked_evidence
        score = max((e.rerank_score for e in ranked if e.rerank_score is not None), default=-1.0)
        expected = case.get("expect")
        target_found = (any(e.document_title == expected["title"] and e.page == expected["page"]
                            and expected["support_text"] in e.text for e in ranked) if expected else None)
        rows.append({"id": case["id"], "split": case["split"], "answerable": case["answerable"],
                     "question": case["question"], "user": case["user"], "top_score": score,
                     "target_found_at_5": target_found, "pre_gate_reason": outcome.reason,
                     "top_title": ranked[0].document_title if ranked else None,
                     "top_page": ranked[0].page if ranked else None})
    return rows


def score_split(rows: list[dict], threshold: float) -> dict:
    positive = [r for r in rows if r["answerable"]]
    negative = [r for r in rows if not r["answerable"]]
    if not positive or not negative:
        raise ValueError("each split needs answerable and unanswerable cases")
    accepted = lambda r: r["top_score"] >= threshold and r["pre_gate_reason"] != "IDENTIFIER_MISMATCH"
    true_accept = sum(bool(accepted(r) and r["target_found_at_5"]) for r in positive)
    false_accept = sum(bool(accepted(r)) for r in negative)
    return {"threshold": round(threshold, 6), "answerable": len(positive), "unanswerable": len(negative),
            "true_accept": true_accept, "false_reject_or_miss": len(positive) - true_accept,
            "false_accept": false_accept, "true_reject": len(negative) - false_accept,
            "target_misses_at_5": sum(not r["target_found_at_5"] for r in positive),
            "balanced_accuracy": round((true_accept / len(positive) +
                                        (len(negative) - false_accept) / len(negative)) / 2, 4)}


def select_threshold(rows: list[dict]) -> tuple[float, dict]:
    values = sorted({r["top_score"] for r in rows if 0 <= r["top_score"] <= 1})
    candidates = sorted({0.0, 1.0, *values, *((a + b) / 2 for a, b in zip(values, values[1:]))})
    scored = [(value, score_split(rows, value)) for value in candidates]
    # Equal balanced accuracy favours fewer unsupported accepts, then a lower
    # threshold to preserve answerable recall. Validation is never consulted.
    threshold, result = max(scored, key=lambda pair: (pair[1]["balanced_accuracy"],
                                                      -pair[1]["false_accept"], -pair[0]))
    return threshold, result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("output already exists")
    cases, case_hash = load_cases(args.cases)
    if settings.encoder != "bge-m3" or settings.rerank != "bge":
        parser.error("set KNV_ENCODER=bge-m3 and KNV_RERANK=bge for real local-model calibration")
    enc, rr = get_encoder(), get_reranker()
    if rr is None:
        raise RuntimeError("reranker did not load")
    with SessionLocal() as db:
        users = {u.email.split("@")[0]: u for u in db.scalars(select(User))}
        corpus = corpus_fingerprints(db)
        rows = measure(db, enc, rr, users, cases)
    calibration = [row for row in rows if row["split"] == "calibration"]
    validation = [row for row in rows if row["split"] == "validation"]
    threshold, chosen = select_threshold(calibration)
    report = {"schema_version": 1, "date": dt.date.today().isoformat(), "mode": "rerank_threshold_calibration",
              "llm_calls": 0, "case_sha256": case_hash, "n_cases": len(cases), "corpus": corpus,
              "encoder": getattr(enc, "model_name", None), "reranker": settings.rerank_model,
              "score_semantics": "normalized reranker score; not an answerability probability",
              "selection_rule": "maximize calibration balanced accuracy; tie: fewer false accepts, then lower threshold",
              "calibration": chosen, "validation": score_split(validation, threshold),
              "serving_threshold_unchanged": settings.rerank_threshold, "cases": rows}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
    print(json.dumps({"calibration": chosen, "validation": report["validation"], "corpus": corpus},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
