from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from knoventra.db.models import Chunk, Document, Revision
from knoventra.ingestion.chunking import chunk_file
from knoventra.ingestion.encoder import Encoder, encode_checked
from knoventra.ingestion.normalize import to_simplified

TITLE_MAX_CHARS = 255
SUPPORTED_LANGUAGES = ("zh-Hans", "zh-Hant", "en")


class DocumentMetadataError(ValueError):
    pass


def validate_revision_metadata(db: Session, *, kb_id: str, title: str, lang: str) -> None:
    if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX_CHARS or "\x00" in title:
        raise DocumentMetadataError("title must be nonblank, at most 255 characters and contain no NUL")
    if lang not in SUPPORTED_LANGUAGES:
        raise DocumentMetadataError("language must be zh-Hans, zh-Hant or en")
    # This preflight must not flush unrelated pending writes or acquire document locks.
    with db.no_autoflush:
        existing_lang = db.scalar(select(Document.source_lang).where(Document.kb_id == kb_id, Document.title == title))
    if existing_lang is not None and existing_lang != lang:
        raise DocumentMetadataError("revision language must match the existing document")


def add_revision(db: Session, enc: Encoder, *, kb_id: str, title: str, lang: str, path: Path) -> Revision:
    """Parse, encode and store a new DRAFT revision. Nothing is searchable until `publish`."""
    validate_revision_metadata(db, kb_id=kb_id, title=title, lang=lang)
    chunks = chunk_file(path)
    if not chunks:
        raise ValueError("document contains no extractable text")
    norms = [to_simplified(c.text) for c in chunks]
    # Dense encoding keeps original glyphs; sparse encoding shares a simplified vocabulary.
    dense = encode_checked(enc, [c.text for c in chunks])
    sparse = encode_checked(enc, norms)
    # Encode before acquiring the lock; an expensive/failed encoder must not
    # block other uploads or mutate a document. Unique identity handles first uploads.
    db.execute(pg_insert(Document).values(kb_id=kb_id, title=title, source_lang=lang)
               .on_conflict_do_nothing(index_elements=["kb_id", "title"]))
    doc = db.scalar(select(Document).where(Document.kb_id == kb_id, Document.title == title).with_for_update())
    if doc.source_lang != lang:
        raise DocumentMetadataError("revision language must match the existing document")
    last = db.scalar(
        select(Revision.version).where(Revision.document_id == doc.id).order_by(Revision.version.desc()).limit(1)
    )
    rev = Revision(
        document_id=doc.id,
        version=(last or 0) + 1,
        status="DRAFT",
        content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    db.add(rev)
    db.flush()
    for c, n, (d, _), (_, s) in zip(chunks, norms, dense, sparse, strict=True):
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
    doc_id = db.scalar(select(Revision.document_id).where(Revision.id == revision_id))
    if doc_id is None:
        raise ValueError("revision not publishable")
    # Every publisher of a document must wait on the same parent row, even
    # when publishing different draft revisions. Re-read stale ORM state after waiting.
    db.execute(select(Document.id).where(Document.id == doc_id).with_for_update())
    rev = db.scalar(select(Revision).where(Revision.id == revision_id).execution_options(populate_existing=True))
    if rev is None or rev.status not in {"DRAFT", "PUBLISHED"}:
        raise ValueError("revision not publishable")
    if rev.status == "PUBLISHED":
        return  # Idempotent retry must not change the publication timestamp.
    if db.scalar(select(Chunk.id).where(Chunk.revision_id == rev.id).limit(1)) is None:
        raise ValueError("revision contains no chunks")
    db.execute(
        update(Revision)
        .where(Revision.document_id == rev.document_id, Revision.status == "PUBLISHED")
        .values(status="RETIRED")
    )
    rev.status = "PUBLISHED"
    rev.published_at = dt.datetime.now(dt.UTC)
    db.flush()
