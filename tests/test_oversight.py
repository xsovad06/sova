"""Tests for sova.oversight.agent -- oversight agent daemon."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from pydantic import ValidationError

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
        with pytest.raises(ValidationError, match="wake_interval_minutes"):
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
        cfg = OversightConfig(wake_interval_minutes=1, enabled=True)
        agent = OversightAgent(config=cfg)
        with patch.object(agent, "_reload_config", return_value=cfg):
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
        cfg = OversightConfig(wake_interval_minutes=1, enabled=True)
        agent = OversightAgent(config=cfg)

        recorded: list[dict] = []

        async def _mock_record(run_id, cycle, status, duration_ms, *, started_at=None, error=None, snapshot=None):
            recorded.append(
                {
                    "run_id": run_id,
                    "cycle": cycle,
                    "status": status,
                    "duration_ms": duration_ms,
                    "error": error,
                }
            )

        async def _noop_observe():
            return {"projects": [], "partial": False}

        async def _noop_analyze(snapshot, run_id):
            return [], None

        with (
            patch.object(agent, "_reload_config", return_value=cfg),
            patch.object(agent, "_observe", side_effect=_noop_observe),
            patch.object(agent, "_analyze", side_effect=_noop_analyze),
            patch.object(agent, "_record_run", side_effect=_mock_record),
        ):

            async def _fake_sleep(seconds):
                raise asyncio.CancelledError

            with patch.object(agent, "_interruptible_sleep", side_effect=_fake_sleep):
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
        cfg = OversightConfig(wake_interval_minutes=1, enabled=True)
        agent = OversightAgent(config=cfg)

        call_count = 0

        async def _failing_record(*args, **kwargs):
            raise RuntimeError("DB unavailable")

        async def _fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                raise asyncio.CancelledError

        async def _noop_observe():
            return None

        with (
            patch.object(agent, "_reload_config", return_value=cfg),
            patch.object(agent, "_observe", side_effect=_noop_observe),
            patch.object(agent, "_record_run", side_effect=_failing_record),
            patch.object(agent, "_interruptible_sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        # The loop ran 3 cycles without crashing despite _record_run failures
        assert call_count == 3  # 2 successful sleeps + 1 that cancelled
        assert agent._cycle_number == 3

    @pytest.mark.asyncio
    async def test_cancelled_during_sleep_completes_prior_cycle(self) -> None:
        """When cancelled during sleep between cycles, the prior cycle is recorded as done."""
        cfg = OversightConfig(wake_interval_minutes=1, enabled=True)
        agent = OversightAgent(config=cfg)

        recorded: list[dict] = []

        async def _mock_record(run_id, cycle, status, duration_ms, *, started_at=None, error=None, snapshot=None):
            recorded.append({"status": status, "error": error})

        async def _fake_sleep(seconds):
            raise asyncio.CancelledError

        async def _noop_observe():
            return {"projects": [], "partial": False}

        async def _noop_analyze(snapshot, run_id):
            return [], None

        # First cycle runs immediately, then sleep raises CancelledError
        with (
            patch.object(agent, "_reload_config", return_value=cfg),
            patch.object(agent, "_observe", side_effect=_noop_observe),
            patch.object(agent, "_analyze", side_effect=_noop_analyze),
            patch.object(agent, "_record_run", side_effect=_mock_record),
            patch.object(agent, "_interruptible_sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        # First cycle completed as done, then sleep cancelled (no second cycle)
        assert len(recorded) == 1
        assert recorded[0]["status"] == OversightRunStatus.DONE

    @pytest.mark.asyncio
    async def test_record_run_with_db(self) -> None:
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
    async def test_cancelled_during_cycle_body_records_error(self) -> None:
        """When CancelledError fires during the cycle body, record an ERROR run."""
        cfg = OversightConfig(wake_interval_minutes=1, enabled=True)
        agent = OversightAgent(config=cfg)

        recorded: list[dict] = []

        async def _mock_record(run_id, cycle, status, duration_ms, *, started_at=None, error=None, snapshot=None):
            recorded.append({"status": status, "error": error})

        sleep_count = 0

        async def _fake_sleep(seconds):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count == 1:
                return  # let cycle start
            raise asyncio.CancelledError  # won't be reached

        call_count = 0
        original_mock_record = _mock_record

        async def _cancel_then_record(
            run_id,
            cycle,
            status,
            duration_ms,
            *,
            started_at=None,
            error=None,
            snapshot=None,
        ):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.CancelledError
            await original_mock_record(run_id, cycle, status, duration_ms, started_at=started_at, error=error)

        async def _noop_observe():
            return None

        with (
            patch.object(agent, "_reload_config", return_value=cfg),
            patch.object(agent, "_observe", side_effect=_noop_observe),
            patch.object(agent, "_record_run", side_effect=_cancel_then_record),
            patch.object(agent, "_interruptible_sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert len(recorded) == 1
        assert recorded[0]["status"] == OversightRunStatus.ERROR
        assert recorded[0]["error"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancelled_during_cycle_body_record_fails(self) -> None:
        """When _record_run fails during CancelledError handling, CancelledError still propagates."""
        cfg = OversightConfig(wake_interval_minutes=1, enabled=True)
        agent = OversightAgent(config=cfg)

        async def _fake_sleep(seconds):
            return  # let cycle start immediately

        call_count = 0

        async def _failing_record(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.CancelledError
            raise RuntimeError("DB gone during cancellation")

        async def _noop_observe():
            return None

        with (
            patch.object(agent, "_reload_config", return_value=cfg),
            patch.object(agent, "_observe", side_effect=_noop_observe),
            patch.object(agent, "_record_run", side_effect=_failing_record),
            patch.object(agent, "_interruptible_sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        # The CancelledError was still re-raised despite the inner _record_run failure
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_observe_none_records_error(self) -> None:
        """When _observe returns None the cycle is recorded as ERROR, not DONE."""
        cfg = OversightConfig(wake_interval_minutes=1, enabled=True)
        agent = OversightAgent(config=cfg)

        recorded: list[dict] = []

        async def _mock_record(run_id, cycle, status, duration_ms, *, started_at=None, error=None, snapshot=None):
            recorded.append({"status": status, "error": error})

        async def _observe_none():
            return None

        with (
            patch.object(agent, "_reload_config", return_value=cfg),
            patch.object(agent, "_observe", side_effect=_observe_none),
            patch.object(agent, "_record_run", side_effect=_mock_record),
        ):

            async def _fake_sleep(seconds):
                raise asyncio.CancelledError

            with patch.object(agent, "_interruptible_sleep", side_effect=_fake_sleep):
                task = agent.start()
                with pytest.raises(asyncio.CancelledError):
                    await task

        assert len(recorded) == 1
        assert recorded[0]["status"] == OversightRunStatus.ERROR
        assert recorded[0]["error"] == "observation_failed"

    @pytest.mark.asyncio
    async def test_cycle_skips_when_disabled(self) -> None:
        """run_cycle_once returns early when config.enabled is False."""
        cfg = OversightConfig(wake_interval_minutes=1, enabled=False)
        agent = OversightAgent(config=cfg)

        with patch.object(agent, "_reload_config", return_value=cfg):
            await agent.run_cycle_once()

        assert agent._cycle_number == 0

    @pytest.mark.asyncio
    async def test_record_run_db_failure_does_not_raise(self) -> None:
        """If get_session raises, _record_run logs but doesn't propagate."""
        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        with patch("sova.db.session.get_session", side_effect=RuntimeError("DB gone")):
            # Should not raise
            await agent._record_run("test-uuid", 1, OversightRunStatus.DONE, 42)


