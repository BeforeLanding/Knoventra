from contextlib import contextmanager
import json
from pathlib import Path
import runpy

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from fakes import ScriptedModel, call, session_factory_for
from knoventra.agent.checkpoint import make_checkpointer
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.api.chat import get_graph
from knoventra.api.deps import get_db
from knoventra.api.run_lock import ConversationLease
from knoventra.config import settings
from knoventra.db.models import Conversation, Escalation, Message
from knoventra.main import app


def events(response):
    return {raw.splitlines()[0][7:]: json.loads(raw.splitlines()[1][6:])
            for raw in response.text.strip().split("\n\n")}


def auth(client, who="sg-ops"):
    token = client.post("/api/v1/auth/login", json={"email": f"{who}@example.test", "password": "demo-pass-1"}).json()["token"]
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def recovery_client(db, enc, monkeypatch):
    monkeypatch.setattr(settings, "gate_threshold", 0.1)
    monkeypatch.setattr(settings, "gate_sparse_threshold", 1e9)
    saver, pool = make_checkpointer(settings.database_url)

    def make(script, factory=None):
        graph = build_graph(AgentDeps(factory or session_factory_for(db), enc, None, ScriptedModel(script=script), saver))
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_graph] = lambda: graph
        return TestClient(app), graph

    yield make
    app.dependency_overrides.clear()
    pool.close()


