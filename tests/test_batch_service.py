"""Tests for sova.dashboard.services.batch_service."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig
from sova.dashboard.services.batch_service import (
    DEFAULT_CONCURRENCY,
    BatchJob,
    _active_batches,
    cancel_batch,
    get_active_batch,
    get_batch_status,
    start_batch,
    start_batch_run,
)
from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture(autouse=True)
def clear_batches():
    _active_batches.clear()
    yield
    _active_batches.clear()


def _mock_adapter(tasks: list[Task] | None = None) -> AsyncMock:
    adapter = AsyncMock()
    task_list = tasks or [
        Task(id="1", title="First", body="Body", state=TaskState.BACKLOG),
        Task(id="2", title="Second", body="Body", state=TaskState.BACKLOG),
    ]
    adapter.list_tasks.return_value = task_list
    adapter.get_task.side_effect = lambda tid: next((t for t in task_list if t.id == tid), None)
    return adapter


def _mock_config() -> ProjectConfig:
    return ProjectConfig(github_repo="test/repo", task_source={"type": "github"})


class TestBatchJobModel:
    def test_to_dict_structure(self) -> None:
        from sova.dashboard.services.batch_service import BatchItemResult

        job = BatchJob(
            batch_id="abc123",
            action="triage",
            max_concurrency=3,
            results=[
                BatchItemResult(issue_id="1", status="done", detail="ok"),
                BatchItemResult(issue_id="2", status="pending"),
            ],
        )
        d = job.to_dict()
        assert d["batch_id"] == "abc123"
        assert d["total"] == 2
        assert d["completed"] == 1
        assert d["failed"] == 0
        assert d["max_concurrency"] == 3
        assert len(d["results"]) == 2

    def test_completed_count(self) -> None:
        from sova.dashboard.services.batch_service import BatchItemResult

        job = BatchJob(
            batch_id="x",
            action="harden",
            results=[
                BatchItemResult(issue_id="1", status="done"),
                BatchItemResult(issue_id="2", status="failed"),
                BatchItemResult(issue_id="3", status="skipped"),
                BatchItemResult(issue_id="4", status="running"),
                BatchItemResult(issue_id="5", status="pending"),
            ],
        )
        assert job.completed_count == 3
        assert job.total == 5


class TestGetBatchStatus:
    def test_returns_none_for_unknown(self) -> None:
        assert get_batch_status("nonexistent") is None

    def test_returns_dict_for_known(self) -> None:
        job = BatchJob(batch_id="test1", action="triage", results=[])
        _active_batches["test1"] = job
        result = get_batch_status("test1")
        assert result is not None
        assert result["batch_id"] == "test1"


class TestCancelBatch:
    def test_cancel_unknown_returns_false(self) -> None:
        assert cancel_batch("nonexistent") is False

    def test_cancel_running_returns_true(self) -> None:
        job = BatchJob(batch_id="test2", action="triage", status="running", results=[])
        _active_batches["test2"] = job
        assert cancel_batch("test2") is True
        assert job.cancelled is True

    def test_cancel_done_returns_false(self) -> None:
        job = BatchJob(batch_id="test3", action="triage", status="done", results=[])
        _active_batches["test3"] = job
        assert cancel_batch("test3") is False


class TestGetActiveBatch:
    def test_returns_none_when_empty(self) -> None:
        assert get_active_batch() is None

    def test_returns_none_when_all_done(self) -> None:
        _active_batches["done1"] = BatchJob(batch_id="done1", action="triage", status="done", results=[])
        assert get_active_batch() is None

    def test_returns_running_batch(self) -> None:
        _active_batches["run1"] = BatchJob(batch_id="run1", action="harden", status="running", results=[])
        result = get_active_batch()
        assert result is not None
        assert result["batch_id"] == "run1"
        assert result["action"] == "harden"

    def test_returns_first_running_when_multiple(self) -> None:
        _active_batches["done1"] = BatchJob(batch_id="done1", action="triage", status="done", results=[])
        _active_batches["run1"] = BatchJob(batch_id="run1", action="harden", status="running", results=[])
        result = get_active_batch()
        assert result is not None
        assert result["batch_id"] == "run1"

    def test_filters_by_project_dir(self) -> None:
        proj_a = Path("/projects/alpha")
        proj_b = Path("/projects/beta")
        _active_batches["a1"] = BatchJob(
            batch_id="a1",
            action="harden",
            status="running",
            results=[],
            project_dir=proj_a,
        )
        assert get_active_batch(proj_a) is not None
        assert get_active_batch(proj_a)["batch_id"] == "a1"
        assert get_active_batch(proj_b) is None

    def test_no_filter_returns_any(self) -> None:
        _active_batches["a1"] = BatchJob(
            batch_id="a1",
            action="triage",
            status="running",
            results=[],
            project_dir=Path("/proj/x"),
        )
        result = get_active_batch()
        assert result is not None
        assert result["batch_id"] == "a1"


class TestStartBatch:
    @patch("sova.dashboard.services.batch_service._run_batch_triage")
    async def test_start_triage_returns_batch_id(self, mock_worker) -> None:
        mock_worker.return_value = None
        batch_id = start_batch("triage", ["1", "2"], Path("/tmp"))
        assert batch_id is not None
        assert len(batch_id) == 12
        assert batch_id in _active_batches
        job = _active_batches[batch_id]
        assert job.action == "triage"
        assert job.total == 2
        assert job.project_dir == Path("/tmp")

    @patch("sova.dashboard.services.batch_service._run_batch_harden")
    async def test_start_harden_returns_batch_id(self, mock_worker) -> None:
        mock_worker.return_value = None
        batch_id = start_batch("harden", ["3"], Path("/tmp"), {"skip_triage": True})
        assert batch_id in _active_batches
        job = _active_batches[batch_id]
        assert job.action == "harden"

    @patch("sova.dashboard.services.batch_service._run_batch_triage")
    async def test_triage_uses_default_concurrency(self, mock_worker) -> None:
        mock_worker.return_value = None
        batch_id = start_batch("triage", ["1"], Path("/tmp"))
        job = _active_batches[batch_id]
        assert job.max_concurrency == DEFAULT_CONCURRENCY["triage"]

    @patch("sova.dashboard.services.batch_service._run_batch_harden")
    async def test_harden_uses_default_concurrency(self, mock_worker) -> None:
        mock_worker.return_value = None
        batch_id = start_batch("harden", ["1"], Path("/tmp"))
        job = _active_batches[batch_id]
        assert job.max_concurrency == DEFAULT_CONCURRENCY["harden"]

    @patch("sova.dashboard.services.batch_service._run_batch_triage")
    async def test_custom_concurrency_override(self, mock_worker) -> None:
        mock_worker.return_value = None
        batch_id = start_batch("triage", ["1"], Path("/tmp"), {"max_concurrency": 5})
        job = _active_batches[batch_id]
        assert job.max_concurrency == 5

    async def test_unknown_action_marks_failed(self) -> None:
        batch_id = start_batch("unknown_action", ["1"], Path("/tmp"))
        job = _active_batches[batch_id]
        assert job.status == "done"
        assert all(r.status == "failed" for r in job.results)


class TestBatchTriage:
    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch("sova.config.loader.load_config")
    @patch("sova.adapters.create_adapter")
    @patch("sova.roles.triage.TriageRole")
    async def test_triage_processes_all_issues(
        self, mock_role_cls, mock_create_adapter, mock_config, mock_init_db
    ) -> None:
        from sova.dashboard.services.batch_service import BatchItemResult, _run_batch_triage

        adapter = _mock_adapter()
        mock_create_adapter.return_value = adapter
        mock_config.return_value = _mock_config()

        mock_role = mock_role_cls.return_value
        mock_assessment = AsyncMock()
        mock_assessment.suitability = "ready"
        mock_role.heuristic_assess = MagicMock(return_value=mock_assessment)
        mock_role.SUITABILITY_LABELS = {"ready": "agent:ready"}
        mock_role.allowed_input_states = frozenset({TaskState.BACKLOG})
        mock_role._build_assessment_comment.return_value = "Assessment comment"

        job = BatchJob(
            batch_id="t1",
            action="triage",
            results=[
                BatchItemResult(issue_id="1"),
                BatchItemResult(issue_id="2"),
            ],
        )

        await _run_batch_triage(job, Path("/tmp"))

        assert job.status == "done"
        assert all(r.status == "done" for r in job.results)
        assert mock_role.heuristic_assess.call_count == 2
        assert adapter.add_label.call_count == 2
        assert adapter.transition_state.call_count == 2

    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch("sova.config.loader.load_config")
    @patch("sova.adapters.create_adapter")
    @patch("sova.roles.triage.TriageRole")
    async def test_triage_skips_non_backlog(
        self, mock_role_cls, mock_create_adapter, mock_config, mock_init_db
    ) -> None:
        from sova.dashboard.services.batch_service import BatchItemResult, _run_batch_triage

        tasks = [
            Task(id="1", title="Triaged", body="Body", state=TaskState.TRIAGED),
        ]
        adapter = _mock_adapter(tasks)
        mock_create_adapter.return_value = adapter
        mock_config.return_value = _mock_config()

        job = BatchJob(
            batch_id="t2",
            action="triage",
            results=[BatchItemResult(issue_id="1")],
        )

        await _run_batch_triage(job, Path("/tmp"))

        assert job.results[0].status == "skipped"
        mock_role_cls.return_value.heuristic_assess.assert_not_called()

    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch("sova.config.loader.load_config")
    @patch("sova.adapters.create_adapter")
    @patch("sova.roles.triage.TriageRole")
    async def test_triage_cancel_skips_remaining(
        self, mock_role_cls, mock_create_adapter, mock_config, mock_init_db
    ) -> None:
        from sova.dashboard.services.batch_service import BatchItemResult, _run_batch_triage

        adapter = _mock_adapter()
        mock_create_adapter.return_value = adapter
        mock_config.return_value = _mock_config()

        mock_role = mock_role_cls.return_value
        mock_assessment = AsyncMock()
        mock_assessment.suitability = "ready"
        mock_role.heuristic_assess = MagicMock(return_value=mock_assessment)
        mock_role.SUITABILITY_LABELS = {"ready": "agent:ready"}
        mock_role.allowed_input_states = frozenset({TaskState.BACKLOG})
        mock_role._build_assessment_comment.return_value = "Assessment"

        job = BatchJob(
            batch_id="t3",
            action="triage",
            results=[
                BatchItemResult(issue_id="1"),
                BatchItemResult(issue_id="2"),
            ],
            cancelled=True,
        )

        await _run_batch_triage(job, Path("/tmp"))

        assert job.status == "cancelled"
        assert all(r.status == "skipped" for r in job.results)

    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch("sova.config.loader.load_config")
    @patch("sova.adapters.create_adapter")
    @patch("sova.roles.triage.TriageRole")
    async def test_triage_emits_feed_when_labels_created(
        self, mock_role_cls, mock_create_adapter, mock_config, mock_init_db
    ) -> None:
        """Pre-flight creates missing labels and emits an info feed event."""
        from sova.dashboard.services.batch_service import BatchItemResult, _run_batch_triage

        adapter = _mock_adapter()
        adapter.ensure_repo_labels = AsyncMock(return_value=["agent:ready", "agent:triaged"])
        mock_create_adapter.return_value = adapter
        mock_config.return_value = _mock_config()

        mock_role = mock_role_cls.return_value
        mock_assessment = MagicMock()
        mock_assessment.suitability = "ready"
        mock_role.heuristic_assess = MagicMock(return_value=mock_assessment)
        mock_role.SUITABILITY_LABELS = {"ready": "agent:ready"}
        mock_role.allowed_input_states = frozenset({TaskState.BACKLOG})
        mock_role._build_assessment_comment.return_value = "Assessment"

        emitted: list[str] = []

        def _capture_emit(title, **kwargs):
            emitted.append(title)

        job = BatchJob(
            batch_id="label_ok",
            action="triage",
            results=[BatchItemResult(issue_id="1")],
        )

        with patch("sova.dashboard.services.batch_service.emit_safe", side_effect=_capture_emit):
            await _run_batch_triage(job, Path("/tmp"))

        assert any("Created 2 missing agent label(s)" in m for m in emitted)
        adapter.ensure_repo_labels.assert_awaited_once()

    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch("sova.config.loader.load_config")
    @patch("sova.adapters.create_adapter")
    @patch("sova.roles.triage.TriageRole")
    async def test_triage_aborts_when_label_preflight_fails(
        self, mock_role_cls, mock_create_adapter, mock_config, mock_init_db
    ) -> None:
        """Pre-flight failure marks all items failed and emits an error feed event."""
        from sova.dashboard.services.batch_service import BatchItemResult, _run_batch_triage
        from sova.dashboard.services.feed_service import FeedEventSeverity

        adapter = _mock_adapter()
        adapter.ensure_repo_labels = AsyncMock(
            side_effect=RuntimeError("Could not create label 'agent:ready': permission denied")
        )
        mock_create_adapter.return_value = adapter
        mock_config.return_value = _mock_config()

        emitted_severities: list[str] = []

        def _capture_emit(title, severity=FeedEventSeverity.info, **kwargs):
            emitted_severities.append(severity)

        job = BatchJob(
            batch_id="label_fail",
            action="triage",
            results=[
                BatchItemResult(issue_id="1"),
                BatchItemResult(issue_id="2"),
            ],
        )

        with patch("sova.dashboard.services.batch_service.emit_safe", side_effect=_capture_emit):
            await _run_batch_triage(job, Path("/tmp"))

        assert all(r.status == "failed" for r in job.results)
        assert "missing agent labels" in job.results[0].detail
        assert FeedEventSeverity.error in emitted_severities
        mock_role_cls.return_value.heuristic_assess.assert_not_called()

    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch("sova.config.loader.load_config")
    @patch("sova.adapters.create_adapter")
    @patch("sova.roles.triage.TriageRole")
    async def test_triage_skips_preflight_when_cancelled(
        self, mock_role_cls, mock_create_adapter, mock_config, mock_init_db
    ) -> None:
        """Pre-flight is skipped for cancelled batches; items are marked skipped by the loop."""
        from sova.dashboard.services.batch_service import BatchItemResult, _run_batch_triage

        adapter = _mock_adapter()
        mock_create_adapter.return_value = adapter
        mock_config.return_value = _mock_config()

        job = BatchJob(
            batch_id="cancelled_preflight",
            action="triage",
            cancelled=True,
            results=[BatchItemResult(issue_id="1")],
        )

        await _run_batch_triage(job, Path("/tmp"))

        adapter.ensure_repo_labels.assert_not_awaited()
        assert all(r.status == "skipped" for r in job.results)


class TestBatchHarden:
    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch("sova.config.loader.load_config")
    @patch("sova.adapters.create_adapter")
    @patch("sova.llm.client.invoke", new_callable=AsyncMock)
    async def test_harden_processes_issues(self, mock_invoke, mock_create_adapter, mock_config, mock_init_db) -> None:
        from sova.dashboard.services.batch_service import BatchItemResult, _run_batch_harden
        from sova.llm.models import LLMResult

        tasks = [
            Task(id="1", title="First", body="Body", state=TaskState.BACKLOG, labels=["type:feature"]),
        ]
        adapter = _mock_adapter(tasks)
        mock_create_adapter.return_value = adapter
        mock_config.return_value = _mock_config()

        mock_invoke.return_value = LLMResult(
            text="## Objective\nEnriched content",
            model="claude",
        )

        job = BatchJob(
            batch_id="h1",
            action="harden",
            results=[BatchItemResult(issue_id="1")],
        )

        await _run_batch_harden(job, Path("/tmp"), skip_triage=True)

        assert job.status == "done"
        assert job.results[0].status == "done"
        adapter.edit_body.assert_called_once()
        adapter.post_comment.assert_not_called()

    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch("sova.config.loader.load_config")
    @patch("sova.adapters.create_adapter")
    @patch("sova.llm.client.invoke", new_callable=AsyncMock)
    async def test_harden_skips_ineligible_state(
        self, mock_invoke, mock_create_adapter, mock_config, mock_init_db
    ) -> None:
        from sova.dashboard.services.batch_service import BatchItemResult, _run_batch_harden

        tasks = [
            Task(id="1", title="Done", body="Body", state=TaskState.DONE),
        ]
        adapter = _mock_adapter(tasks)
        mock_create_adapter.return_value = adapter
        mock_config.return_value = _mock_config()

        job = BatchJob(
            batch_id="h2",
            action="harden",
            results=[BatchItemResult(issue_id="1")],
        )

        await _run_batch_harden(job, Path("/tmp"), skip_triage=False)

        assert job.results[0].status == "skipped"
        mock_invoke.assert_not_called()


class TestBatchConcurrency:
    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch("sova.config.loader.load_config")
    @patch("sova.adapters.create_adapter")
    @patch("sova.roles.triage.TriageRole")
    async def test_triage_respects_concurrency_limit(
        self, mock_role_cls, mock_create_adapter, mock_config, mock_init_db
    ) -> None:
        from sova.dashboard.services.batch_service import BatchItemResult, _run_batch_triage

        tasks = [Task(id=str(i), title=f"Task {i}", body="Body", state=TaskState.BACKLOG) for i in range(1, 5)]
        adapter = _mock_adapter(tasks)
        mock_create_adapter.return_value = adapter
        mock_config.return_value = _mock_config()

        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        mock_assessment = AsyncMock()
        mock_assessment.suitability = "ready"

        task_list = [Task(id=str(i), title=f"Task {i}", body="Body", state=TaskState.BACKLOG) for i in range(1, 5)]

        async def _tracking_get_task(issue_id):
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
            await asyncio.sleep(0.01)
            async with lock:
                current_concurrent -= 1
            return next((t for t in task_list if t.id == issue_id), task_list[0])

        adapter.get_task = AsyncMock(side_effect=_tracking_get_task)

        mock_role = mock_role_cls.return_value
        mock_role.heuristic_assess = MagicMock(return_value=mock_assessment)
        mock_role.SUITABILITY_LABELS = {"ready": "agent:ready"}
        mock_role.allowed_input_states = frozenset({TaskState.BACKLOG})
        mock_role._build_assessment_comment.return_value = "Assessment"

        job = BatchJob(
            batch_id="conc1",
            action="triage",
            max_concurrency=2,
            results=[BatchItemResult(issue_id=str(i)) for i in range(1, 5)],
        )

        await _run_batch_triage(job, Path("/tmp"))

        assert job.status == "done"
        assert all(r.status == "done" for r in job.results)
        assert max_concurrent <= 2

    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch("sova.config.loader.load_config")
    @patch("sova.adapters.create_adapter")
    @patch("sova.llm.client.invoke", new_callable=AsyncMock)
    async def test_harden_respects_concurrency_limit(
        self, mock_invoke, mock_create_adapter, mock_config, mock_init_db
    ) -> None:
        from sova.dashboard.services.batch_service import BatchItemResult, _run_batch_harden
        from sova.llm.models import LLMResult

        tasks = [
            Task(id=str(i), title=f"Task {i}", body="Body", state=TaskState.BACKLOG, labels=["type:feature"])
            for i in range(1, 5)
        ]
        adapter = _mock_adapter(tasks)
        mock_create_adapter.return_value = adapter
        mock_config.return_value = _mock_config()

        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def _tracking_invoke(*args, **kwargs):
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
            await asyncio.sleep(0.01)
            async with lock:
                current_concurrent -= 1
            return LLMResult(text="## Objective\nEnriched", model="claude")

        mock_invoke.side_effect = _tracking_invoke

        job = BatchJob(
            batch_id="conc2",
            action="harden",
            max_concurrency=2,
            results=[BatchItemResult(issue_id=str(i)) for i in range(1, 5)],
        )

        await _run_batch_harden(job, Path("/tmp"), skip_triage=True)

        assert job.status == "done"
        assert all(r.status == "done" for r in job.results)
        assert max_concurrent <= 2


class TestStartBatchRun:
    @patch("sova.dashboard.services.control_service.start_agent", new_callable=AsyncMock)
    async def test_starts_first_issue(self, mock_start) -> None:
        mock_start.return_value = {"status": "started", "pid": 1234}

        result = await start_batch_run(["10", "20", "30"], Path("/tmp"))

        assert result["started"] == "10"
        assert result["remaining"] == ["20", "30"]
        mock_start.assert_called_once_with(issue="10")

    async def test_empty_issues_returns_error(self) -> None:
        result = await start_batch_run([], Path("/tmp"))
        assert "error" in result
