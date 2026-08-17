"""Tests to cover uncovered paths in CreatePRStep."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig, TaskSourceConfig
from sova.core.context import ExecutionContext
from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for workflow engine tests."""
    original_db_url = os.environ.get("SOVA_DATABASE_URL")
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    if original_db_url is not None:
        os.environ["SOVA_DATABASE_URL"] = original_db_url
    else:
        os.environ.pop("SOVA_DATABASE_URL", None)


def _mock_adapter() -> AsyncMock:
    """Create a mock TaskAdapter for tests."""
    adapter = AsyncMock()
    adapter.get_state.return_value = TaskState.RESEARCHED
    adapter.get_task.return_value = Task(id="1", title="Test issue")
    return adapter


def _make_ctx(**kwargs) -> ExecutionContext:
    """Create a test ExecutionContext with sensible defaults."""
    defaults = {
        "project_dir": Path("/tmp/test"),
        "config": ProjectConfig(),
        "adapter": _mock_adapter(),
        "issue_number": "42",
        "role": "developer",
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


class TestCreatePRStepLLMFailureFallback:
    """Test that LLM failure in PR body generation triggers fallback."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_llm_failure_triggers_fallback_body(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """When LLM fails, should use structured fallback body."""
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/app.py | 10 ++++\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        # LLM raises RuntimeError
        mock_invoke.side_effect = RuntimeError("LLM timeout")
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(
            branch_name="feat/issue-42",
            task=Task(id="42", title="Add widget", body="We need a widget"),
        )
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        # Verify fallback body structure was used
        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "## Summary" in body_arg
        assert "Automated changes for: Add widget" in body_arg
        assert "## Commits" in body_arg
        assert "abc123" in body_arg
        assert "Closes #42" in body_arg


class TestCreatePRStepTaskTitleFallback:
    """Test task title resolution with adapter failure fallback."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_adapter_get_task_failure_falls_back_to_branch_name(
        self, mock_create_pr, mock_run, mock_invoke, _find
    ) -> None:
        """When adapter.get_task fails, should extract title from branch name."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        adapter.get_task.side_effect = RuntimeError("Network error")

        ctx = _make_ctx(
            adapter=adapter,
            task=None,  # No task in context
            branch_name="feat/issue-42-add-widget-support",
        )
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        # Title should be extracted from branch name
        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "add widget support" in title_arg


class TestCreatePRStepGitCommandFailures:
    """Test handling of git command failures."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_git_log_failure_shows_unavailable(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """When git log fails, should show (unavailable) in prompt."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=False, stdout=""),  # git log fails
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        # Verify LLM was called with "(unavailable)" for commit log
        prompt_arg = mock_invoke.call_args.args[0]
        assert "(unavailable)" in prompt_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_git_diff_stat_failure_shows_unavailable(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """When git diff --stat fails, should show (unavailable) in prompt."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=False, stdout=""),  # git diff --stat fails
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        prompt_arg = mock_invoke.call_args.args[0]
        assert "(unavailable)" in prompt_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_git_diff_content_failure_shows_unavailable(
        self, mock_create_pr, mock_run, mock_invoke, _find
    ) -> None:
        """When git diff (content) fails, should show (unavailable) in prompt."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=False, stdout=""),  # git diff (content) fails
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        prompt_arg = mock_invoke.call_args.args[0]
        assert "(unavailable)" in prompt_arg


class TestCreatePRStepSideEffectFailures:
    """Test exception handling in post-create side effects."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_assign_pr_failure_is_non_fatal(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """When PR assignment fails, should log but not fail the step."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(branch_name="feat/issue-42")
        ctx.config = ProjectConfig(github_user="xsovad06")

        with patch("sova.core.steps.create_pr.git_ops.assign_pr", new_callable=AsyncMock) as mock_assign:
            mock_assign.side_effect = RuntimeError("Permission denied")
            step = CreatePRStep()
            result = await step.execute(ctx)

        # Should succeed despite assignment failure
        assert result.success
        assert ctx.pr_number == 10

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_tracker_state_transition_failure_is_non_fatal(
        self, mock_create_pr, mock_run, mock_invoke, _find
    ) -> None:
        """When tracker state transition fails, should log but not fail the step."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        adapter.transition_state.side_effect = RuntimeError("API error")

        ctx = _make_ctx(adapter=adapter, branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        # Should succeed despite tracker update failure
        assert result.success
        assert ctx.pr_number == 10


class TestCreatePRStepCanSkip:
    """Test the can_skip method."""

    async def test_can_skip_when_pr_number_exists(self) -> None:
        """Should skip when PR number is already set."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(pr_number=123)
        step = CreatePRStep()
        assert await step.can_skip(ctx)

    async def test_cannot_skip_when_no_pr_number(self) -> None:
        """Should not skip when PR number is not set."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(pr_number=None)
        step = CreatePRStep()
        assert not await step.can_skip(ctx)


class TestCreatePRStepJiraBodyCleaning:
    """Test JIRA-specific body cleaning logic."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_jira_body_strips_closes_syntax(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """JIRA PRs should strip 'Closes #N' from LLM output and add JIRA link instead."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        # LLM incorrectly includes "Closes #48928"
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- stuff\n\nCloses #48928", model="sonnet", cost_usd=Decimal("0.01")
        )
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(
            task=Task(id="48928", title="Fix parity", body=""),
            issue_number="48928",
            branch_name="feat/RHCLOUD-48928",
        )
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="RHCLOUD",
                jira_base_url="https://issues.redhat.com",
            )
        )
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        body_arg = mock_create_pr.call_args.kwargs["body"]
        # Should strip "Closes #48928" and add JIRA link
        assert "Closes #48928" not in body_arg
        assert "JIRA: https://issues.redhat.com/browse/RHCLOUD-48928" in body_arg


