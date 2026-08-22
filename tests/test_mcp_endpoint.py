"""Tests for MCP endpoint -- SSE transport, authentication, cross-run access denial."""

from __future__ import annotations

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
