import datetime as dt

import bcrypt
import jwt

from knoventra.config import settings


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())


def issue_token(user_id: str) -> str:
    exp = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=settings.jwt_ttl_minutes)
    return jwt.encode({"sub": user_id, "exp": exp}, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> str:
    """Returns the user id. Identity/region/department are always re-read from the DB."""
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])["sub"]
