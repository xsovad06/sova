"""Tests for researcher role bypass detection fixes (issue #832).

Three fixes:
1. FetchTaskStep validates preconditions when allowed_input_states is set
2. _adopt_task_run() raises on missing TaskRun
3. ResearchStep catches all exceptions except CancelledError
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.core.steps.fetch_task import FetchTaskStep
from sova.core.steps.research import ResearchStep
from sova.core.workflow import WorkflowEngine
from sova.db.models import TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for these tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _mock_adapter(state: TaskState = TaskState.TRIAGED) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_task.return_value = Task(
        id="42",
        title="Test issue",
        body="Some description",
        state=state,
    )
    return adapter


def _make_ctx(
    *,
    state: TaskState = TaskState.TRIAGED,
    allowed_input_states: frozenset[TaskState] | None = None,
    force: bool = False,
    **kwargs,
) -> ExecutionContext:
    defaults = {
        "project_dir": Path("/tmp/test"),
        "config": ProjectConfig(),
        "adapter": _mock_adapter(state),
        "issue_number": "42",
        "role": "researcher",
        "allowed_input_states": allowed_input_states,
        "force": force,
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


# ---------------------------------------------------------------------------
# FetchTaskStep precondition validation
# ---------------------------------------------------------------------------


class TestFetchTaskPreconditionValidation:
    """Test that FetchTaskStep validates state when allowed_input_states is set."""

    async def test_fetch_task_passes_when_state_valid(self) -> None:
        """FetchTaskStep succeeds when task state is in allowed_input_states."""
        ctx = _make_ctx(
            state=TaskState.TRIAGED,
            allowed_input_states=frozenset({TaskState.TRIAGED}),
        )
        step = FetchTaskStep()
        result = await step.execute(ctx)

        assert result.success
        assert ctx.task is not None
        assert ctx.task.state == TaskState.TRIAGED

    async def test_fetch_task_fails_when_state_invalid(self) -> None:
        """FetchTaskStep fails when task state not in allowed_input_states."""
        ctx = _make_ctx(
            state=TaskState.BACKLOG,
            allowed_input_states=frozenset({TaskState.TRIAGED}),
        )
        step = FetchTaskStep()
        result = await step.execute(ctx)

        assert not result.success
        assert "Precondition failed" in result.summary
        assert "backlog" in result.error.lower()
        assert "triaged" in result.error.lower()

    async def test_fetch_task_bypasses_validation_when_force(self) -> None:
        """FetchTaskStep skips state validation when ctx.force is True."""
        ctx = _make_ctx(
            state=TaskState.BACKLOG,
            allowed_input_states=frozenset({TaskState.TRIAGED}),
            force=True,
        )
        step = FetchTaskStep()
        result = await step.execute(ctx)

        assert result.success
        assert ctx.task is not None

    async def test_fetch_task_no_validation_when_allowed_states_none(self) -> None:
        """FetchTaskStep skips validation when allowed_input_states is None."""
        ctx = _make_ctx(
            state=TaskState.BACKLOG,
            allowed_input_states=None,
        )
        step = FetchTaskStep()
        result = await step.execute(ctx)

        assert result.success

    async def test_fetch_task_adapter_error_returns_failure(self) -> None:
        """FetchTaskStep returns failure with adapter error when adapter raises."""
        ctx = _make_ctx()
        ctx.adapter.get_task.side_effect = RuntimeError("API timeout")
        step = FetchTaskStep()
        result = await step.execute(ctx)

        assert not result.success
        assert result.summary == "Adapter error"
        assert "API timeout" in result.error

    async def test_fetch_task_state_transition_between_spawn_and_execute(self) -> None:
        """FetchTaskStep fails gracefully when issue changes state after spawn."""
        ctx = _make_ctx(
            state=TaskState.RESEARCHED,
            allowed_input_states=frozenset({TaskState.TRIAGED}),
        )
        step = FetchTaskStep()
        result = await step.execute(ctx)

        assert not result.success
        assert "researched" in result.error.lower()


# ---------------------------------------------------------------------------
# WorkflowEngine._adopt_task_run() validation
# ---------------------------------------------------------------------------


class TestAdoptTaskRunValidation:
    """Test that _adopt_task_run() raises on missing or terminal TaskRun."""

    async def test_adopt_task_run_succeeds_when_run_exists(self) -> None:
        """_adopt_task_run() succeeds when TaskRun exists and is not terminal."""
        async with await get_session() as session, session.begin():
            task_run = TaskRun(
                issue_number="42",
                role="researcher",
                status="pending",
                current_step="agent",
            )
            session.add(task_run)
            await session.flush()
            task_run_id = task_run.id

        ctx = _make_ctx(task_run_id=task_run_id)
        steps = [FetchTaskStep()]
        engine = WorkflowEngine(steps=steps, ctx=ctx)
        engine._task_run_id = task_run_id

        await engine._adopt_task_run()

        async with await get_session() as session:
            adopted_run = await session.get(TaskRun, task_run_id)
            assert adopted_run.status == "running"
            assert adopted_run.current_step is None

    async def test_adopt_task_run_raises_when_run_missing(self) -> None:
        """_adopt_task_run() raises RuntimeError when TaskRun does not exist."""
        ctx = _make_ctx(task_run_id=999)
        steps = [FetchTaskStep()]
        engine = WorkflowEngine(steps=steps, ctx=ctx)
        engine._task_run_id = 999

        with pytest.raises(RuntimeError, match="TaskRun 999 not found"):
            await engine._adopt_task_run()

    async def test_adopt_task_run_raises_when_run_terminal(self) -> None:
        """_adopt_task_run() raises RuntimeError when TaskRun is already terminal."""
        async with await get_session() as session, session.begin():
            task_run = TaskRun(
                issue_number="42",
                role="researcher",
                status="interrupted",
                current_step=None,
            )
            session.add(task_run)
            await session.flush()
            task_run_id = task_run.id

        ctx = _make_ctx(task_run_id=task_run_id)
        steps = [FetchTaskStep()]
        engine = WorkflowEngine(steps=steps, ctx=ctx)
        engine._task_run_id = task_run_id

        with pytest.raises(RuntimeError, match="already terminal.*interrupted"):
            await engine._adopt_task_run()


# ---------------------------------------------------------------------------
# ResearchStep exception handling
# ---------------------------------------------------------------------------


class TestResearchStepExceptionHandling:
    """Test that ResearchStep catches all exceptions except CancelledError."""

    async def test_research_step_catches_runtime_error(self) -> None:
        """ResearchStep returns failure when invoke_command raises RuntimeError."""
        ctx = _make_ctx()
        step = ResearchStep()

        with patch("sova.core.steps.research.invoke_command") as mock_invoke:
            mock_invoke.side_effect = RuntimeError("Command failed")
            result = await step.execute(ctx)

        assert not result.success
        assert result.summary == "Research failed"
        assert "Command failed" in result.error

    async def test_research_step_catches_value_error(self) -> None:
        """ResearchStep returns failure when invoke_command raises ValueError."""
        ctx = _make_ctx()
        step = ResearchStep()

        with patch("sova.core.steps.research.invoke_command") as mock_invoke:
            mock_invoke.side_effect = ValueError("Invalid argument")
            result = await step.execute(ctx)

        assert not result.success
        assert "Invalid argument" in result.error

    async def test_research_step_catches_key_error(self) -> None:
        """ResearchStep returns failure when invoke_command raises KeyError."""
        ctx = _make_ctx()
        step = ResearchStep()

        with patch("sova.core.steps.research.invoke_command") as mock_invoke:
            mock_invoke.side_effect = KeyError("missing_key")
            result = await step.execute(ctx)

        assert not result.success
        assert "missing_key" in result.error

    async def test_research_step_propagates_cancelled_error(self) -> None:
        """ResearchStep re-raises CancelledError without catching it."""
        ctx = _make_ctx()
        step = ResearchStep()

        with patch("sova.core.steps.research.invoke_command") as mock_invoke:
            mock_invoke.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await step.execute(ctx)

    async def test_research_step_succeeds_normally(self) -> None:
        """ResearchStep succeeds when invoke_command returns normally."""
        ctx = _make_ctx()
        step = ResearchStep()

        mock_result = MagicMock()
        mock_result.cost_usd = Decimal("0.05")
        mock_result.total_tokens = 1000

        with patch("sova.core.steps.research.invoke_command") as mock_invoke:
            mock_invoke.return_value = mock_result
            result = await step.execute(ctx)

        assert result.success
        assert "1000 tokens" in result.summary
        assert ctx.cost_usd == Decimal("0.05")
