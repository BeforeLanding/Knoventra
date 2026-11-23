"""Offline field-retrieval evaluation (no LLM, no paid calls).

Runs against the corpus built by `setup_field_eval.py`.

    KNV_ENCODER=bge-m3 uv run python eval/run_field_eval.py --split dev
    KNV_ENCODER=bge-m3 uv run python eval/run_field_eval.py --split holdout --confirm-final

Configs: pg (dense+sparse, equal RRF: the v3 baseline), es_equal (+ES +exact-code
channel, equal RRF), es_weighted (+ES +exact-code, weighted RRF). Metrics are
Recall@5 and MRR on the top 5 after fusion, before the evidence gate. A hit means
a top-5 chunk contains the expected fact (from the expected KB if one is given);
source_recall@5 also requires the expected document. Access
cases count leaked chunks; the diversity check compares duplicate slots in the
top 5 between raw journal rows and deduplicated cases. Holdout reports are
written with exclusive create, so each config is scored on holdout once.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import statistics
from pathlib import Path

EVAL_DB = "knoventra_eval_v4"
ADMIN_URL = os.environ.get("KNV_EVAL_ADMIN_URL", "postgresql+psycopg://knoventra:knoventra@localhost:55432/postgres")
os.environ["KNV_DATABASE_URL"] = ADMIN_URL.rsplit("/", 1)[0] + "/" + EVAL_DB
os.environ.setdefault("KNV_ES_URL", "http://localhost:59200")
os.environ["KNV_ES_INDEX"] = "knv_eval_v4"

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases_field_v1.jsonl"
CONFIGS = ("pg", "es_equal", "es_weighted")
TOP_K = 5


def load_cases(split: str) -> tuple[list[dict], str]:
    raw = CASES.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    return [r for r in rows if r["split"] == split], hashlib.sha256(raw).hexdigest()


def run(db, enc, users, cases, config: str, kb_names: dict[str, str]) -> list[dict]:
    from knoventra.access.scope import NoAccess, compute_scope
    from knoventra.config import settings
    from knoventra.ingestion.normalize import to_simplified
    from knoventra.retrieval.search import search
    from knoventra.search_es.query import default_keyword_channel

    settings.rrf_weighting = "weighted" if config == "es_weighted" else "equal"
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
        k = 20 if c["category"] == "access" else TOP_K
        res = search(db, enc, scope, c["question"], top_k=k, keyword=keyword, use_ident=config != "pg")
        r["degraded"] = res.degraded
        chunk_kb = _chunk_kbs(db, [e.chunk_id for e in res.evidence], kb_names)
        if c["category"] == "access":
            r["leaked"] = sum(1 for e in res.evidence if chunk_kb[e.chunk_id] in exp["forbidden_kbs"])
        else:
            want = to_simplified(exp["support_text"])

            def supports(e):  # answer support: the fact is in the chunk, from the expected KB if given
                return want in to_simplified(e.text) and (not exp.get("kb") or chunk_kb[e.chunk_id] == exp["kb"])

            rank = next((i for i, e in enumerate(res.evidence, 1) if supports(e)), None)
            source_rank = next((i for i, e in enumerate(res.evidence, 1) if supports(e) and (
                not exp.get("title_prefix") or e.document_title.startswith(exp["title_prefix"]))), None)
            r.update(hit=rank is not None, rank=rank, source_hit=source_rank is not None, source_rank=source_rank,
                     top=[f"{chunk_kb[e.chunk_id]}|{e.document_title[:40]}" for e in res.evidence])
        out.append(r)
    return out


def _chunk_kbs(db, chunk_ids, kb_names):
    from sqlalchemy import select

    from knoventra.db.models import Chunk

    return {cid: kb_names[kb] for cid, kb in db.execute(select(Chunk.id, Chunk.kb_id).where(Chunk.id.in_(chunk_ids)))}


def summarize(results: list[dict]) -> dict:
    answerable = [r for r in results if r["category"] != "access"]

    def metric(rs):
        return {"n": len(rs), "recall@5": round(sum(r["hit"] for r in rs) / len(rs), 3),
                "mrr": round(sum(1 / r["rank"] for r in rs if r["rank"]) / len(rs), 3),
                "source_recall@5": round(sum(r["source_hit"] for r in rs) / len(rs), 3)}

    cats = sorted({r["category"] for r in answerable})
    access = [r for r in results if r["category"] == "access"]
    return {"overall": metric(answerable), "by_category": {c: metric([r for r in answerable if r["category"] == c])
                                                           for c in cats},
            "access": {"n": len(access), "leaked_chunks": sum(r["leaked"] for r in access)},
            "degraded_queries": sum(1 for r in results if r.get("degraded"))}


def diversity(db, enc, users, cases, kb_ids: dict[str, str]) -> dict:
    """Duplicate slots in the top 5 for case questions: raw rows vs deduplicated cases."""
    from knoventra.access.scope import AccessScope
    from knoventra.config import settings
    from knoventra.ingestion.cases import normalize_for_dedup
    from knoventra.retrieval.search import search
    from knoventra.search_es.query import default_keyword_channel

    settings.rrf_weighting = "weighted"
    out = {}
    questions = [c for c in cases if c["category"] == "case"]
    for kb in ("field-rows-raw", "field-cases"):
        dups = []
        for c in questions:
            scope = AccessScope(users[c["user"]].id, frozenset({kb_ids[kb]}))
            ev = search(db, enc, scope, c["question"], top_k=TOP_K, keyword=default_keyword_channel()).evidence
            dups.append(len(ev) - len({normalize_for_dedup(e.text) for e in ev}))
        out[kb] = {"questions": len(questions), "mean_duplicate_slots_in_top5": round(statistics.mean(dups), 2)
                   if dups else None, "per_question": dups}
    return out


def validate(db, cases, kb_names) -> list[str]:
    """Every expectation must exist in the published corpus, or the case is broken."""
    from sqlalchemy import select

    from knoventra.db.models import Chunk, Document, Revision
    from knoventra.ingestion.normalize import to_simplified

    rows = db.execute(select(Document.title, Chunk.kb_id, Chunk.text).join(Revision, Revision.document_id == Document.id)
                      .join(Chunk, Chunk.revision_id == Revision.id).where(Revision.status == "PUBLISHED")).all()
    broken = []
    for c in cases:
        exp = c["expect"]
        if c["category"] == "access":
            continue
        want = to_simplified(exp["support_text"])
        if not any((not exp.get("title_prefix") or t.startswith(exp["title_prefix"]))
                   and (not exp.get("kb") or kb_names[kb] == exp["kb"]) and want in to_simplified(text)
                   for t, kb, text in rows):
            broken.append(c["id"])
    return broken


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "holdout"], default="dev")
    ap.add_argument("--configs", nargs="+", choices=CONFIGS, default=list(CONFIGS))
    ap.add_argument("--confirm-final", action="store_true", help="required for holdout; run once per config")
    ap.add_argument("--weights-identifier", type=json.loads)
    ap.add_argument("--weights-natural", type=json.loads)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.split == "holdout" and not a.confirm_final:
        ap.error("holdout is the frozen final split: pass --confirm-final")

    from sqlalchemy import func, select

    from knoventra.config import settings
    from knoventra.db.models import Chunk, KnowledgeBase, Revision, User
    from knoventra.db.session import SessionLocal
    from knoventra.ingestion.encoder import get_encoder

    if a.weights_identifier:
        settings.rrf_weights_identifier = a.weights_identifier
    if a.weights_natural:
        settings.rrf_weights_natural = a.weights_natural
    cases, cases_sha = load_cases(a.split)
    enc = get_encoder()
    report = {"date": dt.date.today().isoformat(), "split": a.split, "cases_sha256": cases_sha,
              "n_cases": len(cases), "config": {"encoder": settings.encoder, "top_k": TOP_K, "rrf_k": settings.rrf_k,
                                                "weights_identifier": settings.rrf_weights_identifier,
                                                "weights_natural": settings.rrf_weights_natural},
              "results": {}}
    with SessionLocal() as db:
        kb_names = dict(db.execute(select(KnowledgeBase.id, KnowledgeBase.name)).all())
        broken = validate(db, cases, kb_names)
        if broken:
            raise SystemExit(f"expectations missing from the corpus: {broken}")
        report["corpus"] = {"published_chunks": db.scalar(select(func.count()).select_from(Chunk).join(Revision)
                                                          .where(Revision.status == "PUBLISHED"))}
        users = {u.email.split("@")[0]: u for u in db.scalars(select(User))}
        for config in a.configs:
            res = run(db, enc, users, cases, config, kb_names)
            report["results"][config] = {"metrics": summarize(res), "cases": res}
        report["diversity"] = diversity(db, enc, users, cases, {v: k for k, v in kb_names.items()})
        db.rollback()
    out = a.out or HERE / "results" / (f"field-holdout-{'-'.join(a.configs)}.json" if a.split == "holdout"
                                      else f"field-dev-{dt.datetime.now():%Y%m%d-%H%M%S}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x" if a.split == "holdout" else "w", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    for config in a.configs:
        print(config, json.dumps(report["results"][config]["metrics"], ensure_ascii=False))
    print("diversity", json.dumps({k: v["mean_duplicate_slots_in_top5"] for k, v in report["diversity"].items()}))
    print(f"-> {out}")


if __name__ == "__main__":
    main()
