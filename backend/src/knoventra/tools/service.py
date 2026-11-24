"""Tool implementations shared by the in-process backend and the MCP server (v4 D13).

Every function takes the caller as a `User` row resolved by the transport (the graph's
server-side config in-process, the verified identity token over MCP) and recomputes access
from the database; no argument here identifies the caller. Results are JSON-serializable so
both transports return the same shapes. Functions flush; the transport commits.

Writes are two-phase. `prepare_help_request` stores the exact payload to be filed on a
PENDING confirmation and returns the card the user approves. `submit_help_request` takes
only the confirmation id, re-checks it at execution time (the caller's own row, APPROVED)
and files the stored payload idempotently, so nothing can change between approval and
filing, and a replayed submit returns the same request.
"""
from __future__ import annotations

import datetime as dt
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from knoventra.access.scope import NoAccess, compute_scope
from knoventra.agent.model_variants import clarification_needed
from knoventra.config import settings
from knoventra.db.models import Chunk, Confirmation, Escalation, Revision, User
from knoventra.help.recommend import recommend
from knoventra.help.service import assign_by_system, create_request
from knoventra.ingestion.encoder import Encoder
from knoventra.memory import profile as user_profile
from knoventra.retrieval.pipeline import OK, retrieve
from knoventra.retrieval.rerank import Reranker
from knoventra.skills import can_view_skills, format_hits, lookup_skills

KINDS = ("expert_escalation", "field_help")


class ToolError(Exception):
    """A refused tool call; `code` is stable for callers and tests."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _card(e) -> dict:
    return {"chunk_id": e.chunk_id, "document_id": e.document_id, "document_title": e.document_title,
            "revision": e.revision, "page": e.page, "section_path": e.section_path, "text": e.text, "lang": e.lang}


def search(db: Session, enc: Encoder, rr: Reranker | None, user: User, *, query: str, original_query: str = "",
           route_kbs: list[str] | None = None, machine_model: str | None = None,
           stated_models: list[str] = (), want_clarification: bool = False) -> dict:
    """Permission-filtered retrieval. `route_kbs` and `machine_model` can only narrow the result."""
    res = retrieve(db, enc, rr, user, query, original_query=original_query, route_kbs=route_kbs,
                   machine_model=machine_model)
    clarification = None
    if want_clarification and res.status == OK:
        # Only the best evidence decides: a low-ranked filler chunk must not trigger a question.
        clarification = clarification_needed(db, res.evidence[:1], list(stated_models))
    return {"status": res.status, "reason": res.reason, "required_identifiers": res.required_identifiers,
            "missing_identifiers": res.missing_identifiers, "degraded": res.degraded, "route": res.route,
            "evidence": [_card(e) for e in res.evidence], "clarification": clarification}


def skills(db: Session, user: User, *, skill: str) -> str:
    if not can_view_skills(user):
        return "NO_ACCESS: 技能表仅对 Ops 部门开放。"
    return format_hits(lookup_skills(db, skill))


def help_status(db: Session, user: User) -> str:
    rows = db.scalars(select(Escalation).where(Escalation.user_id == user.id)
                      .order_by(Escalation.created_at.desc().nulls_last(), Escalation.id.desc()).limit(5)).all()
    return "\n".join(f"{r.id}: {r.status} - {r.summary[:60]}" + ("（现场求助）" if r.kind == "field_help" else "")
                     for r in rows) or "暂无转交记录"


def _readable_refs(db: Session, user: User, refs: list[str]) -> list[str]:
    """Keep only evidence the caller can read now: a request never points experts at hidden chunks."""
    if not refs:
        return []
    try:
        readable = compute_scope(db, user).readable_kb_ids
    except NoAccess:
        return []
    ok = set(db.scalars(select(Chunk.id).join(Revision, Revision.id == Chunk.revision_id)
                        .where(Chunk.id.in_(refs), Chunk.kb_id.in_(readable), Revision.status == "PUBLISHED")))
    return [r for r in dict.fromkeys(refs) if r in ok]


def prepare_help_request(db: Session, user: User, *, thread_id: str, operation_id: str, kind: str, summary: str,
                         slots: dict | None = None, evidence_refs: list[str] = ()) -> dict:
    """Create (or return) the PENDING confirmation holding exactly what would be filed."""
    if kind not in KINDS:
        raise ToolError("INVALID_INPUT", f"unknown kind {kind}")
    conf = db.scalar(select(Confirmation).where(Confirmation.user_id == user.id,
                                                Confirmation.operation_id == operation_id))
    if conf is None:  # get-or-create: the graph re-runs this step when it resumes
        recommendation = None
        card = summary[:2000]
        if kind == "field_help":
            slots = slots or {}
            rec = recommend(db, user, f"{slots.get('symptom', '')} {slots.get('tried_steps', '')}")
            recommendation = rec.as_dict()
            card = (summary + (f"\n推荐专家：{rec.chosen.engineer}（{rec.reason}）" if rec.chosen
                               else f"\n派单：{rec.reason}"))[:2000]
        conf = Confirmation(
            user_id=user.id, thread_id=thread_id, operation_id=operation_id, summary=card, status="PENDING",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=settings.confirm_ttl_minutes),
            payload={"kind": kind, "slots": slots if kind == "field_help" else None,
                     "evidence_refs": _readable_refs(db, user, list(evidence_refs)),
                     "recommendation": recommendation})
        db.add(conf)
        db.flush()
    return {"confirmation_id": conf.id, "summary": conf.summary, "operation_id": conf.operation_id,
            "status": conf.status}


def submit_help_request(db: Session, user: User, *, confirmation_id: str) -> dict:
    """File an approved confirmation. Execution-side check: owner, APPROVED; idempotent."""
    conf = db.scalar(select(Confirmation).where(Confirmation.id == confirmation_id,
                                                Confirmation.user_id == user.id))
    if conf is None:
        raise ToolError("NOT_FOUND")  # someone else's confirmation is indistinguishable from none
    if conf.status != "APPROVED":
        # PENDING: not decided; REJECTED / EXPIRED: decided against. The approval endpoint
        # already refuses to approve past expires_at, so APPROVED means approved in time.
        raise ToolError("NOT_APPROVED", conf.status)
    payload = conf.payload or {"kind": "expert_escalation", "evidence_refs": []}  # pre-D13 confirmation
    kind = payload.get("kind") or "expert_escalation"
    request_id = hashlib.sha256(f"{user.id}:{conf.operation_id}".encode()).hexdigest()[:32]
    existed = db.scalar(select(Escalation.id).where(Escalation.user_id == user.id,
                                                    Escalation.operation_id == conf.operation_id))
    req = create_request(db, request_id=request_id, requester_id=user.id, operation_id=conf.operation_id, kind=kind,
                         summary=conf.summary, evidence_refs=payload.get("evidence_refs") or [],
                         slots=payload.get("slots"))
    if existed is None:  # first filing only: a replay never re-assigns or re-counts
        rec = payload.get("recommendation") or {}
        if rec.get("assignee_id"):
            req = assign_by_system(db, req.id, rec["assignee_id"], rec["reason"])
        slots = payload.get("slots") or {}
        if kind == "field_help":
            user_profile.bump(db, user.id, machines=[slots["machine_id"]] if slots.get("machine_id") else [],
                              models=[slots["machine_model"]] if slots.get("machine_model") else [])
    return {"request_id": req.id, "status": req.status, "kind": req.kind, "replayed": existed is not None}
