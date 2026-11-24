"""Tool transports for the agent graph: in-process (default) or the MCP server.

The graph never puts identity in tool arguments. In-process, the caller is the server-side
`user_id` from the run config. Over MCP, the caller is whatever the identity token in the
HTTP header proves; the token travels in a `ToolCredential` object, because LangGraph copies
plain-string config values into checkpoint metadata and a token must not be stored there.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.orm import Session

from knoventra.db.models import User
from knoventra.ingestion.encoder import Encoder
from knoventra.retrieval.rerank import Reranker
from knoventra.tools import service
from knoventra.tools.service import ToolError

ERROR_PREFIX = "KNV_TOOL_ERROR:"
_ERROR = re.compile(re.escape(ERROR_PREFIX) + r"([A-Z_]+)")


class ToolCredential:
    """Opaque holder for a run's identity token (an object, so it is never checkpointed)."""

    __slots__ = ("token",)

    def __init__(self, token: str):
        self.token = token

    def __repr__(self) -> str:
        return "ToolCredential(<redacted>)"


@dataclass(frozen=True)
class Caller:
    user_id: str
    thread_id: str
    credential: ToolCredential | None = field(default=None, repr=False)


class ToolBackendUnavailable(RuntimeError):
    pass


class ToolBackend(Protocol):
    def search(self, caller: Caller, **kw) -> dict: ...
    def skills(self, caller: Caller, skill: str) -> str: ...
    def help_status(self, caller: Caller) -> str: ...
    def prepare(self, caller: Caller, **kw) -> dict: ...
    def submit(self, caller: Caller, confirmation_id: str) -> dict: ...


class InProcessTools:
    def __init__(self, session_factory: Callable[[], AbstractContextManager[Session]], encoder: Encoder,
                 reranker: Reranker | None):
        self.session_factory, self.encoder, self.reranker = session_factory, encoder, reranker

    def _run(self, caller: Caller, fn, *, commit: bool = False, **kw):
        with self.session_factory() as db:
            user = db.get(User, caller.user_id)
            if user is None:
                raise ToolError("UNAUTHENTICATED")
            out = fn(db, user, **kw)
            if commit:
                db.commit()
            return out

    def search(self, caller: Caller, **kw) -> dict:
        return self._run(caller, lambda db, user, **a: service.search(db, self.encoder, self.reranker, user, **a), **kw)

    def skills(self, caller: Caller, skill: str) -> str:
        return self._run(caller, service.skills, skill=skill)

    def help_status(self, caller: Caller) -> str:
        return self._run(caller, service.help_status)

    def prepare(self, caller: Caller, **kw) -> dict:
        return self._run(caller, service.prepare_help_request, commit=True, thread_id=caller.thread_id, **kw)

    def submit(self, caller: Caller, confirmation_id: str) -> dict:
        return self._run(caller, service.submit_help_request, commit=True, confirmation_id=confirmation_id)


class McpTools:
    """Calls the MCP server over streamable HTTP, one stateless request per tool call."""

    def __init__(self, url: str, timeout_s: float):
        self.url, self.timeout_s = url, timeout_s

    def _call(self, caller: Caller, name: str, args: dict) -> Any:
        import anyio
        import httpx2
        from mcp.client.client import Client
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        if caller.credential is None:
            raise ToolError("UNAUTHENTICATED", "no identity token for this run")

        async def run():
            headers = {"Authorization": f"Bearer {caller.credential.token}"}
            async with create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(self.timeout_s)) as http:
                async with Client(streamable_http_client(self.url, http_client=http),
                                  read_timeout_seconds=self.timeout_s) as client:
                    return await client.call_tool(name, args)
        try:
            result = anyio.run(run)
        except BaseException as exc:  # noqa: BLE001 - transport/auth failures arrive as exception groups
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ToolBackendUnavailable(f"MCP call {name} failed: {_root(exc)!r}") from exc
        text = "".join(getattr(block, "text", "") for block in result.content)
        if result.is_error:
            match = _ERROR.search(text)
            raise ToolError(match.group(1) if match else "TOOL_FAILED", text[:200])
        return json.loads(text)

    def search(self, caller: Caller, **kw) -> dict:
        return self._call(caller, "search_knowledge", kw)

    def skills(self, caller: Caller, skill: str) -> str:
        return self._call(caller, "lookup_skills", {"skill": skill})["text"]

    def help_status(self, caller: Caller) -> str:
        return self._call(caller, "get_help_request", {})["text"]

    def prepare(self, caller: Caller, **kw) -> dict:
        return self._call(caller, "prepare_help_request", kw)

    def submit(self, caller: Caller, confirmation_id: str) -> dict:
        return self._call(caller, "submit_help_request", {"confirmation_id": confirmation_id})


def _root(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc
