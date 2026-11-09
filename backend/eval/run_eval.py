"""Evaluation runner.

    uv run python eval/run_eval.py --split dev                    # tuning split (use freely)
    uv run python eval/run_eval.py --split holdout --confirm-final # final numbers: run once per config
    flags: --no-sparse  --no-opencc  --rerank  --threshold 0.6  --mode retrieval|agent

`retrieval` mode needs no LLM: Recall@5 / MRR, leakage, gate refusal. `agent` mode additionally needs
KNV_LLM_*, --allow-llm and explicit input/output prices. Inspect --dry-run first; estimates are not
billing caps. Agent mode checks cited sources, refusal codes and escalation record counts end to end.
The database must already be seeded with `python -m knoventra.db.seed` using the encoder you report.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import copy
import datetime as dt
import hashlib
import json
import math
import os
import platform
import sys
import time
import uuid
from pathlib import Path
from importlib.metadata import PackageNotFoundError, version

from answer_review import make_snapshot
from answer_timing import ANSWER_TIMING_POLICY, AnswerChunkTimer
from preflight import plan, select_cases
from progress import ProgressJournal
from knoventra.retrieval.identifiers import IDENTIFIER_POLICY

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.jsonl"
CASES_V2 = HERE / "cases_v2.jsonl"
CASES_V3 = HERE / "cases_v3.jsonl"
CASES_V4 = HERE / "cases_v4.jsonl"
CASES_V5 = HERE / "cases_v5.jsonl"
AGENT_LATENCY_POLICY = "agent-wall-and-completed-search-v1"


def load_cases(split: str) -> list[dict]:
    src = {"val2": CASES_V2, "val3": CASES_V3, "val4": CASES_V4, "val5": CASES_V5}.get(split, CASES)
    rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows if split in ("all", "val2", "val3", "val4", "val5") else [c for c in rows if c["split"] == split]


def matches(e, exp: dict) -> bool:
    return (e.document_title == exp["title"] and e.page in exp["pages"] and e.revision == exp["revision"]
            and exp.get("support_text", "") in e.text)


def corpus_fingerprints(db) -> dict:
    """Bind comparisons to actual published text and grants, excluding random IDs."""
    from sqlalchemy import select
    from knoventra.db.models import Chunk, Document, KbGrant, KnowledgeBase, Revision

    rows = db.execute(select(KnowledgeBase.name, Document.title, Document.source_lang, Revision.version,
                             Revision.content_hash, Chunk.page, Chunk.section_path, Chunk.text, Chunk.text_norm)
                      .join(Document, Document.kb_id == KnowledgeBase.id)
                      .join(Revision, Revision.document_id == Document.id)
                      .join(Chunk, Chunk.revision_id == Revision.id).where(Revision.status == "PUBLISHED")).all()
    grants = db.execute(select(KnowledgeBase.name, KbGrant.region, KbGrant.department, KbGrant.role)
                        .join(KbGrant, KbGrant.kb_id == KnowledgeBase.id)).all()

    def fingerprint(values):
        encoded = sorted(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) for row in values)
        return hashlib.sha256("\n".join(encoded).encode()).hexdigest()

    return {"published_text_sha256": fingerprint(rows), "grants_sha256": fingerprint(grants)}


def dependency_versions() -> dict:
    versions = {}
    for package in ("FlagEmbedding", "torch", "transformers", "pgvector", "sqlalchemy"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = None
    return versions


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
        r.update(status=out.status, reason=out.reason, required_identifiers=out.required_identifiers,
                 missing_identifiers=out.missing_identifiers)
        exp = c["expect"]
        if c["category"] in ("ops", "cross"):
            # Use the very candidates scored by the pipeline, including reranking.
            # A second plain search would silently measure RRF instead of Rerank.
            ev = out.ranked_evidence
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
        r["ranked_evidence"] = [
            {"chunk_id": e.chunk_id, "document_title": e.document_title, "revision": e.revision, "page": e.page,
             "dense_sim": e.dense_sim, "sparse_ip": e.sparse_ip, "rerank_score": e.rerank_score}
            for e in out.ranked_evidence
        ]
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

def latency_summary(values) -> dict:
    """Nearest-rank percentiles over recorded samples; missing timings are not zeros."""
    samples = sorted(v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
                     and math.isfinite(v) and v >= 0)
    if not samples:
        return {"n": 0, "p50": None, "p95": None, "max": None}
    return {"n": len(samples), "p50": samples[math.ceil(len(samples) * 0.50) - 1],
            "p95": samples[math.ceil(len(samples) * 0.95) - 1], "max": samples[-1]}


def eval_agent(graph, db, users, cases, *, on_start=None, on_complete=None) -> list[dict]:
    """End-to-end per case. Escalation cases drive interrupt/resume with the listed decisions."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.types import Command
    from sqlalchemy import func, select

    from knoventra.db.models import Escalation

    out = []
    for c in cases:
        user = users[c["user"]]
        cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "user_id": user.id}}
        if on_start:
            on_start(c["id"], cfg["configurable"]["thread_id"])
        t0 = time.perf_counter()
        answer_timer = AnswerChunkTimer(t0)
        cfg["callbacks"] = [answer_timer]
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
                                    and s["revision"] == exp["revision"]
                                    and exp.get("support_text", "") in s.get("text", "") for s in srcs)
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
        r["first_answer_chunk_ms"] = (answer_timer.first_answer_chunk_ms
                                       if r["error_code"] in (None, "INSUFFICIENT_EVIDENCE", "NO_ACCESS") else None)
        r["review_snapshot"] = make_snapshot(c["question"], r["answer"], st.get("evidence") or {},
                                              st.get("sources") or [], st.get("error_code"))
        r["thread_id"] = cfg["configurable"]["thread_id"]
        r["expect"] = copy.deepcopy(exp)
        r["diagnostics"] = {"schema_version": 1,
                            "retrieval_trace": copy.deepcopy(st.get("retrieval_trace") or []),
                            "model_declared_refusal": bool(st.get("model_declared_refusal")),
                            "model_finish_reason": st.get("model_finish_reason")}
        r["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out.append(r)
        if on_complete:
            on_complete(r)
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
    m["latency_ms"] = latency_summary(r.get("latency_ms") for r in rs)
    m["first_answer_chunk_ms"] = latency_summary(r.get("first_answer_chunk_ms") for r in rs)
    traces = [trace for r in rs for trace in (r.get("diagnostics") or {}).get("retrieval_trace", [])]
    m["retrieval_latency_ms"] = {**latency_summary(t.get("latency_ms") for t in traces),
                                 "completed_calls": len(traces)}
    return m


def make_evaluation_graph(enc, rr, max_output_tokens: int):
    """Separate from the cached web graph: evaluation caps/retry policy are explicit."""
    from contextlib import contextmanager
    from langchain_openai import ChatOpenAI
    from knoventra.agent.checkpoint import make_checkpointer
    from knoventra.agent.graph import AgentDeps, build_graph
    from knoventra.agent.model import chat_model_kwargs
    from knoventra.config import settings
    from knoventra.db.session import SessionLocal

    if not settings.llm_model:
        raise ValueError("LLM model is not configured")
    llm = ChatOpenAI(**chat_model_kwargs(), max_retries=0,
                     max_tokens=max_output_tokens, streaming=True)

    @contextmanager
    def session():
        with SessionLocal() as db:
            yield db

    saver, pool = make_checkpointer(settings.database_url)
    try:
        return build_graph(AgentDeps(session, enc, rr, llm, saver)), pool
    except BaseException:
        pool.close()
        raise


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "holdout", "all", "val2", "val3", "val4", "val5"], default="dev")
    ap.add_argument("--mode", choices=["retrieval", "agent"], default="retrieval")
    ap.add_argument("--no-sparse", action="store_true")
    ap.add_argument("--no-opencc", action="store_true")
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--rerank-model", help="model name/path; requires --rerank")
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--confirm-final", action="store_true", help="required for holdout/all/val3/val4/val5")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--progress-out", type=Path, help="agent-only durable JSONL progress; never resumes a run")
    ap.add_argument("--dry-run", action="store_true", help="offline plan only; no DB/model/network calls")
    ap.add_argument("--case-id", action="append", help="select a case from the split (repeatable)")
    ap.add_argument("--allow-llm", action="store_true", help="explicitly enable real agent evaluation")
    ap.add_argument("--max-output-tokens", type=int, default=1024)
    ap.add_argument("--assumed-input-tokens", type=int, default=4000, help="scenario estimate, not an input limit")
    ap.add_argument("--input-price-per-million", type=float)
    ap.add_argument("--output-price-per-million", type=float)
    ap.add_argument("--currency", default="USD", help="unit of the supplied prices; no exchange-rate conversion")
    a = ap.parse_args()
    if a.out is not None and a.out.exists():
        ap.error("output already exists; choose a new result path")
    if a.progress_out is not None and a.mode != "agent":
        ap.error("--progress-out only applies to agent mode")
    if a.progress_out is not None and a.progress_out.exists():
        ap.error("progress output already exists; choose a new path")
    if a.out is not None and a.progress_out is not None and a.out.resolve() == a.progress_out.resolve():
        ap.error("report and progress paths must differ")
    if a.rerank_model and not a.rerank:
        ap.error("--rerank-model requires --rerank")
    if a.mode == "agent" and (a.no_sparse or a.no_opencc or a.threshold is not None):
        ap.error("--no-sparse, --no-opencc and --threshold are retrieval-only options")
    if a.mode != "agent" and a.allow_llm:
        ap.error("--allow-llm only applies to agent mode")
    if a.split in ("holdout", "all", "val3", "val4", "val5") and not a.confirm_final:
        sys.exit(f"{a.split} is for final reporting only; pass --confirm-final to proceed")

    from knoventra.config import settings
    try:
        cases = select_cases(load_cases(a.split), a.case_id)
        preflight = plan(cases, mode=a.mode, split=a.split, tool_limit=settings.max_tool_calls_per_turn,
                         model=settings.llm_model if a.mode == "agent" else "", max_output_tokens=a.max_output_tokens,
                         assumed_input_tokens=a.assumed_input_tokens, input_price=a.input_price_per_million,
                         output_price=a.output_price_per_million, currency=a.currency)
        if a.mode == "agent":
            preflight.update(llm_thinking=settings.llm_thinking, llm_streaming=True)
    except ValueError as error:
        ap.error(str(error))
    if a.dry_run:
        if a.out:
            a.out.parent.mkdir(parents=True, exist_ok=True)
            with a.out.open("x", encoding="utf-8") as output:
                output.write(json.dumps(preflight, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        print(json.dumps(preflight, ensure_ascii=False, indent=2, allow_nan=False))
        return
    if a.mode == "agent" and not a.allow_llm:
        ap.error("agent evaluation requires --allow-llm; inspect --dry-run first")
    if a.mode == "agent" and a.input_price_per_million is None:
        ap.error("agent evaluation requires input/output prices for the preflight estimate")
    if a.mode == "agent" and not settings.llm_model:
        ap.error("LLM model is not configured")

    a.out = a.out or HERE / "results" / f"{a.split}-{a.mode}-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    progress_path = a.progress_out or Path(str(a.out) + ".progress.jsonl")
    if a.mode == "agent" and (a.out.resolve() == progress_path.resolve() or a.out.exists()):
        ap.error("choose separate, unused report and progress paths")
    journal_context = ProgressJournal(progress_path, preflight) if a.mode == "agent" else nullcontext(None)
    with journal_context as journal:
        run_evaluation(a, cases, preflight, journal)


def run_evaluation(a, cases, preflight, journal=None):
    from knoventra.retrieval.search import QUERY_ENCODING_POLICY, QUERY_VECTOR_POLICY
    from knoventra.config import settings
    from sqlalchemy import func, select

    from knoventra.db.models import Chunk, Document, Revision, User
    from knoventra.db.session import SessionLocal
    from knoventra.ingestion.encoder import get_encoder
    from knoventra.retrieval.rerank import get_reranker

    if a.rerank:
        settings.rerank = "bge"
    if a.rerank_model:
        settings.rerank_model = a.rerank_model
    enc, rr = get_encoder(), get_reranker()
    th = (settings.rerank_threshold if rr else settings.gate_threshold) if a.threshold is None else a.threshold
    with SessionLocal() as db:
        users = {u.email.split("@")[0]: u for u in db.scalars(select(User))}
        corpus = {
            "documents": db.scalar(select(func.count()).select_from(Document)),
            "published_revisions": db.scalar(select(func.count()).select_from(Revision).where(Revision.status == "PUBLISHED")),
            "published_chunks": db.scalar(select(func.count()).select_from(Chunk).join(Revision)
                                           .where(Revision.status == "PUBLISHED")),
        }
        corpus.update(corpus_fingerprints(db))
        if journal:
            journal.record("corpus_captured", corpus=corpus)
        if a.mode == "retrieval":
            res = eval_retrieval(db, enc, rr, users, cases, use_sparse=not a.no_sparse,
                                 use_opencc=not a.no_opencc, threshold=th)
            metrics = summarize(res)
        else:
            graph, pool = make_evaluation_graph(enc, rr, a.max_output_tokens)
            try:
                res = eval_agent(graph, db, users, cases,
                                 on_start=journal.start_case if journal else None,
                                 on_complete=journal.complete_case if journal else None)
                metrics = summarize_agent(res)
                db.rollback()
            finally:
                pool.close()
    prompt_sha256 = None
    citation_policy = None
    answer_script_policy = None
    if a.mode == "agent":
        from knoventra.agent.graph import CITATION_POLICY, SYSTEM_PROMPT
        prompt_sha256 = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
        citation_policy = CITATION_POLICY
        from knoventra.agent.language import ANSWER_SCRIPT_POLICY
        answer_script_policy = ANSWER_SCRIPT_POLICY
    report = {
        "date": dt.date.today().isoformat(), "split": a.split, "mode": a.mode, "n_cases": len(cases),
        "config": {"encoder": settings.encoder, "rerank": "bge" if rr else "off", "sparse": not a.no_sparse,
                   "encoder_model": getattr(enc, "model_name", None),
                   "encoder_model_revision": getattr(enc, "model_revision", None),
                   "opencc": not a.no_opencc, "gate_threshold": th,
                   "gate_sparse_threshold": settings.gate_sparse_threshold, "identifier_policy": IDENTIFIER_POLICY,
                   "rerank_model": settings.rerank_model if rr else None,
                   "rerank_model_revision": getattr(rr, "model_revision", None) if rr else None,
                   "rerank_candidates": settings.rerank_candidates if rr else None,
                   "rerank_input": "section_path+text-v1" if rr else None,
                   "rank_metric_stage": "post_rerank_pre_gate" if rr else "post_rrf_pre_gate",
                   "llm_model": settings.llm_model or None,
                   "review_snapshot_schema": 1 if a.mode == "agent" else None,
                   "latency_policy": AGENT_LATENCY_POLICY if a.mode == "agent" else None,
                   "answer_timing_policy": ANSWER_TIMING_POLICY if a.mode == "agent" else None,
                   "llm_streaming": True if a.mode == "agent" else None,
                   "llm_thinking": settings.llm_thinking if a.mode == "agent" else None,
                   "citation_policy": citation_policy,
                   "answer_script_policy": answer_script_policy,
                   "query_vector_policy": QUERY_VECTOR_POLICY,
                   "query_encoding_policy": QUERY_ENCODING_POLICY,
                   "prompt_sha256": prompt_sha256},
        "corpus": corpus,
        "cases_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "env": {"python": platform.python_version(), "platform": platform.platform(), "packages": dependency_versions()},
        "metrics": metrics, "cases": res,
        "preflight": preflight if a.mode == "agent" else None,
        "progress_path": str(journal.path) if journal else None,
    }
    path = a.out or HERE / "results" / f"{a.split}-{a.mode}-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=1, allow_nan=False))
        output.flush()
        os.fsync(output.fileno())
    if journal:
        journal.record("run_completed", report_path=str(path), completed_cases=len(res))
    print(json.dumps({"config": report["config"], "metrics": metrics}, ensure_ascii=False, indent=1))
    print("wrote", path)


if __name__ == "__main__":
    main()
