"""Agent-layer evaluation (v4 §4): routing, clarification, slot filling, duplicate filings.

Drives the real LangGraph graph turn by turn over `cases_agent_v1.jsonl` and scores what the
graph did, not how the answer reads:

- routing accuracy: the planner's intent (query_plan trace, status ok) against the label;
  a planner fallback counts as wrong. Turns without an intent label are not scored
  (slot follow-ups skip the planner by design, and filing a help request has no label).
- clarification: asked = the turn ended with `clarification` set. Reported as the trigger
  rate on turns that should ask, and the false-trigger rate on turns that should not.
- tool: the expected tool is among the turn's tool calls; `null` means no tool at all.
- multi-question / coreference / skill item: planner query count, rewrite and skill argument.
- slot completeness: expected slots found on the confirmation card (micro average over
  expected slots; a case that never reaches the card scores 0 for all of its slots).
- duplicate filings under retry and refresh: after each decision the runner replays the
  submit with the same confirmation id and rebuilds the graph over the same checkpointer
  (a page refresh); new help requests beyond the approved cards are duplicates. Must be 0.

Paid runs (each batch needs the user's approval; see docs/evaluation-report.md):

    uv run python eval/run_agent_eval.py --split dev --dry-run          # no DB, no model: counts only
    KNV_ENCODER=bge-m3 uv run python eval/run_agent_eval.py --split dev --allow-llm
    KNV_ENCODER=bge-m3 uv run python eval/run_agent_eval.py --split holdout --allow-llm --confirm-final

The corpus is the one `setup_field_eval.py` builds (database knoventra_eval_v4, ES alias
knv_eval_v4), so skills and field cases exist. Long-term memory is off: cases share demo users.
Tests drive `run_case` with scripted models (tests/test_agent_eval.py), offline.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import time
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases_agent_v1.jsonl"
EVAL_DB = "knoventra_eval_v4"
INTENTS = ("case_sop", "policy", "it_guide", "skill_lookup", "help_status", "chitchat")  # labels the frozen cases use
VALID_INTENTS = (*INTENTS, "request_help")  # every intent the planner can return
TOOLS = ("search_knowledge", "get_escalation_status", "escalate_to_expert", "lookup_skills", "request_help")
SLOT_KEYS = ("machine_id", "machine_model", "alarm_codes", "symptom", "tried_steps")
TURN_KEYS = {"intent", "clarify", "tool", "queries_min", "queries_max", "rewrite_any", "skill_any", "missing", "card"}
CASE_KEYS = {"id", "split", "category", "user", "turns", "decisions", "slots", "setup", "notes"}
CATEGORIES = ("clarify_model", "multi_question", "skill_lookup", "help_status", "coreference", "help_slots", "routing")


# ------------------------------------------------------------------ cases

def load_cases(split: str | None, path: Path = CASES) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    return [r for r in rows if split in (None, r["split"])], hashlib.sha256(raw).hexdigest()


def validate_cases(cases: list[dict]) -> list[str]:
    """Schema problems, as messages; an empty list means the file is usable."""
    errors, seen = [], set()
    for c in cases:
        cid = c.get("id")
        if cid in seen:
            errors.append(f"{cid}: duplicate id")
        seen.add(cid)
        if set(c) - CASE_KEYS:
            errors.append(f"{cid}: unknown keys {sorted(set(c) - CASE_KEYS)}")
        if c.get("split") not in ("dev", "holdout") or c.get("category") not in CATEGORIES:
            errors.append(f"{cid}: bad split or category")
        if not c.get("turns"):
            errors.append(f"{cid}: no turns")
        for i, t in enumerate(c.get("turns") or [], 1):
            exp = t.get("expect") or {}
            if not isinstance(t.get("user"), str) or not t["user"].strip():
                errors.append(f"{cid} turn {i}: empty user text")
            if set(exp) - TURN_KEYS:
                errors.append(f"{cid} turn {i}: unknown expect keys {sorted(set(exp) - TURN_KEYS)}")
            intent = exp.get("intent")
            if intent is not None and not all(x in VALID_INTENTS for x in ([intent] if isinstance(intent, str) else intent)):
                errors.append(f"{cid} turn {i}: unknown intent {intent}")
            if "tool" in exp and exp["tool"] is not None and exp["tool"] not in TOOLS:
                errors.append(f"{cid} turn {i}: unknown tool {exp['tool']}")
        if set(c.get("slots") or {}) - set(SLOT_KEYS):
            errors.append(f"{cid}: unknown slots {sorted(set(c['slots']) - set(SLOT_KEYS))}")
        if c.get("slots") and not any((t.get("expect") or {}).get("card") for t in c["turns"]):
            errors.append(f"{cid}: slots given but no turn expects the card")
    return errors


def expected_records(case: dict) -> int:
    return sum(1 for d in case.get("decisions") or [] if d)


# ------------------------------------------------------------------ model-call estimate (no DB, no model)

def estimate_calls(cases: list[dict], max_tool_calls: int = 4) -> dict:
    """Expected and worst-case chat-model calls. The worst case assumes every agent step loops
    to the per-turn tool-call cap. Summaries are not counted: these short dialogues stay under
    the context budget, so the summarizer is not expected to run."""
    low = high = turns = 0
    for c in cases:
        open_draft = False
        for t in c["turns"]:
            exp, turns = t.get("expect") or {}, turns + 1
            planner = 0 if open_draft else 1
            tool = exp.get("tool")
            if exp.get("clarify"):
                agent = 0  # the clarify node answers without the agent model
            elif tool in ("get_escalation_status", "request_help"):
                agent = 2 + (1 if exp.get("card") else 0)  # call, reply, reply after the decision
            elif tool is None and open_draft:
                agent = 0  # a cancel phrase is handled before the model
            else:
                agent = 1
            low += planner + agent
            high += planner + max_tool_calls + 1 + (1 if exp.get("card") else 0)
            open_draft = bool(exp.get("missing"))
    return {"cases": len(cases), "turns": turns, "expected_model_calls": low, "max_model_calls": high}


# ------------------------------------------------------------------ one case

def _norm(text: str) -> str:
    from knoventra.ingestion.normalize import to_simplified

    return " ".join(to_simplified(str(text)).lower().split())


def _interrupts(snapshot) -> list[dict]:
    return [i.value for task in snapshot.tasks for i in task.interrupts if isinstance(i.value, dict)]


def _record_decision(db, snapshot, approve: bool) -> None:
    """What POST /chat/confirm does before resuming: store the decision on the confirmation."""
    from sqlalchemy import update

    from knoventra.db.models import Confirmation

    ids = [v["confirmation_id"] for v in _interrupts(snapshot) if v.get("confirmation_id")]
    db.execute(update(Confirmation).where(Confirmation.id.in_(ids), Confirmation.status == "PENDING")
               .values(status="APPROVED" if approve else "REJECTED"))
    db.commit()


def _insert_fixtures(db, user, setup: dict | None) -> None:
    from knoventra.db.models import HelpRequest

    for r in (setup or {}).get("help_requests", []):
        db.add(HelpRequest(user_id=user.id, operation_id=f"eval-fixture-{uuid.uuid4().hex}", kind=r["kind"],
                           summary=r["symptom"], status=r["status"], machine_id=r.get("machine_id"),
                           symptom=r["symptom"]))
    db.commit()


def _filings(db, user) -> int:
    from sqlalchemy import func, select

    from knoventra.db.models import HelpRequest

    return db.scalar(select(func.count()).select_from(HelpRequest).where(HelpRequest.user_id == user.id))


def _confirmations(db, thread: str) -> int:
    from sqlalchemy import func, select

    from knoventra.db.models import Confirmation

    return db.scalar(select(func.count()).select_from(Confirmation).where(Confirmation.thread_id == thread))


def _turn_view(values: dict) -> dict:
    """What the graph did in the turn that ends the current state."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from knoventra.help import slots as help_slots
    from knoventra.memory import drafts

    messages = values.get("messages") or []
    start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1) + 1
    turn = messages[start:]
    calls = [tc for m in turn if isinstance(m, AIMessage) for tc in m.tool_calls]
    plan = values.get("query_plan") or {}
    missing = None
    for m in turn:
        if isinstance(m, ToolMessage) and isinstance(m.artifact, dict) and drafts.ARTIFACT_KEY in m.artifact:
            draft = m.artifact[drafts.ARTIFACT_KEY]
            missing = help_slots.missing(draft) if draft else None
    answer = next((m.content for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls), "")
    return {"tools": [c["name"] for c in calls],
            "skill_args": [c["args"].get("skill", "") for c in calls if c["name"] == "lookup_skills"],
            "plan_status": plan.get("status"), "plan_reason": plan.get("reason"),
            "intent": plan.get("intent") if plan.get("status") == "ok" else None,
            "queries": list(plan.get("queries") or []) if plan.get("status") == "ok" else [],
            "rewritten": plan.get("rewritten"),
            "clarified": values.get("clarification") is not None, "missing": missing,
            "error_code": values.get("error_code"), "answer": str(answer)[:400],
            # Why the server refused the model's tool calls (names, field names, error types; no values).
            "tool_rejection": values.get("tool_rejection"),
            # What the answer cited, so a wrong-but-passing answer (e.g. a missed section) is visible.
            "sources": [f'{s.get("document_title", "")} > {s.get("section_path", "")}'
                        for s in values.get("sources") or []]}


