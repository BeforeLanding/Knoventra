"""D13: tools over MCP. Identity only from the token, two-phase writes re-checked on the tool
side, idempotent replay, restart between prepare and submit, and no token in checkpoints.

The server runs for real (uvicorn in a thread) against the committed test database, so every
row a test creates is removed in teardown.
"""
import datetime as dt
import socket
import threading
import time
import uuid
from contextlib import contextmanager

import httpx
import jwt
import pytest
import uvicorn
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from fakes import ScriptedModel, call
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.auth.security import TOOL_AUDIENCE, decode_token, decode_tool_token, issue_token, issue_tool_token
from knoventra.config import settings
from knoventra.db.models import (Chunk, Confirmation, Episode, HelpEvent, HelpRequest, KnowledgeBase, Profile,
                                 Revision, User)
from knoventra.ingestion.encoder import HashEncoder
from knoventra.mcp_server.app import build_app
from knoventra.tools.backend import Caller, McpTools, ToolBackendUnavailable, ToolCredential
from knoventra.tools.service import ToolError


class ServerThread:
    def __init__(self, engine):
        @contextmanager
        def sessions():
            with Session(engine) as s:
                yield s
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        base = f"http://127.0.0.1:{self.port}"
        self.url = f"{base}/mcp"
        app = build_app(session_factory=sessions, encoder=HashEncoder(), reranker=None, base_url=base)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            assert time.time() < deadline, "MCP server did not start"
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def sessions(engine):
    @contextmanager
    def factory():
        with Session(engine) as s:
            yield s
    return factory


@pytest.fixture
def people(engine):
    with Session(engine) as s:
        return {u.email.split("@")[0]: u.id for u in s.scalars(select(User))}


@pytest.fixture
def threads(engine, people):
    """Thread ids used by a test; everything filed under them is removed afterwards."""
    used: list[str] = []
    yield used
    with Session(engine) as s, s.begin():
        ops = select(Confirmation.operation_id).where(Confirmation.thread_id.in_(used))
        ids = select(HelpRequest.id).where(HelpRequest.operation_id.in_(ops))
        s.execute(delete(HelpRequest).where(HelpRequest.id.in_(ids)))  # events cascade
        s.execute(delete(Confirmation).where(Confirmation.thread_id.in_(used)))
        s.execute(delete(Episode).where(Episode.thread_id.in_(used)))
        s.execute(delete(Profile).where(Profile.user_id.in_(list(people.values()))))


@pytest.fixture
def server(engine):
    with ServerThread(engine) as srv:
        yield srv


def caller(people, threads, who="sg-ops", thread=None, ttl=None):
    thread = thread or uuid.uuid4().hex
    threads.append(thread)
    token = issue_tool_token(people[who], thread, uuid.uuid4().hex)
    return Caller(people[who], thread, ToolCredential(token))


def tools(server):
    return McpTools(server.url, timeout_s=10)


# ------------------------------------------------------------------ identity

def test_tool_schemas_carry_no_identity(server, people, threads):
    import anyio
    from mcp.client.client import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    token = caller(people, threads).credential.token

    async def listing():
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
            async with Client(streamable_http_client(server.url, http_client=http)) as c:
                return (await c.list_tools()).tools
    listed = anyio.run(listing)
    assert {t.name for t in listed} == {"search_knowledge", "lookup_skills", "get_help_request",
                                        "prepare_help_request", "submit_help_request"}
    params = {p for t in listed for p in (t.input_schema.get("properties") or {})}
    assert not params & {"user_id", "user", "requester_id", "sub", "thread_id", "token", "region", "department"}


def _expired(people):
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
    return jwt.encode({"sub": people["sg-ops"], "tid": "t", "rid": "r", "aud": TOOL_AUDIENCE, "iat": past,
                       "exp": past + dt.timedelta(minutes=5)}, settings.tool_token_secret, algorithm="HS256")


@pytest.mark.parametrize("make", [
    lambda p: None,                                                                    # no token
    lambda p: jwt.encode({"sub": p["sg-ops"], "tid": "t", "rid": "r", "aud": TOOL_AUDIENCE,
                          "exp": int(time.time()) + 60}, "forged-secret-of-sufficient-length-0123456789", algorithm="HS256"),
    _expired,
    lambda p: issue_token(p["sg-ops"]),                                                 # a login token
    lambda p: jwt.encode({"sub": p["sg-ops"], "tid": "t", "rid": "r", "aud": "someone-else",
                          "exp": int(time.time()) + 60}, settings.tool_token_secret, algorithm="HS256"),
], ids=["missing", "forged", "expired", "login-token", "wrong-audience"])
def test_requests_without_a_valid_identity_never_reach_a_tool(server, people, make):
    token = make(people)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_help_request", "arguments": {}}}
    assert httpx.post(server.url, json=body, headers=headers, timeout=10).status_code == 401
    with pytest.raises(ToolBackendUnavailable):
        tools(server).help_status(Caller(people["sg-ops"], "t", ToolCredential(token or "x")))


