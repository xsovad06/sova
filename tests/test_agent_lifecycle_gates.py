"""agent_lifecycle pre-spawn gates: network, memory pressure and budgets.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.app import create_app
from sova.db.session import get_session
from tests.conftest import csrf_request_headers as _csrf_request_headers

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows", "no_launch_gh")


# ---------------------------------------------------------------------------
# Per-issue budget check
# ---------------------------------------------------------------------------


class TestIssueBudgetCheck:
    """_check_issue_budget must block spawns when cumulative cost exceeds limit."""

    async def test_blocks_over_budget_issue(self) -> None:
        from pathlib import Path
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _check_issue_budget
        from sova.db.models import IssueLifecycle
        from sova.db.session import get_session as real_get_session

        async with await get_session() as session:
            async with session.begin():
                lifecycle = IssueLifecycle(
                    issue_number="200",
                    current_phase="development",
                    total_cost_usd=Decimal("55.00"),
                )
                session.add(lifecycle)

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with patch("sova.dashboard.services.agent_validation.get_session", side_effect=_ignore_project_dir):
            result = await _check_issue_budget("200", Path.cwd())

        assert result is not None
        assert "error" in result
        assert "exceeded" in result["error"]
        assert result["total_cost_usd"] == "55.000000"

    async def test_allows_under_budget_issue(self) -> None:
        from pathlib import Path
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _check_issue_budget
        from sova.db.models import IssueLifecycle
        from sova.db.session import get_session as real_get_session

        async with await get_session() as session:
            async with session.begin():
                lifecycle = IssueLifecycle(
                    issue_number="201",
                    current_phase="development",
                    total_cost_usd=Decimal("5.00"),
                )
                session.add(lifecycle)

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with patch("sova.dashboard.services.agent_validation.get_session", side_effect=_ignore_project_dir):
            result = await _check_issue_budget("201", Path.cwd())

        assert result is None

    async def test_allows_no_lifecycle(self) -> None:
        """No prior lifecycle for the issue: first run, always allowed."""
        from pathlib import Path
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _check_issue_budget
        from sova.db.session import get_session as real_get_session

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with patch("sova.dashboard.services.agent_validation.get_session", side_effect=_ignore_project_dir):
            result = await _check_issue_budget("999", Path.cwd())
        assert result is None


# ---------------------------------------------------------------------------
# Budget override
# ---------------------------------------------------------------------------


class TestBudgetOverride:
    """Budget override: structured 409 response, DB recording, workflow bypass."""

    async def test_budget_error_includes_code_field(self) -> None:
        """_check_issue_budget returns code='budget_exceeded' for structured routing."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _check_issue_budget
        from sova.db.models import IssueLifecycle
        from sova.db.session import get_session as real_get_session

        async with await get_session() as session:
            async with session.begin():
                session.add(
                    IssueLifecycle(
                        issue_number="500",
                        current_phase="development",
                        total_cost_usd=Decimal("55.00"),
                    )
                )

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with patch("sova.dashboard.services.agent_validation.get_session", side_effect=_ignore_project_dir):
            result = await _check_issue_budget("500", Path.cwd())

        assert result is not None
        assert result["code"] == "budget_exceeded"
        assert "exceeded" in result["error"]

    async def test_start_agent_returns_structured_409_on_budget(self) -> None:
        """POST /agents/start returns a structured 409 with code + spend + limit."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import control_service

        budget_error = {
            "code": "budget_exceeded",
            "error": "Issue #42 has exceeded the per-issue budget ($55.00 / $50.00). Use --force to bypass.",
            "total_cost_usd": "55.000000",
            "max_issue_budget": "50.000000",
        }
        mock_start = AsyncMock(return_value=budget_error)

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            with patch.object(control_service, "start_agent", mock_start):
                resp = await ac.post("/api/agents/start", json={"issue": "42"}, headers=_csrf_request_headers(ac))

        assert resp.status_code == 409
        body = resp.json()
        detail = body["detail"]
        assert detail["code"] == "budget_exceeded"
        assert detail["total_cost_usd"] == "55.000000"
        assert detail["max_issue_budget"] == "50.000000"

    async def test_start_agent_conflict_error_no_budget_code(self) -> None:
        """Non-budget 409 errors must NOT include code='budget_exceeded'."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import control_service

        conflict_error = {"error": "Issue #42 already has a running agent"}
        mock_start = AsyncMock(return_value=conflict_error)

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            with patch.object(control_service, "start_agent", mock_start):
                resp = await ac.post("/api/agents/start", json={"issue": "42"}, headers=_csrf_request_headers(ac))

        assert resp.status_code == 409
        body = resp.json()
        detail = body["detail"]
        assert isinstance(detail, str)
        assert "budget" not in detail.lower()

    async def test_record_budget_override_persists_to_db(self) -> None:
        """_record_budget_override creates a BudgetOverride row."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _record_budget_override
        from sova.db.models import BudgetOverride, TaskRun
        from sova.db.session import get_session as real_get_session

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="600", role="developer", status="running")
                session.add(run)
                await session.flush()
                run_id = run.id

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        budget_error = {
            "total_cost_usd": "55.000000",
            "max_issue_budget": "50.000000",
        }

        with patch("sova.dashboard.services.agent_lifecycle.get_session", side_effect=_ignore_project_dir):
            await _record_budget_override("600", run_id, budget_error, Path.cwd())

        async with await get_session() as session:
            from sqlalchemy import select

            stmt = select(BudgetOverride).where(BudgetOverride.issue_number == "600")
            rows = (await session.execute(stmt)).scalars().all()

        assert len(rows) == 1
        assert rows[0].task_run_id == run_id
        assert float(rows[0].spend_usd) == 55.0
        assert float(rows[0].limit_usd) == 50.0

    async def test_record_budget_override_db_failure_logged_not_raised(self) -> None:
        """_record_budget_override swallows DB errors and logs a warning instead of raising."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _record_budget_override

        budget_error = {
            "total_cost_usd": "55.000000",
            "max_issue_budget": "50.000000",
        }

        with (
            patch("sova.dashboard.services.agent_lifecycle.get_session", side_effect=RuntimeError("db unavailable")),
            patch("sova.dashboard.services.agent_lifecycle.log") as mock_log,
        ):
            await _record_budget_override("600", 1, budget_error, Path.cwd())

        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args[0][0] == "agent.budget_override_record_failed"

    async def test_budget_override_start_records_override_when_over_budget(self, tmp_path: Path) -> None:
        """start_agent with budget_override=True records the override when issue is over budget."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 99999

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        budget_error = {
            "code": "budget_exceeded",
            "error": "Over budget",
            "total_cost_usd": "55.000000",
            "max_issue_budget": "50.000000",
        }

        mock_record = AsyncMock()

        original = get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", side_effect=lambda *a, **kw: mock_process),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=77),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_update_task_run_output_path", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(
                agent_lifecycle,
                "_resolve_branch_name",
                new_callable=AsyncMock,
                return_value="feat/issue-600",
            ),
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=tmp_path / "wt",
            ),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(
                agent_lifecycle,
                "_check_issue_budget",
                new_callable=AsyncMock,
                return_value=budget_error,
            ),
            patch.object(agent_lifecycle, "_record_budget_override", mock_record),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
            patch("sova.dashboard.services.agent_lifecycle.get_session", side_effect=_ignore_project_dir),
            patch("sova.dashboard.services.agent_validation.get_session", side_effect=_ignore_project_dir),
        ):
            result = await start_agent("600", budget_override=True)

        assert result.get("status") == "started"
        mock_record.assert_awaited_once()
        call_args = mock_record.call_args
        assert call_args[0][0] == "600"
        assert call_args[0][1] == 77

    async def test_force_alone_does_not_bypass_budget(self, tmp_path: Path) -> None:
        """start_agent with force=True but budget_override=False still returns the budget error.

        Regression test: force is used by unrelated callers (CLI --force,
        resume_from_approval) to skip conflict/state checks. It must NOT
        silently bypass the budget guard: only an explicit budget_override
        confirmation should do that.
        """
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        budget_error = {
            "code": "budget_exceeded",
            "error": "Over budget",
            "total_cost_usd": "55.000000",
            "max_issue_budget": "50.000000",
        }

        mock_record = AsyncMock()

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value=""),
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=tmp_path / "wt",
            ),
            patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value=budget_error),
            patch.object(agent_lifecycle, "_record_budget_override", mock_record),
        ):
            result = await start_agent("600", force=True, budget_override=False)

        assert "error" in result
        assert result["code"] == "budget_exceeded"
        mock_record.assert_not_awaited()

    async def test_no_force_no_override_recorded(self, tmp_path: Path) -> None:
        """start_agent without force returns budget error, does not record override."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        budget_error = {
            "code": "budget_exceeded",
            "error": "Over budget",
            "total_cost_usd": "55.000000",
            "max_issue_budget": "50.000000",
        }

        mock_record = AsyncMock()

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value=""),
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=tmp_path / "wt",
            ),
            patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value=budget_error),
            patch.object(agent_lifecycle, "_record_budget_override", mock_record),
        ):
            result = await start_agent("600", force=False)

        assert "error" in result
        assert result["code"] == "budget_exceeded"
        mock_record.assert_not_awaited()


