"""Tests for router error handling standardization (issue #141)."""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from tests.conftest import csrf_request_headers as _csrf_request_headers

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


@pytest.fixture(autouse=True)
def _no_active_agent_on_the_issue():
    """/revise peeks at the process-wide agent registry, which an earlier test's agent can still occupy."""
    from unittest.mock import AsyncMock, patch

    from sova.dashboard.services import control_service

    with patch.object(control_service, "issue_conflict", new=AsyncMock(return_value=None)):
        yield


@pytest.fixture
async def client(dashboard_app):

    app = dashboard_app
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers.update(_csrf_request_headers(ac))
        yield ac


class TestTasksRouterErrors:
    async def test_list_issues_returns_503_on_task_source_failure(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.routers.tasks._issues_cache", {}):
            with patch("sova.dashboard.routers.tasks.load_config", side_effect=RuntimeError("no config")):
                resp = await client.get("/api/tasks/issues")
        assert resp.status_code == 503
        assert resp.json() == {"detail": "Task source unavailable"}
        assert "no config" not in resp.text


@pytest.mark.usefixtures("setup_root_tmp")
class TestSetupRouterErrors:
    async def test_install_hides_exception_detail(self, client: AsyncClient, tmp_path: Path) -> None:
        with patch(
            "sova.cli.commands.project._install",
            new_callable=AsyncMock,
            side_effect=RuntimeError("secret path /etc/shadow"),
        ):
            resp = await client.post(
                "/api/setup/install",
                json={"project_path": str(tmp_path)},
            )
        assert resp.status_code == 500
        assert resp.json() == {"detail": "Internal error"}
        assert "/etc/shadow" not in resp.text

    async def test_install_reports_an_unreadable_registry_as_409_not_a_failed_install(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        from sova.config.registry import RegistryUnreadableError

        registry_file = tmp_path / "projects.json"
        with (
            patch("sova.cli.commands.project._install", new_callable=AsyncMock),
            patch("sova.dashboard.routers.setup.register_project", side_effect=RegistryUnreadableError(registry_file)),
        ):
            resp = await client.post("/api/setup/install", json={"project_path": str(tmp_path)})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert str(registry_file) in detail
        assert "Installation failed" not in detail

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

    async def test_jira_test_programming_error_is_a_fixed_500(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.routers.setup.setup_service.test_jira_connection",
            side_effect=TypeError("NoneType has no len()"),
        ):
            resp = await client.post(
                "/api/setup/jira/test",
                json={"base_url": "http://jira", "email": "a@b.com", "api_token": "tok"},
            )
        assert resp.status_code == 500
        assert "NoneType" not in resp.text


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
                "sova.dashboard.routers.spec._transition_issue",
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

    async def test_a_failed_tracker_transition_is_a_502_and_starts_no_agent(self, client: AsyncClient) -> None:
        from sova.dashboard.services import control_service as cs

        adapter = MagicMock()
        adapter.transition_state = AsyncMock(side_effect=RuntimeError("gh: token=abc rejected"))
        with (
            patch("sova.dashboard.routers.spec.spec_service.approve_spec", return_value={"status": "approved"}),
            patch.object(cs, "complete_awaiting_approval_by_issue", new_callable=AsyncMock, return_value=None),
            patch("sova.dashboard.routers.spec.load_config", return_value=MagicMock()),
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch.object(cs, "start_agent", new_callable=AsyncMock) as start,
        ):
            resp = await client.post("/api/spec/42/approve")

        assert resp.status_code == 502
        assert resp.json() == {"detail": "Failed to transition issue #42 to RESEARCHED"}
        assert "token" not in resp.text
        start.assert_not_called()

    async def test_an_unloadable_config_answers_by_the_one_mapping(self, client: AsyncClient) -> None:
        from sova.dashboard.services import control_service as cs

        with (
            patch("sova.dashboard.routers.spec.spec_service.approve_spec", return_value={"status": "approved"}),
            patch.object(cs, "complete_awaiting_approval_by_issue", new_callable=AsyncMock, return_value=None),
            patch("sova.dashboard.routers.spec.load_config", side_effect=FileNotFoundError("sova.toml")),
        ):
            resp = await client.post("/api/spec/42/approve")

        assert resp.status_code == 400
        assert resp.json() == {"detail": "Failed to load project config"}

    @pytest.mark.parametrize("action", ["approve", "revise", "skip"])
    async def test_an_agent_spawn_crash_reaches_the_catch_all_handler(self, client: AsyncClient, action: str) -> None:
        from sova.dashboard.services import control_service as cs

        with (
            patch("sova.dashboard.routers.spec.spec_service.approve_spec", return_value={"status": "approved"}),
            patch.object(cs, "complete_awaiting_approval_by_issue", new_callable=AsyncMock, return_value=None),
            patch("sova.dashboard.routers.spec._transition_issue", new_callable=AsyncMock),
            patch("sova.dashboard.routers.spec._issue_state", new=AsyncMock(return_value="researched")),
            patch.object(cs, "start_agent", new_callable=AsyncMock, side_effect=RuntimeError("spawn: /secret/path")),
        ):
            resp = await client.post(f"/api/spec/42/{action}")

        assert resp.status_code == 500
        assert resp.json() == {"detail": "Internal error"}


class TestPrBackfillConfigErrors:
    """``POST /prs/metrics/backfill`` loads the config through ``load_config_or_http()``, not a route mapping."""

    @pytest.mark.parametrize(
        ("exc", "status", "detail"),
        [
            (FileNotFoundError("sova.toml"), 400, "Failed to load project config"),
            (OSError("disk"), 503, "Failed to load configuration"),
            (AttributeError("bug in the loader"), 500, "Internal error"),
        ],
    )
    async def test_config_failures_by_cause(
        self, client: AsyncClient, exc: Exception, status: int, detail: str
    ) -> None:
        with patch("sova.dashboard.routers.prs.load_config", side_effect=exc):
            resp = await client.post("/api/prs/metrics/backfill", headers=_csrf_request_headers(client))

        assert resp.status_code == status
        assert resp.json() == {"detail": detail}


class TestSettingsRouterErrors:
    async def test_update_config_error_chains_exception(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.routers.settings.settings_service.update_config",
            side_effect=RuntimeError("toml parse error"),
        ):
            resp = await client.post(
                "/api/settings/config",
                json={"key": "base_branch", "value": "main"},
                headers=_csrf_request_headers(client),
            )
        assert resp.status_code == 500
        assert resp.json() == {"detail": "Internal error"}
        # Same-origin JS reads this body: it is JSON, with no exception text in it.
        assert resp.headers["content-type"].startswith("application/json")
        assert "toml parse error" not in resp.text

    async def test_audit_labels_not_github_returns_400(self, client: AsyncClient) -> None:
        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "jira"
        with patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg):
            resp = await client.get("/api/settings/labels/audit")
        assert resp.status_code == 400
        assert "GitHub-only" in resp.json()["detail"]

    async def test_audit_labels_no_github_repo_returns_400(self, client: AsyncClient) -> None:
        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = ""
        with patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg):
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
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
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
        assert resp.status_code == 502
        assert resp.json() == {"detail": "GitHub CLI not available or not authenticated"}

    async def test_audit_labels_json_parse_error(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
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
        assert resp.status_code == 502
        assert resp.json() == {"detail": "Failed to parse GitHub CLI output"}

    @pytest.mark.parametrize(("method", "path"), [("get", "/audit"), ("post", "/create")])
    async def test_label_failures_never_carry_gh_stderr(self, client: AsyncClient, method: str, path: str) -> None:
        """A `gh label list` failure answers a fixed 502 detail: the stderr (with its token) only reaches the log."""
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value={}),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                return_value=ShellResult(returncode=1, stdout="", stderr="HTTP 500: token ghp_SECRET rejected"),
            ),
        ):
            resp = await getattr(client, method)(f"/api/settings/labels{path}")
        assert resp.status_code == 502
        assert resp.json() == {"detail": "Failed to fetch labels"}
        assert "ghp_SECRET" not in resp.text

    async def test_audit_labels_success_with_missing(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
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
        with patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 400
        assert "GitHub-only" in resp.json()["detail"]

    async def test_create_labels_fetch_error_returns_502(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
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
        assert resp.status_code == 502
        assert resp.json() == {"detail": "Failed to fetch labels"}

    async def test_create_labels_no_missing_labels(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        # Return all required labels as existing
        from sova.adapters.label_catalog import REQUIRED_LABELS

        all_labels = [{"name": label["name"]} for label in REQUIRED_LABELS]

        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
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
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
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
                "sova.dashboard.services.github_labels_service.REQUIRED_LABELS",
                [{"name": "test:label", "color": "ZZZZZZ", "description": "Test"}],
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 502
        data = resp.json()["detail"]
        assert data["created"] == 0
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
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
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
                "sova.dashboard.services.github_labels_service.REQUIRED_LABELS",
                [{"name": "test:label", "color": "a2eeef", "description": "Test"}],
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 502
        data = resp.json()["detail"]
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
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
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
                "sova.dashboard.services.github_labels_service.REQUIRED_LABELS",
                failing_labels,
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 502
        data = resp.json()["detail"]
        assert data["created"] == 0
        assert len(data["errors"]) == 10
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
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
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
                "sova.dashboard.services.github_labels_service.REQUIRED_LABELS",
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
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
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
                "sova.dashboard.services.github_labels_service.REQUIRED_LABELS",
                test_labels,
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 502
        data = resp.json()["detail"]
        assert data["message"] == "Some labels could not be created"
        assert data["created"] == 1
        assert len(data["errors"]) == 1
        assert "test:fail" in data["errors"][0]
        assert "API error" not in resp.text

    async def test_create_labels_error_never_echoes_gh_stderr(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        test_labels = [{"name": "test:label", "color": "a2eeef", "description": "Test"}]
        long_error = "x" * 300

        async def run_mock(*args, **kwargs):
            if "label" in args and "list" in args:
                return ShellResult(returncode=0, stdout="[]", stderr="")
            return ShellResult(returncode=1, stdout="", stderr=long_error)

        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
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
                "sova.dashboard.services.github_labels_service.REQUIRED_LABELS",
                test_labels,
            ),
        ):
            resp = await client.post("/api/settings/labels/create")
        assert resp.status_code == 502
        data = resp.json()["detail"]
        assert data["errors"] == ["test:label: Failed to create the label"]
        assert "xxx" not in resp.text

    async def test_create_labels_spawn_failure_mid_batch_is_a_per_label_error(self, client: AsyncClient) -> None:
        """A gh that cannot be spawned for one label fails that label, not the whole request with a 500."""
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"
        test_labels = [
            {"name": "test:ok", "color": "a2eeef", "description": "ok"},
            {"name": "test:gone", "color": "d4c5f9", "description": "gone"},
        ]

        async def run_mock(*args, **kwargs):
            if "label" in args and "list" in args:
                return ShellResult(returncode=0, stdout="[]", stderr="")
            if "test:gone" in args:
                raise FileNotFoundError("gh")
            return ShellResult(returncode=0, stdout="", stderr="")

        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value={}),
            patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=run_mock),
            patch("sova.dashboard.services.github_labels_service.REQUIRED_LABELS", test_labels),
        ):
            resp = await client.post("/api/settings/labels/create")

        assert resp.status_code == 502
        data = resp.json()["detail"]
        assert data["created"] == 1
        assert data["errors"] == ["test:gone: GitHub CLI not available"]

    async def test_audit_tolerates_a_label_entry_without_a_name(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"

        async def run_mock(*args, **kwargs):
            return ShellResult(returncode=0, stdout='[{"color": "fff"}, {"name": "test:one"}]', stderr="")

        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=mock_cfg),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value={}),
            patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=run_mock),
            patch("sova.dashboard.services.github_labels_service.REQUIRED_LABELS", [{"name": "test:one"}]),
        ):
            resp = await client.get("/api/settings/labels/audit")

        assert resp.status_code == 200
        assert resp.json()["missing"] == []

    @pytest.mark.parametrize(
        ("exc", "status"),
        [
            pytest.param(FileNotFoundError("sova.toml"), 400, id="missing-file"),
            pytest.param(OSError("disk"), 503, id="io"),
        ],
    )
    async def test_label_routes_categorize_an_unloadable_config(
        self, client: AsyncClient, exc: Exception, status: int
    ) -> None:
        with patch("sova.dashboard.routers.settings.load_config", side_effect=exc):
            audit = await client.get("/api/settings/labels/audit")
            create = await client.post("/api/settings/labels/create")

        assert (audit.status_code, create.status_code) == (status, status)


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
                return_value={"error": "conflict", "code": "no_free_slot", "detail": "Slot full"},
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
                return_value={"status": "error", "code": "issue_conflict", "detail": "Issue already running"},
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
                return_value={"status": "error", "code": "no_free_slot", "detail": "No agent slots available"},
            ),
        ):
            resp = await client.post("/api/handoff/execute", json={"action_id": "integrate"})
        assert resp.status_code == 409
        assert "No agent slots available" in resp.json()["detail"]


