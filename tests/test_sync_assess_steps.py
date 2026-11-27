"""Sync, assess, worktree and rebase steps.

Split from tests/test_core.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig, TaskSourceConfig
from tests.core_helpers import _make_ctx, _mock_adapter

# Every test in this module runs against the shared per-worker in-memory DB
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# Step implementations (unit tests for individual steps)
# ---------------------------------------------------------------------------


def _mock_git_identity_ok():
    """Return a patch context manager that mocks check_git_identity as valid."""
    from sova.utils.shell import GitIdentityResult

    valid = GitIdentityResult(name="Test User", email="test@example.com")
    return patch("sova.core.steps.sync.check_git_identity", new_callable=AsyncMock, return_value=valid)


class TestSyncStep:
    async def test_execute_syncs_branch(self, tmp_path: Path) -> None:
        from sova.core.steps.sync import SyncStep

        ctx = _make_ctx(tmp_path)
        step = SyncStep()

        with (
            _mock_git_identity_ok(),
            patch("sova.core.steps.sync.git_ops") as mock_ops,
        ):
            mock_ops.sync_branch = AsyncMock()
            result = await step.execute(ctx)

        assert result.success
        mock_ops.sync_branch.assert_awaited_once_with("main", cwd=tmp_path)

    async def test_fails_when_git_identity_missing(self, tmp_path: Path) -> None:
        from sova.core.steps.sync import SyncStep
        from sova.utils.shell import GitIdentityResult

        ctx = _make_ctx(tmp_path)
        step = SyncStep()
        missing_identity = GitIdentityResult(name="", email="")

        with patch("sova.core.steps.sync.check_git_identity", new_callable=AsyncMock, return_value=missing_identity):
            result = await step.execute(ctx)

        assert not result.success
        assert "user.name" in result.error
        assert "user.email" in result.error

    async def test_fails_when_git_name_only_missing(self, tmp_path: Path) -> None:
        from sova.core.steps.sync import SyncStep
        from sova.utils.shell import GitIdentityResult

        ctx = _make_ctx(tmp_path)
        step = SyncStep()
        partial = GitIdentityResult(name="", email="test@example.com")

        with patch("sova.core.steps.sync.check_git_identity", new_callable=AsyncMock, return_value=partial):
            result = await step.execute(ctx)

        assert not result.success
        assert "user.name" in result.error
        assert result.error.startswith("Missing git config: user.name.")

    async def test_fails_with_configured_user_when_push_permission_denied(self, tmp_path: Path) -> None:
        """When github_user is configured and its token was actually used for the
        check, report that identity, not whatever gh account happens to be
        ambient-active."""
        from sova.core.steps.sync import SyncStep
        from sova.utils.gh import PushPermission, PushPermissionCheck

        ctx = _make_ctx(tmp_path, config=ProjectConfig(github_repo="owner/repo", github_user="xsovad06"))
        step = SyncStep()

        with (
            _mock_git_identity_ok(),
            patch(
                "sova.core.steps.sync.check_push_permission",
                new_callable=AsyncMock,
                return_value=PushPermissionCheck(PushPermission.DENIED, checked_as="xsovad06"),
            ),
            patch(
                "sova.core.steps.sync.get_active_gh_user",
                new_callable=AsyncMock,
                return_value="some-other-user",
            ) as mock_active_user,
        ):
            result = await step.execute(ctx)

        assert not result.success
        assert "xsovad06" in result.error
        assert "some-other-user" not in result.error
        assert "owner/repo" in result.error
        mock_active_user.assert_not_awaited()

    async def test_fails_with_active_user_when_no_user_configured(self, tmp_path: Path) -> None:
        from sova.core.steps.sync import SyncStep
        from sova.utils.gh import PushPermission, PushPermissionCheck

        ctx = _make_ctx(tmp_path, config=ProjectConfig(github_repo="owner/repo"))
        step = SyncStep()

        with (
            _mock_git_identity_ok(),
            patch(
                "sova.core.steps.sync.check_push_permission",
                new_callable=AsyncMock,
                return_value=PushPermissionCheck(PushPermission.DENIED, checked_as=None),
            ),
            patch(
                "sova.core.steps.sync.get_active_gh_user",
                new_callable=AsyncMock,
                return_value="ambient-user",
            ),
        ):
            result = await step.execute(ctx)

        assert not result.success
        assert "ambient-user" in result.error

    async def test_fails_with_active_user_when_configured_user_token_unresolved(self, tmp_path: Path) -> None:
        """A github_user is configured, but token resolution for it failed (e.g. that
        account was never `gh auth login`'d on this machine), so the API call actually
        ran under whatever account is ambient-active. The error must name that account,
        not the configured one, since the configured identity's credentials were never
        exercised."""
        from sova.core.steps.sync import SyncStep
        from sova.utils.gh import PushPermission, PushPermissionCheck

        ctx = _make_ctx(tmp_path, config=ProjectConfig(github_repo="owner/repo", github_user="xsovad06"))
        step = SyncStep()

        with (
            _mock_git_identity_ok(),
            patch(
                "sova.core.steps.sync.check_push_permission",
                new_callable=AsyncMock,
                # checked_as=None signals token resolution for "xsovad06" failed, so the
                # call ran under whatever account is ambient-active instead.
                return_value=PushPermissionCheck(PushPermission.DENIED, checked_as=None),
            ),
            patch(
                "sova.core.steps.sync.get_active_gh_user",
                new_callable=AsyncMock,
                return_value="ambient-user",
            ) as mock_active_user,
        ):
            result = await step.execute(ctx)

        assert not result.success
        assert "ambient-user" in result.error
        assert "xsovad06" not in result.error
        mock_active_user.assert_awaited_once()

    async def test_fails_with_unknown_account_when_nothing_resolves(self, tmp_path: Path) -> None:
        from sova.core.steps.sync import SyncStep
        from sova.utils.gh import PushPermission, PushPermissionCheck

        ctx = _make_ctx(tmp_path, config=ProjectConfig(github_repo="owner/repo"))
        step = SyncStep()

        with (
            _mock_git_identity_ok(),
            patch(
                "sova.core.steps.sync.check_push_permission",
                new_callable=AsyncMock,
                return_value=PushPermissionCheck(PushPermission.DENIED, checked_as=None),
            ),
            patch("sova.core.steps.sync.get_active_gh_user", new_callable=AsyncMock, return_value=None),
        ):
            result = await step.execute(ctx)

        assert not result.success
        assert "(unknown account)" in result.error

    async def test_proceeds_when_push_permission_allowed(self, tmp_path: Path) -> None:
        from sova.core.steps.sync import SyncStep
        from sova.utils.gh import PushPermission, PushPermissionCheck

        ctx = _make_ctx(tmp_path, config=ProjectConfig(github_repo="owner/repo", base_branch="main"))
        step = SyncStep()

        with (
            _mock_git_identity_ok(),
            patch(
                "sova.core.steps.sync.check_push_permission",
                new_callable=AsyncMock,
                return_value=PushPermissionCheck(PushPermission.ALLOWED),
            ) as mock_check,
            patch("sova.core.steps.sync.git_ops") as mock_ops,
        ):
            mock_ops.sync_branch = AsyncMock()
            result = await step.execute(ctx)

        assert result.success
        mock_check.assert_awaited_once_with("owner/repo", github_user="", cwd=ctx.project_dir)

    async def test_proceeds_when_push_permission_unknown(self, tmp_path: Path) -> None:
        from sova.core.steps.sync import SyncStep
        from sova.utils.gh import PushPermission, PushPermissionCheck

        ctx = _make_ctx(tmp_path, config=ProjectConfig(github_repo="owner/repo", base_branch="main"))
        step = SyncStep()

        with (
            _mock_git_identity_ok(),
            patch(
                "sova.core.steps.sync.check_push_permission",
                new_callable=AsyncMock,
                return_value=PushPermissionCheck(PushPermission.UNKNOWN),
            ),
            patch("sova.core.steps.sync.git_ops") as mock_ops,
        ):
            mock_ops.sync_branch = AsyncMock()
            result = await step.execute(ctx)

        assert result.success

    async def test_skips_push_permission_check_when_no_repo(self, tmp_path: Path) -> None:
        from sova.core.steps.sync import SyncStep

        ctx = _make_ctx(tmp_path, config=ProjectConfig(github_repo="", base_branch="main"))
        step = SyncStep()

        with (
            _mock_git_identity_ok(),
            patch("sova.core.steps.sync.check_push_permission", new_callable=AsyncMock) as mock_check,
            patch("sova.core.steps.sync.git_ops") as mock_ops,
        ):
            mock_ops.sync_branch = AsyncMock()
            result = await step.execute(ctx)

        assert result.success
        mock_check.assert_not_awaited()


class TestAssessStep:
    @pytest.fixture(autouse=True)
    def _no_existing_pr(self):
        """No existing PR for the issue, without a real `gh pr list` lookup (a test may patch it itself)."""
        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock, return_value=None):
            yield

    async def test_researched_issue_passes(self, tmp_path: Path) -> None:
        from sova.core.steps.assess import AssessStep

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.RESEARCHED
        ctx = _make_ctx(tmp_path, adapter=adapter)
        step = AssessStep()

        result = await step.execute(ctx)

        assert result.success

    async def test_non_researched_issue_fails(self, tmp_path: Path) -> None:
        from sova.core.steps.assess import AssessStep

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.BACKLOG
        ctx = _make_ctx(tmp_path, adapter=adapter)
        step = AssessStep()

        result = await step.execute(ctx)

        assert not result.success

    async def test_force_mode_skips_gate(self, tmp_path: Path) -> None:
        from sova.core.steps.assess import AssessStep

        ctx = _make_ctx(tmp_path, force=True)
        step = AssessStep()
        assert await step.can_skip(ctx)

    async def test_existing_pr_adopted_into_context(self, tmp_path: Path) -> None:
        """When a fresh developer run finds an existing PR, AssessStep should
        adopt it (set ctx.pr_number + ctx.branch_name) and succeed, routing
        the pipeline into the address-review variant."""
        from sova.core.steps.assess import AssessStep
        from sova.git.pr import PRInfo

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.IN_PROGRESS
        ctx = _make_ctx(tmp_path, adapter=adapter, issue_number="42")
        step = AssessStep()

        with (
            patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find,
            patch("sova.core.steps.assess.get_pr_branch", new_callable=AsyncMock, return_value="feat/issue-42"),
        ):
            mock_find.return_value = PRInfo(number=99, url="https://github.com/test/repo/pull/99")
            result = await step.execute(ctx)

        assert result.success, f"Expected success, got: {result.error}"
        assert "Adopted existing PR #99" in result.summary
        assert ctx.pr_number == 99
        assert ctx.branch_name == "feat/issue-42"

    async def test_existing_pr_adoption_uses_prinfo_branch_without_api_call(self, tmp_path: Path) -> None:
        """When PRInfo already carries a branch, AssessStep must use it directly
        and must NOT call get_pr_branch (avoids an extra GitHub API round-trip)."""
        from sova.core.steps.assess import AssessStep
        from sova.git.pr import PRInfo

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.IN_PROGRESS
        ctx = _make_ctx(tmp_path, adapter=adapter, issue_number="42")
        step = AssessStep()

        with (
            patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find,
            patch("sova.core.steps.assess.get_pr_branch", new_callable=AsyncMock) as mock_get_branch,
        ):
            mock_find.return_value = PRInfo(
                number=99, url="https://github.com/test/repo/pull/99", branch="feat/issue-42"
            )
            result = await step.execute(ctx)

        assert result.success
        assert ctx.pr_number == 99
        assert ctx.branch_name == "feat/issue-42"
        mock_get_branch.assert_not_awaited()

    async def test_existing_pr_adoption_sets_pr_number_even_without_branch(self, tmp_path: Path) -> None:
        """PR adoption should still succeed when get_pr_branch returns empty string."""
        from sova.core.steps.assess import AssessStep
        from sova.git.pr import PRInfo

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.IN_PROGRESS
        ctx = _make_ctx(tmp_path, adapter=adapter, issue_number="42")
        step = AssessStep()

        with (
            patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find,
            patch("sova.core.steps.assess.get_pr_branch", new_callable=AsyncMock, return_value=""),
        ):
            mock_find.return_value = PRInfo(number=99, url="https://github.com/test/repo/pull/99")
            result = await step.execute(ctx)

        assert result.success
        assert ctx.pr_number == 99

    async def test_existing_pr_allowed_when_pr_number_set(self, tmp_path: Path) -> None:
        from sova.core.steps.assess import AssessStep

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.IN_PROGRESS
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=99)
        step = AssessStep()

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            result = await step.execute(ctx)

        mock_find.assert_not_called()
        assert result.success

    async def test_existing_pr_allowed_with_force(self, tmp_path: Path) -> None:
        from sova.core.steps.assess import AssessStep

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.IN_PROGRESS
        ctx = _make_ctx(tmp_path, adapter=adapter, force=True)
        step = AssessStep()

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            result = await step.execute(ctx)

        mock_find.assert_not_called()
        assert result.success

    async def test_no_existing_pr_passes(self, tmp_path: Path) -> None:
        from sova.core.steps.assess import AssessStep

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.RESEARCHED
        ctx = _make_ctx(tmp_path, adapter=adapter)
        step = AssessStep()

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            mock_find.return_value = None
            result = await step.execute(ctx)

        assert result.success


class TestAssessStepDivergence:
    """AssessStep must fail a doomed run in seconds when local HEAD has
    already diverged from origin/{branch}, instead of discovering the
    conflict at push time after a full develop cycle."""

    @pytest.fixture(autouse=True)
    def _no_existing_pr(self):
        """No existing PR for the issue, without a real `gh pr list` lookup (a test may patch it itself)."""
        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock, return_value=None):
            yield

    async def test_noop_when_branch_name_empty(self, tmp_path: Path) -> None:
        """A fresh developer run has no branch yet; the check must not run."""
        from sova.core.steps.assess import AssessStep

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.RESEARCHED
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="")
        step = AssessStep()

        with patch("sova.core.steps.assess.check_branch_divergence", new_callable=AsyncMock) as mock_check:
            result = await step.execute(ctx)

        assert result.success
        mock_check.assert_not_awaited()

    async def test_fails_when_diverged(self, tmp_path: Path) -> None:
        from sova.core.steps.assess import AssessStep
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.RESEARCHED
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-42", worktree_dir=tmp_path / "worktree")
        step = AssessStep()

        with patch(
            "sova.core.steps.assess.check_branch_divergence",
            new_callable=AsyncMock,
            return_value=DivergenceCheck(status=DivergenceStatus.DIVERGED, remote_sha="deadbeef", head_sha="cafe"),
        ):
            result = await step.execute(ctx)

        assert not result.success
        assert "deadbeef" in result.error
        assert "cafe" in result.error

    async def test_proceeds_when_ancestor(self, tmp_path: Path) -> None:
        from sova.core.steps.assess import AssessStep
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.RESEARCHED
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-42", worktree_dir=tmp_path / "worktree")
        step = AssessStep()

        with patch(
            "sova.core.steps.assess.check_branch_divergence",
            new_callable=AsyncMock,
            return_value=DivergenceCheck(status=DivergenceStatus.ANCESTOR, remote_sha="aaa", head_sha="bbb"),
        ):
            result = await step.execute(ctx)

        assert result.success

    async def test_proceeds_when_check_errors(self, tmp_path: Path) -> None:
        """A failed check (shallow clone, git unreachable) must not be
        misread as a divergence; the run proceeds normally."""
        from sova.core.steps.assess import AssessStep
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.RESEARCHED
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-42", worktree_dir=tmp_path / "worktree")
        step = AssessStep()

        with patch(
            "sova.core.steps.assess.check_branch_divergence",
            new_callable=AsyncMock,
            return_value=DivergenceCheck(status=DivergenceStatus.ERROR, detail="shallow clone"),
        ):
            result = await step.execute(ctx)

        assert result.success

    async def test_skips_check_when_worktree_dir_unresolved(self, tmp_path: Path) -> None:
        """branch_name can be populated on a resumed run whose worktree_path
        no longer exists on disk, leaving working_dir falling back to
        project_dir (the main checkout). Running the check there would
        compare origin/{branch} against the wrong ref, so it must be skipped
        rather than guessed at."""
        from sova.core.steps.assess import AssessStep

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.RESEARCHED
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-42")
        assert ctx.worktree_dir is None
        step = AssessStep()

        with patch("sova.core.steps.assess.check_branch_divergence", new_callable=AsyncMock) as mock_check:
            result = await step.execute(ctx)

        assert result.success
        mock_check.assert_not_awaited()


class TestWorktreeStep:
    async def test_creates_worktree(self, tmp_path: Path) -> None:
        from sova.core.steps.create_worktree import WorktreeStep

        ctx = _make_ctx(tmp_path, issue_number="42", branch_name="feat/test")
        step = WorktreeStep()

        with patch("sova.core.steps.create_worktree.worktree") as mock_wt:
            mock_info = MagicMock()
            mock_info.path = tmp_path / ".claude" / "worktrees" / "42"
            mock_wt.create_worktree = AsyncMock(return_value=mock_info)
            result = await step.execute(ctx)

        assert result.success
        assert ctx.worktree_dir == tmp_path / ".claude" / "worktrees" / "42"

    async def test_clears_stale_check_cache_marker(self, tmp_path: Path) -> None:
        """A worktree reused across cycles (same path, different issue/branch) must
        not inherit a check-cache marker whose hash happens to coincidentally match
        the new cycle's content."""
        from sova.core.steps.create_worktree import WorktreeStep

        ctx = _make_ctx(tmp_path, issue_number="42", branch_name="feat/test")
        step = WorktreeStep()

        with (
            patch("sova.core.steps.create_worktree.worktree") as mock_wt,
            patch("sova.core.steps.create_worktree.clear_marker") as mock_clear,
        ):
            mock_info = MagicMock()
            mock_info.path = tmp_path / ".claude" / "worktrees" / "42"
            mock_wt.create_worktree = AsyncMock(return_value=mock_info)
            await step.execute(ctx)

        mock_clear.assert_called_once_with(tmp_path / ".claude" / "worktrees" / "42")


class TestEnsureWorktreeStep:
    async def test_skips_when_worktree_exists(self, tmp_path: Path) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        wt = tmp_path / ".claude" / "worktrees" / "42"
        ctx = _make_ctx(tmp_path, worktree_dir=wt, branch_name="feat/issue-42")
        step = EnsureWorktreeStep()

        with patch.object(Path, "exists", return_value=True):
            assert await step.can_skip(ctx) is True

    async def test_fails_without_branch_name(self, tmp_path: Path) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        ctx = _make_ctx(tmp_path, branch_name="")
        step = EnsureWorktreeStep()
        result = await step.execute(ctx)
        assert not result.success
        assert "branch_name" in result.error

    async def test_finds_existing_worktree_by_branch(self, tmp_path: Path) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42", project_dir=tmp_path / "proj")
        step = EnsureWorktreeStep()
        wt_path = tmp_path / "proj" / ".claude" / "worktrees" / "42"

        with patch(
            "sova.core.steps.ensure_worktree.find_worktree_by_branch",
            new_callable=AsyncMock,
            return_value=wt_path,
        ):
            result = await step.execute(ctx)

        assert result.success
        assert ctx.worktree_dir == wt_path

    async def test_creates_worktree_for_pr(self, tmp_path: Path) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        ctx = _make_ctx(
            tmp_path,
            issue_number="",
            pr_number=55,
            branch_name="fix/standalone",
            project_dir=tmp_path / "proj",
        )
        step = EnsureWorktreeStep()
        wt_path = tmp_path / "proj" / ".claude" / "worktrees" / "pr-55"

        with (
            patch(
                "sova.core.steps.ensure_worktree.find_worktree_by_branch",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "sova.core.steps.ensure_worktree.create_worktree",
                new_callable=AsyncMock,
            ) as mock_create,
            patch.object(
                EnsureWorktreeStep,
                "_resolve_base_branch",
                new_callable=AsyncMock,
                return_value="fix/standalone",
            ),
        ):
            mock_info = MagicMock()
            mock_info.path = wt_path
            mock_create.return_value = mock_info
            result = await step.execute(ctx)

        assert result.success
        assert ctx.worktree_dir == wt_path
        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["issue_id"] == "pr-55"

    async def test_rejects_project_dir_as_worktree(self, tmp_path: Path) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        project = tmp_path / "proj"
        ctx = _make_ctx(
            tmp_path,
            worktree_dir=project,
            branch_name="feat/issue-42",
            project_dir=project,
        )
        step = EnsureWorktreeStep()

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "resolve", return_value=project),
        ):
            assert await step.can_skip(ctx) is False

    async def test_clears_project_dir_worktree_and_discovers(self, tmp_path: Path) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        project = tmp_path / "proj"
        wt_path = tmp_path / "proj" / ".claude" / "worktrees" / "42"
        ctx = _make_ctx(
            tmp_path,
            worktree_dir=project,
            branch_name="feat/issue-42",
            project_dir=project,
        )
        step = EnsureWorktreeStep()

        with patch(
            "sova.core.steps.ensure_worktree.find_worktree_by_branch",
            new_callable=AsyncMock,
            return_value=wt_path,
        ):
            result = await step.execute(ctx)

        assert result.success
        assert ctx.worktree_dir == wt_path

    async def test_validate_output_rejects_project_dir(self, tmp_path: Path) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        project = tmp_path / "proj"
        ctx = _make_ctx(tmp_path, worktree_dir=project, project_dir=project)
        step = EnsureWorktreeStep()

        with patch.object(Path, "exists", return_value=True):
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "project root" in gate.reason

    async def test_resolve_base_branch_prefers_local(self, tmp_path: Path) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        mock_result = MagicMock(success=True, stdout="abc123\n")
        with patch("sova.core.steps.ensure_worktree.run", new_callable=AsyncMock, return_value=mock_result):
            branch = await EnsureWorktreeStep._resolve_base_branch("feat/test", tmp_path)

        assert branch == "feat/test"

    async def test_resolve_base_branch_falls_back_to_origin(self, tmp_path: Path) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        fail = MagicMock(success=False)
        ok = MagicMock(success=True, stdout="abc123\n")
        with patch("sova.core.steps.ensure_worktree.run", new_callable=AsyncMock, side_effect=[fail, ok]):
            branch = await EnsureWorktreeStep._resolve_base_branch("feat/test", tmp_path)

        assert branch == "origin/feat/test"

    async def test_resolve_base_branch_returns_name_when_unresolved(self, tmp_path: Path) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        fail = MagicMock(success=False)
        with patch("sova.core.steps.ensure_worktree.run", new_callable=AsyncMock, return_value=fail):
            branch = await EnsureWorktreeStep._resolve_base_branch("feat/test", tmp_path)

        assert branch == "feat/test"


