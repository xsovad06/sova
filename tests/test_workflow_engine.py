"""WorkflowEngine: execution, DB persistence, resume, run lifecycle and signals.

Split from tests/test_core.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
from sova.core.context import ExecutionContext
from sova.core.state import TaskStatus
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.core.workflow import WorkflowEngine, _is_billing_failure
from sova.db.models import CostRecord, FailureRecord, StepExecution, TaskRun
from sova.db.session import get_session
from sova.llm.errors import (
    BillingError,
    LLMInvocationError,
    ModelUnavailableError,
    RateLimitError,
)
from tests.conftest import closing_stand_in
from tests.core_helpers import DummyStep, _make_ctx, _sova_commit_config

# Every test in this module runs against the shared per-worker in-memory DB
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# WorkflowEngine
# ---------------------------------------------------------------------------


class TestWorkflowEngine:
    async def test_run_single_step_success(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.steps_completed == 1
        assert result.steps_failed == 0

    async def test_run_step_failure_records_error(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=False, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert result.steps_failed == 1

    async def test_run_gate_check_failure_pauses(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=False)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.PAUSED

    async def test_skipped_step(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        step1 = DummyStep(should_pass=True, gate_pass=True, skip=True)
        step2 = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step1, step2], ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.steps_completed == 1
        assert result.steps_skipped == 1

    async def test_extract_memory_step_is_recorded_as_skipped(self, tmp_path: Path) -> None:
        from sova.core.steps.extract_memory import ExtractMemoryStep

        engine = WorkflowEngine(steps=[ExtractMemoryStep()], ctx=_make_ctx(tmp_path))

        result = await engine.run()

        assert result.success
        assert result.steps_completed == 0
        assert [(r.step_name, r.status) for r in result.step_records] == [("extract_memory", "skipped")]

    async def test_multiple_steps_happy_path(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        steps = [
            DummyStep(should_pass=True, gate_pass=True),
            DummyStep(should_pass=True, gate_pass=True),
            DummyStep(should_pass=True, gate_pass=True),
        ]
        engine = WorkflowEngine(steps=steps, ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.steps_completed == 3
        assert result.final_status == TaskStatus.DONE

    async def test_budget_exceeded_pauses(self, tmp_path: Path) -> None:
        config = ProjectConfig(agent={"max_budget": Decimal("0.01")})
        ctx = _make_ctx(tmp_path, config=config)
        ctx.add_cost(Decimal("0.02"))

        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.PAUSED

    async def test_runaway_guard_step_count_pauses(self, tmp_path: Path) -> None:
        """Step-count guard trips even when cost stays at $0 the whole run.

        Named acceptance scenario for the R6 backstop: a provider that always
        reports cost_usd=0 never trips is_budget_exceeded, but the pipeline
        must still be stopped.
        """
        config = ProjectConfig(runaway={"max_run_steps": 1, "max_run_wall_clock_seconds": 0, "max_llm_calls": 0})
        ctx = _make_ctx(tmp_path, config=config)
        steps = [
            DummyStep(should_pass=True, gate_pass=True, name="step1"),
            DummyStep(should_pass=True, gate_pass=True, name="step2"),
        ]
        engine = WorkflowEngine(steps=steps, ctx=ctx)

        result = await engine.run()

        assert not ctx.is_budget_exceeded
        assert not result.success
        assert result.final_status == TaskStatus.PAUSED
        assert result.steps_completed == 1
        assert len(result.step_records) == 1

    async def test_runaway_guard_step_count_disabled_by_zero(self, tmp_path: Path) -> None:
        config = ProjectConfig(runaway={"max_run_steps": 0, "max_run_wall_clock_seconds": 0, "max_llm_calls": 0})
        ctx = _make_ctx(tmp_path, config=config)
        steps = [
            DummyStep(should_pass=True, gate_pass=True, name="step1"),
            DummyStep(should_pass=True, gate_pass=True, name="step2"),
        ]
        engine = WorkflowEngine(steps=steps, ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.steps_completed == 2

    async def test_runaway_guard_wall_clock_pauses(self, tmp_path: Path) -> None:
        import time

        config = ProjectConfig(runaway={"max_run_wall_clock_seconds": 100, "max_run_steps": 0, "max_llm_calls": 0})
        ctx = _make_ctx(tmp_path, config=config)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        # Simulate a run that started 500s ago (real time.monotonic() can't be
        # mocked globally without breaking asyncio's own event loop clock).
        engine._run_started_at = time.monotonic() - 500.0

        result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.PAUSED
        assert result.steps_completed == 0

        async with await get_session() as session:
            rows = (await session.execute(select(FailureRecord))).scalars().all()
            assert any(r.failure_type == "runaway_guard" and "wall clock" in r.message for r in rows)

    async def test_runaway_guard_wall_clock_scales_with_complexity(self, tmp_path: Path) -> None:
        """An EPIC-complexity run gets 2x the configured wall-clock ceiling."""
        import time

        from sova.llm.complexity import ComplexityTier

        config = ProjectConfig(runaway={"max_run_wall_clock_seconds": 100, "max_run_steps": 0, "max_llm_calls": 0})
        ctx = _make_ctx(tmp_path, config=config)
        ctx.complexity = ComplexityTier.EPIC
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        # 150s elapsed exceeds the flat 100s ceiling but not the EPIC-scaled 200s.
        engine._run_started_at = time.monotonic() - 150.0

        result = await engine.run()

        assert result.success

    async def test_runaway_guard_llm_call_count_pauses(self, tmp_path: Path) -> None:
        """LLM call count guard trips even when cost stays at $0 and step count is small.

        Named acceptance scenario for the R6 gap: retry/fix loops that burn LLM
        calls inside a single step's execute() without incrementing
        steps_completed are invisible to the step-count guard alone.
        """
        from sova.llm.client import _increment_call_counter

        class _LLMCallStep(DummyStep):
            """Simulates a step whose execute() makes LLM calls internally
            (e.g. MonitorCIStep's CI-fix loop) without incrementing steps_completed
            more than once."""

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                _increment_call_counter(2)
                return await super().execute(ctx)

        config = ProjectConfig(runaway={"max_llm_calls": 1, "max_run_steps": 0, "max_run_wall_clock_seconds": 0})
        ctx = _make_ctx(tmp_path, config=config)
        steps = [
            _LLMCallStep(should_pass=True, gate_pass=True, name="step1"),
            DummyStep(should_pass=True, gate_pass=True, name="step2"),
        ]
        engine = WorkflowEngine(steps=steps, ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.PAUSED
        assert result.steps_completed == 1

    async def test_step_retry_on_failure(self, tmp_path: Path) -> None:
        """A step with max_retries > 0 retries before failing."""
        ctx = _make_ctx(tmp_path)

        step = DummyStep(should_pass=False)
        step.max_retries = 1
        call_count = 0

        async def flaky_execute(ctx: ExecutionContext) -> StepResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return StepResult(success=False, summary="Flaky fail", error="transient")
            return StepResult(success=True, summary="Succeeded on retry")

        step.execute = flaky_execute

        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert result.success
        assert call_count == 2

    async def test_max_step_attempts_caps_total_attempts(self, tmp_path: Path) -> None:
        """runaway.max_step_attempts caps cumulative attempts even when the
        step's own max_retries would allow more, catching the case where a
        fallback-model chain would otherwise reset the per-model retry
        counter and multiply attempts unbounded. Exhaustion is a runaway
        trip (PAUSED, resumable), not an ordinary step failure (FAILED).
        """
        config = ProjectConfig(runaway={"max_step_attempts": 3})
        ctx = _make_ctx(tmp_path, config=config)

        step = DummyStep(should_pass=False)
        step.max_retries = 10

        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.PAUSED
        assert result.step_records[0].total_attempts == 3

        async with await get_session() as session:
            rows = (await session.execute(select(FailureRecord))).scalars().all()
            assert any(r.failure_type == "runaway_guard" and "attempts" in r.message for r in rows)

    async def test_max_step_attempts_disabled_by_zero(self, tmp_path: Path) -> None:
        config = ProjectConfig(runaway={"max_step_attempts": 0})
        ctx = _make_ctx(tmp_path, config=config)

        step = DummyStep(should_pass=False)
        step.max_retries = 2

        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert not result.success
        assert result.step_records[0].total_attempts == 3

    async def test_step_records_populated(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert len(result.step_records) == 1
        assert result.step_records[0].step_name == "dummy"
        assert result.step_records[0].status == "done"

    async def test_adopt_existing_task_run(self, tmp_path: Path) -> None:
        """When ctx.task_run_id is set, the engine should reuse it instead of creating a new one."""
        async with await get_session() as session:
            async with session.begin():
                existing = TaskRun(
                    issue_number="42",
                    role="developer",
                    status="running",
                    current_step="agent",
                    pid=99999,
                )
                session.add(existing)
                await session.flush()
                existing_id = existing.id

        ctx = _make_ctx(tmp_path, task_run_id=existing_id)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.task_run_id == existing_id

        async with await get_session() as session:
            task_run = await session.get(TaskRun, existing_id)
            assert task_run.pid == 99999  # PID preserved from dashboard
            assert task_run.current_step != "agent"

    async def test_adopt_does_not_create_extra_task_run(self, tmp_path: Path) -> None:
        """Adopting must not create a second TaskRun for the same issue."""
        async with await get_session() as session:
            async with session.begin():
                existing = TaskRun(
                    issue_number="42",
                    role="developer",
                    status="running",
                    current_step="agent",
                    pid=88888,
                )
                session.add(existing)
                await session.flush()
                existing_id = existing.id

        ctx = _make_ctx(tmp_path, task_run_id=existing_id)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        await engine.run()

        async with await get_session() as session:
            from sqlalchemy import func

            count = (await session.execute(select(func.count(TaskRun.id)).where(TaskRun.issue_number == "42"))).scalar()
            assert count == 1

    async def test_adopting_a_missing_run_hides_the_database_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The error names the database for diagnosis, but never with the password of its URL."""
        engine = WorkflowEngine(steps=[], ctx=_make_ctx(tmp_path, task_run_id=987654))
        engine._task_run_id = 987654
        session = await get_session()
        monkeypatch.setenv("SOVA_DATABASE_URL", "postgresql://sova:hunter2@db.example.com/sova")
        with (
            patch("sova.core.workflow.get_session", AsyncMock(return_value=session)),
            capture_logs() as logs,
            pytest.raises(RuntimeError, match="sova:\\*\\*\\*@db.example.com") as exc_info,
        ):
            await engine._adopt_task_run()

        assert "hunter2" not in str(exc_info.value)
        assert "hunter2" not in repr(logs)
        assert any("sova:***@db.example.com" in repr(entry) for entry in logs)

    async def test_adopting_a_missing_run_names_the_default_database_path_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no SOVA_DATABASE_URL the error names the project's own database file as is, even with an "@"."""
        project_dir = tmp_path / "user@host"
        project_dir.mkdir()
        engine = WorkflowEngine(steps=[], ctx=_make_ctx(project_dir, task_run_id=987654))
        engine._task_run_id = 987654
        session = await get_session()
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        with (
            patch("sova.core.workflow.get_session", AsyncMock(return_value=session)),
            pytest.raises(RuntimeError) as exc_info,
        ):
            await engine._adopt_task_run()

        assert str(project_dir / ".claude" / "sova.db") in str(exc_info.value)
        assert "***" not in str(exc_info.value)

    async def test_no_run_id_creates_new_task_run(self, tmp_path: Path) -> None:
        """Without task_run_id, the engine should create its own TaskRun (backward compat)."""
        ctx = _make_ctx(tmp_path)
        assert ctx.task_run_id is None

        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert result.success
        assert ctx.task_run_id is not None
        assert result.task_run_id == ctx.task_run_id

    async def test_db_writes_use_explicit_project_dir_not_ambient_contextvar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WorkflowEngine DB session calls must route via ctx.project_dir, never the ambient contextvar.

        Regression test for PR #1069 review: get_session() with no argument
        falls back to sova.config.context.get_project_dir(), so a call site
        that forgot to pass project_dir would silently write to whatever
        project the contextvar happens to be scoped to (e.g. stale state in
        multi-project dashboard mode). WorkflowEngine passes
        self._ctx.project_dir explicitly at every DB call site instead. This
        points the contextvar at a different project directory than
        ctx.project_dir and confirms the TaskRun still lands in
        ctx.project_dir's database, not the contextvar's.
        """
        from sova.config.context import clear_project_context, set_project_context
        from sova.db.session import _engines, init_db_for_project

        real_dir = tmp_path / "real-project"
        other_dir = tmp_path / "other-project"
        real_url = f"sqlite+aiosqlite:///{real_dir}/.claude/sova.db"
        other_url = f"sqlite+aiosqlite:///{other_dir}/.claude/sova.db"

        # The module-wide reset_db_rows fixture points every project_dir at
        # the same in-memory DB, which would mask the routing bug this test
        # exists to catch.
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        try:
            with patch("sova.db.session._backup_db"):
                await init_db_for_project(real_dir)
                await init_db_for_project(other_dir)

            ctx = _make_ctx(tmp_path, project_dir=real_dir)
            engine = WorkflowEngine(steps=[], ctx=ctx)

            set_project_context(other_dir, "other")
            try:
                task_run_id = await engine._create_task_run()
            finally:
                clear_project_context()

            async with await get_session(real_dir) as real_session:
                assert await real_session.get(TaskRun, task_run_id) is not None

            async with await get_session(other_dir) as other_session:
                assert await other_session.get(TaskRun, task_run_id) is None
        finally:
            for url in (real_url, other_url):
                if entry := _engines.pop(url, None):
                    await entry[0].dispose()

    async def test_create_step_execution_db_error_retries(self, tmp_path: Path) -> None:
        """When _create_step_execution raises, the engine retries and can still succeed."""
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        step.max_retries = 1
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        call_count = 0
        original_create = engine._create_step_execution

        async def failing_create(step_name: str, retry_count: int = 0) -> int:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("DB connection lost")
            return await original_create(step_name, retry_count=retry_count)

        engine._create_step_execution = failing_create
        result = await engine.run()

        assert result.success
        assert call_count == 2
        assert result.step_records[0].retries == 1

    async def test_create_step_execution_db_error_exhausts_retries(self, tmp_path: Path) -> None:
        """When _create_step_execution fails on every attempt, the step fails."""
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        step.max_retries = 0
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        async def always_failing_create(step_name: str, retry_count: int = 0) -> int:
            raise RuntimeError("DB permanently down")

        engine._create_step_execution = always_failing_create
        result = await engine.run()

        assert not result.success
        assert result.steps_failed == 1
        record = result.step_records[0]
        assert record.status == "failed"
        assert record.result is not None
        assert "DB error" in (record.result.summary or "")

    async def test_update_step_execution_db_error_non_fatal(self, tmp_path: Path) -> None:
        """When _update_step_execution raises, the step still succeeds."""
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        async def failing_update(step_exec_id: int, result: StepResult, elapsed_ms: int) -> None:
            raise RuntimeError("DB write failed")

        engine._update_step_execution = failing_update
        result = await engine.run()

        assert result.success
        assert result.steps_completed == 1

    async def test_update_step_execution_splits_mixed_runtime_and_provider_cost(self, tmp_path: Path) -> None:
        """A step that both dispatches to an unpriced runtime (Codex) and makes a
        separate priced provider call (e.g. DevelopStep's implementation-notes
        extraction) must split into two CostRecords by source, not merge the
        runtime's tokens into a record labelled with the provider's model
        (issue #1128 review, round 4)."""
        from sova.core.context import TokenUsage
        from sova.llm.models import CostSource

        ctx = _make_ctx(tmp_path)
        engine = WorkflowEngine(steps=[], ctx=ctx)
        engine._task_run_id = await engine._create_task_run()
        step_exec_id = await engine._create_step_execution("develop")

        ctx.runtime_model = "gpt-5-codex"
        ctx.runtime_model_reason = "runtime:codex reported model"
        ctx.runtime_cost_source = CostSource.UNKNOWN
        ctx.runtime_usage = TokenUsage(input_tokens=1000, output_tokens=500)
        ctx.resolved_model = "claude-sonnet-5"
        ctx.model_selection_reason = "complexity:high"
        ctx.cost_source = CostSource.PRICED

        result = StepResult(
            success=True,
            summary="Development completed",
            cost_usd=Decimal("0.05"),
            usage=TokenUsage(input_tokens=1200, output_tokens=600),
        )
        await engine._update_step_execution(step_exec_id, result, elapsed_ms=100)

        async with await get_session(ctx.project_dir) as session:
            rows = await session.execute(select(CostRecord).where(CostRecord.task_run_id == engine._task_run_id))
            records = sorted(rows.scalars().all(), key=lambda r: r.model)

        assert len(records) == 2
        codex_record = next(r for r in records if r.model == "gpt-5-codex")
        assert codex_record.cost_usd == Decimal("0")
        assert codex_record.cost_source == "unknown"
        assert codex_record.input_tokens == 1000
        assert codex_record.output_tokens == 500

        claude_record = next(r for r in records if r.model == "claude-sonnet-5")
        assert claude_record.cost_usd == Decimal("0.05")
        assert claude_record.cost_source == "priced"
        assert claude_record.input_tokens == 200
        assert claude_record.output_tokens == 100

    async def test_update_step_execution_attributes_fix_loop_dispatch_to_the_runtime(self, tmp_path: Path) -> None:
        """DevelopStep's real sequence on Codex, driven through add_usage(): the /develop
        dispatch, the priced implementation-notes call, then a fix-loop dispatch. The
        fix-loop's tokens belong on the runtime record and the provider record keeps
        the priced call's provenance, whichever call came last (PR #1169 review,
        thread on context.py add_usage)."""
        from sova.llm.models import CostSource, LLMResult

        ctx = _make_ctx(tmp_path)
        engine = WorkflowEngine(steps=[], ctx=ctx)
        engine._task_run_id = await engine._create_task_run()
        step_exec_id = await engine._create_step_execution("develop")
        ctx.runtime_model = "gpt-5-codex"
        ctx.runtime_model_reason = "runtime:codex reported model"
        ctx.resolved_model = "claude-sonnet-5"
        cost_before, usage_before = ctx.cost_usd, ctx.usage_snapshot()

        def codex(input_tokens: int, output_tokens: int) -> LLMResult:
            return LLMResult(
                text="",
                model="gpt-5-codex",
                cost_source=CostSource.UNKNOWN,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                runtime_name="codex",
            )

        ctx.add_usage(codex(1000, 500))
        ctx.add_usage(
            LLMResult(
                text="",
                model="claude-sonnet-5",
                cost_usd=Decimal("0.05"),
                cost_source=CostSource.PRICED,
                input_tokens=200,
                output_tokens=100,
                cache_creation_tokens=0,
            )
        )
        ctx.add_usage(codex(300, 50))

        result = StepResult(
            success=True,
            summary="Development completed",
            cost_usd=ctx.cost_usd - cost_before,
            usage=ctx.usage_snapshot() - usage_before,
        )
        await engine._update_step_execution(step_exec_id, result, elapsed_ms=100)

        async with await get_session(ctx.project_dir) as session:
            rows = await session.execute(select(CostRecord).where(CostRecord.task_run_id == engine._task_run_id))
            records = {r.model: r for r in rows.scalars().all()}

        assert set(records) == {"gpt-5-codex", "claude-sonnet-5"}
        codex_record = records["gpt-5-codex"]
        assert (codex_record.input_tokens, codex_record.output_tokens) == (1300, 550)
        assert codex_record.cost_usd == Decimal("0")
        assert codex_record.cost_source == "unknown"
        claude_record = records["claude-sonnet-5"]
        assert (claude_record.input_tokens, claude_record.output_tokens) == (200, 100)
        assert claude_record.cost_usd == Decimal("0.05")
        assert claude_record.cost_source == "priced"

    async def test_update_step_execution_splits_routed_fix_attempt_by_model(self, tmp_path: Path) -> None:
        """Issue #780 review: a routed cheap attempt and the dev-model attempt that
        escalated past it are written as one CostRecord per model that ran, each
        with its own reason, never merged under resolved_model."""
        from sova.llm.models import CostSource, LLMResult

        ctx = _make_ctx(tmp_path)
        engine = WorkflowEngine(steps=[], ctx=ctx)
        engine._task_run_id = await engine._create_task_run()
        step_exec_id = await engine._create_step_execution("validate")
        ctx.resolved_model = "claude-opus-5"
        ctx.model_selection_reason = "complexity:high"
        ctx.step_model_selection_reason = (
            "task_type:validate fix attempt 1 failed, escalated to dev model claude-opus-5"
        )
        cost_before, usage_before = ctx.cost_usd, ctx.usage_snapshot()

        reason = "task_type:validate fix attempt 1 routed to claude-haiku-4-5"
        ctx.add_usage(
            LLMResult(
                text="",
                model="claude-haiku-4-5",
                cost_usd=Decimal("0.01"),
                cost_source=CostSource.PRICED,
                input_tokens=100,
                output_tokens=40,
                routed_task_type_model="claude-haiku-4-5",
            ),
            routed_reason=reason,
        )
        ctx.add_usage(
            LLMResult(
                text="",
                model="claude-opus-5",
                cost_usd=Decimal("0.20"),
                cost_source=CostSource.PRICED,
                input_tokens=300,
                output_tokens=90,
            ),
            routed_reason=None,
        )

        result = StepResult(
            success=True,
            summary="Validation passed",
            cost_usd=ctx.cost_usd - cost_before,
            usage=ctx.usage_snapshot() - usage_before,
        )
        await engine._update_step_execution(step_exec_id, result, elapsed_ms=100)

        async with await get_session(ctx.project_dir) as session:
            rows = await session.execute(select(CostRecord).where(CostRecord.task_run_id == engine._task_run_id))
            records = {r.model: r for r in rows.scalars().all()}

        assert set(records) == {"claude-haiku-4-5", "claude-opus-5"}
        routed = records["claude-haiku-4-5"]
        assert (routed.input_tokens, routed.output_tokens) == (100, 40)
        assert routed.cost_usd == Decimal("0.01")
        assert routed.model_selection_reason == reason
        dev = records["claude-opus-5"]
        assert (dev.input_tokens, dev.output_tokens) == (300, 90)
        assert dev.cost_usd == Decimal("0.20")
        assert "escalated to dev model" in dev.model_selection_reason

    async def test_update_step_execution_successful_routed_attempt_writes_only_the_routed_record(
        self, tmp_path: Path
    ) -> None:
        """A routed attempt that fixed the tree on its own is labelled with the routed
        model, not resolved_model, and leaves no empty dev-model record behind."""
        from sova.llm.models import CostSource, LLMResult

        ctx = _make_ctx(tmp_path)
        engine = WorkflowEngine(steps=[], ctx=ctx)
        engine._task_run_id = await engine._create_task_run()
        step_exec_id = await engine._create_step_execution("monitor_ci")
        ctx.resolved_model = "claude-opus-5"
        cost_before, usage_before = ctx.cost_usd, ctx.usage_snapshot()

        ctx.add_usage(
            LLMResult(
                text="",
                model="claude-haiku-4-5",
                cost_usd=Decimal("0.01"),
                cost_source=CostSource.PRICED,
                input_tokens=100,
                output_tokens=40,
                routed_task_type_model="claude-haiku-4-5",
            ),
            routed_reason="task_type:monitor_ci fix attempt 1 routed to claude-haiku-4-5",
        )

        result = StepResult(
            success=True,
            summary="CI fixed",
            cost_usd=ctx.cost_usd - cost_before,
            usage=ctx.usage_snapshot() - usage_before,
        )
        await engine._update_step_execution(step_exec_id, result, elapsed_ms=100)

        async with await get_session(ctx.project_dir) as session:
            rows = await session.execute(select(CostRecord).where(CostRecord.task_run_id == engine._task_run_id))
            records = rows.scalars().all()

        assert [r.model for r in records] == ["claude-haiku-4-5"]
        assert records[0].cost_usd == Decimal("0.01")

    def test_add_usage_keeps_a_routed_result_echoing_resolved_model_on_the_provider_path(self, tmp_path: Path) -> None:
        """A route that resolved to the dev model itself must not duplicate that model
        into a second record."""
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path)
        ctx.resolved_model = "claude-opus-5"
        ctx.add_usage(
            LLMResult(text="", model="claude-opus-5", cost_usd=Decimal("0.02"), input_tokens=10),
            routed_reason="task_type:validate fix attempt 1 routed to claude-opus-5",
        )

        assert ctx.routed_spend == {}
        assert ctx.cost_usd == Decimal("0.02")

    async def test_update_step_execution_runtime_only_step_writes_one_runtime_record(self, tmp_path: Path) -> None:
        """A step whose only call was a Codex dispatch writes exactly one record,
        attributed to the runtime, even with no tokens reported (a dispatch killed
        before its terminal event): an unknown cost must still be recorded, not
        dropped as free."""
        from sova.core.context import TokenUsage
        from sova.llm.models import CostSource

        ctx = _make_ctx(tmp_path)
        engine = WorkflowEngine(steps=[], ctx=ctx)
        engine._task_run_id = await engine._create_task_run()
        step_exec_id = await engine._create_step_execution("self_review")
        ctx.runtime_model = "codex"
        ctx.runtime_model_reason = "runtime:codex reported no model"
        ctx.runtime_cost_source = CostSource.UNKNOWN
        ctx.runtime_usage = TokenUsage()
        ctx.resolved_model = "claude-sonnet-5"

        result = StepResult(success=True, summary="Reviewed", cost_usd=Decimal("0"), usage=TokenUsage())
        await engine._update_step_execution(step_exec_id, result, elapsed_ms=100)

        async with await get_session(ctx.project_dir) as session:
            rows = await session.execute(select(CostRecord).where(CostRecord.task_run_id == engine._task_run_id))
            records = rows.scalars().all()

        assert len(records) == 1
        assert records[0].model == "codex"
        assert records[0].cost_source == "unknown"
        assert records[0].model_selection_reason == "runtime:codex reported no model"

    async def test_awaiting_approval_pauses_pipeline(self, tmp_path: Path) -> None:
        """A step returning awaiting_approval=True pauses the pipeline."""
        ctx = _make_ctx(tmp_path)

        class ApprovalStep(BaseStep):
            name = "needs_approval"

            async def execute(self, ctx_inner: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="Needs human approval", awaiting_approval=True)

            async def validate_output(self, ctx_inner: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step1 = ApprovalStep()
        step2 = DummyStep(should_pass=True, gate_pass=True, name="after_approval")
        engine = WorkflowEngine(steps=[step1, step2], ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.AWAITING_APPROVAL
        assert result.steps_completed == 1
        # Second step should NOT have run
        assert len(result.step_records) == 1
        assert result.step_records[0].step_name == "needs_approval"

    async def test_awaiting_approval_default_false_no_pause(self, tmp_path: Path) -> None:
        """Steps returning default awaiting_approval=False do not pause."""
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.final_status == TaskStatus.DONE

    async def test_awaiting_approval_with_handoff_actions(self, tmp_path: Path) -> None:
        """A step returning handoff_actions writes a handoff file."""
        from sova.ipc.handoff import HandoffAction

        ctx = _make_ctx(tmp_path)
        actions = [
            HandoffAction(
                id="approve_spec",
                label="Approve",
                description="Approve the spec",
                style="approve",
                auto_execute=False,
            ),
        ]

        class ApprovalWithActionsStep(BaseStep):
            name = "spec"

            async def execute(self, ctx_inner: ExecutionContext) -> StepResult:
                return StepResult(
                    success=True,
                    summary="Spec ready for approval",
                    awaiting_approval=True,
                    handoff_actions=actions,
                )

            async def validate_output(self, ctx_inner: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step = ApprovalWithActionsStep()
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        with patch("sova.ipc.handoff.write_handoff_file") as mock_write:
            result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.AWAITING_APPROVAL
        mock_write.assert_called_once()
        written_handoff = mock_write.call_args[0][1]
        assert written_handoff.status == "awaiting_action"
        assert len(written_handoff.next_actions) == 1
        assert written_handoff.next_actions[0].id == "approve_spec"

    async def test_step_hard_timeout(self, tmp_path: Path) -> None:
        """Steps exceeding the hard timeout are terminated with a failure."""

        class SlowStep(BaseStep):
            name = "slow"
            max_retries = 0

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                await asyncio.sleep(999)
                return StepResult(success=True, summary="should not reach here")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        config = ProjectConfig(agent={"step_timeout_normal": 1})
        ctx = _make_ctx(tmp_path, config=config)
        step = SlowStep()
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert result.steps_failed == 1
        assert result.error == "step_hard_timeout"

    async def test_billing_failure_triggers_model_fallback(self, tmp_path: Path) -> None:
        """A billing failure should switch to the next fallback model and retry."""
        config = ProjectConfig(
            agent={"model": "opus", "fallback_models": ["sonnet"]},
            llm={"engine_owned_fallback": True},
        )
        ctx = _make_ctx(tmp_path, config=config)

        call_models: list[str | None] = []

        async def billing_then_succeed(ctx_inner: ExecutionContext) -> StepResult:
            call_models.append(ctx_inner.resolved_model)
            if ctx_inner.resolved_model != "sonnet":
                return StepResult(success=False, summary="Billing fail", error="budget_exhausted")
            return StepResult(success=True, summary="Passed with fallback")

        step = DummyStep(should_pass=True, gate_pass=True)
        step.max_retries = 0
        step.execute = billing_then_succeed
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert result.success
        assert "sonnet" in call_models

    async def test_all_fallback_models_exhausted(self, tmp_path: Path) -> None:
        """When all fallback models fail with billing errors, step fails."""
        config = ProjectConfig(
            agent={"model": "opus", "fallback_models": ["sonnet", "haiku"]},
            llm={"engine_owned_fallback": True},
        )
        ctx = _make_ctx(tmp_path, config=config)

        step = DummyStep(should_pass=False, gate_pass=True)
        step.max_retries = 0
        step.execute = AsyncMock(
            return_value=StepResult(success=False, summary="Billing fail", error="rate_limit exceeded")
        )
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert not result.success
        assert step.execute.await_count == 3  # opus + sonnet + haiku

    async def test_configuration_failure_is_never_retried_or_fallback(self, tmp_path: Path) -> None:
        """A static configuration fault (e.g. CodexRuntime's budget-cap refusal) must
        cost exactly one attempt and never walk agent.fallback_models, unlike a billing
        failure (test_billing_failure_triggers_model_fallback/test_all_fallback_models_exhausted
        above): no retry or fallback-model hop changes whether the fault still applies."""
        config = ProjectConfig(
            agent={"model": "opus", "fallback_models": ["sonnet"]},
            llm={"engine_owned_fallback": True},
        )
        ctx = _make_ctx(tmp_path, config=config)

        step = DummyStep(should_pass=False, gate_pass=True)
        step.max_retries = 5
        step.execute = AsyncMock(
            return_value=StepResult(
                success=False,
                summary="Configuration fail",
                error="sova_configuration_error: Codex CLI cannot enforce a budget cap",
            )
        )
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert not result.success
        assert step.execute.await_count == 1
        assert result.step_records[0].status == "failed"
        # Never advanced by _advance_fallback (which only runs for
        # "billing_exhausted"): the fixture never runs AssessStep either, so
        # staying at its unset default is itself proof no fallback hop ran.
        assert ctx.resolved_model is None

    async def test_non_billing_failure_does_not_trigger_fallback(self, tmp_path: Path) -> None:
        """A regular code error should not trigger model fallback."""
        config = ProjectConfig(
            agent={"model": "opus", "fallback_models": ["sonnet"]},
            llm={"engine_owned_fallback": True},
        )
        ctx = _make_ctx(tmp_path, config=config)

        step = DummyStep(should_pass=False, gate_pass=True)
        step.max_retries = 0
        step.execute = AsyncMock(
            return_value=StepResult(success=False, summary="Code error", error="test assertion failed")
        )
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert not result.success
        assert step.execute.await_count == 1

    async def test_fallback_skips_duplicate_model(self, tmp_path: Path) -> None:
        """If fallback_models contains the same model as primary, skip it."""
        config = ProjectConfig(
            agent={"model": "opus", "fallback_models": ["opus", "sonnet"]},
            llm={"engine_owned_fallback": True},
        )
        ctx = _make_ctx(tmp_path, config=config)

        models_tried: list[str | None] = []

        async def track_and_fail(ctx_inner: ExecutionContext) -> StepResult:
            models_tried.append(ctx_inner.resolved_model)
            if ctx_inner.resolved_model == "sonnet":
                return StepResult(success=True, summary="ok")
            return StepResult(success=False, summary="fail", error="budget_exhausted")

        step = DummyStep(should_pass=True, gate_pass=True)
        step.max_retries = 0
        step.execute = track_and_fail
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert result.success
        assert "sonnet" in models_tried
        assert models_tried.count(None) <= 1  # opus might show as None (unresolved)

    async def test_engine_fallback_off_by_default(self, tmp_path: Path) -> None:
        """Fallback is client-owned: the engine must not walk the chain too.

        Without the gate the client loop and the engine loop would nest, giving
        N*N attempts per step.
        """
        config = ProjectConfig(agent={"model": "opus", "fallback_models": ["sonnet", "haiku"]})
        assert config.llm.engine_owned_fallback is False
        ctx = _make_ctx(tmp_path, config=config)

        step = DummyStep(should_pass=False, gate_pass=True)
        step.max_retries = 0
        step.execute = AsyncMock(return_value=StepResult(success=False, summary="fail", error="budget_exhausted"))
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert not result.success
        assert step.execute.await_count == 1

    async def test_empty_fallback_models_no_change(self, tmp_path: Path) -> None:
        """Empty fallback_models means billing failure just fails the step."""
        config = ProjectConfig(agent={"model": "opus", "fallback_models": []})
        ctx = _make_ctx(tmp_path, config=config)

        step = DummyStep(should_pass=False, gate_pass=True)
        step.max_retries = 0
        step.execute = AsyncMock(return_value=StepResult(success=False, summary="fail", error="budget_exhausted"))
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert not result.success
        assert step.execute.await_count == 1

    async def test_fallback_model_persists_for_subsequent_steps(self, tmp_path: Path) -> None:
        """After a successful fallback, subsequent steps use the fallback model."""
        config = ProjectConfig(
            agent={"model": "opus", "fallback_models": ["sonnet"]},
            llm={"engine_owned_fallback": True},
        )
        ctx = _make_ctx(tmp_path, config=config)

        step2_model: list[str | None] = []

        async def billing_fail(ctx_inner: ExecutionContext) -> StepResult:
            if ctx_inner.resolved_model != "sonnet":
                return StepResult(success=False, summary="fail", error="billing error")
            return StepResult(success=True, summary="ok")

        async def capture_model(ctx_inner: ExecutionContext) -> StepResult:
            step2_model.append(ctx_inner.resolved_model)
            return StepResult(success=True, summary="ok")

        s1 = DummyStep(should_pass=True, gate_pass=True, name="step1")
        s1.max_retries = 0
        s1.execute = billing_fail
        s2 = DummyStep(should_pass=True, gate_pass=True, name="step2")
        s2.max_retries = 0
        s2.execute = capture_model

        engine = WorkflowEngine(steps=[s1, s2], ctx=ctx)
        result = await engine.run()

        assert result.success
        assert step2_model == ["sonnet"]

    async def test_step_unhandled_exception_captured(self, tmp_path: Path) -> None:
        """Unhandled exceptions in step.execute are captured and recorded as failures."""
        ctx = _make_ctx(tmp_path)

        async def raise_exception(ctx_inner: ExecutionContext) -> StepResult:
            raise RuntimeError("Unexpected error in step")

        step = DummyStep(should_pass=True, gate_pass=True)
        step.execute = raise_exception
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert not result.success
        assert result.steps_failed == 1
        assert "Unexpected error in step" in (result.error or "")

    async def test_persist_step_result_db_error_non_fatal(self, tmp_path: Path) -> None:
        """DB errors when persisting step results are logged but don't fail the pipeline."""
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)

        with patch("sova.core.workflow.WorkflowEngine._update_step_execution", side_effect=Exception("DB error")):
            engine = WorkflowEngine(steps=[step], ctx=ctx)
            result = await engine.run()

        assert result.success

    async def test_prepare_step_execution_db_error_retries(self, tmp_path: Path) -> None:
        """DB errors when creating StepExecution trigger retry on the same step."""
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        step.max_retries = 1

        call_count = 0

        async def mock_create_failing_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("DB connection failed")
            return 123

        with patch("sova.core.workflow.WorkflowEngine._create_step_execution", side_effect=mock_create_failing_once):
            engine = WorkflowEngine(steps=[step], ctx=ctx)
            result = await engine.run()

        assert result.success
        assert call_count == 2


class TestBillingFailureDetection:
    def test_budget_exhausted(self) -> None:
        assert _is_billing_failure("terminal_reason=budget_exhausted")

    def test_rate_limit(self) -> None:
        assert _is_billing_failure("rate_limit exceeded for model")

    def test_overloaded(self) -> None:
        assert _is_billing_failure("API overloaded, try again")

    def test_billing_error(self) -> None:
        assert _is_billing_failure("billing account suspended")

    def test_insufficient_quota(self) -> None:
        assert _is_billing_failure("insufficient_quota for this org")

    def test_http_429(self) -> None:
        assert _is_billing_failure("HTTP 429 Too Many Requests")

    def test_regular_error(self) -> None:
        assert not _is_billing_failure("test assertion failed: expected 3 got 4")

    def test_none_error(self) -> None:
        assert not _is_billing_failure(None)

    def test_empty_error(self) -> None:
        assert not _is_billing_failure("")

    def test_timeout_error(self) -> None:
        assert not _is_billing_failure("step_hard_timeout")

    def test_typed_billing_exception(self) -> None:
        assert _is_billing_failure(BillingError("budget_exhausted"))

    def test_typed_rate_limit_exception(self) -> None:
        assert _is_billing_failure(RateLimitError("slow down"))

    def test_typed_model_unavailable_exception(self) -> None:
        assert _is_billing_failure(ModelUnavailableError("model is not available"))

    def test_typed_non_billing_exception(self) -> None:
        assert not _is_billing_failure(LLMInvocationError("cannot parse output"))

    def test_stringified_typed_error_still_detected(self) -> None:
        """R4: steps stringify exceptions before the workflow layer sees them."""
        assert _is_billing_failure(str(RateLimitError("rate_limit exceeded for model")))


class TestHandleStepFailureResultClassificationBoundary:
    """Issue #1260 review: DevelopStep's check-output tail must not be fed
    into the billing/configuration classifiers, since a target project's own
    check output can plausibly contain a billing/rate-limit-shaped substring
    (e.g. a pytest node id or an assertion on an HTTP status)."""

    def _engine(self, tmp_path: Path) -> WorkflowEngine:
        ctx = _make_ctx(
            tmp_path,
            config=ProjectConfig(
                agent=AgentConfig(fallback_models=["claude-haiku-4-6"]),
                llm=LLMConfig(engine_owned_fallback=True),
            ),
        )
        return WorkflowEngine(steps=[], ctx=ctx)

    def test_billing_pattern_in_check_output_tail_is_not_classified_as_billing(self, tmp_path: Path) -> None:
        from sova.core.steps.base import StepResult

        error = (
            "checks still failing after 3 fix cycle(s)"
            "\n\nLast check output (tail): "
            "tests/test_llm.py::TestClassifyError::test_rate_limit_patterns FAILED | assert 429 == 200"
        )
        result = WorkflowEngine._handle_step_failure_result(
            self._engine(tmp_path), StepResult(success=False, summary="Development failed", error=error)
        )

        assert result == "failed"

    def test_billing_pattern_in_reason_itself_is_still_classified_as_billing(self, tmp_path: Path) -> None:
        """The split must not blind the classifier to a real billing failure
        that never went through the develop check-output tail path."""
        from sova.core.steps.base import StepResult

        result = WorkflowEngine._handle_step_failure_result(
            self._engine(tmp_path),
            StepResult(success=False, summary="Development failed", error="rate_limit exceeded for model"),
        )

        assert result == "billing_exhausted"

    def test_non_retryable_flag_wins_over_a_billing_looking_error(self, tmp_path: Path) -> None:
        """A step that left the tracker in a state a retry would worsen is never retried or fallback-hopped."""
        from sova.core.steps.base import StepResult

        result = WorkflowEngine._handle_step_failure_result(
            self._engine(tmp_path),
            StepResult(success=False, summary="Research corrupted", error="rate_limit exceeded", non_retryable=True),
        )

        assert result == "non_retryable"


# ---------------------------------------------------------------------------
# WorkflowEngine DB persistence
# ---------------------------------------------------------------------------


class TestWorkflowDB:
    async def test_creates_task_run_record(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.task_run_id is not None
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run is not None
            assert task_run.issue_number == "42"
            assert task_run.role == "developer"

    async def test_task_run_finalized_on_success(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.status == TaskStatus.DONE.value
            assert task_run.ended_at is not None

    async def test_step_execution_created(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        session = await get_session()
        async with session.begin():
            rows = (
                (await session.execute(select(StepExecution).where(StepExecution.task_run_id == result.task_run_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].step_name == "dummy"
            assert rows[0].status == "done"

    async def test_extract_memory_step_execution_is_persisted_as_skipped(self, tmp_path: Path) -> None:
        """A fresh context (nothing completed) still records `skipped`, and never a `done` row, for the step."""
        from sova.core.steps.extract_memory import ExtractMemoryStep

        ctx = _make_ctx(tmp_path)
        assert not ctx.completed_steps
        step = ExtractMemoryStep()
        assert await step.can_skip(ctx) is True
        with patch.object(ExtractMemoryStep, "execute", new_callable=AsyncMock) as execute:
            result = await WorkflowEngine(steps=[step], ctx=ctx).run()
        execute.assert_not_awaited()

        session = await get_session()
        async with session.begin():
            rows = (
                (await session.execute(select(StepExecution).where(StepExecution.task_run_id == result.task_run_id)))
                .scalars()
                .all()
            )
        assert [(row.step_name, row.status) for row in rows] == [("extract_memory", "skipped")]

    @pytest.mark.parametrize(
        "module",
        [
            "memory",
            "retrieval",
            "tiers",
            "sharing",
            "lifecycle",
            "embeddings",
            "graph",
            "extraction",
            "similarity",
            "review_patterns",
        ],
    )
    def test_removed_knowledge_modules_are_not_importable(self, module: str) -> None:
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"sova.knowledge.{module}")

    def test_extract_memory_step_imports_nothing_removed_from_knowledge(self) -> None:
        """Every import form (plain, from, relative) that reaches sova.knowledge stays on the survivors."""
        import ast
        import inspect

        from sova.core.steps import extract_memory

        allowed = {"sova.knowledge.assessments", "sova.knowledge.personas"}
        package = extract_memory.__package__ or ""
        imported: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(extract_memory))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parent = ".".join(package.split(".")[: len(package.split(".")) - node.level + 1])
                    base = f"{parent}.{base}" if base else parent
                imported.add(base)
                imported.update(f"{base}.{alias.name}" for alias in node.names)
        reaching = {name for name in imported if name.startswith("sova.knowledge")}
        assert reaching <= allowed | {"sova.knowledge"}

    def test_extract_memory_is_registered_and_precedes_the_handoff_in_both_pipelines(self) -> None:
        from sova.core.steps import STEP_REGISTRY, get_address_review_steps, get_developer_steps

        assert "extract_memory" in STEP_REGISTRY
        for steps in (get_developer_steps(), get_address_review_steps()):
            names = [s.name for s in steps]
            assert names.index("extract_memory") + 1 == names.index("handoff_to_reviewer")

    async def test_failure_creates_failure_record(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=False, gate_pass=True)
        step.max_retries = 0
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert not result.success
        session = await get_session()
        async with session.begin():
            rows = (
                (await session.execute(select(FailureRecord).where(FailureRecord.task_run_id == result.task_run_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].failure_type == "exception"
            assert rows[0].step_name == "dummy"

    async def test_gate_failure_creates_failure_record(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=False)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        session = await get_session()
        async with session.begin():
            rows = (
                (await session.execute(select(FailureRecord).where(FailureRecord.task_run_id == result.task_run_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].failure_type == "gate_check"

    async def test_budget_exceeded_creates_failure_record(self, tmp_path: Path) -> None:
        config = ProjectConfig(agent={"max_budget": Decimal("0.01")})
        ctx = _make_ctx(tmp_path, config=config)
        ctx.add_cost(Decimal("0.02"))

        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        session = await get_session()
        async with session.begin():
            rows = (
                (await session.execute(select(FailureRecord).where(FailureRecord.task_run_id == result.task_run_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].failure_type == "budget_exceeded"

    async def test_multiple_steps_create_multiple_step_executions(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        steps = [
            DummyStep(should_pass=True, gate_pass=True),
            DummyStep(should_pass=True, gate_pass=True),
        ]
        # Give them distinct names
        steps[0].name = "step_a"
        steps[1].name = "step_b"
        engine = WorkflowEngine(steps=steps, ctx=ctx)

        result = await engine.run()

        session = await get_session()
        async with session.begin():
            rows = (
                (
                    await session.execute(
                        select(StepExecution)
                        .where(StepExecution.task_run_id == result.task_run_id)
                        .order_by(StepExecution.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 2
            assert rows[0].step_name == "step_a"
            assert rows[1].step_name == "step_b"

    async def test_skipped_step_creates_step_execution(self, tmp_path: Path) -> None:
        """Skipped steps create a StepExecution record with status 'skipped'."""
        ctx = _make_ctx(tmp_path)
        step_a = DummyStep(should_pass=True, gate_pass=True, skip=True, name="step_a")
        step_b = DummyStep(should_pass=True, gate_pass=True, name="step_b")
        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.steps_skipped == 1
        assert result.steps_completed == 1
        session = await get_session()
        async with session.begin():
            rows = (
                (
                    await session.execute(
                        select(StepExecution)
                        .where(StepExecution.task_run_id == result.task_run_id)
                        .order_by(StepExecution.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 2
            assert rows[0].step_name == "step_a"
            assert rows[0].status == "skipped"
            assert rows[0].duration_ms == 0
            assert rows[0].started_at is not None
            assert rows[0].ended_at is not None
            assert rows[0].started_at == rows[0].ended_at
            assert rows[1].step_name == "step_b"
            assert rows[1].status == "done"

    async def test_skipped_step_db_failure_does_not_break_workflow(self, tmp_path: Path) -> None:
        """When _create_step_execution raises during a skip, the workflow continues."""
        ctx = _make_ctx(tmp_path)
        step_a = DummyStep(should_pass=True, gate_pass=True, skip=True, name="step_a")
        step_b = DummyStep(should_pass=True, gate_pass=True, name="step_b")
        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)

        original = engine._create_step_execution
        call_count = 0

        async def fail_first_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("db down")
            return await original(*args, **kwargs)

        with patch.object(engine, "_create_step_execution", side_effect=fail_first_call):
            result = await engine.run()

        assert result.success
        assert result.steps_skipped == 1
        assert result.steps_completed == 1

    async def test_task_run_id_set_on_context(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        await engine.run()

        assert ctx.task_run_id is not None

    async def test_resumed_from_id_stored(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, resume_run_id=99)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.resumed_from_id == 99

    async def test_step_in_status_map_sets_final_status(self, tmp_path: Path) -> None:
        """A step whose name is in _STEP_STATUS_MAP updates final_status mid-pipeline."""
        ctx = _make_ctx(tmp_path)
        step_sync = DummyStep(should_pass=True, gate_pass=True, name="sync")
        step_fail = DummyStep(should_pass=True, gate_pass=False, name="next")
        engine = WorkflowEngine(steps=[step_sync, step_fail], ctx=ctx)

        status_updates: list[TaskStatus] = []
        original = engine._update_task_run_status

        async def spy_update(status: TaskStatus, **kwargs: object) -> None:
            status_updates.append(status)
            await original(status, **kwargs)

        engine._update_task_run_status = spy_update  # type: ignore[assignment]

        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        assert TaskStatus.PAUSED in status_updates

    async def test_step_execute_generic_exception(self, tmp_path: Path) -> None:
        """A generic exception in step.execute() is caught and recorded."""
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        step.execute = AsyncMock(side_effect=ValueError("boom"))
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert "boom" in (result.error or "")

    async def test_step_timeout_monitor_ci(self, tmp_path: Path) -> None:
        """_step_timeout returns ci.max_wait + 120 for monitor_ci."""
        ctx = _make_ctx(tmp_path)
        engine = WorkflowEngine(steps=[], ctx=ctx)

        timeout = engine._step_timeout("monitor_ci")

        assert timeout == ctx.config.ci.max_wait + 120

    async def test_step_timeout_regular_step(self, tmp_path: Path) -> None:
        """_step_timeout reads the per-tier fields, not the legacy step_timeout ones.

        Asserting against develop.step_timeout/agent.step_timeout would pass by
        coincidence today (the *_normal defaults are numerically equal to them)
        while testing nothing, and would break silently if either default moved.
        """
        ctx = _make_ctx(tmp_path)
        ctx.config.develop.step_timeout_normal = 1234
        ctx.config.agent.step_timeout_normal = 567
        engine = WorkflowEngine(steps=[], ctx=ctx)

        assert engine._step_timeout("develop") == 1234
        assert engine._step_timeout("commit") == 567

    async def test_write_output_flushes_when_needed(self, tmp_path: Path) -> None:
        """_write_output calls flush() when should_flush() returns True."""
        ctx = _make_ctx(tmp_path)
        engine = WorkflowEngine(steps=[], ctx=ctx)
        writer = MagicMock()
        writer.should_flush.return_value = True
        writer.flush = AsyncMock()
        engine._output_writer = writer

        await engine._write_output("hello")

        writer.write_line.assert_called_once_with("hello")
        writer.flush.assert_awaited_once()

    async def test_update_step_execution_status_none_id(self, tmp_path: Path) -> None:
        """_update_step_execution_status returns early when step_exec_id is None."""
        ctx = _make_ctx(tmp_path)
        engine = WorkflowEngine(steps=[], ctx=ctx)

        with patch("sova.core.workflow.get_session") as mock_get_session, capture_logs() as logs:
            await engine._update_step_execution_status(None, "done")

        mock_get_session.assert_not_called()
        assert [e for e in logs if e["log_level"] == "warning"] == []

    async def test_update_step_execution_status_db_error(self, tmp_path: Path) -> None:
        """_update_step_execution_status logs warning on DB error."""
        ctx = _make_ctx(tmp_path)
        engine = WorkflowEngine(steps=[], ctx=ctx)

        with patch("sova.core.workflow.get_session", side_effect=RuntimeError("db down")):
            with patch("sova.core.workflow.log") as mock_log:
                await engine._update_step_execution_status(999, "done")

            mock_log.warning.assert_called_once()
            call_kwargs = mock_log.warning.call_args
            assert "status_update_failed" in str(call_kwargs)

    async def test_write_approval_handoff_exception(self, tmp_path: Path) -> None:
        """_write_approval_handoff logs warning when write_handoff_file raises."""
        ctx = _make_ctx(tmp_path)
        engine = WorkflowEngine(steps=[], ctx=ctx)
        result = StepResult(success=True, summary="needs approval", awaiting_approval=True)

        with patch("sova.core.workflow.log") as mock_log:
            with patch("sova.ipc.handoff.write_handoff_file", side_effect=OSError("disk full")):
                engine._write_approval_handoff("develop", result)

            mock_log.warning.assert_called()

    async def test_step_with_cost_creates_cost_record(self, tmp_path: Path) -> None:
        """When a step returns cost_usd > 0, a CostRecord is created."""
        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)

        async def execute_with_cost(_ctx: object) -> StepResult:
            return StepResult(success=True, summary="Done", cost_usd=Decimal("0.05"))

        step.execute = execute_with_cost
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.success
        session = await get_session()
        async with session.begin():
            rows = (
                (await session.execute(select(CostRecord).where(CostRecord.task_run_id == result.task_run_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].cost_usd == Decimal("0.05")
            assert rows[0].phase == "dummy"

    async def test_finalize_task_run_preserves_existing_ended_at(self, tmp_path: Path) -> None:
        """_finalize_task_run does not overwrite ended_at if already set."""
        from datetime import datetime, timezone

        ctx = _make_ctx(tmp_path)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        known_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            task_run.ended_at = known_time

        await engine._finalize_task_run()

        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.ended_at.replace(tzinfo=None) == known_time.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Checkpoint / Resume
# ---------------------------------------------------------------------------


class TestCheckpointResume:
    async def test_completed_steps_skips_steps(self, tmp_path: Path) -> None:
        """Steps in completed_steps are skipped via can_skip()."""
        ctx = _make_ctx(tmp_path, completed_steps=frozenset({"step_a"}))

        step_a = DummyStep(should_pass=True, gate_pass=True, name="step_a")
        step_b = DummyStep(should_pass=True, gate_pass=True, name="step_b")

        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)
        result = await engine.run()

        assert result.success
        assert result.steps_skipped == 1
        assert result.steps_completed == 1

    async def test_failed_step_not_skipped(self, tmp_path: Path) -> None:
        """Steps NOT in completed_steps execute normally."""
        ctx = _make_ctx(tmp_path, completed_steps=frozenset({"step_a"}))

        step_a = DummyStep(should_pass=True, gate_pass=True, name="step_a")
        step_b = DummyStep(should_pass=False, gate_pass=True, name="step_b")

        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)
        result = await engine.run()

        assert not result.success
        assert result.steps_skipped == 1
        assert result.steps_failed == 1

    async def test_empty_completed_steps_runs_all(self, tmp_path: Path) -> None:
        """With no completed_steps, all steps execute."""
        ctx = _make_ctx(tmp_path, completed_steps=frozenset())

        step_a = DummyStep(should_pass=True, gate_pass=True, name="step_a")
        step_b = DummyStep(should_pass=True, gate_pass=True, name="step_b")

        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)
        result = await engine.run()

        assert result.success
        assert result.steps_skipped == 0
        assert result.steps_completed == 2

    async def test_resume_preserves_cost_from_context(self, tmp_path: Path) -> None:
        """Resumed run's budget check uses pre-loaded cost."""
        ctx = _make_ctx(
            tmp_path,
            completed_steps=frozenset({"step_a"}),
            cost_usd=Decimal("100"),
        )
        ctx.config.agent.max_budget = Decimal("50")

        step_a = DummyStep(should_pass=True, gate_pass=True, name="step_a")
        step_b = DummyStep(should_pass=True, gate_pass=True, name="step_b")

        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)
        result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.PAUSED
        assert "Budget exceeded" in (result.error or "")


# ---------------------------------------------------------------------------
# Context persistence on non-success paths
# ---------------------------------------------------------------------------


class TestContextPersistence:
    """Verify worktree_path/branch_name are saved to TaskRun on every exit path."""

    async def test_context_persisted_after_successful_step(self, tmp_path: Path) -> None:
        """worktree_path and branch_name are saved after each successful step."""
        worktree = tmp_path / "wt" / "42"
        worktree.mkdir(parents=True)
        ctx = _make_ctx(
            tmp_path,
            branch_name="feat/issue-42",
            worktree_dir=worktree,
        )
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.success
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.branch_name == "feat/issue-42"
            assert task_run.worktree_path == str(worktree)

    async def test_context_persisted_on_gate_failure(self, tmp_path: Path) -> None:
        """worktree_path and branch_name survive a gate_failed pause."""
        worktree = tmp_path / "wt" / "73"
        worktree.mkdir(parents=True)
        ctx = _make_ctx(
            tmp_path,
            branch_name="feat/issue-73",
            worktree_dir=worktree,
        )
        step = DummyStep(should_pass=True, gate_pass=False)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.branch_name == "feat/issue-73"
            assert task_run.worktree_path == str(worktree)

    async def test_context_persisted_on_step_failure(self, tmp_path: Path) -> None:
        """worktree_path and branch_name survive a step execution failure."""
        worktree = tmp_path / "wt" / "99"
        worktree.mkdir(parents=True)
        ctx = _make_ctx(
            tmp_path,
            branch_name="fix/broken",
            worktree_dir=worktree,
        )
        step = DummyStep(should_pass=False)
        step.max_retries = 0
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.FAILED
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.branch_name == "fix/broken"
            assert task_run.worktree_path == str(worktree)

    async def test_context_persisted_on_budget_exceeded(self, tmp_path: Path) -> None:
        """worktree_path and branch_name survive a budget pause."""
        worktree = tmp_path / "wt" / "88"
        worktree.mkdir(parents=True)
        config = ProjectConfig(agent={"max_budget": Decimal("0.01")})
        ctx = _make_ctx(
            tmp_path,
            config=config,
            branch_name="feat/expensive",
            worktree_dir=worktree,
        )
        ctx.add_cost(Decimal("0.02"))

        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.branch_name == "feat/expensive"
            assert task_run.worktree_path == str(worktree)

    async def test_context_updated_mid_pipeline(self, tmp_path: Path) -> None:
        """Context fields set by a middle step are persisted even if a later step fails."""
        ctx = _make_ctx(tmp_path)
        worktree = tmp_path / "wt" / "dynamic"
        worktree.mkdir(parents=True)

        class WorktreeSettingStep(BaseStep):
            name = "set_worktree"

            async def execute(self, ctx_inner: ExecutionContext) -> StepResult:
                ctx_inner.branch_name = "feat/dynamic"
                ctx_inner.worktree_dir = worktree
                return StepResult(success=True, summary="Set worktree")

            async def validate_output(self, ctx_inner: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step_a = WorktreeSettingStep()
        step_b = DummyStep(should_pass=True, gate_pass=False, name="failing_gate")

        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)
        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.branch_name == "feat/dynamic"
            assert task_run.worktree_path == str(worktree)

    async def test_pr_number_persisted_on_failure(self, tmp_path: Path) -> None:
        """pr_number set during execution is saved even if a later step fails."""
        ctx = _make_ctx(
            tmp_path,
            branch_name="feat/pr-test",
            pr_number=42,
        )
        step = DummyStep(should_pass=True, gate_pass=False)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.pr_number == 42

    async def test_awaiting_approval_sets_task_run_status(self, tmp_path: Path) -> None:
        """TaskRun status is set to awaiting_approval when a step pauses."""
        worktree = tmp_path / "wt" / "approval"
        worktree.mkdir(parents=True)
        ctx = _make_ctx(
            tmp_path,
            branch_name="feat/spec-approval",
            worktree_dir=worktree,
        )

        class ApprovalStep(BaseStep):
            name = "spec"

            async def execute(self, ctx_inner: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="Spec ready", awaiting_approval=True)

            async def validate_output(self, ctx_inner: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step = ApprovalStep()
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.AWAITING_APPROVAL
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.status == "awaiting_approval"
            assert task_run.current_step == "spec"
            assert task_run.ended_at is not None
            assert task_run.branch_name == "feat/spec-approval"
            assert task_run.worktree_path == str(worktree)

    async def test_awaiting_approval_step_execution_status(self, tmp_path: Path) -> None:
        """StepExecution is marked 'awaiting_approval' (not 'done') so resume re-executes it."""
        ctx = _make_ctx(tmp_path)

        class ApprovalStep(BaseStep):
            name = "needs_approval"

            async def execute(self, ctx_inner: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="Awaiting", awaiting_approval=True)

            async def validate_output(self, ctx_inner: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step = ApprovalStep()
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        session = await get_session()
        async with session.begin():
            rows = (
                (await session.execute(select(StepExecution).where(StepExecution.task_run_id == result.task_run_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].step_name == "needs_approval"
            assert rows[0].status == "awaiting_approval"

    async def test_awaiting_approval_step_not_in_completed_on_resume(self) -> None:
        """The paused step's 'awaiting_approval' status is NOT in STEP_DONE_STATUSES, so resume re-executes it."""
        from sova.core.state import STEP_DONE_STATUSES

        assert "awaiting_approval" not in STEP_DONE_STATUSES


# ---------------------------------------------------------------------------
# Issue-less workflow runs
# ---------------------------------------------------------------------------


class TestIssuelessWorkflow:
    """Test that the workflow engine and steps handle issue-less runs."""

    async def test_workflow_engine_issueless_run(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, issue_number="", run_label="planner-123")
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.steps_completed == 1

        async with await get_session() as session:
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run is not None

    async def test_workflow_engine_issueless_creates_task_run(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, issue_number="", run_label="test-run")
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()
        assert result.task_run_id is not None

    async def test_dispatch_requires_role_for_issueless(self, tmp_path: Path) -> None:
        """Dispatch without issue and without role should raise ValueError."""
        from sova.roles.dispatcher import dispatch

        ctx = _make_ctx(tmp_path, issue_number="")
        with pytest.raises(ValueError, match="Issue-less runs require"):
            await dispatch(ctx)


# ---------------------------------------------------------------------------
# Resume validation: _load_checkpoint issue/issueless mismatch
# ---------------------------------------------------------------------------


class TestResumeValidation:
    """Verify _load_checkpoint catches issue/issueless resume mismatches."""

    async def test_resume_issue_run_without_issue_arg_fails(self) -> None:
        """Resuming an issue-based run without providing an issue should fail."""
        from sova.cli.commands.run import _load_checkpoint

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="42", role="developer", status="paused")
                session.add(run)
                await session.flush()
                run_id = run.id

        result = await _load_checkpoint(run_id, "")
        assert "error" in result
        assert "cannot resume without an issue" in result["error"]

    async def test_resume_issueless_run_with_issue_arg_fails(self) -> None:
        """Resuming an issue-less run with an issue arg should fail."""
        from sova.cli.commands.run import _load_checkpoint

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number=None, role="planner", status="paused")
                session.add(run)
                await session.flush()
                run_id = run.id

        result = await _load_checkpoint(run_id, "42")
        assert "error" in result
        assert "issue-less run" in result["error"]

    async def test_resume_matching_issue_succeeds(self) -> None:
        """Resuming with the correct issue should succeed."""
        from sova.cli.commands.run import _load_checkpoint

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="42", role="developer", status="paused")
                session.add(run)
                await session.flush()
                run_id = run.id

        result = await _load_checkpoint(run_id, "42")
        assert "error" not in result
        assert result["role"] == "developer"

    async def test_resume_issueless_without_issue_succeeds(self) -> None:
        """Resuming an issue-less run without an issue arg should succeed."""
        from sova.cli.commands.run import _load_checkpoint

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number=None, role="planner", status="paused")
                session.add(run)
                await session.flush()
                run_id = run.id

        result = await _load_checkpoint(run_id, "")
        assert "error" not in result
        assert result["role"] == "planner"

    async def test_resume_restores_confidence_score_from_assessment_json(self) -> None:
        """A completed confidence_score step's persisted score survives resume."""
        from sova.cli.commands.run import _load_checkpoint

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="42",
                    role="developer",
                    status="paused",
                    assessment_json={
                        "confidence_score": 82,
                        "confidence_details": {"risks": [], "summary": "Looks safe."},
                    },
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        result = await _load_checkpoint(run_id, "42")
        assert "error" not in result
        assert result["confidence_score"] == 82
        assert result["confidence_details"] == {"risks": [], "summary": "Looks safe."}

    async def test_resume_confidence_score_absent_when_never_scored(self) -> None:
        from sova.cli.commands.run import _load_checkpoint

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="42", role="developer", status="paused")
                session.add(run)
                await session.flush()
                run_id = run.id

        result = await _load_checkpoint(run_id, "42")
        assert "error" not in result
        assert result["confidence_score"] is None
        assert result["confidence_details"] is None


# ---------------------------------------------------------------------------
# _run_workflow: outcome reporting (success / awaiting_approval / failure)
# ---------------------------------------------------------------------------


class TestRunWorkflowOutcome:
    """Verify _run_workflow reports each RoleResult outcome distinctly."""

    @contextmanager
    def _patch_run_deps(self, role_result):
        # MagicMock's `name=` constructor kwarg sets the mock's own repr, not a
        # `.name` attribute, so it's assigned separately here.
        role = MagicMock()
        role.name = "researcher"
        # A real ProjectConfig, not a bare MagicMock: _run_workflow reads numeric
        # settings off it (server.log_max_bytes/log_backup_count feed
        # rotation_kwargs()), and a MagicMock attribute would be silently truthy
        # rather than a usable value.
        from sova.config.models import ProjectConfig

        with (
            patch("sova.cli.commands.run.load_config", return_value=ProjectConfig()),
            patch("sova.dashboard.services.agent_validation.load_config", return_value=ProjectConfig()),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch("sova.db.session.init_db", new=AsyncMock()),
            patch("sova.utils.logging.setup_logging"),
            patch("sova.roles.dispatcher.dispatch", new=AsyncMock(return_value=(role, role_result))),
            # Avoid installing a real SIGTERM handler on the test event loop;
            # that mechanism is covered on its own in TestInstallSigtermHandler.
            patch("sova.cli.commands.run._install_sigterm_handler"),
        ):
            yield

    async def test_awaiting_approval_prints_paused_and_does_not_raise(self, tmp_path: Path) -> None:
        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        result = RoleResult(success=False, summary="Spec for #1 awaiting human approval", awaiting_approval=True)
        with self._patch_run_deps(result):
            with patch("sova.cli.commands.run.console.print") as mock_print:
                await _run_workflow(
                    "1",
                    project_dir=tmp_path,
                    role_name="researcher",
                    force=False,
                )

        messages = [call.args[0] for call in mock_print.call_args_list]
        assert any("Workflow paused (researcher)" in m for m in messages)

    async def test_success_prints_completed(self, tmp_path: Path) -> None:
        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        result = RoleResult(success=True, summary="Issue #1 researched")
        with self._patch_run_deps(result):
            with patch("sova.cli.commands.run.console.print") as mock_print:
                await _run_workflow(
                    "1",
                    project_dir=tmp_path,
                    role_name="researcher",
                    force=False,
                )

        messages = [call.args[0] for call in mock_print.call_args_list]
        assert any("Workflow completed (researcher)" in m for m in messages)

    async def test_failure_prints_failed_and_raises_exit(self, tmp_path: Path) -> None:
        import typer

        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        result = RoleResult(success=False, summary="Research failed", error="boom")
        with self._patch_run_deps(result):
            with patch("sova.cli.commands.run.console.print") as mock_print:
                with pytest.raises(typer.Exit):
                    await _run_workflow(
                        "1",
                        project_dir=tmp_path,
                        role_name="researcher",
                        force=False,
                    )

        messages = [call.args[0] for call in mock_print.call_args_list]
        assert any("Workflow failed (researcher)" in m for m in messages)


class TestRunWorkflowLease:
    """A run holds one machine-wide lease while it dispatches, so concurrent checks split the cores (issue #1157)."""

    @pytest.mark.parametrize("raises", [False, True], ids=["dispatch returns", "dispatch raises"])
    async def test_the_lease_is_held_during_dispatch_and_released_after(self, tmp_path: Path, raises: bool) -> None:
        import os

        from sova.cli.commands.run import _run_workflow
        from sova.config.models import ProjectConfig
        from sova.roles.base import RoleResult
        from sova.utils import agent_leases

        seen: dict[str, set[int]] = {}

        async def dispatch(*args: object, **kwargs: object) -> tuple[MagicMock, RoleResult]:
            seen["during"] = agent_leases.live_lease_pids()
            if raises:
                raise RuntimeError("role crashed")
            return MagicMock(), RoleResult(success=True, summary="done")

        with (
            patch("sova.cli.commands.run.load_config", return_value=ProjectConfig()),
            patch("sova.dashboard.services.agent_validation.load_config", return_value=ProjectConfig()),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch("sova.db.session.init_db", new=AsyncMock()),
            patch("sova.utils.logging.setup_logging"),
            patch("sova.roles.dispatcher.dispatch", new=dispatch),
            patch("sova.cli.commands.run._install_sigterm_handler"),
            patch("sova.cli.commands.run._persist_run_cost", new=AsyncMock()),
            patch("sova.cli.commands.run.console.print"),
        ):
            if raises:
                with pytest.raises(RuntimeError):
                    await _run_workflow("1", project_dir=tmp_path, role_name="researcher", force=False)
            else:
                await _run_workflow("1", project_dir=tmp_path, role_name="researcher", force=False)

        assert seen["during"] == {os.getpid()}
        assert agent_leases.live_lease_pids() == set()
        assert agent_leases.LEASE_ENV_KEY not in os.environ

    async def test_parallel_holds_one_lease_per_pipeline_it_runs_concurrently(self, tmp_path: Path) -> None:
        """Pipelines sharing one process each count as an agent, so their checks split the cores (issue #1157)."""
        import asyncio
        import os

        from sova.cli.commands.run import _parallel
        from sova.config.models import ProjectConfig
        from sova.roles.base import RoleResult
        from sova.utils import agent_leases

        counts: list[int] = []
        markers: list[str | None] = []
        everyone_started = asyncio.Event()
        everyone_counted = asyncio.Event()
        started = 0

        async def dispatch(*args: object, **kwargs: object) -> tuple[MagicMock, RoleResult]:
            nonlocal started
            started += 1
            if started == 3:
                everyone_started.set()
            await everyone_started.wait()
            counts.append(agent_leases.active_agent_count())
            markers.append(os.environ.get(agent_leases.LEASE_ENV_KEY))
            if len(counts) == 3:
                everyone_counted.set()
            await everyone_counted.wait()
            return MagicMock(), RoleResult(success=True, summary="done")

        config = ProjectConfig(max_parallel_agents=3)
        with (
            patch("sova.cli.commands.run.load_config", return_value=config),
            patch("sova.dashboard.services.agent_validation.load_config", return_value=config),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch("sova.db.session.init_db", new=AsyncMock()),
            patch("sova.roles.dispatcher.dispatch", new=dispatch),
            patch("sova.cli.commands.run.console.print"),
        ):
            await _parallel(issues=["1", "2", "3"], project_dir=tmp_path, force=False)

        assert counts == [3, 3, 3]
        assert markers == [None, None, None]
        assert agent_leases.live_lease_pids() == set()

    async def test_watch_holds_a_lease_for_each_task_it_dispatches(self, tmp_path: Path) -> None:
        """`sova watch` runs pipelines in its own process, so it leases each one as `sova run` does."""
        import os

        from sova.adapters.base import TaskState
        from sova.cli.commands.run import _watch
        from sova.config.models import ProjectConfig
        from sova.roles.base import RoleResult
        from sova.utils import agent_leases

        seen: dict[str, set[int]] = {}

        async def dispatch(*args: object, **kwargs: object) -> tuple[MagicMock, RoleResult]:
            seen["during"] = agent_leases.live_lease_pids()
            return MagicMock(), RoleResult(success=True, summary="done")

        config = ProjectConfig()
        config.watch.interval_active = 0
        adapter = MagicMock()
        task = MagicMock(id="1", title="t", state=TaskState.BACKLOG)
        # The second poll stops the loop the way Ctrl-C does.
        adapter.list_tasks = AsyncMock(side_effect=[[task], KeyboardInterrupt()])
        with (
            patch("sova.cli.commands.run.load_config", return_value=config),
            patch("sova.dashboard.services.agent_validation.load_config", return_value=config),
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.db.session.init_db", new=AsyncMock()),
            patch("sova.roles.dispatcher.dispatch", new=dispatch),
            patch("sova.cli.commands.run.console.print"),
        ):
            await _watch(project_dir=tmp_path, force=False)

        assert seen["during"] == {os.getpid()}
        assert agent_leases.live_lease_pids() == set()
        assert agent_leases.LEASE_ENV_KEY not in os.environ


# ---------------------------------------------------------------------------
# _run_workflow: cost persistence for roles that bypass WorkflowEngine (issue #1368)
# ---------------------------------------------------------------------------


class TestRunWorkflowPersistsRoleCost:
    """_run_workflow() writes ctx.cost_usd to the TaskRun after dispatch(), on success and on failure.

    ReviewerRole, TriageRole and PlannerRole add their spend to ``ctx.cost_usd`` but never touch
    ``TaskRun.total_cost_usd``: only WorkflowEngine does that, and only for the roles it runs.
    """

    @staticmethod
    async def _seed_run(role: str, *, cost: Decimal = Decimal("0"), issue: str = "1368") -> int:
        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number=issue, role=role, status="running", total_cost_usd=cost)
                session.add(run)
                await session.flush()
                return run.id

    @staticmethod
    async def _recorded_cost(run_id: int) -> Decimal:
        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run is not None
            return run.total_cost_usd

    @staticmethod
    @contextmanager
    def _patch_run_deps(dispatch_mock: AsyncMock) -> Iterator[None]:
        with (
            patch("sova.cli.commands.run.load_config", return_value=ProjectConfig()),
            patch("sova.dashboard.services.agent_validation.load_config", return_value=ProjectConfig()),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch("sova.db.session.init_db", new=AsyncMock()),
            patch("sova.utils.logging.setup_logging"),
            patch("sova.roles.dispatcher.dispatch", new=dispatch_mock),
            patch("sova.cli.commands.run._install_sigterm_handler"),
        ):
            yield

    @staticmethod
    def _spending_dispatch(role_name: str, amount: Decimal, result: object) -> AsyncMock:
        """A dispatch() stand-in that spends *amount* on the context the way a direct-spawn role does."""
        role = MagicMock()
        role.name = role_name

        async def _dispatch(ctx: ExecutionContext, **_kwargs: object) -> tuple[object, object]:
            ctx.add_cost(amount)
            if isinstance(result, Exception):
                raise result
            return role, result

        return AsyncMock(side_effect=_dispatch)

    async def test_reviewer_success_records_cost(self, tmp_path: Path) -> None:
        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        run_id = await self._seed_run("reviewer")
        dispatch_mock = self._spending_dispatch("reviewer", Decimal("1.40"), RoleResult(success=True, summary="ok"))

        with self._patch_run_deps(dispatch_mock):
            await _run_workflow("1368", project_dir=tmp_path, role_name="reviewer", force=False, task_run_id=run_id)

        assert await self._recorded_cost(run_id) == Decimal("1.40")

    async def test_reviewer_failed_result_still_records_cost(self, tmp_path: Path) -> None:
        import typer

        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        run_id = await self._seed_run("reviewer")
        dispatch_mock = self._spending_dispatch(
            "reviewer", Decimal("0.91"), RoleResult(success=False, summary="Review failed", error="boom")
        )

        with self._patch_run_deps(dispatch_mock):
            with pytest.raises(typer.Exit):
                await _run_workflow("1368", project_dir=tmp_path, role_name="reviewer", force=False, task_run_id=run_id)

        assert await self._recorded_cost(run_id) == Decimal("0.91")

    async def test_role_raising_after_spend_still_records_cost_and_reraises(self, tmp_path: Path) -> None:
        from sova.cli.commands.run import _run_workflow

        run_id = await self._seed_run("reviewer")
        dispatch_mock = self._spending_dispatch("reviewer", Decimal("2.10"), RuntimeError("provider exploded"))

        with self._patch_run_deps(dispatch_mock):
            with pytest.raises(RuntimeError, match="provider exploded"):
                await _run_workflow("1368", project_dir=tmp_path, role_name="reviewer", force=False, task_run_id=run_id)

        assert await self._recorded_cost(run_id) == Decimal("2.10")

    async def test_resumed_run_records_only_its_own_spend(self, tmp_path: Path) -> None:
        """--resume seeds ctx.cost_usd with the prior run's cost; the prior row keeps that cost, so the
        new row must not store it again or the per-issue budget sum counts it twice."""
        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        run_id = await self._seed_run("reviewer")
        dispatch_mock = self._spending_dispatch("reviewer", Decimal("1.00"), RoleResult(success=True, summary="ok"))
        checkpoint = {"role": "reviewer", "cost_usd": Decimal("4.00"), "completed_steps": set()}

        with self._patch_run_deps(dispatch_mock):
            with patch("sova.cli.commands.run._load_checkpoint", new=AsyncMock(return_value=checkpoint)):
                await _run_workflow(
                    "1368", project_dir=tmp_path, role_name="reviewer", force=False, resume_run_id=1, task_run_id=run_id
                )

        assert await self._recorded_cost(run_id) == Decimal("1.00")

    async def test_engine_finalized_cost_is_not_double_counted(self, tmp_path: Path) -> None:
        """A developer run: WorkflowEngine already stored 5.00 and ctx.cost_usd is the same 5.00."""
        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        run_id = await self._seed_run("developer", cost=Decimal("5.00"))
        dispatch_mock = self._spending_dispatch("developer", Decimal("5.00"), RoleResult(success=True, summary="ok"))

        with self._patch_run_deps(dispatch_mock):
            await _run_workflow("1368", project_dir=tmp_path, role_name="developer", force=False, task_run_id=run_id)

        assert await self._recorded_cost(run_id) == Decimal("5.00")

    async def test_zero_spend_never_overwrites_recorded_cost(self, tmp_path: Path) -> None:
        """The dashboard finalizer may have stored a runtime-stream cost; a role with no spend must not zero it."""
        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        run_id = await self._seed_run("reviewer", cost=Decimal("3.00"))
        dispatch_mock = self._spending_dispatch("reviewer", Decimal("0"), RoleResult(success=True, summary="ok"))

        with self._patch_run_deps(dispatch_mock):
            await _run_workflow("1368", project_dir=tmp_path, role_name="reviewer", force=False, task_run_id=run_id)

        assert await self._recorded_cost(run_id) == Decimal("3.00")

    async def test_cost_lands_on_the_run_dispatch_opened_when_none_was_passed(self, tmp_path: Path) -> None:
        """A terminal run passes no --run-id; dispatch() opens the row for a non-engine role (issue #1403)."""
        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        run_id = await self._seed_run("reviewer")
        role = MagicMock()
        role.name = "reviewer"

        async def _dispatch(ctx: ExecutionContext, **_kwargs: object) -> tuple[object, object]:
            ctx.task_run_id = run_id
            ctx.add_cost(Decimal("0.75"))
            return role, RoleResult(success=True, summary="ok")

        with self._patch_run_deps(AsyncMock(side_effect=_dispatch)):
            await _run_workflow("1368", project_dir=tmp_path, role_name="reviewer", force=False)

        assert await self._recorded_cost(run_id) == Decimal("0.75")

    async def test_failed_write_is_logged_and_does_not_replace_the_role_outcome(self, tmp_path: Path) -> None:
        from sqlalchemy.exc import OperationalError

        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        run_id = await self._seed_run("reviewer")
        dispatch_mock = self._spending_dispatch("reviewer", Decimal("1.00"), RoleResult(success=True, summary="ok"))

        with self._patch_run_deps(dispatch_mock):
            with patch(
                "sova.cli.commands.run.get_session", side_effect=OperationalError("stmt", {}, Exception("locked"))
            ):
                with patch("sova.cli.commands.run.log") as mock_log:
                    await _run_workflow(
                        "1368", project_dir=tmp_path, role_name="reviewer", force=False, task_run_id=run_id
                    )

        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args.args[0] == "run.cost_persist_failed"
        assert await self._recorded_cost(run_id) == Decimal("0")

    async def test_sigterm_during_the_write_takes_the_shutdown_path(self, tmp_path: Path) -> None:
        """The write after dispatch() is a new cancellation point: SIGTERM there must still record cost."""
        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        dispatch_mock = self._spending_dispatch("reviewer", Decimal("1.00"), RoleResult(success=True, summary="ok"))
        shutdown_calls = []

        async def fake_shutdown(ctx: ExecutionContext, task_run_id: int | None, prior_cost: Decimal) -> None:
            shutdown_calls.append((task_run_id, ctx.cost_usd))

        with self._patch_run_deps(dispatch_mock):
            with (
                patch("sova.cli.commands.run._persist_run_cost", new=AsyncMock(side_effect=asyncio.CancelledError)),
                patch("sova.cli.commands.run._handle_sigterm_shutdown", side_effect=fake_shutdown),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await _run_workflow("1368", project_dir=tmp_path, role_name="reviewer", force=False, task_run_id=5)

        assert shutdown_calls == [(5, Decimal("1.00"))]

    async def test_no_task_run_id_skips_the_write(self, tmp_path: Path) -> None:
        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        dispatch_mock = self._spending_dispatch("reviewer", Decimal("1.00"), RoleResult(success=True, summary="ok"))

        with self._patch_run_deps(dispatch_mock):
            with patch("sova.cli.commands.run.get_session") as mock_get_session:
                await _run_workflow("1368", project_dir=tmp_path, role_name="reviewer", force=False)

        mock_get_session.assert_not_called()

    async def test_recorded_reviewer_cost_counts_toward_the_per_issue_budget(self, tmp_path: Path) -> None:
        """WorkflowEngine._check_per_issue_budget() sums TaskRun.total_cost_usd, so a recorded review counts."""
        from sova.cli.commands.run import _run_workflow
        from sova.core.workflow import WorkflowResult
        from sova.roles.base import RoleResult

        reviewer_id = await self._seed_run("reviewer", issue="77")
        async with await get_session() as session:
            async with session.begin():
                (await session.get(TaskRun, reviewer_id)).status = "done"
        dispatch_mock = self._spending_dispatch("reviewer", Decimal("12.00"), RoleResult(success=True, summary="ok"))
        with self._patch_run_deps(dispatch_mock):
            await _run_workflow("77", project_dir=tmp_path, role_name="reviewer", force=False, task_run_id=reviewer_id)

        config = ProjectConfig(agent=AgentConfig(max_issue_budget=Decimal("10.00")))
        ctx = ExecutionContext(project_dir=tmp_path, config=config, adapter=MagicMock(), issue_number="77")
        engine = WorkflowEngine(steps=[], ctx=ctx)
        engine._task_run_id = await self._seed_run("developer", issue="77")
        result = WorkflowResult(success=True, final_status=TaskStatus.DONE)

        await engine._check_per_issue_budget(result)

        assert result.final_status == TaskStatus.PAUSED
        assert "Per-issue budget exceeded" in (result.error or "")


# ---------------------------------------------------------------------------
# sova run SIGTERM handling (issue #978)
# ---------------------------------------------------------------------------


class TestInstallSigtermHandler:
    """_install_sigterm_handler() cancels the running task when SIGTERM arrives."""

    async def test_registers_handler_that_cancels_current_task(self) -> None:
        import signal

        from sova.cli.commands.run import _install_sigterm_handler

        loop = asyncio.get_running_loop()
        registered = {}

        def fake_add_signal_handler(sig, callback):
            registered["sig"] = sig
            registered["callback"] = callback

        with patch.object(loop, "add_signal_handler", side_effect=fake_add_signal_handler):
            _install_sigterm_handler(task_run_id=42)

        assert registered["sig"] == signal.SIGTERM

        task = asyncio.current_task()
        with patch.object(task, "cancel") as mock_cancel:
            registered["callback"]()
        mock_cancel.assert_called_once()

    async def test_unavailable_add_signal_handler_does_not_raise(self) -> None:
        from sova.cli.commands.run import _install_sigterm_handler

        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler", side_effect=NotImplementedError):
            with capture_logs() as logs:
                _install_sigterm_handler(task_run_id=None)

        assert [e["log_level"] for e in logs if e["event"] == "run.sigterm_handler_unavailable"] == ["debug"]


class TestHandleSigtermShutdown:
    """_handle_sigterm_shutdown() (issue #978): partial-work staging + best-effort cost update."""

    async def test_stages_partial_work_and_updates_cost(self, tmp_path: Path) -> None:
        from sova.cli.commands.run import _handle_sigterm_shutdown
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="978", role="developer", status="running")
                session.add(run)
                await session.flush()
                run_id = run.id

        ctx = MagicMock()
        ctx.partial_work_blocked = ""
        ctx.worktree_dir = tmp_path / "does-not-matter"
        ctx.project_dir = None
        ctx.cost_usd = Decimal("1.23")
        ctx.config = _sova_commit_config()

        with patch("sova.git.worktree.preserve_partial_work", new=AsyncMock(return_value=True)) as mock_preserve:
            await _handle_sigterm_shutdown(ctx, run_id)

        mock_preserve.assert_awaited_once_with(
            ctx.worktree_dir, "sigterm", commit_type="chore", scope="core", config=ctx.config
        )

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.total_cost_usd == Decimal("1.23")

    async def test_a_step_that_blocked_partial_work_is_not_committed_over_on_sigterm(self, tmp_path: Path) -> None:
        from sova.cli.commands.run import _handle_sigterm_shutdown

        ctx = MagicMock()
        ctx.worktree_dir = tmp_path / "does-not-matter"
        ctx.project_dir = None
        ctx.cost_usd = Decimal("1.23")
        ctx.config = _sova_commit_config()
        ctx.partial_work_blocked = "Rearrange restore failed; restore it with: git -C x reset -q abc"

        with patch("sova.git.worktree.preserve_partial_work", new=AsyncMock(return_value=True)) as mock_preserve:
            await _handle_sigterm_shutdown(ctx, None)

        mock_preserve.assert_not_awaited()

    async def test_a_resumed_run_stores_only_its_own_spend(self, tmp_path: Path) -> None:
        from sova.cli.commands.run import _handle_sigterm_shutdown
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="978", role="reviewer", status="running")
                session.add(run)
                await session.flush()
                run_id = run.id

        ctx = MagicMock()
        ctx.partial_work_blocked = ""
        ctx.worktree_dir = None
        ctx.project_dir = None
        ctx.cost_usd = Decimal("1.50")  # seeded with the prior run's 1.00 on resume

        await _handle_sigterm_shutdown(ctx, run_id, Decimal("1.00"))

        async with await get_session() as session:
            async with session.begin():
                assert (await session.get(TaskRun, run_id)).total_cost_usd == Decimal("0.50")

    async def test_no_task_run_id_skips_db_update(self, tmp_path: Path) -> None:
        from sova.cli.commands.run import _handle_sigterm_shutdown

        ctx = MagicMock()
        ctx.partial_work_blocked = ""
        ctx.worktree_dir = tmp_path / "does-not-matter"
        ctx.config = _sova_commit_config()

        with patch("sova.git.worktree.preserve_partial_work", new=AsyncMock(return_value=False)) as mock_preserve:
            await _handle_sigterm_shutdown(ctx, None)

        mock_preserve.assert_awaited_once()

    async def test_no_worktree_skips_staging(self) -> None:
        """Before create_worktree runs, ctx.worktree_dir is None: never stage in the primary checkout."""
        from sova.cli.commands.run import _handle_sigterm_shutdown

        ctx = MagicMock()
        ctx.partial_work_blocked = ""
        ctx.worktree_dir = None

        with patch("sova.git.worktree.preserve_partial_work", new=AsyncMock()) as mock_preserve:
            await _handle_sigterm_shutdown(ctx, None)

        mock_preserve.assert_not_awaited()

    async def test_db_failure_during_shutdown_does_not_raise(self, tmp_path: Path) -> None:
        """Best-effort cleanup during shutdown must not raise, even on a broken session."""
        from sova.cli.commands.run import _handle_sigterm_shutdown

        ctx = MagicMock()
        ctx.partial_work_blocked = ""
        ctx.worktree_dir = tmp_path / "does-not-matter"
        ctx.project_dir = None
        ctx.cost_usd = Decimal("0")
        ctx.config = _sova_commit_config()

        with (
            patch("sova.git.worktree.preserve_partial_work", new=AsyncMock(return_value=False)),
            patch("sova.cli.commands.run.get_session", side_effect=RuntimeError("db unavailable")),
            capture_logs() as logs,
        ):
            await _handle_sigterm_shutdown(ctx, 999)

        warnings = [e for e in logs if e["log_level"] == "warning"]
        assert [e["event"] for e in warnings] == ["run.sigterm_db_update_failed"]
        assert warnings[0]["run_id"] == 999


class TestRunWorkflowCancellation:
    """_run_workflow() catches CancelledError from dispatch(), cleans up, then re-raises."""

    async def test_cancellation_runs_shutdown_handler_and_reraises(self, tmp_path: Path) -> None:
        from sova.cli.commands.run import _run_workflow

        shutdown_calls = []

        async def fake_shutdown(ctx, task_run_id, prior_cost):
            shutdown_calls.append(task_run_id)

        from sova.config.models import ProjectConfig

        with (
            # A real config, not a MagicMock: see TestRunWorkflowOutcome._patch_run_deps.
            patch("sova.cli.commands.run.load_config", return_value=ProjectConfig()),
            patch("sova.dashboard.services.agent_validation.load_config", return_value=ProjectConfig()),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch("sova.db.session.init_db", new=AsyncMock()),
            patch("sova.utils.logging.setup_logging"),
            patch("sova.roles.dispatcher.dispatch", new=AsyncMock(side_effect=asyncio.CancelledError)),
            patch("sova.cli.commands.run._install_sigterm_handler"),
            patch("sova.cli.commands.run._handle_sigterm_shutdown", side_effect=fake_shutdown),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _run_workflow(
                    "1",
                    project_dir=tmp_path,
                    role_name="researcher",
                    force=False,
                    task_run_id=77,
                )

        assert shutdown_calls == [77]

    def test_run_issue_maps_cancellation_to_signal_shaped_exit_code(self) -> None:
        """run_issue() exits 128+SIGTERM so a parent observes a signal-shaped exit."""
        import signal

        import typer

        from sova.cli.commands.run import run_issue

        with patch("sova.cli.commands.run.asyncio.run", side_effect=closing_stand_in(raises=asyncio.CancelledError)):
            with pytest.raises(typer.Exit) as exc_info:
                run_issue(issue="1")

        assert exc_info.value.exit_code == 128 + signal.SIGTERM
