"""dispatch() records a TaskRun for a role that runs outside WorkflowEngine (issue #1403).

A terminal ``sova run`` of the reviewer used to leave no row and no CostRecord: the review's model,
tokens and cost were only in ``sova.log``. dispatch() now opens and finalizes the row when the caller
passed none, and the reviewer writes a CostRecord per review call.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.db.models import CostRecord, TaskRun
from sova.db.session import get_session
from sova.llm.models import CostSource, LLMResult
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.roles.dispatcher import dispatch

pytestmark = pytest.mark.usefixtures("reset_db_rows")


class _ScriptedRole(AgentRole):
    """A role outside WorkflowEngine whose execute() returns, or raises, what the test asks for."""

    name = "scripted"

    def __init__(self, outcome: RoleResult | BaseException) -> None:
        self._outcome = outcome

    async def assess_task(self, task: Task) -> TaskAssessment:  # pragma: no cover (not dispatched to)
        raise NotImplementedError

    async def execute(self, ctx: ExecutionContext) -> RoleResult:
        ctx.pr_number = 7
        ctx.add_cost(Decimal("0.40"))
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _ctx(tmp_path: Path, **fields: object) -> ExecutionContext:
    return ExecutionContext(
        project_dir=tmp_path, config=ProjectConfig(), adapter=MagicMock(), issue_number="42", role="x", **fields
    )


async def _dispatch(ctx: ExecutionContext, role: AgentRole) -> RoleResult:
    with patch("sova.roles.dispatcher.get_role_async", AsyncMock(return_value=role)):
        _, result = await dispatch(ctx, role_name=role.name)
    return result


async def _runs() -> list[TaskRun]:
    async with await get_session() as session:
        return list((await session.execute(select(TaskRun).order_by(TaskRun.id))).scalars())


class TestDispatchOpensTaskRun:
    @pytest.mark.parametrize(
        ("outcome", "status", "error"),
        [
            (RoleResult(success=True, summary="ok"), "done", None),
            (RoleResult(success=False, summary="no", error="review failed"), "failed", "review failed"),
            (RoleResult(success=False, summary="paused", awaiting_approval=True), "awaiting_approval", None),
        ],
    )
    async def test_role_outside_the_engine_gets_a_finalized_row(
        self, tmp_path: Path, outcome: RoleResult, status: str, error: str | None
    ) -> None:
        ctx = _ctx(tmp_path)

        await _dispatch(ctx, _ScriptedRole(outcome))

        (run,) = await _runs()
        assert ctx.task_run_id == run.id
        assert (run.issue_number, run.role, run.status, run.pr_number) == ("42", "scripted", status, 7)
        assert run.error_message == error
        assert run.ended_at is not None
        assert run.current_step is None
        # sova watch never calls the CLI's cost persist: the row carries the role's own spend itself.
        assert run.total_cost_usd == Decimal("0.40")

    async def test_a_raising_role_is_recorded_as_failed_and_the_error_propagates(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)

        with pytest.raises(RuntimeError, match="provider exploded"):
            await _dispatch(ctx, _ScriptedRole(RuntimeError("provider exploded")))

        (run,) = await _runs()
        assert (run.status, run.error_message) == ("failed", "provider exploded")

    async def test_a_cancelled_role_is_left_running_for_the_liveness_sweep(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)

        with pytest.raises(asyncio.CancelledError):
            await _dispatch(ctx, _ScriptedRole(asyncio.CancelledError()))

        (run,) = await _runs()
        assert run.status == "running"
        assert run.ended_at is None

    async def test_a_passed_task_run_is_not_duplicated(self, tmp_path: Path) -> None:
        """The dashboard creates the row and passes --run-id: dispatch() must not open a second one."""
        ctx = _ctx(tmp_path, task_run_id=99)

        await _dispatch(ctx, _ScriptedRole(RoleResult(success=True, summary="ok")))

        assert await _runs() == []
        assert ctx.task_run_id == 99

    async def test_a_row_another_path_already_closed_keeps_its_status(self, tmp_path: Path) -> None:
        """The dashboard finalizer or liveness sweep may close the row first: dispatch must not reopen it."""
        ctx = _ctx(tmp_path)

        class _ClosedByOthers(_ScriptedRole):
            async def execute(self, ctx: ExecutionContext) -> RoleResult:
                async with await get_session() as session, session.begin():
                    run = await session.get(TaskRun, ctx.task_run_id)
                    run.status = "interrupted"
                return await super().execute(ctx)

        await _dispatch(ctx, _ClosedByOthers(RoleResult(success=True, summary="ok")))

        (run,) = await _runs()
        assert run.status == "interrupted"
        assert run.total_cost_usd == Decimal("0.40")

    async def test_finalization_completes_when_the_dispatching_task_is_cancelled(self, tmp_path: Path) -> None:
        """SIGTERM landing during the finalizing write must not leave a finished run as running."""
        from sova.roles import dispatcher

        ctx = _ctx(tmp_path)
        real = dispatcher._finalize_task_run
        started = asyncio.Event()

        async def slow_finalize(*args: object, **kwargs: object) -> None:
            started.set()
            await asyncio.sleep(0.05)
            await real(*args, **kwargs)

        with patch("sova.roles.dispatcher._finalize_task_run", slow_finalize):
            task = asyncio.ensure_future(_dispatch(ctx, _ScriptedRole(RoleResult(success=True, summary="ok"))))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        (run,) = await _runs()
        assert run.status == "done"
        assert run.ended_at is not None

    async def test_row_creation_completes_when_the_dispatching_task_is_cancelled(self, tmp_path: Path) -> None:
        """SIGTERM landing during the creating write must still assign ctx.task_run_id to the committed row."""
        from sova.roles import dispatcher

        ctx = _ctx(tmp_path)
        real = dispatcher._create_task_run
        started = asyncio.Event()

        async def slow_create(*args: object, **kwargs: object) -> int | None:
            started.set()
            await asyncio.sleep(0.05)
            return await real(*args, **kwargs)

        with patch("sova.roles.dispatcher._create_task_run", slow_create):
            task = asyncio.ensure_future(_dispatch(ctx, _ScriptedRole(RoleResult(success=True, summary="ok"))))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        (run,) = await _runs()
        assert run.status == "running"
        assert ctx.task_run_id == run.id

    async def test_a_failed_row_write_runs_the_role_untracked(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)

        with patch("sova.roles.dispatcher.get_session", AsyncMock(side_effect=RuntimeError("db down"))):
            result = await _dispatch(ctx, _ScriptedRole(RoleResult(success=True, summary="ok")))

        assert result.success
        assert ctx.task_run_id is None
        assert await _runs() == []

    @pytest.mark.parametrize("role_cls_path", ["developer.DeveloperRole", "researcher.ResearcherRole"])
    async def test_engine_roles_create_their_own_row(self, tmp_path: Path, role_cls_path: str) -> None:
        import importlib

        module, cls = role_cls_path.split(".")
        role_cls = getattr(importlib.import_module(f"sova.roles.{module}"), cls)
        assert role_cls.creates_own_task_run
        role = role_cls()
        ctx = _ctx(tmp_path)

        with patch.object(role_cls, "execute", AsyncMock(return_value=RoleResult(success=True, summary="ok"))):
            await _dispatch(ctx, role)

        assert await _runs() == []

    def test_only_the_engine_roles_and_custom_roles_record_their_own_run(self) -> None:
        from sova.roles.custom import CustomRole
        from sova.roles.planner import PlannerRole
        from sova.roles.reviewer import ReviewerRole
        from sova.roles.triage import TriageRole

        # A custom role's DAG nodes each start a command run with its own TaskRun.
        assert CustomRole.creates_own_task_run
        assert not any(role.creates_own_task_run for role in (ReviewerRole, TriageRole, PlannerRole))


class TestTerminalReviewerRunIsRecorded:
    async def test_terminal_review_writes_its_run_and_a_cost_record_per_call(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.get_task = AsyncMock(return_value=Task(id="42", title="Fix it", body="", state=TaskState.IN_REVIEW))
        for name in ("post_pr_review", "post_pr_comment", "add_label", "remove_label"):
            setattr(adapter, name, AsyncMock())
        ctx = ExecutionContext(
            project_dir=tmp_path, config=ProjectConfig(), adapter=adapter, issue_number="42", role="reviewer"
        )
        ctx.pr_number, ctx.branch_name = 7, "feat/issue-42"
        review = LLMResult(
            text=json.dumps({"findings": [], "summary": "fine"}),
            model="gpt-6.1-sol",
            cost_usd=Decimal("0.0123"),
            cost_source=CostSource.PRICED,
            input_tokens=2100,
            output_tokens=320,
        )

        with (
            patch("sova.roles.reviewer.get_pr_diff", AsyncMock(return_value="diff --git a/a.py b/a.py\n")),
            patch("sova.roles.reviewer.get_pr_files", AsyncMock(return_value=["a.py"])),
            patch("sova.roles.reviewer.get_pr_head_sha", AsyncMock(return_value="abc1234")),
            patch("sova.roles.reviewer.invoke", AsyncMock(return_value=review)),
        ):
            _, result = await dispatch(ctx, role_name="reviewer")

        assert result.success, result.error
        (run,) = await _runs()
        assert (run.role, run.status, run.pr_number, run.issue_number) == ("reviewer", "done", 7, "42")
        async with await get_session() as session:
            records = list((await session.execute(select(CostRecord))).scalars())
        assert [(r.task_run_id, r.phase, r.model, r.input_tokens, r.output_tokens) for r in records] == [
            (run.id, "review", "gpt-6.1-sol", 2100, 320)
        ]
        assert (records[0].cost_usd, records[0].cost_source) == (Decimal("0.0123"), "priced")


class TestPanelReviewKeepsItsCalls:
    def test_record_call_keeps_the_result_for_its_cost_record(self) -> None:
        from sova.roles._review_comments import ReviewResult
        from sova.roles.panel_review import _PanelState

        state = _PanelState(result=ReviewResult(), budget_remaining=Decimal("1.00"))
        call = LLMResult(text="{}", model="gpt-6.1-sol", cost_usd=Decimal("0.25"))

        state.record_call(call)

        assert state.result.llm_results == [call]
        assert (state.result.total_cost, state.budget_remaining) == (Decimal("0.25"), Decimal("0.75"))


class TestCancelledReviewKeepsCompletedCalls:
    async def test_cost_records_survive_a_cancellation_after_a_completed_call(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.get_task = AsyncMock(return_value=Task(id="42", title="Fix it", body="", state=TaskState.IN_REVIEW))
        ctx = ExecutionContext(
            project_dir=tmp_path, config=ProjectConfig(), adapter=adapter, issue_number="42", role="reviewer"
        )
        ctx.pr_number, ctx.branch_name = 7, "feat/issue-42"
        first = LLMResult(text="not json", model="gpt-6.1-sol", cost_usd=Decimal("0.02"), cost_source=CostSource.PRICED)
        # The first call completes (and its output fails validation); the schema retry is cancelled.
        invoke = AsyncMock(side_effect=[first, asyncio.CancelledError()])

        with (
            patch("sova.roles.reviewer.get_pr_diff", AsyncMock(return_value="diff --git a/a.py b/a.py\n")),
            patch("sova.roles.reviewer.get_pr_files", AsyncMock(return_value=["a.py"])),
            patch("sova.roles.reviewer.get_pr_head_sha", AsyncMock(return_value="abc1234")),
            patch("sova.roles.reviewer.invoke", invoke),
            pytest.raises(asyncio.CancelledError),
        ):
            await dispatch(ctx, role_name="reviewer")

        async with await get_session() as session:
            records = list((await session.execute(select(CostRecord))).scalars())
        assert [(r.phase, r.model, r.cost_usd) for r in records] == [("review", "gpt-6.1-sol", Decimal("0.02"))]

    async def test_a_completed_retry_is_counted_when_a_later_retry_is_cancelled(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.get_task = AsyncMock(return_value=Task(id="42", title="Fix it", body="", state=TaskState.IN_REVIEW))
        ctx = ExecutionContext(
            project_dir=tmp_path, config=ProjectConfig(), adapter=adapter, issue_number="42", role="reviewer"
        )
        ctx.pr_number, ctx.branch_name = 7, "feat/issue-42"
        first = LLMResult(text="not json", model="gpt-6.1-sol", cost_usd=Decimal("0.02"), cost_source=CostSource.PRICED)
        retry = LLMResult(text="still not json", model="gpt-6.1-sol", cost_usd=Decimal("0.03"))
        invoke = AsyncMock(side_effect=[first, retry, asyncio.CancelledError()])

        with (
            patch("sova.roles.reviewer.get_pr_diff", AsyncMock(return_value="diff --git a/a.py b/a.py\n")),
            patch("sova.roles.reviewer.get_pr_files", AsyncMock(return_value=["a.py"])),
            patch("sova.roles.reviewer.get_pr_head_sha", AsyncMock(return_value="abc1234")),
            patch("sova.roles.reviewer.invoke", invoke),
            pytest.raises(asyncio.CancelledError),
        ):
            await dispatch(ctx, role_name="reviewer")

        assert ctx.cost_usd == Decimal("0.05")
        async with await get_session() as session:
            records = list((await session.execute(select(CostRecord))).scalars())
        assert sorted(r.cost_usd for r in records) == [Decimal("0.02"), Decimal("0.03")]

    async def test_cost_records_are_written_to_the_context_project_database(self, tmp_path: Path) -> None:
        from sova.roles.reviewer import ReviewerRole, ReviewResult

        ctx = ExecutionContext(
            project_dir=tmp_path, config=ProjectConfig(), adapter=MagicMock(), issue_number="42", role="reviewer"
        )
        review = ReviewResult()
        review.llm_results.append(LLMResult(text="x", model="m", cost_usd=Decimal("0.01")))

        with patch("sova.llm.cost.record_cost", AsyncMock()) as recorder:
            await ReviewerRole()._record_review_costs(ctx, review)

        assert recorder.await_args.kwargs["project_dir"] == tmp_path
