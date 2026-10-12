import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from support import ADMIN, OPERATOR, READER, Service, approve, config, headers, invoke

from tool_gateway.app import ADMIN_SCOPES, Config, Principal, create_app
from tool_gateway.models import GatewayError


@pytest.fixture
def environment(tmp_path):
    service = Service()
    settings = config(tmp_path, service)
    with TestClient(create_app(settings)) as client:
        yield client, settings, service


def test_discovery_registration_and_alias_policy(environment):
    client, settings, _ = environment
    response = client.get("/v1/tools", headers=headers())
    assert response.status_code == 200
    tools = response.json()["tools"]
    assert len(tools) == 4
    assert all(tool["input_schema"] and tool["output_schema"] for tool in tools)
    assert (
        client.post(
            "/v1/tools", headers=headers(), json={"id": "files.inspect", "adapter": "file.metadata"}
        ).status_code
        == 403
    )
    response = client.post(
        "/v1/tools",
        headers=headers(ADMIN),
        json={"id": "files.inspect", "adapter": "file.metadata"},
    )
    assert response.status_code == 201
    assert response.json()["permissions"] == ["files:read"]
    assert invoke(client, "files.inspect", {"path": "pyproject.toml"}).json()["verified_completion"]
    assert (
        client.post(
            "/v1/tools",
            headers=headers(ADMIN),
            json={"id": "files.inspect", "adapter": "file.metadata"},
        ).status_code
        == 409
    )
    assert create_app(settings).state.store.registry()["files.inspect"] == "file.metadata"


def test_authentication_permissions_and_audit_redaction(environment):
    client, _, _ = environment
    assert client.get("/v1/tools").status_code == 401
    assert client.get("/v1/tools", headers=headers("wrong")).status_code == 401
    assert invoke(client, token=READER).status_code == 403
    assert (
        invoke(client, "file.metadata", {"path": "pyproject.toml"}, token=READER).status_code == 200
    )
    own = client.get("/v1/audit", headers=headers(READER)).json()["events"]
    assert all(event["actor"] == "reader" for event in own)
    all_events = client.get("/v1/audit", headers=headers(ADMIN)).json()["events"]
    assert len(all_events) > len(own)
    serialized = json.dumps(all_events)
    assert OPERATOR not in serialized and ADMIN not in serialized


@pytest.mark.parametrize(
    "arguments", [{"command": "shell"}, {"path": 42}, {"path": "../secret"}, {"path": "/outside"}]
)
def test_invalid_or_escaping_arguments(environment, arguments):
    client, _, _ = environment
    response = invoke(client, arguments=arguments)
    assert response.status_code in {403, 422}


def test_malformed_request_unknown_tool_body_limit(environment):
    client, _, _ = environment
    assert (
        client.post(
            "/v1/tools/file.metadata/invoke",
            headers=headers(),
            json={"arguments": {}, "idempotency_key": "key", "shell": True},
        ).status_code
        == 422
    )
    assert invoke(client, "not.registered").status_code == 404
    assert client.post("/v1/approvals", headers=headers(), content=b"x" * 65537).status_code == 413


def test_verified_file_and_repository_results(environment):
    client, settings, _ = environment
    report = invoke(client).json()
    assert report["state"] == "SUCCEEDED"
    assert report["execution_success"] and report["verified_completion"]
    report = invoke(client, "file.metadata", {"path": "pyproject.toml"}, key="file").json()
    assert report["result"]["bytes"] == (settings.root / "pyproject.toml").stat().st_size
    assert len(report["result"]["sha256"]) == 64
    assert report["metrics"]["attempts"] == 1


def test_missing_manifest_is_successful_execution_but_unverified(environment):
    client, settings, _ = environment
    (settings.root / "pyproject.toml").unlink()
    report = invoke(client).json()
    assert report["state"] == "UNVERIFIED" and report["execution_success"]
    assert not report["verified_completion"]


def test_file_byte_budget(environment):
    client, settings, _ = environment
    (settings.root / "large").write_bytes(b"x" * (1024 * 1024 + 1))
    report = invoke(client, "file.metadata", {"path": "large"}).json()
    assert report["error"]["code"] == "resource_budget"


def test_side_effect_requires_matching_approval_and_retrieval(environment):
    client, _, service = environment
    arguments = {"title": "Investigate failing build", "priority": "normal"}
    assert invoke(client, "api.ticket", arguments).status_code == 403
    assert service.posts == 0
    approval_id = approve(client, arguments)
    detail = client.get(f"/v1/approvals/{approval_id}", headers=headers(ADMIN)).json()
    assert detail["arguments"] == arguments
    assert client.get(f"/v1/approvals/{approval_id}", headers=headers(READER)).status_code == 403
    assert (
        invoke(client, "api.ticket", {"title": "changed"}, approval=approval_id).status_code == 403
    )
    first = invoke(client, "api.ticket", arguments, approval=approval_id).json()
    assert (
        first["verified_completion"]
        and first["verification"]["retrieved_ticket"] == first["result"]
    )
    assert invoke(client, "api.ticket", arguments, approval=approval_id).json() == first
    assert service.posts == 1
    assert (
        invoke(client, "api.ticket", arguments, key="other", approval=approval_id).status_code
        == 403
    )


