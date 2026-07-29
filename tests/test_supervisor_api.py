"""Tests for the supervisor dashboard API router and service."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

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

    async def test_stop_supervisor_no_project_returns_503(self, app) -> None:
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/stop")
        assert resp.status_code == 503

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

    async def test_start_supervisor_no_project_context(self, app) -> None:
        """POST /supervisor/start returns 503 when no project context is set."""
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/start")
                assert resp.status_code == 503

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

        set_pending_plan([])
        yield
        set_pending_plan([])

    async def test_get_plan_empty(self, app) -> None:
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
        set_pending_plan(decisions)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/supervisor/plan")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["pending"]) == 2
        assert data["pending"][0]["issue_number"] == 42
        assert data["pending"][0]["action"] == "spawn_researcher"
        assert data["pending"][0]["role"] == "researcher"
        assert data["pending"][1]["issue_number"] == 43

    async def test_approve_empty_plan_returns_zero(self, app) -> None:
        """approve_plan returns early with approved=0 when plan is empty."""
        from sova.dashboard.services.supervisor_service import set_pending_plan

        set_pending_plan([])

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
        set_pending_plan(decisions)

        _mock_exec = patch(
            "sova.dashboard.routers.supervisor._execute_plan_decisions",
            new_callable=AsyncMock,
            return_value=[{"run_id": 1}],
        )
        with _mock_exec:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/plan/approve", json={})
        assert resp.status_code == 200
        # Plan should be cleared after approval
        assert get_pending_plan() == []

    async def test_approve_selected_issues(self, app) -> None:
        from sova.dashboard.services.supervisor_service import get_pending_plan, set_pending_plan
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        decisions = [
            ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher"),
            ProgressionDecision(issue_number=43, action=ProgressionAction.SPAWN_DEVELOPER, role="developer"),
        ]
        set_pending_plan(decisions)

        _mock_exec = patch(
            "sova.dashboard.routers.supervisor._execute_plan_decisions",
            new_callable=AsyncMock,
            return_value=[],
        )
        with _mock_exec:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/plan/approve", json={"issue_numbers": [42]})
        assert resp.status_code == 200
        # Issue 43 should remain in the plan (not approved)
        remaining = get_pending_plan()
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
        set_pending_plan(decisions)

        _mock_exec = patch(
            "sova.dashboard.routers.supervisor._execute_plan_decisions",
            new_callable=AsyncMock,
            return_value=[{"run_id": 1}, {"error": "spawn failed", "issue_number": 43}],
        )
        with _mock_exec:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/plan/approve", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] == 2
        assert len(data["errors"]) == 1
        assert data["errors"][0]["issue_number"] == 43
        # Plan cleared regardless of partial failure
        assert get_pending_plan() == []

    async def test_skip_removes_from_plan(self, app) -> None:
        from sova.dashboard.services.supervisor_service import get_pending_plan, set_pending_plan
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        decisions = [
            ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER),
            ProgressionDecision(issue_number=43, action=ProgressionAction.SPAWN_DEVELOPER),
        ]
        set_pending_plan(decisions)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/supervisor/plan/skip/42")
        assert resp.status_code == 200
        remaining = get_pending_plan()
        assert len(remaining) == 1
        assert remaining[0].issue_number == 43

    async def test_skip_nonexistent_issue_returns_404(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/supervisor/plan/skip/999")
        assert resp.status_code == 404


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

        set_pending_plan([])
        cfg = ProjectConfig(supervisor=SupervisorConfig(enabled=True, require_approval=True))
        daemon = SupervisorDaemon(config=cfg, project_dir=Path("/tmp/test"), session_factory=session_factory)

        decisions = [ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher")]

        with (
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
        plan = get_pending_plan()
        assert len(plan) == 1
        assert plan[0].issue_number == 42
        assert result["progression"]["pending"] == 1
        assert result["progression"]["executed"] == 0
        set_pending_plan([])

    async def test_require_approval_false_executes_directly(self, session_factory) -> None:
        from sova.config.models import ProjectConfig, SupervisorConfig
        from sova.supervisor.daemon import SupervisorDaemon
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        cfg = ProjectConfig(supervisor=SupervisorConfig(enabled=True, require_approval=False))
        daemon = SupervisorDaemon(config=cfg, project_dir=Path("/tmp/test"), session_factory=session_factory)

        decisions = [ProgressionDecision(issue_number=42, action=ProgressionAction.SPAWN_RESEARCHER, role="researcher")]
        execute_results = [{"run_id": 1}]

        with (
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
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=Path("/tmp/test")),
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
            results = await _execute_plan_decisions(decisions)

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
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=Path("/tmp/test")),
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
            results = await _execute_plan_decisions(decisions)

        assert len(results) == 2
        assert results[0] == {"run_id": 1}
        assert results[1] == {"error": "agent slot full", "issue_number": 43}


class TestTaskQueueRouter:
    """Tests for the task queue CRUD endpoints."""

    @pytest.fixture
    def app(self, tmp_path):
        from sova.dashboard.app import create_app

        # Write a minimal sova.toml so queue writes have a file to update
        (tmp_path / "sova.toml").write_text('[project]\ngithub_repo = "test/repo"\n')

        with patch("sova.dashboard.app.recover_stale_runs", new_callable=AsyncMock):
            with patch("sova.dashboard.app.list_projects", return_value={}):
                return create_app(project_dir=tmp_path)

    @pytest.fixture
    def project_dir(self, tmp_path):
        return tmp_path

    async def test_get_queue_empty(self, app, project_dir) -> None:
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/queue")
        assert resp.status_code == 200
        assert resp.json()["queue"] == []

    async def test_add_to_queue(self, app, project_dir) -> None:
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/queue", json={"issue_number": 42})
        assert resp.status_code == 201
        assert resp.json()["queue"] == [42]

    async def test_add_duplicate_returns_409(self, app, project_dir) -> None:
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post("/api/supervisor/queue", json={"issue_number": 42})
                resp = await client.post("/api/supervisor/queue", json={"issue_number": 42})
        assert resp.status_code == 409

    async def test_set_queue_with_reorder(self, app, project_dir) -> None:
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post("/api/supervisor/queue", json={"issue_number": 10})
                await client.post("/api/supervisor/queue", json={"issue_number": 20})
                resp = await client.put("/api/supervisor/queue", json={"issue_numbers": [20, 10]})
        assert resp.status_code == 200
        assert resp.json()["queue"] == [20, 10]

    async def test_set_queue_deduplicates(self, app, project_dir) -> None:
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put("/api/supervisor/queue", json={"issue_numbers": [5, 5, 10, 5]})
        assert resp.status_code == 200
        assert resp.json()["queue"] == [5, 10]

    async def test_remove_from_queue(self, app, project_dir) -> None:
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post("/api/supervisor/queue", json={"issue_number": 42})
                await client.post("/api/supervisor/queue", json={"issue_number": 43})
                resp = await client.delete("/api/supervisor/queue/42")
        assert resp.status_code == 200
        assert resp.json()["queue"] == [43]

    async def test_remove_nonexistent_returns_404(self, app, project_dir) -> None:
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.delete("/api/supervisor/queue/999")
        assert resp.status_code == 404

    async def test_clear_queue(self, app, project_dir) -> None:
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post("/api/supervisor/queue", json={"issue_number": 42})
                resp = await client.delete("/api/supervisor/queue")
        assert resp.status_code == 200
        assert resp.json()["queue"] == []

    async def test_queue_persists_to_toml(self, app, project_dir) -> None:
        """Verify queue changes are written to sova.toml."""
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post("/api/supervisor/queue", json={"issue_number": 42})

        import tomllib

        with open(project_dir / "sova.toml", "rb") as f:
            data = tomllib.load(f)
        assert data["supervisor"]["task_queue"] == [42]


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

        (tmp_path / "sova.toml").write_text('[project]\ngithub_repo = "test/repo"\n')
        with patch("sova.dashboard.app.recover_stale_runs", new_callable=AsyncMock):
            with patch("sova.dashboard.app.list_projects", return_value={}):
                app = create_app(project_dir=tmp_path)

        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=tmp_path):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/queue", json={"issue_number": 0})
        assert resp.status_code == 422


class TestTaskQueueErrorPaths:
    """Tests for queue error paths: malformed TOML, write failures, negative numbers."""

    @pytest.fixture
    def app(self, tmp_path):
        from sova.dashboard.app import create_app

        (tmp_path / "sova.toml").write_text('[project]\ngithub_repo = "test/repo"\n')

        with patch("sova.dashboard.app.recover_stale_runs", new_callable=AsyncMock):
            with patch("sova.dashboard.app.list_projects", return_value={}):
                return create_app(project_dir=tmp_path)

    @pytest.fixture
    def project_dir(self, tmp_path):
        return tmp_path

    async def test_malformed_toml_returns_503_on_save(self, app, project_dir) -> None:
        """_save_task_queue with corrupt TOML returns 503.

        We patch load_config to succeed (returning an empty queue) so the endpoint
        reaches _save_task_queue, which then hits the corrupt file.
        """
        mock_cfg = MagicMock()
        mock_cfg.supervisor.task_queue = []
        (project_dir / "sova.toml").write_text("{{{{not valid toml")
        with (
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/queue", json={"issue_number": 42})
        assert resp.status_code == 503

    async def test_negative_issue_number_rejected(self, app, project_dir) -> None:
        """POST /queue with negative issue_number returns 422."""
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/queue", json={"issue_number": -5})
        assert resp.status_code == 422

    async def test_set_queue_negative_rejected(self, app, project_dir) -> None:
        """PUT /queue with negative issue_numbers returns 422."""
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put("/api/supervisor/queue", json={"issue_numbers": [1, -2, 3]})
        assert resp.status_code == 422

    async def test_set_queue_zero_rejected(self, app, project_dir) -> None:
        """PUT /queue with zero issue_numbers returns 422."""
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put("/api/supervisor/queue", json={"issue_numbers": [0]})
        assert resp.status_code == 422

    async def test_write_failure_returns_503(self, app, project_dir) -> None:
        """Writing queue returns 503 when file I/O fails."""
        with (
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
            patch("pathlib.Path.write_text", side_effect=OSError("disk full")),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put("/api/supervisor/queue", json={"issue_numbers": [42]})
        assert resp.status_code == 503

    def test_save_task_queue_atomic_write_cleanup(self, tmp_path) -> None:
        """Verify temp file is cleaned up on write failure."""
        from sova.dashboard.routers.supervisor import _save_task_queue

        (tmp_path / "sova.toml").write_text('[project]\ngithub_repo = "test/repo"\n')
        tmp_file = tmp_path / "sova.toml.tmp"

        with patch.object(Path, "replace", side_effect=OSError("disk error")):
            with pytest.raises(Exception):
                _save_task_queue(tmp_path, [42])
        # Temp file should be cleaned up
        assert not tmp_file.exists()


class TestSaveTaskQueueEdgeCases:
    """Tests for _save_task_queue edge cases."""

    def test_save_creates_toml_when_missing(self, tmp_path) -> None:
        from sova.dashboard.routers.supervisor import _save_task_queue

        _save_task_queue(tmp_path, [42, 43])
        import tomllib

        with open(tmp_path / "sova.toml", "rb") as f:
            data = tomllib.load(f)
        assert data["supervisor"]["task_queue"] == [42, 43]

    def test_save_preserves_existing_config(self, tmp_path) -> None:
        from sova.dashboard.routers.supervisor import _save_task_queue

        (tmp_path / "sova.toml").write_text('[project]\ngithub_repo = "test/repo"\n')
        _save_task_queue(tmp_path, [10])
        import tomllib

        with open(tmp_path / "sova.toml", "rb") as f:
            data = tomllib.load(f)
        assert data["project"]["github_repo"] == "test/repo"
        assert data["supervisor"]["task_queue"] == [10]

    def test_save_with_none_project_dir(self, tmp_path, monkeypatch) -> None:
        from sova.dashboard.routers.supervisor import _save_task_queue

        monkeypatch.chdir(tmp_path)
        _save_task_queue(None, [1])
        import tomllib

        with open(tmp_path / "sova.toml", "rb") as f:
            data = tomllib.load(f)
        assert data["supervisor"]["task_queue"] == [1]


class TestAutoResearchDefault:
    """Verify auto_research defaults to False per architecture.md."""

    def test_auto_research_defaults_to_false(self) -> None:
        from sova.config.models import SupervisorConfig

        cfg = SupervisorConfig()
        assert cfg.auto_research is False


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
