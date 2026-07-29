"""Tests for sova.supervisor.daemon: SupervisorDaemon polling loop."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
        status = daemon.get_status()
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

    async def test_stop_handles_failed_task(self, daemon: SupervisorDaemon) -> None:
        """stop() should complete cleanly even if the task raises an exception."""

        async def _raise():
            raise RuntimeError("task failed unexpectedly")

        daemon._running = True
        daemon._task = asyncio.create_task(_raise())
        # Give the task time to fail
        await asyncio.sleep(0.05)
        # stop() uses gather(return_exceptions=True) and should not raise
        await daemon.stop()
        assert daemon.running is False
        assert daemon._task is None

    async def test_stop_cancels_running_task(self, daemon: SupervisorDaemon) -> None:
        """stop() cancels a long-running task and cleans up the task reference."""
        with patch.object(daemon, "_poll_once", new_callable=AsyncMock, return_value={}):
            task = daemon.start()
            assert daemon.running is True
            await asyncio.sleep(0.05)
            await daemon.stop()
            assert daemon.running is False
            assert task.done()
            assert daemon._task is None

    async def test_run_loop_exits_on_cancelled(self, daemon: SupervisorDaemon) -> None:
        """_run_loop should propagate CancelledError to allow task cancellation."""
        call_count = 0

        async def cancelling_poll():
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError

        with patch.object(daemon, "_poll_once", side_effect=cancelling_poll):
            with patch.object(daemon, "_purge_old_logs", new_callable=AsyncMock):
                daemon._running = True
                task = asyncio.create_task(daemon._run_loop())
                await asyncio.sleep(0.05)
                await asyncio.gather(task, return_exceptions=True)
        assert call_count >= 1

    async def test_run_loop_handles_poll_exception(self, daemon: SupervisorDaemon) -> None:
        """_run_loop should log errors and continue polling on non-CancelledError exceptions."""
        call_count = 0

        async def failing_poll():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("transient error")
            daemon._running = False
            return {}

        with patch.object(daemon, "_poll_once", side_effect=failing_poll):
            with patch.object(daemon, "_purge_old_logs", new_callable=AsyncMock):
                daemon._running = True
                with patch.object(daemon, "_config") as mock_cfg:
                    mock_cfg.supervisor.poll_interval_seconds = 0
                    await daemon._run_loop()
        assert call_count >= 2

    async def test_poll_once_adapter_creation_failure(self, daemon: SupervisorDaemon) -> None:
        """_poll_once should return error dict when adapter creation fails."""
        with patch("sova.adapters.create_adapter", side_effect=Exception("no auth")):
            result = await daemon._poll_once()
        assert "error" in result["progression"]
        assert "no auth" in result["progression"]["error"]

    async def test_poll_progression_with_decisions(
        self,
        session_factory: async_sessionmaker,
    ) -> None:
        """_poll_progression executes immediately when require_approval=False."""

        from sova.config.models import ProjectConfig, SupervisorConfig
        from sova.supervisor.progression import BlockReason, ProgressionAction, ProgressionDecision

        cfg = ProjectConfig(
            supervisor=SupervisorConfig(enabled=True, poll_interval_seconds=1, require_approval=False),
            github_repo="test/repo",
        )
        daemon = SupervisorDaemon(config=cfg, project_dir=Path("/tmp/test"), session_factory=session_factory)

        mock_decision = ProgressionDecision(
            issue_number=42,
            action=ProgressionAction.SPAWN_DEVELOPER,
            reason="Ready to develop",
            blocked_by=(BlockReason(gate="slots", detail="1 slot available"),),
        )

        mock_engine = AsyncMock()
        mock_engine.evaluate_all.return_value = [mock_decision]
        mock_engine.execute_decisions.return_value = 1

        adapter = AsyncMock()

        with patch("sova.supervisor.progression.TaskProgressionEngine", return_value=mock_engine):
            result = await daemon._poll_progression(adapter)

        assert result["decisions"] == 1
        assert result["executed"] == 1

        # Verify the decision was logged to DB
        async with session_factory() as session:
            db_result = await session.execute(
                select(SupervisorDecision).where(SupervisorDecision.component == "progression")
            )
            rows = db_result.scalars().all()
            assert len(rows) >= 1
            assert rows[0].action == "spawn_developer"

    async def test_poll_progression_with_decisions_no_blocked_by(
        self,
        daemon: SupervisorDaemon,
        session_factory: async_sessionmaker,
    ) -> None:
        """_poll_progression handles decisions without blocked_by (metadata_json=None)."""
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        mock_decision = ProgressionDecision(
            issue_number=7,
            action=ProgressionAction.WAIT,
            reason="Waiting for CI",
        )

        mock_engine = AsyncMock()
        mock_engine.evaluate_all.return_value = [mock_decision]
        mock_engine.execute_decisions.return_value = 0

        adapter = AsyncMock()

        with patch("sova.supervisor.progression.TaskProgressionEngine", return_value=mock_engine):
            result = await daemon._poll_progression(adapter)

        assert result["decisions"] == 1

    async def test_poll_health_ok(
        self,
        daemon: SupervisorDaemon,
        session_factory: async_sessionmaker,
    ) -> None:
        """_poll_health returns db=ok when DB is reachable."""
        result = await daemon._poll_health()
        assert result["db"] == "ok"
        assert "adapter" not in result

    async def test_log_decisions_batch_handles_db_error(self, daemon: SupervisorDaemon) -> None:
        """_log_decisions_batch should not raise on DB errors."""
        bad_record = SupervisorDecision(
            component="test",
            event_type="test",
            action="test",
        )
        with patch.object(daemon, "_session_factory", side_effect=Exception("db down")):
            # Should not raise
            await daemon._log_decisions_batch([bad_record])

    async def test_poll_quota_enabled(
        self,
        daemon: SupervisorDaemon,
        session_factory: async_sessionmaker,
    ) -> None:
        """_poll_quota should sync and return quota status when enabled."""
        from sova.config.models import CodeRabbitQuotaConfig

        daemon._config.coderabbit_quota = CodeRabbitQuotaConfig(enabled=True, reviews_per_hour=4)

        mock_status = MagicMock()
        mock_status.can_create_pr = True
        mock_status.reviews_in_window = 2
        mock_status.reviews_per_hour = 4
        mock_status.next_available_minutes = 0

        with patch("sova.supervisor.coderabbit_quota.sync_from_github", new_callable=AsyncMock):
            with patch(
                "sova.supervisor.coderabbit_quota.get_quota_status",
                new_callable=AsyncMock,
                return_value=mock_status,
            ):
                result = await daemon._poll_quota()

        assert result["can_create_pr"] is True
        assert result["reviews_in_window"] == 2

    async def test_poll_quota_enabled_error(
        self,
        daemon: SupervisorDaemon,
    ) -> None:
        """_poll_quota should return error dict when an exception occurs."""
        from sova.config.models import CodeRabbitQuotaConfig

        daemon._config.coderabbit_quota = CodeRabbitQuotaConfig(enabled=True, reviews_per_hour=4)

        with patch("sova.supervisor.coderabbit_quota.sync_from_github", side_effect=Exception("api error")):
            result = await daemon._poll_quota()

        assert "error" in result
        assert "api error" in result["error"]

    async def test_poll_health_db_error(
        self,
        daemon: SupervisorDaemon,
    ) -> None:
        """_poll_health should report DB error when session factory fails."""
        with patch.object(daemon, "_session_factory", side_effect=Exception("db connection failed")):
            result = await daemon._poll_health()
        assert "error" in result["db"]
        assert "db connection failed" in result["db"]

    async def test_purge_old_logs_exception(
        self,
        daemon: SupervisorDaemon,
    ) -> None:
        """_purge_old_logs should swallow exceptions without raising."""
        with patch.object(daemon, "_session_factory", side_effect=Exception("db down")):
            await daemon._purge_old_logs()

    async def test_poll_once_quota_runs_before_progression(self, daemon: SupervisorDaemon) -> None:
        """Quota sync must complete before the progression engine evaluates tasks."""
        call_order: list[str] = []

        async def mock_quota():
            call_order.append("quota")
            return {"enabled": True, "can_create_pr": True, "reviews_in_window": 0}

        async def mock_progression(_adapter):
            call_order.append("progression")
            return {"decisions": 0, "executed": 0}

        with (
            patch.object(daemon, "_poll_quota", side_effect=mock_quota),
            patch.object(daemon, "_poll_progression", side_effect=mock_progression),
            patch.object(daemon, "_poll_health", new_callable=AsyncMock, return_value={"db": "ok"}),
            patch("sova.adapters.create_adapter", return_value=AsyncMock()),
        ):
            await daemon.poll_once()

        assert call_order == ["quota", "progression"], f"Expected quota before progression, got: {call_order}"


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
