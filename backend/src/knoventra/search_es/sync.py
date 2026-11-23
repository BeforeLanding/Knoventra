"""Keep the ES keyword index in step with PostgreSQL.

PostgreSQL is the only source of truth. Publishing writes an outbox row in the
same transaction; after commit, `drain` recomputes each touched document's
desired state from PG (chunks of its PUBLISHED revision) and makes ES match it.
Failures stay in the outbox with the error; `reconcile` compares every chunk id
in PG and ES and repairs both directions; `rebuild` reindexes into a fresh
physical index and swaps the alias.

    uv run python -m knoventra.search_es.sync drain | reconcile | rebuild
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from functools import lru_cache

from elasticsearch import Elasticsearch, helpers
from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from knoventra.config import settings
from knoventra.db.models import Chunk, Document, EsOutbox, Revision
from knoventra.ingestion.cases import models_from_text
from knoventra.ingestion.normalize import to_simplified
from knoventra.retrieval.identifiers import evidence_identifiers, normalize_identifiers
from knoventra.search_es.index import index_body, physical_name

log = logging.getLogger(__name__)


def get_client() -> Elasticsearch | None:
    """None when ES is not configured; callers then skip the keyword channel."""
    if not settings.es_url:
        return None
    return _client(settings.es_url, settings.es_timeout_s)


@lru_cache(maxsize=4)
def _client(url: str, timeout: float) -> Elasticsearch:
    return Elasticsearch(url, request_timeout=timeout, max_retries=0)


def _claim(db: Session, limit: int | None = None) -> list[tuple[int, str, dt.datetime]]:
    """Snapshot outbox rows (id, document_id, enqueued_at) BEFORE reading PG state."""
    stmt = select(EsOutbox.id, EsOutbox.document_id, EsOutbox.enqueued_at).order_by(EsOutbox.enqueued_at)
    return [tuple(r) for r in db.execute(stmt.limit(limit) if limit else stmt)]


def _release(db: Session, claimed: list[tuple[int, str, dt.datetime]]) -> int:
    """Delete only rows still holding the enqueued_at we read. A publish that re-enqueued the
    same document meanwhile has moved enqueued_at forward, so its row survives for the next
    drain; if that publish is still uncommitted, this DELETE waits for it and then skips it."""
    if not claimed:
        return 0
    return db.execute(delete(EsOutbox).where(
        tuple_(EsOutbox.id, EsOutbox.enqueued_at).in_([(i, at) for i, _, at in claimed]))).rowcount


def ensure_index(es: Elasticsearch, alias: str) -> str:
    """Create the physical index for the current mapping and point the alias at it if none exists."""
    if es.indices.exists_alias(name=alias):
        return next(iter(es.indices.get_alias(name=alias)))
    name = physical_name(alias)
    if not es.indices.exists(index=name):
        es.indices.create(index=name, **index_body())
    es.indices.put_alias(index=name, name=alias)
    return name


def enqueue(db: Session, document_id: str) -> None:
    """Record that a document's ES state must be recomputed. Call inside the publishing transaction."""
    db.execute(pg_insert(EsOutbox).values(document_id=document_id)
               .on_conflict_do_update(index_elements=["document_id"],
                                      set_={"enqueued_at": func.clock_timestamp()}))


def _es_doc(r) -> dict:
    text = normalize_identifiers(r.text_norm)  # text_norm is already OpenCC-simplified
    section = to_simplified(normalize_identifiers(r.section_path))
    return {
        "chunk_id": r.chunk_id, "document_id": r.document_id, "revision_id": r.revision_id, "kb_id": r.kb_id,
        "revision_status": r.status, "lang": r.source_lang,
        "machine_model": models_from_text(f"{section}\n{text}"),
        # Region is not indexed: KB grants already carry regional access (see plan 3.2).
        "region": [],
        "identifiers": sorted(evidence_identifiers(r.section_path, r.text)),
        "title": to_simplified(r.title), "section_path": section, "text": text,
        "published_at": r.published_at.isoformat() if r.published_at else None,
    }


def published_docs(db: Session, document_ids: list[str] | None = None) -> list[dict]:
    # Text columns only: never load the dense/sparse vectors just to build keyword documents.
    stmt = (select(Chunk.id.label("chunk_id"), Chunk.kb_id, Chunk.section_path, Chunk.text, Chunk.text_norm,
                   Revision.id.label("revision_id"), Revision.status, Revision.published_at,
                   Document.id.label("document_id"), Document.title, Document.source_lang)
            .join(Revision, Revision.id == Chunk.revision_id)
            .join(Document, Document.id == Revision.document_id)
            .where(Revision.status == "PUBLISHED"))
    if document_ids is not None:
        stmt = stmt.where(Document.id.in_(document_ids))
    return [_es_doc(r) for r in db.execute(stmt.order_by(Chunk.id))]


