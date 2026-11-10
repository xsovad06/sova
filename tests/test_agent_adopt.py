"""Tests for sova.dashboard.services.agent_adopt: adopting agents that outlived the previous server."""

from __future__ import annotations

import errno
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.dashboard.services import agent_adopt
from sova.dashboard.services.agent_pool import ProjectAgents
from sova.ipc.control import FileAgentProcess

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


@pytest.fixture(autouse=True)
def _release_adoption_claims():
    yield
    for root, run_id in list(agent_adopt._adoption_claims):
        agent_adopt._release_claim(Path(root), run_id)


def _run(run_id: int = 11, pid: int = 4242, issue: str | None = "77") -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.pid = pid
    run.issue_number = issue
    run.role = "developer"
    run.pr_number = None
    run.started_at = datetime.now(timezone.utc)
    return run


@pytest.fixture
def background():
    """Neutralise the tasks adoption starts; each is an AsyncMock so create_task has a coroutine."""
    with (
        patch("sova.dashboard.services.agent_output._read_output", new=AsyncMock()) as reader,
        patch("sova.dashboard.services.agent_output._read_stderr", new=AsyncMock()) as stderr,
        patch("sova.dashboard.services.agent_finalize._wait_and_finalize", new=AsyncMock()) as finalize,
        patch("sova.dashboard.services.agent_resource._start_resource_monitoring") as monitoring,
    ):
        yield {"reader": reader, "stderr": stderr, "finalize": finalize, "monitoring": monitoring}


class TestAdoptOrphanedRun:
    async def test_registers_the_run_in_the_pool_and_starts_managing_it(self, tmp_path: Path, background) -> None:
        pa = ProjectAgents(project_dir=tmp_path)
        out = tmp_path / ".claude" / "agent-output"
        out.mkdir(parents=True)
        (out / "11.stdout").write_text("before restart\n", encoding="utf-8")

        with patch.object(agent_adopt, "_pool_for", return_value=pa):
            adopted = await agent_adopt.adopt_orphaned_run(_run(), tmp_path)

        assert adopted is True
        agent = pa.agents[11]
        assert agent.issue == "77"
        assert agent.role == "developer"
        assert isinstance(agent.process, FileAgentProcess)
        assert agent.process.pid == 4242
        assert agent.process.stdout_path == out / "11.stdout"
        assert agent.process._tail_from_end is True
        background["finalize"].assert_called_once()
        background["monitoring"].assert_called_once()

    async def test_the_adopted_run_is_seen_by_the_issue_conflict_check(self, tmp_path: Path, background) -> None:
        from sova.dashboard.services.agent_validation import _check_issue_conflict

        pa = ProjectAgents(project_dir=tmp_path)
        with patch.object(agent_adopt, "_pool_for", return_value=pa):
            await agent_adopt.adopt_orphaned_run(_run(), tmp_path)

        conflict = await _check_issue_conflict("77", pa)
        assert conflict is not None
        assert conflict["existing_run_id"] == 11

    async def test_elapsed_time_counts_from_the_run_start_not_the_adoption(self, tmp_path: Path, background) -> None:
        import time
        from datetime import timedelta

        pa = ProjectAgents(project_dir=tmp_path)
        run = _run()
        run.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
        with patch.object(agent_adopt, "_pool_for", return_value=pa):
            await agent_adopt.adopt_orphaned_run(run, tmp_path)

        agent = pa.agents[11]
        assert agent.started_at_utc == run.started_at
        assert time.monotonic() - agent.started_at == pytest.approx(7200, abs=60)

    async def test_adopting_twice_registers_once(self, tmp_path: Path, background) -> None:
        pa = ProjectAgents(project_dir=tmp_path)
        with patch.object(agent_adopt, "_pool_for", return_value=pa):
            await agent_adopt.adopt_orphaned_run(_run(), tmp_path)
            await agent_adopt.adopt_orphaned_run(_run(), tmp_path)

        assert list(pa.agents) == [11]
        background["finalize"].assert_called_once()

    async def test_a_failure_leaves_the_run_unmanaged(self, tmp_path: Path, background) -> None:
        pa = ProjectAgents(project_dir=tmp_path)
        with (
            patch.object(agent_adopt, "_pool_for", return_value=pa),
            patch.object(agent_adopt, "FileAgentProcess", side_effect=RuntimeError("boom")),
        ):
            adopted = await agent_adopt.adopt_orphaned_run(_run(), tmp_path)

        assert adopted is False
        assert pa.agents == {}
        background["finalize"].assert_not_called()

    async def test_a_missing_output_file_adopts_without_capture(self, tmp_path: Path, background) -> None:
        pa = ProjectAgents(project_dir=tmp_path)
        with patch.object(agent_adopt, "_pool_for", return_value=pa):
            assert await agent_adopt.adopt_orphaned_run(_run(), tmp_path) is True

        lines = [line async for line in pa.agents[11].process.stdout_lines()]
        assert lines == []


