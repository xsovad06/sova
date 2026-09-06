"""Tests for model alias pinning and availability-based fallback.

Covers the fix for #826: generic aliases (opus, sonnet, haiku) resolved by
the CLI to unavailable versions on Vertex AI deployments. The fix pins
aliases to the configured agent.model when they share the same family, and
treats "model not available" errors as fallback-eligible.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.core.steps.assess import AssessStep
from sova.core.workflow import _is_billing_failure
from sova.llm.client import resolve_model
from sova.llm.complexity import ComplexityTier
from sova.llm.routing import (
    _get_model_family,
    _is_pinned_version,
    _pin_to_configured_model,
    route_model,
)

# ---------------------------------------------------------------------------
# Unit tests for routing helper functions
# ---------------------------------------------------------------------------


class TestModelFamilyDetection:
    def test_bare_alias(self) -> None:
        assert _get_model_family("opus") == "opus"
        assert _get_model_family("sonnet") == "sonnet"
        assert _get_model_family("haiku") == "haiku"

    def test_versioned_model_id(self) -> None:
        assert _get_model_family("claude-opus-4-6") == "opus"
        assert _get_model_family("claude-sonnet-4-6") == "sonnet"
        assert _get_model_family("claude-haiku-4-5") == "haiku"

    def test_unknown_model(self) -> None:
        assert _get_model_family("ollama/llama3") is None
        assert _get_model_family("gpt-4") is None

    def test_case_insensitive(self) -> None:
        assert _get_model_family("Claude-Opus-4-6") == "opus"
        assert _get_model_family("SONNET") == "sonnet"


class TestIsPinnedVersion:
    def test_bare_alias_not_pinned(self) -> None:
        assert not _is_pinned_version("opus")
        assert not _is_pinned_version("sonnet")
        assert not _is_pinned_version("haiku")

    def test_versioned_is_pinned(self) -> None:
        assert _is_pinned_version("claude-opus-4-6")
        assert _is_pinned_version("claude-sonnet-4-6")
        assert _is_pinned_version("ollama/llama3")


class TestPinToConfiguredModel:
    def test_pins_when_same_family(self) -> None:
        assert _pin_to_configured_model("opus", "claude-opus-4-6") == "claude-opus-4-6"

    def test_no_pin_when_different_family(self) -> None:
        assert _pin_to_configured_model("opus", "claude-sonnet-4-6") == "opus"

    def test_no_pin_when_agent_model_is_bare_alias(self) -> None:
        assert _pin_to_configured_model("opus", "opus") == "opus"

    def test_no_pin_when_agent_model_none(self) -> None:
        assert _pin_to_configured_model("opus", None) == "opus"

    def test_no_pin_when_agent_model_empty(self) -> None:
        assert _pin_to_configured_model("opus", "") == "opus"

    def test_pins_sonnet_family(self) -> None:
        assert _pin_to_configured_model("sonnet", "claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_pins_haiku_family(self) -> None:
        assert _pin_to_configured_model("haiku", "claude-haiku-4-5") == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Unit tests for route_model with pinning
# ---------------------------------------------------------------------------


class TestRouteModelPinning:
    def test_default_routing_pins_to_agent_model(self) -> None:
        llm_config = MagicMock()
        llm_config.routing = {}

        model, reason = route_model(
            ComplexityTier.COMPLEX,
            llm_config=llm_config,
            agent_model="claude-opus-4-6",
        )
        assert model == "claude-opus-4-6"
        assert "pinned" in reason

    def test_default_routing_no_pin_when_different_family(self) -> None:
        llm_config = MagicMock()
        llm_config.routing = {}

        model, reason = route_model(
            ComplexityTier.COMPLEX,
            llm_config=llm_config,
            agent_model="claude-sonnet-4-6",
        )
        assert model == "opus"
        assert "pinned" not in reason

    def test_default_routing_no_pin_when_agent_model_is_alias(self) -> None:
        llm_config = MagicMock()
        llm_config.routing = {}

        model, reason = route_model(
            ComplexityTier.COMPLEX,
            llm_config=llm_config,
            agent_model="opus",
        )
        assert model == "opus"
        assert "pinned" not in reason

    def test_config_override_pins_to_agent_model(self) -> None:
        llm_config = MagicMock()
        llm_config.routing = {"complex": "opus"}

        model, reason = route_model(
            ComplexityTier.COMPLEX,
            llm_config=llm_config,
            agent_model="claude-opus-4-6",
        )
        assert model == "claude-opus-4-6"
        assert "pinned" in reason

    def test_task_type_routing_bypasses_pinning(self) -> None:
        llm_config = MagicMock()
        llm_config.routing = {"validate": "sonnet"}

        model, reason = route_model(
            ComplexityTier.COMPLEX,
            task_type="validate",
            llm_config=llm_config,
            agent_model="claude-opus-4-6",
        )
        assert model == "sonnet"
        assert "task_type" in reason

    def test_no_agent_model_returns_alias(self) -> None:
        llm_config = MagicMock()
        llm_config.routing = {}

        model, reason = route_model(
            ComplexityTier.COMPLEX,
            llm_config=llm_config,
            agent_model=None,
        )
        assert model == "opus"

    def test_moderate_pins_sonnet(self) -> None:
        llm_config = MagicMock()
        llm_config.routing = {}

        model, reason = route_model(
            ComplexityTier.MODERATE,
            llm_config=llm_config,
            agent_model="claude-sonnet-4-6",
        )
        assert model == "claude-sonnet-4-6"
        assert "pinned" in reason

    def test_trivial_pins_haiku(self) -> None:
        llm_config = MagicMock()
        llm_config.routing = {}

        model, reason = route_model(
            ComplexityTier.TRIVIAL,
            llm_config=llm_config,
            agent_model="claude-haiku-4-5",
        )
        assert model == "claude-haiku-4-5"
        assert "pinned" in reason


# ---------------------------------------------------------------------------
# Integration test: resolve_model passes agent_model through
# ---------------------------------------------------------------------------


class TestResolveModelPinning:
    def test_resolve_model_passes_agent_model(self) -> None:
        roles_config = MagicMock()
        roles_config.researcher_model = None
        roles_config.triage_model = None
        roles_config.reviewer_model = None
        roles_config.developer_model = None
        roles_config.planner_model = None

        llm_config = MagicMock()
        llm_config.routing = {}

        result = resolve_model(
            role="developer",
            roles_config=roles_config,
            complexity=ComplexityTier.COMPLEX,
            llm_config=llm_config,
            agent_model="claude-opus-4-6",
        )
        assert result is not None
        model, reason = result
        assert model == "claude-opus-4-6"

    def test_resolve_model_role_override_ignores_agent_model(self) -> None:
        roles_config = MagicMock()
        roles_config.researcher_model = "sonnet"
        roles_config.triage_model = None
        roles_config.reviewer_model = None
        roles_config.developer_model = None
        roles_config.planner_model = None

        result = resolve_model(
            role="researcher",
            roles_config=roles_config,
            complexity=ComplexityTier.COMPLEX,
            agent_model="claude-opus-4-6",
        )
        assert result is not None
        model, _ = result
        assert model == "sonnet"


# ---------------------------------------------------------------------------
# Integration test: AssessStep end-to-end with pinned agent.model
# ---------------------------------------------------------------------------


@pytest.fixture
def pinned_config() -> MagicMock:
    config = MagicMock()
    config.github_repo = "owner/repo"
    config.github_user = "testuser"
    config.base_branch = "main"
    config.agent.max_budget = Decimal("10")
    config.agent.model = "claude-opus-4-6"
    config.roles.researcher_model = None
    config.roles.triage_model = None
    config.roles.reviewer_model = None
    config.roles.developer_model = None
    config.roles.planner_model = None
    config.llm.routing = {}
    return config


@pytest.fixture
def pinned_adapter() -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_task = AsyncMock()
    adapter.get_state = AsyncMock(return_value=TaskState.RESEARCHED)
    return adapter


@pytest.fixture
def pinned_context(pinned_config: MagicMock, pinned_adapter: AsyncMock, tmp_path: Path) -> ExecutionContext:
    return ExecutionContext(
        project_dir=tmp_path,
        config=pinned_config,
        adapter=pinned_adapter,
        issue_number="90",
        role="developer",
        run_label="test-run",
    )


class TestAssessStepPinning:
    @pytest.mark.asyncio
    async def test_complex_task_uses_pinned_opus(
        self, pinned_context: ExecutionContext, pinned_adapter: AsyncMock
    ) -> None:
        """When agent.model is claude-opus-4-6 and complexity is COMPLEX,
        the resolved model should be claude-opus-4-6 (not bare "opus")."""
        task = Task(
            id="90",
            title="Refactor auth system",
            body="Architectural redesign",
            labels=["complex"],
            state=TaskState.RESEARCHED,
        )
        pinned_adapter.get_task.return_value = task

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            mock_find.return_value = None

            step = AssessStep()
            result = await step.execute(pinned_context)

            assert result.success
            assert pinned_context.resolved_model == "claude-opus-4-6"
            assert "pinned" in pinned_context.model_selection_reason

    @pytest.mark.asyncio
    async def test_moderate_task_not_pinned_to_opus(
        self, pinned_context: ExecutionContext, pinned_adapter: AsyncMock
    ) -> None:
        """When agent.model is claude-opus-4-6 but complexity is MODERATE
        (routes to sonnet), the opus pin should NOT apply."""
        task = Task(
            id="90",
            title="Add new endpoint",
            body="Moderate complexity feature",
            labels=["moderate"],
            state=TaskState.RESEARCHED,
        )
        pinned_adapter.get_task.return_value = task

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            mock_find.return_value = None

            step = AssessStep()
            result = await step.execute(pinned_context)

            assert result.success
            assert pinned_context.resolved_model == "sonnet"
            assert "pinned" not in pinned_context.model_selection_reason


# ---------------------------------------------------------------------------
# Tests for model-not-available fallback
# ---------------------------------------------------------------------------


class TestModelNotAvailableFallback:
    def test_not_available_is_billing_failure(self) -> None:
        error = "The model claude-opus-5 is not available on your vertex deployment"
        assert _is_billing_failure(error)

    def test_model_not_available_variant(self) -> None:
        assert _is_billing_failure("model_not_available: claude-opus-5")

    def test_not_available_case_insensitive(self) -> None:
        assert _is_billing_failure("Model is Not Available on this region")

    def test_existing_billing_patterns_still_work(self) -> None:
        assert _is_billing_failure("budget_exhausted")
        assert _is_billing_failure("billing error")
        assert _is_billing_failure("rate_limit exceeded")
        assert _is_billing_failure("overloaded")
        assert _is_billing_failure("insufficient_quota")
        assert _is_billing_failure("HTTP 429 Too Many Requests")

    def test_normal_errors_not_billing(self) -> None:
        assert not _is_billing_failure("TypeError: cannot iterate")
        assert not _is_billing_failure("FileNotFoundError: no such file")
        assert not _is_billing_failure(None)
        assert not _is_billing_failure("")

    def test_loose_not_available_not_matched(self) -> None:
        assert not _is_billing_failure("feature not available in free tier")
