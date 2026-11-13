"""The Settings page's grouped payload and its Reset to default route (#1463).

Runs the real router and settings service against a file database for the startup project (single-project mode,
the server started from another directory), as tests/test_settings_single_project_roundtrip.py does: reads and
resets must resolve the same project as saves.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import csrf_request_headers as _csrf_request_headers


@pytest.fixture
async def startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[AsyncClient, Path]]:
    from sova.config.loader import invalidate_config_cache
    from sova.dashboard.app import create_app
    from sova.dashboard.services import agent_pool, settings_service
    from sova.db.session import close_db, init_db_for_project

    project = tmp_path / "startup-project"
    elsewhere = tmp_path / "elsewhere"
    (project / ".claude").mkdir(parents=True)
    elsewhere.mkdir()
    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    monkeypatch.chdir(elsewhere)
    # create_app() installs the pool's default project dir: monkeypatch puts back whatever it was, including unset,
    # so a later test never meets a directory that tmp_path has deleted.
    monkeypatch.setattr(agent_pool, "_default_project_dir", agent_pool.get_default_project_dir())
    settings_service._default_values.cache_clear()
    await init_db_for_project(project)
    app = create_app(project_dir=project)
    invalidate_config_cache()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, project
    finally:
        invalidate_config_cache()
        settings_service._default_values.cache_clear()
        await close_db()


def _setting(payload: dict, key: str) -> dict:
    return next(s for g in payload["groups"] for s in g["settings"] if s["key"] == key)


async def _save(client: AsyncClient, key: str, value: object) -> None:
    res = await client.post(
        "/api/settings/config", json={"key": key, "value": value}, headers=_csrf_request_headers(client)
    )
    assert res.status_code == 200, res.text
    assert "error" not in res.json(), res.text


class TestGroupedPayload:
    async def test_carries_default_stored_and_environment_flags(
        self, startup: tuple[AsyncClient, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _project = startup
        await _save(client, "max_parallel_agents", 5)
        monkeypatch.setenv("SOVA_AGENT_MODEL", "env-model")

        payload = (await client.get("/api/settings/config/grouped")).json()

        limit = _setting(payload, "max_parallel_agents")
        assert (limit["value"], limit["stored"], limit["env_var"]) == (5, True, "")
        assert limit["default"] != 5
        model = _setting(payload, "agent.model")
        assert (model["value"], model["env_var"], model["stored"]) == ("env-model", "SOVA_AGENT_MODEL", False)
        assert payload["leftover"] == []
        assert "keyring_available" in payload

    async def test_every_group_returned_has_a_section(self, startup: tuple[AsyncClient, Path]) -> None:
        """Every group the payload returns sits in one of the page's sections, and each group in exactly one.

        Key by key, that every setting can be found is checked under node on this same payload, in
        tests/test_settings_page_js.py (test_every_setting_of_the_real_payload_is_reachable_by_search).
        """
        from sova.dashboard.settings_meta import GROUP_ORDER, SECTIONS

        assert sorted(g for groups in SECTIONS.values() for g in groups) == sorted(GROUP_ORDER)
        client, _project = startup
        payload = (await client.get("/api/settings/config/grouped")).json()
        placed = {gid for groups in SECTIONS.values() for gid in groups}

        assert payload["groups"]
        assert [g["id"] for g in payload["groups"] if g["id"] not in placed] == []


class TestResetRoute:
    async def test_needs_the_csrf_pair(self, startup: tuple[AsyncClient, Path]) -> None:
        client, _project = startup
        res = await client.post("/api/settings/config/reset", json={"key": "max_parallel_agents"})
        assert res.status_code == 403

    async def test_deletes_the_stored_value_and_answers_with_the_default(
        self, startup: tuple[AsyncClient, Path]
    ) -> None:
        client, _project = startup
        await _save(client, "max_parallel_agents", 5)
        default = _setting((await client.get("/api/settings/config/grouped")).json(), "max_parallel_agents")["default"]

        res = await client.post(
            "/api/settings/config/reset", json={"key": "max_parallel_agents"}, headers=_csrf_request_headers(client)
        )

        assert res.status_code == 200, res.text
        assert res.json()["value"] == default
        after = _setting((await client.get("/api/settings/config/grouped")).json(), "max_parallel_agents")
        assert (after["value"], after["stored"]) == (default, False)

    async def test_a_setting_set_by_the_environment_is_refused(
        self, startup: tuple[AsyncClient, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _project = startup
        await _save(client, "agent.model", "claude-sonnet-5-5")
        monkeypatch.setenv("SOVA_AGENT_MODEL", "env-model")

        res = await client.post(
            "/api/settings/config/reset", json={"key": "agent.model"}, headers=_csrf_request_headers(client)
        )

        assert res.status_code == 409
        assert "SOVA_AGENT_MODEL" in res.json()["detail"]
        assert _setting((await client.get("/api/settings/config/grouped")).json(), "agent.model")["stored"] is True

    async def test_an_unknown_key_is_not_found(self, startup: tuple[AsyncClient, Path]) -> None:
        client, _project = startup
        res = await client.post(
            "/api/settings/config/reset", json={"key": "no_such.setting"}, headers=_csrf_request_headers(client)
        )
        assert res.status_code == 404
        assert "Unknown setting" in res.json()["detail"]

    async def test_a_reset_applies_the_update_like_a_save(
        self, startup: tuple[AsyncClient, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reset changes the value in effect just as a save does, so running components hear about it."""
        from sova.dashboard.routers import settings as settings_router

        client, _project = startup
        await _save(client, "max_parallel_agents", 5)
        seen: list[str] = []

        async def _record(_project_dir: Path, key: str, _result: dict) -> None:
            seen.append(key)

        monkeypatch.setattr(settings_router, "_apply_config_update", _record)
        res = await client.post(
            "/api/settings/config/reset", json={"key": "max_parallel_agents"}, headers=_csrf_request_headers(client)
        )

        assert res.status_code == 200
        assert seen == ["max_parallel_agents"]