class TestDeriveExitCode:
    @staticmethod
    def _patched(status: str | None, handoff: dict | None = None):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(
            patch("sova.dashboard.services.agent_db._get_task_run_status", new=AsyncMock(return_value=status))
        )
        stack.enter_context(
            patch("sova.dashboard.services.agent_recovery._find_completion_handoff", return_value=handoff)
        )
        return stack

    @staticmethod
    def _stdout(tmp_path: Path, *lines: str) -> Path:
        path = tmp_path / "1.stdout"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    @pytest.mark.parametrize("status", ["done", "awaiting_approval", "paused"])
    async def test_a_run_that_ended_without_failing_exits_zero(self, tmp_path: Path, status: str) -> None:
        with self._patched(status):
            assert await agent_adopt._derive_exit_code(1, "5", None, tmp_path, tmp_path / "1.stdout") == 0

    @pytest.mark.parametrize("status", ["failed", "rejected", "interrupted", "stopped"])
    async def test_any_other_terminal_status_is_a_failure(self, tmp_path: Path, status: str) -> None:
        with self._patched(status):
            assert await agent_adopt._derive_exit_code(1, "5", None, tmp_path, tmp_path / "1.stdout") == 1

    async def test_a_completion_handoff_exits_zero(self, tmp_path: Path) -> None:
        with self._patched("running", {"status": "x"}):
            assert await agent_adopt._derive_exit_code(1, "5", None, tmp_path, tmp_path / "1.stdout") == 0

    async def test_a_command_run_with_a_success_result_line_exits_zero(self, tmp_path: Path) -> None:
        """A command run is finalized only from the exit code: its row never leaves "running" on its own."""
        stdout = self._stdout(
            tmp_path, '{"type":"assistant","message":{}}', '{"type":"result","is_error":false,"result":"ok"}'
        )
        with self._patched("running"):
            assert await agent_adopt._derive_exit_code(1, "5", None, tmp_path, stdout, role="command:/x") == 0

    async def test_an_error_result_line_is_a_failure(self, tmp_path: Path) -> None:
        stdout = self._stdout(tmp_path, '{"type":"result","is_error":true,"result":"boom"}')
        with self._patched("running"):
            assert await agent_adopt._derive_exit_code(1, "5", None, tmp_path, stdout, role="command:/x") == 1

    async def test_the_last_result_line_wins(self, tmp_path: Path) -> None:
        stdout = self._stdout(
            tmp_path, '{"type":"result","is_error":true}', '{"type":"result","is_error":false}', "not json"
        )
        with self._patched("running"):
            assert await agent_adopt._derive_exit_code(1, "5", None, tmp_path, stdout, role="command:/x") == 0

    @pytest.mark.parametrize("role", ["developer", "reviewer", "researcher", ""])
    async def test_a_pipeline_runs_stdout_is_not_trusted(self, tmp_path: Path, role: str) -> None:
        """A direct-spawn run's stdout is CLI output: a stray success line must not turn a killed run into done."""
        from sova.ipc.control import EXIT_OUTCOME_UNKNOWN

        stdout = self._stdout(tmp_path, '{"type":"result","is_error":false,"result":"ok"}')
        with self._patched("running"):
            code = await agent_adopt._derive_exit_code(1, "5", None, tmp_path, stdout, role=role)
        assert code == EXIT_OUTCOME_UNKNOWN

    async def test_no_signal_at_all_is_unknown_not_a_failure_or_a_success(self, tmp_path: Path) -> None:
        from sova.ipc.control import EXIT_OUTCOME_UNKNOWN

        stdout = self._stdout(tmp_path, '{"type":"assistant","message":{}}')
        with self._patched("running"):
            assert await agent_adopt._derive_exit_code(1, "5", None, tmp_path, stdout) == EXIT_OUTCOME_UNKNOWN

    async def test_a_missing_output_file_is_unknown(self, tmp_path: Path) -> None:
        from sova.ipc.control import EXIT_OUTCOME_UNKNOWN

        with self._patched("running"):
            code = await agent_adopt._derive_exit_code(1, "5", None, tmp_path, tmp_path / "absent.stdout")
        assert code == EXIT_OUTCOME_UNKNOWN


