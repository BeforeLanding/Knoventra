import pytest
from fastapi.testclient import TestClient

from knoventra.api.deps import get_db
from knoventra.main import app


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def tok(client, who):
    r = client.post("/api/v1/auth/login", json={"email": f"{who}@example.test", "password": "demo-pass-1"})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_login_and_me(client):
    assert client.post("/api/v1/auth/login", json={"email": "hr@example.test", "password": "x"}).status_code == 401
    me = client.get("/api/v1/me", headers=tok(client, "hr")).json()
    assert (me["department"], me["region"]) == ("HR", "SG")
    assert client.get("/api/v1/me").status_code == 401


def test_debug_search_respects_scope(client):
    q = {"q": "年假怎么申请"}
    hr = client.get("/api/v1/debug/search", params=q, headers=tok(client, "hr")).json()
    ops = client.get("/api/v1/debug/search", params=q, headers=tok(client, "sg-ops")).json()
    assert any(e["document_title"] == "休假制度" for e in hr["evidence"])
    assert not any(e["document_title"] == "休假制度" for e in ops["evidence"])


def test_admin_endpoints_forbidden_for_users(client):
    r = client.post("/api/v1/admin/revisions/x/publish", headers=tok(client, "sg-ops"))
    assert r.status_code == 403


def test_admin_upload_then_publish(client):
    h = tok(client, "admin")
    r = client.post("/api/v1/admin/documents", headers=h,
                    data={"kb": "it-public", "title": "VPN 使用说明", "lang": "zh-Hans"},
                    files={"file": ("v.md", "# VPN 使用说明\n## 远程接入\n新版：使用 SSO 登录 VPN。".encode(), "text/markdown")})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert client.post(f"/api/v1/admin/revisions/{r.json()['revision_id']}/publish", headers=h).json()["status"] == "PUBLISHED"
    ev = client.get("/api/v1/debug/search", params={"q": "VPN SSO"}, headers=tok(client, "hr")).json()["evidence"]
    vpn = [e for e in ev if e["document_title"] == "VPN 使用说明"]
    assert vpn and {e["revision"] for e in vpn} == {2}


def test_admin_list_documents(client):
    assert client.get("/api/v1/admin/documents", headers=tok(client, "hr")).status_code == 403
    d = client.get("/api/v1/admin/documents", headers=tok(client, "admin")).json()
    e17 = next(x for x in d["documents"] if x["title"].startswith("E17"))
    assert [r["status"] for r in e17["revisions"]] == ["PUBLISHED", "DRAFT"] and "hr" in d["kbs"]
