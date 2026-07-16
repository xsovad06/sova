"""Shared fixtures for runtime, stress, and chaos tests."""

from __future__ import annotations

import asyncio
import os

import pytest

from sova.db.session import close_db, init_db
from sova.ipc.testing import MockRuntime


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for runtime tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
def mock_runtime():
    """Install a MockRuntime as the global runtime, restore after test."""
    from sova.ipc.runtime import get_runtime, set_runtime

    original = get_runtime()
    runtime = MockRuntime()
    set_runtime(runtime)
    yield runtime
    set_runtime(original)


@pytest.fixture
def mock_runtime_hanging():
    """MockRuntime whose agents hang until stopped."""
    from sova.ipc.runtime import get_runtime, set_runtime

    original = get_runtime()
    runtime = MockRuntime(should_hang=True)
    set_runtime(runtime)
    yield runtime
    set_runtime(original)


@pytest.fixture
async def clean_agent_state(tmp_path):
    """Reset module-level agent lifecycle state between tests."""
    from sova.dashboard.services import agent_lifecycle
    from sova.dashboard.services.agent_pool import _projects, set_project_dir

    set_project_dir(tmp_path)
    agent_lifecycle._background_tasks.clear()
    yield tmp_path
    cancelled: list[asyncio.Task[object]] = []
    for pa in _projects.values():
        for agent in list(pa.agents.values()):
            for attr in ("reader_task", "stderr_task", "resource_flush_task"):
                task = getattr(agent, attr, None)
                if task and not task.done():
                    task.cancel()
                    cancelled.append(task)
        pa.agents.clear()
    if cancelled:
        await asyncio.gather(*cancelled, return_exceptions=True)
    agent_lifecycle._background_tasks.clear()


@pytest.fixture
async def ws_manager():
    """Provide a fresh WebSocket connection manager."""
    from sova.dashboard.routers.agents import _ConnectionManager

    manager = _ConnectionManager()
    yield manager
    await manager.cancel_all()