class TestDeriveExitCodeFromRuntimeAndPr:
    """Adopted runs whose end is not a Claude `result` line or an issue handoff."""

    async def test_a_codex_turn_completed_tail_exits_zero(self, tmp_path: Path) -> None:
        from sova.ipc.codex import CodexStreamParser

        stdout = tmp_path / "1.stdout"
        stdout.write_text(
            '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.completed","usage":{}}\n', encoding="utf-8"
        )
        with (
            patch("sova.dashboard.services.agent_db._get_task_run_status", new=AsyncMock(return_value="running")),
            patch.object(agent_adopt, "_fresh_stream_parser", return_value=CodexStreamParser()),
        ):
            assert await agent_adopt._derive_exit_code(1, None, None, tmp_path, stdout, role="command:/x") == 0

    async def test_a_codex_turn_failed_tail_is_a_failure(self, tmp_path: Path) -> None:
        from sova.ipc.codex import CodexStreamParser

        stdout = tmp_path / "1.stdout"
        stdout.write_text('{"type":"turn.failed","error":{"message":"boom"}}\n', encoding="utf-8")
        with (
            patch("sova.dashboard.services.agent_db._get_task_run_status", new=AsyncMock(return_value="running")),
            patch.object(agent_adopt, "_fresh_stream_parser", return_value=CodexStreamParser()),
        ):
            assert await agent_adopt._derive_exit_code(1, None, None, tmp_path, stdout, role="command:/x") == 1

    async def test_an_issue_less_reviewer_run_is_finished_by_its_pr_handoff(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        started = datetime.now(timezone.utc).replace(microsecond=0)
        write_handoff_file(
            tmp_path, DashboardHandoff(source="reviewer", status="awaiting_action", pr_number=42, summary="reviewed")
        )
        other = DashboardHandoff(source="reviewer", status="awaiting_action", pr_number=43, summary="other pr")
        with patch("sova.dashboard.services.agent_db._get_task_run_status", new=AsyncMock(return_value="running")):
            done = await agent_adopt._derive_exit_code(
                1, None, started, tmp_path, tmp_path / "1.stdout", role="reviewer", pr_number=42
            )
            other_pr = await agent_adopt._derive_exit_code(
                1, None, started, tmp_path, tmp_path / "1.stdout", role="reviewer", pr_number=other.pr_number
            )
        from sova.ipc.control import EXIT_OUTCOME_UNKNOWN

        assert done == 0
        assert other_pr == EXIT_OUTCOME_UNKNOWN


class TestAdoptedLiveness:
    async def test_an_exited_unreaped_child_is_not_alive_and_wait_returns(self) -> None:
        import asyncio
        import subprocess
        import sys
        import time

        import psutil

        from sova.ipc.control import AdoptedProcess

        child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 (fixed argv)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and psutil.Process(child.pid).status() != psutil.STATUS_ZOMBIE:
                await asyncio.sleep(0.05)
            assert psutil.Process(child.pid).status() == psutil.STATUS_ZOMBIE
            started = datetime.fromtimestamp(psutil.Process(child.pid).create_time(), tz=timezone.utc)
            assert agent_adopt._is_adopted_alive(child.pid, started) is False

            adopted = AdoptedProcess(
                child.pid,
                is_alive=lambda: agent_adopt._is_adopted_alive(child.pid, started),
                resolve_exit_code=AsyncMock(return_value=0),
            )
            assert await asyncio.wait_for(adopted.wait(), timeout=10) == 0
        finally:
            child.poll()

    def test_a_running_process_is_alive(self) -> None:
        import os

        import psutil

        started = datetime.fromtimestamp(psutil.Process().create_time(), tz=timezone.utc)
        assert agent_adopt._is_adopted_alive(os.getpid(), started) is True

    def test_a_process_with_no_recorded_start_time_is_never_alive(self) -> None:
        """Without a creation time a recycled PID cannot be told apart, so it must never be signalled."""
        import os

        assert agent_adopt._is_adopted_alive(os.getpid(), None) is False


class TestAdoptedCommandWorktree:
    @staticmethod
    def _command_run() -> MagicMock:
        run = _run()
        run.role = "command:/address-pr"
        run.pr_number = 9
        run.worktree_path = ""
        run.branch_name = ""
        return run

    async def test_a_pr_command_run_finds_its_issue_worktree_and_logs_the_missing_baseline(
        self, tmp_path: Path, background
    ) -> None:
        pa = ProjectAgents(project_dir=tmp_path)
        wt = tmp_path / ".claude" / "worktrees" / "77"
        wt.mkdir(parents=True)
        with (
            patch.object(agent_adopt, "_pool_for", return_value=pa),
            patch.object(agent_adopt.log, "warning") as warn,
        ):
            assert await agent_adopt.adopt_orphaned_run(self._command_run(), tmp_path) is True

        assert pa.agents[11].worktree_path == wt
        assert pa.agents[11].pre_run_sha is None
        assert "adopt.diff_guard_baseline_unavailable" in [c.args[0] for c in warn.call_args_list]

    async def test_the_runs_recorded_worktree_wins(self, tmp_path: Path, background) -> None:
        pa = ProjectAgents(project_dir=tmp_path)
        recorded = tmp_path / "elsewhere"
        recorded.mkdir()
        run = self._command_run()
        run.worktree_path = str(recorded)
        with patch.object(agent_adopt, "_pool_for", return_value=pa):
            await agent_adopt.adopt_orphaned_run(run, tmp_path)
        assert pa.agents[11].worktree_path == recorded

    async def test_the_recorded_branch_is_looked_up_without_gh(self, tmp_path: Path, background) -> None:
        pa = ProjectAgents(project_dir=tmp_path)
        found = tmp_path / "wt-for-branch"
        run = self._command_run()
        run.issue_number = None
        run.branch_name = "feat/x"
        with (
            patch.object(agent_adopt, "_pool_for", return_value=pa),
            patch("sova.git.worktree.find_worktree_by_branch", new=AsyncMock(return_value=found)) as lookup,
        ):
            await agent_adopt.adopt_orphaned_run(run, tmp_path)
        lookup.assert_awaited_once_with("feat/x", cwd=tmp_path)
        assert pa.agents[11].worktree_path == found

    async def test_adoption_never_prepares_or_creates_a_worktree_or_shells_out(
        self, tmp_path: Path, background
    ) -> None:
        """The agent is live in its checkout: adoption must not repair, recreate, stash or call gh."""
        pa = ProjectAgents(project_dir=tmp_path)
        (tmp_path / ".claude" / "worktrees" / "77").mkdir(parents=True)
        run = self._command_run()
        run.branch_name = "feat/x"
        with (
            patch.object(agent_adopt, "_pool_for", return_value=pa),
            patch("sova.dashboard.services.agent_context._resolve_issue_worktree", new=AsyncMock()) as resolve,
            patch("sova.dashboard.services.agent_context._resolve_branch_name", new=AsyncMock()) as branch,
            patch("sova.git.worktree.ensure_worktree_usable", new=AsyncMock()) as usable,
            patch("sova.git.worktree.create_worktree", new=AsyncMock()) as create,
            patch("sova.utils.shell.run", new=AsyncMock()) as shell,
        ):
            assert await agent_adopt.adopt_orphaned_run(run, tmp_path) is True

        for forbidden in (resolve, branch, usable, create, shell):
            forbidden.assert_not_called()

    async def test_no_worktree_found_adopts_without_one(self, tmp_path: Path, background) -> None:
        pa = ProjectAgents(project_dir=tmp_path)
        run = self._command_run()
        run.issue_number = None
        with patch.object(agent_adopt, "_pool_for", return_value=pa):
            assert await agent_adopt.adopt_orphaned_run(run, tmp_path) is True
        assert pa.agents[11].worktree_path is None

    async def test_a_developer_run_gets_no_worktree(self, tmp_path: Path, background) -> None:
        pa = ProjectAgents(project_dir=tmp_path)
        with patch.object(agent_adopt, "_pool_for", return_value=pa):
            await agent_adopt.adopt_orphaned_run(_run(), tmp_path)
        assert pa.agents[11].worktree_path is None


class TestStreamParserOnAdoption:
    async def test_a_runtime_role_gets_the_runtimes_parser_and_name(self, tmp_path: Path, background) -> None:
        parser = MagicMock()
        runtime = MagicMock()
        runtime.name = "codex"
        runtime.create_stream_parser.return_value = parser
        runtime.stream_reader_drain_timeout.return_value = 12.5
        run = _run()
        run.role = "command:review-pr"

        pa = ProjectAgents(project_dir=tmp_path)
        with (
            patch.object(agent_adopt, "_pool_for", return_value=pa),
            patch("sova.ipc.runtime.get_runtime", return_value=runtime),
        ):
            assert await agent_adopt.adopt_orphaned_run(run, tmp_path) is True

        agent = pa.agents[11]
        assert agent.stream_parser is parser
        assert agent.stream_reader_drain_timeout == 12.5
        assert agent.runtime_name == "codex"

    async def test_a_direct_spawn_role_gets_no_parser(self, tmp_path: Path, background) -> None:
        pa = ProjectAgents(project_dir=tmp_path)
        with (
            patch.object(agent_adopt, "_pool_for", return_value=pa),
            patch("sova.ipc.runtime.get_runtime") as get_runtime,
        ):
            assert await agent_adopt.adopt_orphaned_run(_run(), tmp_path) is True

        get_runtime.assert_not_called()
        agent = pa.agents[11]
        assert agent.stream_parser is None
        assert agent.runtime_name is None

    async def test_a_parser_failure_still_adopts_the_run(self, tmp_path: Path, background) -> None:
        runtime = MagicMock()
        runtime.name = "claude-code"
        runtime.create_stream_parser.side_effect = RuntimeError("boom")
        run = _run()
        run.role = "command:review-pr"

        pa = ProjectAgents(project_dir=tmp_path)
        with (
            patch.object(agent_adopt, "_pool_for", return_value=pa),
            patch("sova.ipc.runtime.get_runtime", return_value=runtime),
        ):
            assert await agent_adopt.adopt_orphaned_run(run, tmp_path) is True

        assert pa.agents[11].stream_parser is None
        assert pa.agents[11].runtime_name == "claude-code"


class TestRuntimeBuildFailureOnAdoption:
    async def test_a_runtime_that_cannot_be_built_still_adopts_the_run(self, tmp_path: Path, background) -> None:
        run = _run()
        run.role = "command:review-pr"
        pa = ProjectAgents(project_dir=tmp_path)
        with (
            patch.object(agent_adopt, "_pool_for", return_value=pa),
            patch("sova.ipc.runtime.get_runtime", side_effect=RuntimeError("runtime config broken")),
        ):
            assert await agent_adopt.adopt_orphaned_run(run, tmp_path) is True

        assert pa.agents[11].stream_parser is None
        assert pa.agents[11].runtime_name is None


class TestAdoptionClaim:
    async def test_a_second_pool_cannot_adopt_an_already_claimed_run(self, tmp_path: Path, background) -> None:
        first = ProjectAgents(project_dir=tmp_path)
        second = ProjectAgents(project_dir=tmp_path)
        with patch.object(agent_adopt, "_pool_for", side_effect=[first, second]):
            assert await agent_adopt.adopt_orphaned_run(_run(), tmp_path) is True
            assert await agent_adopt.adopt_orphaned_run(_run(), tmp_path) is False

        assert 11 in first.agents
        assert second.agents == {}
        background["finalize"].assert_called_once()

    async def test_the_claim_is_released_when_the_wait_task_ends(self, tmp_path: Path, background) -> None:
        import asyncio

        pa = ProjectAgents(project_dir=tmp_path)
        with patch.object(agent_adopt, "_pool_for", return_value=pa):
            await agent_adopt.adopt_orphaned_run(_run(), tmp_path)
        assert (str(tmp_path), 11) in agent_adopt._adoption_claims

        await asyncio.sleep(0)  # the mocked finalize task completes
        await asyncio.sleep(0)
        assert (str(tmp_path), 11) not in agent_adopt._adoption_claims
        assert agent_adopt._claim_run(tmp_path, 11) is True  # free again for a later server
        agent_adopt._release_claim(tmp_path, 11)

    async def test_a_failed_adoption_releases_its_claim(self, tmp_path: Path, background) -> None:
        pa = ProjectAgents(project_dir=tmp_path)
        with (
            patch.object(agent_adopt, "_pool_for", return_value=pa),
            patch.object(agent_adopt, "FileAgentProcess", side_effect=RuntimeError("boom")),
        ):
            assert await agent_adopt.adopt_orphaned_run(_run(), tmp_path) is False
        assert (str(tmp_path), 11) not in agent_adopt._adoption_claims

    async def test_a_claim_held_by_a_live_spawner_blocks_adoption(self, tmp_path: Path, background) -> None:
        """The lock is real: another open file description (a second process) holds it, so nobody adopts."""
        import fcntl

        lock = tmp_path / ".claude" / "agent-output" / "11.adopt.lock"
        lock.parent.mkdir(parents=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            pa = ProjectAgents(project_dir=tmp_path)
            with patch.object(agent_adopt, "_pool_for", return_value=pa):
                assert await agent_adopt.adopt_orphaned_run(_run(), tmp_path) is False
            assert pa.agents == {}
        finally:
            os.close(fd)

    async def test_an_unopenable_lock_file_does_not_block_adoption(self, tmp_path: Path, background) -> None:
        """Permissions on the lock file are not contention: the claim is skipped and the live run is adopted."""
        pa = ProjectAgents(project_dir=tmp_path)
        with (
            patch.object(agent_adopt, "_pool_for", return_value=pa),
            patch.object(agent_adopt.os, "open", side_effect=PermissionError(errno.EACCES, "denied")),
        ):
            assert await agent_adopt.adopt_orphaned_run(_run(), tmp_path) is True
        assert 11 in pa.agents

    async def test_a_spawner_holds_the_claim_until_its_run_ends(self, tmp_path: Path) -> None:
        import asyncio

        done = asyncio.Event()

        async def _run_until_done() -> None:
            await done.wait()

        task = asyncio.create_task(_run_until_done())
        agent_adopt.hold_spawn_claim(tmp_path, 12, task)
        assert (str(tmp_path), 12) in agent_adopt._adoption_claims
        done.set()
        await task
        await asyncio.sleep(0)
        assert (str(tmp_path), 12) not in agent_adopt._adoption_claims

    async def test_a_run_with_no_start_time_is_not_adopted(self, tmp_path: Path, background) -> None:
        run = _run()
        run.started_at = None
        pa = ProjectAgents(project_dir=tmp_path)
        with patch.object(agent_adopt, "_pool_for", return_value=pa):
            assert await agent_adopt.adopt_orphaned_run(run, tmp_path) is False
        assert pa.agents == {}


class TestUnknownOutcomeFinalization:
    def test_classified_as_interrupted_not_failed(self) -> None:
        from sova.dashboard.services.agent_db import _classify_exit_status
        from sova.ipc.control import EXIT_OUTCOME_UNKNOWN

        agent = MagicMock()
        agent.process.termination_record = None
        assert _classify_exit_status(EXIT_OUTCOME_UNKNOWN, agent) == ("interrupted", "outcome_unknown")

    def test_the_failure_message_says_the_outcome_is_unknown(self) -> None:
        from sova.dashboard.services.agent_db import _build_exit_failure_message
        from sova.ipc.control import EXIT_OUTCOME_UNKNOWN

        message = _build_exit_failure_message(EXIT_OUTCOME_UNKNOWN, [], None)
        assert message.startswith("Outcome unknown")
        assert str(EXIT_OUTCOME_UNKNOWN) not in message

    async def test_finalize_neither_notifies_a_failure_nor_fails_the_lifecycle_phase(self, tmp_path: Path) -> None:
        import asyncio

        from sova.dashboard.services import agent_finalize
        from sova.dashboard.services.agent_pool import AgentState
        from sova.ipc.control import EXIT_OUTCOME_UNKNOWN

        process = MagicMock()
        process.wait = AsyncMock(return_value=EXIT_OUTCOME_UNKNOWN)
        agent = AgentState(run_id=5, issue="77", role="developer", process=process, project_dir=tmp_path)
        pa = ProjectAgents(project_dir=tmp_path)
        pa.agents[5] = agent
        finalize_task_run = AsyncMock(return_value=True)

        with (
            patch.object(agent_finalize, "_wait_with_terminal_check", new=AsyncMock(return_value=EXIT_OUTCOME_UNKNOWN)),
            patch.object(agent_finalize, "_finalize_task_run", new=finalize_task_run),
            patch("sova.dashboard.services.agent_resource._finalize_resource_monitoring", new=AsyncMock()),
            patch.object(agent_finalize, "_get_task_run_status", new=AsyncMock(return_value="interrupted")),
            patch.object(agent_finalize, "_announce_agent_finished", new=AsyncMock()),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new=AsyncMock()) as phase,
            patch("sova.ipc.notifications.notify") as notify,
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new=AsyncMock()) as handoff,
        ):
            await asyncio.wait_for(agent_finalize._wait_and_finalize(pa, agent), timeout=10)

        assert finalize_task_run.await_args.kwargs["exit_code"] == EXIT_OUTCOME_UNKNOWN
        notify.assert_not_called()
        phase.assert_awaited_once()  # the phase is stopped, not failed: see lifecycle_service.finalize_phase_from_run
        handoff.assert_not_called()

    def test_the_feed_detail_hides_the_sentinel(self) -> None:
        from decimal import Decimal

        from sova.dashboard.services import agent_db
        from sova.ipc.control import EXIT_OUTCOME_UNKNOWN

        agent = MagicMock()
        agent.issue = "77"
        agent.role = "developer"
        with patch.object(agent_db, "emit_safe") as emit:
            agent_db._emit_finalize_event(
                5, status="interrupted", exit_code=EXIT_OUTCOME_UNKNOWN, agent=agent, cost=Decimal("0")
            )
        assert emit.call_args.kwargs["detail"] == "Outcome unknown (adopted run)"

    async def test_a_merge_role_with_unknown_outcome_but_a_merged_pr_notifies_success(self, tmp_path: Path) -> None:
        import asyncio

        from sova.dashboard.services import agent_finalize
        from sova.dashboard.services.agent_pool import AgentState
        from sova.ipc.control import EXIT_OUTCOME_UNKNOWN

        process = MagicMock()
        agent = AgentState(
            run_id=5, issue="", role="command:/integrate-pr", process=process, project_dir=tmp_path, pr_number=9
        )
        pa = ProjectAgents(project_dir=tmp_path)
        pa.agents[5] = agent
        finalize_task_run = AsyncMock(return_value=True)

        with (
            patch.object(agent_finalize, "_wait_with_terminal_check", new=AsyncMock(return_value=EXIT_OUTCOME_UNKNOWN)),
            patch.object(agent_finalize, "_check_pr_merged_on_failure", new=AsyncMock(return_value=True)),
            patch.object(agent_finalize, "_crash_recovery_cleanup", new=AsyncMock()),
            patch.object(agent_finalize, "_check_merge_queue_marker_file", new=AsyncMock()),
            patch.object(
                agent_finalize,
                "_validate_command_outcome",
                new=AsyncMock(return_value=MagicMock(failure_reason="", network_blocked=False)),
            ),
            patch.object(agent_finalize, "_validate_pipeline_outcome", new=AsyncMock(return_value="")),
            patch.object(agent_finalize, "_finalize_task_run", new=finalize_task_run),
            patch("sova.dashboard.services.agent_resource._finalize_resource_monitoring", new=AsyncMock()),
            patch.object(agent_finalize, "_get_task_run_status", new=AsyncMock(return_value="done")),
            patch.object(agent_finalize, "_announce_agent_finished", new=AsyncMock()),
            patch("sova.ipc.notifications.notify") as notify,
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new=AsyncMock()),
        ):
            await asyncio.wait_for(agent_finalize._wait_and_finalize(pa, agent), timeout=10)

        assert finalize_task_run.await_args.kwargs["exit_code"] == 0
        notify.assert_called_once()
        assert "finished" in notify.call_args.kwargs["subtitle"]

    async def test_the_lifecycle_phase_is_stopped_not_failed(self) -> None:
        from sova.dashboard.services import lifecycle_service
        from sova.ipc.control import EXIT_OUTCOME_UNKNOWN

        run = MagicMock()
        run.lifecycle_id = 3
        run.role = "developer"
        run.status = "interrupted"
        run.pr_number = None
        run.branch_name = None
        session = MagicMock()
        session.get = AsyncMock(return_value=run)

        with (
            patch.object(lifecycle_service, "infer_phase_from_role", return_value="develop"),
            patch.object(lifecycle_service, "get_lifecycle", new=AsyncMock(return_value=None)),
            patch.object(lifecycle_service, "stop_phase", new=AsyncMock()) as stop,
            patch.object(lifecycle_service, "fail_phase", new=AsyncMock()) as fail,
        ):
            await lifecycle_service.finalize_phase_from_run(session, 5, EXIT_OUTCOME_UNKNOWN)

        stop.assert_awaited_once()
        fail.assert_not_called()


