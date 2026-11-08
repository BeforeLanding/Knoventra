"""Exercise migrations in a newly created disposable database, never the app DB.

Run from backend/: uv run python dev/check_migrations.py
Uses KNV_TEST_ADMIN_URL (same demo default as pytest) to create/drop a unique test DB.
No model or network calls are made by this script.
"""
from __future__ import annotations

import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from knoventra.config import settings
from knoventra.db.models import Chunk, Document, Revision, User
from knoventra.db.seed import seed
from knoventra.ingestion.encoder import HashEncoder


def main() -> None:
    admin_url = make_url(os.environ.get("KNV_TEST_ADMIN_URL",
                         "postgresql+psycopg://knoventra:knoventra@localhost:55432/postgres"))
    if admin_url.get_backend_name() != "postgresql":
        raise ValueError("migration smoke check requires PostgreSQL")
    # Identifiers are generated here, never accepted as command-line input.
    database = "knoventra_migrations_" + uuid.uuid4().hex
    test_url = admin_url.set(database=database)
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    original_url = settings.database_url
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5})
    engine = None
    created = False
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{database}"'))
            created = True
        settings.database_url = test_url.render_as_string(hide_password=False)
        command.upgrade(cfg, "head")
        command.check(cfg)
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        command.check(cfg)
        engine = create_engine(test_url)
        with Session(engine) as db, db.begin():
            first = seed(db, HashEncoder())
            tables = (User, Document, Revision, Chunk)
            before = {table.__tablename__: db.scalar(select(func.count()).select_from(table)) for table in tables}
            second = seed(db, HashEncoder())
            after = {table.__tablename__: db.scalar(select(func.count()).select_from(table)) for table in tables}
            if not first or second or before != after:
                raise RuntimeError("demo seed replay is not idempotent")
        print("Migration upgrade/downgrade, metadata check and demo seed replay passed.")
    finally:
        settings.database_url = original_url
        if engine is not None:
            engine.dispose()
        if created:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    main()
