"""Output streaming and stream-json parsing for agent processes."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from sova.dashboard.services.output_stream_service import get_output_stream_service
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.dashboard.services.agent_pool import AgentState
    from sova.ipc.runtime import StreamParser

log = get_logger(component="dashboard.output")


async def get_output(since: int = 0, slug: str | None = None, *, run_id: int | None = None) -> list[str]:
    """Get output lines since the given cursor.

    If *run_id* is specified, returns output for that specific agent.
    Falls back to the DB-persisted output when the agent is not in memory,
    then to a legacy log file for pre-migration runs.
    Otherwise returns output for the first (legacy single-agent compat).
    """
    from sova.dashboard.services.agent_pool import _get_project_agents
    from sova.dashboard.services.output_service import read_lines, read_lines_from_file

    pa = _get_project_agents(slug)

    if run_id is not None:
        agent = pa.agents.get(run_id)
        if agent is not None:
            lines = list(agent.output_lines)
            return lines[since:]
        lines, total = await read_lines(pa.project_dir, run_id, since)
        if lines or total > 0:
            return lines
        file_output_path = pa.project_dir / ".claude" / "agent-output" / f"{run_id}.stdout"
        lines, _total = read_lines_from_file(file_output_path, since)
        if lines:
            return lines
        legacy_path = pa.project_dir / ".claude" / "agent-output" / f"{run_id}.log"
        lines, _total = read_lines_from_file(legacy_path, since)
        return lines

    if not pa.agents:
        return []
    first = next(iter(pa.agents.values()))
    lines = list(first.output_lines)
    return lines[since:]


async def _buffer_line(agent: AgentState, text: str) -> None:
    """Append a line to the agent's deque, persist to DB, and push to SSE subscribers."""
    agent.output_lines.append(text)
    if agent.output_writer:
        agent.output_writer.write_line(text)
        if agent.output_writer.should_flush():
            await agent.output_writer.flush()

    oss = get_output_stream_service()
    if oss.has_subscribers(agent.run_id):
        oss.publish(agent.run_id, text)


async def _read_output(agent: AgentState) -> None:
    """Background task to read stdout lines into the agent's deque and DB."""
    try:
        if agent.process is None:
            return
        async for line in agent.process.stdout_lines():
            text = _parse_stream_line(line, agent)
            if text:
                await _buffer_line(agent, text)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 (reader task must report and exit cleanly, never crash the event loop)
        log.exception("output_reader.failed", run_id=agent.run_id)
        msg = "[ERROR] Output reader crashed -- agent may still be running"
        try:
            await _buffer_line(agent, msg)
        except (OSError, RuntimeError, SQLAlchemyError):
            log.debug("output_reader.error_line_write_failed", run_id=agent.run_id, exc_info=True)


def _parse_runtime_stream_line(line: str, agent: AgentState, parser: StreamParser) -> str:
    """Extract readable text from a line via a per-process runtime stream parser.

    Used for runtimes (currently only Codex) that expose a stateful
    ``create_stream_parser()`` result on ``agent.stream_parser``, so their
    own event format never needs to be understood here. The parser is passed
    in rather than read off *agent* so this function never has to assume the
    caller already proved it is set. A terminal failure result sets
    ``agent.stream_failure`` rather than raising, so ``_wait_and_finalize()``
    can downgrade even a zero exit code.

    Terminal-event deduplication (so a duplicate ``turn.completed``/
    ``turn.failed`` never surfaces twice) is enforced entirely inside
    ``CodexStreamParser.parse_line()``'s own internal latch; this function
    has no ``terminal_result`` attribute to consult and only ever reads the
    ``stop_reason`` of the single ``StreamEvent`` returned for this line.
    """
    event = parser.parse_line(line)
    if event is None:
        return ""
    if event.type != "result" or event.result is None:
        return event.text or ""

    result = event.result
    if result.session_id:
        agent.runtime_session_id = result.session_id
    if result.stop_reason == "error":
        agent.stream_failure = result.text or "runtime reported a terminal failure"
        return f"\n--- Result [error]: {agent.stream_failure} ---"

    # ``result.text`` is the last agent message, already emitted as its own
    # content event, so echoing it would duplicate. A token summary marks the
    # end of the turn in the log viewer the way the Claude path's cost marker
    # does, without repeating content.
    return f"\n--- Result [tokens: {result.input_tokens} in / {result.output_tokens} out] ---"


def _parse_stream_line(line: str, agent: AgentState) -> str:
    """Extract readable text from an agent output line.

    Delegates to the agent's own per-process runtime parser when it has one
    (Codex); otherwise parses Claude Code's stream-json format inline.
    """
    if not line.strip():
        return ""

    if agent.stream_parser is not None:
        return _parse_runtime_stream_line(line, agent, agent.stream_parser)

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return line

    msg_type = data.get("type", "")

    if msg_type == "assistant":
        content = data.get("message", {}).get("content", [])
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)

    if msg_type == "content_block_delta":
        delta = data.get("delta", {})
        if delta.get("type") == "text_delta":
            return delta.get("text", "")

    if msg_type == "result":
        cost = data.get("total_cost_usd")
        if cost:
            agent.last_result_cost = float(cost)
            return f"\n--- Result [cost: ${cost}] ---"

    return ""


async def _read_stderr(agent: AgentState) -> None:
    """Background task to capture stderr lines into the agent's output deque and DB."""
    try:
        if agent.process is None:
            return
        async for line in agent.process.stderr_lines():
            if line.strip():
                await _buffer_line(agent, f"[stderr] {line}")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 (reader task must report and exit cleanly, never crash the event loop)
        log.exception("stderr_reader.failed", run_id=agent.run_id)
