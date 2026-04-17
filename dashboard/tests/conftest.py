"""Shared fixtures for dashboard tests."""

import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

FIXTURES_DIR = Path(__file__).parent / "fixtures" / ".claude"


@pytest.fixture(scope="session", autouse=True)
def _set_agent_data_dir():
    """Point agent data dir to test fixtures before any app imports."""
    os.environ["AGENT_DATA_DIR"] = str(FIXTURES_DIR)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
