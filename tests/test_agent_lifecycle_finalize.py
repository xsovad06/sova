"""agent_lifecycle run finalization, cost recording and wait-and-finalize.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from sova.db.models import CostRecord, StepExecution, TaskRun
from sova.db.session import get_session

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# Finalize guard: terminal status not overwritten
# ---------------------------------------------------------------------------


class TestFinalizeTaskRunGuard:
    """_finalize_task_run must not overwrite an already-terminal run."""

    async def test_finalize_skips_already_failed_run(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="88",
                    role="developer",
                    status="failed",
                    error_message="Manually abandoned",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0.5
        mock_agent.runtime_model = None
        mock_agent.runtime_name = None
        mock_agent.last_result_cost_source = None
        mock_agent.last_result_input_tokens = None
        mock_agent.last_result_output_tokens = None
        mock_agent.last_result_cache_read_tokens = None
        mock_agent.last_result_cache_write_tokens = None
        mock_agent.last_result_reasoning_tokens = None
        mock_agent.project_dir = None

        await _finalize_task_run(run_id, exit_code=1, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "failed"
                assert refreshed.error_message == "Manually abandoned"

    async def test_finalize_skips_done_run(self) -> None:
        """Already-done runs must not be overwritten (even with different exit code)."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="91",
                    role="developer",
                    status="done",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0
        mock_agent.runtime_model = None
        mock_agent.runtime_name = None
        mock_agent.last_result_cost_source = None
        mock_agent.last_result_input_tokens = None
        mock_agent.last_result_output_tokens = None
        mock_agent.last_result_cache_read_tokens = None
        mock_agent.last_result_cache_write_tokens = None
        mock_agent.last_result_reasoning_tokens = None
        mock_agent.project_dir = None

        await _finalize_task_run(run_id, exit_code=1, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "done"
                assert refreshed.error_message is None

    async def test_finalize_skips_paused_run(self) -> None:
        """WorkflowEngine may set status to 'paused' (budget exceeded); dashboard must not overwrite."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="89",
                    role="developer",
                    status="paused",
                    error_message="Budget exceeded: $10.50",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 10.5
        mock_agent.runtime_model = None
        mock_agent.runtime_name = None
        mock_agent.last_result_cost_source = None
        mock_agent.last_result_input_tokens = None
        mock_agent.last_result_output_tokens = None
        mock_agent.last_result_cache_read_tokens = None
        mock_agent.last_result_cache_write_tokens = None
        mock_agent.last_result_reasoning_tokens = None
        mock_agent.project_dir = None

        await _finalize_task_run(run_id, exit_code=0, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "paused"
                assert refreshed.error_message == "Budget exceeded: $10.50"

    async def test_finalize_still_updates_cost_on_terminal(self) -> None:
        """Even when status is terminal, stream cost should be written (more accurate)."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="90",
                    role="developer",
                    status="done",
                    total_cost_usd=Decimal("5.00"),
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 7.50
        mock_agent.runtime_model = None
        mock_agent.runtime_name = None
        mock_agent.last_result_cost_source = None
        mock_agent.last_result_input_tokens = None
        mock_agent.last_result_output_tokens = None
        mock_agent.last_result_cache_read_tokens = None
        mock_agent.last_result_cache_write_tokens = None
        mock_agent.last_result_reasoning_tokens = None
        mock_agent.project_dir = None
        mock_agent.issue = "90"

        await _finalize_task_run(run_id, exit_code=0, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "done"
                assert refreshed.total_cost_usd == Decimal("7.50")

    async def test_finalize_applies_file_handoff_when_already_terminal(self) -> None:
        """_finalize_task_run must persist file handoff even when TaskRun is already terminal.

        The WorkflowEngine subprocess may finalize status before the dashboard's
        _finalize_task_run runs, causing an early-return. handoff_json must still
        be set so every other consumer of TaskRun.handoff_json finds it.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="93",
                    role="reviewer",
                    status="done",
                    pr_number=77,
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        findings = [{"file": "a.py", "line": 1, "severity": 9, "category": "bug", "description": "X", "suggestion": ""}]
        file_handoff_data = {
            "issue": "93",
            "pr_number": 77,
            "details": {"next_action": "address_review", "pending_findings": findings, "cost_usd": "0.05"},
            "source": "reviewer",
        }

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0.0
        mock_agent.runtime_model = None
        mock_agent.runtime_name = None
        mock_agent.last_result_cost_source = None
        mock_agent.last_result_input_tokens = None
        mock_agent.last_result_output_tokens = None
        mock_agent.last_result_cache_read_tokens = None
        mock_agent.last_result_cache_write_tokens = None
        mock_agent.last_result_reasoning_tokens = None
        mock_agent.project_dir = None
        mock_agent.issue = "93"
        mock_agent.role = "reviewer"

        original = get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with (
            patch("sova.dashboard.services.agent_db._read_file_handoff", return_value=file_handoff_data),
            patch("sova.dashboard.services.agent_db.get_session", side_effect=_ignore_project_dir),
            patch("sova.supervisor.pr_throttle.dequeue", AsyncMock()),
        ):
            await _finalize_task_run(run_id, exit_code=0, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "done"
                assert refreshed.handoff_json is not None, "handoff_json must be set even on terminal early-return"
                assert refreshed.handoff_json.get("next_action") == "address_review"
                findings_out = refreshed.handoff_json.get("pending_findings", [])
                assert len(findings_out) == 1
                assert findings_out[0]["severity"] == 9


# ---------------------------------------------------------------------------
# Merge-aware finalization
# ---------------------------------------------------------------------------


class TestMergeAwareFinalization:
    """_check_pr_merged_on_failure should detect merged PRs for integration commands."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            pytest.param("MERGED", True, id="detects-merged-pr"),
            pytest.param("OPEN", False, id="false-for-open-pr"),
        ],
    )
    async def test_pr_state(self, state: str, expected: bool) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_lifecycle import _check_pr_merged_on_failure

        mock_cfg = MagicMock(github_repo="owner/repo", github_user="user")
        mock_status = MagicMock(state=state)
        with (
            patch("sova.dashboard.services.agent_validation.load_config", return_value=mock_cfg),
            patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, return_value=mock_status),
        ):
            assert await _check_pr_merged_on_failure(pr_number=88, project_dir=None) is expected

    async def test_returns_false_on_gh_failure(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_lifecycle import _check_pr_merged_on_failure

        mock_cfg = MagicMock(github_repo="owner/repo", github_user="user")
        with (
            patch("sova.dashboard.services.agent_validation.load_config", return_value=mock_cfg),
            patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, side_effect=RuntimeError("not found")),
        ):
            assert await _check_pr_merged_on_failure(pr_number=88, project_dir=None) is False

    async def test_returns_false_when_no_pr(self) -> None:
        from sova.dashboard.services.agent_lifecycle import _check_pr_merged_on_failure

        assert await _check_pr_merged_on_failure(pr_number=None, project_dir=None) is False

    async def test_start_command_passes_pr_number_to_task_run(self, tmp_path: Path) -> None:
        """start_command should extract pr from args and pass to _create_task_run."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle

        mock_process = MagicMock()
        mock_process.pid = 12345

        with (
            patch.object(agent_lifecycle, "_get_project_agents") as mock_gpa,
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=AsyncMock(return_value=mock_process)),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=99) as mock_create,
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            # Branch lookup is not under test: it calls load_config(project_dir), which now resolves unset commit
            # lists from project files and git history (sova/config/commit_lists.py), and project_dir here is a
            # bare MagicMock that nothing can read.
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value=""),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "check_memory_pressure", return_value=(None, None)),
            patch.object(agent_lifecycle, "check_stale_command", return_value=None),
            # Resolved explicitly rather than left to fall through
            # _resolve_issue_worktree()'s real worktree-discovery logic against a
            # bare MagicMock project_dir: that function now calls
            # ensure_worktree_usable() (#1099), which does real filesystem/git
            # probing no mock here simulates, and can legitimately fall back to
            # returning project_dir itself, tripping start_command()'s own
            # "cwd == project_dir" isolation-failure guard for a PR-scoped run.
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=tmp_path / "fake-worktree-32",
            ),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
            patch(
                "sova.dashboard.services.agent_context.ensure_worktree_usable",
                new_callable=AsyncMock,
                side_effect=lambda project_dir, worktree_path, **kwargs: worktree_path,
            ),
        ):
            from sova.dashboard.services.agent_pool import ProjectAgents

            pa = ProjectAgents()
            pa.project_dir = MagicMock()
            pa.project_dir.__truediv__ = MagicMock(return_value=MagicMock(is_file=MagicMock(return_value=False)))
            mock_gpa.return_value = pa

            result = await agent_lifecycle.start_command(
                "integrate-pr",
                args={"issue": "32", "pr": 88},
            )

        assert "error" not in result
        mock_create.assert_awaited_once()
        assert mock_create.call_args.kwargs.get("pr_number") == 88

    async def test_start_command_with_an_unresolved_issue_leaves_every_handoff_in_place(self, tmp_path: Path) -> None:
        """``gh pr view`` failing resolves the issue to "", which must not clear other issues' handoffs (#1569)."""
        import json
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_pool import ProjectAgents
        from sova.utils.shell import ShellResult

        control = tmp_path / ".claude" / "agent-control"
        control.mkdir(parents=True)
        files = [control / "handoff-1.json", control / "handoff-2.json", control / "handoff.json"]
        for f in files:
            f.write_text(json.dumps({"source": "developer", "status": "awaiting_action"}), encoding="utf-8")
        failed_view = ShellResult(returncode=1, stdout="", stderr="gh: not found")
        mock_process = MagicMock(pid=12345)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=ProjectAgents(project_dir=tmp_path)),
            patch("sova.dashboard.services.agent_context.run_shell", new=AsyncMock(return_value=failed_view)),
            patch.object(
                agent_lifecycle, "get_runtime", return_value=MagicMock(spawn=AsyncMock(return_value=mock_process))
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=99),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value=""),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_db._capture_pr_head_sha", new_callable=AsyncMock, return_value=None),
            patch(
                "sova.dashboard.services.agent_db._capture_worktree_head_sha", new_callable=AsyncMock, return_value=None
            ),
            patch.object(agent_lifecycle, "check_memory_pressure", return_value=(None, None)),
            patch.object(agent_lifecycle, "check_network_connectivity", return_value=(None, None)),
            patch.object(agent_lifecycle, "check_stale_command", return_value=None),
            patch.object(
                agent_lifecycle, "_resolve_issue_worktree", new_callable=AsyncMock, return_value=tmp_path / "wt-88"
            ),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await agent_lifecycle.start_command("integrate-pr", args={"pr": 88})

        assert "error" not in result
        assert all(f.exists() for f in files)
        assert not (control / "handoff-archive").exists()

    async def test_start_command_sets_stream_parser_from_runtime(self, tmp_path: Path) -> None:
        """start_command stores the runtime's per-process parser on AgentState, like start_agent."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle

        mock_process = MagicMock()
        mock_process.pid = 12345

        sentinel_parser = object()
        mock_rt = MagicMock(
            spawn=AsyncMock(return_value=mock_process),
            create_stream_parser=MagicMock(return_value=sentinel_parser),
            stream_reader_drain_timeout=MagicMock(return_value=42.0),
        )

        with (
            patch.object(agent_lifecycle, "_get_project_agents") as mock_gpa,
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_rt),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=99),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            # Branch lookup is not under test: it calls load_config(project_dir), which now resolves unset commit
            # lists from project files and git history (sova/config/commit_lists.py), and project_dir here is a
            # bare MagicMock that nothing can read.
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value=""),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "check_memory_pressure", return_value=(None, None)),
            patch.object(agent_lifecycle, "check_stale_command", return_value=None),
            # See the comment on the same patch in
            # test_start_command_passes_pr_number_to_task_run above.
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=tmp_path / "fake-worktree-32",
            ),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            from sova.dashboard.services.agent_pool import ProjectAgents

            pa = ProjectAgents()
            pa.project_dir = MagicMock()
            pa.project_dir.__truediv__ = MagicMock(return_value=MagicMock(is_file=MagicMock(return_value=False)))
            mock_gpa.return_value = pa

            result = await agent_lifecycle.start_command(
                "integrate-pr",
                args={"issue": "32", "pr": 88},
            )

        assert "error" not in result
        assert pa.agents[99].stream_parser is sentinel_parser
        assert pa.agents[99].stream_reader_drain_timeout == 42.0

    async def test_start_command_create_stream_parser_failure_does_not_orphan_process(self, tmp_path: Path) -> None:
        """A parser-construction failure after a successful spawn must not be treated as a spawn failure.

        Mirrors test_start_agent_create_stream_parser_failure_does_not_orphan_process for the
        start_command() path: the subprocess is already running by the time
        create_stream_parser() is called, so raising here must not finalize the run as orphaned.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle

        mock_process = MagicMock()
        mock_process.pid = 12345

        mock_rt = MagicMock(
            spawn=AsyncMock(return_value=mock_process),
            create_stream_parser=MagicMock(side_effect=RuntimeError("bad codex jsonl schema")),
        )

        with (
            patch.object(agent_lifecycle, "_get_project_agents") as mock_gpa,
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_rt),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=99),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            # Branch lookup is not under test: it calls load_config(project_dir), which now resolves unset commit
            # lists from project files and git history (sova/config/commit_lists.py), and project_dir here is a
            # bare MagicMock that nothing can read.
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value=""),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "check_memory_pressure", return_value=(None, None)),
            patch.object(agent_lifecycle, "check_stale_command", return_value=None),
            patch.object(agent_lifecycle, "_finalize_orphaned_run", new_callable=AsyncMock) as mock_orphan,
            # See the comment on the same patch in
            # test_start_command_passes_pr_number_to_task_run above.
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=tmp_path / "fake-worktree-32",
            ),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            from sova.dashboard.services.agent_pool import ProjectAgents

            pa = ProjectAgents()
            pa.project_dir = MagicMock()
            pa.project_dir.__truediv__ = MagicMock(return_value=MagicMock(is_file=MagicMock(return_value=False)))
            mock_gpa.return_value = pa

            result = await agent_lifecycle.start_command(
                "integrate-pr",
                args={"issue": "32", "pr": 88},
            )

        assert "error" not in result
        assert pa.agents[99].stream_parser is None
        mock_orphan.assert_not_awaited()

    async def test_start_command_drain_timeout_failure_does_not_discard_parser(self, tmp_path: Path) -> None:
        """A stream_reader_drain_timeout() failure must not discard an already-built parser.

        Mirrors test_start_agent_drain_timeout_failure_does_not_discard_parser for the
        start_command() path.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle

        mock_process = MagicMock()
        mock_process.pid = 12345

        sentinel_parser = object()
        mock_rt = MagicMock(
            spawn=AsyncMock(return_value=mock_process),
            create_stream_parser=MagicMock(return_value=sentinel_parser),
            stream_reader_drain_timeout=MagicMock(side_effect=RuntimeError("bad codex config")),
        )

        with (
            patch.object(agent_lifecycle, "_get_project_agents") as mock_gpa,
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_rt),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=99),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            # Branch lookup is not under test: it calls load_config(project_dir), which now resolves unset commit
            # lists from project files and git history (sova/config/commit_lists.py), and project_dir here is a
            # bare MagicMock that nothing can read.
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value=""),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "check_memory_pressure", return_value=(None, None)),
            patch.object(agent_lifecycle, "check_stale_command", return_value=None),
            patch.object(agent_lifecycle, "_finalize_orphaned_run", new_callable=AsyncMock) as mock_orphan,
            # See the comment on the same patch in
            # test_start_command_passes_pr_number_to_task_run above.
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=tmp_path / "fake-worktree-32",
            ),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            from sova.dashboard.services.agent_pool import ProjectAgents

            pa = ProjectAgents()
            pa.project_dir = MagicMock()
            pa.project_dir.__truediv__ = MagicMock(return_value=MagicMock(is_file=MagicMock(return_value=False)))
            mock_gpa.return_value = pa

            result = await agent_lifecycle.start_command(
                "integrate-pr",
                args={"issue": "32", "pr": 88},
            )

        assert "error" not in result
        assert pa.agents[99].stream_parser is sentinel_parser
        assert pa.agents[99].stream_reader_drain_timeout is None
        mock_orphan.assert_not_awaited()

    async def test_wait_and_finalize_overrides_failed_to_done_when_pr_merged(self, tmp_path: Path) -> None:
        """_wait_and_finalize should mark status 'done' when integration cmd fails but PR is merged."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, CompletedAgent, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=1)

        agent = AgentState(
            run_id=42,
            issue="99",
            role="command:integrate-pr",
            process=mock_process,
            pr_number=88,
            project_dir=tmp_path,
        )

        pa = ProjectAgents()
        pa.agents[42] = agent

        with (
            patch.object(agent_finalize, "_check_pr_merged_on_failure", new_callable=AsyncMock, return_value=True),
            patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock) as mock_finalize,
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_db.load_config", side_effect=Exception("skip notifications")),
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip notifications")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        assert len(pa.recently_completed) == 1
        completed: CompletedAgent = pa.recently_completed[0]
        assert completed.status == "done"
        assert completed.run_id == 42

        mock_finalize.assert_awaited_once()
        assert mock_finalize.call_args.kwargs["exit_code"] == 0


