"""Tests for sova.supervisor.rebase."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.core.state import TaskStatus
from sova.db.models import Base, TaskRun
from sova.git.pr import PRLookupError
from sova.supervisor import rebase as rebase_module
from sova.supervisor.rebase import (
    PRE_PUSH_HOOK_TIMEOUT,
    REBASE_MAX_COMMITS,
    ROLE_SUPERVISOR_REBASE,
    RebaseOperations,
    _already_attempted,
    _create_rebase_run,
    _finalize_run,
    _find_running_rival,
    _get_pr_info,
    _run_pre_push_hook,
    _update_run_cost,
    _write_manual_handoff,
    attempt_auto_rebase,
    running_rebase_stale_after,
)


def _make_ops(**overrides: object) -> RebaseOperations:
    """Build a RebaseOperations with every collaborator mocked to a success shape.

    Replaces the old pattern of 8-13 stacked ``@patch("sova.supervisor.rebase.X")``
    decorators per test: a test overrides just the attributes relevant to the
    behavior under test on one object instead of patching each function.
    """
    defaults: dict[str, object] = dict(
        load_config=MagicMock(),
        get_pr_info=AsyncMock(),
        already_attempted=AsyncMock(return_value=False),
        create_worktree=AsyncMock(),
        find_worktree_by_branch=AsyncMock(return_value=None),
        check_worktree_active_agent=AsyncMock(return_value=None),
        cleanup_worktree=AsyncMock(),
        rebase_with_conflict_resolution=AsyncMock(),
        create_rebase_run=AsyncMock(),
        find_running_rival=AsyncMock(return_value=None),
        update_run_cost=AsyncMock(),
        finalize_run=AsyncMock(),
        run_pre_push_hook=AsyncMock(return_value={"passed": True, "output": ""}),
        run=AsyncMock(return_value=MagicMock(success=True, stdout="", stderr="")),
        write_manual_handoff=MagicMock(),
    )
    defaults.update(overrides)
    return RebaseOperations(**defaults)


@pytest.fixture(autouse=True)
def _clear_rebase_locks():
    """Every test function gets a fresh event loop, but `_rebase_locks` is a
    module-level dict that outlives any single test. Reusing an `asyncio.Lock`
    created on a prior test's (now-closed) loop breaks contention on the next
    test that reuses the same pr_number (55 in most fixtures here). Clearing
    before each test forces a fresh Lock bound to the current loop."""
    rebase_module._rebase_locks.clear()
    yield
    rebase_module._rebase_locks.clear()


@pytest.fixture(autouse=True)
def _no_real_gh_token_lookup():
    """`_get_pr_info()` resolves a per-user GH_TOKEN through `gh auth token`; keep tests off the real gh CLI."""
    with patch("sova.supervisor.rebase.resolve_gh_env", new_callable=AsyncMock, return_value=None):
        yield


def _mock_session_factory():
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=mock_session)
    return factory, mock_session


class TestAlreadyAttempted:
    @pytest.mark.asyncio
    async def test_no_runs_returns_false(self) -> None:
        factory, session = _mock_session_factory()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=mock_result)
        assert await _already_attempted(55, "abc123", factory) is False

    @pytest.mark.asyncio
    async def test_matching_sha_returns_true(self) -> None:
        factory, session = _mock_session_factory()
        mock_run = MagicMock()
        mock_run.handoff_json = {"head_sha": "abc123"}
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run]
        session.execute = AsyncMock(return_value=mock_result)
        assert await _already_attempted(55, "abc123", factory) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status", "blocked"), [(TaskStatus.FAILED, False), (TaskStatus.DONE, True)])
    async def test_explicit_retry_ignores_only_failed_attempts(self, status: str, blocked: bool) -> None:
        factory, session = _mock_session_factory()
        mock_run = MagicMock(status=status)
        mock_run.handoff_json = {"head_sha": "abc123", "attempted": True}
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run]
        session.execute = AsyncMock(return_value=mock_result)

        assert await _already_attempted(55, "abc123", factory) is True
        assert await _already_attempted(55, "abc123", factory, retry_failed=True) is blocked

    @pytest.mark.asyncio
    @pytest.mark.parametrize("retry_failed", [False, True])
    async def test_running_run_is_not_an_attempt(self, retry_failed: bool) -> None:
        """A running row has no head_sha yet; `_find_running_rival()` owns that case."""
        factory, session = _mock_session_factory()
        mock_run = MagicMock(status=TaskStatus.RUNNING)
        mock_run.handoff_json = None
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run]
        session.execute = AsyncMock(return_value=mock_result)

        assert await _already_attempted(55, "abc123", factory, retry_failed=retry_failed) is False

    @pytest.mark.asyncio
    async def test_different_sha_returns_false(self) -> None:
        factory, session = _mock_session_factory()
        mock_run = MagicMock()
        mock_run.handoff_json = {"head_sha": "old_sha"}
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run]
        session.execute = AsyncMock(return_value=mock_result)
        assert await _already_attempted(55, "new_sha", factory) is False

    @pytest.mark.asyncio
    async def test_safety_skip_does_not_count_as_attempted(self) -> None:
        """A busy/dirty/safety-check-failed skip must not block a later retry
        once the transient condition clears, since the rebase never ran."""
        factory, session = _mock_session_factory()
        mock_run = MagicMock()
        mock_run.handoff_json = {"head_sha": "abc123", "attempted": False}
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run]
        session.execute = AsyncMock(return_value=mock_result)
        assert await _already_attempted(55, "abc123", factory) is False

    @pytest.mark.asyncio
    async def test_matching_sha_without_attempted_key_returns_true(self) -> None:
        """Records written before the attempted flag existed default to True
        (a real attempt) rather than being silently reinterpreted as skips."""
        factory, session = _mock_session_factory()
        mock_run = MagicMock()
        mock_run.handoff_json = {"head_sha": "abc123"}
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run]
        session.execute = AsyncMock(return_value=mock_result)
        assert await _already_attempted(55, "abc123", factory) is True


class TestGetPrInfo:
    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.find_pr_for_issue_checked", new_callable=AsyncMock)
    async def test_no_pr_returns_none(self, mock_find: AsyncMock) -> None:
        mock_find.return_value = None
        result = await _get_pr_info(42, "owner/repo", "user")
        assert result is None

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.find_pr_for_issue_checked", new_callable=AsyncMock)
    async def test_successful_lookup(self, mock_find: AsyncMock, mock_run: AsyncMock) -> None:
        import json

        mock_pr = MagicMock()
        mock_pr.number = 55
        mock_find.return_value = mock_pr
        mock_run.return_value = MagicMock(
            success=True,
            stdout=json.dumps({"headRefOid": "abc123def", "headRefName": "feat/my-feature", "baseRefName": "main"}),
        )
        result = await _get_pr_info(42, "owner/repo", "user")
        assert result is not None
        assert result["number"] == 55
        assert result["branch"] == "feat/my-feature"
        assert result["head_sha"] == "abc123def"


class TestRunPrePushHook:
    @pytest.mark.asyncio
    async def test_no_hook_returns_passed(self, tmp_path: Path) -> None:
        result = await _run_pre_push_hook(tmp_path)
        assert result["passed"] is True

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    async def test_hook_failure(self, mock_run: AsyncMock, tmp_path: Path) -> None:
        hook_dir = tmp_path / ".githooks"
        hook_dir.mkdir()
        hook_file = hook_dir / "pre-push"
        hook_file.write_text("#!/bin/bash")
        hook_file.chmod(0o755)
        mock_run.return_value = MagicMock(success=False, stdout="lint errors", stderr="")
        result = await _run_pre_push_hook(tmp_path)
        assert result["passed"] is False
        assert "lint errors" in result["output"]


class TestWriteManualHandoff:
    @patch("sova.supervisor.rebase.write_handoff_file")
    def test_writes_handoff_with_action(self, mock_write: MagicMock, tmp_path: Path) -> None:
        _write_manual_handoff(tmp_path, 42, 55, "feat/branch", "Rebase failed")
        mock_write.assert_called_once()
        handoff = mock_write.call_args[0][1]
        assert handoff.source == "supervisor:rebase"
        assert handoff.status == "failed"
        assert handoff.pr_number == 55
        assert len(handoff.next_actions) == 1
        assert handoff.next_actions[0].id == "manual_rebase"

    @patch("sova.supervisor.rebase.write_handoff_file")
    def test_includes_validation_error(self, mock_write: MagicMock, tmp_path: Path) -> None:
        _write_manual_handoff(tmp_path, 42, 55, "feat/branch", "Failed", validation_error="lint errors")
        handoff = mock_write.call_args[0][1]
        assert handoff.details["validation_error"] == "lint errors"


class TestAttemptAutoRebase:
    @pytest.mark.asyncio
    async def test_no_github_repo_skips(self) -> None:
        ops = _make_ops()
        ops.load_config.return_value.github_repo = ""
        factory, _ = _mock_session_factory()
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_no_pr_skips(self) -> None:
        ops = _make_ops()
        ops.load_config.return_value.github_repo = "owner/repo"
        ops.load_config.return_value.github_user = "user"
        ops.get_pr_info.return_value = None
        factory, _ = _mock_session_factory()
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_already_attempted_skips(self) -> None:
        ops = _make_ops()
        ops.load_config.return_value.github_repo = "owner/repo"
        ops.load_config.return_value.github_user = "user"
        ops.load_config.return_value.base_branch = "main"
        ops.get_pr_info.return_value = {"number": 55, "branch": "feat/x", "head_sha": "abc123", "base_branch": "main"}
        ops.already_attempted.return_value = True
        factory, _ = _mock_session_factory()
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "skipped"
        assert "Already attempted" in result["reason"]

    @pytest.mark.asyncio
    async def test_running_rival_skips_with_in_progress_reason(self) -> None:
        ops = _make_ops()
        ops.load_config.return_value.github_repo = "owner/repo"
        ops.load_config.return_value.github_user = "user"
        ops.load_config.return_value.base_branch = "main"
        ops.get_pr_info.return_value = {"number": 55, "branch": "feat/x", "head_sha": "abc123", "base_branch": "main"}
        ops.create_rebase_run.return_value = MagicMock(id=2)
        ops.find_running_rival.return_value = MagicMock(started_at=datetime(2026, 11, 19, 9, 30, tzinfo=timezone.utc))
        factory, _ = _mock_session_factory()
        result = await attempt_auto_rebase(42, Path("/fake"), factory, retry_failed=True, ops=ops)
        assert result["status"] == "skipped"
        assert result["reason"] == "Rebase for PR #55 is already running (started 09:30 UTC)"
        assert "Already attempted" not in result["reason"]
        ops.finalize_run.assert_awaited_once()
        assert ops.finalize_run.await_args.args[1] == TaskStatus.FAILED
        assert ops.finalize_run.await_args.kwargs["attempted"] is False
        ops.create_worktree.assert_not_called()
        ops.rebase_with_conflict_resolution.assert_not_called()

    @pytest.mark.asyncio
    async def test_attempt_finalized_between_check_and_claim_skips(self) -> None:
        """A rival that finishes after the first check but before the claim is caught by the re-check."""
        ops = _make_ops()
        ops.load_config.return_value.github_repo = "owner/repo"
        ops.load_config.return_value.github_user = "user"
        ops.load_config.return_value.base_branch = "main"
        ops.get_pr_info.return_value = {"number": 55, "branch": "feat/x", "head_sha": "abc123", "base_branch": "main"}
        ops.already_attempted.side_effect = [False, True]
        ops.create_rebase_run.return_value = MagicMock(id=2)
        ops.find_running_rival.return_value = None
        factory, _ = _mock_session_factory()
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "skipped"
        assert "Already attempted" in result["reason"]
        assert ops.finalize_run.await_args.kwargs["attempted"] is False
        ops.create_worktree.assert_not_called()
        ops.rebase_with_conflict_resolution.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_exception_returns_dict(self) -> None:
        """Setup exceptions (before task_run created) return failed dict, not propagate."""
        ops = _make_ops()
        ops.load_config.return_value.github_repo = "owner/repo"
        ops.load_config.return_value.github_user = "user"
        ops.load_config.return_value.base_branch = "main"
        ops.get_pr_info.return_value = {"number": 55, "branch": "feat/x", "head_sha": "abc123", "base_branch": "main"}
        ops.already_attempted.side_effect = RuntimeError("db crash")
        factory, _ = _mock_session_factory()
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "failed"
        assert "db crash" in result["error"]

    def test_default_ops_bind_the_real_collaborators(self) -> None:
        ops = rebase_module._default_ops()
        assert ops.get_pr_info is _get_pr_info
        assert ops.finalize_run is _finalize_run
        assert ops.write_manual_handoff is _write_manual_handoff
        assert ops.cleanup_worktree is rebase_module.cleanup_worktree

    @pytest.mark.asyncio
    async def test_no_ops_uses_module_collaborators_at_call_time(self) -> None:
        """Callers (dashboard, progression daemon) pass no `ops`; the defaults must
        resolve when called, so patching a module-level name still takes effect."""
        factory, _ = _mock_session_factory()
        with patch.object(rebase_module, "load_config") as mock_load:
            mock_load.return_value.github_repo = ""
            result = await attempt_auto_rebase(42, Path("/fake"), factory)
        assert result == {"status": "skipped", "reason": "No github_repo configured"}
        mock_load.assert_called_once_with(Path("/fake"))


class TestRoleSupervisorRebaseConstant:
    def test_constant_value(self) -> None:
        assert ROLE_SUPERVISOR_REBASE == "supervisor:rebase"


class TestGetPrInfoEdgeCases:
    @staticmethod
    def _pr(number: int = 55) -> MagicMock:
        pr = MagicMock()
        pr.number = number
        return pr

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.find_pr_for_issue_checked", new_callable=AsyncMock)
    async def test_gh_view_fails_raises_with_stderr(self, mock_find, mock_run) -> None:
        mock_find.return_value = self._pr()
        mock_run.return_value = MagicMock(success=False, stdout="", stderr="Could not resolve to a Repository")
        with pytest.raises(PRLookupError, match="Could not resolve to a Repository"):
            await _get_pr_info(42, "owner/repo", "user")

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.find_pr_for_issue_checked", new_callable=AsyncMock)
    async def test_invalid_json_raises(self, mock_find, mock_run) -> None:
        mock_find.return_value = self._pr()
        mock_run.return_value = MagicMock(success=True, stdout="not valid json")
        with pytest.raises(PRLookupError, match="unparseable"):
            await _get_pr_info(42, "owner/repo", "user")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stdout",
        [
            "[]",
            "null",
            "{}",
            '{"headRefName": "feat/x"}',
            '{"headRefOid": "abc123"}',
            '{"headRefName": "", "headRefOid": "abc123"}',
            '{"headRefName": "feat/x", "headRefOid": ""}',
        ],
    )
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.find_pr_for_issue_checked", new_callable=AsyncMock)
    async def test_missing_head_ref_raises(self, mock_find, mock_run, stdout: str) -> None:
        mock_find.return_value = self._pr()
        mock_run.return_value = MagicMock(success=True, stdout=stdout)
        with pytest.raises(PRLookupError, match="no head ref"):
            await _get_pr_info(42, "owner/repo", "user")

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.find_pr_for_issue_checked", new_callable=AsyncMock)
    async def test_search_lookup_error_propagates(self, mock_find) -> None:
        mock_find.side_effect = PRLookupError("all searches failed")
        with pytest.raises(PRLookupError, match="all searches failed"):
            await _get_pr_info(42, "owner/repo", "user")

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.resolve_gh_env", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.find_pr_for_issue_checked", new_callable=AsyncMock)
    async def test_gh_view_runs_with_per_user_env(self, mock_find, mock_run, mock_env) -> None:
        mock_find.return_value = self._pr()
        mock_env.return_value = {"GH_TOKEN": "tok"}
        mock_run.return_value = MagicMock(
            success=True, stdout='{"headRefOid": "abc123", "headRefName": "feat/x", "baseRefName": "main"}'
        )
        await _get_pr_info(42, "owner/repo", "dsova06")
        mock_env.assert_awaited_once_with("dsova06")
        assert mock_run.call_args.kwargs["env"] == {"GH_TOKEN": "tok"}


class TestAttemptAutoRebasePrLookup:
    @pytest.mark.asyncio
    async def test_lookup_error_returns_failed_and_warns(self) -> None:
        factory, _ = _mock_session_factory()
        ops = _make_ops(get_pr_info=AsyncMock(side_effect=PRLookupError("gh pr view failed: boom")))
        with patch.object(rebase_module, "log") as mock_log:
            result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "failed"
        assert result["pr_number"] is None
        assert "gh pr view failed: boom" in result["error"]
        mock_log.warning.assert_called_once()
        mock_log.exception.assert_not_called()
        ops.create_rebase_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirmed_empty_search_is_skipped_without_warning(self) -> None:
        factory, _ = _mock_session_factory()
        ops = _make_ops(get_pr_info=AsyncMock(return_value=None))
        with patch.object(rebase_module, "log") as mock_log:
            result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result == {"status": "skipped", "reason": "No open PR found for issue #42"}
        mock_log.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_lookup_setup_error_returns_failed_and_logs_exception(self) -> None:
        """A non-PRLookupError raised during setup takes the generic branch, not the lookup one."""
        factory, _ = _mock_session_factory()
        ops = _make_ops(get_pr_info=AsyncMock(side_effect=RuntimeError("config exploded")))
        with patch.object(rebase_module, "log") as mock_log:
            result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result == {"status": "failed", "pr_number": None, "error": "config exploded"}
        mock_log.exception.assert_called_once()
        mock_log.warning.assert_not_called()
        ops.create_rebase_run.assert_not_called()


class TestCreateRebaseRun:
    @pytest.mark.asyncio
    async def test_creates_task_run(self) -> None:
        factory, session = _mock_session_factory()
        mock_run = MagicMock()
        mock_run.id = 1
        mock_run.role = ROLE_SUPERVISOR_REBASE
        mock_run.status = "running"
        session.refresh = AsyncMock(return_value=None)
        session.add = MagicMock()
        with patch("sova.supervisor.rebase.TaskRun", return_value=mock_run):
            result = await _create_rebase_run(42, 55, "feat/x", factory)
        assert result.role == ROLE_SUPERVISOR_REBASE
        assert result.status == "running"
        session.commit.assert_awaited_once()


class TestUpdateRunCost:
    @pytest.mark.asyncio
    async def test_updates_cost(self) -> None:
        factory, session = _mock_session_factory()
        mock_run = MagicMock()
        mock_run.total_cost_usd = Decimal("0.00")
        session.get = AsyncMock(return_value=mock_run)
        await _update_run_cost(1, Decimal("0.05"), factory)
        assert mock_run.total_cost_usd == Decimal("0.05")
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_run_found(self) -> None:
        factory, session = _mock_session_factory()
        session.get = AsyncMock(return_value=None)
        await _update_run_cost(999, Decimal("0.05"), factory)
        session.commit.assert_not_awaited()


class TestFinalizeRun:
    @pytest.mark.asyncio
    async def test_finalizes_success(self) -> None:
        factory, session = _mock_session_factory()
        mock_run = MagicMock()
        session.get = AsyncMock(return_value=mock_run)
        await _finalize_run(1, "done", None, "abc123", factory)
        assert mock_run.status == "done"
        assert mock_run.error_message is None
        assert mock_run.handoff_json == dict(head_sha="abc123", attempted=True)
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_finalizes_failure(self) -> None:
        factory, session = _mock_session_factory()
        mock_run = MagicMock()
        session.get = AsyncMock(return_value=mock_run)
        await _finalize_run(1, "failed", "something broke", "abc123", factory)
        assert mock_run.status == "failed"
        assert mock_run.error_message == "something broke"
        assert mock_run.handoff_json == dict(head_sha="abc123", attempted=True)

    @pytest.mark.asyncio
    async def test_finalizes_safety_skip_as_not_attempted(self) -> None:
        factory, session = _mock_session_factory()
        mock_run = MagicMock()
        session.get = AsyncMock(return_value=mock_run)
        await _finalize_run(1, "failed", "worktree busy", "abc123", factory, attempted=False)
        assert mock_run.handoff_json == dict(head_sha="abc123", attempted=False)

    @pytest.mark.asyncio
    async def test_no_run_found(self) -> None:
        factory, session = _mock_session_factory()
        session.get = AsyncMock(return_value=None)
        await _finalize_run(999, "done", None, "abc123", factory)
        session.commit.assert_not_awaited()


class TestAttemptAutoRebaseExecution:
    def _setup_mocks(self):
        factory, session = _mock_session_factory()
        mock_task_run = MagicMock()
        mock_task_run.id = 1
        pr_info = dict(number=55, branch="feat/x", head_sha="abc123def", base_branch="main")
        return factory, session, mock_task_run, pr_info

    def _cfg_mock(self, ops: RebaseOperations) -> None:
        ops.load_config.return_value.github_repo = "owner/repo"
        ops.load_config.return_value.github_user = "user"
        ops.load_config.return_value.base_branch = "main"
        ops.load_config.return_value.worktree.copy_files = []

    @pytest.mark.asyncio
    async def test_success_path(self, tmp_path: Path) -> None:
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        wt_info = MagicMock()
        wt_info.path = tmp_path / "wt"
        ops.create_worktree.return_value = wt_info
        ops.create_rebase_run.return_value = mock_task_run
        ops.rebase_with_conflict_resolution.return_value = (
            MagicMock(success=True, conflicts_resolved=2),
            Decimal("0.05"),
        )
        result = await attempt_auto_rebase(42, Path("/fake"), factory, retry_failed=True, ops=ops)
        assert result["status"] == "success"
        assert result["pr_number"] == 55
        assert ops.already_attempted.await_args.kwargs["retry_failed"] is True
        assert ops.rebase_with_conflict_resolution.await_args.kwargs["project_dir"] == Path("/fake")
        ops.cleanup_worktree.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_default_call_does_not_retry_failed_attempts(self, tmp_path: Path) -> None:
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = True
        await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert ops.already_attempted.await_args.kwargs["retry_failed"] is False

    def test_default_ops_accept_the_keywords_the_orchestration_passes(self) -> None:
        import inspect

        ops = rebase_module._default_ops()
        assert "project_dir" in inspect.signature(ops.rebase_with_conflict_resolution).parameters
        assert "retry_failed" in inspect.signature(ops.already_attempted).parameters

    @pytest.mark.asyncio
    async def test_empty_pr_base_branch_falls_back_to_config(self, tmp_path: Path) -> None:
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.load_config.return_value.base_branch = "develop"
        ops.get_pr_info.return_value = {**pr_info, "base_branch": ""}
        wt_info = MagicMock()
        wt_info.path = tmp_path / "wt"
        ops.create_worktree.return_value = wt_info
        ops.create_rebase_run.return_value = mock_task_run
        ops.rebase_with_conflict_resolution.return_value = (
            MagicMock(success=True, conflicts_resolved=0),
            Decimal("0"),
        )
        await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert ops.rebase_with_conflict_resolution.call_args.args[0] == "develop"

    @pytest.mark.asyncio
    async def test_rebase_failure_writes_handoff(self, tmp_path: Path) -> None:
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        wt_info = MagicMock()
        wt_info.path = tmp_path / "wt"
        ops.create_worktree.return_value = wt_info
        ops.create_rebase_run.return_value = mock_task_run
        ops.rebase_with_conflict_resolution.return_value = (MagicMock(success=False, error="conflict"), Decimal("0.03"))
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "failed"
        ops.cleanup_worktree.assert_awaited_once()
        ops.write_manual_handoff.assert_called_once()

    @pytest.mark.asyncio
    async def test_reuses_existing_worktree_for_branch(self) -> None:
        """When the branch is already checked out in another worktree (e.g. the
        developer's persistent worktree), reuse it instead of trying to create
        a second, colliding worktree, and never clean up a worktree we don't own."""
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        existing_path = Path("/fake/.claude/worktrees/991")
        ops.find_worktree_by_branch.return_value = existing_path
        ops.check_worktree_active_agent.return_value = None
        ops.create_rebase_run.return_value = mock_task_run
        ops.rebase_with_conflict_resolution.return_value = (
            MagicMock(success=True, conflicts_resolved=1),
            Decimal("0.01"),
        )
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "success"
        ops.create_worktree.assert_not_awaited()
        ops.cleanup_worktree.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_existing_worktree_busy(self) -> None:
        """A worktree with a live agent PID must never be touched by auto-rebase."""
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        ops.find_worktree_by_branch.return_value = Path("/fake/.claude/worktrees/991")
        ops.check_worktree_active_agent.return_value = 12345
        ops.create_rebase_run.return_value = mock_task_run
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "skipped"
        assert "actively in use" in result["reason"]

    @pytest.mark.asyncio
    async def test_fails_when_worktree_safety_check_errors(self) -> None:
        """A DB failure while checking worktree safety fails closed rather than reusing blindly."""
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        ops.find_worktree_by_branch.return_value = Path("/fake/.claude/worktrees/991")
        ops.check_worktree_active_agent.side_effect = RuntimeError("DB query failed")
        ops.create_rebase_run.return_value = mock_task_run
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "failed"
        assert "DB query failed" in result["error"]

    @pytest.mark.asyncio
    async def test_skips_when_branch_checked_out_in_main_worktree(self) -> None:
        """A branch checked out in the main project checkout (no dedicated worktree)
        must never be reused or collided into a second create_worktree() call."""
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        ops.find_worktree_by_branch.return_value = Path("/fake")
        ops.create_rebase_run.return_value = mock_task_run
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "skipped"
        assert "main project checkout" in result["reason"]
        ops.create_worktree.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_existing_worktree_is_dirty(self) -> None:
        """A worktree with uncommitted changes must not be reused: the rebase
        helper stashes and pops around the rebase, risking a stranded stash."""
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        ops.find_worktree_by_branch.return_value = Path("/fake/.claude/worktrees/991")
        ops.check_worktree_active_agent.return_value = None
        ops.create_rebase_run.return_value = mock_task_run
        ops.run.return_value = MagicMock(success=True, stdout="M some_file.py\n", stderr="")
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "skipped"
        assert "uncommitted changes" in result["reason"]

    @pytest.mark.asyncio
    async def test_fails_closed_when_worktree_status_check_errors(self) -> None:
        """An unreadable git status must fail closed (skip reuse), not assume clean."""
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        ops.find_worktree_by_branch.return_value = Path("/fake/.claude/worktrees/991")
        ops.check_worktree_active_agent.return_value = None
        ops.create_rebase_run.return_value = mock_task_run
        ops.run.return_value = MagicMock(success=False, stdout="", stderr="fatal: not a git repository")
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "skipped"
        assert "uncommitted changes" in result["reason"]

    @pytest.mark.asyncio
    async def test_branch_worktree_lookup_failure_falls_back_to_create(self, tmp_path: Path) -> None:
        """A failed find_worktree_by_branch lookup (e.g. git error) falls back to creating a dedicated worktree."""
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        ops.find_worktree_by_branch.side_effect = OSError("git not found")
        wt_info = MagicMock()
        wt_info.path = tmp_path / "wt"
        ops.create_worktree.return_value = wt_info
        ops.create_rebase_run.return_value = mock_task_run
        ops.rebase_with_conflict_resolution.return_value = (
            MagicMock(success=True, conflicts_resolved=1),
            Decimal("0.01"),
        )
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "success"
        ops.create_worktree.assert_awaited_once()
        ops.cleanup_worktree.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pre_push_hook_failure(self, tmp_path: Path) -> None:
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        wt_info = MagicMock()
        wt_info.path = tmp_path / "wt"
        ops.create_worktree.return_value = wt_info
        ops.create_rebase_run.return_value = mock_task_run
        ops.rebase_with_conflict_resolution.return_value = (
            MagicMock(success=True, conflicts_resolved=1),
            Decimal("0.02"),
        )
        ops.run_pre_push_hook.return_value = {"passed": False, "output": "ruff check failed"}
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "failed"
        assert "Pre-push hook failed" in result["error"]

    @pytest.mark.asyncio
    async def test_push_stale_info_skipped(self, tmp_path: Path) -> None:
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        wt_info = MagicMock()
        wt_info.path = tmp_path / "wt"
        ops.create_worktree.return_value = wt_info
        ops.create_rebase_run.return_value = mock_task_run
        ops.rebase_with_conflict_resolution.return_value = (
            MagicMock(success=True, conflicts_resolved=1),
            Decimal("0.01"),
        )
        ops.run.return_value = MagicMock(success=False, stdout="", stderr="stale info detected")
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_push_other_failure(self, tmp_path: Path) -> None:
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        wt_info = MagicMock()
        wt_info.path = tmp_path / "wt"
        ops.create_worktree.return_value = wt_info
        ops.create_rebase_run.return_value = mock_task_run
        ops.rebase_with_conflict_resolution.return_value = (
            MagicMock(success=True, conflicts_resolved=1),
            Decimal("0.01"),
        )
        ops.run.return_value = MagicMock(success=False, stdout="", stderr="network timeout")
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "failed"
        assert "network timeout" in result["error"]

    @pytest.mark.asyncio
    async def test_unexpected_exception(self, tmp_path: Path) -> None:
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        wt_info = MagicMock()
        wt_info.path = tmp_path / "wt"
        ops.create_worktree.return_value = wt_info
        ops.create_rebase_run.return_value = mock_task_run
        ops.rebase_with_conflict_resolution.side_effect = RuntimeError("unexpected crash")
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "failed"
        assert "unexpected crash" in result["error"]
        ops.finalize_run.assert_awaited_once()
        ops.cleanup_worktree.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancelled_rebase_finalizes_run_and_reraises(self, tmp_path: Path) -> None:
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        wt_info = MagicMock()
        wt_info.path = tmp_path / "wt"
        ops.create_worktree.return_value = wt_info
        ops.create_rebase_run.return_value = mock_task_run
        ops.rebase_with_conflict_resolution.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        ops.finalize_run.assert_awaited_once_with(1, "failed", "Rebase cancelled", "abc123def", factory)
        ops.cleanup_worktree.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_worktree_cleanup_failure_non_fatal(self, tmp_path: Path) -> None:
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        wt_info = MagicMock()
        wt_info.path = tmp_path / "wt"
        ops.create_worktree.return_value = wt_info
        ops.create_rebase_run.return_value = mock_task_run
        ops.rebase_with_conflict_resolution.return_value = (
            MagicMock(success=True, conflicts_resolved=1),
            Decimal("0.01"),
        )
        ops.cleanup_worktree.side_effect = OSError("cleanup failed")
        result = await attempt_auto_rebase(42, Path("/fake"), factory, ops=ops)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_concurrent_calls_for_same_pr_are_serialized(self, tmp_path: Path) -> None:
        """Two concurrent attempt_auto_rebase() calls for the same PR (e.g. a
        dashboard click racing a daemon poll cycle) must not both reach the
        rebase step at once and risk reusing the same worktree concurrently."""
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        wt_info = MagicMock()
        wt_info.path = tmp_path / "wt"
        ops.create_worktree.return_value = wt_info
        ops.create_rebase_run.return_value = mock_task_run

        concurrent = 0
        max_concurrent = 0

        async def _fake_rebase(*args, **kwargs):
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1
            return MagicMock(success=True, conflicts_resolved=0), Decimal("0.00")

        ops.rebase_with_conflict_resolution.side_effect = _fake_rebase

        results = await asyncio.gather(
            attempt_auto_rebase(42, Path("/fake"), factory, ops=ops),
            attempt_auto_rebase(42, Path("/fake"), factory, ops=ops),
        )
        assert max_concurrent == 1
        assert all(r["status"] == "success" for r in results)

    @pytest.mark.asyncio
    async def test_failed_run_cleans_up_worktree_before_releasing_lock(self, tmp_path: Path) -> None:
        """When the first call's rebase fails, its owned worktree must be
        cleaned up before a same-PR call waiting on the lock proceeds, or the
        waiting call could start reusing/recreating the worktree while the
        first call is still tearing it down."""
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        ops = _make_ops()
        self._cfg_mock(ops)
        ops.get_pr_info.return_value = pr_info
        ops.already_attempted.return_value = False
        ops.find_worktree_by_branch.return_value = None
        ops.create_rebase_run.return_value = mock_task_run

        events: list[str] = []
        wt_info = MagicMock()
        wt_info.path = tmp_path / "wt"

        async def _fake_create_worktree(*args, **kwargs):
            events.append("create_worktree")
            return wt_info

        ops.create_worktree.side_effect = _fake_create_worktree

        async def _fake_cleanup(*args, **kwargs):
            events.append("cleanup")

        ops.cleanup_worktree.side_effect = _fake_cleanup

        call_count = 0

        async def _fake_rebase(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.sleep(0.01)
                raise RuntimeError("rebase blew up")
            return MagicMock(success=True, conflicts_resolved=0), Decimal("0.00")

        ops.rebase_with_conflict_resolution.side_effect = _fake_rebase

        results = await asyncio.gather(
            attempt_auto_rebase(42, Path("/fake"), factory, ops=ops),
            attempt_auto_rebase(42, Path("/fake"), factory, ops=ops),
        )
        assert results[0]["status"] == "failed"
        assert results[1]["status"] == "success"
        # Both calls create their own worktree (neither reuses one) and both
        # clean up in their own finally block; the key property is ordering:
        # the first call's cleanup must complete before the second call's
        # create_worktree, proving the lock wasn't released until cleanup
        # finished.
        assert events == ["create_worktree", "cleanup", "create_worktree", "cleanup"]


class TestRunningRebaseStaleAfter:
    def test_scales_with_the_configured_timeout(self) -> None:
        short, long = MagicMock(), MagicMock()
        short.llm.cli_timeout = 300
        long.llm.cli_timeout = 1800
        assert running_rebase_stale_after(long) > running_rebase_stale_after(short)
        assert running_rebase_stale_after(short) > timedelta(seconds=REBASE_MAX_COMMITS * 300 + PRE_PUSH_HOOK_TIMEOUT)

    @pytest.mark.parametrize("value", [0, None, "bad"])
    def test_unusable_timeout_falls_back_to_the_default(self, value: object) -> None:
        cfg = MagicMock()
        cfg.llm.cli_timeout = value
        default = MagicMock()
        default.llm.cli_timeout = rebase_module.DEFAULT_RESOLUTION_TIMEOUT
        assert running_rebase_stale_after(cfg) == running_rebase_stale_after(default)


@pytest.fixture
async def real_session_factory(tmp_path: Path):
    """A file-backed SQLite session factory, so two callers hold separate connections as two processes would."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rebase.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _real_ops(pr_info: dict, **overrides: object) -> RebaseOperations:
    """Mocked collaborators except the DB-backed ones, which run against the real session factory."""
    ops = _make_ops(
        already_attempted=_already_attempted,
        create_rebase_run=_create_rebase_run,
        find_running_rival=_find_running_rival,
        update_run_cost=_update_run_cost,
        finalize_run=_finalize_run,
        **overrides,
    )
    ops.load_config.return_value.github_repo = "owner/repo"
    ops.load_config.return_value.github_user = "user"
    ops.load_config.return_value.base_branch = "main"
    ops.load_config.return_value.worktree.copy_files = []
    ops.get_pr_info.return_value = pr_info
    return ops


_PR_INFO = {"number": 55, "branch": "feat/x", "head_sha": "abc123def", "base_branch": "main"}


class TestRebaseWithRealDatabase:
    @pytest.fixture
    def worktree(self, tmp_path: Path) -> MagicMock:
        info = MagicMock()
        info.path = tmp_path / "wt"
        return info

    @pytest.mark.asyncio
    async def test_failed_rebase_blocks_scheduler_but_not_manual_retry(
        self, real_session_factory, worktree: MagicMock
    ) -> None:
        failing = MagicMock(success=False, error="unresolved conflict", conflicts_resolved=0)
        ops = _real_ops(
            _PR_INFO,
            create_worktree=AsyncMock(return_value=worktree),
            rebase_with_conflict_resolution=AsyncMock(return_value=(failing, Decimal("0.01"))),
        )

        first = await attempt_auto_rebase(42, Path("/fake"), real_session_factory, ops=ops)
        assert first["status"] == "failed"
        async with real_session_factory() as session:
            row = (await session.execute(TaskRun.__table__.select())).one()
        assert row.status == TaskStatus.FAILED
        assert row.handoff_json == {"head_sha": "abc123def", "attempted": True}

        scheduler = await attempt_auto_rebase(42, Path("/fake"), real_session_factory, retry_failed=False, ops=ops)
        assert scheduler["status"] == "skipped"
        assert "Already attempted" in scheduler["reason"]
        assert ops.rebase_with_conflict_resolution.await_count == 1

        manual = await attempt_auto_rebase(42, Path("/fake"), real_session_factory, retry_failed=True, ops=ops)
        assert manual["status"] == "failed"
        assert ops.rebase_with_conflict_resolution.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param({"hook": {"passed": False, "output": "lint failed"}}, id="pre_push_failure"),
            pytest.param({"push": MagicMock(success=False, stderr="denied")}, id="push_failure"),
            pytest.param({"raises": RuntimeError("boom")}, id="exception"),
        ],
    )
    async def test_every_failure_path_persists_a_retryable_failed_run(
        self, real_session_factory, worktree: MagicMock, failure: dict
    ) -> None:
        ok = MagicMock(success=True, conflicts_resolved=1)
        rebase = AsyncMock(return_value=(ok, Decimal("0")))
        if "raises" in failure:
            rebase = AsyncMock(side_effect=failure["raises"])
        ops = _real_ops(
            _PR_INFO,
            create_worktree=AsyncMock(return_value=worktree),
            rebase_with_conflict_resolution=rebase,
            run_pre_push_hook=AsyncMock(return_value=failure.get("hook", {"passed": True, "output": ""})),
        )
        ops.run = AsyncMock(
            side_effect=lambda *a, **k: (
                failure.get("push", MagicMock(success=True, stdout="", stderr=""))
                if "push" in a
                else MagicMock(success=True, stdout="", stderr="")
            )
        )

        result = await attempt_auto_rebase(42, Path("/fake"), real_session_factory, ops=ops)
        assert result["status"] == "failed"
        async with real_session_factory() as session:
            row = (await session.execute(TaskRun.__table__.select())).one()
        assert row.status == TaskStatus.FAILED
        assert row.handoff_json["attempted"] is True

        blocked = await attempt_auto_rebase(42, Path("/fake"), real_session_factory, retry_failed=False, ops=ops)
        assert blocked["status"] == "skipped"
        retried = await attempt_auto_rebase(42, Path("/fake"), real_session_factory, retry_failed=True, ops=ops)
        assert "Already attempted" not in retried.get("reason", "")

    @pytest.mark.asyncio
    async def test_two_concurrent_callers_with_separate_locks_run_one_rebase(
        self, real_session_factory, worktree: MagicMock
    ) -> None:
        """The daemon and the dashboard each hold their own lock registry: only the DB claim can stop the second."""
        gate = asyncio.Event()

        async def slow_rebase(*args: object, **kwargs: object):
            await gate.wait()
            return MagicMock(success=True, conflicts_resolved=0), Decimal("0")

        def make_ops() -> RebaseOperations:
            return _real_ops(
                _PR_INFO,
                create_worktree=AsyncMock(return_value=worktree),
                rebase_with_conflict_resolution=AsyncMock(side_effect=slow_rebase),
            )

        ops_a, ops_b = make_ops(), make_ops()
        # Separate lock registries: each call sees its own lock, as two processes would.
        locks = {"a": asyncio.Lock(), "b": asyncio.Lock()}
        callers = iter(["a", "b"])

        def fake_lock(_pr: int) -> asyncio.Lock:
            return locks[next(callers)]

        with patch.object(rebase_module, "_get_rebase_lock", side_effect=fake_lock):
            task_a = asyncio.create_task(
                attempt_auto_rebase(42, Path("/fake"), real_session_factory, retry_failed=True, ops=ops_a)
            )
            task_b = asyncio.create_task(
                attempt_auto_rebase(42, Path("/fake"), real_session_factory, retry_failed=True, ops=ops_b)
            )
            # Let both reach the claim before either rebase may finish.
            for _ in range(200):
                await asyncio.sleep(0.01)
                async with real_session_factory() as session:
                    rows = (await session.execute(TaskRun.__table__.select())).all()
                if len(rows) == 2 and any(r.status != TaskStatus.RUNNING for r in rows):
                    break
            gate.set()
            results = await asyncio.gather(task_a, task_b)

        statuses = sorted(r["status"] for r in results)
        assert statuses == ["skipped", "success"]
        skipped = next(r for r in results if r["status"] == "skipped")
        assert "already running" in skipped["reason"]
        assert (
            ops_a.rebase_with_conflict_resolution.await_count + ops_b.rebase_with_conflict_resolution.await_count == 1
        )

    @pytest.mark.asyncio
    async def test_stale_running_row_does_not_block(self, real_session_factory) -> None:
        old = datetime.now(timezone.utc) - running_rebase_stale_after(MagicMock()) - timedelta(minutes=1)
        async with real_session_factory() as session:
            session.add(TaskRun(role=ROLE_SUPERVISOR_REBASE, pr_number=55, status=TaskStatus.RUNNING, started_at=old))
            await session.commit()
        newer = await _create_rebase_run(42, 55, "feat/x", real_session_factory)
        assert (
            await _find_running_rival(
                newer.id, 55, real_session_factory, stale_after=running_rebase_stale_after(MagicMock())
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_fresh_running_row_blocks_a_later_claim_only(self, real_session_factory) -> None:
        stale_after = running_rebase_stale_after(MagicMock())
        first = await _create_rebase_run(42, 55, "feat/x", real_session_factory)
        second = await _create_rebase_run(42, 55, "feat/x", real_session_factory)
        assert await _find_running_rival(first.id, 55, real_session_factory, stale_after=stale_after) is None
        rival = await _find_running_rival(second.id, 55, real_session_factory, stale_after=stale_after)
        assert rival is not None
        assert rival.id == first.id
        # A different PR is never a rival.
        assert await _find_running_rival(second.id, 56, real_session_factory, stale_after=stale_after) is None