class TestImportErrorHandler:
    """A stale-code ImportError escaping a route becomes a 503 that says to restart (issue #1409)."""

    async def _get_with_raise(self, exc: Exception) -> tuple[int, dict]:
        from sova.dashboard.app import create_app

        app = create_app(multi_project=False)

        @app.get("/api/_boom")
        async def boom() -> None:
            raise exc

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as ac:
            resp = await ac.get("/api/_boom")
        return resp.status_code, resp.json()

    async def test_import_error_returns_503_with_restart_detail(self) -> None:
        status, body = await self._get_with_raise(ImportError("cannot import name 'x' from 'sova.y'"))
        assert status == 503
        assert "restart" in body["detail"].lower()

    async def test_stale_import_of_a_name_missing_from_a_loaded_module_returns_503(self) -> None:
        """The incident shape: a route's lazy import asks a cached module for a name it lacks."""
        from sova.dashboard.app import create_app

        app = create_app(multi_project=False)

        @app.get("/api/_stale")
        async def stale() -> None:
            from sova.utils.version import name_added_after_startup  # noqa: F401

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as ac:
            resp = await ac.get("/api/_stale")
        assert resp.status_code == 503
        assert "restart the server" in resp.json()["detail"]

    async def test_module_not_found_error_is_covered_too(self) -> None:
        status, body = await self._get_with_raise(ModuleNotFoundError("No module named 'sova.new'"))
        assert status == 503
        assert "restart" in body["detail"].lower()

    async def test_body_does_not_leak_the_shas_or_the_exception_text(self) -> None:
        from sova.utils.version import CodeStaleness

        stale = CodeStaleness("a" * 40, "b" * 40, True)
        with patch("sova.dashboard.app.get_code_staleness", return_value=stale):
            _, body = await self._get_with_raise(ImportError("secret-module-name"))
        assert "a" * 40 not in body["detail"]
        assert "b" * 40 not in body["detail"]
        assert "secret-module-name" not in body["detail"]

    async def test_other_exceptions_keep_the_generic_500(self) -> None:
        from sova.dashboard.app import create_app

        app = create_app(multi_project=False)

        @app.get("/api/_boom")
        async def boom() -> None:
            raise RuntimeError("x")

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as ac:
            resp = await ac.get("/api/_boom")
        assert resp.status_code == 500


