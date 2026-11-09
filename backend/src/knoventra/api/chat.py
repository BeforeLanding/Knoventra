from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from knoventra.access.scope import NoAccess, compute_scope
from knoventra.agent.checkpoint import make_checkpointer
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.agent.model import chat_model_kwargs
from knoventra.api.deps import current_user, get_db
from knoventra.api.run_lock import ConversationLease, ConversationStreamingResponse
from knoventra.config import settings
from knoventra.db.models import Chunk, Confirmation, Conversation, Document, Escalation, Message, Revision, User
from knoventra.db.session import SessionLocal
from knoventra.ingestion.encoder import get_encoder
from knoventra.retrieval.rerank import get_reranker

router = APIRouter(prefix="/api/v1")


@contextmanager
def _session():
    with SessionLocal() as db:
        yield db


@lru_cache(maxsize=1)
def get_graph():
    """Process-wide compiled graph. Tests override this dependency."""
    if not settings.llm_model:
        raise HTTPException(503, "LLM_NOT_CONFIGURED")
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(**chat_model_kwargs(), max_retries=1, streaming=True)
    saver, _pool = make_checkpointer(settings.database_url)
    return build_graph(AgentDeps(_session, get_encoder(), get_reranker(), llm, saver))


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _public(card: dict) -> dict:
    return {**{k: card[k] for k in ("alias", "chunk_id", "document_title", "revision", "page", "section_path", "lang")},
            "snippet": card["text"][:240]}


def _confirmation_card(conf: Confirmation) -> dict:
    return {"confirmation_id": conf.id, "summary": conf.summary, "status": conf.status,
            "expires_at": conf.expires_at.isoformat(),
            "expired": conf.status == "PENDING" and conf.expires_at <= dt.datetime.now(dt.UTC)}


def _history(db: Session, conv: Conversation) -> list[dict]:
    rows = db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at, Message.id)).all()
    return [{"role": m.role, "content": m.content, "sources": m.evidence_json or [], "error_code": m.error_code} for m in rows]


def _interrupts(snapshot):
    return [interruption for task in snapshot.tasks for interruption in task.interrupts]


