import os
import uuid
from pathlib import Path

import pytest

TEST_DB = "knoventra_test"
ADMIN_URL = os.environ.get("KNV_TEST_ADMIN_URL", "postgresql+psycopg://knoventra:knoventra@localhost:55432/postgres")
os.environ["KNV_DATABASE_URL"] = ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB
os.environ["KNV_ENCODER"] = "hash"

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from knoventra.config import settings  # noqa: E402
from knoventra.db.models import User  # noqa: E402
from knoventra.db.seed import seed  # noqa: E402
from knoventra.ingestion.encoder import HashEncoder  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {TEST_DB}"))
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    command.upgrade(cfg, "head")
    eng = create_engine(settings.database_url)
    with Session(eng) as s, s.begin():
        seed(s, HashEncoder())
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """Per-test session inside a rolled-back outer transaction (savepoints allowed)."""
    conn = engine.connect()
    outer = conn.begin()
    s = Session(conn, join_transaction_mode="create_savepoint")
    yield s
    s.close()
    outer.rollback()
    conn.close()


@pytest.fixture
def enc():
    return HashEncoder()


@pytest.fixture
def users(db):
    return {u.email.split("@")[0]: u for u in db.scalars(select(User))}


# Elasticsearch (compose service `es`); tests using these skip when it is unreachable.
ES_URL = os.environ.get("KNV_TEST_ES_URL", "http://localhost:59200")


@pytest.fixture(scope="session")
def es():
    from elasticsearch import Elasticsearch

    client = Elasticsearch(ES_URL, request_timeout=10, max_retries=0)
    try:
        client.info()
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("KNV_REQUIRE_ES"):  # CI: a missing ES must fail, not silently skip
            pytest.fail(f"Elasticsearch required but not reachable at {ES_URL}: {exc}")
        pytest.skip(f"Elasticsearch not reachable at {ES_URL}: {exc}")
    return client


@pytest.fixture
def alias(es):
    name = f"knv_test_{uuid.uuid4().hex[:10]}"
    yield name
    # ES 8 refuses wildcard deletes (action.destructive_requires_name); resolve names first.
    for index in es.indices.get(index=f"{name}-*", allow_no_indices=True, ignore_unavailable=True):
        es.indices.delete(index=index)
