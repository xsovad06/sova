"""Tests for RefreshPrBodyStep: regenerates the PR body after address-review
changes the diff, without disturbing sections it did not generate (e.g.
Confidence Score)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.core.steps.create_pr import _title_from_branch
from sova.core.steps.refresh_pr_body import RefreshPrBodyStep
from sova.llm.models import LLMResult


def _mock_adapter() -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_state.return_value = TaskState.RESEARCHED
    adapter.get_task.return_value = Task(id="42", title="Fix the bug", body="We need a fix")
    return adapter


def _git_results(commit_log: str = "", diff_stat: str = "", diff_content: str = "") -> list[MagicMock]:
    """Results for the step's three git calls, in order: log, diff --stat, diff."""
    return [MagicMock(success=True, stdout=out) for out in (commit_log, diff_stat, diff_content)]


def _make_ctx(tmp_path: Path, **kwargs) -> ExecutionContext:
    defaults = {
        "project_dir": tmp_path,
        "config": ProjectConfig(),
        "adapter": _mock_adapter(),
        "issue_number": "42",
        "role": "developer",
        "pr_number": 99,
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


class TestRefreshPrBodyStepNoPR:
    async def test_no_pr_number_is_a_noop(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, pr_number=None)
        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        assert "No PR to refresh" in result.summary


class TestRefreshPrBodyStepSuccess:
    @patch("sova.core.steps.refresh_pr_body.update_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_refreshes_body_without_disturbing_confidence_score(
        self, mock_run, mock_invoke, mock_get_body, mock_update_body, tmp_path: Path
    ) -> None:
        mock_run.side_effect = _git_results(
            "abc123 fix: address review\n", " src/app.py | 2 ++\n", "diff --git a/src/app.py\n+change\n"
        )
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- new summary\n\n## Review guidance\n- look here\n\n## Test plan\n- ran tests",
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
        mock_get_body.return_value = (
            "## Summary\nold summary\n\n## Review guidance\nold guidance\n\n"
            "## Test plan\nold plan\n\nCloses #42\n\n## Confidence Score\n\n**85/100**\n"
        )

        ctx = _make_ctx(tmp_path)
        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        new_body = mock_update_body.call_args.kwargs["body"]
        assert "new summary" in new_body
        assert "look here" in new_body
        assert "ran tests" in new_body
        assert "old summary" not in new_body
        assert "Closes #42" in new_body
        assert "**85/100**" in new_body
        assert "## Confidence Score" in new_body

    @patch("sova.core.steps.refresh_pr_body.update_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_prompt_never_requests_a_title_block(
        self, mock_run, mock_invoke, mock_get_body, mock_update_body, tmp_path: Path
    ) -> None:
        """This step only refreshes the body, never the title: the title
        block build_prompt() offers must not even be requested here, rather
        than being requested and then stripped off every response."""
        from sova.core.steps._pr_body import TITLE_MARKER

        mock_run.side_effect = _git_results(
            "abc123 fix: address review\n", " src/app.py | 2 ++\n", "diff --git a/src/app.py\n+change\n"
        )
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- new summary\n\n## Review guidance\n- look here\n\n## Test plan\n- ran tests",
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
        mock_get_body.return_value = "## Summary\nold\n\n## Review guidance\nold\n\n## Test plan\nold\n\nCloses #42\n"

        ctx = _make_ctx(tmp_path)
        await RefreshPrBodyStep().execute(ctx)

        prompt = mock_invoke.call_args[0][0]
        assert TITLE_MARKER not in prompt

    @patch("sova.core.steps.refresh_pr_body.update_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_refetches_issue_body_for_up_to_date_criteria(
        self, mock_run, mock_invoke, mock_get_body, mock_update_body, tmp_path: Path
    ) -> None:
        """Acceptance criteria may have been edited since create_pr ran; the
        step must re-fetch the issue rather than trust a stale ctx.task."""
        mock_run.side_effect = _git_results(
            "abc123 fix: address review\n", " src/app.py | 2 ++\n", "diff --git a/src/app.py\n+change\n"
        )
        mock_invoke.return_value = LLMResult(
            text=(
                "## Summary\n- stuff\n\n## Review guidance\n- stuff\n\n## Test plan\n- stuff\n"
                "<!-- ACCEPTANCE_VERDICTS -->\n- [met] Updated criterion"
            ),
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
        mock_get_body.return_value = "## Summary\nold\n\n## Review guidance\nold\n\n## Test plan\nold\n\nCloses #42\n"

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Fix the bug", body="## Acceptance Criteria\n- [ ] Updated criterion\n"
        )
        ctx = _make_ctx(tmp_path, adapter=adapter)
        ctx.task = Task(id="42", title="stale cached title", body="stale body with no criteria")

        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        adapter.get_task.assert_awaited_once_with("42")
        new_body = mock_update_body.call_args.kwargs["body"]
        assert "Closes #42" in new_body

    @patch("sova.core.steps.refresh_pr_body.update_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_reduced_change_set_flips_closes_to_part_of(
        self, mock_run, mock_invoke, mock_get_body, mock_update_body, tmp_path: Path
    ) -> None:
        """An address-review round that *reduces* the change set can turn a
        previously-met criterion unmet; the refreshed body must flip from
        Closes to Part of, with no stale Closes reference left behind."""
        mock_run.side_effect = _git_results(
            "abc123 fix: address review\n", " src/app.py | 2 ++\n", "diff --git a/src/app.py\n+change\n"
        )
        mock_invoke.return_value = LLMResult(
            text=(
                "## Summary\n- stuff\n\n## Review guidance\n- stuff\n\n## Test plan\n- stuff\n"
                "<!-- ACCEPTANCE_VERDICTS -->\n- [met] A\n- [unmet] B"
            ),
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
        mock_get_body.return_value = "## Summary\nold\n\n## Review guidance\nold\n\n## Test plan\nold\n\nCloses #42\n"

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Fix the bug", body="## Acceptance Criteria\n- [ ] A\n- [ ] B\n"
        )
        ctx = _make_ctx(tmp_path, adapter=adapter)

        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        new_body = mock_update_body.call_args.kwargs["body"]
        assert "Part of #42" in new_body
        assert "Not done in this PR:" in new_body
        assert "- B" in new_body
        assert "Closes #42" not in new_body

    @patch("sova.core.steps.refresh_pr_body.update_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_heading_case_drift_still_leaves_exactly_one_closing_reference(
        self, mock_run, mock_invoke, mock_get_body, mock_update_body, tmp_path: Path
    ) -> None:
        """The closing decision lives in its own ``## Issue`` section, so even
        when the LLM's heading casing drifts from the existing body's (``##
        Test Plan`` vs ``## Test plan``) and the old section is orphaned
        rather than replaced, the closing reference itself is never
        duplicated or lost."""
        mock_run.side_effect = _git_results(
            "abc123 fix: address review\n", " src/app.py | 2 ++\n", "diff --git a/src/app.py\n+change\n"
        )
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- new\n\n## Review guidance\n- new\n\n## Test Plan\n- new plan",
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
        mock_get_body.return_value = (
            "## Summary\nold\n\n## Review guidance\nold\n\n## Test plan\nold plan\n\n"
            "## Issue\n\nCloses #42\n\n## Confidence Score\n\n**50/100**\n"
        )

        ctx = _make_ctx(tmp_path)
        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        new_body = mock_update_body.call_args.kwargs["body"]
        assert new_body.count("Closes #42") == 1
        assert "new plan" in new_body
        assert "**50/100**" in new_body

    @patch("sova.core.steps.refresh_pr_body.update_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_legacy_body_with_heading_drift_leaves_exactly_one_closing_reference(
        self, mock_run, mock_invoke, mock_get_body, mock_update_body, tmp_path: Path
    ) -> None:
        """A PR opened before the dedicated ``## Issue`` heading existed (or by
        the unchanged ``/pr`` command) carries its closing keyword as trailing
        text inside whatever section happens to be last, not under its own
        heading. Combined with heading-case drift that orphans that old
        section rather than replacing it, the stray keyword must still be
        removed rather than surviving next to the fresh "Part of #42"."""
        mock_run.side_effect = _git_results(
            "abc123 fix: address review\n", " src/app.py | 2 ++\n", "diff --git a/src/app.py\n+change\n"
        )
        mock_invoke.return_value = LLMResult(
            text=(
                "## Summary\n- new\n\n## Review guidance\n- new\n\n## Test Plan\n- new plan\n"
                "<!-- ACCEPTANCE_VERDICTS -->\n- [unmet] A"
            ),
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
        mock_get_body.return_value = "## Summary\nold\n\n## Test plan\nold plan\n\nCloses #42\n"

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(id="42", title="Fix the bug", body="## Acceptance Criteria\n- [ ] A\n")
        ctx = _make_ctx(tmp_path, adapter=adapter)

        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        new_body = mock_update_body.call_args.kwargs["body"]
        assert "Closes #42" not in new_body
        assert new_body.count("Part of #42") == 1


class TestRefreshPrBodyStepUnresolvedTask:
    """An unresolvable issue body must never flip an existing "Part of #N"
    decision back to an unconditional "Closes #N": that is the exact
    failure this step exists to prevent."""

    @patch("sova.core.steps.refresh_pr_body.update_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_no_cached_task_and_fetch_failure_preserves_existing_decision(
        self, mock_run, mock_invoke, mock_get_body, mock_update_body, tmp_path: Path
    ) -> None:
        mock_run.side_effect = _git_results("abc123 fix: thing\n", " src/app.py | 1 +\n", "diff --git a/x\n")
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- new\n\n## Review guidance\n- new\n\n## Test plan\n- new", model="sonnet"
        )
        mock_get_body.return_value = (
            "## Summary\nold\n\n## Test plan\nold\n\n## Issue\n\nPart of #42\n\nNot done in this PR:\n- A\n"
        )

        adapter = _mock_adapter()
        adapter.get_task.side_effect = RuntimeError("gh issue view failed")
        ctx = _make_ctx(tmp_path, adapter=adapter)
        ctx.task = None

        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        new_body = mock_update_body.call_args.kwargs["body"]
        assert "Part of #42" in new_body
        assert "Closes #42" not in new_body

    @patch("sova.core.steps.refresh_pr_body.update_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_fetch_failure_with_cached_task_still_computes_verdicts(
        self, mock_run, mock_invoke, mock_get_body, mock_update_body, tmp_path: Path
    ) -> None:
        """The cached task fallback still carries acceptance criteria, so the
        step is "resolved" and computes a fresh decision, rather than being
        treated the same as a total fetch failure."""
        mock_run.side_effect = _git_results("abc123 fix: thing\n", " src/app.py | 1 +\n", "diff --git a/x\n")
        mock_invoke.return_value = LLMResult(
            text=(
                "## Summary\n- new\n\n## Review guidance\n- new\n\n## Test plan\n- new\n"
                "<!-- ACCEPTANCE_VERDICTS -->\n- [unmet] A"
            ),
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
        mock_get_body.return_value = "## Summary\nold\n\n## Test plan\nold\n\n## Issue\n\nCloses #42\n"

        adapter = _mock_adapter()
        adapter.get_task.side_effect = RuntimeError("gh issue view failed")
        ctx = _make_ctx(tmp_path, adapter=adapter)
        ctx.task = Task(id="42", title="Cached title", body="## Acceptance Criteria\n- [ ] A\n")

        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        new_body = mock_update_body.call_args.kwargs["body"]
        assert "Part of #42" in new_body
        assert "Closes #42" not in new_body


class TestRefreshPrBodyStepGitFailure:
    """A git failure must never be read as "verified bad": the diff/log
    calls failing leaves no real evidence to describe the diff or verdict
    criteria from, so the step skips the whole refresh (never calling the
    LLM or touching the PR body) rather than overwrite a correct
    Summary/Review guidance/Test plan with text generated from the issue
    alone, or a false "Part of #N" derived from "(unavailable)"."""

    @patch("sova.core.steps.refresh_pr_body.update_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_all_git_calls_failing_skips_the_whole_refresh(
        self, mock_run, mock_invoke, mock_get_body, mock_update_body, tmp_path: Path
    ) -> None:
        mock_run.return_value = MagicMock(success=False, stdout="")

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Fix the bug", body="## Acceptance Criteria\n- [ ] A\n- [ ] B\n"
        )
        ctx = _make_ctx(tmp_path, adapter=adapter)

        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        assert "could not collect the branch diff" in result.summary
        mock_invoke.assert_not_called()
        mock_get_body.assert_not_awaited()
        mock_update_body.assert_not_awaited()


class TestRefreshPrBodyStepFailures:
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_llm_failure_is_non_fatal(self, mock_run, mock_invoke, tmp_path: Path) -> None:
        mock_run.side_effect = _git_results()
        mock_invoke.side_effect = RuntimeError("LLM timeout")

        ctx = _make_ctx(tmp_path)
        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        assert "skipped" in result.summary

    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_llm_non_runtime_error_is_also_non_fatal(self, mock_run, mock_invoke, tmp_path: Path) -> None:
        """invoke() routes through guard_prompt(), which can raise
        PromptInjectionError (a plain Exception, not a RuntimeError); this
        step's own contract is that a failure here must never abort the
        address-review round."""
        mock_run.side_effect = _git_results()
        mock_invoke.side_effect = ValueError("prompt injection risk too high")

        ctx = _make_ctx(tmp_path)
        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        assert "skipped" in result.summary

    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_pr_update_failure_is_non_fatal(self, mock_run, mock_invoke, mock_get_body, tmp_path: Path) -> None:
        mock_run.side_effect = _git_results()
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- a\n\n## Review guidance\n- b\n\n## Test plan\n- c", model="sonnet"
        )
        mock_get_body.side_effect = RuntimeError("gh pr view failed")

        ctx = _make_ctx(tmp_path)
        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        assert "skipped" in result.summary

    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_pr_update_non_runtime_error_is_also_non_fatal(
        self, mock_run, mock_invoke, mock_get_body, tmp_path: Path
    ) -> None:
        mock_run.side_effect = _git_results()
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- a\n\n## Review guidance\n- b\n\n## Test plan\n- c", model="sonnet"
        )
        mock_get_body.side_effect = ValueError("gh env resolution failed")

        ctx = _make_ctx(tmp_path)
        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        assert "skipped" in result.summary


