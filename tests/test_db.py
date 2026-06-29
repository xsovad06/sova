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
            retry_count=2,
        )
        session.add(step)
        await session.commit()
        await session.refresh(step)

        assert step.task_run_id == run.id
        assert step.cost_usd == Decimal("1.50")
        assert step.retry_count == 2


async def test_step_execution_retry_count_defaults_to_zero() -> None:
    """retry_count defaults to 0 when not specified."""
    async with await get_session() as session:
        run = TaskRun(issue_number="11", role="developer", status="in_progress")
        session.add(run)
        await session.commit()
        await session.refresh(run)

        step = StepExecution(
            task_run_id=run.id,
            step_name="sync",
            status="success",
            cost_usd=Decimal("0"),
            duration_ms=100,
        )
        session.add(step)
        await session.commit()
        await session.refresh(step)

        assert step.retry_count == 0


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
        assert cost.model_selection_reason is None


async def test_cost_record_with_model_selection_reason() -> None:
    """CostRecord stores model_selection_reason when provided."""
    async with await get_session() as session:
        cost = CostRecord(
            phase="triage",
            issue="99",
            model="haiku",
            cost_usd=Decimal("0.01"),
            model_selection_reason="role:triage->haiku",
        )
        session.add(cost)
        await session.commit()
        await session.refresh(cost)

        assert cost.model_selection_reason == "role:triage->haiku"


def _import_migration_011():
    """Import migration 011 via importlib (module name starts with a digit)."""
    import importlib

    return importlib.import_module("sova.db.migrations.versions.011_add_model_selection_reason")


_MIG_011_MODULE = "sova.db.migrations.versions.011_add_model_selection_reason"


async def test_migration_011_column_exists_helper() -> None:
    """Migration 011 _column_exists correctly detects model_selection_reason column."""
    mod = _import_migration_011()

    from unittest.mock import MagicMock, patch

    from alembic import op

    mock_conn = MagicMock()
    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [
        {"name": "id"},
        {"name": "model"},
        {"name": "model_selection_reason"},
    ]

    with patch.object(op, "get_bind", return_value=mock_conn), patch("sqlalchemy.inspect", return_value=mock_inspector):
        assert mod._column_exists("cost_records", "model_selection_reason") is True
        assert mod._column_exists("cost_records", "nonexistent") is False


async def test_migration_011_upgrade_skip_when_exists() -> None:
    """Migration 011 upgrade is idempotent -- skips if column already exists."""
    from unittest.mock import patch

    mod = _import_migration_011()

    with (
        patch(f"{_MIG_011_MODULE}._column_exists", return_value=True) as mock_exists,
        patch(f"{_MIG_011_MODULE}.op") as mock_op,
    ):
        mod.upgrade()
        mock_exists.assert_called_once_with("cost_records", "model_selection_reason")
        mock_op.add_column.assert_not_called()


async def test_migration_011_upgrade_adds_column() -> None:
    """Migration 011 upgrade adds column when it doesn't exist."""
    from unittest.mock import patch

    mod = _import_migration_011()

    with patch(f"{_MIG_011_MODULE}._column_exists", return_value=False), patch(f"{_MIG_011_MODULE}.op") as mock_op:
        mod.upgrade()
        mock_op.add_column.assert_called_once()


async def test_migration_011_downgrade_drops_column() -> None:
    """Migration 011 downgrade drops column when it exists."""
    from unittest.mock import patch

    mod = _import_migration_011()

    with patch(f"{_MIG_011_MODULE}._column_exists", return_value=True), patch(f"{_MIG_011_MODULE}.op") as mock_op:
        mod.downgrade()
        mock_op.drop_column.assert_called_once_with("cost_records", "model_selection_reason")


async def test_migration_011_downgrade_skip_when_missing() -> None:
    """Migration 011 downgrade is idempotent -- skips if column doesn't exist."""
    from unittest.mock import patch

    mod = _import_migration_011()

    with patch(f"{_MIG_011_MODULE}._column_exists", return_value=False), patch(f"{_MIG_011_MODULE}.op") as mock_op:
        mod.downgrade()
        mock_op.drop_column.assert_not_called()


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


