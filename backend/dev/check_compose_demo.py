"""Build and exercise a fresh demo stack, then remove only this run's resources.

From backend/: uv run --no-sync python dev/check_compose_demo.py
Requires Docker Compose v2 and build-time package/registry access. No LLM calls.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import time
import uuid

import httpx

from check_demo import (
    check_answer_and_boundaries, check_publication, check_resumed_confirmation,
    events, login_all, records, require, start_confirmation,
)


ROOT = Path(__file__).resolve().parents[2]


def main():
    project = "knoventra-demo-check-" + uuid.uuid4().hex[:12]
    env = {**os.environ, "KNV_DEMO_PORT": "0"}
    command = ["docker", "compose", "-p", project, "-f", str(ROOT / "deploy/compose.demo.yaml")]
    print("Isolated container acceptance project: " + project, flush=True)

    def compose(*args, capture=False, timeout=180):
        return subprocess.run([*command, *args], cwd=ROOT, env=env, check=True,
                              text=True, capture_output=capture, timeout=timeout)

    try:
        compose("up", "--build", "--detach", "--wait", "--wait-timeout", "120", timeout=900)
        binding = compose("port", "web", "80", capture=True).stdout.strip()
        require(binding.startswith("127.0.0.1:"), "demo must be published on loopback only")
        origin = "http://" + binding
        with httpx.Client(base_url=origin + "/api/v1/", timeout=30, trust_env=False) as client:
            page = client.get(origin + "/")
            require(page.status_code == 200 and '<div id="root">' in page.text, "frontend HTML was not served")
            assets = re.findall(r'(?:src|href)="(/assets/[^"?]+)"', page.text)
            require(any(path.endswith(".js") for path in assets) and any(path.endswith(".css") for path in assets),
                    "frontend HTML must reference built JS and CSS")
            for path in assets:
                asset = client.get(origin + path)
                require(asset.status_code == 200 and asset.content and "text/html" not in asset.headers.get("content-type", ""),
                        "frontend asset is missing: " + path)
            auth = login_all(client)
            source = check_answer_and_boundaries(client, auth)
            check_publication(client, auth, source)
            cid, confirmation_id = start_confirmation(client, auth["sg-ops"])
            compose("restart", "api")
            compose("up", "--detach", "--no-build", "--wait", "--wait-timeout", "120")
            eid = check_resumed_confirmation(client, auth, cid, confirmation_id)
            # Recreate rather than restart: nginx must resolve the replacement API.
            compose("up", "--detach", "--no-build", "--force-recreate", "--no-deps", "--wait",
                    "--wait-timeout", "120", "api")
            # Docker DNS may cache the former address for nginx's configured 5s.
            deadline = time.monotonic() + 15
            while True:
                try:
                    if client.get("me", headers=auth["sg-ops"]).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                require(time.monotonic() < deadline, "API proxy did not recover after container recreation")
                time.sleep(0.2)
            replay = events(client, "chat/confirm", auth["sg-ops"],
                            {"confirmation_id": confirmation_id, "approve": True})
            require(replay["meta"].get("status") == "APPROVED", "approval lost after API recreation")
            require([item["id"] for item in records(client, auth["sg-ops"])] == [eid], "handoff duplicated after API recreation")
        print("Container demo acceptance passed: locked fresh builds, static assets, nginx HTTP/SSE, "
              "permissions, publication and persistent confirmation across restart/recreation.")
    except Exception:
        # Logs contain fictional demo data; do not dump inherited environment.
        subprocess.run([*command, "logs", "--no-color", "--tail", "80"], cwd=ROOT, env=env, timeout=30)
        raise
    finally:
        compose("down", "--volumes", "--remove-orphans", timeout=90)
        print("Removed this run's demo containers, network and volume: " + project)


if __name__ == "__main__":
    main()
