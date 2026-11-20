"""Settings and LLM/runtime settings API routes.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import TaskRun
from tests.conftest import csrf_request_headers as _csrf_request_headers
from tests.dashboard_helpers import NO_CSRF

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


class TestSettingsAPI:
    """Tests for the settings API endpoints."""

    async def test_get_config(self, client: AsyncClient) -> None:
        resp = await client.get("/api/settings/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "config" in data

    async def test_invariants_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/settings/invariants")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["invariants"], list)

    async def test_invariants_with_files(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        inv_dir = tmp_path / "invariants"
        inv_dir.mkdir()
        (inv_dir / "check-types.sh").write_text("#!/bin/bash\necho ok")
        (inv_dir / "check-types.sh").chmod(0o755)

        monkeypatch.setattr("sova.dashboard.routers.settings.get_project_dir", lambda: tmp_path)

        resp = await client.get("/api/settings/invariants")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["invariants"]) == 1
        assert data["invariants"][0]["name"] == "check-types.sh"
        assert data["invariants"][0]["executable"] is True

    async def test_detected_stack(self, client: AsyncClient) -> None:
        resp = await client.get("/api/settings/detected-stack")
        assert resp.status_code == 200
        assert list(resp.json()) == ["detected"]

    async def test_detected_stack_reports_the_stack_of_the_project(
        self, client: AsyncClient, tmp_path, monkeypatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        monkeypatch.setattr("sova.dashboard.routers.settings.get_project_dir", lambda: tmp_path)

        resp = await client.get("/api/settings/detected-stack")

        assert resp.json() == {"detected": "python"}

    async def test_the_old_personas_route_is_gone(self, client: AsyncClient) -> None:
        resp = await client.get("/api/settings/personas")
        assert resp.status_code == 404

    async def test_settings_page_renders_the_detected_stack_widget(self, client: AsyncClient) -> None:
        """The slot's loader in static/js/pages/settings/widgets.js reads /settings/detected-stack."""
        resp = await client.get("/settings")
        assert b'data-widget="detected-stack"' in resp.content
        assert b'data-widget="personas"' not in resp.content
        assert b"/settings/personas" not in resp.content

    async def test_settings_page_renders(self, client: AsyncClient) -> None:
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert b"Settings" in resp.content

    async def test_get_config_grouped(self, client: AsyncClient) -> None:
        resp = await client.get("/api/settings/config/grouped")
        assert resp.status_code == 200
        data = resp.json()
        assert "groups" in data
        groups = data["groups"]
        assert isinstance(groups, list)
        if groups:
            g = groups[0]
            assert "id" in g
            assert "label" in g
            assert "settings" in g
            assert isinstance(g["settings"], list)
            if g["settings"]:
                s = g["settings"][0]
                assert "key" in s
                assert "label" in s
                assert "value" in s
                assert "value_type" in s

    async def test_grouped_config_exposes_options_source(self, client: AsyncClient) -> None:
        """A model field must reach the client marked enumerable, or the settings
        page has no way to know it should render a dropdown instead of a text box.
        """
        resp = await client.get("/api/settings/config/grouped")
        assert resp.status_code == 200
        settings = {s["key"]: s for g in resp.json()["groups"] for s in g["settings"]}
        assert settings["agent.model"]["options_source"] == "llm"
        assert settings["roles.developer_model"]["options_source"] == "llm"
        # A non-model field must stay free text.
        assert settings["llm.api_base"]["options_source"] == ""

    async def test_grouped_config_has_descriptions(self, client: AsyncClient) -> None:
        resp = await client.get("/api/settings/config/grouped")
        data = resp.json()
        groups = data["groups"]
        has_description = any(s.get("description") for g in groups for s in g["settings"])
        assert has_description, "At least some settings should have descriptions"


