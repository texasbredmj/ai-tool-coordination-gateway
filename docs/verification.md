# Project 02 verification record

Executed locally October 12, 2026, for version 0.1.0. Results are observed runs rather than
performance guarantees or production/deployment claims. See
[machine-readable evidence](evidence/verification-summary.json).

| Check | Observed result |
| --- | --- |
| Windows Python 3.14.6 full suite | 30 passed in 3.84 seconds |
| Linux Python 3.12 image full suite | 30 passed in 4.48 seconds |
| Clean Windows venv, pinned dependencies, non-editable installation | Passed |
| Installed CLI `--help` | Passed |
| Ruff lint and formatting | Passed |
| `pip check` | No broken requirements |
| Real localhost Uvicorn smoke test | Health 200, anonymous discovery 401, forbidden invocation 403 |
| Offline HTTP-boundary demo after clean installation | Verified; 4 tools, 1 approved write, 11 audit events |
| Docker image build and isolated Linux demo | Passed; verified 4 tools and exactly 1 approved write |

Final test failures: zero. One pinned Starlette TestClient deprecation warning appeared on both
platforms; HTTPX compatibility remains functional. Earlier path-rejection tests exposed that unsafe
paths were only being classified inside execution reports; path authorization now runs before
execution claims and returns explicit HTTP rejection. Windows rooted paths are also rejected.

Commands executed from this repository directory:

```powershell
python -m venv .venv-clean
.\.venv-clean\Scripts\python.exe -m pip install -r requirements.lock .
.\.venv-clean\Scripts\python.exe -m pip check
.\.venv-clean\Scripts\tool-gateway.exe --help
.\.venv-clean\Scripts\python.exe scripts/demo.py
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe -m pytest -q --junitxml=.gateway/tests-windows.xml
.\.venv\Scripts\python.exe scripts/smoke_server.py
docker compose build demo
docker compose run --rm demo
```

Linux suite execution mounted this project's sources read-only at `/verification`, used a
non-root user with networking disabled, disabled pytest cache, and stored test state in `/data`:

```text
docker compose run --rm --entrypoint python --volume "PROJECT_PATH:/verification:ro" --workdir /verification demo -m pytest -q -p no:cacheprovider --basetemp=/data/linux-tests --junitxml=/data/tests-linux.xml tests
```

Coverage includes discovery, admin registration, inherited alias policy, authentication, permissions,
strict schemas, traversal rejection, file/request/response bounds, independent completion checks,
matching/rejected/expired/single-use approvals, requester versus approver access, self-approval
rejection, malformed upstream JSON/schema, HTTP failures, redirects, rate limits, cooperative
timeouts, unknown write outcomes, receipt mismatch, persistence, interrupted records, conflicting
idempotency input, concurrent claims, and simultaneous duplicates without a second invocation.

Raw local evidence lives under `.gateway/`: Windows/Linux JUnit, clean/Linux demo summaries,
full demo reports, and smoke summary. Runtime data and credentials are excluded from Git.
The source hashes in the summary identify the tested implementation and configuration.

Known limits: single-worker concurrency/rate limits, synchronous filesystem reads, no durable
workflow recovery, no pagination/retention for audit, basic bearer identity, no hostile multi-tenant
hardening, and an operator-approved fixed upstream contract. No live external API writes, paid
cloud resources, cloud deployment, GitHub publication, or remote CI run occurred. Additional
Python/OS matrix combinations await remote CI. The next project is Project 03, durable orchestration.