class TestRefreshPrBodyStepNoSections:
    @patch("sova.core.steps.refresh_pr_body.update_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_heading_less_llm_output_skips_update_instead_of_a_silent_noop(
        self, mock_run, mock_invoke, mock_get_body, mock_update_body, tmp_path: Path
    ) -> None:
        """A heading-less LLM response (no issue to wrap it in ``## Issue``
        either, since this is an issue-less run) must not be reported as a
        successful refresh when it changed nothing: ``update_pr_body`` must
        never even be called."""
        mock_run.side_effect = _git_results("abc123 chore: cleanup\n")
        mock_invoke.return_value = LLMResult(
            text="Just some plain prose with no markdown headings at all.", model="sonnet"
        )

        ctx = _make_ctx(tmp_path, issue_number="")
        ctx.task = Task(id="", title="Ad-hoc cleanup", body="")
        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        assert "no sections" in result.summary.lower()
        mock_get_body.assert_not_awaited()
        mock_update_body.assert_not_awaited()


class TestRefreshPrBodyStepValidateOutput:
    async def test_validate_output_always_passes(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        gate = await RefreshPrBodyStep().validate_output(ctx)
        assert gate.passed


class TestRefreshPrBodyStepGitRanges:
    """The three-dot range is the merge-base diff for "git diff", but the
    symmetric difference for "git log". An address-review round usually
    happens after the base branch moved, so a three-dot log would list the
    base's own new commits as if this branch had made them. Both ranges use
    the remote-tracking ref: the address-review pipeline's rebase step
    fetches and rebases onto origin/{base} without ever moving the local
    ref, so a stale local base would otherwise pollute both ranges with the
    base's own commits since the PR's fork point."""

    @patch("sova.core.steps.refresh_pr_body.update_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_git_log_uses_two_dot_range_and_diff_uses_three(
        self, mock_run, mock_invoke, mock_get_body, _mock_update_body, tmp_path: Path
    ) -> None:
        mock_run.side_effect = _git_results("abc123 fix: thing\n", " src/app.py | 1 +\n", "diff --git a/src/app.py\n")
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- a\n\n## Review guidance\n- b\n\n## Test plan\n- c", model="sonnet"
        )
        mock_get_body.return_value = "## Summary\nold\n"

        ctx = _make_ctx(tmp_path)
        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        calls = [call.args for call in mock_run.call_args_list]
        assert calls[0][:3] == ("git", "log", f"origin/{ctx.base_branch}..HEAD")
        for args in calls[1:]:
            assert args[:2] == ("git", "diff")
            assert args[2] == f"origin/{ctx.base_branch}...HEAD"


