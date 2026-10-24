"""Tests for sova.core.steps.base.resolve_fix_attempt_route: the escalation
decision ValidateStep and MonitorCIStep's fix loops share (issue #780)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
from sova.core.context import ExecutionContext, RoutedSpend
from sova.core.steps.base import resolve_fix_attempt_route


def _ctx(
    tmp_path: Path, *, routing: dict[str, str] | None = None, agent_model: str = "claude-opus-4-6"
) -> ExecutionContext:
    cfg = ProjectConfig(
        llm=LLMConfig(routing=routing or {}, resolve_tier_aliases=False),
        agent=AgentConfig(model=agent_model),
    )
    return ExecutionContext(
        project_dir=tmp_path,
        config=cfg,
        adapter=AsyncMock(),
        issue_number="1",
        role="developer",
        run_label="test",
    )


class TestResolveFixAttemptRoute:
    def test_attempt_one_carries_the_routed_task_type(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path, routing={"validate": "haiku"})

        model, task_type = resolve_fix_attempt_route(ctx, "validate", 1)

        assert model == "claude-opus-4-6"
        assert task_type == "validate"
        assert ctx.step_model_selection_reason is None

    def test_escalation_reason_set_only_when_attempt_one_actually_routed(self, tmp_path: Path) -> None:
        """ctx.routed_spend is add_usage()'s own evidence that attempt 1's
        result reported a model other than resolved_model, i.e. the route
        demonstrably took effect."""
        ctx = _ctx(tmp_path, routing={"validate": "haiku"})
        ctx.routed_spend["haiku-model-id"] = RoutedSpend(
            model="haiku-model-id", reason="task_type:validate fix attempt 1 routed to haiku"
        )

        model, task_type = resolve_fix_attempt_route(ctx, "validate", 2)

        assert model == "claude-opus-4-6"
        assert task_type is None
        assert ctx.step_model_selection_reason is not None
        assert "escalated to dev model" in ctx.step_model_selection_reason

    @pytest.mark.parametrize("routing", [{"validate": "haiku"}, {}])
    def test_no_escalation_reason_without_observed_routed_spend(self, tmp_path: Path, routing: dict[str, str]) -> None:
        """No routed_spend entry means attempt 1 never actually ran a
        different model, whether because no route was configured, a fallback
        suppressed it, the runtime-incompatible swap already fell back to
        the dev model on attempt 1, the runtime's honours_model_override is
        False (e.g. Codex), or the route pinned back to the dev model
        itself. None of these may claim a false escalation."""
        ctx = _ctx(tmp_path, routing=routing)

        model, task_type = resolve_fix_attempt_route(ctx, "validate", 2)

        assert model == "claude-opus-4-6"
        assert task_type is None
        assert ctx.step_model_selection_reason is None
