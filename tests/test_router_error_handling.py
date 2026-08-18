"""Tests for router error handling standardization (issue #141)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def client():
    from sova.dashboard.app import create_app

    app = create_app(multi_project=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestTasksRouterErrors:
    async def test_list_issues_returns_503_on_adapter_failure(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.routers.tasks._issues_cache", {}):
            with patch("sova.config.loader.load_config", side_effect=RuntimeError("no config")):
                resp = await client.get("/api/tasks/issues")
        assert resp.status_code == 503
        assert "Task source unavailable" in resp.json()["detail"]

    async def test_task_history_validates_limit(self, client: AsyncClient) -> None:
        resp = await client.get("/api/tasks/history?limit=0")
        assert resp.status_code == 422

    async def test_task_history_validates_limit_upper(self, client: AsyncClient) -> None:
        resp = await client.get("/api/tasks/history?limit=501")
        assert resp.status_code == 422


class TestSetupRouterErrors:
    async def test_install_hides_exception_detail(self, client: AsyncClient) -> None:
        with patch(
            "sova.cli.commands.project._install",
            new_callable=AsyncMock,
            side_effect=RuntimeError("secret path /etc/shadow"),
        ):
            resp = await client.post(
                "/api/setup/install",
                json={"project_path": "/tmp"},
            )
        assert resp.status_code == 500
        assert "Installation failed" in resp.json()["detail"]
        assert "secret" not in resp.json()["detail"]

    async def test_jira_test_config_error_returns_400(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.routers.setup.setup_service.test_jira_connection",
            side_effect=ValueError("bad url"),
        ):
            resp = await client.post(
                "/api/setup/jira/test",
                json={"base_url": "http://bad", "email": "a@b.com", "api_token": "tok"},
            )
        assert resp.status_code == 400
        assert "Configuration validation failed" in resp.json()["detail"]

    async def test_jira_test_connection_error_returns_503(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.routers.setup.setup_service.test_jira_connection",
            side_effect=ConnectionError("unreachable"),
        ):
            resp = await client.post(
                "/api/setup/jira/test",
                json={"base_url": "http://jira", "email": "a@b.com", "api_token": "tok"},
            )
        assert resp.status_code == 503
        assert "Connection test failed" in resp.json()["detail"]

    async def test_jira_projects_connection_error_returns_503(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.routers.setup.setup_service.discover_jira_projects",
            side_effect=ConnectionError("unreachable"),
        ):
            resp = await client.post(
                "/api/setup/jira/projects",
                json={"base_url": "http://jira", "email": "a@b.com", "api_token": "tok"},
            )
        assert resp.status_code == 503

    async def test_jira_statuses_connection_error_returns_503(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.routers.setup.setup_service.discover_jira_statuses",
            side_effect=ConnectionError("unreachable"),
        ):
            resp = await client.post(
                "/api/setup/jira/statuses",
                json={
                    "base_url": "http://jira",
                    "email": "a@b.com",
                    "api_token": "tok",
                    "project_key": "PROJ",
                },
            )
        assert resp.status_code == 503

    async def test_jira_test_programming_error_propagates(self, client: AsyncClient) -> None:
        with (
            patch(
                "sova.dashboard.routers.setup.setup_service.test_jira_connection",
                side_effect=TypeError("NoneType has no len()"),
            ),
            pytest.raises(TypeError, match="NoneType"),
        ):
            await client.post(
                "/api/setup/jira/test",
                json={"base_url": "http://jira", "email": "a@b.com", "api_token": "tok"},
            )


class TestRolesRouterErrors:
    async def test_update_role_dag_error_is_string(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.routers.roles.validate_dag", return_value=(["cycle detected", "missing node"], {})):
            resp = await client.put(
                "/api/roles/test-role",
                json={"graph_json": {"steps": []}},
            )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert isinstance(detail, str)
        assert "Invalid DAG" in detail
        assert "cycle detected" in detail


class TestCostsRouterErrors:
    async def test_daily_costs_validates_days(self, client: AsyncClient) -> None:
        resp = await client.get("/api/costs/daily?days=0")
        assert resp.status_code == 422

    async def test_daily_costs_validates_days_upper(self, client: AsyncClient) -> None:
        resp = await client.get("/api/costs/daily?days=91")
        assert resp.status_code == 422


class TestQuotaRouterErrors:
    async def test_sync_quota_no_repo_returns_503(self, client: AsyncClient) -> None:
        mock_cfg = MagicMock()
        mock_cfg.coderabbit_quota.enabled = True
        mock_cfg.github_repo = ""
        with patch("sova.dashboard.routers.quota.load_config", return_value=mock_cfg):
            resp = await client.post("/api/quota/coderabbit/sync")
        assert resp.status_code == 503
        assert "github_repo" in resp.json()["detail"]


class TestLogsRouterErrors:
    async def test_logs_validates_limit(self, client: AsyncClient) -> None:
        resp = await client.get("/api/logs?limit=0")
        assert resp.status_code == 422

    async def test_logs_validates_limit_upper(self, client: AsyncClient) -> None:
        resp = await client.get("/api/logs?limit=1001")
        assert resp.status_code == 422

    async def test_logs_validates_offset_negative(self, client: AsyncClient) -> None:
        resp = await client.get("/api/logs?offset=-1")
        assert resp.status_code == 422


class TestAgentsRouterErrors:
    async def test_run_command_error_dict_returns_409(self, client: AsyncClient) -> None:
        from sova.dashboard.services import control_service as cs

        with patch.object(
            cs,
            "start_command",
            new_callable=AsyncMock,
            return_value={"error": "conflict", "detail": "Agent already running"},
        ):
            resp = await client.post(
                "/api/agents/command",
                json={"command": "integrate-pr", "args": {}},
            )
        assert resp.status_code == 409
        assert "Agent already running" in resp.json()["detail"]

    async def test_run_command_status_error_dict_returns_409(self, client: AsyncClient) -> None:
        """Error dict with 'status': 'error' (no 'error' key) still returns 409."""
        from sova.dashboard.services import control_service as cs

        with patch.object(
            cs,
            "start_command",
            new_callable=AsyncMock,
            return_value={"status": "error", "detail": "Issue conflict"},
        ):
            resp = await client.post(
                "/api/agents/command",
                json={"command": "integrate-pr", "args": {}},
            )
        assert resp.status_code == 409
        assert "Issue conflict" in resp.json()["detail"]

    async def test_start_agent_error_dict_returns_409(self, client: AsyncClient) -> None:
        """Error dict from start_agent with 'error' key returns 409."""
        from sova.dashboard.services import control_service as cs

        with patch.object(
            cs,
            "start_agent",
            new_callable=AsyncMock,
            return_value={"error": "conflict", "detail": "Agent already running"},
        ):
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "42"},
            )
        assert resp.status_code == 409
        assert "Agent already running" in resp.json()["detail"]

    async def test_start_agent_status_error_dict_returns_409(self, client: AsyncClient) -> None:
        """Error dict from start_agent with 'status': 'error' (no 'error' key) returns 409."""
        from sova.dashboard.services import control_service as cs

        with patch.object(
            cs,
            "start_agent",
            new_callable=AsyncMock,
            return_value={"status": "error", "detail": "Issue already running"},
        ):
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "42"},
            )
        assert resp.status_code == 409
        assert "Issue already running" in resp.json()["detail"]


class TestQueueRouterErrors:
    async def test_start_from_queue_error_dict_returns_409(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.routers.queue.start_agent",
            new_callable=AsyncMock,
            return_value={"error": "conflict", "detail": "Issue already running"},
        ):
            resp = await client.post(
                "/api/queue/start/42",
                json={},
            )
        assert resp.status_code == 409
        assert "Issue already running" in resp.json()["detail"]


class TestSpecRouterErrors:
    async def test_approve_spec_agent_error_returns_409(self, client: AsyncClient) -> None:
        from sova.dashboard.services import control_service as cs

        with (
            patch(
                "sova.dashboard.routers.spec.spec_service.approve_spec",
                return_value={"status": "approved"},
            ),
            patch(
                "sova.dashboard.routers.spec._transition_to_researched",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                cs,
                "start_agent",
                new_callable=AsyncMock,
                return_value={"error": "conflict", "detail": "Slot full"},
            ),
        ):
            resp = await client.post("/api/spec/42/approve")
        assert resp.status_code == 409


class TestSettingsRouterErrors:
    async def test_update_config_error_chains_exception(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.routers.settings.settings_service.update_config",
            side_effect=RuntimeError("toml parse error"),
        ):
            resp = await client.post(
                "/api/settings/config",
                json={"key": "base_branch", "value": "main"},
            )
        assert resp.status_code == 500
        assert "Failed to update configuration" in resp.json()["detail"]

    async def test_audit_labels_not_github_returns_400(self, client: AsyncClient) -> None:
        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "jira"
        with patch("sova.config.loader.load_config", return_value=mock_cfg):
            resp = await client.get("/api/settings/labels/audit")
        assert resp.status_code == 400
        assert "GitHub-only" in resp.json()["detail"]

    async def test_audit_labels_no_github_repo_returns_400(self, client: AsyncClient) -> None:
        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = ""
        with patch("sova.config.loader.load_config", return_value=mock_cfg):
            resp = await client.get("/api/settings/labels/audit")
        assert resp.status_code == 400
        assert "not configured" in resp.json()["detail"]

    async def test_audit_labels_gh_cli_unavailable(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                return_value=ShellResult(returncode=1, stdout="", stderr="gh: command not found"),
            ),
        ):
            resp = await client.get("/api/settings/labels/audit")
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert "not available" in data["error"]
        assert data["missing"] == []

    async def test_audit_labels_json_parse_error(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                return_value=ShellResult(returncode=0, stdout="invalid json", stderr=""),
            ),
        ):
            resp = await client.get("/api/settings/labels/audit")
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert "parse" in data["error"]

    async def test_audit_labels_success_with_missing(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                return_value=ShellResult(returncode=0, stdout='[{"name": "type: feature"}]', stderr=""),
            ),
        ):
            resp = await client.get("/api/settings/labels/audit")
        assert resp.status_code == 200
        data = resp.json()
        assert "missing" in data
        assert len(data["missing"]) > 0
        assert "total_required" in data

    async def test_create_labels_not_github_returns_400(self, client: AsyncClient) -> None:
        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "jira"
        with patch("sova.config.loader.load_config", return_value=mock_cfg):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 400
        assert "GitHub-only" in resp.json()["detail"]

    async def test_create_labels_fetch_error_returns_500(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                return_value=ShellResult(returncode=1, stdout="", stderr="connection failed"),
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 500
        assert "Failed to fetch labels" in resp.json()["detail"]

    async def test_create_labels_no_missing_labels(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        # Return all required labels as existing
        settings_module = __import__("sova.dashboard.routers.settings", fromlist=["_REQUIRED_LABELS"])
        all_labels = [{"name": label["name"]} for label in settings_module._REQUIRED_LABELS]

        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                return_value=ShellResult(
                    returncode=0,
                    stdout=__import__("json").dumps(all_labels),
                    stderr="",
                ),
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == 0
        assert "already exist" in data["message"]

    async def test_create_labels_invalid_color_format(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                return_value=ShellResult(returncode=0, stdout="[]", stderr=""),
            ),
            patch(
                "sova.dashboard.routers.settings._REQUIRED_LABELS",
                [{"name": "test:label", "color": "ZZZZZZ", "description": "Test"}],
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == 0
        assert "errors" in data
        assert any("Invalid color" in err for err in data["errors"])

    async def test_create_labels_permission_denied(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        async def run_mock(*args, **kwargs):
            if "label" in args and "list" in args:
                return ShellResult(returncode=0, stdout="[]", stderr="")
            return ShellResult(returncode=1, stdout="", stderr="HTTP 403: permission denied")

        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                side_effect=run_mock,
            ),
            patch(
                "sova.dashboard.routers.settings._REQUIRED_LABELS",
                [{"name": "test:label", "color": "a2eeef", "description": "Test"}],
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" in data
        assert any("Permission denied" in err for err in data["errors"])

    async def test_create_labels_error_truncation(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        # Create more than _MAX_LABEL_ERRORS (10) failing labels
        failing_labels = [{"name": f"test:label{i}", "color": "a2eeef", "description": f"Test {i}"} for i in range(15)]

        async def run_mock(*args, **kwargs):
            if "label" in args and "list" in args:
                return ShellResult(returncode=0, stdout="[]", stderr="")
            return ShellResult(returncode=1, stdout="", stderr="creation failed")

        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                side_effect=run_mock,
            ),
            patch(
                "sova.dashboard.routers.settings._REQUIRED_LABELS",
                failing_labels,
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" in data
        assert len(data["errors"]) == 10
        assert "errors_truncated" in data
        assert data["errors_truncated"] == 5

    async def test_create_labels_success_creates_missing(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        test_labels = [
            {"name": "test:one", "color": "a2eeef", "description": "Test 1"},
            {"name": "test:two", "color": "d4c5f9", "description": "Test 2"},
            {"name": "test:three", "color": "0e8a16", "description": "Test 3"},
        ]

        async def run_mock(*args, **kwargs):
            if "label" in args and "list" in args:
                # Return empty list so all labels are missing
                return ShellResult(returncode=0, stdout="[]", stderr="")
            # All create commands succeed
            return ShellResult(returncode=0, stdout="", stderr="")

        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                side_effect=run_mock,
            ),
            patch(
                "sova.dashboard.routers.settings._REQUIRED_LABELS",
                test_labels,
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == 3
        assert "errors" not in data

    async def test_create_labels_partial_success(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        test_labels = [
            {"name": "test:success", "color": "a2eeef", "description": "Will succeed"},
            {"name": "test:fail", "color": "d4c5f9", "description": "Will fail"},
        ]

        async def run_mock(*args, **kwargs):
            if "label" in args and "list" in args:
                return ShellResult(returncode=0, stdout="[]", stderr="")
            # First label succeeds, second fails
            if "test:success" in args:
                return ShellResult(returncode=0, stdout="", stderr="")
            return ShellResult(returncode=1, stdout="", stderr="API error")

        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                side_effect=run_mock,
            ),
            patch(
                "sova.dashboard.routers.settings._REQUIRED_LABELS",
                test_labels,
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == 1
        assert "errors" in data
        assert len(data["errors"]) == 1
        assert "test:fail" in data["errors"][0]

    async def test_create_labels_message_truncation(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        test_labels = [{"name": "test:label", "color": "a2eeef", "description": "Test"}]
        # Error message longer than _MAX_ERROR_MESSAGE_LENGTH (200)
        long_error = "x" * 300

        async def run_mock(*args, **kwargs):
            if "label" in args and "list" in args:
                return ShellResult(returncode=0, stdout="[]", stderr="")
            return ShellResult(returncode=1, stdout="", stderr=long_error)

        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                side_effect=run_mock,
            ),
            patch(
                "sova.dashboard.routers.settings._REQUIRED_LABELS",
                test_labels,
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" in data
        # Error message should be truncated to 200 chars
        assert all(len(err) <= 220 for err in data["errors"])  # 200 + label name prefix


class TestHandoffRouterErrors:
    """Tests for handoff.py execute_handoff_action error dict handling."""

    async def test_execute_agent_action_error_dict_returns_409(self, client: AsyncClient) -> None:
        """Error dict from start_agent with 'error' key returns 409."""
        from sova.dashboard.services import control_service as cs

        handoff_data = {
            "issue": "10",
            "pr_number": 5,
            "next_actions": [
                {"id": "run-dev", "label": "Run Developer", "type": "agent", "role": "developer", "issue": "10"},
            ],
        }
        with (
            patch(
                "sova.dashboard.routers.handoff.handoff_service.get_all_handoffs",
                return_value=[handoff_data],
            ),
            patch(
                "sova.dashboard.routers.handoff.handoff_service.build_action_command",
                return_value={"type": "agent", "issue": "10", "role": "developer"},
            ),
            patch(
                "sova.dashboard.routers.handoff.handoff_service.clear_handoff",
            ),
            patch.object(
                cs,
                "start_agent",
                new_callable=AsyncMock,
                return_value={"error": "conflict", "detail": "Slot full"},
            ),
        ):
            resp = await client.post("/api/handoff/execute", json={"action_id": "run-dev"})
        assert resp.status_code == 409
        assert "Slot full" in resp.json()["detail"]

    async def test_execute_agent_action_status_error_returns_409(self, client: AsyncClient) -> None:
        """Error dict from start_agent with 'status': 'error' (no 'error' key) returns 409."""
        from sova.dashboard.services import control_service as cs

        handoff_data = {
            "issue": "10",
            "pr_number": 5,
            "next_actions": [
                {"id": "run-dev", "label": "Run Developer", "type": "agent", "role": "developer", "issue": "10"},
            ],
        }
        with (
            patch(
                "sova.dashboard.routers.handoff.handoff_service.get_all_handoffs",
                return_value=[handoff_data],
            ),
            patch(
                "sova.dashboard.routers.handoff.handoff_service.build_action_command",
                return_value={"type": "agent", "issue": "10", "role": "developer"},
            ),
            patch(
                "sova.dashboard.routers.handoff.handoff_service.clear_handoff",
            ),
            patch.object(
                cs,
                "start_agent",
                new_callable=AsyncMock,
                return_value={"status": "error", "detail": "Issue already running"},
            ),
        ):
            resp = await client.post("/api/handoff/execute", json={"action_id": "run-dev"})
        assert resp.status_code == 409
        assert "Issue already running" in resp.json()["detail"]

    async def test_execute_command_action_error_dict_returns_409(self, client: AsyncClient) -> None:
        """Error dict from start_command with 'status': 'error' returns 409."""
        from sova.dashboard.services import control_service as cs

        handoff_data = {
            "issue": "10",
            "pr_number": 5,
            "next_actions": [
                {
                    "id": "integrate",
                    "label": "Integrate PR",
                    "type": "claude-command",
                    "command": "integrate-pr",
                    "args": {},
                },
            ],
        }
        with (
            patch(
                "sova.dashboard.routers.handoff.handoff_service.get_all_handoffs",
                return_value=[handoff_data],
            ),
            patch(
                "sova.dashboard.routers.handoff.handoff_service.build_action_command",
                return_value={"type": "claude-command", "command": "integrate-pr", "args": {}},
            ),
            patch(
                "sova.dashboard.routers.handoff.handoff_service.clear_handoff",
            ),
            patch.object(
                cs,
                "start_command",
                new_callable=AsyncMock,
                return_value={"status": "error", "detail": "No agent slots available"},
            ),
        ):
            resp = await client.post("/api/handoff/execute", json={"action_id": "integrate"})
        assert resp.status_code == 409
        assert "No agent slots available" in resp.json()["detail"]
