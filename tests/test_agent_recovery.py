"""Tests for sova.dashboard.services.agent_recovery."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sova.db.models import TaskRun
from sova.db.session import get_session

# agent_recovery.py resolves `get_session` via a function-local `from sova.db.session
# import get_session` on every call (not a module-level import), so patching
# "sova.db.session.get_session" at its definition module takes effect correctly here,
# unlike the general rule of patching at the importing module's namespace.


# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


class TestControlServiceRecovery:
    async def test_recover_stale_runs_marks_dead_processes(self) -> None:
        """Runs with dead PIDs should be marked as interrupted."""
        from sova.dashboard.services.control_service import recover_stale_runs

        # Use get_session so data and recovery share the same connection
        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="99",
                role="developer",
                status="running",
                pid=999999,  # PID that doesn't exist
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        interrupted = await recover_stale_runs()

        assert len(interrupted) == 1
        assert interrupted[0]["issue"] == "99"
        assert interrupted[0]["run_id"] == run_id

        # Verify DB was updated (same session factory)
        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"
            assert updated.ended_at is not None

    async def test_recover_stale_runs_no_stale(self) -> None:
        """No running TaskRuns means nothing to recover."""
        from sova.dashboard.services.control_service import recover_stale_runs

        session = await get_session()
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="dev", status="done"))

        interrupted = await recover_stale_runs()
        assert interrupted == []

    async def test_recover_stale_runs_no_pid(self) -> None:
        """Runs without a PID (legacy) should also be marked interrupted."""
        from sova.dashboard.services.control_service import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="50", role="auto", status="running", pid=None)
            session.add(run)
            await session.flush()
            run_id = run.id

        interrupted = await recover_stale_runs()

        assert len(interrupted) == 1

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"

    async def test_recover_stale_runs_skips_paused(self) -> None:
        """Paused runs (gate failures) must not be clobbered to interrupted."""
        from sova.dashboard.services.control_service import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="77",
                role="researcher",
                status="paused",
                pid=999999,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        interrupted = await recover_stale_runs()
        assert all(r["run_id"] != run_id for r in interrupted)

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "paused"

    async def test_is_process_alive(self) -> None:
        """Process liveness check should work for known PIDs."""
        import os

        from sova.dashboard.services.control_service import _is_process_alive

        # Current process is alive
        assert _is_process_alive(os.getpid()) is True
        # Non-existent PID
        assert _is_process_alive(999999) is False

    async def test_recover_stale_runs_catches_pending_status(self) -> None:
        """recover_stale_runs should catch runs stuck in non-running non-terminal states."""
        from sova.dashboard.services.agent_recovery import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="83", role="dev", status="pending", pid=999999)
            session.add(run)
            await session.flush()
            run_id = run.id

        interrupted = await recover_stale_runs()
        assert len(interrupted) == 1
        assert interrupted[0]["issue"] == "83"

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"
            assert "pending" in updated.error_message


class TestRecoveryMergeCheck:
    async def test_recover_merge_role_checks_pr_merged(self) -> None:
        """Merge-role runs should be marked 'done' if PR was actually merged."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.control_service import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="113",
                role="command:integrate-pr",
                status="running",
                pid=999999,
                pr_number=130,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        with patch(
            "sova.dashboard.services.agent_lifecycle._check_pr_merged_on_failure",
            new_callable=AsyncMock,
            return_value=True,
        ):
            interrupted = await recover_stale_runs()

        assert len(interrupted) == 0

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "done"
            assert "merged successfully" in updated.error_message

    async def test_recover_merge_role_not_merged_stays_interrupted(self) -> None:
        """Merge-role runs where PR was NOT merged stay interrupted."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.control_service import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="113",
                role="command:integrate-pr",
                status="running",
                pid=999999,
                pr_number=130,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_queue_status = MagicMock()
        mock_queue_status.in_queue = False
        mock_queue_status.is_merged = False
        mock_queue_status.state = "NOT_QUEUED"

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle._check_pr_merged_on_failure",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "sova.git.merge.get_merge_queue_status",
                new_callable=AsyncMock,
                return_value=mock_queue_status,
            ) as mock_queue_check,
        ):
            interrupted = await recover_stale_runs()

        assert len(interrupted) == 1
        mock_queue_check.assert_awaited_once()

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"

    async def test_recover_non_merge_role_ignores_pr(self) -> None:
        """Non-merge roles should not check PR status even if pr_number is set."""
        from sova.dashboard.services.control_service import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="running",
                pid=999999,
                pr_number=130,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        interrupted = await recover_stale_runs()

        assert len(interrupted) == 1

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"


class TestZombieProcessIdentity:
    """Tests for _is_zombie_process and _kill_process."""

    def test_is_zombie_process_returns_true_for_matching_pid(self) -> None:
        """Process created before the run started is treated as the expected agent."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_recovery import _is_zombie_process

        run_start = datetime.now(timezone.utc)
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = (run_start - timedelta(seconds=10)).timestamp()

        with patch("psutil.Process", return_value=mock_proc):
            assert _is_zombie_process(12345, run_start) is True

    def test_is_zombie_process_detects_recycled_pid(self) -> None:
        """Process created well after the run started indicates PID reuse."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_recovery import _is_zombie_process

        run_start = datetime.now(timezone.utc) - timedelta(hours=2)
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = (run_start + timedelta(minutes=30)).timestamp()

        with patch("psutil.Process", return_value=mock_proc):
            assert _is_zombie_process(12345, run_start) is False

    def test_is_zombie_process_falls_back_on_psutil_error(self) -> None:
        """When psutil fails, fall back to basic liveness check."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _is_zombie_process

        with patch("psutil.Process", side_effect=Exception("no psutil")):
            with patch("sova.dashboard.services.agent_recovery._is_process_alive", return_value=True):
                assert _is_zombie_process(12345, datetime.now(timezone.utc)) is True

    def test_is_zombie_process_detects_a_process_far_older_than_the_run(self) -> None:
        """A process created long before the run cannot be its agent: the PID was reused the other way round."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_recovery import _is_zombie_process

        run_start = datetime.now(timezone.utc)
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = (run_start - timedelta(hours=3)).timestamp()

        with patch("psutil.Process", return_value=mock_proc):
            assert _is_zombie_process(12345, run_start) is False

    def test_is_in_process_host_only_for_a_process_far_older_than_the_run(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_recovery import _is_in_process_host

        run_start = datetime.now(timezone.utc)
        older, matching, newer = MagicMock(), MagicMock(), MagicMock()
        older.create_time.return_value = (run_start - timedelta(hours=3)).timestamp()
        matching.create_time.return_value = (run_start - timedelta(seconds=10)).timestamp()
        newer.create_time.return_value = (run_start + timedelta(hours=1)).timestamp()

        for proc, expected in ((older, True), (matching, False), (newer, False)):
            with patch("psutil.Process", return_value=proc):
                assert _is_in_process_host(12345, run_start) is expected
        assert _is_in_process_host(12345, None) is False
        with patch("psutil.Process", side_effect=Exception("gone")):
            assert _is_in_process_host(12345, run_start) is False

    def test_is_zombie_process_with_none_started_at(self) -> None:
        """When started_at is None, skip time comparison and return True for alive PIDs."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_recovery import _is_zombie_process

        mock_proc = MagicMock()
        mock_proc.create_time.return_value = 1000000.0

        with patch("psutil.Process", return_value=mock_proc):
            assert _is_zombie_process(12345, None) is True

    async def test_kill_process_sends_sigterm_then_sigkill(self) -> None:
        """Verify the SIGTERM -> sleep -> SIGKILL escalation."""
        from unittest.mock import call, patch

        from sova.dashboard.services.agent_recovery import _kill_process

        with (
            patch("os.kill") as mock_kill,
            patch("asyncio.sleep") as mock_sleep,
        ):
            import signal

            mock_kill.side_effect = [None, None, None]  # SIGTERM, alive check, SIGKILL
            await _kill_process(99999)
            assert mock_kill.call_args_list[0] == call(99999, signal.SIGTERM)
            assert mock_kill.call_args_list[1] == call(99999, 0)
            assert mock_kill.call_args_list[2] == call(99999, signal.SIGKILL)
            mock_sleep.assert_awaited_once_with(2)

    async def test_kill_process_returns_if_process_gone(self) -> None:
        """When SIGTERM raises OSError, the function returns without SIGKILL."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _kill_process

        with patch("os.kill", side_effect=OSError("no such process")):
            await _kill_process(99999)  # should not raise


class TestTerminalZombieRecovery:
    """Tests for _kill_terminal_zombies and orphan detection."""

    async def test_kill_terminal_zombies_kills_alive_process(self) -> None:
        """Terminal runs with alive PIDs should be killed."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import _kill_terminal_zombies

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="900", role="dev", status="done", pid=999999)
            session.add(run)

        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
        ):
            killed = await _kill_terminal_zombies()

        assert killed >= 1
        mock_kill.assert_any_call(999999)

    async def test_kill_terminal_zombies_skips_dead_process(self) -> None:
        """Terminal runs with dead PIDs should not be killed."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import _kill_terminal_zombies

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="901", role="dev", status="done", pid=999999)
            session.add(run)

        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=False),
            patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
        ):
            await _kill_terminal_zombies()

        assert 999999 not in [c.args[0] for c in mock_kill.call_args_list]

    async def test_kill_terminal_zombies_skips_managed(self) -> None:
        """Terminal runs that are managed by the current dashboard should be skipped."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_pool import _projects
        from sova.dashboard.services.agent_recovery import _kill_terminal_zombies

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="902", role="dev", status="done", pid=999999)
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_pa = MagicMock()
        mock_pa.agents = {run_id: MagicMock()}
        _projects["__test__"] = mock_pa
        try:
            with (
                patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=True),
                patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
            ):
                await _kill_terminal_zombies()
            assert 999999 not in [c.args[0] for c in mock_kill.call_args_list]
        finally:
            _projects.pop("__test__", None)

    async def test_recover_stale_runs_kills_orphan(self) -> None:
        """Alive processes not managed by current dashboard should be killed."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="903", role="dev", status="running", pid=999999)
            session.add(run)
            await session.flush()
            run_id = run.id

        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._is_orphaned_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._recovery_kill_orphans", return_value=True),
            patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
        ):
            interrupted = await recover_stale_runs()

        assert any(r["run_id"] == run_id for r in interrupted)
        mock_kill.assert_any_call(999999)

    async def test_live_unmanaged_run_with_a_live_parent_survives_recovery(self) -> None:
        """A terminal ``sova run`` sharing the DB has a live parent: not an orphan, at startup or in the sweep."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="904", role="developer", status="running", pid=999998)
            session.add(run)
            await session.flush()
            run_id = run.id

        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._is_orphaned_process", return_value=False),
            patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
        ):
            interrupted = await recover_stale_runs()

        mock_kill.assert_not_called()
        assert all(r["run_id"] != run_id for r in interrupted)
        async with await get_session() as check:
            assert (await check.get(TaskRun, run_id)).status == "running"


