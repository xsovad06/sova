"""Tests for failure analysis service."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.dashboard.services.failure_analysis_service import analyze_failures, get_failure_category_counts
from sova.db.models import StepExecution, TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for failure analysis tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def session() -> AsyncSession:
    return await get_session()


@pytest.fixture
async def client():
    """Create test client for router endpoints."""
    from sova.dashboard.app import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestAnalyzeFailures:
    """Test the analyze_failures service function."""

    async def test_empty_db_returns_zero_rates(self, session: AsyncSession) -> None:
        breakdown = await analyze_failures(session)
        assert breakdown.total_runs == 0
        assert breakdown.pipeline_failure_rate == 0.0

    async def test_excludes_operational_failures(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        dismissed = TaskRun(
            issue_number="1",
            role="developer",
            status="failed",
            error_message="Dismissed by user",
            started_at=now - timedelta(hours=1),
            ended_at=now,
        )
        stale = TaskRun(
            issue_number="2",
            role="developer",
            status="failed",
            error_message="Stale run recovered on startup",
            started_at=now - timedelta(hours=1),
            ended_at=now,
        )
        real_failure = TaskRun(
            issue_number="3",
            role="developer",
            status="failed",
            error_message="Rebase could not be completed",
            started_at=now - timedelta(hours=1),
            ended_at=now,
        )
        success = TaskRun(
            issue_number="4",
            role="developer",
            status="done",
            started_at=now - timedelta(hours=1),
            ended_at=now,
        )

        session.add_all([dismissed, stale, real_failure, success])
        await session.commit()

        breakdown = await analyze_failures(session)
        assert breakdown.total_runs == 4
        assert breakdown.failed_runs == 3
        assert breakdown.operational_failures == 2
        assert breakdown.true_pipeline_failures == 1

    async def test_interrupted_runs_excluded_from_denominator(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        interrupted = TaskRun(
            issue_number="1",
            role="developer",
            status="interrupted",
            started_at=now,
            ended_at=now,
        )
        done = TaskRun(
            issue_number="2",
            role="developer",
            status="done",
            started_at=now,
            ended_at=now,
        )

        session.add_all([interrupted, done])
        await session.commit()

        breakdown = await analyze_failures(session)
        assert breakdown.interrupted_runs == 1
        assert breakdown.pipeline_failure_rate == 0.0

    async def test_pipeline_failure_rate_calculation(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        runs = [
            TaskRun(issue_number="1", role="developer", status="done", started_at=now, ended_at=now),
            TaskRun(issue_number="2", role="developer", status="done", started_at=now, ended_at=now),
            TaskRun(issue_number="3", role="developer", status="done", started_at=now, ended_at=now),
            TaskRun(
                issue_number="4",
                role="developer",
                status="failed",
                error_message="Rebase could not complete",
                started_at=now,
                ended_at=now,
            ),
            TaskRun(
                issue_number="5",
                role="developer",
                status="failed",
                error_message="Dismissed by user",
                started_at=now,
                ended_at=now,
            ),
        ]
        session.add_all(runs)
        await session.commit()

        breakdown = await analyze_failures(session)
        # 5 total, 1 operational, 1 true failure
        # denominator = 5 - 0 interrupted - 0 rejected - 1 operational = 4
        # rate = 1/4 * 100 = 25.0%
        assert breakdown.pipeline_failure_rate == 25.0
        assert breakdown.true_pipeline_failures == 1
        assert breakdown.operational_failures == 1

    async def test_top_error_patterns_excludes_operational(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        session.add_all(
            [
                TaskRun(
                    issue_number="1",
                    role="developer",
                    status="failed",
                    error_message="Dismissed by user",
                    started_at=now,
                    ended_at=now,
                ),
                TaskRun(
                    issue_number="2",
                    role="developer",
                    status="failed",
                    error_message="Claude CLI failed (exit 1): timeout",
                    started_at=now,
                    ended_at=now,
                ),
            ]
        )
        await session.commit()

        breakdown = await analyze_failures(session)
        error_msgs = [msg for msg, _ in breakdown.top_error_patterns]
        assert "Dismissed by user" not in error_msgs
        assert "Claude CLI failed (exit 1): timeout" in error_msgs


class TestGetFailureCategoryCounts:
    """Test failure categorization."""

    async def test_detects_rebase_failures(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        session.add(
            TaskRun(
                issue_number="1",
                role="developer",
                status="failed",
                error_message="Rebase could not be completed: conflicts in 3 files",
                started_at=now,
                ended_at=now,
            )
        )
        await session.commit()

        categories = await get_failure_category_counts(session)
        assert categories["rebase_failures"] == 1

    async def test_detects_no_op_commands(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        session.add(
            TaskRun(
                issue_number="1",
                role="developer",
                status="failed",
                error_message="address-pr completed without pushing changes",
                started_at=now,
                ended_at=now,
            )
        )
        await session.commit()

        categories = await get_failure_category_counts(session)
        assert categories["no_op_commands"] == 1

    async def test_detects_pipeline_bypasses(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        session.add(
            TaskRun(
                issue_number="1",
                role="developer",
                status="failed",
                current_step="agent",
                error_message="Pipeline bypassed: developer agent completed without executing workflow steps",
                started_at=now,
                ended_at=now,
            )
        )
        await session.commit()

        categories = await get_failure_category_counts(session)
        assert categories["pipeline_bypasses"] == 1

    async def test_detects_llm_failures(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        session.add(
            TaskRun(
                issue_number="1",
                role="developer",
                status="failed",
                error_message="Claude CLI failed (exit 1): is_error=true",
                started_at=now,
                ended_at=now,
            )
        )
        await session.commit()

        categories = await get_failure_category_counts(session)
        assert categories["llm_failures"] == 1

    async def test_top_step_failures(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        run = TaskRun(
            issue_number="1",
            role="developer",
            status="failed",
            started_at=now,
            ended_at=now,
        )
        session.add(run)
        await session.flush()

        step = StepExecution(
            task_run_id=run.id,
            step_name="rebase",
            status="failed",
            error_message="Unresolved conflicts",
            started_at=now,
            ended_at=now,
        )
        session.add(step)
        await session.commit()

        breakdown = await analyze_failures(session)
        assert any(s == "rebase" for s, _ in breakdown.top_step_failures)

    async def test_detects_spec_issues(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        session.add(
            TaskRun(
                issue_number="1",
                role="researcher",
                status="failed",
                error_message="Expected .claude/specs/issue-1.md but file not found",
                started_at=now,
                ended_at=now,
            )
        )
        await session.commit()

        categories = await get_failure_category_counts(session)
        assert categories["spec_issues"] == 1

    async def test_detects_non_substantive_output(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        session.add(
            TaskRun(
                issue_number="1",
                role="developer",
                status="failed",
                error_message="Development produced no substantive code changes (only: package-lock.json)",
                started_at=now,
                ended_at=now,
            )
        )
        await session.commit()

        categories = await get_failure_category_counts(session)
        assert categories["non_substantive_output"] == 1

    async def test_project_slug_filter(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        session.add_all(
            [
                TaskRun(
                    issue_number="1",
                    role="developer",
                    status="failed",
                    error_message="Claude CLI failed (exit 1): error",
                    project_slug="project-a",
                    started_at=now,
                    ended_at=now,
                ),
                TaskRun(
                    issue_number="2",
                    role="developer",
                    status="failed",
                    error_message="Claude CLI failed (exit 1): error",
                    project_slug="project-b",
                    started_at=now,
                    ended_at=now,
                ),
            ]
        )
        await session.commit()

        cats_a = await get_failure_category_counts(session, project_slug="project-a")
        assert cats_a["llm_failures"] == 1

        cats_all = await get_failure_category_counts(session)
        assert cats_all["llm_failures"] == 2

    async def test_project_slug_filter_on_analyze_failures(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        session.add_all(
            [
                TaskRun(
                    issue_number="1",
                    role="developer",
                    status="done",
                    project_slug="alpha",
                    started_at=now,
                    ended_at=now,
                ),
                TaskRun(
                    issue_number="2",
                    role="developer",
                    status="failed",
                    error_message="Some error",
                    project_slug="beta",
                    started_at=now,
                    ended_at=now,
                ),
            ]
        )
        await session.commit()

        breakdown = await analyze_failures(session, project_slug="alpha")
        assert breakdown.total_runs == 1
        assert breakdown.failed_runs == 0

    async def test_step_failures_with_project_slug(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)

        run = TaskRun(
            issue_number="1",
            role="developer",
            status="failed",
            project_slug="proj-x",
            started_at=now,
            ended_at=now,
        )
        session.add(run)
        await session.flush()

        step = StepExecution(
            task_run_id=run.id,
            step_name="develop",
            status="failed",
            error_message="LLM error",
            started_at=now,
            ended_at=now,
        )
        session.add(step)
        await session.commit()

        breakdown = await analyze_failures(session, project_slug="proj-x")
        assert any(s == "develop" for s, _ in breakdown.top_step_failures)

        breakdown_other = await analyze_failures(session, project_slug="proj-y")
        assert len(breakdown_other.top_step_failures) == 0


class TestFailureAnalysisRouter:
    """Test the failure analysis API router endpoints."""

    async def test_breakdown_endpoint(self, client: AsyncClient) -> None:
        resp = await client.get("/api/failure-analysis/breakdown")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_runs" in data
        assert "pipeline_failure_rate" in data
        assert "top_step_failures" in data

    async def test_categories_endpoint(self, client: AsyncClient) -> None:
        resp = await client.get("/api/failure-analysis/categories")
        assert resp.status_code == 200
        data = resp.json()
        assert "rebase_failures" in data
        assert "llm_failures" in data


class TestAnalyzeCLI:
    """Test the CLI command's _analyze function."""

    async def test_analyze_empty_db(self, capsys: pytest.CaptureFixture[str]) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.cli.commands.analyze_failures import _analyze
        from sova.dashboard.services.failure_analysis_service import FailureBreakdown

        breakdown = FailureBreakdown(
            total_runs=0,
            done_runs=0,
            failed_runs=0,
            interrupted_runs=0,
            rejected_runs=0,
            operational_failures=0,
            true_pipeline_failures=0,
            pipeline_failure_rate=0.0,
            top_step_failures=[],
            top_error_patterns=[],
        )

        with (
            patch("sova.cli.commands.analyze_failures.init_db", new_callable=AsyncMock),
            patch("sova.cli.commands.analyze_failures.get_session") as mock_session,
            patch(
                "sova.cli.commands.analyze_failures.failure_analysis_service.analyze_failures",
                new_callable=AsyncMock,
                return_value=breakdown,
            ),
            patch(
                "sova.cli.commands.analyze_failures.failure_analysis_service.get_failure_category_counts",
                new_callable=AsyncMock,
                return_value={"rebase_failures": 0, "llm_failures": 0},
            ),
        ):
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_session.return_value = mock_ctx

            await _analyze(None)

        captured = capsys.readouterr()
        assert "Total runs: 0" in captured.out
        assert "Done: 0" in captured.out
        assert "Failed (all): 0" in captured.out

    async def test_analyze_with_data(self, capsys: pytest.CaptureFixture[str]) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.cli.commands.analyze_failures import _analyze
        from sova.dashboard.services.failure_analysis_service import FailureBreakdown

        breakdown = FailureBreakdown(
            total_runs=3,
            done_runs=1,
            failed_runs=2,
            interrupted_runs=0,
            rejected_runs=0,
            operational_failures=1,
            true_pipeline_failures=1,
            pipeline_failure_rate=50.0,
            top_step_failures=[("develop", 1)],
            top_error_patterns=[
                (
                    "Claude CLI failed (exit 1): something very long error message"
                    " that should be truncated at eighty characters for display",
                    1,
                )
            ],
        )
        categories = {
            "rebase_failures": 1,
            "no_op_commands": 0,
            "pipeline_bypasses": 0,
            "non_substantive_output": 0,
            "spec_issues": 0,
            "llm_failures": 0,
        }

        with (
            patch("sova.cli.commands.analyze_failures.init_db", new_callable=AsyncMock),
            patch("sova.cli.commands.analyze_failures.get_session") as mock_session,
            patch(
                "sova.cli.commands.analyze_failures.failure_analysis_service.analyze_failures",
                new_callable=AsyncMock,
                return_value=breakdown,
            ),
            patch(
                "sova.cli.commands.analyze_failures.failure_analysis_service.get_failure_category_counts",
                new_callable=AsyncMock,
                return_value=categories,
            ),
        ):
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_session.return_value = mock_ctx

            await _analyze(None)

        captured = capsys.readouterr()
        assert "Total runs: 3" in captured.out
        assert "Done: 1 (33.3%)" in captured.out
        assert "Operational failures (dismissed, stale): 1" in captured.out
        assert "True pipeline failures: 1 (50.0%)" in captured.out
        assert "Rebase Failures: 1" in captured.out
        assert "Top Failing Steps" in captured.out
        assert "develop: 1" in captured.out
        assert "Top Error Patterns" in captured.out
        assert "..." in captured.out
