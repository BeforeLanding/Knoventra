"""Isolated real-model val5 comparison; local retrieval only, no LLM calls.

    uv run python eval/run_rerank_pair.py --confirm-final --out-dir eval/results/new-v2m3-pair

Requires cached BGE-M3 and the requested reranker. Creates/drops a private
temporary PostgreSQL database using KNV_TEST_ADMIN_URL, never the app database.
Each configuration runs once; existing output directories are rejected.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-final", action="store_true")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--model", default="BAAI/bge-reranker-v2-m3")
    args = parser.parse_args()
    if not args.confirm_final:
        parser.error("val5 measurement requires --confirm-final")
    if args.out_dir.exists():
        parser.error("output directory already exists; choose a fresh path")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from knoventra.config import settings

    backend = Path(__file__).resolve().parents[1]
    output = args.out_dir.resolve()
    output.mkdir(parents=True)
    admin_url = make_url(os.environ.get("KNV_TEST_ADMIN_URL",
                         "postgresql+psycopg://knoventra:knoventra@localhost:55432/postgres"))
    if admin_url.get_backend_name() != "postgresql":
        parser.error("comparison requires PostgreSQL")
    database = "knoventra_rerank_" + uuid.uuid4().hex
    target = admin_url.set(database=database).render_as_string(hide_password=False)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5})
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    original_url = settings.database_url
    created = False
    env = {**os.environ, "KNV_DATABASE_URL": target, "KNV_ENCODER": "bge-m3", "KNV_RERANK": "off",
           "KNV_LLM_API_KEY": "", "KNV_LLM_BASE_URL": "", "KNV_LLM_MODEL": "",
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_PROGRESS_BARS": "1"}
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{database}"'))
            created = True
        settings.database_url = target
        command.upgrade(cfg, "head")
        # Separate processes free GPU memory between seed and each configuration.
        subprocess.run([sys.executable, "-m", "knoventra.db.seed"], cwd=backend, env=env, check=True)
        base = [sys.executable, "eval/run_eval.py", "--split", "val5", "--confirm-final", "--mode", "retrieval"]
        subprocess.run([*base, "--out", str(output / "off.json")], cwd=backend, env=env, check=True)
        subprocess.run([*base, "--rerank", "--rerank-model", args.model, "--out", str(output / "rerank.json")],
                       cwd=backend, env=env, check=True)
        off = json.loads((output / "off.json").read_text(encoding="utf-8"))
        rerank = json.loads((output / "rerank.json").read_text(encoding="utf-8"))
        if off["cases_sha256"] != rerank["cases_sha256"] or off["corpus"] != rerank["corpus"]:
            raise RuntimeError("paired case/corpus fingerprints differ")
        manifest = {"schema_version": 1, "mode": "retrieval", "split": "val5", "runs_per_configuration": 1,
                    "model": args.model, "cases_sha256": off["cases_sha256"], "corpus": off["corpus"],
                    "outputs": ["off.json", "rerank.json"], "llm_calls": 0,
                    "isolation": "fresh disposable PostgreSQL database; cached local models only"}
        with (output / "pair.json").open("x", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print("Paired retrieval comparison completed:", output)
    finally:
        settings.database_url = original_url
        try:
            if created:
                with admin.connect() as conn:
                    conn.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        finally:
            admin.dispose()


if __name__ == "__main__":
    main()
