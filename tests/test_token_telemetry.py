"""Tests for per-step token telemetry: context accumulation and CostRecord attribution."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sova.core.context import ExecutionContext, TokenUsage
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.core.workflow import WorkflowEngine
from sova.llm.models import CostSource, LLMResult, combine_cost_sources


@pytest.fixture
def mock_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.repo = "test/repo"
    return adapter


@pytest.fixture
def ctx(tmp_path: Path, mock_adapter: MagicMock) -> ExecutionContext:
    from sova.config.loader import load_config

    (tmp_path / "sova.toml").write_text("github_repo = 'test/repo'\n")
    cfg = load_config(tmp_path)
    return ExecutionContext(
        project_dir=tmp_path,
        config=cfg,
        adapter=mock_adapter,
        issue_number="123",
        role="developer",
    )


def _llm_result(**overrides: Any) -> LLMResult:
    fields: dict[str, Any] = {
        "text": "ok",
        "model": "claude",
        "cost_usd": Decimal("0.10"),
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 50,
        "cache_creation_tokens": 25,
    }
    fields.update(overrides)
    return LLMResult(**fields)


class TestTokenUsage:
    def test_subtraction_yields_delta(self) -> None:
        after = TokenUsage(
            input_tokens=100,
            output_tokens=30,
            cache_read_tokens=10,
            cache_write_tokens=4,
            tokens_saved=6,
            compressed_calls=2,
        )
        before = TokenUsage(
            input_tokens=40,
            output_tokens=10,
            cache_read_tokens=3,
            cache_write_tokens=1,
            tokens_saved=2,
            compressed_calls=1,
        )
        assert after - before == TokenUsage(
            input_tokens=60,
            output_tokens=20,
            cache_read_tokens=7,
            cache_write_tokens=3,
            tokens_saved=4,
            compressed_calls=1,
        )

    def test_default_counters_are_zero_and_saved_is_none(self) -> None:
        """tokens_saved defaults to None: compression not running is not the same as saving zero."""
        usage = TokenUsage()
        assert (usage.input_tokens, usage.output_tokens) == (0, 0)
        assert (usage.cache_read_tokens, usage.cache_write_tokens) == (0, 0)
        assert usage.tokens_saved is None

    def test_delta_keeps_none_when_compression_never_ran(self) -> None:
        assert (TokenUsage(input_tokens=5) - TokenUsage()).tokens_saved is None

    def test_delta_keeps_zero_distinct_from_none(self) -> None:
        """Compression that ran and saved nothing must report 0, not None."""
        assert (TokenUsage(tokens_saved=0, compressed_calls=1) - TokenUsage()).tokens_saved == 0

    def test_delta_from_none_baseline_counts_full_saving(self) -> None:
        assert (TokenUsage(tokens_saved=40, compressed_calls=1) - TokenUsage()).tokens_saved == 40

    def test_delta_is_none_when_no_compression_ran_in_this_window(self) -> None:
        """An earlier step's saving must not read as this step compressing to zero.

        Both snapshots carry the same cumulative total, so subtracting alone
        yields 0, which would persist as "compression ran and saved nothing".
        """
        before = TokenUsage(input_tokens=1000, tokens_saved=120, compressed_calls=1)
        after = TokenUsage(input_tokens=2000, tokens_saved=120, compressed_calls=1)
        assert (after - before).tokens_saved is None

    def test_addition_sums_every_counter(self) -> None:
        first = TokenUsage(
            input_tokens=100,
            output_tokens=30,
            cache_read_tokens=10,
            cache_write_tokens=4,
            tokens_saved=6,
            compressed_calls=2,
            cache_write_reported_calls=1,
            reasoning_tokens=5,
            reasoning_reported_calls=1,
        )
        second = TokenUsage(
            input_tokens=40,
            output_tokens=10,
            cache_read_tokens=3,
            cache_write_tokens=1,
            reasoning_tokens=2,
            reasoning_reported_calls=1,
        )
        assert first + second == TokenUsage(
            input_tokens=140,
            output_tokens=40,
            cache_read_tokens=13,
            cache_write_tokens=5,
            tokens_saved=6,
            compressed_calls=2,
            cache_write_reported_calls=1,
            reasoning_tokens=7,
            reasoning_reported_calls=2,
        )

    def test_addition_keeps_none_when_neither_side_compressed(self) -> None:
        assert (TokenUsage(input_tokens=1) + TokenUsage(input_tokens=2)).tokens_saved is None


class TestCombineCostSources:
    @pytest.mark.parametrize(
        ("current", "new", "expected"),
        [
            (None, CostSource.SUBSCRIPTION, CostSource.SUBSCRIPTION),
            (CostSource.PRICED, CostSource.PRICED, CostSource.PRICED),
            # An unpriced contribution makes the combined cost_usd a lower bound.
            (CostSource.PRICED, CostSource.UNKNOWN, CostSource.UNKNOWN),
            (CostSource.UNKNOWN, CostSource.PRICED, CostSource.UNKNOWN),
            (CostSource.SUBSCRIPTION, CostSource.UNKNOWN, CostSource.UNKNOWN),
            # Every contribution known: the summed cost_usd is complete.
            (CostSource.PRICED, CostSource.SUBSCRIPTION, CostSource.PRICED),
            (CostSource.SUBSCRIPTION, CostSource.PRICED, CostSource.PRICED),
            (CostSource.FREE_LOCAL, CostSource.SUBSCRIPTION, CostSource.PRICED),
        ],
    )
    def test_combination(self, current: CostSource | None, new: CostSource, expected: CostSource) -> None:
        assert combine_cost_sources(current, new) == expected


class TestAddUsage:
    def test_accumulates_cost_and_tokens(self, ctx: ExecutionContext) -> None:
        ctx.add_usage(_llm_result())
        ctx.add_usage(_llm_result())

        assert ctx.cost_usd == Decimal("0.20")
        assert ctx.input_tokens == 2000
        assert ctx.output_tokens == 400
        assert ctx.cache_read_tokens == 100
        assert ctx.cache_write_tokens == 50

    def test_tokens_saved_stays_none_when_compression_did_not_run(self, ctx: ExecutionContext) -> None:
        """tokens_saved is None on every path where compression did not run."""
        ctx.add_usage(_llm_result(tokens_saved=None))
        assert ctx.tokens_saved is None

    def test_tokens_saved_zero_is_preserved(self, ctx: ExecutionContext) -> None:
        """Compression ran and saved nothing: that is a real 0, not a missing value."""
        ctx.add_usage(_llm_result(tokens_saved=0))
        assert ctx.tokens_saved == 0

    def test_tokens_saved_accumulates_when_present(self, ctx: ExecutionContext) -> None:
        ctx.add_usage(_llm_result(tokens_saved=120))
        ctx.add_usage(_llm_result(tokens_saved=80))
        assert ctx.tokens_saved == 200

    def test_uncompressed_call_does_not_reset_accumulated_saving(self, ctx: ExecutionContext) -> None:
        ctx.add_usage(_llm_result(tokens_saved=120))
        ctx.add_usage(_llm_result(tokens_saved=None))
        assert ctx.tokens_saved == 120

    def test_uncompressed_step_after_a_compressed_one_reports_none(self, ctx: ExecutionContext) -> None:
        """The window that matters is the step, not the run."""
        ctx.add_usage(_llm_result(tokens_saved=120))
        before = ctx.usage_snapshot()

        ctx.add_usage(_llm_result(tokens_saved=None))

        assert (ctx.usage_snapshot() - before).tokens_saved is None

    def test_snapshot_reflects_accumulation(self, ctx: ExecutionContext) -> None:
        ctx.add_usage(_llm_result())
        assert ctx.usage_snapshot() == TokenUsage(
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=50,
            cache_write_tokens=25,
            tokens_saved=None,
            cache_write_reported_calls=1,
        )

    def test_cache_write_reported_calls_stays_zero_when_never_reported(self, ctx: ExecutionContext) -> None:
        """A Codex-only call (no cache-creation figure at all) must not look like a confirmed zero."""
        ctx.add_usage(_llm_result(cache_creation_tokens=None))
        assert ctx.cache_write_tokens == 0
        assert ctx.usage_snapshot().cache_write_reported_calls == 0

    def test_reasoning_tokens_accumulate_when_reported(self, ctx: ExecutionContext) -> None:
        ctx.add_usage(_llm_result(reasoning_output_tokens=15))
        ctx.add_usage(_llm_result(reasoning_output_tokens=5))
        assert ctx.reasoning_tokens == 20
        assert ctx.usage_snapshot().reasoning_reported_calls == 2

    def test_reasoning_tokens_stay_unreported_when_absent(self, ctx: ExecutionContext) -> None:
        """Every non-Codex LLMResult leaves reasoning_output_tokens at its None default."""
        ctx.add_usage(_llm_result())
        assert ctx.reasoning_tokens == 0
        assert ctx.usage_snapshot().reasoning_reported_calls == 0


def _codex_result(**overrides: Any) -> LLMResult:
    """A result as _dispatch_to_runtime returns it from a model-ignoring runtime."""
    fields: dict[str, Any] = {
        "cost_usd": Decimal("0"),
        "cost_source": CostSource.UNKNOWN,
        "cache_creation_tokens": None,
        "runtime_name": "codex",
    }
    fields.update(overrides)
    return _llm_result(**fields)


class TestAddUsageRuntimeSplit:
    """DevelopStep on Codex feeds three results through add_usage() in one step:
    the /develop dispatch, the priced implementation-notes call, then a fix-loop
    dispatch. The provider CostRecord must describe only the priced call, and the
    runtime record every dispatch (PR #1169 review, thread on context.py add_usage)."""

    def test_runtime_result_does_not_overwrite_provider_cost_source(self, ctx: ExecutionContext) -> None:
        ctx.add_usage(_codex_result())
        ctx.add_usage(_llm_result(cost_source=CostSource.PRICED))
        ctx.add_usage(_codex_result())

        assert ctx.cost_source == CostSource.PRICED
        assert ctx.runtime_cost_source == CostSource.UNKNOWN

    def test_every_runtime_dispatch_accumulates_into_runtime_usage(self, ctx: ExecutionContext) -> None:
        ctx.add_usage(_codex_result(input_tokens=1000, output_tokens=500))
        ctx.add_usage(_llm_result(input_tokens=200, output_tokens=100, cost_source=CostSource.PRICED))
        ctx.add_usage(_codex_result(input_tokens=300, output_tokens=50))

        assert ctx.runtime_usage is not None
        assert (ctx.runtime_usage.input_tokens, ctx.runtime_usage.output_tokens) == (1300, 550)
        assert ctx.runtime_cost_usd == Decimal("0")
        # The step totals still count every call: StepResult.usage and the
        # budget checks read them.
        assert (ctx.input_tokens, ctx.output_tokens) == (1500, 650)
        assert ctx.cost_usd == Decimal("0.10")

    def test_runtime_usage_stays_none_without_a_runtime_result(self, ctx: ExecutionContext) -> None:
        ctx.add_usage(_llm_result(cost_source=CostSource.PRICED))

        assert ctx.runtime_usage is None
        assert ctx.runtime_cost_source is None

    def test_provider_cost_source_does_not_depend_on_call_order(self, ctx: ExecutionContext) -> None:
        ctx.add_usage(_llm_result(cost_source=CostSource.UNKNOWN))
        ctx.add_usage(_llm_result(cost_source=CostSource.PRICED))

        assert ctx.cost_source == CostSource.UNKNOWN

    def test_runtime_cost_source_does_not_depend_on_call_order(self, ctx: ExecutionContext) -> None:
        """A dispatch with no terminal event (UNKNOWN) taints the whole runtime record,
        even when a later dispatch in the same step reports SUBSCRIPTION."""
        ctx.add_usage(_codex_result(cost_source=CostSource.UNKNOWN))
        ctx.add_usage(_codex_result(cost_source=CostSource.SUBSCRIPTION))

        assert ctx.runtime_cost_source == CostSource.UNKNOWN


class _SpendingStep(BaseStep):
    name = "develop"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        ctx.add_usage(_llm_result())
        return StepResult(success=True, summary="done", cost_usd=Decimal("0.10"))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)


