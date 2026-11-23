"""Elasticsearch keyword index: analysis, outbox sync, reconcile and rebuild.

Needs the compose `es` service (KNV_TEST_ES_URL, default http://localhost:59200).
Every test uses its own alias and deletes its indices afterwards.
"""
import pytest
from sqlalchemy import func, select

from knoventra.db.models import Chunk, Document, EsOutbox, Revision


def _tokens(es, index, analyzer, text):
    return [t["token"] for t in es.indices.analyze(index=index, analyzer=analyzer, text=text)["tokens"]]


def _published_chunk_ids(db, document_id=None):
    stmt = select(Chunk.id).join(Revision, Revision.id == Chunk.revision_id).where(Revision.status == "PUBLISHED")
    if document_id:
        stmt = stmt.where(Revision.document_id == document_id)
    return set(db.scalars(stmt))


def _es_ids(es, alias, **filters):
    es.indices.refresh(index=alias)
    query = {"bool": {"filter": [{"term": {k: v}} for k, v in filters.items()]}} if filters else {"match_all": {}}
    return {h["_id"] for h in es.search(index=alias, query=query, size=1000, source=False)["hits"]["hits"]}


def test_typos_are_fixed_on_both_sides_and_synonyms_expand_queries(es, alias):
    from knoventra.search_es.sync import ensure_index

    index = ensure_index(es, alias)
    assert "shooting" in _tokens(es, index, "knv_index", "DI flow trouble shotting")
    assert "chemical" in _tokens(es, index, "knv_search", "chamical flow 调试")
    assert {"alarm", "报警", "告警"} <= set(_tokens(es, index, "knv_search", "告警"))
    assert "SOP-WCL-380110-001".lower() in _tokens(es, index, "knv_index", "按 SOP-WCL-380110-001 第 4 节")


def test_publish_enqueues_in_the_same_transaction(db, enc, tmp_path):
    from knoventra.ingestion.service import add_revision, publish

    kb = db.scalar(select(Document.kb_id).limit(1))
    path = tmp_path / "doc.md"
    path.write_text("# 测试\n## 一\nalarm A-9001 处理步骤", encoding="utf-8")
    with db.begin_nested() as sp:
        rev = add_revision(db, enc, kb_id=kb, title="outbox-rollback", lang="zh-Hans", path=path)
        publish(db, rev.id)
        assert db.scalar(select(EsOutbox).where(EsOutbox.document_id == rev.document_id))
        sp.rollback()
    assert db.scalar(select(func.count()).select_from(EsOutbox).join(
        Document, Document.id == EsOutbox.document_id).where(Document.title == "outbox-rollback")) == 0


def test_drain_indexes_only_the_published_revision(db, enc, es, alias):
    from knoventra.ingestion.service import publish
    from knoventra.search_es.sync import drain, enqueue

    for doc_id in db.scalars(select(Document.id)):
        enqueue(db, doc_id)
    result = drain(db, es, alias)
    assert not result.failed and result.synced_documents >= 10
    mixtank = db.scalar(select(Document).where(Document.title.like("SOP-WCL-380110-001%")))
    assert _es_ids(es, alias, document_id=mixtank.id) == _published_chunk_ids(db, mixtank.id)
    assert db.scalar(select(func.count()).select_from(EsOutbox)) == 0

    # Publishing v1 again retires v2; after a drain ES must hold v1's chunks and none of v2's.
    v1, v2 = db.scalars(select(Revision.id).where(Revision.document_id == mixtank.id).order_by(Revision.version))
    publish(db, v1)
    drain(db, es, alias)
    v1_chunks = set(db.scalars(select(Chunk.id).where(Chunk.revision_id == v1)))
    v2_chunks = set(db.scalars(select(Chunk.id).where(Chunk.revision_id == v2)))
    assert _es_ids(es, alias, document_id=mixtank.id) == v1_chunks
    assert not v2_chunks & _es_ids(es, alias)

    hits = es.search(index=alias, query={"term": {"identifiers": "sop-wcl-380120-002"}}, size=50)["hits"]["hits"]
    assert hits and all(h["_source"]["title"].startswith("SOP-WCL-380120-002") for h in hits)
    spindle = {m for h in hits for m in h["_source"]["machine_model"]}
    assert {"WX-38", "WX-P"} <= spindle


def test_es_outage_keeps_the_outbox_and_reconcile_repairs_both_directions(db, enc, es, alias):
    from elasticsearch import Elasticsearch

    from knoventra.search_es.sync import drain, enqueue, ensure_index, reconcile

    ensure_index(es, alias)
    doc = db.scalar(select(Document).where(Document.title == "泵组维护手册"))
    enqueue(db, doc.id)
    down = Elasticsearch("http://127.0.0.1:1", request_timeout=0.5, max_retries=0)
    failed = drain(db, down, alias)
    row = db.scalar(select(EsOutbox).where(EsOutbox.document_id == doc.id))
    assert doc.id in failed.failed and row.attempts >= 1 and row.last_error

    es.index(index=alias, id="stale-chunk", document={"chunk_id": "stale-chunk", "document_id": doc.id,
                                                    "kb_id": doc.kb_id, "revision_status": "PUBLISHED",
                                                    "text": "旧修订残留"}, refresh=True)
    fixed = reconcile(db, es, alias)
    assert fixed.stale_in_es == 1 and fixed.missing_in_es == len(_published_chunk_ids(db)) and fixed.outbox_cleared
    assert _es_ids(es, alias) == _published_chunk_ids(db)
    again = reconcile(db, es, alias)
    assert (again.missing_in_es, again.stale_in_es) == (0, 0)


def test_rebuild_swaps_the_alias_to_a_fresh_index(db, es, alias):
    from knoventra.search_es.sync import ensure_index, rebuild

    first = ensure_index(es, alias)
    second = rebuild(db, es, alias)
    assert second != first and list(es.indices.get_alias(name=alias)) == [second]
    assert not es.indices.exists(index=first)
    assert _es_ids(es, alias) == _published_chunk_ids(db)


def test_traditional_text_is_indexed_simplified(db, es, alias):
    from knoventra.search_es.sync import published_docs

    docs = [d for d in published_docs(db) if d["lang"] == "zh-Hant"]
    assert docs and all(d["text"] for d in docs)
    from knoventra.ingestion.normalize import to_simplified

    assert all(to_simplified(d["text"]) == d["text"] for d in docs)


def test_deleted_document_is_purged_from_es(db, es, alias):
    from sqlalchemy import delete

    from knoventra.search_es.sync import drain, enqueue, reconcile

    reconcile(db, es, alias)
    doc = db.scalar(select(Document).where(Document.title == "泵组维护手册"))
    assert _es_ids(es, alias, document_id=doc.id)
    revs = select(Revision.id).where(Revision.document_id == doc.id)
    db.execute(delete(Chunk).where(Chunk.revision_id.in_(revs)))
    db.execute(delete(Revision).where(Revision.document_id == doc.id))
    enqueue(db, doc.id)
    db.execute(delete(Document).where(Document.id == doc.id))
    assert not drain(db, es, alias).failed
    assert not _es_ids(es, alias, document_id=doc.id)