# ---------------------------------------------------------------------------
# Migration fallback self-healing
# ---------------------------------------------------------------------------


class TestMigrationFallback:
    """_run_migrations fallback should self-heal corrupted alembic_version."""

    async def test_bogus_version_self_heals_on_fallback(self, tmp_path) -> None:
        """A bogus alembic_version should be dropped during fallback so stamp succeeds."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _run_migrations

        db_path = tmp_path / "test.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        # First run: create tables and stamp at head
        await _run_migrations(engine)

        # Corrupt: set a bogus version_num
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = 'bogus_xyz'"))

        # Second run: should fallback and self-heal
        await _run_migrations(engine)

        # Verify: alembic_version should have the real head, not 'bogus_xyz'
        async with engine.connect() as conn:
            row = await conn.run_sync(lambda c: c.execute(text("SELECT version_num FROM alembic_version")).fetchone())
        assert row is not None
        assert row[0] != "bogus_xyz"

        await engine.dispose()

    async def test_empty_alembic_version_self_heals(self, tmp_path) -> None:
        """An empty alembic_version table (case 4) should be dropped and re-stamped."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _run_migrations

        db_path = tmp_path / "test.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        await _run_migrations(engine)

        # Corrupt: empty the version table
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM alembic_version"))

        await _run_migrations(engine)

        async with engine.connect() as conn:
            row = await conn.run_sync(lambda c: c.execute(text("SELECT version_num FROM alembic_version")).fetchone())
        assert row is not None
        assert row[0] != ""

        await engine.dispose()


# ---------------------------------------------------------------------------
# Issue-less TaskRun (nullable issue_number + run_label)
# ---------------------------------------------------------------------------


async def test_create_issueless_task_run() -> None:
    """Create a TaskRun with no issue_number (project-scope role)."""
    async with await get_session() as session:
        run = TaskRun(
            issue_number=None,
            run_label="planner-1718640000",
            role="planner",
            status="pending",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        assert run.id is not None
        assert not run.issue_number  # None or empty string
        assert run.run_label == "planner-1718640000"
        assert run.role == "planner"


async def test_issueless_task_run_empty_string() -> None:
    """Create a TaskRun with empty string issue_number."""
    async with await get_session() as session:
        run = TaskRun(
            issue_number="",
            run_label="sprint-planner",
            role="planner",
            status="running",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        assert run.issue_number == ""
        assert run.run_label == "sprint-planner"


async def test_normalize_issue_number_handles_none() -> None:
    """The validator should accept None for issue-less runs."""
    run = TaskRun(issue_number=None, role="planner", status="pending")
    assert not run.issue_number  # None or empty string


async def test_normalize_issue_number_strips_hash() -> None:
    """The validator should strip '#' prefix."""
    run = TaskRun(issue_number="#42", role="developer", status="pending")
    assert run.issue_number == "42"


async def test_query_issueless_runs() -> None:
    """Query runs with NULL issue_number."""
    async with await get_session() as session:
        session.add(TaskRun(issue_number=None, run_label="plan-a", role="planner", status="done"))
        session.add(TaskRun(issue_number="42", role="developer", status="done"))
        session.add(TaskRun(issue_number=None, run_label="plan-b", role="planner", status="running"))
        session.add(TaskRun(issue_number="", run_label="plan-c", role="planner", status="done"))
        await session.commit()

        # Query runs without a real issue number (None or empty)
        result = await session.execute(
            select(TaskRun).where(TaskRun.issue_number.is_(None) | (TaskRun.issue_number == ""))
        )
        issueless = result.scalars().all()
        assert len(issueless) == 3
        labels = {r.run_label for r in issueless}
        assert labels == {"plan-a", "plan-b", "plan-c"}