# ---------------------------------------------------------------------------
# WebSocket graph_invalidated broadcast
# ---------------------------------------------------------------------------


class TestBroadcastEvent:
    async def test_noop_when_no_connections(self) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.routers.agents import _ConnectionManager

        mgr = _ConnectionManager()
        with patch.object(mgr, "_broadcast", new=AsyncMock()) as broadcast:
            await mgr.broadcast_event("graph_invalidated", None)

        broadcast.assert_not_awaited()

    async def test_sends_event_when_connections_exist(self) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.routers.agents import _ConnectionManager

        mgr = _ConnectionManager()
        ws = AsyncMock()
        ws.send_json = AsyncMock()
        mgr._groups[None] = [ws]

        await mgr.broadcast_event("graph_invalidated", None)

        ws.send_json.assert_awaited_once_with({"type": "graph_invalidated"})
        mgr._groups.clear()

    async def test_skips_mismatched_project_dir(self) -> None:
        from pathlib import Path
        from unittest.mock import AsyncMock

        from sova.dashboard.routers.agents import _ConnectionManager

        mgr = _ConnectionManager()
        ws = AsyncMock()
        ws.send_json = AsyncMock()
        mgr._groups[Path("/other")] = [ws]

        await mgr.broadcast_event("graph_invalidated", None)

        ws.send_json.assert_not_awaited()
        mgr._groups.clear()


