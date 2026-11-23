"""Help request lifecycle: creation, server-checked state machine, append-only events.

OPEN -> ASSIGNED -> IN_PROGRESS -> RESOLVED -> CLOSED, plus CANCELLED. Every
transition locks the row, checks both the edge and the actor's role on this
request, and writes exactly one event in the same transaction.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from knoventra.db.models import HelpEvent, HelpRequest, User


class HelpError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code  # NOT_FOUND | FORBIDDEN | INVALID_TRANSITION | INVALID_INPUT


@dataclass(frozen=True)
class Action:
    sources: tuple[str, ...]
    target: str
    who: tuple[str, ...]  # requester | assignee | admin


ACTIONS: dict[str, Action] = {
    "assign": Action(("OPEN",), "ASSIGNED", ("admin",)),
    "unassign": Action(("ASSIGNED",), "OPEN", ("admin",)),
    "start": Action(("ASSIGNED",), "IN_PROGRESS", ("assignee",)),
    "progress": Action(("IN_PROGRESS",), "IN_PROGRESS", ("assignee",)),
    "resolve": Action(("IN_PROGRESS",), "RESOLVED", ("assignee",)),
    "reopen": Action(("RESOLVED",), "IN_PROGRESS", ("requester",)),
    "close": Action(("RESOLVED",), "CLOSED", ("requester", "admin")),
    "cancel": Action(("OPEN", "ASSIGNED", "IN_PROGRESS"), "CANCELLED", ("requester", "admin")),
}


def roles(req: HelpRequest, user: User) -> set[str]:
    out = set()
    if req.user_id == user.id:
        out.add("requester")
    if req.assignee_id is not None and req.assignee_id == user.id:
        out.add("assignee")
    if user.role == "admin":
        out.add("admin")
    return out


def create_request(db: Session, *, request_id: str, requester_id: str, operation_id: str, kind: str,
                   summary: str, evidence_refs: list[str], slots: dict | None = None) -> HelpRequest:
    """Idempotent on (requester, operation_id); the creation event is written only once."""
    slots = slots or {}
    inserted = db.execute(pg_insert(HelpRequest).values(
        id=request_id, user_id=requester_id, operation_id=operation_id, kind=kind, summary=summary,
        evidence_refs=evidence_refs, status="OPEN", machine_id=slots.get("machine_id"),
        machine_model=slots.get("machine_model"), alarm_codes=slots.get("alarm_codes") or [],
        symptom=slots.get("symptom") or "", tried_steps=slots.get("tried_steps") or "",
    ).on_conflict_do_nothing(index_elements=["user_id", "operation_id"]).returning(HelpRequest.id)).scalar()
    req = db.scalar(select(HelpRequest).where(HelpRequest.user_id == requester_id,
                                              HelpRequest.operation_id == operation_id))
    if inserted is not None:
        db.add(HelpEvent(request_id=req.id, actor_id=requester_id, action="created", from_status=None,
                         to_status="OPEN", note=kind))
        db.flush()
    return req


def transition(db: Session, request_id: str, actor: User, action: str, *, note: str = "",
               assignee_id: str | None = None, resolution: str | None = None) -> HelpRequest:
    spec = ACTIONS.get(action)
    if spec is None:
        raise HelpError("INVALID_INPUT", f"unknown action {action}")
    req = db.scalar(select(HelpRequest).where(HelpRequest.id == request_id).with_for_update()
                    .execution_options(populate_existing=True))
    if req is None or not roles(req, actor):
        raise HelpError("NOT_FOUND")  # do not reveal requests the actor cannot see
    if not roles(req, actor) & set(spec.who):
        raise HelpError("FORBIDDEN")
    if req.status not in spec.sources:
        raise HelpError("INVALID_TRANSITION", f"{action} not allowed from {req.status}")
    note = (note or "").strip()
    if action == "assign":
        assignee = db.get(User, assignee_id) if assignee_id else None
        if assignee is None or not assignee.enabled:
            raise HelpError("INVALID_INPUT", "assignee must be an enabled user")
        req.assignee_id = assignee.id
        note = note or f"assigned to {assignee.email}"
    elif action == "unassign":
        req.assignee_id = None
    elif action == "progress" and not note:
        raise HelpError("INVALID_INPUT", "a progress update needs a note")
    elif action == "resolve":
        if not resolution or not resolution.strip():
            raise HelpError("INVALID_INPUT", "a resolution is required")
        req.resolution = resolution.strip()
    now = dt.datetime.now(dt.UTC)
    if spec.target == "RESOLVED":
        req.resolved_at = now
    elif action == "reopen":
        req.resolved_at = None
    if spec.target in ("CLOSED", "CANCELLED"):
        req.closed_at = now
    db.add(HelpEvent(request_id=req.id, actor_id=actor.id, action=action, from_status=req.status,
                     to_status=spec.target, note=note or (resolution or "").strip()))
    req.status = spec.target
    req.updated_at = now
    db.flush()
    return req


def events(db: Session, request_id: str) -> list[HelpEvent]:
    return list(db.scalars(select(HelpEvent).where(HelpEvent.request_id == request_id)
                           .order_by(HelpEvent.at, HelpEvent.id)))
