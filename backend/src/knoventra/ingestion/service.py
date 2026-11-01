from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from knoventra.db.models import Chunk, Document, Revision
from knoventra.ingestion.chunking import chunk_file
from knoventra.ingestion.encoder import Encoder
from knoventra.ingestion.normalize import to_simplified


def add_revision(db: Session, enc: Encoder, *, kb_id: str, title: str, lang: str, path: Path) -> Revision:
    """Parse, encode and store a new DRAFT revision. Nothing is searchable until `publish`."""
    doc = db.scalar(select(Document).where(Document.kb_id == kb_id, Document.title == title))
    if doc is None:
        doc = Document(kb_id=kb_id, title=title, source_lang=lang)
        db.add(doc)
        db.flush()
    last = db.scalar(
        select(Revision.version).where(Revision.document_id == doc.id).order_by(Revision.version.desc()).limit(1)
    )
    chunks = chunk_file(path)
    rev = Revision(
        document_id=doc.id,
        version=(last or 0) + 1,
        status="DRAFT",
        content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    db.add(rev)
    db.flush()
    norms = [to_simplified(c.text) for c in chunks]
    # encode the normalised text so Traditional/Simplified share one sparse vocabulary;
    # the dense channel gets the original glyphs (BGE-M3 is cross-lingual).
    dense = enc.encode([c.text for c in chunks])
    sparse = enc.encode(norms)
    for c, n, (d, _), (_, s) in zip(chunks, norms, dense, sparse):
        db.add(Chunk(revision_id=rev.id, kb_id=kb_id, page=c.page, section_path=c.section_path,
                     text=c.text, text_norm=n, dense=d, sparse=_sparse(s)))
    db.flush()
    return rev


def _sparse(s: dict[int, float]):
    from pgvector import SparseVector

    from knoventra.config import settings

    return SparseVector(s, settings.sparse_dim)


def publish(db: Session, revision_id: str) -> None:
    """Atomically retire the old PUBLISHED revision and publish this one.

    Runs inside the caller's transaction; any failure rolls both changes back, so
    the previous version stays effective.
    """
    rev = db.get(Revision, revision_id)
    if rev is None or rev.status == "RETIRED":
        raise ValueError("revision not publishable")
    db.execute(
        update(Revision)
        .where(Revision.document_id == rev.document_id, Revision.status == "PUBLISHED")
        .values(status="RETIRED")
    )
    rev.status = "PUBLISHED"
    rev.published_at = dt.datetime.now(dt.UTC)
    db.flush()
