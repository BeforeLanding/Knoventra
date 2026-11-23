"""User profile: usage counts that help the assistant, never decide access.

Counts: the Chinese script of the user's questions, machine models named in questions, and
machines in help requests the user filed. Department and region are read from knv_user and
shown as-is; the profile never stores or changes them. Uses: reply script when a question
gives no signal, a "usual machine" hint when a help request lacks its machine (the model
must ask, never fill it in), and context for the answer model. Permissions ignore it.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from knoventra.db.models import Profile, User

LANG_MIN_QUESTIONS = 3  # a preferred script needs this many signals ...
LANG_MIN_SHARE = 0.8  # ... and this share of them
TOP = 3


def bump(db: Session, user_id: str, *, lang: str | None = None, models: list[str] = (),
         machines: list[str] = ()) -> None:
    if not (lang or models or machines):
        return
    db.execute(pg_insert(Profile).values(user_id=user_id).on_conflict_do_nothing(index_elements=["user_id"]))
    # Row lock: concurrent turns in two conversations must not lose each other's counts.
    prof = db.scalars(select(Profile).where(Profile.user_id == user_id).with_for_update()).one()

    def add(counts: dict, keys) -> dict:
        out = dict(counts or {})
        for key in keys:
            out[key] = out.get(key, 0) + 1
        return out
    if lang:
        prof.lang_counts = add(prof.lang_counts, [lang])
    if models:
        prof.model_counts = add(prof.model_counts, models)
    if machines:
        prof.machine_counts = add(prof.machine_counts, machines)
    prof.updated_at = func.clock_timestamp()
    db.flush()


def _top(counts: dict | None) -> list[tuple[str, int]]:
    return sorted((counts or {}).items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]


def snapshot(db: Session, user: User) -> dict:
    prof = db.get(Profile, user.id)
    return {"department": user.department, "region": user.region,
            "preferred_script": preferred_script(prof.lang_counts if prof else None),
            "models": _top(prof.model_counts if prof else None),
            "machines": _top(prof.machine_counts if prof else None)}


def preferred_script(lang_counts: dict | None) -> str | None:
    counts = lang_counts or {}
    total = sum(counts.values())
    if total < LANG_MIN_QUESTIONS:
        return None
    script, n = max(counts.items(), key=lambda kv: kv[1])
    return script if n / total >= LANG_MIN_SHARE else None


def text(snap: dict) -> str:
    parts = [f"部门 {snap['department']}"]
    if snap.get("region"):
        parts.append(f"区域 {snap['region']}")
    if snap.get("models"):
        parts.append("常问机型 " + "、".join(f"{m}（{n} 次）" for m, n in snap["models"]))
    if snap.get("machines"):
        parts.append("提过求助的机台 " + "、".join(f"{m}（{n} 次）" for m, n in snap["machines"]))
    return "；".join(parts)


def machine_hint(snap: dict | None) -> str:
    if not snap or not snap.get("machines"):
        return ""
    machine, n = snap["machines"][0]
    return f"该用户以往求助最多的机台是 {machine}（{n} 次）。可以问用户是不是这台，不要直接填写。"