class TestPoolFor:
    def test_picks_the_pool_that_owns_the_project_dir(self, tmp_path: Path) -> None:
        other = ProjectAgents(project_dir=tmp_path / "other")
        mine = ProjectAgents(project_dir=tmp_path)
        with patch("sova.dashboard.services.agent_pool.list_all_pools", return_value={"a": other, "b": mine}):
            assert agent_adopt._pool_for(tmp_path) is mine

    def test_falls_back_to_the_default_pool(self, tmp_path: Path) -> None:
        default = ProjectAgents(project_dir=tmp_path)
        with (
            patch("sova.dashboard.services.agent_pool.list_all_pools", return_value={}),
            patch("sova.config.registry.find_slug_for_path", return_value=None),
            patch("sova.dashboard.services.agent_pool._get_project_agents", return_value=default) as get,
        ):
            assert agent_adopt._pool_for(tmp_path) is default
        get.assert_called_once_with(None)


class TestShutdownLeavesFileBackedAgentsRunning:
    """Lifespan shutdown cancels _wait_and_finalize; a file-backed agent must survive it to be adopted later."""

    async def test_cancel_does_not_signal_a_file_backed_agent_under_a_service_manager(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
        from types import SimpleNamespace

        from sova.dashboard.services.agent_finalize import _wait_and_finalize

        proc = MagicMock()
        proc.pid = 4242
        proc.returncode = None
        process = FileAgentProcess(proc, stdout_path=tmp_path / "1.stdout", stderr_path=tmp_path / "1.stderr")
        agent = SimpleNamespace(run_id=1, process=process, project_dir=tmp_path)

        target = "sova.dashboard.services.agent_finalize._wait_with_terminal_check"
        controller = MagicMock()
        with patch(target, side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await _wait_and_finalize(controller, agent)

        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    async def test_an_interactive_server_still_stops_a_file_backed_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no service manager there is no successor to adopt the agent, so shutdown terminates it."""
        import asyncio
        from types import SimpleNamespace

        from sova.dashboard.services.agent_finalize import _wait_and_finalize

        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        monkeypatch.delenv("SOVA_SERVER_LOG", raising=False)
        monkeypatch.delenv("SOVA_SERVER_ERR_LOG", raising=False)
        proc = MagicMock()
        proc.pid = 4242
        proc.returncode = None
        proc.wait = AsyncMock(return_value=0)
        process = FileAgentProcess(proc, stdout_path=tmp_path / "1.stdout", stderr_path=tmp_path / "1.stderr")
        agent = SimpleNamespace(run_id=1, process=process, project_dir=tmp_path)

        target = "sova.dashboard.services.agent_finalize._wait_with_terminal_check"
        with patch(target, side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await _wait_and_finalize(MagicMock(), agent)

        proc.terminate.assert_called_once()