class TestResetFailureStatuses:
    async def test_a_database_failure_is_a_503_a_client_may_retry(self, startup: tuple[AsyncClient, Path]) -> None:
        from unittest.mock import patch

        from sqlalchemy.exc import SQLAlchemyError

        client, _project = startup
        await _save(client, "max_parallel_agents", 5)
        with patch("sova.config.db_loader.delete_setting", side_effect=SQLAlchemyError("down")):
            res = await client.post(
                "/api/settings/config/reset", json={"key": "max_parallel_agents"}, headers=_csrf_request_headers(client)
            )

        assert res.status_code == 503
        assert "Failed to remove the stored value" in res.json()["detail"]

    async def test_an_unreadable_database_is_a_503_not_an_unknown_key(self, startup: tuple[AsyncClient, Path]) -> None:
        from unittest.mock import patch

        client, _project = startup
        with (
            patch("sova.config.db_loader.read_stored_settings", return_value=None),
            patch("sova.config.db_loader.config_db_unreadable_reason", return_value="database is locked"),
        ):
            res = await client.post(
                "/api/settings/config/reset", json={"key": "old.removed"}, headers=_csrf_request_headers(client)
            )

        assert res.status_code == 503
        assert "database is locked" in res.json()["detail"]

    async def test_a_refused_consistency_check_stays_a_422(self, startup: tuple[AsyncClient, Path]) -> None:
        client, _project = startup
        await _save(client, "llm.model", "ollama/qwen3")
        await _save(client, "llm.provider", "ollama")
        res = await client.post(
            "/api/settings/config/reset", json={"key": "llm.model"}, headers=_csrf_request_headers(client)
        )

        assert res.status_code == 422
        assert res.json()["detail"].startswith("Not reset:")