class TestCatchAllHandler:
    """The app-level catch-all: one place logs unexpected route failures and answers with a fixed 500."""

    @pytest.fixture
    async def handler_client(self) -> AsyncIterator[AsyncClient]:
        # A fresh app, so the test routes below never reach the module's shared ``dashboard_app``.
        from sova.dashboard.app import create_app

        app = create_app(multi_project=False)

        @app.get("/api/_test/boom")
        async def _boom() -> dict:
            raise RuntimeError("secret internal detail")

        @app.post("/api/_test/boom-post")
        async def _boom_post() -> dict:
            raise RuntimeError("secret internal detail")

        @app.get("/api/_test/teapot")
        async def _teapot() -> dict:
            raise HTTPException(status_code=418, detail="short and stout")

        # Starlette's ServerErrorMiddleware re-raises after the handler answers, so the client must not re-raise.
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            ac.headers.update(_csrf_request_headers(ac))
            yield ac

    async def test_unexpected_exception_returns_fixed_body_without_exception_text(
        self, handler_client: AsyncClient
    ) -> None:
        resp = await handler_client.get("/api/_test/boom")

        assert resp.status_code == 500
        assert resp.json() == {"detail": "Internal error"}
        assert "secret internal detail" not in resp.text

    async def test_a_post_through_the_csrf_guard_gets_the_same_fixed_500(self, handler_client: AsyncClient) -> None:
        resp = await handler_client.post("/api/_test/boom-post", json={})

        assert resp.status_code == 500
        assert resp.json() == {"detail": "Internal error"}
        assert "secret internal detail" not in resp.text

    async def test_a_post_without_the_csrf_token_is_still_rejected_before_any_route_runs(self) -> None:
        from sova.dashboard.app import create_app

        app = create_app(multi_project=False)
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/settings/models/pin", json={}, headers={"Origin": "http://evil.example"})
        assert resp.status_code == 403

    async def test_the_500_sits_outside_the_user_middlewares_so_it_has_no_security_headers(
        self, handler_client: AsyncClient
    ) -> None:
        """Pinned, not endorsed: a JSON error body needs none, and a change here should be deliberate."""
        error = await handler_client.get("/api/_test/boom")
        handled = await handler_client.get("/api/_test/teapot")

        assert "x-frame-options" not in error.headers
        assert "content-security-policy" not in error.headers
        assert handled.headers["x-frame-options"] == "DENY"

    async def test_unexpected_exception_is_logged_with_a_traceback_method_and_route(
        self, handler_client: AsyncClient
    ) -> None:
        with patch("sova.dashboard.app.log") as mock_log:
            await handler_client.get("/api/_test/boom")

        mock_log.error.assert_called_once()
        args, kwargs = mock_log.error.call_args
        assert args == ("dashboard.unhandled_exception",)
        assert kwargs["method"] == "GET"
        assert kwargs["route"] == "/api/_test/boom"
        # The handler is ``async``, so it runs inside the middleware's ``except`` block and ``True`` logs the traceback.
        assert kwargs["exc_info"] is True

    async def test_http_exception_keeps_its_own_status_and_detail(self, handler_client: AsyncClient) -> None:
        with patch("sova.dashboard.app.log") as mock_log:
            resp = await handler_client.get("/api/_test/teapot")

        assert resp.status_code == 418
        assert resp.json() == {"detail": "short and stout"}
        mock_log.error.assert_not_called()