class TestLLMSettingsAPI:
    """Tests for the LLM provider test-connection and cost-projection endpoints."""

    async def test_test_connection_config_error_not_500(self, client: AsyncClient, monkeypatch) -> None:
        def boom(*_a, **_kw):
            raise OSError("bad config")

        monkeypatch.setattr("sova.dashboard.routers.settings.load_config", boom)
        resp = await client.post("/api/settings/llm/test-connection")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Failed to load configuration"

    async def test_test_connection_provider_create_error_not_500(self, client: AsyncClient, monkeypatch) -> None:
        from sova.config.models import ProjectConfig

        monkeypatch.setattr("sova.dashboard.routers.settings.load_config", lambda *_a, **_kw: ProjectConfig())

        def boom(*_a, **_kw):
            from sova.llm.errors import ComponentMisconfiguredError

            raise ComponentMisconfiguredError("Unknown LLM provider 'x'")

        monkeypatch.setattr("sova.llm.provider.create_provider", boom)
        resp = await client.post("/api/settings/llm/test-connection")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Unknown LLM provider 'x'"

    async def test_test_connection_provider_unexpected_create_error_is_502(
        self, client: AsyncClient, monkeypatch
    ) -> None:
        from sova.config.models import ProjectConfig

        monkeypatch.setattr("sova.dashboard.routers.settings.load_config", lambda *_a, **_kw: ProjectConfig())

        def boom(*_a, **_kw):
            raise RuntimeError("secret internals")

        monkeypatch.setattr("sova.llm.provider.create_provider", boom)
        resp = await client.post("/api/settings/llm/test-connection")
        assert resp.status_code == 502
        assert resp.json()["detail"] == "LLM provider check failed"
        assert "secret internals" not in resp.text

    async def test_test_connection_success(self, client: AsyncClient, monkeypatch) -> None:
        from sova.config.models import ProjectConfig

        monkeypatch.setattr("sova.dashboard.routers.settings.load_config", lambda *_a, **_kw: ProjectConfig())

        class _FakeProvider:
            async def check_available(self):
                return True, "provider ready"

        monkeypatch.setattr("sova.llm.provider.create_provider", lambda *_a, **_kw: _FakeProvider())
        resp = await client.post("/api/settings/llm/test-connection")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["detail"] == "provider ready"

    async def test_test_connection_check_available_raises_not_500(self, client: AsyncClient, monkeypatch) -> None:
        from sova.config.models import ProjectConfig

        monkeypatch.setattr("sova.dashboard.routers.settings.load_config", lambda *_a, **_kw: ProjectConfig())

        class _FakeProvider:
            async def check_available(self):
                raise RuntimeError("network down")

        monkeypatch.setattr("sova.llm.provider.create_provider", lambda *_a, **_kw: _FakeProvider())
        resp = await client.post("/api/settings/llm/test-connection")
        assert resp.status_code == 502
        assert resp.json()["detail"] == "LLM provider unreachable"

    async def test_cost_projection_insufficient_data(self, client: AsyncClient) -> None:
        resp = await client.get("/api/settings/llm/cost-projection")
        assert resp.status_code == 200
        data = resp.json()
        assert data["insufficient_data"] is True

    async def test_cost_projection_with_data(self, client: AsyncClient, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="1",
                role="developer",
                status="done",
                total_cost_usd=Decimal("30.00"),
                started_at=now - timedelta(days=10),
            )
        )
        await session.commit()

        resp = await client.get("/api/settings/llm/cost-projection")
        assert resp.status_code == 200
        data = resp.json()
        assert data["insufficient_data"] is False
        assert Decimal(str(data["projected_monthly_usd"])) > 0

    async def test_installation_diff_source_unavailable(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("sova.dashboard.routers.settings.get_project_dir", lambda: tmp_path)
        monkeypatch.setattr("sova.commands.catalog.get_canonical_dir", lambda: tmp_path / "nonexistent")

        resp = await client.get("/api/settings/installation/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_available"] is False
        assert data["files"] == []

    async def test_installation_diff_returns_files(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        canonical_dir = tmp_path / "canonical"
        canonical_dir.mkdir()
        (canonical_dir / "new-cmd.md").write_text(
            "---\nname: new-cmd\ndescription: New.\nuser-invocable: true\ncategory: core\n---\n\nNew.\n"
        )
        project_dir = tmp_path / "project"
        (project_dir / ".claude" / "commands").mkdir(parents=True)

        monkeypatch.setattr("sova.dashboard.routers.settings.get_project_dir", lambda: project_dir)
        monkeypatch.setattr("sova.commands.catalog.get_canonical_dir", lambda: canonical_dir)

        resp = await client.get("/api/settings/installation/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_available"] is True
        entry = next(f for f in data["files"] if f["filename"] == "new-cmd.md")
        assert entry["status"] == "new"
        assert entry["category"] == "command"
        assert entry["local_content"] is None

    async def test_installation_diff_unreadable_managed_file_not_500(
        self, client: AsyncClient, tmp_path, monkeypatch
    ) -> None:
        """A single non-UTF-8 managed local file must not 500 the whole diff.

        Regression test for a CRITICAL review finding: _reverse_diff_files()
        used to read every managed local file with no error guard, so one
        corrupted file (BOM, stray byte, editor corruption) raised
        UnicodeDecodeError and broke the installation-review modal for the
        entire project. The endpoint must still return 200 with the other
        (unrelated, unaffected) files intact.
        """
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        canonical_dir = tmp_path / "canonical"
        canonical_dir.mkdir()
        (canonical_dir / "standup.md").write_text(
            "---\nname: standup\ndescription: Show standup.\nuser-invocable: true\n"
            "category: management\n---\n\nStandup.\n"
        )
        project_dir = tmp_path / "project"
        commands_dir = project_dir / ".claude" / "commands"
        commands_dir.mkdir(parents=True)

        monkeypatch.setattr("sova.dashboard.routers.settings.get_project_dir", lambda: project_dir)
        monkeypatch.setattr("sova.commands.catalog.get_canonical_dir", lambda: canonical_dir)

        install_commands(canonical_dir, commands_dir, ProjectConfig())
        (commands_dir / "standup.md").write_bytes(b"\xff\xfe not valid utf-8")

        # Added to canonical only after installation, so it's unrelated new upstream
        # content, not something the corrupted local file could affect.
        (canonical_dir / "new-cmd.md").write_text(
            "---\nname: new-cmd\ndescription: New.\nuser-invocable: true\ncategory: core\n---\n\nNew.\n"
        )

        resp = await client.get("/api/settings/installation/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_available"] is True
        filenames = [f["filename"] for f in data["files"]]
        assert "standup.md" not in filenames
        entry = next(f for f in data["files"] if f["filename"] == "new-cmd.md")
        assert entry["status"] == "new"

    async def test_installation_diff_unreadable_canonical_file_not_500(
        self, client: AsyncClient, tmp_path, monkeypatch
    ) -> None:
        """A single non-UTF-8 canonical source file must not 500 the whole diff.

        Regression test for a HIGH review finding: the CRITICAL fix above only
        hardened the managed *local* file read. The adjacent *canonical* source
        reads in _diff_files() and _reverse_diff_files() were still unguarded, so
        a single corrupted canonical file (bad checkout, non-UTF-8 edit) raised
        UnicodeDecodeError uncaught and produced the same blanket 500 on every
        request, since _diff_files() reads every canonical file unconditionally.
        The endpoint must still return 200 with the other (unrelated, unaffected)
        files intact. Commands are discovered via discover(), which parses
        frontmatter from every canonical file up front and already skips unreadable
        ones (catalog._parse_command_file), so the corrupted file surfaces here as
        "removed" (indistinguishable from a file deleted upstream) rather than
        raising or vanishing silently.
        """
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        canonical_dir = tmp_path / "canonical"
        canonical_dir.mkdir()
        (canonical_dir / "standup.md").write_text(
            "---\nname: standup\ndescription: Show standup.\nuser-invocable: true\n"
            "category: management\n---\n\nStandup.\n"
        )
        project_dir = tmp_path / "project"
        commands_dir = project_dir / ".claude" / "commands"
        commands_dir.mkdir(parents=True)

        monkeypatch.setattr("sova.dashboard.routers.settings.get_project_dir", lambda: project_dir)
        monkeypatch.setattr("sova.commands.catalog.get_canonical_dir", lambda: canonical_dir)

        install_commands(canonical_dir, commands_dir, ProjectConfig())

        # Corrupt the canonical source after install. The local copy is left
        # untouched (matches the manifest hash), so this exercises only the
        # forward diff's unguarded canonical read (_diff_files) and the reverse
        # diff's early hash-match skip, not the "local drift" DriftEntry path
        # (covered separately by test_unreadable_canonical_file_treated_as_removed
        # in tests/test_commands.py).
        (canonical_dir / "standup.md").write_bytes(b"\xff\xfe not valid utf-8")

        # Added to canonical only after corruption, so it's unrelated new upstream
        # content unaffected by the corrupted canonical file.
        (canonical_dir / "new-cmd.md").write_text(
            "---\nname: new-cmd\ndescription: New.\nuser-invocable: true\ncategory: core\n---\n\nNew.\n"
        )

        resp = await client.get("/api/settings/installation/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_available"] is True
        standup_entry = next(f for f in data["files"] if f["filename"] == "standup.md")
        assert standup_entry["status"] == "removed"
        entry = next(f for f in data["files"] if f["filename"] == "new-cmd.md")
        assert entry["status"] == "new"

    async def test_installation_diff_no_project(self, client: AsyncClient, monkeypatch) -> None:
        monkeypatch.setattr("sova.dashboard.routers.settings._settings_project_dir", lambda: None)
        resp = await client.get("/api/settings/installation/diff")
        assert resp.status_code == 400
        assert "No active project" in resp.json()["detail"]

    async def test_installation_diff_bad_config(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        from unittest.mock import patch

        monkeypatch.setattr("sova.dashboard.routers.settings.get_project_dir", lambda: tmp_path)
        with patch(
            "sova.dashboard.routers.settings.load_config",
            side_effect=FileNotFoundError("sova.toml not found"),
        ):
            resp = await client.get("/api/settings/installation/diff")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Failed to load project config"

    async def test_installation_diff_unexpected_error(
        self, client_no_raise: AsyncClient, tmp_path, monkeypatch
    ) -> None:
        from unittest.mock import patch

        monkeypatch.setattr("sova.dashboard.routers.settings.get_project_dir", lambda: tmp_path)
        with patch(
            "sova.dashboard.services.settings_service.build_installation_diff",
            side_effect=RuntimeError("boom"),
        ):
            resp = await client_no_raise.get("/api/settings/installation/diff")
        assert resp.status_code == 500
        assert resp.json() == {"detail": "Internal error"}


class TestAgentRuntimeSettingsAPI:
    """Tests for the agent runtime test-connection endpoint."""

    async def test_test_connection_config_error_not_500(self, client: AsyncClient, monkeypatch) -> None:
        def boom(*_a, **_kw):
            raise OSError("bad config")

        monkeypatch.setattr("sova.dashboard.routers.settings.load_config", boom)
        resp = await client.post("/api/settings/agent-runtime/test-connection")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Failed to load configuration"

    async def test_test_connection_runtime_create_error_not_500(self, client: AsyncClient, monkeypatch) -> None:
        from sova.config.models import ProjectConfig

        monkeypatch.setattr("sova.dashboard.routers.settings.load_config", lambda *_a, **_kw: ProjectConfig())

        def boom(*_a, **_kw):
            from sova.llm.errors import ComponentMisconfiguredError

            raise ComponentMisconfiguredError("Unknown agent runtime: 'x'")

        monkeypatch.setattr("sova.ipc.runtime.create_runtime", boom)
        resp = await client.post("/api/settings/agent-runtime/test-connection")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Unknown agent runtime: 'x'"

    async def test_test_connection_success(self, client: AsyncClient, monkeypatch) -> None:
        from sova.config.models import ProjectConfig

        monkeypatch.setattr(
            "sova.dashboard.routers.settings.load_config", lambda *_a, **_kw: ProjectConfig(agent={"runtime": "codex"})
        )

        class _FakeRuntime:
            async def check_available(self):
                return True, "codex-cli 0.20.0 (authenticated)"

        monkeypatch.setattr("sova.ipc.runtime.create_runtime", lambda *_a, **_kw: _FakeRuntime())
        resp = await client.post("/api/settings/agent-runtime/test-connection")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["runtime"] == "codex"
        assert data["detail"] == "codex-cli 0.20.0 (authenticated)"

    async def test_test_connection_check_available_raises_not_500(self, client: AsyncClient, monkeypatch) -> None:
        from sova.config.models import ProjectConfig

        monkeypatch.setattr("sova.dashboard.routers.settings.load_config", lambda *_a, **_kw: ProjectConfig())

        class _FakeRuntime:
            async def check_available(self):
                raise RuntimeError("cli not found")

        monkeypatch.setattr("sova.ipc.runtime.create_runtime", lambda *_a, **_kw: _FakeRuntime())
        resp = await client.post("/api/settings/agent-runtime/test-connection")
        assert resp.status_code == 502
        assert resp.json()["detail"] == "Agent runtime unreachable"


class TestSettingsMaxParallelSync:
    async def test_update_config_triggers_sync(self, client: AsyncClient, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        sync_called = []
        monkeypatch.setattr(
            "sova.dashboard.routers.settings.settings_service",
            MagicMock(update_config=AsyncMock(return_value={"status": "ok", "key": "max_parallel_agents"})),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_pool.sync_max_concurrent",
            lambda *a, **kw: sync_called.append(True),
        )
        resp = await client.post(
            "/api/settings/config",
            json={"key": "max_parallel_agents", "value": "5"},
            headers=_csrf_request_headers(client),
        )
        assert resp.status_code == 200
        assert len(sync_called) == 1

    async def test_update_config_no_sync_for_other_keys(self, client: AsyncClient, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        sync_called = []
        monkeypatch.setattr(
            "sova.dashboard.routers.settings.settings_service",
            MagicMock(update_config=AsyncMock(return_value={"status": "ok", "key": "github_repo"})),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_pool.sync_max_concurrent",
            lambda *a, **kw: sync_called.append(True),
        )
        resp = await client.post(
            "/api/settings/config",
            json={"key": "github_repo", "value": "org/repo"},
            headers=_csrf_request_headers(client),
        )
        assert resp.status_code == 200
        assert len(sync_called) == 0

    async def test_update_config_unreadable_database_is_a_422(self, client: AsyncClient, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        error = {"error": "Setting not saved: unsupported dialect", "unreadable_database": True}
        monkeypatch.setattr(
            "sova.dashboard.routers.settings.settings_service",
            MagicMock(update_config=AsyncMock(return_value=error)),
        )
        resp = await client.post(
            "/api/settings/config",
            json={"key": "github_repo", "value": "org/repo"},
            headers=_csrf_request_headers(client),
        )
        assert resp.status_code == 422
        assert "Setting not saved" in resp.json()["detail"]

    async def test_update_config_accepts_json_boolean(self, client: AsyncClient, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        mock_service = MagicMock(update_config=AsyncMock(return_value={"status": "ok", "key": "supervisor.enabled"}))
        monkeypatch.setattr("sova.dashboard.routers.settings.settings_service", mock_service)
        resp = await client.post(
            "/api/settings/config",
            json={"key": "supervisor.enabled", "value": True},
            headers=_csrf_request_headers(client),
        )
        assert resp.status_code == 200
        mock_service.update_config.assert_called_once()
        _, kwargs = mock_service.update_config.call_args
        assert kwargs["value"] == "true"

    async def test_update_config_without_csrf_is_rejected(self, client: AsyncClient, monkeypatch):
        """`/settings/config` is one of the two pre-existing routes the
        Connections work wired into require_same_origin_csrf; a request
        missing the double-submit pair must never reach settings_service."""
        from unittest.mock import AsyncMock, MagicMock

        mock_service = MagicMock(update_config=AsyncMock(return_value={"status": "ok"}))
        monkeypatch.setattr("sova.dashboard.routers.settings.settings_service", mock_service)
        resp = await client.post(
            "/api/settings/config", json={"key": "github_repo", "value": "org/repo"}, headers=NO_CSRF
        )
        assert resp.status_code == 403
        mock_service.update_config.assert_not_called()
