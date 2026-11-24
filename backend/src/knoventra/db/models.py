from __future__ import annotations

import datetime as dt
import uuid

from pgvector.sqlalchemy import SPARSEVEC, Vector
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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
    __table_args__ = (UniqueConstraint("engineer_name", name="knv_user_engineer_name_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    region: Mapped[str | None] = mapped_column(String(8))
    department: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | admin
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Links a login to its row in the skill matrix (knv_skill.engineer_name); NULL for non-engineers.
    engineer_name: Mapped[str | None] = mapped_column(String(128))


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
    # Canonical codes in section path + text (retrieval.identifiers); exact-code recall channel.
    identifiers: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list, server_default="{}")


class Conversation(Base):
    __tablename__ = "knv_conversation"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("knv_user.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String(64), unique=True, default=_id)
    title: Mapped[str] = mapped_column(String(255), default="")
    active_turn_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "knv_message"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("knv_conversation.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[list | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    completion_turn_id: Mapped[str | None] = mapped_column(String(32))
    __table_args__ = (UniqueConstraint("conversation_id", "completion_turn_id", name="uq_knv_message_completion"),)
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
    # What submit files: kind, slots, evidence refs, recommendation (D13). NULL on older rows.
    payload: Mapped[dict | None] = mapped_column(JSON)


HELP_STATUSES = ("OPEN", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "CLOSED", "CANCELLED")


class HelpRequest(Base):
    """A help request. v3 expert escalations are rows of kind `expert_escalation` (table renamed in 0011)."""

    __tablename__ = "knv_help_request"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("knv_user.id"), index=True)
    operation_id: Mapped[str] = mapped_column(String(64))  # idempotency key per requester
    kind: Mapped[str] = mapped_column(String(24), default="expert_escalation", server_default="expert_escalation")
    summary: Mapped[str] = mapped_column(Text)
    evidence_refs: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    machine_id: Mapped[str | None] = mapped_column(String(64))
    machine_model: Mapped[str | None] = mapped_column(String(16))
    alarm_codes: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    symptom: Mapped[str] = mapped_column(Text, default="", server_default="")
    tried_steps: Mapped[str] = mapped_column(Text, default="", server_default="")
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("knv_user.id"), index=True)
    resolution: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    case_document_id: Mapped[str | None] = mapped_column(ForeignKey("knv_document.id"))  # draft/published case
    __table_args__ = (
        UniqueConstraint("user_id", "operation_id", name="knv_help_request_user_id_operation_id_key"),
        CheckConstraint("status IN ('OPEN', 'ASSIGNED', 'IN_PROGRESS', 'RESOLVED', 'CLOSED', 'CANCELLED')",
                        name="ck_knv_help_request_status"),
        CheckConstraint("kind IN ('expert_escalation', 'field_help')", name="ck_knv_help_request_kind"),
    )


Escalation = HelpRequest  # v3 name; API and callers stay compatible


class HelpEvent(Base):
    """Append-only audit trail of a help request (a trigger rejects UPDATE and DELETE)."""

    __tablename__ = "knv_help_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("knv_help_request.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("knv_user.id"))  # NULL = system
    action: Mapped[str] = mapped_column(String(24))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class Skill(Base):
    """Skill matrix from journal workbooks: structured lookup only, never chunked or embedded."""

    __tablename__ = "knv_skill"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    machine_id: Mapped[str] = mapped_column(String(64))
    engineer_name: Mapped[str] = mapped_column(String(128))
    skill_key: Mapped[str] = mapped_column(String(255))
    skill_item: Mapped[str] = mapped_column(String(255))
    op_count: Mapped[int] = mapped_column(Integer)
    level: Mapped[str] = mapped_column(String(16))  # none | familiar | proficient
    note: Mapped[str] = mapped_column(Text, default="")
    company: Mapped[str] = mapped_column(String(128), default="")
    source_file: Mapped[str] = mapped_column(String(255))
    source_row: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    __table_args__ = (
        UniqueConstraint("machine_id", "engineer_name", "skill_key", name="uq_knv_skill_machine_engineer_key"),
        CheckConstraint("level IN ('none', 'familiar', 'proficient')", name="ck_knv_skill_level"),
        CheckConstraint("op_count >= 0", name="ck_knv_skill_op_count"),
    )


