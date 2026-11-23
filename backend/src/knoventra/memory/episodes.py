"""Long-term episodic memory: what was answered before, and how help requests were resolved.

Episodes are per user (every query filters on user_id) and deletable. Two kinds:
- `answer`: a cited answer, written when the turn ends; `refs` holds the cited chunk ids.
- `help_request`: a resolved help request, written in the resolve transaction.

Recall never trusts the stored text on its own. Each candidate is re-checked against the
current state before it reaches the model: every cited chunk must still be in a PUBLISHED
revision of a knowledge base the user can read now, and a help request must still belong
to the user and be RESOLVED or CLOSED. One failing reference drops the whole episode (it may
paraphrase that source), and the drop is traced. Dropped rows are kept: access may return.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from knoventra.access.scope import NoAccess, compute_scope
from knoventra.config import settings
from knoventra.db.models import Chunk, Episode, HelpRequest, Revision, User
from knoventra.ingestion.encoder import Encoder, encode_checked

_MARK = re.compile(r"\[S\d+\]")


@dataclass
class Recalled:
    id: str
    kind: str
    text: str
    score: float


def _upsert(db: Session, enc: Encoder, *, user_id: str, kind: str, source_id: str, thread_id: str | None,
            text: str, refs: list[str]) -> None:
    text = text[:settings.episode_chars]
    (dense, _), = encode_checked(enc, [text])
    stmt = pg_insert(Episode).values(user_id=user_id, source_kind=kind, source_id=source_id, thread_id=thread_id,
                                     text=text, refs=refs, dense=dense)
    db.execute(stmt.on_conflict_do_update(
        constraint="uq_knv_episode_source",
        set_={"text": stmt.excluded.text, "refs": stmt.excluded.refs, "dense": stmt.excluded.dense,
              "updated_at": func.clock_timestamp()}))


def remember_answer(db: Session, enc: Encoder, *, user_id: str, thread_id: str, turn_id: str, question: str,
                    answer: str, chunk_ids: list[str]) -> None:
    """A cited answer from this turn (idempotent per turn)."""
    if not chunk_ids:
        return  # only grounded answers become memories; refusals and chit-chat do not
    answer = _MARK.sub("", answer).strip()
    _upsert(db, enc, user_id=user_id, kind="answer", source_id=turn_id, thread_id=thread_id,
            text=f"问：{question.strip()[:200]}\n答：{answer}", refs=list(dict.fromkeys(chunk_ids)))


def help_text(req: HelpRequest) -> str:
    head = [f"求助单 {req.id[:8]}"]
    if req.machine_id:
        head.append(f"机台 {req.machine_id}")
    if req.machine_model:
        head.append(f"机型 {req.machine_model}")
    if req.alarm_codes:
        head.append("告警 " + "、".join(req.alarm_codes))
    if req.resolved_at:
        head.append(f"解决于 {req.resolved_at:%Y-%m-%d}")
    problem = req.symptom or req.summary.splitlines()[0]
    return f"{'，'.join(head)}。现象：{problem}；处理结果：{req.resolution}"


def remember_help(db: Session, enc: Encoder, req: HelpRequest) -> None:
    """A resolved help request, for its requester (re-resolving replaces the text)."""
    if req.status != "RESOLVED" or not req.resolution:
        return
    _upsert(db, enc, user_id=req.user_id, kind="help_request", source_id=req.id, thread_id=None,
            text=help_text(req), refs=[])


def _invalid(db: Session, user: User, ep: Episode, readable: frozenset[str]) -> str | None:
    """Why this episode must not be reused now; None when it is still valid."""
    if ep.source_kind == "help_request":
        req = db.get(HelpRequest, ep.source_id)
        if req is None or req.user_id != user.id:
            return "help_request_gone"
        if req.status not in ("RESOLVED", "CLOSED"):
            return "help_request_reopened"
        return None
    refs = ep.refs or []
    if not refs:
        return "no_refs"
    rows = db.execute(select(Chunk.id, Chunk.kb_id, Revision.status).join(Revision, Revision.id == Chunk.revision_id)
                      .where(Chunk.id.in_(refs))).all()
    found = {r.id: r for r in rows}
    for ref in refs:
        row = found.get(ref)
        if row is None:
            return "source_deleted"
        if row.status != "PUBLISHED":
            return "source_not_published"
        if row.kb_id not in readable:
            return "source_not_readable"
    return None


def recall(db: Session, enc: Encoder, user: User, question: str, *, thread_id: str | None) -> tuple[list[Recalled], list[dict]]:
    """Top episodes for `question`, each re-validated; (kept, trace). Other conversations only."""
    if not question.strip():
        return [], []
    (dense, _), = encode_checked(enc, [question])
    distance = Episode.dense.cosine_distance(dense)
    stmt = select(Episode, distance.label("dist")).where(Episode.user_id == user.id)
    if thread_id:
        stmt = stmt.where((Episode.thread_id.is_(None)) | (Episode.thread_id != thread_id))
    rows = db.execute(stmt.order_by(distance).limit(settings.episode_recall_k * 3)).all()
    try:
        readable = compute_scope(db, user).readable_kb_ids
    except NoAccess:
        readable = frozenset()  # fail closed: document-backed memories are all dropped
    kept: list[Recalled] = []
    trace: list[dict] = []
    for ep, dist in rows:
        score = round(1.0 - float(dist), 4)
        if score < settings.episode_min_score:
            break  # ordered by distance: everything after is weaker
        reason = _invalid(db, user, ep, readable)
        trace.append({"episode_id": ep.id, "kind": ep.source_kind, "score": score,
                      "kept": reason is None and len(kept) < settings.episode_recall_k, "reason": reason})
        if reason is None and len(kept) < settings.episode_recall_k:
            kept.append(Recalled(ep.id, ep.source_kind, ep.text, score))
    return kept, trace
