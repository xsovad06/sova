"""Regression tests for richer agent failure capture (issue #975).

Covers the bare exit-code message built by _finalize_task_run() in agent_db.py.
_extract_failure_detail() coverage (the Claude CLI provider's other capture
site) lives in tests/test_llm.py::TestExtractFailureDetail.
"""

from __future__ import annotations

from collections import deque

import pytest

from sova.dashboard.services.agent_db import _build_exit_failure_message


@pytest.fixture(autouse=True)
async def setup_db(monkeypatch: pytest.MonkeyPatch):
    """Initialize an in-memory DB for the _finalize_task_run integration test."""
    from sova.db.session import close_db, init_db

    monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
    await init_db(run_migrations=False)
    yield
    await close_db()


class TestBuildExitFailureMessage:
    """Scenarios (c), (d), (e): enriched bare exit-code messages."""

    def test_bare_exit_code_with_no_output_and_no_step(self) -> None:
        message = _build_exit_failure_message(1, deque(), None)

        assert message == "Process exited with code 1"

    def test_includes_output_tail_and_step_name(self) -> None:
        output = deque(["line one", "line two", "line three"])

        message = _build_exit_failure_message(1, output, "develop")

        assert "Process exited with code 1" in message
        assert "line one" in message
        assert "line three" in message
        assert "step=develop" in message

    def test_redacts_secrets_in_output_tail(self) -> None:
        output = deque(["about to call the API", "api_key=sk-ant-abcdef1234567890abcdef1234567890"])

        message = _build_exit_failure_message(1, output, "develop")

        assert "sk-ant-abcdef1234567890abcdef1234567890" not in message
        assert "REDACTED" in message

    def test_omits_agent_sentinel_step(self) -> None:
        message = _build_exit_failure_message(1, deque(["some output"]), "agent")

        assert "step=" not in message

    def test_oversized_output_tail_is_truncated_to_bound(self) -> None:
        output = deque([f"line {i} " + "x" * 50 for i in range(200)])

        message = _build_exit_failure_message(1, output, "develop")

        assert len(message) <= 2000
        # Only the tail of the output should survive truncation.
        assert "line 199" in message

    def test_single_oversized_line_is_bounded_before_redaction(self) -> None:
        # A single stream-json line can carry an arbitrarily large text block; each raw
        # line must be capped before joining so the redaction scan input stays bounded.
        output = deque(["x" * 100_000])

        message = _build_exit_failure_message(1, output, "develop")

        assert len(message) <= 2000

    def test_length_bound_applies_after_concatenating_fragments(self) -> None:
        output = deque(["y" * 500])
        long_step_name = "step_" + "z" * 2500

        message = _build_exit_failure_message(1, output, long_step_name)

        assert len(message) == 2000

    def test_message_is_a_single_line(self) -> None:
        output = deque(["line one", "line two", "line three"])

        message = _build_exit_failure_message(1, output, "develop")

        assert "\n" not in message
        assert message == "Process exited with code 1 (step=develop); last output: line one | line two | line three"

    def test_secret_straddling_truncation_boundary_is_still_redacted(self) -> None:
        secret = "api_key=sk-ant-abcdef1234567890abcdef1234567890"
        # Trailing padding pushes the secret's start (index 0 of the joined text)
        # 19 chars before the last-500-chars cutoff, so truncating before redacting
        # would slice off the "api_key=sk-" prefix and let the regex miss entirely.
        trailing = "y" * 471
        output = deque([secret, trailing])

        message = _build_exit_failure_message(1, output, "develop")

        assert secret not in message
        assert "REDACTED" in message


