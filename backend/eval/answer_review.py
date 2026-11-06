"""Offline, source-bound annotation of answer claims. Never calls a model or DB.

Checksums detect accidental drift, not malicious editing or semantic entailment.
The reviewer supplies the actual judgement; human and agent labels stay separate.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter

_CITATION = re.compile(r"\[S([1-9]\d*)\]")
_ALIAS = re.compile(r"S[1-9]\d*")
_SENTENCE = re.compile(r".+?(?:[。！？!?；;](?:[ \t]*\[S[1-9]\d*\])*|$)")
_VERDICTS = {"unreviewed", "supported", "partially_supported", "unsupported", "not_a_claim"}
_FACTUAL = {"supported", "partially_supported", "unsupported"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def make_snapshot(question: str, answer: str, registry: dict, sources: list, error_code: str | None) -> dict:
    if any(not isinstance(card, dict) or card.get("alias") != alias for alias, card in registry.items()):
        raise ValueError("evidence registry keys must match card aliases")
    body = copy.deepcopy({"schema_version": 1, "question": question, "answer": answer,
                          "evidence": [registry[a] for a in sorted(registry)], "sources": sources,
                          "error_code": error_code})
    snapshot = {**body, "sha256": digest(body)}
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot: dict) -> dict[str, dict]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("unsupported or missing answer snapshot")
    body = {k: v for k, v in snapshot.items() if k != "sha256"}
    if snapshot.get("sha256") != digest(body):
        raise ValueError("answer snapshot checksum mismatch")
    if not isinstance(snapshot.get("question"), str) or not isinstance(snapshot.get("answer"), str):
        raise ValueError("snapshot question and answer must be text")
    if not isinstance(snapshot.get("evidence"), list) or not isinstance(snapshot.get("sources"), list):
        raise ValueError("snapshot must contain evidence and sources, including explicit empty lists")
    registry = {}
    for card in snapshot["evidence"]:
        if not isinstance(card, dict):
            raise ValueError("invalid evidence card")
        alias = card.get("alias")
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias) or alias in registry:
            raise ValueError("invalid or duplicate evidence alias")
        if any(not isinstance(card.get(k), str) for k in ("chunk_id", "document_id", "document_title", "text", "section_path", "lang")):
            raise ValueError("evidence must preserve identity, section and full source text")
        if not isinstance(card.get("revision"), int) or isinstance(card["revision"], bool):
            raise ValueError("evidence revision must be an integer")
        if "page" not in card or (card["page"] is not None and
                                  (not isinstance(card["page"], int) or isinstance(card["page"], bool))):
            raise ValueError("evidence must preserve the page number or explicit null")
        registry[alias] = card
    seen = set()
    for card in snapshot["sources"]:
        if not isinstance(card, dict) or not isinstance(card.get("alias"), str):
            raise ValueError("invalid cited source")
        alias = card["alias"]
        if alias in seen or registry.get(alias) != card:
            raise ValueError("cited source does not match the captured evidence registry")
        seen.add(alias)
    return registry


def split_claims(answer: str) -> list[dict]:
    """Suggest units by line and Chinese sentence punctuation; reviewer decides
    whether headings, greetings, recommendations, etc. are factual claims."""
    claims = []
    for line in answer.splitlines():
        for match in _SENTENCE.finditer(line.strip()):
            text = match.group().strip()
            if not _CITATION.sub("", text).strip():
                continue
            aliases = list(dict.fromkeys("S" + m for m in _CITATION.findall(text)))
            claims.append({"id": f"C{len(claims) + 1}", "text": text, "citations": aliases,
                           "verdict": "unreviewed", "reviewer": None, "reviewer_kind": None,
                           "notes": "", "supports": []})
    return claims


def prepare_packet(report: dict, case_ids: list[str] | None = None) -> dict:
    if report.get("mode") != "agent":
        raise ValueError("answer review requires an agent evaluation report")
    rows = report.get("cases", [])
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate evaluation case ids")
    requested = set(case_ids) if case_ids else set(ids)
    if requested - set(ids):
        raise ValueError("requested case id does not exist in the report")
    cases = []
    for row in rows:
        if row["id"] not in requested:
            continue
        snapshot = row.get("review_snapshot")
        if snapshot is None:
            raise ValueError(f"{row['id']}: no captured source snapshot; historical sources cannot be reconstructed")
        validate_snapshot(snapshot)
        if row.get("answer") != snapshot["answer"]:
            raise ValueError("report answer differs from captured snapshot")
        cases.append({"id": row["id"], "snapshot": copy.deepcopy(snapshot),
                      "claims": split_claims(snapshot["answer"])})
    if not cases:
        raise ValueError("no cases selected for review")
    return {"schema_version": 1, "report_sha256": digest(report),
            "selected_case_ids": [c["id"] for c in cases], "cases": cases}


def summarize_packet(packet: dict, report: dict) -> dict:
    # Recreate immutable fields from the original report, so edits to the claim
    # text, cited aliases, source snapshot, case set or report are detected.
    ids = [c["id"] for c in packet.get("cases", [])]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate review case ids")
    selected = packet.get("selected_case_ids")
    if not isinstance(selected, list) or ids != selected:
        raise ValueError("review case selection has changed")
    original = prepare_packet(report, selected)
    if packet.get("schema_version") != 1 or packet.get("report_sha256") != original["report_sha256"]:
        raise ValueError("review packet does not match this evaluation report")
    if len(packet["cases"]) != len(original["cases"]):
        raise ValueError("review case set has changed")
    counts = {"human": Counter(), "agent": Counter()}
    total = pending = uncited = 0
    for case, source in zip(packet["cases"], original["cases"]):
        if case["id"] != source["id"] or case.get("snapshot") != source["snapshot"]:
            raise ValueError("review snapshot or case order has changed")
        if len(case.get("claims", [])) != len(source["claims"]):
            raise ValueError("review claim units have changed")
        registry = validate_snapshot(case["snapshot"])
        displayed = {s["alias"] for s in case["snapshot"]["sources"]}
        for claim, expected in zip(case["claims"], source["claims"]):
            if any(claim.get(k) != expected[k] for k in ("id", "text", "citations")):
                raise ValueError("review claim text or citations have changed")
            total += 1
            verdict = claim.get("verdict")
            if verdict not in _VERDICTS:
                raise ValueError("unknown claim verdict")
            if verdict == "unreviewed":
                pending += 1
                continue
            kind = claim.get("reviewer_kind")
            if kind not in counts or not isinstance(claim.get("reviewer"), str) or not claim["reviewer"].strip():
                raise ValueError("reviewed claims require a named human or agent reviewer")
            if not isinstance(claim.get("notes"), str) or not claim["notes"].strip():
                raise ValueError("reviewed claims require a judgement rationale")
            supports = claim.get("supports")
            if not isinstance(supports, list):
                raise ValueError("claim supports must be a list")
            if verdict in {"supported", "partially_supported"} and not supports:
                raise ValueError("support verdict requires at least one exact source quotation")
            for support in supports:
                if not isinstance(support, dict):
                    raise ValueError("invalid support quotation")
                alias, quote = support.get("alias"), support.get("quote")
                if not isinstance(alias, str) or alias not in registry:
                    raise ValueError("quotation references unknown evidence")
                card = registry[alias]
                if not isinstance(quote, str) or not quote.strip() or quote not in card["section_path"] + "\n" + card["text"]:
                    raise ValueError("quotation is absent from the captured source")
                if verdict in {"supported", "partially_supported"} and claim["citations"] \
                        and (alias not in claim["citations"] or alias not in displayed):
                    raise ValueError("support must come from the claim's displayed citations")
            counts[kind][verdict] += 1
            if verdict in _FACTUAL and not claim["citations"]:
                counts[kind]["uncited_factual_claims"] += 1
                uncited += 1
    metrics = {}
    for kind, values in counts.items():
        n = sum(values[v] for v in _FACTUAL)
        metrics[kind] = {"reviewed_factual_claims": n, "supported": values["supported"],
                         "partially_supported": values["partially_supported"], "unsupported": values["unsupported"],
                         "not_a_claim": values["not_a_claim"], "uncited_factual_claims": values["uncited_factual_claims"],
                         "fully_supported_rate": round(values["supported"] / n, 4) if n else None}
    return {"schema_version": 1, "report_sha256": packet["report_sha256"], "review_packet_sha256": digest(packet),
            "report_cases": len(report["cases"]), "n_cases": len(ids), "selected_case_ids": ids,
            "claim_units": total, "unreviewed_units": pending,
            "annotation_coverage": round((total - pending) / total, 4) if total else None,
            "uncited_reviewed_factual_claims": uncited, "by_reviewer_kind": metrics}