# ---------------------------------------------------------------------------
# SyncStep: task fetch
# ---------------------------------------------------------------------------


class TestSyncStepTaskFetch:
    async def test_fetches_task_during_sync(self, tmp_path: Path) -> None:
        from sova.core.steps.sync import SyncStep

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(id="42", title="Test task")
        ctx = _make_ctx(tmp_path, adapter=adapter)
        assert ctx.task is None

        step = SyncStep()
        with (
            _mock_git_identity_ok(),
            patch("sova.core.steps.sync.git_ops") as mock_ops,
        ):
            mock_ops.sync_branch = AsyncMock()
            result = await step.execute(ctx)

        assert result.success
        assert ctx.task is not None
        assert ctx.task.title == "Test task"

    async def test_preserves_existing_task(self, tmp_path: Path) -> None:
        from sova.core.steps.sync import SyncStep

        existing_task = Task(id="42", title="Already set")
        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, task=existing_task)

        step = SyncStep()
        with (
            _mock_git_identity_ok(),
            patch("sova.core.steps.sync.git_ops") as mock_ops,
        ):
            mock_ops.sync_branch = AsyncMock()
            result = await step.execute(ctx)

        assert result.success
        assert ctx.task.title == "Already set"
        adapter.get_task.assert_not_awaited()

    async def test_sync_succeeds_even_if_task_fetch_fails(self, tmp_path: Path) -> None:
        from sova.core.steps.sync import SyncStep

        adapter = _mock_adapter()
        adapter.get_task.side_effect = RuntimeError("GitHub API down")
        ctx = _make_ctx(tmp_path, adapter=adapter)

        step = SyncStep()
        with (
            _mock_git_identity_ok(),
            patch("sova.core.steps.sync.git_ops") as mock_ops,
        ):
            mock_ops.sync_branch = AsyncMock()
            result = await step.execute(ctx)

        assert result.success
        assert ctx.task is None