# ---------------------------------------------------------------------------
# Action wiring tests
# ---------------------------------------------------------------------------


class TestOversightActionWiring:
    @pytest.mark.asyncio
    async def test_propose_issues_called_with_findings(self) -> None:
        """When _analyze returns findings and no error, _propose_issues is called."""
        from sova.db.models import OversightFinding

        cfg = OversightConfig(wake_interval_minutes=1, enabled=True)
        agent = OversightAgent(config=cfg)

        finding = OversightFinding(run_id="r", title="t", scope="global", confidence=0.9)
        propose_called = False

        async def _mock_observe():
            return {"projects": []}

        async def _mock_analyze(snapshot, run_id):
            return [finding], None

        async def _mock_propose(findings):
            nonlocal propose_called
            propose_called = True
            assert len(findings) == 1

        async def _mock_record(*args, **kwargs):
            pass

        async def _fake_sleep(seconds):
            raise asyncio.CancelledError

        with (
            patch.object(agent, "_reload_config", return_value=cfg),
            patch.object(agent, "_observe", side_effect=_mock_observe),
            patch.object(agent, "_analyze", side_effect=_mock_analyze),
            patch.object(agent, "_propose_issues", side_effect=_mock_propose),
            patch.object(agent, "_record_run", side_effect=_mock_record),
            patch.object(agent, "_interruptible_sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert propose_called is True

    @pytest.mark.asyncio
    async def test_propose_issues_skipped_on_analysis_error(self) -> None:
        """When _analyze returns an error, _propose_issues is NOT called."""
        from sova.db.models import OversightFinding

        cfg = OversightConfig(wake_interval_minutes=1, enabled=True)
        agent = OversightAgent(config=cfg)

        finding = OversightFinding(run_id="r", title="t", scope="global", confidence=0.9)
        propose_called = False

        async def _mock_observe():
            return {"projects": []}

        async def _mock_analyze(snapshot, run_id):
            return [finding], "partial: parse failed"

        async def _mock_propose(findings):
            nonlocal propose_called
            propose_called = True

        async def _mock_record(*args, **kwargs):
            pass

        async def _fake_sleep(seconds):
            raise asyncio.CancelledError

        with (
            patch.object(agent, "_reload_config", return_value=cfg),
            patch.object(agent, "_observe", side_effect=_mock_observe),
            patch.object(agent, "_analyze", side_effect=_mock_analyze),
            patch.object(agent, "_propose_issues", side_effect=_mock_propose),
            patch.object(agent, "_record_run", side_effect=_mock_record),
            patch.object(agent, "_interruptible_sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert propose_called is False

    @pytest.mark.asyncio
    async def test_propose_issues_skipped_with_empty_findings(self) -> None:
        """When _analyze returns no findings, _propose_issues is NOT called."""
        cfg = OversightConfig(wake_interval_minutes=1, enabled=True)
        agent = OversightAgent(config=cfg)

        propose_called = False

        async def _mock_observe():
            return {"projects": []}

        async def _mock_analyze(snapshot, run_id):
            return [], None

        async def _mock_propose(findings):
            nonlocal propose_called
            propose_called = True

        async def _mock_record(*args, **kwargs):
            pass

        async def _fake_sleep(seconds):
            raise asyncio.CancelledError

        with (
            patch.object(agent, "_reload_config", return_value=cfg),
            patch.object(agent, "_observe", side_effect=_mock_observe),
            patch.object(agent, "_analyze", side_effect=_mock_analyze),
            patch.object(agent, "_propose_issues", side_effect=_mock_propose),
            patch.object(agent, "_record_run", side_effect=_mock_record),
            patch.object(agent, "_interruptible_sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert propose_called is False

    @pytest.mark.asyncio
    async def test_propose_issues_skips_jira_task_source(self) -> None:
        """_propose_issues returns early when task_source.type is not github."""
        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        from unittest.mock import MagicMock

        from sova.config.models import TaskSourceConfig
        from sova.db.models import OversightFinding

        finding = OversightFinding(run_id="r", title="t", scope="global", confidence=0.9)

        mock_config = MagicMock()
        mock_config.github_repo = "owner/repo"
        mock_config.task_source = TaskSourceConfig(type="jira")

        with (
            patch("sova.config.loader.load_config", return_value=mock_config),
            patch("sova.adapters.create_adapter") as mock_create,
            patch("sova.oversight.actions.propose_issues") as mock_propose,
        ):
            await agent._propose_issues([finding])

        mock_create.assert_not_called()
        mock_propose.assert_not_called()

    @pytest.mark.asyncio
    async def test_propose_issues_happy_path(self) -> None:
        """_propose_issues creates adapter, calls propose_issues, and triggers auto_triage."""
        from unittest.mock import MagicMock

        from sova.config.models import TaskSourceConfig
        from sova.db.models import OversightFinding

        cfg = OversightConfig(wake_interval_minutes=1, auto_triage=True)
        agent = OversightAgent(config=cfg)

        finding = OversightFinding(
            run_id="r",
            title="t",
            scope="global",
            confidence=0.9,
            github_issue_number=42,
        )

        mock_config = MagicMock()
        mock_config.github_repo = "owner/repo"
        mock_config.task_source = TaskSourceConfig(type="github")

        mock_adapter = MagicMock()
        triage_called = False

        async def _mock_triage(findings):
            nonlocal triage_called
            triage_called = True

        async def _mock_propose(*args, **kwargs):
            return [finding]

        with (
            patch("sova.config.loader.load_config", return_value=mock_config),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch("sova.config.registry.list_projects", return_value={}),
            patch("sova.oversight.actions.propose_issues", side_effect=_mock_propose),
            patch.object(agent, "_auto_triage", side_effect=_mock_triage),
        ):
            await agent._propose_issues([finding])

        assert triage_called is True

    @pytest.mark.asyncio
    async def test_propose_issues_skips_jira_projects(self) -> None:
        """_propose_issues only creates adapters for GitHub-backed projects."""
        from unittest.mock import MagicMock

        from sova.config.models import TaskSourceConfig
        from sova.db.models import OversightFinding

        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        finding = OversightFinding(
            run_id="r",
            title="t",
            scope="project",
            confidence=0.9,
            project_slug="gh-proj",
        )

        sova_cfg = MagicMock()
        sova_cfg.github_repo = "owner/sova"
        sova_cfg.task_source = TaskSourceConfig(type="github")

        gh_cfg = MagicMock()
        gh_cfg.github_repo = "owner/gh-proj"
        gh_cfg.task_source = TaskSourceConfig(type="github")

        jira_cfg = MagicMock()
        jira_cfg.github_repo = ""
        jira_cfg.task_source = TaskSourceConfig(type="jira")

        def _load(path=None):
            if path is None:
                return sova_cfg
            if str(path) == "/projects/gh-proj":
                return gh_cfg
            return jira_cfg

        adapters_created: list[str] = []
        original_mock = MagicMock()

        def _create(cfg_arg):
            adapters_created.append(cfg_arg.github_repo)
            return original_mock

        async def _mock_propose(*args, **kwargs):
            return []

        with (
            patch("sova.config.loader.load_config", side_effect=_load),
            patch("sova.adapters.create_adapter", side_effect=_create),
            patch(
                "sova.config.registry.list_projects",
                return_value={"gh-proj": "/projects/gh-proj", "jira-proj": "/projects/jira-proj"},
            ),
            patch("sova.oversight.actions.propose_issues", side_effect=_mock_propose) as mock_propose,
        ):
            await agent._propose_issues([finding])

        # SOVA adapter + gh-proj adapter created, but not jira-proj
        assert "owner/sova" in adapters_created
        assert "owner/gh-proj" in adapters_created
        assert len(adapters_created) == 2

        # propose_issues was called with project_adapters containing only gh-proj
        call_kwargs = mock_propose.call_args
        project_adapters = call_kwargs[0][3] if len(call_kwargs[0]) > 3 else call_kwargs[1].get("project_adapters", {})
        assert "gh-proj" in project_adapters
        assert "jira-proj" not in project_adapters

    @pytest.mark.asyncio
    async def test_auto_triage_runs_subprocess(self) -> None:
        """_auto_triage spawns sova triage for global findings with issue numbers."""
        from unittest.mock import AsyncMock

        from sova.db.models import OversightFinding

        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        finding = OversightFinding(
            run_id="r",
            title="t",
            scope="global",
            confidence=0.9,
            github_issue_number=42,
        )

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=None)
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await agent._auto_triage([finding])

        mock_exec.assert_awaited_once()
        args = mock_exec.call_args[0]
        assert "sova" in args
        assert "triage" in args
        assert "42" in args

    @pytest.mark.asyncio
    async def test_auto_triage_skips_non_global(self) -> None:
        """_auto_triage skips local findings."""

        from sova.db.models import OversightFinding

        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        finding = OversightFinding(
            run_id="r",
            title="t",
            scope="local",
            confidence=0.9,
            github_issue_number=42,
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await agent._auto_triage([finding])

        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_triage_timeout_kills_process(self) -> None:
        """_auto_triage kills the process on timeout."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.db.models import OversightFinding

        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        finding = OversightFinding(
            run_id="r",
            title="t",
            scope="global",
            confidence=0.9,
            github_issue_number=42,
        )

        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", side_effect=TimeoutError),
        ):
            await agent._auto_triage([finding])

        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_propose_issues_exception_is_non_fatal(self) -> None:
        """_propose_issues catches and logs exceptions without propagating."""
        from unittest.mock import MagicMock

        from sova.config.models import TaskSourceConfig
        from sova.db.models import OversightFinding

        cfg = OversightConfig(wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        finding = OversightFinding(run_id="r", title="t", scope="global", confidence=0.9)

        mock_config = MagicMock()
        mock_config.github_repo = "owner/repo"
        mock_config.task_source = TaskSourceConfig(type="github")

        with (
            patch("sova.config.loader.load_config", return_value=mock_config),
            patch("sova.adapters.create_adapter", side_effect=RuntimeError("adapter boom")),
        ):
            await agent._propose_issues([finding])


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
