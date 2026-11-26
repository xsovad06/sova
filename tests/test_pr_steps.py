"""Create-PR and handoff steps.

Split from tests/test_core.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig, TaskSourceConfig
from tests.core_helpers import _make_ctx, _mock_adapter, _sova_commit_config

# Every test in this module runs against the shared per-worker in-memory DB
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


class TestCreatePRStep:
    async def test_gate_check_requires_pr_number(self, tmp_path: Path) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(tmp_path, pr_number=None)
        step = CreatePRStep()
        gate = await step.validate_output(ctx)
        assert not gate.passed

    async def test_gate_check_passes_with_pr(self, tmp_path: Path) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(tmp_path, pr_number=99)
        step = CreatePRStep()
        gate = await step.validate_output(ctx)
        assert gate.passed

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_generates_structured_body(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/app.py | 10 ++++\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- Add widget\n\nCloses #42", model="sonnet", cost_usd=Decimal("0.01")
        )
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(
            tmp_path,
            adapter=adapter,
            task=Task(id="42", title="Add widget", body="We need a widget"),
            branch_name="feat/issue-42",
        )
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        assert ctx.pr_number == 10
        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "## Summary" in body_arg
        assert "Closes #42" in body_arg

    @pytest.mark.parametrize("configured_user", ["xsovad06", ""])
    @patch("sova.core.steps.create_pr.git_ops.create_pr", new_callable=AsyncMock)
    async def test_immediate_create_passes_configured_github_user(
        self, mock_create_pr, configured_user: str, tmp_path: Path
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")
        ctx = _make_ctx(tmp_path, config=ProjectConfig(github_user=configured_user), branch_name="feat/issue-42")
        step = CreatePRStep()
        step._post_create_side_effects = AsyncMock()

        result = await step._create_pr_immediate(ctx, "title", "body")

        assert result.success
        assert mock_create_pr.call_args.kwargs["github_user"] == configured_user

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_includes_closes_for_issue(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=12, url="https://github.com/x/y/pull/12")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
        step = CreatePRStep()
        await step.execute(ctx)

        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "Closes #42" in body_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_appends_closes_for_bare_issue_mention(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        """A bare '#42' mention (no Closes/Fixes/Resolves keyword) does not count as closing the issue."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- See issue #42 for context", model="sonnet", cost_usd=Decimal("0.01")
        )
        mock_create_pr.return_value = MagicMock(number=13, url="https://github.com/x/y/pull/13")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
        step = CreatePRStep()
        await step.execute(ctx)

        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "Closes #42" in body_arg
        assert body_arg.count("#42") == 2

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_appends_closes_even_when_body_closes_a_different_issue(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        """A stacked PR's body can legitimately close a different issue; ours still gets appended."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- stuff\n\nCloses #12", model="sonnet", cost_usd=Decimal("0.01")
        )
        mock_create_pr.return_value = MagicMock(number=14, url="https://github.com/x/y/pull/14")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
        step = CreatePRStep()
        await step.execute(ctx)

        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "Closes #12" in body_arg
        assert "Closes #42" in body_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_body_includes_commits_and_diff(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/app.py | 10 ++++\n 1 file changed, 10 insertions(+)\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py b/src/app.py\n+widget code\n"),
        ]
        mock_invoke.return_value = LLMResult(
            text=(
                "## Summary\n- widget\n\n## Commits\nabc123 feat: add widget\n\n## Files changed\nsrc/app.py | 10 ++++"
            ),
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
        mock_create_pr.return_value = MagicMock(number=11, url="https://github.com/x/y/pull/11")

        ctx = _make_ctx(
            tmp_path,
            branch_name="feat/issue-42",
            task=Task(id="42", title="Add widget", body="We need a widget"),
        )
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        assert ctx.pr_number == 11
        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "Closes #42" in body_arg
        assert "## Summary" in body_arg
        assert "abc123" in body_arg
        assert "src/app.py" in body_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_assigns_pr_to_user(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
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
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-42")
        ctx.config = ProjectConfig(github_user="xsovad06")
        step = CreatePRStep()

        with patch("sova.core.steps.create_pr.git_ops.assign_pr", new_callable=AsyncMock) as mock_assign:
            result = await step.execute(ctx)

        assert result.success
        mock_assign.assert_awaited_once_with(10, assignee="xsovad06", repo="", github_user="xsovad06")

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_skips_assignment_when_no_github_user(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
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
        ctx.config = ProjectConfig(github_user="")
        step = CreatePRStep()

        with patch("sova.core.steps.create_pr.git_ops.assign_pr", new_callable=AsyncMock) as mock_assign:
            result = await step.execute(ctx)

        assert result.success
        mock_assign.assert_not_awaited()

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock)
    async def test_execute_adopts_existing_pr(self, mock_find, tmp_path: Path) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        mock_find.return_value = MagicMock(number=55, url="https://github.com/x/y/pull/55")

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        assert "Adopted existing PR #55" in result.summary
        assert ctx.pr_number == 55
        assert ctx.pr_url == "https://github.com/x/y/pull/55"

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock)
    async def test_execute_transitions_to_in_review_on_adopt(self, mock_find, tmp_path: Path) -> None:
        """Adopting an existing PR must still transition issue to IN_REVIEW."""
        from sova.core.steps.create_pr import CreatePRStep

        mock_find.return_value = MagicMock(number=55, url="https://github.com/x/y/pull/55")

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-42")
        step = CreatePRStep()
        await step.execute(ctx)

        adapter.transition_state.assert_awaited_once_with("42", TaskState.IN_REVIEW)

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_adopts_pr_from_already_exists_error(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.side_effect = RuntimeError(
            'a pull request for branch "feat/issue-48809" into branch "master" '
            "already exists: https://github.com/org/repo/pull/3148"
        )

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-48809")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        assert "Adopted existing PR #3148" in result.summary
        assert ctx.pr_number == 3148

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_fails_on_non_duplicate_runtime_error(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.side_effect = RuntimeError("permission denied")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert not result.success
        assert "permission denied" in result.error


class TestCreatePRStepJira:
    """PR creation with JIRA task source: title prefix and body link."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_jira_pr_title_has_ticket_prefix(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
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
        ctx = _make_ctx(
            tmp_path,
            adapter=adapter,
            task=Task(id="48928", title="Improve parity check log output"),
            issue_number="48928",
            branch_name="feat/issue-48928",
        )
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="PROJ",
                jira_base_url="https://jira.example.com",
            )
        )
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "[PROJ-48928]" in title_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_jira_pr_body_has_ticket_link(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
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
        ctx = _make_ctx(
            tmp_path,
            adapter=adapter,
            task=Task(id="48928", title="Improve parity check log output"),
            issue_number="48928",
            branch_name="feat/issue-48928",
        )
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="PROJ",
                jira_base_url="https://jira.example.com",
            )
        )
        step = CreatePRStep()
        await step.execute(ctx)

        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "https://jira.example.com/browse/PROJ-48928" in body_arg
        assert "Closes #48928" not in body_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_jira_fallback_body_has_ticket_link(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.side_effect = RuntimeError("LLM unavailable")
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(
            tmp_path,
            adapter=adapter,
            task=Task(id="48928", title="Improve parity check log output"),
            issue_number="48928",
            branch_name="feat/issue-48928",
        )
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="PROJ",
                jira_base_url="https://jira.example.com",
            )
        )
        step = CreatePRStep()
        await step.execute(ctx)

        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "https://jira.example.com/browse/PROJ-48928" in body_arg
        assert "Closes #48928" not in body_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_github_pr_title_uses_inferred_scope_not_issue_number(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        """GitHub titles use a valid conventional scope, never feat(#N): (no JIRA prefix)."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            # _resolve_changed_files: git diff --name-only (runs before body generation)
            MagicMock(success=True, stdout="sova/dashboard/app.py\nsova/dashboard/routers/x.py\n"),
            # _generate_pr_body: log, diff --stat, diff
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" sova/dashboard/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/sova/dashboard/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- stuff\n\nCloses #42", model="sonnet", cost_usd=Decimal("0.01")
        )
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-42")
        ctx.config = _sova_commit_config().model_copy(update={"task_source": TaskSourceConfig(type="github")})
        step = CreatePRStep()
        await step.execute(ctx)

        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "[" not in title_arg  # no JIRA prefix
        assert "#42" not in title_arg  # issue number is linked in the body, not the title
        assert title_arg.startswith("feat(dashboard): ")  # scope inferred from changed paths


class TestCreatePRStepJiraPrompt:
    """Verify the LLM prompt itself is JIRA-aware (not just post-processing)."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_jira_prompt_has_no_closes_syntax(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        """The LLM prompt for JIRA projects should not contain Closes #N."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(
            tmp_path,
            task=Task(id="48928", title="Improve output"),
            issue_number="48928",
            branch_name="feat/PROJ-48928-improve-output",
        )
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="PROJ",
                jira_base_url="https://jira.example.com",
            )
        )
        step = CreatePRStep()
        await step.execute(ctx)

        prompt_arg = mock_invoke.call_args[0][0]
        assert "Closes #48928" not in prompt_arg
        assert "PROJ-48928" in prompt_arg
        assert "jira.example.com/browse/PROJ-48928" in prompt_arg
        assert "JIRA: JIRA:" not in prompt_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_branch_fallback_title_strips_prefixes(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        """When task.title is missing, branch name should be cleaned for PR title."""
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
        adapter.get_task.side_effect = RuntimeError("unavailable")
        ctx = _make_ctx(
            tmp_path,
            adapter=adapter,
            task=None,
            issue_number="48928",
            branch_name="feat/PROJ-48928-security-logging",
        )
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="PROJ",
                jira_base_url="https://jira.example.com",
            )
        )
        step = CreatePRStep()
        await step.execute(ctx)

        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "feat/PROJ" not in title_arg
        assert "security logging" in title_arg.lower() or "security-logging" in title_arg.lower()


