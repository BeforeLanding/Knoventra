from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from knoventra.db.models import KbGrant, User


class NoAccess(Exception):
    """Raised when the readable set is empty or cannot be computed (fail-closed)."""


@dataclass(frozen=True)
class AccessScope:
    user_id: str
    readable_kb_ids: frozenset[str]


def compute_scope(db: Session, user: User) -> AccessScope:
    try:
        if not user.enabled:
            raise NoAccess("user disabled")
        g = KbGrant
        stmt = select(g.kb_id).where(
            or_(g.region.is_(None), g.region == user.region),
            or_(g.department.is_(None), g.department == user.department),
            or_(g.role.is_(None), g.role == user.role),
        )
        ids = frozenset(db.scalars(stmt).all())
    except NoAccess:
        raise
    except Exception as e:  # permission lookup failed -> never fall back to "everything"
        raise NoAccess(f"scope lookup failed: {e}") from e
    if not ids:
        raise NoAccess("empty scope")
    return AccessScope(user.id, ids)