class TestCreatePRStepAdoptedPRTrackerUpdate:
    """Test tracker state transition when adopting existing PR."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock)
    async def test_adopted_pr_tracker_failure_is_non_fatal(self, mock_find) -> None:
        """When adopting existing PR, tracker update failure should be non-fatal."""
        from sova.core.steps.create_pr import CreatePRStep

        mock_find.return_value = MagicMock(number=55, url="https://github.com/x/y/pull/55")

        adapter = _mock_adapter()
        adapter.transition_state.side_effect = RuntimeError("Tracker API down")

        ctx = _make_ctx(adapter=adapter, branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        # Should succeed despite tracker failure
        assert result.success
        assert ctx.pr_number == 55


class TestCreatePRStepJiraTitleBuilding:
    """Test JIRA-specific PR title building."""

    def test_jira_title_without_conventional_prefix(self) -> None:
        """JIRA PR titles should prepend [KEY-N] and add 'feat:' when missing."""
        from sova.core.steps.create_pr import _build_pr_title

        ts = TaskSourceConfig(
            type="jira",
            jira_project_key="RHCLOUD",
            jira_base_url="https://issues.redhat.com",
        )
        title = _build_pr_title("Add widget support", "48928", task_source=ts)
        assert title == "[RHCLOUD-48928] feat: Add widget support"

    def test_jira_title_with_conventional_prefix(self) -> None:
        """JIRA PR titles should prepend [KEY-N] and preserve existing type."""
        from sova.core.steps.create_pr import _build_pr_title

        ts = TaskSourceConfig(
            type="jira",
            jira_project_key="PROJ",
            jira_base_url="https://jira.example.com",
        )
        title = _build_pr_title("fix(auth): broken login", "42", task_source=ts)
        assert title == "[PROJ-42] fix: broken login"


class TestCreatePRStepDiffTruncation:
    """Test diff content truncation logic."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_large_diff_content_is_truncated(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """When diff content exceeds 8000 chars, should truncate with message."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        large_diff = "+" + ("x" * 9000)  # 9001 chars
        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout=large_diff),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        # Verify LLM prompt contains truncated diff
        prompt_arg = mock_invoke.call_args.args[0]
        assert "(diff truncated" in prompt_arg
        assert "showing first 8000 chars)" in prompt_arg


class TestCreatePRStepJiraFallbackBody:
    """Test JIRA-specific fallback body building."""

    def test_jira_fallback_body_includes_jira_link(self) -> None:
        """JIRA fallback body should include JIRA link instead of Closes."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(
            task=Task(id="48928", title="Fix parity", body=""),
            issue_number="48928",
            branch_name="feat/RHCLOUD-48928",
        )
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="RHCLOUD",
                jira_base_url="https://issues.redhat.com",
            )
        )

        body = CreatePRStep._build_fallback_body(ctx, "Fix parity", "abc123 feat", "x.py | 3 +++")
        assert "JIRA: https://issues.redhat.com/browse/RHCLOUD-48928" in body
        assert "Closes" not in body


class TestCreatePRStepCodeRabbitTrigger:
    """Test CodeRabbit review trigger after PR creation."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_trigger_review_posts_comment_when_enabled(
        self, mock_create_pr, mock_run, mock_invoke, _find
    ) -> None:
        """When trigger_review is enabled, should post @coderabbitai review comment."""
        from decimal import Decimal

        from sova.config.models import CodeRabbitConfig, ExternalReviewsConfig
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(branch_name="feat/issue-42")
        ctx.config = ProjectConfig(
            external_reviews=ExternalReviewsConfig(
                coderabbit=CodeRabbitConfig(trigger_review=True),
            ),
        )

        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        ctx.adapter.post_pr_comment.assert_called_once_with(10, "@coderabbitai review")

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_trigger_review_skipped_when_disabled(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """When trigger_review is disabled (default), should NOT post comment."""
        from decimal import Decimal

        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        ctx.adapter.post_pr_comment.assert_not_called()

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_trigger_review_failure_is_non_fatal(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """When posting the trigger comment fails, step should still succeed."""
        from decimal import Decimal

        from sova.config.models import CodeRabbitConfig, ExternalReviewsConfig
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        adapter.post_pr_comment.side_effect = RuntimeError("API error")

        ctx = _make_ctx(adapter=adapter, branch_name="feat/issue-42")
        ctx.config = ProjectConfig(
            external_reviews=ExternalReviewsConfig(
                coderabbit=CodeRabbitConfig(trigger_review=True),
            ),
        )

        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        assert ctx.pr_number == 10
