"""Tests for per-step token telemetry: context accumulation and CostRecord attribution."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
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


class TestNumTurnsParsing:
    """build_claude_cli_result() keeps the CLI's own turn count (issue #1336)."""

    def test_num_turns_read_from_result_event(self) -> None:
        from sova.llm.models import build_claude_cli_result

        assert build_claude_cli_result({"num_turns": 52}).num_turns == 52

    @pytest.mark.parametrize("raw", [None, "52", 52.0, True, -1])
    def test_absent_or_non_integer_num_turns_is_none(self, raw: object) -> None:
        """None means "not reported", never zero: a bool, a string, a float or a negative is not a turn count."""
        from sova.llm.models import build_claude_cli_result

        data = {} if raw is None else {"num_turns": raw}
        assert build_claude_cli_result(data).num_turns is None

    def test_accumulated_results_sum_turns_and_keep_none_only_when_both_unreported(self) -> None:
        from sova.llm.models import accumulate_llm_result

        assert accumulate_llm_result(_llm_result(num_turns=3), _llm_result(num_turns=4)).num_turns == 7
        assert accumulate_llm_result(_llm_result(num_turns=3), _llm_result()).num_turns == 3
        assert accumulate_llm_result(_llm_result(), _llm_result()).num_turns is None


class TestTurnCounters:
    def test_add_usage_counts_turns_and_reporting_calls(self, ctx: ExecutionContext) -> None:
        ctx.add_usage(_llm_result(num_turns=5))
        ctx.add_usage(_llm_result())  # a provider that reports no turns (Codex)
        ctx.add_usage(_llm_result(num_turns=2))

        snapshot = ctx.usage_snapshot()
        assert snapshot.turns == 7
        assert snapshot.turns_reported_calls == 2

    def test_delta_and_sum_carry_turns(self) -> None:
        after = TokenUsage(turns=9, turns_reported_calls=3)
        before = TokenUsage(turns=4, turns_reported_calls=1)

        assert (after - before) == TokenUsage(turns=5, turns_reported_calls=2)
        assert (before + before) == TokenUsage(turns=8, turns_reported_calls=2)

    def test_routed_spend_bucket_carries_turns(self, ctx: ExecutionContext) -> None:
        ctx.add_usage(
            _llm_result(model="cheap", routed_task_type_model="cheap", num_turns=3),
            routed_reason="task_type:fix",
        )

        assert ctx.routed_spend["cheap"].usage.turns == 3
        assert ctx.routed_spend["cheap"].usage.turns_reported_calls == 1


class TestNumTurnsCostRecord:
    @pytest.fixture(autouse=True)
    async def setup_db(self) -> AsyncIterator[None]:
        import os

        from sova.db.session import close_db, init_db

        os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
        await init_db(run_migrations=False)
        yield
        await close_db()
        os.environ.pop("SOVA_DATABASE_URL", None)

    async def _engine_with_run(self, ctx: ExecutionContext) -> WorkflowEngine:
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

    async def _step_records(self, ctx: ExecutionContext, usage: TokenUsage) -> list:
        from sqlalchemy import select

        from sova.db.models import CostRecord
        from sova.db.session import get_session

        engine = await self._engine_with_run(ctx)
        step_exec_id = await engine._create_step_execution("develop")
        result = StepResult(success=True, summary="done", cost_usd=Decimal("0.10"), usage=usage)
        await engine._update_step_execution(step_exec_id, result, elapsed_ms=10)

        async with await get_session() as session:
            return list((await session.execute(select(CostRecord))).scalars().all())

    async def test_step_turns_reach_the_cost_record(self, ctx: ExecutionContext) -> None:
        rows = await self._step_records(ctx, TokenUsage(input_tokens=1000, turns=12, turns_reported_calls=2))

        assert len(rows) == 1
        assert rows[0].num_turns == 12

    async def test_turns_null_when_no_call_reported_them(self, ctx: ExecutionContext) -> None:
        """NULL means not reported (Codex, legacy rows), never zero."""
        rows = await self._step_records(ctx, TokenUsage(input_tokens=1000))

        assert rows[0].num_turns is None

    async def test_a_share_reporting_only_turns_is_still_recorded(self, ctx: ExecutionContext) -> None:
        """The provider-record gate counts reported turns, so a zero-cost, zero-token share keeps its turn count."""
        from sqlalchemy import select

        from sova.db.models import CostRecord
        from sova.db.session import get_session

        engine = await self._engine_with_run(ctx)
        step_exec_id = await engine._create_step_execution("develop")
        result = StepResult(
            success=True, summary="done", cost_usd=Decimal("0"), usage=TokenUsage(turns=3, turns_reported_calls=1)
        )
        await engine._update_step_execution(step_exec_id, result, elapsed_ms=10)

        async with await get_session() as session:
            rows = list((await session.execute(select(CostRecord))).scalars().all())

        assert [row.num_turns for row in rows] == [3]

    async def test_record_cost_writes_num_turns(self) -> None:
        """Direct-call roles (triage) record through record_cost(), which carries the turn count too."""
        from sova.llm.cost import record_cost

        record = await record_cost(_llm_result(num_turns=4), phase="triage", issue="123")
        unreported = await record_cost(_llm_result(), phase="triage", issue="123")

        assert record.num_turns == 4
        assert unreported.num_turns is None