class TestFinalizeTaskRunReturnValue:
    async def test_returns_true_on_status_transition(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="508", role="developer", status="running")
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0.1
        mock_agent.runtime_model = None
        mock_agent.runtime_name = None
        mock_agent.last_result_cost_source = None
        mock_agent.last_result_input_tokens = None
        mock_agent.last_result_output_tokens = None
        mock_agent.last_result_cache_read_tokens = None
        mock_agent.last_result_cache_write_tokens = None
        mock_agent.last_result_reasoning_tokens = None
        mock_agent.project_dir = None
        mock_agent.issue = ""
        mock_agent.role = "developer"

        result = await _finalize_task_run(run_id, exit_code=0, agent=mock_agent)

        assert result is True

    async def test_returns_false_when_already_terminal(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="508", role="developer", status="done")
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0.0
        mock_agent.runtime_model = None
        mock_agent.runtime_name = None
        mock_agent.last_result_cost_source = None
        mock_agent.last_result_input_tokens = None
        mock_agent.last_result_output_tokens = None
        mock_agent.last_result_cache_read_tokens = None
        mock_agent.last_result_cache_write_tokens = None
        mock_agent.last_result_reasoning_tokens = None
        mock_agent.project_dir = None
        mock_agent.issue = ""

        result = await _finalize_task_run(run_id, exit_code=0, agent=mock_agent)

        assert result is False

    async def test_returns_false_for_missing_run(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0.0
        mock_agent.runtime_model = None
        mock_agent.runtime_name = None
        mock_agent.last_result_cost_source = None
        mock_agent.last_result_input_tokens = None
        mock_agent.last_result_output_tokens = None
        mock_agent.last_result_cache_read_tokens = None
        mock_agent.last_result_cache_write_tokens = None
        mock_agent.last_result_reasoning_tokens = None
        mock_agent.project_dir = None
        mock_agent.issue = ""

        result = await _finalize_task_run(999999, exit_code=0, agent=mock_agent)

        assert result is False


class TestFinalizeTaskRunRuntimeSessionId:
    """_finalize_task_run merges a runtime session id into assessment_json, diagnostics only."""

    async def _make_run(self, *, assessment_json=None, status: str = "running") -> int:
        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="600", role="reviewer", status=status)
                if assessment_json is not None:
                    run.assessment_json = assessment_json
                session.add(run)
                await session.flush()
                return run.id

    def _agent(self, *, runtime_session_id):
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_pool import AgentState

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0.0
        mock_agent.runtime_model = None
        mock_agent.runtime_name = None
        mock_agent.last_result_cost_source = None
        mock_agent.last_result_input_tokens = None
        mock_agent.last_result_output_tokens = None
        mock_agent.last_result_cache_read_tokens = None
        mock_agent.last_result_cache_write_tokens = None
        mock_agent.last_result_reasoning_tokens = None
        mock_agent.project_dir = None
        mock_agent.issue = "600"
        mock_agent.role = "reviewer"
        mock_agent.runtime_session_id = runtime_session_id
        return mock_agent

    async def test_session_id_persisted_on_new_run(self) -> None:
        from sova.dashboard.services.agent_db import _finalize_task_run

        run_id = await self._make_run()
        await _finalize_task_run(run_id, exit_code=0, agent=self._agent(runtime_session_id="thread-abc"))

        async with await get_session() as session:
            task_run = await session.get(TaskRun, run_id)
            assert task_run.assessment_json == {"runtime": {"session_id": "thread-abc"}}

    async def test_session_id_merges_without_overwriting_existing_keys(self) -> None:
        from sova.dashboard.services.agent_db import _finalize_task_run

        run_id = await self._make_run(assessment_json={"confidence_score": 87})
        await _finalize_task_run(run_id, exit_code=0, agent=self._agent(runtime_session_id="thread-xyz"))

        async with await get_session() as session:
            task_run = await session.get(TaskRun, run_id)
            assert task_run.assessment_json == {
                "confidence_score": 87,
                "runtime": {"session_id": "thread-xyz"},
            }

    async def test_empty_session_id_is_not_persisted(self) -> None:
        """thread.started never arrived: no empty string written into assessment_json."""
        from sova.dashboard.services.agent_db import _finalize_task_run

        run_id = await self._make_run()
        await _finalize_task_run(run_id, exit_code=0, agent=self._agent(runtime_session_id=""))

        async with await get_session() as session:
            task_run = await session.get(TaskRun, run_id)
            assert task_run.assessment_json is None

    async def test_session_id_persisted_when_run_already_terminal(self) -> None:
        """The inner `sova run` usually finalizes the TaskRun before the wrapper exits.

        _finalize_task_run() then takes the _TERMINAL_STATUSES early return, so a
        session id recorded after that branch would never reach the database. The
        merge runs before the return precisely so this common path still keeps the
        diagnostic id. Regression test for the gap CodeRabbit flagged on PR #1085.
        """
        from sova.dashboard.services.agent_db import _finalize_task_run

        run_id = await self._make_run(status="done")
        await _finalize_task_run(run_id, exit_code=0, agent=self._agent(runtime_session_id="thread-terminal"))

        async with await get_session() as session:
            task_run = await session.get(TaskRun, run_id)
            assert task_run.assessment_json == {"runtime": {"session_id": "thread-terminal"}}
            assert task_run.status == "done"

    async def test_non_string_session_id_is_not_persisted(self) -> None:
        """A Mock attribute (unset in a test double) must never be written as a session id."""
        from sova.dashboard.services.agent_db import _finalize_task_run

        run_id = await self._make_run()
        await _finalize_task_run(run_id, exit_code=0, agent=self._agent(runtime_session_id=object()))

        async with await get_session() as session:
            task_run = await session.get(TaskRun, run_id)
            assert task_run.assessment_json is None


