"""Full retrieval: scope -> hybrid recall -> (rerank) -> evidence gate."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from knoventra.access.scope import AccessScope, NoAccess, compute_scope
from knoventra.config import settings
from knoventra.db.models import User
from knoventra.ingestion.encoder import Encoder
from knoventra.retrieval.rerank import Reranker, rerank
from knoventra.retrieval.search import Evidence, search

OK, NO_ACCESS, INSUFFICIENT = "OK", "NO_ACCESS", "INSUFFICIENT_EVIDENCE"


@dataclass
class Outcome:
    status: str
    evidence: list[Evidence] = field(default_factory=list)


def gate_score(e: Evidence) -> float:
    """Rerank score when a reranker ran; else the dense cosine similarity.

    RRF is rank-based, so with BGE-M3 sparse (which matches any shared token) every query puts some chunk
    at rank 1 in both channels and the fused score cannot tell an answerable query from an unanswerable one.
    """
    if e.rerank_score is not None:
        return e.rerank_score
    return e.dense_sim if e.dense_sim is not None else 0.0


def retrieve(db: Session, enc: Encoder, rr: Reranker | None, user: User, query: str, *,
             threshold: float | None = None, use_sparse: bool = True, use_opencc: bool = True) -> Outcome:
    try:
        scope: AccessScope = compute_scope(db, user)
    except NoAccess:
        return Outcome(NO_ACCESS)
    n = settings.rerank_candidates if rr else settings.final_top_k
    ev = search(db, enc, scope, query, use_sparse=use_sparse, use_opencc=use_opencc, top_k=n).evidence
    if rr:
        ev = rerank(rr, query, ev, settings.final_top_k)
    th = settings.gate_threshold if threshold is None else threshold
    if not ev or max(gate_score(e) for e in ev) < th:
        return Outcome(INSUFFICIENT)
    return Outcome(OK, ev)