def _assistant(message_id: str, *, usage: dict | None = None, command: str | None = None, text: str = "") -> dict:
    """One stream-json assistant event: the CLI repeats a message across one event per content block."""
    block: dict = {"type": "text", "text": text}
    if command is not None:
        block = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    message: dict = {"id": message_id, "content": [block]}
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "message": message}


def _usage(input_tokens: int, cache_read: int, cache_write: int) -> dict:
    return {
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "output_tokens": 1,
    }


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class TestPhaseTally:
    def test_turns_before_the_first_marker_go_to_unmarked(self) -> None:
        from sova.llm.phase_usage import PhaseTally

        tally = PhaseTally(started_at=100.0, clock=_FakeClock())
        tally.observe(_assistant("m1", usage=_usage(2, 100, 10)))

        assert tally.snapshot() == [
            {
                "phase": "unmarked",
                "model_calls": 1,
                "input_tokens": 2,
                "cache_read_tokens": 100,
                "cache_write_tokens": 10,
                "context_tokens": 112,
                "duration_ms": 0,
            }
        ]

    def test_a_repeated_message_id_is_one_model_call_counted_in_the_phase_of_its_first_event(self) -> None:
        """The marker's own message started in the old phase; the switch applies from the next message."""
        from sova.llm.phase_usage import PhaseTally

        clock = _FakeClock()
        tally = PhaseTally(started_at=100.0, clock=clock)
        tally.observe(_assistant("m1", usage=_usage(1, 50, 5), text="thinking"))
        clock.now = 102.5
        tally.observe(_assistant("m1", usage=_usage(1, 50, 5), command='echo "::sova-phase::ci-wait"\ngh pr checks 1'))
        clock.now = 104.0
        tally.observe(_assistant("m2", usage=_usage(1, 60, 0)))

        phases = {p["phase"]: p for p in tally.snapshot()}
        assert phases["unmarked"]["model_calls"] == 1
        assert phases["unmarked"]["context_tokens"] == 56
        assert phases["unmarked"]["duration_ms"] == 2500
        assert phases["ci-wait"]["model_calls"] == 1
        assert phases["ci-wait"]["context_tokens"] == 61
        assert phases["ci-wait"]["duration_ms"] == 1500

    def test_a_phase_entered_twice_accumulates_into_one_entry_in_first_seen_order(self) -> None:
        from sova.llm.phase_usage import PhaseTally

        tally = PhaseTally(started_at=100.0, clock=_FakeClock())
        for i, marker in enumerate(["fix", "ci-wait", "fix"]):
            tally.observe(_assistant(f"marker-{i}", usage=_usage(1, 1, 0), command=f"echo '::sova-phase::{marker}'"))
            tally.observe(_assistant(f"work-{i}", usage=_usage(1, 1, 0)))

        snapshot = tally.snapshot()
        assert [p["phase"] for p in snapshot] == ["unmarked", "fix", "ci-wait"]
        assert [p["model_calls"] for p in snapshot] == [1, 3, 2]

    @pytest.mark.parametrize(
        "command",
        [
            "grep -n '::sova-phase::ci-wait' integrate-pr.md",  # a command that mentions the marker
            'echo "the marker looks like ::sova-phase::ci-wait"',  # prose, not a standalone marker
            'echo "::sova-phase::Bad_Name"',  # not a valid phase name
        ],
    )
    def test_only_a_standalone_marker_line_switches_the_phase(self, command: str) -> None:
        from sova.llm.phase_usage import PhaseTally

        tally = PhaseTally(started_at=100.0, clock=_FakeClock())
        tally.observe(_assistant("m1", usage=_usage(1, 1, 0), command=command))
        tally.observe(_assistant("m2", usage=_usage(1, 1, 0)))

        assert [p["phase"] for p in tally.snapshot()] == ["unmarked"]

    def test_the_last_marker_in_one_command_wins(self) -> None:
        """An agent that merges two phase blocks into one Bash call lands in the later phase."""
        from sova.llm.phase_usage import PhaseTally

        tally = PhaseTally(started_at=100.0, clock=_FakeClock())
        merged = 'echo "::sova-phase::fix"\ngit status\necho "::sova-phase::squash-rebase"'
        tally.observe(_assistant("m1", usage=_usage(1, 1, 0), command=merged))
        tally.observe(_assistant("m2", usage=_usage(1, 1, 0)))

        snapshot = tally.snapshot()
        assert snapshot[-1]["phase"] == "squash-rebase"
        assert snapshot[-1]["model_calls"] == 1

    def test_a_marker_from_a_non_bash_tool_is_ignored(self) -> None:
        from sova.llm.phase_usage import PhaseTally

        tally = PhaseTally(started_at=100.0, clock=_FakeClock())
        event = _assistant("m1", usage=_usage(1, 1, 0))
        event["message"]["content"] = [
            {"type": "tool_use", "name": "Write", "input": {"command": 'echo "::sova-phase::fix"'}}
        ]
        tally.observe(event)
        tally.observe(_assistant("m2", usage=_usage(1, 1, 0)))

        assert [p["phase"] for p in tally.snapshot()] == ["unmarked"]

    def test_a_subagent_message_is_a_model_call_in_the_current_phase(self) -> None:
        """Messages with parent_tool_use_id set come from a subagent: real model calls, charged where they ran."""
        from sova.llm.phase_usage import PhaseTally

        tally = PhaseTally(started_at=100.0, clock=_FakeClock())
        tally.observe(_assistant("m1", usage=_usage(1, 1, 0), command='echo "::sova-phase::fix"'))
        subagent = _assistant("sub-1", usage=_usage(1, 40, 0))
        subagent["parent_tool_use_id"] = "toolu_1"
        tally.observe(subagent)

        assert tally.snapshot()[1] == {**tally.snapshot()[1], "phase": "fix", "model_calls": 1, "cache_read_tokens": 40}

    def test_no_per_message_usage_snapshots_to_none(self) -> None:
        """A stream without per-message usage stores NULL, never a fabricated zero breakdown."""
        from sova.llm.phase_usage import PhaseTally

        tally = PhaseTally(started_at=100.0, clock=_FakeClock())
        assert tally.snapshot() is None
        tally.observe(_assistant("m1"))
        tally.observe({"type": "user", "message": {"content": []}})
        assert tally.snapshot() is None

    def test_snapshot_does_not_close_the_open_phase(self) -> None:
        """Snapshots are taken while the run is live too; reading one must not shift later durations."""
        from sova.llm.phase_usage import PhaseTally

        clock = _FakeClock()
        tally = PhaseTally(started_at=100.0, clock=clock)
        tally.observe(_assistant("m1", usage=_usage(1, 1, 0)))
        clock.now = 101.0
        assert tally.snapshot()[0]["duration_ms"] == 1000
        clock.now = 103.0
        assert tally.snapshot()[0]["duration_ms"] == 3000