class TestFormerlyOkWithErrorEndpoints:
    """Every route that used to answer 200 with an error body now answers a status, with a fixed detail."""

    async def test_propose_issue_tracker_failure_is_a_502(self, client: AsyncClient) -> None:
        with (
            patch("sova.adapters.github.GitHubAdapter") as adapter_cls,
            patch("sova.config.context.get_project_dir", return_value=None),
        ):
            adapter_cls.return_value.create_issue = AsyncMock(side_effect=RuntimeError("gh auth: not logged in"))
            resp = await client.post(
                "/api/fleet-insights/propose-issue", json={"title": "t", "body": "b", "labels": []}
            )

        assert resp.status_code == 502
        assert resp.json() == {"detail": "Failed to create the issue on the tracker"}
        assert "gh auth" not in resp.text

    async def test_create_issues_partial_failure_is_a_502_whose_results_keep_what_was_created(
        self, client: AsyncClient
    ) -> None:
        from sova.adapters.base import Task, TaskState

        calls = 0

        async def create_issue(title: str, body: str = "", labels=None, **_kw) -> Task:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Rate limited: token ghp_SECRET")
            return Task(id=str(calls), title=title, state=TaskState.BACKLOG, url=f"https://t.example/{calls}")

        adapter = AsyncMock()
        adapter.create_issue.side_effect = create_issue
        with patch("sova.adapters.create_adapter", return_value=adapter):
            resp = await client.post(
                "/api/agents/planner/create-issues",
                json={"tasks": [{"title": f"T{i}", "description": "d"} for i in range(3)]},
            )

        assert resp.status_code == 502
        results = resp.json()["detail"]["results"]
        assert [(r["ok"], r.get("number")) for r in results] == [(True, "1"), (False, None), (True, "3")]
        # Every entry, created or failed, carries the title it was submitted with, and a created one its url.
        assert [r["title"] for r in results] == ["T0", "T1", "T2"]
        assert [r.get("url") for r in results] == ["https://t.example/1", None, "https://t.example/3"]
        # A failed entry carries a fixed reason, never the tracker's exception text.
        assert results[1]["error"] == "Issue creation failed"
        assert "ghp_SECRET" not in resp.text

    async def test_a_created_issue_reports_the_submitted_title_not_the_trackers_echo(self, client: AsyncClient) -> None:
        from sova.adapters.base import Task, TaskState

        adapter = AsyncMock()
        adapter.create_issue.return_value = Task(
            id="7", title="[prefixed] T0", state=TaskState.BACKLOG, url="https://t.example/7"
        )
        with patch("sova.adapters.create_adapter", return_value=adapter):
            resp = await client.post(
                "/api/agents/planner/create-issues", json={"tasks": [{"title": "T0", "description": "d"}]}
            )

        assert resp.status_code == 200
        assert [c["title"] for c in resp.json()["created"]] == ["T0"]

    @pytest.mark.parametrize(
        ("url", "label"),
        [
            pytest.param("/api/settings/llm/test-connection", "LLM provider", id="llm"),
            pytest.param("/api/settings/agent-runtime/test-connection", "Agent runtime", id="runtime"),
        ],
    )
    async def test_a_connection_probe_that_raises_is_a_502_naming_the_component(
        self, client: AsyncClient, url: str, label: str
    ) -> None:
        probe = MagicMock()
        probe.check_available = AsyncMock(side_effect=RuntimeError("socket: SECRET_HOST refused"))
        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=MagicMock()),
            patch("sova.llm.provider.create_provider", return_value=probe),
            patch("sova.ipc.runtime.create_runtime", return_value=probe),
        ):
            resp = await client.post(url)

        assert resp.status_code == 502
        assert resp.json() == {"detail": f"{label} unreachable"}
        assert "SECRET_HOST" not in resp.text

    async def test_a_component_reporting_itself_unavailable_is_still_a_200(self, client: AsyncClient) -> None:
        probe = MagicMock()
        probe.check_available = AsyncMock(return_value=(False, "no API key"))
        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=MagicMock()),
            patch("sova.llm.provider.create_provider", return_value=probe),
        ):
            resp = await client.post("/api/settings/llm/test-connection")

        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    async def test_labels_audit_never_returns_gh_stderr(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        cfg = MagicMock()
        cfg.task_source.type = "github"
        cfg.github_repo = "owner/repo"
        cfg.github_user = "testuser"
        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=cfg),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value={}),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                return_value=ShellResult(returncode=1, stdout="", stderr="STDERR_MARKER /Users/x/.config/gh/hosts.yml"),
            ),
        ):
            audit = await client.get("/api/settings/labels/audit")
            create = await client.post("/api/settings/labels/create")

        for resp in (audit, create):
            assert resp.status_code == 502
            assert resp.json() == {"detail": "Failed to fetch labels"}
            assert "STDERR_MARKER" not in resp.text

    async def test_label_creation_failures_return_a_fixed_reason_not_gh_stderr(self, client: AsyncClient) -> None:
        from sova.utils.shell import ShellResult

        cfg = MagicMock()
        cfg.task_source.type = "github"
        cfg.github_repo = "owner/repo"
        cfg.github_user = "testuser"

        async def run(*args: str, **_kw) -> ShellResult:
            if "list" in args:
                return ShellResult(returncode=0, stdout="[]", stderr="")
            return ShellResult(returncode=1, stdout="", stderr="HTTP 500 STDERR_MARKER owner/private-repo")

        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=cfg),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value={}),
            patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=run),
            patch(
                "sova.dashboard.services.github_labels_service.REQUIRED_LABELS",
                [{"name": "test:label", "color": "a2eeef", "description": "Test"}],
            ),
        ):
            resp = await client.post("/api/settings/labels/create")

        assert resp.status_code == 502
        assert resp.json()["detail"]["errors"] == ["test:label: Failed to create the label"]
        assert "STDERR_MARKER" not in resp.text

    async def test_plan_approve_partial_failure_is_a_502_that_keeps_every_result_and_consumes_the_plan(
        self, client: AsyncClient
    ) -> None:
        from sova.dashboard.services import supervisor_service
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        decisions = [
            ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher"),
            ProgressionDecision(issue_number=43, action=ProgressionAction.SPAWN_DEVELOPER, role="developer"),
        ]
        supervisor_service.set_pending_plan(decisions, project_slug="test/repo")
        results = [
            {"ok": True, "run_id": 1},
            {"ok": False, "error": "Decision failed", "issue_number": 43},
        ]
        try:
            with (
                patch("sova.dashboard.routers.supervisor.load_config", return_value=MagicMock(github_repo="test/repo")),
                patch.object(supervisor_service, "execute_plan_decisions", AsyncMock(return_value=results)),
            ):
                resp = await client.post("/api/supervisor/plan/approve", json={})

            assert resp.status_code == 502
            detail = resp.json()["detail"]
            assert detail["message"] == "Some approved decisions failed to execute"
            assert detail["results"] == results
            assert supervisor_service.get_pending_plan("test/repo") == []
        finally:
            supervisor_service.set_pending_plan([], project_slug="test/repo")

    async def test_plan_approve_failure_detail_never_carries_exception_text(self, client: AsyncClient) -> None:
        from sova.dashboard.services import supervisor_service
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        decisions = [ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_DEVELOPER, role="developer")]
        supervisor_service.set_pending_plan(decisions, project_slug="test/repo")
        try:
            with (
                patch("sova.dashboard.routers.supervisor.load_config", return_value=MagicMock(github_repo="test/repo")),
                patch(
                    "sova.dashboard.services.supervisor_service.load_config",
                    return_value=MagicMock(github_repo="test/repo"),
                ),
                patch("sova.adapters.create_adapter", return_value=MagicMock()),
                patch("sova.db.session.get_session_factory", new_callable=AsyncMock, return_value=MagicMock()),
                patch("sova.supervisor.progression.TaskProgressionEngine.__init__", return_value=None),
                patch(
                    "sova.supervisor.progression.TaskProgressionEngine.execute_decision",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("token ghp_SECRET in url"),
                ),
            ):
                resp = await client.post("/api/supervisor/plan/approve", json={})

            assert resp.status_code == 502
            assert resp.json()["detail"]["results"] == [{"ok": False, "error": "Decision failed", "issue_number": 42}]
            assert "ghp_SECRET" not in resp.text
        finally:
            supervisor_service.set_pending_plan([], project_slug="test/repo")

    async def test_a_rejected_setting_value_is_a_422_not_a_200_error_body(self, client: AsyncClient) -> None:
        resp = await client.post("/api/settings/config", json={"key": "supervisor.auto_triage", "value": "maybe"})

        assert resp.status_code == 422
        assert "error" not in resp.json()
        assert isinstance(resp.json()["detail"], str)

    async def test_an_unknown_setting_key_is_a_404(self, client: AsyncClient) -> None:
        resp = await client.post("/api/settings/config", json={"key": "no.such.setting", "value": "x"})

        assert resp.status_code == 404
        assert "no.such.setting" in resp.json()["detail"]

    @pytest.mark.parametrize(
        ("url", "factory"),
        [
            pytest.param("/api/settings/llm/test-connection", "sova.llm.provider.create_provider", id="llm"),
            pytest.param(
                "/api/settings/agent-runtime/test-connection", "sova.ipc.runtime.create_runtime", id="runtime"
            ),
        ],
    )
    async def test_a_misconfigured_component_shows_what_to_fix(
        self, client: AsyncClient, url: str, factory: str
    ) -> None:
        from sova.llm.errors import ComponentMisconfiguredError

        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=MagicMock()),
            patch(factory, side_effect=ComponentMisconfiguredError("Unknown thing: 'x'. Available: a, b")),
        ):
            resp = await client.post(url)

        assert resp.status_code == 400
        assert resp.json() == {"detail": "Unknown thing: 'x'. Available: a, b"}

    async def test_a_plain_value_error_from_a_factory_is_not_shown(self, client: AsyncClient) -> None:
        with (
            patch("sova.dashboard.routers.settings.load_config", return_value=MagicMock()),
            patch("sova.llm.provider.create_provider", side_effect=ValueError("/Users/x/secret/path")),
        ):
            resp = await client.post("/api/settings/llm/test-connection")

        assert resp.status_code == 502
        assert "secret" not in resp.text

    @pytest.mark.parametrize(
        ("error", "status"),
        [
            pytest.param(FileNotFoundError("sova.toml"), 400, id="missing-toml"),
            pytest.param(OSError("db down"), 503, id="io"),
            pytest.param(AttributeError("db down"), 500, id="loader-bug-reaches-the-catch-all"),
        ],
    )
    @pytest.mark.parametrize(
        ("method", "url"),
        [
            pytest.param("post", "/api/settings/llm/test-connection", id="llm-test"),
            pytest.param("post", "/api/settings/persona/open", id="persona-open"),
            pytest.param("get", "/api/settings/persona", id="persona"),
        ],
    )
    async def test_an_unloadable_config_has_one_mapping_on_every_settings_route(
        self, client: AsyncClient, method: str, url: str, error: Exception, status: int
    ) -> None:
        with patch("sova.dashboard.routers.settings.load_config", side_effect=error):
            resp = await getattr(client, method)(url)

        assert resp.status_code == status
        assert "db down" not in resp.text

    async def test_a_config_that_fails_validation_is_a_422_naming_the_fields(self, client: AsyncClient) -> None:
        from pydantic import ValidationError

        from sova.config.models import ProjectConfig

        try:
            ProjectConfig(max_parallel_agents="not-a-number")  # type: ignore[arg-type]
        except ValidationError as exc:
            error = exc
        with patch("sova.dashboard.routers.settings.load_config", side_effect=error):
            resp = await client.post("/api/settings/llm/test-connection")

        assert resp.status_code == 422
        assert resp.json()["detail"].startswith("Invalid configuration: max_parallel_agents")