class TestCreatePRStepDiffContent:
    """Tests for diff content inclusion in PR body generation."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_prompt_includes_actual_diff_content(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        """The LLM prompt must include actual diff content, not just --stat."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/app.py | 10 ++++\n 1 file changed, 10 insertions(+)\n"),
            MagicMock(
                success=True,
                stdout="diff --git a/src/app.py b/src/app.py\n+def widget():\n+    return 'widget'\n",
            ),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(
            tmp_path,
            branch_name="feat/issue-42",
            task=Task(id="42", title="Add widget", body="We need a widget"),
        )
        step = CreatePRStep()
        await step.execute(ctx)

        prompt_arg = mock_invoke.call_args[0][0]
        assert "diff --git a/src/app.py b/src/app.py" in prompt_arg
        assert "+def widget():" in prompt_arg
        assert "Actual diff" in prompt_arg or "diff content" in prompt_arg.lower()

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_large_diff_is_truncated(self, mock_create_pr, mock_run, mock_invoke, _find, tmp_path: Path) -> None:
        """Large diffs should be truncated to avoid exceeding LLM context."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        large_diff = "diff --git a/src/file.py b/src/file.py\n" + ("+" + "x" * 100 + "\n") * 200
        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/file.py | 200 ++++\n"),
            MagicMock(success=True, stdout=large_diff),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42", task=Task(id="42", title="Add widget"))
        step = CreatePRStep()
        await step.execute(ctx)

        prompt_arg = mock_invoke.call_args[0][0]
        # Verify truncation: known prefix present, content beyond 8000 absent
        assert "+xxxx" in prompt_arg  # First 5 chars of diff should be present
        assert "x" * 8500 not in prompt_arg  # Content beyond 8000 should be excluded
        assert "truncated" in prompt_arg.lower() or "..." in prompt_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_prompt_truncates_issue_body(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        """Issue body should be truncated in the prompt to avoid over-weighting it."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        long_body = "x" * 1000
        mock_run.side_effect = [
            MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42", task=Task(id="42", title="Title", body=long_body))
        step = CreatePRStep()
        await step.execute(ctx)

        prompt_arg = mock_invoke.call_args[0][0]
        assert long_body not in prompt_arg
        assert len([line for line in prompt_arg.split("\n") if "x" * 500 in line]) == 0


class TestHandoffToReviewerStep:
    async def test_writes_handoff_and_succeeds(self, tmp_path: Path) -> None:
        from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=42)
        step = HandoffToReviewerStep()

        with (
            patch("sova.core.steps._handoff_helpers.write_handoff", new_callable=AsyncMock),
            patch("sova.core.steps._handoff_helpers.write_handoff_file") as mock_file,
            patch("sova.core.steps._handoff_helpers.notify"),
        ):
            result = await step.execute(ctx)

        assert result.success
        assert "Reviewer" in result.summary
        mock_file.assert_called_once()
        handoff = mock_file.call_args[0][1]
        assert handoff.status == "awaiting_action"
        assert len(handoff.next_actions) == 1
        assert handoff.next_actions[0].auto_execute is True
        assert handoff.next_actions[0].mode == "agent"

    async def test_auto_execute_disabled_by_config(self, tmp_path: Path) -> None:
        from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep

        config = ProjectConfig()
        config.pipeline.auto_handoff = False
        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=42, config=config)
        step = HandoffToReviewerStep()

        with (
            patch("sova.core.steps._handoff_helpers.write_handoff", new_callable=AsyncMock),
            patch("sova.core.steps._handoff_helpers.write_handoff_file") as mock_file,
            patch("sova.core.steps._handoff_helpers.notify"),
        ):
            result = await step.execute(ctx)

        assert result.success
        handoff = mock_file.call_args[0][1]
        assert handoff.next_actions[0].auto_execute is False

    async def test_can_skip_when_completed(self, tmp_path: Path) -> None:
        from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep

        ctx = _make_ctx(tmp_path, completed_steps=frozenset({"handoff_to_reviewer"}))
        step = HandoffToReviewerStep()
        assert await step.can_skip(ctx)

    @staticmethod
    async def _handoff_with_score(tmp_path: Path, score: int, *, gate_enabled: bool = True):
        """Run the step with a confidence score set and return the written handoff."""
        from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep

        config = ProjectConfig(confidence={"enabled": True, "gate_enabled": gate_enabled})
        ctx = _make_ctx(tmp_path, adapter=_mock_adapter(), pr_number=42, config=config)
        ctx.confidence_score = score

        with (
            patch("sova.core.steps._handoff_helpers.write_handoff", new_callable=AsyncMock),
            patch("sova.core.steps._handoff_helpers.write_handoff_file") as mock_file,
            patch("sova.core.steps._handoff_helpers.notify"),
        ):
            await HandoffToReviewerStep().execute(ctx)

        return mock_file.call_args[0][1]

    async def test_critical_score_forces_manual_review_when_gate_enabled(self, tmp_path: Path) -> None:
        handoff = await self._handoff_with_score(tmp_path, 40)

        assert len(handoff.next_actions) == 1
        review_action = handoff.next_actions[0]
        assert review_action.auto_execute is False
        assert "40/100" in review_action.description
        assert "critical risk" in review_action.description

    async def test_low_score_forces_manual_review_when_gate_enabled(self, tmp_path: Path) -> None:
        handoff = await self._handoff_with_score(tmp_path, 55)

        assert len(handoff.next_actions) == 1
        review_action = handoff.next_actions[0]
        assert review_action.auto_execute is False
        assert "55/100" in review_action.description
        assert "below review threshold" in review_action.description

    async def test_high_score_adds_integrate_action_when_gate_enabled(self, tmp_path: Path) -> None:
        handoff = await self._handoff_with_score(tmp_path, 90)

        assert {a.id for a in handoff.next_actions} == {"review", "integrate"}
        integrate_action = next(a for a in handoff.next_actions if a.id == "integrate")
        assert integrate_action.auto_execute is False
        # Review must not auto-spawn: it would clear the handoff file before the
        # integrate action the score just earned ever reaches the dashboard.
        review_action = next(a for a in handoff.next_actions if a.id == "review")
        assert review_action.auto_execute is False
        assert "90/100" in review_action.description

    async def test_mid_score_leaves_routing_unaffected_when_gate_enabled(self, tmp_path: Path) -> None:
        handoff = await self._handoff_with_score(tmp_path, 70)

        assert len(handoff.next_actions) == 1
        review_action = handoff.next_actions[0]
        assert review_action.auto_execute is True
        assert "70/100" in review_action.description

    async def test_score_ignored_when_gate_disabled(self, tmp_path: Path) -> None:
        handoff = await self._handoff_with_score(tmp_path, 10, gate_enabled=False)

        assert len(handoff.next_actions) == 1
        assert handoff.next_actions[0].auto_execute is True


