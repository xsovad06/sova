"""Tests for complexity-based model routing in AssessStep."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.core.steps.assess import AssessStep
from sova.llm.complexity import ComplexityTier


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a mock ProjectConfig."""
    config = MagicMock()
    config.github_repo = "owner/repo"
    config.github_user = "testuser"
    config.base_branch = "main"
    config.agent.max_budget = Decimal("10")
    config.roles.researcher_model = None
    config.roles.triage_model = None
    config.llm.routing = {}
    return config


@pytest.fixture
def mock_adapter() -> AsyncMock:
    """Create a mock TaskAdapter."""
    adapter = AsyncMock()
    adapter.get_task = AsyncMock()
    adapter.get_state = AsyncMock(return_value=TaskState.RESEARCHED)
    return adapter


@pytest.fixture
def execution_context(mock_config: MagicMock, mock_adapter: AsyncMock, tmp_path: Path) -> ExecutionContext:
    """Create a test ExecutionContext."""
    return ExecutionContext(
        project_dir=tmp_path,
        config=mock_config,
        adapter=mock_adapter,
        issue_number="123",
        role="developer",
        run_label="test-run",
    )


class TestAssessStepComplexityRouting:
    """Tests for complexity assessment and model routing in AssessStep."""

    @pytest.mark.asyncio
    async def test_assess_computes_complexity_trivial(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """AssessStep computes complexity tier from task metadata."""
        task = Task(
            id="123",
            title="Fix typo in README",
            body="Simple typo fix",
            labels=["trivial"],
            state=TaskState.RESEARCHED,
        )
        mock_adapter.get_task.return_value = task

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            mock_find.return_value = None

            step = AssessStep()
            result = await step.execute(execution_context)

            assert result.success
            assert execution_context.complexity == ComplexityTier.TRIVIAL
            assert execution_context.task == task

    @pytest.mark.asyncio
    async def test_assess_computes_complexity_complex(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """AssessStep computes COMPLEX tier for architecture changes."""
        task = Task(
            id="123",
            title="Refactor authentication system",
            body="Complete architectural redesign of the auth module",
            labels=["refactor", "architecture"],
            state=TaskState.RESEARCHED,
        )
        mock_adapter.get_task.return_value = task

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            mock_find.return_value = None

            step = AssessStep()
            result = await step.execute(execution_context)

            assert result.success
            assert execution_context.complexity == ComplexityTier.COMPLEX

    @pytest.mark.asyncio
    async def test_assess_resolves_model_from_complexity(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """AssessStep resolves model based on complexity tier."""
        task = Task(
            id="123",
            title="Add new feature",
            body="Moderate complexity feature",
            labels=["moderate"],
            state=TaskState.RESEARCHED,
        )
        mock_adapter.get_task.return_value = task

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            mock_find.return_value = None

            step = AssessStep()
            result = await step.execute(execution_context)

            assert result.success
            assert execution_context.complexity == ComplexityTier.MODERATE
            # These assertions encode the current _DEFAULT_ROUTING policy.
            # Failures here signal an intentional routing default change, not a propagation bug.
            assert execution_context.resolved_model == "sonnet"
            assert execution_context.model_selection_reason == "complexity:moderate->sonnet"

    @pytest.mark.asyncio
    async def test_assess_respects_llm_config_override(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """AssessStep respects llm.routing config overrides."""
        execution_context.config.llm.routing = {"simple": "haiku"}
        task = Task(
            id="123",
            title="Small fix",
            body="Minor bug fix",
            labels=["simple"],
            state=TaskState.RESEARCHED,
        )
        mock_adapter.get_task.return_value = task

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            mock_find.return_value = None

            step = AssessStep()
            result = await step.execute(execution_context)

            assert result.success
            assert execution_context.complexity == ComplexityTier.SIMPLE
            assert execution_context.resolved_model == "haiku"
            assert execution_context.model_selection_reason == "config:override->haiku"

    @pytest.mark.asyncio
    async def test_assess_role_model_overrides_complexity(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """Role-specific model config takes priority over complexity routing."""
        execution_context.config.roles.researcher_model = "opus"
        execution_context.role = "researcher"
        task = Task(
            id="123",
            title="Simple task",
            body="Easy work",
            labels=["trivial"],
            state=TaskState.RESEARCHED,
        )
        mock_adapter.get_task.return_value = task

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            mock_find.return_value = None

            step = AssessStep()
            result = await step.execute(execution_context)

            assert result.success
            assert execution_context.complexity == ComplexityTier.TRIVIAL
            # Role model takes priority; "opus" encodes current _ROLE_MODEL_FIELDS default.
            assert execution_context.resolved_model == "opus"
            assert execution_context.model_selection_reason == "role:researcher->opus"

    @pytest.mark.asyncio
    async def test_assess_no_model_when_unmapped_role(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """Developer role (unmapped) falls back to complexity routing."""
        task = Task(
            id="123",
            title="Epic refactor",
            body="Multi-system breaking change",
            labels=["epic"],
            state=TaskState.RESEARCHED,
        )
        mock_adapter.get_task.return_value = task

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            mock_find.return_value = None

            step = AssessStep()
            result = await step.execute(execution_context)

            assert result.success
            assert execution_context.complexity == ComplexityTier.EPIC
            # Developer is not in _ROLE_MODEL_FIELDS, so falls back to complexity.
            # "opus" encodes the current _DEFAULT_ROUTING policy for EPIC tier.
            assert execution_context.resolved_model == "opus"
            assert execution_context.model_selection_reason == "complexity:epic->opus"

    @pytest.mark.asyncio
    async def test_assess_handles_empty_task_body(
        self, execution_context: ExecutionContext, mock_adapter: AsyncMock
    ) -> None:
        """AssessStep handles tasks with empty body gracefully."""
        task = Task(
            id="123",
            title="Fix bug",
            body="",
            labels=[],
            state=TaskState.RESEARCHED,
        )
        mock_adapter.get_task.return_value = task

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            mock_find.return_value = None

            step = AssessStep()
            result = await step.execute(execution_context)

            assert result.success
            # Empty body defaults to MODERATE
            assert execution_context.complexity == ComplexityTier.MODERATE
            assert execution_context.resolved_model == "sonnet"

    @pytest.mark.asyncio
    async def test_can_skip_reruns_when_routing_state_missing(self, execution_context: ExecutionContext) -> None:
        """AssessStep re-runs when routing state is missing on resume."""
        step = AssessStep()
        execution_context.completed_steps = frozenset({"assess"})
        execution_context.resolved_model = None
        execution_context.model_selection_reason = None

        # Should NOT skip when routing state is missing
        can_skip = await step.can_skip(execution_context)
        assert not can_skip

    @pytest.mark.asyncio
    async def test_can_skip_allows_when_routing_state_present(self, execution_context: ExecutionContext) -> None:
        """AssessStep skips when routing state exists on resume."""
        step = AssessStep()
        execution_context.completed_steps = frozenset({"assess"})
        execution_context.resolved_model = "sonnet"
        execution_context.model_selection_reason = "complexity:moderate->sonnet"

        # Should skip when routing state is present
        can_skip = await step.can_skip(execution_context)
        assert can_skip

    @pytest.mark.asyncio
    async def test_can_skip_allows_force_even_without_routing_state(self, execution_context: ExecutionContext) -> None:
        """AssessStep skips with --force even when routing state is missing."""
        step = AssessStep()
        execution_context.force = True
        execution_context.resolved_model = None

        # Should skip when force is set, regardless of routing state
        can_skip = await step.can_skip(execution_context)
        assert can_skip

    @pytest.mark.asyncio
    async def test_can_skip_allows_issueless_without_routing_state(self, execution_context: ExecutionContext) -> None:
        """AssessStep skips for issueless runs even without routing state."""
        step = AssessStep()
        execution_context.issue_number = ""
        execution_context.resolved_model = None

        # Should skip when no issue, regardless of routing state
        can_skip = await step.can_skip(execution_context)
        assert can_skip
