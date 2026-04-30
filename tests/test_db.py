"""Tests for SOVA database operations."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import inspect, select

from sova.db.models import CostRecord, FailureRecord, Memory, StepExecution, TaskAssessmentRecord, TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db(tmp_path):
    """Initialize a fresh in-memory DB for each test."""
    import os

    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


async def test_create_task_run() -> None:
    """Create and retrieve a task run."""
    async with await get_session() as session:
        run = TaskRun(
            issue_number="42",
            role="developer",
            status="pending",
            project_slug="test-project",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        assert run.id is not None
        assert run.issue_number == "42"
        assert run.role == "developer"
        assert run.status == "pending"
        assert run.total_cost_usd == Decimal("0")
        assert run.started_at is not None


async def test_create_step_execution() -> None:
    """Create a step execution linked to a task run."""
    async with await get_session() as session:
        run = TaskRun(issue_number="10", role="developer", status="in_progress")
        session.add(run)
        await session.commit()
        await session.refresh(run)

        step = StepExecution(
            task_run_id=run.id,
            step_name="develop",
            status="success",
            cost_usd=Decimal("1.50"),
            duration_ms=45000,
            output_summary="Implemented feature X",
        )
        session.add(step)
        await session.commit()
        await session.refresh(step)

        assert step.task_run_id == run.id
        assert step.cost_usd == Decimal("1.50")


async def test_create_failure_record() -> None:
    """Create a failure record with context."""
    async with await get_session() as session:
        run = TaskRun(issue_number="99", role="developer", status="failed")
        session.add(run)
        await session.commit()
        await session.refresh(run)

        failure = FailureRecord(
            task_run_id=run.id,
            step_name="develop",
            failure_type="gate_check",
            message="Development produced no code changes",
            context={"git_diff": "", "worktree": "/tmp/wt"},
        )
        session.add(failure)
        await session.commit()
        await session.refresh(failure)

        assert failure.failure_type == "gate_check"
        assert failure.context["git_diff"] == ""
        assert failure.resolved is False


async def test_create_cost_record() -> None:
    """Create a cost record for an LLM invocation."""
    async with await get_session() as session:
        cost = CostRecord(
            phase="step4-develop",
            issue="42",
            model="claude-opus-4",
            input_tokens=5000,
            output_tokens=2000,
            cost_usd=Decimal("0.35"),
            duration_ms=12000,
        )
        session.add(cost)
        await session.commit()
        await session.refresh(cost)

        assert cost.model == "claude-opus-4"
        assert cost.cost_usd == Decimal("0.35")


async def test_create_memory() -> None:
    """Create a memory entry."""
    async with await get_session() as session:
        memory = Memory(
            category="learning",
            title="Always run migrations before tests",
            content="The test database needs current migrations to pass.",
            tags="testing,database",
            repo="user/project",
            issue_number="42",
        )
        session.add(memory)
        await session.commit()
        await session.refresh(memory)

        assert memory.category == "learning"
        assert memory.tier == "project"


async def test_create_task_assessment() -> None:
    """Create a task assessment record."""
    async with await get_session() as session:
        assessment = TaskAssessmentRecord(
            issue_number="55",
            project_slug="test",
            suitability="ready",
            confidence=0.85,
            reasoning="Well-defined task with clear acceptance criteria",
            missing_context=[],
            estimated_complexity="simple",
            suggested_role="developer",
        )
        session.add(assessment)
        await session.commit()
        await session.refresh(assessment)

        assert assessment.suitability == "ready"
        assert float(assessment.confidence) == pytest.approx(0.85)


async def test_task_assessment_default_project_slug() -> None:
    """TaskAssessmentRecord without explicit project_slug defaults to empty string."""
    async with await get_session() as session:
        assessment = TaskAssessmentRecord(
            issue_number="99",
            suitability="needs_spec",
            confidence=0.60,
            reasoning="Missing acceptance criteria",
        )
        session.add(assessment)
        await session.commit()
        await session.refresh(assessment)

        assert assessment.project_slug == ""
        assert assessment.issue_number == "99"


async def test_query_task_runs_by_status() -> None:
    """Query task runs filtered by status."""
    async with await get_session() as session:
        session.add(TaskRun(issue_number="1", status="done", role="developer"))
        session.add(TaskRun(issue_number="2", status="in_progress", role="developer"))
        session.add(TaskRun(issue_number="3", status="done", role="reviewer"))
        await session.commit()

        result = await session.execute(select(TaskRun).where(TaskRun.status == "done"))
        done_runs = result.scalars().all()
        assert len(done_runs) == 2


async def test_assessments_project_slug_index_exists() -> None:
    """task_assessments table has an index on project_slug for multi-project filtering."""
    async with await get_session() as session:
        conn = await session.connection()
        indexes = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_indexes("task_assessments"))
    index_names = {idx["name"] for idx in indexes}
    assert "ix_assessments_project_slug" in index_names


async def test_memories_superseded_by_index_exists() -> None:
    """memories table has an index on superseded_by for search() filtering."""
    async with await get_session() as session:
        conn = await session.connection()
        indexes = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_indexes("memories"))
    index_names = {idx["name"] for idx in indexes}
    assert "ix_memories_superseded_by" in index_names


async def test_filter_memories_by_superseded_by() -> None:
    """Queries filtering on superseded_by benefit from the new index."""
    async with await get_session() as session:
        m1 = Memory(category="learning", title="Old", content="Replaced", tags="")
        session.add(m1)
        await session.commit()
        await session.refresh(m1)

        m2 = Memory(category="learning", title="New", content="Current", tags="")
        session.add(m2)
        await session.commit()
        await session.refresh(m2)

        m1.superseded_by = m2.id
        await session.commit()

        result = await session.execute(select(Memory).where(Memory.superseded_by.is_(None)))
        active = result.scalars().all()
        assert len(active) == 1
        assert active[0].title == "New"


async def test_filter_assessments_by_project_slug() -> None:
    """Queries filtering on project_slug benefit from the new index."""
    async with await get_session() as session:
        session.add(
            TaskAssessmentRecord(
                issue_number="1",
                project_slug="alpha",
                suitability="ready",
                confidence=0.9,
                reasoning="Good",
            )
        )
        session.add(
            TaskAssessmentRecord(
                issue_number="2",
                project_slug="beta",
                suitability="ready",
                confidence=0.8,
                reasoning="OK",
            )
        )
        session.add(
            TaskAssessmentRecord(
                issue_number="3",
                project_slug="alpha",
                suitability="needs_spec",
                confidence=0.6,
                reasoning="Thin",
            )
        )
        await session.commit()

        result = await session.execute(select(TaskAssessmentRecord).where(TaskAssessmentRecord.project_slug == "alpha"))
        alpha = result.scalars().all()
        assert len(alpha) == 2
