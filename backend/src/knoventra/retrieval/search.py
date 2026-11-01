from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from knoventra.access.scope import AccessScope, NoAccess
from knoventra.config import settings
from knoventra.ingestion.encoder import Encoder
from knoventra.ingestion.normalize import to_simplified


@dataclass
class Evidence:
    chunk_id: str
    document_id: str
    document_title: str
    revision: int
    page: int | None
    section_path: str
    text: str
    lang: str
    score: float
    rerank_score: float | None = None
    sparse_ip: float | None = None  # sparse inner product, if it was a sparse candidate
    dense_sim: float | None = None  # cosine similarity from the dense channel, if it was a dense candidate
    channels: dict[str, int] = field(default_factory=dict)  # channel -> rank (1-based)


@dataclass
class SearchResult:
    evidence: list[Evidence]
    stages: dict[str, list[str]]  # for /debug/search: chunk ids per stage


_BASE = """
SELECT c.id, {dist} AS dist, d.id AS doc_id, d.title, r.version, c.page, c.section_path, c.text, d.source_lang
FROM knv_chunk c
JOIN knv_revision r ON r.id = c.revision_id AND r.status = 'PUBLISHED'
JOIN knv_document d ON d.id = r.document_id
WHERE c.kb_id = ANY(:readable){extra}
ORDER BY {order}
LIMIT :k
"""
_DENSE = text(_BASE.format(order="c.dense <=> CAST(:q AS vector)", extra="", dist="c.dense <=> CAST(:q AS vector)"))
_SPARSE = text(_BASE.format(order="c.sparse <#> CAST(:q AS sparsevec)", dist="c.sparse <#> CAST(:q AS sparsevec)",
                         extra=" AND (c.sparse <#> CAST(:q AS sparsevec)) < 0"))  # no lexical overlap -> no candidate)


def rrf(rankings: dict[str, list[str]], k: int) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ids in rankings.values():
        for rank, cid in enumerate(ids, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return scores


def search(db: Session, enc: Encoder, scope: AccessScope, query: str, *,
           use_sparse: bool = True, use_opencc: bool = True, top_k: int | None = None) -> SearchResult:
    """Permission-aware hybrid retrieval. The readable-KB filter is applied inside both
    recall SQLs, so unauthorised chunks never reach fusion, rerank or the model."""
    if not scope.readable_kb_ids:
        raise NoAccess("empty scope")
    readable = sorted(scope.readable_kb_ids)
    k = settings.recall_top_k
    dense_q = enc.encode([query])[0][0]
    sparse_q = enc.encode([to_simplified(query) if use_opencc else query])[0][1]

    from pgvector import SparseVector

    db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows: dict[str, tuple] = {}
    dense_sims: dict[str, float] = {}
    sparse_ips: dict[str, float] = {}
    rankings: dict[str, list[str]] = {}

    def run(name, stmt, q):
        got = db.execute(stmt, {"readable": readable, "q": q, "k": k}).all()
        rankings[name] = [r.id for r in got]
        for r in got:
            rows.setdefault(r.id, r)
            if name == "dense":
                dense_sims[r.id] = 1.0 - float(r.dist)
            else:
                sparse_ips[r.id] = -float(r.dist)  # pgvector's <#> is the negative inner product

    run("dense", _DENSE, "[" + ",".join(f"{x:.6f}" for x in dense_q) + "]")
    if use_sparse and sparse_q:
        run("sparse", _SPARSE, SparseVector(sparse_q, settings.sparse_dim).to_text())

    fused = rrf(rankings, settings.rrf_k)
    order = sorted(fused, key=fused.get, reverse=True)[: top_k or settings.final_top_k]
    evidence = []
    for cid in order:
        r = rows[cid]
        evidence.append(Evidence(
            chunk_id=cid, document_id=r.doc_id, document_title=r.title, revision=r.version,
            page=r.page, section_path=r.section_path, text=r.text, lang=r.source_lang,
            score=fused[cid], dense_sim=dense_sims.get(cid), sparse_ip=sparse_ips.get(cid),
            channels={n: ids.index(cid) + 1 for n, ids in rankings.items() if cid in ids},
        ))
    return SearchResult(evidence, {**rankings, "fused": order})
