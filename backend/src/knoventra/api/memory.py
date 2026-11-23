"""The user's own long-term memory: view and delete (v4 D12).

Only the caller's rows are ever visible; another user's episode id is a 404. Deleting is
permanent for that episode; clearing removes every episode and the usage profile.
Department and region are shown from the user record and cannot be changed here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from knoventra.api.deps import current_user, get_db
from knoventra.db.models import Episode, Profile, User
from knoventra.memory.profile import snapshot

router = APIRouter(prefix="/api/v1")


@router.get("/memory")
def my_memory(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Episode).where(Episode.user_id == user.id)
                      .order_by(Episode.updated_at.desc(), Episode.id)).all()
    return {"profile": snapshot(db, user),
            "episodes": [{"id": e.id, "kind": e.source_kind, "text": e.text, "created_at": e.created_at,
                          "updated_at": e.updated_at} for e in rows]}


@router.delete("/memory/episodes/{episode_id}", status_code=204)
def forget_episode(episode_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    deleted = db.execute(delete(Episode).where(Episode.id == episode_id, Episode.user_id == user.id)).rowcount
    if not deleted:
        raise HTTPException(404, "NOT_FOUND")
    db.commit()
    return Response(status_code=204)


@router.delete("/memory", status_code=204)
def forget_everything(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(delete(Episode).where(Episode.user_id == user.id))
    db.execute(delete(Profile).where(Profile.user_id == user.id))
    db.commit()
    return Response(status_code=204)