def test_tool_and_login_tokens_are_not_interchangeable(people):
    tool_token = issue_tool_token(people["sg-ops"], "t", "r")
    with pytest.raises(jwt.PyJWTError):
        decode_token(tool_token)  # API login refuses the tool token
    with pytest.raises(jwt.PyJWTError):
        decode_tool_token(issue_token(people["sg-ops"]))  # and the tool side refuses a login token


def test_disabled_user_is_refused_even_with_a_valid_token(server, engine, people, threads):
    who = caller(people, threads, "tw-ops")
    with Session(engine) as s, s.begin():
        s.execute(update(User).where(User.id == people["tw-ops"]).values(enabled=False))
    try:
        with pytest.raises(ToolError) as exc:
            tools(server).help_status(who)
        assert exc.value.code == "UNAUTHENTICATED"
    finally:
        with Session(engine) as s, s.begin():
            s.execute(update(User).where(User.id == people["tw-ops"]).values(enabled=True))


def test_scope_is_recomputed_on_the_tool_side(server, engine, people, threads):
    ops = tools(server).search(caller(people, threads, "sg-ops"), query="E17-42 排查")
    assert ops["status"] == "OK" and ops["evidence"]
    with Session(engine) as s:
        ops_kbs = list(s.scalars(select(KnowledgeBase.name).where(KnowledgeBase.name.like("ops%"))))
    # HR cannot read Ops manuals; naming Ops KBs as the route cannot widen that.
    hr = tools(server).search(caller(people, threads, "hr"), query="E17-42 排查", route_kbs=ops_kbs)
    assert not hr["evidence"]
    assert tools(server).skills(caller(people, threads, "hr"), "golden check").startswith("NO_ACCESS")


# ------------------------------------------------------------------ two-phase writes

SLOTS = {"machine_id": "610101", "machine_model": "WX-38", "symptom": "切换 tank 时 pump 不转",
         "tried_steps": "重插 LS-2", "alarm_codes": ["A-1301"]}


def _decide(engine, confirmation_id, status):
    with Session(engine) as s, s.begin():
        s.execute(update(Confirmation).where(Confirmation.id == confirmation_id).values(status=status))


def _hidden_chunk(engine):
    with Session(engine) as s:
        return s.scalar(select(Chunk.id).join(KnowledgeBase, KnowledgeBase.id == Chunk.kb_id)
                        .join(Revision, Revision.id == Chunk.revision_id)
                        .where(KnowledgeBase.name == "hr", Revision.status == "PUBLISHED"))


def test_submit_rechecks_approval_and_replays_idempotently(server, engine, people, threads):
    me = caller(people, threads)
    backend = tools(server)
    hidden = _hidden_chunk(engine)
    prepared = backend.prepare(me, operation_id=f"op-{uuid.uuid4().hex}", kind="field_help",
                               summary="现场求助单\n机台号：610101", slots=SLOTS, evidence_refs=[hidden])
    with Session(engine) as s:
        conf = s.get(Confirmation, prepared["confirmation_id"])
        assert conf.thread_id == me.thread_id  # bound to the token's conversation, not an argument
        assert conf.payload["slots"] == SLOTS and conf.payload["evidence_refs"] == []  # unreadable ref dropped
        assert "推荐专家" in conf.summary or "派单" in conf.summary

    with pytest.raises(ToolError) as exc:
        backend.submit(me, prepared["confirmation_id"])  # PENDING: the user has not approved
    assert exc.value.code == "NOT_APPROVED"
    with pytest.raises(ToolError) as exc:
        backend.submit(caller(people, threads, "tw-ops"), prepared["confirmation_id"])  # someone else's
    assert exc.value.code == "NOT_FOUND"

    _decide(engine, prepared["confirmation_id"], "APPROVED")
    first = backend.submit(me, prepared["confirmation_id"])
    replay = backend.submit(me, prepared["confirmation_id"])
    assert first["request_id"] == replay["request_id"] and first["replayed"] is False and replay["replayed"] is True
    with Session(engine) as s:
        req = s.get(HelpRequest, first["request_id"])
        assert (req.user_id, req.kind, req.machine_id, req.alarm_codes) == (people["sg-ops"], "field_help", "610101",
                                                                             ["A-1301"])
        assert s.scalar(select(func.count()).select_from(HelpEvent)
                        .where(HelpEvent.request_id == req.id, HelpEvent.action == "created")) == 1


@pytest.mark.parametrize("status", ["REJECTED", "EXPIRED"])
def test_declined_or_expired_confirmations_are_never_filed(server, engine, people, threads, status):
    me = caller(people, threads)
    prepared = tools(server).prepare(me, operation_id=f"op-{uuid.uuid4().hex}", kind="expert_escalation",
                                     summary="请专家看一下")
    _decide(engine, prepared["confirmation_id"], status)
    with pytest.raises(ToolError) as exc:
        tools(server).submit(me, prepared["confirmation_id"])
    assert exc.value.code == "NOT_APPROVED"


