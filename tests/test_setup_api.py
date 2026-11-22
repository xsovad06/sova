"""Setup wizard, installation, Jira setup and milestones API routes.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from sova.dashboard.app import create_app
from sova.dashboard.services import model_profile_service
from sova.db.models import ProjectSetting
from sova.db.session import get_session
from tests.conftest import csrf_request_headers as _csrf_request_headers

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


@pytest.mark.usefixtures("setup_root_tmp")
class TestSetupAPI:
    async def test_browse_home(self, client: AsyncClient) -> None:
        resp = await client.post("/api/setup/browse", json={"path": ""})
        assert resp.status_code == 200
        data = resp.json()
        assert "current" in data
        assert "entries" in data
        assert isinstance(data["entries"], list)

    async def test_browse_specific_dir(self, client: AsyncClient, tmp_path) -> None:
        sub = tmp_path / "myproject"
        sub.mkdir()
        (sub / ".git").mkdir()
        resp = await client.post("/api/setup/browse", json={"path": str(tmp_path)})
        assert resp.status_code == 200
        data = resp.json()
        assert data["current"] == str(tmp_path)
        project_entry = next((e for e in data["entries"] if e["name"] == "myproject"), None)
        assert project_entry is not None
        assert project_entry["is_project"] is True

    async def test_scan_project(self, client: AsyncClient, tmp_path) -> None:
        # Create a minimal Python project
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        (tmp_path / ".git").mkdir()
        resp = await client.post("/api/setup/scan", json={"project_path": str(tmp_path)})
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_name"] == tmp_path.name
        assert "python" in data["tech_stack"]
        assert data["already_installed"] is False

    async def test_configure_project(self, client: AsyncClient, tmp_path) -> None:
        import sova.config.registry as reg_mod

        orig_file = reg_mod._REGISTRY_FILE
        orig_dir = reg_mod._REGISTRY_DIR
        reg_file = tmp_path / "reg" / "projects.json"
        reg_mod._REGISTRY_FILE = reg_file
        reg_mod._REGISTRY_DIR = tmp_path / "reg"

        project = tmp_path / "proj"
        project.mkdir()

        resp = await client.post(
            "/api/setup/configure",
            json={
                "project_path": str(project),
                "github_repo": "user/test",
                "base_branch": "main",
                "agent_model": "opus",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["config_source"] == "database"
        assert "slug" in data

        # Verify via async session (in-memory DB is accessible async, not sync)
        from sqlalchemy import select

        from sova.config.db_loader import _rows_to_nested
        from sova.db.models import ProjectSetting
        from sova.db.session import get_session

        async with await get_session(project_dir=project) as session:
            result = await session.execute(select(ProjectSetting).order_by(ProjectSetting.key))
            db_config = _rows_to_nested(result.scalars().all())
        assert db_config is not None
        assert db_config["github_repo"] == "user/test"

        reg_mod._REGISTRY_FILE = orig_file
        reg_mod._REGISTRY_DIR = orig_dir

    async def test_install_nonexistent_directory(self, client: AsyncClient, tmp_path: Path) -> None:
        resp = await client.post(
            "/api/setup/install",
            json={"project_path": str(tmp_path / "nonexistent_sova_test_dir_xyz")},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert "Directory not found" in data["detail"]

    async def test_configure_nonexistent_directory(self, client: AsyncClient, tmp_path: Path) -> None:
        resp = await client.post(
            "/api/setup/configure",
            json={"project_path": str(tmp_path / "nonexistent_sova_test_dir_xyz"), "github_repo": "u/r"},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert "Directory not found" in data["detail"]

    async def test_configure_invalidates_config_cache_only_after_a_write(
        self, client: AsyncClient, tmp_path, monkeypatch
    ) -> None:
        from unittest.mock import patch

        import sova.config.registry as reg_mod

        monkeypatch.setattr(reg_mod, "_REGISTRY_FILE", tmp_path / "reg" / "projects.json")
        monkeypatch.setattr(reg_mod, "_REGISTRY_DIR", tmp_path / "reg")
        project = tmp_path / "proj"
        project.mkdir()
        with patch("sova.config.loader.invalidate_config_cache") as invalidate:
            resp = await client.post(
                "/api/setup/configure", json={"project_path": str(project), "github_repo": "user/test"}
            )
        assert resp.status_code == 200
        invalidate.assert_called_once_with(project.resolve())

        monkeypatch.setenv("SOVA_DATABASE_URL", "mysql+aiomysql://user:pw@host/db")
        with patch("sova.config.loader.invalidate_config_cache") as invalidate:
            resp = await client.post(
                "/api/setup/configure", json={"project_path": str(project), "github_repo": "user/test"}
            )
        assert resp.status_code == 422
        invalidate.assert_not_called()

    async def test_configure_refuses_unreadable_database(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("SOVA_DATABASE_URL", "mysql+aiomysql://user:hunter2-secret@host/db")
        project = tmp_path / "proj"
        project.mkdir()
        resp = await client.post(
            "/api/setup/configure",
            json={"project_path": str(project), "github_repo": "user/test"},
        )
        assert resp.status_code == 422
        assert "not saved" in resp.json()["detail"]
        assert "hunter2-secret" not in resp.text

    async def test_sync_commands_success(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        from unittest.mock import patch

        from sova.commands.distribution import UpdateResult

        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)
        fake_result = UpdateResult(updated=3, skipped=1, conflicts=["foo.md"])
        with patch("sova.dashboard.routers.setup.asyncio.to_thread", return_value=fake_result):
            resp = await client.post("/api/setup/commands/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["updated"] == 3
        assert data["skipped"] == 1
        assert data["conflicts"] == ["foo.md"]

    async def test_sync_commands_logs_combined_outcome(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        """/api/setup/commands/sync logs one structured event covering both halves."""
        from sova.commands.distribution import UpdateResult
        from sova.commands.manifest import create_manifest

        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        create_manifest(rules_dir, {})

        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "sova.commands.distribution.update_commands",
            lambda *a, **kw: UpdateResult(updated=3, skipped=1, conflicts=["foo.md"]),
        )
        monkeypatch.setattr(
            "sova.commands.distribution.update_guidelines",
            lambda *a, **kw: UpdateResult(updated=2, skipped=5, conflicts=["bar.md"]),
        )

        with patch("sova.dashboard.routers.setup.log.info") as mock_info:
            resp = await client.post("/api/setup/commands/sync")
        assert resp.status_code == 200

        calls = [call for call in mock_info.call_args_list if call.args and call.args[0] == "setup.commands.sync"]
        assert len(calls) == 1
        kwargs = calls[0].kwargs
        assert kwargs["project"] == str(tmp_path)
        assert kwargs["commands_synced"] is True
        assert kwargs["commands_updated"] == 3
        assert kwargs["commands_skipped"] == 1
        assert kwargs["commands_conflicts"] == ["foo.md"]
        assert kwargs["guidelines_synced"] is True
        assert kwargs["guidelines_updated"] == 2
        assert kwargs["guidelines_skipped"] == 5
        assert kwargs["guidelines_conflicts"] == ["bar.md"]

    async def test_sync_commands_no_project(self, client: AsyncClient, monkeypatch) -> None:
        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: None)
        resp = await client.post("/api/setup/commands/sync")
        assert resp.status_code == 400
        assert "No active project" in resp.json()["detail"]

    async def test_sync_commands_bad_config(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        from unittest.mock import patch

        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)
        with patch(
            "sova.dashboard.routers.setup.load_config",
            side_effect=FileNotFoundError("sova.toml not found"),
        ):
            resp = await client.post("/api/setup/commands/sync")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Failed to load project config"

    async def test_sync_commands_filenames_restricts_and_forces(
        self, client: AsyncClient, tmp_path, monkeypatch
    ) -> None:
        from sova.commands.distribution import UpdateResult

        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)

        captured: dict = {}

        def fake_update_commands(canonical_dir, target_dir, cfg, **kwargs):
            captured.update(kwargs)
            return UpdateResult(updated=1)

        monkeypatch.setattr("sova.commands.distribution.update_commands", fake_update_commands)

        resp = await client.post("/api/setup/commands/sync", json={"filenames": ["develop.md"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["commands"]["updated"] == 1
        assert captured["filenames"] == ["develop.md"]
        assert captured["force"] is True

    async def test_sync_commands_threads_claude_code_adapter(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        """A command restricted to another runtime (e.g. codex-only) must not be
        installed into .claude/commands/ via the dashboard sync path, matching
        the CLI's `sova commands sync`/`sova install`, both of which already
        pass adapter=ClaudeCodeAdapter() to update_commands()/install_commands().
        Without threading the adapter here too, the review modal and this
        sync endpoint would disagree with the CLI about what belongs in
        .claude/commands/.
        """
        from sova.agents.claude_code import ClaudeCodeAdapter
        from sova.commands.distribution import UpdateResult

        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)

        captured: dict = {}

        def fake_update_commands(canonical_dir, target_dir, cfg, **kwargs):
            captured.update(kwargs)
            return UpdateResult(updated=1)

        monkeypatch.setattr("sova.commands.distribution.update_commands", fake_update_commands)

        resp = await client.post("/api/setup/commands/sync", json={})
        assert resp.status_code == 200
        assert isinstance(captured["adapter"], ClaudeCodeAdapter)

    async def test_sync_commands_empty_filenames_updates_nothing(
        self, client: AsyncClient, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)

        captured: dict = {}

        def fake_update_commands(canonical_dir, target_dir, cfg, **kwargs):
            captured.update(kwargs)
            from sova.commands.distribution import UpdateResult

            return UpdateResult()

        monkeypatch.setattr("sova.commands.distribution.update_commands", fake_update_commands)

        resp = await client.post("/api/setup/commands/sync", json={"filenames": []})
        assert resp.status_code == 200
        assert resp.json()["commands"]["updated"] == 0
        assert captured["filenames"] == []

    @pytest.mark.parametrize(
        ("field", "filename"),
        [
            pytest.param("filenames", "../../etc/passwd", id="path-traversal-filename"),
            pytest.param("guideline_filenames", "/etc/passwd", id="absolute-path-guideline-filename"),
        ],
    )
    async def test_sync_commands_rejects_unsafe_filename(
        self, client: AsyncClient, tmp_path, monkeypatch, field: str, filename: str
    ) -> None:
        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)

        resp = await client.post("/api/setup/commands/sync", json={field: [filename]})
        assert resp.status_code == 400
        assert f"Invalid {field}" in resp.json()["detail"]

    async def test_sync_commands_omitted_guideline_filenames_skips_guidelines(
        self, client: AsyncClient, tmp_path, monkeypatch
    ) -> None:
        """A present body that omits guideline_filenames must not sync guidelines, even if installed."""
        from sova.commands.distribution import UpdateResult
        from sova.commands.manifest import create_manifest

        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        create_manifest(rules_dir, {})

        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "sova.commands.distribution.update_commands",
            lambda *a, **kw: UpdateResult(updated=1),
        )

        def fail_if_called(*_a, **_kw):
            raise AssertionError("update_guidelines must not be called")

        monkeypatch.setattr("sova.commands.distribution.update_guidelines", fail_if_called)

        resp = await client.post("/api/setup/commands/sync", json={"filenames": ["develop.md"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["guidelines"]["updated"] == 0

    async def test_sync_commands_omitted_filenames_skips_commands(
        self, client: AsyncClient, tmp_path, monkeypatch
    ) -> None:
        """A present body that omits filenames must not sync commands, mirroring the
        guideline_filenames omission rule: a present body must name what it wants."""
        from sova.commands.distribution import UpdateResult
        from sova.commands.manifest import create_manifest

        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        create_manifest(rules_dir, {})

        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)

        def fail_if_called(*_a, **_kw):
            raise AssertionError("update_commands must not be called")

        monkeypatch.setattr("sova.commands.distribution.update_commands", fail_if_called)
        monkeypatch.setattr(
            "sova.commands.distribution.update_guidelines",
            lambda *a, **kw: UpdateResult(updated=1),
        )

        with patch("sova.dashboard.routers.setup.log.info") as mock_info:
            resp = await client.post("/api/setup/commands/sync", json={"guideline_filenames": ["security.md"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["commands"]["updated"] == 0
        assert data["guidelines"]["updated"] == 1

        calls = [call for call in mock_info.call_args_list if call.args and call.args[0] == "setup.commands.sync"]
        assert len(calls) == 1
        assert calls[0].kwargs["commands_synced"] is False
        assert calls[0].kwargs["commands_updated"] == 0
        assert calls[0].kwargs["guidelines_synced"] is True
        assert calls[0].kwargs["guidelines_updated"] == 1

    async def test_sync_commands_no_rules_manifest_skips_guidelines_but_syncs_commands(
        self, client: AsyncClient, tmp_path, monkeypatch
    ) -> None:
        """A project with no guidelines manifest reports `guidelines_synced=False`
        while `commands_synced=True`, so the two halves are shown to be
        independently reported rather than coupled to one shared flag."""
        from sova.commands.distribution import UpdateResult

        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "sova.commands.distribution.update_commands",
            lambda *a, **kw: UpdateResult(updated=1),
        )

        def fail_if_called(*_a, **_kw):
            raise AssertionError("update_guidelines must not be called")

        monkeypatch.setattr("sova.commands.distribution.update_guidelines", fail_if_called)

        with patch("sova.dashboard.routers.setup.log.info") as mock_info:
            resp = await client.post("/api/setup/commands/sync", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["commands"]["updated"] == 1
        assert data["guidelines"]["updated"] == 0

        calls = [call for call in mock_info.call_args_list if call.args and call.args[0] == "setup.commands.sync"]
        assert len(calls) == 1
        assert calls[0].kwargs["commands_synced"] is True
        assert calls[0].kwargs["guidelines_synced"] is False

    async def test_sync_commands_empty_body_syncs_guidelines_like_no_body(
        self, client: AsyncClient, tmp_path, monkeypatch
    ) -> None:
        """An empty JSON body ({}) must be treated the same as no body at all: both
        mean "sync everything", not a selective request that skips guidelines."""
        from sova.commands.distribution import UpdateResult
        from sova.commands.manifest import create_manifest

        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        create_manifest(rules_dir, {})

        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "sova.commands.distribution.update_commands",
            lambda *a, **kw: UpdateResult(updated=1),
        )
        monkeypatch.setattr(
            "sova.commands.distribution.update_guidelines",
            lambda *a, **kw: UpdateResult(updated=2),
        )

        resp = await client.post("/api/setup/commands/sync", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["guidelines"]["updated"] == 2

    async def test_create_milestones_returns_404_for_missing_dir(self, client: AsyncClient, tmp_path: Path) -> None:
        resp = await client.post(
            "/api/setup/milestones/create",
            json={"project_path": str(tmp_path / "does" / "not" / "exist")},
        )
        assert resp.status_code == 404
        assert "Directory not found" in resp.json()["detail"]


@pytest.mark.usefixtures("setup_root_tmp")
class TestSetupModelProfile:
    """The saved model profile (#1323) on the setup wizard's configure and install routes."""

    @pytest.fixture(autouse=True)
    def _profile_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Point the profile at this test's own directory, so none of these tests can touch the real one."""
        path = tmp_path / "profile-home" / "model-profile.json"
        monkeypatch.setattr(model_profile_service, "profile_path", lambda: path)
        assert tmp_path in model_profile_service.profile_path().parents
        return path

    @staticmethod
    def _write_profile(data: object) -> None:
        path = model_profile_service.profile_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    @staticmethod
    async def _stored_settings(project: Path) -> dict[str, object]:
        async with await get_session(project_dir=project) as session:
            rows = (await session.execute(select(ProjectSetting))).scalars().all()
        return {row.key: json.loads(row.value) for row in rows}

    @staticmethod
    async def _configure(client: AsyncClient, project: Path, **fields: object):
        return await client.post(
            "/api/setup/configure", json={"project_path": str(project), "github_repo": "u/t", **fields}
        )

    async def test_configure_fills_only_the_gaps_from_the_saved_model_profile(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        self._write_profile({"agent.model": "claude-sonnet-5-5", "roles.reviewer_model": "claude-opus-5-5"})
        project = tmp_path / "proj"
        project.mkdir()

        resp = await self._configure(client, project, agent_model="claude-haiku-5-5")

        assert resp.status_code == 200, resp.text
        assert "warning" not in resp.json()
        stored = await self._stored_settings(project)
        # The wizard's own model choice stays; the profile fills the setting the wizard does not ask for.
        assert (stored["agent.model"], stored["roles.reviewer_model"]) == ("claude-haiku-5-5", "claude-opus-5-5")
        assert resp.json()["profile_applied"] == ["roles.reviewer_model"]

    async def test_configure_without_agent_model_takes_the_saved_profile_model(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        self._write_profile({"agent.model": "claude-sonnet-5-5"})
        project = tmp_path / "proj"
        project.mkdir()

        resp = await self._configure(client, project)

        assert resp.status_code == 200, resp.text
        assert (await self._stored_settings(project))["agent.model"] == "claude-sonnet-5-5"
        assert resp.json()["profile_applied"] == ["agent.model"]

    async def test_configure_without_agent_model_or_profile_stores_no_model_row(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()

        resp = await self._configure(client, project)

        assert resp.status_code == 200, resp.text
        stored = await self._stored_settings(project)
        # No row: the built-in default model applies, and a later change to that default still reaches it.
        assert "agent.model" not in stored
        assert "agent.max_budget" in stored

    async def test_reconfigure_without_agent_model_keeps_the_stored_model(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        first = await self._configure(client, project, agent_model="claude-haiku-5-5")
        self._write_profile({"agent.model": "claude-sonnet-5-5"})

        resp = await self._configure(client, project)

        assert (first.status_code, resp.status_code) == (200, 200), resp.text
        assert (await self._stored_settings(project))["agent.model"] == "claude-haiku-5-5"

    async def test_reconfigure_keeps_stored_model_rows_and_never_refills_from_the_profile(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """A reconfigure of a project that chose models keeps them, and a route it cleared stays cleared."""
        project = tmp_path / "proj"
        project.mkdir()
        first = await self._configure(client, project, agent_model="claude-haiku-5-5")
        assert first.status_code == 200, first.text
        async with await get_session(project_dir=project) as session, session.begin():
            session.add(ProjectSetting(key="agent.fallback_models", value=json.dumps(["claude-x-5"])))
            session.add(ProjectSetting(key="roles.reviewer_model", value=json.dumps("claude-opus-5-5")))
        self._write_profile(
            {
                "agent.fallback_models": ["claude-y-5"],
                "roles.reviewer_model": "claude-sonnet-5-5",
                "llm.routing.review": "claude-opus-5-5",
            }
        )

        resp = await self._configure(client, project)

        assert resp.status_code == 200, resp.text
        stored = await self._stored_settings(project)
        assert stored["agent.fallback_models"] == ["claude-x-5"]
        assert stored["roles.reviewer_model"] == "claude-opus-5-5"
        assert stored["agent.model"] == "claude-haiku-5-5"
        assert "llm.routing.review" not in stored
        assert resp.json()["profile_applied"] == []

    async def test_configure_reports_a_failed_profile_without_the_exception_text(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        self._write_profile({"roles.reviewer_model": "claude-opus-5-5"})
        project = tmp_path / "proj"
        project.mkdir()

        with patch.object(model_profile_service, "get_session", side_effect=SQLAlchemyError("/secret/path.db")):
            resp = await self._configure(client, project)

        assert resp.status_code == 200, resp.text
        assert "could not be applied" in resp.json()["warning"]
        assert "/secret/path.db" not in resp.text
        stored = await self._stored_settings(project)
        # The wizard's own settings were written, and no profile row was.
        assert "agent.max_budget" in stored
        assert "roles.reviewer_model" not in stored

    @pytest.mark.parametrize("content", ["{not json", "[]"], ids=["invalid-json", "not-an-object"])
    async def test_configure_with_a_corrupt_profile_warns_and_applies_nothing(
        self, client: AsyncClient, tmp_path: Path, content: str
    ) -> None:
        self._write_profile(content)
        project = tmp_path / "proj"
        project.mkdir()

        resp = await self._configure(client, project)

        assert resp.status_code == 200, resp.text
        assert "unreadable or malformed" in resp.json()["warning"]
        assert str(tmp_path) not in resp.text
        stored = await self._stored_settings(project)
        assert "agent.max_budget" in stored
        assert not any(key.startswith("llm.routing") or key.endswith("_model") for key in stored)

    async def test_configure_expands_profile_routes_and_skips_an_unknown_task_for_a_project_with_no_models(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        # A first configure that chose no model, so the profile written afterwards still applies.
        first = await self._configure(client, project)
        self._write_profile(
            {
                "llm.routing.review": "profile-model",
                "llm.routing.develop": "claude-sonnet-5-5",
                "llm.routing.no_such_task": "claude-sonnet-5-5",
            }
        )

        resp = await self._configure(client, project)

        assert (first.status_code, resp.status_code) == (200, 200), resp.text
        stored = await self._stored_settings(project)
        assert stored["llm.routing.develop"] == "claude-sonnet-5-5"
        assert stored["llm.routing.review"] == "profile-model"
        assert "llm.routing.no_such_task" not in stored
        assert "llm.routing.no_such_task" in resp.json()["warning"]


class TestSyncFilenameValidation:
    """Direct unit coverage for the path-traversal guard, independent of the endpoint."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            pytest.param("develop.md", True, id="legitimate-filename"),
            pytest.param("../../etc/passwd", False, id="relative-traversal"),
            pytest.param("..", False, id="bare-dotdot"),
            pytest.param("/etc/passwd", False, id="leading-forward-slash"),
            pytest.param("\\windows\\system32", False, id="leading-backslash"),
            # Known limitation: ``".." in name`` matches any substring, so ``v1..2.md`` is rejected although it has
            # no separator. Fails closed; pinned so a future regex change is deliberate, not an accident.
            pytest.param("v1..2.md", False, id="merely-containing-dotdot-substring"),
            # The ``{skill}/SKILL.md`` exception is accepted by the validator even though this endpoint never syncs
            # skills (update_commands/update_guidelines only pass command and guideline filenames through).
            pytest.param("my-skill/SKILL.md", True, id="skill-md-pattern"),
            pytest.param("a/b/SKILL.md", False, id="skill-pattern-with-extra-path-segment"),
        ],
    )
    def test_is_safe_sync_filename(self, filename: str, expected: bool) -> None:
        from sova.dashboard.routers.setup import _is_safe_sync_filename

        assert _is_safe_sync_filename(filename) is expected

    def test_validate_sync_filenames_none_is_noop(self) -> None:
        from sova.dashboard.routers.setup import _validate_sync_filenames

        with patch("sova.dashboard.routers.setup._is_safe_sync_filename") as is_safe:
            result = _validate_sync_filenames(None, field_name="filenames")

        assert result is None
        is_safe.assert_not_called()

    def test_validate_sync_filenames_raises_on_invalid(self) -> None:
        from fastapi import HTTPException

        from sova.dashboard.routers.setup import _validate_sync_filenames

        with pytest.raises(HTTPException) as exc_info:
            _validate_sync_filenames(["ok.md", "../evil.md"], field_name="filenames")
        assert exc_info.value.status_code == 400
        assert "../evil.md" in exc_info.value.detail


class TestTomlConfigGeneration:
    """Tests for TomlConfig dataclass defaults."""

    def test_default_toml_config(self) -> None:
        from sova.dashboard.services.setup_service import TomlConfig

        cfg = TomlConfig()
        assert cfg.base_branch == "main"
        assert cfg.task_source == "github"
        assert cfg.agent_model is None
        assert cfg.max_budget == "10.00"
        assert cfg.ai_coauthor is True
        assert cfg.pr_auto_link is True

    def test_toml_config_jira_defaults(self) -> None:
        from sova.dashboard.services.setup_service import TomlConfig

        cfg = TomlConfig()
        assert cfg.jira_base_url == ""
        assert cfg.jira_email == ""
        assert cfg.jira_project_key == ""
        assert cfg.jira_component == ""
        assert cfg.jira_status_mapping is None
        assert cfg.jira_track_agent_work is False


class TestJiraSetupAPI:
    async def test_jira_test_connection_success(self, client) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        with patch("sova.dashboard.services.setup_service._jira_api_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: {"displayName": "Test User", "emailAddress": "e@x.com"}
            )
            resp = await client.post(
                "/api/setup/jira/test",
                json={"base_url": "https://test.atlassian.net", "email": "e@x.com", "api_token": "t"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["display_name"] == "Test User"

    async def test_jira_projects_success(self, client) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        with patch("sova.dashboard.services.setup_service._jira_api_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: [{"key": "PROJ", "name": "My Project", "lead": {"displayName": "Lead"}}]
            )
            resp = await client.post(
                "/api/setup/jira/projects",
                json={"base_url": "https://test.atlassian.net", "email": "e@x.com", "api_token": "t"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert len(data["projects"]) == 1
        assert data["projects"][0]["key"] == "PROJ"

    async def test_jira_statuses_success(self, client) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        with patch("sova.dashboard.services.setup_service._jira_api_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: [
                    {
                        "statuses": [
                            {"name": "To Do", "statusCategory": {"name": "To Do"}},
                            {"name": "In Progress", "statusCategory": {"name": "In Progress"}},
                        ],
                    }
                ],
            )
            resp = await client.post(
                "/api/setup/jira/statuses",
                json={
                    "base_url": "https://test.atlassian.net",
                    "email": "e@x.com",
                    "api_token": "t",
                    "project_key": "TEST",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert len(data["statuses"]) == 2
        assert "To Do" in data["suggested_mapping"]


class TestJiraSSRFPrevention:
    """Verify _validate_jira_base_url rejects internal/private addresses."""

    @pytest.mark.parametrize(
        ("url", "match"),
        [
            pytest.param("https://localhost/jira", "local address", id="localhost"),
            pytest.param("https://127.0.0.1/jira", "local address", id="loopback-ip"),
            pytest.param("https://10.0.0.1/jira", "private/reserved", id="private-10-range"),
            pytest.param("https://172.16.0.1/jira", "private/reserved", id="private-172-range"),
            pytest.param("https://192.168.1.1/jira", "private/reserved", id="private-192-range"),
            pytest.param("https://169.254.1.1/jira", "private/reserved", id="link-local"),
            pytest.param("http://example.atlassian.net", "only https", id="http-scheme"),
            pytest.param("https://[::1]/jira", "local address", id="ipv6-loopback"),
        ],
    )
    def test_rejects_unsafe_url(self, url: str, match: str) -> None:
        from sova.dashboard.services.setup_service import _validate_jira_base_url

        with pytest.raises(ValueError, match=match):
            _validate_jira_base_url(url)

    def test_accepts_valid_atlassian_url(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.setup_service import _validate_jira_base_url

        # Mock DNS resolution to return a public IP
        with patch(
            "socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("104.192.141.1", 0)),
            ],
        ):
            result = _validate_jira_base_url("https://mycompany.atlassian.net")
        assert result == "https://mycompany.atlassian.net"


# ---------------------------------------------------------------------------
# create_starter_milestones
# ---------------------------------------------------------------------------


class TestCreateStarterMilestones:
    async def test_creates_milestones_skips_existing(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.adapters.base import Milestone
        from sova.dashboard.services.setup_service import create_starter_milestones

        mock_adapter = AsyncMock()
        mock_adapter.list_milestones.return_value = [
            Milestone(title="Phase 1: Now", state="open"),
        ]
        mock_adapter.create_milestone.return_value = Milestone(title="Phase 2: Next", state="open")

        with (
            patch("sova.dashboard.services.setup_service.load_config"),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
        ):
            result = await create_starter_milestones(tmp_path)

        assert result["status"] == "ok"
        assert "Phase 1: Now" in result["skipped"]
        assert len(result["created"]) == 3  # Phase 2, 3, 4
        # Verify descriptions are passed to create_milestone
        for call in mock_adapter.create_milestone.call_args_list:
            assert "description" in call.kwargs
            assert call.kwargs["description"]  # non-empty

    async def test_handles_partial_failure(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.setup_service import create_starter_milestones

        mock_adapter = AsyncMock()
        mock_adapter.list_milestones.return_value = []
        call_count = 0

        async def side_effect(title: str, **_kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("API error")
            from sova.adapters.base import Milestone

            return Milestone(title=title, state="open")

        mock_adapter.create_milestone.side_effect = side_effect

        with (
            patch("sova.dashboard.services.setup_service.load_config"),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
        ):
            result = await create_starter_milestones(tmp_path)

        assert result["status"] == "ok"
        assert len(result["created"]) == 3
        assert len(result["failed"]) == 1

    async def test_returns_error_when_no_config(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.setup_service import create_starter_milestones

        with patch(
            "sova.dashboard.services.setup_service.load_config",
            side_effect=FileNotFoundError("sova.toml not found"),
        ):
            result = await create_starter_milestones(tmp_path)

        assert result["status"] == "error"
        assert "config" in result["detail"].lower()

    async def test_permission_error_captured(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.setup_service import create_starter_milestones

        mock_adapter = AsyncMock()
        mock_adapter.list_milestones.return_value = []
        mock_adapter.create_milestone.side_effect = PermissionError("Insufficient permissions")

        with (
            patch("sova.dashboard.services.setup_service.load_config"),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
        ):
            result = await create_starter_milestones(tmp_path)

        assert result["status"] == "ok"
        assert len(result["failed"]) == 4
        assert "permissions" in result["failed"][0]["error"].lower()


# ---------------------------------------------------------------------------
# _read_existing_toml
# ---------------------------------------------------------------------------


class TestReadExistingToml:
    def test_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        from sova.dashboard.services.setup_service import _read_existing_toml

        assert _read_existing_toml(tmp_path) == {}

    def test_reads_flat_and_nested_keys(self, tmp_path: Path) -> None:
        from sova.dashboard.services.setup_service import _read_existing_toml

        (tmp_path / "sova.toml").write_text('github_repo = "owner/repo"\n\n[task_source]\ntype = "github"\n')
        result = _read_existing_toml(tmp_path)
        assert result["github_repo"] == "owner/repo"
        assert result["task_source.type"] == "github"

    def test_returns_empty_on_invalid_toml(self, tmp_path: Path) -> None:
        from sova.dashboard.services.setup_service import _read_existing_toml

        (tmp_path / "sova.toml").write_text("invalid toml {{{{")
        assert _read_existing_toml(tmp_path) == {}


# ---------------------------------------------------------------------------
# create_starter_milestones: adapter error paths
# ---------------------------------------------------------------------------


class TestCreateStarterMilestonesAdapterError:
    async def test_returns_error_when_adapter_fails(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.setup_service import create_starter_milestones

        with (
            patch("sova.dashboard.services.setup_service.load_config"),
            patch("sova.adapters.create_adapter", side_effect=ValueError("missing repo")),
        ):
            result = await create_starter_milestones(tmp_path)

        assert result["status"] == "error"

    async def test_returns_error_when_list_milestones_fails(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.setup_service import create_starter_milestones

        mock_adapter = AsyncMock()
        mock_adapter.list_milestones.side_effect = RuntimeError("network error")

        with (
            patch("sova.dashboard.services.setup_service.load_config"),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
        ):
            result = await create_starter_milestones(tmp_path)

        assert result["status"] == "error"
        assert "milestones" in result["detail"].lower()


# ---------------------------------------------------------------------------
# Milestones endpoint
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("setup_root_tmp")
class TestMilestonesEndpoint:
    async def test_create_milestones_success(self, client, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "sova.dashboard.services.setup_service.create_starter_milestones",
            new_callable=AsyncMock,
            return_value={"status": "ok", "created": ["Phase 1: Now"], "skipped": [], "failed": []},
        ):
            resp = await client.post(
                "/api/setup/milestones/create",
                json={"project_path": str(tmp_path)},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Installation status / sync API
# ---------------------------------------------------------------------------


class TestInstallationAPI:
    @pytest.fixture
    async def install_client(self, tmp_path, monkeypatch):
        """Dashboard client with a project that has commands and guidelines installed."""
        from sova.commands.manifest import create_manifest

        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        (project_dir / ".claude").mkdir()

        # Create sova.toml
        (project_dir / "sova.toml").write_text(
            'github_repo = "owner/myapp"\ngithub_user = "owner"\ntest_cmd = "pytest"\nlint_cmd = "ruff"\n'
        )

        commands_dir = project_dir / ".claude" / "commands"
        commands_dir.mkdir(parents=True, exist_ok=True)
        rules_dir = project_dir / ".claude" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)

        # Install a few fake commands with manifest
        (commands_dir / "develop.md").write_text("# Develop\n")
        create_manifest(commands_dir, {"develop.md": "abc123"})

        # Install a guideline with manifest
        (rules_dir / "security.md").write_text("# Security\n")
        create_manifest(rules_dir, {"security.md": "def456"})

        # Patch get_project_dir so the settings router resolves to our tmp project
        monkeypatch.setattr(
            "sova.dashboard.routers.settings.get_project_dir",
            lambda: project_dir,
        )
        monkeypatch.setattr(
            "sova.dashboard.routers.setup.get_project_dir",
            lambda: project_dir,
        )

        app = create_app(project_dir=project_dir)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            ac.headers.update(_csrf_request_headers(ac))
            yield ac

    async def test_installation_status_returns_200(self, install_client: AsyncClient) -> None:
        resp = await install_client.get("/api/settings/installation/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "commands" in data
        assert "guidelines" in data
        assert "total_updates" in data
        assert "has_updates" in data
        assert isinstance(data["commands"]["changed"], list)
        assert isinstance(data["commands"]["new"], list)
        assert isinstance(data["commands"]["removed"], list)
        assert isinstance(data["guidelines"]["changed"], list)

    async def test_sync_returns_structured_and_flat(self, install_client: AsyncClient) -> None:
        resp = await install_client.post("/api/setup/commands/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        # Backward-compatible flat fields
        assert "updated" in data
        assert "skipped" in data
        assert "conflicts" in data
        # Structured per-category fields
        assert "commands" in data
        assert "guidelines" in data
        assert "updated" in data["commands"]
        assert "updated" in data["guidelines"]

    async def test_installation_diff_returns_200(self, install_client: AsyncClient) -> None:
        resp = await install_client.get("/api/settings/installation/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_available"] is True
        assert isinstance(data["files"], list)

        entry = next(f for f in data["files"] if f["filename"] == "standup.md")
        assert entry["status"] == "new"
        assert entry["category"] == "command"
        assert entry["local_content"] is None
        assert entry["canonical_content"]

    async def test_sync_with_filenames_updates_selected_and_skips_guidelines(self, install_client: AsyncClient) -> None:
        """A selective sync body only touches the named commands and leaves guidelines alone."""
        resp = await install_client.post("/api/setup/commands/sync", json={"filenames": ["standup.md"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["commands"]["updated"] == 1
        assert data["guidelines"]["updated"] == 0