class TestWorkflowBudgetForceBypass:
    """WorkflowEngine._check_per_issue_budget skips only when ctx.budget_override is True."""

    async def test_budget_override_skips_per_issue_budget(self) -> None:
        from unittest.mock import MagicMock

        from sova.core.workflow import WorkflowEngine, WorkflowResult

        ctx = MagicMock()
        ctx.budget_override = True
        ctx.issue_number = "42"

        engine = object.__new__(WorkflowEngine)
        engine._ctx = ctx

        result = WorkflowResult(success=False, final_status="pending", task_run_id=1)
        await engine._check_per_issue_budget(result)
        assert result.error is None

    async def test_force_alone_does_not_skip_per_issue_budget(self) -> None:
        """force=True without budget_override must still hit the hard budget stop.

        Regression test for the HIGH finding: resume_from_approval() and other
        force=True callers unrelated to budget overrides must not silently
        bypass this guard.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.core.workflow import WorkflowEngine, WorkflowResult
        from sova.db.models import TaskRun

        async with await get_session() as session:
            async with session.begin():
                session.add(
                    TaskRun(
                        issue_number="700",
                        role="developer",
                        status="done",
                        total_cost_usd=Decimal("50.00"),
                    )
                )

        ctx = MagicMock()
        ctx.force = True
        ctx.budget_override = False
        ctx.issue_number = "700"
        ctx.config.agent.max_issue_budget = Decimal("10.00")

        engine = object.__new__(WorkflowEngine)
        engine._ctx = ctx
        engine._task_run_id = 9998

        result = WorkflowResult(success=False, final_status="pending", task_run_id=9998)

        with (
            patch.object(engine, "_write_output", new_callable=AsyncMock),
            patch.object(engine, "_close_output", new_callable=AsyncMock),
            patch.object(engine, "_record_failure", new_callable=AsyncMock),
            patch.object(engine, "_update_task_run_status", new_callable=AsyncMock),
        ):
            await engine._check_per_issue_budget(result)

        assert result.error is not None
        assert "budget exceeded" in result.error.lower()

    async def test_no_force_checks_budget(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.core.workflow import WorkflowEngine, WorkflowResult
        from sova.db.models import TaskRun

        async with await get_session() as session:
            async with session.begin():
                session.add(
                    TaskRun(
                        issue_number="42",
                        role="developer",
                        status="done",
                        total_cost_usd=Decimal("50.00"),
                    )
                )

        ctx = MagicMock()
        ctx.force = False
        ctx.budget_override = False
        ctx.issue_number = "42"
        ctx.config.agent.max_issue_budget = Decimal("10.00")

        engine = object.__new__(WorkflowEngine)
        engine._ctx = ctx
        engine._task_run_id = 9999

        result = WorkflowResult(success=False, final_status="pending", task_run_id=9999)

        with (
            patch.object(engine, "_write_output", new_callable=AsyncMock),
            patch.object(engine, "_close_output", new_callable=AsyncMock),
            patch.object(engine, "_record_failure", new_callable=AsyncMock),
            patch.object(engine, "_update_task_run_status", new_callable=AsyncMock),
        ):
            await engine._check_per_issue_budget(result)

        assert result.error is not None
        assert "budget exceeded" in result.error.lower()


# ---------------------------------------------------------------------------
# Memory pressure gate
# ---------------------------------------------------------------------------


class TestNetworkConnectivityPreSpawnCheck:
    """check_network_connectivity blocks spawns while the network is unreachable."""

    @staticmethod
    def _cfg(*, enabled: bool = True, block_spawns: bool = True):
        from types import SimpleNamespace

        return SimpleNamespace(network_guard=SimpleNamespace(enabled=enabled, block_spawns=block_spawns))

    @staticmethod
    def _status(*, is_down: bool):
        from sova.supervisor.network_health import ConnectivityStatus

        return ConnectivityStatus(
            is_down=is_down,
            down_for_seconds=45.0 if is_down else 0.0,
            healthy_for_seconds=0.0 if is_down else 600.0,
            consecutive_failures=2 if is_down else 0,
            last_source="probe",
        )

    def test_blocks_while_unreachable(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_network_connectivity

        with (
            patch("sova.dashboard.services.agent_validation.load_config", return_value=self._cfg()),
            patch("sova.supervisor.network_health.get_connectivity_status", return_value=self._status(is_down=True)),
        ):
            block, warn = check_network_connectivity(tmp_path)

        assert block is not None
        assert block["code"] == "network_unreachable"
        assert "45s" in block["error"]
        # Reachability is binary, so unlike memory pressure there is no warn tier.
        assert warn is None

    def test_clear_when_reachable(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_network_connectivity

        with (
            patch("sova.dashboard.services.agent_validation.load_config", return_value=self._cfg()),
            patch("sova.supervisor.network_health.get_connectivity_status", return_value=self._status(is_down=False)),
        ):
            assert check_network_connectivity(tmp_path) == (None, None)

    def test_respects_disabled_config(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_network_connectivity

        with patch("sova.supervisor.network_health.get_connectivity_status", return_value=self._status(is_down=True)):
            with patch("sova.dashboard.services.agent_validation.load_config", return_value=self._cfg(enabled=False)):
                assert check_network_connectivity(tmp_path) == (None, None)
            with patch(
                "sova.dashboard.services.agent_validation.load_config", return_value=self._cfg(block_spawns=False)
            ):
                assert check_network_connectivity(tmp_path) == (None, None)

    def test_fails_open_when_unevaluable(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_network_connectivity

        # An unloadable config must not stop every spawn on the machine.
        with patch("sova.dashboard.services.agent_validation.load_config", side_effect=RuntimeError("bad config")):
            assert check_network_connectivity(tmp_path) == (None, None)


class TestMemoryPressureGate:
    """check_memory_pressure blocks, warns, or clears based on available memory."""

    def setup_method(self) -> None:
        import sova.dashboard.services.resource_service as rs

        rs._metrics_cache.clear()

    def test_blocks_when_below_block_threshold(self, tmp_path: Path) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        cfg = SimpleNamespace(memory_guard=guard)
        mem = SimpleNamespace(available=int(0.5 * 1024**3))

        with (
            patch("sova.dashboard.services.agent_validation.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mem),
        ):
            block, warn = check_memory_pressure(tmp_path)

        assert block is not None
        assert "error" in block
        assert "Insufficient" in block["error"]
        assert warn is None

    def test_warns_when_below_warn_threshold(self, tmp_path: Path) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        cfg = SimpleNamespace(memory_guard=guard)
        mem = SimpleNamespace(available=int(2.5 * 1024**3))

        with (
            patch("sova.dashboard.services.agent_validation.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mem),
        ):
            block, warn = check_memory_pressure(tmp_path)

        assert block is None
        assert warn is not None
        assert "Low memory" in warn

    def test_clears_when_sufficient_memory(self, tmp_path: Path) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        cfg = SimpleNamespace(memory_guard=guard)
        mem = SimpleNamespace(available=int(8.0 * 1024**3))

        with (
            patch("sova.dashboard.services.agent_validation.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mem),
        ):
            block, warn = check_memory_pressure(tmp_path)

        assert block is None
        assert warn is None

    def test_disabled_skips_check(self, tmp_path: Path) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        guard = SimpleNamespace(enabled=False, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        cfg = SimpleNamespace(memory_guard=guard)

        with patch("sova.dashboard.services.agent_validation.load_config", return_value=cfg):
            block, warn = check_memory_pressure(tmp_path)

        assert block is None
        assert warn is None

    def test_psutil_failure_is_fail_open(self, tmp_path: Path) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        cfg = SimpleNamespace(memory_guard=guard)

        with (
            patch("sova.dashboard.services.agent_validation.load_config", return_value=cfg),
            patch("psutil.virtual_memory", side_effect=OSError("no psutil")),
        ):
            block, warn = check_memory_pressure(tmp_path)

        assert block is None
        assert warn is None

    def test_config_load_failure_is_fail_open(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        with patch(
            "sova.dashboard.services.agent_validation.load_config",
            side_effect=RuntimeError("bad config"),
        ):
            block, warn = check_memory_pressure(tmp_path)

        assert block is None
        assert warn is None

    def test_block_includes_available_gb(self, tmp_path: Path) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        cfg = SimpleNamespace(memory_guard=guard)
        mem = SimpleNamespace(available=int(0.5 * 1024**3))

        with (
            patch("sova.dashboard.services.agent_validation.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mem),
        ):
            block, _ = check_memory_pressure(tmp_path)

        assert block is not None
        assert block["available_gb"] == 0.5
        assert block["block_threshold_gb"] == 1.0

    async def test_start_agent_blocked_by_memory_pressure(self) -> None:
        """start_agent returns error when memory is below block threshold."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services import agent_lifecycle

        mem_error = {"error": "Insufficient memory: 0.5 GB available"}

        with (
            patch.object(agent_lifecycle, "_get_project_agents") as mock_gpa,
            patch.object(agent_lifecycle, "check_memory_pressure", return_value=(mem_error, None)),
        ):
            from sova.dashboard.services.agent_pool import ProjectAgents

            pa = ProjectAgents()
            pa.project_dir = MagicMock()
            mock_gpa.return_value = pa

            result = await agent_lifecycle.start_agent("42")

        assert "error" in result
        assert "Insufficient memory" in result["error"]

    async def test_start_agent_force_bypasses_memory_gate(self, tmp_path: Path) -> None:
        """start_agent with force=True skips the memory check."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle

        mock_process = MagicMock()
        mock_process.pid = 99

        with (
            patch.object(agent_lifecycle, "_get_project_agents") as mock_gpa,
            patch.object(agent_lifecycle, "spawn_direct", new_callable=AsyncMock, return_value=mock_process),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=1),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_update_task_run_output_path", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "check_memory_pressure") as mock_mem,
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            from sova.dashboard.services.agent_pool import ProjectAgents

            pa = ProjectAgents()
            pa.project_dir = tmp_path
            mock_gpa.return_value = pa

            result = await agent_lifecycle.start_agent("42", force=True)

        assert "error" not in result
        mock_mem.assert_not_called()

    async def test_start_command_blocked_by_memory_pressure(self) -> None:
        """start_command returns error when memory is below block threshold."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle

        mem_error = {"error": "Insufficient memory: 0.5 GB available"}

        with (
            patch.object(agent_lifecycle, "_get_project_agents") as mock_gpa,
            patch.object(agent_lifecycle, "check_memory_pressure", return_value=(mem_error, None)),
            patch.object(
                agent_lifecycle, "_resolve_command_context", new_callable=AsyncMock, return_value=(None, "42")
            ),
        ):
            from sova.dashboard.services.agent_pool import ProjectAgents

            pa = ProjectAgents()
            pa.project_dir = MagicMock()
            mock_gpa.return_value = pa

            result = await agent_lifecycle.start_command("integrate-pr", args={"issue": "42"})

        assert "error" in result
        assert "Insufficient memory" in result["error"]

    def test_system_metrics_pressure_level_critical(self) -> None:
        """get_system_metrics includes memory_pressure='critical' when below block threshold."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services import resource_service

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        mem = SimpleNamespace(
            total=16 * 1024**3,
            available=int(0.5 * 1024**3),
            percent=97.0,
        )

        with (
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.virtual_memory", return_value=mem),
            patch("psutil.cpu_count", return_value=8),
            patch.object(resource_service, "_get_memory_guard_config", return_value=guard),
        ):
            result = resource_service.get_system_metrics()

        assert result["system"]["memory_pressure"] == "critical"
        assert result["system"]["memory_available_gb"] == 0.5

    def test_system_metrics_pressure_level_warning(self) -> None:
        """get_system_metrics includes memory_pressure='warning' when below warn threshold."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services import resource_service

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        mem = SimpleNamespace(
            total=16 * 1024**3,
            available=int(2.5 * 1024**3),
            percent=85.0,
        )

        with (
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.virtual_memory", return_value=mem),
            patch("psutil.cpu_count", return_value=8),
            patch.object(resource_service, "_get_memory_guard_config", return_value=guard),
        ):
            result = resource_service.get_system_metrics()

        assert result["system"]["memory_pressure"] == "warning"

    def test_system_metrics_pressure_level_ok(self) -> None:
        """get_system_metrics includes memory_pressure='ok' when above all thresholds."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services import resource_service

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        mem = SimpleNamespace(
            total=16 * 1024**3,
            available=int(8.0 * 1024**3),
            percent=50.0,
        )

        with (
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.virtual_memory", return_value=mem),
            patch("psutil.cpu_count", return_value=8),
            patch.object(resource_service, "_get_memory_guard_config", return_value=guard),
        ):
            result = resource_service.get_system_metrics()

        assert result["system"]["memory_pressure"] == "ok"


# ---------------------------------------------------------------------------
# check_memory_pressure
# ---------------------------------------------------------------------------


class TestCheckMemoryPressure:
    def test_returns_block_when_below_block_threshold(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from sova.config.models import MemoryGuardConfig, ProjectConfig
        from sova.dashboard.services.agent_validation import check_memory_pressure

        cfg = ProjectConfig(memory_guard=MemoryGuardConfig(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=2.0))
        mock_mem = MagicMock()
        mock_mem.available = int(1.0 * 1024**3)  # 1.0 GB, below block_threshold_gb=2.0

        with (
            patch("sova.dashboard.services.agent_validation.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mock_mem),
        ):
            block, warn = check_memory_pressure(tmp_path)

        assert block is not None
        assert "Insufficient memory" in block["error"]
        assert block["available_gb"] == 1.0
        assert warn is None

    def test_returns_warning_when_below_warn_threshold(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from sova.config.models import MemoryGuardConfig, ProjectConfig
        from sova.dashboard.services.agent_validation import check_memory_pressure

        cfg = ProjectConfig(memory_guard=MemoryGuardConfig(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=2.0))
        mock_mem = MagicMock()
        mock_mem.available = int(3.0 * 1024**3)  # 3.0 GB, below warn=4.0 but above block=2.0

        with (
            patch("sova.dashboard.services.agent_validation.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mock_mem),
        ):
            block, warn = check_memory_pressure(tmp_path)

        assert block is None
        assert warn is not None
        assert "Low memory" in warn

    def test_returns_none_when_sufficient(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from sova.config.models import MemoryGuardConfig, ProjectConfig
        from sova.dashboard.services.agent_validation import check_memory_pressure

        cfg = ProjectConfig(memory_guard=MemoryGuardConfig(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=2.0))
        mock_mem = MagicMock()
        mock_mem.available = int(8.0 * 1024**3)

        with (
            patch("sova.dashboard.services.agent_validation.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mock_mem),
        ):
            block, warn = check_memory_pressure(tmp_path)

        assert block is None
        assert warn is None

    def test_returns_none_when_disabled(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.config.models import MemoryGuardConfig, ProjectConfig
        from sova.dashboard.services.agent_validation import check_memory_pressure

        cfg = ProjectConfig(
            memory_guard=MemoryGuardConfig(enabled=False, warn_threshold_gb=4.0, block_threshold_gb=2.0)
        )

        with patch("sova.dashboard.services.agent_validation.load_config", return_value=cfg):
            block, warn = check_memory_pressure(tmp_path)

        assert block is None
        assert warn is None

    def test_fail_open_on_exception(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        with patch("sova.dashboard.services.agent_validation.load_config", side_effect=RuntimeError("boom")):
            block, warn = check_memory_pressure(tmp_path)

        assert block is None
        assert warn is None


# ---------------------------------------------------------------------------
# _get_memory_guard_config and get_system_metrics memory pressure fields
# ---------------------------------------------------------------------------


class TestResourceServiceMemoryPressure:
    def setup_method(self) -> None:
        import sova.dashboard.services.resource_service as rs

        rs._metrics_cache.clear()

    def test_get_memory_guard_config_returns_config(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        import sova.dashboard.services.resource_service as rs
        from sova.config.models import MemoryGuardConfig, ProjectConfig

        cfg = ProjectConfig(memory_guard=MemoryGuardConfig(enabled=True, warn_threshold_gb=5.0, block_threshold_gb=2.0))

        with (
            patch("sova.dashboard.services.resource_service.load_config", return_value=cfg),
            patch("sova.config.context.get_project_dir", return_value=tmp_path),
        ):
            result = rs._get_memory_guard_config()

        assert result is not None
        assert result.warn_threshold_gb == 5.0
        assert result.block_threshold_gb == 2.0

    def test_get_memory_guard_config_returns_none_on_error(self) -> None:
        from unittest.mock import patch

        import sova.dashboard.services.resource_service as rs

        with patch("sova.dashboard.services.resource_service.load_config", side_effect=RuntimeError("no config")):
            result = rs._get_memory_guard_config()

        assert result is None

    def test_system_metrics_includes_memory_pressure_critical(self) -> None:
        from unittest.mock import MagicMock, patch

        import sova.dashboard.services.resource_service as rs
        from sova.config.models import MemoryGuardConfig

        guard = MemoryGuardConfig(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=2.0)

        mock_mem = MagicMock()
        mock_mem.available = int(1.0 * 1024**3)  # 1.0 GB
        mock_mem.total = int(16.0 * 1024**3)
        mock_mem.percent = 93.75

        with (
            patch("psutil.virtual_memory", return_value=mock_mem),
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.cpu_count", return_value=8),
            patch.object(rs, "_get_memory_guard_config", return_value=guard),
            patch.object(rs, "_get_project_agents", return_value=MagicMock(agents={}, max_concurrent=3)),
        ):
            result = rs.get_system_metrics()

        assert result["available"] is True
        assert result["system"]["memory_pressure"] == "critical"
        assert result["system"]["memory_available_gb"] == 1.0

    def test_system_metrics_includes_memory_pressure_warning(self) -> None:
        from unittest.mock import MagicMock, patch

        import sova.dashboard.services.resource_service as rs
        from sova.config.models import MemoryGuardConfig

        guard = MemoryGuardConfig(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=2.0)

        mock_mem = MagicMock()
        mock_mem.available = int(3.0 * 1024**3)  # 3.0 GB (between block=2 and warn=4)
        mock_mem.total = int(16.0 * 1024**3)
        mock_mem.percent = 81.25

        with (
            patch("psutil.virtual_memory", return_value=mock_mem),
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.cpu_count", return_value=8),
            patch.object(rs, "_get_memory_guard_config", return_value=guard),
            patch.object(rs, "_get_project_agents", return_value=MagicMock(agents={}, max_concurrent=3)),
        ):
            result = rs.get_system_metrics()

        assert result["system"]["memory_pressure"] == "warning"

    def test_system_metrics_pressure_ok_when_guard_disabled(self) -> None:
        from unittest.mock import MagicMock, patch

        import sova.dashboard.services.resource_service as rs
        from sova.config.models import MemoryGuardConfig

        guard = MemoryGuardConfig(enabled=False, warn_threshold_gb=4.0, block_threshold_gb=2.0)

        mock_mem = MagicMock()
        mock_mem.available = int(1.0 * 1024**3)  # low but guard disabled
        mock_mem.total = int(16.0 * 1024**3)
        mock_mem.percent = 93.75

        with (
            patch("psutil.virtual_memory", return_value=mock_mem),
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.cpu_count", return_value=8),
            patch.object(rs, "_get_memory_guard_config", return_value=guard),
            patch.object(rs, "_get_project_agents", return_value=MagicMock(agents={}, max_concurrent=3)),
        ):
            result = rs.get_system_metrics()

        assert result["system"]["memory_pressure"] == "ok"

    def test_system_metrics_pressure_ok_when_guard_none(self) -> None:
        from unittest.mock import MagicMock, patch

        import sova.dashboard.services.resource_service as rs

        mock_mem = MagicMock()
        mock_mem.available = int(1.0 * 1024**3)
        mock_mem.total = int(16.0 * 1024**3)
        mock_mem.percent = 93.75

        with (
            patch("psutil.virtual_memory", return_value=mock_mem),
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.cpu_count", return_value=8),
            patch.object(rs, "_get_memory_guard_config", return_value=None),
            patch.object(rs, "_get_project_agents", return_value=MagicMock(agents={}, max_concurrent=3)),
        ):
            result = rs.get_system_metrics()

        assert result["system"]["memory_pressure"] == "ok"
