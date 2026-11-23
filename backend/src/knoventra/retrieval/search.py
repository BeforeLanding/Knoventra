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
from knoventra.search_es.query import default_keyword_channel

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
    degraded: list[str] = field(default_factory=list)  # e.g. ["es_unavailable"]


_AUTO = object()  # search(keyword=...) default: the configured ES channel, if any

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


# Candidates proposed by the keyword channels are re-read here: the row must still be
# PUBLISHED and in a readable KB, whatever ES claims. Scores are recomputed so the
# evidence gate treats every channel's candidates alike.
_RECHECK = """
SELECT c.id, (c.dense <=> CAST(:dq AS vector)) AS ddist, (c.sparse <#> CAST(:sq AS sparsevec)) AS sdist,
       d.id AS doc_id, d.title, r.version, c.page, c.section_path, c.text, d.source_lang
FROM knv_chunk c
JOIN knv_revision r ON r.id = c.revision_id AND r.status = 'PUBLISHED'
JOIN knv_document d ON d.id = r.document_id
WHERE c.kb_id = ANY(:readable) AND {where}
"""
_BY_IDS = text(_RECHECK.format(where="c.id = ANY(:ids)"))
_BY_IDENTIFIER = text(_RECHECK.format(where="c.identifiers && CAST(:codes AS varchar[])")
                      + " ORDER BY ddist NULLS LAST, c.id LIMIT :k")


def rrf(rankings: dict[str, list[str]], k: int, weights: dict[str, float] | None = None) -> dict[str, float]:
    """Weighted reciprocal rank fusion: sum over channels of w_c / (k + rank_c); missing weight = 1."""
    weights = weights or {}
    scores: dict[str, float] = {}
    for name, ids in rankings.items():
        w = weights.get(name, 1.0)
        for rank, cid in enumerate(ids, start=1):
            scores[cid] = scores.get(cid, 0.0) + w / (k + rank)
    return scores


def channel_weights(has_identifiers: bool) -> dict[str, float]:
    if settings.rrf_weighting == "equal":
        return {}
    return dict(settings.rrf_weights_identifier if has_identifiers else settings.rrf_weights_natural)


def search(db: Session, enc: Encoder, scope: AccessScope, query: str, *,
           use_sparse: bool = True, use_opencc: bool = True, top_k: int | None = None,
           original_query: str = "", keyword=_AUTO, use_ident: bool = True) -> SearchResult:
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
    required = extract_query_identifiers(query) | extract_query_identifiers(original_query)
    degraded: list[str] = []
    extra_stages: dict[str, list[str]] = {}

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

    score_args = {"dq": dense_vector.to_text() if dense_usable else None,
                  "sq": sparse_vector.to_text() if sparse_usable else None}

    def adopt(name: str, got, order: list[str] | None = None) -> None:
        by_id = {r.id: r for r in got}
        ids = [i for i in (order or [r.id for r in got]) if i in by_id]
        rankings[name] = ids
        for cid in ids:
            r = by_id[cid]
            rows.setdefault(cid, r)
            if r.ddist is not None and math.isfinite(float(r.ddist)):
                dense_sims.setdefault(cid, 1.0 - float(r.ddist))
            if r.sdist is not None and math.isfinite(float(r.sdist)) and float(r.sdist) < 0:
                sparse_ips.setdefault(cid, -float(r.sdist))

    if required and use_ident:
        adopt("ident", db.execute(_BY_IDENTIFIER, {"readable": readable, "codes": sorted(required), "k": k,
                                                   **score_args}).all())
    channel = default_keyword_channel() if keyword is _AUTO else keyword
    if channel is not None:
        try:
            # The executed query (a rewrite or sub-query), not the whole user message;
            # codes from the user's message still arrive through `required`.
            es_ids = channel.search(query, readable, sorted(required), k)
        except Exception as exc:  # noqa: BLE001 - any ES failure degrades to the pgvector channels
            degraded.append("es_unavailable")
            extra_stages["es_error"] = [type(exc).__name__]
        else:
            got = db.execute(_BY_IDS, {"readable": readable, "ids": es_ids, **score_args}).all() if es_ids else []
            adopt("es", got, es_ids)
            rejected = [i for i in es_ids if i not in set(rankings["es"])]
            if rejected:
                extra_stages["es_rejected"] = rejected  # stale or unreadable in PG: never fused

    fused = rrf(rankings, settings.rrf_k, channel_weights(bool(required)))
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
    stages = {**rankings, **unscorable, **extra_stages, "fused": order}
    if degraded:
        stages["degraded"] = degraded
    if required:
        # Filter before Top-K / rerank, so near-misses cannot crowd out an exact
        # match already present in the permission-filtered recall candidates.
        evidence = [e for e in evidence if required & evidence_identifiers(e.section_path, e.text)]
        stages["identifier_match"] = [e.chunk_id for e in evidence]
    evidence = evidence[: top_k or settings.final_top_k]
    stages["selected"] = [e.chunk_id for e in evidence]
    covered = frozenset().union(*(evidence_identifiers(e.section_path, e.text) for e in evidence))
    return SearchResult(evidence, stages, sorted(required), sorted(required - covered), degraded)
