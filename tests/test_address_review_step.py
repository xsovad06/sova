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