class TestOrphanAdoption:
    """A live, identity-matched orphan is adopted, never signalled, unless recovery.kill_orphans is on."""

    async def _make_run(self, pid: int = 999997) -> int:
        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="905", role="developer", status="running", pid=pid)
            session.add(run)
            await session.flush()
            return run.id

    async def test_a_live_orphan_is_adopted_and_left_running(self) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        run_id = await self._make_run()
        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._is_orphaned_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._recovery_kill_orphans", return_value=False),
            patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
            patch("sova.dashboard.services.agent_recovery.adopt_orphaned_run", new_callable=AsyncMock) as mock_adopt,
        ):
            interrupted = await recover_stale_runs()

        mock_kill.assert_not_called()
        mock_adopt.assert_awaited_once()
        assert mock_adopt.await_args.args[0].id == run_id
        assert all(r["run_id"] != run_id for r in interrupted)
        async with await get_session() as check:
            assert (await check.get(TaskRun, run_id)).status == "running"

    async def test_a_real_orphan_process_is_adopted_end_to_end(self, tmp_path: Path) -> None:
        """No identity or orphan check is patched: psutil create_time, the marker and the parent are all real."""
        import subprocess
        import sys
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_adopt
        from sova.dashboard.services.agent_pool import ProjectAgents
        from sova.dashboard.services.agent_recovery import recover_stale_runs

        pa = ProjectAgents(project_dir=tmp_path)
        proc = subprocess.Popen(  # noqa: S603 (fixed argv: the current interpreter running a sleep)
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env={**os.environ, "SOVA_AGENT_RUN": "1"},
            start_new_session=True,
        )
        try:
            session = await get_session()
            async with session.begin():
                run = TaskRun(
                    issue_number="906",
                    role="developer",
                    status="running",
                    pid=proc.pid,
                    started_at=datetime.now(timezone.utc),
                )
                session.add(run)
                await session.flush()
                run_id = run.id

            with (
                patch("sova.dashboard.services.agent_recovery._recovery_kill_orphans", return_value=False),
                patch.object(agent_adopt, "_pool_for", return_value=pa),
                patch("sova.dashboard.services.agent_output._read_output", new=AsyncMock()),
                patch("sova.dashboard.services.agent_output._read_stderr", new=AsyncMock()),
                patch("sova.dashboard.services.agent_finalize._wait_and_finalize", new=AsyncMock()),
                patch("sova.dashboard.services.agent_resource._start_resource_monitoring"),
            ):
                interrupted = await recover_stale_runs()

            assert proc.poll() is None
            assert all(r["run_id"] != run_id for r in interrupted)
            assert run_id in pa.agents
            assert pa.agents[run_id].process.pid == proc.pid
            async with await get_session() as check:
                assert (await check.get(TaskRun, run_id)).status == "running"
        finally:
            proc.kill()
            proc.wait(timeout=10)

    async def test_kill_orphans_opt_in_kills_instead_of_adopting(self) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        run_id = await self._make_run()
        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._is_orphaned_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._recovery_kill_orphans", return_value=True),
            patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
            patch("sova.dashboard.services.agent_recovery.adopt_orphaned_run", new_callable=AsyncMock) as mock_adopt,
        ):
            interrupted = await recover_stale_runs()

        mock_adopt.assert_not_called()
        mock_kill.assert_awaited_once_with(999997)
        assert any(r["run_id"] == run_id for r in interrupted)

    async def test_a_recycled_pid_is_marked_interrupted_without_any_signal(self) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        run_id = await self._make_run()
        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=False),
            patch("sova.dashboard.services.agent_recovery._recovery_kill_orphans", return_value=True),
            patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
            patch("os.kill") as os_kill,
            patch("sova.dashboard.services.agent_recovery.adopt_orphaned_run", new_callable=AsyncMock) as mock_adopt,
        ):
            interrupted = await recover_stale_runs()

        mock_kill.assert_not_called()
        os_kill.assert_not_called()
        mock_adopt.assert_not_called()
        assert any(r["run_id"] == run_id for r in interrupted)

    async def test_a_live_in_process_host_run_is_left_running(self) -> None:
        """A `sova watch` or `sova server` older than the run, holding its own PID on it, is live: never interrupted."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        run_id = await self._make_run()
        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=False),
            patch("sova.dashboard.services.agent_recovery._is_in_process_host", return_value=True),
            patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
            patch("sova.dashboard.services.agent_recovery.adopt_orphaned_run", new_callable=AsyncMock) as mock_adopt,
        ):
            interrupted = await recover_stale_runs()

        mock_kill.assert_not_called()
        mock_adopt.assert_not_called()
        assert all(r["run_id"] != run_id for r in interrupted)
        async with await get_session() as check:
            assert (await check.get(TaskRun, run_id)).status == "running"

    async def test_unknown_pool_state_neither_adopts_nor_kills(self) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        run_id = await self._make_run()
        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._is_orphaned_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._recovery_kill_orphans", return_value=True),
            patch("sova.dashboard.services.agent_recovery._get_managed_run_ids", return_value=(set(), False)),
            patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
            patch("sova.dashboard.services.agent_recovery.adopt_orphaned_run", new_callable=AsyncMock) as mock_adopt,
        ):
            interrupted = await recover_stale_runs()

        mock_kill.assert_not_called()
        mock_adopt.assert_not_called()
        assert all(r["run_id"] != run_id for r in interrupted)

    async def test_a_run_the_pool_already_manages_is_not_adopted_again(self) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        run_id = await self._make_run()
        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._is_orphaned_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._get_managed_run_ids", return_value=({run_id}, True)),
            patch("sova.dashboard.services.agent_recovery.adopt_orphaned_run", new_callable=AsyncMock) as mock_adopt,
        ):
            await recover_stale_runs()

        mock_adopt.assert_not_called()

    def test_kill_orphans_defaults_off_and_an_unreadable_config_answers_off(self, monkeypatch) -> None:
        from unittest.mock import patch

        from sova.config.models import ProjectConfig
        from sova.dashboard.services.agent_recovery import _recovery_kill_orphans

        assert ProjectConfig().recovery.kill_orphans is False
        with patch("sova.config.loader.load_config", side_effect=ValueError("bad")):
            assert _recovery_kill_orphans(None) is False


class TestFinishedWhileNoServerRan:
    """A run whose process is gone is recorded from what it left behind, not blindly as interrupted."""

    async def _make_command_run(self, pid: int = 999996) -> int:
        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number=None, role="command:/review-pr", status="running", pid=pid, pr_number=9)
            session.add(run)
            await session.flush()
            return run.id

    async def _recover(self, result_is_error: bool | None):
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=False),
            patch("sova.dashboard.services.agent_recovery._is_in_process_host", return_value=False),
            patch("sova.dashboard.services.agent_recovery._read_result_is_error", return_value=result_is_error),
            patch("sova.dashboard.services.agent_recovery._find_pr_completion_handoff", return_value=False),
        ):
            return await recover_stale_runs()

    async def test_a_dead_command_run_with_a_success_result_is_done(self) -> None:
        run_id = await self._make_command_run()
        interrupted = await self._recover(result_is_error=False)

        assert all(r["run_id"] != run_id for r in interrupted)
        async with await get_session() as check:
            assert (await check.get(TaskRun, run_id)).status == "done"

    async def test_a_dead_command_run_with_an_error_result_is_failed(self) -> None:
        run_id = await self._make_command_run()
        await self._recover(result_is_error=True)

        async with await get_session() as check:
            row = await check.get(TaskRun, run_id)
        assert row.status == "failed"
        assert row.error_message

    async def test_an_unknown_outcome_stays_interrupted(self) -> None:
        run_id = await self._make_command_run()
        interrupted = await self._recover(result_is_error=None)

        assert any(r["run_id"] == run_id for r in interrupted)
        async with await get_session() as check:
            assert (await check.get(TaskRun, run_id)).status == "interrupted"

    async def test_a_pr_handoff_finishes_an_issue_less_run(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        run_id = await self._make_command_run()
        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=False),
            patch("sova.dashboard.services.agent_recovery._is_in_process_host", return_value=False),
            patch("sova.dashboard.services.agent_recovery._find_pr_completion_handoff", return_value=True),
        ):
            await recover_stale_runs()

        async with await get_session() as check:
            assert (await check.get(TaskRun, run_id)).status == "done"

    async def test_the_real_stdout_tail_of_a_successful_command_run_is_read(self, tmp_path: Path) -> None:
        """No parser or reader is patched: a stream-json success `result` line in the .stdout file says done."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _derive_finished_outcome

        out = tmp_path / ".claude" / "agent-output"
        out.mkdir(parents=True)
        (out / "5.stdout").write_text(
            '{"type":"assistant"}\n{"type":"result","is_error":false,"result":"ok"}\n', encoding="utf-8"
        )
        run = SimpleNamespace(id=5, issue_number=None, pr_number=None, role="command:/review-pr", started_at=None)
        with patch("sova.dashboard.services.agent_adopt._fresh_stream_parser", return_value=None):
            assert await _derive_finished_outcome(run, tmp_path) is True

    async def test_a_pipeline_runs_stdout_is_not_trusted(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from sova.dashboard.services.agent_recovery import _derive_finished_outcome

        out = tmp_path / ".claude" / "agent-output"
        out.mkdir(parents=True)
        (out / "5.stdout").write_text('{"type":"result","is_error":false}\n', encoding="utf-8")
        run = SimpleNamespace(id=5, issue_number="7", pr_number=None, role="developer", started_at=None)
        assert await _derive_finished_outcome(run, tmp_path) is None


class TestIsOrphanedProcess:
    """_is_orphaned_process: only a marked agent this dashboard launched, whose launcher is gone."""

    @staticmethod
    def _procs(*, env: dict[str, str], ppid: int, parent_cmdline: list[str] | None = None):  # noqa: ANN205
        from unittest.mock import MagicMock

        child = MagicMock()
        child.environ.return_value = env
        child.ppid.return_value = ppid
        parent = MagicMock()
        parent.cmdline.return_value = parent_cmdline or []
        return lambda pid: child if pid == 777 else parent

    @pytest.mark.parametrize("ppid", [1, 0, -1, "dashboard"])
    def test_marked_agent_under_init_or_this_dashboard_is_an_orphan(self, ppid: int | str) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _is_orphaned_process

        real_ppid = os.getpid() if ppid == "dashboard" else ppid
        with patch("psutil.Process", side_effect=self._procs(env={"SOVA_AGENT_RUN": "1"}, ppid=real_ppid)):
            assert _is_orphaned_process(777) is True

    def test_unmarked_process_under_init_is_someone_elses_run(self) -> None:
        """A `nohup sova run N &` or a service-run `sova watch` is reparented to init but is not an agent."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _is_orphaned_process

        with patch("psutil.Process", side_effect=self._procs(env={}, ppid=1)):
            assert _is_orphaned_process(777) is False

    def test_dashboard_never_signals_itself(self) -> None:
        """`sova server` runs the watch loop in-process, so a TaskRun's pid can be the dashboard's own."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _is_orphaned_process

        with patch("psutil.Process", side_effect=AssertionError("must not probe itself")):
            assert _is_orphaned_process(os.getpid()) is False

    def test_marked_agent_reparented_to_a_subreaper_is_an_orphan(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _is_orphaned_process

        procs = self._procs(env={"SOVA_AGENT_RUN": "1"}, ppid=4242, parent_cmdline=["systemd", "--user"])
        with patch("psutil.Process", side_effect=procs):
            assert _is_orphaned_process(777) is True

    def test_marked_agent_under_a_live_sova_run_is_left_alone(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _is_orphaned_process

        procs = self._procs(env={"SOVA_AGENT_RUN": "1"}, ppid=4242, parent_cmdline=["python", "-m", "sova", "run", "7"])
        with patch("psutil.Process", side_effect=procs):
            assert _is_orphaned_process(777) is False

    @pytest.mark.parametrize(
        "cmdline",
        [
            ["/Users/dsova/bin/tmux", "new", "-s", "sova"],
            ["/usr/bin/python3", "/Users/dsova/Code/sova/scripts/tool.py"],
            ["systemd", "--user"],
        ],
    )
    def test_a_parent_whose_path_merely_contains_sova_is_not_a_sova_launcher(self, cmdline: list[str]) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _is_orphaned_process

        procs = self._procs(env={"SOVA_AGENT_RUN": "1"}, ppid=4242, parent_cmdline=cmdline)
        with patch("psutil.Process", side_effect=procs):
            assert _is_orphaned_process(777) is True

    @pytest.mark.parametrize(
        "cmdline",
        [
            ["/usr/local/bin/sova", "watch"],
            ["python3", "/home/u/.local/bin/sova", "run", "7"],
            ["python", "-m", "sova.cli"],
        ],
    )
    def test_a_parent_running_the_sova_program_is_a_live_launcher(self, cmdline: list[str]) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _is_orphaned_process

        procs = self._procs(env={"SOVA_AGENT_RUN": "1"}, ppid=4242, parent_cmdline=cmdline)
        with patch("psutil.Process", side_effect=procs):
            assert _is_orphaned_process(777) is False

    def test_unverifiable_process_is_left_alone(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _is_orphaned_process

        with patch("psutil.Process", side_effect=OSError("no such process")):
            assert _is_orphaned_process(777) is False

    def test_marker_name_matches_the_spawn_side(self) -> None:
        from sova.dashboard.services.agent_recovery import _AGENT_MARKER_ENV
        from sova.ipc.runtime import _SOVA_AGENT_ENV_KEY

        assert _AGENT_MARKER_ENV == _SOVA_AGENT_ENV_KEY


class TestSynthesizePrActions:
    @pytest.fixture(autouse=True)
    def _synthesis_env(self, monkeypatch, tmp_path):
        """Set up common mocks for synthesize_pr_actions tests."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery
        from sova.git.pr import PRInfo

        agent_recovery._synthesis_cache.clear()
        agent_recovery._issue_pr_cache.clear()

        monkeypatch.setattr("sova.config.context.get_project_dir", lambda: tmp_path)
        mock_cfg = type(
            "Cfg",
            (),
            {
                "github_repo": "user/repo",
                "github_user": "testuser",
                "task_source": type("TS", (), {"type": "github", "github_project_number": 0})(),
            },
        )()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)
        monkeypatch.setattr(
            "sova.git.operations.find_pr_for_issue",
            AsyncMock(return_value=PRInfo(number=99, url="https://github.com/user/repo/pull/99")),
        )

        self.mock_adapter = AsyncMock()
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: self.mock_adapter)

    async def test_returns_address_review_on_changes_requested(self) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services import agent_recovery

        self.mock_adapter.get_pr_reviews.return_value = [
            PRReview(
                reviewer="alice",
                state="CHANGES_REQUESTED",
                body="Fix this",
                submitted_at="2026-01-01T10:00:00Z",
                is_bot=False,
            ),
        ]

        actions = await agent_recovery.synthesize_pr_actions("42")

        assert actions is not None
        assert len(actions) == 1
        assert actions[0]["id"] == "address_review"
        assert actions[0]["mode"] == "agent"
        assert actions[0]["args"]["issue"] == "42"
        assert actions[0]["args"]["pr"] == 99
        assert actions[0]["auto_execute"] is False

    @pytest.mark.parametrize(
        ("reviews", "expected_id"),
        [
            pytest.param(
                [("alice", "APPROVED", "LGTM", "2026-01-01T10:00:00Z", False)],
                "integrate",
                id="integrate-on-all-approved",
            ),
            pytest.param(
                [
                    ("alice", "APPROVED", "LGTM", "2026-01-01T10:00:00Z", False),
                    ("bob", "CHANGES_REQUESTED", "Fix this", "2026-01-01T11:00:00Z", False),
                ],
                "address_review",
                id="changes-requested-takes-priority-over-approval",
            ),
            pytest.param(
                [
                    ("alice", "CHANGES_REQUESTED", "Fix", "2026-01-01T10:00:00Z", False),
                    ("alice", "APPROVED", "LGTM now", "2026-01-01T12:00:00Z", False),
                ],
                "integrate",
                id="deduplicates-reviewer-keeps-latest",
            ),
        ],
    )
    async def test_single_action_for_reviews(
        self, reviews: list[tuple[str, str, str, str, bool]], expected_id: str
    ) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services import agent_recovery

        self.mock_adapter.get_pr_reviews.return_value = [
            PRReview(reviewer=r, state=st, body=b, submitted_at=at, is_bot=bot) for r, st, b, at, bot in reviews
        ]

        actions = await agent_recovery.synthesize_pr_actions("42")

        assert actions is not None
        assert len(actions) == 1
        assert actions[0]["id"] == expected_id

    @pytest.mark.parametrize(
        "review",
        [
            pytest.param(
                ("coderabbit[bot]", "CHANGES_REQUESTED", "Issues", "2026-01-01T10:00:00Z", True), id="only-bot-reviews"
            ),
            pytest.param(("alice", "DISMISSED", "", "2026-01-01T10:00:00Z", False), id="dismissed-reviews-excluded"),
        ],
    )
    async def test_returns_none_for_reviews(self, review: tuple[str, str, str, str, bool]) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services import agent_recovery

        reviewer, state, body, submitted_at, is_bot = review
        self.mock_adapter.get_pr_reviews.return_value = [
            PRReview(reviewer=reviewer, state=state, body=body, submitted_at=submitted_at, is_bot=is_bot)
        ]

        actions = await agent_recovery.synthesize_pr_actions("42")
        assert actions is None

    async def test_returns_none_when_no_pr(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr(
            "sova.git.operations.find_pr_for_issue",
            AsyncMock(return_value=None),
        )

        actions = await agent_recovery.synthesize_pr_actions("42")
        assert actions is None


class TestSummarizeCiChecks:
    def test_returns_unknown_for_none(self) -> None:
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks

        assert _summarize_ci_checks(None) == "unknown"

    def test_returns_none_for_empty(self) -> None:
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks

        assert _summarize_ci_checks([]) == "none"

    def test_returns_passed_when_all_success(self) -> None:
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks
        from sova.git.operations import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(name="test", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url=""),
            CICheck(name="lint", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url=""),
        ]
        assert _summarize_ci_checks(checks) == "passed"

    def test_returns_failed_when_any_failure(self) -> None:
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks
        from sova.git.operations import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(name="test", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url=""),
            CICheck(name="lint", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.FAILURE, details_url=""),
        ]
        assert _summarize_ci_checks(checks) == "failed"

    def test_returns_pending_when_in_progress(self) -> None:
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks
        from sova.git.operations import CheckStatus, CICheck

        checks = [
            CICheck(name="test", status=CheckStatus.IN_PROGRESS, conclusion=None, details_url=""),
        ]
        assert _summarize_ci_checks(checks) == "pending"

    def test_returns_passed_for_completed_non_failure_non_success(self) -> None:
        """Completed checks with non-failure, non-success conclusion fall through to 'passed'."""
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks
        from sova.git.operations import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(name="lint", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.NEUTRAL, details_url=""),
        ]
        assert _summarize_ci_checks(checks) == "passed"

    def test_returns_failed_for_action_required(self) -> None:
        """ACTION_REQUIRED and TIMED_OUT are merge-blocking (CICheck.is_failed treats
        them as such), so this classifier must agree rather than fall through to
        'passed' via a conclusion-only comparison (#1175 CodeRabbit review: finding 1)."""
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks
        from sova.git.operations import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(
                name="deploy-approval",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.ACTION_REQUIRED,
                details_url="",
            ),
        ]
        assert _summarize_ci_checks(checks) == "failed"

    def test_returns_failed_for_timed_out(self) -> None:
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks
        from sova.git.operations import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(
                name="slow-test", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.TIMED_OUT, details_url=""
            ),
        ]
        assert _summarize_ci_checks(checks) == "failed"

    def test_returns_failed_for_all_cancelled(self) -> None:
        """An all-CANCELLED set (the normal effect of a superseding push) must
        not read as 'passed': MonitorCIStep._poll_ci treats the identical set
        as a failure, and this classifier feeds PRFacts.ci_status, which can
        otherwise unlock Integrate for a run that never actually passed CI
        (#1175 CodeRabbit review round 4, finding 1)."""
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks
        from sova.git.operations import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(name="CI", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.CANCELLED, details_url=""),
        ]
        assert _summarize_ci_checks(checks) == "failed"

    def test_returns_passed_for_cancelled_plus_success(self) -> None:
        """A CANCELLED run alongside a genuine SUCCESS still passes: the
        carve-out only applies when nothing passed at all."""
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks
        from sova.git.operations import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(name="flaky", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.CANCELLED, details_url=""),
            CICheck(name="CI", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url=""),
        ]
        assert _summarize_ci_checks(checks) == "passed"


class TestLoadRepoConfig:
    def test_load_repo_config_no_project_dir(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _load_repo_config

        with patch("sova.config.context.get_project_dir", return_value=None):
            assert _load_repo_config() is None

    def test_load_repo_config_exception(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _load_repo_config

        with (
            patch("sova.config.context.get_project_dir", return_value=tmp_path),
            patch(
                "sova.config.loader.load_config",
                side_effect=RuntimeError("bad config"),
            ),
        ):
            assert _load_repo_config() is None

    def test_load_repo_config_no_github_repo(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_recovery import _load_repo_config

        mock_cfg = MagicMock()
        mock_cfg.github_repo = ""
        with (
            patch("sova.config.context.get_project_dir", return_value=tmp_path),
            patch(
                "sova.config.loader.load_config",
                return_value=mock_cfg,
            ),
        ):
            assert _load_repo_config() is None


class TestGetPrStatusForIssue:
    async def test_pr_status_fetch_exception(self) -> None:
        """get_pr_status_for_issue returns error dict when get_pr_status raises."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_recovery import get_pr_status_for_issue

        mock_pr = MagicMock(number=10)
        with (
            patch(
                "sova.dashboard.services.agent_recovery._load_repo_config",
                return_value=("owner/repo", "user"),
            ),
            patch(
                "sova.git.operations.find_pr_for_issue",
                new_callable=AsyncMock,
                return_value=mock_pr,
            ),
            patch(
                "sova.git.operations.get_pr_status",
                new_callable=AsyncMock,
                side_effect=RuntimeError("api error"),
            ),
        ):
            result = await get_pr_status_for_issue("42")
        assert result["has_pr"] is True
        assert result["pr_number"] == 10
        assert "error" in result

    async def test_ci_checks_fetch_exception(self) -> None:
        """get_pr_status_for_issue handles CI check failures gracefully."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_recovery import get_pr_status_for_issue

        mock_pr = MagicMock(number=10)
        mock_status = MagicMock(
            number=10,
            state="OPEN",
            review_decision=None,
            mergeable=True,
            title="test",
            url="http://x",
            is_approved=False,
            is_mergeable=True,
        )
        with (
            patch(
                "sova.dashboard.services.agent_recovery._load_repo_config",
                return_value=("owner/repo", "user"),
            ),
            patch(
                "sova.git.operations.find_pr_for_issue",
                new_callable=AsyncMock,
                return_value=mock_pr,
            ),
            patch(
                "sova.git.operations.get_pr_status",
                new_callable=AsyncMock,
                return_value=mock_status,
            ),
            patch(
                "sova.git.operations.get_ci_checks",
                new_callable=AsyncMock,
                side_effect=RuntimeError("ci api error"),
            ),
            patch(
                "sova.dashboard.services.work_verdict.resolve_sova_verdict",
                new_callable=AsyncMock,
                return_value={"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None},
            ),
        ):
            result = await get_pr_status_for_issue("42")
        assert result["has_pr"] is True
        assert result["ci_status"] == "unknown"

    async def test_empty_checks_with_workflow_run_reports_pending_not_none(self) -> None:
        """An empty check list is ambiguous between "no CI configured" and
        "checks have not registered yet" (get_ci_checks()'s own docstring).
        work_state._unmet_merge_conditions() treats "none" as satisfying the
        CI merge gate, so an uncorroborated "none" here would let a PR whose
        checks are seconds from appearing pass the gate (#1314 CodeRabbit
        review on PR #1317, Round 4 finding: fan-out gap in
        _summarize_ci_checks()). A workflow run existing for the head SHA
        must report "pending" instead."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_recovery import get_pr_status_for_issue

        mock_pr = MagicMock(number=10)
        mock_status = MagicMock(
            number=10,
            state="OPEN",
            review_decision=None,
            mergeable=True,
            title="test",
            url="http://x",
            is_approved=False,
            is_mergeable=True,
        )
        with (
            patch(
                "sova.dashboard.services.agent_recovery._load_repo_config",
                return_value=("owner/repo", "user"),
            ),
            patch(
                "sova.git.operations.find_pr_for_issue",
                new_callable=AsyncMock,
                return_value=mock_pr,
            ),
            patch(
                "sova.git.operations.get_pr_status",
                new_callable=AsyncMock,
                return_value=mock_status,
            ),
            patch(
                "sova.git.operations.get_ci_checks",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "sova.git.operations.get_pr_head_sha",
                new_callable=AsyncMock,
                return_value="abc123def456",
            ),
            patch(
                "sova.git.operations.sha_has_workflow_runs",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_runs,
            patch(
                "sova.dashboard.services.work_verdict.resolve_sova_verdict",
                new_callable=AsyncMock,
                return_value={"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None},
            ),
        ):
            result = await get_pr_status_for_issue("42")
        assert result["ci_status"] == "pending"
        mock_runs.assert_awaited_once()

    async def test_empty_checks_confirmed_by_no_workflow_run_reports_none(self) -> None:
        """The counterpart to the above: a confirmed absence of any workflow
        run for the head SHA is genuine "no CI", so "none" still stands."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_recovery import get_pr_status_for_issue

        mock_pr = MagicMock(number=10)
        mock_status = MagicMock(
            number=10,
            state="OPEN",
            review_decision=None,
            mergeable=True,
            title="test",
            url="http://x",
            is_approved=False,
            is_mergeable=True,
        )
        with (
            patch(
                "sova.dashboard.services.agent_recovery._load_repo_config",
                return_value=("owner/repo", "user"),
            ),
            patch(
                "sova.git.operations.find_pr_for_issue",
                new_callable=AsyncMock,
                return_value=mock_pr,
            ),
            patch(
                "sova.git.operations.get_pr_status",
                new_callable=AsyncMock,
                return_value=mock_status,
            ),
            patch(
                "sova.git.operations.get_ci_checks",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "sova.git.operations.get_pr_head_sha",
                new_callable=AsyncMock,
                return_value="abc123def456",
            ),
            patch(
                "sova.git.operations.sha_has_workflow_runs",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "sova.dashboard.services.work_verdict.resolve_sova_verdict",
                new_callable=AsyncMock,
                return_value={"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None},
            ),
        ):
            result = await get_pr_status_for_issue("42")
        assert result["ci_status"] == "none"


class TestTTLCache:
    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache.clear()
        agent_recovery._synthesis_cache.clear()
        yield
        agent_recovery._issue_pr_cache.clear()
        agent_recovery._synthesis_cache.clear()

    def test_issue_cache_miss(self) -> None:
        from sova.dashboard.services import agent_recovery

        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert not resolved
        assert pr is None
        assert result is None

    def test_issue_cache_sentinel_no_pr(self) -> None:
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["99"] = agent_recovery._SENTINEL_NO_PR
        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert resolved
        assert pr is None
        assert result is None

    def test_issue_cache_pr_known_synthesis_cached(self) -> None:
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["99"] = 42
        agent_recovery._synthesis_cache[("99", 42)] = [{"id": "test"}]
        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert resolved
        assert pr == 42
        assert result == [{"id": "test"}]

    def test_issue_cache_pr_known_synthesis_not_cached(self) -> None:
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["99"] = 42
        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert not resolved
        assert pr == 42
        assert result is None

    def test_issue_cache_pr_none_value(self) -> None:
        """When cached_pr is None (not sentinel), return miss."""
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["99"] = None
        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert not resolved
        assert pr is None
        assert result is None


class TestDeduplicateReviews:
    def test_fallback_string_comparison(self) -> None:
        """When timestamp parsing fails, fall back to string comparison."""
        from sova.dashboard.services.agent_recovery import _deduplicate_reviews

        r1 = type("R", (), {"reviewer": "alice", "state": "APPROVED", "submitted_at": "bad-ts-1", "is_bot": False})()
        attrs = {"reviewer": "alice", "state": "CHANGES_REQUESTED", "submitted_at": "bad-ts-2", "is_bot": False}
        r2 = type("R", (), attrs)()

        result = _deduplicate_reviews([r1, r2])
        assert len(result) == 1
        assert result["alice"].state == "CHANGES_REQUESTED"


class TestInvalidateSynthesisCache:
    def test_clears_both_caches(self) -> None:
        from sova.dashboard.services.agent_recovery import (
            _issue_pr_cache,
            _synthesis_cache,
            invalidate_synthesis_cache,
        )

        _synthesis_cache[("42", 99)] = [{"id": "test"}]
        _issue_pr_cache["42"] = 99

        invalidate_synthesis_cache("42", 99)

        assert ("42", 99) not in _synthesis_cache
        assert "42" not in _issue_pr_cache


class TestFetchAndInterpretReviews:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        from sova.dashboard.services import agent_recovery

        agent_recovery._synthesis_cache.clear()
        agent_recovery._issue_pr_cache.clear()

        monkeypatch.setattr("sova.config.context.get_project_dir", lambda: tmp_path)
        mock_cfg = type(
            "Cfg",
            (),
            {
                "github_repo": "user/repo",
                "github_user": "testuser",
                "task_source": type("TS", (), {"type": "github", "github_project_number": 0})(),
            },
        )()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)

    async def test_caches_none_on_adapter_exception(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

        result = await agent_recovery._fetch_and_interpret_reviews("42", 99, ("42", 99))
        assert result is None
        assert ("42", 99) in agent_recovery._synthesis_cache

    async def test_caches_none_on_empty_reviews(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        mock_adapter = AsyncMock()
        mock_adapter.get_pr_reviews.return_value = []
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        result = await agent_recovery._fetch_and_interpret_reviews("42", 99, ("42", 99))
        assert result is None
        assert ("42", 99) in agent_recovery._synthesis_cache


class TestSynthesizePrActionsCachePaths:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        from sova.dashboard.services import agent_recovery

        agent_recovery._synthesis_cache.clear()
        agent_recovery._issue_pr_cache.clear()

        monkeypatch.setattr("sova.config.context.get_project_dir", lambda: tmp_path)
        mock_cfg = type(
            "Cfg",
            (),
            {
                "github_repo": "user/repo",
                "github_user": "testuser",
                "task_source": type("TS", (), {"type": "github", "github_project_number": 0})(),
            },
        )()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)

    async def test_returns_none_when_no_repo_config(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr("sova.config.context.get_project_dir", lambda: None)

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result is None

    async def test_uses_cached_pr_number(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["42"] = 99

        mock_adapter = AsyncMock()
        mock_adapter.get_pr_reviews.return_value = []
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        mock_find = AsyncMock()
        monkeypatch.setattr("sova.git.operations.find_pr_for_issue", mock_find)

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result is None
        mock_find.assert_not_awaited()

    async def test_returns_cached_synthesis_result(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["42"] = 99
        agent_recovery._synthesis_cache[("42", 99)] = [{"id": "cached_action"}]

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result == [{"id": "cached_action"}]

    async def test_skips_synthesis_when_active_run(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery
        from sova.git.pr import PRInfo

        monkeypatch.setattr(
            "sova.git.operations.find_pr_for_issue",
            AsyncMock(return_value=PRInfo(number=99, url="https://github.com/user/repo/pull/99")),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_recovery._has_active_run",
            AsyncMock(return_value=True),
        )

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result is None

    async def test_continues_when_active_run_check_fails(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.adapters.base import PRReview
        from sova.dashboard.services import agent_recovery
        from sova.git.pr import PRInfo

        monkeypatch.setattr(
            "sova.git.operations.find_pr_for_issue",
            AsyncMock(return_value=PRInfo(number=99, url="https://github.com/user/repo/pull/99")),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_recovery._has_active_run",
            AsyncMock(side_effect=RuntimeError("db error")),
        )

        mock_adapter = AsyncMock()
        mock_adapter.get_pr_reviews.return_value = [
            PRReview(reviewer="alice", state="APPROVED", body="ok", submitted_at="2026-01-01T10:00:00Z", is_bot=False),
        ]
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result is not None
        assert result[0]["id"] == "integrate"

    async def test_synthesis_cache_hit_after_pr_lookup(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery
        from sova.git.pr import PRInfo

        monkeypatch.setattr(
            "sova.git.operations.find_pr_for_issue",
            AsyncMock(return_value=PRInfo(number=99, url="https://github.com/user/repo/pull/99")),
        )

        agent_recovery._synthesis_cache[("42", 99)] = [{"id": "cached"}]

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result == [{"id": "cached"}]

    async def test_strips_hash_from_issue_number(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery
        from sova.git.pr import PRInfo

        mock_find = AsyncMock(return_value=PRInfo(number=99, url="https://github.com/user/repo/pull/99"))
        monkeypatch.setattr("sova.git.operations.find_pr_for_issue", mock_find)

        mock_adapter = AsyncMock()
        mock_adapter.get_pr_reviews.return_value = []
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        await agent_recovery.synthesize_pr_actions("#42")
        mock_find.assert_awaited_once_with("42", repo="user/repo", github_user="testuser")


class TestGetSynthesizedHandoff:
    @staticmethod
    def _mock_session_with_runs(runs, monkeypatch):
        """Build a mock get_session that returns the given runs from the query."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = runs

        @asynccontextmanager
        async def fake_begin():
            yield

        @asynccontextmanager
        async def fake_session():
            session = AsyncMock()
            session.execute.return_value = mock_result
            session.begin = fake_begin
            yield session

        async def get_session():
            return fake_session()

        monkeypatch.setattr("sova.db.session.get_session", get_session)

    async def test_returns_handoff_for_recent_done_run(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        agent_recovery._synthesis_cache.clear()
        agent_recovery._issue_pr_cache.clear()

        mock_run = type(
            "Run",
            (),
            {
                "issue_number": "42",
                "pr_number": 99,
                "ended_at": datetime.now(timezone.utc),
                "started_at": datetime.now(timezone.utc),
            },
        )()

        self._mock_session_with_runs([mock_run], monkeypatch)

        mock_actions = [{"id": "integrate", "label": "Integrate PR"}]
        monkeypatch.setattr(
            "sova.dashboard.services.agent_recovery.synthesize_pr_actions",
            AsyncMock(return_value=mock_actions),
        )

        result = await agent_recovery.get_synthesized_handoff()
        assert result is not None
        assert result["source"] == "pr-review-state"
        assert result["issue"] == "42"
        assert result["pr_number"] == 99

    async def test_returns_none_when_no_runs(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_recovery

        self._mock_session_with_runs([], monkeypatch)

        result = await agent_recovery.get_synthesized_handoff()
        assert result is None

    async def test_returns_none_on_exception(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr("sova.db.session.get_session", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

        result = await agent_recovery.get_synthesized_handoff()
        assert result is None

    async def test_skips_runs_without_issue_number(self, monkeypatch) -> None:

        from sova.dashboard.services import agent_recovery

        mock_run = type(
            "Run",
            (),
            {
                "issue_number": None,
                "pr_number": 99,
                "ended_at": datetime.now(timezone.utc),
                "started_at": datetime.now(timezone.utc),
            },
        )()

        self._mock_session_with_runs([mock_run], monkeypatch)

        result = await agent_recovery.get_synthesized_handoff()
        assert result is None


class TestGetRecoveryConfig:
    @pytest.mark.parametrize(
        ("repo", "user", "expected"),
        [
            pytest.param("user/repo", "alice", ("user/repo", "alice"), id="returns-repo-and-user"),
            pytest.param(None, None, ("", ""), id="missing-fields-fall-back-to-empty-string"),
        ],
    )
    def test_reads_repo_and_user(
        self, monkeypatch, repo: str | None, user: str | None, expected: tuple[str, str]
    ) -> None:
        from sova.dashboard.services.agent_recovery import _get_recovery_config

        mock_cfg = type("Cfg", (), {"github_repo": repo, "github_user": user})()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)

        assert _get_recovery_config(None) == expected

    def test_load_config_exception_returns_empty_tuple(self, monkeypatch) -> None:
        from sova.dashboard.services.agent_recovery import _get_recovery_config

        monkeypatch.setattr("sova.config.loader.load_config", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

        assert _get_recovery_config(None) == ("", "")


class TestGetPrBranchForRecovery:
    async def test_no_repo_returns_empty_string(self) -> None:
        from sova.dashboard.services.agent_recovery import _get_pr_branch_for_recovery

        result = await _get_pr_branch_for_recovery(1, "", None)
        assert result == ""

    @pytest.mark.parametrize(
        ("success", "stdout", "expected"),
        [
            pytest.param(True, "feat/issue-42\n", "feat/issue-42", id="returns-branch-on-success"),
            pytest.param(False, "", "", id="returns-empty-string-on-failed-command"),
        ],
    )
    async def test_gh_command_result(self, monkeypatch, success: bool, stdout: str, expected: str) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr(agent_recovery, "_get_recovery_config", lambda _: ("user/repo", "alice"))
        monkeypatch.setattr("sova.utils.gh.resolve_gh_env", AsyncMock(return_value={}))
        monkeypatch.setattr(
            "sova.utils.shell.run",
            AsyncMock(return_value=type("Result", (), {"success": success, "stdout": stdout})()),
        )

        result = await agent_recovery._get_pr_branch_for_recovery(42, "user/repo", None)
        assert result == expected

    async def test_returns_empty_string_on_exception(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr(agent_recovery, "_get_recovery_config", lambda _: ("user/repo", "alice"))
        monkeypatch.setattr("sova.utils.gh.resolve_gh_env", AsyncMock(side_effect=RuntimeError("network error")))

        result = await agent_recovery._get_pr_branch_for_recovery(42, "user/repo", None)
        assert result == ""


class TestGetManagedRunIds:
    def test_returns_run_ids_from_all_projects(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_pool
        from sova.dashboard.services.agent_recovery import _get_managed_run_ids

        pa1 = agent_pool.ProjectAgents()
        pa1.agents[1] = object()
        pa1.agents[2] = object()
        pa2 = agent_pool.ProjectAgents()
        pa2.agents[3] = object()

        monkeypatch.setattr(agent_pool, "_projects", {"proj1": pa1, "proj2": pa2})

        ids, loaded_ok = _get_managed_run_ids()
        assert loaded_ok is True
        assert ids == {1, 2, 3}

    def test_returns_empty_and_not_loaded_on_exception(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_pool
        from sova.dashboard.services.agent_recovery import _get_managed_run_ids

        class BadDict(dict):
            def values(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(agent_pool, "_projects", BadDict())

        ids, loaded_ok = _get_managed_run_ids()
        assert ids == set()
        assert loaded_ok is False


class TestGetInterruptedRunsAndTotal:
    async def test_returns_interrupted_runs(self) -> None:
        from sova.dashboard.services.agent_recovery import get_interrupted_runs_and_total

        session = await get_session()
        async with session.begin():
            session.add(TaskRun(issue_number="80", role="developer", status="interrupted"))
            session.add(TaskRun(issue_number="81", role="developer", status="done"))

        runs, total = await get_interrupted_runs_and_total()
        assert len(runs) == 1
        assert runs[0]["issue_number"] == "80"
        assert total == 1

    async def test_respects_limit_but_total_counts_all(self) -> None:
        from sova.dashboard.services.agent_recovery import get_interrupted_runs_and_total

        session = await get_session()
        async with session.begin():
            for i in range(3):
                session.add(TaskRun(issue_number=str(90 + i), role="developer", status="interrupted"))

        runs, total = await get_interrupted_runs_and_total(limit=2)
        assert len(runs) == 2
        assert total == 3

    async def test_excludes_runs_outside_recency_window(self) -> None:
        """A run interrupted long ago must not keep the banner permanently stuck."""
        from sova.dashboard.services.agent_recovery import get_interrupted_runs_and_total

        stale = datetime.now(timezone.utc) - timedelta(days=30)
        session = await get_session()
        async with session.begin():
            session.add(TaskRun(issue_number="80", role="developer", status="interrupted", started_at=stale))
            session.add(TaskRun(issue_number="81", role="developer", status="interrupted"))

        runs, total = await get_interrupted_runs_and_total()
        assert len(runs) == 1
        assert runs[0]["issue_number"] == "81"
        assert total == 1

    async def test_includes_run_ended_recently_even_if_started_long_ago(self) -> None:
        """Recovery can mark a long-running run interrupted; the window keys on when
        it ended (recovery time), not when it started, or the banner never shows it."""
        from sova.dashboard.services.agent_recovery import get_interrupted_runs_and_total

        old_start = datetime.now(timezone.utc) - timedelta(hours=30)
        recent_end = datetime.now(timezone.utc)
        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="80",
                    role="developer",
                    status="interrupted",
                    started_at=old_start,
                    ended_at=recent_end,
                )
            )

        runs, total = await get_interrupted_runs_and_total()
        assert len(runs) == 1
        assert runs[0]["issue_number"] == "80"
        assert total == 1

    async def test_returns_empty_on_exception(self, monkeypatch) -> None:
        from sova.dashboard.services import run_service
        from sova.dashboard.services.agent_recovery import get_interrupted_runs_and_total

        async def _boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(run_service, "list_runs", _boom)

        runs, total = await get_interrupted_runs_and_total()
        assert runs == []
        assert total == 0

    async def test_orders_by_recency_window_key_not_started_at(self) -> None:
        """A run that started earlier but was interrupted most recently must sort
        first: the window and total both key on coalesce(ended_at, started_at), so
        the list order must use the same column or the newest interruption can be
        the row `limit` cuts off."""
        from sova.dashboard.services.agent_recovery import get_interrupted_runs_and_total

        now = datetime.now(timezone.utc)
        session = await get_session()
        async with session.begin():
            # Started 20h ago, interrupted 5 minutes ago.
            session.add(
                TaskRun(
                    issue_number="80",
                    role="developer",
                    status="interrupted",
                    started_at=now - timedelta(hours=20),
                    ended_at=now - timedelta(minutes=5),
                )
            )
            # Started more recently, but interrupted earlier.
            session.add(
                TaskRun(
                    issue_number="81",
                    role="developer",
                    status="interrupted",
                    started_at=now - timedelta(hours=2),
                    ended_at=now - timedelta(hours=1, minutes=30),
                )
            )

        runs, total = await get_interrupted_runs_and_total(limit=1)
        assert total == 2
        assert len(runs) == 1
        assert runs[0]["issue_number"] == "80"


class TestDismissInterruptedRuns:
    async def test_preserve_diagnostic_keeps_original_error_message(self) -> None:
        """A relaunch-triggered dismiss must not overwrite the interruption cause
        get_failure_taxonomy() classifies on."""
        from sova.dashboard.services.agent_recovery import dismiss_interrupted_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="80",
                role="developer",
                status="interrupted",
                error_message="Stale run recovered on startup (was 'developing')",
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        count = await dismiss_interrupted_runs([run_id], preserve_diagnostic=True)
        assert count == 1

        session2 = await get_session()
        async with session2.begin():
            refreshed = await session2.get(TaskRun, run_id)
            assert refreshed.status == "failed"
            assert refreshed.error_message == "Stale run recovered on startup (was 'developing')"

    async def test_default_overwrites_error_message(self) -> None:
        from sova.dashboard.services.agent_recovery import dismiss_interrupted_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="80", role="developer", status="interrupted", error_message="original")
            session.add(run)
            await session.flush()
            run_id = run.id

        count = await dismiss_interrupted_runs([run_id])
        assert count == 1

        session2 = await get_session()
        async with session2.begin():
            refreshed = await session2.get(TaskRun, run_id)
            assert refreshed.error_message == "Dismissed by user"

    async def test_returns_negative_one_on_exception(self, monkeypatch) -> None:
        """0 is a legitimate 'nothing matched' result, so a swallowed DB error must
        use a sentinel outside that range rather than looking like a no-op success."""
        from sova.dashboard.services.agent_recovery import dismiss_interrupted_runs
        from sova.db import session as session_module

        async def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(session_module, "get_session", _boom)

        count = await dismiss_interrupted_runs([1])
        assert count == -1


class TestInterpretReviews:
    def test_ignores_dismissed_and_bot_reviews(self) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services.agent_recovery import _interpret_reviews

        reviews = {
            "alice": PRReview(reviewer="alice", state="DISMISSED", body="", submitted_at="t", is_bot=False),
            "coderabbit": PRReview(
                reviewer="coderabbit", state="CHANGES_REQUESTED", body="", submitted_at="t", is_bot=True
            ),
        }

        has_changes, approvals, count = _interpret_reviews(reviews)
        assert has_changes is False
        assert approvals == 0
        assert count == 0

    def test_counts_changes_requested_and_approvals(self) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services.agent_recovery import _interpret_reviews

        reviews = {
            "alice": PRReview(reviewer="alice", state="CHANGES_REQUESTED", body="", submitted_at="t", is_bot=False),
            "bob": PRReview(reviewer="bob", state="APPROVED", body="", submitted_at="t", is_bot=False),
            "carol": PRReview(reviewer="carol", state="COMMENTED", body="", submitted_at="t", is_bot=False),
        }

        has_changes, approvals, count = _interpret_reviews(reviews)
        assert has_changes is True
        assert approvals == 1
        assert count == 3


class TestBuildReviewActions:
    def test_build_address_review_action_shape(self) -> None:
        from sova.dashboard.services.agent_recovery import _build_address_review_action

        actions = _build_address_review_action("42", 99)
        assert len(actions) == 1
        action = actions[0]
        assert action["id"] == "address_review"
        assert action["mode"] == "agent"
        assert action["auto_execute"] is False
        assert action["args"] == {"issue": "42", "pr": 99, "role": "developer"}

    def test_build_integrate_actions_shape(self) -> None:
        from sova.dashboard.services.agent_recovery import _build_integrate_actions

        actions = _build_integrate_actions("42", 99)
        assert len(actions) == 1
        action = actions[0]
        assert action["id"] == "integrate"
        assert action["mode"] == "claude-command"
        assert action["command"] == "/integrate-pr 99"
        assert action["args"] == {"issue": "42", "pr": 99}


class TestRollbackIssueState:
    async def test_missing_run_returns_without_error(self) -> None:
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        await rollback_issue_state(999999)  # no such run id; should not raise

    async def test_missing_issue_number_skips_rollback(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number=None, role="developer", status="failed")
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_create_adapter = AsyncMock()
        monkeypatch.setattr("sova.adapters.create_adapter", mock_create_adapter)

        await rollback_issue_state(run_id)
        mock_create_adapter.assert_not_called()

    async def test_skips_when_concurrent_run_exists(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            failed_run = TaskRun(issue_number="70", role="developer", status="failed")
            other_run = TaskRun(issue_number="70", role="developer", status="developing")
            session.add_all([failed_run, other_run])
            await session.flush()
            run_id = failed_run.id

        mock_create_adapter = AsyncMock()
        monkeypatch.setattr("sova.adapters.create_adapter", mock_create_adapter)

        await rollback_issue_state(run_id)
        mock_create_adapter.assert_not_called()

    async def test_triage_role_removes_agent_labels(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.adapters.base import Task
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="71", role="triage", status="failed")
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_adapter = AsyncMock()
        mock_adapter.get_task.return_value = Task(
            id="71", title="t", labels=["agent:triaged", "type:bug", "agent:in-progress"]
        )
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: object())
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        await rollback_issue_state(run_id)

        assert mock_adapter.remove_label.await_count == 2
        removed = {c.args[1] for c in mock_adapter.remove_label.await_args_list}
        assert removed == {"agent:triaged", "agent:in-progress"}
        mock_adapter.transition_state.assert_not_called()

    async def test_developer_with_pr_rolls_back_to_in_review(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.adapters.base import TaskState
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="72", role="developer", status="failed", pr_number=88)
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_adapter = AsyncMock()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: object())
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        await rollback_issue_state(run_id)

        mock_adapter.transition_state.assert_awaited_once_with("72", TaskState.IN_REVIEW)

    async def test_developer_without_pr_rolls_back_to_researched(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.adapters.base import TaskState
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="73", role="developer", status="failed")
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_adapter = AsyncMock()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: object())
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        await rollback_issue_state(run_id)

        mock_adapter.transition_state.assert_awaited_once_with("73", TaskState.RESEARCHED)

    async def test_command_prefix_role_is_parsed(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.adapters.base import TaskState
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="74", role="command:address-pr", status="failed")
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_adapter = AsyncMock()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: object())
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        await rollback_issue_state(run_id)

        mock_adapter.transition_state.assert_awaited_once_with("74", TaskState.IN_REVIEW)

    async def test_unknown_role_does_not_transition(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="75", role="planner", status="failed")
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_adapter = AsyncMock()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: object())
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        await rollback_issue_state(run_id)

        mock_adapter.transition_state.assert_not_called()

    async def test_adapter_exception_is_swallowed(self, monkeypatch) -> None:
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="76", role="developer", status="failed")
            session.add(run)
            await session.flush()
            run_id = run.id

        monkeypatch.setattr("sova.config.loader.load_config", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

        await rollback_issue_state(run_id)  # must not raise
