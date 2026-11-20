"""Roles API routes.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# Roles API
# ---------------------------------------------------------------------------


class TestRolesAPI:
    async def test_list_roles(self, client):
        """GET /api/roles returns built-in roles."""
        resp = await client.get("/api/roles")
        assert resp.status_code == 200
        data = resp.json()
        assert "roles" in data
        names = {r["name"] for r in data["roles"]}
        assert "developer" in names
        assert "triage" in names

    async def test_list_commands(self, client):
        """GET /api/roles/commands returns discovered commands with content and metadata."""
        resp = await client.get("/api/roles/commands")
        assert resp.status_code == 200
        data = resp.json()
        assert "commands" in data
        assert len(data["commands"]) > 0
        names = {c["name"] for c in data["commands"]}
        assert "develop" in names
        cmd = next(c for c in data["commands"] if c["name"] == "develop")
        assert "content" in cmd
        assert len(cmd["content"]) > 0
        assert "installed_path" in cmd
        assert "has_local_drift" in cmd
        assert isinstance(cmd["has_local_drift"], bool)

    async def test_get_command_by_name(self, client):
        """GET /api/roles/commands/{name} returns a single command."""
        resp = await client.get("/api/roles/commands/develop")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "develop"
        assert "content" in data
        assert len(data["content"]) > 0
        assert "category" in data

    async def test_get_command_not_found(self, client):
        """GET /api/roles/commands/{name} returns 404 for unknown command."""
        resp = await client.get("/api/roles/commands/nonexistent-command-xyz")
        assert resp.status_code == 404

    async def test_get_command_path_traversal(self, client):
        """GET /api/roles/commands/{name} rejects path-traversal names."""
        for name in ["../etc/passwd", "foo/bar", "..\\windows", "name..ext"]:
            resp = await client.get(f"/api/roles/commands/{name}")
            assert resp.status_code in (404, 422), f"Expected 404/422 for {name!r}, got {resp.status_code}"

    async def test_commands_page(self, client):
        """GET /commands returns the commands browser page."""
        resp = await client.get("/commands")
        assert resp.status_code == 200
        assert "Commands" in resp.text

    async def test_list_commands_with_project_dir(self, client, tmp_path, monkeypatch):
        """GET /api/roles/commands with project_dir loads installed commands and detects drift."""
        installed_dir = tmp_path / ".claude" / "commands"
        installed_dir.mkdir(parents=True)
        (installed_dir / "develop.md").write_text("---\nname: develop\n---\nlocal copy", encoding="utf-8")
        monkeypatch.setattr("sova.config.context.get_project_dir", lambda: tmp_path)
        from sova.dashboard.services import role_service

        role_service._cmd_cache.clear()
        resp = await client.get("/api/roles/commands")
        assert resp.status_code == 200
        data = resp.json()
        cmd = next((c for c in data["commands"] if c["name"] == "develop"), None)
        assert cmd is not None
        assert cmd["installed_path"] == ".claude/commands/develop.md"
        assert cmd["has_local_drift"] is True
        role_service._cmd_cache.clear()

    async def test_list_commands_in_single_project_mode_uses_the_default_project_dir(
        self, client, tmp_path, monkeypatch
    ):
        """With no per-request project, the single-project default supplies the installed commands and drift.

        This endpoint used to read only the per-request contextvar, which single-project mode never sets, so it
        served the canonical catalog with no drift metadata there. The shared resolver's default fallback is
        an intended behaviour change of the project-dir consolidation, pinned here.
        """
        installed_dir = tmp_path / ".claude" / "commands"
        installed_dir.mkdir(parents=True)
        (installed_dir / "develop.md").write_text("---\nname: develop\n---\nlocal copy", encoding="utf-8")
        monkeypatch.setattr("sova.dashboard.routers.roles.get_default_project_dir", lambda: tmp_path)
        from sova.dashboard.services import role_service

        role_service._cmd_cache.clear()
        resp = await client.get("/api/roles/commands")

        assert resp.status_code == 200
        cmd = next(c for c in resp.json()["commands"] if c["name"] == "develop")
        assert cmd["installed_path"] == ".claude/commands/develop.md"
        assert cmd["has_local_drift"] is True
        role_service._cmd_cache.clear()

    async def test_list_commands_without_a_project_ignores_the_cwd(self, client, tmp_path, monkeypatch):
        """No project selected means the canonical catalog only: the cwd's own .claude/commands is not the project's."""
        installed_dir = tmp_path / ".claude" / "commands"
        installed_dir.mkdir(parents=True)
        (installed_dir / "develop.md").write_text("---\nname: develop\n---\nlocal copy", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sova.dashboard.routers.roles.get_default_project_dir", lambda: None)
        from sova.dashboard.services import role_service

        role_service._cmd_cache.clear()
        resp = await client.get("/api/roles/commands")

        assert resp.status_code == 200
        cmd = next(c for c in resp.json()["commands"] if c["name"] == "develop")
        assert cmd["has_local_drift"] is False
        role_service._cmd_cache.clear()

    async def test_list_commands_symlink_rejected(self, client, tmp_path, monkeypatch):
        """Symlinked command files in the installed directory are skipped."""
        installed_dir = tmp_path / ".claude" / "commands"
        installed_dir.mkdir(parents=True)
        target = tmp_path / "secret.txt"
        target.write_text("secret data", encoding="utf-8")
        symlink = installed_dir / "evil.md"
        symlink.symlink_to(target)
        monkeypatch.setattr("sova.config.context.get_project_dir", lambda: tmp_path)
        from sova.dashboard.services import role_service

        role_service._cmd_cache.clear()
        resp = await client.get("/api/roles/commands")
        assert resp.status_code == 200
        data = resp.json()
        names = {c["name"] for c in data["commands"]}
        assert "evil" not in names
        role_service._cmd_cache.clear()

    async def test_list_commands_ttl_cache(self, client, monkeypatch):
        """Second call within TTL returns cached result without re-discovering."""
        from sova.dashboard.services import role_service

        role_service._cmd_cache.clear()
        resp1 = await client.get("/api/roles/commands")
        assert resp1.status_code == 200
        count1 = len(resp1.json()["commands"])
        resp2 = await client.get("/api/roles/commands")
        assert resp2.status_code == 200
        assert len(resp2.json()["commands"]) == count1
        role_service._cmd_cache.clear()

    async def test_command_entry_to_dict_without_installed(self):
        """_command_entry_to_dict with no installed content returns canonical body."""
        from sova.commands.catalog import CommandEntry
        from sova.dashboard.services.role_service import _command_entry_to_dict

        entry = CommandEntry(
            name="test-cmd",
            description="A test",
            category="test",
            user_invocable=True,
            path=Path("/nonexistent/test-cmd.md"),
        )
        result = _command_entry_to_dict(entry)
        assert result["name"] == "test-cmd"
        assert result["content"] == ""
        assert result["installed_path"] is None
        assert result["has_local_drift"] is False

    async def test_command_entry_to_dict_with_installed_no_drift(self, tmp_path):
        """_command_entry_to_dict detects no drift when installed matches canonical."""
        from sova.commands.catalog import CommandEntry
        from sova.dashboard.services.role_service import _command_entry_to_dict

        cmd_file = tmp_path / "my-cmd.md"
        cmd_file.write_text("---\nname: my-cmd\n---\nbody", encoding="utf-8")
        entry = CommandEntry(
            name="my-cmd",
            description="desc",
            category="dev",
            user_invocable=True,
            path=cmd_file,
        )
        result = _command_entry_to_dict(entry, {"my-cmd": "---\nname: my-cmd\n---\nbody"})
        assert result["has_local_drift"] is False
        assert result["installed_path"] == ".claude/commands/my-cmd.md"
        assert result["content"] == "---\nname: my-cmd\n---\nbody"

    async def test_command_entry_to_dict_with_drift(self, tmp_path):
        """_command_entry_to_dict detects drift when installed differs from canonical."""
        from sova.commands.catalog import CommandEntry
        from sova.dashboard.services.role_service import _command_entry_to_dict

        cmd_file = tmp_path / "my-cmd.md"
        cmd_file.write_text("canonical body", encoding="utf-8")
        entry = CommandEntry(
            name="my-cmd",
            description="desc",
            category="dev",
            user_invocable=True,
            path=cmd_file,
        )
        result = _command_entry_to_dict(entry, {"my-cmd": "different local body"})
        assert result["has_local_drift"] is True
        assert result["content"] == "different local body"

    async def test_get_command_detail_traversal_direct(self):
        """get_command_detail rejects path-traversal names at the service level."""
        from sova.dashboard.services.role_service import get_command_detail

        assert get_command_detail("../etc/passwd") is None
        assert get_command_detail("foo/bar") is None
        assert get_command_detail("..\\windows") is None

    async def test_get_command_detail_valid_name(self):
        """get_command_detail returns a command dict for a valid name."""
        from sova.dashboard.services.role_service import _cmd_cache, get_command_detail

        _cmd_cache.clear()
        result = get_command_detail("develop")
        assert result is not None
        assert result["name"] == "develop"
        _cmd_cache.clear()

    async def test_commands_endpoint_resolves_the_project_dir_from_the_request_context(
        self, client, tmp_path, monkeypatch
    ):
        """GET /api/roles/commands hands role_service the contextvar project, as a string."""
        from sova.config.context import clear_project_context, set_project_context

        set_project_context(tmp_path, "slug")
        try:
            with patch("sova.dashboard.routers.roles.role_service.get_available_commands", return_value=[]) as mock:
                resp = await client.get("/api/roles/commands")
        finally:
            clear_project_context()

        assert resp.status_code == 200
        mock.assert_called_once_with(str(tmp_path))

    async def test_commands_endpoint_falls_back_to_the_single_project_default(self, client, tmp_path):
        """With no request context the single-project default resolves, not None."""
        with (
            patch("sova.dashboard.routers.roles.get_default_project_dir", return_value=tmp_path),
            patch("sova.dashboard.routers.roles.role_service.get_available_commands", return_value=[]) as mock,
        ):
            resp = await client.get("/api/roles/commands")

        assert resp.status_code == 200
        mock.assert_called_once_with(str(tmp_path))

    async def test_command_detail_endpoint_keeps_none_without_any_project(self, client):
        """No context and no default means no project: role_service serves the canonical catalog.

        The server's own cwd must not stand in for a project, or a dashboard started from an
        unrelated directory with its own .claude/commands/ would serve those as the project's.
        """
        with (
            patch("sova.dashboard.routers.roles.get_default_project_dir", return_value=None),
            patch("sova.dashboard.routers.roles.role_service.get_command_detail", return_value={"name": "x"}) as mock,
        ):
            resp = await client.get("/api/roles/commands/x")

        assert resp.status_code == 200
        mock.assert_called_once_with("x", None)

    async def test_commands_endpoint_keeps_none_without_any_project(self, client):
        with (
            patch("sova.dashboard.routers.roles.get_default_project_dir", return_value=None),
            patch("sova.dashboard.routers.roles.role_service.get_available_commands", return_value=[]) as mock,
        ):
            resp = await client.get("/api/roles/commands")

        assert resp.status_code == 200
        mock.assert_called_once_with(None)

    async def test_get_builtin_role(self, client):
        """GET /api/roles/developer returns the built-in developer role."""
        resp = await client.get("/api/roles/developer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "developer"
        assert data["is_builtin"] is True
        assert "graph_json" in data

    async def test_get_custom_role(self, client):
        """GET /api/roles/{name} returns a custom role from the DB."""
        graph = {
            "nodes": [{"id": "n1", "command": "develop", "label": "Dev", "position": {"x": 0, "y": 0}, "params": {}}],
            "edges": [],
        }
        await client.post(
            "/api/roles",
            json={
                "name": "fetch-me",
                "description": "Fetchable role",
                "graph_json": graph,
            },
        )
        resp = await client.get("/api/roles/fetch-me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "fetch-me"
        assert data["is_builtin"] is False

    async def test_get_nonexistent_role(self, client):
        """GET /api/roles/{name} returns 404 for unknown role."""
        resp = await client.get("/api/roles/does-not-exist")
        assert resp.status_code == 404

    async def test_create_custom_role(self, client):
        """POST /api/roles creates a custom role."""
        graph = {
            "nodes": [{"id": "n1", "command": "develop", "label": "Dev", "position": {"x": 0, "y": 0}, "params": {}}],
            "edges": [],
        }
        resp = await client.post(
            "/api/roles",
            json={
                "name": "my-workflow",
                "description": "Test workflow",
                "graph_json": graph,
                "input_states": ["researched"],
                "output_state": "in_review",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "my-workflow"
        assert data["is_builtin"] is False

    async def test_create_rejects_builtin_name(self, client):
        """POST /api/roles rejects built-in role names."""
        resp = await client.post(
            "/api/roles",
            json={
                "name": "developer",
                "graph_json": {"nodes": [{"id": "n1", "command": "test"}], "edges": []},
            },
        )
        assert resp.status_code == 409

    async def test_create_accepts_draft_dag(self, client):
        """POST /api/roles accepts drafts (validation on save, not create)."""
        graph = {
            "nodes": [{"id": "n1", "command": "", "label": "Start"}],
            "edges": [],
        }
        resp = await client.post(
            "/api/roles",
            json={"name": "draft-role", "graph_json": graph},
        )
        assert resp.status_code == 200

    async def test_update_rejects_invalid_dag(self, client):
        """PUT /api/roles/{name} rejects DAGs with cycles."""
        graph_ok = {
            "nodes": [{"id": "a", "command": "x"}],
            "edges": [],
        }
        resp = await client.post(
            "/api/roles",
            json={"name": "bad-update-role", "graph_json": graph_ok},
        )
        assert resp.status_code == 200

        graph_bad = {
            "nodes": [{"id": "a", "command": "x"}, {"id": "b", "command": "y"}],
            "edges": [{"id": "e1", "source": "a", "target": "b"}, {"id": "e2", "source": "b", "target": "a"}],
        }
        resp = await client.put(
            "/api/roles/bad-update-role",
            json={"graph_json": graph_bad},
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert isinstance(detail, str)
        assert "Invalid DAG" in detail

    async def test_update_custom_role(self, client):
        """PUT /api/roles/{name} updates a custom role."""
        # Create first
        graph = {
            "nodes": [{"id": "n1", "command": "develop", "label": "Dev", "position": {"x": 0, "y": 0}, "params": {}}],
            "edges": [],
        }
        await client.post(
            "/api/roles",
            json={
                "name": "editable",
                "graph_json": graph,
            },
        )

        # Update
        resp = await client.put(
            "/api/roles/editable",
            json={
                "description": "Updated description",
            },
        )
        data = resp.json()
        assert data.get("description") == "Updated description"

    async def test_update_rejects_builtin(self, client):
        """PUT /api/roles/developer rejects updates to built-in roles."""
        resp = await client.put(
            "/api/roles/developer",
            json={
                "description": "Hacked",
            },
        )
        assert resp.status_code == 404

    async def test_delete_custom_role(self, client):
        """DELETE /api/roles/{name} removes custom roles."""
        graph = {
            "nodes": [{"id": "n1", "command": "test", "label": "T", "position": {"x": 0, "y": 0}, "params": {}}],
            "edges": [],
        }
        await client.post(
            "/api/roles",
            json={
                "name": "deletable",
                "graph_json": graph,
            },
        )

        resp = await client.delete("/api/roles/deletable")
        data = resp.json()
        assert data["status"] == "deleted"

    async def test_delete_rejects_builtin(self, client):
        """DELETE /api/roles/developer rejects deleting built-in roles."""
        resp = await client.delete("/api/roles/developer")
        assert resp.status_code == 404

    async def test_validate_dag(self, client):
        """POST /api/roles/{name}/validate validates DAG structure."""
        graph = {
            "nodes": [{"id": "a", "command": "develop"}, {"id": "b", "command": "test"}],
            "edges": [{"id": "e1", "source": "a", "target": "b"}],
        }
        resp = await client.post("/api/roles/test/validate", json={"graph_json": graph})
        data = resp.json()
        assert data["valid"] is True
        assert data["errors"] == []

    async def test_validate_dag_with_errors(self, client):
        """POST /api/roles/{name}/validate returns errors for invalid DAGs."""
        resp = await client.post("/api/roles/test/validate", json={"graph_json": {"nodes": [], "edges": []}})
        data = resp.json()
        assert data["valid"] is False
        assert len(data["errors"]) > 0

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/roles", id="roles-list"),
            pytest.param("/roles/developer", id="role-editor"),
        ],
    )
    async def test_page_renders(self, client: AsyncClient, path: str) -> None:
        resp = await client.get(path)
        assert resp.status_code == 200
