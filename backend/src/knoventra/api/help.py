"""Help request APIs for requesters, experts and dispatchers.

Visibility follows the service: a request is visible to its requester, its
assignee and admins; anything else is a 404. State changes go through the
service state machine only.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from knoventra.api.deps import current_user, get_db, require_admin
from knoventra.db.models import HelpRequest, User
from knoventra.help.recommend import recommend
from knoventra.help.service import HelpError, events, roles, transition

router = APIRouter(prefix="/api/v1")
HTTP = {"NOT_FOUND": 404, "FORBIDDEN": 403, "INVALID_TRANSITION": 409, "INVALID_INPUT": 422}


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["assign", "unassign", "start", "progress", "resolve", "reopen", "close", "cancel"]
    note: str = Field(default="", max_length=2000)
    assignee_id: str | None = Field(default=None, max_length=32)
    resolution: str | None = Field(default=None, max_length=4000)


def _names(db: Session, ids) -> dict[str, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: (u.engineer_name or u.email) for u in db.scalars(select(User).where(User.id.in_(ids)))}


def _item(r: HelpRequest, names: dict[str, str], viewer: User) -> dict:
    return {"id": r.id, "kind": r.kind, "status": r.status, "summary": r.summary, "machine_id": r.machine_id,
            "machine_model": r.machine_model, "alarm_codes": r.alarm_codes or [], "symptom": r.symptom,
            "tried_steps": r.tried_steps, "resolution": r.resolution, "requester": names.get(r.user_id),
            "assignee": names.get(r.assignee_id), "assignee_id": r.assignee_id, "created_at": r.created_at,
            "updated_at": r.updated_at, "my_roles": sorted(roles(r, viewer))}


def _visible(db: Session, request_id: str, user: User) -> HelpRequest:
    r = db.get(HelpRequest, request_id)
    if r is None or not roles(r, user):
        raise HTTPException(404)
    return r


@router.get("/help-requests")
def list_help_requests(scope: Literal["mine", "assigned", "queue"] = "mine", limit: int = Query(50, ge=1, le=200),
                       user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(HelpRequest)
    if scope == "mine":
        q = q.where(HelpRequest.user_id == user.id)
    elif scope == "assigned":
        q = q.where(HelpRequest.assignee_id == user.id)
    else:  # dispatch queue: open and unassigned field help, admins only
        if user.role != "admin":
            raise HTTPException(403, "admin only")
        q = q.where(HelpRequest.status == "OPEN", HelpRequest.assignee_id.is_(None))
    rows = db.scalars(q.order_by(HelpRequest.updated_at.desc(), HelpRequest.id.desc()).limit(limit)).all()
    names = _names(db, [i for r in rows for i in (r.user_id, r.assignee_id)])
    return {"items": [_item(r, names, user) for r in rows]}


@router.get("/help-requests/{request_id}")
def help_request(request_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = _visible(db, request_id, user)
    trail = events(db, r.id)
    names = _names(db, [r.user_id, r.assignee_id, *(e.actor_id for e in trail)])
    return {**_item(r, names, user),
            "events": [{"action": e.action, "from_status": e.from_status, "to_status": e.to_status, "note": e.note,
                        "actor": names.get(e.actor_id) if e.actor_id else "系统", "at": e.at} for e in trail]}


@router.post("/help-requests/{request_id}/actions")
def act(request_id: str, body: ActionBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        transition(db, request_id, user, body.action, note=body.note, assignee_id=body.assignee_id,
                   resolution=body.resolution)
        db.commit()
    except HelpError as e:
        db.rollback()
        raise HTTPException(HTTP[e.code], {"code": e.code, "message": str(e)}) from None
    return help_request(request_id, user, db)


@router.get("/help-requests/{request_id}/recommendation")
def recommendation(request_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    r = _visible(db, request_id, admin)
    rec = recommend(db, db.get(User, r.user_id), f"{r.symptom} {r.tried_steps} {r.summary}")
    return {"reason": rec.reason, "candidates": [
        {"user_id": c.user_id, "engineer": c.engineer, "skill": c.skill, "op_count": c.op_count,
         "same_region": c.same_region, "open_load": c.open_load, "reason": c.reason} for c in rec.candidates]}
