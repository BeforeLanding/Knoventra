import datetime as dt
from pathlib import Path
import runpy
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import null, select

from knoventra.api.deps import get_db
from knoventra.auth.security import issue_token
from knoventra.db.models import Escalation
from knoventra.main import app


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def headers(user):
    return {"Authorization": "Bearer " + issue_token(user.id)}


def record(db, user, number, created):
    row = Escalation(id=f"{number:032x}", user_id=user.id, operation_id=uuid.uuid4().hex,
                     summary=f"转交摘要 {number}", status="OPEN", evidence_refs=[],
                     created_at=created if created is not None else null())
    db.add(row)
    db.flush()
    return row


def test_owned_keyset_pages_handle_equal_dates_and_unknown_legacy_dates(client, db, users):
    now = dt.datetime(2026, 11, 7, tzinfo=dt.UTC)
    rows = [record(db, users["sg-ops"], n, created) for n, created in
            [(1, now), (2, now), (3, now - dt.timedelta(days=1)), (4, None), (5, None)]]
    foreign = record(db, users["hr"], 6, now + dt.timedelta(days=1))
    h = headers(users["sg-ops"])
    seen, cursor = [], None
    for _ in range(3):
        params = {"limit": 2, **({"before": cursor} if cursor else {})}
        response = client.get("/api/v1/escalations", headers=h, params=params)
        assert response.status_code == 200
        page = response.json()
        seen.extend(r["id"] for r in page["items"])
        cursor = page["next_cursor"]
    assert seen == [rows[i].id for i in (1, 0, 2, 4, 3)] and cursor is None
    assert len(set(seen)) == 5
    assert page["items"][0]["created_at"] is None
    assert client.get("/api/v1/escalations", headers=h, params={"before": foreign.id}).status_code == 404
    assert client.get("/api/v1/escalations", headers=h, params={"before": "missing"}).status_code == 404
    assert client.get(f"/api/v1/escalations/{foreign.id}", headers=h).status_code == 404
    assert client.get(f"/api/v1/escalations/{rows[0].id}", headers=h).json()["created_at"] is not None
    assert client.get("/api/v1/escalations").status_code == 401


def test_newer_inserts_do_not_duplicate_or_shift_older_cursor_pages(client, db, users):
    now = dt.datetime(2026, 11, 7, tzinfo=dt.UTC)
    for n in range(1, 5):
        record(db, users["sg-ops"], n, now + dt.timedelta(minutes=n))
    h = headers(users["sg-ops"])
    first = client.get("/api/v1/escalations", headers=h, params={"limit": 2}).json()
    new = record(db, users["sg-ops"], 9, now + dt.timedelta(days=1))
    rest = client.get("/api/v1/escalations", headers=h, params={"limit": 2, "before": first["next_cursor"]}).json()
    assert [r["summary"] for r in first["items"] + rest["items"]] == [f"转交摘要 {n}" for n in (4, 3, 2, 1)]
    assert rest["next_cursor"] is None
    assert client.get("/api/v1/escalations", headers=h).json()["items"][0]["id"] == new.id


@pytest.mark.parametrize("limit", [0, 101])
def test_list_limit_is_bounded(client, users, limit):
    assert client.get("/api/v1/escalations", headers=headers(users["sg-ops"]), params={"limit": limit}).status_code == 422


def test_empty_owned_list_has_no_cursor(client, users):
    assert client.get("/api/v1/escalations", headers=headers(users["sg-ops"])).json() == {"items": [], "next_cursor": None}


def test_created_date_is_generated_and_migration_keeps_unknown_history(db, users):
    row = Escalation(user_id=users["sg-ops"].id, operation_id=uuid.uuid4().hex, summary="新记录", evidence_refs=[])
    db.add(row)
    db.flush()
    assert row.created_at is not None
    original_id = row.id
    steps = runpy.run_path(str(Path(__file__).resolve().parents[1] / "alembic/versions/0006_escalation_created_at.py"))
    with Operations.context(MigrationContext.configure(db.connection())):
        steps["downgrade"]()
        steps["upgrade"]()
    db.expire_all()
    old = db.get(Escalation, original_id)
    assert old.summary == "新记录" and old.created_at is None
    newer = Escalation(user_id=users["sg-ops"].id, operation_id=uuid.uuid4().hex, summary="升级后", evidence_refs=[])
    db.add(newer)
    db.flush()
    assert newer.created_at is not None
