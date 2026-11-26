"""agent_lifecycle start_agent and start_command: duplicate prevention, worktree resolution, gates.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient
from structlog.testing import capture_logs

from sova.dashboard.services.agent_lifecycle import ProjectAgents, cancel_background_tasks, start_command
from sova.db.models import TaskRun
from sova.db.session import get_session
from sova.git.artifact_exclusions import STASH_EXCLUDED_PATHS, exclude_pathspecs, unstage_command
from sova.utils import agent_leases
from sova.utils.env import PYTEST_WORKERS_ENV_KEY
from tests.process_fakes import FakeAgentRuntime, FakeGh, add_issue_worktree

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# Control Service: duplicate agent prevention
# ---------------------------------------------------------------------------


class TestDuplicateAgentPrevention:
    async def test_start_agent_rejects_duplicate_issue(self) -> None:
        """Starting an agent for an issue that already has one running should fail."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, ProjectAgents, start_agent

        pa = ProjectAgents()
        existing = AgentState(
            run_id=1,
            issue="73",
            role="developer",
            process=MagicMock(),
        )
        pa.agents[1] = existing

        with patch.object(agent_lifecycle, "_get_project_agents", return_value=pa):
            result = await start_agent("73")

        assert "error" in result
        assert "already has an active agent" in result["error"]
        assert result["existing_run_id"] == 1

    async def test_start_agent_allows_different_issue(self) -> None:
        """Starting an agent for a different issue should succeed (mocked spawn)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, ProjectAgents, start_agent

        pa = ProjectAgents()
        existing = AgentState(
            run_id=1,
            issue="73",
            role="developer",
            process=MagicMock(),
        )
        pa.agents[1] = existing

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", new_callable=AsyncMock, return_value=mock_process),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=2),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("74")

        assert result["status"] == "started"
        assert result["run_id"] == 2

    async def test_start_agent_passes_pr_number_to_task_run(self, tmp_path: Path) -> None:
        """pr_number should be forwarded to _create_task_run for immediate persistence."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=AsyncMock(return_value=mock_process)),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=3) as mock_create,
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value="feat/test"),
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=tmp_path / "wt",
            ),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("93", role="custom", pr_number=122)

        assert result["status"] == "started"
        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs.kwargs["pr_number"] == 122

    async def test_start_agent_skips_in_progress_for_reviewer(self, tmp_path: Path) -> None:
        """Reviewer agents must not trigger _transition_to_in_progress (would overwrite in_review)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", new_callable=AsyncMock, return_value=mock_process),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=4),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value="feat/test"),
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=tmp_path / "wt",
            ),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock) as mock_transition,
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("75", role="reviewer", pr_number=78)

        assert result["status"] == "started"
        mock_transition.assert_not_called()

    async def test_start_agent_skips_in_progress_for_address_review(self, tmp_path: Path) -> None:
        """Developer spawned with pr_number (address-review) must not transition to in_progress."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", new_callable=AsyncMock, return_value=mock_process),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=5),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value="feat/test"),
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=tmp_path / "wt",
            ),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock) as mock_transition,
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("80", role="developer", pr_number=88)

        assert result["status"] == "started"
        mock_transition.assert_not_called()

    async def test_start_agent_includes_run_id_in_prompt(self) -> None:
        """Developer role uses spawn_direct with --run-id in cmd_parts."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn_direct = AsyncMock(return_value=mock_process)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", mock_spawn_direct),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99")

        assert result["status"] == "started"
        assert mock_spawn_direct.call_args is not None, "spawn_direct() was never called"
        cmd_parts = mock_spawn_direct.call_args[0][0]
        assert "--run-id" in cmd_parts
        assert "7" in cmd_parts

    async def test_start_agent_uses_runtime_for_non_pipeline_roles(self) -> None:
        """The custom role goes through runtime.spawn(), never spawn_direct()."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn = AsyncMock(return_value=mock_process)
        mock_rt = MagicMock(spawn=mock_spawn)
        mock_spawn_direct = AsyncMock()

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_rt),
            patch.object(agent_lifecycle, "spawn_direct", mock_spawn_direct),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99", role="custom")

        assert result["status"] == "started"
        mock_spawn_direct.assert_not_awaited()
        assert mock_spawn.call_args is not None, "runtime.spawn() was never called"

    async def test_start_agent_uses_spawn_direct_for_reviewer(self) -> None:
        """The reviewer role never needed interactive coding-agent tool-use (it only makes
        a model call over a diff plus GitHub/DB writes), so it is spawned the same way
        pipeline roles are: as a plain trusted subprocess, never through an AgentRuntime."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn_direct = AsyncMock(return_value=mock_process)
        mock_runtime_spawn = AsyncMock()
        mock_rt = MagicMock(spawn=mock_runtime_spawn)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_rt),
            patch.object(agent_lifecycle, "spawn_direct", mock_spawn_direct),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99", role="reviewer")

        assert result["status"] == "started"
        mock_runtime_spawn.assert_not_awaited()
        assert mock_spawn_direct.call_args is not None, "spawn_direct() was never called"

    async def test_start_agent_threads_codex_api_key_to_reviewer_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CODEX_API_KEY is scrubbed from every spawned agent's environment by default
        (SCRUBBED_VARS), so the reviewer's optional Codex repo-context sub-call can only
        ever see it if it is re-admitted at the reviewer's own spawn boundary, and only
        when that sub-call is actually enabled via review.repo_context_agent."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        monkeypatch.setenv("CODEX_API_KEY", "codex-secret")

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn_direct = AsyncMock(return_value=mock_process)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", mock_spawn_direct),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_repo_context_agent_enabled", return_value=True),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99", role="reviewer")

        assert result["status"] == "started"
        assert mock_spawn_direct.call_args.kwargs["extra_env"] == {"CODEX_API_KEY": "codex-secret"}

    async def test_start_agent_omits_codex_api_key_when_repo_context_agent_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """review.repo_context_agent defaults off, so the common configuration must not
        export CODEX_API_KEY into the reviewer's process (and everything it spawns) at
        all, even though the key is present in the server's own environment."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        monkeypatch.setenv("CODEX_API_KEY", "codex-secret")

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn_direct = AsyncMock(return_value=mock_process)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", mock_spawn_direct),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_repo_context_agent_enabled", return_value=False),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99", role="reviewer")

        assert result["status"] == "started"
        assert mock_spawn_direct.call_args.kwargs["extra_env"] is None

    async def test_start_agent_omits_codex_api_key_for_non_reviewer_direct_spawn_role(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with the feature enabled, only the reviewer role's spawn gets the key:
        it is read solely by ReviewerRole._gather_repo_context()."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        monkeypatch.setenv("CODEX_API_KEY", "codex-secret")

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn_direct = AsyncMock(return_value=mock_process)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", mock_spawn_direct),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_repo_context_agent_enabled", return_value=True),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99", role="developer")

        assert result["status"] == "started"
        assert mock_spawn_direct.call_args.kwargs["extra_env"] is None

    async def test_start_agent_spawns_reviewer_via_spawn_direct_under_codex_runtime(self) -> None:
        """agent.runtime and llm.provider are orthogonal for the reviewer: since it never
        touches AgentRuntime at all, a project configured with agent.runtime = "codex"
        (for the developer/pipeline roles) must not block the reviewer from spawning."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn_direct = AsyncMock(return_value=mock_process)
        mock_rt = MagicMock(spawn=AsyncMock())
        mock_rt.name = "codex"

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_rt),
            patch.object(agent_lifecycle, "spawn_direct", mock_spawn_direct),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99", role="reviewer")

        assert result["status"] == "started"
        assert "error" not in result
        mock_rt.spawn.assert_not_awaited()
        mock_spawn_direct.assert_awaited_once()

    async def test_start_agent_warns_when_model_override_ignored_for_direct_spawn_role(self) -> None:
        """A direct-spawn role (reviewer, developer, ...) never reaches runtime.spawn(),
        so an explicit model override silently has no effect; this must be logged and
        surfaced to the API caller (dashboard model picker) rather than only the server
        log, since a caller reading just {"status": "started"} has no way to know their
        choice was discarded."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn_direct = AsyncMock(return_value=mock_process)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", mock_spawn_direct),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle.log, "warning") as mock_warning,
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99", role="reviewer", model="opus")

        assert result["status"] == "started"
        assert any(call.args and call.args[0] == "agent.model_override_ignored" for call in mock_warning.call_args_list)
        assert result["warnings"] == ["model override 'opus' ignored: reviewer runs resolve their model from config"]

    async def test_start_agent_omits_warnings_when_no_model_override(self) -> None:
        """The happy path (no override) must not carry a stale or empty warnings key."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn_direct = AsyncMock(return_value=mock_process)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", mock_spawn_direct),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99", role="reviewer")

        assert result["status"] == "started"
        assert "warnings" not in result

    async def test_start_agent_sets_stream_parser_from_runtime(self) -> None:
        """A non-pipeline spawn stores the runtime's per-process parser on AgentState."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn = AsyncMock(return_value=mock_process)
        sentinel_parser = object()
        mock_rt = MagicMock(
            spawn=mock_spawn,
            create_stream_parser=MagicMock(return_value=sentinel_parser),
            stream_reader_drain_timeout=MagicMock(return_value=42.0),
        )

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_rt),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99", role="custom")

        assert result["status"] == "started"
        assert pa.agents[7].stream_parser is sentinel_parser
        # Resolved once at spawn time from the runtime, not re-read from config at finalize.
        assert pa.agents[7].stream_reader_drain_timeout == 42.0

    async def test_start_agent_create_stream_parser_failure_does_not_orphan_process(self) -> None:
        """A parser-construction failure after a successful spawn must not be treated as a spawn failure.

        The subprocess is already running by the time create_stream_parser() is called, so
        raising here must not finalize the run as orphaned (which would leave a live, untracked
        process with no liveness-sweep entry). The run should proceed with stream_parser=None.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn = AsyncMock(return_value=mock_process)
        mock_rt = MagicMock(
            spawn=mock_spawn,
            create_stream_parser=MagicMock(side_effect=RuntimeError("bad codex jsonl schema")),
        )

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_rt),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_finalize_orphaned_run", new_callable=AsyncMock) as mock_orphan,
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99", role="custom")

        assert result["status"] == "started"
        assert pa.agents[7].stream_parser is None
        mock_orphan.assert_not_awaited()

    async def test_start_agent_drain_timeout_failure_does_not_discard_parser(self) -> None:
        """A stream_reader_drain_timeout() failure must not discard an already-built parser.

        create_stream_parser() and stream_reader_drain_timeout() are independent runtime
        calls; a failure in the second must fall back to the module default timeout, not
        wipe out the successfully constructed parser from the first.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn = AsyncMock(return_value=mock_process)
        sentinel_parser = object()
        mock_rt = MagicMock(
            spawn=mock_spawn,
            create_stream_parser=MagicMock(return_value=sentinel_parser),
            stream_reader_drain_timeout=MagicMock(side_effect=RuntimeError("bad codex config")),
        )

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_rt),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_finalize_orphaned_run", new_callable=AsyncMock) as mock_orphan,
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99", role="custom")

        assert result["status"] == "started"
        assert pa.agents[7].stream_parser is sentinel_parser
        assert pa.agents[7].stream_reader_drain_timeout is None
        mock_orphan.assert_not_awaited()

    async def test_start_agent_spawn_direct_leaves_stream_parser_none(self) -> None:
        """Pipeline roles bypass the runtime entirely (spawn_direct), so there is no parser to attach."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn_direct = AsyncMock(return_value=mock_process)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", mock_spawn_direct),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99")

        assert result["status"] == "started"
        assert pa.agents[7].stream_parser is None

    async def test_start_agent_cleans_up_on_spawn_failure(self) -> None:
        """If process spawn fails, the pre-created TaskRun should be marked failed."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(
                agent_lifecycle,
                "spawn_direct",
                new_callable=AsyncMock,
                side_effect=OSError("spawn failed"),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=8),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_finalize_orphaned_run", new_callable=AsyncMock) as mock_orphan,
        ):
            result = await start_agent("100")

        assert "error" in result
        mock_orphan.assert_awaited_once_with(8, pa.project_dir)

    async def test_start_agent_resolves_issue_from_pr(self) -> None:
        """When issue is empty but pr_number is set, derive issue from PR body."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", new_callable=AsyncMock, return_value=mock_process),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=10) as mock_create,
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_from_pr",
                new_callable=AsyncMock,
                return_value="55",
            ) as mock_resolve,
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_branch_name",
                new_callable=AsyncMock,
                return_value="fix/issue-55",
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=pa.project_dir / ".claude" / "worktrees" / "55",
            ),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("", role="developer", pr_number=332)

        assert result["status"] == "started"
        mock_resolve.assert_awaited_once_with(332, pa.project_dir)
        call_args = mock_create.call_args
        assert call_args[0][0] == "55"

    async def test_start_agent_no_issue_resolve_when_issue_provided(self) -> None:
        """When issue is already provided, do not call _resolve_issue_from_pr."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        worktree_path = pa.project_dir / ".claude" / "worktrees" / "42"

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", new_callable=AsyncMock, return_value=mock_process),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=11),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_from_pr",
                new_callable=AsyncMock,
                return_value="99",
            ) as mock_resolve,
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_branch_name",
                new_callable=AsyncMock,
                return_value="fix/issue-42",
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=worktree_path,
            ),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("42", role="developer", pr_number=332)

        assert result["status"] == "started"
        mock_resolve.assert_not_awaited()

    async def test_start_agent_resolves_worktree_from_pr(self) -> None:
        """start_agent with pr_number should resolve worktree via branch lookup."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        worktree_path = pa.project_dir / ".claude" / "worktrees" / "55"

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(
                agent_lifecycle, "spawn_direct", new_callable=AsyncMock, return_value=mock_process
            ) as mock_spawn_direct,
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=10),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_from_pr",
                new_callable=AsyncMock,
                return_value="55",
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_branch_name",
                new_callable=AsyncMock,
                return_value="fix/issue-55",
            ) as mock_branch,
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=worktree_path,
            ) as mock_wt,
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("", role="developer", pr_number=332)

        assert result["status"] == "started"
        mock_branch.assert_awaited_once_with(332, pa.project_dir)
        mock_wt.assert_awaited_once_with("55", pa.project_dir, branch_name="fix/issue-55", pr_number=332)
        actual_cwd = mock_spawn_direct.call_args[0][1]
        assert actual_cwd == worktree_path

    async def test_start_agent_recovers_pr_number_from_db_history(self, tmp_path: Path) -> None:
        """When start_agent is called for a developer run without pr_number but a prior
        terminal run for the same issue has one, start_agent must recover that pr_number
        so the agent gets --pr and routes to the address-review pipeline instead of
        failing at AssessStep with 'Open PR already exists'."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        # Seed a terminal developer run with a known pr_number in the test DB
        async with await get_session() as session:
            async with session.begin():
                prior_run = TaskRun(
                    issue_number="344",
                    role="developer",
                    status="interrupted",
                    pr_number=372,
                )
                session.add(prior_run)

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 98765

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        spawned_cmd: list[list[str]] = []

        async def _capture_spawn(cmd_parts, cwd, **kwargs):
            spawned_cmd.append(cmd_parts)
            return mock_process

        original = get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", side_effect=_capture_spawn),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=99),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_update_task_run_output_path", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(
                agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value="feat/issue-344"
            ),
            patch.object(
                agent_lifecycle, "_resolve_issue_worktree", new_callable=AsyncMock, return_value=tmp_path / "wt"
            ),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value=None),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
            patch("sova.dashboard.services.agent_lifecycle.get_session", side_effect=_ignore_project_dir),
            patch("sova.dashboard.services.agent_validation.get_session", side_effect=_ignore_project_dir),
        ):
            result = await start_agent("344", role="developer")  # no pr_number passed

        assert result.get("status") == "started", f"Expected started, got: {result}"
        assert spawned_cmd, "spawn_direct was never called"
        cmd = spawned_cmd[0]
        assert "--pr" in cmd and "372" in cmd, f"Expected '--pr 372' in cmd, got: {cmd}"

    async def test_start_agent_does_not_recover_pr_number_for_reviewer(self, tmp_path: Path) -> None:
        """PR number recovery from DB history should only apply to 'developer' role.
        Other roles are not affected."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        # Seed a terminal developer run with a pr_number
        async with await get_session() as session:
            async with session.begin():
                session.add(TaskRun(issue_number="344", role="developer", status="done", pr_number=372))

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 11111

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        spawned_cmd: list[list[str]] = []

        async def _capture_spawn_direct(cmd_parts, cwd, **kwargs):
            spawned_cmd.append(cmd_parts)
            return mock_process

        original = get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "spawn_direct", _capture_spawn_direct),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=88),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_update_task_run_output_path", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value=""),
            patch.object(
                agent_lifecycle, "_resolve_issue_worktree", new_callable=AsyncMock, return_value=tmp_path / "wt"
            ),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value=None),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
            patch("sova.dashboard.services.agent_lifecycle.get_session", side_effect=_ignore_project_dir),
            patch("sova.dashboard.services.agent_validation.get_session", side_effect=_ignore_project_dir),
        ):
            result = await start_agent("344", role="reviewer")  # reviewer role

        assert result.get("status") == "started"
        assert spawned_cmd
        # reviewer should NOT have --pr recovered from history
        assert "--pr" not in spawned_cmd[0], f"Reviewer should not get --pr: {spawned_cmd[0]}"

    async def test_recover_pr_number_returns_none_on_db_error(self, tmp_path: Path) -> None:
        """_recover_last_pr_number must return None (not raise) when the DB query fails."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _recover_last_pr_number

        with patch("sova.dashboard.services.agent_lifecycle.get_session", side_effect=RuntimeError("db boom")):
            result = await _recover_last_pr_number("42", tmp_path / "proj")

        assert result is None

    async def test_recover_pr_number_includes_stopped_runs(self, tmp_path: Path) -> None:
        """A manually-stopped developer run (issue #978) with a pr_number must still

        be recoverable: before the "stopped" status existed, a deliberately-stopped
        run exited nonzero and was classified "interrupted"/"failed", both of which
        this query already covered.
        """
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _recover_last_pr_number

        async with await get_session() as session:
            async with session.begin():
                session.add(
                    TaskRun(
                        issue_number="345",
                        role="developer",
                        status="stopped",
                        termination_reason="manual_stop",
                        pr_number=373,
                    )
                )

        original = get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with patch("sova.dashboard.services.agent_lifecycle.get_session", side_effect=_ignore_project_dir):
            result = await _recover_last_pr_number("345", tmp_path / "proj")

        assert result == 373

    async def test_start_command_rejects_duplicate_issue(self) -> None:
        """start_command() should reject if the same issue already has an active agent."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, ProjectAgents, start_command

        pa = ProjectAgents()
        existing = AgentState(
            run_id=1,
            issue="16",
            role="developer",
            process=MagicMock(),
        )
        pa.agents[1] = existing

        with patch.object(agent_lifecycle, "_get_project_agents", return_value=pa):
            result = await start_command("integrate-pr", {"issue": "16"})

        assert "error" in result
        assert "already has an active agent" in result["error"]

    async def test_check_issue_conflict_catches_db_duplicate(self) -> None:
        """_check_issue_conflict should detect DB TaskRuns with alive PIDs."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import ProjectAgents, _check_issue_conflict
        from sova.db.models import TaskRun
        from sova.db.session import get_session as real_get_session

        pa = ProjectAgents()

        async with await real_get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="16",
                    role="developer",
                    status="running",
                    pid=12345,
                    current_step="develop",
                )
                session.add(run)

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with (
            patch("sova.dashboard.services.agent_validation.get_session", side_effect=_ignore_project_dir),
            patch(
                "sova.dashboard.services.agent_recovery._is_process_alive",
                return_value=True,
            ),
        ):
            result = await _check_issue_conflict("16", pa)

        assert result is not None
        assert "already has an active agent" in result["error"]
        assert "external" in result["error"]

    async def test_check_issue_conflict_degrades_on_db_failure(self) -> None:
        """_check_issue_conflict should return None (no conflict) if DB query fails."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import ProjectAgents, _check_issue_conflict

        pa = ProjectAgents()

        with patch("sova.dashboard.services.agent_validation.get_session", side_effect=RuntimeError("DB down")):
            result = await _check_issue_conflict("16", pa)

        assert result is None

    async def test_check_issue_conflict_auto_recovers_dead_pid(self) -> None:
        """_check_issue_conflict should mark dead-PID DB runs as interrupted."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import ProjectAgents, _check_issue_conflict
        from sova.db.models import TaskRun
        from sova.db.session import get_session as real_get_session

        pa = ProjectAgents()

        async with await real_get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="84", role="developer", status="running", pid=999999)
                session.add(run)
                await session.flush()
                run_id = run.id

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with patch("sova.dashboard.services.agent_validation.get_session", side_effect=_ignore_project_dir):
            result = await _check_issue_conflict("84", pa)

        assert result is None

        async with await real_get_session() as session:
            async with session.begin():
                updated = await session.get(TaskRun, run_id)
                assert updated.status == "interrupted"
                assert updated.error_message is not None

    async def test_check_issue_conflict_force_skips_live_external(self) -> None:
        """_check_issue_conflict with force=True should skip live external agents."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import ProjectAgents, _check_issue_conflict
        from sova.db.models import TaskRun
        from sova.db.session import get_session as real_get_session

        pa = ProjectAgents()

        async with await real_get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="85", role="developer", status="running", pid=12345)
                session.add(run)

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with (
            patch("sova.dashboard.services.agent_validation.get_session", side_effect=_ignore_project_dir),
            patch("sova.dashboard.services.agent_recovery._is_process_alive", return_value=True),
        ):
            result_no_force = await _check_issue_conflict("85", pa)
            assert result_no_force is not None
            assert "already has an active agent" in result_no_force["error"]

            result_force = await _check_issue_conflict("85", pa, force=True)
            assert result_force is None


# ---------------------------------------------------------------------------
# start_command worktree resolution
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stub_collision_cleanup")
class TestStartCommandWorktreeResolution:
    """start_command() should use an existing worktree as cwd when available."""

    async def test_uses_worktree_when_exists(
        self, project_agents: ProjectAgents, fake_runtime: FakeAgentRuntime, fake_gh: FakeGh, git_project: Path
    ) -> None:
        """When a worktree exists for the issue, the agent runs in it, not in the primary checkout."""
        worktree = add_issue_worktree(git_project, "42", "feat/issue-42")
        fake_gh.respond("headRefOid", stdout="abc123\n")

        result = await start_command("address-pr", {"issue": "42", "pr": 10})

        assert result["status"] == "started"
        assert fake_runtime.last_spawn["cwd"] == worktree
        assert fake_runtime.last_spawn["network_access"] is True
        assert project_agents.agents[result["run_id"]].worktree_path == worktree
        async with await get_session() as session:
            run = await session.get(TaskRun, result["run_id"])
        assert run is not None
        assert run.issue_number == "42"
        assert run.role == "command:address-pr"
        assert run.status == "running"

    async def test_a_command_agent_is_leased_for_its_lifetime(
        self, project_agents: ProjectAgents, fake_runtime: FakeAgentRuntime, fake_gh: FakeGh, git_project: Path
    ) -> None:
        """`claude -p /<command>` is not a `sova run`, so the dashboard leases on its behalf and releases on reap."""
        add_issue_worktree(git_project, "42", "feat/issue-42")
        fake_gh.respond("headRefOid", stdout="abc123\n")
        child = subprocess.Popen(["sleep", "60"])
        try:
            fake_runtime.pid = child.pid

            def leases() -> list[Path]:
                return list(agent_leases._LEASE_DIR.glob(f"{child.pid}-*"))

            result = await start_command("address-pr", {"issue": "42", "pr": 10})
            assert result["status"] == "started"
            assert len(leases()) == 1

            await cancel_background_tasks()
            for _ in range(100):
                if not leases():
                    break
                await asyncio.sleep(0.01)
            assert leases() == []
        finally:
            child.kill()
            child.wait()

    @pytest.mark.parametrize(
        ("operator_value", "expected"),
        [pytest.param(None, "3", id="configured"), pytest.param("5", "5", id="operator-wins")],
    )
    async def test_spawn_env_carries_the_pytest_worker_cap(
        self,
        project_agents: ProjectAgents,
        fake_runtime: FakeAgentRuntime,
        fake_gh: FakeGh,
        git_project: Path,
        monkeypatch: pytest.MonkeyPatch,
        operator_value: str | None,
        expected: str,
    ) -> None:
        """A command agent runs its own `make check`, so it gets the worker cap unless the operator set one."""
        add_issue_worktree(git_project, "42", "feat/issue-42")
        fake_gh.respond("headRefOid", stdout="abc123\n")
        # The cap is "set unless the operator already did": a value inherited from the shell running the tests is one.
        monkeypatch.delenv(PYTEST_WORKERS_ENV_KEY, raising=False)
        monkeypatch.setenv("SOVA_TESTING_CHECK_WORKERS", "3")
        if operator_value is not None:
            monkeypatch.setenv(PYTEST_WORKERS_ENV_KEY, operator_value)

        result = await start_command("address-pr", {"issue": "42", "pr": 10})

        assert result["status"] == "started"
        assert fake_runtime.last_spawn["env"][PYTEST_WORKERS_ENV_KEY] == expected

    async def test_spawn_env_carries_the_project_gh_token(
        self,
        project_agents: ProjectAgents,
        fake_runtime: FakeAgentRuntime,
        fake_gh: FakeGh,
        git_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The configured github_user's token reaches the spawned agent as GH_TOKEN."""
        add_issue_worktree(git_project, "42", "feat/issue-42")
        fake_gh.respond("headRefOid", stdout="abc123\n")
        fake_gh.respond("auth", "token", stdout="t\n")
        monkeypatch.setenv("SOVA_GITHUB_USER", "octocat")

        result = await start_command("address-pr", {"issue": "42", "pr": 10})

        assert result["status"] == "started"
        assert fake_gh.called_with("auth", "token", "--user", "octocat")
        assert fake_runtime.last_spawn["env"]["GH_TOKEN"] == "t"

    async def test_rejects_pr_command_when_worktree_isolation_fails(
        self, project_agents: ProjectAgents, fake_runtime: FakeAgentRuntime, fake_gh: FakeGh
    ) -> None:
        """PR-based commands are rejected, and nothing is spawned, when no worktree exists for the PR's issue."""
        result = await start_command("address-pr", {"issue": "42", "pr": 10})

        assert "worktree isolation failed" in result["error"]
        assert fake_runtime.spawn_calls == []
        assert project_agents.agents == {}

    async def test_falls_back_to_project_dir_when_no_pr(
        self, project_agents: ProjectAgents, fake_runtime: FakeAgentRuntime, git_project: Path
    ) -> None:
        """Non-PR commands fall back to the project directory when no worktree exists."""
        result = await start_command("address-pr", {"issue": "42"})

        assert result["status"] == "started"
        assert fake_runtime.last_spawn["cwd"] == git_project

    @pytest.mark.parametrize(
        "issue",
        [
            pytest.param("42", id="returns-worktree-path"),
            pytest.param("#42", id="strips-hash"),
        ],
    )
    async def test_resolve_issue_worktree_returns_existing_worktree(self, tmp_path: Path, issue: str) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_worktree

        worktree = tmp_path / ".claude" / "worktrees" / "42"
        worktree.mkdir(parents=True)
        with patch(
            "sova.dashboard.services.agent_context.ensure_worktree_usable",
            new_callable=AsyncMock,
            return_value=worktree,
        ):
            assert await _resolve_issue_worktree(issue, tmp_path) == worktree

    @pytest.mark.parametrize(
        "issue",
        [
            pytest.param("42", id="no-worktree-exists"),
            pytest.param("standup", id="non-numeric-issue"),
        ],
    )
    async def test_resolve_issue_worktree_falls_back_to_project_dir(self, tmp_path: Path, issue: str) -> None:
        from sova.dashboard.services.agent_lifecycle import _resolve_issue_worktree

        assert await _resolve_issue_worktree(issue, tmp_path) == tmp_path

    async def test_resolve_issue_worktree_repopulates_missing_artifacts(self, tmp_path: Path) -> None:
        """A reused issue-based worktree missing .claude artifacts gets them repopulated in place."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_worktree

        (tmp_path / ".claude" / "commands").mkdir(parents=True)
        (tmp_path / ".claude" / "commands" / "dev.md").write_text("cmd")
        worktree = tmp_path / ".claude" / "worktrees" / "42"
        worktree.mkdir(parents=True)
        (worktree / ".git").touch()

        git_dir_ok = AsyncMock()
        git_dir_ok.success = True
        with patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=git_dir_ok):
            result = await _resolve_issue_worktree("42", tmp_path)

        assert result == worktree
        assert (worktree / ".claude" / "commands" / "dev.md").read_text() == "cmd"

    async def test_resolve_issue_worktree_recreates_broken_worktree(self, tmp_path: Path) -> None:
        """An issue-based worktree with broken git linkage is recreated when the branch is known."""
        from dataclasses import dataclass
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        @dataclass
        class FakeWorktreeInfo:
            path: Path
            branch: str
            issue_id: str

        worktree = tmp_path / ".claude" / "worktrees" / "42"
        worktree.mkdir(parents=True)
        fake_info = FakeWorktreeInfo(path=worktree, branch="feat/issue-42", issue_id="42")

        git_dir_fail = AsyncMock()
        git_dir_fail.success = False
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=git_dir_fail),
            patch("sova.git.worktree.create_worktree", new_callable=AsyncMock, return_value=fake_info) as mock_create,
        ):
            result = await _resolve_issue_worktree("42", tmp_path, branch_name="feat/issue-42")

        assert result == worktree
        mock_create.assert_awaited_once_with(
            issue_id="42",
            branch="feat/issue-42",
            base_branch="HEAD",
            project_dir=tmp_path,
        )

    async def test_resolve_issue_worktree_unusable_no_branch_falls_back_to_project_dir(self, tmp_path: Path) -> None:
        """An unusable issue-based worktree with no known branch falls back to project_dir."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_worktree

        worktree = tmp_path / ".claude" / "worktrees" / "42"
        worktree.mkdir(parents=True)

        git_dir_fail = AsyncMock()
        git_dir_fail.success = False
        with patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=git_dir_fail):
            result = await _resolve_issue_worktree("42", tmp_path)

        assert result == tmp_path

    async def test_resolve_issue_worktree_branch_fallback(self, tmp_path: Path) -> None:
        """_resolve_issue_worktree falls back to branch-based lookup."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        wt_path = tmp_path / "worktrees" / "feat-branch"
        wt_path.mkdir(parents=True)
        with (
            patch(
                "sova.dashboard.services.agent_context.find_worktree_by_branch",
                new_callable=AsyncMock,
                return_value=wt_path,
            ),
            patch(
                "sova.dashboard.services.agent_context.ensure_worktree_usable",
                new_callable=AsyncMock,
                return_value=wt_path,
            ),
        ):
            result = await _resolve_issue_worktree("standup", tmp_path, branch_name="feat/issue-99")
        assert result == wt_path

    async def test_resolve_issue_worktree_branch_fallback_unusable_falls_back_to_project_dir(
        self, tmp_path: Path
    ) -> None:
        """An unusable-but-existing branch worktree must not trigger a mismatched fresh create.

        The branch is still checked out at the discovered (but unusable) worktree
        path. Recomputing a fresh identity (issue_id/pr-N/sanitized branch name)
        and calling create_worktree() with it would try to add a second worktree
        for a branch git already has checked out elsewhere, and fail. The
        resolver must go straight to project_dir instead of attempting that.
        """
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        stale_wt_path = tmp_path / "elsewhere" / "feat-branch"

        with (
            patch(
                "sova.dashboard.services.agent_context.find_worktree_by_branch",
                new_callable=AsyncMock,
                return_value=stale_wt_path,
            ),
            patch(
                "sova.dashboard.services.agent_context.ensure_worktree_usable",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "sova.git.worktree.create_worktree",
                new_callable=AsyncMock,
            ) as mock_create,
            patch("sova.dashboard.services.agent_context.run_shell", new_callable=AsyncMock) as mock_run_shell,
        ):
            result = await _resolve_issue_worktree("42", tmp_path, branch_name="feat/issue-42")

        assert result == tmp_path
        mock_create.assert_not_awaited()
        # branch_on_main must stay False: the stale worktree is not the main repo,
        # so no stash/checkout dance should be attempted either.
        mock_run_shell.assert_not_awaited()

    async def test_resolve_issue_worktree_branch_fallback_filters_main(self, tmp_path: Path) -> None:
        """_resolve_issue_worktree skips branch worktree if it equals project_dir."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        with patch(
            "sova.dashboard.services.agent_context.find_worktree_by_branch",
            new_callable=AsyncMock,
            return_value=tmp_path,
        ):
            result = await _resolve_issue_worktree("standup", tmp_path, branch_name="main")
        assert result == tmp_path

    async def test_resolve_issue_worktree_branch_fallback_error(self, tmp_path: Path) -> None:
        """_resolve_issue_worktree handles branch lookup errors gracefully."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        with patch(
            "sova.dashboard.services.agent_context.find_worktree_by_branch",
            new_callable=AsyncMock,
            side_effect=RuntimeError("git error"),
        ):
            result = await _resolve_issue_worktree("standup", tmp_path, branch_name="broken")
        assert result == tmp_path

    async def test_resolve_issue_worktree_frees_branch_from_main(self, tmp_path: Path) -> None:
        """When branch is checked out in main repo, stash, switch to default branch, create worktree, pop stash."""
        from dataclasses import dataclass
        from unittest.mock import AsyncMock, call, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        @dataclass
        class FakeWorktreeInfo:
            path: Path
            branch: str
            issue_id: str

        wt_path = tmp_path / ".claude" / "worktrees" / "pr-99"
        fake_info = FakeWorktreeInfo(path=wt_path, branch="fix/stuff", issue_id="pr-99")
        mock_shell = AsyncMock()

        dirty_result = AsyncMock()
        dirty_result.success = True
        dirty_result.stdout = "M  some_file.py\n"

        success_result = AsyncMock()
        success_result.success = True
        success_result.stdout = "origin/main\n"

        stash_result = AsyncMock()
        stash_result.success = True
        stash_result.stdout = "Saved working directory and index state"

        pop_result = AsyncMock()
        pop_result.success = True
        pop_result.stdout = ""

        mock_shell.side_effect = [
            success_result,  # default branch lookup
            dirty_result,
            success_result,  # unstage runtime paths
            stash_result,
            success_result,  # checkout
            pop_result,
        ]

        with (
            patch(
                "sova.dashboard.services.agent_context.find_worktree_by_branch",
                new_callable=AsyncMock,
                return_value=tmp_path,
            ),
            patch(
                "sova.dashboard.services.agent_context.run_shell",
                mock_shell,
            ),
            patch(
                "sova.git.worktree.create_worktree",
                new_callable=AsyncMock,
                return_value=fake_info,
            ),
        ):
            result = await _resolve_issue_worktree("", tmp_path, branch_name="fix/stuff", pr_number=99)

        assert result == wt_path
        assert mock_shell.await_args_list == [
            call(
                "git",
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "--short",
                cwd=tmp_path,
                timeout=5,
            ),
            call("git", "status", "--porcelain", cwd=tmp_path, timeout=10),
            call(*unstage_command(STASH_EXCLUDED_PATHS), cwd=tmp_path, timeout=10),
            call(
                "git",
                "stash",
                "--include-untracked",
                "--",
                *exclude_pathspecs(STASH_EXCLUDED_PATHS),
                cwd=tmp_path,
                timeout=10,
            ),
            call("git", "checkout", "main", cwd=tmp_path, timeout=10),
            call("git", "stash", "pop", cwd=tmp_path, timeout=10),
        ]
        # The stash-pathspec probe runs on this request path too, so it gets the same 10 s budget.
        from sova.dashboard.services import agent_context

        agent_context.runtime_stash_pathspecs.assert_awaited_once_with(STASH_EXCLUDED_PATHS, tmp_path, timeout=10)

    async def test_resolve_issue_worktree_skips_pop_when_only_runtime_files_are_dirty(self, tmp_path: Path) -> None:
        """Runtime files make the checkout dirty but stay out of the stash, so nothing is popped (#1402)."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        wt_path = tmp_path / ".claude" / "worktrees" / "pr-99"
        mock_shell = AsyncMock(
            side_effect=[
                SimpleNamespace(success=True, stdout="origin/main\n", stderr=""),
                SimpleNamespace(success=True, stdout="?? .claude/sova.db-wal\n", stderr=""),
                SimpleNamespace(success=True, stdout="", stderr=""),
                SimpleNamespace(success=True, stdout="No local changes to save\n", stderr=""),
                SimpleNamespace(success=True, stdout="", stderr=""),
            ]
        )
        with (
            patch(
                "sova.dashboard.services.agent_context.find_worktree_by_branch",
                new_callable=AsyncMock,
                return_value=tmp_path,
            ),
            patch("sova.dashboard.services.agent_context.run_shell", mock_shell),
            patch(
                "sova.git.worktree.create_worktree",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(path=wt_path, branch="fix/stuff", issue_id="pr-99"),
            ),
        ):
            result = await _resolve_issue_worktree("", tmp_path, branch_name="fix/stuff", pr_number=99)

        assert result == wt_path
        assert not any("pop" in c.args for c in mock_shell.await_args_list)

    async def test_resolve_issue_worktree_returns_project_dir_on_switch_failure(self, tmp_path: Path) -> None:
        """When git checkout fails, stash is popped and project_dir returned without attempting create_worktree."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        mock_shell = AsyncMock()
        dirty_result = AsyncMock()
        dirty_result.success = True
        dirty_result.stdout = "M  file.py\n"

        stash_result = AsyncMock()
        stash_result.success = True
        stash_result.stdout = "Saved working directory and index state"

        symbolic_ref_result = AsyncMock()
        symbolic_ref_result.success = True
        symbolic_ref_result.stdout = "origin/main\n"

        checkout_result = AsyncMock()
        checkout_result.success = False
        checkout_result.stderr = "error: Your local changes would be overwritten"

        pop_result = AsyncMock()
        pop_result.success = True
        pop_result.stdout = ""

        unstage_result = AsyncMock()
        unstage_result.success = True
        unstage_result.stdout = ""

        mock_shell.side_effect = [
            symbolic_ref_result,
            dirty_result,
            unstage_result,  # unstage runtime paths
            stash_result,
            checkout_result,
            pop_result,  # stash pop after the failed checkout
        ]

        mock_create = AsyncMock()
        with (
            patch(
                "sova.dashboard.services.agent_context.find_worktree_by_branch",
                new_callable=AsyncMock,
                return_value=tmp_path,
            ),
            patch("sova.dashboard.services.agent_context.run_shell", mock_shell),
            patch("sova.git.worktree.create_worktree", mock_create),
        ):
            result = await _resolve_issue_worktree("", tmp_path, branch_name="fix/stuff", pr_number=99)

        assert result == tmp_path
        mock_create.assert_not_awaited()
        assert mock_shell.await_count == 6

    async def test_resolve_issue_worktree_clean_tree_skips_stash(self, tmp_path: Path) -> None:
        """Clean working tree: git status --porcelain returns empty, so stash is skipped entirely."""
        from dataclasses import dataclass
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        @dataclass
        class FakeWorktreeInfo:
            path: Path
            branch: str
            issue_id: str

        wt_path = tmp_path / ".claude" / "worktrees" / "pr-50"
        fake_info = FakeWorktreeInfo(path=wt_path, branch="fix/clean", issue_id="pr-50")
        mock_shell = AsyncMock()

        clean_status = AsyncMock()
        clean_status.success = True
        clean_status.stdout = ""

        success_result = AsyncMock()
        success_result.success = True
        success_result.stdout = "origin/main\n"

        mock_shell.side_effect = [success_result, clean_status, success_result]

        with (
            patch(
                "sova.dashboard.services.agent_context.find_worktree_by_branch",
                new_callable=AsyncMock,
                return_value=tmp_path,
            ),
            patch("sova.dashboard.services.agent_context.run_shell", mock_shell),
            patch("sova.git.worktree.create_worktree", new_callable=AsyncMock, return_value=fake_info),
        ):
            result = await _resolve_issue_worktree("", tmp_path, branch_name="fix/clean", pr_number=50)

        assert result == wt_path
        assert mock_shell.await_count == 3

    async def test_non_issue_command_uses_project_dir(
        self, project_agents: ProjectAgents, fake_runtime: FakeAgentRuntime, git_project: Path
    ) -> None:
        """Commands without an issue number always run in the project directory."""
        result = await start_command("standup", {})

        assert result["status"] == "started"
        assert fake_runtime.last_spawn["cwd"] == git_project


# ---------------------------------------------------------------------------
# start_command input validation (every caller, not only the HTTP route)
# ---------------------------------------------------------------------------


class TestStartCommandValidatesItsInput:
    @pytest.mark.parametrize(
        ("command", "args", "code"),
        [
            ("not-a-real-command", {}, "unknown_command"),
            ("../../etc/passwd", {}, "unknown_command"),
            ("/standup", {}, "unknown_command"),
            ("standup", {"model": "other"}, "invalid_command_args"),
            ("standup", {"issue": {"nested": "42"}}, "invalid_command_args"),
        ],
    )
    async def test_rejected_before_anything_starts(
        self, project_agents: ProjectAgents, fake_runtime: FakeAgentRuntime, command: str, args: dict, code: str
    ) -> None:
        with capture_logs() as logs:
            result = await start_command(command, args)

        assert result["code"] == code
        assert result["error"]
        assert fake_runtime.spawn_calls == []
        rejected = [e for e in logs if e["event"] == "start_command.rejected"]
        assert [(e["log_level"], e["code"]) for e in rejected] == [("warning", code)]

    @pytest.mark.parametrize(
        ("args", "accepted"),
        [
            (None, True),
            ({}, True),
            ({"issue": "42", "pr": 10}, True),
            ({"issue": "PROJ-123"}, True),
            ({"issue": "", "pr": None}, True),
            ({"pr": "10"}, True),
            ({"role": "developer"}, False),
            ({"issue": ["42"]}, False),
            ({"issue": 4.2}, False),
            ({"pr": True}, False),
            ({"issue": "42 extra"}, False),
            ({"issue": "42\nmore"}, False),
            ({"issue": "-42"}, False),
            ({"issue": "#42"}, True),
            ({"issue": "owner/repo#42"}, True),
            ({"issue": "owner/repo#42 more"}, False),
            ({"issue": "../x"}, False),
        ],
    )
    def test_args_are_allowlisted_scalars(self, args: dict | None, accepted: bool) -> None:
        from sova.dashboard.services.agent_context import command_args_error

        assert (command_args_error(args) is None) is accepted

    @pytest.mark.parametrize("command", [None, 42, ""])
    async def test_missing_or_non_string_command_is_unknown(
        self, project_agents: ProjectAgents, fake_runtime: FakeAgentRuntime, command: object
    ) -> None:
        """A DAG node without a ``command`` key passes None: refused as unknown, never a TypeError."""
        result = await start_command(command, {})  # type: ignore[arg-type]

        assert result["code"] == "unknown_command"
        assert fake_runtime.spawn_calls == []

    @pytest.mark.parametrize(
        "args",
        [
            # SupervisorEngine._execute_command_decision(): str(issue_number) and the resolved PR.
            {"issue": "42", "pr": 10},
            {"issue": "42", "pr": None},
            # claude-command handoff actions (handoff_to_user, handoff_to_reviewer, reviewer role).
            {"issue": "PROJ-123", "pr": 10},
            # A workflow (DAG) node's params from the role editor, or a node without params.
            {},
            None,
        ],
    )
    def test_every_callers_real_args_are_accepted(self, args: dict | None) -> None:
        from sova.dashboard.services.agent_context import command_args_error

        assert command_args_error(args) is None


# ---------------------------------------------------------------------------
# build_action_command
# ---------------------------------------------------------------------------


class TestBuildActionCommand:
    def test_uses_issue_key_over_pr(self) -> None:
        from sova.dashboard.services.handoff_service import build_action_command

        result = build_action_command(
            {
                "mode": "agent",
                "args": {"issue": "16", "pr": 67, "role": "developer"},
            }
        )
        assert result["issue"] == "16"
        assert result["pr_number"] == 67
        assert result["role"] == "developer"

    def test_falls_back_to_ticket_key(self) -> None:
        from sova.dashboard.services.handoff_service import build_action_command

        result = build_action_command(
            {
                "mode": "agent",
                "args": {"ticket": "42", "role": "reviewer"},
            }
        )
        assert result["issue"] == "42"

    def test_falls_back_to_pr_as_issue(self) -> None:
        from sova.dashboard.services.handoff_service import build_action_command

        result = build_action_command(
            {
                "mode": "agent",
                "args": {"pr": 99},
            }
        )
        assert result["issue"] == "99"

    def test_keeps_explicitly_empty_issue(self) -> None:
        """An issue-less reviewer handoff must not turn its PR number into an issue number."""
        from sova.dashboard.services.handoff_service import build_action_command

        result = build_action_command(
            {
                "mode": "agent",
                "args": {"issue": "", "pr": 60, "role": "developer"},
            }
        )
        assert result["issue"] == ""
        assert result["pr_number"] == 60

    @pytest.mark.parametrize(
        ("raw", "name"),
        [("/integrate-pr 99", "integrate-pr"), ("integrate-pr", "integrate-pr"), (None, ""), ("", ""), ("  /  ", "")],
    )
    def test_claude_command_is_reduced_to_its_name(self, raw: str | None, name: str) -> None:
        """A handoff action stores the slash form; start_command() takes only a catalog name."""
        from sova.dashboard.services.handoff_service import build_action_command

        result = build_action_command({"mode": "claude-command", "command": raw, "args": {"pr": 99}})
        assert result == {"type": "claude-command", "command": name, "args": {"pr": 99}}


# ---------------------------------------------------------------------------
# Auto-handoff memory pressure gate
# ---------------------------------------------------------------------------


class TestStartAgentMemoryGate:
    async def test_start_agent_blocked_by_memory(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_lifecycle import start_agent

        block_error = {"error": "Insufficient memory: 0.5 GB available"}

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle.check_memory_pressure",
                return_value=(block_error, None),
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._check_issue_conflict",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("sova.dashboard.services.agent_lifecycle._evict_completed_for_issue", new_callable=MagicMock),
        ):
            result = await start_agent("42", role="developer")

        assert result == block_error

    async def test_start_agent_bypasses_memory_with_force(self, tmp_path: Path, project_agents: ProjectAgents) -> None:
        """When force=True, memory pressure check is skipped."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_lifecycle import start_agent

        mock_check = MagicMock(return_value=({"error": "blocked"}, None))

        with (
            patch(
                "sova.dashboard.services.agent_validation.check_memory_pressure",
                mock_check,
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._check_issue_conflict",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("sova.dashboard.services.agent_lifecycle._evict_completed_for_issue", new_callable=MagicMock),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_branch_name",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=tmp_path,
            ),
            patch("sova.dashboard.services.agent_lifecycle._create_task_run", new_callable=AsyncMock, return_value=99),
            patch("sova.dashboard.services.agent_lifecycle._transition_to_in_progress", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.get_runtime") as mock_runtime,
        ):
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_runtime.return_value.spawn = AsyncMock(return_value=mock_process)
            mock_runtime.return_value.get_model_name.return_value = "test"

            await start_agent("42", role="developer", force=True)

        # memory_pressure should NOT have been called when force=True
        mock_check.assert_not_called()

    async def test_start_command_blocked_by_memory(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_lifecycle import start_command

        block_error = {"error": "Insufficient memory: 0.5 GB available"}

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle.check_memory_pressure",
                return_value=(block_error, None),
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._check_issue_conflict",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("sova.dashboard.services.agent_lifecycle._evict_completed_for_issue", new_callable=MagicMock),
        ):
            result = await start_command("review-pr", args={"issue": "42"})

        assert result == block_error


@pytest.mark.usefixtures("stub_collision_cleanup")
class TestSyncBranchWorktreeConflict:
    async def test_already_used_by_worktree_updates_ref(self, tmp_path: Path) -> None:
        """When branch is in active use by another worktree, fetch refspec should be used."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.branch import sync_branch

        checkout_fail = MagicMock()
        checkout_fail.success = False
        checkout_fail.stderr = "fatal: 'main' is already used by worktree at '/some/path'"

        fetch_ref_ok = MagicMock()
        fetch_ref_ok.success = True

        stash_ok = MagicMock()
        stash_ok.success = True
        stash_ok.stdout = "No local changes to save"

        with (
            patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_fetch,
            patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),
                stash_ok,
                checkout_fail,
                fetch_ref_ok,
            ]

            await sync_branch("main", cwd=tmp_path)

        mock_fetch.assert_awaited_once_with("git", "fetch", "origin", "main", cwd=tmp_path)
        assert mock_run.await_count == 4
        mock_run.assert_any_await("git", "fetch", "origin", "main:main", cwd=tmp_path)
        stash_pop_calls = [c for c in mock_run.call_args_list if c[0] == ("git", "stash", "pop")]
        assert len(stash_pop_calls) == 0, "No stash pop when nothing was stashed"

    async def test_already_used_by_worktree_pops_stash_when_dirty(self, tmp_path: Path) -> None:
        """When branch is in active use by another worktree and we stashed, stash must be popped."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.branch import sync_branch

        checkout_fail = MagicMock()
        checkout_fail.success = False
        checkout_fail.stderr = "fatal: 'main' is already used by worktree at '/some/path'"

        fetch_ref_ok = MagicMock()
        fetch_ref_ok.success = True

        stash_dirty = MagicMock()
        stash_dirty.success = True
        stash_dirty.stdout = "Saved working directory and index state WIP on feat: abc1234"

        pop_ok = MagicMock()
        pop_ok.success = True

        with (
            patch("sova.git.branch.run_checked", new_callable=AsyncMock),
            patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),
                stash_dirty,
                checkout_fail,
                fetch_ref_ok,
                pop_ok,
            ]

            await sync_branch("main", cwd=tmp_path)

        assert mock_run.await_count == 5
        mock_run.assert_any_await("git", "stash", "pop", cwd=tmp_path)

    async def test_already_used_by_worktree_ref_update_fails(self, tmp_path: Path) -> None:
        """When fetch refspec fails, should log warning but not raise."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.branch import sync_branch

        checkout_fail = MagicMock()
        checkout_fail.success = False
        checkout_fail.stderr = "fatal: 'main' is already used by worktree at '/some/path'"

        fetch_ref_fail = MagicMock()
        fetch_ref_fail.success = False
        fetch_ref_fail.stderr = "non-fast-forward update"

        stash_ok = MagicMock()
        stash_ok.success = True
        stash_ok.stdout = "No local changes to save"

        with (
            patch("sova.git.branch.run_checked", new_callable=AsyncMock),
            patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),
                stash_ok,
                checkout_fail,
                fetch_ref_fail,
            ]
            with capture_logs() as logs:
                await sync_branch("main", cwd=tmp_path)

        warnings = [e["event"] for e in logs if e["log_level"] == "warning"]
        assert warnings == ["git.sync_branch.active_worktree", "git.sync_branch.ref_update_failed"]
        assert mock_run.await_count == 4


# ---------------------------------------------------------------------------
# get_primary_worktree_root
# ---------------------------------------------------------------------------


class TestGetPrimaryWorktreeRoot:
    @pytest.mark.parametrize(
        ("stdout", "cwd"),
        [
            pytest.param(
                "/home/user/project/.git\n",
                "/home/user/project/.claude/worktrees/42",
                id="parent-of-absolute-common-dir",
            ),
            pytest.param(".git\n", "/home/user/project", id="cwd-for-relative-common-dir"),
        ],
    )
    async def test_resolves_root(self, stdout: str, cwd: str) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.worktree import get_primary_worktree_root

        result_mock = MagicMock()
        result_mock.success = True
        result_mock.stdout = stdout

        with patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=result_mock):
            root = await get_primary_worktree_root(cwd=Path(cwd))

        assert root == Path("/home/user/project")

    async def test_returns_cwd_on_git_failure(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.worktree import get_primary_worktree_root

        result_mock = MagicMock()
        result_mock.success = False

        with patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=result_mock):
            root = await get_primary_worktree_root(cwd=tmp_path / "not-a-repo")

        assert root == tmp_path / "not-a-repo"

    async def test_defaults_to_cwd(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.worktree import get_primary_worktree_root

        result_mock = MagicMock()
        result_mock.success = True
        result_mock.stdout = ".git\n"

        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=result_mock),
            patch("pathlib.Path.cwd", return_value=Path("/current/dir")),
        ):
            root = await get_primary_worktree_root()

        assert root == Path("/current/dir")


# ---------------------------------------------------------------------------
# Start agent model parameter
# ---------------------------------------------------------------------------


class TestStartAgentModelParam:
    """Tests for model parameter in POST /api/agents/start."""

    async def test_start_agent_passes_model(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "sova.dashboard.services.control_service.start_agent",
            new_callable=AsyncMock,
            return_value={"run_id": 1, "status": "started"},
        ) as mock_start:
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "42", "role": "developer", "model": "haiku"},
            )
            assert resp.status_code == 200
            mock_start.assert_called_once()
            _, kwargs = mock_start.call_args
            assert kwargs["model"] == "haiku"

    async def test_start_agent_model_defaults_to_none(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "sova.dashboard.services.control_service.start_agent",
            new_callable=AsyncMock,
            return_value={"run_id": 1, "status": "started"},
        ) as mock_start:
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "42"},
            )
            assert resp.status_code == 200
            mock_start.assert_called_once()
            _, kwargs = mock_start.call_args
            assert kwargs["model"] is None
