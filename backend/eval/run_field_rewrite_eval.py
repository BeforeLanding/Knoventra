"""Query rewrite / planning on-off ablation for the v4 field-retrieval set.

The planner (one real model call per case) is run once and cached; every retrieval
comparison then runs offline from that cache, so planner randomness cannot leak into
the retrieval comparison and nothing is billed twice.

    # 1. plan + retrieve (paid: one planner call per case; needs --allow-llm)
    KNV_ENCODER=bge-m3 HF_HUB_OFFLINE=1 uv run python eval/run_field_rewrite_eval.py --split dev --allow-llm
    KNV_ENCODER=bge-m3 HF_HUB_OFFLINE=1 uv run python eval/run_field_rewrite_eval.py --split holdout --allow-llm --confirm-final
    # 2. retrieval only, from an existing plan cache (0 model calls)
    KNV_ENCODER=bge-m3 uv run python eval/run_field_rewrite_eval.py --split dev --plans-in eval/results/field-rewrite-plans-dev.json --out <new file>
    # preflight: no database, no model
    uv run python eval/run_field_rewrite_eval.py --split dev --dry-run

Retrieval configs (as in run_field_eval.py): pg = dense + sparse, equal RRF, no ES and no
exact-code channel; es_weighted = four channels, weighted RRF.
Query modes, measured pairwise on the same corpus snapshot:
  off          query = question; no route, no machine model (identical to run_field_eval.py)
  rewrite_only query = plan.rewritten (original_query = question); no route, no machine model
  full_plan    the production search semantics: planned queries (up to 3, merged round-robin),
               KB routing that can only narrow the readable set, machine-model section filter
Metrics are those of run_field_eval.py (top 5 after fusion, before the evidence gate).
Output files are created exclusively and never overwritten; the frozen holdout needs
--confirm-final and is run once.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import math
import time
from pathlib import Path

import run_field_eval as rf  # sets the evaluation database / ES environment before settings load

HERE = rf.HERE
CONFIG_NAMES = ("pg", "es_weighted")
MODES = ("off", "rewrite_only", "full_plan")
CACHE_KIND = "field-rewrite-plans"
PLAN_FIELDS = ("rewritten", "queries", "intent", "confidence", "machine_model", "route_kbs", "skill_item", "notes")


# ------------------------------------------------------------------ plan cache

def prompt_sha() -> str:
    from knoventra.agent.query_plan import PLANNER_PROMPT

    return hashlib.sha256(PLANNER_PROMPT.encode("utf-8")).hexdigest()


def plan_to_dict(plan) -> dict | None:
    return None if plan is None else {k: getattr(plan, k) for k in PLAN_FIELDS}


def plan_from_dict(data: dict | None):
    from knoventra.agent.query_plan import PlanResult

    return None if data is None else PlanResult(**{k: data[k] for k in PLAN_FIELDS})


def make_plans(planner, cases: list[dict]) -> dict[str, dict]:
    """One planner call per case, with the planner's own trace (status, reason, latency)."""
    from langchain_core.messages import HumanMessage

    from knoventra.agent.query_plan import plan_query

    plans = {}
    for c in cases:
        plan, trace = plan_query(planner, [HumanMessage(c["question"])])
        plans[c["id"]] = {"status": trace["status"], "reason": trace.get("reason"),
                          "latency_ms": trace["latency_ms"], "plan": plan_to_dict(plan)}
    return plans


def build_cache(*, split: str, cases: list[dict], cases_sha: str, model: str, thinking: str, plans: dict) -> dict:
    from knoventra.agent.query_plan import QUERY_PLAN_POLICY

    return {"kind": CACHE_KIND, "date": dt.date.today().isoformat(), "split": split, "cases_sha256": cases_sha,
            "n_cases": len(cases), "model": model, "llm_thinking": thinking, "planner_prompt_sha256": prompt_sha(),
            "query_plan_policy": QUERY_PLAN_POLICY, "plans": plans}


def check_cache(cache: dict, *, split: str, cases: list[dict], cases_sha: str) -> None:
    """A cache is reusable only for exactly the cases it was planned for."""
    if cache.get("kind") != CACHE_KIND:
        raise ValueError("not a field rewrite plan cache")
    if cache.get("cases_sha256") != cases_sha:
        raise ValueError("plan cache was made for a different case file (cases_sha256 mismatch)")
    if cache.get("split") != split:
        raise ValueError(f"plan cache is for split {cache.get('split')!r}, not {split!r}")
    if set(cache.get("plans", {})) != {c["id"] for c in cases}:
        raise ValueError("plan cache does not cover exactly this split's cases")
    if cache.get("planner_prompt_sha256") != prompt_sha():
        raise ValueError("plan cache was made with a different planner prompt")