class TestSettingsWidgetsAndModelMap:
    """Routes whose config or database failure has a deliberate answer (not the generic 500)."""

    async def test_the_cost_projection_widget_degrades_on_a_database_failure(self, client: AsyncClient) -> None:
        from sqlalchemy.exc import OperationalError

        with patch(
            "sova.dashboard.routers.settings.get_session",
            new_callable=AsyncMock,
            side_effect=OperationalError("select", {}, Exception("db is locked /secret")),
        ) as mock_session:
            resp = await client.get("/api/settings/llm/cost-projection")

        # The empty test database answers insufficient_data too, so only the call proves the failure path ran.
        mock_session.assert_awaited()
        assert resp.status_code == 200
        assert resp.json() == {"insufficient_data": True}
        assert "secret" not in resp.text

    async def test_the_model_map_of_an_invalid_config_is_a_422_naming_the_fields(self, client: AsyncClient) -> None:
        from pydantic import ValidationError

        from sova.config.models import ProjectConfig

        try:
            ProjectConfig(max_parallel_agents="not-a-number")  # type: ignore[arg-type]
        except ValidationError as exc:
            error = exc
        with patch("sova.dashboard.routers.settings.load_config", side_effect=error):
            resp = await client.get("/api/settings/models/map")

        assert resp.status_code == 422
        assert resp.json()["detail"].startswith("Invalid configuration: max_parallel_agents")

    async def test_pin_catalog_is_none_when_the_config_cannot_be_loaded(self) -> None:
        from sova.dashboard.services import settings_service

        with patch("sova.dashboard.services.settings_service.load_config", side_effect=OSError("db down")):
            assert await settings_service.pin_catalog(None) is None

    async def test_a_priority_the_tracker_lacks_is_a_400_with_the_typed_user_message(self, client: AsyncClient) -> None:
        from sova.adapters.base import PriorityUnavailableError

        adapter = AsyncMock()
        adapter.set_priority.side_effect = PriorityUnavailableError("Jira has no priority for 'blocker'")
        with (
            patch("sova.dashboard.routers.issues.load_config", return_value=MagicMock()),
            patch("sova.adapters.create_adapter", return_value=adapter),
        ):
            resp = await client.put("/api/issues/1/priority", json={"priority": "blocker"})

        assert resp.status_code == 400
        assert resp.json() == {"detail": "Jira has no priority for 'blocker'"}