# ---------------------------------------------------------------------------
# WorktreeStep: JIRA-aware branch naming
# ---------------------------------------------------------------------------


class TestWorktreeStepJira:
    async def test_jira_branch_name_uses_project_key(self, tmp_path: Path) -> None:
        """JIRA projects should produce feat/PROJKEY-N-slug branch names."""
        from sova.core.steps.create_worktree import WorktreeStep

        ctx = _make_ctx(tmp_path, issue_number="48767")
        ctx.task = Task(id="48767", title="Add security logging")
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="PROJ",
                jira_base_url="https://jira.example.com",
            )
        )
        step = WorktreeStep()

        with patch("sova.core.steps.create_worktree.worktree.create_worktree", new_callable=AsyncMock) as mock_wt:
            mock_wt.return_value = MagicMock(path=tmp_path / "wt")
            await step.execute(ctx)

        assert ctx.branch_name == "feat/PROJ-48767-Add-security-logging"

    async def test_jira_branch_name_without_task_title(self, tmp_path: Path) -> None:
        """JIRA branch name without task title should use bare JIRA key."""
        from sova.core.steps.create_worktree import WorktreeStep

        ctx = _make_ctx(tmp_path, issue_number="48767")
        ctx.task = None
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="PROJ",
                jira_base_url="https://jira.example.com",
            )
        )
        step = WorktreeStep()

        with patch("sova.core.steps.create_worktree.worktree.create_worktree", new_callable=AsyncMock) as mock_wt:
            mock_wt.return_value = MagicMock(path=tmp_path / "wt")
            await step.execute(ctx)

        assert ctx.branch_name == "feat/PROJ-48767"

    async def test_github_branch_name_unchanged(self, tmp_path: Path) -> None:
        """GitHub projects should still produce feat/issue-N branch names."""
        from sova.core.steps.create_worktree import WorktreeStep

        ctx = _make_ctx(tmp_path, issue_number="42")
        ctx.config = ProjectConfig(task_source=TaskSourceConfig(type="github"))
        step = WorktreeStep()

        with patch("sova.core.steps.create_worktree.worktree.create_worktree", new_callable=AsyncMock) as mock_wt:
            mock_wt.return_value = MagicMock(path=tmp_path / "wt")
            await step.execute(ctx)

        assert ctx.branch_name == "feat/issue-42"


