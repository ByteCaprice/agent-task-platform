"""Real CLI/API/worker integration against the isolated pg_url test database."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import yaml
from conftest import write_config

ROOT = Path(__file__).resolve().parents[1]


def wait_for(fn, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except (httpx.HTTPError, KeyError):
            pass
        time.sleep(0.15)
    raise AssertionError("timed out waiting for condition")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_real_api_worker_callbacks(pg_url, tmp_path):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, body, self.headers.get("X-Agent-Run-Signature")))
            payload = {"ackStatus": "RETRY" if self.path == "/fail" else "RECEIVED", "reason": "test"}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    callback_base = f"http://127.0.0.1:{receiver.server_port}/"
    config = write_config(tmp_path)
    marker = tmp_path / "restart-marker"
    stage_marker = tmp_path / "stage-marker"
    (tmp_path / "restart_agent.py").write_text(
        "import asyncio\nfrom pathlib import Path\n"
        f"marker = Path({str(marker)!r})\nstage_marker = Path({str(stage_marker)!r})\n"
        "class Agent:\n"
        " async def run(self, context, input_data):\n"
        "  async def stage(stage_context):\n"
        "   stage_marker.write_text(stage_marker.read_text() + 'x' if stage_marker.exists() else 'x')\n"
        "   return {'value': 84}\n"
        "  result = await context.run_stage('durable', input_data, stage)\n"
        "  if not marker.exists():\n"
        "   marker.write_text('started')\n"
        "   await asyncio.sleep(60)\n"
        "  return result\n"
        "def create_agent(): return Agent()\n"
    )
    agents_path = config / "agents.yaml"
    agents = yaml.safe_load(agents_path.read_text())
    agents["agents"].append(
        {
            "name": "restart-agent",
            "version": "1.0.0",
            "route_tags": ["test.restart"],
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "tools": [],
            "permissions": [],
            "retry_policy": {"max_attempts": 1, "backoff_seconds": 0, "backoff_type": "fixed"},
            "runtime": {"type": "python", "target": "restart_agent:create_agent"},
        }
    )
    agents["agents"].append(
        {
            "name": "failing-agent",
            "version": "1.0.0",
            "route_tags": ["test.fail"],
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "tools": [],
            "permissions": [],
            "retry_policy": {"max_attempts": 1, "backoff_seconds": 0, "backoff_type": "fixed"},
            "runtime": {"type": "fail"},
        }
    )
    agents_path.write_text(yaml.safe_dump(agents))
    settings_path = config / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text())
    # Trust-auth test servers can still accept a non-empty synthetic password.
    settings["database"]["password"] = settings["database"].get("password") or "local-test-password"
    settings["callback"].update(signing_secret="local-test-secret", url_allowlist=[callback_base])
    settings["worker"].update(poll_interval_seconds=0.1, lease_seconds=2)
    settings_path.write_text(yaml.safe_dump(settings))
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(ROOT)))
    env.pop("AGENT_TASK_PLATFORM_CONFIG_DIR", None)
    cli = [sys.executable, "-m", "interfaces.cli.main"]
    procs = []
    logs = []
    try:
        migration = subprocess.run(
            cli + ["migrate", "--config-dir", str(config)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert migration.returncode == 0, migration.stdout + migration.stderr
        port = free_port()
        base = f"http://127.0.0.1:{port}"

        def start(args, name):
            log = (tmp_path / name).open("w+")
            logs.append(log)
            proc = subprocess.Popen(cli + args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            procs.append(proc)
            return proc

        start(
            ["serve", "--api-only", "--config-dir", str(config), "--host", "127.0.0.1", "--port", str(port)], "api.log"
        )
        with httpx.Client(base_url=base, timeout=3, trust_env=False) as client:
            wait_for(lambda: client.get("/readyz").status_code == 200)
            assert client.get("/v1/runs").status_code in (401, 403)
            client.headers["X-API-Key"] = "test-key"
            assert client.get("/v1/runs").status_code == 200
            payload = {
                "route_tag": "example.tool_agent",
                "request_id": "duplicate",
                "caller": "tester",
                "input": {"expression": "6 * 14"},
            }

            def submit(_):
                with httpx.Client(base_url=base, headers={"X-API-Key": "test-key"}, timeout=10, trust_env=False) as c:
                    r = c.post("/v1/runs", json=payload)
                    return r.status_code, r.text

            with ThreadPoolExecutor(max_workers=8) as pool:
                responses = list(pool.map(submit, range(8)))
            assert all(status == 202 for status, _ in responses), responses
            ids = {json.loads(body)["run_id"] for _, body in responses}
            assert len(ids) == 1, responses
            run_id = ids.pop()
            time.sleep(1)
            assert client.get(f"/v1/runs/{run_id}").json()["status"] == "QUEUED"
            cancel_payload = {**payload, "request_id": "cancel"}
            canceled_id = client.post("/v1/runs", json=cancel_payload).json()["run_id"]
            assert client.post(f"/v1/runs/{canceled_id}/cancel").json()["status"] == "CANCELED"
            callback_ids = []
            for path in ("ok", "fail"):
                data = {**payload, "request_id": path, "callback": {"url": callback_base + path}}
                response = client.post("/v1/runs", json=data)
                assert response.status_code == 202, response.text
                callback_ids.append(response.json()["run_id"])
            worker = start(["worker", "--config-dir", str(config), "--worker-id", "e2e-worker"], "worker.log")

            def run(rid):
                return client.get(f"/v1/runs/{rid}").json()

            completed = wait_for(lambda: r if (r := run(run_id))["status"] == "SUCCEEDED" else None)
            assert completed["output"]["data"]["calculation"]["result"] == 84, completed
            assert run(canceled_id)["status"] == "CANCELED"
            wait_for(lambda: run(callback_ids[0])["status"] == "SUCCEEDED")
            failed = wait_for(lambda: r if (r := run(callback_ids[1]))["callback_status"] == "FAILED" else None)
            assert failed["status"] == "WAITING_CALLBACK", failed
            wait_for(lambda: len(received) >= 3)
            assert sum(path == "/ok" for path, _, _ in received) == 1
            assert sum(path == "/fail" for path, _, _ in received) == 2
            for _, body, signature in received:
                assert signature == hmac.new(b"local-test-secret", body, hashlib.sha256).hexdigest()

            response = client.post(
                "/v1/runs",
                json={
                    "route_tag": "test.fail",
                    "request_id": "failed-business-outcome",
                    "input": {},
                    "callback": {"url": callback_base + "ok"},
                },
            )
            assert response.status_code == 202, response.text
            failed_id = response.json()["run_id"]
            failed_run = wait_for(lambda: r if (r := run(failed_id))["callback_status"] == "DELIVERED" else None)
            assert failed_run["status"] == "FAILED", failed_run
            assert failed_run["error_type"] == "AGENT_EXECUTION_ERROR", failed_run
            assert failed_run["error_message"], failed_run
            deliveries = [(path, body, sig) for path, body, sig in received if json.loads(body)["run_id"] == failed_id]
            assert len(deliveries) == 1, deliveries
            path, body, signature = deliveries[0]
            assert path == "/ok"
            assert json.loads(body)["status"] == "failed"
            assert signature == hmac.new(b"local-test-secret", body, hashlib.sha256).hexdigest()

            response = client.post(
                "/v1/runs",
                json={
                    "route_tag": "test.restart",
                    "request_id": "restart",
                    "input": {},
                },
            )
            assert response.status_code == 202, response.text
            restart_id = response.json()["run_id"]
            wait_for(marker.exists)
            assert run(restart_id)["status"] == "RUNNING"
            worker.kill()
            worker.wait(timeout=5)
            start(["worker", "--config-dir", str(config), "--worker-id", "replacement"], "replacement.log")
            recovered = wait_for(lambda: r if (r := run(restart_id))["status"] == "SUCCEEDED" else None)
            assert recovered["output"]["value"] == 84, recovered
            assert stage_marker.read_text() == "x", "completed durable stage unexpectedly reran"
            stages = client.get(f"/v1/runs/{restart_id}/stages").json()
            assert len(stages) == 1 and stages[0]["status"] == "SUCCEEDED", stages
    finally:
        for proc in reversed(procs):
            proc.terminate()
        for proc in reversed(procs):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for log in logs:
            log.close()
        receiver.shutdown()
        receiver.server_close()
        thread.join(timeout=5)
