from __future__ import annotations

import datetime as dt
import uuid

from pgvector.sqlalchemy import SPARSEVEC, Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from knoventra.config import settings


def _id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "knv_user"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    region: Mapped[str | None] = mapped_column(String(8))
    department: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | admin
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class KnowledgeBase(Base):
    __tablename__ = "knv_kb"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")


class KbGrant(Base):
    """A NULL column is a wildcard; any matching row makes the KB readable."""

    __tablename__ = "knv_kb_grant"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knv_kb.id"), index=True)
    region: Mapped[str | None] = mapped_column(String(8))
    department: Mapped[str | None] = mapped_column(String(32))
    role: Mapped[str | None] = mapped_column(String(16))


class Document(Base):
    __tablename__ = "knv_document"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knv_kb.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    source_lang: Mapped[str] = mapped_column(String(8))  # zh-Hans | zh-Hant | en
    __table_args__ = (UniqueConstraint("kb_id", "title", name="uq_knv_document_kb_title"),)


class Revision(Base):
    __tablename__ = "knv_revision"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("knv_document.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")  # DRAFT|PUBLISHED|RETIRED
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("document_id", "version"),)


class Chunk(Base):
    __tablename__ = "knv_chunk"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    revision_id: Mapped[str] = mapped_column(ForeignKey("knv_revision.id"), index=True)
    # Denormalised so the permission pre-filter needs only one indexed predicate.
    kb_id: Mapped[str] = mapped_column(ForeignKey("knv_kb.id"), index=True)
    page: Mapped[int | None] = mapped_column(Integer)
    section_path: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text)
    text_norm: Mapped[str] = mapped_column(Text)
    dense = mapped_column(Vector(settings.dense_dim))
    sparse = mapped_column(SPARSEVEC(settings.sparse_dim))


class Conversation(Base):
    __tablename__ = "knv_conversation"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("knv_user.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String(64), unique=True, default=_id)
    title: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "knv_message"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("knv_conversation.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[list | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    # now() is fixed at transaction start, so multiple messages can receive the
    # same timestamp and replay in random UUID order. Capture insertion time.
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class Confirmation(Base):
    __tablename__ = "knv_confirmation"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("knv_user.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String(64))
    operation_id: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING|APPROVED|REJECTED|EXPIRED
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Escalation(Base):
    __tablename__ = "knv_escalation"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("knv_user.id"), index=True)
    operation_id: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    evidence_refs: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    __table_args__ = (UniqueConstraint("user_id", "operation_id"),)


Index("ix_knv_chunk_dense", Chunk.dense, postgresql_using="hnsw",
      postgresql_with={"m": 16, "ef_construction": 64},
      postgresql_ops={"dense": "vector_cosine_ops"})
Index("ix_knv_chunk_sparse", Chunk.sparse, postgresql_using="hnsw",
      postgresql_with={"m": 16, "ef_construction": 64},
      postgresql_ops={"sparse": "sparsevec_ip_ops"})
Index("uq_knv_revision_published", Revision.document_id, unique=True,
      postgresql_where=Revision.status == "PUBLISHED")
