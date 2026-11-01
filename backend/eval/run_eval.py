"""Evaluation runner.

    uv run python eval/run_eval.py --split dev                    # tuning split (use freely)
    uv run python eval/run_eval.py --split holdout --confirm-final # final numbers: run once per config
    flags: --no-sparse  --no-opencc  --rerank  --threshold 0.6  --mode retrieval|agent

`retrieval` mode needs no LLM: Recall@5 / MRR, leakage, gate refusal. `agent` mode additionally needs
KNV_LLM_* and checks cited sources, refusal codes and escalation record counts end to end.
The database must already be seeded with `python -m knoventra.db.seed` using the encoder you report.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.jsonl"
CASES_V2 = HERE / "cases_v2.jsonl"
CASES_V3 = HERE / "cases_v3.jsonl"


def load_cases(split: str) -> list[dict]:
    src = {"val2": CASES_V2, "val3": CASES_V3}.get(split, CASES)
    rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows if split in ("all", "val2", "val3") else [c for c in rows if c["split"] == split]


def matches(e, exp: dict) -> bool:
    return e.document_title == exp["title"] and e.page in exp["pages"] and e.revision == exp["revision"]


def eval_retrieval(db, enc, rr, users, cases, *, use_sparse, use_opencc, threshold) -> list[dict]:
    from knoventra.access.scope import NoAccess, compute_scope
    from knoventra.retrieval.pipeline import INSUFFICIENT, retrieve
    from knoventra.retrieval.search import search

    results = []
    for c in cases:
        if c["category"] == "escalation":
            continue
        user = users[c["user"]]
        t0 = time.perf_counter()
        r = {"id": c["id"], "category": c["category"], "user": c["user"]}
        out = retrieve(db, enc, rr, user, c["question"], threshold=threshold,
                       use_sparse=use_sparse, use_opencc=use_opencc)
        r["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        r["status"] = out.status
        exp = c["expect"]
        if c["category"] in ("ops", "cross"):
            # recall is measured on the retrieval top-5 before the gate, so gate tuning does not hide misses
            scope = compute_scope(db, user)
            ev = search(db, enc, scope, c["question"], use_sparse=use_sparse, use_opencc=use_opencc).evidence
            rank = next((i for i, e in enumerate(ev, 1) if matches(e, exp)), None)
            r.update(hit=rank is not None, rank=rank, gated_out=out.status == INSUFFICIENT)
        elif c["category"] == "access":
            try:
                scope = compute_scope(db, user)
                ev = search(db, enc, scope, c["question"], use_sparse=use_sparse, use_opencc=use_opencc, top_k=20).evidence
            except NoAccess:
                ev = []
            r["leaked"] = [e.document_title for e in ev if e.document_title in exp["forbidden_titles"]]
        elif c["category"] == "no_basis":
            r["refused"] = out.status == INSUFFICIENT
        results.append(r)
    return results


def summarize(results: list[dict]) -> dict:
    def cat(n):
        return [r for r in results if r["category"] == n]

    m: dict = {}
    for n in ("ops", "cross"):
        rs = cat(n)
        if rs:
            m[n] = {"n": len(rs), "recall@5": round(sum(r["hit"] for r in rs) / len(rs), 3),
                    "mrr": round(sum(1 / r["rank"] for r in rs if r["rank"]) / len(rs), 3),
                    "gated_out_of_hits": sum(1 for r in rs if r["hit"] and r["gated_out"])}
    if cat("access"):
        m["access"] = {"n": len(cat("access")), "leaks": sum(len(r["leaked"]) for r in cat("access"))}
    if cat("no_basis"):
        rs = cat("no_basis")
        m["no_basis"] = {"n": len(rs), "refusal_rate": round(sum(r["refused"] for r in rs) / len(rs), 3)}
    lat = sorted(r["latency_ms"] for r in results)
    if lat:
        m["latency_ms"] = {"p50": lat[len(lat) // 2], "max": lat[-1]}
    return m


# ---- agent mode (needs a real LLM) -------------------------------------------------------------

def eval_agent(graph, db, users, cases) -> list[dict]:
    """End-to-end per case. Escalation cases drive interrupt/resume with the listed decisions."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.types import Command
    from sqlalchemy import func, select

    from knoventra.db.models import Escalation

    out = []
    for c in cases:
        user = users[c["user"]]
        cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": user.id}}
        t0 = time.perf_counter()
        st = graph.invoke({"messages": [HumanMessage(c["question"])], "evidence": {}, "sources": [], "error_code": None}, cfg)
        r = {"id": c["id"], "category": c["category"], "user": c["user"]}
        exp = c["expect"]
        if c["category"] == "escalation":
            before = db.scalar(select(func.count()).select_from(Escalation).where(Escalation.user_id == user.id))
            for approve in exp["decisions"]:
                if graph.get_state(cfg).next:
                    st = graph.invoke(Command(resume={"approve": approve}), cfg)
            n = db.scalar(select(func.count()).select_from(Escalation).where(Escalation.user_id == user.id)) - before
            r.update(records=n, ok=n == exp["records"], asked_confirmation=bool(exp["decisions"]))
        else:
            srcs = st.get("sources", [])
            tool_text = " ".join(m.content for m in st["messages"] if isinstance(m, ToolMessage))
            answer = next((m.content for m in reversed(st["messages"]) if isinstance(m, AIMessage) and not m.tool_calls), "")
            if c["category"] in ("ops", "cross"):
                r["cited_ok"] = any(s["document_title"] == exp["title"] and s["page"] in exp["pages"]
                                    and s["revision"] == exp["revision"] for s in srcs)
            elif c["category"] == "access":
                # v2 rule: a title counts only if it was RETURNED (tool output lists "《title》") or shown as a
                # source. v1 also matched the title as a substring of the model's prose, which gave a false
                # positive on holdout (access-06); v1 numbers stay in the report as measured.
                r["leaked"] = [t for t in exp["forbidden_titles"]
                               if f"《{t}》" in tool_text or any(s["document_title"] == t for s in srcs)]
            else:
                r["refused"] = st.get("error_code") in ("INSUFFICIENT_EVIDENCE", "NO_ACCESS") and not srcs
        r["queries"] = [tc["args"].get("query") for m in st["messages"] if isinstance(m, AIMessage)
                        for tc in m.tool_calls if tc["name"] == "search_knowledge"]
        r["answer"] = next((m.content for m in reversed(st["messages"]) if isinstance(m, AIMessage) and not m.tool_calls), "")
        r["error_code"] = st.get("error_code")
        r["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out.append(r)
    return out


def summarize_agent(rs: list[dict]) -> dict:
    def cat(*n):
        return [r for r in rs if r["category"] in n]

    m = {}
    if cat("ops", "cross"):
        m["citation_correct"] = round(sum(r["cited_ok"] for r in cat("ops", "cross")) / len(cat("ops", "cross")), 3)
    if cat("access"):
        m["leaks"] = sum(len(r["leaked"]) for r in cat("access"))
    if cat("no_basis"):
        m["refusal_correct"] = round(sum(r["refused"] for r in cat("no_basis")) / len(cat("no_basis")), 3)
    if cat("escalation"):
        m["escalation_ok"] = sum(r["ok"] for r in cat("escalation"))
        m["escalation_n"] = len(cat("escalation"))
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "holdout", "all", "val2", "val3"], default="dev")
    ap.add_argument("--mode", choices=["retrieval", "agent"], default="retrieval")
    ap.add_argument("--no-sparse", action="store_true")
    ap.add_argument("--no-opencc", action="store_true")
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--confirm-final", action="store_true", help="required for holdout/all")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.split in ("holdout", "all", "val3") and not a.confirm_final:
        sys.exit("holdout/all are for final reporting only; pass --confirm-final to proceed")

    import os
    if a.rerank:
        os.environ["KNV_RERANK"] = "bge"
    from sqlalchemy import select

    from knoventra.config import settings
    from knoventra.db.models import User
    from knoventra.db.session import SessionLocal
    from knoventra.ingestion.encoder import get_encoder
    from knoventra.retrieval.rerank import get_reranker

    cases = load_cases(a.split)
    enc, rr = get_encoder(), get_reranker()
    th = settings.gate_threshold if a.threshold is None else a.threshold
    with SessionLocal() as db:
        users = {u.email.split("@")[0]: u for u in db.scalars(select(User))}
        if a.mode == "retrieval":
            res = eval_retrieval(db, enc, rr, users, cases, use_sparse=not a.no_sparse,
                                 use_opencc=not a.no_opencc, threshold=th)
            metrics = summarize(res)
        else:
            from knoventra.api.chat import get_graph
            res = eval_agent(get_graph(), db, users, cases)
            metrics = summarize_agent(res)
            db.rollback()
    report = {
        "date": dt.date.today().isoformat(), "split": a.split, "mode": a.mode, "n_cases": len(cases),
        "config": {"encoder": settings.encoder, "rerank": "bge" if rr else "off", "sparse": not a.no_sparse,
                   "opencc": not a.no_opencc, "gate_threshold": th, "llm_model": settings.llm_model or None},
        "env": {"python": platform.python_version(), "platform": platform.platform()},
        "metrics": metrics, "cases": res,
    }
    path = a.out or HERE / "results" / f"{a.split}-{a.mode}-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"config": report["config"], "metrics": metrics}, ensure_ascii=False, indent=1))
    print("wrote", path)


if __name__ == "__main__":
    main()