def _sync_documents(es: Elasticsearch, alias: str, db: Session, document_ids: list[str]) -> int:
    docs = published_docs(db, document_ids)
    actions = [{"_op_type": "index", "_index": alias, "_id": d["chunk_id"], "_source": d} for d in docs]
    if actions:
        helpers.bulk(es, actions, refresh=False)
    keep = [d["chunk_id"] for d in docs]
    es.delete_by_query(index=alias, refresh=True, conflicts="proceed", query={"bool": {
        "filter": [{"terms": {"document_id": document_ids}}],
        "must_not": [{"ids": {"values": keep}}] if keep else [],
    }})
    return len(docs)


@dataclass
class DrainResult:
    synced_documents: int = 0
    indexed_chunks: int = 0
    failed: list[str] = field(default_factory=list)


def drain(db: Session, es: Elasticsearch, alias: str | None = None, *, limit: int = 500) -> DrainResult:
    """Push queued documents to ES. Commits nothing itself; removes rows only after ES accepted them."""
    alias = alias or settings.es_index
    claimed = _claim(db, limit)
    out = DrainResult()
    if not claimed:
        return out
    ids = sorted({doc for _, doc, _ in claimed})
    try:
        ensure_index(es, alias)
        out.indexed_chunks = _sync_documents(es, alias, db, ids)
    except Exception as exc:  # noqa: BLE001 - any ES failure keeps the outbox for reconcile
        log.warning("es sync failed for %d documents: %s", len(ids), exc)
        db.execute(update(EsOutbox).where(EsOutbox.document_id.in_(ids))
                   .values(attempts=EsOutbox.attempts + 1, last_error=str(exc)[:500]))
        out.failed = ids
        return out
    _release(db, claimed)
    out.synced_documents = len(ids)
    return out


def drain_after_commit(db: Session) -> None:
    """Best effort for request handlers: never raises; leftovers wait for reconcile."""
    es = get_client()
    if es is None:
        return
    try:
        drain(db, es)  # records its own failure on the outbox rows
        db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("post-commit es drain skipped: %s", exc)
        db.rollback()


def _es_ids(es: Elasticsearch, alias: str) -> set[str]:
    es.indices.refresh(index=alias)
    return {h["_id"] for h in helpers.scan(es, index=alias, query={"query": {"match_all": {}}}, _source=False)}


@dataclass
class ReconcileResult:
    missing_in_es: int = 0
    stale_in_es: int = 0
    outbox_cleared: int = 0


def reconcile(db: Session, es: Elasticsearch, alias: str | None = None) -> ReconcileResult:
    """Make ES equal to PG's published chunks in both directions, then clear the outbox."""
    alias = alias or settings.es_index
    ensure_index(es, alias)
    claimed = _claim(db)  # only events seen before the snapshot below are cleared
    docs = {d["chunk_id"]: d for d in published_docs(db)}
    present = _es_ids(es, alias)
    missing = [docs[i] for i in docs.keys() - present]
    stale = sorted(present - docs.keys())
    if missing:
        helpers.bulk(es, ({"_op_type": "index", "_index": alias, "_id": d["chunk_id"], "_source": d}
                          for d in missing))
    if stale:
        helpers.bulk(es, ({"_op_type": "delete", "_index": alias, "_id": i} for i in stale), raise_on_error=False)
    es.indices.refresh(index=alias)
    cleared = _release(db, claimed)
    return ReconcileResult(len(missing), len(stale), cleared)


def rebuild(db: Session, es: Elasticsearch, alias: str | None = None) -> str:
    """Reindex from PG into a new physical index, swap the alias atomically, then reconcile.

    Drains that ran during the bulk load wrote to the old index, which is deleted after
    the swap; the closing reconcile compares chunk ids (new revisions mean new ids) and
    repairs anything they changed."""
    alias = alias or settings.es_index
    name = physical_name(alias, dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S") + f"{time.time_ns() % 1000:03d}")
    es.indices.create(index=name, **index_body())
    helpers.bulk(es, ({"_op_type": "index", "_index": name, "_id": d["chunk_id"], "_source": d}
                      for d in published_docs(db)), refresh=True)
    old = list(es.indices.get_alias(name=alias)) if es.indices.exists_alias(name=alias) else []
    es.indices.update_aliases(actions=[{"remove": {"index": o, "alias": alias}} for o in old]
                              + [{"add": {"index": name, "alias": alias}}])
    for o in old:
        es.indices.delete(index=o)
    reconcile(db, es, alias)
    return name


def main() -> None:
    from knoventra.db.session import SessionLocal

    ap = argparse.ArgumentParser(description="Sync the Elasticsearch keyword index from PostgreSQL.")
    ap.add_argument("command", choices=["drain", "reconcile", "rebuild"])
    args = ap.parse_args()
    es = get_client()
    if es is None:
        raise SystemExit("KNV_ES_URL is not set")
    with SessionLocal.begin() as db:
        if args.command == "drain":
            r = drain(db, es)
            print(json.dumps(r.__dict__))
        elif args.command == "reconcile":
            print(json.dumps(reconcile(db, es).__dict__))
        else:
            print(f"alias {settings.es_index} -> {rebuild(db, es)}")


if __name__ == "__main__":
    main()
