"""Tests for sova.oversight.agent: oversight agent daemon."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from sova.config.models import OversightConfig
from sova.db.models import OversightRunStatus
from sova.oversight.agent import OversightAgent
from tests.conftest import closing_stand_in

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


@pytest.fixture
def _stub_start_run():
    """Keep ``_start_run`` off the DB in tests that stub ``_record_run``.

    Otherwise each cycle opens a file-backed engine under the fake home that no
    fixture disposes (the ``reset_db_rows`` leak).
    """
    with patch.object(OversightAgent, "_start_run", new_callable=AsyncMock):
        yield


@pytest.mark.usefixtures("_stub_start_run")
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
        with capture_logs() as logs:
            await agent.stop()

        assert agent._task is None
        assert [e["event"] for e in logs] == []

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
            raise SQLAlchemyError("DB unavailable")

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

        with patch("sova.oversight.agent.get_session", side_effect=_mock_get_session):
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
            raise SQLAlchemyError("DB gone during cancellation")

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

        with capture_logs() as logs, patch("sova.oversight.agent.get_session", side_effect=RuntimeError("DB gone")):
            await agent._record_run("test-uuid", 1, OversightRunStatus.DONE, 42)

        warnings = [e for e in logs if e["log_level"] == "warning"]
        assert [e["event"] for e in warnings] == ["oversight.record_failed"]
        assert warnings[0]["run_id"] == "test-uuid"


# ---------------------------------------------------------------------------
# Action wiring tests
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_stub_start_run")
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
            patch("sova.oversight.agent.load_config", return_value=mock_config),
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
            patch("sova.oversight.agent.load_config", return_value=mock_config),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch("sova.config.registry.list_projects", return_value={}),
            patch("sova.oversight.actions.propose_issues", side_effect=_mock_propose),
            patch.object(agent, "_auto_triage", side_effect=_mock_triage),
        ):
            await agent._propose_issues([finding])

        assert triage_called is True

    @pytest.mark.asyncio
    async def test_propose_issues_receives_the_agents_db_scope(self, tmp_path) -> None:
        """The action phase writes to the agent's own project database, not the caller's contextvar."""
        from pathlib import Path
        from unittest.mock import MagicMock

        from sova.config.models import TaskSourceConfig

        agent = OversightAgent(config=OversightConfig(wake_interval_minutes=1), project_dir=str(tmp_path))
        mock_config = MagicMock()
        mock_config.github_repo = "owner/repo"
        mock_config.task_source = TaskSourceConfig(type="github")

        with (
            patch("sova.oversight.agent.load_config", return_value=mock_config),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch("sova.config.registry.list_projects", return_value={}),
            patch("sova.oversight.actions.propose_issues", new_callable=AsyncMock, return_value=[]) as mock_propose,
        ):
            await agent._propose_issues([])

        assert mock_propose.await_args.kwargs["project_dir"] == Path(tmp_path)

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
            patch("sova.oversight.agent.load_config", side_effect=_load),
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
            patch("asyncio.wait_for", side_effect=closing_stand_in(raises=TimeoutError)),
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
            capture_logs() as logs,
            patch("sova.oversight.agent.load_config", return_value=mock_config),
            patch("sova.adapters.create_adapter", side_effect=RuntimeError("adapter boom")),
        ):
            await agent._propose_issues([finding])

        assert [e["event"] for e in logs if e["log_level"] == "warning"] == ["oversight.actions.failed"]


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


# ---------------------------------------------------------------------------
# Migration test
# ---------------------------------------------------------------------------


class TestOversightMigration:
    def test_migration_metadata(self) -> None:
        import importlib

        mig = importlib.import_module("sova.db.migrations.versions.022_add_oversight_runs_table")
        assert mig.revision == "022"
        assert mig.down_revision == "021"


# ---------------------------------------------------------------------------
# Run row lifecycle (foreign key from oversight_findings to oversight_runs)
# ---------------------------------------------------------------------------


@pytest.fixture
async def fk_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A file-less SQLite DB that enforces foreign keys, as production does.

    The shared test DB never registers the pragma, so it cannot catch a finding
    written before its OversightRun row exists.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from sova.db.models import Base
    from sova.db.session import _register_sqlite_pragmas

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    _register_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1

    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class TestRunRowLifecycle:
    @staticmethod
    async def _run_cycle(
        agent: OversightAgent,
        factory: async_sessionmaker[AsyncSession],
        findings_json: str,
        *,
        on_analysis: Callable[[], Awaitable[None]] | None = None,
        session_calls: list[dict] | None = None,
    ) -> None:
        """Run one cycle on ``factory``; ``on_analysis`` runs as the LLM call starts, before it answers."""
        from sova.llm.models import LLMResult

        async def _get_session(**kwargs: object) -> AsyncSession:
            if session_calls is not None:
                session_calls.append(kwargs)
            return factory()

        async def _invoke(*args: object, **kwargs: object) -> LLMResult:
            if on_analysis is not None:
                await on_analysis()
            return LLMResult(text=findings_json, model="test")

        provider = AsyncMock()
        provider.invoke = AsyncMock(side_effect=_invoke)
        with (
            patch("sova.oversight.agent.get_session", side_effect=_get_session),
            patch("sova.oversight.analysis.get_session", side_effect=_get_session),
            patch("sova.oversight.actions.get_session", side_effect=_get_session),
            patch("sova.llm.client.get_provider", return_value=provider),
            patch.object(agent, "_reload_config", return_value=agent._config),
            patch.object(agent, "_observe", new_callable=AsyncMock, return_value={"projects": []}),
            patch.object(agent, "_propose_issues", new_callable=AsyncMock),
        ):
            await agent.run_cycle_once()

    @pytest.mark.asyncio
    async def test_findings_persist_under_enforced_foreign_keys(
        self, fk_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The run row exists before analysis writes findings, and ends as one done row."""
        from sqlalchemy import select

        from sova.db.models import OversightFinding, OversightRun

        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))
        findings = '[{"title": "CI flaky", "scope": "global", "severity": "warning"}]'
        await self._run_cycle(agent, fk_session_factory, findings)

        async with fk_session_factory() as session:
            runs = (await session.execute(select(OversightRun))).scalars().all()
            stored = (await session.execute(select(OversightFinding))).scalars().all()

        assert len(runs) == 1
        assert runs[0].status == OversightRunStatus.DONE
        assert runs[0].error is None
        assert runs[0].duration_ms is not None
        assert runs[0].ended_at is not None
        assert [f.title for f in stored] == ["CI flaky"]
        assert stored[0].run_id == runs[0].id

    @pytest.mark.asyncio
    async def test_run_row_is_running_before_analysis_starts(
        self, fk_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A row inserted only once analysis finishes, or inserted as done, fails here."""
        from sqlalchemy import select

        from sova.db.models import OversightRun

        seen: list[list[OversightRunStatus]] = []

        async def _capture_status() -> None:
            async with fk_session_factory() as session:
                rows = (await session.execute(select(OversightRun))).scalars().all()
            seen.append([r.status for r in rows])

        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))
        await self._run_cycle(agent, fk_session_factory, "[]", on_analysis=_capture_status)

        assert seen == [[OversightRunStatus.RUNNING]]
        async with fk_session_factory() as session:
            runs = (await session.execute(select(OversightRun))).scalars().all()
        assert [r.status for r in runs] == [OversightRunStatus.DONE]

    @pytest.mark.asyncio
    async def test_every_cycle_write_uses_the_agents_own_project_db(
        self, fk_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Start, dedup, findings and finish all name the agent's project, not the caller's contextvar."""
        agent = OversightAgent(
            config=OversightConfig(enabled=True, wake_interval_minutes=1),
            project_dir=str(tmp_path),
        )
        calls: list[dict] = []
        findings = '[{"title": "CI flaky", "scope": "global", "severity": "warning"}]'
        await self._run_cycle(agent, fk_session_factory, findings, session_calls=calls)

        assert len(calls) == 4
        assert all(c.get("project_dir") == Path(tmp_path) for c in calls), calls

    @pytest.mark.asyncio
    async def test_agent_without_project_dir_keeps_default_session_resolution(
        self, fk_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))
        calls: list[dict] = []
        await self._run_cycle(agent, fk_session_factory, "[]", session_calls=calls)

        assert calls
        assert all(c.get("project_dir") is None for c in calls), calls

    @pytest.mark.asyncio
    async def test_failed_start_insert_falls_back_to_one_row_at_the_end(
        self, fk_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from sqlalchemy import select

        from sova.db.models import OversightRun

        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))
        with patch.object(agent, "_start_run", new_callable=AsyncMock):
            await self._run_cycle(agent, fk_session_factory, "[]")

        async with fk_session_factory() as session:
            runs = (await session.execute(select(OversightRun))).scalars().all()
        assert [r.status for r in runs] == [OversightRunStatus.DONE]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc", [SQLAlchemyError("boom"), OSError("disk")])
    async def test_start_run_swallows_db_failure(self, exc: Exception) -> None:
        from datetime import datetime, timezone

        agent = OversightAgent(config=OversightConfig(wake_interval_minutes=1))
        with capture_logs() as logs, patch("sova.oversight.agent.get_session", side_effect=exc):
            await agent._start_run("run-x", 1, datetime.now(timezone.utc))

        warnings = [e for e in logs if e["log_level"] == "warning"]
        assert [e["event"] for e in warnings] == ["oversight.start_run_failed"]
        assert warnings[0]["run_id"] == "run-x"

    @pytest.mark.asyncio
    async def test_cancelled_cycle_updates_the_running_row(
        self, fk_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from sqlalchemy import select

        from sova.db.models import OversightRun

        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))

        async def _get_session(**kwargs: object) -> AsyncSession:
            return fk_session_factory()

        with (
            patch("sova.oversight.agent.get_session", side_effect=_get_session),
            patch("sova.oversight.analysis.get_session", side_effect=_get_session),
            patch("sova.oversight.actions.get_session", side_effect=_get_session),
            patch.object(agent, "_reload_config", return_value=agent._config),
            patch.object(agent, "_observe", new_callable=AsyncMock, side_effect=asyncio.CancelledError),
        ):
            with pytest.raises(asyncio.CancelledError):
                await agent.run_cycle_once()

        async with fk_session_factory() as session:
            runs = (await session.execute(select(OversightRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == OversightRunStatus.ERROR
        assert runs[0].error == "cancelled"


class TestCancellationCleanupShield:
    @pytest.mark.asyncio
    async def test_second_cancellation_does_not_abandon_the_finishing_write(self) -> None:
        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))
        cleanup_started = asyncio.Event()
        cleanup_done = asyncio.Event()
        observe_started = asyncio.Event()

        async def _observe_forever() -> None:
            observe_started.set()
            await asyncio.sleep(60)

        async def _slow_record(*args: object, **kwargs: object) -> None:
            cleanup_started.set()
            await asyncio.sleep(0.05)
            cleanup_done.set()

        with (
            patch.object(agent, "_reload_config", return_value=agent._config),
            patch.object(agent, "_start_run", new_callable=AsyncMock),
            patch.object(agent, "_observe", side_effect=_observe_forever),
            patch.object(agent, "_record_run", side_effect=_slow_record),
        ):
            task = asyncio.ensure_future(agent.run_cycle_once())
            await observe_started.wait()
            task.cancel()
            await cleanup_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert cleanup_done.is_set()


class TestWriteShields:
    """Every run-row write on the cancellable cycle task finishes before a cancellation propagates."""

    @pytest.mark.asyncio
    async def test_cancel_during_start_insert_lets_the_insert_finish(self) -> None:
        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))
        start_began = asyncio.Event()
        start_done = asyncio.Event()

        async def _slow_start(*args: object, **kwargs: object) -> None:
            start_began.set()
            await asyncio.sleep(0.05)
            start_done.set()

        with (
            patch.object(agent, "_reload_config", return_value=agent._config),
            patch.object(agent, "_start_run", side_effect=_slow_start),
            patch.object(agent, "_record_error_safe", new_callable=AsyncMock),
        ):
            task = asyncio.ensure_future(agent.run_cycle_once())
            await start_began.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert start_done.is_set()

    @pytest.mark.asyncio
    async def test_cancel_during_generic_error_write_lets_it_finish(self) -> None:
        """A cycle that fails (persona load) and is then cancelled mid error-write still finalizes the row."""
        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))
        error_began = asyncio.Event()
        error_done = asyncio.Event()

        async def _slow_error(*args: object, **kwargs: object) -> None:
            error_began.set()
            await asyncio.sleep(0.05)
            error_done.set()

        with (
            patch.object(agent, "_reload_config", return_value=agent._config),
            patch.object(agent, "_start_run", new_callable=AsyncMock),
            patch("sova.oversight.agent.load_persona", side_effect=ValueError("bad persona")),
            patch.object(agent, "_record_error_safe", side_effect=_slow_error),
        ):
            task = asyncio.ensure_future(agent.run_cycle_once())
            await error_began.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert error_done.is_set()

    @pytest.mark.asyncio
    async def test_repeated_cancellations_do_not_abandon_a_shielded_write(self) -> None:
        began = asyncio.Event()
        done = asyncio.Event()

        async def _slow_write() -> None:
            began.set()
            await asyncio.sleep(0.05)
            done.set()

        task = asyncio.ensure_future(OversightAgent._shielded(_slow_write()))
        await began.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await task

        assert done.is_set()

    @pytest.mark.asyncio
    async def test_cancel_during_normal_finalization_lets_the_write_finish(self) -> None:
        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))
        record_began = asyncio.Event()
        record_done = asyncio.Event()

        async def _slow_record(*args: object, **kwargs: object) -> None:
            record_began.set()
            await asyncio.sleep(0.05)
            record_done.set()

        with (
            patch.object(agent, "_reload_config", return_value=agent._config),
            patch.object(agent, "_start_run", new_callable=AsyncMock),
            patch.object(agent, "_observe", new_callable=AsyncMock, return_value={"projects": []}),
            patch.object(agent, "_analyze", new_callable=AsyncMock, return_value=([], None)),
            patch.object(agent, "_record_run", side_effect=_slow_record),
        ):
            task = asyncio.ensure_future(agent.run_cycle_once())
            await record_began.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert record_done.is_set()

    @pytest.mark.asyncio
    async def test_cancelled_analysis_keeps_the_observed_snapshot(
        self, fk_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from sqlalchemy import select

        from sova.db.models import OversightRun

        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))
        snapshot = {"projects": ["alpha"]}

        async def _get_session(**kwargs: object) -> AsyncSession:
            return fk_session_factory()

        with (
            patch("sova.oversight.agent.get_session", side_effect=_get_session),
            patch("sova.oversight.analysis.get_session", side_effect=_get_session),
            patch("sova.oversight.actions.get_session", side_effect=_get_session),
            patch.object(agent, "_reload_config", return_value=agent._config),
            patch.object(agent, "_observe", new_callable=AsyncMock, return_value=snapshot),
            patch.object(agent, "_analyze", new_callable=AsyncMock, side_effect=asyncio.CancelledError),
        ):
            with pytest.raises(asyncio.CancelledError):
                await agent.run_cycle_once()

        async with fk_session_factory() as session:
            runs = (await session.execute(select(OversightRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].error == "cancelled"
        assert runs[0].snapshot_json == snapshot