# ---------------------------------------------------------------------------
# RebaseStep
# ---------------------------------------------------------------------------


class TestRebaseStep:
    async def test_clean_rebase(self, tmp_path: Path) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = RebaseStep()

        with (
            patch("sova.core.steps.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock) as mock_rebase,
            patch("sova.core.steps.rebase.ensure_claude_artifacts"),
        ):
            mock_rebase.return_value = (
                MagicMock(success=True, conflicts_resolved=0),
                Decimal("0"),
            )
            result = await step.execute(ctx)

        assert result.success
        assert "Rebased onto" in result.summary

    async def test_rebase_with_conflicts_resolved(self, tmp_path: Path) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = RebaseStep()

        with (
            patch("sova.core.steps.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock) as mock_rebase,
            patch("sova.core.steps.rebase.ensure_claude_artifacts"),
        ):
            mock_rebase.return_value = (
                MagicMock(success=True, conflicts_resolved=2),
                Decimal("0.05"),
            )
            result = await step.execute(ctx)

        assert result.success
        assert "2 conflicts resolved" in result.summary

    async def test_clean_rebase_same_dir_skips_artifacts(self, tmp_path: Path) -> None:
        """When working_dir == project_dir, ensure_claude_artifacts is not called."""
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(tmp_path, branch_name="feat/test")
        step = RebaseStep()

        with (
            patch("sova.core.steps.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock) as mock_rebase,
            patch("sova.core.steps.rebase.ensure_claude_artifacts") as mock_artifacts,
        ):
            mock_rebase.return_value = (
                MagicMock(success=True, conflicts_resolved=0),
                Decimal("0"),
            )
            result = await step.execute(ctx)

        assert result.success
        mock_artifacts.assert_not_called()

    async def test_rebase_failure(self, tmp_path: Path) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = RebaseStep()

        with patch("sova.core.steps.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock) as mock_rebase:
            mock_rebase.return_value = (
                MagicMock(success=False, conflicts_resolved=0, error="Unresolved conflicts"),
                Decimal("0.03"),
            )
            result = await step.execute(ctx)

        assert not result.success
        assert "Unresolved conflicts" in result.error

    async def test_gate_check_detects_rebase_in_progress(self, tmp_path: Path) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(tmp_path / "project", worktree_dir=tmp_path)
        step = RebaseStep()

        # Simulate rebase-merge marker in the git directory
        git_dir = tmp_path / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "rebase-merge").mkdir()

        with patch("sova.core.steps.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=str(git_dir))
            gate = await step.validate_output(ctx)

        assert not gate.passed

    async def test_gate_check_passes_when_clean(self, tmp_path: Path) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(tmp_path / "project", worktree_dir=tmp_path)
        step = RebaseStep()

        git_dir = tmp_path / ".git"
        git_dir.mkdir(exist_ok=True)

        with patch("sova.core.steps.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=str(git_dir))
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_gate_check_fails_when_git_dir_unknown(self, tmp_path: Path) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RebaseStep()

        with patch("sova.core.steps.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stdout="", stderr="not a git repo")
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "Cannot determine git directory" in gate.reason

    async def test_ensure_claude_artifacts_called_on_success(self, tmp_path: Path) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(
            tmp_path,
            branch_name="feat/test",
            worktree_dir=tmp_path / "worktree",
            project_dir=tmp_path / "project",
        )
        step = RebaseStep()

        with (
            patch("sova.core.steps.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock) as mock_rebase,
            patch("sova.core.steps.rebase.ensure_claude_artifacts") as mock_artifacts,
        ):
            mock_rebase.return_value = (
                MagicMock(success=True, conflicts_resolved=0),
                Decimal("0"),
            )
            result = await step.execute(ctx)

        assert result.success
        mock_artifacts.assert_called_once_with(tmp_path / "project", tmp_path / "worktree")

    async def test_ensure_claude_artifacts_not_called_when_same_dir(self, tmp_path: Path) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "project")
        ctx.project_dir = tmp_path / "project"
        step = RebaseStep()

        with (
            patch("sova.core.steps.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock) as mock_rebase,
            patch("sova.core.steps.rebase.ensure_claude_artifacts") as mock_artifacts,
        ):
            mock_rebase.return_value = (
                MagicMock(success=True, conflicts_resolved=0),
                Decimal("0"),
            )
            result = await step.execute(ctx)

        assert result.success
        mock_artifacts.assert_not_called()

    async def test_ensure_claude_artifacts_not_called_on_failure(self, tmp_path: Path) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(
            tmp_path,
            branch_name="feat/test",
            worktree_dir=tmp_path / "worktree",
            project_dir=tmp_path / "project",
        )
        step = RebaseStep()

        with (
            patch("sova.core.steps.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock) as mock_rebase,
            patch("sova.core.steps.rebase.ensure_claude_artifacts") as mock_artifacts,
        ):
            mock_rebase.return_value = (
                MagicMock(success=False, conflicts_resolved=0, error="conflict"),
                Decimal("0.01"),
            )
            result = await step.execute(ctx)

        assert not result.success
        mock_artifacts.assert_not_called()

    async def test_can_skip_when_completed(self, tmp_path: Path) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(tmp_path, completed_steps=frozenset({"rebase"}))
        step = RebaseStep()
        assert await step.can_skip(ctx)


# ---------------------------------------------------------------------------
# RebaseWithConflictResolution (git operations)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stub_collision_cleanup")
class TestRebaseWithConflictResolution:
    async def test_clean_rebase_no_conflicts(self, tmp_path: Path) -> None:
        from sova.git.operations import rebase_with_conflict_resolution

        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True),  # fetch
                MagicMock(success=True),  # unstage runtime paths
                MagicMock(success=True, stdout="No local changes to save"),  # stash (nothing to stash)
                MagicMock(success=True, stdout=""),  # rebase (clean)
            ]
            result, cost = await rebase_with_conflict_resolution("main", cwd=tmp_path)

        assert result.success
        assert cost == Decimal("0")

    async def test_clean_rebase_stashes_and_restores_dirty_worktree(self, tmp_path: Path) -> None:
        """Unstaged changes are stashed before rebasing and restored after."""
        from sova.git.operations import rebase_with_conflict_resolution

        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True),  # fetch
                MagicMock(success=True),  # unstage runtime paths
                MagicMock(success=True, stdout="Saved working directory"),  # stash (changes stashed)
                MagicMock(success=True, stdout=""),  # rebase (clean)
                MagicMock(success=True),  # stash pop
            ]
            result, cost = await rebase_with_conflict_resolution("main", cwd=tmp_path)

        assert result.success
        assert cost == Decimal("0")
        assert mock_run.call_args_list[1][0][:4] == ("git", "reset", "-q", "--")
        assert mock_run.call_args_list[2][0][:3] == ("git", "stash", "--include-untracked")
        assert mock_run.call_args_list[4][0][:3] == ("git", "stash", "pop")

    async def test_unstaged_changes_no_conflict_surfaces_real_error(self, tmp_path: Path) -> None:
        """When rebase fails for a non-conflict reason, the real error is reported."""
        from sova.git.operations import rebase_with_conflict_resolution

        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True),  # fetch
                MagicMock(success=True),  # unstage runtime paths
                MagicMock(success=True, stdout="No local changes to save"),  # stash
                MagicMock(success=False, stderr="fatal: cannot rebase: index mismatch"),  # rebase fails
                MagicMock(success=True, stdout=""),  # conflicted files (none)
                MagicMock(success=True),  # rebase --abort
            ]
            result, cost = await rebase_with_conflict_resolution("main", cwd=tmp_path)

        assert not result.success
        assert "Rebase failed" in result.error
        assert "index mismatch" in result.error

    async def test_conflict_resolved_by_llm(self, tmp_path: Path) -> None:
        from sova.git.operations import rebase_with_conflict_resolution
        from sova.llm.models import LLMResult

        with (
            patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.rebase.invoke_command", new_callable=AsyncMock) as mock_llm,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # fetch
                MagicMock(success=True),  # unstage runtime paths
                MagicMock(success=True, stdout="No local changes to save"),  # stash
                MagicMock(success=False, stderr="CONFLICT"),  # rebase fails
                MagicMock(success=True, stdout="file.py\n"),  # conflicted files (initial check)
                MagicMock(success=True, stdout=""),  # no remaining conflicts (after LLM)
                MagicMock(success=True),  # rebase --continue
            ]
            mock_llm.return_value = LLMResult(text="resolved", model="sonnet", cost_usd=Decimal("0.02"))

            result, cost = await rebase_with_conflict_resolution("main", cwd=tmp_path)

        assert result.success
        assert result.conflicts_resolved == 1
        assert cost == Decimal("0.02")

    async def test_conflict_resolution_fails_aborts(self, tmp_path: Path) -> None:
        from sova.git.operations import rebase_with_conflict_resolution

        with (
            patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.rebase.invoke_command", new_callable=AsyncMock) as mock_llm,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # fetch
                MagicMock(success=True),  # unstage runtime paths
                MagicMock(success=True, stdout="No local changes to save"),  # stash
                MagicMock(success=False, stderr="CONFLICT"),  # rebase fails
                MagicMock(success=True, stdout="file.py\n"),  # conflicted files (initial check)
                MagicMock(success=True),  # rebase --abort
            ]
            mock_llm.side_effect = RuntimeError("LLM failed")

            result, cost = await rebase_with_conflict_resolution("main", cwd=tmp_path)

        assert not result.success
        assert "LLM failed" in result.error

    async def test_fetch_failure_returns_error(self, tmp_path: Path) -> None:
        from sova.git.operations import rebase_with_conflict_resolution

        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stderr="Could not resolve host")
            result, cost = await rebase_with_conflict_resolution("main", cwd=tmp_path)

        assert not result.success
        assert "Fetch failed" in result.error
        from sova.git.rebase import RebaseFailure

        assert result.cause == RebaseFailure.FETCH