class _RaisingStep(BaseStep):
    name = "develop"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        ctx.add_usage(_llm_result())
        raise RuntimeError("boom")

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)


class TestEngineAttribution:
    @pytest.mark.asyncio
    async def test_usage_attached_on_success(self, ctx: ExecutionContext) -> None:
        step = _SpendingStep()
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine._run_step_with_timeout(step)

        assert result.usage is not None
        assert result.usage.input_tokens == 1000
        assert result.usage.output_tokens == 200

    @pytest.mark.asyncio
    async def test_usage_attached_when_step_raises(self, ctx: ExecutionContext) -> None:
        """Tokens burned before a crash are still billed, so they must be attributed."""
        step = _RaisingStep()
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine._run_step_with_timeout(step)

        assert result.success is False
        assert result.usage is not None
        assert result.usage.input_tokens == 1000

    @pytest.mark.asyncio
    async def test_failure_result_carries_accrued_cost(self, ctx: ExecutionContext) -> None:
        """Without cost on the synthetic result, _update_step_execution writes no CostRecord."""
        step = _RaisingStep()
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine._run_step_with_timeout(step)

        assert result.cost_usd == Decimal("0.10")

    @pytest.mark.asyncio
    async def test_delta_excludes_spend_from_earlier_steps(self, ctx: ExecutionContext) -> None:
        """Per-step attribution must not re-count what earlier steps already spent."""
        ctx.add_usage(_llm_result())
        step = _SpendingStep()
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine._run_step_with_timeout(step)

        assert result.usage is not None
        assert result.usage.input_tokens == 1000
        assert ctx.input_tokens == 2000


