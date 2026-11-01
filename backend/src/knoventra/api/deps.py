from collections.abc import Iterator

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from knoventra.auth.security import decode_token
from knoventra.db.models import User
from knoventra.db.session import SessionLocal


def get_db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db


def current_user(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> User:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing token")
    try:
        uid = decode_token(authorization[7:])
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid token") from None
    user = db.get(User, uid)
    if user is None or not user.enabled:
        raise HTTPException(401, "unknown user")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user
