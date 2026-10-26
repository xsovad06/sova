"""Tests for sova.dashboard.services.agent_status: agent status aggregator."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

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


async def _status_for(run_id: int, **kwargs: object) -> object | None:
    """The get_all_agent_statuses() entry for *run_id*, or None if the run is not listed."""
    from sova.dashboard.services.agent_status import get_all_agent_statuses

    return next((s for s in await get_all_agent_statuses(**kwargs) if s.run_id == run_id), None)


# Single run status tests -------------------------------------------------
#
# Each test reads one run's entry from get_all_agent_statuses(), the list the
# WebSocket producer broadcasts. Terminal runs are never listed, so they have
# no status of their own to check (see test_terminal_runs_are_excluded).


@pytest.mark.asyncio
async def test_running_run(session: AsyncSession):
    """Running run returns correct progress and timing."""
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

    status = await _status_for(run.id)
    assert status is not None
    assert status.run_id == run.id
    assert status.status == "running"
    assert status.role == "developer"
    assert status.pipeline_variant == "developer"
    assert status.current_step == "develop"
    assert status.step_index == 4  # develop is index 4 in developer pipeline
    assert status.total_steps == 17
    assert status.completed_steps == ["sync", "assess", "create_worktree"]
    # 3 completed out of 17 steps
    assert abs(status.step_progress_pct - (3 / 17 * 100)) < 0.01
    # Should have positive time in step (at least ~30 seconds)
    assert status.time_in_step_ms > 0
    assert status.time_in_step_known is True  # from the in-progress "develop" StepExecution
    assert status.is_stuck is False
    assert status.error_message is None


@pytest.mark.asyncio
async def test_passed_status_counted_as_completed(session: AsyncSession):
    """Steps with legacy 'passed' status are counted as completed in progress."""
    run = TaskRun(
        issue_number="13",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=5),
    )
    session.add(run)
    await session.flush()

    session.add_all(
        [
            StepExecution(
                task_run_id=run.id,
                step_name="sync",
                status="passed",
                duration_ms=3000,
                started_at=NOW - timedelta(minutes=5),
                ended_at=NOW - timedelta(minutes=4, seconds=57),
            ),
            StepExecution(
                task_run_id=run.id,
                step_name="assess",
                status="passed",
                duration_ms=2000,
                started_at=NOW - timedelta(minutes=4, seconds=57),
                ended_at=NOW - timedelta(minutes=4, seconds=55),
            ),
            StepExecution(
                task_run_id=run.id,
                step_name="create_worktree",
                status="done",
                duration_ms=500,
                started_at=NOW - timedelta(minutes=4, seconds=55),
                ended_at=NOW - timedelta(minutes=4, seconds=54),
            ),
            StepExecution(
                task_run_id=run.id,
                step_name="develop",
                status="running",
                duration_ms=0,
                started_at=NOW - timedelta(seconds=30),
            ),
        ]
    )
    await session.commit()

    status = await _status_for(run.id)
    assert status is not None
    assert status.completed_steps == ["sync", "assess", "create_worktree"]
    assert abs(status.step_progress_pct - (3 / 17 * 100)) < 0.01


@pytest.mark.asyncio
async def test_stuck_detection_true(session: AsyncSession):
    """Run stuck in a step beyond threshold is detected."""
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
    status = await _status_for(run.id)
    assert status is not None
    assert status.is_stuck is True
    assert status.time_in_step_ms > 300_000


@pytest.mark.asyncio
async def test_stuck_detection_false(session: AsyncSession):
    """Run within threshold is not stuck."""
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

    status = await _status_for(run.id)
    assert status is not None
    assert status.is_stuck is False


@pytest.mark.asyncio
async def test_stuck_custom_threshold(session: AsyncSession):
    """Custom stuck threshold is respected."""
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
    status = await _status_for(run.id, stuck_threshold_ms=1000)
    assert status is not None
    assert status.is_stuck is True


@pytest.mark.asyncio
async def test_estimation_with_history(session: AsyncSession):
    """Estimation returns positive value when sufficient history exists."""
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
            ("capture_baseline", 1000),
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
            ("confidence_score", 4000),
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

    status = await _status_for(run.id)
    assert status is not None
    assert status.estimated_remaining_ms is not None
    assert status.estimated_remaining_ms > 0
    # Remaining steps: capture_baseline through handoff_to_reviewer (14 steps)
    # Expected: sum of averages = 640000
    expected = (
        1000 + 300000 + 60000 + 40000 + 10000 + 20000 + 5000 + 15000 + 30000 + 25000 + 120000 + 4000 + 8000 + 2000
    )
    assert abs(status.estimated_remaining_ms - expected) < 1000


@pytest.mark.asyncio
async def test_estimation_partial_step_history(session: AsyncSession):
    """Estimation works when many runs exist but some steps have sparse history."""
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

    # Only 1 run reached step 4 (develop): fewer than _MIN_HISTORY_RUNS per step
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

    status = await _status_for(run.id)
    assert status is not None
    # Should NOT return None: enough historical runs exist (6 >= _MIN_HISTORY_RUNS)
    # But remaining steps include some without full history, so estimation may be None
    # (per finding #8 fix: missing steps -> None). The key fix from finding #1 is that
    # we no longer reject ALL data just because one step has < 2 runs.
    # With 6 runs total >= _MIN_HISTORY_RUNS, the averages dict is populated.
    # However, remaining steps without history cause None (per _compute_estimation
    # line 331: "if any remaining step has no historical data, return None").
    # This is correct behavior: partial estimation is unreliable.
    assert status.estimated_remaining_ms is None


@pytest.mark.asyncio
async def test_estimation_without_history(session: AsyncSession):
    """Estimation returns None when insufficient history exists."""
    run = TaskRun(
        issue_number="21",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=5),
    )
    session.add(run)
    await session.commit()

    status = await _status_for(run.id)
    assert status is not None
    assert status.estimated_remaining_ms is None


@pytest.mark.asyncio
async def test_unknown_step(session: AsyncSession):
    """Unknown current_step (agent sentinel) yields step_index=0 and 0% progress."""
    run = TaskRun(
        issue_number="22",
        role="developer",
        status="running",
        current_step="agent",
        started_at=NOW - timedelta(seconds=5),
    )
    session.add(run)
    await session.commit()

    status = await _status_for(run.id)
    assert status is not None
    assert status.step_index == 0
    assert status.step_progress_pct == 0.0


# Bulk fetch tests --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["done", "failed", "rejected", "interrupted", "paused", "awaiting_approval", "stopped"]
)
async def test_terminal_runs_are_excluded(session: AsyncSession, status: str):
    """A terminal run is never listed, however long ago its in-progress step started,
    so it can never be reported as stuck."""
    from sova.dashboard.services.agent_db import _TERMINAL_STATUSES

    assert status in _TERMINAL_STATUSES
    run = TaskRun(
        issue_number="16",
        role="developer",
        status=status,
        current_step="develop",
        started_at=NOW - timedelta(hours=2),
    )
    session.add(run)
    await session.flush()
    session.add(
        StepExecution(
            task_run_id=run.id,
            step_name="develop",
            status="running",
            duration_ms=0,
            started_at=NOW - timedelta(hours=1),
        )
    )
    await session.commit()

    assert await _status_for(run.id) is None


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

    status = await _status_for(run.id)
    assert status is not None
    assert status.pipeline_variant == "researcher"
    assert status.total_steps == 4
    assert len(status.completed_steps) == 1


@pytest.mark.asyncio
async def test_address_review_pipeline(session: AsyncSession):
    """Address-review variant is detected from pr_number + agent sentinel."""
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

    status = await _status_for(run.id)
    assert status is not None
    assert status.pipeline_variant == "address_review"
    assert status.total_steps == 11


@pytest.mark.asyncio
async def test_no_steps_yet(session: AsyncSession):
    """Run with no StepExecution records yet falls back to TaskRun.started_at."""
    run = TaskRun(
        issue_number="60",
        role="developer",
        status="running",
        current_step="sync",
        started_at=NOW - timedelta(seconds=10),
    )
    session.add(run)
    await session.commit()

    status = await _status_for(run.id)
    assert status is not None
    assert status.completed_steps == []
    assert status.step_progress_pct == 0.0
    assert status.time_in_step_ms > 0  # falls back to TaskRun.started_at
    assert status.time_in_step_known is False  # fallback is not a genuine in-step measurement


# REST-parity fields (issue #1051) --------------------------------------
#
# AgentStatus must be a strict superset of a single agent dict from
# get_unified_agents() (the /agents/active REST shape) so the WebSocket
# broadcast and the REST poll never disagree on what a field means.


@pytest.mark.asyncio
async def test_rest_parity_fields_from_task_run(session: AsyncSession, monkeypatch):
    """issue/pid/elapsed_seconds/cost_usd/pr_number come straight from TaskRun."""
    monkeypatch.setattr("sova.dashboard.services.agent_pool.list_all_pools", lambda: {})

    run = TaskRun(
        issue_number="70",
        role="developer",
        status="running",
        current_step="develop",
        pid=12345,
        pr_number=99,
        total_cost_usd=Decimal("1.234500"),
        started_at=NOW - timedelta(minutes=2),
    )
    session.add(run)
    await session.commit()

    status = await _status_for(run.id)
    assert status is not None
    assert status.issue == "70"
    assert status.pr_number == 99
    assert status.cost_usd == "1.234500"
    assert status.elapsed_seconds >= 120


@pytest.mark.asyncio
async def test_rest_parity_defaults_with_no_live_process(session: AsyncSession, monkeypatch):
    """No matching pool entry and no alive pid: resource fields absent, source defaults to dashboard."""
    monkeypatch.setattr("sova.dashboard.services.agent_pool.list_all_pools", lambda: {})

    run = TaskRun(
        issue_number="71",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=1),
    )
    session.add(run)
    await session.commit()

    status = await _status_for(run.id)
    assert status is not None
    assert status.pid is None
    assert status.cpu_percent is None
    assert status.memory_rss_bytes is None
    assert status.output_lines == 0
    assert status.source == "dashboard"
    # TaskRun.total_cost_usd defaults to Decimal("0") (Numeric(10, 6)), not None, so this
    # exercises decimal_to_json()'s non-None branch; decimal_to_json(None) -> "0.00" is
    # covered separately below for a genuinely unset value.
    assert status.cost_usd == "0.000000"


def test_decimal_to_json_none_defaults_to_zero_string():
    """decimal_to_json(None) is the "0.00" fallback used when total_cost_usd is genuinely unset."""
    from sova.utils.formatting import decimal_to_json

    assert decimal_to_json(None) == "0.00"


@pytest.mark.asyncio
async def test_null_cost_db_row_serializes_at_full_precision(session: AsyncSession, monkeypatch):
    """A DB row with total_cost_usd=None (e.g. a pre-migration row SQLite backfilled as
    NULL regardless of the model's Python-side default) must still serialize cost_usd at
    the same precision as every other branch through get_all_agent_statuses() (the list the
    WebSocket producer broadcasts). decimal_to_json(None) alone would answer "0.00", silently
    disagreeing with the "0.000000" every other source produces; format_cost_usd() must be
    used at every cost site instead.
    """
    from sova.dashboard.services.agent_status import get_all_agent_statuses

    monkeypatch.setattr("sova.dashboard.services.agent_pool.list_all_pools", lambda: {})

    run = TaskRun(
        issue_number="73",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=1),
    )
    run.total_cost_usd = None
    session.add(run)
    await session.commit()

    all_statuses = await get_all_agent_statuses()
    matching = [s for s in all_statuses if s.run_id == run.id]
    assert len(matching) == 1
    assert matching[0].cost_usd == "0.000000"


@pytest.mark.asyncio
async def test_rest_parity_dashboard_pool_resource_sample(session: AsyncSession, monkeypatch, tmp_path: Path):
    """A run tracked in the in-memory dashboard pool reports its latest resource sample."""
    run = TaskRun(
        issue_number="72",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=1),
    )
    session.add(run)
    await session.commit()

    project_dir = tmp_path / "sova-test-project-1051"
    fake_pool = SimpleNamespace(
        project_dir=project_dir,
        agents={
            run.id: SimpleNamespace(
                resource_collector=SimpleNamespace(
                    samples=[SimpleNamespace(cpu_percent=12.5, memory_rss_bytes=104857600)]
                ),
                output_lines=["one", "two", "three"],
            )
        },
    )
    monkeypatch.setattr("sova.dashboard.services.agent_pool.list_all_pools", lambda: {"p": fake_pool})

    status = await _status_for(run.id, project_dir=project_dir)
    assert status is not None
    assert status.source == "dashboard"
    assert status.cpu_percent == 12.5
    assert status.memory_rss_bytes == 104857600
    assert status.output_lines == 3


@pytest.mark.asyncio
async def test_rest_parity_external_source_for_alive_untracked_pid(session: AsyncSession, monkeypatch):
    """A run with a live PID the dashboard pool doesn't know about is CLI/externally-spawned."""
    monkeypatch.setattr("sova.dashboard.services.agent_pool.list_all_pools", lambda: {})
    monkeypatch.setattr("sova.utils.process.is_process_alive", lambda pid: True)

    run = TaskRun(
        issue_number="73",
        role="developer",
        status="running",
        current_step="develop",
        pid=54321,
        started_at=NOW - timedelta(minutes=1),
    )
    session.add(run)
    await session.commit()

    status = await _status_for(run.id)
    assert status is not None
    assert status.source == "external"
    assert status.cpu_percent is None
    assert status.memory_rss_bytes is None


@pytest.mark.asyncio
async def test_bulk_fetch_groups_steps_by_run(session: AsyncSession, monkeypatch):
    """get_all_agent_statuses groups StepExecutions by run_id before computing status."""
    from sova.dashboard.services.agent_status import get_all_agent_statuses

    monkeypatch.setattr("sova.dashboard.services.agent_pool.list_all_pools", lambda: {})

    r1 = TaskRun(
        issue_number="80",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=5),
    )
    r2 = TaskRun(
        issue_number="81",
        role="developer",
        status="running",
        current_step="assess",
        started_at=NOW - timedelta(minutes=2),
    )
    session.add_all([r1, r2])
    await session.flush()

    session.add_all(
        [
            StepExecution(
                task_run_id=r1.id,
                step_name="sync",
                status="done",
                duration_ms=1000,
                started_at=NOW - timedelta(minutes=5),
            ),
            StepExecution(
                task_run_id=r2.id,
                step_name="sync",
                status="done",
                duration_ms=1000,
                started_at=NOW - timedelta(minutes=2),
            ),
        ]
    )
    await session.commit()

    statuses = await get_all_agent_statuses()
    by_id = {s.run_id: s for s in statuses}
    assert by_id[r1.id].completed_steps == ["sync"]
    assert by_id[r2.id].completed_steps == ["sync"]