def write_new(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:  # exclusive: never overwrite a result
        f.write(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


# ------------------------------------------------------------------ retrieval

def merge_round_robin(rankings: list[list], top_k: int) -> list:
    """Interleave per-sub-query rankings by rank, dropping repeated chunks."""
    seen, out = set(), []
    for rank in range(max((len(r) for r in rankings), default=0)):
        for r in rankings:
            if rank < len(r) and r[rank].chunk_id not in seen:
                seen.add(r[rank].chunk_id)
                out.append(r[rank])
    return out[:top_k]


def _queries_for(mode: str, question: str, plan, stated: list[str]) -> list[tuple[str, str | None]]:
    """(query, machine_model) pairs to search for one case."""
    if mode == "off" or plan is None:
        return [(question, None)]
    if mode == "rewrite_only":
        return [(plan.rewritten, None)]
    from knoventra.ingestion.cases import models_from_text

    out = []
    for q in plan.queries:
        in_query = models_from_text(q)
        # As in the agent's tools node: in a comparison, a query naming one of the stated models
        # searches that model's sections; otherwise the plan's single model (if any) applies.
        model = in_query[0] if len(stated) > 1 and len(in_query) == 1 and in_query[0] in stated else plan.machine_model
        out.append((q, model))
    return out


def run_mode(db, enc, users, cases, plans: dict, config: str, mode: str, kb_names: dict[str, str]) -> list[dict]:
    from knoventra.config import settings

    previous = settings.rrf_weighting
    settings.rrf_weighting = "weighted" if config == "es_weighted" else "equal"
    try:
        return _score_cases(db, enc, users, cases, plans, config, mode, kb_names)
    finally:
        settings.rrf_weighting = previous  # no global state left behind


def _score_cases(db, enc, users, cases, plans, config, mode, kb_names) -> list[dict]:
    from knoventra.access.scope import NoAccess, compute_scope
    from knoventra.agent.query_plan import RETRIEVAL_INTENTS
    from knoventra.ingestion.cases import models_from_text
    from knoventra.ingestion.normalize import to_simplified
    from knoventra.retrieval.pipeline import narrow_scope
    from knoventra.retrieval.search import search
    from knoventra.search_es.query import default_keyword_channel

    keyword = None if config == "pg" else default_keyword_channel()
    out = []
    for c in cases:
        exp, user = c["expect"], users[c["user"]]
        r = {"id": c["id"], "category": c["category"]}
        try:
            scope = compute_scope(db, user)
        except NoAccess:
            r.update(leaked=0, hit=None)
            out.append(r)
            continue
        entry = plans[c["id"]]
        plan = plan_from_dict(entry["plan"])
        if mode != "off":
            r["planner_status"] = entry["status"]
        if mode == "full_plan" and plan is not None:
            if plan.intent in RETRIEVAL_INTENTS and plan.route_kbs:
                scope, applied = narrow_scope(db, scope, plan.route_kbs)
                r["route"] = {"requested": plan.route_kbs, "applied": applied}
            elif plan.intent not in RETRIEVAL_INTENTS:
                r["non_retrieval_intent"] = plan.intent  # retrieval falls back to the full readable scope
        k = 20 if c["category"] == "access" else rf.TOP_K
        pairs = _queries_for(mode, c["question"], plan, models_from_text(c["question"]))
        started = time.perf_counter()
        results = [search(db, enc, scope, q, top_k=k, original_query=c["question"] if mode != "off" else "",
                          keyword=keyword, use_ident=config != "pg", machine_model=model) for q, model in pairs]
        r["retrieval_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        evidence = results[0].evidence if len(results) == 1 else merge_round_robin([x.evidence for x in results], k)
        r["degraded"] = sorted({d for x in results for d in x.degraded})
        if mode != "off":
            r["queries"] = [q for q, _ in pairs]
        chunk_kb = rf._chunk_kbs(db, [e.chunk_id for e in evidence], kb_names)
        if c["category"] == "access":
            r["leaked"] = sum(1 for e in evidence if chunk_kb[e.chunk_id] in exp["forbidden_kbs"])
        else:
            want = to_simplified(exp["support_text"])

            def supports(e):
                return want in to_simplified(e.text) and (not exp.get("kb") or chunk_kb[e.chunk_id] == exp["kb"])

            rank = next((i for i, e in enumerate(evidence, 1) if supports(e)), None)
            source_rank = next((i for i, e in enumerate(evidence, 1) if supports(e) and (
                not exp.get("title_prefix") or e.document_title.startswith(exp["title_prefix"]))), None)
            r.update(hit=rank is not None, rank=rank, source_hit=source_rank is not None, source_rank=source_rank,
                     top=[f"{chunk_kb[e.chunk_id]}|{e.document_title[:40]}" for e in evidence])
        out.append(r)
    return out


# ------------------------------------------------------------------ statistics

def distribution(values: list[float]) -> dict:
    """n / p50 / p95 / max, nearest rank (ceil(p * n)); empty input gives nulls."""
    xs = sorted(values)
    if not xs:
        return {"n": 0, "p50": None, "p95": None, "max": None}

    def pct(p):
        return xs[max(1, math.ceil(p * len(xs))) - 1]
    return {"n": len(xs), "p50": pct(0.5), "p95": pct(0.95), "max": xs[-1]}


def diff_vs_off(cases: list[dict], off: list[dict], other: list[dict]) -> list[dict]:
    """Per-case rank / source-rank changes relative to the off baseline (answerable cases)."""
    by_id = {c["id"]: c for c in cases}
    changes = []
    for a, b in zip(off, other, strict=True):
        if a["category"] == "access" or "rank" not in a:
            continue
        if (a["rank"], a["source_rank"]) != (b["rank"], b["source_rank"]):
            changes.append({"id": a["id"], "category": a["category"], "question": by_id[a["id"]]["question"],
                            "rank": [a["rank"], b["rank"]], "source_rank": [a["source_rank"], b["source_rank"]],
                            "queries": b.get("queries"), "route": b.get("route"),
                            "non_retrieval_intent": b.get("non_retrieval_intent")})
    return changes


def planner_summary(cases: list[dict], plans: dict) -> dict:
    answerable = [c for c in cases if c["category"] != "access"]
    ok = [c for c in cases if plans[c["id"]]["status"] == "ok"]
    intents: dict[str, int] = {}
    answerable_intents: dict[str, list[str]] = {}
    for c in cases:
        plan = plans[c["id"]]["plan"]
        if plan:
            intents[plan["intent"]] = intents.get(plan["intent"], 0) + 1
            if c in answerable:
                answerable_intents.setdefault(plan["intent"], []).append(c["id"])
    return {"n": len(cases), "ok": len(ok), "fallback": len(cases) - len(ok),
            "fallback_reasons": sorted({plans[c["id"]]["reason"] for c in cases if plans[c["id"]]["status"] != "ok"}),
            "intents": dict(sorted(intents.items())),
            "answerable_intents": {k: v for k, v in sorted(answerable_intents.items())},
            "latency_ms": distribution([plans[c["id"]]["latency_ms"] for c in cases])}


def evaluate(db, enc, users, cases, plans, configs, modes, kb_names) -> dict:
    out = {}
    for config in configs:
        out[config] = {}
        for mode in modes:
            res = run_mode(db, enc, users, cases, plans, config, mode, kb_names)
            out[config][mode] = {"metrics": rf.summarize(res), "cases": res,
                                 "retrieval_latency_ms": distribution([r["retrieval_latency_ms"] for r in res
                                                                       if "retrieval_latency_ms" in r])}
        if "off" in modes:
            for mode in modes:
                if mode != "off":
                    out[config][mode]["changes_vs_off"] = diff_vs_off(cases, out[config]["off"]["cases"],
                                                                      out[config][mode]["cases"])
    return out


# ------------------------------------------------------------------ entry point

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "holdout"], default="dev")
    ap.add_argument("--configs", nargs="+", choices=CONFIG_NAMES, default=list(CONFIG_NAMES))
    ap.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    ap.add_argument("--allow-llm", action="store_true", help="required to create a plan cache: one paid planner call per case")
    ap.add_argument("--plans-in", type=Path, help="reuse this plan cache; no model call")
    ap.add_argument("--plans-out", type=Path, help="where to write the new plan cache (exclusive create)")
    ap.add_argument("--confirm-final", action="store_true", help="required for holdout; run once")
    ap.add_argument("--dry-run", action="store_true", help="no database, no model: validate arguments and print the plan")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.split == "holdout" and not a.confirm_final:
        ap.error("holdout is the frozen final split: pass --confirm-final")
    if not a.plans_in and not a.allow_llm and not a.dry_run:
        ap.error("creating a plan cache makes paid planner calls: pass --allow-llm (or reuse one with --plans-in)")
    if a.plans_in and a.plans_out:
        ap.error("--plans-out is only for a newly created cache")
    cases, cases_sha = rf.load_cases(a.split)
    from knoventra.config import settings

    cache = None
    if a.plans_in:
        cache = json.loads(a.plans_in.read_text(encoding="utf-8"))
        try:
            check_cache(cache, split=a.split, cases=cases, cases_sha=cases_sha)
        except ValueError as exc:
            ap.error(str(exc))
    model_calls = 0 if cache else len(cases)
    plans_out = a.plans_out or HERE / "results" / f"field-rewrite-plans-{a.split}.json"
    out = a.out or HERE / "results" / f"field-{a.split}-rewrite-ablation.json"
    preflight = {"split": a.split, "cases": len(cases), "cases_sha256": cases_sha, "model": settings.llm_model,
                 "llm_thinking": settings.llm_thinking, "planner_prompt_sha256": prompt_sha(),
                 "planner_model_calls": model_calls, "configs": a.configs, "modes": a.modes,
                 "plans_in": str(a.plans_in) if a.plans_in else None,
                 "plans_out": None if cache else str(plans_out), "out": str(out)}
    print(json.dumps(preflight, ensure_ascii=False))
    if a.dry_run:
        return
    if not cache:
        if not settings.llm_model:
            ap.error("KNV_LLM_MODEL is not configured")
    for path in ([out] if cache else [plans_out, out]):
        if path.exists():
            ap.error(f"refusing to overwrite {path}")

    from langchain_openai import ChatOpenAI
    from sqlalchemy import func, select

    from knoventra.agent.model import chat_model_kwargs
    from knoventra.db.models import Chunk, KnowledgeBase, Revision, User
    from knoventra.db.session import SessionLocal
    from knoventra.ingestion.encoder import get_encoder

    enc = get_encoder()
    with SessionLocal() as db:
        kb_names = dict(db.execute(select(KnowledgeBase.id, KnowledgeBase.name)).all())
        broken = rf.validate(db, cases, kb_names)
        if broken:
            raise SystemExit(f"expectations missing from the corpus: {broken}")
        users = {u.email.split("@")[0]: u for u in db.scalars(select(User))}
        if cache is None:
            planner = ChatOpenAI(**{**chat_model_kwargs(), "timeout": settings.query_plan_timeout_s}, max_retries=0)
            plans = make_plans(planner, cases)
            cache = build_cache(split=a.split, cases=cases, cases_sha=cases_sha, model=settings.llm_model,
                                thinking=settings.llm_thinking, plans=plans)
            write_new(plans_out, cache)  # saved before any retrieval, so a later failure never wastes the calls
            print(f"plans -> {plans_out}")
        plans = cache["plans"]
        results = evaluate(db, enc, users, cases, plans, a.configs, a.modes, kb_names)
        corpus = {"published_chunks": db.scalar(select(func.count()).select_from(Chunk).join(Revision)
                                                .where(Revision.status == "PUBLISHED"))}
        db.rollback()
    report = {"date": dt.date.today().isoformat(), "split": a.split, "cases_sha256": cases_sha, "n_cases": len(cases),
              "config": {"encoder": settings.encoder, "top_k": rf.TOP_K, "rrf_k": settings.rrf_k,
                         "weights_identifier": settings.rrf_weights_identifier,
                         "weights_natural": settings.rrf_weights_natural, "model": cache["model"],
                         "llm_thinking": cache["llm_thinking"], "planner_prompt_sha256": cache["planner_prompt_sha256"],
                         "query_plan_policy": cache["query_plan_policy"],
                         "query_plan_timeout_s": settings.query_plan_timeout_s,
                         "query_plan_min_confidence": settings.query_plan_min_confidence,
                         "plan_cache": str(a.plans_in or plans_out)},
              "corpus": corpus, "planner": planner_summary(cases, plans), "results": results}
    write_new(out, report)
    for config in a.configs:
        for mode in a.modes:
            print(config, mode, json.dumps(results[config][mode]["metrics"]["overall"], ensure_ascii=False),
                  "leaked", results[config][mode]["metrics"]["access"]["leaked_chunks"])
    print("planner", json.dumps(report["planner"], ensure_ascii=False))
    print(f"-> {out}")


if __name__ == "__main__":
    main()