class TestConfigHttpError:
    """``config_http_error()`` maps a config load failure by cause, and logs every branch with its traceback."""

    @staticmethod
    def _validation_error() -> Exception:
        from pydantic import ValidationError

        from sova.config.models import ProjectConfig

        try:
            ProjectConfig(max_parallel_agents="not-a-number")  # type: ignore[arg-type]
        except ValidationError as exc:
            return exc
        raise AssertionError("ProjectConfig accepted an invalid value")

    def test_a_validation_error_is_a_422_not_the_400_of_the_value_error_it_subclasses(self) -> None:
        from sova.dashboard.config_errors import config_http_error

        error = config_http_error(self._validation_error(), event="test.config")

        assert error.status_code == 422
        assert error.detail.startswith("Invalid configuration: max_parallel_agents")

    @pytest.mark.parametrize(
        ("exc", "status"),
        [
            pytest.param(ValueError("bad toml"), 400, id="value-error"),
            pytest.param(KeyError("section"), 400, id="key-error"),
            pytest.param(FileNotFoundError("sova.toml"), 400, id="file-not-found"),
            pytest.param(OSError("disk"), 503, id="os-error"),
        ],
    )
    def test_each_expected_failure_has_one_status_and_a_fixed_detail(self, exc: Exception, status: int) -> None:
        from sova.dashboard.config_errors import config_http_error

        error = config_http_error(exc, event="test.config")

        assert error.status_code == status
        assert str(exc) not in str(error.detail)

    def test_a_wrapped_validation_error_is_a_422(self) -> None:
        from sova.dashboard.config_errors import config_http_error

        try:
            raise ValueError("config invalid") from self._validation_error()
        except ValueError as exc:
            assert config_http_error(exc, event="test.config").status_code == 422

    def test_every_translated_branch_logs_the_exception_with_its_traceback(self) -> None:
        from sova.dashboard import config_errors

        for exc in (self._validation_error(), ValueError("x"), KeyError("k"), OSError("o")):
            with patch.object(config_errors, "log") as log:
                config_errors.config_http_error(exc, event="test.config")
            log.warning.assert_called_once_with("test.config", exc_info=True)

    def test_a_bug_in_the_loader_is_not_translated(self) -> None:
        from sova.dashboard.config_errors import load_config_or_http

        def loader() -> None:
            raise AttributeError("loader bug")

        with pytest.raises(AttributeError):
            load_config_or_http(loader, event="test.config")  # type: ignore[arg-type]