@pytest.mark.asyncio
async def test_get_all_agent_statuses_skips_run_that_raises(session: AsyncSession, monkeypatch):
    """One run's status computation raising does not abort the whole listing."""
    from sova.dashboard.services.agent_status import get_all_agent_statuses

    monkeypatch.setattr("sova.dashboard.services.agent_pool.list_all_pools", lambda: {})

    good = TaskRun(
        issue_number="91",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=1),
    )
    bad = TaskRun(
        issue_number="92",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=1),
    )
    session.add_all([good, bad])
    await session.commit()

    from sova.dashboard.services import agent_lifecycle as agent_lifecycle_module

    real_get_step_progress = agent_lifecycle_module.get_step_progress
    call_count = {"n": 0}

    async def side_effect(current_step, *, role, pr_number, project_dir=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("boom")
        return await real_get_step_progress(current_step, role=role, pr_number=pr_number, project_dir=project_dir)

    monkeypatch.setattr(agent_lifecycle_module, "get_step_progress", side_effect)

    statuses = await get_all_agent_statuses()
    # Exactly one run failed and was skipped; the other succeeded.
    assert len(statuses) == 1


@pytest.mark.asyncio
async def test_get_unified_agents_is_strict_subset_of_agent_status(session: AsyncSession, monkeypatch):
    """Mechanically verify the module docstring's claim: every field in a single
    get_unified_agents() agent dict (the /agents/active REST shape) must also appear on the
    matching AgentStatus, with the same value and the same type. Prior review rounds found two
    violations of this exact claim (cost_usd type, current_step normalization) by inspection
    alone; this test catches the next one mechanically instead of relying on inspection again.
    """
    from dataclasses import asdict

    from sova.dashboard.services.agent_lifecycle import get_unified_agents

    monkeypatch.setattr("sova.dashboard.services.agent_pool.list_all_pools", lambda: {})
    monkeypatch.setattr("sova.utils.process.is_process_alive", lambda pid: True)
    monkeypatch.setattr("sova.dashboard.services.agent_recovery._is_process_alive", lambda pid: True)

    run = TaskRun(
        issue_number="110",
        role="developer",
        status="running",
        current_step="develop",
        pid=54399,
        pr_number=12,
        total_cost_usd=Decimal("2.500000"),
        started_at=NOW - timedelta(minutes=4),
    )
    session.add(run)
    await session.commit()

    unified = await get_unified_agents()
    matches = [a for a in unified["agents"] if a["run_id"] == run.id]
    assert len(matches) == 1
    agent_dict = matches[0]

    status = await _status_for(run.id)
    assert status is not None
    status_dict = asdict(status)

    assert set(agent_dict) <= set(status_dict)
    for key, value in agent_dict.items():
        if key == "elapsed_seconds":
            # Each side computes elapsed independently against "now" at a
            # slightly different instant; equal to within a second is enough.
            assert abs(value - status_dict[key]) <= 1, key
            continue
        assert value == status_dict[key], key
        assert type(value) is type(status_dict[key]), key


@pytest.mark.asyncio
async def test_get_unified_agents_dashboard_pool_branch_is_strict_subset_of_agent_status(
    session: AsyncSession, monkeypatch, tmp_path: Path
):
    """Same mechanical parity check as test_get_unified_agents_is_strict_subset_of_agent_status,
    but for the dashboard-pool branch (get_all_agents), which the other test cannot reach: patching
    list_all_pools to {} there forces get_unified_agents() down its external/CLI branch, which reads
    the same TaskRun columns AgentStatus does. The dashboard-pool branch is the common case and the
    only one with genuinely independent sources for the same field names: elapsed_seconds is
    time.monotonic()-based (AgentState.started_at) here vs AgentStatus's wall-clock TaskRun.started_at
    delta, pid is agent.process.pid vs TaskRun.pid, output_lines is len(agent.output_lines) vs
    _compute_live_fields's own read of that deque, and status/current_step come from
    _fetch_run_states's DB row vs AgentStatus's own independent read of the same row. Drift between
    either pair of sources can land here with the other parity test still green.
    """
    import time
    from collections import deque
    from dataclasses import asdict

    from sova.dashboard.services.agent_lifecycle import get_unified_agents
    from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

    project_dir = tmp_path / "sova-test-project-1051-pool"
    fake_pid = 54322
    elapsed_s = 240

    # Both sides' elapsed clocks must be seeded from the same instant, taken fresh here rather
    # than from the module-level NOW (captured at import time, which can be well behind "now" by
    # the time this test actually runs in a large suite): one side is wall-clock
    # (TaskRun.started_at), the other time.monotonic() (AgentState.started_at).
    wall_now = datetime.now(timezone.utc)

    run = TaskRun(
        issue_number="111",
        role="developer",
        status="running",
        current_step="develop",
        pid=fake_pid,
        pr_number=13,
        total_cost_usd=Decimal("3.250000"),
        started_at=wall_now - timedelta(seconds=elapsed_s),
    )
    session.add(run)
    await session.commit()

    agent_state = AgentState(
        run_id=run.id,
        issue="111",
        role="developer",
        process=SimpleNamespace(pid=fake_pid),
        output_lines=deque(["a", "b", "c"], maxlen=5000),
        started_at=time.monotonic() - elapsed_s,
        resource_collector=SimpleNamespace(samples=[SimpleNamespace(cpu_percent=22.5, memory_rss_bytes=209715200)]),
        project_dir=project_dir,
    )
    pa = ProjectAgents(project_dir=project_dir, max_concurrent=2)
    pa.agents[run.id] = agent_state

    monkeypatch.setattr("sova.dashboard.services.agent_lifecycle._get_project_agents", lambda slug=None: pa)
    monkeypatch.setattr("sova.dashboard.services.agent_pool.list_all_pools", lambda: {"fake": pa})

    unified = await get_unified_agents()
    matches = [a for a in unified["agents"] if a["run_id"] == run.id]
    assert len(matches) == 1
    agent_dict = matches[0]
    assert agent_dict["source"] == "dashboard"

    status = await _status_for(run.id, project_dir=project_dir)
    assert status is not None
    status_dict = asdict(status)

    assert set(agent_dict) <= set(status_dict)
    for key, value in agent_dict.items():
        if key == "elapsed_seconds":
            # One side is time.monotonic()-based, the other wall-clock: equal
            # to within a second is enough.
            assert abs(value - status_dict[key]) <= 1, key
            continue
        assert value == status_dict[key], key
        assert type(value) is type(status_dict[key]), key


@pytest.mark.asyncio
async def test_normalizes_null_current_step(session: AsyncSession, monkeypatch):
    """A TaskRun with no recorded current_step reports "agent", matching get_unified_agents()'s own
    ``r.current_step or "agent"`` normalization (agent_db.py's ``_fetch_run_states``), so the WS and
    REST paths agree on what a null step means instead of one sending ``None`` and the other
    ``"agent"``.
    """
    monkeypatch.setattr("sova.dashboard.services.agent_pool.list_all_pools", lambda: {})

    run = TaskRun(
        issue_number="76",
        role="developer",
        status="running",
        current_step=None,
        started_at=NOW - timedelta(minutes=1),
    )
    session.add(run)
    await session.commit()

    status = await _status_for(run.id)
    assert status is not None
    assert status.current_step == "agent"


@pytest.mark.asyncio
async def test_resolve_live_pool_agents_skips_mismatched_project_dir(
    session: AsyncSession, monkeypatch, tmp_path: Path
):
    """A pool at a different project_dir must not supply resource fields for this run.

    ``_resolve_live_pool_agents`` matches pools by resolved project_dir rather than blindly
    returning the first pool found; this exercises that matching logic with a non-empty,
    non-matching pools dict (the trivial all-empty case is already covered by
    test_rest_parity_defaults_with_no_live_process).
    """
    run = TaskRun(
        issue_number="75",
        role="developer",
        status="running",
        current_step="develop",
        started_at=NOW - timedelta(minutes=1),
    )
    session.add(run)
    await session.commit()

    other_pool = SimpleNamespace(
        project_dir=tmp_path / "sova-test-other-project-1051",
        agents={
            run.id: SimpleNamespace(
                resource_collector=SimpleNamespace(samples=[SimpleNamespace(cpu_percent=99.0, memory_rss_bytes=1)]),
                output_lines=["x"],
            )
        },
    )
    monkeypatch.setattr("sova.dashboard.services.agent_pool.list_all_pools", lambda: {"other": other_pool})

    status = await _status_for(run.id, project_dir=tmp_path / "sova-test-target-project-1051")
    assert status is not None
    assert status.source == "dashboard"
    assert status.cpu_percent is None
    assert status.memory_rss_bytes is None


def test_compute_live_fields_null_resource_collector():
    """A pool entry with no resource collector yet still reports dashboard-sourced output_lines."""
    from sova.dashboard.services.agent_status import _compute_live_fields

    run = SimpleNamespace(id=1, pid=None)
    state = SimpleNamespace(resource_collector=None, output_lines=["a", "b"])
    result = _compute_live_fields(run, {1: state})
    assert result == {"cpu_percent": None, "memory_rss_bytes": None, "output_lines": 2, "source": "dashboard"}


def test_compute_live_fields_empty_samples():
    """A pool entry whose collector has not taken a sample yet (the normal state for the first
    seconds of every dashboard-spawned run) reports no resource fields rather than raising.
    """
    from sova.dashboard.services.agent_status import _compute_live_fields

    run = SimpleNamespace(id=2, pid=None)
    state = SimpleNamespace(resource_collector=SimpleNamespace(samples=[]), output_lines=[])
    result = _compute_live_fields(run, {2: state})
    assert result == {"cpu_percent": None, "memory_rss_bytes": None, "output_lines": 0, "source": "dashboard"}


# Pure helper unit tests ----------------------------------------------------


def test_as_utc_preserves_aware_datetime():
    """A datetime that already carries tzinfo is returned unchanged."""
    from sova.dashboard.services.agent_status import _as_utc

    aware = NOW.astimezone(timezone.utc)
    assert _as_utc(aware) is aware


def test_compute_elapsed_seconds_no_started_at():
    """A TaskRun with no recorded start time has 0 elapsed seconds."""
    from sova.dashboard.services.agent_status import _compute_elapsed_seconds

    run = TaskRun(issue_number="100", role="developer", status="running", started_at=None)
    assert _compute_elapsed_seconds(run, NOW) == 0


def test_compute_progress_pct_zero_total_steps():
    """Zero total_steps (not the step_index==0/completed_count==0 case) yields 0%."""
    from sova.dashboard.services.agent_status import _compute_progress_pct

    assert _compute_progress_pct(step_index=1, total_steps=0, completed_count=0) == 0.0


def test_compute_time_in_step_no_steps_no_started_at():
    """No in-progress steps and no TaskRun.started_at falls through to (0, not authoritative)."""
    from sova.dashboard.services.agent_status import _compute_time_in_step

    run = TaskRun(issue_number="101", role="developer", status="running", started_at=None)
    assert _compute_time_in_step(run, []) == (0, False)


def test_compute_time_in_step_run_started_at_fallback_not_authoritative():
    """Falling back to TaskRun.started_at (no in-progress StepExecution yet) yields a nonzero
    duration but is flagged as not authoritative, since it measures run elapsed time rather than
    time in the current step.
    """
    from sova.dashboard.services.agent_status import _compute_time_in_step

    run = TaskRun(issue_number="103", role="developer", status="running", started_at=NOW - timedelta(seconds=30))
    ms, known = _compute_time_in_step(run, [])
    assert ms > 0
    assert known is False


@pytest.mark.asyncio
async def test_fetch_step_averages_empty_pipeline_steps(session: AsyncSession):
    """No pipeline steps to average means an empty result, no query issued."""
    from sova.dashboard.services.agent_status import _fetch_step_averages

    assert await _fetch_step_averages(session, []) == {}


@pytest.mark.asyncio
async def test_fetch_step_averages_no_positive_duration_rows(session: AsyncSession):
    """Enough qualifying historical runs exist, but all matching durations are 0."""
    from sova.dashboard.services.agent_status import _fetch_step_averages

    pipeline_steps = ["fetch_task", "research", "spec", "extract_memory"]

    for i in range(2):
        hist_run = TaskRun(
            issue_number=str(300 + i),
            role="researcher",
            status="done",
            current_step="extract_memory",
            started_at=NOW - timedelta(hours=1 + i),
            ended_at=NOW - timedelta(minutes=30 + i),
        )
        session.add(hist_run)
        await session.flush()
        for step_name in pipeline_steps:
            session.add(
                StepExecution(
                    task_run_id=hist_run.id,
                    step_name=step_name,
                    status="done",
                    duration_ms=0,
                    started_at=NOW - timedelta(hours=1),
                )
            )
    await session.commit()

    averages = await _fetch_step_averages(session, pipeline_steps)
    assert averages == {}


def test_compute_estimation_no_remaining_steps():
    """All pipeline steps already completed: zero remaining time, not None."""
    from sova.dashboard.services.agent_status import _compute_estimation

    result = _compute_estimation({"a": 100.0}, ["a", "b"], ["a", "b"])
    assert result == 0


def test_compute_estimation_missing_average_for_remaining_step():
    """A remaining step with no historical average makes the whole estimate unreliable."""
    from sova.dashboard.services.agent_status import _compute_estimation

    result = _compute_estimation({"a": 100.0}, ["a", "b"], [])
    assert result is None


def test_format_status_update():
    """format_status_update wraps a list of AgentStatus into the WS payload shape."""
    from sova.dashboard.services.agent_status import AgentStatus, format_status_update

    status = AgentStatus(
        run_id=1,
        status="running",
        role="developer",
        pipeline_variant="developer",
        current_step="develop",
        step_index=1,
        total_steps=17,
        step_progress_pct=5.0,
        time_in_step_ms=1000,
        time_in_step_known=True,
        is_stuck=False,
        estimated_remaining_ms=None,
        issue="1",
        pid=None,
        elapsed_seconds=10,
        cost_usd="0.000000",
        pr_number=None,
    )

    payload = format_status_update([status])
    assert payload["type"] == "status_update"
    assert payload["runs"] == [
        {
            "run_id": 1,
            "status": "running",
            "role": "developer",
            "pipeline_variant": "developer",
            "current_step": "develop",
            "step_index": 1,
            "total_steps": 17,
            "step_progress_pct": 5.0,
            "time_in_step_ms": 1000,
            "time_in_step_known": True,
            "is_stuck": False,
            "estimated_remaining_ms": None,
            "issue": "1",
            "pid": None,
            "elapsed_seconds": 10,
            "cost_usd": "0.000000",
            "pr_number": None,
            "completed_steps": [],
            "error_message": None,
            "output_lines": 0,
            "cpu_percent": None,
            "memory_rss_bytes": None,
            "source": "dashboard",
        }
    ]