def _checkpoint_id(snapshot) -> str | None:
    return (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")


def _delivery_pending(db: Session, conv: Conversation, snapshot) -> bool:
    if snapshot.next or not conv.active_turn_id:
        return False
    messages = snapshot.values.get("messages") or []
    if not messages or not isinstance(messages[-1], AIMessage) or messages[-1].tool_calls:
        return False
    human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    if human is None or human.id != conv.active_turn_id:
        return False
    return db.scalar(select(Message.id).where(Message.conversation_id == conv.id,
                                              Message.completion_turn_id == conv.active_turn_id).limit(1)) is None


def _deliver_answer(db: Session, conv: Conversation, snapshot) -> Iterator[str]:
    state = snapshot.values
    answer = state["messages"][-1].content
    sources = [_public(c) for c in state.get("sources", [])]
    db.execute(pg_insert(Message).values(conversation_id=conv.id, role="assistant", content=answer,
                                        evidence_json=sources, error_code=state.get("error_code"),
                                        completion_turn_id=conv.active_turn_id)
               .on_conflict_do_nothing(constraint="uq_knv_message_completion"))
    db.commit()
    yield sse("sources", sources)
    if state.get("error_code"):
        yield sse("error", {"code": state["error_code"]})
    yield sse("done", {"answer": answer})


def _stream_run(graph, db: Session, conv: Conversation, user_id: str, inp, *, user_text: str | None) -> Iterator[str]:
    cfg = {"configurable": {"thread_id": conv.thread_id, "user_id": user_id}}
    yield sse("meta", {"conversation_id": conv.id, "run_id": uuid.uuid4().hex})
    if user_text is not None:
        conv.active_turn_id = inp["messages"][0].id
        db.add(Message(id=conv.active_turn_id, conversation_id=conv.id, role="user", content=user_text))
        db.commit()
    failure_code = "RETRIEVAL_UNAVAILABLE"
    try:
        interrupted = None
        for mode, payload in graph.stream(inp, cfg, stream_mode=["messages", "updates"]):
            if mode == "messages":
                msg, meta = payload
                if meta.get("langgraph_node") == "agent" and isinstance(msg, (AIMessageChunk, AIMessage)) \
                        and isinstance(msg.content, str) and msg.content and not msg.tool_calls:
                    yield sse("token", {"text": msg.content})
            else:
                if "__interrupt__" in payload:
                    interrupted = payload["__interrupt__"][0].value
                elif "agent" in payload:
                    for m in payload["agent"]["messages"]:
                        for tc in getattr(m, "tool_calls", []):
                            yield sse("status", {"tool": tc["name"]})
        snapshot = graph.get_state(cfg)
        if interrupted is not None:
            conf = db.scalar(select(Confirmation).where(Confirmation.id == interrupted["confirmation_id"],
                                                        Confirmation.user_id == user_id,
                                                        Confirmation.thread_id == conv.thread_id))
            if conf is None:
                raise ValueError("confirmation does not belong to this conversation")
            yield sse("confirm", _confirmation_card(conf))
            return
        failure_code = "ANSWER_SAVE_FAILED"
        yield from _deliver_answer(db, conv, snapshot)
    except Exception as e:  # surface as a typed SSE error rather than a broken stream
        db.rollback()
        db.add(Message(conversation_id=conv.id, role="assistant", content="", evidence_json=[],
                       error_code=failure_code))
        db.commit()
        yield sse("error", {"code": failure_code, "detail": type(e).__name__})
        yield sse("done", {"answer": ""})


class ChatIn(BaseModel):
    conversation_id: str | None = None
    message: str = Field(strict=True, min_length=1, description="Nonblank question; character limit is exposed by /me")

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must contain non-whitespace text")
        if "\x00" in value:
            raise ValueError("message must not contain NUL")
        if len(value) > settings.chat_max_chars:
            raise ValueError(f"message exceeds {settings.chat_max_chars} characters")
        return value


def _own_conversation(db: Session, user: User, cid: str) -> Conversation:
    conv = db.scalar(select(Conversation).where(Conversation.id == cid, Conversation.user_id == user.id))
    if conv is None:
        raise HTTPException(404)
    return conv


@router.post("/chat")
def chat(body: ChatIn, user: User = Depends(current_user), db: Session = Depends(get_db),
         graph=Depends(get_graph)):
    if body.conversation_id:
        conv = _own_conversation(db, user, body.conversation_id)
    else:
        conv = Conversation(user_id=user.id, thread_id=uuid.uuid4().hex, title=body.message[:30])
        db.add(conv)
        db.commit()
    lease = ConversationLease.acquire(db.get_bind().engine, conv.id)
    try:
        snapshot = graph.get_state({"configurable": {"thread_id": conv.thread_id}})
        if snapshot.next:
            raise HTTPException(409, "pending confirmation must be resolved first" if _interrupts(snapshot)
                                else "RUN_INCOMPLETE")
        if _delivery_pending(db, conv, snapshot):
            raise HTTPException(409, "ANSWER_NOT_SAVED")
        inp = {"messages": [HumanMessage(body.message, id=uuid.uuid4().hex)], "evidence": {}, "sources": [], "error_code": None}
        return ConversationStreamingResponse(_stream_run(graph, db, conv, user.id, inp, user_text=body.message), lease)
    except BaseException:
        lease.close()
        raise


class ConfirmIn(BaseModel):
    confirmation_id: str
    approve: bool


class RetryIn(BaseModel):
    conversation_id: str
    checkpoint_id: str


@router.post("/chat/recover")
def recover(body: RetryIn, user: User = Depends(current_user), db: Session = Depends(get_db),
            graph=Depends(get_graph)):
    conv = _own_conversation(db, user, body.conversation_id)
    lease = ConversationLease.acquire(db.get_bind().engine, conv.id)
    try:
        snapshot = graph.get_state({"configurable": {"thread_id": conv.thread_id, "user_id": user.id}})
        if not body.checkpoint_id or _checkpoint_id(snapshot) != body.checkpoint_id:
            raise HTTPException(409, "CHECKPOINT_CHANGED")
        if not _delivery_pending(db, conv, snapshot):
            raise HTTPException(409, "RUN_NOT_RECOVERABLE")

        def gen():
            yield sse("meta", {"conversation_id": conv.id, "run_id": uuid.uuid4().hex})
            try:
                yield from _deliver_answer(db, conv, snapshot)
            except Exception:
                db.rollback()
                yield sse("error", {"code": "ANSWER_SAVE_FAILED"})
                yield sse("done", {"answer": ""})

        return ConversationStreamingResponse(gen(), lease)
    except BaseException:
        lease.close()
        raise


@router.post("/chat/retry")
def retry(body: RetryIn, user: User = Depends(current_user), db: Session = Depends(get_db),
          graph=Depends(get_graph)):
    conv = _own_conversation(db, user, body.conversation_id)
    lease = ConversationLease.acquire(db.get_bind().engine, conv.id)
    try:
        snapshot = graph.get_state({"configurable": {"thread_id": conv.thread_id, "user_id": user.id}})
        if not snapshot.next or _interrupts(snapshot):
            raise HTTPException(409, "RUN_NOT_RETRYABLE")
        if not body.checkpoint_id or _checkpoint_id(snapshot) != body.checkpoint_id:
            raise HTTPException(409, "CHECKPOINT_CHANGED")
        # No new HumanMessage or resume decision. LangGraph replays pending
        # nodes with the original operation IDs, tool budget and confirmed state.
        return ConversationStreamingResponse(_stream_run(graph, db, conv, user.id, None, user_text=None), lease)
    except BaseException:
        lease.close()
        raise


@router.post("/chat/confirm")
def confirm(body: ConfirmIn, user: User = Depends(current_user), db: Session = Depends(get_db),
            graph=Depends(get_graph)):
    conf = db.scalar(select(Confirmation).where(Confirmation.id == body.confirmation_id,
                                                Confirmation.user_id == user.id))
    if conf is None:
        raise HTTPException(404)
    conv = db.scalar(select(Conversation).where(Conversation.thread_id == conf.thread_id,
                                                Conversation.user_id == user.id))
    if conv is None:
        raise HTTPException(404)
    lease = ConversationLease.acquire(db.get_bind().engine, conv.id)
    try:
        db.refresh(conf)
        cfg = {"configurable": {"thread_id": conf.thread_id, "user_id": user.id}}
        snapshot = graph.get_state(cfg)
        active_ids = {interruption.value.get("confirmation_id")
                      for interruption in _interrupts(snapshot)
                      if isinstance(interruption.value, dict)}
        still_waiting = conf.id in active_ids
        if not still_waiting and (snapshot.next or conf.status == "PENDING"):
            # A previous confirmation from this thread must never resume the
            # current interrupt or change a now-orphaned pending decision.
            raise HTTPException(409, "CONFIRMATION_NOT_ACTIVE")
        now = dt.datetime.now(dt.UTC)
        expired = conf.status == "PENDING" and conf.expires_at <= now
        approve = body.approve and not expired
        # Claim the decision only after excluding concurrent chat/resume runs.
        target = "EXPIRED" if expired else ("APPROVED" if approve else "REJECTED")
        db.execute(update(Confirmation).where(Confirmation.id == conf.id, Confirmation.status == "PENDING")
                   .values(status=target))
        db.commit()
        db.refresh(conf)
    except BaseException:
        lease.close()
        raise

    def gen() -> Iterator[str]:
        if expired:
            yield sse("error", {"code": "CONFIRMATION_EXPIRED"})
        if not still_waiting:  # duplicate submit / refresh: the work is already done, replay nothing
            yield sse("meta", {"conversation_id": conv.id, "status": conf.status})
            yield sse("done", {"answer": ""})
            return
        # Resume with the stored decision (not the request body), so a retry can never flip it.
        stored = {"approve": conf.status == "APPROVED"}
        yield from _stream_run(graph, db, conv, user.id, Command(resume=stored), user_text=None)

    return ConversationStreamingResponse(gen(), lease)


@router.get("/conversations")
def conversations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Conversation).where(Conversation.user_id == user.id)
                      .order_by(Conversation.created_at.desc())).all()
    return [{"id": c.id, "title": c.title, "created_at": c.created_at} for c in rows]


