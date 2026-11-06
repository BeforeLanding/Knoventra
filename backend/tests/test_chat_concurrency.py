from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import json
import uuid

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from fakes import ScriptedModel, call
from knoventra.agent.checkpoint import make_checkpointer
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.api.chat import get_graph
from knoventra.api.deps import get_db
from knoventra.auth.security import issue_token
from knoventra.config import settings
from knoventra.db.models import Confirmation, Conversation, Escalation, Message, User
from knoventra.main import app


@pytest.mark.parametrize("mode", ["chat", "confirm"])
def test_independent_http_requests_cannot_advance_same_checkpoint(engine, enc, mode):
    entered, release = threading.Event(), threading.Event()
    thread_id = uuid.uuid4().hex

    @contextmanager
    def sessions():
        with Session(engine) as session:
            yield session

    def db_dependency():
        with sessions() as db:
            yield db

    def script(messages):
        last = messages[-1]
        if isinstance(last, HumanMessage) and last.content == "转人工":
            return call("escalate_to_expert", {"summary": "并发确认测试"}, cid="concurrent-esc")
        if (mode == "chat" and isinstance(last, HumanMessage) and last.content == "等待处理") \
                or (mode == "confirm" and isinstance(last, ToolMessage)):
            entered.set()
            assert release.wait(10), "test did not release the model"
        return AIMessage("处理完成。")

    with sessions() as db, db.begin():
        user = db.scalar(select(User).where(User.email == "sg-ops@example.test"))
        headers = {"Authorization": "Bearer " + issue_token(user.id)}
        conv = Conversation(user_id=user.id, thread_id=thread_id, title="并发 HTTP 测试")
        db.add(conv)
        db.flush()
        cid = conv.id
    saver, pool = make_checkpointer(settings.database_url)
    conversation_ids = [cid]
    graph = build_graph(AgentDeps(sessions, enc, None, ScriptedModel(script=script), saver))
    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[get_graph] = lambda: graph
    try:
        if mode == "confirm":
            start = TestClient(app).post("/api/v1/chat", headers=headers,
                                         json={"conversation_id": cid, "message": "转人工"})
            assert start.status_code == 200
            with sessions() as db:
                confirmation_id = db.scalar(select(Confirmation.id).where(Confirmation.thread_id == thread_id))
            path, body = "/api/v1/chat/confirm", {"confirmation_id": confirmation_id, "approve": True}
        else:
            path, body = "/api/v1/chat", {"conversation_id": cid, "message": "等待处理"}

        def first_request():
            return TestClient(app).post(path, headers=headers, json=body)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(first_request)
            try:
                assert entered.wait(5), "first request did not reach the model"
                with sessions() as db:
                    before = db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == cid))
                duplicate = TestClient(app).post(path, headers=headers, json=body)
                assert duplicate.status_code == 409 and duplicate.json()["detail"] == "CONVERSATION_BUSY"
                chat = TestClient(app).post("/api/v1/chat", headers=headers,
                                            json={"conversation_id": cid, "message": "并发消息"})
                assert chat.status_code == 409 and chat.json()["detail"] == "CONVERSATION_BUSY"
                with sessions() as db:
                    assert db.scalar(select(func.count()).select_from(Message)
                                     .where(Message.conversation_id == cid)) == before
                # Another conversation remains usable while this checkpoint is busy.
                other = TestClient(app).post("/api/v1/chat", headers=headers, json={"message": "独立问题"})
                assert other.status_code == 200
                meta = json.loads(next(line[6:] for line in other.text.splitlines() if line.startswith("data: ")))
                conversation_ids.append(meta["conversation_id"])
            finally:
                release.set()
            assert future.result(timeout=10).status_code == 200
        if mode == "confirm":
            assert TestClient(app).post(path, headers=headers, json=body).status_code == 200
            with sessions() as db:
                ops = select(Confirmation.operation_id).where(Confirmation.thread_id == thread_id)
                assert db.scalar(select(func.count()).select_from(Escalation).where(Escalation.operation_id.in_(ops))) == 1
        assert graph.get_state({"configurable": {"thread_id": thread_id}}).next == ()
    finally:
        release.set()
        app.dependency_overrides.clear()
        pool.close()
        with sessions() as db, db.begin():
            ops = select(Confirmation.operation_id).where(Confirmation.thread_id == thread_id)
            db.execute(delete(Escalation).where(Escalation.operation_id.in_(ops)))
            db.execute(delete(Confirmation).where(Confirmation.thread_id == thread_id))
            db.execute(delete(Message).where(Message.conversation_id.in_(conversation_ids)))
            db.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
