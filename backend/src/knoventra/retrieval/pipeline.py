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


def gate_passes(e: Evidence, threshold: float) -> bool:
    """Evidence gate.

    With a reranker: its score must reach `threshold`. Without one: dense cosine >= `threshold` OR sparse
    inner product >= `gate_sparse_threshold`. The sparse arm exists because exact fault codes / part numbers
    score low on dense similarity but hit the lexical channel (holdout: 3 answerable questions were refused).
    A score gate cannot tell a real code from a near-miss (E17-42 vs E18-42 share every token), so near-misses
    pass and are refused by the model, not the gate.
    """
    if e.rerank_score is not None:
        return e.rerank_score >= threshold
    return (e.dense_sim or 0.0) >= threshold or (e.sparse_ip or 0.0) >= settings.gate_sparse_threshold


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
    if not ev or not any(gate_passes(e, th) for e in ev):
        return Outcome(INSUFFICIENT)
    return Outcome(OK, ev)