class TestRecordCostRuntimeModel:
    """_record_cost must attribute spend to the runtime's own reported model, and
    must write a record even when that runtime's cost is a confirmed $0 (issue
    #1128 review). Drives real Codex JSONL through _parse_stream_line() into a
    real AgentState (not a MagicMock with hand-set attributes) so the actual
    zero-cost path is what gets exercised, not an impossible last_result_cost."""

    async def _make_run(self) -> int:
        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="700", role="developer", status="running")
                session.add(run)
                await session.flush()
                return run.id

    async def _cost_records(self, run_id: int) -> list[CostRecord]:
        from sqlalchemy import select

        async with await get_session() as session:
            rows = await session.execute(select(CostRecord).where(CostRecord.task_run_id == run_id))
            return list(rows.scalars().all())

    async def test_codex_zero_cost_turn_is_recorded_under_its_own_model(self) -> None:
        from decimal import Decimal

        from sova.dashboard.services.agent_db import _record_cost
        from sova.dashboard.services.agent_output import _parse_stream_line
        from sova.dashboard.services.agent_pool import AgentState
        from sova.ipc.codex import CodexStreamParser

        run_id = await self._make_run()
        agent = AgentState(run_id=run_id, issue="700", role="developer", process=None)
        agent.stream_parser = CodexStreamParser(model="")

        _parse_stream_line(
            '{"type": "turn.completed", "model": "gpt-5-codex", "usage": {"input_tokens": 10, "output_tokens": 5}}',
            agent,
        )

        assert agent.runtime_model == "gpt-5-codex"
        assert agent.last_result_cost == 0.0

        async with await get_session() as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                _record_cost(task_run, run_id, Decimal(str(agent.last_result_cost)), agent, session)

        records = await self._cost_records(run_id)
        assert len(records) == 1
        assert records[0].model == "gpt-5-codex"
        assert records[0].cost_usd == Decimal("0")
        assert records[0].input_tokens == 10
        assert records[0].output_tokens == 5

    async def test_falls_back_to_claude_when_runtime_model_unset(self) -> None:
        """A runtime with no per-process stream parser (Claude Code itself) never
        sets runtime_model, so the hardcoded default must still apply."""
        from decimal import Decimal

        from sova.dashboard.services.agent_db import _record_cost
        from sova.dashboard.services.agent_pool import AgentState

        run_id = await self._make_run()
        agent = AgentState(run_id=run_id, issue="700", role="developer", process=None)
        agent.last_result_cost = 1.5

        async with await get_session() as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                _record_cost(task_run, run_id, Decimal("1.5"), agent, session)

        records = await self._cost_records(run_id)
        assert len(records) == 1
        assert records[0].model == "claude"

    async def test_falls_back_to_runtime_name_when_stream_reports_no_model(self) -> None:
        """Default Codex config (codex.model unset, stream carries no model field):
        runtime_model never gets set, but the run was still spawned through the Codex
        AgentRuntime, so attribution must fall back to that runtime's own name rather
        than the hardcoded Claude Code default (issue #1128 review)."""
        from decimal import Decimal

        from sova.dashboard.services.agent_db import _record_cost
        from sova.dashboard.services.agent_pool import AgentState

        run_id = await self._make_run()
        agent = AgentState(run_id=run_id, issue="700", role="developer", process=None, runtime_name="codex")
        agent.last_result_cost = 0.0

        async with await get_session() as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                _record_cost(task_run, run_id, Decimal("0"), agent, session)

        records = await self._cost_records(run_id)
        assert len(records) == 1
        assert records[0].model == "codex"

    async def test_claude_code_runtime_name_does_not_relabel_model(self) -> None:
        """runtime_name="claude-code" is the real production value for the
        default spawn path (agent_lifecycle.py sets it unconditionally), not
        the unreachable runtime_name=None the earlier tests above pinned.
        Drives a real Claude "result" JSONL event (carrying the model under
        modelUsage) through _parse_stream_line so runtime_model is set from
        it, and the real model id must win over the runtime_name fallback."""
        from decimal import Decimal

        from sova.dashboard.services.agent_db import _record_cost
        from sova.dashboard.services.agent_output import _parse_stream_line
        from sova.dashboard.services.agent_pool import AgentState

        run_id = await self._make_run()
        agent = AgentState(run_id=run_id, issue="700", role="developer", process=None, runtime_name="claude-code")

        _parse_stream_line(
            '{"type": "result", "total_cost_usd": 0.02, "modelUsage": {"claude-sonnet-5": {}}, '
            '"usage": {"input_tokens": 100, "output_tokens": 20}}',
            agent,
        )

        assert agent.runtime_model == "claude-sonnet-5"

        async with await get_session() as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                _record_cost(task_run, run_id, Decimal(str(agent.last_result_cost)), agent, session)

        records = await self._cost_records(run_id)
        assert len(records) == 1
        assert records[0].model == "claude-sonnet-5"
        assert records[0].cost_source == "priced"
        assert records[0].input_tokens == 100
        assert records[0].output_tokens == 20

    async def test_claude_code_runtime_name_falls_back_to_claude_without_result_event(self) -> None:
        """runtime_name="claude-code" with no terminal "result" event parsed (so
        runtime_model stays None) must still fall back to the "claude" literal,
        not the runtime name itself, so the historical cost-by-model bucket is
        not split across a "claude"/"claude-code" boundary (issue #1128 review)."""
        from decimal import Decimal

        from sova.dashboard.services.agent_db import _record_cost
        from sova.dashboard.services.agent_pool import AgentState

        run_id = await self._make_run()
        agent = AgentState(run_id=run_id, issue="700", role="developer", process=None, runtime_name="claude-code")
        agent.last_result_cost = 1.5

        async with await get_session() as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                _record_cost(task_run, run_id, Decimal("1.5"), agent, session)

        records = await self._cost_records(run_id)
        assert len(records) == 1
        assert records[0].model == "claude"

    async def test_no_terminal_event_writes_no_record(self) -> None:
        """Neither last_result_cost nor runtime_model ever got set (no terminal
        stream event arrived): nothing to attribute, so no record is written."""
        from decimal import Decimal

        from sova.dashboard.services.agent_db import _record_cost
        from sova.dashboard.services.agent_pool import AgentState

        run_id = await self._make_run()
        agent = AgentState(run_id=run_id, issue="700", role="developer", process=None)

        async with await get_session() as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                _record_cost(task_run, run_id, Decimal("0"), agent, session)

        records = await self._cost_records(run_id)
        assert len(records) == 0