class TestHandoffToUserStep:
    async def test_writes_handoff_with_integrate_actions(self, tmp_path: Path) -> None:
        from sova.core.steps.handoff_to_user import HandoffToUserStep

        ctx = _make_ctx(tmp_path, pr_number=42)
        step = HandoffToUserStep()

        with (
            patch("sova.core.steps._handoff_helpers.write_handoff", new_callable=AsyncMock),
            patch("sova.core.steps._handoff_helpers.write_handoff_file") as mock_file,
            patch("sova.core.steps._handoff_helpers.notify"),
        ):
            result = await step.execute(ctx)

        assert result.success
        handoff = mock_file.call_args[0][1]
        assert len(handoff.next_actions) == 1
        assert handoff.next_actions[0].id == "integrate"
        assert not handoff.next_actions[0].auto_execute


# ---------------------------------------------------------------------------
# CreatePRStep: branch name to title fallback
# ---------------------------------------------------------------------------


class TestCreatePRTitleFromBranch:
    def test_bare_issue_branch_returns_update_fallback(self) -> None:
        from sova.core.steps.create_pr import _title_from_branch

        assert _title_from_branch("feat/issue-48767") == "update"

    def test_strips_jira_key_prefix(self) -> None:
        from sova.core.steps.create_pr import _title_from_branch

        result = _title_from_branch("feat/PROJ-48767-security-logging")
        assert result == "security logging"

    def test_strips_fix_prefix(self) -> None:
        from sova.core.steps.create_pr import _title_from_branch

        assert _title_from_branch("fix/issue-42-login-bug") == "login bug"

    def test_bare_branch_returned_unchanged(self) -> None:
        from sova.core.steps.create_pr import _title_from_branch

        assert _title_from_branch("main") == "main"

    def test_prefix_only_returns_update_fallback(self) -> None:
        from sova.core.steps.create_pr import _title_from_branch

        assert _title_from_branch("feat/") == "update"

    def test_empty_branch_returns_update_fallback(self) -> None:
        from sova.core.steps.create_pr import _title_from_branch

        assert _title_from_branch("") == "update"


