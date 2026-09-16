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

        await _finalize_task_run(run_id, exit_code=1, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "failed"
                assert refreshed.error_message == "Process exited with code 1"
