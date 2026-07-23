"""Tests for sova.supervisor.rebase."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.supervisor.rebase import (
    ROLE_SUPERVISOR_REBASE,
    _already_attempted,
    _create_rebase_run,
    _finalize_run,
    _get_pr_info,
    _run_pre_push_hook,
    _update_run_cost,
    _write_manual_handoff,
    attempt_auto_rebase,
)


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
    async def test_different_sha_returns_false(self) -> None:
        factory, session = _mock_session_factory()
        mock_run = MagicMock()
        mock_run.handoff_json = {"head_sha": "old_sha"}
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run]
        session.execute = AsyncMock(return_value=mock_result)
        assert await _already_attempted(55, "new_sha", factory) is False


class TestGetPrInfo:
    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.find_pr_for_issue", new_callable=AsyncMock)
    async def test_no_pr_returns_none(self, mock_find: AsyncMock) -> None:
        mock_find.return_value = None
        result = await _get_pr_info(42, "owner/repo", "user")
        assert result is None

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.find_pr_for_issue", new_callable=AsyncMock)
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
    @patch("sova.supervisor.rebase.load_config")
    async def test_no_github_repo_skips(self, mock_cfg: MagicMock) -> None:
        mock_cfg.return_value.github_repo = ""
        factory, _ = _mock_session_factory()
        result = await attempt_auto_rebase(42, Path("/fake"), factory)
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase._get_pr_info", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.load_config")
    async def test_no_pr_skips(self, mock_cfg: MagicMock, mock_info: AsyncMock) -> None:
        mock_cfg.return_value.github_repo = "owner/repo"
        mock_cfg.return_value.github_user = "user"
        mock_info.return_value = None
        factory, _ = _mock_session_factory()
        result = await attempt_auto_rebase(42, Path("/fake"), factory)
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase._already_attempted", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._get_pr_info", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.load_config")
    async def test_already_attempted_skips(
        self,
        mock_cfg: MagicMock,
        mock_info: AsyncMock,
        mock_dup: AsyncMock,
    ) -> None:
        mock_cfg.return_value.github_repo = "owner/repo"
        mock_cfg.return_value.github_user = "user"
        mock_cfg.return_value.base_branch = "main"
        mock_info.return_value = {"number": 55, "branch": "feat/x", "head_sha": "abc123", "base_branch": "main"}
        mock_dup.return_value = True
        factory, _ = _mock_session_factory()
        result = await attempt_auto_rebase(42, Path("/fake"), factory)
        assert result["status"] == "skipped"
        assert "Already attempted" in result["reason"]

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase._get_pr_info", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.load_config")
    async def test_setup_exception_returns_dict(self, mock_cfg: MagicMock, mock_info: AsyncMock) -> None:
        """Setup exceptions (before task_run created) return failed dict, not propagate."""
        mock_cfg.return_value.github_repo = "owner/repo"
        mock_cfg.return_value.github_user = "user"
        mock_cfg.return_value.base_branch = "main"
        mock_info.return_value = {"number": 55, "branch": "feat/x", "head_sha": "abc123", "base_branch": "main"}
        factory, _ = _mock_session_factory()
        with patch(
            "sova.supervisor.rebase._already_attempted", new_callable=AsyncMock, side_effect=RuntimeError("db crash")
        ):
            result = await attempt_auto_rebase(42, Path("/fake"), factory)
        assert result["status"] == "failed"
        assert "db crash" in result["error"]


class TestRoleSupervisorRebaseConstant:
    def test_constant_value(self) -> None:
        assert ROLE_SUPERVISOR_REBASE == "supervisor:rebase"


class TestGetPrInfoEdgeCases:
    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.find_pr_for_issue", new_callable=AsyncMock)
    async def test_gh_view_fails_returns_none(self, mock_find, mock_run) -> None:
        mock_pr = MagicMock()
        mock_pr.number = 55
        mock_find.return_value = mock_pr
        mock_run.return_value = MagicMock(success=False, stdout="", stderr="error")
        result = await _get_pr_info(42, "owner/repo", "user")
        assert result is None

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.find_pr_for_issue", new_callable=AsyncMock)
    async def test_invalid_json_returns_none(self, mock_find, mock_run) -> None:
        mock_pr = MagicMock()
        mock_pr.number = 55
        mock_find.return_value = mock_pr
        mock_run.return_value = MagicMock(success=True, stdout="not valid json")
        result = await _get_pr_info(42, "owner/repo", "user")
        assert result is None


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
        assert mock_run.handoff_json == dict(head_sha="abc123")
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_finalizes_failure(self) -> None:
        factory, session = _mock_session_factory()
        mock_run = MagicMock()
        session.get = AsyncMock(return_value=mock_run)
        await _finalize_run(1, "failed", "something broke", "abc123", factory)
        assert mock_run.status == "failed"
        assert mock_run.error_message == "something broke"

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

    def _cfg_mock(self, mock_cfg):
        mock_cfg.return_value.github_repo = "owner/repo"
        mock_cfg.return_value.github_user = "user"
        mock_cfg.return_value.base_branch = "main"
        mock_cfg.return_value.worktree.copy_files = []

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.cleanup_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._run_pre_push_hook", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._finalize_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._update_run_cost", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._create_rebase_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.create_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._already_attempted", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._get_pr_info", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.load_config")
    async def test_success_path(
        self,
        mock_cfg,
        mock_info,
        mock_dup,
        mock_wt,
        mock_rebase,
        mock_create_run,
        mock_update_cost,
        mock_finalize,
        mock_hook,
        mock_run,
        mock_cleanup,
    ) -> None:  # noqa: E501
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        self._cfg_mock(mock_cfg)
        mock_info.return_value = pr_info
        mock_dup.return_value = False
        wt_info = MagicMock()
        wt_info.path = Path("/tmp/wt")
        mock_wt.return_value = wt_info
        mock_create_run.return_value = mock_task_run
        mock_rebase.return_value = (MagicMock(success=True, conflicts_resolved=2), Decimal("0.05"))
        mock_hook.return_value = {"passed": True, "output": ""}
        mock_run.return_value = MagicMock(success=True, stdout="", stderr="")
        result = await attempt_auto_rebase(42, Path("/fake"), factory)
        assert result["status"] == "success"
        assert result["pr_number"] == 55
        mock_cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.cleanup_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._finalize_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._update_run_cost", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._create_rebase_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.create_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._already_attempted", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._get_pr_info", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.load_config")
    async def test_rebase_failure_writes_handoff(
        self,
        mock_cfg,
        mock_info,
        mock_dup,
        mock_wt,
        mock_rebase,
        mock_create_run,
        mock_update_cost,
        mock_finalize,
        mock_cleanup,
    ) -> None:  # noqa: E501
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        self._cfg_mock(mock_cfg)
        mock_info.return_value = pr_info
        mock_dup.return_value = False
        wt_info = MagicMock()
        wt_info.path = Path("/tmp/wt")
        mock_wt.return_value = wt_info
        mock_create_run.return_value = mock_task_run
        mock_rebase.return_value = (MagicMock(success=False, error="conflict"), Decimal("0.03"))
        with patch("sova.supervisor.rebase._write_manual_handoff"):
            result = await attempt_auto_rebase(42, Path("/fake"), factory)
        assert result["status"] == "failed"
        mock_cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.cleanup_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._run_pre_push_hook", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._finalize_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._update_run_cost", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._create_rebase_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.create_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._already_attempted", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._get_pr_info", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.load_config")
    async def test_pre_push_hook_failure(
        self,
        mock_cfg,
        mock_info,
        mock_dup,
        mock_wt,
        mock_rebase,
        mock_create_run,
        mock_update_cost,
        mock_finalize,
        mock_hook,
        mock_cleanup,
    ) -> None:  # noqa: E501
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        self._cfg_mock(mock_cfg)
        mock_info.return_value = pr_info
        mock_dup.return_value = False
        wt_info = MagicMock()
        wt_info.path = Path("/tmp/wt")
        mock_wt.return_value = wt_info
        mock_create_run.return_value = mock_task_run
        mock_rebase.return_value = (MagicMock(success=True, conflicts_resolved=1), Decimal("0.02"))
        mock_hook.return_value = {"passed": False, "output": "ruff check failed"}
        with patch("sova.supervisor.rebase._write_manual_handoff"):
            result = await attempt_auto_rebase(42, Path("/fake"), factory)
        assert result["status"] == "failed"
        assert "Pre-push hook failed" in result["error"]

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.cleanup_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._run_pre_push_hook", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._finalize_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._update_run_cost", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._create_rebase_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.create_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._already_attempted", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._get_pr_info", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.load_config")
    async def test_push_stale_info_skipped(
        self,
        mock_cfg,
        mock_info,
        mock_dup,
        mock_wt,
        mock_rebase,
        mock_create_run,
        mock_update_cost,
        mock_finalize,
        mock_hook,
        mock_run,
        mock_cleanup,
    ) -> None:  # noqa: E501
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        self._cfg_mock(mock_cfg)
        mock_info.return_value = pr_info
        mock_dup.return_value = False
        wt_info = MagicMock()
        wt_info.path = Path("/tmp/wt")
        mock_wt.return_value = wt_info
        mock_create_run.return_value = mock_task_run
        mock_rebase.return_value = (MagicMock(success=True, conflicts_resolved=1), Decimal("0.01"))
        mock_hook.return_value = {"passed": True, "output": ""}
        mock_run.return_value = MagicMock(success=False, stdout="", stderr="stale info detected")
        result = await attempt_auto_rebase(42, Path("/fake"), factory)
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.cleanup_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._run_pre_push_hook", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._finalize_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._update_run_cost", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._create_rebase_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.create_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._already_attempted", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._get_pr_info", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.load_config")
    async def test_push_other_failure(
        self,
        mock_cfg,
        mock_info,
        mock_dup,
        mock_wt,
        mock_rebase,
        mock_create_run,
        mock_update_cost,
        mock_finalize,
        mock_hook,
        mock_run,
        mock_cleanup,
    ) -> None:  # noqa: E501
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        self._cfg_mock(mock_cfg)
        mock_info.return_value = pr_info
        mock_dup.return_value = False
        wt_info = MagicMock()
        wt_info.path = Path("/tmp/wt")
        mock_wt.return_value = wt_info
        mock_create_run.return_value = mock_task_run
        mock_rebase.return_value = (MagicMock(success=True, conflicts_resolved=1), Decimal("0.01"))
        mock_hook.return_value = {"passed": True, "output": ""}
        mock_run.return_value = MagicMock(success=False, stdout="", stderr="network timeout")
        result = await attempt_auto_rebase(42, Path("/fake"), factory)
        assert result["status"] == "failed"
        assert "network timeout" in result["error"]

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.cleanup_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._finalize_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._create_rebase_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.create_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._already_attempted", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._get_pr_info", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.load_config")
    async def test_unexpected_exception(
        self, mock_cfg, mock_info, mock_dup, mock_wt, mock_create_run, mock_finalize, mock_cleanup
    ) -> None:  # noqa: E501
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        self._cfg_mock(mock_cfg)
        mock_info.return_value = pr_info
        mock_dup.return_value = False
        wt_info = MagicMock()
        wt_info.path = Path("/tmp/wt")
        mock_wt.return_value = wt_info
        mock_create_run.return_value = mock_task_run
        with patch(
            "sova.supervisor.rebase.rebase_with_conflict_resolution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected crash"),
        ):  # noqa: E501
            result = await attempt_auto_rebase(42, Path("/fake"), factory)
        assert result["status"] == "failed"
        assert "unexpected crash" in result["error"]
        mock_finalize.assert_awaited_once()
        mock_cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("sova.supervisor.rebase.run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._run_pre_push_hook", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._finalize_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._update_run_cost", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._create_rebase_run", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.create_worktree", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._already_attempted", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase._get_pr_info", new_callable=AsyncMock)
    @patch("sova.supervisor.rebase.load_config")
    async def test_worktree_cleanup_failure_non_fatal(
        self,
        mock_cfg,
        mock_info,
        mock_dup,
        mock_wt,
        mock_rebase,
        mock_create_run,
        mock_update_cost,
        mock_finalize,
        mock_hook,
        mock_run,
    ) -> None:  # noqa: E501
        factory, session, mock_task_run, pr_info = self._setup_mocks()
        self._cfg_mock(mock_cfg)
        mock_info.return_value = pr_info
        mock_dup.return_value = False
        wt_info = MagicMock()
        wt_info.path = Path("/tmp/wt")
        mock_wt.return_value = wt_info
        mock_create_run.return_value = mock_task_run
        mock_rebase.return_value = (MagicMock(success=True, conflicts_resolved=1), Decimal("0.01"))
        mock_hook.return_value = {"passed": True, "output": ""}
        mock_run.return_value = MagicMock(success=True, stdout="", stderr="")
        with patch(
            "sova.supervisor.rebase.cleanup_worktree", new_callable=AsyncMock, side_effect=OSError("cleanup failed")
        ):  # noqa: E501
            result = await attempt_auto_rebase(42, Path("/fake"), factory)
        assert result["status"] == "success"