class TestRefreshPrBodyStepIssueless:
    @patch("sova.core.steps.refresh_pr_body.update_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.get_pr_body", new_callable=AsyncMock)
    @patch("sova.core.steps.refresh_pr_body.invoke")
    @patch("sova.core.steps.refresh_pr_body.run")
    async def test_issueless_run_writes_no_closing_block(
        self, mock_run, mock_invoke, mock_get_body, mock_update_body, tmp_path: Path
    ) -> None:
        """With no issue there is nothing to close, so the refreshed body is
        the LLM's sections verbatim."""
        mock_run.side_effect = _git_results("abc123 chore: cleanup\n")
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- cleanup\n\n## Review guidance\n- b\n\n## Test plan\n- c", model="sonnet"
        )
        mock_get_body.return_value = "## Summary\nold\n"

        ctx = _make_ctx(tmp_path, issue_number="")
        ctx.task = Task(id="", title="Ad-hoc cleanup", body="")
        result = await RefreshPrBodyStep().execute(ctx)

        assert result.success
        new_body = mock_update_body.call_args.kwargs["body"]
        assert "cleanup" in new_body
        assert "Closes #" not in new_body
        assert "Part of #" not in new_body


class TestRefreshPrBodyStepResolveTask:
    async def test_refetch_failure_falls_back_to_cached_task(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        adapter.get_task.side_effect = RuntimeError("gh issue view failed")
        ctx = _make_ctx(tmp_path, adapter=adapter)
        ctx.task = Task(id="42", title="Cached title", body="Cached body")

        title, body, resolved = await RefreshPrBodyStep._resolve_task(ctx)

        assert (title, body) == ("Cached title", "Cached body")
        assert resolved is True

    async def test_no_task_at_all_falls_back_to_branch_name(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        adapter.get_task.side_effect = RuntimeError("gh issue view failed")
        ctx = _make_ctx(tmp_path, adapter=adapter, branch_name="feat/make-it-work")
        ctx.task = None

        title, body, resolved = await RefreshPrBodyStep._resolve_task(ctx)

        assert body == ""
        assert title == _title_from_branch("feat/make-it-work")
        assert resolved is False