_FIXTURE = Path(__file__).parent / "fixtures" / "address_pr_two_markers.stream.jsonl"


def _command_agent(role: str = "command:address-pr") -> Any:
    from sova.dashboard.services.agent_pool import AgentState

    return AgentState(run_id=1, issue="1336", role=role, process=MagicMock(), project_dir=Path("."))


class TestStreamReplay:
    """Replays the first seven messages of command:address-pr run 2921 with two markers inserted.

    Message ids, the repeated event per content block and every per-message usage figure are the
    recorded ones; text and commands are replaced, and the markers sit on messages 2 and 5.
    """

    def test_phase_breakdown_from_a_recorded_stream(self) -> None:
        from sova.dashboard.services.agent_output import _parse_stream_line

        agent = _command_agent()
        for line in _FIXTURE.read_text(encoding="utf-8").splitlines():
            _parse_stream_line(line, agent)

        phases = {p["phase"]: p for p in agent.phase_tally.snapshot()}
        assert list(phases) == ["unmarked", "fix", "push-ci-wait"]
        assert [phases[p]["model_calls"] for p in phases] == [2, 3, 2]
        assert phases["unmarked"]["context_tokens"] == 76_563 + 91_205
        assert phases["fix"]["context_tokens"] == 93_944 + 96_629 + 96_954
        assert phases["push-ci-wait"]["context_tokens"] == 98_163 + 98_772
        assert phases["fix"]["cache_read_tokens"] == 91_203 + 93_942 + 96_627
        # The CLI's num_turns counts user-side messages (each tool result, plus the prompt), a different
        # quantity from model calls: it is stored as reported, never derived from the phase counts.
        assert agent.last_result_num_turns == 9

    def test_num_turns_sums_across_result_events(self) -> None:
        """A session that resumes after background tasks emits one result per segment, each with its own
        num_turns (run 2915: 43, 1, 4, 3, 1, 1 and 9), while total_cost_usd is cumulative."""
        from sova.dashboard.services.agent_output import _parse_stream_line

        agent = _command_agent()
        for turns, cost in ((43, 2.49), (1, 2.54), (9, 3.42)):
            _parse_stream_line(json.dumps({"type": "result", "num_turns": turns, "total_cost_usd": cost}), agent)

        assert agent.last_result_num_turns == 53
        assert agent.last_result_cost == 3.42

    def test_a_result_without_num_turns_keeps_the_running_total(self) -> None:
        from sova.dashboard.services.agent_output import _parse_stream_line

        agent = _command_agent()
        _parse_stream_line(json.dumps({"type": "result", "num_turns": 5}), agent)
        _parse_stream_line(json.dumps({"type": "result"}), agent)

        assert agent.last_result_num_turns == 5

    def test_only_command_runs_keep_a_tally(self) -> None:
        assert _command_agent(role="reviewer").phase_tally is None
        assert _command_agent(role="command:integrate-pr").phase_tally is not None


