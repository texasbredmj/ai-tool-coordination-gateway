import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from .models import (
    EmptyInput,
    FileOutput,
    GatewayError,
    PathInput,
    RepositoryOutput,
    StatusOutput,
    TicketInput,
    TicketOutput,
)


@dataclass(frozen=True)
class Tool:
    adapter: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    permission: str
    side_effect: bool = False

    def metadata(self, identifier: str, timeout: float) -> dict[str, Any]:
        return {
            "id": identifier,
            "adapter": self.adapter,
            "version": "1.0",
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
            "permissions": [self.permission],
            "timeout_seconds": timeout,
            "retry_eligible": False,
            "idempotency": "durable no-replay claim; upstream key for POST",
            "side_effect": "external_write" if self.side_effect else "read",
            "approval_required": self.side_effect,
            "execution_budget": {"attempts": 1, "max_response_bytes": 1024 * 1024},
        }


TOOLS = {
    "repository.inspect": Tool(
        "repository.inspect",
        "Inspect authorized repository metadata",
        PathInput,
        RepositoryOutput,
        "repository:read",
    ),
    "file.metadata": Tool(
        "file.metadata", "Hash and inspect an authorized file", PathInput, FileOutput, "files:read"
    ),
    "api.status": Tool(
        "api.status",
        "Read the configured API's health status",
        EmptyInput,
        StatusOutput,
        "api:read",
    ),
    "api.ticket": Tool(
        "api.ticket",
        "Create and independently retrieve a support ticket",
        TicketInput,
        TicketOutput,
        "api:write",
        True,
    ),
}


def resolve_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or path.drive or path.root or ".." in path.parts:
        raise GatewayError(
            "unsafe_path", "only paths relative to the configured root are allowed", 403
        )
    candidate = root / path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise GatewayError("missing_path", "requested path does not exist", 404) from exc
    if not resolved.is_relative_to(root):
        raise GatewayError("unsafe_path", "path escapes configured root", 403)
    # Reject links even if they currently resolve inside the root.
    current = candidate
    while current != root:
        if current.is_symlink() or getattr(current.lstat(), "st_file_attributes", 0) & 0x400:
            raise GatewayError("unsafe_path", "linked paths are not permitted", 403)
        current = current.parent
    return resolved


async def api_json(client: httpx.AsyncClient, method: str, path: str, **kwargs):
    async with client.stream(method, path, **kwargs) as response:
        if response.status_code == 429:
            raise GatewayError("upstream_rate_limit", "upstream rate limited", 502)
        if response.status_code >= 400 or response.is_redirect:
            raise GatewayError("upstream_http", "upstream returned a failure or redirect", 502)
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > 1024 * 1024:
                raise GatewayError("upstream_size", "upstream response exceeds 1 MiB", 502)
        try:
            import json

            return json.loads(content)
        except ValueError as exc:
            raise GatewayError("upstream_schema", "upstream returned invalid JSON", 502) from exc


async def execute(
    tool: Tool,
    arguments: BaseModel,
    root: Path,
    client: httpx.AsyncClient | None,
    upstream_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if tool.adapter == "repository.inspect":
        path = resolve_path(root, arguments.path)
        if not path.is_dir():
            raise GatewayError("not_directory", "repository path must be a directory")
        entries = []
        for entry in path.iterdir():
            entries.append(entry.name)
            if len(entries) > 1000:
                raise GatewayError("resource_budget", "directory exceeds 1000 entries", 413)
        output = {
            "path": str(path.relative_to(root)),
            "pyproject_present": (path / "pyproject.toml").is_file(),
            "tests_present": (path / "tests").is_dir(),
            "entries": sorted(entries),
        }
        evidence = {"manifest_checked": (path / "pyproject.toml").is_file()}
        verified = output["pyproject_present"]
    elif tool.adapter == "file.metadata":
        path = resolve_path(root, arguments.path)
        if not path.is_file():
            raise GatewayError("not_file", "metadata path must be a file")
        before = path.stat()
        with path.open("rb") as handle:
            content = handle.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise GatewayError("resource_budget", "file exceeds 1 MiB", 413)
        after = path.stat()
        verified = before.st_mtime_ns == after.st_mtime_ns and before.st_size == len(content)
        output = {
            "path": str(path.relative_to(root)),
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        evidence = {"size_checked": after.st_size, "unchanged_during_read": verified}
    else:
        if client is None:
            raise GatewayError("upstream_disabled", "API adapters require configured upstream", 503)
        if tool.adapter == "api.status":
            output = await api_json(client, "GET", "/status")
            evidence, verified = {"expected_status": "ok"}, output.get("status") == "ok"
        else:
            output = await api_json(
                client,
                "POST",
                "/tickets",
                json=arguments.model_dump(),
                headers={"Idempotency-Key": upstream_key},
            )
            ticket = TicketOutput.model_validate(output)
            observed = await api_json(client, "GET", f"/tickets/{ticket.id}")
            verified = observed == output and all(
                observed.get(key) == value for key, value in arguments.model_dump().items()
            )
            evidence = {"retrieved_ticket": observed, "upstream_idempotency_key": upstream_key}
    validated = tool.output_model.model_validate(output).model_dump()
    return validated, {"verified": verified, **evidence}
