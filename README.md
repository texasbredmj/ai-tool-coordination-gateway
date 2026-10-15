# AI Tool Coordination Gateway

## Start Here

[Launch the complete employer demonstration on GitHub](https://github.com/texasbredmj/ai-engineering-portfolio#start-here).

The public portfolio contains this project's runnable employer source, sample fixtures and verified demonstration, plus one Codespaces launch for all eight projects. No separate private-repository access is required. Run `python scripts/project_demo.py 2` from the portfolio Codespace to demonstrate this project.

The instructions below describe this repository's standalone version. The integrated platform and its current verification evidence are maintained in the portfolio.


Project 02 of the AI Systems Engineering Lab: a standalone FastAPI service that discovers,
authorizes, invokes, and verifies typed tools. It does not depend on Project 01 at runtime.

## Problem and architecture

Tools need enforceable permissions, explicit side-effect approval, bounded execution, and evidence
of completion. A caller's request or an agent's assertion is insufficient authorization or proof.

```text
Bearer principal → schema + permission checks → rate/concurrency budget
→ atomic idempotency claim + approval consumption → fixed adapter → verification
→ durable report + correlated audit
```

Four adapters are included: repository inspection, file metadata/hash inspection, a configured
API status request, and support-ticket creation followed by an independent GET receipt check.
No tool accepts a caller-supplied URL, Python code, shell command, or executable adapter upload.
Admins can register aliases of server-defined adapters; aliases retain the adapter's policy.
See [architecture](docs/architecture.md) and [security](docs/threat-model.md).

## Install and test

Python 3.11+ is required. From this repository directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
```

On Linux/macOS replace `.venv\Scripts` with `.venv/bin`. `requirements.lock` records the tested
versions, including development tools; it is a version snapshot rather than a hash-verified lock.
Tests use HTTPX mock transports, never paid providers or live external APIs.

## Demonstrate verified completion

```powershell
.\.venv\Scripts\python.exe scripts/demo.py
.\.venv\Scripts\python.exe scripts/smoke_server.py
```

The offline demo discovers and executes all four adapters through the HTTP/ASGI boundary. It
blocks an unapproved write, requests approval, has a separate approver inspect and approve its
arguments, creates and retrieves the ticket, submits a duplicate, and verifies exactly one
upstream write. Each invocation creates a fresh workspace and JSON evidence under `.gateway/`.
The mock fixture credentials exist only inside that demonstration; the actual server has no
default credentials. The smoke script starts an actual localhost server with ephemeral credentials,
checks health/authentication/authorization, and stops it.

Successful reports contain `state: SUCCEEDED`, `execution_success: true`, and
`verified_completion: true`, plus tool ID, actor, correlation/call IDs, timestamps, output,
verification evidence, duration, and attempt/token/provider-cost metrics. Execution can succeed
while verification produces `UNVERIFIED`. An ambiguous write failure is `UNKNOWN_OUTCOME`.

## Run the API

Generate distinct operator and admin tokens locally, then configure the process:

```powershell
$env:GATEWAY_OPERATOR_TOKEN = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$env:GATEWAY_ADMIN_TOKEN = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$env:GATEWAY_ROOT = (Resolve-Path examples/repository).Path
$env:GATEWAY_DATA_DIR = '.gateway/server'
.\.venv\Scripts\tool-gateway.exe --host 127.0.0.1 --port 8000
```

In a client with the operator token configured, discover tools with `GET /v1/tools`, then submit:

```http
POST /v1/tools/file.metadata/invoke
Authorization: Bearer YOUR_OPERATOR_TOKEN
Content-Type: application/json

{"arguments":{"path":"pyproject.toml"},"idempotency_key":"metadata-001"}
```

Endpoints:

| Endpoint | Authorization and behavior |
| --- | --- |
| `GET /health` | Public health, no configuration or credentials returned |
| `GET /v1/tools` | Authenticated discovery with input/output schemas and policies |
| `POST /v1/tools` | Admin; register `{"id":"files.inspect","adapter":"file.metadata"}` |
| `POST /v1/tools/{id}/invoke` | Invocation + adapter permission; body as above |
| `POST /v1/approvals` | Operator with write permission; `tool_id` and matching `arguments` |
| `GET /v1/approvals/{id}` | Requester or approver; inspect exact arguments before deciding |
| `POST /v1/approvals/{id}/decision` | Separate approver; `{"approve":true}` or false |
| `GET /v1/audit` | Own events, or all events for admin |

Invoke `api.ticket` with its matching `approval_id`. Approvals expire after five minutes and are
bound to actor, tool identifier, and normalized arguments. They are consumed atomically with the
execution claim. Admin tokens cannot invoke tools by default; operator tokens cannot approve or
register tools. No client flag can override these server policies.

Optional `GATEWAY_UPSTREAM_URL` selects one operator-approved HTTP(S) origin. Its API contract
is `GET /status → {"status":"ok"}`, `POST /tickets → {"id":...,"title":...,"priority":...}`,
and `GET /tickets/{id}` returning the same ticket. The upstream must honor `Idempotency-Key`.
Omitting the origin disables API execution. Redirects and proxy environment variables are ignored.
Only configure a service you are authorized to access; live writes require a matching approval.

Input/permission errors return normalized HTTP 4xx responses. Accepted tool executions return
HTTP 200 reports even when execution fails; callers must inspect state and verification fields.
409 means conflicting idempotency input, 429 means exhausted rate or concurrency budget.

## Docker and CI

```text
docker compose build demo
docker compose run --rm demo
docker compose --profile serve up gateway
```

The demo runs non-root with no network, a read-only root filesystem, dropped capabilities,
resource limits, and durable `/data`. The optional server profile exposes only localhost port 8000
and requires your configured tokens. It permits networking for client/API communication.
GitHub Actions checks Python 3.11/3.12/3.14 on Windows/Linux, lint, formatting, tests, both local
demos, and container builds. Remote CI is unverified until the repository is published and it runs.

## Reliability, security, and limits

- Default budgets: four in-flight invocations, 60 invocations/minute/principal, five-second async
  tool deadline, 64 KiB request bodies, 1 MiB file/API responses, and 1000 repository entries.
- SQLite atomically prevents concurrent duplicate claims and stores reports/audits/approvals/aliases.
  Duplicate requests return historical evidence; they do not recheck current external state.
- There are no automatic tool retries. Ambiguous writes require operator investigation, never blind
  replay. Interrupted records remain unverified and block automatic replay after restart.
- Rate/concurrency limits are per process. This local service is designed for one worker; distributed
  rate limits, durable orchestration, recovery, and fleet control belong to later projects.
- Local filesystem operations are bounded synchronous reads; an OS/filesystem stall can exceed
  the async deadline. HTTP operations use cooperative cancellation and HTTPX timeouts.
- The configured filesystem root and upstream are trusted. Path checks reject traversal and links,
  but cannot eliminate races with hostile concurrent writers. Filesystem isolation is not provided.
- Bearer tokens are suitable for a local demonstration, not a complete identity platform. TLS,
  token rotation, secret management, audit retention, and hostile multi-tenant hardening remain.
- Audit contains tool arguments/results but excludes bearer credentials. Protect storage accordingly.
  Audit records are not tamper-proof, and broad audit queries are not paginated in this release.
- HTTPX's pinned TestClient compatibility emits a deprecation warning; automated tests still run.

## Evidence and next project

See [verification record](docs/verification.md) for actually executed checks and known limitations.
Skills demonstrated: FastAPI APIs, typed tools, least-privilege policies, approval binding, SQLite
transactions, idempotency, async HTTP, fault normalization, deterministic testing, and containers.
The next project is **Project 03 — Autonomous Workflow Orchestrator**, with interruption/resumption
and durable dependency execution. No cloud deployment or GitHub publication is part of this local delivery.