def test_rejected_expired_and_self_approval(environment):
    client, _, _ = environment
    args = {"title": "Investigate failing build", "priority": "normal"}
    rejected = approve(client, args, decision=False)
    assert invoke(client, "api.ticket", args, approval=rejected).status_code == 403
    expired = approve(client, args)
    with client.app.state.store.connect() as db:
        db.execute("UPDATE approvals SET expires=? WHERE id=?", (time.time() - 1, expired))
    assert invoke(client, "api.ticket", args, approval=expired).status_code == 403
    store = client.app.state.store
    pending = store.approval("admin", "api.ticket", args, 300)
    with pytest.raises(GatewayError, match="separate approver"):
        store.decide(pending, "admin", True)


@pytest.mark.parametrize(
    "mode, code",
    [
        ("timeout", "timeout"),
        ("rate", "upstream_rate_limit"),
        ("malformed", "upstream_schema"),
        ("schema", "invalid_tool_response"),
        ("http_failure", "upstream_http"),
        ("redirect", "upstream_http"),
        ("large", "upstream_size"),
    ],
)
def test_normalized_upstream_failures(environment, mode, code):
    client, _, service = environment
    service.mode = mode
    report = invoke(client, "api.status").json()
    assert report["state"] == "FAILED"
    assert report["error"]["code"] == code
    assert not report["verified_completion"]


def test_write_timeout_is_unknown_and_never_replayed(environment):
    client, _, service = environment
    approval_id = approve(client)
    service.mode = "timeout"
    args = {"title": "Investigate failing build", "priority": "normal"}
    report = invoke(client, "api.ticket", args, approval=approval_id).json()
    assert report["state"] == "UNKNOWN_OUTCOME"
    service.mode = "ok"
    assert invoke(client, "api.ticket", args, approval=approval_id).json() == report
    assert service.posts == 0


def test_write_receipt_mismatch_cannot_claim_completion(environment):
    client, _, service = environment
    approval_id = approve(client)
    service.mode = "mismatch"
    report = invoke(
        client,
        "api.ticket",
        {"title": "Investigate failing build", "priority": "normal"},
        approval=approval_id,
    ).json()
    assert report["state"] == "UNVERIFIED" and report["execution_success"]
    assert not report["verified_completion"]


def test_idempotency_conflict_persistence_and_interruption(environment):
    client, settings, _ = environment
    original = invoke(client).json()
    assert invoke(client, arguments={"path": "tests"}).status_code == 409
    with TestClient(create_app(settings)) as restarted:
        assert invoke(restarted).json() == original
    store = client.app.state.store
    call_id, created, _ = store.claim(
        "operator", "interrupted", "repository.inspect", {"path": "."}, None, False
    )
    assert created
    report = invoke(client, key="interrupted").json()
    assert report["call_id"] == call_id and report["state"] == "RUNNING_OR_INTERRUPTED"


def test_rate_limit(tmp_path):
    with TestClient(create_app(config(tmp_path, rate_per_minute=1))) as client:
        assert invoke(client).status_code == 200
        assert invoke(client, key="second").status_code == 429


def test_concurrency_limit_and_duplicate_claim(tmp_path):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return httpx.Response(200, json={"status": "ok"})

        settings = config(
            tmp_path,
            upstream_url="https://mock.invalid",
            concurrency=1,
            transport=httpx.MockTransport(handler),
        )
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                body = {"arguments": {}, "idempotency_key": "concurrent"}
                first = asyncio.create_task(
                    client.post("/v1/tools/api.status/invoke", headers=headers(), json=body)
                )
                await asyncio.wait_for(started.wait(), 2)
                overloaded = await client.post(
                    "/v1/tools/api.status/invoke",
                    headers=headers(),
                    json={**body, "idempotency_key": "other"},
                )
                assert overloaded.status_code == 429
                release.set()
                report = (await first).json()
                assert report["verified_completion"]
                assert (
                    await client.post("/v1/tools/api.status/invoke", headers=headers(), json=body)
                ).json() == report
                assert calls == 1

    asyncio.run(scenario())


def test_configuration_rejects_unsafe_origin_and_credentials(tmp_path):
    with pytest.raises(ValueError):
        create_app(config(tmp_path, upstream_url="https://user:password@example.com"))
    with pytest.raises(ValueError):
        create_app(Config(tmp_path, tmp_path / "data", {"short": Principal("admin", ADMIN_SCOPES)}))
