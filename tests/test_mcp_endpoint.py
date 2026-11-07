"""Tests for MCP endpoint: SSE transport, authentication, cross-run access denial."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.dashboard.app import create_app
from sova.dashboard.services.mcp_service import generate_mcp_token
from sova.db.models import TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for MCP endpoint tests."""
    import os

    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def session() -> AsyncSession:
    return await get_session()


@pytest.fixture
async def seed_runs(session: AsyncSession):
    """Create test task runs."""
    now = datetime.now(timezone.utc)

    run1 = TaskRun(
        issue_number="200",
        role="developer",
        status="running",
        current_step="develop",
        branch_name="feat/mcp-test",
        pr_number=60,
        total_cost_usd=Decimal("1.25"),
        project_slug="test-proj",
        started_at=now - timedelta(minutes=30),
    )
    session.add(run1)
    await session.commit()

    return [run1]


@pytest.fixture
def test_secret():
    """Fixed secret for testing."""
    return "test-mcp-secret-key-for-testing"


@pytest.fixture
async def client():
    """HTTP client for testing the MCP endpoint."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_mcp_endpoint_invalid_token(client: AsyncClient):
    """MCP endpoint rejects requests with invalid tokens."""
    # MCP uses JSON-RPC 2.0 protocol via SSE
    response = await client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "get_run_status",
                "arguments": {"run_id": 1},
            },
            "id": 1,
        },
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_mcp_endpoint_missing_token(client: AsyncClient):
    """MCP endpoint rejects requests without authentication."""
    response = await client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "get_run_status",
                "arguments": {"run_id": 1},
            },
            "id": 1,
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_mcp_endpoint_cross_run_access_denied(client: AsyncClient, seed_runs, test_secret, monkeypatch):
    """MCP endpoint rejects queries for different run_id than token allows."""
    run1 = seed_runs[0]
    token = generate_mcp_token(run1.id, test_secret)

    # Mock config to return the test secret
    def mock_get_secret(project_dir=None):
        return test_secret

    monkeypatch.setattr(
        "sova.dashboard.routers.mcp._get_mcp_secret",
        mock_get_secret,
    )

    # Try to query a different run_id
    different_run_id = run1.id + 999
    response = await client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "get_run_status",
                "arguments": {"run_id": different_run_id},
            },
            "id": 1,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    # Should get JSON-RPC error response
    data = response.json()
    assert "error" in data
    assert "access denied" in data["error"]["message"].lower()


@pytest.mark.asyncio
async def test_mcp_endpoint_happy_path(client: AsyncClient, seed_runs, test_secret, monkeypatch):
    """MCP endpoint returns tool result for a valid authenticated call."""
    run1 = seed_runs[0]
    token = generate_mcp_token(run1.id, test_secret)

    def mock_get_secret(project_dir=None):
        return test_secret

    monkeypatch.setattr(
        "sova.dashboard.routers.mcp._get_mcp_secret",
        mock_get_secret,
    )

    response = await client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "get_run_status",
                "arguments": {"run_id": run1.id},
            },
            "id": 1,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert "result" in data
    assert data["result"]["status"] == "running"
    assert data["result"]["current_step"] == "develop"
    assert data["result"]["role"] == "developer"
    assert data["id"] == 1


@pytest.mark.asyncio
async def test_mcp_endpoint_missing_run_id(client: AsyncClient, seed_runs, test_secret, monkeypatch):
    """MCP endpoint returns error when run_id is missing from arguments."""
    run1 = seed_runs[0]
    token = generate_mcp_token(run1.id, test_secret)

    def mock_get_secret(project_dir=None):
        return test_secret

    monkeypatch.setattr(
        "sova.dashboard.routers.mcp._get_mcp_secret",
        mock_get_secret,
    )

    response = await client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "get_run_status",
                "arguments": {},
            },
            "id": 1,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_mcp_endpoint_ignores_client_supplied_project_dir(
    client: AsyncClient, seed_runs, test_secret, monkeypatch
):
    """A client-supplied project_dir argument must never reach the tool function.

    project_dir scopes every DB/session call a tool makes; if the server forwarded
    it verbatim from client JSON, a token valid for one run could redirect a query
    at an arbitrary other project's database (#1204). The server must always
    resolve it itself and ignore whatever the caller sends.
    """
    run1 = seed_runs[0]
    token = generate_mcp_token(run1.id, test_secret)

    def mock_get_secret(project_dir=None):
        return test_secret

    monkeypatch.setattr(
        "sova.dashboard.routers.mcp._get_mcp_secret",
        mock_get_secret,
    )

    response = await client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "get_run_status",
                "arguments": {"run_id": run1.id, "project_dir": "/nonexistent/other-project"},
            },
            "id": 1,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert "result" in data, data
    assert data["result"]["status"] == "running"


@pytest.fixture
async def mcp_headers(seed_runs, test_secret, monkeypatch) -> dict[str, str]:
    """Authorization header for the seeded run, with the secret lookup patched."""
    monkeypatch.setattr("sova.dashboard.routers.mcp._get_mcp_secret", lambda project_dir=None: test_secret)
    return {"Authorization": f"Bearer {generate_mcp_token(seed_runs[0].id, test_secret)}"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_id"),
    [
        (None, None),
        (42, None),
        ([], None),
        ([{"method": "tools/call"}], None),
        ({"jsonrpc": "2.0", "id": 1}, 1),
        ({"jsonrpc": "2.0", "method": 5, "id": "req-9"}, "req-9"),
        ({"jsonrpc": "2.0", "method": "tools/call", "id": {"a": 1}}, None),
        ({"jsonrpc": "2.0", "method": "tools/call", "id": [1]}, None),
        ({"jsonrpc": "2.0", "method": "tools/call", "id": 1.5}, None),
        ({"jsonrpc": "2.0", "method": "tools/call", "id": True}, None),
        ({"jsonrpc": "1.0", "method": "x", "id": 1}, 1),
        ({"jsonrpc": 2, "method": "x", "id": 1}, 1),
    ],
)
async def test_mcp_endpoint_invalid_envelope_returns_32600(client: AsyncClient, mcp_headers, body, expected_id):
    """A malformed JSON-RPC envelope is answered with -32600 (HTTP 200), never a 500."""
    response = await client.post(
        "/api/mcp", content=json.dumps(body), headers={**mcp_headers, "Content-Type": "application/json"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["error"]["code"] == -32600
    assert data["id"] == expected_id


@pytest.mark.asyncio
async def test_mcp_endpoint_envelope_error_still_requires_auth(client: AsyncClient):
    """Auth runs before envelope validation: a bad token on a bad body is still a 401."""
    response = await client.post("/api/mcp", json=[], headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_mcp_endpoint_notification_without_id_does_not_500(client: AsyncClient, mcp_headers, seed_runs):
    response = await client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "get_run_status", "arguments": {"run_id": seed_runs[0].id}},
        },
        headers=mcp_headers,
    )

    assert response.status_code == 200
    assert response.json()["result"]["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("req_id", [7, "abc", None])
async def test_mcp_endpoint_accepts_supported_id_types(client: AsyncClient, mcp_headers, seed_runs, req_id):
    """A real tools/call with an int, str or null id passes envelope validation and returns a result."""
    response = await client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "get_run_status", "arguments": {"run_id": seed_runs[0].id}},
            "id": req_id,
        },
        headers=mcp_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert "error" not in data, data
    assert data["result"]["status"] == "running"
    assert data["id"] == req_id


@pytest.mark.asyncio
async def test_mcp_endpoint_unknown_method_keeps_32601(client: AsyncClient, mcp_headers):
    response = await client.post("/api/mcp", json={"jsonrpc": "2.0", "method": "nope", "id": 3}, headers=mcp_headers)

    assert response.json() == {
        "jsonrpc": "2.0",
        "error": {"code": -32601, "message": "Method not found: nope"},
        "id": 3,
    }


@pytest.mark.asyncio
async def test_mcp_endpoint_unknown_method_with_array_params_keeps_32601(client: AsyncClient, mcp_headers):
    """Positional params are valid JSON-RPC: an unknown method with them is still -32601, not -32600."""
    response = await client.post(
        "/api/mcp", json={"jsonrpc": "2.0", "method": "foo", "params": [1], "id": 4}, headers=mcp_headers
    )

    assert response.json()["error"]["code"] == -32601


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [None, {"name": 5}, {"arguments": []}, {"name": ["x"]}, {"name": "get_budget", "arguments": []}],
)
async def test_mcp_endpoint_unknown_method_keeps_32601_for_any_params(client: AsyncClient, mcp_headers, params):
    """The envelope does not police `params`: an unknown method is -32601 whatever they hold."""
    response = await client.post(
        "/api/mcp", json={"jsonrpc": "2.0", "method": "foo", "params": params, "id": 6}, headers=mcp_headers
    )

    data = response.json()
    assert data["error"]["code"] == -32601
    assert data["id"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [None, {"name": ["x"]}, {"name": "get_budget", "arguments": []}],
)
async def test_mcp_endpoint_tools_call_with_bad_params_is_32602(client: AsyncClient, mcp_headers, params):
    response = await client.post(
        "/api/mcp", json={"jsonrpc": "2.0", "method": "tools/call", "params": params, "id": 7}, headers=mcp_headers
    )

    data = response.json()
    assert data["error"]["code"] == -32602
    assert data["id"] == 7


@pytest.mark.asyncio
async def test_mcp_endpoint_tools_call_with_array_params_is_32602(client: AsyncClient, mcp_headers):
    response = await client.post(
        "/api/mcp", json={"jsonrpc": "2.0", "method": "tools/call", "params": [1], "id": 5}, headers=mcp_headers
    )

    data = response.json()
    assert data["error"]["code"] == -32602
    assert data["id"] == 5


@pytest.mark.asyncio
async def test_mcp_endpoint_client_token_run_id_never_survives(
    client: AsyncClient, mcp_headers, seed_runs, monkeypatch
):
    """A client-supplied token_run_id cannot widen access or reach the tool function."""
    from unittest.mock import AsyncMock

    run_id = seed_runs[0].id
    tool = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("sova.dashboard.routers.mcp.mcp_service.get_budget", tool)
    monkeypatch.setattr("sova.dashboard.routers.mcp._filter_tool_arguments", lambda _f, args: args)

    response = await client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "get_budget", "arguments": {"run_id": run_id, "token_run_id": run_id + 99}},
            "id": 1,
        },
        headers=mcp_headers,
    )

    assert response.json()["result"] == {"ok": True}
    assert tool.await_args.kwargs == {"run_id": run_id}


@pytest.mark.asyncio
async def test_mcp_endpoint_client_token_run_id_cannot_grant_other_run(client: AsyncClient, mcp_headers, seed_runs):
    other = seed_runs[0].id + 1
    response = await client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "get_run_status", "arguments": {"run_id": other, "token_run_id": other}},
            "id": 1,
        },
        headers=mcp_headers,
    )

    assert response.json()["error"]["code"] == -32000


async def _call_tool(client: AsyncClient, headers: dict[str, str], name: str, arguments: dict) -> dict:
    response = await client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": name, "arguments": arguments}, "id": 9},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.asyncio
async def test_mcp_endpoint_malformed_json_returns_32700(client: AsyncClient, mcp_headers):
    response = await client.post(
        "/api/mcp", content=b"{not json", headers={**mcp_headers, "Content-Type": "application/json"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["error"]["code"] == -32700
    assert data["id"] is None


@pytest.mark.asyncio
async def test_mcp_endpoint_unknown_tool_returns_32601(client: AsyncClient, mcp_headers, seed_runs):
    data = await _call_tool(client, mcp_headers, "drop_tables", {"run_id": seed_runs[0].id})

    assert data["error"] == {"code": -32601, "message": "Method not found: drop_tables"}
    assert data["id"] == 9


@pytest.mark.asyncio
async def test_mcp_endpoint_non_integer_run_id_returns_32602(client: AsyncClient, mcp_headers):
    data = await _call_tool(client, mcp_headers, "get_run_status", {"run_id": "abc"})

    assert data["error"]["code"] == -32602
    assert data["error"]["message"] == "Invalid run_id: expected integer, got str"


@pytest.mark.asyncio
async def test_mcp_endpoint_tool_value_error_returns_32000(client: AsyncClient, mcp_headers, seed_runs, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "sova.dashboard.routers.mcp.mcp_service.get_budget", AsyncMock(side_effect=ValueError("run not found"))
    )

    data = await _call_tool(client, mcp_headers, "get_budget", {"run_id": seed_runs[0].id})

    assert data["error"] == {"code": -32000, "message": "run not found"}


@pytest.mark.asyncio
async def test_mcp_endpoint_tool_crash_returns_32603_without_detail(
    client: AsyncClient, mcp_headers, seed_runs, monkeypatch
):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "sova.dashboard.routers.mcp.mcp_service.get_budget", AsyncMock(side_effect=RuntimeError("db password=x"))
    )

    data = await _call_tool(client, mcp_headers, "get_budget", {"run_id": seed_runs[0].id})

    assert data["error"] == {"code": -32603, "message": "Internal error"}
