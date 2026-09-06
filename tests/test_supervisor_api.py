"""Tests for the supervisor dashboard API router and service."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError

from sova.dashboard.services.supervisor_service import get_decision_counts, get_recent_decisions
from sova.db.models import SupervisorDecision
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def seed_decisions():
    session = await get_session()
    async with session.begin():
        session.add_all(
            [
                SupervisorDecision(
                    component="progression",
                    event_type="decision",
                    action="spawn_developer",
                    issue_number="42",
                    detail="Dependencies met",
                    project_slug="test/repo",
                ),
                SupervisorDecision(
                    component="quota",
                    event_type="status",
                    action="ok",
                    detail="2/4 reviews in window",
                    project_slug="test/repo",
                ),
                SupervisorDecision(
                    component="health",
                    event_type="health",
                    action="error",
                    detail="Adapter check failed",
                    project_slug="test/repo",
                ),
            ]
        )


class TestSupervisorService:
    async def test_get_recent_decisions_empty(self) -> None:
        result = await get_recent_decisions(Path.cwd())
        assert result == []

    async def test_get_recent_decisions_returns_data(self, seed_decisions) -> None:
        result = await get_recent_decisions(Path.cwd())
        assert len(result) == 3
        # Newest first
        assert result[0]["component"] == "health"

    async def test_get_recent_decisions_filter_component(self, seed_decisions) -> None:
        result = await get_recent_decisions(Path.cwd(), component="quota")
        assert len(result) == 1
        assert result[0]["component"] == "quota"

    async def test_get_recent_decisions_filter_event_type(self, seed_decisions) -> None:
        result = await get_recent_decisions(Path.cwd(), event_type="health")
        assert len(result) == 1
        assert result[0]["event_type"] == "health"

    async def test_get_recent_decisions_limit(self, seed_decisions) -> None:
        result = await get_recent_decisions(Path.cwd(), limit=1)
        assert len(result) == 1

    async def test_get_decision_counts(self, seed_decisions) -> None:
        counts = await get_decision_counts(Path.cwd())
        assert counts["progression"] == 1
        assert counts["quota"] == 1
        assert counts["health"] == 1

    async def test_get_recent_decisions_filter_project_slug(self, seed_decisions) -> None:
        result = await get_recent_decisions(Path.cwd(), project_slug="test/repo")
        assert len(result) == 3
        result_other = await get_recent_decisions(Path.cwd(), project_slug="other/repo")
        assert len(result_other) == 0

    async def test_get_decision_counts_filter_project_slug(self, seed_decisions) -> None:
        counts = await get_decision_counts(Path.cwd(), project_slug="test/repo")
        assert counts["progression"] == 1
        counts_other = await get_decision_counts(Path.cwd(), project_slug="other/repo")
        assert counts_other == {}


class TestSupervisorRouter:
    @pytest.fixture
    def app(self):
        from sova.dashboard.app import create_app

        with patch("sova.dashboard.app.recover_stale_runs", new_callable=AsyncMock):
            with patch("sova.dashboard.app.list_projects", return_value={}):
                return create_app(project_dir=Path.cwd())

    async def test_ci_budget_no_repo(self, app) -> None:
        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value.github_repo = ""
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/ci-budget")
                assert resp.status_code == 200
                data = resp.json()
                assert data["total"] == 0
                assert data["warn"] is False
                assert data["block"] is False

    async def test_ci_budget_with_data(self, app) -> None:
        from sova.supervisor.ci_budget import CIBudget

        mock_tracker = MagicMock()
        mock_tracker.get_budget = AsyncMock(return_value=CIBudget(total=2000, used=1850, remaining=150, pct_used=92.5))
        mock_tracker._cache = {"owner/repo": (0.0, CIBudget(total=2000, used=1850, remaining=150, pct_used=92.5))}

        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value.github_repo = "owner/repo"
            mock_cfg.return_value.github_user = "user"
            mock_cfg.return_value.supervisor.ci_warn_minutes = 200
            mock_cfg.return_value.supervisor.ci_block_minutes = 50
            with patch("sova.supervisor.ci_budget.get_ci_budget_tracker", return_value=mock_tracker):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.get("/api/supervisor/ci-budget")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["total"] == 2000
                    assert data["used"] == 1850
                    assert data["remaining"] == 150
                    assert data["warn"] is True
                    assert data["block"] is False
                    assert data["cached"] is True

    async def test_ci_budget_block_state(self, app) -> None:
        from sova.supervisor.ci_budget import CIBudget

        mock_tracker = MagicMock()
        mock_tracker.get_budget = AsyncMock(return_value=CIBudget(total=2000, used=1970, remaining=30, pct_used=98.5))
        mock_tracker._cache = {"owner/repo": (0.0, CIBudget(total=2000, used=1970, remaining=30, pct_used=98.5))}

        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value.github_repo = "owner/repo"
            mock_cfg.return_value.github_user = "user"
            mock_cfg.return_value.supervisor.ci_warn_minutes = 200
            mock_cfg.return_value.supervisor.ci_block_minutes = 50
            with patch("sova.supervisor.ci_budget.get_ci_budget_tracker", return_value=mock_tracker):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.get("/api/supervisor/ci-budget")
                    data = resp.json()
                    assert data["warn"] is True
                    assert data["block"] is True

    async def test_ci_budget_exception_returns_zero(self, app) -> None:
        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value.github_repo = "owner/repo"
            mock_cfg.return_value.github_user = "user"
            with patch(
                "sova.supervisor.ci_budget.get_ci_budget_tracker",
                side_effect=RuntimeError("connection failed"),
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.get("/api/supervisor/ci-budget")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["total"] == 0
                    assert data["used"] == 0
                    assert data["remaining"] == 0
                    assert data["warn"] is False
                    assert data["block"] is False
                    assert data["cached"] is False

    async def test_ci_budget_zero_from_api(self, app) -> None:
        from sova.supervisor.ci_budget import CIBudget

        mock_tracker = MagicMock()
        mock_tracker.get_budget = AsyncMock(return_value=CIBudget(total=0, used=0, remaining=0, pct_used=0.0))
        mock_tracker._cache = {}

        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value.github_repo = "owner/repo"
            mock_cfg.return_value.github_user = "user"
            mock_cfg.return_value.supervisor.ci_warn_minutes = 200
            mock_cfg.return_value.supervisor.ci_block_minutes = 50
            with patch("sova.supervisor.ci_budget.get_ci_budget_tracker", return_value=mock_tracker):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.get("/api/supervisor/ci-budget")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["total"] == 0
                    assert data["warn"] is False
                    assert data["block"] is False

    async def test_get_status_no_daemon(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/supervisor/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["enabled"] is False
            assert data["running"] is False

    async def test_get_decisions_empty(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/supervisor/decisions")
            assert resp.status_code == 200
            assert resp.json()["decisions"] == []

    async def test_poll_without_daemon_returns_404(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/supervisor/poll")
            assert resp.status_code == 404

    async def test_supervisor_page_loads(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/supervisor")
            assert resp.status_code == 200
            assert "Supervisor" in resp.text

    async def test_get_status_with_daemon(self, app) -> None:
        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_daemon = MagicMock()
        mock_daemon.get_status.return_value = {
            "enabled": True,
            "running": True,
            "poll_interval_seconds": 60,
            "log_retention_days": 14,
            "project_dir": str(Path.cwd()),
        }
        project_dir = Path.cwd().resolve()
        set_daemon_registry({str(project_dir): mock_daemon})

        try:
            with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.get("/api/supervisor/status")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["enabled"] is True
                    assert data["running"] is True
        finally:
            set_daemon_registry({})

    async def test_trigger_poll_with_daemon(self, app) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_daemon = MagicMock()
        mock_daemon.poll_once = AsyncMock(return_value={"progression": {"decisions": 0}})
        project_dir = Path.cwd().resolve()
        set_daemon_registry({str(project_dir): mock_daemon})

        try:
            with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.post("/api/supervisor/poll")
                    assert resp.status_code == 202
                    assert resp.json()["status"] == "accepted"
        finally:
            set_daemon_registry({})

    async def test_stop_supervisor_no_project_no_daemon_returns_404(self, app) -> None:
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/stop")
        assert resp.status_code == 404

    async def test_stop_supervisor_no_daemon_returns_404(self, app) -> None:
        project_dir = Path.cwd().resolve()
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/stop")
        assert resp.status_code == 404

    async def test_stop_supervisor_success(self, app) -> None:
        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_daemon = MagicMock()
        mock_daemon.running = True
        mock_daemon.stop = AsyncMock()
        project_dir = Path.cwd().resolve()
        set_daemon_registry({str(project_dir): mock_daemon})

        try:
            with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.post("/api/supervisor/stop")
            assert resp.status_code == 200
            data = resp.json()
            assert data["stopped"] is True
            assert data["running"] is False
            mock_daemon.stop.assert_awaited_once()
        finally:
            set_daemon_registry({})

    async def test_get_counts_empty(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/supervisor/counts")
            assert resp.status_code == 200
            assert "counts" in resp.json()

    async def test_get_counts_with_data(self, app, seed_decisions) -> None:
        mock_cfg = MagicMock()
        mock_cfg.github_repo = "test/repo"
        with patch("sova.config.loader.load_config", return_value=mock_cfg):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/counts")
                assert resp.status_code == 200
                counts = resp.json()["counts"]
                assert counts.get("progression") == 1
                assert counts.get("quota") == 1

    async def test_get_decisions_with_filters(self, app, seed_decisions) -> None:
        mock_cfg = MagicMock()
        mock_cfg.github_repo = "test/repo"
        with patch("sova.config.loader.load_config", return_value=mock_cfg):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/decisions?component=progression&limit=10")
                assert resp.status_code == 200
                data = resp.json()["decisions"]
                assert all(d["component"] == "progression" for d in data)

    async def test_start_supervisor_disabled_in_config(self, app) -> None:
        """POST /supervisor/start returns 409 when supervisor.enabled is false."""
        mock_cfg = MagicMock()
        mock_cfg.supervisor.enabled = False
        project_dir = Path.cwd().resolve()
        with (
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/start")
                assert resp.status_code == 409

    async def test_start_supervisor_already_running(self, app) -> None:
        """POST /supervisor/start returns started=False when daemon is already running."""
        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_daemon = MagicMock()
        mock_daemon.running = True
        mock_daemon.get_status.return_value = {
            "enabled": True,
            "running": True,
            "poll_interval_seconds": 120,
            "log_retention_days": 7,
            "project_dir": str(Path.cwd()),
        }
        project_dir = Path.cwd().resolve()
        set_daemon_registry({str(project_dir): mock_daemon})

        try:
            with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.post("/api/supervisor/start")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["started"] is False
                    assert data["reason"] == "already running"
        finally:
            set_daemon_registry({})

    async def test_start_supervisor_success(self, app) -> None:
        """POST /supervisor/start creates and starts a new daemon when config has it enabled."""
        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_cfg = MagicMock()
        mock_cfg.supervisor.enabled = True
        mock_daemon = MagicMock()
        mock_daemon.running = False
        mock_daemon.get_status.return_value = {
            "enabled": True,
            "running": True,
            "poll_interval_seconds": 120,
            "log_retention_days": 7,
            "project_dir": str(Path.cwd()),
        }
        project_dir = Path.cwd().resolve()
        set_daemon_registry({})

        try:
            with (
                patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
                patch("sova.config.loader.load_config", return_value=mock_cfg),
                patch("sova.db.session.get_session_factory", new_callable=AsyncMock, return_value=MagicMock()),
                patch("sova.supervisor.daemon.SupervisorDaemon", return_value=mock_daemon),
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.post("/api/supervisor/start")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["started"] is True
                    assert data["running"] is True
                    mock_daemon.start.assert_called_once()
        finally:
            set_daemon_registry({})

    async def test_start_supervisor_no_project_context_falls_back(self, app) -> None:
        """POST /supervisor/start loads cwd config when context is unset."""
        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_cfg = MagicMock()
        mock_cfg.supervisor.enabled = False
        set_daemon_registry({})
        try:
            with (
                patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=None),
                patch("sova.config.loader.load_config", return_value=mock_cfg) as mock_load,
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.post("/api/supervisor/start")
            assert resp.status_code == 409
            mock_load.assert_called_once_with(Path.cwd().resolve())
        finally:
            set_daemon_registry({})

    async def test_get_daemon_single_registry_entry_fallback(self, app) -> None:
        """_get_daemon returns sole daemon when project_dir is None and one daemon registered."""
        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_daemon = MagicMock()
        mock_daemon.get_status.return_value = {"enabled": True, "running": True}
        set_daemon_registry({"/some/project": mock_daemon})
        try:
            with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=None):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.get("/api/supervisor/status")
                    assert resp.status_code == 200
                    assert resp.json()["running"] is True
        finally:
            set_daemon_registry({})

    async def test_resolve_project_dir_single_registry_fallback(self, app) -> None:
        """_resolve_project_dir returns sole registry key when context is unset."""
        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_cfg = MagicMock()
        mock_cfg.supervisor.enabled = False
        stale_daemon = MagicMock()
        stale_daemon.running = False
        set_daemon_registry({"/registered/project": stale_daemon})
        try:
            with (
                patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=None),
                patch("sova.config.loader.load_config", return_value=mock_cfg) as mock_load,
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.post("/api/supervisor/start")
            assert resp.status_code == 409
            mock_load.assert_called_once_with(Path("/registered/project"))
        finally:
            set_daemon_registry({})

    async def test_resolve_project_dir_cwd_fallback(self, app) -> None:
        """_resolve_project_dir falls back to cwd when registry is empty and agent_pool has no default."""
        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_cfg = MagicMock()
        mock_cfg.supervisor.enabled = False
        set_daemon_registry({})
        try:
            with (
                patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=None),
                patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=None),
                patch("sova.config.loader.load_config", return_value=mock_cfg) as mock_load,
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.post("/api/supervisor/start")
            assert resp.status_code == 409
            mock_load.assert_called_once_with(Path.cwd().resolve())
        finally:
            set_daemon_registry({})

    async def test_get_decisions_load_config_error(self, app) -> None:
        """GET /supervisor/decisions falls back to project_slug=None when load_config raises."""
        with patch("sova.config.loader.load_config", side_effect=Exception("no config")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/decisions")
                assert resp.status_code == 200
                assert resp.json()["decisions"] == []

    async def test_get_counts_load_config_error(self, app) -> None:
        """GET /supervisor/counts falls back to project_slug=None when load_config raises."""
        with patch("sova.config.loader.load_config", side_effect=Exception("no config")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/counts")
                assert resp.status_code == 200
                assert "counts" in resp.json()


class TestSupervisorPlan:
    """Tests for the supervisor approval plan endpoints and service."""

    @pytest.fixture
    def app(self):
        from sova.dashboard.app import create_app

        with patch("sova.dashboard.app.recover_stale_runs", new_callable=AsyncMock):
            with patch("sova.dashboard.app.list_projects", return_value={}):
                return create_app(project_dir=Path.cwd())

    @pytest.fixture(autouse=True)
    def clear_plan(self):
        """Reset the in-memory plan before and after each test."""
        from sova.dashboard.services.supervisor_service import set_pending_plan

        set_pending_plan([], project_slug="test/repo")
        yield
        set_pending_plan([], project_slug="test/repo")

    async def test_get_plan_empty(self, app) -> None:
        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(github_repo="test/repo")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/plan")
        assert resp.status_code == 200
        assert resp.json()["pending"] == []

    async def test_get_plan_with_decisions(self, app) -> None:
        from sova.dashboard.services.supervisor_service import set_pending_plan
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        decisions = [
            ProgressionDecision(
                issue_number=42,
                action=ProgressionAction.SPAWN_RESEARCHER,
                role="researcher",
                reason="Dependencies met",
            ),
            ProgressionDecision(
                issue_number=43,
                action=ProgressionAction.SPAWN_DEVELOPER,
                role="developer",
                reason="Spec approved",
            ),
        ]
        set_pending_plan(decisions, project_slug="test/repo")

        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(github_repo="test/repo")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/plan")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["pending"]) == 2
        assert data["pending"][0]["issue_number"] == 42
        assert data["pending"][0]["action"] == "spawn_researcher"
        assert data["pending"][0]["role"] == "researcher"
        assert data["pending"][1]["issue_number"] == 43

    async def test_get_plan_returns_empty_when_config_load_fails(self, app) -> None:
        """GET /plan returns empty plan when load_config raises."""
        from sova.dashboard.services.supervisor_service import set_pending_plan
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        # Set up a plan for a project, but simulate config load failure
        decisions = [
            ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher"),
        ]
        set_pending_plan(decisions, project_slug="test/repo")

        with patch("sova.config.loader.load_config", side_effect=FileNotFoundError("Config load failed")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/plan")
        assert resp.status_code == 200
        data = resp.json()
        # Should return empty plan on config failure, not crash
        assert data["reasoning"] is None
        assert data["pending"] == []
        assert data["deferred"] == []

    async def test_approve_empty_plan_returns_zero(self, app) -> None:
        """approve_plan returns early with approved=0 when plan is empty."""
        from sova.dashboard.services.supervisor_service import set_pending_plan

        set_pending_plan([], project_slug="test/repo")

        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(github_repo="test/repo")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/plan/approve", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] == 0
        assert data["results"] == []

    async def test_approve_all_executes_decisions(self, app) -> None:
        from sova.dashboard.services.supervisor_service import get_pending_plan, set_pending_plan
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        decisions = [
            ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher"),
        ]
        set_pending_plan(decisions, project_slug="test/repo")

        _mock_exec = patch(
            "sova.dashboard.routers.supervisor._execute_plan_decisions",
            new_callable=AsyncMock,
            return_value=[{"run_id": 1}],
        )
        with patch("sova.config.loader.load_config") as mock_cfg, _mock_exec:
            mock_cfg.return_value = MagicMock(github_repo="test/repo")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/plan/approve", json={})
        assert resp.status_code == 200
        # Plan should be cleared after approval
        assert get_pending_plan("test/repo") == []

    async def test_approve_selected_issues(self, app) -> None:
        from sova.dashboard.services.supervisor_service import get_pending_plan, set_pending_plan
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        decisions = [
            ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher"),
            ProgressionDecision(issue_number=43, action=ProgressionAction.SPAWN_DEVELOPER, role="developer"),
        ]
        set_pending_plan(decisions, project_slug="test/repo")

        _mock_exec = patch(
            "sova.dashboard.routers.supervisor._execute_plan_decisions",
            new_callable=AsyncMock,
            return_value=[],
        )
        with patch("sova.config.loader.load_config") as mock_cfg, _mock_exec:
            mock_cfg.return_value = MagicMock(github_repo="test/repo")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/plan/approve", json={"issue_numbers": [42]})
        assert resp.status_code == 200
        # Issue 43 should remain in the plan (not approved)
        remaining = get_pending_plan("test/repo")
        assert len(remaining) == 1
        assert remaining[0].issue_number == 43

    async def test_approve_partial_failure_returns_errors(self, app) -> None:
        """approve_plan returns 200 with partial error info when some decisions fail."""
        from sova.dashboard.services.supervisor_service import get_pending_plan, set_pending_plan
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        decisions = [
            ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher"),
            ProgressionDecision(issue_number=43, action=ProgressionAction.SPAWN_DEVELOPER, role="developer"),
        ]
        set_pending_plan(decisions, project_slug="test/repo")

        _mock_exec = patch(
            "sova.dashboard.routers.supervisor._execute_plan_decisions",
            new_callable=AsyncMock,
            return_value=[{"run_id": 1}, {"error": "spawn failed", "issue_number": 43}],
        )
        with patch("sova.config.loader.load_config") as mock_cfg, _mock_exec:
            mock_cfg.return_value = MagicMock(github_repo="test/repo")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/plan/approve", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] == 2
        assert len(data["errors"]) == 1
        assert data["errors"][0]["issue_number"] == 43
        # Plan cleared regardless of partial failure
        assert get_pending_plan("test/repo") == []

    async def test_skip_removes_from_plan(self, app) -> None:
        from sova.dashboard.services.supervisor_service import get_pending_plan, set_pending_plan
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        decisions = [
            ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER),
            ProgressionDecision(issue_number=43, action=ProgressionAction.SPAWN_DEVELOPER),
        ]
        set_pending_plan(decisions, project_slug="test/repo")

        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(github_repo="test/repo")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/plan/skip/42")
        assert resp.status_code == 200
        remaining = get_pending_plan("test/repo")
        assert len(remaining) == 1
        assert remaining[0].issue_number == 43

    async def test_skip_nonexistent_issue_returns_404(self, app) -> None:
        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(github_repo="test/repo")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/plan/skip/999")
        assert resp.status_code == 404

    async def test_approve_with_config_load_failure(self, app) -> None:
        """approve_plan propagates exception when load_config fails."""
        import pytest

        with patch("sova.config.loader.load_config", side_effect=Exception("Config load failed")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                with pytest.raises(Exception, match="Config load failed"):
                    await client.post("/api/supervisor/plan/approve", json={})

    async def test_skip_with_config_load_failure(self, app) -> None:
        """skip_plan_item propagates exception when load_config fails."""
        import pytest

        with patch("sova.config.loader.load_config", side_effect=Exception("Config load failed")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                with pytest.raises(Exception, match="Config load failed"):
                    await client.post("/api/supervisor/plan/skip/42")


class TestSupervisorDaemonRequireApproval:
    """Tests for require_approval config in the daemon poll loop."""

    @pytest.fixture
    async def session_factory(self):
        from sova.db.session import get_session_factory

        return await get_session_factory(Path.cwd())

    async def test_require_approval_stores_plan_without_executing(self, session_factory) -> None:
        from sova.config.models import ProjectConfig, SupervisorConfig
        from sova.dashboard.services.supervisor_service import get_pending_plan, set_pending_plan
        from sova.supervisor.daemon import SupervisorDaemon
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        set_pending_plan([], project_slug="test/repo")
        cfg = ProjectConfig(supervisor=SupervisorConfig(enabled=True, require_approval=True), github_repo="test/repo")
        daemon = SupervisorDaemon(config=cfg, project_dir=Path("/tmp/test"), session_factory=session_factory)

        decisions = [ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher")]

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch.object(daemon, "_poll_health", new_callable=AsyncMock, return_value={}),
            patch.object(daemon, "_poll_quota", new_callable=AsyncMock, return_value={"enabled": False}),
            patch(
                "sova.supervisor.progression.TaskProgressionEngine.evaluate_all",
                new_callable=AsyncMock,
                return_value=decisions,
            ),
        ):
            result = await daemon.poll_once()

        # Decisions stored in plan, not executed
        plan = get_pending_plan("test/repo")
        assert len(plan) == 1
        assert plan[0].issue_number == 42
        assert result["progression"]["pending"] == 1
        assert result["progression"]["executed"] == 0
        set_pending_plan([], project_slug="test/repo")

    async def test_require_approval_false_executes_directly(self, session_factory) -> None:
        from sova.config.models import ProjectConfig, SupervisorConfig
        from sova.supervisor.daemon import SupervisorDaemon
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        cfg = ProjectConfig(supervisor=SupervisorConfig(enabled=True, require_approval=False))
        daemon = SupervisorDaemon(config=cfg, project_dir=Path("/tmp/test"), session_factory=session_factory)

        decisions = [ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher")]
        execute_results = [{"run_id": 1}]

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch.object(daemon, "_poll_health", new_callable=AsyncMock, return_value={}),
            patch.object(daemon, "_poll_quota", new_callable=AsyncMock, return_value={"enabled": False}),
            patch(
                "sova.supervisor.progression.TaskProgressionEngine.evaluate_all",
                new_callable=AsyncMock,
                return_value=decisions,
            ),
            patch(
                "sova.supervisor.progression.TaskProgressionEngine.execute_decisions",
                new_callable=AsyncMock,
                return_value=execute_results,
            ),
        ):
            result = await daemon.poll_once()

        assert result["progression"]["executed"] == execute_results
        assert result["progression"].get("pending", 0) == 0


class TestExecutePlanDecisions:
    """Direct tests of _execute_plan_decisions to cover lines not reached via mocked approve_plan tests."""

    async def test_execute_plan_decisions_success(self) -> None:
        from sova.dashboard.routers.supervisor import _execute_plan_decisions
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        decisions = [
            ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher"),
        ]

        async def _fake_execute_decision(decision):
            return {"run_id": 1, "issue_number": decision.issue_number}

        with (
            patch("sova.config.loader.load_config", return_value=MagicMock(supervisor=MagicMock(), github_repo="r/r")),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch("sova.db.session.get_session_factory", new_callable=AsyncMock, return_value=MagicMock()),
            patch("sova.supervisor.progression.TaskProgressionEngine.__init__", return_value=None),
            patch(
                "sova.supervisor.progression.TaskProgressionEngine.execute_decision",
                new_callable=AsyncMock,
                side_effect=_fake_execute_decision,
            ),
        ):
            results = await _execute_plan_decisions(decisions, Path("/tmp/test"))

        assert len(results) == 1
        assert results[0]["run_id"] == 1

    async def test_execute_plan_decisions_catches_per_decision_exception(self) -> None:
        from sova.dashboard.routers.supervisor import _execute_plan_decisions
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        decisions = [
            ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher"),
            ProgressionDecision(issue_number=43, action=ProgressionAction.SPAWN_DEVELOPER, role="developer"),
        ]

        async def _fake_execute_decision(decision):
            if decision.issue_number == 43:
                raise RuntimeError("agent slot full")
            return {"run_id": 1}

        with (
            patch("sova.config.loader.load_config", return_value=MagicMock(supervisor=MagicMock(), github_repo="r/r")),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch("sova.db.session.get_session_factory", new_callable=AsyncMock, return_value=MagicMock()),
            patch("sova.supervisor.progression.TaskProgressionEngine.__init__", return_value=None),
            patch(
                "sova.supervisor.progression.TaskProgressionEngine.execute_decision",
                new_callable=AsyncMock,
                side_effect=_fake_execute_decision,
            ),
        ):
            results = await _execute_plan_decisions(decisions, Path("/tmp/test"))

        assert len(results) == 2
        assert results[0] == {"run_id": 1}
        assert results[1] == {"error": "agent slot full", "issue_number": 43}


class TestMultiProjectPlanIsolation:
    """Tests for project-keyed plan isolation."""

    @pytest.fixture(autouse=True)
    def clear_all_plans(self):
        """Reset all project plans before and after each test."""
        from sova.dashboard.services import supervisor_service

        supervisor_service._pending_plan.clear()
        supervisor_service._plan_reasoning.clear()
        supervisor_service._plan_deferred.clear()
        yield
        supervisor_service._pending_plan.clear()
        supervisor_service._plan_reasoning.clear()
        supervisor_service._plan_deferred.clear()

    def test_plans_isolated_by_project(self) -> None:
        """Plans for different projects do not interfere with each other."""
        from sova.dashboard.services.supervisor_service import get_pending_plan, set_pending_plan
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        project_a_decisions = [
            ProgressionDecision(issue_number=10, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher"),
        ]
        project_b_decisions = [
            ProgressionDecision(issue_number=20, action=ProgressionAction.SPAWN_DEVELOPER, role="developer"),
        ]

        set_pending_plan(project_a_decisions, project_slug="test/repo")
        set_pending_plan(project_b_decisions, project_slug="other/repo")

        # Each project sees only its own plan
        assert len(get_pending_plan(project_slug="test/repo")) == 1
        assert get_pending_plan(project_slug="test/repo")[0].issue_number == 10
        assert len(get_pending_plan(project_slug="other/repo")) == 1
        assert get_pending_plan(project_slug="other/repo")[0].issue_number == 20

    def test_remove_plan_items_scoped_to_project(self) -> None:
        """remove_plan_items only affects the specified project."""
        from sova.dashboard.services.supervisor_service import (
            get_pending_plan,
            remove_plan_items,
            set_pending_plan,
        )
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        project_a_decisions = [
            ProgressionDecision(issue_number=10, action=ProgressionAction.SPAWN_RESEARCHER),
            ProgressionDecision(issue_number=11, action=ProgressionAction.SPAWN_DEVELOPER),
        ]
        project_b_decisions = [
            ProgressionDecision(issue_number=10, action=ProgressionAction.SPAWN_RESEARCHER),
        ]

        set_pending_plan(project_a_decisions, project_slug="test/repo")
        set_pending_plan(project_b_decisions, project_slug="other/repo")

        # Remove issue 10 from project A
        removed = remove_plan_items({10}, project_slug="test/repo")
        assert len(removed) == 1
        assert removed[0].issue_number == 10

        # Project A now has only issue 11
        assert len(get_pending_plan(project_slug="test/repo")) == 1
        assert get_pending_plan(project_slug="test/repo")[0].issue_number == 11

        # Project B still has issue 10
        assert len(get_pending_plan(project_slug="other/repo")) == 1
        assert get_pending_plan(project_slug="other/repo")[0].issue_number == 10

    def test_plan_reasoning_and_deferred_isolated(self) -> None:
        """Reasoning and deferred lists are also project-scoped."""
        from sova.dashboard.services.supervisor_service import (
            get_plan_deferred,
            get_plan_reasoning,
            set_pending_plan,
        )

        set_pending_plan([], project_slug="test/repo", reasoning="Test reasoning", deferred=[{"action": "test"}])
        set_pending_plan([], project_slug="other/repo", reasoning="Other reasoning", deferred=[{"action": "other"}])

        assert get_plan_reasoning(project_slug="test/repo") == "Test reasoning"
        assert get_plan_reasoning(project_slug="other/repo") == "Other reasoning"
        assert get_plan_deferred(project_slug="test/repo") == [{"action": "test"}]
        assert get_plan_deferred(project_slug="other/repo") == [{"action": "other"}]

    def test_empty_github_repo_uses_empty_string_key(self) -> None:
        """Projects with no github_repo use empty string as key."""
        from sova.dashboard.services.supervisor_service import get_pending_plan, set_pending_plan
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        decisions = [
            ProgressionDecision(issue_number=99, action=ProgressionAction.SPAWN_RESEARCHER),
        ]
        set_pending_plan(decisions, project_slug="")

        assert len(get_pending_plan(project_slug="")) == 1
        assert get_pending_plan(project_slug="")[0].issue_number == 99

        # Does not leak into projects with real slugs
        assert len(get_pending_plan(project_slug="test/repo")) == 0


class TestTaskQueueRouter:
    """Tests for the task queue CRUD endpoints."""

    @pytest.fixture
    def project_dir(self, tmp_path):
        return tmp_path

    @pytest.fixture
    def app(self, project_dir):
        from sova.dashboard.app import create_app

        # Write a minimal sova.toml so queue writes have a file to update
        (project_dir / "sova.toml").write_text('[project]\ngithub_repo = "test/repo"\n')

        with patch("sova.dashboard.app.recover_stale_runs", new_callable=AsyncMock):
            with patch("sova.dashboard.app.list_projects", return_value={}):
                return create_app(project_dir=project_dir)

    async def test_get_queue_empty(self, app, project_dir) -> None:
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/queue")
        assert resp.status_code == 200
        assert resp.json()["queue"] == []

    async def test_add_to_queue(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/supervisor/queue", json={"issue_number": 42})
        assert resp.status_code == 201
        assert resp.json()["queue"] == [42]

    async def test_add_duplicate_returns_409(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/api/supervisor/queue", json={"issue_number": 42})
            resp = await client.post("/api/supervisor/queue", json={"issue_number": 42})
        assert resp.status_code == 409

    async def test_set_queue_with_reorder(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/api/supervisor/queue", json={"issue_number": 10})
            await client.post("/api/supervisor/queue", json={"issue_number": 20})
            resp = await client.put("/api/supervisor/queue", json={"issue_numbers": [20, 10]})
        assert resp.status_code == 200
        assert resp.json()["queue"] == [20, 10]

    async def test_set_queue_deduplicates(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put("/api/supervisor/queue", json={"issue_numbers": [5, 5, 10, 5]})
        assert resp.status_code == 200
        assert resp.json()["queue"] == [5, 10]

    async def test_remove_from_queue(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/api/supervisor/queue", json={"issue_number": 42})
            await client.post("/api/supervisor/queue", json={"issue_number": 43})
            resp = await client.delete("/api/supervisor/queue/42")
        assert resp.status_code == 200
        assert resp.json()["queue"] == [43]

    async def test_remove_nonexistent_returns_404(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/api/supervisor/queue/999")
        assert resp.status_code == 404

    async def test_clear_queue(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/api/supervisor/queue", json={"issue_number": 42})
            resp = await client.delete("/api/supervisor/queue")
        assert resp.status_code == 200
        assert resp.json()["queue"] == []

    async def test_queue_persists_to_db(self, app, project_dir) -> None:
        """Verify queue changes are written to DB."""
        from sova.config.db_loader import get_setting
        from sova.db.session import get_session

        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post("/api/supervisor/queue", json={"issue_number": 42})

        async with await get_session(project_dir=project_dir) as session:
            queue = await get_setting(session, "supervisor.task_queue")
        assert queue == [42]

    async def test_concurrent_adds_no_duplicates(self, app, project_dir) -> None:
        """Concurrent add requests must not produce duplicate entries."""
        import asyncio

        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                results = await asyncio.gather(
                    client.post("/api/supervisor/queue", json={"issue_number": 10}),
                    client.post("/api/supervisor/queue", json={"issue_number": 20}),
                    client.post("/api/supervisor/queue", json={"issue_number": 30}),
                )
        statuses = sorted(r.status_code for r in results)
        assert all(s in (201, 409) for s in statuses)

        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/queue")
        queue = resp.json()["queue"]
        assert len(queue) == len(set(queue)), f"Duplicate entries in queue: {queue}"


class TestTaskQueueDaemonNotify:
    """Queue-mutating endpoints must refresh a registered daemon's config
    snapshot (non-fatal, without waking it early: see #935)."""

    @pytest.fixture
    def project_dir(self, tmp_path):
        return tmp_path

    @pytest.fixture
    def app(self, project_dir):
        from sova.dashboard.app import create_app

        (project_dir / "sova.toml").write_text('[project]\ngithub_repo = "test/repo"\n')

        with patch("sova.dashboard.app.recover_stale_runs", new_callable=AsyncMock):
            with patch("sova.dashboard.app.list_projects", return_value={}):
                return create_app(project_dir=project_dir)

    @staticmethod
    def _registered_daemon(project_dir):
        """Context manager registering a mock daemon for project_dir, restored after."""
        from contextlib import contextmanager

        from sova.dashboard.routers import supervisor as sup_mod

        @contextmanager
        def _cm():
            mock_daemon = MagicMock()
            orig_registry = sup_mod._daemon_registry
            sup_mod._daemon_registry = {str(project_dir.resolve()): mock_daemon}
            try:
                yield mock_daemon
            finally:
                sup_mod._daemon_registry = orig_registry

        return _cm()

    async def test_add_to_queue_notifies_daemon_without_waking(self, app, project_dir) -> None:
        with (
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
            self._registered_daemon(project_dir) as mock_daemon,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/queue", json={"issue_number": 42})
        assert resp.status_code == 201
        mock_daemon.reload_config.assert_called_once()
        _args, kwargs = mock_daemon.reload_config.call_args
        assert kwargs.get("wake") is False

    async def test_set_queue_notifies_daemon(self, app, project_dir) -> None:
        with (
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
            self._registered_daemon(project_dir) as mock_daemon,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put("/api/supervisor/queue", json={"issue_numbers": [1, 2]})
        assert resp.status_code == 200
        mock_daemon.reload_config.assert_called_once()

    async def test_remove_from_queue_notifies_daemon(self, app, project_dir) -> None:
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post("/api/supervisor/queue", json={"issue_number": 42})

            with self._registered_daemon(project_dir) as mock_daemon:
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.delete("/api/supervisor/queue/42")
        assert resp.status_code == 200
        mock_daemon.reload_config.assert_called_once()

    async def test_clear_queue_notifies_daemon(self, app, project_dir) -> None:
        with (
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
            self._registered_daemon(project_dir) as mock_daemon,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.delete("/api/supervisor/queue")
        assert resp.status_code == 200
        mock_daemon.reload_config.assert_called_once()

    async def test_no_registered_daemon_is_noop(self, app, project_dir) -> None:
        """No daemon registered (supervisor disabled): endpoint still returns 200/201."""
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/queue", json={"issue_number": 42})
        assert resp.status_code == 201

    async def test_notify_failure_is_non_fatal(self, app, project_dir) -> None:
        """A broken daemon.reload_config() must not fail the queue write."""
        with (
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
            self._registered_daemon(project_dir) as mock_daemon,
        ):
            mock_daemon.reload_config.side_effect = RuntimeError("boom")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/queue", json={"issue_number": 42})
        assert resp.status_code == 201
        assert resp.json()["queue"] == [42]

    async def test_notify_only_touches_daemon_for_own_project(self, app, project_dir, tmp_path_factory) -> None:
        """A queue edit on project A must not refresh project B's daemon (multi-project isolation)."""
        from sova.dashboard.routers import supervisor as sup_mod

        other_project_dir = tmp_path_factory.mktemp("other-project")
        mock_daemon_a = MagicMock()
        mock_daemon_b = MagicMock()
        orig_registry = sup_mod._daemon_registry
        sup_mod._daemon_registry = {
            str(project_dir.resolve()): mock_daemon_a,
            str(other_project_dir.resolve()): mock_daemon_b,
        }
        try:
            with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.post("/api/supervisor/queue", json={"issue_number": 42})
            assert resp.status_code == 201
            mock_daemon_a.reload_config.assert_called_once()
            mock_daemon_b.reload_config.assert_not_called()
        finally:
            sup_mod._daemon_registry = orig_registry


class TestTaskQueueValidation:
    """Tests for task_queue field validation in SupervisorConfig and QueueAddRequest."""

    def test_task_queue_rejects_zero(self) -> None:
        from sova.config.models import SupervisorConfig

        with pytest.raises(ValueError, match="positive integers"):
            SupervisorConfig(task_queue=[0])

    def test_task_queue_rejects_negative(self) -> None:
        from sova.config.models import SupervisorConfig

        with pytest.raises(ValueError, match="positive integers"):
            SupervisorConfig(task_queue=[-1])

    def test_task_queue_accepts_positive(self) -> None:
        from sova.config.models import SupervisorConfig

        cfg = SupervisorConfig(task_queue=[1, 42, 100])
        assert cfg.task_queue == [1, 42, 100]

    def test_task_queue_accepts_empty(self) -> None:
        from sova.config.models import SupervisorConfig

        cfg = SupervisorConfig(task_queue=[])
        assert cfg.task_queue == []

    def test_queue_add_request_rejects_zero(self) -> None:
        from sova.dashboard.routers.supervisor import QueueAddRequest

        with pytest.raises(ValueError):
            QueueAddRequest(issue_number=0)

    def test_queue_add_request_rejects_negative(self) -> None:
        from sova.dashboard.routers.supervisor import QueueAddRequest

        with pytest.raises(ValueError):
            QueueAddRequest(issue_number=-5)

    def test_queue_add_request_accepts_positive(self) -> None:
        from sova.dashboard.routers.supervisor import QueueAddRequest

        req = QueueAddRequest(issue_number=42)
        assert req.issue_number == 42

    def test_queue_set_request_rejects_negative(self) -> None:
        from sova.dashboard.routers.supervisor import QueueSetRequest

        with pytest.raises(ValueError, match="positive integers"):
            QueueSetRequest(issue_numbers=[1, -2, 3])

    def test_queue_set_request_rejects_zero(self) -> None:
        from sova.dashboard.routers.supervisor import QueueSetRequest

        with pytest.raises(ValueError, match="positive integers"):
            QueueSetRequest(issue_numbers=[0])

    def test_queue_set_request_accepts_positive(self) -> None:
        from sova.dashboard.routers.supervisor import QueueSetRequest

        req = QueueSetRequest(issue_numbers=[1, 42, 100])
        assert req.issue_numbers == [1, 42, 100]

    async def test_add_zero_issue_returns_422(self, tmp_path) -> None:
        """POST /queue with issue_number=0 returns 422 validation error."""
        from sova.dashboard.app import create_app

        with patch("sova.dashboard.app.recover_stale_runs", new_callable=AsyncMock):
            with patch("sova.dashboard.app.list_projects", return_value={}):
                app = create_app(project_dir=tmp_path)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/supervisor/queue", json={"issue_number": 0})
        assert resp.status_code == 422


class TestTaskQueueErrorPaths:
    """Tests for queue error paths: DB write failures, negative numbers."""

    @pytest.fixture
    def project_dir(self, tmp_path):
        return tmp_path

    @pytest.fixture
    def app(self, project_dir):
        from sova.dashboard.app import create_app

        (project_dir / "sova.toml").write_text('[project]\ngithub_repo = "test/repo"\n')

        with patch("sova.dashboard.app.recover_stale_runs", new_callable=AsyncMock):
            with patch("sova.dashboard.app.list_projects", return_value={}):
                return create_app(project_dir=project_dir)

    async def test_negative_issue_number_rejected(self, app, project_dir) -> None:
        """POST /queue with negative issue_number returns 422."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/supervisor/queue", json={"issue_number": -5})
        assert resp.status_code == 422

    async def test_set_queue_negative_rejected(self, app) -> None:
        """PUT /queue with negative issue_numbers returns 422."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put("/api/supervisor/queue", json={"issue_numbers": [1, -2, 3]})
        assert resp.status_code == 422

    async def test_set_queue_zero_rejected(self, app) -> None:
        """PUT /queue with zero issue_numbers returns 422."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put("/api/supervisor/queue", json={"issue_numbers": [0]})
        assert resp.status_code == 422

    async def test_set_queue_write_failure_returns_503(self, app, project_dir) -> None:
        """PUT /queue returns 503 when DB save fails."""
        with (
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
            patch("sova.config.db_loader.save_setting", side_effect=OperationalError("DB error", None, None)),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put("/api/supervisor/queue", json={"issue_numbers": [42]})
        assert resp.status_code == 503

    async def test_add_to_queue_write_failure_returns_503(self, app, project_dir) -> None:
        """POST /queue returns 503 when DB save fails."""
        with (
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
            patch("sova.config.db_loader.save_setting", side_effect=OperationalError("DB error", None, None)),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/queue", json={"issue_number": 42})
        assert resp.status_code == 503

    async def test_remove_from_queue_write_failure_returns_503(self, app, project_dir) -> None:
        """DELETE /queue/{issue_number} returns 503 when DB save fails."""
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post("/api/supervisor/queue", json={"issue_number": 42})

        with (
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
            patch("sova.config.db_loader.save_setting", side_effect=OperationalError("DB error", None, None)),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.delete("/api/supervisor/queue/42")
        assert resp.status_code == 503

    async def test_clear_queue_write_failure_returns_503(self, app, project_dir) -> None:
        """DELETE /queue returns 503 when DB save fails."""
        with (
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
            patch("sova.config.db_loader.save_setting", side_effect=OperationalError("DB error", None, None)),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.delete("/api/supervisor/queue")
        assert resp.status_code == 503


class TestSaveTaskQueueEdgeCases:
    """Tests for save_task_queue/load_task_queue shared helpers."""

    async def test_save_creates_db_entry_when_missing(self, tmp_path) -> None:
        """Verify queue is saved to DB when no prior entry exists."""
        from sova.config.db_loader import get_setting, save_task_queue
        from sova.db.session import get_session

        await save_task_queue(tmp_path, [42, 43])

        async with await get_session(project_dir=tmp_path) as session:
            queue = await get_setting(session, "supervisor.task_queue")
        assert queue == [42, 43]

    async def test_save_updates_existing_queue(self, tmp_path) -> None:
        """Verify queue updates replace the existing DB entry."""
        from sova.config.db_loader import get_setting, save_setting, save_task_queue
        from sova.db.session import get_session

        async with await get_session(project_dir=tmp_path) as session:
            async with session.begin():
                await save_setting(session, "supervisor.task_queue", [1, 2])

        await save_task_queue(tmp_path, [10])

        async with await get_session(project_dir=tmp_path) as session:
            queue = await get_setting(session, "supervisor.task_queue")
        assert queue == [10]

    async def test_save_with_none_project_dir(self, tmp_path, monkeypatch) -> None:
        """Verify save_task_queue handles None project_dir."""
        from sova.config.db_loader import get_setting, save_task_queue
        from sova.db.session import get_session

        monkeypatch.chdir(tmp_path)
        await save_task_queue(None, [1])

        async with await get_session(project_dir=None) as session:
            queue = await get_setting(session, "supervisor.task_queue")
        assert queue == [1]

    async def test_load_returns_empty_when_unset(self, tmp_path) -> None:
        """load_task_queue returns [] when no queue is stored."""
        from sova.config.db_loader import load_task_queue

        result = await load_task_queue(tmp_path)
        assert result == []

    async def test_load_returns_saved_queue(self, tmp_path) -> None:
        """load_task_queue returns the queue that was saved."""
        from sova.config.db_loader import load_task_queue, save_task_queue

        await save_task_queue(tmp_path, [10, 20, 30])
        result = await load_task_queue(tmp_path)
        assert result == [10, 20, 30]


class TestSupervisorPersonaEndpoints:
    """Tests for persona GET and POST /persona/open endpoints."""

    @pytest.fixture
    def app(self):
        from sova.dashboard.app import create_app

        with patch("sova.dashboard.app.recover_stale_runs", new_callable=AsyncMock):
            with patch("sova.dashboard.app.list_projects", return_value={}):
                return create_app(project_dir=Path.cwd())

    async def test_get_persona_success(self, app) -> None:
        persona_info = {"content": "Test persona", "path": "/tmp/persona.md", "exists": True}
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.supervisor.persona.get_persona_info", return_value=persona_info),
        ):
            mock_cfg.return_value.supervisor.persona_path = "/tmp/persona.md"
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/persona")
        assert resp.status_code == 200
        assert resp.json()["content"] == "Test persona"

    async def test_get_persona_error_returns_500(self, app) -> None:
        with patch("sova.config.loader.load_config", side_effect=RuntimeError("bad config")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/persona")
        assert resp.status_code == 500
        assert "Failed to fetch" in resp.json()["detail"]

    async def test_open_persona_config_error_returns_500(self, app) -> None:
        with patch("sova.config.loader.load_config", side_effect=RuntimeError("no config")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/persona/open")
        assert resp.status_code == 500
        assert "Failed to load project configuration" in resp.json()["detail"]

    async def test_open_persona_no_editor_returns_400(self, app) -> None:
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.supervisor.persona.ensure_persona_exists", return_value=Path("/tmp/persona.md")),
            patch("sova.oversight.persona.get_open_command", return_value=None),
        ):
            mock_cfg.return_value.supervisor.persona_path = "/tmp/persona.md"
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/persona/open")
        assert resp.status_code == 400
        assert "No editor command" in resp.json()["detail"]

    async def test_open_persona_editor_not_found_returns_400(self, app) -> None:
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.supervisor.persona.ensure_persona_exists", return_value=Path("/tmp/persona.md")),
            patch("sova.oversight.persona.get_open_command", return_value="nonexistent-editor"),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, side_effect=FileNotFoundError()),
        ):
            mock_cfg.return_value.supervisor.persona_path = "/tmp/persona.md"
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/persona/open")
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"]

    async def test_open_persona_subprocess_error_returns_500(self, app) -> None:
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.supervisor.persona.ensure_persona_exists", return_value=Path("/tmp/persona.md")),
            patch("sova.oversight.persona.get_open_command", return_value="code"),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, side_effect=OSError("spawn failed")),
        ):
            mock_cfg.return_value.supervisor.persona_path = "/tmp/persona.md"
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/persona/open")
        assert resp.status_code == 500
        assert "Failed to open editor" in resp.json()["detail"]

    async def test_open_persona_success(self, app) -> None:
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.supervisor.persona.ensure_persona_exists", return_value=Path("/tmp/persona.md")),
            patch("sova.oversight.persona.get_open_command", return_value="code"),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock),
        ):
            mock_cfg.return_value.supervisor.persona_path = "/tmp/persona.md"
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/persona/open")
        assert resp.status_code == 200
        assert resp.json()["status"] == "spawned"


class TestAutoResearchDefault:
    """Verify auto_research defaults to False per architecture.md."""

    def test_auto_research_defaults_to_false(self) -> None:
        from sova.config.models import SupervisorConfig

        cfg = SupervisorConfig()
        assert cfg.auto_research is False


class TestMaybeMigrateQueueFromToml:
    """Tests for _maybe_migrate_queue_from_toml one-time migration."""

    @pytest.fixture
    def project_dir(self, tmp_path):
        return tmp_path

    @pytest.fixture(autouse=True)
    def _reset_flag(self):
        import sova.dashboard.routers.supervisor as sup_mod

        sup_mod._toml_migrated.clear()
        yield
        sup_mod._toml_migrated.clear()

    async def test_skips_when_already_migrated(self) -> None:
        import sova.dashboard.routers.supervisor as sup_mod

        project_dir = Path("/already-done")
        sup_mod._toml_migrated.add(str(project_dir))
        mock_read = AsyncMock()
        with patch("sova.dashboard.routers.supervisor._read_queue", mock_read):
            await sup_mod._maybe_migrate_queue_from_toml(project_dir)
        mock_read.assert_not_called()

    async def test_skips_when_toml_queue_empty(self, project_dir) -> None:
        from sova.dashboard.routers.supervisor import _maybe_migrate_queue_from_toml

        mock_cfg = MagicMock()
        mock_cfg.supervisor.task_queue = []
        with patch("sova.config.loader.load_config", return_value=mock_cfg):
            await _maybe_migrate_queue_from_toml(project_dir)

    async def test_migrates_toml_queue_to_db(self, project_dir) -> None:
        from sova.config.db_loader import load_task_queue
        from sova.dashboard.routers.supervisor import _maybe_migrate_queue_from_toml

        mock_cfg = MagicMock()
        mock_cfg.supervisor.task_queue = [10, 20, 30]
        with patch("sova.config.loader.load_config", return_value=mock_cfg):
            await _maybe_migrate_queue_from_toml(project_dir)

        loaded = await load_task_queue(project_dir)
        assert loaded == [10, 20, 30]

    async def test_does_not_overwrite_existing_db_value(self, project_dir) -> None:
        from sova.config.db_loader import load_task_queue, save_task_queue
        from sova.dashboard.routers.supervisor import _maybe_migrate_queue_from_toml

        await save_task_queue(project_dir, [99])

        mock_cfg = MagicMock()
        mock_cfg.supervisor.task_queue = [10, 20]
        with patch("sova.config.loader.load_config", return_value=mock_cfg):
            await _maybe_migrate_queue_from_toml(project_dir)

        loaded = await load_task_queue(project_dir)
        assert loaded == [99]

    async def test_exception_is_swallowed(self) -> None:
        from sova.dashboard.routers.supervisor import _maybe_migrate_queue_from_toml

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("boom")):
            await _maybe_migrate_queue_from_toml(Path("/nonexistent"))


class TestDeduplicate:
    def test_preserves_first_occurrence(self) -> None:
        from sova.dashboard.routers.supervisor import _deduplicate

        assert _deduplicate([3, 1, 2, 1, 3]) == [3, 1, 2]

    def test_empty_list(self) -> None:
        from sova.dashboard.routers.supervisor import _deduplicate

        assert _deduplicate([]) == []

    def test_no_duplicates(self) -> None:
        from sova.dashboard.routers.supervisor import _deduplicate

        assert _deduplicate([1, 2, 3]) == [1, 2, 3]
