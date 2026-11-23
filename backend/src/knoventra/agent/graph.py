"""LangGraph agent: agent -> tools -> (prepare_escalation -> execute_escalation) -> cite_check.

User identity and AccessScope are injected by the server via `config["configurable"]` and recomputed
inside tools on every call; neither lives in model-editable state.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import time
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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from knoventra.config import settings
from knoventra.agent.language import format_answer
from knoventra.agent.escalation_intent import direct_escalation_summary
from knoventra.agent.model_variants import clarification_needed, clarification_text, stated_models
from knoventra.agent.query_plan import RETRIEVAL_INTENTS, plan_query
from knoventra.help import slots as help_slots
from knoventra.help.service import create_request
from knoventra.skills import can_view_skills, format_hits
from knoventra.skills import lookup_skills as lookup_skills_in_db
from knoventra.db.models import Confirmation, Escalation, User
from knoventra.ingestion.encoder import Encoder
from knoventra.retrieval.pipeline import OK, retrieve
from knoventra.retrieval.rerank import Reranker

CITATION_POLICY = "retrieval-batch-v1"

SYSTEM_PROMPT = """你是示例精密设备公司的内部知识助手。
规则：
1. 只依据 search_knowledge 返回的资料回答，不得用常识补全；每个论断后用 [S1] 这样的标记引用来源编号。
2. 若资料无法回答问题——检索返回 NO_ACCESS 或 INSUFFICIENT_EVIDENCE，或检索到的资料并不涉及所问内容（例如只是型号或故障码相近）——回答必须以「【无依据】」开头，说明没有可靠依据并拒答，不得使用 [S1] 之类的引用标记，建议联系对应职能团队（Ops/HR/FIN/IT），不要猜测。
3. 回答语言与字形跟随用户的提问（简体问简体答，繁体问繁体答）。
4. 用户明确要求转人工/转交专家时，立即调用 escalate_to_expert，即使信息不全也不要先追问；summary 依据已有对话写成可直接交给专家的问题摘要（确认卡会让用户核对）。其他情况不要调用。
5. 查询转交进度用 get_escalation_status。只报告工具实际返回的记录和状态；转交工具仅创建待处理记录，不代表已通知、已受理或已分派到特定团队，不承诺响应时间。没有执行相应工具时，不承诺能修改已提交的转交，也不要让用户在已完成的确认卡中补充内容。
6. 严格保留用户问题中的时间、频率、对象和编码约束，不自行增加条件（例如“日常”不等于“每日”）。用户未明确要求全部项目或完整清单时，不擅自增加完整性要求。章节标题给出的类别与正文频率可以同时成立，例如“日常检查”章节中的“每周检查”仍属于该类检查；回答时保留原频率，不因它不是每日而判作无关。资料只支持问题的一部分时，先引用并回答可证实的范围，对其余部分拒绝推断，不因无法证明清单完整就整体拒答。
8. 现场故障查不到可行方案、或用户要发起求助单时，调用 request_help，把对话中已知的机台号、告警、现象、已尝试步骤填进去；工具会告诉你还缺什么，一次只向用户追问一项，不要编造。
7. 用户问谁会做某项技能、该找谁时，调用 lookup_skills，只报告工具返回的人员、掌握程度和作业次数，不推测其他人选。"""


class _ToolArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    @field_validator("*", check_fields=False)
    @classmethod
    def nonblank(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("text must be nonblank")
        return value


class _SearchArgs(_ToolArgs):
    query: str = Field(min_length=1, max_length=4000, description="检索问题或关键词")


class _StatusArgs(_ToolArgs):
    pass


@tool(args_schema=_SearchArgs)
def search_knowledge(query: str) -> str:
    """在用户有权访问的企业知识库中检索资料。"""


@tool(args_schema=_StatusArgs)
def get_escalation_status() -> str:
    """查询当前用户已提交的专家转交记录及状态。"""


class _EscArgs(_ToolArgs):
    summary: str = Field(min_length=1, max_length=2000, description="交给专家的问题摘要")


@tool(args_schema=_EscArgs)
def escalate_to_expert(summary: str) -> str:
    """创建待处理的专家转交记录（需用户确认，不通知或分派具体团队）。"""


class _SkillArgs(_ToolArgs):
    skill: str = Field(min_length=1, max_length=200, description="要查的技能项，如 golden check")


@tool(args_schema=_SkillArgs)
def lookup_skills(skill: str) -> str:
    """在技能矩阵中查询掌握或了解某项技能的工程师（结构化查询，不检索文档）。"""


class _HelpArgs(_ToolArgs):
    machine_id: str | None = Field(default=None, max_length=64, description="机台号")
    symptom: str | None = Field(default=None, max_length=500, description="故障现象")
    alarm: str | None = Field(default=None, max_length=200, description="告警码或告警信息")
    tried_steps: str | None = Field(default=None, max_length=500, description="已尝试的步骤")


@tool(args_schema=_HelpArgs)
def request_help(machine_id: str | None = None, symptom: str | None = None, alarm: str | None = None,
                 tried_steps: str | None = None) -> str:
    """填写现场求助单草稿；信息齐全后弹出确认卡，用户确认后才建单。"""


TOOLS = [search_knowledge, get_escalation_status, escalate_to_expert, lookup_skills, request_help]
_TOOL_SCHEMAS = {tool.name: tool.args_schema for tool in TOOLS}


def _valid_tool_batch(calls: list[dict]) -> bool:
    ids = set()
    escalations = 0
    for call in calls:
        call_id = call.get("id")
        schema = _TOOL_SCHEMAS.get(call.get("name"))
        if not isinstance(call_id, str) or not call_id.strip() or call_id in ids or schema is None:
            return False
        ids.add(call_id)
        try:
            schema.model_validate(call.get("args"))
        except ValidationError:
            return False
        escalations += call["name"] == "escalate_to_expert"
        if escalations > 1:
            return False  # One confirmation card per model response.
    return True


def _invalid_tool_response(response: AIMessage) -> dict:
    return {"messages": [AIMessage("模型返回的工具调用不符合要求，本轮已停止。若已确认转交，请先查看转交记录。", id=response.id)],
            "error_code": "MODEL_TOOL_CALL_INVALID", "sources": [], "pending": None,
            "model_declared_refusal": False}


class State(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    evidence: dict[str, dict]  # alias ("S1") -> evidence card; per turn, reset by the caller
    sources: list[dict]
    error_code: str | None
    pending: dict | None
    retrieval_trace: list[dict]  # per-turn diagnostics; never added to model messages
    model_declared_refusal: bool
    model_finish_reason: str | None
    query_plan: dict | None  # per-turn planner trace; never added to model messages
    clarification: dict | None  # set when the answer depends on an unstated machine model
    help_draft: dict | None  # field help slots across turns; server-merged, cleared on create or cancel
    route_kbs: list[str] | None  # server-chosen KB names for this turn's searches (narrowing only)


@dataclass
class AgentDeps:
    session_factory: Callable[[], AbstractContextManager[Session]]
    encoder: Encoder
    reranker: Reranker | None
    llm: BaseChatModel
    checkpointer: Any
    planner: BaseChatModel | None = None  # query planning; None keeps v3 behaviour


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
        return {"retrieval_trace": [], "model_declared_refusal": False, "model_finish_reason": None,
                "query_plan": None, "route_kbs": None, "clarification": None}

    def query_plan(state: State):
        if deps.planner is None:
            return {"query_plan": {"status": "skipped", "reason": "disabled"}}
        if direct_escalation_summary(state["messages"]) is not None:
            return {"query_plan": {"status": "skipped", "reason": "explicit_escalation"}}
        if state.get("help_draft"):
            return {"query_plan": {"status": "skipped", "reason": "help_draft"}}
        plan, trace = plan_query(deps.planner, state["messages"])
        if plan is not None and plan.intent == "skill_lookup" \
                and plan.confidence >= settings.query_plan_min_confidence:
            call = {"name": "lookup_skills", "args": {"skill": plan.skill_item or plan.rewritten},
                    "id": "plan-skill", "type": "tool_call"}
            return {"query_plan": trace, "messages": [AIMessage("", tool_calls=[call])]}
        if plan is None or plan.intent not in RETRIEVAL_INTENTS:
            return {"query_plan": trace}
        # Retrieval intents: search the planned queries directly; the agent answers from the results.
        calls = [{"name": "search_knowledge", "args": {"query": q}, "id": f"plan-{i}", "type": "tool_call"}
                 for i, q in enumerate(plan.queries, start=1)]
        return {"query_plan": trace, "route_kbs": plan.route_kbs, "messages": [AIMessage("", tool_calls=calls)]}

    def after_plan(state: State):
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "agent"

    def agent(state: State):
        last_human = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
        if state.get("help_draft") and last_human is not None and isinstance(state["messages"][-1], HumanMessage) \
                and help_slots.CANCEL.match(str(last_human.content)):
            return {"messages": [AIMessage("已取消求助单草稿，没有创建求助单。")], "help_draft": None,
                    "model_finish_reason": None}
        summary = direct_escalation_summary(state["messages"])
        if summary is not None:
            return {"messages": [AIMessage("", tool_calls=[{"name": "escalate_to_expert",
                       "args": {"summary": summary}, "id": "explicit-escalation", "type": "tool_call"}])],
                    "model_finish_reason": None}
        msgs = [SystemMessage(SYSTEM_PROMPT), *state["messages"]]
        response = deps.llm.bind_tools(TOOLS).invoke(msgs)
        reason = response.response_metadata.get("finish_reason")
        reason = reason if isinstance(reason, str) else None
        error = None
        if reason == "length":
            error = "MODEL_OUTPUT_TRUNCATED"
            text = "模型输出因长度限制中断，本轮未生成完整回答。若已确认转交，请先查看转交记录。"
        elif response.invalid_tool_calls or not _valid_tool_batch(response.tool_calls):
            error = "MODEL_TOOL_CALL_INVALID"
            text = "模型返回的工具调用不符合要求，本轮已停止。若已确认转交，请先查看转交记录。"
        if error:
            # A partial call must never reach tools, even if another call parsed successfully.
            # Keep evidence/trace for diagnostics, but never present it as support for this failure.
            return {"messages": [AIMessage(text, id=response.id)], "error_code": error,
                    "sources": [], "pending": None, "model_declared_refusal": False,
                    "model_finish_reason": reason}
        return {"messages": [response], "model_finish_reason": reason}

    def after_agent(state: State):
        return "tools" if state["messages"][-1].tool_calls else "cite_check"

    def tools(state: State, config: RunnableConfig):
        calls = state["messages"][-1].tool_calls
        # Also guard tools-node retries/checkpoints created before agent-side validation existed.
        if not _valid_tool_batch(calls):
            return _invalid_tool_response(state["messages"][-1])
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
        stated = stated_models(state["messages"])
        model = stated[0] if len(stated) == 1 else None  # two named models = a comparison: no filter
        clarification = state.get("clarification")
        help_update = state.get("help_draft")
        for c in calls:
            if c["name"] == "search_knowledge":
                started = time.perf_counter()
                with deps.session_factory() as db:
                    res = retrieve(db, deps.encoder, deps.reranker, db.get(User, uid), c["args"].get("query", ""),
                                   original_query=original_query, route_kbs=state.get("route_kbs"),
                                   machine_model=model)
                    if res.status == OK and clarification is None:
                        clarification = clarification_needed(db, res.evidence, stated)
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                trace.append({"tool_call_id": c["id"], "query": c["args"].get("query", ""),
                              "latency_ms": elapsed_ms,
                              "status": res.status, "reason": res.reason,
                              "required_identifiers": res.required_identifiers,
                              "missing_identifiers": res.missing_identifiers,
                              "returned_chunk_ids": [e.chunk_id for e in res.evidence],
                              "degraded": res.degraded, "route": res.route, "machine_model": model})
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
                                      .order_by(Escalation.created_at.desc().nulls_last(), Escalation.id.desc()).limit(5)).all()
                out.append(ToolMessage("\n".join(f"{r.id}: {r.status} - {r.summary[:60]}"
                                                 + ("（现场求助）" if r.kind == "field_help" else "")
                                                 for r in rows) or "暂无转交记录",
                                       tool_call_id=c["id"]))
            elif c["name"] == "lookup_skills":
                with deps.session_factory() as db:
                    user = db.get(User, uid)
                    text = (format_hits(lookup_skills_in_db(db, c["args"]["skill"])) if can_view_skills(user)
                            else "NO_ACCESS: 技能表仅对 Ops 部门开放。")
                out.append(ToolMessage(text, tool_call_id=c["id"]))
            elif c["name"] == "request_help" and pending is None:
                draft = help_slots.merge(state.get("help_draft"), c["args"], state["messages"])
                need = help_slots.missing(draft)
                if need:
                    help_update = draft
                    out.append(ToolMessage("求助单草稿已更新。还缺："
                                           + "、".join(help_slots.LABELS[k] for k in need)
                                           + "。请一次只向用户追问一项，不要编造。", tool_call_id=c["id"]))
                    continue
                help_update = draft
                turn_id = next(m.id for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
                op_id = hashlib.sha256(f"{config['configurable']['thread_id']}:{turn_id}:{c['id']}".encode()).hexdigest()[:32]
                pending = {"tool_call_id": c["id"], "args": {"summary": help_slots.card(draft)},
                           "operation_id": op_id, "kind": "field_help", "slots": draft}
            elif c["name"] == "escalate_to_expert" and pending is None:
                turn_id = next(m.id for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
                op_id = hashlib.sha256(f"{config['configurable']['thread_id']}:{turn_id}:{c['id']}".encode()).hexdigest()[:32]
                pending = {"tool_call_id": c["id"], "args": c["args"], "operation_id": op_id}
            else:
                out.append(ToolMessage("未知或重复的工具调用，已忽略", tool_call_id=c["id"]))
        return {"messages": out, "evidence": evidence, "pending": pending, "error_code": error,
                "retrieval_trace": trace, "clarification": clarification, "help_draft": help_update}

    def after_tools(state: State):
        if state.get("error_code") in {"RUN_LIMIT", "MODEL_TOOL_CALL_INVALID"}:
            return END
        if state.get("pending"):
            return "prepare_escalation"
        return "clarify" if state.get("clarification") else "agent"

    def clarify(state: State):
        # Answering would mix model-specific steps; ask instead and show no sources.
        question = next((m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), "")
        return {"messages": [AIMessage(format_answer(question, clarification_text(state["clarification"])))],
                "sources": [], "error_code": None}

    def prepare_escalation(state: State, config: RunnableConfig):
        cfg = config["configurable"]
        p = state["pending"]
        # Older checkpoints predate turn-scoped IDs. Preserve their legacy
        # operation on resume rather than create a replacement confirmation.
        op_id = p.get("operation_id") or hashlib.sha256(f"{cfg['thread_id']}:{p['tool_call_id']}".encode()).hexdigest()[:32]
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
            kind = p.get("kind", "expert_escalation")
            with deps.session_factory() as db:
                esc = create_request(db, request_id=eid, requester_id=uid, operation_id=p["operation_id"],
                                     kind=kind, summary=p["summary"], evidence_refs=refs, slots=p.get("slots"))
                db.commit()
                label = "求助单" if kind == "field_help" else "转交记录"
                msg = f"已创建{label} {esc.id}，状态 {esc.status}。"
        update = {"messages": [ToolMessage(msg, tool_call_id=p["tool_call_id"])], "pending": None}
        if p.get("kind") == "field_help":
            update["help_draft"] = None  # filed or declined: either way the draft is finished
        return update

    def cite_check(state: State):
        reg = state.get("evidence") or {}
        last = state["messages"][-1]
        question = next((m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), "")

        def reply(text: str):
            return AIMessage(format_answer(question, text), id=last.id)

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
        # Scope the citation requirement to retrieval replies. Workflow acknowledgements
        # (including mixed search/escalation batches) and plain conversation are distinct.
        last_calls = []
        for message in reversed(state["messages"][:-1]):
            if isinstance(message, HumanMessage):
                break
            if isinstance(message, AIMessage) and message.tool_calls:
                last_calls = message.tool_calls
                break
        retrieval_reply = bool(last_calls) and {call["name"] for call in last_calls} == {"search_knowledge"}
        if retrieval_reply and not reg and state.get("error_code") in {"NO_ACCESS", "INSUFFICIENT_EVIDENCE"}:
            text = ("你当前没有可检索的资料权限，请联系对应职能团队。" if state["error_code"] == "NO_ACCESS"
                    else "资料中没有可靠依据，无法回答该问题。请联系对应职能团队。")
            return {"sources": [], "messages": [reply(text)],
                    "model_declared_refusal": bool(refusal_mark)}
        if refusal_mark:
            # Model-declared refusal: show no source cards (they would "prove" an absence) and flag the turn.
            text = _MARK.sub("", stripped[len(refusal_mark):]).strip()
            return {"sources": [], "error_code": state.get("error_code") or "INSUFFICIENT_EVIDENCE",
                    "model_declared_refusal": True,
                    "messages": [reply(text)]}
        text = _MARK.sub(sub, content)
        if retrieval_reply and not cited and state.get("error_code") is None:
            return {"sources": [], "error_code": "MODEL_CITATION_MISSING",
                    "messages": [reply("检索回答缺少有效引用，本轮未提供可核验的答案。请重新提问。")]}
        text = format_answer(question, text)
        upd: dict[str, Any] = {"sources": [reg[a] for a in cited]}
        if text != content:
            upd["messages"] = [AIMessage(text, id=last.id)]
        return upd

    g = StateGraph(State)
    for name, fn in [("start_turn", start_turn), ("query_plan", query_plan), ("agent", agent), ("tools", tools), ("prepare_escalation", prepare_escalation),
                     ("execute_escalation", execute_escalation), ("cite_check", cite_check),
                     ("clarify", clarify)]:
        g.add_node(name, fn)
    g.add_edge(START, "start_turn")
    g.add_edge("start_turn", "query_plan")
    g.add_conditional_edges("query_plan", after_plan, ["tools", "agent"])
    g.add_conditional_edges("agent", after_agent, ["tools", "cite_check"])
    g.add_conditional_edges("tools", after_tools, ["agent", "prepare_escalation", "clarify", END])
    g.add_edge("clarify", END)
    g.add_edge("prepare_escalation", "execute_escalation")
    g.add_edge("execute_escalation", "agent")
    g.add_edge("cite_check", END)
    return g.compile(checkpointer=deps.checkpointer)
