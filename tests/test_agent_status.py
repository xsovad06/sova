"""Tests for sova.dashboard.services.agent_status -- agent status aggregator."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import StepExecution, TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for agent status tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def session() -> AsyncSession:
    async with await get_session() as s:
        yield s


NOW = datetime.now(timezone.utc)


# -- Single run status tests -------------------------------------------------


@pytest.mark.asyncio
async def test_running_run(session: AsyncSession):
    """Running run returns correct progress and timing."""
    from sova.dashboard.services.agent_status import get_agent_status

    run = TaskRun(
        issue_number="10",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=5),
    )
    session.add(run)
    await session.flush()

    # Add completed steps
    s1 = StepExecution(
        task_run_id=run.id,
        step_name="sync",
        status="done",
        duration_ms=5000,
        started_at=NOW - timedelta(minutes=5),
        ended_at=NOW - timedelta(minutes=4, seconds=55),
    )
    s2 = StepExecution(
        task_run_id=run.id,
        step_name="assess",
        status="done",
        duration_ms=3000,
        started_at=NOW - timedelta(minutes=4, seconds=55),
        ended_at=NOW - timedelta(minutes=4, seconds=52),
    )
    s3 = StepExecution(
        task_run_id=run.id,
        step_name="create_worktree",
        status="done",
        duration_ms=2000,
        started_at=NOW - timedelta(minutes=4, seconds=52),
        ended_at=NOW - timedelta(minutes=4, seconds=50),
    )
    # Current in-progress step
    s4 = StepExecution(
        task_run_id=run.id,
        step_name="develop",
        status="running",
        duration_ms=0,
        started_at=NOW - timedelta(seconds=30),
    )
    session.add_all([s1, s2, s3, s4])
    await session.commit()

    status = await get_agent_status(run.id)
    assert status is not None
    assert status.run_id == run.id
    assert status.status == "running"
    assert status.role == "developer"
    assert status.pipeline_variant == "developer"
    assert status.current_step == "develop"
    assert status.step_index == 3  # develop is index 3 in developer pipeline
    assert status.total_steps == 15
    assert status.completed_steps == ["sync", "assess", "create_worktree"]
    # 3 completed out of 15 steps
    assert abs(status.step_progress_pct - (3 / 15 * 100)) < 0.01
    # Should have positive time in step (at least ~30 seconds)
    assert status.time_in_step_ms > 0
    assert status.is_stuck is False
    assert status.error_message is None


@pytest.mark.asyncio
async def test_completed_run(session: AsyncSession):
    """Completed (done) run returns 100% progress."""
    from sova.dashboard.services.agent_status import get_agent_status

    run = TaskRun(
        issue_number="11",
        role="developer",
        status="done",
        current_step="handoff_to_reviewer",
        started_at=NOW - timedelta(hours=1),
        ended_at=NOW,
    )
    session.add(run)
    await session.commit()

    status = await get_agent_status(run.id)
    assert status is not None
    assert status.step_progress_pct == 100.0
    assert status.time_in_step_ms == 0
    assert status.is_stuck is False


@pytest.mark.asyncio
async def test_failed_run(session: AsyncSession):
    """Failed run uses completed step count for progress."""
    from sova.dashboard.services.agent_status import get_agent_status

    run = TaskRun(
        issue_number="12",
        role="developer",
        status="failed",
        current_step="develop",
        error_message="Tests failed",
        started_at=NOW - timedelta(minutes=30),
        ended_at=NOW,
    )
    session.add(run)
    await session.flush()

    s1 = StepExecution(
        task_run_id=run.id,
        step_name="sync",
        status="done",
        duration_ms=5000,
        started_at=NOW - timedelta(minutes=30),
    )
    s2 = StepExecution(
        task_run_id=run.id,
        step_name="develop",
        status="failed",
        duration_ms=100000,
        started_at=NOW - timedelta(minutes=29),
    )
    session.add_all([s1, s2])
    await session.commit()

    status = await get_agent_status(run.id)
    assert status is not None
    assert status.status == "failed"
    assert status.error_message == "Tests failed"
    # 1 completed step out of 15
    assert abs(status.step_progress_pct - (1 / 15 * 100)) < 0.01
    assert status.time_in_step_ms == 0  # terminal
    assert status.is_stuck is False


@pytest.mark.asyncio
async def test_stuck_detection_true(session: AsyncSession):
    """Run stuck in a step beyond threshold is detected."""
    from sova.dashboard.services.agent_status import get_agent_status

    run = TaskRun(
        issue_number="13",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=20),
    )
    session.add(run)
    await session.flush()

    # In-progress step started 10 minutes ago
    s1 = StepExecution(
        task_run_id=run.id,
        step_name="develop",
        status="running",
        duration_ms=0,
        started_at=NOW - timedelta(minutes=10),
    )
    session.add(s1)
    await session.commit()

    # Default threshold is 5 min (300,000 ms), 10 min should be stuck
    status = await get_agent_status(run.id)
    assert status is not None
    assert status.is_stuck is True
    assert status.time_in_step_ms > 300_000


@pytest.mark.asyncio
async def test_stuck_detection_false(session: AsyncSession):
    """Run within threshold is not stuck."""
    from sova.dashboard.services.agent_status import get_agent_status

    run = TaskRun(
        issue_number="14",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(seconds=30),
    )
    session.add(run)
    await session.flush()

    s1 = StepExecution(
        task_run_id=run.id,
        step_name="develop",
        status="running",
        duration_ms=0,
        started_at=NOW - timedelta(seconds=10),
    )
    session.add(s1)
    await session.commit()

    status = await get_agent_status(run.id)
    assert status is not None
    assert status.is_stuck is False


@pytest.mark.asyncio
async def test_stuck_custom_threshold(session: AsyncSession):
    """Custom stuck threshold is respected."""
    from sova.dashboard.services.agent_status import get_agent_status

    run = TaskRun(
        issue_number="15",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(seconds=30),
    )
    session.add(run)
    await session.flush()

    s1 = StepExecution(
        task_run_id=run.id,
        step_name="develop",
        status="running",
        duration_ms=0,
        started_at=NOW - timedelta(seconds=10),
    )
    session.add(s1)
    await session.commit()

    # Very low threshold should make it stuck
    status = await get_agent_status(run.id, stuck_threshold_ms=1000)
    assert status is not None
    assert status.is_stuck is True


@pytest.mark.asyncio
async def test_stuck_terminal_always_false(session: AsyncSession):
    """Terminal runs are never stuck regardless of timing."""
    from sova.dashboard.services.agent_status import get_agent_status

    run = TaskRun(
        issue_number="16",
        role="developer",
        status="interrupted",
        current_step="develop",
        started_at=NOW - timedelta(hours=2),
    )
    session.add(run)
    await session.flush()

    # Step started a long time ago
    s1 = StepExecution(
        task_run_id=run.id,
        step_name="develop",
        status="running",
        duration_ms=0,
        started_at=NOW - timedelta(hours=1),
    )
    session.add(s1)
    await session.commit()

    status = await get_agent_status(run.id)
    assert status is not None
    assert status.is_stuck is False
    assert status.time_in_step_ms == 0


@pytest.mark.asyncio
async def test_estimation_with_history(session: AsyncSession):
    """Estimation returns positive value when sufficient history exists."""
    from sova.dashboard.services.agent_status import get_agent_status

    # Create 2 completed historical runs with step durations
    for i in range(2):
        hist_run = TaskRun(
            issue_number=str(100 + i),
            role="developer",
            status="done",
            current_step="handoff_to_reviewer",
            started_at=NOW - timedelta(hours=10 + i),
            ended_at=NOW - timedelta(hours=9 + i),
        )
        session.add(hist_run)
        await session.flush()

        # Add completed steps with durations for all developer pipeline steps
        for step_name, duration in [
            ("sync", 5000),
            ("assess", 3000),
            ("create_worktree", 2000),
            ("develop", 300000),
            ("simplify", 60000),
            ("self_review", 40000),
            ("commit", 10000),
            ("validate", 20000),
            ("push", 5000),
            ("create_pr", 15000),
            ("wait_for_external_reviews", 30000),
            ("address_external_findings", 25000),
            ("monitor_ci", 120000),
            ("extract_memory", 8000),
            ("handoff_to_reviewer", 2000),
        ]:
            session.add(
                StepExecution(
                    task_run_id=hist_run.id,
                    step_name=step_name,
                    status="done",
                    duration_ms=duration,
                    started_at=NOW - timedelta(hours=10),
                )
            )

    # Create the current running run
    run = TaskRun(
        issue_number="20",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=5),
    )
    session.add(run)
    await session.flush()

    # Completed first 3 steps
    for step_name in ["sync", "assess", "create_worktree"]:
        session.add(
            StepExecution(
                task_run_id=run.id,
                step_name=step_name,
                status="done",
                duration_ms=5000,
                started_at=NOW - timedelta(minutes=5),
            )
        )

    session.add(
        StepExecution(
            task_run_id=run.id,
            step_name="develop",
            status="running",
            duration_ms=0,
            started_at=NOW - timedelta(minutes=2),
        )
    )

    await session.commit()

    status = await get_agent_status(run.id)
    assert status is not None
    assert status.estimated_remaining_ms is not None
    assert status.estimated_remaining_ms > 0
    # Remaining steps: develop through handoff_to_reviewer (12 steps)
    # Expected: sum of averages = 635000
    expected = 300000 + 60000 + 40000 + 10000 + 20000 + 5000 + 15000 + 30000 + 25000 + 120000 + 8000 + 2000
    assert abs(status.estimated_remaining_ms - expected) < 1000


@pytest.mark.asyncio
async def test_estimation_partial_step_history(session: AsyncSession):
    """Estimation works when many runs exist but some steps have sparse history."""
    from sova.dashboard.services.agent_status import get_agent_status

    # Create 5 completed historical runs with only early steps (sync, assess, create_worktree)
    for i in range(5):
        hist_run = TaskRun(
            issue_number=str(200 + i),
            role="developer",
            status="done",
            current_step="handoff_to_reviewer",
            started_at=NOW - timedelta(hours=20 + i),
            ended_at=NOW - timedelta(hours=19 + i),
        )
        session.add(hist_run)
        await session.flush()

        for step_name, duration in [
            ("sync", 4000),
            ("assess", 2000),
            ("create_worktree", 1500),
        ]:
            session.add(
                StepExecution(
                    task_run_id=hist_run.id,
                    step_name=step_name,
                    status="done",
                    duration_ms=duration,
                    started_at=NOW - timedelta(hours=20),
                )
            )

    # Only 1 run reached step 4 (develop) -- fewer than _MIN_HISTORY_RUNS per step
    # but enough total runs exist
    hist_run_extra = TaskRun(
        issue_number="205",
        role="developer",
        status="done",
        current_step="handoff_to_reviewer",
        started_at=NOW - timedelta(hours=25),
        ended_at=NOW - timedelta(hours=24),
    )
    session.add(hist_run_extra)
    await session.flush()
    for step_name, duration in [
        ("sync", 5000),
        ("assess", 3000),
        ("create_worktree", 2000),
        ("develop", 300000),
    ]:
        session.add(
            StepExecution(
                task_run_id=hist_run_extra.id,
                step_name=step_name,
                status="done",
                duration_ms=duration,
                started_at=NOW - timedelta(hours=25),
            )
        )

    # Current running run with 2 completed steps
    run = TaskRun(
        issue_number="206",
        role="developer",
        status="running",
        current_step="create_worktree",
        started_at=NOW - timedelta(minutes=1),
    )
    session.add(run)
    await session.flush()
    for step_name in ["sync", "assess"]:
        session.add(
            StepExecution(
                task_run_id=run.id,
                step_name=step_name,
                status="done",
                duration_ms=3000,
                started_at=NOW - timedelta(minutes=1),
            )
        )
    await session.commit()

    status = await get_agent_status(run.id)
    assert status is not None
    # Should NOT return None -- enough historical runs exist (6 >= _MIN_HISTORY_RUNS)
    # But remaining steps include some without full history, so estimation may be None
    # (per finding #8 fix: missing steps -> None). The key fix from finding #1 is that
    # we no longer reject ALL data just because one step has < 2 runs.
    # With 6 runs total >= _MIN_HISTORY_RUNS, the averages dict is populated.
    # However, remaining steps without history cause None (per _compute_estimation
    # line 331: "if any remaining step has no historical data, return None").
    # This is correct behavior -- partial estimation is unreliable.
    assert status.estimated_remaining_ms is None


@pytest.mark.asyncio
async def test_estimation_without_history(session: AsyncSession):
    """Estimation returns None when insufficient history exists."""
    from sova.dashboard.services.agent_status import get_agent_status

    run = TaskRun(
        issue_number="21",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=5),
    )
    session.add(run)
    await session.commit()

    status = await get_agent_status(run.id)
    assert status is not None
    assert status.estimated_remaining_ms is None


@pytest.mark.asyncio
async def test_unknown_step(session: AsyncSession):
    """Unknown current_step (agent sentinel) yields step_index=0 and 0% progress."""
    from sova.dashboard.services.agent_status import get_agent_status

    run = TaskRun(
        issue_number="22",
        role="developer",
        status="running",
        current_step="agent",
        started_at=NOW - timedelta(seconds=5),
    )
    session.add(run)
    await session.commit()

    status = await get_agent_status(run.id)
    assert status is not None
    assert status.step_index == 0
    assert status.step_progress_pct == 0.0


@pytest.mark.asyncio
async def test_nonexistent_run_id():
    """Nonexistent run_id returns None."""
    from sova.dashboard.services.agent_status import get_agent_status

    status = await get_agent_status(99999)
    assert status is None


# -- Bulk fetch tests --------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_fetch(session: AsyncSession):
    """get_all_agent_statuses returns statuses for non-terminal runs only."""
    from sova.dashboard.services.agent_status import get_all_agent_statuses

    # Non-terminal runs
    r1 = TaskRun(
        issue_number="30",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=5),
    )
    r2 = TaskRun(
        issue_number="31",
        role="researcher",
        status="running",
        current_step="research",
        started_at=NOW - timedelta(minutes=3),
    )
    # Terminal runs (should be excluded)
    r3 = TaskRun(
        issue_number="32",
        role="developer",
        status="done",
        current_step="handoff_to_reviewer",
        started_at=NOW - timedelta(hours=1),
    )
    r4 = TaskRun(
        issue_number="33",
        role="developer",
        status="failed",
        current_step="develop",
        started_at=NOW - timedelta(hours=2),
    )
    session.add_all([r1, r2, r3, r4])
    await session.commit()

    statuses = await get_all_agent_statuses()
    assert len(statuses) == 2
    run_ids = {s.run_id for s in statuses}
    assert r1.id in run_ids
    assert r2.id in run_ids
    assert r3.id not in run_ids
    assert r4.id not in run_ids


@pytest.mark.asyncio
async def test_bulk_fetch_empty(session: AsyncSession):
    """get_all_agent_statuses returns empty list when no active runs."""
    from sova.dashboard.services.agent_status import get_all_agent_statuses

    # Only terminal runs
    r1 = TaskRun(
        issue_number="40",
        role="developer",
        status="done",
        started_at=NOW - timedelta(hours=1),
    )
    session.add(r1)
    await session.commit()

    statuses = await get_all_agent_statuses()
    assert statuses == []


@pytest.mark.asyncio
async def test_researcher_pipeline(session: AsyncSession):
    """Researcher role is detected correctly."""
    from sova.dashboard.services.agent_status import get_agent_status

    run = TaskRun(
        issue_number="50",
        role="researcher",
        status="running",
        current_step="research",
        started_at=NOW - timedelta(minutes=2),
    )
    session.add(run)
    await session.flush()

    s1 = StepExecution(
        task_run_id=run.id,
        step_name="fetch_task",
        status="done",
        duration_ms=3000,
        started_at=NOW - timedelta(minutes=2),
    )
    session.add(s1)
    await session.commit()

    status = await get_agent_status(run.id)
    assert status is not None
    assert status.pipeline_variant == "researcher"
    assert status.total_steps == 4
    assert len(status.completed_steps) == 1


@pytest.mark.asyncio
async def test_address_review_pipeline(session: AsyncSession):
    """Address-review variant is detected from pr_number + agent sentinel."""
    from sova.dashboard.services.agent_status import get_agent_status

    run = TaskRun(
        issue_number="51",
        role="developer",
        status="running",
        current_step="address_review",
        pr_number=42,
        started_at=NOW - timedelta(minutes=2),
    )
    session.add(run)
    await session.commit()

    status = await get_agent_status(run.id)
    assert status is not None
    assert status.pipeline_variant == "address_review"
    assert status.total_steps == 9


@pytest.mark.asyncio
async def test_no_steps_yet(session: AsyncSession):
    """Run with no StepExecution records yet falls back to TaskRun.started_at."""
    from sova.dashboard.services.agent_status import get_agent_status

    run = TaskRun(
        issue_number="60",
        role="developer",
        status="running",
        current_step="sync",
        started_at=NOW - timedelta(seconds=10),
    )
    session.add(run)
    await session.commit()

    status = await get_agent_status(run.id)
    assert status is not None
    assert status.completed_steps == []
    assert status.step_progress_pct == 0.0
    assert status.time_in_step_ms > 0  # falls back to TaskRun.started_at