def test_prepare_is_get_or_create_per_operation(server, people, threads):
    me = caller(people, threads)
    op = f"op-{uuid.uuid4().hex}"
    a = tools(server).prepare(me, operation_id=op, kind="expert_escalation", summary="第一次")
    b = tools(server).prepare(me, operation_id=op, kind="expert_escalation", summary="被改过的摘要")
    assert a["confirmation_id"] == b["confirmation_id"] and b["summary"] == "第一次"


def test_server_restart_between_prepare_and_submit(engine, people, threads):
    me = caller(people, threads)
    with ServerThread(engine) as first:
        prepared = tools(first).prepare(me, operation_id=f"op-{uuid.uuid4().hex}", kind="expert_escalation",
                                        summary="重启前准备")
    _decide(engine, prepared["confirmation_id"], "APPROVED")
    with ServerThread(engine) as second:  # a new process: nothing kept in memory
        filed = tools(second).submit(me, prepared["confirmation_id"])
    assert filed["kind"] == "expert_escalation" and filed["status"] == "OPEN"


# ------------------------------------------------------------------ the agent graph over MCP

def test_agent_files_over_mcp_and_never_checkpoints_the_token(server, engine, sessions, people, threads):
    def agent(messages):
        last = messages[-1]
        if isinstance(last, ToolMessage):
            return AIMessage("已处理。")
        return call("escalate_to_expert", {"summary": "E17-42 反复报警，请专家协助"}, "esc")
    saver = InMemorySaver()
    graph = build_graph(AgentDeps(sessions, HashEncoder(), None, ScriptedModel(script=agent), saver,
                                  tools=tools(server), long_term_memory=False))
    thread = uuid.uuid4().hex
    threads.append(thread)

    def cfg():  # a fresh identity per run, as the API issues one per HTTP request
        token = issue_tool_token(people["sg-ops"], thread, uuid.uuid4().hex)
        return {"configurable": {"thread_id": thread, "user_id": people["sg-ops"],
                                 "tool_credential": ToolCredential(token)}}, token
    first, token1 = cfg()
    out = graph.invoke({"messages": [HumanMessage("帮我转人工")], "evidence": {}, "sources": [], "error_code": None},
                       first)
    confirmation_id = out["__interrupt__"][0].value["confirmation_id"]
    _decide(engine, confirmation_id, "APPROVED")
    second, token2 = cfg()
    out = graph.invoke(Command(resume={"approve": True}), second)
    assert "已创建转交记录" in next(m.content for m in out["messages"] if isinstance(m, ToolMessage))
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(HelpRequest).where(
            HelpRequest.operation_id == s.scalar(select(Confirmation.operation_id)
                                                 .where(Confirmation.id == confirmation_id)))) == 1

    stored = repr([(t.config, t.metadata, t.checkpoint) for t in saver.list({"configurable": {"thread_id": thread}})])
    assert token1 not in stored and token2 not in stored


def test_agent_without_a_credential_cannot_use_mcp_tools(server, sessions, people, threads):
    graph = build_graph(AgentDeps(sessions, HashEncoder(), None,
                                  ScriptedModel(script=lambda m: call("get_escalation_status", {}, "s")),
                                  InMemorySaver(), tools=tools(server), long_term_memory=False))
    thread = uuid.uuid4().hex
    threads.append(thread)
    with pytest.raises(ToolError) as exc:
        graph.invoke({"messages": [HumanMessage("我的转交进度")], "evidence": {}, "sources": [], "error_code": None},
                     {"configurable": {"thread_id": thread, "user_id": people["sg-ops"]}})
    assert exc.value.code == "UNAUTHENTICATED"


def test_dns_rebinding_protection_follows_allowed_hosts(engine):
    from starlette.testclient import TestClient

    @contextmanager
    def sessions():
        with Session(engine) as s:
            yield s
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    # A valid identity, so the request gets past the bearer check and reaches the Host check.
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "Authorization": "Bearer " + issue_tool_token("someone", "t", "r")}
    local = build_app(session_factory=sessions, encoder=HashEncoder())  # bound to 127.0.0.1
    with TestClient(local, base_url="http://mcp:58765") as client:
        assert client.post("/mcp", json=body, headers=headers).status_code == 421  # foreign Host refused
    def compose():  # each app runs its session manager once
        return build_app(session_factory=sessions, encoder=HashEncoder(), host="0.0.0.0", allowed_hosts=["mcp:*"])
    with TestClient(compose(), base_url="http://mcp:58765") as client:
        assert client.post("/mcp", json=body, headers=headers).status_code == 200  # allowed Host
    with TestClient(compose(), base_url="http://evil.example:58765") as client:
        assert client.post("/mcp", json=body, headers=headers).status_code == 421
