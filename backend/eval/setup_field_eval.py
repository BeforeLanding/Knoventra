"""Build the disposable v4 field-retrieval evaluation corpus (no paid calls).

Creates database `knoventra_eval_v4` (dropped first), migrates, seeds the demo
with the configured encoder (use BGE-M3 for reportable numbers), imports the
fictional journals, publishes deduplicated cases to `field-cases`, publishes
every redacted raw row to `field-rows-raw` (no grants; only the diversity
comparison reads it), and rebuilds the ES alias `knv_eval_v4`.

    KNV_ENCODER=bge-m3 uv run python eval/setup_field_eval.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

ADMIN_URL = os.environ.get("KNV_EVAL_ADMIN_URL", "postgresql+psycopg://knoventra:knoventra@localhost:55432/postgres")
EVAL_DB = "knoventra_eval_v4"
os.environ["KNV_DATABASE_URL"] = ADMIN_URL.rsplit("/", 1)[0] + "/" + EVAL_DB
os.environ.setdefault("KNV_ES_URL", "http://localhost:59200")
os.environ["KNV_ES_INDEX"] = "knv_eval_v4"

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from knoventra.config import settings  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
JOURNALS = BACKEND.parent / "data" / "demo" / "journals"
RAW_KB = "field-rows-raw"


def main() -> None:
    from knoventra.db.models import KnowledgeBase
    from knoventra.db.seed import seed
    from knoventra.ingestion.case_docs import publish_cases
    from knoventra.ingestion.cases import detect_lang, redact
    from knoventra.ingestion.encoder import get_encoder
    from knoventra.ingestion.journal import import_workbooks, parse_workbook
    from knoventra.ingestion.service import add_revision, publish
    from knoventra.search_es.sync import get_client, rebuild

    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {EVAL_DB} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {EVAL_DB}"))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "head")

    enc = get_encoder()
    engine = create_engine(settings.database_url)
    files = sorted(JOURNALS.glob("*.xlsx"))
    with Session(engine) as db, db.begin():
        seed(db, enc)
        report = import_workbooks(db, files, JOURNALS / "machines.json")
        cases_kb = db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == "field-cases"))
        published = publish_cases(db, enc, cases_kb)
        raw = KnowledgeBase(name=RAW_KB, description="未去重的原始日志行（仅评测用，无授权）")
        db.add(raw)
        db.flush()
        rows = 0
        with tempfile.TemporaryDirectory() as tmp:
            for path in files:
                for r in parse_workbook(path).journals:
                    topic, content, notes = (redact(v)[0] for v in (r.topic, r.content, r.notes))
                    body = f"# ROW {r.source.file}:{r.source.row} {topic}\n\n## 处理记录\n{content}\n\n## 注意事项\n{notes}\n"
                    md = Path(tmp) / f"{r.source.file}-{r.source.row}.md"
                    md.write_bytes(body.encode("utf-8"))
                    lang = detect_lang(topic + content)
                    rev = add_revision(db, enc, kb_id=raw.id, title=f"ROW {r.source.file}:{r.source.row}",
                                       lang=lang, path=md)
                    publish(db, rev.id)
                    rows += 1
    with Session(engine) as db, db.begin():
        index = rebuild(db, get_client(), settings.es_index)
    print(f"encoder={settings.encoder} db={EVAL_DB} es={settings.es_index}->{index}")
    print(f"cases: {report['counts']}  published: {published}  raw rows: {rows}")


if __name__ == "__main__":
    main()