class TestNoExceptionTextInErrorBodies:
    """Exception text reaches a client through no route, service or handler under ``sova/dashboard/``.

    An AST walk, not a text grep, so every spelling is caught: ``detail=str(exc)``, ``detail=repr(exc)``, any
    f-string or ``%``/``+`` expression that holds a ``str()``/``repr()`` call or an exception-named variable,
    a name assigned from ``str()``/``repr()`` in the same function, and the same in a ``{"detail": ...}`` dict
    given to a response call.
    There is no exemption marker: a domain error that is safe to show exposes ``user_message`` and the route uses
    that, so the guarantee rests on the type. A ``{"status": "error", "detail": ...}`` payload a service returns
    in a 200 body is not an HTTP error answer and is outside this scan.
    """

    _EXCEPTION_NAMES = frozenset({"exc", "e", "err", "error", "ex", "exception"})
    # Attributes that carry tool or exception text; any other attribute (``exc.path``, ``exc.user_message``) does not.
    _TEXT_ATTRIBUTES = frozenset({"args", "stderr", "stdout", "output", "strerror"})
    _CONTAINERS = (ast.List, ast.Tuple, ast.Set)
    _COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp)

    @classmethod
    def _taints(cls, expr: ast.AST | None, text_names: set[str]) -> bool:
        """Whether *expr* is, or formats into itself, exception or tool text.

        Follows a slice (``str(exc)[:200]``), ``.format()``/``.join()``, an f-string, ``%``/``+``, a conditional, and
        a dict, list or comprehension holding any such value (the shape of ``{"errors": [f"{name}: {stderr}"]}``).
        """
        if expr is None:
            return False
        taints = lambda node: cls._taints(node, text_names)  # noqa: E731 (a one-line recursion helper)
        if isinstance(expr, ast.Call):
            if isinstance(expr.func, ast.Name) and expr.func.id in {"str", "repr"}:
                return True
            if isinstance(expr.func, ast.Attribute):
                # ``"{}".format(exc)``, ``"; ".join(...)``, ``text.strip()``: tainted when the receiver or an arg is.
                arguments = [*expr.args, *(k.value for k in expr.keywords)]
                return taints(expr.func.value) or any(taints(a) for a in arguments)
            return False
        if isinstance(expr, ast.Name):
            return expr.id in cls._EXCEPTION_NAMES or expr.id in text_names
        if isinstance(expr, ast.Attribute):
            return expr.attr in cls._TEXT_ATTRIBUTES
        if isinstance(expr, ast.Subscript):
            return taints(expr.value)
        if isinstance(expr, ast.JoinedStr):
            return any(isinstance(v, ast.FormattedValue) and taints(v.value) for v in expr.values)
        if isinstance(expr, ast.BinOp):
            return taints(expr.left) or taints(expr.right)
        if isinstance(expr, ast.BoolOp):
            return any(taints(v) for v in expr.values)
        if isinstance(expr, ast.IfExp):
            return taints(expr.body) or taints(expr.orelse)
        if isinstance(expr, ast.Dict):
            return any(taints(v) for v in expr.values)
        if isinstance(expr, cls._CONTAINERS):
            return any(taints(v) for v in expr.elts)
        if isinstance(expr, cls._COMPREHENSIONS):
            return taints(expr.elt)
        return False

    @classmethod
    def _detail_values(cls, tree: ast.AST) -> list[ast.AST]:
        """Every client-facing detail: a ``detail=`` keyword, or a ``"detail"`` key of a dict a response call gets.

        A dict counts as a call's positional argument or any keyword (``content={"detail": ...}`` of a
        ``JSONResponse``), so a response built with ``status_code=`` first is scanned too.
        """
        values: list[ast.AST] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            values += [kw.value for kw in node.keywords if kw.arg == "detail"]
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if isinstance(arg, ast.Dict):
                    values += [
                        v
                        for k, v in zip(arg.keys, arg.values, strict=True)
                        if isinstance(k, ast.Constant) and k.value == "detail"
                    ]
        return values

    @classmethod
    def _text_names(cls, scope: ast.AST) -> set[str]:
        """Names assigned in *scope* from a tainted expression, to a fixed point (``a = str(e)``, ``b = f"{a}"``)."""
        names: set[str] = set()
        assigns = [n for n in ast.walk(scope) if isinstance(n, ast.Assign)]
        while True:
            found = {
                target.id
                for node in assigns
                if cls._taints(node.value, names)
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            if found <= names:
                return names
            names |= found

    @classmethod
    def _offenders(cls, path: Path) -> list[int]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines: list[int] = []
        for scope in [tree, *[n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]]:
            text_names = cls._text_names(scope)
            lines += [v.lineno for v in cls._detail_values(scope) if cls._taints(v, text_names)]
        return sorted(set(lines))

    def test_no_dashboard_module_builds_a_detail_from_exception_text(self) -> None:
        root = Path(__file__).resolve().parent.parent / "sova" / "dashboard"
        offenders = [
            f"{path.relative_to(root)}:{n}" for path in sorted(root.rglob("*.py")) for n in self._offenders(path)
        ]

        assert offenders == []

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("raise HTTPException(400, detail=str(exc))", id="str"),
            pytest.param('raise HTTPException(400, detail=f"Failed: {exc}")', id="fstring-after-text"),
            pytest.param('raise HTTPException(400, detail=f"Failed: {e!r}")', id="fstring-repr"),
            pytest.param("def f():\n    m = str(exc)\n    raise HTTPException(400, detail=m)", id="via-name"),
            pytest.param('JSONResponse({"detail": str(exc)})', id="dict"),
            pytest.param("raise HTTPException(400, detail=str(exc)[:200])", id="sliced"),
            pytest.param("raise HTTPException(400, detail=exc.args[0])", id="args"),
            pytest.param('raise HTTPException(400, detail="{}".format(exc))', id="format"),
            pytest.param('raise HTTPException(400, detail="; ".join(str(x) for x in errs))', id="join"),
            pytest.param(
                'def f():\n    msg = f"failed: {exc}"\n    raise HTTPException(400, detail=msg)', id="fstring-via-name"
            ),
            pytest.param('JSONResponse(status_code=500, content={"detail": str(exc)})', id="content-keyword"),
            pytest.param(
                'raise HTTPException(502, detail={"errors": [f"{name}: {result.stderr}"]})', id="nested-stderr"
            ),
        ],
    )
    def test_the_scan_catches_every_spelling(self, tmp_path: Path, source: str) -> None:
        module = tmp_path / "mod.py"
        module.write_text(source, encoding="utf-8")

        assert self._offenders(module) != []

    def test_the_scan_allows_a_typed_user_message(self, tmp_path: Path) -> None:
        module = tmp_path / "mod.py"
        module.write_text(
            'raise HTTPException(400, detail=exc.user_message)\nHTTPException(502, detail="fixed")', "utf-8"
        )

        assert self._offenders(module) == []


