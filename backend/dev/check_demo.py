"""Offline acceptance over real HTTP and a real server restart.

Run from backend/: uv run --no-sync python dev/check_demo.py
Only a generated disposable database is migrated/seeded/deleted. Uses the same
KNV_TEST_ADMIN_URL as check_migrations.py, never the configured application DB.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import re
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


@contextmanager
def mcp_server(env, directory, port, calls):
    """The real MCP entrypoint (`python -m knoventra.mcp_server`); appends its access-log
    status codes for POST /mcp to `calls` when it stops."""
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "knoventra.mcp_server"], cwd=directory,
            env={**env, "KNV_MCP_PORT": str(port)}, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                require(process.poll() is None, "MCP server exited during startup")
                try:
                    # No identity token: the bearer middleware must refuse before any tool runs.
                    if httpx.post(f"http://127.0.0.1:{port}/mcp", json={}, timeout=1,
                                  trust_env=False).status_code == 401:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("MCP server did not become ready within 30 seconds")
            yield
        finally:
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
            log.seek(0)
            text_log = log.read().decode("utf-8", errors="replace")
            codes = [int(code) for code in re.findall(r'"POST /mcp HTTP/1\.1" (\d{3})', text_log)]
            if 401 in codes:
                codes.remove(401)  # the one readiness probe above, sent without a token on purpose
            calls.extend(codes)


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


def answer_text(result):
    return result.get("done", {}).get("answer", "")


def check_v4_stories(client, auth):
    """The v4 stories the fixed-answer server can show: skill lookup, a multi-turn help request
    with expert recommendation, machine-model clarification, and memory recall. Only the choice
    of tool is scripted; slots, recommendation, clarification and recall are server code."""
    ops = auth["sg-ops"]
    skills = events(client, "chat", ops, {"message": "谁会做 golden check"})
    require("golden check" in answer_text(skills) and "掌握" in answer_text(skills),
            "skill lookup must list engineers from the skill matrix")
    hidden = events(client, "chat", auth["hr"], {"message": "谁会做 golden check"})
    require("只对 Ops 部门开放" in answer_text(hidden), "the skill matrix must stay Ops-only")

    first = events(client, "chat", ops, {"message": "WX-38 机台 630101 切换 tank 时报 A-1301，pump 不转，帮我提个求助单"})
    require("已尝试的步骤" in answer_text(first) and "confirm" not in first,
            "a help request with missing slots must ask for them, not file")
    cid = first["meta"]["conversation_id"]
    card = events(client, "chat", ops, {"conversation_id": cid, "message": "已经重新插紧 LS-2，还是不行"})
    summary = card.get("confirm", {}).get("summary", "")
    require(card.get("confirm", {}).get("kind") == "field_help", "the card must say it files a help request")
    for label in ("机台号：630101", "机型：WX-38", "告警码：A-1301", "已尝试的步骤：已经重新插紧 LS-2"):
        require(label in summary, f"confirmation card must show every slot: {label}")
    require("推荐专家" in summary or "派单" in summary, "the card must show the routing decision")
    filed = events(client, "chat/confirm", ops, {"confirmation_id": card["confirm"]["confirmation_id"], "approve": True})
    require("已创建求助单" in answer_text(filed), "an approved card must file the help request")
    mine = request(client, "GET", "help-requests", ops).json()["items"]
    item = next((i for i in mine if i["kind"] == "field_help" and i["machine_id"] == "630101"), None)
    require(item is not None and item["assignee_id"], "the filed request must be assigned from the skill matrix")

    # Story 6: the recommended expert resolves; the requester sees it; the case is reviewed first.
    manifest = json.loads((BACKEND.parent / "data/demo/manifest.json").read_text(encoding="utf-8"))
    expert = auth[next(u["email"].split("@")[0] for u in manifest["users"]
                       if u.get("engineer_name") == item["assignee"])]
    fix = "更换 LS-2 液位传感器并重新校准后恢复，切换 tank 不再报 A-1301"
    for body in ({"action": "start"}, {"action": "progress", "note": "远程查看 alarm log"},
                 {"action": "resolve", "resolution": fix}):
        request(client, "POST", f'help-requests/{item["id"]}/actions', expert, json=body)
    notice = request(client, "GET", f"conversations/{cid}/state", ops).json()["help_updates"]
    require([(u["status"], u["note"]) for u in notice] == [("RESOLVED", fix)],
            "returning to the conversation must show the resolution")
    question = {"message": "WX-38 机台 630101 切换 tank 时报 A-1301，pump 不转"}
    titles = lambda r: [s["document_title"] for s in r.get("sources") or []]  # noqa: E731
    require(not any(t.startswith("HELP-") for t in titles(events(client, "chat", ops, question))),
            "an unreviewed case draft must not be searchable")
    docs = request(client, "GET", "admin/documents", auth["admin"]).json()["documents"]
    case = next(d for d in docs if d["kb"] == "field-cases" and d["title"].startswith("HELP-"))
    draft = next(r for r in case["revisions"] if r["status"] == "DRAFT")
    request(client, "POST", f'admin/revisions/{draft["id"]}/publish', auth["admin"])
    require(any(t.startswith("HELP-") for t in titles(events(client, "chat", ops, question))),
            "the published case must answer the same question")

    # Story 7: the fix is recalled in a new conversation.
    recall = events(client, "chat", ops, {"message": "上次 630101 切换 tank 时报 A-1301、pump 不转的求助单后来怎么解决的"})
    require("根据你以往的记录" in answer_text(recall) and fix in answer_text(recall),
            "a resolved help request must be recalled with its fix: " + answer_text(recall)[:400])

    asked = events(client, "chat", ops, {"message": "spindle 更换扭矩多少"})
    require("哪个机型" in answer_text(asked) and not asked.get("sources"),
            "a model-specific procedure without a model must ask first, with no sources")
    told = events(client, "chat", ops, {"conversation_id": asked["meta"]["conversation_id"], "message": "WX-38"})
    require(told.get("sources") and all("WX-P" not in s["section_path"] for s in told["sources"]),
            "after the model is given, only that model's sections may be cited")

    # The earlier E17 answer cited v1, which check_publication has since retired: re-validation
    # must drop it. A new answer citing the published v2 is then recalled.
    stale = events(client, "chat", ops, {"message": "上次 E17 设备报代码 42 先查什么来着"})
    require("根据你以往的记录" not in answer_text(stale), "a memory citing a retired revision must not be reused")
    require("求助单还需要" not in answer_text(stale), "a recall question must never open a help request draft")
    events(client, "chat", ops, {"message": "E17 设备报代码 42 先查什么"})
    recalled = events(client, "chat", ops, {"message": "上次 E17 设备报代码 42 先查什么来着"})
    require("根据你以往的记录" in answer_text(recalled) and "E17" in answer_text(recalled),
            "an earlier cited answer must be recalled in a new conversation")
    require("答：" in answer_text(recalled), "a recalled answer memory must keep its answer, not just the question")

    # Tried steps in any wording fill the slot (no keyword needed); decline the card afterwards.
    loose = events(client, "chat", ops, {"message": "机台 610102 门锁报警，帮我提单"})
    other = events(client, "chat", ops, {"conversation_id": loose["meta"]["conversation_id"], "message": "LS-2 插紧了"})
    require("已尝试的步骤：LS-2 插紧了" in other.get("confirm", {}).get("summary", ""),
            "an answer to the tried-steps question must fill that slot")
    events(client, "chat/confirm", ops, {"confirmation_id": other["confirm"]["confirmation_id"], "approve": False})

    status = events(client, "chat", ops, {"message": "我的求助单进度"})
    require("（现场求助）" in answer_text(status) and "没有可靠依据" not in answer_text(status),
            "a status question must list the user's requests")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    # --mcp: every tool call goes API -> MCP server over HTTP with a per-run identity token,
    # and the MCP server is restarted together with the API between phases.
    use_mcp = "--mcp" in sys.argv[1:]
    admin_url = make_url(os.environ.get("KNV_TEST_ADMIN_URL", DEFAULT_ADMIN_URL))
    require(admin_url.get_backend_name() == "postgresql", "demo acceptance requires PostgreSQL")
    database = "knoventra_demo_check_" + uuid.uuid4().hex
    env = offline_environment(admin_url.set(database=database).render_as_string(hide_password=False))
    mcp_calls: list[int] = []
    mcp_port = free_port()
    if use_mcp:
        env.update(KNV_MCP_URL=f"http://127.0.0.1:{mcp_port}/mcp",
                   KNV_TOOL_TOKEN_SECRET="acceptance-only-tool-secret-" + uuid.uuid4().hex,
                   # Retrieval runs in the MCP process: give it the demo's hash-encoder gate,
                   # which mock_server.py sets in code for its own process.
                   KNV_GATE_THRESHOLD="0.1", KNV_GATE_SPARSE_THRESHOLD="1e9")

    @contextmanager
    def stack():
        if not use_mcp:
            with server(env, directory) as client:
                yield client
            return
        with mcp_server(env, directory, mcp_port, mcp_calls), server(env, directory) as client:
            yield client
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
            journals = BACKEND.parent / "data/demo/journals"
            journal_import = ["-m", "knoventra.ingestion.journal", *map(str, sorted(journals.glob("*.xlsx"))),
                              "--registry", str(journals / "machines.json"), "--publish-kb", "field-cases"]
            for args in (["-c", migration], ["-m", "knoventra.db.seed"], journal_import):
                subprocess.run([sys.executable, *args], cwd=directory, env=env, check=True, timeout=90)
            with stack() as client:
                auth = login_all(client)
                source = check_answer_and_boundaries(client, auth)
                check_publication(client, auth, source)
                check_v4_stories(client, auth)
                cid, confirmation_id = start_confirmation(client, auth["sg-ops"])
            # No in-memory graph/pool survives this boundary; reuse the stored JWT.
            with stack() as client:
                eid = check_resumed_confirmation(client, auth, cid, confirmation_id)
            with stack() as client:
                replay = events(client, "chat/confirm", auth["sg-ops"],
                                {"confirmation_id": confirmation_id, "approve": True})
                require(replay["meta"].get("status") == "APPROVED", "approval lost after second restart")
                require([item["id"] for item in records(client, auth["sg-ops"])] == [eid],
                        "record was duplicated or lost after second restart")
        if use_mcp:
            require(mcp_calls and all(code == 200 for code in mcp_calls),
                    f"tools must have run on the MCP server, got statuses {sorted(set(mcp_calls))}")
        print("Offline HTTP acceptance passed: six logins, evidence/isolation/refusal, publication, "
              "skill lookup, help request slots and recommendation, expert resolution with case review and publication, "
              "model clarification, memory recall and its revocation, "
              "confirmation across two server restarts, approval replay and rejection. No model calls."
              + (f" Tools ran on the MCP server ({len(mcp_calls)} authenticated calls, restarted with the API)."
                 if use_mcp else ""))
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