# ---------------------------------------------------------------------------
# CreatePRStep: task title fallback via adapter (#438)
# ---------------------------------------------------------------------------


class TestCreatePRStepTitleFallback:
    """When ctx.task is None but an issue number exists, CreatePRStep should
    fetch the issue title from the adapter instead of using 'update'."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_fetches_title_from_adapter_when_task_is_none(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.return_value = MagicMock(success=True, stdout="abc feat\n")
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(id="375", title="concurrent branch file-overlap prevention gate")
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-375", task=None)

        step = CreatePRStep()
        await step.execute(ctx)
        adapter.get_task.assert_awaited_once_with(ctx.issue_number)

        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "concurrent branch file-overlap prevention gate" in title_arg
        assert "update" not in title_arg
        assert ctx.task is not None, "adapter fetch should populate ctx.task"

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_falls_back_to_branch_name_when_adapter_fails(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.return_value = MagicMock(success=True, stdout="abc feat\n")
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        adapter.get_task.side_effect = RuntimeError("API error")
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/issue-375", task=None)

        step = CreatePRStep()
        await step.execute(ctx)
        adapter.get_task.assert_awaited_once_with(ctx.issue_number)

        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "update" in title_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_uses_task_title_directly_when_available(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        """When ctx.task is populated, no adapter call is needed."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.return_value = MagicMock(success=True, stdout="abc feat\n")
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(
            tmp_path,
            adapter=adapter,
            branch_name="feat/issue-42",
            task=Task(id="42", title="Add widget support"),
        )

        step = CreatePRStep()
        await step.execute(ctx)

        adapter.get_task.assert_not_called()
        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "add widget support" in title_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_no_adapter_call_for_issueless_run(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
        tmp_path: Path,
    ) -> None:
        """Issueless runs should use branch name, not try the adapter."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.return_value = MagicMock(success=True, stdout="abc feat\n")
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, issue_number="", branch_name="feat/sprint-plan", task=None)

        step = CreatePRStep()
        await step.execute(ctx)

        adapter.get_task.assert_not_called()
        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "sprint plan" in title_arg


# ---------------------------------------------------------------------------
# CreatePRStep: issueless runs
# ---------------------------------------------------------------------------


class TestCreatePRStepIssueless:
    async def test_issueless_pr_title_has_no_issue_ref(self, tmp_path: Path) -> None:
        """Issueless PRs should not include '#(none)' in the title."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(
            tmp_path,
            issue_number="",
            run_label="sprint-planning",
            branch_name="feat/sprint-plan",
            worktree_dir=tmp_path / "worktree",
        )
        ctx.task = None
        step = CreatePRStep()

        pr_info = MagicMock(number=99, url="https://github.com/test/repo/pull/99")

        with (
            patch("sova.core.steps.create_pr.run") as mock_run,
            patch("sova.core.steps.create_pr.invoke", new_callable=AsyncMock) as mock_invoke,
            patch("sova.core.steps.create_pr.git_ops.create_pr", new_callable=AsyncMock, return_value=pr_info),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout="sova/core/x.py\n"),  # git diff --name-only (scope inference)
                MagicMock(success=True, stdout="abc123 feat: plan\n"),
                MagicMock(success=True, stdout=" plan.py | 3 +++\n"),
                MagicMock(success=True, stdout="diff --git a/plan.py\n+planning\n"),
            ]
            mock_invoke.return_value = LLMResult(
                text="## Summary\n- Sprint planning", model="sonnet", cost_usd=Decimal("0.01")
            )
            result = await step.execute(ctx)

        assert result.success
        assert ctx.pr_number == 99
        assert "(#" not in result.summary or "99" in result.summary

    async def test_issueless_pr_body_has_no_closes(self, tmp_path: Path) -> None:
        """PR body for issueless runs should omit Closes line."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(tmp_path, issue_number="", run_label="sprint-planning")
        ctx.task = None
        body = CreatePRStep._build_fallback_body(ctx, "sprint plan", "abc123 feat: plan", "plan.py | 3 +++")
        assert "Closes" not in body
        assert "sprint plan" in body

    async def test_pr_body_includes_issue_excerpt(self, tmp_path: Path) -> None:
        """PR body should include a truncated issue body excerpt."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(
            tmp_path, branch_name="feat/issue-42", task=Task(id="42", title="Widget", body="Add widget support")
        )
        body = CreatePRStep._build_fallback_body(ctx, "Widget", "abc feat", "x.py | 3 +++")
        assert "## Context" in body
        assert "Add widget support" in body
        assert "Closes #42" in body

    async def test_pr_body_truncates_long_issue_body(self, tmp_path: Path) -> None:
        """Issue body excerpts longer than 500 chars should be truncated."""
        from sova.core.steps.create_pr import CreatePRStep

        long_body = "x" * 600
        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42", task=Task(id="42", title="Big", body=long_body))
        body = CreatePRStep._build_fallback_body(ctx, "Big", "abc feat", "x.py | 3 +++")
        assert "## Context" in body
        assert "..." in body
        assert long_body not in body  # should be truncated

    async def test_pr_body_omits_context_when_no_issue_body(self, tmp_path: Path) -> None:
        """PR body should not include Context section when issue has no body."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42", task=Task(id="42", title="Fix", body=""))
        body = CreatePRStep._build_fallback_body(ctx, "Fix", "abc feat", "x.py | 3 +++")
        assert "## Context" not in body
        assert "Closes #42" in body

    async def test_pr_body_omits_context_when_whitespace_only_body(self, tmp_path: Path) -> None:
        """PR body should not include Context section when issue body is whitespace-only."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(tmp_path, branch_name="feat/issue-42", task=Task(id="42", title="Fix", body="   "))
        body = CreatePRStep._build_fallback_body(ctx, "Fix", "abc feat", "x.py | 3 +++")
        assert "## Context" not in body
        assert "Closes #42" in body