class TestWaitAndFinalizeGraphBroadcast:
    async def test_broadcasts_graph_invalidated_on_finalization(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)

        agent = AgentState(
            run_id=42,
            issue="508",
            role="developer",
            process=mock_process,
            project_dir=tmp_path,
        )

        pa = ProjectAgents()
        pa.agents[42] = agent

        mock_ws_manager = MagicMock()
        mock_ws_manager.broadcast_event = AsyncMock()

        with (
            patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock, return_value=True),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip notifications")),
            patch("sova.dashboard.routers.agents._ws_manager", mock_ws_manager),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        project = tmp_path
        assert [c.args for c in mock_ws_manager.broadcast_event.await_args_list] == [
            ("agent_finished", project),
            ("graph_invalidated", project),
        ]

    async def test_sends_only_agent_finished_when_already_terminal(self, tmp_path: Path) -> None:
        """A run that reached a terminal status on its own (awaiting_approval) still refreshes the Tasks list."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)

        agent = AgentState(
            run_id=43,
            issue="508",
            role="developer",
            process=mock_process,
            project_dir=tmp_path,
        )

        pa = ProjectAgents()
        pa.agents[43] = agent

        mock_ws_manager = MagicMock()
        mock_ws_manager.broadcast_event = AsyncMock()

        with (
            patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock, return_value=False),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip notifications")),
            patch("sova.dashboard.routers.agents._ws_manager", mock_ws_manager),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        mock_ws_manager.broadcast_event.assert_awaited_once_with(
            "agent_finished",
            tmp_path,
            extra={"run_id": 43, "status": "done", "issue": "508", "pr_number": None, "role": "developer"},
        )

    async def test_broadcasts_even_when_auto_handoff_raises(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)

        agent = AgentState(
            run_id=44,
            issue="508",
            role="developer",
            process=mock_process,
            project_dir=tmp_path,
        )

        pa = ProjectAgents()
        pa.agents[44] = agent

        mock_ws_manager = MagicMock()
        mock_ws_manager.broadcast_event = AsyncMock()

        import pytest

        with (
            patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock, return_value=True),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch(
                "sova.dashboard.services.agent_handoff._process_auto_handoff",
                new_callable=AsyncMock,
                side_effect=RuntimeError("handoff exploded"),
            ),
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip notifications")),
            patch("sova.dashboard.routers.agents._ws_manager", mock_ws_manager),
        ):
            with pytest.raises(RuntimeError, match="handoff exploded"):
                await agent_lifecycle._wait_and_finalize(pa, agent)

        events = [c.args[0] for c in mock_ws_manager.broadcast_event.await_args_list]
        assert events == ["agent_finished", "graph_invalidated"]

    async def test_broadcasts_after_agent_removed_from_pool(self, tmp_path: Path) -> None:
        """A client refetching on agent_finished must not still see the agent as running (#1149)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        agent = AgentState(
            run_id=45,
            issue="1148",
            role="researcher",
            process=mock_process,
            project_dir=tmp_path,
        )
        pa = ProjectAgents()
        pa.agents[45] = agent

        seen_running: list[bool] = []

        async def _record(_event: str, _project_dir: Path, extra: dict | None = None) -> None:
            seen_running.append(45 in pa.agents)

        mock_ws_manager = MagicMock()
        mock_ws_manager.broadcast_event = AsyncMock(side_effect=_record)

        with (
            patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock, return_value=True),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip notifications")),
            patch("sova.dashboard.routers.agents._ws_manager", mock_ws_manager),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        assert seen_running == [False, False]

    async def test_invalidates_queue_and_pr_caches(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, call, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        agent = AgentState(
            run_id=46,
            issue="1148",
            role="researcher",
            process=mock_process,
            project_dir=tmp_path,
            pr_number=99,
        )
        pa = ProjectAgents()
        pa.agents[46] = agent

        cfg = MagicMock()
        cfg.github_repo = "owner/repo"
        mock_ws_manager = MagicMock()
        mock_ws_manager.broadcast_event = AsyncMock()

        with (
            patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock, return_value=False),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_finalize.load_config", return_value=cfg),
            patch("sova.ipc.notifications.notify"),
            patch("sova.dashboard.routers.agents._ws_manager", mock_ws_manager),
            patch("sova.dashboard.services.queue_service.invalidate_queue_cache") as invalidate_queue,
            patch("sova.dashboard.services.pr_service.invalidate_pr_cache") as invalidate_prs,
            patch("sova.dashboard.services.work_verdict.invalidate_verdict") as invalidate_verdict,
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        # The queue and the verdict twice: before the terminal status is written, and again in the announcement,
        # after the rollback. The PR list once: the rollback changes labels, not PRs (one gh pr list per exit).
        assert invalidate_queue.call_args_list == [call(tmp_path)] * 2
        assert invalidate_prs.call_args_list == [call("owner/repo")]
        assert invalidate_verdict.call_args_list == [call(tmp_path, 99)] * 2

    def test_verdict_cache_invalidated_even_when_queue_and_pr_caches_fail(self, tmp_path: Path) -> None:
        """A reviewer exit must still drop the superseded SOVA verdict even if the
        unrelated queue/PR cache invalidation failed (#1162 review: a reviewer's
        agent_finished previously left the Tasks list showing a superseded verdict
        for up to 5 minutes since nothing invalidated work_verdict's own cache).
        """
        from unittest.mock import patch

        from sova.dashboard.services import agent_finalize

        with (
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=RuntimeError("bad config")),
            patch("sova.dashboard.services.queue_service.invalidate_queue_cache", side_effect=OSError("boom")),
            patch("sova.dashboard.services.pr_service.invalidate_pr_cache") as invalidate_prs,
            patch("sova.dashboard.services.work_verdict.invalidate_verdict") as invalidate_verdict,
        ):
            agent_finalize.invalidate_work_item_caches(tmp_path, 99)

        invalidate_prs.assert_not_called()
        invalidate_verdict.assert_called_once_with(tmp_path, 99)

    def test_queue_cache_invalidated_even_when_config_load_fails(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services import agent_finalize

        with (
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=RuntimeError("bad config")),
            patch("sova.dashboard.services.queue_service.invalidate_queue_cache") as invalidate_queue,
            patch("sova.dashboard.services.pr_service.invalidate_pr_cache") as invalidate_prs,
        ):
            agent_finalize.invalidate_work_item_caches(tmp_path)

        invalidate_queue.assert_called_once_with(tmp_path)
        invalidate_prs.assert_not_called()

    def test_pr_cache_invalidated_even_when_queue_cache_invalidation_fails(self, tmp_path: Path) -> None:
        """A failed queue cache invalidation degrades to stale-until-TTL, not a propagated exception (#1149 review)."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services import agent_finalize

        cfg = MagicMock()
        cfg.github_repo = "owner/repo"
        with (
            patch("sova.dashboard.services.agent_finalize.load_config", return_value=cfg),
            patch("sova.dashboard.services.queue_service.invalidate_queue_cache", side_effect=OSError("stale symlink")),
            patch("sova.dashboard.services.pr_service.invalidate_pr_cache") as invalidate_prs,
        ):
            agent_finalize.invalidate_work_item_caches(tmp_path)

        invalidate_prs.assert_called_once_with("owner/repo")

    def test_pr_cache_untouched_without_github_repo(self, tmp_path: Path) -> None:
        """A Jira-only project has no repo-keyed PR cache entry to drop."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services import agent_finalize

        cfg = MagicMock()
        cfg.github_repo = ""
        with (
            patch("sova.dashboard.services.agent_finalize.load_config", return_value=cfg),
            patch("sova.dashboard.services.queue_service.invalidate_queue_cache"),
            patch("sova.dashboard.services.pr_service.invalidate_pr_cache") as invalidate_prs,
        ):
            agent_finalize.invalidate_work_item_caches(tmp_path)

        invalidate_prs.assert_not_called()

    def test_verdict_cache_invalidation_failure_does_not_raise(self, tmp_path: Path) -> None:
        """A failed verdict-cache drop degrades to stale-until-TTL, not a propagated exception."""
        from unittest.mock import patch

        from structlog.testing import capture_logs

        from sova.dashboard.services import agent_finalize

        with (
            capture_logs() as logs,
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=RuntimeError("bad config")),
            patch("sova.dashboard.services.queue_service.invalidate_queue_cache"),
            patch("sova.dashboard.services.work_verdict.invalidate_verdict", side_effect=OSError("boom")),
        ):
            agent_finalize.invalidate_work_item_caches(tmp_path, 99)

        warnings = [e["event"] for e in logs if e["log_level"] == "warning"]
        assert warnings == [
            "finalize.pr_cache_invalidation_failed",
            "finalize.verdict_cache_invalidation_failed",
        ]

    def test_repo_context_agent_defaults_off_when_config_load_fails(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services import agent_lifecycle

        with patch("sova.dashboard.services.agent_lifecycle.load_config", side_effect=RuntimeError("bad config")):
            assert agent_lifecycle._resolve_repo_context_agent_enabled(tmp_path) is False

    async def test_broadcast_failure_does_not_raise(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_finalize
        from sova.dashboard.services.agent_pool import AgentState

        agent = AgentState(run_id=47, issue="1", role="developer", process=None, project_dir=tmp_path)
        mock_ws_manager = MagicMock()
        mock_ws_manager.broadcast_event = AsyncMock(side_effect=RuntimeError("socket gone"))

        with (
            patch.object(agent_finalize, "invalidate_work_item_caches"),
            patch("sova.dashboard.routers.agents._ws_manager", mock_ws_manager),
        ):
            await agent_finalize._announce_agent_finished(agent, status_changed=True, status="done")

        mock_ws_manager.broadcast_event.assert_awaited_once()


# ---------------------------------------------------------------------------
# Agent Recovery: direct service tests
# ---------------------------------------------------------------------------


class TestWaitWithTerminalCheck:
    """Tests for _wait_with_terminal_check and _is_run_terminal_in_db."""

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            pytest.param("done", True, id="true"),
            pytest.param("running", False, id="false"),
        ],
    )
    async def test_is_run_terminal_in_db(self, status: str, expected: bool) -> None:
        from sova.dashboard.services.agent_lifecycle import _is_run_terminal_in_db

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="910", role="dev", status=status)
            session.add(run)
            await session.flush()
            run_id = run.id

        result = await _is_run_terminal_in_db(run_id, None)
        assert result is expected

    async def test_is_run_terminal_in_db_missing(self) -> None:
        from sova.dashboard.services.agent_lifecycle import _is_run_terminal_in_db

        result = await _is_run_terminal_in_db(99999, None)
        assert result is True

    async def test_is_run_terminal_in_db_returns_false_on_db_error(self) -> None:
        """DB errors should return False (fail open for polling safety)."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _is_run_terminal_in_db

        with patch("sova.dashboard.services.agent_finalize.get_session", side_effect=Exception("db broken")):
            result = await _is_run_terminal_in_db(1, None)
        assert result is False

    @pytest.mark.parametrize(
        ("issue_number", "db_status", "should_hang", "exit_code", "pid"),
        [
            # When DB shows terminal, the hanging process should be stopped.
            pytest.param("920", "done", True, 0, 88888, id="kills-on-terminal-db"),
            # When process exits normally, return its exit code.
            pytest.param("921", "running", False, 0, 88889, id="returns-on-normal-exit"),
            # Non-zero exit codes should be preserved, not converted to -1.
            pytest.param("922", "done", True, 1, 88890, id="preserves-nonzero-exit"),
        ],
    )
    async def test_wait_with_terminal_check_exit_code(
        self, issue_number: str, db_status: str, should_hang: bool, exit_code: int, pid: int
    ) -> None:
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _wait_with_terminal_check
        from sova.ipc.testing import MockAgentProcess

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number=issue_number, role="dev", status=db_status)
            session.add(run)
            await session.flush()
            run_id = run.id

        process = MockAgentProcess(should_hang=should_hang, exit_code=exit_code, pid=pid)
        agent = SimpleNamespace(run_id=run_id, process=process, project_dir=None)

        with patch("sova.dashboard.services.agent_finalize._DB_TERMINAL_POLL_INTERVAL", 0.1):
            rc = await asyncio.wait_for(_wait_with_terminal_check(agent), timeout=5.0)

        assert rc == exit_code

    async def test_wait_with_terminal_check_stop_failure_escalates_to_kill(self) -> None:
        """When stop() raises, the process should be killed via _proc.kill()."""
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_lifecycle import _wait_with_terminal_check

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="923", role="dev", status="done")
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock(return_value=None)
        mock_process = MagicMock()
        mock_process.pid = 88891
        mock_process.returncode = None
        mock_process.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_process.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        mock_process._proc = mock_proc

        agent = SimpleNamespace(run_id=run_id, process=mock_process, project_dir=None)

        with patch("sova.dashboard.services.agent_finalize._DB_TERMINAL_POLL_INTERVAL", 0.1):
            rc = await asyncio.wait_for(_wait_with_terminal_check(agent), timeout=5.0)

        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()
        assert rc == -1  # returncode was None, so -1

    async def test_wait_and_finalize_cancellation_terminates_process(self) -> None:
        """CancelledError during wait should terminate the process."""
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_process = MagicMock()
        mock_process.pid = 88892
        mock_process.is_running = True
        mock_process._proc = mock_proc
        mock_process.wait = AsyncMock(side_effect=asyncio.CancelledError)

        pa = MagicMock()
        agent = SimpleNamespace(
            run_id=999,
            process=mock_process,
            project_dir=None,
            issue="test",
            role="dev",
            pr_number=None,
            pre_run_sha=None,
        )

        from sova.dashboard.services.agent_lifecycle import _wait_and_finalize

        patch_target = "sova.dashboard.services.agent_finalize._wait_with_terminal_check"
        with patch(patch_target, side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await _wait_and_finalize(pa, agent)

        mock_proc.terminate.assert_called_once()

    async def test_wait_and_finalize_cancellation_escalates_to_kill(self) -> None:
        """When terminate succeeds but process doesn't exit, escalate to kill."""
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_process = MagicMock()
        mock_process.pid = 88893
        mock_process.is_running = True
        mock_process._proc = mock_proc
        mock_process.wait = AsyncMock(side_effect=asyncio.CancelledError)

        pa = MagicMock()
        agent = SimpleNamespace(
            run_id=999,
            process=mock_process,
            project_dir=None,
            issue="test",
            role="dev",
            pr_number=None,
            pre_run_sha=None,
        )

        from sova.dashboard.services.agent_lifecycle import _wait_and_finalize

        patch_target = "sova.dashboard.services.agent_finalize._wait_with_terminal_check"
        with patch(patch_target, side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await _wait_and_finalize(pa, agent)

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

    async def test_wait_and_finalize_cancellation_terminate_process_gone(self) -> None:
        """When terminate raises OSError (ProcessLookupError, PermissionError), skip gracefully."""
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        mock_proc = MagicMock()
        mock_proc.terminate.side_effect = ProcessLookupError
        mock_process = MagicMock()
        mock_process.pid = 88894
        mock_process.is_running = True
        mock_process._proc = mock_proc

        pa = MagicMock()
        agent = SimpleNamespace(
            run_id=999,
            process=mock_process,
            project_dir=None,
            issue="test",
            role="dev",
            pr_number=None,
            pre_run_sha=None,
        )

        from sova.dashboard.services.agent_lifecycle import _wait_and_finalize

        patch_target = "sova.dashboard.services.agent_finalize._wait_with_terminal_check"
        with patch(patch_target, side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await _wait_and_finalize(pa, agent)

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_not_called()

    async def test_wait_and_finalize_cancellation_terminate_permission_error(self) -> None:
        """PermissionError on terminate (PID recycled to another user) skips gracefully."""
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        mock_proc = MagicMock()
        mock_proc.terminate.side_effect = PermissionError("Operation not permitted")
        mock_process = MagicMock()
        mock_process.pid = 88895
        mock_process.is_running = True
        mock_process._proc = mock_proc

        pa = MagicMock()
        agent = SimpleNamespace(
            run_id=999,
            process=mock_process,
            project_dir=None,
            issue="test",
            role="dev",
            pr_number=None,
            pre_run_sha=None,
        )

        from sova.dashboard.services.agent_lifecycle import _wait_and_finalize

        patch_target = "sova.dashboard.services.agent_finalize._wait_with_terminal_check"
        with patch(patch_target, side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await _wait_and_finalize(pa, agent)

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_not_called()


class TestDowngradeToFailed:
    """_downgrade_to_failed changes done runs to failed with a reason."""

    async def test_downgrades_done_to_failed(self) -> None:
        from sova.dashboard.services.agent_db import _downgrade_to_failed

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="320", role="command:address-pr", status="done")
            session.add(run)
            await session.flush()
            run_id = run.id

        await _downgrade_to_failed(run_id, "Did not push changes", None)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "failed"
                assert refreshed.error_message == "Did not push changes"

    async def test_does_not_downgrade_non_done(self) -> None:
        from sova.dashboard.services.agent_db import _downgrade_to_failed

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="321", role="developer", status="failed", error_message="Original error")
            session.add(run)
            await session.flush()
            run_id = run.id

        await _downgrade_to_failed(run_id, "New reason", None)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "failed"
                assert refreshed.error_message == "Original error"


class TestOrphanedStepFinalization:
    """_finalize_task_run must mark running steps as interrupted."""

    async def test_running_step_marked_interrupted(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="628", role="developer", status="running")
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="sync", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="running"))

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0
        mock_agent.runtime_model = None
        mock_agent.runtime_name = None
        mock_agent.last_result_cost_source = None
        mock_agent.last_result_input_tokens = None
        mock_agent.last_result_output_tokens = None
        mock_agent.last_result_cache_read_tokens = None
        mock_agent.last_result_cache_write_tokens = None
        mock_agent.last_result_reasoning_tokens = None
        mock_agent.project_dir = None
        mock_agent.issue = None

        await _finalize_task_run(run_id, exit_code=0, agent=mock_agent)

        async with await get_session() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(StepExecution).where(StepExecution.task_run_id == run_id).order_by(StepExecution.id)
            )
            steps = result.scalars().all()
            assert steps[0].status == "done"
            assert steps[1].status == "interrupted"
            assert steps[1].ended_at is not None

    async def test_no_running_steps_is_noop(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="629", role="developer", status="running")
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="sync", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="done"))

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0
        mock_agent.runtime_model = None
        mock_agent.runtime_name = None
        mock_agent.last_result_cost_source = None
        mock_agent.last_result_input_tokens = None
        mock_agent.last_result_output_tokens = None
        mock_agent.last_result_cache_read_tokens = None
        mock_agent.last_result_cache_write_tokens = None
        mock_agent.last_result_reasoning_tokens = None
        mock_agent.project_dir = None
        mock_agent.issue = None

        await _finalize_task_run(run_id, exit_code=0, agent=mock_agent)

        async with await get_session() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(StepExecution).where(StepExecution.task_run_id == run_id).order_by(StepExecution.id)
            )
            steps = result.scalars().all()
            assert steps[0].status == "done"
            assert steps[1].status == "done"

    async def test_already_terminal_run_still_finalizes_steps(self) -> None:
        """Regression: already-terminal early-return path must also finalize orphaned steps."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="628b", role="developer", status="done")
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="sync", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="running"))

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0
        mock_agent.runtime_model = None
        mock_agent.runtime_name = None
        mock_agent.last_result_cost_source = None
        mock_agent.last_result_input_tokens = None
        mock_agent.last_result_output_tokens = None
        mock_agent.last_result_cache_read_tokens = None
        mock_agent.last_result_cache_write_tokens = None
        mock_agent.last_result_reasoning_tokens = None
        mock_agent.project_dir = None
        mock_agent.issue = None

        result = await _finalize_task_run(run_id, exit_code=0, agent=mock_agent)
        assert result is False

        async with await get_session() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(StepExecution).where(StepExecution.task_run_id == run_id).order_by(StepExecution.id)
            )
            steps = result.scalars().all()
            assert steps[1].status == "interrupted"
            assert steps[1].ended_at is not None


class TestInterruptedStepValidation:
    """_validate_pipeline_outcome catches runs with interrupted last step."""

    async def test_interrupted_last_step_detected(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="630",
                    role="developer",
                    status="done",
                    current_step="develop",
                    pr_number=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="sync", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="assess", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="interrupted"))

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "interrupted" in result
        assert "develop" in result

    async def test_completed_last_step_passes(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="631",
                    role="developer",
                    status="done",
                    current_step="handoff_to_reviewer",
                    pr_number=100,
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="commit", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="push", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="create_pr", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="handoff_to_reviewer", status="done"))

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is None

    async def test_researcher_interrupted_detected(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="632",
                    role="researcher",
                    status="done",
                    current_step="research",
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="fetch_task", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="research", status="interrupted"))

        agent = MagicMock(spec=AgentState)
        agent.role = "researcher"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "research" in result


# ---------------------------------------------------------------------------
# Finalize-before-pop ordering (race fix)
# ---------------------------------------------------------------------------


class TestWaitAndFinalizeOrdering:
    """_wait_and_finalize must finalize the DB record before removing from pa.agents."""

    async def test_agent_in_pa_agents_during_finalize(self, tmp_path: Path) -> None:
        """The run_id must still be in pa.agents when _finalize_task_run is called."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)

        agent = AgentState(
            run_id=50,
            issue="100",
            role="developer",
            process=mock_process,
            project_dir=tmp_path,
        )

        pa = ProjectAgents()
        pa.agents[50] = agent

        was_in_agents_during_finalize = []

        async def tracking_finalize(run_id, *, exit_code, agent):
            was_in_agents_during_finalize.append(run_id in pa.agents)

        with (
            patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock, side_effect=tracking_finalize),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip notifications")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        assert was_in_agents_during_finalize == [True], (
            "_finalize_task_run must be called while run_id is still in pa.agents"
        )
        assert 50 not in pa.agents, "run_id should be removed from pa.agents after finalization"

    async def test_finalize_before_pop_prevents_sweep_race(self, tmp_path: Path) -> None:
        """Sweep should skip runs still in pa.agents, preventing the interrupted race."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=1)

        agent = AgentState(
            run_id=51,
            issue="101",
            role="command:integrate-pr",
            process=mock_process,
            pr_number=200,
            project_dir=tmp_path,
        )

        pa = ProjectAgents()
        pa.agents[51] = agent

        finalize_call_order = []

        async def recording_finalize(run_id, *, exit_code, agent):
            finalize_call_order.append(("finalize", run_id in pa.agents))

        with (
            patch.object(
                agent_finalize,
                "_check_pr_merged_on_failure",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                agent_finalize,
                "_finalize_task_run",
                new_callable=AsyncMock,
                side_effect=recording_finalize,
            ),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_db.load_config", side_effect=Exception("skip notifications")),
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip notifications")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        assert finalize_call_order[0] == ("finalize", True)


class TestLivenessSweepMergeCheck:
    """_liveness_sweep_loop should check PR merge status for merge-role runs."""

    @staticmethod
    def _patch_sweep_deps(**extra_patches):
        """Context manager patching sweep dependencies to use the test in-memory DB."""
        from contextlib import ExitStack
        from unittest.mock import patch

        async def _test_get_session(project_dir=None):  # noqa: ARG001
            return await get_session()

        stack = ExitStack()
        stack.enter_context(patch("sova.dashboard.app.get_session", _test_get_session))
        stack.enter_context(patch("sova.dashboard.services.control_service._is_process_alive", return_value=False))
        for target, mock_val in extra_patches.items():
            stack.enter_context(patch(target, **mock_val))
        return stack

    async def test_sweep_marks_merged_pr_as_done(self) -> None:
        """Dead-PID merge-role run with merged PR should get status 'done', not 'interrupted'."""
        from unittest.mock import AsyncMock

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="42",
                    role="command:integrate-pr",
                    status="running",
                    pid=999999,
                    pr_number=100,
                    project_slug="test",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        with self._patch_sweep_deps(
            **{
                "sova.dashboard.services.agent_lifecycle._check_pr_merged_on_failure": {
                    "new_callable": AsyncMock,
                    "return_value": True,
                },
            }
        ):
            from sova.dashboard.app import _liveness_sweep_once

            await _liveness_sweep_once(None, is_multi=False)

        async with await get_session() as session:
            refreshed = await session.get(TaskRun, run_id)
            assert refreshed.status == "done"
            assert "merged" in (refreshed.error_message or "").lower()

    async def test_sweep_marks_non_merge_role_as_interrupted(self) -> None:
        """Dead-PID non-merge-role run should still get 'interrupted'."""
        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="43",
                    role="developer",
                    status="running",
                    pid=999998,
                    project_slug="test",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        with self._patch_sweep_deps():
            from sova.dashboard.app import _liveness_sweep_once

            await _liveness_sweep_once(None, is_multi=False)

        async with await get_session() as session:
            refreshed = await session.get(TaskRun, run_id)
            assert refreshed.status == "interrupted"

    async def test_sweep_skips_managed_runs(self) -> None:
        """Runs still in pa.agents should be skipped by the sweep."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.control_service import _projects

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="44",
                    role="developer",
                    status="running",
                    pid=999997,
                    project_slug="test",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        from sova.dashboard.services.agent_pool import ProjectAgents

        mock_pa = ProjectAgents()
        mock_pa.agents[run_id] = MagicMock()

        original = dict(_projects)
        _projects["__default__"] = mock_pa
        try:
            with self._patch_sweep_deps():
                from sova.dashboard.app import _liveness_sweep_once

                await _liveness_sweep_once(None, is_multi=False)
        finally:
            _projects.clear()
            _projects.update(original)

        async with await get_session() as session:
            refreshed = await session.get(TaskRun, run_id)
            assert refreshed.status == "running", "managed run should not be touched by sweep"

    @pytest.mark.parametrize(
        ("role", "status", "alive"),
        [
            # Runs whose PID is still alive should be left untouched.
            pytest.param("developer", "running", True, id="alive-process"),
            # Paused runs (gate failures) must not be reclassified by the sweep.
            pytest.param("researcher", "paused", False, id="paused-run"),
            # A live-PID awaiting_approval run (spec genuinely pending human review) is the normal case.
            pytest.param("researcher", "awaiting_approval", True, id="live-pid-awaiting-approval"),
        ],
    )
    async def test_sweep_leaves_run_untouched(self, role: str, status: str, alive: bool) -> None:
        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="45",
                    role=role,
                    status=status,
                    pid=999996,
                    project_slug="test",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        with self._patch_sweep_deps(
            **{
                "sova.dashboard.services.control_service._is_process_alive": {
                    "return_value": alive,
                },
            }
        ):
            from sova.dashboard.app import _liveness_sweep_once

            await _liveness_sweep_once(None, is_multi=False)

        async with await get_session() as session:
            refreshed = await session.get(TaskRun, run_id)
            assert refreshed.status == status, f"{status} run should not be touched by the sweep"

    async def test_sweep_does_not_touch_dead_pid_awaiting_approval(self) -> None:
        """A dead-PID awaiting_approval run must NOT be reclassified by the sweep.

        The researcher's ephemeral process always exits within moments of writing
        its spec and setting awaiting_approval, so a dead PID here is the expected
        steady state, not a crash. check_already_running() already blocks
        re-research on awaiting_approval unconditionally regardless of PID
        liveness, and the slot gate excludes it from occupancy either way, so
        nothing depends on the sweep touching it, and reclassifying it as
        "interrupted" mislabels a successful spec completion as a crash.
        """
        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="126",
                    role="researcher",
                    status="awaiting_approval",
                    pid=999992,
                    project_slug="test",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        with self._patch_sweep_deps():
            from sova.dashboard.app import _liveness_sweep_once

            await _liveness_sweep_once(None, is_multi=False)

        async with await get_session() as session:
            refreshed = await session.get(TaskRun, run_id)
            assert refreshed.status == "awaiting_approval", "dead-PID awaiting_approval run should not be touched"

    async def test_sweep_write_retry_exhaustion_does_not_raise(self) -> None:
        """Phase 3 write retries exhausting on a locked DB must log and move on.

        Raising here would abandon every remaining project directory in the
        same sweep pass, which is the exact bug this retry loop exists to
        avoid. The run should be left untouched (still its original status)
        rather than the sweep crashing.
        """
        from sqlalchemy.exc import OperationalError

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="128",
                    role="developer",
                    status="running",
                    pid=999990,
                    project_slug="test",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        class _LockedBegin:
            async def __aenter__(self):
                raise OperationalError("BEGIN", {}, Exception("database is locked"))

            async def __aexit__(self, *exc_info):
                return False

        class _LockedSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            def begin(self):
                return _LockedBegin()

        call_count = 0

        async def _test_get_session(project_dir=None):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Phase 1 read must succeed so the dead run is discovered.
                return await get_session()
            return _LockedSession()

        with self._patch_sweep_deps(
            **{
                "sova.dashboard.app.get_session": {"new": _test_get_session},
                "sova.dashboard.app._SWEEP_WRITE_RETRY_DELAY": {"new": 0},
            }
        ):
            from sova.dashboard.app import _SWEEP_WRITE_RETRY_ATTEMPTS, _liveness_sweep_once

            await _liveness_sweep_once(None, is_multi=False)

        # Phase 1 (real session) + one call per retry attempt.
        assert call_count == 1 + _SWEEP_WRITE_RETRY_ATTEMPTS

        async with await get_session() as session:
            refreshed = await session.get(TaskRun, run_id)
            assert refreshed.status == "running", "exhausted retries must leave the run untouched, not raise"


