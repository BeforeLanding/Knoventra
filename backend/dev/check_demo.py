"""Offline acceptance over real HTTP and a real server restart.

Run from backend/: uv run --no-sync python dev/check_demo.py
Only a generated disposable database is migrated/seeded/deleted. Uses the same
KNV_TEST_ADMIN_URL as check_migrations.py, never the configured application DB.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_ADMIN_URL = "postgresql+psycopg://knoventra:knoventra@localhost:55432/postgres"


def require(condition, message):
    # Acceptance must still execute when Python is run with -O.
    if not condition:
        raise RuntimeError(message)


def offline_environment(database_url):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("KNV_")}
    env.update(KNV_DATABASE_URL=database_url, KNV_ENCODER="hash", KNV_RERANK="off",
               KNV_LLM_API_KEY="", KNV_LLM_BASE_URL="", KNV_LLM_MODEL="",
               KNV_ENV="dev", HF_HUB_OFFLINE="1", PYTHONUTF8="1",
               PYTHONPATH=str(BACKEND / "src"))
    return env


@contextmanager
def server(env, directory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Run outside backend/ so an existing .env cannot affect offline acceptance.
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "mock_server:app", "--app-dir", str(BACKEND / "dev"),
             "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
            cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1/", timeout=30,
                              trust_env=False) as client:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    require(process.poll() is None, "offline HTTP server exited during startup")
                    try:
                        if client.get("me", timeout=1).status_code == 401:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("offline HTTP server did not become ready within 30 seconds")
                yield client
        except Exception:
            log.seek(0)
            print(log.read().decode("utf-8", errors="replace"), file=sys.stderr)
            raise
        finally:
            # A Windows venv python.exe can be a redirector with a Python child.
            # Killing only the redirector leaves the server and its cwd alive.
            if os.name == "nt" and process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            elif process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def request(client, method, path, headers=None, *, status=200, **kwargs):
    response = client.request(method, path, headers=headers, **kwargs)
    require(response.status_code == status,
            f"{method} {path}: expected HTTP {status}, got {response.status_code}")
    return response


def events(client, path, headers, body):
    response = request(client, "POST", path, headers, json=body)
    require(response.headers.get("content-type", "").startswith("text/event-stream"), "expected SSE")
    result, name = [], None
    for line in response.text.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            result.append((name, json.loads(line[6:])))
    require(bool(result) and result[0][0] == "meta", "SSE must begin with meta")
    require(result[-1][0] in {"done", "confirm"}, "SSE did not finish with done or confirm")
    return dict(result)


def login_all(client):
    manifest = json.loads((BACKEND.parent / "data/demo/manifest.json").read_text(encoding="utf-8"))
    headers = {}
    for user in manifest["users"]:
        payload = request(client, "POST", "auth/login",
                          json={"email": user["email"], "password": manifest["password"]}).json()
        auth = {"Authorization": "Bearer " + payload["token"]}
        me = request(client, "GET", "me", auth).json()
        require(all(me[key] == user[key] for key in ("email", "region", "department", "role")),
                "login returned an unexpected identity")
        headers[user["email"].split("@")[0]] = auth
    return headers


def check_answer_and_boundaries(client, auth):
    ops, hr = auth["sg-ops"], auth["hr"]
    answer = events(client, "chat", ops, {"message": "E17 设备报代码 42 先查什么"})
    require("error" not in answer and answer.get("sources") and answer.get("done", {}).get("answer"),
            "authorized answer must contain evidence and finish without errors")
    source = answer["sources"][0]
    require(source["document_title"].startswith("E17") and source["revision"] == 1 and source["page"] == 2,
            "answer must cite the published E17 v1 page 2")
    require(f'[{source["alias"]}]' in answer["done"]["answer"], "answer must reference a registered source")
    cid = answer["meta"]["conversation_id"]
    preview = request(client, "GET", f'evidence/{source["chunk_id"]}', ops).json()
    require(preview["text"] and preview["revision_status"] == "PUBLISHED", "preview is not published evidence")
    for path in (f'evidence/{source["chunk_id"]}', f"conversations/{cid}/messages", f"conversations/{cid}/state"):
        request(client, "GET", path, hr, status=404)
    cross_script = events(client, "chat", auth["tw-ops"], {"message": "循環泵 P03-11 壓力偏低先查什麼"})
    require("error" not in cross_script and any(
        item["document_title"] == "泵组维护手册" and item["lang"] == "zh-Hans"
        for item in cross_script.get("sources", [])), "traditional query did not retrieve the simplified manual")
    regional = request(client, "GET", "debug/search", auth["cn-ops"], params={"q": "C21-05 校准过期"}).json()["evidence"]
    require(regional and all(item["document_title"] == "校准流程（仅限中国区）" for item in regional),
            "CN account did not retrieve its regional manual")
    request(client, "GET", f'evidence/{regional[0]["chunk_id"]}', auth["cn-ops"])
    request(client, "GET", f'evidence/{regional[0]["chunk_id"]}', ops, status=404)
    require(request(client, "GET", "debug/search", ops,
                    params={"q": "C21-05 校准过期"}).json()["evidence"] == [], "regional retrieval leaked to SG")
    for who, query in (("hr", "E17-42 排查"), ("sg-ops", "E17-44 怎么处理")):
        refusal = events(client, "chat", auth[who], {"message": query})
        require(refusal.get("error", {}).get("code") == "INSUFFICIENT_EVIDENCE"
                and refusal.get("sources") == [], "expected refusal without sources")
        state = request(client, "GET", f'conversations/{refusal["meta"]["conversation_id"]}/state', auth[who]).json()
        require(state["messages"][-1]["error_code"] == "INSUFFICIENT_EVIDENCE"
                and state["messages"][-1]["sources"] == [], "refusal was not persisted")
    return source


def check_publication(client, auth, old_source):
    ops, admin = auth["sg-ops"], auth["admin"]
    catalog = request(client, "GET", "admin/documents", admin).json()
    doc = next(doc for doc in catalog["documents"] if doc["title"] == old_source["document_title"])
    draft = next(rev for rev in doc["revisions"] if rev["version"] == 2)
    require(draft["status"] == "DRAFT", "seed must contain E17 v2 draft")
    request(client, "POST", f'admin/revisions/{draft["id"]}/publish', ops, status=403)
    search = lambda: request(client, "GET", "debug/search", ops, params={"q": "E17-42 排查"}).json()["evidence"]
    before = [item for item in search() if item["document_title"] == doc["title"]]
    require(before and all(item["revision"] == 1 for item in before), "draft replaced v1 before publication")
    published = request(client, "POST", f'admin/revisions/{draft["id"]}/publish', admin).json()
    require(published["status"] == "PUBLISHED", "publication did not succeed")
    after = [item for item in search() if item["document_title"] == doc["title"]]
    require(after and all(item["revision"] == 2 for item in after), "published retrieval still contains v1")
    historical = request(client, "GET", f'evidence/{old_source["chunk_id"]}', ops).json()
    require(historical["revision_status"] == "RETIRED", "historical reference must remain verifiable")


def start_confirmation(client, auth):
    pending = events(client, "chat", auth, {"message": "转人工：E17-42 复位后仍报警"})
    require("confirm" in pending and "done" not in pending and "error" not in pending,
            "handoff must wait for confirmation")
    require(pending["confirm"]["status"] == "PENDING", "confirmation must be pending")
    return pending["meta"]["conversation_id"], pending["confirm"]["confirmation_id"]


def records(client, auth):
    return request(client, "GET", "escalations", auth).json()["items"]


def check_resumed_confirmation(client, auth, cid, confirmation_id):
    ops, hr = auth["sg-ops"], auth["hr"]
    state = request(client, "GET", f"conversations/{cid}/state", ops).json()
    require(state["blocked"] and state["pending_confirmation"]["confirmation_id"] == confirmation_id,
            "new server process did not restore the original pending confirmation")
    require(records(client, ops) == [], "handoff created a record before approval")
    request(client, "POST", "chat/confirm", hr, status=404,
            json={"confirmation_id": confirmation_id, "approve": True})
    approved = events(client, "chat/confirm", ops, {"confirmation_id": confirmation_id, "approve": True})
    require("error" not in approved and "done" in approved, "approval failed")
    saved = records(client, ops)
    require(len(saved) == 1 and saved[0]["created_at"] and saved[0]["status"] == "OPEN", "expected one open record")
    eid = saved[0]["id"]
    for decision in (True, False):
        duplicate = events(client, "chat/confirm", ops, {"confirmation_id": confirmation_id, "approve": decision})
        require(duplicate["meta"].get("status") == "APPROVED", "stored approval changed on replay")
    require([item["id"] for item in records(client, ops)] == [eid], "duplicate approval created another record")
    request(client, "GET", f"escalations/{eid}", hr, status=404)
    require(records(client, hr) == [], "another user's record leaked into list")
    _, rejected_id = start_confirmation(client, ops)
    rejected = events(client, "chat/confirm", ops, {"confirmation_id": rejected_id, "approve": False})
    require("error" not in rejected and "取消" in rejected["done"]["answer"], "rejection did not complete")
    require([item["id"] for item in records(client, ops)] == [eid], "rejection created a record")
    return eid


def main():
    admin_url = make_url(os.environ.get("KNV_TEST_ADMIN_URL", DEFAULT_ADMIN_URL))
    require(admin_url.get_backend_name() == "postgresql", "demo acceptance requires PostgreSQL")
    database = "knoventra_demo_check_" + uuid.uuid4().hex
    env = offline_environment(admin_url.set(database=database).render_as_string(hide_password=False))
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5})
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
            created = True
        with tempfile.TemporaryDirectory(prefix="knoventra_demo_check_") as directory:
            migration = ("from alembic import command; from alembic.config import Config; "
                         f"c=Config({str(BACKEND / 'alembic.ini')!r}); "
                         f"c.set_main_option('script_location', {str(BACKEND / 'alembic')!r}); command.upgrade(c, 'head')")
            for args in (["-c", migration], ["-m", "knoventra.db.seed"]):
                subprocess.run([sys.executable, *args], cwd=directory, env=env, check=True, timeout=90)
            with server(env, directory) as client:
                auth = login_all(client)
                source = check_answer_and_boundaries(client, auth)
                check_publication(client, auth, source)
                cid, confirmation_id = start_confirmation(client, auth["sg-ops"])
            # No in-memory graph/pool survives this boundary; reuse the stored JWT.
            with server(env, directory) as client:
                eid = check_resumed_confirmation(client, auth, cid, confirmation_id)
            with server(env, directory) as client:
                replay = events(client, "chat/confirm", auth["sg-ops"],
                                {"confirmation_id": confirmation_id, "approve": True})
                require(replay["meta"].get("status") == "APPROVED", "approval lost after second restart")
                require([item["id"] for item in records(client, auth["sg-ops"])] == [eid],
                        "record was duplicated or lost after second restart")
        print("Offline HTTP acceptance passed: six logins, evidence/isolation/refusal, publication, "
              "confirmation across two server restarts, approval replay and rejection. No model calls.")
    finally:
        try:
            if created:
                with admin.connect() as connection:
                    connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
                print("Disposable acceptance database removed.")
        finally:
            admin.dispose()


if __name__ == "__main__":
    main()