class TestHandoffToReviewerRereview:
    """The address-review pipeline ends with HandoffToReviewerStep so the loop reaches
    an approving verdict on the new head without a human click."""

    async def test_address_review_variant_writes_rereview_handoff(self, tmp_path: Path) -> None:
        from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=42)
        ctx.task_run_id = 1
        ctx.pipeline_variant = "address_review"
        ctx.addressed_review_findings = [{"file": "a.py", "line": 1, "severity": 5, "description": "x"}]
        step = HandoffToReviewerStep()

        with (
            patch("sova.core.steps._handoff_helpers.write_handoff", new_callable=AsyncMock) as mock_db,
            patch("sova.core.steps._handoff_helpers.write_handoff_file") as mock_file,
            patch("sova.core.steps._handoff_helpers.notify"),
        ):
            result = await step.execute(ctx)

        assert result.success
        assert "re-review" in result.summary
        agent_handoff = mock_db.call_args[0][1]
        assert agent_handoff.phase == "address_review"
        assert agent_handoff.next_action == "review"
        assert agent_handoff.addressed_findings == ctx.addressed_review_findings
        handoff = mock_file.call_args[0][1]
        assert [a.id for a in handoff.next_actions] == ["review"]
        assert handoff.next_actions[0].auto_execute is True
        assert handoff.next_actions[0].args["role"] == "reviewer"

    async def test_developer_variant_is_unchanged(self, tmp_path: Path) -> None:
        from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep

        adapter = _mock_adapter()
        ctx = _make_ctx(tmp_path, adapter=adapter, pr_number=42)
        ctx.task_run_id = 1
        step = HandoffToReviewerStep()

        with (
            patch("sova.core.steps._handoff_helpers.write_handoff", new_callable=AsyncMock) as mock_db,
            patch("sova.core.steps._handoff_helpers.write_handoff_file"),
            patch("sova.core.steps._handoff_helpers.notify"),
        ):
            result = await step.execute(ctx)

        assert result.success
        assert mock_db.call_args[0][1].phase == "develop"
