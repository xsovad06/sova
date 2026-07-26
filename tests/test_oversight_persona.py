"""Tests for sova.oversight.persona -- operations persona file management."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from sova.oversight.persona import (
    DEFAULT_PERSONA_TEMPLATE,
    PERSONA_FILENAME,
    _get_persona_path,
    ensure_persona_exists,
    get_open_command,
    get_persona_info,
    load_persona,
)

# ---------------------------------------------------------------------------
# _get_persona_path
# ---------------------------------------------------------------------------


class TestGetPersonaPath:
    def test_default_path(self) -> None:
        path = _get_persona_path()
        assert path.name == PERSONA_FILENAME
        assert ".config/sova" in str(path)

    def test_override_path(self) -> None:
        path = _get_persona_path("/tmp/custom_persona.md")
        assert path == Path("/tmp/custom_persona.md")

    def test_tilde_expansion(self) -> None:
        path = _get_persona_path("~/my_persona.md")
        assert "~" not in str(path)
        assert path.name == "my_persona.md"


# ---------------------------------------------------------------------------
# ensure_persona_exists
# ---------------------------------------------------------------------------


class TestEnsurePersonaExists:
    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        persona_path = tmp_path / "sova" / "operations_persona.md"
        result = ensure_persona_exists(str(persona_path))
        assert result == persona_path
        assert persona_path.exists()
        assert persona_path.read_text(encoding="utf-8") == DEFAULT_PERSONA_TEMPLATE

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        persona_path = tmp_path / "deep" / "nested" / "persona.md"
        ensure_persona_exists(str(persona_path))
        assert persona_path.parent.exists()
        assert persona_path.exists()

    def test_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        persona_path = tmp_path / "persona.md"
        custom_content = "# My Custom Persona\n"
        persona_path.write_text(custom_content, encoding="utf-8")

        ensure_persona_exists(str(persona_path))
        assert persona_path.read_text(encoding="utf-8") == custom_content

    def test_handles_permission_error(self, tmp_path: Path) -> None:
        persona_path = tmp_path / "readonly" / "persona.md"
        with patch("sova.oversight.persona.Path.mkdir", side_effect=OSError("Permission denied")):
            result = ensure_persona_exists(str(persona_path))
        assert result == persona_path


# ---------------------------------------------------------------------------
# load_persona
# ---------------------------------------------------------------------------


class TestLoadPersona:
    def test_loads_file_content(self, tmp_path: Path) -> None:
        persona_path = tmp_path / "persona.md"
        content = "# Custom Persona\nBe careful."
        persona_path.write_text(content, encoding="utf-8")

        result = load_persona(str(persona_path))
        assert result == content

    def test_returns_default_when_missing(self, tmp_path: Path) -> None:
        persona_path = tmp_path / "nonexistent.md"
        result = load_persona(str(persona_path))
        assert result == DEFAULT_PERSONA_TEMPLATE

    def test_returns_default_when_empty(self, tmp_path: Path) -> None:
        persona_path = tmp_path / "persona.md"
        persona_path.write_text("", encoding="utf-8")

        result = load_persona(str(persona_path))
        assert result == DEFAULT_PERSONA_TEMPLATE

    def test_returns_default_when_whitespace_only(self, tmp_path: Path) -> None:
        persona_path = tmp_path / "persona.md"
        persona_path.write_text("   \n  \n  ", encoding="utf-8")

        result = load_persona(str(persona_path))
        assert result == DEFAULT_PERSONA_TEMPLATE

    def test_returns_default_on_permission_error(self, tmp_path: Path) -> None:
        persona_path = tmp_path / "persona.md"
        persona_path.write_text("content", encoding="utf-8")

        with patch.object(Path, "read_text", side_effect=OSError("Permission denied")):
            result = load_persona(str(persona_path))
        assert result == DEFAULT_PERSONA_TEMPLATE


# ---------------------------------------------------------------------------
# get_persona_info
# ---------------------------------------------------------------------------


class TestGetPersonaInfo:
    def test_returns_info_for_existing_file(self, tmp_path: Path) -> None:
        persona_path = tmp_path / "persona.md"
        content = "# My Persona\nCustom content."
        persona_path.write_text(content, encoding="utf-8")

        info = get_persona_info(str(persona_path))
        assert info["path"] == str(persona_path)
        assert info["exists"] is True
        assert info["is_default"] is False
        assert info["content"] == content

    def test_returns_info_for_missing_file(self, tmp_path: Path) -> None:
        persona_path = tmp_path / "nonexistent.md"

        info = get_persona_info(str(persona_path))
        assert info["exists"] is False
        assert info["is_default"] is True
        assert info["content"] == DEFAULT_PERSONA_TEMPLATE

    def test_default_template_detected(self, tmp_path: Path) -> None:
        persona_path = tmp_path / "persona.md"
        persona_path.write_text(DEFAULT_PERSONA_TEMPLATE, encoding="utf-8")

        info = get_persona_info(str(persona_path))
        assert info["is_default"] is True


# ---------------------------------------------------------------------------
# get_open_command
# ---------------------------------------------------------------------------


class TestGetOpenCommand:
    def test_darwin(self) -> None:
        with patch("sova.oversight.persona.platform.system", return_value="Darwin"):
            assert get_open_command() == "open"

    def test_linux(self) -> None:
        with patch("sova.oversight.persona.platform.system", return_value="Linux"):
            assert get_open_command() == "xdg-open"

    def test_windows(self) -> None:
        with patch("sova.oversight.persona.platform.system", return_value="Windows"):
            assert get_open_command() == "start"

    def test_unknown_os(self) -> None:
        with patch("sova.oversight.persona.platform.system", return_value="FreeBSD"):
            assert get_open_command() is None


# ---------------------------------------------------------------------------
# OversightAgent persona integration
# ---------------------------------------------------------------------------


class TestOversightAgentPersona:
    @pytest.mark.asyncio
    async def test_persona_loaded_each_cycle(self, tmp_path: Path) -> None:
        """Verify the agent loads persona content on each wake cycle."""
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        persona_path = tmp_path / "persona.md"
        persona_path.write_text("# Test Persona", encoding="utf-8")

        cfg = OversightConfig(wake_interval_minutes=1, persona_path=str(persona_path))
        agent = OversightAgent(config=cfg)

        recorded: list[dict] = []

        async def _mock_record(run_id, cycle, status, duration_ms, *, started_at=None, error=None):
            recorded.append({"persona": agent._persona})

        async def _fake_sleep(seconds):
            raise asyncio.CancelledError

        with (
            patch.object(agent, "_record_run", side_effect=_mock_record),
            patch("sova.oversight.agent.asyncio.sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert len(recorded) == 1
        assert recorded[0]["persona"] == "# Test Persona"

    @pytest.mark.asyncio
    async def test_persona_picks_up_file_changes(self, tmp_path: Path) -> None:
        """Verify persona is re-read each cycle (no caching)."""
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        persona_path = tmp_path / "persona.md"
        persona_path.write_text("# Version 1", encoding="utf-8")

        cfg = OversightConfig(wake_interval_minutes=1, persona_path=str(persona_path))
        agent = OversightAgent(config=cfg)

        personas_seen: list[str] = []
        cycle_count = 0

        async def _mock_record(run_id, cycle, status, duration_ms, *, started_at=None, error=None):
            personas_seen.append(agent._persona)

        async def _fake_sleep(seconds):
            nonlocal cycle_count
            cycle_count += 1
            if cycle_count == 1:
                persona_path.write_text("# Version 2", encoding="utf-8")
                return
            raise asyncio.CancelledError

        with (
            patch.object(agent, "_record_run", side_effect=_mock_record),
            patch("sova.oversight.agent.asyncio.sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert personas_seen[0] == "# Version 1"
        assert personas_seen[1] == "# Version 2"


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestPersonaEndpoints:
    @pytest.mark.asyncio
    async def test_get_persona_endpoint(self, tmp_path: Path) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        persona_path = tmp_path / "persona.md"
        persona_path.write_text("# Test Persona\nContent here.", encoding="utf-8")

        app = create_app(project_dir=tmp_path)

        with patch("sova.dashboard.routers.settings.get_project_dir", return_value=tmp_path):
            with patch("sova.dashboard.routers.settings.settings_service"):
                with patch(
                    "sova.config.loader.load_config",
                    return_value=type("C", (), {"oversight": type("O", (), {"persona_path": str(persona_path)})()})(),
                ):
                    with patch(
                        "sova.oversight.persona.get_persona_info",
                        return_value={
                            "path": str(persona_path),
                            "exists": True,
                            "is_default": False,
                            "content": "# Test Persona\nContent here.",
                        },
                    ):
                        transport = ASGITransport(app=app)
                        async with AsyncClient(transport=transport, base_url="http://test") as client:
                            resp = await client.get("/api/settings/persona")

                        assert resp.status_code == 200
                        data = resp.json()
                        assert data["exists"] is True
                        assert data["content"] == "# Test Persona\nContent here."

    @pytest.mark.asyncio
    async def test_open_persona_no_editor(self, tmp_path: Path) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        app = create_app(project_dir=tmp_path)

        with (
            patch("sova.dashboard.routers.settings.get_project_dir", return_value=tmp_path),
            patch(
                "sova.config.loader.load_config",
                return_value=type("C", (), {"oversight": type("O", (), {"persona_path": ""})()})(),
            ),
            patch("sova.oversight.persona.get_open_command", return_value=None),
            patch("sova.oversight.persona.ensure_persona_exists", return_value=tmp_path / "persona.md"),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/settings/persona/open")

            assert resp.status_code == 400
            assert "Edit manually" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_open_persona_success(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        app = create_app(project_dir=tmp_path)
        persona_file = tmp_path / "persona.md"

        mock_proc = AsyncMock()

        with (
            patch("sova.dashboard.routers.settings.get_project_dir", return_value=tmp_path),
            patch(
                "sova.config.loader.load_config",
                return_value=type("C", (), {"oversight": type("O", (), {"persona_path": ""})()})(),
            ),
            patch("sova.oversight.persona.get_open_command", return_value="code"),
            patch("sova.oversight.persona.ensure_persona_exists", return_value=persona_file),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/settings/persona/open")

            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["path"] == str(persona_file)
            mock_exec.assert_called_once_with("code", str(persona_file))

    @pytest.mark.asyncio
    async def test_open_persona_editor_not_found(self, tmp_path: Path) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        app = create_app(project_dir=tmp_path)
        persona_file = tmp_path / "persona.md"

        with (
            patch("sova.dashboard.routers.settings.get_project_dir", return_value=tmp_path),
            patch(
                "sova.config.loader.load_config",
                return_value=type("C", (), {"oversight": type("O", (), {"persona_path": ""})()})(),
            ),
            patch("sova.oversight.persona.get_open_command", return_value="nonexistent-editor"),
            patch("sova.oversight.persona.ensure_persona_exists", return_value=persona_file),
            patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/settings/persona/open")

            assert resp.status_code == 400
            assert "not found" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_open_persona_config_error(self, tmp_path: Path) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        app = create_app(project_dir=tmp_path)

        with (
            patch("sova.dashboard.routers.settings.get_project_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config", side_effect=RuntimeError("bad config")),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/settings/persona/open")

            assert resp.status_code == 500
            assert "Failed to load" in resp.json()["detail"]
