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


TOOL_AUDIENCE = "knoventra-mcp"


def issue_tool_token(user_id: str, thread_id: str, run_id: str) -> str:
    """Short-lived identity for one agent run's tool calls (MCP). Separate secret and an
    audience, so it is useless as a login token and a login token is useless here."""
    now = dt.datetime.now(dt.UTC)
    claims = {"sub": user_id, "tid": thread_id, "rid": run_id, "aud": TOOL_AUDIENCE, "iat": now,
              "exp": now + dt.timedelta(seconds=settings.tool_token_ttl_s)}
    return jwt.encode(claims, settings.tool_token_secret, algorithm="HS256")


def decode_tool_token(token: str) -> dict:
    return jwt.decode(token, settings.tool_token_secret, algorithms=["HS256"], audience=TOOL_AUDIENCE,
                      options={"require": ["sub", "tid", "rid", "exp", "aud"]})
