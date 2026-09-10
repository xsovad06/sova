"""Coverage for the narrowed exception handlers audited in issue #643.

Every test here drives one failure branch that the happy-path suites never
reach: a DB session that refuses to open, a `gh` call that returns garbage, a
psutil probe that raises. The assertions check the documented fallback (return
`None`, return `{}`, mark the item failed, re-raise) rather than the log call,
so they stay valid if the logging detail changes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from sova.dashboard.services import agent_db


def _broken_session(exc: Exception | None = None):
    """Return an async `get_session` replacement that always raises."""

    async def _raise(*args, **kwargs):
        raise exc or SQLAlchemyError("db unavailable")

    return _raise


def _agent(**overrides) -> MagicMock:
    agent = MagicMock()
    agent.run_id = 1
    agent.issue = "42"
    agent.role = "developer"
    agent.pr_number = 7
    agent.pre_run_sha = None
    agent.prompt = "do the thing"
    agent.project_dir = Path("/tmp/sova-test")
    agent.last_result_cost = None
    for key, value in overrides.items():
        setattr(agent, key, value)
    return agent


class TestAgentDbSessionFailures:
    """`sova/dashboard/services/agent_db.py` DB-boundary handlers."""

    async def test_create_task_run_returns_none_when_session_fails(self) -> None:
        with patch("sova.db.session.get_session", _broken_session()):
            assert await agent_db._create_task_run("42", "developer", Path("/tmp")) is None

    async def test_update_pid_swallows_session_failure(self) -> None:
        with patch("sova.db.session.get_session", _broken_session(OSError("disk gone"))):
            assert await agent_db._update_task_run_pid(1, 999, Path("/tmp")) is None

    async def test_update_output_path_swallows_session_failure(self) -> None:
        with patch("sova.db.session.get_session", _broken_session(RuntimeError("closed"))):
            assert await agent_db._update_task_run_output_path(1, "/tmp/out", Path("/tmp")) is None

    async def test_finalize_orphaned_run_swallows_session_failure(self) -> None:
        with patch("sova.db.session.get_session", _broken_session()):
            assert await agent_db._finalize_orphaned_run(1, Path("/tmp")) is None

    async def test_fetch_output_lines_returns_none_on_db_error(self) -> None:
        with patch("sova.db.session.get_session", _broken_session()):
            assert await agent_db._fetch_output_lines(1, Path("/tmp")) is None

    async def test_fetch_run_states_returns_empty_dict_on_db_error(self) -> None:
        with patch("sova.db.session.get_session", _broken_session()):
            assert await agent_db._fetch_run_states([1, 2]) == {}

    async def test_dequeue_pr_entry_swallows_db_error(self) -> None:
        with patch("sova.supervisor.pr_throttle.dequeue", side_effect=SQLAlchemyError("locked")):
            assert await agent_db._dequeue_pr_entry(MagicMock(), 1) is None

    async def test_finalize_orphaned_steps_swallows_db_error(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock(side_effect=SQLAlchemyError("locked"))
        assert await agent_db._finalize_orphaned_steps(session, 1) is None

    async def test_validate_pipeline_outcome_returns_none_on_db_error(self) -> None:
        with patch("sova.db.session.get_session", _broken_session()):
            assert await agent_db._validate_pipeline_outcome(1, _agent()) is None

    async def test_downgrade_to_failed_swallows_db_error(self) -> None:
        with patch("sova.db.session.get_session", _broken_session()):
            assert await agent_db._downgrade_to_failed(1, "bypassed", Path("/tmp")) is None

    async def test_downgrade_to_failed_survives_rollback_failure(self) -> None:
        """The downgrade is recorded even when the issue rollback blows up."""
        session = MagicMock()
        task_run = MagicMock()
        task_run.status = "done"
        session.get = AsyncMock(return_value=task_run)
        session.begin = MagicMock(return_value=AsyncMock())
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        async def _get_session(**kwargs):
            return session

        with (
            patch("sova.db.session.get_session", _get_session),
            patch(
                "sova.dashboard.services.agent_recovery.rollback_issue_state",
                side_effect=RuntimeError("tracker offline"),
            ),
        ):
            await agent_db._downgrade_to_failed(1, "bypassed", Path("/tmp"))

        assert task_run.status == "failed"
        assert task_run.error_message == "bypassed"

    async def test_persist_review_verdict_reraises_after_logging(self) -> None:
        with (
            patch("sova.db.session.get_session", _broken_session()),
            pytest.raises(SQLAlchemyError),
        ):
            await agent_db._persist_review_verdict(1, "approve", Path("/tmp"))

    async def test_validate_command_outcome_returns_none_when_validator_raises(self) -> None:
        agent = _agent(role="command:/review-pr")
        with patch.dict(
            agent_db._COMMAND_VALIDATORS,
            {"review-pr": AsyncMock(side_effect=RuntimeError("validator exploded"))},
        ):
            assert await agent_db._validate_command_outcome(1, agent) is None

    async def test_check_pr_branch_pushed_returns_none_when_shell_raises(self) -> None:
        with patch("sova.dashboard.services.agent_db._fetch_pr_fields", side_effect=OSError("no gh")):
            assert await agent_db._check_pr_branch_pushed(_agent()) is None

    async def test_finalize_task_run_returns_false_on_unexpected_error(self) -> None:
        with patch("sova.db.session.get_session", _broken_session(ValueError("bad state"))):
            assert await agent_db._finalize_task_run(1, exit_code=1, agent=_agent()) is False


class TestBatchServiceFailurePaths:
    """`sova/dashboard/services/batch_service.py` per-item and driver handlers."""

    @staticmethod
    def _job(*issue_ids: str):
        from sova.dashboard.services.batch_service import BatchItemResult, BatchJob

        job = BatchJob(batch_id="b1", action="triage", project_dir=Path("/tmp"))
        job.results = [BatchItemResult(issue_id=i) for i in issue_ids]
        return job

    async def test_triage_marks_pending_items_failed_when_setup_raises(self) -> None:
        from sova.dashboard.services.batch_service import _run_batch_triage

        job = self._job("1", "2")
        with patch("sova.db.session.init_db", side_effect=RuntimeError("no db")):
            await _run_batch_triage(job, Path("/tmp"))

        assert [i.status for i in job.results] == ["failed", "failed"]
        assert all("Batch setup failed" in i.detail for i in job.results)

    async def test_triage_records_per_item_failure_without_aborting_batch(self) -> None:
        from sova.dashboard.services.batch_service import _run_batch_triage

        job = self._job("1", "2")
        adapter = AsyncMock()
        adapter.get_task.side_effect = [RuntimeError("issue 1 gone"), MagicMock(state="closed")]

        with (
            patch("sova.db.session.init_db", AsyncMock()),
            patch("sova.config.loader.load_config", return_value=MagicMock()),
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch(
                "sova.dashboard.services.batch_service._try_batch_llm_triage",
                AsyncMock(return_value={}),
            ),
        ):
            await _run_batch_triage(job, Path("/tmp"))

        assert job.results[0].status == "failed"
        assert "issue 1 gone" in job.results[0].detail
        # The second item was still processed rather than aborted.
        assert job.results[1].status != "pending"

    async def test_harden_marks_pending_items_failed_when_setup_raises(self) -> None:
        from sova.dashboard.services.batch_service import _run_batch_harden

        job = self._job("1")
        with patch("sova.db.session.init_db", side_effect=RuntimeError("no db")):
            await _run_batch_harden(job, Path("/tmp"), skip_triage=True)

        assert job.results[0].status == "failed"
        assert "Batch setup failed" in job.results[0].detail

    async def test_harden_continues_when_list_tasks_raises(self) -> None:
        """A failing `list_tasks` degrades the issue summary but still hardens."""
        from sova.dashboard.services.batch_service import _run_batch_harden

        job = self._job("1")
        adapter = AsyncMock()
        adapter.list_tasks.side_effect = RuntimeError("api down")
        adapter.get_task.side_effect = RuntimeError("issue gone")

        with (
            patch("sova.db.session.init_db", AsyncMock()),
            patch("sova.config.loader.load_config", return_value=MagicMock()),
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.cli.commands.harden._load_project_docs", return_value=""),
            patch("sova.cli.commands.harden._format_issues_summary", return_value=""),
        ):
            await _run_batch_harden(job, Path("/tmp"), skip_triage=True)

        adapter.list_tasks.assert_awaited()
        assert job.results[0].status == "failed"

    async def test_llm_batch_triage_returns_empty_dict_on_failure(self) -> None:
        from sova.dashboard.services.batch_service import _try_batch_llm_triage

        job = self._job("1")
        config = MagicMock()
        config.llm.batch_eligible_tasks = ["triage"]
        role = MagicMock()
        role.assess_tasks_batch = AsyncMock(side_effect=RuntimeError("batch api down"))
        adapter = AsyncMock()
        adapter.get_task.return_value = MagicMock(id="1", state="backlog")

        with patch(
            "sova.llm.providers.anthropic_batch.create_batch_provider",
            return_value=MagicMock(),
        ):
            assert await _try_batch_llm_triage(job, config, adapter, role, Path("/tmp")) == {}

    async def test_llm_batch_triage_skips_issues_whose_fetch_fails(self) -> None:
        from sova.dashboard.services.batch_service import _try_batch_llm_triage

        job = self._job("1")
        config = MagicMock()
        config.llm.batch_eligible_tasks = ["triage"]
        adapter = AsyncMock()
        adapter.get_task.side_effect = RuntimeError("issue gone")
        role = MagicMock()
        role.assess_tasks_batch = AsyncMock()

        with patch(
            "sova.llm.providers.anthropic_batch.create_batch_provider",
            return_value=MagicMock(),
        ):
            assert await _try_batch_llm_triage(job, config, adapter, role, Path("/tmp")) == {}

        # No eligible task survived, so the LLM was never billed.
        role.assess_tasks_batch.assert_not_awaited()


class TestGitHubAdapterMalformedJson:
    """`sova/adapters/github.py` turns unparseable `gh` output into a clear error."""

    @staticmethod
    def _adapter():
        from sova.adapters.github import GitHubAdapter

        return GitHubAdapter(repo="owner/repo", github_user="tester")

    async def test_get_task_raises_runtime_error_on_bad_json(self) -> None:
        result = MagicMock(success=True, stdout="not json at all", stderr="")
        with (
            patch("sova.adapters.github.GitHubAdapter._gh", AsyncMock(return_value=result)),
            pytest.raises(RuntimeError, match="Failed to parse issue #42"),
        ):
            await self._adapter().get_task("42")

    async def test_get_state_raises_runtime_error_on_bad_json(self) -> None:
        result = MagicMock(success=True, stdout="<html>rate limited</html>", stderr="")
        with (
            patch("sova.adapters.github.GitHubAdapter._gh", AsyncMock(return_value=result)),
            pytest.raises(RuntimeError, match="Failed to parse state for issue #42"),
        ):
            await self._adapter().get_state("42")


class TestResourceServiceFailurePaths:
    """`sova/dashboard/services/resource_service.py` router-facing handlers."""

    async def test_total_energy_reraises_db_error(self) -> None:
        from sova.dashboard.services import resource_service

        with (
            patch("sova.db.session.get_session", _broken_session()),
            pytest.raises(SQLAlchemyError),
        ):
            await resource_service.get_total_energy(Path("/tmp"))

    async def test_capacity_reraises_config_load_error(self) -> None:
        from sova.dashboard.services import resource_service

        with (
            patch("sova.config.loader.load_config", side_effect=RuntimeError("bad config")),
            pytest.raises(RuntimeError, match="bad config"),
        ):
            await resource_service.get_capacity_recommendation(Path("/tmp"))

    async def test_capacity_reraises_db_query_error(self) -> None:
        from sova.dashboard.services import resource_service

        with (
            patch("sova.config.loader.load_config", return_value=MagicMock(max_parallel_agents=3)),
            patch("sova.db.session.get_session", _broken_session()),
            pytest.raises(SQLAlchemyError),
        ):
            await resource_service.get_capacity_recommendation(Path("/tmp"))

    async def test_capacity_system_metrics_failure_falls_back_to_zero(self, tmp_path: Path) -> None:
        from sova.dashboard.services import resource_service
        from sova.db.session import init_db

        await init_db(tmp_path)
        cfg = MagicMock()
        cfg.max_parallel_agents = 3
        cfg.monitoring.safety_margin = 0.2

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("psutil.cpu_count", side_effect=OSError("no cpu info")),
            patch.object(resource_service, "get_cross_project_metrics", return_value={}),
        ):
            result = await resource_service.get_capacity_recommendation(tmp_path)

        assert result is not None

    async def test_capacity_cross_project_failure_is_non_fatal(self, tmp_path: Path) -> None:
        from sova.dashboard.services import resource_service
        from sova.db.session import init_db

        await init_db(tmp_path)
        cfg = MagicMock()
        cfg.max_parallel_agents = 3
        cfg.monitoring.safety_margin = 0.2

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch.object(resource_service, "get_cross_project_metrics", side_effect=RuntimeError("registry gone")),
        ):
            result = await resource_service.get_capacity_recommendation(tmp_path)

        assert result is not None


class TestSchedulerServerFailurePaths:
    """`sova/scheduler/server.py` health probes degrade instead of raising."""

    @staticmethod
    def _server():
        from sova.scheduler.server import SOVAServer

        return SOVAServer(config=MagicMock(), project_dir=Path("/tmp"))

    async def test_db_health_check_reports_unhealthy_on_error(self) -> None:
        with patch("sova.db.session.get_session", _broken_session()):
            assert await self._server()._check_db_connection() is False

    def test_active_agent_count_falls_back_to_zero(self) -> None:
        server = self._server()
        with patch("sova.dashboard.services.control_service._projects", None):
            # `None` has no `.values()`, so the AttributeError branch is taken.
            assert server._count_active_agents() == 0


class TestMergeQueueMonitorFailurePaths:
    """`sova/dashboard/services/merge_queue_monitor.py` DB and startup handlers."""

    async def test_create_entry_returns_none_on_db_error(self) -> None:
        from sova.dashboard.services.merge_queue_monitor import create_merge_queue_entry

        with patch("sova.db.session.get_session", _broken_session()):
            entry_id = await create_merge_queue_entry(
                pr_number=7,
                repo="owner/repo",
                project_dir=Path("/tmp"),
            )
        assert entry_id is None

    async def test_load_queued_entries_returns_empty_list_on_db_error(self) -> None:
        from sova.dashboard.services.merge_queue_monitor import _load_queued_entries

        with patch("sova.db.session.get_session", _broken_session()):
            assert await _load_queued_entries(Path("/tmp")) == []

    async def test_update_entry_status_swallows_db_error(self) -> None:
        from sova.dashboard.services.merge_queue_monitor import _update_entry_status

        with patch("sova.db.session.get_session", _broken_session()):
            assert await _update_entry_status(1, "merged", Path("/tmp")) is None

    def test_monitor_creation_skips_projects_with_unloadable_config(self, tmp_path: Path) -> None:
        from sova.dashboard.services.merge_queue_monitor import create_monitors_for_merge_queue

        with (
            patch(
                "sova.config.registry.list_projects",
                return_value={"broken": str(tmp_path)},
            ),
            patch("sova.config.loader.load_config", side_effect=RuntimeError("bad toml")),
        ):
            assert create_monitors_for_merge_queue() == []
