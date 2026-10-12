import asyncio
import hashlib
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .adapters import TOOLS, execute, resolve_path
from .models import ApprovalRequest, Decision, GatewayError, Invocation, Registration
from .store import Store, now

OPERATOR_SCOPES = frozenset(
    {"tools:invoke", "repository:read", "files:read", "api:read", "api:write"}
)
ADMIN_SCOPES = frozenset({"tools:register", "approvals:decide", "audit:read"})


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: frozenset[str]


@dataclass
class Config:
    root: Path
    data_dir: Path
    credentials: dict[str, Principal]
    upstream_url: str | None = None
    timeout_seconds: float = 5.0
    concurrency: int = 4
    rate_per_minute: int = 60
    approval_ttl_seconds: int = 300
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    def validate(self):
        self.root = self.root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("configured root must be a directory")
        if not self.credentials or any(len(token) < 16 for token in self.credentials):
            raise ValueError(
                "distinct nonempty bearer tokens of at least 16 characters are required"
            )
        if len({p.name for p in self.credentials.values()}) != len(self.credentials):
            raise ValueError("credential principals must have unique names")
        if not 0 < self.timeout_seconds <= 60 or not 1 <= self.concurrency <= 32:
            raise ValueError("invalid timeout or concurrency budget")
        if not 1 <= self.rate_per_minute <= 10000 or not 1 <= self.approval_ttl_seconds <= 3600:
            raise ValueError("invalid rate or approval lifetime")
        if self.upstream_url:
            parsed = urlparse(self.upstream_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("upstream must be an operator-approved origin without credentials")


def from_environment() -> Config:
    operator = os.environ.get("GATEWAY_OPERATOR_TOKEN", "")
    admin = os.environ.get("GATEWAY_ADMIN_TOKEN", "")
    if not operator or not admin or operator == admin:
        raise ValueError("configure distinct GATEWAY_OPERATOR_TOKEN and GATEWAY_ADMIN_TOKEN")
    return Config(
        root=Path(os.environ.get("GATEWAY_ROOT", "examples/repository")),
        data_dir=Path(os.environ.get("GATEWAY_DATA_DIR", ".gateway")),
        credentials={
            operator: Principal("operator", OPERATOR_SCOPES),
            admin: Principal("admin", ADMIN_SCOPES),
        },
        upstream_url=os.environ.get("GATEWAY_UPSTREAM_URL") or None,
    )


def create_app(config: Config) -> FastAPI:
    config.validate()
    store = Store(config.data_dir)
    semaphore = asyncio.Semaphore(config.concurrency)
    rates: dict[str, tuple[int, float]] = {}
    client = (
        httpx.AsyncClient(
            base_url=config.upstream_url,
            timeout=config.timeout_seconds,
            transport=config.transport,
            follow_redirects=False,
            trust_env=False,
        )
        if config.upstream_url
        else None
    )

    @asynccontextmanager
    async def lifespan(app):
        yield
        if client is not None:
            await client.aclose()

    app = FastAPI(title="AI Tool Coordination Gateway", version="0.1.0", lifespan=lifespan)
    app.state.store = store

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        request.state.correlation_id = str(uuid.uuid4())
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                return JSONResponse(
                    {
                        "error": {"code": "request_size", "message": "body exceeds 64 KiB"},
                        "correlation_id": request.state.correlation_id,
                    },
                    status_code=413,
                )
        request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, exc: GatewayError):
        store.event(
            getattr(request.state, "actor", "anonymous"),
            "request_rejected",
            {"code": exc.code, "correlation_id": request.state.correlation_id},
        )
        return JSONResponse(
            {
                "error": {"code": exc.code, "message": exc.message},
                "correlation_id": request.state.correlation_id,
            },
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def schema_error(request: Request, exc: RequestValidationError):
        return await gateway_error(
            request, GatewayError("invalid_request", "request schema is invalid", 422)
        )

    def authenticate(request: Request) -> Principal:
        supplied = request.headers.get("Authorization", "")
        if not supplied.startswith("Bearer "):
            raise GatewayError("unauthenticated", "bearer authentication required", 401)
        token = supplied[7:]
        for expected, principal in config.credentials.items():
            if secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
                request.state.actor = principal.name
                return principal
        raise GatewayError("unauthenticated", "invalid bearer credential", 401)

    def require(principal: Principal, permission: str):
        if permission not in principal.scopes:
            raise GatewayError("forbidden", "required permission is missing", 403)

    def rate_limit(principal: Principal):
        used, reset = rates.get(principal.name, (0, time.monotonic() + 60))
        if time.monotonic() >= reset:
            used, reset = 0, time.monotonic() + 60
        if used >= config.rate_per_minute:
            raise GatewayError("rate_limit", "invocation rate budget exhausted", 429)
        rates[principal.name] = used + 1, reset

    def lookup(identifier: str):
        adapter = store.registry().get(identifier, identifier if identifier in TOOLS else None)
        if adapter is None:
            raise GatewayError("unknown_tool", "tool is not registered", 404)
        return TOOLS[adapter]

    def arguments_for(identifier: str, arguments: dict[str, Any], principal: Principal):
        tool = lookup(identifier)
        require(principal, "tools:invoke")
        require(principal, tool.permission)
        try:
            arguments = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise GatewayError(
                "invalid_arguments", "tool arguments do not match schema", 422
            ) from exc
        if tool.adapter in {"repository.inspect", "file.metadata"}:
            resolve_path(config.root, arguments.path)
        return tool, arguments

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/v1/tools")
    async def discovery(principal: Principal = Depends(authenticate)):
        registry = {**{key: key for key in TOOLS}, **store.registry()}
        return {
            "tools": [
                TOOLS[adapter].metadata(identifier, config.timeout_seconds)
                for identifier, adapter in sorted(registry.items())
            ]
        }

    @app.post("/v1/tools", status_code=201)
    async def register(body: Registration, principal: Principal = Depends(authenticate)):
        require(principal, "tools:register")
        if body.id in TOOLS:
            raise GatewayError("duplicate_tool", "built-in identifier is reserved", 409)
        store.register(body.id, body.adapter, principal.name)
        return TOOLS[body.adapter].metadata(body.id, config.timeout_seconds)

    @app.post("/v1/approvals", status_code=201)
    async def request_approval(body: ApprovalRequest, principal: Principal = Depends(authenticate)):
        tool, arguments = arguments_for(body.tool_id, body.arguments, principal)
        if not tool.side_effect:
            raise GatewayError("approval_unneeded", "read tools do not need a write approval")
        approval_id = store.approval(
            principal.name, body.tool_id, arguments.model_dump(), config.approval_ttl_seconds
        )
        return {
            "approval_id": approval_id,
            "state": "PENDING",
            "expires_in_seconds": config.approval_ttl_seconds,
        }

    @app.post("/v1/approvals/{approval_id}/decision")
    async def decide(
        approval_id: str, body: Decision, principal: Principal = Depends(authenticate)
    ):
        require(principal, "approvals:decide")
        store.decide(approval_id, principal.name, body.approve)
        return {"approval_id": approval_id, "state": "APPROVED" if body.approve else "REJECTED"}

    @app.get("/v1/approvals/{approval_id}")
    async def inspect_approval(approval_id: str, principal: Principal = Depends(authenticate)):
        detail = store.approval_detail(approval_id)
        if detail["actor"] != principal.name:
            require(principal, "approvals:decide")
        return detail

    @app.post("/v1/tools/{identifier}/invoke")
    async def invoke(
        identifier: str,
        body: Invocation,
        request: Request,
        principal: Principal = Depends(authenticate),
    ):
        tool, arguments = arguments_for(identifier, body.arguments, principal)
        rate_limit(principal)
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=0.05)
        except TimeoutError as exc:
            raise GatewayError(
                "concurrency_limit", "gateway is at execution capacity", 429
            ) from exc
        try:
            call_id, created, cached = store.claim(
                principal.name,
                body.idempotency_key,
                identifier,
                arguments.model_dump(),
                body.approval_id,
                tool.side_effect,
            )
            if not created:
                return cached or {
                    "call_id": call_id,
                    "state": "RUNNING_OR_INTERRUPTED",
                    "verified_completion": False,
                    "replayed": False,
                }
            started, timestamp = time.monotonic(), now()
            report = {
                "call_id": call_id,
                "correlation_id": request.state.correlation_id,
                "tool_id": identifier,
                "actor": principal.name,
                "started_at": timestamp,
                "authorization": {"permitted": True, "approval_id": body.approval_id},
                "state": "FAILED",
                "execution_success": False,
                "verified_completion": False,
                "result": None,
                "verification": None,
            }
            upstream_key = hashlib.sha256(
                f"{principal.name}:{body.idempotency_key}".encode()
            ).hexdigest()
            try:
                output, evidence = await asyncio.wait_for(
                    execute(tool, arguments, config.root, client, upstream_key),
                    timeout=config.timeout_seconds,
                )
                report.update(
                    execution_success=True,
                    result=output,
                    verification=evidence,
                    verified_completion=evidence["verified"],
                    state="SUCCEEDED" if evidence["verified"] else "UNVERIFIED",
                )
            except (TimeoutError, httpx.TimeoutException):
                report["error"] = {"code": "timeout", "message": "tool execution deadline exceeded"}
                if tool.side_effect:
                    report["state"] = "UNKNOWN_OUTCOME"
            except GatewayError as exc:
                report["error"] = {"code": exc.code, "message": exc.message}
                if tool.side_effect and exc.code != "upstream_disabled":
                    report["state"] = "UNKNOWN_OUTCOME"
            except (ValidationError, ValueError, TypeError, AttributeError):
                report["error"] = {
                    "code": "invalid_tool_response",
                    "message": "tool output schema invalid",
                }
                if tool.side_effect:
                    report["state"] = "UNKNOWN_OUTCOME"
            except httpx.RequestError:
                report["error"] = {
                    "code": "upstream_transport",
                    "message": "upstream transport failed",
                }
                if tool.side_effect:
                    report["state"] = "UNKNOWN_OUTCOME"
            except Exception:
                report["error"] = {"code": "tool_failure", "message": "tool execution failed"}
                if tool.side_effect:
                    report["state"] = "UNKNOWN_OUTCOME"
            report["finished_at"] = now()
            report["metrics"] = {
                "duration_seconds": time.monotonic() - started,
                "attempts": 1,
                "provider_cost_usd": 0,
                "model_tokens": 0,
            }
            store.finish(call_id, principal.name, report)
            return report
        finally:
            semaphore.release()

    @app.get("/v1/audit")
    async def audit(principal: Principal = Depends(authenticate)):
        return {"events": store.audit(principal.name, "audit:read" in principal.scopes)}

    return app
