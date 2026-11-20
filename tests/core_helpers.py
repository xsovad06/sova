"""Helpers shared by the files split from the former tests/test_core.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from sova.adapters.base import Task, TaskState
from sova.config.models import (
    DEFAULT_COMMIT_SCOPES,
    DEFAULT_COMMIT_TYPES,
    CommitConfig,
    ProjectConfig,
)
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult


def _mock_adapter() -> AsyncMock:
    """Create a mock TaskAdapter for tests."""
    adapter = AsyncMock()
    adapter.get_state.return_value = TaskState.RESEARCHED
    adapter.get_task.return_value = Task(id="1", title="Test issue")
    return adapter


def _sova_commit_config() -> ProjectConfig:
    """A config with SOVA's own commit lists, which the loader gives only the SOVA repository.

    A bare ``ProjectConfig()`` carries the neutral defaults (conventional types, no scopes), so a test that
    asserts a ``type(scope):`` subject names the scope list it depends on.
    """
    return ProjectConfig(
        commit=CommitConfig(types=list(DEFAULT_COMMIT_TYPES), scopes=list(DEFAULT_COMMIT_SCOPES)),
    )


def _make_ctx(tmp_path: Path, **kwargs) -> ExecutionContext:
    """Create a test ExecutionContext with sensible defaults."""
    defaults = {
        "project_dir": tmp_path,
        "config": _sova_commit_config(),
        "adapter": _mock_adapter(),
        "issue_number": "42",
        "role": "developer",
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


# ---------------------------------------------------------------------------
# BaseStep / GateCheckResult / StepResult
# ---------------------------------------------------------------------------


class DummyStep(BaseStep):
    """A test step with configurable behavior."""

    name = "dummy"
    max_retries = 1

    def __init__(
        self,
        *,
        should_pass: bool = True,
        gate_pass: bool = True,
        skip: bool = False,
        name: str = "dummy",
    ) -> None:
        self.name = name
        self._should_pass = should_pass
        self._gate_pass = gate_pass
        self._skip = skip

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if self._should_pass:
            return StepResult(success=True, summary="Dummy passed")
        return StepResult(success=False, summary="Dummy failed", error="test error")

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        if self._gate_pass:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="Gate failed")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps or self._skip
