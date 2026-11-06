"""LangGraph agent: agent -> tools -> (prepare_escalation -> execute_escalation) -> cite_check.

User identity and AccessScope are injected by the server via `config["configurable"]` and recomputed
inside tools on every call; neither lives in model-editable state.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from knoventra.config import settings
from knoventra.db.models import Confirmation, Escalation, User
from knoventra.ingestion.encoder import Encoder
from knoventra.retrieval.pipeline import OK, retrieve
from knoventra.retrieval.rerank import Reranker

SYSTEM_PROMPT = """你是示例精密设备公司的内部知识助手。
规则：
1. 只依据 search_knowledge 返回的资料回答，不得用常识补全；每个论断后用 [S1] 这样的标记引用来源编号。
2. 若资料无法回答问题——检索返回 NO_ACCESS 或 INSUFFICIENT_EVIDENCE，或检索到的资料并不涉及所问内容（例如只是型号或故障码相近）——回答必须以「【无依据】」开头，说明没有可靠依据并拒答，不得使用 [S1] 之类的引用标记，建议联系对应职能团队（Ops/HR/FIN/IT），不要猜测。
3. 回答语言与字形跟随用户的提问（简体问简体答，繁体问繁体答）。
4. 用户明确要求转人工/转交专家时，立即调用 escalate_to_expert，即使信息不全也不要先追问；summary 依据已有对话写成可直接交给专家的问题摘要（确认卡会让用户核对）。其他情况不要调用。
5. 查询转交进度用 get_escalation_status。"""


@tool
def search_knowledge(query: str) -> str:
    """在用户有权访问的企业知识库中检索资料。"""


@tool
def get_escalation_status() -> str:
    """查询当前用户已提交的专家转交记录及状态。"""


class _EscArgs(BaseModel):
    summary: str = Field(description="交给专家的问题摘要")


@tool(args_schema=_EscArgs)
def escalate_to_expert(summary: str) -> str:
    """把当前问题转交给人工专家（需用户确认）。"""


TOOLS = [search_knowledge, get_escalation_status, escalate_to_expert]


class State(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    evidence: dict[str, dict]  # alias ("S1") -> evidence card; per turn, reset by the caller
    sources: list[dict]
    error_code: str | None
    pending: dict | None
    retrieval_trace: list[dict]  # per-turn diagnostics; never added to model messages
    model_declared_refusal: bool


@dataclass
class AgentDeps:
    session_factory: Callable[[], AbstractContextManager[Session]]
    encoder: Encoder
    reranker: Reranker | None
    llm: BaseChatModel
    checkpointer: Any


_MARK = re.compile(r"\[S(\d+)\]")
REFUSAL_MARK = "【无依据】"
REFUSAL_MARKS = (REFUSAL_MARK, "【無依據】")


def _card(e, alias: str) -> dict:
    return {"alias": alias, "chunk_id": e.chunk_id, "document_id": e.document_id,
            "document_title": e.document_title, "revision": e.revision, "page": e.page,
            "section_path": e.section_path, "text": e.text, "lang": e.lang}


def _turn_tool_count(messages) -> int:
    n = 0
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, ToolMessage):
            n += 1
    return n


def build_graph(deps: AgentDeps):
    def start_turn(state: State):
        # Command(resume=...) resumes at the interrupt and preserves this trace.
        # A new user turn always starts fresh, even for callers reusing a thread.
        return {"retrieval_trace": [], "model_declared_refusal": False}

    def agent(state: State):
        msgs = [SystemMessage(SYSTEM_PROMPT), *state["messages"]]
        return {"messages": [deps.llm.bind_tools(TOOLS).invoke(msgs)]}

    def after_agent(state: State):
        return "tools" if state["messages"][-1].tool_calls else "cite_check"

    def tools(state: State, config: RunnableConfig):
        calls = state["messages"][-1].tool_calls
        uid = config["configurable"]["user_id"]
        if _turn_tool_count(state["messages"]) + len(calls) > settings.max_tool_calls_per_turn:
            return {
                "messages": [ToolMessage("已超过本轮工具调用上限", tool_call_id=c["id"]) for c in calls]
                + [AIMessage("本轮工具调用次数已达上限，请缩小问题后重试。")],
                "error_code": "RUN_LIMIT",
            }
        evidence = dict(state.get("evidence") or {})
        trace = list(state.get("retrieval_trace") or [])
        out: list[ToolMessage] = []
        pending = None
        error = state.get("error_code")
        original_query = next((m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), "")
        for c in calls:
            if c["name"] == "search_knowledge":
                with deps.session_factory() as db:
                    res = retrieve(db, deps.encoder, deps.reranker, db.get(User, uid), c["args"].get("query", ""),
                                   original_query=original_query)
                trace.append({"tool_call_id": c["id"], "query": c["args"].get("query", ""),
                              "status": res.status, "reason": res.reason,
                              "required_identifiers": res.required_identifiers,
                              "missing_identifiers": res.missing_identifiers,
                              "returned_chunk_ids": [e.chunk_id for e in res.evidence]})
                if res.status != OK:
                    if not evidence:
                        error = res.status
                    out.append(ToolMessage(f"{res.status}: 没有可引用的资料。请拒答并建议联系对应职能团队。",
                                           tool_call_id=c["id"]))
                    continue
                error = None
                lines = []
                for e in res.evidence:
                    alias = next((a for a, v in evidence.items() if v["chunk_id"] == e.chunk_id), None)
                    if alias is None:
                        alias = f"S{len(evidence) + 1}"
                        evidence[alias] = _card(e, alias)
                    lines.append(f"[{alias}] 《{e.document_title}》修订{e.revision} 第{e.page}页 {e.section_path}\n{e.text}")
                out.append(ToolMessage("\n\n".join(lines), tool_call_id=c["id"]))
            elif c["name"] == "get_escalation_status":
                with deps.session_factory() as db:
                    rows = db.scalars(select(Escalation).where(Escalation.user_id == uid)
                                      .order_by(Escalation.id.desc()).limit(5)).all()
                out.append(ToolMessage("\n".join(f"{r.id}: {r.status} - {r.summary[:60]}" for r in rows) or "暂无转交记录",
                                       tool_call_id=c["id"]))
            elif c["name"] == "escalate_to_expert" and pending is None:
                pending = {"tool_call_id": c["id"], "args": c["args"]}
            else:
                out.append(ToolMessage("未知或重复的工具调用，已忽略", tool_call_id=c["id"]))
        return {"messages": out, "evidence": evidence, "pending": pending, "error_code": error,
                "retrieval_trace": trace}

    def after_tools(state: State):
        if state.get("error_code") == "RUN_LIMIT":
            return END
        return "prepare_escalation" if state.get("pending") else "agent"

    def prepare_escalation(state: State, config: RunnableConfig):
        cfg = config["configurable"]
        p = state["pending"]
        op_id = hashlib.sha256(f"{cfg['thread_id']}:{p['tool_call_id']}".encode()).hexdigest()[:32]
        summary = str(p["args"].get("summary", ""))[:2000]
        with deps.session_factory() as db:
            conf = db.scalar(select(Confirmation).where(Confirmation.user_id == cfg["user_id"],
                                                        Confirmation.operation_id == op_id))
            if conf is None:  # get-or-create: this node re-runs from the top on resume
                conf = Confirmation(
                    user_id=cfg["user_id"], thread_id=cfg["thread_id"], operation_id=op_id, summary=summary,
                    status="PENDING",
                    expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=settings.confirm_ttl_minutes))
                db.add(conf)
            db.commit()
            conf_id = conf.id
        decision = interrupt({"confirmation_id": conf_id, "summary": summary, "operation_id": op_id})
        return {"pending": {**p, "operation_id": op_id, "summary": summary, "confirmation_id": conf_id,
                            "approve": bool(decision.get("approve"))}}

    def execute_escalation(state: State, config: RunnableConfig):
        p = state["pending"]
        uid = config["configurable"]["user_id"]
        if not p["approve"]:
            msg = "用户已取消转交，未创建转交记录。"
        else:
            refs = [v["chunk_id"] for v in (state.get("evidence") or {}).values()]
            eid = hashlib.sha256(f"{uid}:{p['operation_id']}".encode()).hexdigest()[:32]
            with deps.session_factory() as db:
                db.execute(pg_insert(Escalation)
                           .values(id=eid, user_id=uid, operation_id=p["operation_id"], summary=p["summary"],
                                   evidence_refs=refs, status="OPEN")
                           .on_conflict_do_nothing(index_elements=["user_id", "operation_id"]))
                esc = db.scalar(select(Escalation).where(Escalation.user_id == uid,
                                                         Escalation.operation_id == p["operation_id"]))
                db.commit()
                msg = f"已创建转交记录 {esc.id}，状态 {esc.status}。"
        return {"messages": [ToolMessage(msg, tool_call_id=p["tool_call_id"])], "pending": None}

    def cite_check(state: State):
        reg = state.get("evidence") or {}
        last = state["messages"][-1]
        cited: list[str] = []

        def sub(m):
            a = f"S{m.group(1)}"
            if a in reg:
                if a not in cited:
                    cited.append(a)
                return m.group(0)
            return ""  # forged / unknown marker is never rendered as a source

        content = last.content if isinstance(last.content, str) else str(last.content)
        stripped = content.lstrip()
        refusal_mark = next((mark for mark in REFUSAL_MARKS if stripped.startswith(mark)), None)
        if refusal_mark:
            # Model-declared refusal: show no source cards (they would "prove" an absence) and flag the turn.
            text = _MARK.sub("", stripped[len(refusal_mark):]).strip()
            return {"sources": [], "error_code": state.get("error_code") or "INSUFFICIENT_EVIDENCE",
                    "model_declared_refusal": True,
                    "messages": [AIMessage(text, id=last.id)]}
        text = _MARK.sub(sub, content)
        upd: dict[str, Any] = {"sources": [reg[a] for a in cited]}
        if text != content:
            upd["messages"] = [AIMessage(text, id=last.id)]
        return upd

    g = StateGraph(State)
    for name, fn in [("start_turn", start_turn), ("agent", agent), ("tools", tools), ("prepare_escalation", prepare_escalation),
                     ("execute_escalation", execute_escalation), ("cite_check", cite_check)]:
        g.add_node(name, fn)
    g.add_edge(START, "start_turn")
    g.add_edge("start_turn", "agent")
    g.add_conditional_edges("agent", after_agent, ["tools", "cite_check"])
    g.add_conditional_edges("tools", after_tools, ["agent", "prepare_escalation", END])
    g.add_edge("prepare_escalation", "execute_escalation")
    g.add_edge("execute_escalation", "agent")
    g.add_edge("cite_check", END)
    return g.compile(checkpointer=deps.checkpointer)
