# Threat model

Trusted: operator-configured filesystem root, fixed upstream origin, installed adapters, and local
SQLite storage. Untrusted: HTTP request fields and tool responses. No LLM decides permissions.

Controls include strict schemas, separate principals/capabilities, constant-time credential
comparison, explicit single-use approval, no self-approval, fixed-origin API routes, no redirects
or environment proxies, bounded request/result sizes, root-relative link-rejecting path resolution,
atomic duplicate claims, and durable correlated audit records. Unauthorized calls never invoke
adapters. A tool alias cannot lower permissions or remove the approval requirement.

Residual risks: bearer token theft, unencrypted non-local HTTP if misconfigured, hostile filesystem
writers racing checks, mutable hardlinked files, synchronous filesystem stalls, audit tampering,
unbounded audit retention, and process-local rather than distributed limits. Health and OpenAPI
schemas are public; tool invocation and discovery enforce authentication. Registry aliases can
grow without a configured count quota. Approval and registry endpoints are not rate-limited.
This is a local portfolio implementation, not a hardened public multi-tenant service.

Side effects can complete upstream before a timeout or interrupted connection. Such failures are
UNKNOWN_OUTCOME and must be investigated. A missing upstream configuration is a known no-write
failure. No automatic retries, compensations, or reconciliation jobs are implemented. External
services must honor the supplied idempotency key; new keys represent new authorized operations.

Audit excludes bearer credentials but contains request arguments, output data, and local paths.
Protect the database and reports. There is no credential dump, arbitrary command execution,
dynamic tool upload, unrestricted target URL, or paid provider integration. Deployment hardening
should add TLS, managed identities, centralized limits, retention, monitoring, dependency review,
and external-service contract verification before making stronger readiness claims.