class TestFinalizeWritesTurnsAndPhases:
    @pytest.fixture(autouse=True)
    async def setup_db(self) -> AsyncIterator[None]:
        import os

        from sova.db.session import close_db, init_db

        os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
        await init_db(run_migrations=False)
        yield
        await close_db()
        os.environ.pop("SOVA_DATABASE_URL", None)

    async def _run(self, status: str) -> int:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session:
            run = TaskRun(issue_number="1336", role="command:address-pr", status=status)
            session.add(run)
            await session.commit()
            await session.refresh(run)
            return run.id

    def _replayed_agent(self, run_id: int) -> Any:
        from sova.dashboard.services.agent_output import _parse_stream_line

        agent = _command_agent()
        agent.run_id = run_id
        agent.project_dir = None
        for line in _FIXTURE.read_text(encoding="utf-8").splitlines():
            _parse_stream_line(line, agent)
        return agent

    async def test_finalize_writes_phase_usage_and_agent_record_turns(self) -> None:
        from sqlalchemy import select

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.db.models import CostRecord, TaskRun
        from sova.db.session import get_session

        run_id = await self._run("running")
        await _finalize_task_run(run_id, exit_code=0, agent=self._replayed_agent(run_id))

        async with await get_session() as session:
            task_run = await session.get(TaskRun, run_id)
            record = (await session.execute(select(CostRecord).where(CostRecord.task_run_id == run_id))).scalar_one()

        assert [p["phase"] for p in task_run.phase_usage] == ["unmarked", "fix", "push-ci-wait"]
        assert record.phase == "agent"
        assert record.num_turns == 9

    async def test_phase_usage_written_when_the_run_is_already_terminal(self) -> None:
        """The terminal-status early return must not drop the breakdown."""
        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        run_id = await self._run("done")
        await _finalize_task_run(run_id, exit_code=0, agent=self._replayed_agent(run_id))

        async with await get_session() as session:
            task_run = await session.get(TaskRun, run_id)

        assert task_run.phase_usage is not None

    async def test_run_without_a_tally_keeps_phase_usage_null(self) -> None:
        from sqlalchemy import text

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        run_id = await self._run("running")
        agent = _command_agent(role="reviewer")
        agent.run_id = run_id
        agent.project_dir = None
        await _finalize_task_run(run_id, exit_code=0, agent=agent)

        async with await get_session() as session:
            task_run = await session.get(TaskRun, run_id)
            # SQL NULL, not the JSON text "null" (database-guidelines.md, "JSON Column NULL Gotcha"),
            # so a report can filter on phase_usage IS NULL.
            is_sql_null = (
                await session.execute(text("SELECT phase_usage IS NULL FROM task_runs WHERE id = :id"), {"id": run_id})
            ).scalar_one()

        assert task_run.phase_usage is None
        assert is_sql_null == 1

    async def test_explicit_none_is_stored_as_sql_null(self) -> None:
        """A later write of None (clearing a breakdown) must not turn into the JSON text "null"."""
        from sqlalchemy import text

        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session:
            run = TaskRun(issue_number="1336", role="command:address-pr", status="done", phase_usage=None)
            session.add(run)
            await session.commit()
            is_sql_null = (
                await session.execute(text("SELECT phase_usage IS NULL FROM task_runs WHERE id = :id"), {"id": run.id})
            ).scalar_one()

        assert is_sql_null == 1
