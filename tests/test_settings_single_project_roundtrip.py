"""A save followed by a read in single-project mode returns the saved value (#1461).

`sova dashboard --project X` started from another directory gives a request no project context. The config
read used to fall back to the server's working directory while the save went to X's database, so a control's
read-back after a save showed another project's value. This runs the real settings service against a file
database for the startup project, with the working directory elsewhere: only the project fallback is faked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import csrf_request_headers as _csrf_request_headers


@pytest.fixture
async def startup_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    from sova.config.loader import invalidate_config_cache
    from sova.dashboard.app import create_app
    from sova.dashboard.services import agent_pool
    from sova.db.session import close_db, init_db_for_project

    startup = tmp_path / "startup-project"
    elsewhere = tmp_path / "elsewhere"
    (startup / ".claude").mkdir(parents=True)
    elsewhere.mkdir()
    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    monkeypatch.chdir(elsewhere)
    previous = agent_pool.get_default_project_dir()
    await init_db_for_project(startup)
    app = create_app(project_dir=startup)
    invalidate_config_cache()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            client.headers.update(_csrf_request_headers(client))
            yield client
    finally:
        if previous is not None:
            agent_pool.set_project_dir(previous)
        invalidate_config_cache()
        await close_db()


async def test_a_saved_value_is_read_back_from_the_startup_project(startup_client: AsyncClient) -> None:
    before = (await startup_client.get("/api/settings/config")).json()["config"]["max_parallel_agents"]
    new_value = 5 if before != 5 else 6

    saved = await startup_client.post("/api/settings/config", json={"key": "max_parallel_agents", "value": new_value})
    assert saved.status_code == 200, saved.text
    assert "error" not in saved.json(), saved.json()

    after = (await startup_client.get("/api/settings/config")).json()["config"]["max_parallel_agents"]
    assert after == new_value


def test_every_settings_route_resolves_its_project_through_one_helper() -> None:
    """Config reads and saves, secret routes and the rest of the router share _settings_project_dir(), so the
    values and the secret state a page shows never describe different projects (#1461)."""
    import inspect

    from sova.dashboard.routers import settings

    source = inspect.getsource(settings)
    helper_body = inspect.getsource(settings._settings_project_dir)
    assert source.replace(helper_body, "").count("get_project_dir()") == 0