def test_failed_model_recovers_without_duplicate_user_message_or_automatic_retries(recovery_client):
    calls = 0

    def script(msgs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary model failure")
        return AIMessage("已恢复回答。")

    client, _ = recovery_client(script)
    headers = auth(client)
    first = events(client.post("/api/v1/chat", headers=headers, json={"message": "恢复问题"}))
    cid = first["meta"]["conversation_id"]
    assert first["error"]["code"] == "RETRIEVAL_UNAVAILABLE"
    state = client.get(f"/api/v1/conversations/{cid}/state", headers=headers).json()
    assert state["blocked"] and state["retry_checkpoint_id"] and state["pending_confirmation"] is None
    assert client.get(f"/api/v1/conversations/{cid}/state", headers=headers).json()["retry_checkpoint_id"] == state["retry_checkpoint_id"]
    assert calls == 1  # Reading state never executes or retries the model.
    new = client.post("/api/v1/chat", headers=headers, json={"conversation_id": cid, "message": "新问题"})
    assert new.status_code == 409 and new.json()["detail"] == "RUN_INCOMPLETE"
    body = {"conversation_id": cid, "checkpoint_id": state["retry_checkpoint_id"]}
    assert client.post("/api/v1/chat/retry", headers=auth(client, "hr"), json=body).status_code == 404
    wrong = client.post("/api/v1/chat/retry", headers=headers, json={**body, "checkpoint_id": "stale"})
    assert wrong.status_code == 409 and wrong.json()["detail"] == "CHECKPOINT_CHANGED"
    assert calls == 1
    retried = client.post("/api/v1/chat/retry", headers=headers, json=body)
    assert retried.status_code == 200 and events(retried)["done"]["answer"] == "已恢复回答。"
    restored = client.get(f"/api/v1/conversations/{cid}/state", headers=headers).json()
    assert not restored["blocked"] and restored["retry_checkpoint_id"] is None
    assert [m["content"] for m in restored["messages"] if m["role"] == "user"] == ["恢复问题"]
    assert sum(m["content"] == "已恢复回答。" for m in restored["messages"]) == 1
    duplicate = client.post("/api/v1/chat/retry", headers=headers, json=body)
    assert duplicate.status_code == 409 and duplicate.json()["detail"] == "RUN_NOT_RETRYABLE"
    assert calls == 2


def test_retry_token_cannot_resume_a_later_incomplete_turn(recovery_client):
    seen = set()

    def script(msgs):
        human = next(m for m in reversed(msgs) if m.type == "human")
        if human.id not in seen:
            seen.add(human.id)
            raise RuntimeError("fail once per turn")
        return AIMessage("已恢复。")

    client, _ = recovery_client(script)
    h = auth(client)
    cid = events(client.post("/api/v1/chat", headers=h, json={"message": "第一轮"}))["meta"]["conversation_id"]
    old = client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()["retry_checkpoint_id"]
    assert client.post("/api/v1/chat/retry", headers=h, json={"conversation_id": cid, "checkpoint_id": old}).status_code == 200
    client.post("/api/v1/chat", headers=h, json={"conversation_id": cid, "message": "第二轮"})
    current = client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()["retry_checkpoint_id"]
    assert current and current != old
    stale = client.post("/api/v1/chat/retry", headers=h, json={"conversation_id": cid, "checkpoint_id": old})
    assert stale.status_code == 409 and stale.json()["detail"] == "CHECKPOINT_CHANGED"
    assert client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()["retry_checkpoint_id"] == current


def test_retry_does_not_bypass_interrupt_or_busy_conversation(recovery_client, db):
    client, _ = recovery_client(lambda msgs: call("escalate_to_expert", {"summary": "等待确认"}))
    h = auth(client)
    cid = events(client.post("/api/v1/chat", headers=h, json={"message": "转人工"}))["meta"]["conversation_id"]
    state = client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()
    assert state["pending_confirmation"] and state["retry_checkpoint_id"] is None
    body = {"conversation_id": cid, "checkpoint_id": "guessed"}
    rejected = client.post("/api/v1/chat/retry", headers=h, json=body)
    assert rejected.status_code == 409 and rejected.json()["detail"] == "RUN_NOT_RETRYABLE"
    assert db.scalar(select(func.count()).select_from(Escalation)) == 0
    lease = ConversationLease.acquire(db.get_bind().engine, cid)
    try:
        busy = client.post("/api/v1/chat/retry", headers=h, json=body)
        assert busy.status_code == 409 and busy.json()["detail"] == "CONVERSATION_BUSY"
    finally:
        lease.close()


def test_recovery_replays_confirmed_write_idempotently_after_restart(recovery_client, db):
    failed = False

    @contextmanager
    def factory():
        nonlocal failed
        yield db
        if not failed and db.scalar(select(func.count()).select_from(Escalation)):
            failed = True
            # The write has committed, but the execute node has not returned its checkpoint update.
            raise RuntimeError("process failed after escalation commit")

    def script(msgs):
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage(msgs[-1].content)
        return call("escalate_to_expert", {"summary": "已确认操作恢复"})

    client, graph = recovery_client(script, factory)
    h = auth(client)
    started = events(client.post("/api/v1/chat", headers=h, json={"message": "转人工"}))
    cid, conf_id = started["meta"]["conversation_id"], started["confirm"]["confirmation_id"]
    failed_response = events(client.post("/api/v1/chat/confirm", headers=h,
                                         json={"confirmation_id": conf_id, "approve": True}))
    assert failed_response["error"]["code"] == "RETRIEVAL_UNAVAILABLE" and failed
    assert db.scalar(select(func.count()).select_from(Escalation)) == 1
    state = client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()
    assert state["retry_checkpoint_id"] and state["pending_confirmation"] is None
    # A fresh compiled graph using the same PostgreSQL checkpointer simulates restart.
    client, restarted = recovery_client(script, factory)
    assert restarted is not graph
    resumed = client.post("/api/v1/chat/retry", headers=h,
                          json={"conversation_id": cid, "checkpoint_id": state["retry_checkpoint_id"]})
    assert resumed.status_code == 200 and "已创建转交记录" in events(resumed)["done"]["answer"]
    assert db.scalar(select(func.count()).select_from(Escalation)) == 1
    assert client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()["retry_checkpoint_id"] is None


def test_retry_preserves_successful_retrieval_and_does_not_reset_turn_state(recovery_client, db):
    failed = False

    def script(msgs):
        nonlocal failed
        if isinstance(msgs[-1], ToolMessage):
            if not failed:
                failed = True
                raise RuntimeError("model failed after successful retrieval")
            return AIMessage("请按手册检查 [S1]。")
        return call("search_knowledge", {"query": "E17-42 排查"})

    client, graph = recovery_client(script)
    h = auth(client)
    first = events(client.post("/api/v1/chat", headers=h, json={"message": "E17-42 如何排查"}))
    cid = first["meta"]["conversation_id"]
    conv = db.get(Conversation, cid)
    cfg = {"configurable": {"thread_id": conv.thread_id}}
    before = graph.get_state(cfg).values
    assert before["evidence"] and len(before["retrieval_trace"]) == 1
    token = client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()["retry_checkpoint_id"]
    done = events(client.post("/api/v1/chat/retry", headers=h, json={"conversation_id": cid, "checkpoint_id": token}))
    after = graph.get_state(cfg).values
    assert after["evidence"] == before["evidence"] and after["retrieval_trace"] == before["retrieval_trace"]
    assert sum(isinstance(m, ToolMessage) for m in after["messages"]) == 1
    assert done["sources"] and done["sources"][0]["alias"] == "S1"


@pytest.fixture
def fail_final_insert(db, monkeypatch):
    execute = db.execute
    failures = {"remaining": 1}

    def wrapped(statement, *args, **kwargs):
        if (getattr(statement, "is_insert", False) and statement.table.name == "knv_message"
                and statement.compile().params.get("completion_turn_id") and failures["remaining"]):
            failures["remaining"] -= 1
            raise RuntimeError("final answer insert failed")
        return execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", wrapped)
    return failures


def test_completed_answer_recovery_does_not_call_model_and_is_idempotent(recovery_client, db, fail_final_insert):
    calls = 0

    def script(msgs):
        nonlocal calls
        calls += 1
        return AIMessage("已完成但未落库的回答。")

    client, _ = recovery_client(script)
    h = auth(client)
    first = events(client.post("/api/v1/chat", headers=h, json={"message": "保存失败测试"}))
    cid = first["meta"]["conversation_id"]
    assert first["error"]["code"] == "ANSWER_SAVE_FAILED"
    state = client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()
    assert state["blocked"] and state["recover_checkpoint_id"] and state["retry_checkpoint_id"] is None
    assert not any(m["content"] == "已完成但未落库的回答。" for m in state["messages"])
    assert calls == 1
    body = {"conversation_id": cid, "checkpoint_id": state["recover_checkpoint_id"]}
    assert client.post("/api/v1/chat/recover", headers=auth(client, "hr"), json=body).status_code == 404
    wrong = client.post("/api/v1/chat/recover", headers=h, json={**body, "checkpoint_id": "stale"})
    assert wrong.status_code == 409 and wrong.json()["detail"] == "CHECKPOINT_CHANGED"
    new = client.post("/api/v1/chat", headers=h, json={"conversation_id": cid, "message": "覆盖旧回答"})
    assert new.status_code == 409 and new.json()["detail"] == "ANSWER_NOT_SAVED"
    lease = ConversationLease.acquire(db.get_bind().engine, cid)
    try:
        assert client.post("/api/v1/chat/recover", headers=h, json=body).json()["detail"] == "CONVERSATION_BUSY"
    finally:
        lease.close()
    recovered = events(client.post("/api/v1/chat/recover", headers=h, json=body))
    assert recovered["done"]["answer"] == "已完成但未落库的回答。" and calls == 1
    assert client.post("/api/v1/chat/recover", headers=h, json=body).json()["detail"] == "RUN_NOT_RECOVERABLE"
    restored = client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()
    assert not restored["blocked"] and restored["recover_checkpoint_id"] is None
    assert sum(m["content"] == "已完成但未落库的回答。" for m in restored["messages"]) == 1
    assert sum(m["role"] == "user" for m in restored["messages"]) == 1


def test_recovery_save_failure_keeps_original_checkpoint_available(recovery_client, fail_final_insert):
    client, _ = recovery_client(lambda msgs: AIMessage("保存重试回答"))
    h = auth(client)
    cid = events(client.post("/api/v1/chat", headers=h, json={"message": "测试"}))["meta"]["conversation_id"]
    token = client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()["recover_checkpoint_id"]
    fail_final_insert["remaining"] = 1
    failed = events(client.post("/api/v1/chat/recover", headers=h, json={"conversation_id": cid, "checkpoint_id": token}))
    assert failed["error"]["code"] == "ANSWER_SAVE_FAILED"
    assert client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()["recover_checkpoint_id"] == token
    success = client.post("/api/v1/chat/recover", headers=h, json={"conversation_id": cid, "checkpoint_id": token})
    assert events(success)["done"]["answer"] == "保存重试回答"


def test_legacy_history_is_not_guessed_or_duplicated(recovery_client, db):
    client, _ = recovery_client(lambda msgs: AIMessage("历史回答"))
    h = auth(client)
    cid = events(client.post("/api/v1/chat", headers=h, json={"message": "历史问题"}))["meta"]["conversation_id"]
    conv = db.get(Conversation, cid)
    conv.active_turn_id = None  # Existing instances have no delivery marker after migration.
    db.commit()
    state = client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()
    assert state["recover_checkpoint_id"] is None and not state["blocked"]
    assert sum(m["content"] == "历史回答" for m in state["messages"]) == 1


@pytest.mark.parametrize("refusal", [False, True])
def test_completed_recovery_preserves_sources_refusal_and_original_model_output(recovery_client, db, fail_final_insert, refusal):
    calls = 0

    def script(msgs):
        nonlocal calls
        calls += 1
        if isinstance(msgs[-1], ToolMessage):
            return AIMessage("【无依据】没有保修期信息 [S1]。" if refusal else "按手册检查 [S1]。")
        return call("search_knowledge", {"query": "E17-42 排查"})

    client, _ = recovery_client(script)
    h = auth(client)
    cid = events(client.post("/api/v1/chat", headers=h, json={"message": "E17-42 排查"}))["meta"]["conversation_id"]
    state = client.get(f"/api/v1/conversations/{cid}/state", headers=h).json()
    recovered = events(client.post("/api/v1/chat/recover", headers=h,
                                    json={"conversation_id": cid, "checkpoint_id": state["recover_checkpoint_id"]}))
    assert calls == 2
    assert bool(recovered["sources"]) is not refusal
    if refusal:
        assert recovered["error"]["code"] == "INSUFFICIENT_EVIDENCE" and "[S1]" not in recovered["done"]["answer"]
    else:
        assert "[S1]" in recovered["done"]["answer"] and recovered["sources"][0]["document_title"].startswith("E17")
    saved = db.scalar(select(Message).where(Message.conversation_id == cid, Message.completion_turn_id.is_not(None)))
    assert saved.content == recovered["done"]["answer"] and saved.evidence_json == recovered["sources"]
    assert saved.error_code == ("INSUFFICIENT_EVIDENCE" if refusal else None)


def test_final_message_uniqueness_is_enforced_by_database(recovery_client, db):
    client, _ = recovery_client(lambda msgs: AIMessage("最终回答"))
    cid = events(client.post("/api/v1/chat", headers=auth(client), json={"message": "测试"}))["meta"]["conversation_id"]
    turn_id = db.get(Conversation, cid).active_turn_id
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(Message(conversation_id=cid, role="assistant", content="重复回答", completion_turn_id=turn_id))
        db.flush()
    # NULL markers remain compatible with old rows and error history.
    db.add_all([Message(conversation_id=cid, role="assistant", content="旧记录") for _ in range(2)])
    db.flush()


def test_delivery_migration_roundtrip_keeps_existing_messages_without_backfill(db):
    steps = runpy.run_path(str(Path(__file__).resolve().parents[1] / "alembic/versions/0005_answer_delivery.py"))
    conv = db.scalar(select(Conversation).limit(1))
    if conv is None:
        from knoventra.db.models import User
        conv = Conversation(user_id=db.scalar(select(User.id).limit(1)), title="旧会话")
        db.add(conv)
        db.flush()
    db.add(Message(conversation_id=conv.id, role="assistant", content="迁移前消息"))
    db.flush()
    with Operations.context(MigrationContext.configure(db.connection())):
        steps["downgrade"]()
        steps["upgrade"]()
    db.expire_all()
    assert db.get(Conversation, conv.id).active_turn_id is None
    row = db.scalar(select(Message).where(Message.conversation_id == conv.id, Message.content == "迁移前消息"))
    assert row is not None and row.completion_turn_id is None