class TestCostRecordWrite:
    @pytest.fixture(autouse=True)
    async def setup_db(self):
        import os

        from sova.db.session import close_db, init_db

        os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
        await init_db(run_migrations=False)
        yield
        await close_db()
        os.environ.pop("SOVA_DATABASE_URL", None)

    async def _engine_with_run(self, ctx: ExecutionContext) -> WorkflowEngine:
        """StepExecution.task_run_id is NOT NULL, so a real TaskRun is required."""
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session:
            run = TaskRun(issue_number="123", role="developer", status="in_progress")
            session.add(run)
            await session.commit()
            await session.refresh(run)
            run_id = run.id

        engine = WorkflowEngine(steps=[], ctx=ctx)
        engine._task_run_id = run_id
        return engine

    async def test_step_tokens_reach_the_cost_record(self, ctx: ExecutionContext) -> None:
        """End-to-end: a step's usage delta lands in the CostRecord token columns."""
        from sqlalchemy import select

        from sova.db.models import CostRecord
        from sova.db.session import get_session

        engine = await self._engine_with_run(ctx)
        step_exec_id = await engine._create_step_execution("develop")
        result = StepResult(
            success=True,
            summary="done",
            cost_usd=Decimal("0.10"),
            usage=TokenUsage(
                input_tokens=1000,
                output_tokens=200,
                cache_read_tokens=50,
                cache_write_tokens=25,
                cache_write_reported_calls=1,
            ),
        )

        await engine._update_step_execution(step_exec_id, result, elapsed_ms=1234)

        async with await get_session() as session:
            rows = (await session.execute(select(CostRecord))).scalars().all()

        assert len(rows) == 1
        assert rows[0].phase == "develop"
        assert rows[0].input_tokens == 1000
        assert rows[0].output_tokens == 200
        assert rows[0].cache_read_tokens == 50
        assert rows[0].cache_write_tokens == 25
        assert rows[0].cache_tokens == 75
        assert rows[0].tokens_saved is None
        assert rows[0].pre_compression_input_tokens is None

    async def test_pre_compression_input_tokens_derived_when_compression_ran(self, ctx: ExecutionContext) -> None:
        """pre_compression_input_tokens is the compressed input plus what compression saved."""
        from sqlalchemy import select

        from sova.db.models import CostRecord
        from sova.db.session import get_session

        engine = await self._engine_with_run(ctx)
        step_exec_id = await engine._create_step_execution("develop")
        result = StepResult(
            success=True,
            summary="done",
            cost_usd=Decimal("0.10"),
            usage=TokenUsage(input_tokens=1000, tokens_saved=120, compressed_calls=1),
        )

        await engine._update_step_execution(step_exec_id, result, elapsed_ms=1234)

        async with await get_session() as session:
            rows = (await session.execute(select(CostRecord))).scalars().all()

        assert rows[0].tokens_saved == 120
        assert rows[0].pre_compression_input_tokens == 1120

    async def test_failed_step_still_persists_its_spend(self, ctx: ExecutionContext) -> None:
        """A step that spent money and then failed must not vanish from cost records."""
        from sqlalchemy import select

        from sova.db.models import CostRecord
        from sova.db.session import get_session

        engine = await self._engine_with_run(ctx)
        step_exec_id = await engine._create_step_execution("develop")
        result = StepResult(
            success=False,
            summary="Exception in develop",
            error="boom",
            cost_usd=Decimal("0.10"),
            usage=TokenUsage(input_tokens=1000, output_tokens=200),
        )

        await engine._update_step_execution(step_exec_id, result, elapsed_ms=10)

        async with await get_session() as session:
            rows = (await session.execute(select(CostRecord))).scalars().all()

        assert len(rows) == 1
        assert rows[0].input_tokens == 1000
        assert rows[0].cost_usd == Decimal("0.10")

    async def test_cache_write_tokens_null_when_never_reported(self, ctx: ExecutionContext) -> None:
        """A Codex-only step (no cache-creation figure at all) must persist NULL, not a fabricated 0."""
        from sqlalchemy import select

        from sova.db.models import CostRecord
        from sova.db.session import get_session

        engine = await self._engine_with_run(ctx)
        step_exec_id = await engine._create_step_execution("develop")
        result = StepResult(
            success=True,
            summary="done",
            cost_usd=Decimal("0"),
            usage=TokenUsage(input_tokens=1000, output_tokens=200, cache_read_tokens=50),
        )

        await engine._update_step_execution(step_exec_id, result, elapsed_ms=1234)

        async with await get_session() as session:
            rows = (await session.execute(select(CostRecord))).scalars().all()

        assert len(rows) == 1
        assert rows[0].cache_write_tokens is None
        assert rows[0].reasoning_tokens is None

    async def test_reasoning_tokens_persist_when_reported(self, ctx: ExecutionContext) -> None:
        from sqlalchemy import select

        from sova.db.models import CostRecord
        from sova.db.session import get_session

        engine = await self._engine_with_run(ctx)
        step_exec_id = await engine._create_step_execution("develop")
        result = StepResult(
            success=True,
            summary="done",
            cost_usd=Decimal("0"),
            usage=TokenUsage(input_tokens=1000, output_tokens=200, reasoning_tokens=40, reasoning_reported_calls=1),
        )

        await engine._update_step_execution(step_exec_id, result, elapsed_ms=1234)

        async with await get_session() as session:
            rows = (await session.execute(select(CostRecord))).scalars().all()

        assert rows[0].reasoning_tokens == 40

    async def test_missing_usage_writes_zeros_not_a_crash(self, ctx: ExecutionContext) -> None:
        """A step that made no LLM call still records cost, with zeroed token columns."""
        from sqlalchemy import select

        from sova.db.models import CostRecord
        from sova.db.session import get_session

        engine = await self._engine_with_run(ctx)
        step_exec_id = await engine._create_step_execution("commit")
        result = StepResult(success=True, summary="done", cost_usd=Decimal("0.01"))

        await engine._update_step_execution(step_exec_id, result, elapsed_ms=10)

        async with await get_session() as session:
            rows = (await session.execute(select(CostRecord))).scalars().all()

        assert len(rows) == 1
        assert rows[0].input_tokens == 0
        assert rows[0].tokens_saved is None