def score_turn(exp: dict, got: dict, card: bool) -> dict[str, bool]:
    checks = {}
    if exp.get("intent") is not None:
        allowed = [exp["intent"]] if isinstance(exp["intent"], str) else exp["intent"]
        checks["intent"] = got["intent"] in allowed
    if "clarify" in exp:
        checks["clarify"] = got["clarified"] == exp["clarify"]
    if "tool" in exp:
        checks["tool"] = not got["tools"] if exp["tool"] is None else exp["tool"] in got["tools"]
    if "queries_min" in exp:
        checks["queries_min"] = len(got["queries"]) >= exp["queries_min"]
    if "queries_max" in exp:
        checks["queries_max"] = 1 <= len(got["queries"]) <= exp["queries_max"]
    if "rewrite_any" in exp:
        text = _norm(" ".join([got["rewritten"] or "", *got["queries"]]))
        checks["rewrite_any"] = any(_norm(s) in text for s in exp["rewrite_any"])
    if "skill_any" in exp:
        text = _norm(" ".join(got["skill_args"]))
        checks["skill_any"] = bool(text) and any(_norm(s) in text for s in exp["skill_any"])
    if "missing" in exp:
        checks["missing"] = got["missing"] is not None and set(got["missing"]) == set(exp["missing"])
    if "card" in exp:
        checks["card"] = card == exp["card"]
    return checks


