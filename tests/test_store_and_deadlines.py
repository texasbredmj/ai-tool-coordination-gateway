import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi.testclient import TestClient
from support import Service, config, headers, invoke

from tool_gateway.app import create_app
from tool_gateway.store import Store


def test_atomic_concurrent_claims(tmp_path):
    store = Store(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(
            executor.map(
                lambda _: store.claim(
                    "operator", "same", "file.metadata", {"path": "file"}, None, False
                ),
                range(8),
            )
        )
    assert sum(row[1] for row in rows) == 1
    assert len({row[0] for row in rows}) == 1


def test_real_async_deadline(tmp_path):
    async def delayed(request):
        await asyncio.sleep(10)
        return httpx.Response(200, json={"status": "ok"})

    settings = config(
        tmp_path,
        upstream_url="https://mock.invalid",
        timeout_seconds=0.05,
        transport=httpx.MockTransport(delayed),
    )
    with TestClient(create_app(settings)) as client:
        report = invoke(client, "api.status").json()
        assert report["error"]["code"] == "timeout"
        assert report["metrics"]["duration_seconds"] < 1


def test_simultaneous_duplicate_is_not_executed(tmp_path):
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
            concurrency=2,
            transport=httpx.MockTransport(handler),
        )
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                body = {"arguments": {}, "idempotency_key": "same"}
                first = asyncio.create_task(
                    client.post("/v1/tools/api.status/invoke", headers=headers(), json=body)
                )
                await asyncio.wait_for(started.wait(), 2)
                duplicate = await client.post(
                    "/v1/tools/api.status/invoke", headers=headers(), json=body
                )
                assert duplicate.json()["state"] == "RUNNING_OR_INTERRUPTED"
                assert not duplicate.json()["verified_completion"]
                release.set()
                assert (await first).json()["verified_completion"]
                assert calls == 1

    asyncio.run(scenario())


def test_upstream_disabled_known_no_write(tmp_path):
    from support import approve

    with TestClient(create_app(config(tmp_path))) as client:
        approval_id = approve(client)
        report = invoke(
            client,
            "api.ticket",
            {"title": "Investigate failing build", "priority": "normal"},
            approval=approval_id,
        ).json()
        assert report["state"] == "FAILED"
        assert report["error"]["code"] == "upstream_disabled"


def test_alias_does_not_bypass_write_permissions(tmp_path):
    from support import ADMIN, READER, approve

    with TestClient(create_app(config(tmp_path, Service()))) as client:
        client.post(
            "/v1/tools", headers=headers(ADMIN), json={"id": "read.alias", "adapter": "api.ticket"}
        )
        denied = invoke(client, "read.alias", {"title": "Write"}, token=READER)
        assert denied.status_code == 403
        assert approve(client)
