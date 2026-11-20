"""Tests to cover uncovered paths in CreatePRStep."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from structlog.testing import capture_logs

from sova.adapters.base import Task, TaskState
from sova.config.models import DEFAULT_COMMIT_SCOPES, ProjectConfig, TaskSourceConfig
from sova.core.context import ExecutionContext
from sova.core.steps._pr_body import _closing_keyword_re
from sova.llm.models import LLMResult

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


def _mock_adapter() -> AsyncMock:
    """Create a mock TaskAdapter for tests."""
    adapter = AsyncMock()
    adapter.get_state.return_value = TaskState.RESEARCHED
    adapter.get_task.return_value = Task(id="1", title="Test issue")
    return adapter


def _make_ctx(tmp_path: Path, **kwargs) -> ExecutionContext:
    """Create a test ExecutionContext with sensible defaults."""
    defaults = {
        "project_dir": tmp_path,
        "config": ProjectConfig(),
        "adapter": _mock_adapter(),
        "issue_number": "42",
        "role": "developer",
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


class TestClosingKeywordRe:
    """_closing_keyword_re must not match a keyword as a word suffix, and must
    recognize the colon form (`Closes: #42`)."""

    def test_matches_bare_closes(self) -> None:
        assert _closing_keyword_re("42").search("Closes #42")

    def test_matches_colon_form(self) -> None:
        assert _closing_keyword_re("42").search("Closes: #42")

    def test_does_not_match_suffix_of_longer_word(self) -> None:
        assert not _closing_keyword_re("42").search("encloses #42")
        assert not _closing_keyword_re("42").search("postfixes #42")

    def test_does_not_match_an_inline_mention_in_a_sentence(self) -> None:
        """Only a line that is *only* the closing reference should match, so a
        summary bullet mentioning the issue inline survives a strip untouched."""
        assert not _closing_keyword_re("42").search("This PR fixes #42 by rewiring the parser.")

    def test_matches_whole_line_with_surrounding_whitespace(self) -> None:
        assert _closing_keyword_re("42").search("  Closes #42  ")

    def test_matches_every_github_supported_keyword_form(self) -> None:
        """All nine GitHub-recognized forms: close/closes/closed, fix/fixes/fixed, resolve/resolves/resolved."""
        for keyword in (
            "close",
            "closes",
            "closed",
            "fix",
            "fixes",
            "fixed",
            "resolve",
            "resolves",
            "resolved",
        ):
            assert _closing_keyword_re("42").search(f"{keyword.capitalize()} #42"), keyword


class TestCreatePRStepLLMFailureFallback:
    """Test that LLM failure in PR body generation triggers fallback."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_llm_failure_triggers_fallback_body(
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """When LLM fails, should use structured fallback body."""
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/app.py | 10 ++++\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        # LLM raises RuntimeError
        mock_invoke.side_effect = RuntimeError("LLM timeout")
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(
            tmp_path,
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

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_non_runtime_error_also_triggers_fallback_body(
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """invoke() routes through guard_prompt(), which raises
        PromptInjectionError (a plain Exception, not a RuntimeError); PR
        creation must fall back to the structured body instead of aborting."""
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/app.py | 10 ++++\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.side_effect = ValueError("prompt injection risk too high")
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(
            tmp_path,
            branch_name="feat/issue-42",
            task=Task(id="42", title="Add widget", body="We need a widget"),
        )
        result = await CreatePRStep().execute(ctx)

        assert result.success
        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "## Summary" in body_arg
        assert "Automated changes for: Add widget" in body_arg
        assert "Closes #42" in body_arg


class TestCreatePRStepTaskTitleFallback:
    """Test task title resolution with adapter failure fallback."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_adapter_get_task_failure_falls_back_to_branch_name(
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """When adapter.get_task fails, should extract title from branch name."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        adapter.get_task.side_effect = RuntimeError("Network error")

        ctx = _make_ctx(
            tmp_path,
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
    async def test_git_log_failure_shows_unavailable(
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """When git log fails, should show (unavailable) in prompt."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=False, stdout=""),  # git log fails
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
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
    async def test_git_diff_stat_failure_shows_unavailable(
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """When git diff --stat fails, should show (unavailable) in prompt."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=False, stdout=""),  # git diff --stat fails
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
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
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """When git diff (content) fails, should show (unavailable) in prompt."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=False, stdout=""),  # git diff (content) fails
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
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
    async def test_assign_pr_failure_is_non_fatal(
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """When PR assignment fails, should log but not fail the step."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
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
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """When tracker state transition fails, should log but not fail the step."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        adapter.transition_state.side_effect = RuntimeError("API error")

        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        # Should succeed despite tracker update failure
        assert result.success
        assert ctx.pr_number == 10


class TestCreatePRStepCanSkip:
    """Test the can_skip method."""

    async def test_can_skip_when_pr_number_exists(self, tmp_path: Path) -> None:
        """Should skip when PR number is already set."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(tmp_path, pr_number=123)
        step = CreatePRStep()
        assert await step.can_skip(ctx)

    async def test_cannot_skip_when_no_pr_number(self, tmp_path: Path) -> None:
        """Should not skip when PR number is not set."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(tmp_path, pr_number=None)
        step = CreatePRStep()
        assert not await step.can_skip(ctx)


class TestCreatePRStepJiraBodyCleaning:
    """Test JIRA-specific body cleaning logic."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_jira_body_strips_closes_syntax(
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
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
            tmp_path,
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


class TestCreatePRStepClosingSection:
    """Every PR closes its issue: the ``## Issue`` section never depends on the issue text or the model answer."""

    @staticmethod
    def _ctx(tmp_path: Path, *, issue_body: str) -> ExecutionContext:
        return _make_ctx(
            tmp_path,
            task=Task(id="42", title="Add widget", body=issue_body),
            branch_name="feat/issue-42",
        )

    @staticmethod
    def _wire(mock_run, mock_invoke, mock_create_pr, llm_text: str, *, git_ok: bool = True) -> None:
        """Wire the four git calls, the LLM body response, and the created PR."""
        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),
            MagicMock(success=git_ok, stdout="abc123 feat: add widget\n" if git_ok else ""),
            MagicMock(success=git_ok, stdout=" src/app.py | 1 +\n" if git_ok else ""),
            MagicMock(success=git_ok, stdout="diff --git a/src/app.py\n+change\n" if git_ok else ""),
        ]
        mock_invoke.return_value = LLMResult(text=llm_text, model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

    @pytest.mark.parametrize(
        ("issue_body", "llm_text", "git_ok"),
        [
            pytest.param("Plain description, no criteria section.", "## Summary\n- stuff", True, id="no-criteria"),
            pytest.param(
                "## Acceptance Criteria\n- [ ] First\n- [ ] Second\n", "## Summary\n- stuff", True, id="criteria"
            ),
            pytest.param(
                "## Acceptance Criteria\n- [ ] First\n- [ ] Second\n",
                "## Summary\n- stuff\n\n<!-- ACCEPTANCE_VERDICTS -->\n- [met] First\n- [unmet] Second",
                True,
                id="model-verdict-block-is-description-only",
            ),
            pytest.param(
                "## Acceptance Criteria\n- [ ] First\n",
                "## Summary\n- stuff\n\n## Review guidance\nThis is Part of #42, the rest is left undone.",
                True,
                id="model-part-of-phrase-is-description-only",
            ),
            pytest.param("## Acceptance Criteria\n- [ ] First\n", "## Summary\n- stuff", False, id="git-unavailable"),
        ],
    )
    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_issue_section_always_reads_closes(
        self, mock_create_pr, mock_run, mock_invoke, _find, issue_body, llm_text, git_ok, tmp_path: Path
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.utils.gh import parse_issue_links
        from sova.utils.markdown import extract_section

        self._wire(mock_run, mock_invoke, mock_create_pr, llm_text, git_ok=git_ok)

        result = await CreatePRStep().execute(self._ctx(tmp_path, issue_body=issue_body))

        assert result.success
        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert extract_section(body_arg, "Issue") == "Closes #42"
        assert parse_issue_links(body_arg).closes == ("42",)
        assert "Not done in this PR:" not in body_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    @pytest.mark.parametrize("retry_times_out", [False, True], ids=["retry-succeeds", "both-attempts-time-out"])
    async def test_timed_out_body_call_still_closes_the_issue(
        self, mock_create_pr, mock_run, mock_invoke, _find, retry_times_out: bool, tmp_path: Path
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.errors import LLMTimeoutError
        from sova.utils.gh import parse_issue_links
        from sova.utils.markdown import extract_section

        self._wire(mock_run, mock_invoke, mock_create_pr, "")
        retry = (
            LLMTimeoutError("timed out again")
            if retry_times_out
            else LLMResult(text="## Summary\n- stuff\n\nno verdicts", model="s", cost_usd=Decimal(0))
        )
        mock_invoke.side_effect = [LLMTimeoutError("timed out"), retry]

        ctx = self._ctx(tmp_path, issue_body="## Acceptance Criteria\n- Works\n")
        result = await CreatePRStep().execute(ctx)

        assert result.success
        assert mock_invoke.await_count == 2
        assert mock_invoke.call_args_list[0].kwargs["timeout"] == ctx.config.commit.pr_body_timeout
        assert ctx.config.commit.pr_body_timeout == 300
        body = mock_create_pr.call_args.kwargs["body"]
        assert extract_section(body, "Issue") == "Closes #42"
        assert parse_issue_links(body).closes == ("42",)


class TestCreatePRStepAdoptedPRTrackerUpdate:
    """Test tracker state transition when adopting existing PR."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock)
    async def test_adopted_pr_tracker_failure_is_non_fatal(self, mock_find, tmp_path: Path) -> None:
        """When adopting existing PR, tracker update failure should be non-fatal."""
        from sova.core.steps.create_pr import CreatePRStep

        mock_find.return_value = MagicMock(number=55, url="https://github.com/x/y/pull/55")

        adapter = _mock_adapter()
        adapter.transition_state.side_effect = RuntimeError("Tracker API down")

        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-42")
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
        assert title == "[RHCLOUD-48928] feat: add widget support"

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


class TestCreatePRStepTitleCandidate:
    """Test the LLM-proposed PR title: accepted, rejected, and the JIRA bypass."""

    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    async def test_valid_llm_candidate_becomes_the_title(self, mock_run, mock_invoke, tmp_path: Path) -> None:
        from sova.core.steps._pr_body import TITLE_MARKER
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(
            text=f"## Summary\n- stuff\n{TITLE_MARKER}\nfeat(core): add a widget",
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
        step = CreatePRStep()
        _, title = await step._generate_pr_body(ctx, "Add widget", "feat(core): fallback title")

        assert title == "feat(core): add a widget"

    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    async def test_overlong_llm_candidate_is_discarded(self, mock_run, mock_invoke, tmp_path: Path) -> None:
        from sova.core.steps._pr_body import TITLE_MARKER
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        overlong = "feat(core): " + "x" * 80
        mock_invoke.return_value = LLMResult(
            text=f"## Summary\n- stuff\n{TITLE_MARKER}\n{overlong}",
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
        step = CreatePRStep()
        _, title = await step._generate_pr_body(ctx, "Add widget", "feat(core): fallback title")

        assert title == "feat(core): fallback title"

    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    async def test_candidate_with_unconfigured_type_is_discarded(self, mock_run, mock_invoke, tmp_path: Path) -> None:
        from sova.core.steps._pr_body import TITLE_MARKER
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(
            text=f"## Summary\n- stuff\n{TITLE_MARKER}\nperf(core): speed things up",
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
        step = CreatePRStep()
        _, title = await step._generate_pr_body(ctx, "Add widget", "feat(core): fallback title")

        assert title == "feat(core): fallback title"

    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    async def test_jira_project_never_requests_a_title_candidate(self, mock_run, mock_invoke, tmp_path: Path) -> None:
        from sova.core.steps._pr_body import TITLE_MARKER
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))

        ts = TaskSourceConfig(type="jira", jira_project_key="PROJ", jira_base_url="https://jira.example.com")
        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42", config=ProjectConfig(task_source=ts))
        step = CreatePRStep()
        _, title = await step._generate_pr_body(ctx, "Add widget", "[PROJ-42] feat: add widget")

        prompt = mock_invoke.call_args.args[0]
        assert TITLE_MARKER not in prompt
        assert title == "[PROJ-42] feat: add widget"

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_freeform_pr_title_format_skips_the_conventional_prefix(
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """commit.pr_title_format == "freeform" must not force a type(scope)
        prefix onto the title, and must not request or validate an LLM
        candidate against commit.types/.scopes."""
        from sova.config.models import CommitConfig
        from sova.core.steps._pr_body import TITLE_MARKER
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # scope inference (unused for freeform)
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(
            tmp_path,
            branch_name="feat/issue-42",
            task=Task(id="42", title="Add widget support", body="We need a widget"),
            config=ProjectConfig(commit=CommitConfig(pr_title_format="freeform")),
        )
        result = await CreatePRStep().execute(ctx)

        assert result.success
        prompt = mock_invoke.call_args.args[0]
        assert TITLE_MARKER not in prompt
        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert title_arg == "Add widget support"


class TestInferScope:
    """Test scope inference from changed paths for GitHub PR titles."""

    def test_single_scope(self) -> None:
        from sova.core.steps.create_pr import _infer_scope

        assert (
            _infer_scope(["sova/dashboard/app.py", "sova/dashboard/routers/x.py"], DEFAULT_COMMIT_SCOPES) == "dashboard"
        )

    def test_dominant_scope_wins(self) -> None:
        from sova.core.steps.create_pr import _infer_scope

        files = ["sova/core/a.py", "sova/core/b.py", "sova/cli/c.py"]
        assert _infer_scope(files, DEFAULT_COMMIT_SCOPES) == "core"

    def test_tie_breaks_to_first_seen(self) -> None:
        from sova.core.steps.create_pr import _infer_scope

        assert _infer_scope(["sova/cli/a.py", "sova/db/b.py"], DEFAULT_COMMIT_SCOPES) == "cli"

    def test_empty_falls_back_to_core(self) -> None:
        from sova.core.steps.create_pr import _infer_scope

        assert _infer_scope([], DEFAULT_COMMIT_SCOPES) == "core"
        assert _infer_scope(None, DEFAULT_COMMIT_SCOPES) == "core"

    def test_non_sova_paths_fall_back_to_core(self) -> None:
        from sova.core.steps.create_pr import _infer_scope

        assert _infer_scope(["tests/test_x.py", "docs/guide.md", "README.md"], DEFAULT_COMMIT_SCOPES) == "core"

    def test_unknown_sova_dir_falls_back_to_core(self) -> None:
        from sova.core.steps.create_pr import _infer_scope

        # sova/notascope/ is not in VALID_SCOPES
        assert _infer_scope(["sova/notascope/x.py"], DEFAULT_COMMIT_SCOPES) == "core"

    def test_top_level_sova_file_falls_back_to_core(self) -> None:
        from sova.core.steps.create_pr import _infer_scope

        # sova/__init__.py has only 2 parts: no scope segment
        assert _infer_scope(["sova/__init__.py"], DEFAULT_COMMIT_SCOPES) == "core"

    def test_mixed_sova_and_non_sova_paths_fall_back_to_core(self) -> None:
        from sova.core.steps.create_pr import _infer_scope

        # A non-sova path (README.md) makes this a mixed change even though
        # the other path is validly scoped, so it must not return "dashboard".
        assert _infer_scope(["sova/dashboard/app.py", "README.md"], DEFAULT_COMMIT_SCOPES) == "core"

    def test_configured_scopes_override_sovas_own_list(self) -> None:
        """A project's own commit.scopes, not SOVA's own DEFAULT_COMMIT_SCOPES,
        decides which top-level sova/<dir> paths carry a real scope."""
        from sova.core.steps.create_pr import _infer_scope

        assert _infer_scope(["sova/widgets/app.py"], scopes=["widgets"]) == "widgets"
        # "dashboard" is a real SOVA scope but not in this project's list, so
        # the mixed/unmatched fallback must be a scope the project actually
        # accepts ("widgets", its only configured one), never the literal
        # "core": that scope might not be in the project's own list either.
        assert _infer_scope(["sova/dashboard/app.py"], scopes=["widgets"]) == "widgets"

    def test_unmatched_path_falls_back_to_core_when_core_is_configured(self) -> None:
        """ "core" stays the fallback when it is itself an accepted scope,
        which is the common case (SOVA's own convention, or any project that
        happens to keep a "core" scope)."""
        from sova.core.steps.create_pr import _infer_scope

        assert _infer_scope(["sova/dashboard/app.py"], scopes=["widgets", "core"]) == "core"

    def test_empty_configured_scopes_infers_no_scope(self) -> None:
        """An empty commit.scopes (a project with no scope convention) never emits a scope."""
        from sova.core.steps.create_pr import _infer_scope

        assert _infer_scope(["sova/dashboard/app.py"], scopes=[]) == ""

    def test_non_sova_directory_segment_matches_a_configured_scope(self) -> None:
        """A project whose layout isn't SOVA's own (no "sova/" top-level
        directory) must still get a real scope from the changed path, not
        always fall back to "core"."""
        from sova.core.steps.create_pr import _infer_scope

        files = ["apps/accounts/models.py", "apps/accounts/views.py"]
        assert _infer_scope(files, scopes=["accounts", "billing"]) == "accounts"

    def test_docs_only_path_matches_the_docs_scope(self) -> None:
        from sova.core.steps.create_pr import _infer_scope

        assert _infer_scope(["docs/pricing.md"], scopes=["accounts", "docs"]) == "docs"


class TestGithubTitleScope:
    """Test GitHub PR title building uses an inferred scope, not the issue number."""

    def test_github_title_infers_scope(self) -> None:
        from sova.core.steps.create_pr import _build_pr_title

        title = _build_pr_title(
            "Add widget support", "1245", changed_files=["sova/dashboard/app.py"], scopes=DEFAULT_COMMIT_SCOPES
        )
        assert title == "feat(dashboard): add widget support"

    def test_github_title_no_issue_number_in_title(self) -> None:
        from sova.core.steps.create_pr import _build_pr_title

        title = _build_pr_title("Fix the thing", "99", changed_files=["sova/cli/app.py"], scopes=DEFAULT_COMMIT_SCOPES)
        assert "#99" not in title
        assert title == "feat(cli): fix the thing"

    def test_github_title_preserves_existing_type(self) -> None:
        from sova.core.steps.create_pr import _build_pr_title

        title = _build_pr_title(
            "fix: broken login", "42", changed_files=["sova/core/auth.py"], scopes=DEFAULT_COMMIT_SCOPES
        )
        assert title == "fix(core): broken login"

    def test_github_title_defaults_to_core_without_files(self) -> None:
        from sova.core.steps.create_pr import _build_pr_title

        title = _build_pr_title("Do a thing", "42", scopes=DEFAULT_COMMIT_SCOPES)
        assert title == "feat(core): do a thing"

    def test_github_title_has_no_scope_when_the_project_has_none(self) -> None:
        from sova.core.steps.create_pr import _build_pr_title

        title = _build_pr_title("Do a thing", "42", changed_files=["sova/dashboard/app.py"])
        assert title == "feat: do a thing"

    def test_github_title_uses_configured_scopes(self) -> None:
        from sova.core.steps.create_pr import _build_pr_title

        title = _build_pr_title("Add a widget", "42", changed_files=["sova/widgets/app.py"], scopes=["widgets"])
        assert title == "feat(widgets): add a widget"

    def test_github_title_uses_configured_types(self) -> None:
        """A project whose commit.types omits "feat" must not get a title its
        own commit-format check rejects just because the LLM call failed."""
        from sova.core.steps.create_pr import _build_pr_title

        title = _build_pr_title(
            "Add a widget", "42", changed_files=["sova/widgets/app.py"], types=["feature"], scopes=["widgets"]
        )
        assert title.startswith("feature(")

    def test_long_issue_title_is_lowercased_and_trimmed_to_72_chars(self) -> None:
        """The deterministic fallback has to satisfy the convention on its own.

        It is the path taken whenever the LLM-proposed title fails
        validation, and a verbatim issue title is routinely far over the
        limit (this one, from a real PR, is 118 characters).
        """
        from sova.core.steps.commit_convention import MAX_LINE_LENGTH, is_valid_commit_line
        from sova.core.steps.create_pr import _build_pr_title

        gwym_title = (
            "Make the pricing claims match the app: drop tax optimization, "
            "gate the health score trend, drop the plan parameter"
        )
        assert len(gwym_title) > MAX_LINE_LENGTH

        title = _build_pr_title(gwym_title, "42", changed_files=["sova/dashboard/app.py"], scopes=DEFAULT_COMMIT_SCOPES)

        assert len(title) <= MAX_LINE_LENGTH
        assert title.startswith("feat(dashboard): make the pricing claims match the app")
        assert is_valid_commit_line(title, types=["feat"], scopes=["dashboard"])

    def test_docs_only_change_gets_the_docs_type(self) -> None:
        """A branch that only touches docs/ gets a PR title of type "docs",
        not the project's default type, when the project configures it."""
        from sova.core.steps.create_pr import _build_pr_title

        title = _build_pr_title(
            "Explain the retry policy", "42", changed_files=["docs/retry-policy.md"], scopes=["docs"]
        )
        assert title == "docs(docs): explain the retry policy"

    def test_non_sova_accounts_path_gets_the_accounts_scope(self) -> None:
        from sova.core.steps.create_pr import _build_pr_title

        title = _build_pr_title(
            "Retry failed billing charges",
            "42",
            changed_files=["apps/accounts/models.py"],
            scopes=["accounts", "billing"],
        )
        assert title == "feat(accounts): retry failed billing charges"

    def test_configured_non_sova_type_prefix_is_not_doubled(self) -> None:
        """An issue title already prefixed with a project's own configured
        type (not one of SOVA's own feat/fix/.../ci) must be recognized and
        stripped, not stapled with a second prefix on top."""
        from sova.core.steps.create_pr import _build_pr_title

        title = _build_pr_title(
            "perf: speed up the dashboard query",
            "42",
            changed_files=["sova/dashboard/app.py"],
            scopes=["dashboard"],
            types=["perf", "feat"],
        )
        assert title == "perf(dashboard): speed up the dashboard query"
        assert "feat(" not in title

    def test_jira_title_keeps_its_bracket_prefix_when_trimmed(self) -> None:
        from sova.core.steps.commit_convention import MAX_LINE_LENGTH
        from sova.core.steps.create_pr import _build_pr_title

        ts = TaskSourceConfig(type="jira", jira_project_key="RHCLOUD", jira_base_url="https://issues.redhat.com")
        title = _build_pr_title("Rework the whole scheduling subsystem " * 5, "4242", task_source=ts)

        assert title.startswith("[RHCLOUD-4242] feat: rework the whole scheduling subsystem")
        assert len(title) <= MAX_LINE_LENGTH


class TestResolveChangedFiles:
    """Test CreatePRStep._resolve_changed_files, the scope-inference input."""

    @patch("sova.core.steps.create_pr.run", new_callable=AsyncMock)
    async def test_jira_skips_the_git_call(self, mock_run, tmp_path: Path) -> None:
        """JIRA titles don't infer a scope, so no git diff should be run."""
        from sova.core.steps.create_pr import CreatePRStep

        ts = TaskSourceConfig(type="jira", jira_project_key="RHCLOUD", jira_base_url="https://issues.redhat.com")
        ctx = _make_ctx(tmp_path, config=ProjectConfig(task_source=ts))
        result = await CreatePRStep._resolve_changed_files(ctx)

        assert result == []
        mock_run.assert_not_called()

    @patch("sova.core.steps.create_pr.run", new_callable=AsyncMock)
    async def test_github_returns_changed_paths(self, mock_run, tmp_path: Path) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.return_value = MagicMock(success=True, stdout="sova/cli/a.py\nsova/db/b.py\n")
        ctx = _make_ctx(tmp_path, config=ProjectConfig(task_source=TaskSourceConfig(type="github")))
        result = await CreatePRStep._resolve_changed_files(ctx)

        assert result == ["sova/cli/a.py", "sova/db/b.py"]

    @patch("sova.core.steps.create_pr.run", new_callable=AsyncMock)
    async def test_uses_merge_base_diff_not_base_tip(self, mock_run, tmp_path: Path) -> None:
        """Must diff against the merge base (three-dot), not the base branch's current
        tip (two-dot): otherwise base-only commits landed after this branch diverged
        would be attributed to this PR's scope inference."""
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.return_value = MagicMock(success=True, stdout="sova/cli/a.py\n")
        ctx = _make_ctx(tmp_path, config=ProjectConfig(task_source=TaskSourceConfig(type="github")))
        await CreatePRStep._resolve_changed_files(ctx)

        mock_run.assert_called_once_with("git", "diff", "origin/main...HEAD", "--name-only", cwd=ctx.working_dir)

    @patch("sova.core.steps.create_pr.run", new_callable=AsyncMock)
    async def test_git_failure_fails_open_to_empty_list(self, mock_run, tmp_path: Path) -> None:
        """A failed git diff must not abort PR creation; scope inference falls back to core."""
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.return_value = MagicMock(success=False, returncode=128, stdout="", stderr="fatal: bad revision")
        ctx = _make_ctx(tmp_path, config=ProjectConfig(task_source=TaskSourceConfig(type="github")))
        result = await CreatePRStep._resolve_changed_files(ctx)

        assert result == []

    @patch("sova.core.steps.create_pr.run", new_callable=AsyncMock)
    async def test_missing_git_binary_fails_open_to_empty_list(self, mock_run, tmp_path: Path) -> None:
        """A missing `git` binary raises FileNotFoundError from the subprocess call, not a
        failed ShellResult; this must also fail open rather than aborting PR creation."""
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.side_effect = FileNotFoundError("git")
        ctx = _make_ctx(tmp_path, config=ProjectConfig(task_source=TaskSourceConfig(type="github")))
        result = await CreatePRStep._resolve_changed_files(ctx)

        assert result == []

    @patch("sova.core.steps.create_pr.run", new_callable=AsyncMock)
    async def test_blank_lines_are_filtered_out(self, mock_run, tmp_path: Path) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.return_value = MagicMock(success=True, stdout="sova/cli/a.py\n\n\n")
        ctx = _make_ctx(tmp_path, config=ProjectConfig(task_source=TaskSourceConfig(type="github")))
        result = await CreatePRStep._resolve_changed_files(ctx)

        assert result == ["sova/cli/a.py"]


class TestValidScopesNoDrift:
    """The CommitConfig defaults must stay in sync with invariants/commit-format.sh."""

    def test_valid_types_match_invariant(self) -> None:
        """DEFAULT_COMMIT_TYPES is a second copy of the invariant's VALID_TYPES.

        Guarded for the same reason the scope list below is: a type added to
        one and not the other either rejects a commit SOVA generated or
        generates one the pre-push hook rejects.
        """
        import re

        from sova.config.models import DEFAULT_COMMIT_TYPES

        root = Path(__file__).resolve().parent.parent
        shell = (root / "invariants" / "commit-format.sh").read_text(encoding="utf-8")
        match = re.search(r'VALID_TYPES="([^"]+)"', shell)
        assert match, "could not find VALID_TYPES in invariants/commit-format.sh"
        shell_types = set(match.group(1).split("|"))
        assert set(DEFAULT_COMMIT_TYPES) == shell_types, (
            "DEFAULT_COMMIT_TYPES has drifted from invariants/commit-format.sh; "
            f"only in python: {set(DEFAULT_COMMIT_TYPES) - shell_types}; "
            f"only in shell: {shell_types - set(DEFAULT_COMMIT_TYPES)}"
        )

    def test_valid_scopes_match_invariant(self) -> None:
        import re

        root = Path(__file__).resolve().parent.parent
        shell = (root / "invariants" / "commit-format.sh").read_text(encoding="utf-8")
        match = re.search(r'VALID_SCOPES="([^"]+)"', shell)
        assert match, "could not find VALID_SCOPES in invariants/commit-format.sh"
        shell_scopes = set(match.group(1).split("|"))
        assert set(DEFAULT_COMMIT_SCOPES) == shell_scopes, (
            "DEFAULT_COMMIT_SCOPES has drifted from invariants/commit-format.sh; "
            f"only in python: {set(DEFAULT_COMMIT_SCOPES) - shell_scopes}; "
            f"only in shell: {shell_scopes - set(DEFAULT_COMMIT_SCOPES)}"
        )


class TestCreatePRStepDiffTruncation:
    """Test diff content truncation logic."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_large_diff_content_is_truncated(
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """When diff content exceeds the pr_body_diff_cap (8000 chars), it is trimmed with a marker."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        large_diff = "+" + ("x" * 9000)  # 9001 chars
        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout=large_diff),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        # Verify LLM prompt contains truncated diff
        prompt_arg = mock_invoke.call_args.args[0]
        assert "...(truncated)" in prompt_arg
        assert "+" + "x" * 7999 in prompt_arg
        assert "x" * 8000 not in prompt_arg


class TestCreatePRStepFallbackBody:
    """The fallback body (no LLM ran) closes the issue like every other generation path."""

    @pytest.mark.parametrize(
        "issue_body",
        [
            pytest.param("## Acceptance Criteria\n- [ ] Do the thing\n", id="criteria"),
            pytest.param("No acceptance criteria heading here.", id="no-criteria"),
        ],
    )
    def test_issue_section_reads_closes(self, tmp_path: Path, issue_body: str) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(
            tmp_path,
            task=Task(id="42", title="Fix the bug", body=issue_body),
            issue_number="42",
            branch_name="feat/issue-42",
        )
        body = CreatePRStep._build_fallback_body(ctx, "Fix the bug", "abc feat", "x.py | 3 +++")
        assert body.split("## Issue", 1)[1].strip() == "Closes #42"
        assert "Part of #42" not in body
        assert "Not done in this PR:" not in body


class TestCreatePRStepJiraFallbackBody:
    """Test JIRA-specific fallback body building."""

    def test_jira_fallback_body_includes_jira_link(self, tmp_path: Path) -> None:
        """JIRA fallback body should include JIRA link instead of Closes."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(
            tmp_path,
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
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """When trigger_review is enabled, should post @coderabbitai review comment."""
        from decimal import Decimal

        from sova.config.models import CodeRabbitConfig, ExternalReviewsConfig
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
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
    async def test_trigger_review_skipped_when_disabled(
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """When trigger_review is disabled (default), should NOT post comment."""
        from decimal import Decimal

        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        ctx.adapter.post_pr_comment.assert_not_called()

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_trigger_review_failure_is_non_fatal(
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        """When posting the trigger comment fails, step should still succeed."""
        from decimal import Decimal

        from sova.config.models import CodeRabbitConfig, ExternalReviewsConfig
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        adapter.post_pr_comment.side_effect = RuntimeError("API error")

        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-42")
        ctx.config = ProjectConfig(
            external_reviews=ExternalReviewsConfig(
                coderabbit=CodeRabbitConfig(trigger_review=True),
            ),
        )

        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        assert ctx.pr_number == 10


class TestCreatePRStepLdapReviewerSuggestion:
    """Test LDAP-based reviewer suggestion after PR creation."""

    async def test_disabled_by_default_skips_lookup(self, tmp_path: Path) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=None)

        await CreatePRStep()._suggest_reviewers(ctx, 10)

        adapter.add_reviewer.assert_not_called()

    async def test_no_github_user_configured_skips_lookup(self, tmp_path: Path) -> None:
        from sova.config.models import LdapConfig
        from sova.core.steps.create_pr import CreatePRStep

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=None)
        ctx.config = ProjectConfig(github_user="", ldap=LdapConfig(enabled=True))

        mock_client = AsyncMock()
        with patch("sova.adapters.ldap_client.create_ldap_client", return_value=mock_client):
            await CreatePRStep()._suggest_reviewers(ctx, 10)

        mock_client.check_connectivity.assert_not_called()
        adapter.add_reviewer.assert_not_called()

    async def test_vpn_unavailable_skips_lookup(self, tmp_path: Path) -> None:
        from sova.config.models import LdapConfig
        from sova.core.steps.create_pr import CreatePRStep

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=None)
        ctx.config = ProjectConfig(github_user="octocat", ldap=LdapConfig(enabled=True))

        mock_client = AsyncMock()
        mock_client.check_connectivity.return_value = False
        with patch("sova.adapters.ldap_client.create_ldap_client", return_value=mock_client):
            await CreatePRStep()._suggest_reviewers(ctx, 10)

        mock_client.find_manager_chain.assert_not_called()
        adapter.add_reviewer.assert_not_called()

    async def test_no_manager_found_skips_lookup(self, tmp_path: Path) -> None:
        from sova.config.models import LdapConfig
        from sova.core.steps.create_pr import CreatePRStep

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=None)
        ctx.config = ProjectConfig(github_user="octocat", ldap=LdapConfig(enabled=True))

        mock_client = AsyncMock()
        mock_client.check_connectivity.return_value = True
        mock_client.find_manager_chain.return_value = []
        with patch("sova.adapters.ldap_client.create_ldap_client", return_value=mock_client):
            await CreatePRStep()._suggest_reviewers(ctx, 10)

        mock_client.get_org_chart.assert_not_called()
        adapter.add_reviewer.assert_not_called()

    async def test_adds_up_to_two_teammates_excluding_author(self, tmp_path: Path) -> None:
        from sova.adapters.ldap_client import Person
        from sova.config.models import LdapConfig
        from sova.core.steps.create_pr import CreatePRStep

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=None, issue_number="42")
        ctx.config = ProjectConfig(github_user="octocat", ldap=LdapConfig(enabled=True))

        manager = Person(uid="mgr", dn="uid=mgr,ou=users,dc=redhat,dc=com")
        mock_client = AsyncMock()
        mock_client.check_connectivity.return_value = True
        mock_client.find_manager_chain.return_value = [manager]
        mock_client.get_org_chart.return_value = [
            Person(uid="octocat"),  # the author themself: must be excluded
            Person(uid="alice"),
            Person(uid="bob"),
            Person(uid="carol"),  # beyond the cap of 2
        ]

        with patch("sova.adapters.ldap_client.create_ldap_client", return_value=mock_client):
            await CreatePRStep()._suggest_reviewers(ctx, 10)

        mock_client.get_org_chart.assert_awaited_once_with("mgr", depth=1)
        assert adapter.add_reviewer.await_args_list == [
            call("42", 10, "alice"),
            call("42", 10, "bob"),
        ]

    async def test_add_reviewer_failure_is_non_fatal(self, tmp_path: Path) -> None:
        from sova.adapters.ldap_client import Person
        from sova.config.models import LdapConfig
        from sova.core.steps.create_pr import CreatePRStep

        adapter = _mock_adapter()
        adapter.add_reviewer.side_effect = RuntimeError("API error")
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=None, issue_number="42")
        ctx.config = ProjectConfig(github_user="octocat", ldap=LdapConfig(enabled=True))

        manager = Person(uid="mgr", dn="uid=mgr,ou=users,dc=redhat,dc=com")
        mock_client = AsyncMock()
        mock_client.check_connectivity.return_value = True
        mock_client.find_manager_chain.return_value = [manager]
        mock_client.get_org_chart.return_value = [Person(uid="alice")]

        with patch("sova.adapters.ldap_client.create_ldap_client", return_value=mock_client):
            with capture_logs() as logs:
                await CreatePRStep()._suggest_reviewers(ctx, 10)

        adapter.add_reviewer.assert_awaited_once_with("42", 10, "alice")
        warnings = [e for e in logs if e["log_level"] == "warning"]
        assert [e["event"] for e in warnings] == ["ldap_reviewers.add_reviewer_failed"]
        assert warnings[0]["user"] == "alice"

    async def test_uses_uid_mapping_when_github_user_differs_from_ldap_uid(self, tmp_path: Path) -> None:
        """github_user and LDAP uid are frequently different identifiers (ldap.uid_mapping)."""
        from sova.adapters.ldap_client import Person
        from sova.config.models import LdapConfig
        from sova.core.steps.create_pr import CreatePRStep

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=None)
        ctx.config = ProjectConfig(
            github_user="octocat", ldap=LdapConfig(enabled=True, uid_mapping={"octocat": "jdoe"})
        )

        manager = Person(uid="mgr", dn="uid=mgr,ou=users,dc=redhat,dc=com")
        mock_client = AsyncMock()
        mock_client.check_connectivity.return_value = True
        mock_client.find_manager_chain.return_value = [manager]
        mock_client.get_org_chart.return_value = []

        with patch("sova.adapters.ldap_client.create_ldap_client", return_value=mock_client):
            await CreatePRStep()._suggest_reviewers(ctx, 10)

        mock_client.find_manager_chain.assert_awaited_once_with("jdoe", max_depth=1)

    async def test_unmapped_identity_miss_is_logged_distinguishably(self, tmp_path: Path) -> None:
        """An unmapped github_user assumed as the LDAP uid logs a distinguishable reason on miss."""
        from sova.config.models import LdapConfig
        from sova.core.steps.create_pr import CreatePRStep

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=None)
        ctx.config = ProjectConfig(github_user="octocat", ldap=LdapConfig(enabled=True))

        mock_client = AsyncMock()
        mock_client.check_connectivity.return_value = True
        mock_client.find_manager_chain.return_value = []

        with (
            patch("sova.adapters.ldap_client.create_ldap_client", return_value=mock_client),
            patch("sova.core.steps.create_pr.log") as mock_log,
        ):
            await CreatePRStep()._suggest_reviewers(ctx, 10)

        mock_client.find_manager_chain.assert_awaited_once_with("octocat", max_depth=1)
        mock_log.info.assert_called_once()
        assert mock_log.info.call_args[0][0] == "ldap_reviewers.identity_unmapped"


class TestCreatePRStepGitRanges:
    """The three-dot range is the merge-base diff for "git diff", but the
    symmetric difference for "git log": the log must use the two-dot form or
    it also lists the base branch's own commits."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_git_log_uses_two_dot_range_and_diff_uses_three(
        self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat: thing\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
        result = await CreatePRStep().execute(ctx)

        assert result.success
        calls = [call.args for call in mock_run.call_args_list]
        log_calls = [args for args in calls if args[:2] == ("git", "log")]
        body_diff_calls = [args for args in calls if args[:2] == ("git", "diff") and "--name-only" not in args]

        assert log_calls, "expected a git log call for the commit log"
        for args in log_calls:
            assert args[2] == f"origin/{ctx.base_branch}..HEAD"
        assert body_diff_calls, "expected git diff calls for the body"
        for args in body_diff_calls:
            assert args[2] == f"origin/{ctx.base_branch}...HEAD"