class TestWaitAndFinalizeOutputWriter:
    """Cover the output_writer.close() try/except in _wait_and_finalize."""

    async def test_output_writer_close_called(self, tmp_path: Path) -> None:
        """output_writer.close() should be called before finalization."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_writer = MagicMock()
        mock_writer.close = AsyncMock()

        agent = AgentState(
            run_id=60,
            issue="200",
            role="developer",
            process=mock_process,
            output_writer=mock_writer,
            project_dir=tmp_path,
        )

        pa = ProjectAgents()
        pa.agents[60] = agent

        with (
            patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        mock_writer.close.assert_awaited_once()

    async def test_output_writer_close_error_does_not_block_finalize(self, tmp_path: Path) -> None:
        """If output_writer.close() raises, finalization must still proceed."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_writer = MagicMock()
        mock_writer.close = AsyncMock(side_effect=OSError("disk full"))

        agent = AgentState(
            run_id=61,
            issue="201",
            role="developer",
            process=mock_process,
            output_writer=mock_writer,
            project_dir=tmp_path,
        )

        pa = ProjectAgents()
        pa.agents[61] = agent

        finalize_called = []

        async def track_finalize(run_id, *, exit_code, agent):
            finalize_called.append(run_id)

        with (
            patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock, side_effect=track_finalize),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        assert finalize_called == [61], "finalization must run even if close() fails"
        assert 61 not in pa.agents