class TestClassifyExitStatus:
    """_classify_exit_status() (issue #978): stop() record wins, else signal shape, else failed."""

    def test_deliberate_stop_wins_regardless_of_exit_code(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _classify_exit_status
        from sova.ipc.control import TerminationRecord

        agent = MagicMock()
        agent.process.termination_record = TerminationRecord(cause="manual_stop", requester="dashboard", signal=15)

        # Even a SIGKILL-shaped exit code (escalation) is still "stopped".
        status, reason = _classify_exit_status(137, agent)
        assert status == "stopped"
        assert reason == "manual_stop"

    def test_signal_shaped_exit_with_no_record_is_interrupted(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _classify_exit_status

        agent = MagicMock()
        agent.process.termination_record = None

        status, reason = _classify_exit_status(143, agent)
        assert status == "interrupted"
        assert reason == "external_signal"

        status, reason = _classify_exit_status(-15, agent)
        assert status == "interrupted"
        assert reason == "external_signal"

    def test_ordinary_nonzero_exit_with_no_record_is_failed(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _classify_exit_status

        agent = MagicMock()
        agent.process.termination_record = None

        status, reason = _classify_exit_status(1, agent)
        assert status == "failed"
        assert reason is None

    def test_none_process_is_treated_as_no_record(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _classify_exit_status

        agent = MagicMock()
        agent.process = None

        status, reason = _classify_exit_status(1, agent)
        assert status == "failed"
        assert reason is None


class TestMemorySnapshotText:
    """_memory_snapshot_text() (issue #978): fails open, never raises."""

    def test_returns_none_when_psutil_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sova.dashboard.services import agent_db

        monkeypatch.setattr(agent_db, "_PSUTIL_AVAILABLE", False)

        assert agent_db._memory_snapshot_text() is None

    def test_returns_none_on_psutil_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services import agent_db

        monkeypatch.setattr(agent_db, "_PSUTIL_AVAILABLE", True)
        broken_psutil = MagicMock()
        broken_psutil.virtual_memory.side_effect = RuntimeError("boom")
        monkeypatch.setattr(agent_db, "psutil", broken_psutil, raising=False)

        assert agent_db._memory_snapshot_text() is None

    def test_returns_formatted_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services import agent_db

        monkeypatch.setattr(agent_db, "_PSUTIL_AVAILABLE", True)
        fake_psutil = MagicMock()
        fake_psutil.virtual_memory.return_value = MagicMock(available=1 * 1024**3, total=8 * 1024**3)
        monkeypatch.setattr(agent_db, "psutil", fake_psutil, raising=False)

        snapshot = agent_db._memory_snapshot_text()
        assert snapshot is not None
        assert "1.00GB available" in snapshot
        assert "8.00GB total" in snapshot


class TestEmitFinalizeEventSeverity:
    """_emit_finalize_event() (issue #978): severity must reflect the real outcome.

    Before "stopped" existed, only "failed" mapped to error and everything else
    (including "interrupted") mapped to success, which already mislabeled a
    crashed/externally-killed run as a success feed event. Adding "stopped"
    without updating this mapping would have compounded it for the exact new
    case this PR introduces.
    """

    def _capture_severity(self, monkeypatch: pytest.MonkeyPatch, status: str) -> object:
        from decimal import Decimal
        from unittest.mock import MagicMock

        from sova.dashboard.services import agent_db

        captured = {}

        def fake_emit_safe(*args, **kwargs):
            captured["severity"] = kwargs.get("severity")

        monkeypatch.setattr(agent_db, "emit_safe", fake_emit_safe)
        agent = MagicMock()
        agent.issue = "978"
        agent.role = "developer"
        agent_db._emit_finalize_event(1, status=status, exit_code=1, agent=agent, cost=Decimal("0"))
        return captured["severity"]

    def test_failed_is_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sova.dashboard.services.feed_service import FeedEventSeverity

        assert self._capture_severity(monkeypatch, "failed") == FeedEventSeverity.error

    def test_interrupted_is_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sova.dashboard.services.feed_service import FeedEventSeverity

        assert self._capture_severity(monkeypatch, "interrupted") == FeedEventSeverity.error

    def test_stopped_is_warning_not_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sova.dashboard.services.feed_service import FeedEventSeverity

        assert self._capture_severity(monkeypatch, "stopped") == FeedEventSeverity.warning

    def test_done_is_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sova.dashboard.services.feed_service import FeedEventSeverity

        assert self._capture_severity(monkeypatch, "done") == FeedEventSeverity.success


class TestFinalizeTaskRunEnrichedMessage:
    """Integration: _finalize_task_run() must persist the enriched message."""

    async def test_finalize_persists_output_tail_and_step_name(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="975",
                    role="developer",
                    status="running",
                    current_step="develop",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0
        mock_agent.project_dir = None
        mock_agent.issue = "975"
        mock_agent.output_lines = deque(["doing work", "hit an error"])
        mock_agent.process = None

        await _finalize_task_run(run_id, exit_code=1, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "failed"
                assert "Process exited with code 1" in refreshed.error_message
                assert "hit an error" in refreshed.error_message
                assert "step=develop" in refreshed.error_message

    async def test_finalize_with_no_output_produces_minimal_message(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="975",
                    role="developer",
                    status="running",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0
        mock_agent.project_dir = None
        mock_agent.issue = "975"
        mock_agent.output_lines = deque()
        mock_agent.process = None

        await _finalize_task_run(run_id, exit_code=1, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "failed"
                assert refreshed.error_message == "Process exited with code 1"


async def _seed_task_run(**overrides: object) -> int:
    """Insert a TaskRun for the finalize integration tests and return its id."""
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    defaults: dict = {"issue_number": "978", "role": "developer", "status": "running"}
    defaults.update(overrides)

    async with await get_session() as session:
        async with session.begin():
            run = TaskRun(**defaults)
            session.add(run)
            await session.flush()
            return run.id


def _make_mock_agent(process: object = None):
    from unittest.mock import MagicMock

    from sova.dashboard.services.agent_pool import AgentState

    mock_agent = MagicMock(spec=AgentState)
    mock_agent.last_result_cost = 0
    mock_agent.project_dir = None
    mock_agent.issue = "978"
    mock_agent.output_lines = deque()
    mock_agent.process = process
    return mock_agent


class TestFinalizeTaskRunTerminationReason:
    """Integration: _finalize_task_run() persists status + termination_reason (issue #978)."""

    async def test_deliberate_stop_persists_stopped_status_and_cause(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.db.models import TaskRun
        from sova.db.session import get_session
        from sova.ipc.control import TerminationRecord

        run_id = await _seed_task_run()

        process = MagicMock()
        process.termination_record = TerminationRecord(cause="manual_stop", requester="dashboard", signal=15)
        mock_agent = _make_mock_agent(process=process)

        await _finalize_task_run(run_id, exit_code=143, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "stopped"
                assert refreshed.termination_reason == "manual_stop"

    async def test_external_signal_persists_interrupted_and_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sova.dashboard.services import agent_db
        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        monkeypatch.setattr(agent_db, "_PSUTIL_AVAILABLE", False)

        run_id = await _seed_task_run()
        mock_agent = _make_mock_agent()

        # -15 is the asyncio-native form of a SIGTERM-shaped exit with no local record.
        await _finalize_task_run(run_id, exit_code=-15, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "interrupted"
                assert refreshed.termination_reason == "external_signal"

    async def test_ordinary_failure_leaves_termination_reason_null(self) -> None:
        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        run_id = await _seed_task_run()
        mock_agent = _make_mock_agent()

        await _finalize_task_run(run_id, exit_code=1, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "failed"
                assert refreshed.termination_reason is None

    async def test_stopped_run_is_not_downgraded_by_later_finalize(self) -> None:
        """A run already 'stopped' must not be reclassified by a later finalize call."""
        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        run_id = await _seed_task_run(status="stopped", termination_reason="manual_stop")
        mock_agent = _make_mock_agent()

        result = await _finalize_task_run(run_id, exit_code=-9, agent=mock_agent)

        assert result is False
        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "stopped"
                assert refreshed.termination_reason == "manual_stop"
