"""Knoventra MCP server: the agent's tools behind an authenticated streamable HTTP endpoint.

Identity comes only from the bearer token (`issue_tool_token`): the SDK's bearer middleware
rejects a missing, forged, expired or wrong-audience token with 401 before any tool runs,
and each tool re-reads the user row from the database (disabled users are refused) and
recomputes access there. No tool parameter names a user, and prepared confirmations are
bound to the token's conversation, not to an argument. The server is stateless
(`stateless_http`): all state is in PostgreSQL, so it can restart between prepare and submit.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager

import anyio
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as SdkToolError
from sqlalchemy.orm import Session

from knoventra.auth.security import decode_tool_token
from knoventra.config import settings
from knoventra.db.models import User
from knoventra.tools import service
from knoventra.tools.backend import ERROR_PREFIX
from knoventra.tools.service import ToolError


class IdentityTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = decode_tool_token(token)
        except jwt.PyJWTError:
            return None
        return AccessToken(token=token, client_id="knoventra-api", scopes=[], subject=claims["sub"],
                           expires_at=int(claims["exp"]), claims={"tid": claims["tid"], "rid": claims["rid"]})


def _session_factory() -> Callable[[], AbstractContextManager[Session]]:
    from knoventra.db.session import SessionLocal

    @contextmanager
    def sessions():
        with SessionLocal() as db:
            yield db
    return sessions


def build_server(session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
                 encoder=None, reranker=None, base_url: str = "http://127.0.0.1") -> MCPServer:
    sessions = session_factory or _session_factory()
    server = MCPServer("knoventra", instructions="Knoventra knowledge, skills and help-request tools.",
                       token_verifier=IdentityTokenVerifier(),
                       # The verifier enforces the audience itself (PyJWT `audience=`), so the
                       # SDK's resource-URL check is off rather than tied to a deploy hostname.
                       auth=AuthSettings(issuer_url=base_url, resource_server_url=f"{base_url}/mcp",
                                         validate_token_resource=False))

    def deps():
        if encoder is not None:  # injected (tests)
            return encoder, reranker
        from knoventra.ingestion.encoder import get_encoder
        from knoventra.retrieval.rerank import get_reranker
        return get_encoder(), get_reranker()

    async def run(fn, *, commit: bool = False, **kw) -> dict:
        token = get_access_token()  # read in the request context, before leaving the event loop
        if token is None or not token.subject:
            raise SdkToolError(ERROR_PREFIX + "UNAUTHENTICATED")
        identity = {"user_id": token.subject, "thread_id": (token.claims or {}).get("tid")}

        def work():
            with sessions() as db:
                user = db.get(User, identity["user_id"])
                if user is None or not user.enabled:
                    raise ToolError("UNAUTHENTICATED")
                out = fn(db, user, identity, **kw)
                if commit:
                    db.commit()
                return out
        try:
            return await anyio.to_thread.run_sync(work)
        except ToolError as exc:
            # The SDK forwards the message of its own ToolError to the client as an is_error
            # result; any other exception becomes a generic message without our code.
            raise SdkToolError(ERROR_PREFIX + exc.code) from None

    @server.tool()
    async def search_knowledge(query: str, original_query: str = "", route_kbs: list[str] | None = None,
                               machine_model: str | None = None, stated_models: list[str] | None = None,
                               want_clarification: bool = False) -> dict:
        """Search the knowledge bases the caller may read. route_kbs and machine_model only narrow."""
        enc, rr = deps()
        return await run(lambda db, user, _: service.search(
            db, enc, rr, user, query=query, original_query=original_query, route_kbs=route_kbs,
            machine_model=machine_model, stated_models=stated_models or [], want_clarification=want_clarification))

    @server.tool()
    async def lookup_skills(skill: str) -> dict:
        """Engineers who know a skill (Ops staff and admins only)."""
        return await run(lambda db, user, _: {"text": service.skills(db, user, skill=skill)})

    @server.tool()
    async def get_help_request() -> dict:
        """The caller's latest help requests and their status."""
        return await run(lambda db, user, _: {"text": service.help_status(db, user)})

    @server.tool()
    async def prepare_help_request(operation_id: str, kind: str, summary: str, slots: dict | None = None,
                                   evidence_refs: list[str] | None = None) -> dict:
        """Phase 1: store what would be filed on a pending confirmation; nothing is filed yet."""
        return await run(lambda db, user, ident: service.prepare_help_request(
            db, user, thread_id=ident["thread_id"], operation_id=operation_id, kind=kind, summary=summary,
            slots=slots, evidence_refs=evidence_refs or []), commit=True)

    @server.tool()
    async def submit_help_request(confirmation_id: str) -> dict:
        """Phase 2: file a confirmation the caller approved; re-checked here, idempotent."""
        return await run(lambda db, user, _: service.submit_help_request(db, user, confirmation_id=confirmation_id),
                         commit=True)

    return server


def build_app(*, host: str = "127.0.0.1", allowed_hosts: list[str] | None = None, **kw):
    security = None
    if allowed_hosts:
        from mcp.server.transport_security import TransportSecuritySettings

        security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts,
                                             allowed_origins=[])
    return build_server(**kw).streamable_http_app(stateless_http=True, json_response=True, host=host,
                                                  transport_security=security)
