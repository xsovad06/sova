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
from sova.config.models import RolesConfig
from sova.core.context import ExecutionContext
from sova.core.steps.assess import AssessStep
from sova.core.workflow import _is_billing_failure
from sova.llm.client import resolve_model
from sova.llm.complexity import ComplexityTier
from sova.llm.routing import (
    _is_pinned_version,
    _pin_to_configured_model,
    get_model_family,
    resolve_extraction_model,
    route_model,
    task_type_model_compatible,
)

# ---------------------------------------------------------------------------
# Unit tests for routing helper functions
# ---------------------------------------------------------------------------


class TestModelFamilyDetection:
    @pytest.mark.parametrize(
        ("model", "family"),
        [
            pytest.param("opus", "opus", id="bare-alias-opus"),
            pytest.param("sonnet", "sonnet", id="bare-alias-sonnet"),
            pytest.param("haiku", "haiku", id="bare-alias-haiku"),
            pytest.param("claude-opus-4-6", "opus", id="versioned-opus"),
            pytest.param("claude-sonnet-4-6", "sonnet", id="versioned-sonnet"),
            pytest.param("claude-haiku-4-5", "haiku", id="versioned-haiku"),
            pytest.param("ollama/llama3", None, id="unknown-ollama"),
            pytest.param("gpt-4", None, id="unknown-gpt"),
            pytest.param("Claude-Opus-4-6", "opus", id="case-insensitive-versioned"),
            pytest.param("SONNET", "sonnet", id="case-insensitive-alias"),
        ],
    )
    def test_get_model_family(self, model: str, family: str | None) -> None:
        assert get_model_family(model) == family


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
    @pytest.mark.parametrize(
        ("alias", "agent_model", "expected"),
        [
            pytest.param("opus", "claude-opus-4-6", "claude-opus-4-6", id="pins-when-same-family"),
            pytest.param("opus", "claude-sonnet-4-6", "opus", id="no-pin-when-different-family"),
            pytest.param("opus", "opus", "opus", id="no-pin-when-agent-model-is-bare-alias"),
            pytest.param("opus", None, "opus", id="no-pin-when-agent-model-none"),
            pytest.param("opus", "", "opus", id="no-pin-when-agent-model-empty"),
            pytest.param("sonnet", "claude-sonnet-4-6", "claude-sonnet-4-6", id="pins-sonnet-family"),
            pytest.param("haiku", "claude-haiku-4-5", "claude-haiku-4-5", id="pins-haiku-family"),
        ],
    )
    def test_pin_to_configured_model(self, alias: str, agent_model: str | None, expected: str) -> None:
        assert _pin_to_configured_model(alias, agent_model) == expected


_FULL_CATALOG = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"]


class TestTaskTypeModelCompatible:
    """Issue #780: the runtime-compatibility check for a configured task-type route."""

    @pytest.mark.parametrize(
        ("model", "catalog", "expected"),
        [
            pytest.param(None, ["claude-opus-5"], True, id="none-model-is-compatible"),
            pytest.param("ollama/qwen3", None, True, id="no-cached-catalog-fails-open"),
            pytest.param("ollama/qwen3", [], True, id="empty-catalog-fails-open"),
            pytest.param("claude-sonnet-5", _FULL_CATALOG, True, id="exact-id-in-catalog-is-compatible"),
            pytest.param(
                "haiku", ["claude-haiku-4-5-20251001"], True, id="same-family-as-a-catalog-entry-is-compatible"
            ),
            pytest.param("haiku", ["claude-opus-5", "claude-sonnet-5"], False, id="different-family-is-incompatible"),
            pytest.param("ollama/qwen3:30b", _FULL_CATALOG, False, id="unrecognized-vendor-prefix-is-incompatible"),
            # Issue #780 review: "ollama/sonnet" contains "sonnet" but is not a Claude CLI id.
            pytest.param(
                "ollama/sonnet", _FULL_CATALOG, False, id="foreign-vendor-sharing-a-family-name-is-incompatible"
            ),
            pytest.param(
                "vertex_ai/claude-sonnet-4-5",
                ["vertex_ai/claude-sonnet-5"],
                True,
                id="same-vendor-prefix-and-family-is-compatible",
            ),
            # A model whose family this module doesn't yet know about (e.g. a new first-party family released
            # ahead of _MODEL_FAMILIES) must fall open rather than be judged incompatible.
            pytest.param(
                "claude-nova-1",
                ["claude-opus-5", "claude-sonnet-5"],
                True,
                id="unrecognized-family-falls-open-on-matching-vendor-prefix",
            ),
            pytest.param(
                "ollama/some-unknown-model",
                ["claude-opus-5", "claude-sonnet-5"],
                False,
                id="unrecognized-family-with-foreign-vendor-prefix-stays-incompatible",
            ),
        ],
    )
    def test_task_type_model_compatible(self, model: str | None, catalog: list[str] | None, expected: bool) -> None:
        assert task_type_model_compatible(model, catalog) is expected

    def test_dated_fable_id_is_compatible_with_curated_catalog(self) -> None:
        """Issue #780 review: a dated, pinned id in a recognized family (the
        configuration AGENTS.md recommends) must not be judged incompatible."""
        from sova.llm.models import CURATED_MODELS

        catalog = [m.id for m in CURATED_MODELS]
        assert task_type_model_compatible("claude-fable-5-1-20260301", catalog) is True

    def test_regression_every_curated_model_id_is_self_compatible(self) -> None:
        """A future CURATED_MODELS addition must stay compatible with itself:
        guards against a new family silently regressing this check (issue #780
        review)."""
        from sova.llm.models import CURATED_MODELS

        catalog = [m.id for m in CURATED_MODELS]
        for model_id in catalog:
            assert task_type_model_compatible(model_id, catalog) is True, model_id


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

    @pytest.mark.parametrize(
        "agent_model",
        [
            pytest.param("claude-sonnet-4-6", id="different-family"),
            pytest.param("opus", id="agent-model-is-alias"),
        ],
    )
    def test_default_routing_no_pin(self, agent_model: str) -> None:
        llm_config = MagicMock()
        llm_config.routing = {}

        model, reason = route_model(
            ComplexityTier.COMPLEX,
            llm_config=llm_config,
            agent_model=agent_model,
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
# Unit tests for resolve_extraction_model
# ---------------------------------------------------------------------------


class TestResolveExtractionModel:
    def test_no_config_returns_haiku(self) -> None:
        assert resolve_extraction_model(None) == "haiku"

    @pytest.mark.parametrize(
        ("routing", "expected"),
        [
            pytest.param({}, "haiku", id="no-override-returns-haiku"),
            pytest.param({"extraction": ""}, "haiku", id="empty-string-override-falls-back-to-haiku"),
            pytest.param({"extraction": "sonnet"}, "sonnet", id="override-returns-configured-model"),
        ],
    )
    def test_routing_override(self, routing: dict[str, str], expected: str) -> None:
        llm_config = MagicMock()
        llm_config.routing = routing

        assert resolve_extraction_model(llm_config) == expected


# ---------------------------------------------------------------------------
# Integration test: resolve_model passes agent_model through
# ---------------------------------------------------------------------------


class TestResolveModelPinning:
    def test_resolve_model_passes_agent_model(self) -> None:
        roles_config = RolesConfig()

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
        roles_config = RolesConfig(researcher_model="sonnet")

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
    config.roles = RolesConfig()
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
