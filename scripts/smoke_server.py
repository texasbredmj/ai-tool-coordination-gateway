"""Start a real localhost server with ephemeral credentials, verify HTTP, and stop it."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx


def main():
    directory = Path(".gateway/smoke") / str(uuid.uuid4())
    root = directory / "repository"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="smoke"\nversion="0.1.0"\n')
    operator, admin = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    env = dict(os.environ)
    env.update(
        GATEWAY_OPERATOR_TOKEN=operator,
        GATEWAY_ADMIN_TOKEN=admin,
        GATEWAY_ROOT=str(root.resolve()),
        GATEWAY_DATA_DIR=str((directory / "data").resolve()),
        GATEWAY_UPSTREAM_URL="",
    )
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                "from tool_gateway.cli import main; main()",
                "--port",
                str(port),
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=1
            ) as client:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        health = client.get("/health")
                        health.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("localhost server failed to start") from None
                        time.sleep(0.05)
                anonymous = client.get("/v1/tools")
                discovered = client.get(
                    "/v1/tools", headers={"Authorization": f"Bearer {operator}"}
                )
                forbidden = client.post(
                    "/v1/tools/repository.inspect/invoke",
                    headers={"Authorization": f"Bearer {admin}"},
                    json={"arguments": {}, "idempotency_key": "smoke"},
                )
                passed = (
                    health.json() == {"status": "ok"}
                    and anonymous.status_code == 401
                    and discovered.status_code == 200
                    and forbidden.status_code == 403
                )
                summary = {
                    "live_server_verified": passed,
                    "health_status": health.status_code,
                    "unauthenticated_status": anonymous.status_code,
                    "forbidden_status": forbidden.status_code,
                    "discovered_tools": len(discovered.json()["tools"]),
                }
                (directory / "smoke-report.json").write_text(
                    json.dumps(summary, indent=2), encoding="utf-8"
                )
                print(json.dumps(summary, indent=2))
                return 0 if passed else 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
