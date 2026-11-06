"""Full retrieval: scope -> hybrid recall -> (rerank) -> evidence gate."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from knoventra.access.scope import AccessScope, NoAccess, compute_scope
from knoventra.config import settings
from knoventra.db.models import User
from knoventra.ingestion.encoder import Encoder
from knoventra.retrieval.identifiers import evidence_identifiers
from knoventra.retrieval.rerank import Reranker, rerank
from knoventra.retrieval.search import Evidence, search

OK, NO_ACCESS, INSUFFICIENT = "OK", "NO_ACCESS", "INSUFFICIENT_EVIDENCE"


@dataclass
class Outcome:
    status: str
    evidence: list[Evidence] = field(default_factory=list)
    reason: str | None = None
    required_identifiers: list[str] = field(default_factory=list)
    missing_identifiers: list[str] = field(default_factory=list)
    ranked_evidence: list[Evidence] = field(default_factory=list)  # diagnostics only: final candidates before score gate


def gate_passes(e: Evidence, threshold: float) -> bool:
    """Evidence gate.

    With a reranker: its score must reach `threshold`. Without one: dense cosine >= `threshold` OR sparse
    inner product >= `gate_sparse_threshold`. The sparse arm exists because exact fault codes / part numbers
    score low on dense similarity but hit the lexical channel (holdout: 3 answerable questions were refused).
    Exact identifier presence is checked separately; similarity alone cannot
    distinguish a real code from a near-miss.
    """
    if e.rerank_score is not None:
        return e.rerank_score >= threshold
    return (e.dense_sim or 0.0) >= threshold or (e.sparse_ip or 0.0) >= settings.gate_sparse_threshold


def retrieve(db: Session, enc: Encoder, rr: Reranker | None, user: User, query: str, *,
             threshold: float | None = None, use_sparse: bool = True, use_opencc: bool = True,
             original_query: str = "") -> Outcome:
    try:
        scope: AccessScope = compute_scope(db, user)
    except NoAccess:
        return Outcome(NO_ACCESS)
    n = settings.rerank_candidates if rr else settings.final_top_k
    res = search(db, enc, scope, query, use_sparse=use_sparse, use_opencc=use_opencc, top_k=n,
                 original_query=original_query)
    ev = res.evidence
    if rr:
        ev = rerank(rr, query, ev, settings.final_top_k)
    th = (settings.rerank_threshold if rr else settings.gate_threshold) if threshold is None else threshold
    diagnostics = {"required_identifiers": res.required_identifiers, "ranked_evidence": ev}
    required = set(res.required_identifiers)
    covered = set().union(*(evidence_identifiers(e.section_path, e.text) for e in ev))
    missing = sorted(required - covered)
    if missing:
        return Outcome(INSUFFICIENT, reason="IDENTIFIER_MISMATCH",
                       missing_identifiers=missing, **diagnostics)
    passing = [e for e in ev if gate_passes(e, th)]
    strong = set().union(*(evidence_identifiers(e.section_path, e.text) for e in passing))
    if not passing or required - strong:
        return Outcome(INSUFFICIENT, reason="LOW_SCORE", **diagnostics)
    return Outcome(OK, ev, **diagnostics)