class TestWaitAndFinalizeStreamFailure:
    """A runtime stream parser's terminal failure must never finalize as success."""

    async def test_stream_failure_with_zero_exit_code_downgrades_to_failed(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)

        agent = AgentState(
            run_id=70,
            issue="300",
            role="reviewer",
            process=mock_process,
            project_dir=tmp_path,
            stream_failure="codex sandbox denied write access",
        )

        pa = ProjectAgents()
        pa.agents[70] = agent

        recorded_exit_codes = []

        async def track_finalize(run_id, *, exit_code, agent):
            recorded_exit_codes.append(exit_code)
            return True

        with (
            patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock, side_effect=track_finalize),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock) as mock_ah,
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        assert recorded_exit_codes == [1]
        mock_ah.assert_not_awaited()
        completed = pa.recently_completed[0]
        assert completed.status == "failed"

    async def test_stream_failure_with_nonzero_exit_code_no_double_downgrade(self, tmp_path: Path) -> None:
        """When the process already exited nonzero, the stream failure adds no extra action."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=1)

        agent = AgentState(
            run_id=71,
            issue="301",
            role="reviewer",
            process=mock_process,
            project_dir=tmp_path,
            stream_failure="codex sandbox denied write access",
        )

        pa = ProjectAgents()
        pa.agents[71] = agent

        recorded_exit_codes = []

        async def track_finalize(run_id, *, exit_code, agent):
            recorded_exit_codes.append(exit_code)
            return True

        with (
            patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock, side_effect=track_finalize),
            patch.object(agent_finalize, "log") as mock_log,
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        assert recorded_exit_codes == [1]
        stream_warnings = [
            c for c in mock_log.warning.call_args_list if c.args[:1] == ("finalize.stream_reported_failure",)
        ]
        assert stream_warnings == []

    async def test_no_stream_failure_finalizes_as_success(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)

        agent = AgentState(
            run_id=72,
            issue="302",
            role="reviewer",
            process=mock_process,
            project_dir=tmp_path,
        )

        pa = ProjectAgents()
        pa.agents[72] = agent

        recorded_exit_codes = []

        async def track_finalize(run_id, *, exit_code, agent):
            recorded_exit_codes.append(exit_code)
            return True

        with (
            patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock, side_effect=track_finalize),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock) as mock_ah,
            patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        assert recorded_exit_codes == [0]
        mock_ah.assert_awaited_once()
        completed = pa.recently_completed[0]
        assert completed.status == "done"

    async def _finalize_with_late_reader(
        self,
        *,
        run_id: int,
        stream_parser,
        reader_delay: float,
        tmp_path: Path,
        drain_timeout: float | None = None,
    ) -> tuple[list[int], object, object]:
        """Run _wait_and_finalize against an agent whose reader sets stream_failure late."""
        import asyncio
        import contextlib
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_finalize, agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)

        agent = AgentState(
            run_id=run_id,
            issue="303",
            role="reviewer",
            process=mock_process,
            project_dir=tmp_path,
            stream_parser=stream_parser,
        )

        async def late_reader() -> None:
            await asyncio.sleep(reader_delay)
            agent.stream_failure = "codex turn.failed written just before exit"

        agent.reader_task = asyncio.create_task(late_reader())

        pa = ProjectAgents()
        pa.agents[run_id] = agent

        recorded_exit_codes: list[int] = []

        async def track_finalize(run_id, *, exit_code, agent):
            recorded_exit_codes.append(exit_code)
            return True

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(agent_finalize, "_finalize_task_run", new_callable=AsyncMock, side_effect=track_finalize)
            )
            mock_log = stack.enter_context(patch.object(agent_finalize, "log"))
            stack.enter_context(
                patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new_callable=AsyncMock)
            )
            stack.enter_context(
                patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock)
            )
            stack.enter_context(
                patch("sova.dashboard.services.agent_finalize.load_config", side_effect=Exception("skip"))
            )
            if drain_timeout is not None:
                stack.enter_context(patch.object(agent_finalize, "_READER_DRAIN_TIMEOUT", drain_timeout))

            await agent_lifecycle._wait_and_finalize(pa, agent)

        if not agent.reader_task.done():
            agent.reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await agent.reader_task
        return recorded_exit_codes, mock_log, agent

    async def test_pending_reader_is_drained_before_stream_failure_is_read(self, tmp_path: Path) -> None:
        """The terminal JSONL event is normally still unread when process.wait() returns.

        Without draining the reader first, ``stream_failure`` is observed as
        ``None`` and a stream-reported failure finalizes as success.
        """
        from unittest.mock import MagicMock

        exit_codes, _log, agent = await self._finalize_with_late_reader(
            run_id=73,
            stream_parser=MagicMock(),
            reader_delay=0.05,
            tmp_path=tmp_path,
        )

        assert exit_codes == [1]
        assert agent.stream_failure is not None

    async def test_drain_is_skipped_when_runtime_has_no_stream_parser(self, tmp_path: Path) -> None:
        """Claude/Aider report failures via the exit code, so finalization must not wait on the reader."""
        exit_codes, _log, _agent = await self._finalize_with_late_reader(
            run_id=74,
            stream_parser=None,
            reader_delay=0.05,
            tmp_path=tmp_path,
        )

        assert exit_codes == [0]

    async def test_drain_timeout_falls_back_to_the_exit_code(self, tmp_path: Path) -> None:
        """A reader that overruns the bound is logged; finalization proceeds rather than hanging."""
        from unittest.mock import MagicMock

        exit_codes, mock_log, _agent = await self._finalize_with_late_reader(
            run_id=75,
            stream_parser=MagicMock(),
            reader_delay=5.0,
            drain_timeout=0.01,
            tmp_path=tmp_path,
        )

        assert exit_codes == [0]
        assert [c for c in mock_log.warning.call_args_list if c.args[:1] == ("finalize.reader_drain_timeout",)]

    async def test_drain_logs_when_reader_task_raises(self, tmp_path: Path) -> None:
        """A reader task that crashes mid-drain must be logged, not silently treated as a clean drain."""
        import asyncio
        from unittest.mock import patch

        from sova.dashboard.services import agent_finalize
        from sova.dashboard.services.agent_pool import AgentState

        agent = AgentState(
            run_id=76,
            issue="304",
            role="reviewer",
            process=None,
            project_dir=tmp_path,
            stream_parser=object(),
        )

        async def failing_reader() -> None:
            raise RuntimeError("malformed jsonl line")

        agent.reader_task = asyncio.create_task(failing_reader())

        with patch.object(agent_finalize, "log") as mock_log:
            await agent_finalize._drain_stream_reader(agent)

        assert agent.reader_task.done()
        assert [c for c in mock_log.warning.call_args_list if c.args[:1] == ("finalize.reader_task_failed",)]

    async def test_drain_logs_when_reader_task_already_done_before_drain_starts(self, tmp_path: Path) -> None:
        """A reader task that crashed BEFORE _drain_stream_reader was ever called must still be
        surfaced. The early ``task.done()`` guard must not return without checking the exception:
        that would silently reintroduce the crashed-reader gap, only narrowed to this timing
        window instead of the mid-drain window covered by test_drain_logs_when_reader_task_raises.
        """
        import asyncio
        import contextlib
        from unittest.mock import patch

        from sova.dashboard.services import agent_finalize
        from sova.dashboard.services.agent_pool import AgentState

        agent = AgentState(
            run_id=79,
            issue="307",
            role="reviewer",
            process=None,
            project_dir=tmp_path,
            stream_parser=object(),
        )

        async def failing_reader() -> None:
            raise RuntimeError("malformed jsonl line")

        agent.reader_task = asyncio.create_task(failing_reader())
        # Let the task actually run and crash before _drain_stream_reader is ever invoked,
        # so task.done() is already True on entry.
        with contextlib.suppress(RuntimeError):
            await agent.reader_task
        assert agent.reader_task.done()

        with patch.object(agent_finalize, "log") as mock_log:
            await agent_finalize._drain_stream_reader(agent)

        assert [c for c in mock_log.warning.call_args_list if c.args[:1] == ("finalize.reader_task_failed",)]

    async def test_resolve_reader_drain_timeout_uses_value_resolved_at_spawn_time(self, tmp_path: Path) -> None:
        """The drain bound comes from AgentState.stream_reader_drain_timeout (set once at spawn
        time from AgentRuntime.stream_reader_drain_timeout()), not a config read on the hot path.
        """
        from sova.dashboard.services import agent_finalize
        from sova.dashboard.services.agent_pool import AgentState

        agent = AgentState(
            run_id=77,
            issue="305",
            role="reviewer",
            process=None,
            project_dir=tmp_path,
            stream_reader_drain_timeout=42.0,
        )

        timeout = agent_finalize._resolve_reader_drain_timeout(agent)

        assert timeout == 42.0

    async def test_resolve_reader_drain_timeout_falls_back_when_unset(self, tmp_path: Path) -> None:
        """A runtime that declares no tuning of its own (stream_reader_drain_timeout=None) falls
        back to the module default, with no config read involved.
        """
        from sova.dashboard.services import agent_finalize
        from sova.dashboard.services.agent_pool import AgentState

        agent = AgentState(
            run_id=78,
            issue="306",
            role="reviewer",
            process=None,
            project_dir=tmp_path,
        )

        timeout = agent_finalize._resolve_reader_drain_timeout(agent)

        assert timeout == agent_finalize._READER_DRAIN_TIMEOUT