class TestRoutersStayThin:
    """The three routers the route-logic move covered hold no query, subprocess or router-to-router import.

    The session rule (docs/database-guidelines.md, "Session Ownership") is checked from the services' side: the
    query service takes the session as its first parameter, the orchestration services never accept one.
    """

    _ROOT = Path(__file__).resolve().parent.parent / "sova" / "dashboard"
    _ROUTERS = ("settings.py", "supervisor.py", "agents.py")

    @classmethod
    def _violations(cls, name: str) -> list[str]:
        tree = ast.parse((cls._ROOT / "routers" / name).read_text(encoding="utf-8"))
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if called in {"select", "create_subprocess_exec"}:
                    found.append(f"{name}:{node.lineno} calls {called}()")
            elif isinstance(node, ast.ImportFrom | ast.Import):
                modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
                routers = [m for m in modules if m.startswith("sova.dashboard.routers")]
                found += [f"{name}:{node.lineno} imports {m}" for m in routers]
        return found

    @pytest.mark.parametrize("name", _ROUTERS)
    def test_the_router_has_no_query_subprocess_or_router_import(self, name: str) -> None:
        assert self._violations(name) == []

    def test_the_scan_flags_each_violation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "routers").mkdir()
        (tmp_path / "routers" / "x.py").write_text(
            "from sova.dashboard.routers.handoff import f\nselect(T)\nasyncio.create_subprocess_exec('ls')\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(type(self), "_ROOT", tmp_path)

        assert len(self._violations("x.py")) == 3

    @staticmethod
    def _functions(module: str) -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
        path = Path(__file__).resolve().parent.parent / "sova" / "dashboard" / "services" / module
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)]

    @staticmethod
    def _parameters(func: ast.AsyncFunctionDef | ast.FunctionDef) -> list[str]:
        return [a.arg for a in [*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs]]

    @pytest.mark.parametrize("module", ["agent_recovery.py", "agent_status.py"])
    def test_an_orchestration_service_never_accepts_a_session(self, module: str) -> None:
        offenders = [f.name for f in self._functions(module) if "session" in self._parameters(f)]

        assert offenders == []

    def test_the_query_service_takes_the_session_first(self) -> None:
        public = [f for f in self._functions("resource_service.py") if f.args.args and not f.name.startswith("_")]
        runs_queries = [f for f in public if "session" in self._parameters(f)]

        assert runs_queries, "resource_service no longer takes an injected session"
        assert [f.name for f in runs_queries if self._parameters(f)[0] != "session"] == []