@router.get("/conversations/page")
def conversation_page(before: str | None = None, limit: int = Query(20, ge=1, le=100),
                      q: str = Query("", max_length=128, pattern=r"^[^\x00]*$"),
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = select(Conversation).where(Conversation.user_id == user.id)
    term = q.strip()
    if term:
        query = query.where(Conversation.title.icontains(term, autoescape=True))
    if before is not None:
        anchor = db.scalar(query.where(Conversation.id == before))
        if anchor is None:
            raise HTTPException(404)
        query = query.where(or_(Conversation.created_at < anchor.created_at,
                                and_(Conversation.created_at == anchor.created_at, Conversation.id < anchor.id)))
    rows = db.scalars(query.order_by(Conversation.created_at.desc(), Conversation.id.desc()).limit(limit + 1)).all()
    return {"items": [{"id": row.id, "title": row.title, "created_at": row.created_at} for row in rows[:limit]],
            "next_cursor": rows[limit - 1].id if len(rows) > limit else None}


@router.get("/conversations/{cid}/messages")
def messages(cid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    conv = _own_conversation(db, user, cid)
    return _history(db, conv)


@router.get("/conversations/{cid}/state")
def conversation_state(cid: str, user: User = Depends(current_user), db: Session = Depends(get_db),
                       graph=Depends(get_graph)):
    """Read the owned checkpoint's actual interrupt; never create/resume a run."""
    conv = _own_conversation(db, user, cid)
    snapshot = graph.get_state({"configurable": {"thread_id": conv.thread_id, "user_id": user.id}})
    pending = None
    for task in snapshot.tasks:
        for interruption in task.interrupts:
            value = interruption.value
            if not isinstance(value, dict) or not value.get("confirmation_id"):
                continue
            conf = db.scalar(select(Confirmation).where(Confirmation.id == value["confirmation_id"],
                                                        Confirmation.user_id == user.id,
                                                        Confirmation.thread_id == conv.thread_id))
            if conf is not None:
                pending = _confirmation_card(conf)
                break
    delivery = _delivery_pending(db, conv, snapshot)
    return {"conversation_id": conv.id, "messages": _history(db, conv),
            "blocked": bool(snapshot.next) or delivery, "pending_confirmation": pending,
            "recover_checkpoint_id": _checkpoint_id(snapshot) if delivery else None,
            "retry_checkpoint_id": _checkpoint_id(snapshot) if snapshot.next and not _interrupts(snapshot) else None}


@router.get("/evidence/{chunk_id}")
def evidence(chunk_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Source preview. Authorised afresh on every call; unreadable and nonexistent are indistinguishable."""
    try:
        scope = compute_scope(db, user)
    except NoAccess:
        raise HTTPException(404) from None
    row = db.execute(
        select(Chunk, Revision, Document).join(Revision, Revision.id == Chunk.revision_id)
        .join(Document, Document.id == Revision.document_id)
        .where(Chunk.id == chunk_id, Chunk.kb_id.in_(scope.readable_kb_ids),
               Revision.status.in_(("PUBLISHED", "RETIRED")))).first()
    if row is None:
        raise HTTPException(404)
    c, r, d = row
    return {"chunk_id": c.id, "document_title": d.title, "revision": r.version, "revision_status": r.status,
            "page": c.page, "section_path": c.section_path, "text": c.text, "lang": d.source_lang}


@router.get("/escalations/{eid}")
def escalation(eid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = db.scalar(select(Escalation).where(Escalation.id == eid, Escalation.user_id == user.id))
    if e is None:
        raise HTTPException(404)
    return _escalation_record(e)


def _escalation_record(e: Escalation) -> dict:
    return {"id": e.id, "summary": e.summary, "status": e.status,
            "evidence_refs": e.evidence_refs or [], "created_at": e.created_at}


@router.get("/escalations")
def escalations(before: str | None = None, limit: int = Query(20, ge=1, le=100),
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = select(Escalation).where(Escalation.user_id == user.id)
    if before is not None:
        anchor = db.scalar(select(Escalation).where(Escalation.id == before, Escalation.user_id == user.id))
        if anchor is None:
            raise HTTPException(404)
        if anchor.created_at is None:
            query = query.where(Escalation.created_at.is_(None), Escalation.id < anchor.id)
        else:
            query = query.where(or_(Escalation.created_at < anchor.created_at, Escalation.created_at.is_(None),
                                    and_(Escalation.created_at == anchor.created_at, Escalation.id < anchor.id)))
    rows = db.scalars(query.order_by(Escalation.created_at.desc().nulls_last(), Escalation.id.desc())
                      .limit(limit + 1)).all()
    return {"items": [_escalation_record(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1].id if len(rows) > limit else None}
