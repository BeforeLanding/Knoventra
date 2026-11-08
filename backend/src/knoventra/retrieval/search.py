from __future__ import annotations

from dataclasses import dataclass, field
import math

from sqlalchemy import text
from sqlalchemy.orm import Session

from knoventra.access.scope import AccessScope, NoAccess
from knoventra.config import settings
from knoventra.ingestion.encoder import Encoder, encode_checked
from knoventra.ingestion.normalize import to_simplified
from knoventra.retrieval.identifiers import evidence_identifiers, extract_query_identifiers, normalize_query_identifiers

QUERY_VECTOR_POLICY = "float32-finite-v1"
QUERY_ENCODING_POLICY = "distinct-batch-v1"

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
    required_identifiers: list[str] = field(default_factory=list)
    missing_identifiers: list[str] = field(default_factory=list)


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
           use_sparse: bool = True, use_opencc: bool = True, top_k: int | None = None,
           original_query: str = "") -> SearchResult:
    """Permission-aware hybrid retrieval. The readable-KB filter is applied inside both
    recall SQLs, so unauthorised chunks never reach fusion, rerank or the model."""
    if not scope.readable_kb_ids:
        raise NoAccess("empty scope")
    readable = sorted(scope.readable_kb_ids)
    k = settings.recall_top_k
    recall_query = normalize_query_identifiers(query)
    # Encode each required text once. Dense keeps the original script; only the
    # sparse arm uses OpenCC, matching ingestion's two text representations.
    texts = [recall_query]
    if use_sparse and use_opencc:
        simplified = to_simplified(recall_query)
        if simplified != recall_query:
            texts.append(simplified)
    encoded = encode_checked(enc, texts)
    dense_q = encoded[0][0]
    sparse_q = encoded[-1][1] if use_sparse else {}

    from pgvector import SparseVector, Vector

    dense_vector = Vector(list(dense_q))
    dense_usable = any(dense_vector.to_list())
    sparse_vector = None
    if use_sparse and sparse_q:
        weights = Vector(list(sparse_q.values())).to_list()
        sparse_vector = SparseVector({index: weight for index, weight in zip(sparse_q, weights, strict=True) if weight},
                                     settings.sparse_dim)
    sparse_usable = sparse_vector is not None and bool(sparse_vector.values())
    if dense_usable or sparse_usable:
        db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows: dict[str, tuple] = {}
    dense_sims: dict[str, float] = {}
    sparse_ips: dict[str, float] = {}
    rankings: dict[str, list[str]] = {"dense": []}
    unscorable: dict[str, list[str]] = {}

    def run(name, stmt, q):
        got = db.execute(stmt, {"readable": readable, "q": q, "k": k}).all()
        rankings[name] = []
        for r in got:
            distance = float(r.dist) if r.dist is not None else math.nan
            if not math.isfinite(distance):
                unscorable.setdefault(f"{name}_unscorable", []).append(r.id)
                continue
            rankings[name].append(r.id)
            rows.setdefault(r.id, r)
            if name == "dense":
                dense_sims[r.id] = 1.0 - distance
            else:
                sparse_ips[r.id] = -distance  # pgvector's <#> is the negative inner product

    if dense_usable:
        run("dense", _DENSE, dense_vector.to_text())
    if sparse_usable:
        run("sparse", _SPARSE, sparse_vector.to_text())

    fused = rrf(rankings, settings.rrf_k)
    order = sorted(fused, key=fused.get, reverse=True)
    evidence = []
    for cid in order:
        r = rows[cid]
        evidence.append(Evidence(
            chunk_id=cid, document_id=r.doc_id, document_title=r.title, revision=r.version,
            page=r.page, section_path=r.section_path, text=r.text, lang=r.source_lang,
            score=fused[cid], dense_sim=dense_sims.get(cid), sparse_ip=sparse_ips.get(cid),
            channels={n: ids.index(cid) + 1 for n, ids in rankings.items() if cid in ids},
        ))
    required = extract_query_identifiers(query) | extract_query_identifiers(original_query)
    stages = {**rankings, **unscorable, "fused": order}
    if required:
        # Filter before Top-K / rerank, so near-misses cannot crowd out an exact
        # match already present in the permission-filtered recall candidates.
        evidence = [e for e in evidence if required & evidence_identifiers(e.section_path, e.text)]
        stages["identifier_match"] = [e.chunk_id for e in evidence]
    evidence = evidence[: top_k or settings.final_top_k]
    stages["selected"] = [e.chunk_id for e in evidence]
    covered = frozenset().union(*(evidence_identifiers(e.section_path, e.text) for e in evidence))
    return SearchResult(evidence, stages, sorted(required), sorted(required - covered))
