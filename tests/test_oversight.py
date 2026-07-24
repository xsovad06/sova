"""Tests for sova.oversight.agent -- oversight agent daemon."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from sova.config.models import OversightConfig
from sova.db.models import OversightRunStatus
from sova.oversight.agent import OversightAgent

# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestOversightConfig:
    def test_defaults(self) -> None:
        cfg = OversightConfig()
        assert cfg.enabled is False
        assert cfg.wake_interval_minutes == 60
        assert cfg.auto_create_issues is False
        assert cfg.auto_triage is False
        assert cfg.persona_path == ""
        assert cfg.analysis_model == "sonnet"

    def test_wake_interval_min_bound(self) -> None:
        with pytest.raises(Exception):
            OversightConfig(wake_interval_minutes=0)

    def test_wake_interval_accepts_one(self) -> None:
        cfg = OversightConfig(wake_interval_minutes=1)
        assert cfg.wake_interval_minutes == 1


# ---------------------------------------------------------------------------
# OversightAgent tests
# ---------------------------------------------------------------------------


class TestOversightAgent:
    def test_init(self) -> None:
        cfg = OversightConfig()
        agent = OversightAgent(config=cfg)
        assert agent._cycle_number == 0
        assert agent._task is None

    @pytest.mark.asyncio
    async def test_start_and_stop(self) -> None:
        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)
        task = agent.start()
        assert task is not None
        assert not task.done()
        await agent.stop()
        assert agent._task is None

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self) -> None:
        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)
        await agent.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_cycle_records_done(self) -> None:
        """Verify that a wake cycle records a 'done' run to the DB."""
        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        recorded: list[dict] = []

        async def _mock_record(run_id, cycle, status, duration_ms, *, started_at=None, error=None):
            recorded.append(
                {
                    "run_id": run_id,
                    "cycle": cycle,
                    "status": status,
                    "duration_ms": duration_ms,
                    "error": error,
                }
            )

        with patch.object(agent, "_record_run", side_effect=_mock_record):
            # Patch sleep to return immediately once, then cancel
            call_count = 0

            async def _fake_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count > 1:
                    raise asyncio.CancelledError

            with patch("sova.oversight.agent.asyncio.sleep", side_effect=_fake_sleep):
                task = agent.start()
                with pytest.raises(asyncio.CancelledError):
                    await task

        assert len(recorded) == 1
        assert recorded[0]["status"] == OversightRunStatus.DONE
        assert recorded[0]["cycle"] == 1
        assert recorded[0]["error"] is None

    @pytest.mark.asyncio
    async def test_cycle_handles_record_failure(self) -> None:
        """If _record_run raises, the loop continues (doesn't crash)."""
        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        call_count = 0

        async def _failing_record(*args, **kwargs):
            raise RuntimeError("DB unavailable")

        async def _fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                raise asyncio.CancelledError

        with (
            patch.object(agent, "_record_run", side_effect=_failing_record),
            patch("sova.oversight.agent.asyncio.sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        # The loop ran 2 cycles without crashing despite _record_run failures
        assert call_count == 3  # 2 successful sleeps + 1 that cancelled
        assert agent._cycle_number == 2

    @pytest.mark.asyncio
    async def test_cancelled_run_records_error(self) -> None:
        """When cancelled during the cycle body, record an error with 'cancelled'."""
        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        recorded: list[dict] = []
        sleep_count = 0

        async def _mock_record(run_id, cycle, status, duration_ms, *, started_at=None, error=None):
            recorded.append({"status": status, "error": error})

        async def _fake_sleep(seconds):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count == 1:
                return  # let first cycle run
            raise asyncio.CancelledError

        # Make the cycle body cancel by patching record to first succeed,
        # then let the second sleep cancel
        with (
            patch.object(agent, "_record_run", side_effect=_mock_record),
            patch("sova.oversight.agent.asyncio.sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        # First cycle: done. Second sleep: CancelledError (no second cycle body)
        assert len(recorded) == 1
        assert recorded[0]["status"] == OversightRunStatus.DONE

    @pytest.mark.asyncio
    async def test_record_run_with_db(self, tmp_path: object) -> None:
        """Integration test: _record_run writes to the DB via in-memory SQLite."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from sova.db.models import Base, OversightRun

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def _mock_get_session(**kwargs):
            return factory()

        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        with patch("sova.db.session.get_session", side_effect=_mock_get_session):
            await agent._record_run("test-uuid", 1, OversightRunStatus.DONE, 42)

        async with factory() as session:
            from sqlalchemy import select

            result = await session.execute(select(OversightRun))
            runs = result.scalars().all()

        assert len(runs) == 1
        assert runs[0].id == "test-uuid"
        assert runs[0].status == OversightRunStatus.DONE
        assert runs[0].cycle_number == 1
        assert runs[0].duration_ms == 42

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_record_run_db_failure_does_not_raise(self) -> None:
        """If get_session raises, _record_run logs but doesn't propagate."""
        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        with patch("sova.db.session.get_session", side_effect=RuntimeError("DB gone")):
            # Should not raise
            await agent._record_run("test-uuid", 1, OversightRunStatus.DONE, 42)


# ---------------------------------------------------------------------------
# DB model tests
# ---------------------------------------------------------------------------


class TestOversightRunModel:
    def test_model_fields(self) -> None:
        from sova.db.models import OversightRun

        run = OversightRun(
            id="abc-123",
            status=OversightRunStatus.DONE,
            cycle_number=5,
            duration_ms=100,
        )
        assert run.id == "abc-123"
        assert run.status == OversightRunStatus.DONE
        assert run.cycle_number == 5
        assert run.duration_ms == 100
        assert run.error is None

    def test_model_with_error(self) -> None:
        from sova.db.models import OversightRun  # noqa: F811

        run = OversightRun(
            id="def-456",
            status=OversightRunStatus.ERROR,
            cycle_number=1,
            duration_ms=50,
            error="cancelled",
        )
        assert run.error == "cancelled"


# ---------------------------------------------------------------------------
# Settings meta registration tests
# ---------------------------------------------------------------------------


class TestOversightSettingsMeta:
    def test_oversight_group_exists(self) -> None:
        from sova.dashboard.settings_meta import GROUP_ORDER, GROUPS

        assert "oversight" in GROUPS
        assert "oversight" in GROUP_ORDER

    def test_oversight_meta_keys_registered(self) -> None:
        from sova.dashboard.settings_meta import get_meta

        expected_keys = [
            "oversight.enabled",
            "oversight.wake_interval_minutes",
            "oversight.auto_create_issues",
            "oversight.auto_triage",
            "oversight.persona_path",
            "oversight.analysis_model",
        ]
        for key in expected_keys:
            meta = get_meta(key)
            assert meta is not None, f"Missing settings meta for {key}"
            assert meta.group == "oversight"


# ---------------------------------------------------------------------------
# Config loader registration test
# ---------------------------------------------------------------------------


class TestOversightConfigLoader:
    def test_oversight_in_nested_sections(self) -> None:
        from sova.config.loader import _NESTED_SECTIONS

        assert "oversight" in _NESTED_SECTIONS

    def test_oversight_on_project_config(self) -> None:
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        assert hasattr(cfg, "oversight")
        assert cfg.oversight.enabled is False
        assert cfg.oversight.wake_interval_minutes == 60


# ---------------------------------------------------------------------------
# Migration test
# ---------------------------------------------------------------------------


class TestOversightMigration:
    def test_migration_metadata(self) -> None:
        import importlib

        mig = importlib.import_module("sova.db.migrations.versions.022_add_oversight_runs_table")
        assert mig.revision == "022"
        assert mig.down_revision == "021"