def score_slots(expected: dict, card: dict | None) -> dict[str, bool]:
    card = card or {}
    out = {}
    for key, want in expected.items():
        have = card.get(key)
        if key == "alarm_codes":
            out[key] = sorted(have or []) == sorted(want)
        elif isinstance(want, list):  # free text: every listed fragment must appear
            out[key] = bool(have) and all(_norm(w) in _norm(have) for w in want)
        else:
            out[key] = have == want
    return out


def run_case(graph_factory: Callable[[], object], tools, db, users: dict, case: dict,
             counter=None) -> dict:
    """Plays every turn of one case and scores it. `graph_factory()` must return a graph over the
    same checkpointer each time (the second graph is the "refresh"); `tools` is the tool backend
    the graph uses, so the runner can replay a submit exactly as a retried confirmation would.
    `counter` (model_call_counter) is only read here: attach it to the chat models themselves,
    because the planner runs on a worker thread that run-config callbacks do not reach."""
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    from knoventra.tools.backend import Caller

    user = users[case["user"]]
    _insert_fixtures(db, user, case.get("setup"))
    graph = graph_factory()
    thread = uuid.uuid4().hex
    cfg = {"configurable": {"thread_id": thread, "user_id": user.id}}
    decisions = list(case.get("decisions") or [])
    before = _filings(db, user)
    calls_before = counter.calls if counter is not None else 0
    t0 = time.perf_counter()
    turns, card_slots, conf_ids, unanswered = [], None, [], 0
    for t in case["turns"]:
        graph.invoke({"messages": [HumanMessage(t["user"], id=uuid.uuid4().hex)], "evidence": {}, "sources": [],
                      "error_code": None}, cfg)
        snapshot = graph.get_state(cfg)
        card = False
        while _interrupts(snapshot):
            pending = snapshot.values.get("pending") or {}
            if pending.get("kind") == "field_help":
                card, card_slots = True, dict(pending.get("slots") or {})
            conf_ids += [v["confirmation_id"] for v in _interrupts(snapshot) if v.get("confirmation_id")]
            # Refresh before deciding: a new graph over the same checkpoint must show the same card.
            refreshed = _interrupts(graph_factory().get_state(cfg))
            if [v.get("confirmation_id") for v in refreshed] != [v.get("confirmation_id")
                                                                   for v in _interrupts(snapshot)]:
                conf_ids.append("refresh-mismatch")
            if not decisions:
                unanswered += 1
                break
            approve = decisions.pop(0)
            _record_decision(db, snapshot, approve)
            graph.invoke(Command(resume={"approve": approve}), cfg)
            snapshot = graph.get_state(cfg)
        got = _turn_view(snapshot.values)
        exp = t.get("expect") or {}
        turns.append({"user": t["user"], "expect": exp, "card": card, **got,
                      "checks": score_turn(exp, got, card)})
    # Retry and refresh after the decisions: replay each submit, then reload the thread.
    replay_errors = []
    caller = Caller(user.id, thread, None)
    for conf_id in dict.fromkeys(c for c in conf_ids if c != "refresh-mismatch"):
        try:
            tools.submit(caller, conf_id)
        except Exception as exc:  # noqa: BLE001 - a rejected replay is fine; only new rows matter
            replay_errors.append(type(exc).__name__)
    reloaded = graph_factory().get_state(cfg)
    filings = _filings(db, user) - before
    want = expected_records(case)
    r = {"id": case["id"], "category": case["category"], "user": case["user"], "thread_id": thread, "turns": turns,
         "expected_records": want, "filings": filings, "duplicate_filings": max(0, filings - want),
         "missing_filings": max(0, want - filings), "confirmations": _confirmations(db, thread),
         "refresh_mismatch": "refresh-mismatch" in conf_ids, "pending_after_reload": bool(reloaded.next),
         "unanswered_confirmations": unanswered, "replay_errors": replay_errors,
         "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
    if counter is not None:
        r["model_calls"] = counter.calls - calls_before
    if case.get("slots"):
        r["card_slots"] = card_slots
        r["slot_checks"] = score_slots(case["slots"], card_slots)
    r["ok"] = (all(all(t["checks"].values()) for t in turns) and all((r.get("slot_checks") or {}).values())
               and filings == want and not r["refresh_mismatch"] and not r["pending_after_reload"])
    return r


def model_call_counter():
    from langchain_core.callbacks import BaseCallbackHandler

    class Counter_(BaseCallbackHandler):
        def __init__(self):
            self.calls = 0

        def on_chat_model_start(self, *args, **kwargs):
            self.calls += 1

    return Counter_()


# ------------------------------------------------------------------ summary

def _rate(hits: int, n: int) -> float | None:
    return round(hits / n, 3) if n else None


def summarize(results: list[dict]) -> dict:
    turns = [(r, t) for r in results for t in r["turns"]]

    def check(name):
        vals = [t["checks"][name] for _, t in turns if name in t["checks"]]
        return {"n": len(vals), "correct": sum(vals), "rate": _rate(sum(vals), len(vals))}

    asks = [t for _, t in turns if "clarify" in t["expect"]]
    exp_yes = [t for t in asks if t["expect"]["clarify"]]
    exp_no = [t for t in asks if not t["expect"]["clarify"]]
    confusion = Counter((t["expect"]["intent"], t["intent"] or f"<{t['plan_status']}>") for _, t in turns
                        if isinstance(t["expect"].get("intent"), str))
    slots = [ok for r in results for ok in (r.get("slot_checks") or {}).values()]
    slot_cases = [r for r in results if "slot_checks" in r]
    return {
        "cases": len(results), "turns": len(turns), "cases_ok": sum(r["ok"] for r in results),
        "routing_accuracy": check("intent"),
        "clarification": {"should_ask": len(exp_yes), "asked": sum(t["clarified"] for t in exp_yes),
                          "trigger_rate": _rate(sum(t["clarified"] for t in exp_yes), len(exp_yes)),
                          "should_not_ask": len(exp_no), "asked_anyway": sum(t["clarified"] for t in exp_no),
                          "false_trigger_rate": _rate(sum(t["clarified"] for t in exp_no), len(exp_no))},
        "tool_accuracy": check("tool"),
        "multi_question_split": {"min": check("queries_min"), "single_stays_single": check("queries_max")},
        "coreference_rewrite": check("rewrite_any"), "skill_item": check("skill_any"),
        "slot_prompt": check("missing"), "card_reached_as_expected": check("card"),
        "slot_completeness": {"cases": len(slot_cases), "expected_slots": len(slots), "filled": sum(slots),
                              "rate": _rate(sum(slots), len(slots)),
                              "cases_all_slots": sum(all(r["slot_checks"].values()) for r in slot_cases)},
        "filings": {"expected": sum(r["expected_records"] for r in results),
                    "actual": sum(r["filings"] for r in results),
                    "duplicate_filings": sum(r["duplicate_filings"] for r in results),
                    "missing_filings": sum(r["missing_filings"] for r in results),
                    "refresh_mismatches": sum(r["refresh_mismatch"] for r in results),
                    "pending_after_reload": sum(r["pending_after_reload"] for r in results)},
        "planner_fallbacks": sum(1 for _, t in turns if t["plan_status"] == "fallback"),
        "intent_confusion": {f"{a}->{b}": n for (a, b), n in sorted(confusion.items()) if a != b},
        "by_category": {c: {"n": len(rs), "ok": sum(r["ok"] for r in rs)}
                        for c in CATEGORIES if (rs := [r for r in results if r["category"] == c])},
        "model_calls": sum(r.get("model_calls", 0) for r in results) if any("model_calls" in r for r in results)
        else None,
    }


def evaluate(graph_factory, tools, db, users, cases, counter=None, on_case=None) -> tuple[list[dict], dict]:
    results = []
    for c in cases:
        r = run_case(graph_factory, tools, db, users, c, counter)
        results.append(r)
        if on_case:
            on_case(r)
    return results, summarize(results)


# ------------------------------------------------------------------ real graph (paid)

def make_graph_factory(enc, rr, max_output_tokens: int, counter):
    """Same model wiring as the web graph (api/chat.py), with evaluation caps and no retries."""
    from contextlib import contextmanager

    from langchain_openai import ChatOpenAI

    from knoventra.agent.checkpoint import make_checkpointer
    from knoventra.agent.graph import AgentDeps, build_graph
    from knoventra.agent.model import chat_model_kwargs
    from knoventra.config import settings
    from knoventra.db.session import SessionLocal
    from knoventra.memory.working import get_working_memory
    from knoventra.tools.backend import InProcessTools

    if not settings.llm_model:
        raise SystemExit("KNV_LLM_MODEL is not configured")
    kw = {**chat_model_kwargs(), "callbacks": [counter]}
    llm = ChatOpenAI(**kw, max_retries=0, max_tokens=max_output_tokens)
    planner = ChatOpenAI(**{**kw, "timeout": settings.query_plan_timeout_s}, max_retries=0)
    summarizer = ChatOpenAI(**{**kw, "timeout": settings.summary_timeout_s}, max_retries=0)

    @contextmanager
    def session():
        with SessionLocal() as db:
            yield db

    saver, pool = make_checkpointer(settings.database_url)
    tools = InProcessTools(session, enc, rr)
    memory = get_working_memory()

    def factory():
        return build_graph(AgentDeps(session, enc, rr, llm, saver, planner, memory=memory, summarizer=summarizer,
                                     long_term_memory=False, tools=tools))
    return factory, tools, pool


def _configure_env() -> None:
    admin = os.environ.get("KNV_EVAL_ADMIN_URL", "postgresql+psycopg://knoventra:knoventra@localhost:55432/postgres")
    os.environ["KNV_DATABASE_URL"] = admin.rsplit("/", 1)[0] + "/" + EVAL_DB
    os.environ.setdefault("KNV_ES_URL", "http://localhost:59200")
    os.environ["KNV_ES_INDEX"] = "knv_eval_v4"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "holdout"], default="dev")
    ap.add_argument("--cases", nargs="+", help="case ids to run (default: the whole split)")
    ap.add_argument("--dry-run", action="store_true", help="print case and model-call counts; no DB, no model")
    ap.add_argument("--allow-llm", action="store_true", help="required: this run makes paid model calls")
    ap.add_argument("--confirm-final", action="store_true", help="required for holdout; run once")
    ap.add_argument("--max-output-tokens", type=int, default=1024)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    cases, sha = load_cases(a.split)
    if a.cases:
        unknown = sorted(set(a.cases) - {c["id"] for c in cases})
        if unknown:
            ap.error(f"unknown case ids for {a.split}: {unknown}")
        cases = [c for c in cases if c["id"] in a.cases]
    errors = validate_cases(cases)
    if errors:
        raise SystemExit("invalid cases:\n" + "\n".join(errors))
    estimate = estimate_calls(cases)
    print(json.dumps({"split": a.split, "cases_sha256": sha, **estimate}, ensure_ascii=False))
    if a.dry_run:
        return
    if not a.allow_llm:
        ap.error("this run makes paid model calls: get approval for the batch, then pass --allow-llm")
    if a.split == "holdout" and not a.confirm_final:
        ap.error("holdout is the frozen final split: pass --confirm-final")

    _configure_env()
    from sqlalchemy import func, select

    from knoventra.config import settings
    from knoventra.db.models import Skill, User
    from knoventra.db.session import SessionLocal
    from knoventra.ingestion.encoder import get_encoder
    from knoventra.retrieval.rerank import get_reranker

    enc, rr = get_encoder(), get_reranker()
    counter = model_call_counter()
    factory, tools, pool = make_graph_factory(enc, rr, a.max_output_tokens, counter)
    out = a.out or HERE / "results" / ("agent-holdout.json" if a.split == "holdout"
                                      else f"agent-dev-{dt.datetime.now():%Y%m%d-%H%M%S}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with SessionLocal() as db:
            if not db.scalar(select(func.count()).select_from(Skill)):
                raise SystemExit(f"{EVAL_DB} has no skill rows: run eval/setup_field_eval.py first")
            users = {u.email.split("@")[0]: u for u in db.scalars(select(User))}
            results, summary = evaluate(factory, tools, db, users, cases, counter,
                                        on_case=lambda r: print(r["id"], "ok" if r["ok"] else "FAIL", flush=True))
    finally:
        pool.close()
    report = {"date": dt.date.today().isoformat(), "split": a.split, "cases_sha256": sha, "n_cases": len(cases),
              "config": {"llm_model": settings.llm_model, "encoder": settings.encoder, "rerank": settings.rerank,
                         "query_plan_min_confidence": settings.query_plan_min_confidence,
                         "max_output_tokens": a.max_output_tokens, "estimate": estimate},
              "summary": summary, "results": results}
    with out.open("x" if a.split == "holdout" else "w", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    print(f"-> {out}")


if __name__ == "__main__":
    main()