class JournalCase(Base):
    """A deduplicated, redacted field case built from one or more journal rows."""

    __tablename__ = "knv_journal_case"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    text_key: Mapped[str] = mapped_column(String(64), unique=True)
    simhash: Mapped[int] = mapped_column(BigInteger)
    models: Mapped[list] = mapped_column(JSON)
    models_key: Mapped[str] = mapped_column(String(128), index=True)  # "" means unknown model
    model_source: Mapped[str] = mapped_column(String(16))  # registry | dictionary | unknown
    topic: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(String(8))
    category: Mapped[str] = mapped_column(String(16))  # troubleshooting | procedure
    symptom: Mapped[str] = mapped_column(Text)
    alarm_codes: Mapped[list] = mapped_column(JSON)
    cause: Mapped[str] = mapped_column(Text)
    solution: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool | None] = mapped_column(Boolean)
    machine_ids: Mapped[list] = mapped_column(JSON)
    engineers: Mapped[list] = mapped_column(JSON)
    companies: Mapped[list] = mapped_column(JSON)
    regions: Mapped[list] = mapped_column(JSON)
    dates: Mapped[list] = mapped_column(JSON)
    sources: Mapped[list] = mapped_column(JSON)
    occurrences: Mapped[int] = mapped_column(Integer)
    first_date: Mapped[dt.date] = mapped_column(Date)
    last_date: Mapped[dt.date] = mapped_column(Date)
    __table_args__ = (
        CheckConstraint("category IN ('troubleshooting', 'procedure')", name="ck_knv_journal_case_category"),
    )


class IngestAudit(Base):
    __tablename__ = "knv_ingest_audit"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    kind: Mapped[str] = mapped_column(String(32))
    files: Mapped[list] = mapped_column(JSON)
    counts: Mapped[dict] = mapped_column(JSON)
    redactions: Mapped[dict] = mapped_column(JSON)  # {"found": {...}, "residual": {...}}
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class EsOutbox(Base):
    """Documents whose ES keyword index must be recomputed from PG (transactional outbox)."""

    __tablename__ = "knv_es_outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # No foreign key: the row must outlive a deleted document so drain can purge its ES copy.
    document_id: Mapped[str] = mapped_column(String(32), unique=True)
    enqueued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)


Index("ix_knv_skill_key_level", Skill.skill_key, Skill.level)
Index("ix_knv_chunk_identifiers", Chunk.identifiers, postgresql_using="gin")
Index("ix_knv_chunk_dense", Chunk.dense, postgresql_using="hnsw",
      postgresql_with={"m": 16, "ef_construction": 64},
      postgresql_ops={"dense": "vector_cosine_ops"})
Index("ix_knv_chunk_sparse", Chunk.sparse, postgresql_using="hnsw",
      postgresql_with={"m": 16, "ef_construction": 64},
      postgresql_ops={"sparse": "sparsevec_ip_ops"})
Index("uq_knv_revision_published", Revision.document_id, unique=True,
      postgresql_where=Revision.status == "PUBLISHED")
Index("ix_knv_help_request_user_created", HelpRequest.user_id,
      HelpRequest.created_at.desc().nulls_last(), HelpRequest.id.desc())


class Episode(Base):
    """Long-term episodic memory (v4 D12): one per answered turn or resolved help request, per user."""

    __tablename__ = "knv_episode"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("knv_user.id"), index=True)
    source_kind: Mapped[str] = mapped_column(String(16))  # answer | help_request
    source_id: Mapped[str] = mapped_column(String(64))  # turn id (answer) or help request id
    thread_id: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    refs: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")  # cited chunk ids (answer)
    dense = mapped_column(Vector(settings.dense_dim), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    __table_args__ = (UniqueConstraint("user_id", "source_kind", "source_id", name="uq_knv_episode_source"),
                      CheckConstraint("source_kind IN ('answer', 'help_request')", name="ck_knv_episode_kind"))


class Profile(Base):
    """User profile (v4 D12): usage counts only. Department and region stay on knv_user (read-only here)."""

    __tablename__ = "knv_profile"
    user_id: Mapped[str] = mapped_column(ForeignKey("knv_user.id"), primary_key=True)
    lang_counts: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    model_counts: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    machine_counts: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
