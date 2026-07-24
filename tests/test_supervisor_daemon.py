"""Tests for sova.supervisor.daemon: SupervisorDaemon polling loop."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.config.models import ProjectConfig, SupervisorConfig
from sova.db.models import SupervisorDecision
from sova.db.session import close_db, get_session_factory, init_db
from sova.supervisor.daemon import SupervisorDaemon


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def session_factory() -> async_sessionmaker:
    return await get_session_factory(Path.cwd())


@pytest.fixture
def config() -> ProjectConfig:
    return ProjectConfig(
        supervisor=SupervisorConfig(
            enabled=True,
            poll_interval_seconds=1,
            log_retention_days=7,
        ),
        github_repo="test/repo",
    )


@pytest.fixture
def daemon(config: ProjectConfig, session_factory: async_sessionmaker) -> SupervisorDaemon:
    return SupervisorDaemon(
        config=config,
        project_dir=Path("/tmp/test"),
        session_factory=session_factory,
    )


class TestSupervisorDaemon:
    async def test_get_status(self, daemon: SupervisorDaemon) -> None:
        status = await daemon.get_status()
        assert status["enabled"] is True
        assert status["running"] is False
        assert status["poll_interval_seconds"] == 1
        assert status["log_retention_days"] == 7

    async def test_start_stop(self, daemon: SupervisorDaemon) -> None:
        with patch.object(daemon, "_poll_once", new_callable=AsyncMock, return_value={}):
            task = daemon.start()
            assert daemon.running is True
            await asyncio.sleep(0.05)
            await daemon.stop()
            assert daemon.running is False
            assert task.done()

    async def test_poll_lock_serializes(self, daemon: SupervisorDaemon) -> None:
        """Concurrent poll_once calls should serialize via the lock."""
        call_count = 0

        async def slow_poll():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return {}

        with patch.object(daemon, "_poll_once", side_effect=slow_poll):
            t1 = asyncio.create_task(daemon.poll_once())
            t2 = asyncio.create_task(daemon.poll_once())
            await asyncio.gather(t1, t2)
            assert call_count == 2

    async def test_poll_once_returns_all_phases(self, daemon: SupervisorDaemon) -> None:
        with (
            patch.object(daemon, "_poll_health", new_callable=AsyncMock, return_value={}),
            patch.object(daemon, "_poll_quota", new_callable=AsyncMock, return_value={"enabled": False}),
            patch.object(
                daemon,
                "_poll_progression",
                new_callable=AsyncMock,
                return_value={"decisions": 0, "executed": 0},
            ),
        ):
            result = await daemon.poll_once()
            assert "progression" in result
            assert "quota" in result
            assert "health" in result

    async def test_log_decision_writes_to_db(
        self,
        daemon: SupervisorDaemon,
        session_factory: async_sessionmaker,
    ) -> None:
        await daemon._log_decision(
            component="test",
            event_type="decision",
            action="test_action",
            detail="test detail",
            issue_number="42",
            metadata={"key": "value"},
        )

        async with session_factory() as session:
            result = await session.execute(select(SupervisorDecision))
            decisions = result.scalars().all()
            assert len(decisions) == 1
            assert decisions[0].component == "test"
            assert decisions[0].event_type == "decision"
            assert decisions[0].action == "test_action"
            assert decisions[0].issue_number == "42"
            assert decisions[0].detail == "test detail"

    async def test_purge_old_logs(self, daemon: SupervisorDaemon, session_factory: async_sessionmaker) -> None:
        # Insert an old decision
        async with session_factory() as session:
            async with session.begin():
                session.add(
                    SupervisorDecision(
                        component="test",
                        event_type="decision",
                        action="old",
                        detail="old entry",
                        created_at=datetime.now(timezone.utc) - timedelta(days=30),
                    )
                )
                session.add(
                    SupervisorDecision(
                        component="test",
                        event_type="decision",
                        action="new",
                        detail="new entry",
                        created_at=datetime.now(timezone.utc),
                    )
                )

        await daemon._purge_old_logs()

        async with session_factory() as session:
            result = await session.execute(select(SupervisorDecision))
            decisions = result.scalars().all()
            assert len(decisions) == 1
            assert decisions[0].action == "new"

    async def test_poll_progression_handles_error(
        self,
        daemon: SupervisorDaemon,
        session_factory: async_sessionmaker,
    ) -> None:
        # Test _poll_progression with a broken adapter
        adapter = AsyncMock()
        with patch("sova.supervisor.progression.TaskProgressionEngine", side_effect=Exception("engine failed")):
            result = await daemon._poll_progression(adapter)
            assert "error" in result

        # Verify error was logged
        async with session_factory() as session:
            result = await session.execute(select(SupervisorDecision).where(SupervisorDecision.event_type == "health"))
            decisions = result.scalars().all()
            assert len(decisions) >= 1

    async def test_poll_quota_disabled(self, daemon: SupervisorDaemon) -> None:
        daemon._config = ProjectConfig(
            supervisor=SupervisorConfig(enabled=True),
        )
        result = await daemon._poll_quota()
        assert result == {"enabled": False}


class TestSupervisorDecisionModel:
    async def test_model_fields(self, session_factory: async_sessionmaker) -> None:
        async with session_factory() as session:
            async with session.begin():
                session.add(
                    SupervisorDecision(
                        project_slug="test/repo",
                        component="progression",
                        event_type="decision",
                        issue_number="42",
                        action="spawn_developer",
                        detail="Dependencies satisfied",
                        metadata_json={"key": "value"},
                    )
                )

            result = await session.execute(select(SupervisorDecision))
            row = result.scalars().first()
            assert row is not None
            assert row.project_slug == "test/repo"
            assert row.component == "progression"
            assert row.event_type == "decision"
            assert row.issue_number == "42"
            assert row.action == "spawn_developer"
            assert row.metadata_json == {"key": "value"}
            assert row.created_at is not None
