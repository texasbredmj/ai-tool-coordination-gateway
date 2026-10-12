# Architecture

The repository is independently installable; Project 01 is neither imported nor required.
`models.py` defines strict request/tool schemas. `adapters.py` defines immutable policies and four
fixed adapters. `app.py` implements the authenticated API, resource budgets, and execution lifecycle.
`store.py` owns durable execution claims, approval decisions, reports, registry aliases, and audit.

The tool registry exposes identifiers, versions, schemas, required scopes, timeout, retry policy,
idempotency behavior, side effects, approval requirements, and budgets. Admin registration only
aliases installed adapters; dynamic executable code and caller-supplied policies are excluded.

Caller identity comes from server-configured bearer tokens. Capability checks precede execution.
Approvals have exact actor/tool/normalized-argument binding and an expiry. A separate authorized
principal decides them. Approval consumption and idempotency insertion share an immediate SQLite
transaction, preventing two callers from reusing one approval or claiming the same key concurrently.
Final reports and audit events commit together. Rejected requests record error classifications.

Execution states are RUNNING, SUCCEEDED, UNVERIFIED, FAILED, or UNKNOWN_OUTCOME. Requests with
previously claimed keys return historical reports or RUNNING_OR_INTERRUPTED. Keys are scoped by
principal and reject differing arguments/tool identifiers. Interrupted operations never auto-replay.
The fixed upstream receives an actor/key digest as its idempotency key. Upstream deduplication is
still part of that external service's contract; the gateway does not promise distributed exactly-once delivery.

Verification checks repository manifest presence, file size/stability during hashing, API status
schema, and independently retrieved ticket contents. A successful POST alone cannot establish
ticket completion. Output models reject malformed responses. HTTP timeouts, connection failures,
rate limits, redirects, oversized results, and schema failures have stable error codes.

An asyncio semaphore and fixed-window per-principal rate counter bound one process. HTTP calls
are cancellable; bounded local filesystem reads are synchronous and cannot enforce a hard deadline
against an OS stall. SQLite requires local storage with locking support. Multiple replicas require
centralized concurrency/rate budgets and durable orchestration; they are not supported here.

The deterministic demonstration uses ASGITransport for the actual HTTP handlers and MockTransport
for the upstream. The smoke script verifies a real localhost Uvicorn server with generated ephemeral
credentials. Test/demo tokens are never server defaults. No model costs or token usage are incurred.
