"""Hybrid recall: exact-code channel, ES keyword channel, PG back-check, degrade, weighted RRF."""
import pytest
from sqlalchemy import select

from knoventra.access.scope import compute_scope
from knoventra.config import settings
from knoventra.db.models import Chunk, Document, KnowledgeBase, Revision
from knoventra.retrieval.search import channel_weights, rrf, search


def test_weighted_rrf_and_query_type_weights(monkeypatch):
    rankings = {"dense": ["a", "b"], "ident": ["b"]}
    scores = rrf(rankings, 60, {"dense": 0.5, "ident": 2.0})
    assert scores["a"] == pytest.approx(0.5 / 61)
    assert scores["b"] == pytest.approx(0.5 / 62 + 2.0 / 61)
    assert rrf(rankings, 60) == rrf(rankings, 60, {})  # missing weights mean 1.0
    assert channel_weights(True) == settings.rrf_weights_identifier
    assert channel_weights(False) == settings.rrf_weights_natural
    monkeypatch.setattr(settings, "rrf_weighting", "equal")
    assert channel_weights(True) == {}


def test_exact_code_channel_recalls_through_a_crowded_corpus(db, enc, users):
    scope = compute_scope(db, users["sg-ops"])
    res = search(db, enc, scope, "SOP-WCL-380120-002 扭矩多少", keyword=None)
    assert res.stages["ident"] and not res.missing_identifiers
    titles = {e.document_title for e in res.evidence}
    assert titles == {"SOP-WCL-380120-002 Spindle 更换"}


def test_exact_code_channel_respects_permissions(db, enc, users):
    scope = compute_scope(db, users["sg-ops"])
    res = search(db, enc, scope, "C21-05", keyword=None)  # only in the CN-restricted KB
    assert res.stages.get("ident") == [] and res.missing_identifiers == ["C21-05"]


@pytest.fixture
def keyword(db, es, alias):
    from knoventra.search_es.query import KeywordChannel
    from knoventra.search_es.sync import reconcile

    reconcile(db, es, alias)
    return KeywordChannel(es, alias)


def _titles(res):
    return [e.document_title for e in res.evidence]


@pytest.mark.parametrize("query, title", [
    ("mix tank trouble shotting", "SOP-WCL-380110-001 Mix tank 液位报警与 interlock 复位"),
    ("chamical flow 流量设定", "SOP-FAC-100110-001 Chemical flow 调试"),
    ("主轴更换扭矩", "SOP-WCL-380120-002 Spindle 更换"),
])
def test_keyword_channel_handles_typos_and_synonyms(db, enc, users, keyword, query, title):
    res = search(db, enc, compute_scope(db, users["sg-ops"]), query, keyword=keyword)
    assert res.stages["es"], "ES returned nothing"
    first_es = db.scalar(select(Document.title).join(Revision, Revision.document_id == Document.id)
                         .join(Chunk, Chunk.revision_id == Revision.id).where(Chunk.id == res.stages["es"][0]))
    assert first_es == title
    assert title in _titles(res)


def _plant(es, alias, chunk: Chunk, kb_id: str, text: str):
    es.index(index=alias, id=chunk.id, refresh=True, document={
        "chunk_id": chunk.id, "document_id": "x", "revision_id": chunk.revision_id, "kb_id": kb_id,
        "revision_status": "PUBLISHED", "text": text, "identifiers": [], "machine_model": [], "region": []})


def test_back_check_drops_stale_revisions_and_unreadable_chunks(db, enc, users, es, alias, keyword):
    mixtank = db.scalar(select(Document).where(Document.title.like("SOP-WCL-380110-001%")))
    draft = db.scalar(select(Chunk).join(Revision, Revision.id == Chunk.revision_id)
                      .where(Revision.document_id == mixtank.id, Revision.status == "DRAFT"))
    cn_kb = db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == "ops-cn"))
    secret = db.scalar(select(Chunk).where(Chunk.kb_id == cn_kb))
    readable = db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == "field-sop"))
    # ES claims both are published and readable; PostgreSQL disagrees.
    _plant(es, alias, draft, readable, "zqxj 残留旧修订")
    _plant(es, alias, secret, readable, "zqxj 越权分块")

    res = search(db, enc, compute_scope(db, users["sg-ops"]), "zqxj", keyword=keyword)
    assert set(res.stages["es_rejected"]) == {draft.id, secret.id}
    assert draft.id not in res.stages["fused"] and secret.id not in res.stages["fused"]
    assert all(e.chunk_id not in {draft.id, secret.id} for e in res.evidence)


def test_es_outage_degrades_without_widening_scope(db, enc, users):
    from elasticsearch import Elasticsearch

    from knoventra.search_es.query import KeywordChannel

    down = KeywordChannel(Elasticsearch("http://127.0.0.1:1", request_timeout=0.5, max_retries=0), "knv_none")
    scope = compute_scope(db, users["sg-ops"])
    res = search(db, enc, scope, "E17-07 如何处理", keyword=down)
    assert res.degraded == ["es_unavailable"] and res.stages["es_error"]
    assert res.evidence and not res.missing_identifiers
    kb_of = {c.id: c.kb_id for c in db.scalars(select(Chunk).where(Chunk.id.in_(res.stages["fused"])))}
    assert set(kb_of.values()) <= scope.readable_kb_ids


def test_pipeline_reports_degraded(db, enc, users, monkeypatch):
    from knoventra.retrieval import pipeline
    from knoventra.retrieval.search import SearchResult

    monkeypatch.setattr(pipeline, "search", lambda *a, **kw: SearchResult([], {}, degraded=["es_unavailable"]))
    out = pipeline.retrieve(db, enc, None, users["sg-ops"], "anything")
    assert out.status == pipeline.INSUFFICIENT and out.degraded == ["es_unavailable"]
