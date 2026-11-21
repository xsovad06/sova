"""agent output: step progress, output service, stream parsing and readers.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import TaskRun

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


class TestStepProgress:
    """Tests for get_step_progress pipeline variant detection."""

    @pytest.mark.parametrize(
        ("step", "kwargs", "variant", "step_index"),
        [
            pytest.param("develop", {}, "developer", 4, id="developer-pipeline-default"),
            pytest.param("rebase", {}, "address_review", 1, id="address-review-from-step"),
            pytest.param(None, {}, "developer", 0, id="none-step-defaults-to-developer"),
            # Dashboard outer TaskRun (current_step='agent') with pr_number: address_review.
            pytest.param(
                "agent",
                {"role": "developer", "pr_number": 147},
                "address_review",
                0,
                id="agent-step-with-pr-number-is-address-review",
            ),
            # WorkflowEngine TaskRun on shared step with pr_number acquired mid-pipeline.
            pytest.param(
                "commit",
                {"role": "developer", "pr_number": 147},
                "developer",
                7,
                id="shared-step-with-pr-number-is-developer",
            ),
            pytest.param("commit", {}, "developer", 7, id="shared-step-without-pr-number-is-developer"),
            pytest.param("agent", {"role": "command:review-pr"}, "command", 0, id="command-role-with-agent-step"),
        ],
    )
    async def test_variant_and_index(
        self, step: str | None, kwargs: dict[str, object], variant: str, step_index: int
    ) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = await get_step_progress(step, **kwargs)
        assert result["pipeline_variant"] == variant
        assert result["step_index"] == step_index

    @pytest.mark.parametrize(
        ("step", "kwargs", "variant", "step_index", "total_steps"),
        [
            pytest.param(
                None,
                {"role": "developer", "pr_number": 147},
                "address_review",
                0,
                13,
                id="none-step-with-pr-number-is-address-review",
            ),
            pytest.param(None, {"role": "command:integrate-pr"}, "command", 0, 1, id="command-role-is-command"),
            pytest.param(None, {"role": "reviewer"}, "command", 0, 1, id="reviewer-role-is-command"),
            pytest.param("capture_baseline", {}, "developer", 3, 18, id="capture-baseline-in-developer-pipeline"),
            pytest.param("ensure_worktree", {}, "address_review", 0, 13, id="ensure-worktree-in-address-review"),
        ],
    )
    async def test_variant_index_and_total(
        self, step: str | None, kwargs: dict[str, object], variant: str, step_index: int, total_steps: int
    ) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = await get_step_progress(step, **kwargs)
        assert result["pipeline_variant"] == variant
        assert result["step_index"] == step_index
        assert result["total_steps"] == total_steps

    async def test_configured_pipeline_overrides_builtin(self, tmp_path: Path) -> None:
        """A [pipelines] override in sova.toml drives the progress bar, not the built-in list."""
        from sova.dashboard.services import agent_progress

        (tmp_path / "sova.toml").write_text('[pipelines]\ndeveloper = ["sync", "develop", "commit", "push"]\n')
        agent_progress._pipeline_cache.clear()
        try:
            result = await agent_progress.get_step_progress("commit", role="developer", project_dir=tmp_path)
            assert result["steps"] == ["sync", "develop", "commit", "push"]
            assert result["step_index"] == 2
            assert result["total_steps"] == 4
        finally:
            agent_progress._pipeline_cache.clear()

    async def test_unreadable_config_falls_back_to_builtin_pipeline(self, tmp_path: Path) -> None:
        """A broken sova.toml must not break the run listing."""
        from sova.dashboard.services import agent_progress

        (tmp_path / "sova.toml").write_text("[pipelines\nnot valid toml")
        agent_progress._pipeline_cache.clear()
        try:
            result = await agent_progress.get_step_progress("develop", role="developer", project_dir=tmp_path)
            assert result["steps"] == agent_progress.DEVELOPER_PIPELINE
        finally:
            agent_progress._pipeline_cache.clear()

    async def test_workflow_engine_post_create_pr_is_developer(self) -> None:
        """WorkflowEngine TaskRun after CreatePRStep must not be mislabeled."""
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        for step in ("monitor_ci", "extract_memory", "handoff_to_reviewer"):
            result = await get_step_progress(step, role="developer", pr_number=147)
            assert result["pipeline_variant"] == "developer", f"step={step} should be developer"

    async def test_reviewer_role_with_pr_number_is_command(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = await get_step_progress("commit", role="reviewer", pr_number=147)
        assert result["pipeline_variant"] == "command"

    async def test_researcher_role_unaffected_by_command_check(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = await get_step_progress(None, role="researcher")
        assert result["pipeline_variant"] == "researcher"

    async def test_planner_role_returns_planner_variant(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = await get_step_progress(None, role="planner")
        assert result["pipeline_variant"] == "planner"
        assert result["total_steps"] == 4

    async def test_planner_step_detected_by_step_name(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        for step, expected_idx in (("scan_project", 0), ("generate_tasks", 1), ("validate_tasks", 2)):
            result = await get_step_progress(step)
            assert result["pipeline_variant"] == "planner", f"step={step} should be planner"
            assert result["step_index"] == expected_idx


# ---------------------------------------------------------------------------
# Output service: OutputWriter and read_lines
# ---------------------------------------------------------------------------


class TestOutputService:
    """Tests for the DB-backed output persistence layer."""

    async def _create_run(self, session: AsyncSession, run_id: int) -> None:
        """Create a TaskRun record for FK constraint."""
        async with session.begin():
            session.add(TaskRun(id=run_id, role="developer", status="running"))

    @pytest.mark.asyncio
    async def test_output_writer_write_and_read(self, session, tmp_path: Path) -> None:
        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 1)
        writer = OutputWriter(tmp_path / "fake", run_id=1)
        writer.write_line("Hello, world")
        writer.write_line("Second line")
        await writer.close()

        lines, total = await read_lines(tmp_path / "fake", run_id=1)
        assert total == 2
        assert lines == ["Hello, world", "Second line"]

    @pytest.mark.asyncio
    async def test_output_writer_read_with_offset(self, session, tmp_path: Path) -> None:
        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 2)
        writer = OutputWriter(tmp_path / "fake", run_id=2)
        writer.write_line("Line 1")
        writer.write_line("Line 2")
        writer.write_line("Line 3")
        await writer.close()

        lines, total = await read_lines(tmp_path / "fake", run_id=2, since=1)
        assert total == 3
        assert lines == ["Line 2", "Line 3"]

    @pytest.mark.asyncio
    async def test_read_lines_empty_run(self, tmp_path: Path) -> None:
        from sova.core.output import read_lines

        lines, total = await read_lines(tmp_path / "fake", run_id=999)
        assert lines == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_output_writer_strips_trailing_newlines(self, session, tmp_path: Path) -> None:
        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 3)
        writer = OutputWriter(tmp_path / "fake", run_id=3)
        writer.write_line("Line with newline\n")
        await writer.close()

        lines, total = await read_lines(tmp_path / "fake", run_id=3)
        assert total == 1
        assert lines == ["Line with newline"]

    @pytest.mark.asyncio
    async def test_flush_threshold(self, session, tmp_path: Path) -> None:
        from sova.core.output import OutputWriter

        await self._create_run(session, 4)
        writer = OutputWriter(tmp_path / "fake", run_id=4, flush_threshold=3)
        writer.write_line("A")
        writer.write_line("B")
        assert not writer.should_flush()
        writer.write_line("C")
        assert writer.should_flush()
        await writer.close()

    @pytest.mark.asyncio
    async def test_closed_writer_ignores_writes(self, session, tmp_path: Path) -> None:
        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 5)
        writer = OutputWriter(tmp_path / "fake", run_id=5)
        writer.write_line("Before close")
        await writer.close()
        writer.write_line("After close")

        lines, total = await read_lines(tmp_path / "fake", run_id=5)
        assert total == 1
        assert lines == ["Before close"]

    @pytest.mark.asyncio
    async def test_cleanup_old_output(self, session, tmp_path: Path) -> None:
        from sova.core.output import OutputWriter, cleanup_old_output, read_lines

        async with session.begin():
            session.add(
                TaskRun(
                    id=100,
                    role="developer",
                    status="done",
                    ended_at=datetime.now(timezone.utc) - timedelta(days=60),
                )
            )

        writer = OutputWriter(tmp_path / "fake", run_id=100)
        writer.write_line("old output")
        await writer.close()

        deleted = await cleanup_old_output(tmp_path / "fake", retention_days=30)
        assert deleted == 1

        lines, total = await read_lines(tmp_path / "fake", run_id=100)
        assert total == 0

    @pytest.mark.asyncio
    async def test_cleanup_preserves_recent_output(self, session, tmp_path: Path) -> None:
        from sova.core.output import OutputWriter, cleanup_old_output, read_lines

        async with session.begin():
            session.add(
                TaskRun(
                    id=101,
                    role="developer",
                    status="done",
                    ended_at=datetime.now(timezone.utc) - timedelta(days=5),
                )
            )

        writer = OutputWriter(tmp_path / "fake", run_id=101)
        writer.write_line("recent output")
        await writer.close()

        deleted = await cleanup_old_output(tmp_path / "fake", retention_days=30)
        assert deleted == 0

        lines, total = await read_lines(tmp_path / "fake", run_id=101)
        assert total == 1

    @pytest.mark.asyncio
    async def test_legacy_file_read(self, tmp_path) -> None:
        from sova.core.output import read_lines_from_file

        log_file = tmp_path / "42.log"
        log_file.write_text("line one\nline two\nline three\n")

        lines, total = read_lines_from_file(log_file)
        assert total == 3
        assert lines == ["line one", "line two", "line three"]

        lines, total = read_lines_from_file(log_file, since=1)
        assert total == 3
        assert lines == ["line two", "line three"]

    @pytest.mark.asyncio
    async def test_legacy_file_read_missing(self, tmp_path) -> None:
        from sova.core.output import read_lines_from_file

        lines, total = read_lines_from_file(tmp_path / "nonexistent.log")
        assert lines == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_readopted_run_seeds_line_number(self, session, tmp_path: Path) -> None:
        """A new OutputWriter on a run with existing output continues numbering."""
        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 200)
        # First writer writes 3 lines
        w1 = OutputWriter(tmp_path / "fake", run_id=200)
        w1.write_line("Line 0")
        w1.write_line("Line 1")
        w1.write_line("Line 2")
        await w1.close()

        # Second writer (re-adoption) should continue from line 3
        w2 = OutputWriter(tmp_path / "fake", run_id=200)
        w2.write_line("Line 3")
        w2.write_line("Line 4")
        await w2.close()

        lines, total = await read_lines(tmp_path / "fake", run_id=200)
        assert total == 5
        assert lines == ["Line 0", "Line 1", "Line 2", "Line 3", "Line 4"]

    @pytest.mark.asyncio
    async def test_double_close_is_noop(self, session, tmp_path: Path) -> None:
        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 6)
        writer = OutputWriter(tmp_path / "fake", run_id=6)
        writer.write_line("data")
        await writer.close()
        await writer.close()  # second close is a no-op

        lines, total = await read_lines(tmp_path / "fake", run_id=6)
        assert total == 1
        assert lines == ["data"]

    @pytest.mark.asyncio
    async def test_flush_failure_recovery(self, session, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 7)
        writer = OutputWriter(tmp_path / "fake", run_id=7, flush_threshold=2)
        writer.write_line("line1")
        writer.write_line("line2")

        # First flush fails: lines should be preserved in buffer
        with patch("sova.core.output.get_session", side_effect=Exception("db down")):
            await writer.flush()

        assert len(writer._buffer) == 2, "failed flush should restore buffer"

        # Second flush succeeds: lines reach DB
        await writer.close()
        lines, total = await read_lines(tmp_path / "fake", run_id=7)
        assert total == 2
        assert lines == ["line1", "line2"]

    @pytest.mark.asyncio
    async def test_cleanup_old_output_exception(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.core.output import cleanup_old_output

        with patch("sova.core.output.get_session", side_effect=Exception("db error")):
            deleted = await cleanup_old_output(tmp_path / "fake", retention_days=30)
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_read_lines_exception(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.core.output import read_lines

        with patch("sova.core.output.get_session", side_effect=Exception("db error")):
            lines, total = await read_lines(tmp_path / "fake", run_id=999)
        assert lines == []
        assert total == 0

    def test_legacy_file_read_oserror(self, tmp_path) -> None:
        from unittest.mock import patch

        from sova.core.output import read_lines_from_file

        log_file = tmp_path / "broken.log"
        log_file.write_text("data")
        with patch("builtins.open", side_effect=OSError("permission denied")):
            lines, total = read_lines_from_file(log_file)
        assert lines == []
        assert total == 0


# ---------------------------------------------------------------------------
# Agent Output: stream-json parsing
# ---------------------------------------------------------------------------


class TestParseStreamLine:
    """Tests for _parse_stream_line in agent_output.py."""

    def _agent(self):
        from unittest.mock import MagicMock

        a = MagicMock()
        a.last_result_cost = None
        a.stream_parser = None
        return a

    def test_empty_line(self) -> None:
        from sova.dashboard.services.agent_output import _parse_stream_line

        assert _parse_stream_line("", self._agent()) == ""
        assert _parse_stream_line("   ", self._agent()) == ""

    def test_plain_text_passthrough(self) -> None:
        from sova.dashboard.services.agent_output import _parse_stream_line

        assert _parse_stream_line("not json", self._agent()) == "not json"

    def test_assistant_message(self) -> None:
        import json

        from sova.dashboard.services.agent_output import _parse_stream_line

        line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello"}]},
            }
        )
        assert _parse_stream_line(line, self._agent()) == "Hello"

    def test_content_block_delta(self) -> None:
        import json

        from sova.dashboard.services.agent_output import _parse_stream_line

        line = json.dumps(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "world"},
            }
        )
        assert _parse_stream_line(line, self._agent()) == "world"

    def test_result_captures_cost(self) -> None:
        import json

        from sova.dashboard.services.agent_output import _parse_stream_line

        agent = self._agent()
        line = json.dumps({"type": "result", "total_cost_usd": 1.23})
        result = _parse_stream_line(line, agent)
        assert "$1.23" in result
        assert agent.last_result_cost == 1.23

    def test_unknown_type_returns_empty(self) -> None:
        import json

        from sova.dashboard.services.agent_output import _parse_stream_line

        line = json.dumps({"type": "system", "data": {}})
        assert _parse_stream_line(line, self._agent()) == ""


class TestParseStreamLineRuntimeParser:
    """Tests for the agent.stream_parser delegation path in _parse_stream_line (Codex, etc.)."""

    def _agent(self, stream_parser):
        from unittest.mock import MagicMock

        a = MagicMock()
        a.last_result_cost = None
        a.stream_parser = stream_parser
        a.runtime_session_id = None
        a.runtime_model = None
        a.stream_failure = None
        return a

    def test_empty_line_never_reaches_stream_parser(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_output import _parse_stream_line

        parser = MagicMock()
        assert _parse_stream_line("", self._agent(parser)) == ""
        parser.parse_line.assert_not_called()

    def test_content_event_returns_text(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_output import _parse_stream_line
        from sova.llm.models import StreamEvent

        parser = MagicMock()
        parser.parse_line.return_value = StreamEvent(type="content", text="hello from codex")
        agent = self._agent(parser)

        assert _parse_stream_line("some line", agent) == "hello from codex"
        parser.parse_line.assert_called_once_with("some line")

    def test_none_event_returns_empty(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_output import _parse_stream_line

        parser = MagicMock()
        parser.parse_line.return_value = None

        assert _parse_stream_line("no-op line", self._agent(parser)) == ""

    def test_successful_result_event_records_session_id(self) -> None:
        from decimal import Decimal
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_output import _parse_stream_line
        from sova.llm.models import CostSource, LLMResult, StreamEvent

        parser = MagicMock()
        result = LLMResult(
            text="final message",
            model="",
            cost_usd=Decimal("0"),
            cost_source=CostSource.UNKNOWN,
            session_id="thread-abc",
            stop_reason="end_turn",
        )
        parser.parse_line.return_value = StreamEvent(type="result", text=result.text, result=result)
        agent = self._agent(parser)

        out = _parse_stream_line("turn.completed line", agent)

        assert agent.runtime_session_id == "thread-abc"
        assert agent.stream_failure is None
        # Marks the end of the turn without echoing the last agent message,
        # which was already emitted as its own content event.
        assert out == "\n--- Result [tokens: 0 in / 0 out] ---"
        assert "final message" not in out

    def test_terminal_failure_sets_stream_failure(self) -> None:
        from decimal import Decimal
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_output import _parse_stream_line
        from sova.llm.models import CostSource, LLMResult, StreamEvent

        parser = MagicMock()
        result = LLMResult(
            text="sandbox denied write access",
            model="",
            cost_usd=Decimal("0"),
            cost_source=CostSource.UNKNOWN,
            session_id="thread-xyz",
            stop_reason="error",
        )
        parser.parse_line.return_value = StreamEvent(type="result", text=result.text, result=result)
        agent = self._agent(parser)

        out = _parse_stream_line("turn.failed line", agent)

        assert agent.stream_failure == "sandbox denied write access"
        assert agent.runtime_session_id == "thread-xyz"
        assert "sandbox denied write access" in out

    def test_successful_result_marker_reports_token_usage(self) -> None:
        from decimal import Decimal
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_output import _parse_stream_line
        from sova.llm.models import CostSource, LLMResult, StreamEvent

        parser = MagicMock()
        result = LLMResult(
            text="done",
            model="",
            cost_usd=Decimal("0"),
            cost_source=CostSource.UNKNOWN,
            input_tokens=1200,
            output_tokens=340,
            stop_reason="end_turn",
        )
        parser.parse_line.return_value = StreamEvent(type="result", text=result.text, result=result)

        out = _parse_stream_line("turn.completed line", self._agent(parser))

        assert out == "\n--- Result [tokens: 1200 in / 340 out] ---"

    def test_thread_never_started_leaves_session_id_unset(self) -> None:
        """No thread.started event ever arrived: session_id stays empty and is not recorded."""
        from decimal import Decimal
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_output import _parse_stream_line
        from sova.llm.models import CostSource, LLMResult, StreamEvent

        parser = MagicMock()
        result = LLMResult(
            text="done",
            model="",
            cost_usd=Decimal("0"),
            cost_source=CostSource.UNKNOWN,
            session_id="",
            stop_reason="end_turn",
        )
        parser.parse_line.return_value = StreamEvent(type="result", text=result.text, result=result)
        agent = self._agent(parser)

        _parse_stream_line("turn.completed line", agent)

        assert agent.runtime_session_id is None

    def test_successful_result_event_records_model(self) -> None:
        """The runtime-reported model must reach agent.runtime_model, so a later
        cost record can attribute spend to the model that actually ran instead of
        hardcoding the Claude Code default (issue #1128 review)."""
        from decimal import Decimal
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_output import _parse_stream_line
        from sova.llm.models import CostSource, LLMResult, StreamEvent

        parser = MagicMock()
        result = LLMResult(
            text="done",
            model="gpt-5-codex",
            cost_usd=Decimal("0"),
            cost_source=CostSource.UNKNOWN,
            stop_reason="end_turn",
        )
        parser.parse_line.return_value = StreamEvent(type="result", text=result.text, result=result)
        agent = self._agent(parser)

        _parse_stream_line("turn.completed line", agent)

        assert agent.runtime_model == "gpt-5-codex"

    def test_model_never_reported_leaves_runtime_model_unset(self) -> None:
        """No model field in the stream at all: runtime_model stays unset rather
        than being overwritten with an empty string."""
        from decimal import Decimal
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_output import _parse_stream_line
        from sova.llm.models import CostSource, LLMResult, StreamEvent

        parser = MagicMock()
        result = LLMResult(
            text="done",
            model="",
            cost_usd=Decimal("0"),
            cost_source=CostSource.UNKNOWN,
            stop_reason="end_turn",
        )
        parser.parse_line.return_value = StreamEvent(type="result", text=result.text, result=result)
        agent = self._agent(parser)

        _parse_stream_line("turn.completed line", agent)

        assert agent.runtime_model is None

    def test_multiple_turns_accumulate_token_usage(self) -> None:
        """A thread that runs several turns reports each turn's own usage, not a
        thread-cumulative total (CodexStreamParser._reset_for_turn lifts the terminal
        latch for exactly this reason), so a second turn.completed must add to the
        running total rather than overwrite the first turn's usage (issue #1128 review)."""
        from sova.dashboard.services.agent_output import _parse_stream_line
        from sova.dashboard.services.agent_pool import AgentState
        from sova.ipc.codex import CodexStreamParser

        parser = CodexStreamParser(model="")
        agent = AgentState(run_id=1, issue="700", role="developer", process=None, stream_parser=parser)

        _parse_stream_line('{"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}', agent)
        _parse_stream_line('{"type": "turn.started"}', agent)
        _parse_stream_line('{"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}', agent)

        assert agent.last_result_input_tokens == 17
        assert agent.last_result_output_tokens == 8


# ---------------------------------------------------------------------------
# Output reader resilience
# ---------------------------------------------------------------------------


class TestReadOutputResilience:
    """_read_output and _read_stderr must log and surface errors instead of dying silently."""

    async def test_read_output_logs_exception_and_writes_error_line(self) -> None:
        """When stdout_lines raises, the error should be logged and written to the output file."""
        from collections import deque
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_output import _read_output
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = AsyncMock()

        async def exploding_lines():
            yield '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}'
            raise RuntimeError("pipe broken")

        mock_process.stdout_lines = exploding_lines

        mock_writer = MagicMock()
        mock_writer.should_flush.return_value = False
        mock_writer.flush = AsyncMock()
        agent = AgentState(
            run_id=1,
            issue="10",
            role="developer",
            process=mock_process,
            output_writer=mock_writer,
            output_lines=deque(maxlen=5000),
        )

        with patch("sova.dashboard.services.agent_output.log") as mock_log:
            await _read_output(agent)

        assert len(agent.output_lines) >= 1
        assert "hello" in agent.output_lines[0]
        assert any("output reader crashed" in line.lower() for line in agent.output_lines)
        mock_log.exception.assert_called_once()
        error_writes = [call for call in mock_writer.write_line.call_args_list if "output reader" in str(call).lower()]
        assert len(error_writes) == 1

    async def test_read_stderr_logs_exception(self) -> None:
        """When stderr_lines raises, the error should be logged."""
        from collections import deque
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_output import _read_stderr
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = AsyncMock()

        async def exploding_stderr():
            yield "some warning"
            raise RuntimeError("stderr pipe broken")

        mock_process.stderr_lines = exploding_stderr

        mock_writer = MagicMock()
        mock_writer.should_flush.return_value = False
        mock_writer.flush = AsyncMock()
        agent = AgentState(
            run_id=2,
            issue="11",
            role="developer",
            process=mock_process,
            output_writer=mock_writer,
            output_lines=deque(maxlen=5000),
        )

        with patch("sova.dashboard.services.agent_output.log") as mock_log:
            await _read_stderr(agent)

        mock_log.exception.assert_called_once()

    async def test_read_output_reraises_cancelled_error(self) -> None:
        """CancelledError must still propagate (not be caught by the general handler)."""
        import asyncio
        from collections import deque
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_output import _read_output
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = AsyncMock()

        async def cancelled_lines():
            raise asyncio.CancelledError()
            yield  # noqa: RET503

        mock_process.stdout_lines = cancelled_lines

        agent = AgentState(
            run_id=3,
            issue="12",
            role="developer",
            process=mock_process,
            output_lines=deque(maxlen=5000),
        )

        with pytest.raises(asyncio.CancelledError):
            await _read_output(agent)


class TestOutputReaderWriteLineFallback:
    """Cover the write_line error path in the output reader crash handler."""

    async def test_write_line_oserror_is_caught(self) -> None:
        """When write_line raises OSError during error handling, it should be caught."""
        from collections import deque
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_output import _read_output
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = AsyncMock()

        async def exploding_stdout():
            yield "line1"
            raise RuntimeError("pipe broken")

        mock_process.stdout_lines = exploding_stdout

        mock_writer = MagicMock()
        mock_writer.should_flush.return_value = False
        mock_writer.flush = AsyncMock()
        # First write_line call succeeds (for "line1"), second fails (for error message)
        mock_writer.write_line.side_effect = [None, OSError("disk full")]

        agent = AgentState(
            run_id=70,
            issue="300",
            role="developer",
            process=mock_process,
            output_writer=mock_writer,
            output_lines=deque(maxlen=5000),
        )

        with patch("sova.dashboard.services.agent_output.log") as mock_log:
            await _read_output(agent)

        # Error message should still be in the in-memory deque
        assert any("output reader crashed" in line.lower() for line in agent.output_lines)
        mock_log.exception.assert_called_once()
        mock_log.debug.assert_called_once()


class TestGetOutputBranches:
    """Cover get_output fallback branches in agent_output.py."""

    async def test_get_output_in_memory_agent(self) -> None:
        """When run_id matches an in-memory agent, return from deque."""
        from collections import deque
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_output import get_output

        mock_agent = MagicMock()
        mock_agent.output_lines = deque(["line0", "line1", "line2"])

        mock_pa = MagicMock()
        mock_pa.agents = {42: mock_agent}

        with patch("sova.dashboard.services.agent_pool._get_project_agents", return_value=mock_pa):
            lines = await get_output(since=1, run_id=42)
        assert lines == ["line1", "line2"]

    async def test_get_output_db_fallback(self, session, tmp_path: Path) -> None:
        """When run_id is not in memory, fall back to DB read."""
        from unittest.mock import MagicMock, patch

        from sova.core.output import OutputWriter
        from sova.dashboard.services.agent_output import get_output
        from sova.db.models import TaskRun

        async with session.begin():
            session.add(TaskRun(id=200, role="developer", status="done"))

        writer = OutputWriter(tmp_path / "fake", run_id=200)
        writer.write_line("db line")
        await writer.close()

        mock_pa = MagicMock()
        mock_pa.agents = {}
        mock_pa.project_dir = tmp_path / "fake"

        with patch("sova.dashboard.services.agent_pool._get_project_agents", return_value=mock_pa):
            lines = await get_output(since=0, run_id=200)
        assert lines == ["db line"]


class TestReadStderrFlushTrigger:
    """Cover _read_stderr flush threshold path."""

    async def test_stderr_triggers_flush(self) -> None:
        from collections import deque
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_output import _read_stderr
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = AsyncMock()

        async def stderr_lines():
            yield "warning line"

        mock_process.stderr_lines = stderr_lines

        mock_writer = MagicMock()
        mock_writer.should_flush.return_value = True
        mock_writer.flush = AsyncMock()
        agent = AgentState(
            run_id=80,
            issue="400",
            role="developer",
            process=mock_process,
            output_writer=mock_writer,
            output_lines=deque(maxlen=5000),
        )

        await _read_stderr(agent)

        mock_writer.write_line.assert_called_once()
        mock_writer.flush.assert_awaited_once()
        assert any("stderr" in line for line in agent.output_lines)
