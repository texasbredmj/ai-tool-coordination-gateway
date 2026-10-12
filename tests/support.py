import httpx

from tool_gateway.app import ADMIN_SCOPES, OPERATOR_SCOPES, Config, Principal

OPERATOR = "test-operator-token-12345"
ADMIN = "test-admin-token-1234567"
READER = "test-reader-token-123456"


class Service:
    def __init__(self):
        self.mode = "ok"
        self.tickets = {}
        self.posts = 0

    async def __call__(self, request):
        if self.mode == "timeout":
            raise httpx.ReadTimeout("injected", request=request)
        if self.mode == "rate":
            return httpx.Response(429, json={"error": "busy"})
        if self.mode == "malformed":
            return httpx.Response(200, content=b"not JSON")
        if self.mode == "http_failure":
            return httpx.Response(503)
        if self.mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://unapproved.invalid"})
        if self.mode == "large":
            return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))
        if request.url.path == "/status":
            return httpx.Response(200, json={"status": "ok" if self.mode != "schema" else "bad"})
        if request.method == "POST":
            import json

            key = request.headers["Idempotency-Key"]
            if key not in self.tickets:
                self.posts += 1
                self.tickets[key] = {"id": f"ticket-{self.posts}", **json.loads(request.content)}
            return httpx.Response(201, json=self.tickets[key])
        for ticket in self.tickets.values():
            if request.url.path.endswith("/" + ticket["id"]):
                observed = {**ticket, "title": "wrong"} if self.mode == "mismatch" else ticket
                return httpx.Response(200, json=observed)
        return httpx.Response(404)


def config(tmp_path, service=None, **overrides):
    root = tmp_path / "repository"
    root.mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text('[project]\nname="fixture"\nversion="0.1.0"\n')
    (root / "tests").mkdir(exist_ok=True)
    data = {
        "root": root,
        "data_dir": tmp_path / "data",
        "credentials": {
            OPERATOR: Principal("operator", OPERATOR_SCOPES),
            ADMIN: Principal("admin", ADMIN_SCOPES),
            READER: Principal("reader", frozenset({"tools:invoke", "files:read"})),
        },
    }
    if service:
        data.update(upstream_url="https://mock.invalid", transport=httpx.MockTransport(service))
    data.update(overrides)
    return Config(**data)


def headers(token=OPERATOR):
    return {"Authorization": f"Bearer {token}"}


def invoke(
    client,
    tool="repository.inspect",
    arguments=None,
    key="request-1",
    approval=None,
    token=OPERATOR,
):
    return client.post(
        f"/v1/tools/{tool}/invoke",
        headers=headers(token),
        json={
            "arguments": arguments if arguments is not None else {},
            "idempotency_key": key,
            "approval_id": approval,
        },
    )


def approve(client, arguments=None, decision=True):
    arguments = arguments or {"title": "Investigate failing build", "priority": "normal"}
    response = client.post(
        "/v1/approvals", headers=headers(), json={"tool_id": "api.ticket", "arguments": arguments}
    )
    assert response.status_code == 201, response.text
    approval_id = response.json()["approval_id"]
    response = client.post(
        f"/v1/approvals/{approval_id}/decision", headers=headers(ADMIN), json={"approve": decision}
    )
    assert response.status_code == 200, response.text
    return approval_id
