"""Query planning before the agent: rewrite, split, intent, machine model.

The planner model proposes; deterministic code disposes:
- spelling is normalised with the same typo table the ES index uses;
- every identifier in the user's question survives the rewrite;
- a machine model is kept only if the conversation actually names it;
- routing can only narrow the readable knowledge bases, never widen them.
Any failure or timeout returns None and the agent falls back to v3 behaviour.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from knoventra.config import settings
from knoventra.ingestion.cases import MODEL_ALIASES, models_from_text
from knoventra.retrieval.identifiers import extract_query_identifiers

QUERY_PLAN_POLICY = "plan-v1"
Intent = Literal["case_sop", "policy", "it_guide", "skill_lookup", "help_status", "chitchat"]
RETRIEVAL_INTENTS = ("case_sop", "policy", "it_guide")
HISTORY_MESSAGES = 6
HISTORY_CHARS = 500

PLANNER_PROMPT = """你是企业知识库的检索规划器，只负责规划，不回答问题。根据对话调用 QueryPlan：
- rewritten：把最后一条用户问题改写成可以独立检索的查询；用前文补全“它”“上次那个”等指代；编号、告警码、机型保持原样。
- sub_queries：问题包含几个彼此独立的问题时，拆成最多 3 个子查询；否则留空。
- intent：case_sop（设备操作、故障、SOP、现场案例）、policy（HR、财务制度）、it_guide（IT 指南、VPN、账号）、skill_lookup（问谁会做某项技能）、help_status（查询求助或转交进度）、chitchat（问候、闲聊）。
- confidence：对 intent 的把握，0 到 1。
- machine_model：用户明确提到的机型（WX-38、WX-P、ST-250），没提到就留空，不要推测。"""


class QueryPlan(BaseModel):
    """检索规划结果。"""

    model_config = ConfigDict(extra="forbid")
    rewritten: str = Field(min_length=1, max_length=500, description="可独立检索的改写查询")
    sub_queries: list[str] = Field(default_factory=list, max_length=3, description="最多 3 个子查询")
    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    machine_model: Literal["WX-38", "WX-P", "ST-250"] | None = None


@dataclass
class PlanResult:
    rewritten: str
    queries: list[str]
    intent: str
    confidence: float
    machine_model: str | None
    route_kbs: list[str] | None  # KB names to search; None = every readable KB
    notes: list[str] = field(default_factory=list)  # deterministic corrections applied

    def trace(self, latency_ms: float) -> dict:
        return {"status": "ok", "policy": QUERY_PLAN_POLICY, "latency_ms": latency_ms, "rewritten": self.rewritten,
                "queries": self.queries, "intent": self.intent, "confidence": self.confidence,
                "machine_model": self.machine_model, "route_kbs": self.route_kbs, "notes": self.notes}


@lru_cache(maxsize=1)
def _typo_rules() -> list[tuple[re.Pattern[str], str]]:
    path = Path(__file__).resolve().parents[1] / "search_es" / "typos.txt"
    rules = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        wrong, right = (part.strip() for part in line.split("=>", 1))
        for w in (x.strip() for x in wrong.split(",") if x.strip()):
            rules.append((re.compile(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", re.IGNORECASE), right))
    return rules


def fix_spelling(text: str) -> str:
    for pattern, right in _typo_rules():
        text = pattern.sub(right, text)
    return text


def _text(m: AnyMessage) -> str:
    return m.content if isinstance(m.content, str) else str(m.content)


def _history(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Plain user/assistant text only; tool traffic and evidence never reach the planner."""
    turns = [m for m in messages if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and not m.tool_calls)]
    out = []
    for m in turns[-HISTORY_MESSAGES:]:
        cls = HumanMessage if isinstance(m, HumanMessage) else AIMessage
        out.append(cls(_text(m)[:HISTORY_CHARS]))
    return out


def postprocess(plan: QueryPlan, messages: list[AnyMessage]) -> PlanResult:
    question = next(_text(m) for m in reversed(messages) if isinstance(m, HumanMessage))
    conversation = "\n".join(_text(m) for m in messages if isinstance(m, HumanMessage))
    notes = []
    rewritten = fix_spelling(plan.rewritten.strip())
    queries = [fix_spelling(q.strip()) for q in plan.sub_queries if q.strip()][:3] or [rewritten]
    required = extract_query_identifiers(question)
    missing = sorted(code for code in required if code not in extract_query_identifiers(" ".join(queries)))
    if missing:  # a rewrite must never drop the user's codes
        queries = [f"{q} {' '.join(missing)}" for q in queries]
        notes.append(f"restored_identifiers:{','.join(missing)}")
    named = models_from_text(conversation)
    model = plan.machine_model if plan.machine_model in named else None
    if plan.machine_model and model is None:
        notes.append(f"dropped_unstated_model:{plan.machine_model}")
    if model is None and len(named) == 1 and named[0] in MODEL_ALIASES:
        model = named[0]
    route = None
    if plan.intent in RETRIEVAL_INTENTS and plan.confidence >= settings.query_plan_min_confidence:
        route = list(settings.intent_kbs.get(plan.intent, [])) or None
    elif plan.intent in RETRIEVAL_INTENTS:
        notes.append("low_confidence_all_readable")
    return PlanResult(rewritten, queries, plan.intent, plan.confidence, model, route, notes)


_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="query-plan")


def plan_query(planner: BaseChatModel, messages: list[AnyMessage]) -> tuple[PlanResult | None, dict]:
    """Returns (plan, trace). plan is None on any failure; the trace says why."""
    started = time.perf_counter()

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 3)

    prompt = [SystemMessage(PLANNER_PROMPT), *_history(messages)]
    try:  # function calling works on OpenAI-compatible providers without json_schema support
        runnable = planner.with_structured_output(QueryPlan, method="function_calling")
    except (TypeError, ValueError, NotImplementedError):
        runnable = planner.with_structured_output(QueryPlan)
    future = _POOL.submit(runnable.invoke, prompt)
    try:
        raw = future.result(timeout=settings.query_plan_timeout_s)
    except FutureTimeout:
        future.cancel()
        return None, {"status": "fallback", "reason": "timeout", "latency_ms": elapsed()}
    except Exception as exc:  # noqa: BLE001 - any planner failure falls back to v3 behaviour
        return None, {"status": "fallback", "reason": type(exc).__name__, "latency_ms": elapsed()}
    if not isinstance(raw, QueryPlan):
        return None, {"status": "fallback", "reason": "no_plan", "latency_ms": elapsed()}
    result = postprocess(raw, messages)
    return result, result.trace(elapsed())
