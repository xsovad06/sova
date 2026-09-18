"""Tests for AddressReviewStep's pending-docs queue drain (issue #1005).

`/integrate-pr` defers documentation and knowledge to
`.claude/agent-control/pending-docs.md` when it finds something stale but has
no push of its own to ride, on the assumption that the next `/address-pr`-shaped
run will drain it. The autonomous counterpart to `/address-pr` is
`AddressReviewStep`, which historically returned early whenever the reviewer
left zero actionable findings, before it ever reached the code that drains the
queue. A PR whose review is clean (the common case) would then never drain a
queue deferred onto it, leaving the content stranded indefinitely with no
error surfaced anywhere.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import TaskState
from sova.config.models import RolesConfig
from sova.core.context import ExecutionContext
from sova.core.steps.address_review import AddressReviewStep, _format_findings_prompt
from sova.llm.models import LLMResult


@pytest.fixture
def mock_config() -> MagicMock:
    config = MagicMock()
    config.github_repo = "owner/repo"
    config.github_user = "testuser"
    config.base_branch = "main"
    config.agent.max_budget = Decimal("10")
    config.agent.model = "claude-opus-5"
    config.agent.step_timeout = 600
    config.roles = RolesConfig()
    config.llm.routing = {}
    return config


@pytest.fixture
def mock_adapter() -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_task = AsyncMock()
    adapter.get_state = AsyncMock(return_value=TaskState.IN_REVIEW)
    adapter.get_pr_reviews = AsyncMock(return_value=[])
    adapter.remove_label = AsyncMock()
    return adapter


@pytest.fixture
def execution_context(mock_config: MagicMock, mock_adapter: AsyncMock, tmp_path: Path) -> ExecutionContext:
    return ExecutionContext(
        project_dir=tmp_path,
        config=mock_config,
        adapter=mock_adapter,
        issue_number="123",
        role="developer",
        run_label="test-run",
        pr_number=42,
    )


class TestFormatFindingsPromptWithoutFindings:
    """_format_findings_prompt must degrade gracefully with an empty findings list."""

    def test_empty_findings_omits_the_address_all_header(self) -> None:
        prompt = _format_findings_prompt([], pending_docs_path=Path("/tmp/pending-docs.md"))
        assert "Address ALL of the following code review findings" not in prompt

    def test_empty_findings_with_pending_docs_still_instructs_the_drain(self) -> None:
        pending = Path("/tmp/pending-docs.md")
        prompt = _format_findings_prompt([], pending_docs_path=pending)
        assert str(pending) in prompt
        assert "no code review findings" in prompt.lower()

    def test_empty_findings_without_pending_docs_produces_a_sane_prompt(self) -> None:
        # Not a reachable production path (execute() only calls this with an
        # empty list when pending docs exist), but the function itself must
        # not crash or emit contradictory instructions for it.
        prompt = _format_findings_prompt([], pending_docs_path=None)
        assert "no code review findings" in prompt.lower()

    def test_nonempty_findings_keeps_the_address_all_header(self) -> None:
        findings = [{"file": "a.py", "line": 1, "description": "bug", "severity": 8, "category": "correctness"}]
        prompt = _format_findings_prompt(findings, pending_docs_path=None)
        assert "Address ALL of the following code review findings" in prompt
        assert "a.py:1" in prompt


class TestAddressReviewStepPendingDocsQueue:
    """The step must not exit before checking the pending-docs queue."""

    @pytest.mark.asyncio
    async def test_zero_findings_and_no_pending_docs_returns_early(self, execution_context: ExecutionContext) -> None:
        """The original, correct behavior: nothing to do, no LLM call."""
        step = AddressReviewStep()
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(return_value=MagicMock(success=True, stdout="abc123")),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=[]),
            patch("sova.core.steps.address_review._load_review_findings_from_db", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_review_findings_by_issue", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_findings_from_github_reviews", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch("sova.core.steps.address_review.invoke_command", new=AsyncMock()) as mock_invoke,
        ):
            result = await step.execute(execution_context)

        assert result.success is True
        assert "No review findings to address" in result.summary
        mock_invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_findings_but_pending_docs_still_invokes_the_llm(
        self, execution_context: ExecutionContext
    ) -> None:
        """The bug: a clean review must not strand a queued documentation fix.

        /integrate-pr deferred documentation here specifically so it would
        ride the next agent's push. Returning early before checking the queue
        left it stranded forever whenever the review was clean.
        """
        pending_docs = execution_context.project_dir / ".claude" / "agent-control" / "pending-docs.md"
        pending_docs.parent.mkdir(parents=True)
        pending_docs.write_text("## From PR #41 (2026-09-11)\nUpdate AGENTS.md test count.\n")

        step = AddressReviewStep()
        fake_result = LLMResult(
            text="done",
            model="claude-opus-5",
            cost_usd=Decimal("0.01"),
            input_tokens=100,
            output_tokens=50,
        )
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(return_value=MagicMock(success=True, stdout="abc123")),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=[]),
            patch("sova.core.steps.address_review._load_review_findings_from_db", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_review_findings_by_issue", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_findings_from_github_reviews", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch(
                "sova.core.steps.address_review.invoke_command", new=AsyncMock(return_value=fake_result)
            ) as mock_invoke,
        ):
            result = await step.execute(execution_context)

        assert result.success is True
        mock_invoke.assert_called_once()
        prompt_arg = mock_invoke.call_args.args[0]
        assert str(pending_docs) in prompt_arg
        assert "drained pending documentation queue" in result.summary

    @pytest.mark.asyncio
    async def test_empty_pending_docs_file_is_treated_as_absent(self, execution_context: ExecutionContext) -> None:
        """A queue file that exists but holds only whitespace must not force an LLM call."""
        pending_docs = execution_context.project_dir / ".claude" / "agent-control" / "pending-docs.md"
        pending_docs.parent.mkdir(parents=True)
        pending_docs.write_text("   \n\n")

        step = AddressReviewStep()
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(return_value=MagicMock(success=True, stdout="abc123")),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=[]),
            patch("sova.core.steps.address_review._load_review_findings_from_db", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_review_findings_by_issue", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_findings_from_github_reviews", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch("sova.core.steps.address_review.invoke_command", new=AsyncMock()) as mock_invoke,
        ):
            result = await step.execute(execution_context)

        assert result.success is True
        assert "No review findings to address" in result.summary
        mock_invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_unreadable_pending_docs_file_does_not_crash_the_step(
        self, execution_context: ExecutionContext
    ) -> None:
        """A read failure (permissions, encoding) must degrade, not raise.

        The queue is treated as absent rather than propagating the exception,
        matching the module's defensive style around every other I/O source.
        """
        pending_docs = execution_context.project_dir / ".claude" / "agent-control" / "pending-docs.md"
        pending_docs.parent.mkdir(parents=True)
        pending_docs.write_text("## From PR #41\nsome content\n")

        step = AddressReviewStep()
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(return_value=MagicMock(success=True, stdout="abc123")),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=[]),
            patch("sova.core.steps.address_review._load_review_findings_from_db", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_review_findings_by_issue", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_findings_from_github_reviews", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch("sova.core.steps.address_review.invoke_command", new=AsyncMock()) as mock_invoke,
            patch.object(Path, "read_text", side_effect=OSError("permission denied")),
        ):
            result = await step.execute(execution_context)

        assert result.success is True
        assert "No review findings to address" in result.summary
        mock_invoke.assert_not_called()
        mock_invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_findings_present_uses_full_prompt_regardless_of_pending_docs(
        self, execution_context: ExecutionContext
    ) -> None:
        """The normal case is unaffected: real findings still drive the summary count."""
        step = AddressReviewStep()
        findings = [{"file": "a.py", "line": 1, "description": "bug", "severity": 8, "category": "correctness"}]
        fake_result = LLMResult(
            text="done",
            model="claude-opus-5",
            cost_usd=Decimal("0.02"),
            input_tokens=100,
            output_tokens=50,
        )
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(return_value=MagicMock(success=True, stdout="abc123")),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=findings),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch("sova.core.steps.address_review.invoke_command", new=AsyncMock(return_value=fake_result)),
        ):
            result = await step.execute(execution_context)

        assert result.success is True
        assert "Addressed 1 review finding" in result.summary


class TestAddressReviewStepClearsStaleVerdictLabel:
    """Findings-addressed success clears sova:revise/sova:block; other paths leave labels alone."""

    @pytest.mark.asyncio
    async def test_findings_addressed_success_removes_stale_labels(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        step = AddressReviewStep()
        findings = [{"file": "a.py", "line": 1, "description": "bug", "severity": 8, "category": "correctness"}]
        fake_result = LLMResult(
            text="done",
            model="claude-opus-5",
            cost_usd=Decimal("0.02"),
            input_tokens=100,
            output_tokens=50,
        )
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(return_value=MagicMock(success=True, stdout="abc123")),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=findings),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch("sova.core.steps.address_review.invoke_command", new=AsyncMock(return_value=fake_result)),
        ):
            result = await step.execute(execution_context)
            assert result.success is True
            mock_adapter.remove_label.assert_not_called()

            gate = await step.validate_output(execution_context)

        assert gate.passed is True
        mock_adapter.remove_label.assert_any_call(execution_context.issue_number, "sova:revise")
        mock_adapter.remove_label.assert_any_call(execution_context.issue_number, "sova:block")
        assert mock_adapter.remove_label.call_count == 2

    @pytest.mark.asyncio
    async def test_no_findings_no_op_does_not_touch_labels(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        step = AddressReviewStep()
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(return_value=MagicMock(success=True, stdout="abc123")),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=[]),
            patch("sova.core.steps.address_review._load_review_findings_from_db", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_review_findings_by_issue", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_findings_from_github_reviews", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch("sova.core.steps.address_review.invoke_command", new=AsyncMock()),
        ):
            result = await step.execute(execution_context)

        assert result.success is True
        mock_adapter.remove_label.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_docs_only_no_op_does_not_touch_labels(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """Drained pending docs with zero review findings must not clear the label."""
        pending_docs = execution_context.project_dir / ".claude" / "agent-control" / "pending-docs.md"
        pending_docs.parent.mkdir(parents=True)
        pending_docs.write_text("## From PR #41\nUpdate docs.\n")

        step = AddressReviewStep()
        fake_result = LLMResult(
            text="done",
            model="claude-opus-5",
            cost_usd=Decimal("0.01"),
            input_tokens=100,
            output_tokens=50,
        )
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(return_value=MagicMock(success=True, stdout="abc123")),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=[]),
            patch("sova.core.steps.address_review._load_review_findings_from_db", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_review_findings_by_issue", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_findings_from_github_reviews", new=AsyncMock(return_value=[])),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch("sova.core.steps.address_review.invoke_command", new=AsyncMock(return_value=fake_result)),
        ):
            result = await step.execute(execution_context)

        assert result.success is True
        mock_adapter.remove_label.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_llm_invocation_does_not_touch_labels(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        step = AddressReviewStep()
        findings = [{"file": "a.py", "line": 1, "description": "bug", "severity": 8, "category": "correctness"}]
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(return_value=MagicMock(success=True, stdout="abc123")),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=findings),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch(
                "sova.core.steps.address_review.invoke_command",
                new=AsyncMock(side_effect=RuntimeError("llm failed")),
            ),
        ):
            result = await step.execute(execution_context)

        assert result.success is False
        mock_adapter.remove_label.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_reports_success_but_no_changes_does_not_clear_labels(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """LLM reports success with no diff, no new commit, and no prior fix must not clear labels.

        Regression guard for the case where the LLM hallucinates a fix, or
        performs a no-op edit: `validate_output()` is the real correctness
        gate, and it must fail (empty diff, HEAD unchanged, no prior commits),
        so the stale sova:revise/sova:block label stays in place.
        """
        step = AddressReviewStep()
        findings = [{"file": "a.py", "line": 1, "description": "bug", "severity": 8, "category": "correctness"}]
        fake_result = LLMResult(
            text="done",
            model="claude-opus-5",
            cost_usd=Decimal("0.02"),
            input_tokens=100,
            output_tokens=50,
        )
        empty_result = MagicMock(success=True, stdout="")
        same_head_result = MagicMock(success=True, stdout="abc123")
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(
                    side_effect=[
                        same_head_result,  # rev-parse HEAD (before LLM, in execute())
                        empty_result,  # diff --stat HEAD (validate_output)
                        empty_result,  # diff --cached --stat (validate_output)
                        same_head_result,  # rev-parse HEAD (validate_output, unchanged)
                        empty_result,  # log base..HEAD (validate_output, no prior commits)
                    ]
                ),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=findings),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch("sova.core.steps.address_review.invoke_command", new=AsyncMock(return_value=fake_result)),
        ):
            result = await step.execute(execution_context)
            assert result.success is True

            gate = await step.validate_output(execution_context)

        assert gate.passed is False
        mock_adapter.remove_label.assert_not_called()

    @pytest.mark.asyncio
    async def test_prior_commits_already_ahead_of_base_does_not_clear_labels(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """A no-op run on a branch already ahead of base must not clear labels.

        On the address-review pipeline, base..HEAD is populated by the
        feature's own pre-existing commits before this run even starts, so
        `has_prior_commits` is trivially true regardless of what this cycle
        did. The gate may still pass (existing "already fixed" tolerance),
        but that alone is not evidence this run addressed anything, so the
        stale sova:revise/sova:block label must stay in place.
        """
        step = AddressReviewStep()
        findings = [{"file": "a.py", "line": 1, "description": "bug", "severity": 8, "category": "correctness"}]
        fake_result = LLMResult(
            text="done",
            model="claude-opus-5",
            cost_usd=Decimal("0.02"),
            input_tokens=100,
            output_tokens=50,
        )
        empty_result = MagicMock(success=True, stdout="")
        same_head_result = MagicMock(success=True, stdout="abc123")
        prior_commits_result = MagicMock(success=True, stdout="deadbee fix: something\n")
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(
                    side_effect=[
                        same_head_result,  # rev-parse HEAD (before LLM, in execute())
                        empty_result,  # diff --stat HEAD (validate_output)
                        empty_result,  # diff --cached --stat (validate_output)
                        same_head_result,  # rev-parse HEAD (validate_output, unchanged)
                        prior_commits_result,  # log base..HEAD (validate_output, already ahead)
                    ]
                ),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=findings),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch("sova.core.steps.address_review.invoke_command", new=AsyncMock(return_value=fake_result)),
        ):
            result = await step.execute(execution_context)
            assert result.success is True

            gate = await step.validate_output(execution_context)

        assert gate.passed is True
        mock_adapter.remove_label.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_stale_verdict_label_no_issue_number_skips_removal(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """Without an issue number there is nothing to un-label; the adapter must not be called."""
        execution_context.issue_number = ""
        step = AddressReviewStep()

        await step._clear_stale_verdict_label(execution_context)

        mock_adapter.remove_label.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_stale_verdict_label_no_adapter_skips_removal(
        self, execution_context: ExecutionContext
    ) -> None:
        """Without an adapter there is nothing to call the removal API on."""
        execution_context.adapter = None
        step = AddressReviewStep()

        # Must not raise AttributeError from a None adapter.
        await step._clear_stale_verdict_label(execution_context)

    @pytest.mark.asyncio
    async def test_label_removal_failure_is_non_fatal(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """A label API failure must not fail an otherwise-successful gate check."""
        mock_adapter.remove_label = AsyncMock(side_effect=RuntimeError("github api error"))
        step = AddressReviewStep()
        findings = [{"file": "a.py", "line": 1, "description": "bug", "severity": 8, "category": "correctness"}]
        fake_result = LLMResult(
            text="done",
            model="claude-opus-5",
            cost_usd=Decimal("0.02"),
            input_tokens=100,
            output_tokens=50,
        )
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(return_value=MagicMock(success=True, stdout="abc123")),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=findings),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch("sova.core.steps.address_review.invoke_command", new=AsyncMock(return_value=fake_result)),
        ):
            result = await step.execute(execution_context)
            assert result.success is True

            gate = await step.validate_output(execution_context)

        assert gate.passed is True

    @pytest.mark.asyncio
    async def test_first_label_failure_does_not_block_second_removal_attempt(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """A failure removing sova:revise must not prevent an attempt to remove sova:block."""
        mock_adapter.remove_label = AsyncMock(side_effect=[RuntimeError("github api error"), None])
        step = AddressReviewStep()

        await step._clear_stale_verdict_label(execution_context)

        mock_adapter.remove_label.assert_any_call(execution_context.issue_number, "sova:revise")
        mock_adapter.remove_label.assert_any_call(execution_context.issue_number, "sova:block")
        assert mock_adapter.remove_label.call_count == 2


class TestAddressReviewStepRecordsAddressedFindings:
    """The step carries the findings it addressed to the post-push summary and the handoff."""

    @pytest.mark.asyncio
    async def test_findings_stored_on_context(self, execution_context: ExecutionContext) -> None:
        step = AddressReviewStep()
        findings = [{"file": "a.py", "line": 1, "description": "bug", "severity": 8, "category": "correctness"}]
        fake_result = LLMResult(
            text="done", model="claude-opus-5", cost_usd=Decimal("0.02"), input_tokens=1, output_tokens=1
        )
        with (
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(return_value=MagicMock(success=True, stdout="abc123")),
            ),
            patch("sova.core.steps.address_review._load_review_findings", return_value=findings),
            patch("sova.core.steps.address_review._load_coderabbit_findings", new=AsyncMock(return_value=([], []))),
            patch("sova.core.steps.address_review.invoke_command", new=AsyncMock(return_value=fake_result)),
        ):
            await step.execute(execution_context)

        assert execution_context.addressed_review_findings == findings


class TestGithubReviewFindingsSkipAddressSummaries:
    """An address cycle's own summary review is never re-parsed as a finding."""

    @pytest.mark.asyncio
    async def test_addressed_marker_bodies_are_skipped(self, execution_context: ExecutionContext) -> None:
        import json

        from sova.core.steps.address_review import _load_findings_from_github_reviews

        execution_context.pr_number = 1063
        reviews = [
            {
                "state": "COMMENTED",
                "user": {"type": "User"},
                "body": (
                    "<!-- sova-addressed: sha=892372e -->\n## Address Review: Round 1\n\n"
                    "| # | Finding | Action |\n|---|---|---|\n| 1 | x | Addressed. |"
                ),
            },
            {
                "state": "COMMENTED",
                "user": {"type": "User"},
                # The header regex accepts the dashes without surrounding spaces,
                # which keeps this fixture clear of the no-double-dash invariant.
                "body": "[HIGH] Correctness --Off-by-one in loop\nLocation: a.py:12\nProblem: loop skips last item",
            },
        ]
        with (
            patch("sova.utils.gh.resolve_gh_env", new=AsyncMock(return_value={})),
            patch(
                "sova.core.steps.address_review.run",
                new=AsyncMock(return_value=MagicMock(success=True, stdout=json.dumps(reviews))),
            ),
        ):
            findings = await _load_findings_from_github_reviews(execution_context)

        assert len(findings) == 1
        assert findings[0]["file"] == "a.py"
