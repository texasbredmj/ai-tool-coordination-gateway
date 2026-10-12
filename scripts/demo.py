"""Offline HTTP-boundary demonstration: discover, authorize, approve, invoke, verify, audit."""

import argparse
import asyncio
import json
import uuid
from pathlib import Path

import httpx

from tool_gateway.app import ADMIN_SCOPES, OPERATOR_SCOPES, Config, Principal, create_app


async def demonstrate(directory: Path) -> dict:
    directory = directory.resolve() / str(uuid.uuid4())
    root = directory / "repository"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="demo"\nversion="0.1.0"\n')
    (root / "tests").mkdir()
    tickets, writes = {}, 0

    async def upstream(request):
        nonlocal writes
        if request.url.path == "/status":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "POST":
            key = request.headers["Idempotency-Key"]
            if key not in tickets:
                writes += 1
                tickets[key] = {"id": "ticket-1", **json.loads(request.content)}
            return httpx.Response(201, json=tickets[key])
        return httpx.Response(200, json=next(iter(tickets.values())))

    operator, admin = "demo-operator-token-only", "demo-approver-token-only"
    app = create_app(
        Config(
            root,
            directory / "data",
            {
                operator: Principal("operator", OPERATOR_SCOPES),
                admin: Principal("admin", ADMIN_SCOPES),
            },
            upstream_url="https://mock.invalid",
            transport=httpx.MockTransport(upstream),
        )
    )
    operator_headers, admin_headers = (
        {"Authorization": f"Bearer {operator}"},
        {"Authorization": f"Bearer {admin}"},
    )
    reports = []
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as client:
            tools = (await client.get("/v1/tools", headers=operator_headers)).json()["tools"]
            for tool_id, arguments in (
                ("repository.inspect", {}),
                ("file.metadata", {"path": "pyproject.toml"}),
                ("api.status", {}),
            ):
                response = await client.post(
                    f"/v1/tools/{tool_id}/invoke",
                    headers=operator_headers,
                    json={"arguments": arguments, "idempotency_key": tool_id},
                )
                response.raise_for_status()
                reports.append(response.json())
            arguments = {"title": "Investigate CI failure", "priority": "high"}
            invocation = {"arguments": arguments, "idempotency_key": "approved-ticket"}
            denied = await client.post(
                "/v1/tools/api.ticket/invoke", headers=operator_headers, json=invocation
            )
            approval = await client.post(
                "/v1/approvals",
                headers=operator_headers,
                json={"tool_id": "api.ticket", "arguments": arguments},
            )
            approval.raise_for_status()
            approval_id = approval.json()["approval_id"]
            inspected = await client.get(f"/v1/approvals/{approval_id}", headers=admin_headers)
            assert inspected.json()["arguments"] == arguments
            decision = await client.post(
                f"/v1/approvals/{approval_id}/decision",
                headers=admin_headers,
                json={"approve": True},
            )
            decision.raise_for_status()
            invocation["approval_id"] = approval_id
            created = await client.post(
                "/v1/tools/api.ticket/invoke", headers=operator_headers, json=invocation
            )
            created.raise_for_status()
            reports.append(created.json())
            duplicate = await client.post(
                "/v1/tools/api.ticket/invoke", headers=operator_headers, json=invocation
            )
            events = (await client.get("/v1/audit", headers=admin_headers)).json()["events"]
    verified = (
        denied.status_code == 403
        and writes == 1
        and duplicate.json() == created.json()
        and all(report["verified_completion"] for report in reports)
    )
    evidence = {
        "verified_demo": verified,
        "discovered_tools": len(tools),
        "blocked_unapproved_status": denied.status_code,
        "upstream_writes": writes,
        "reports": reports,
        "audit": events,
    }
    output = directory / "demo-report.json"
    output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return {
        "verified_demo": verified,
        "tools_executed": len(reports),
        "upstream_writes": writes,
        "audit_events": len(events),
        "report": str(output),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".gateway/demos"))
    args = parser.parse_args()
    summary = asyncio.run(demonstrate(args.data_dir))
    print(json.dumps(summary, indent=2))
    return 0 if summary["verified_demo"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
