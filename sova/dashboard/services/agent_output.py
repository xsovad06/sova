"""Output streaming and stream-json parsing for agent processes."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.dashboard.services.agent_pool import AgentState

log = get_logger(component="dashboard.output")


def get_output(since: int = 0, slug: str | None = None, *, run_id: int | None = None) -> list[str]:
    """Get output lines since the given cursor.

    If run_id is specified, returns output for that specific agent.
    Falls back to the persisted output file when the agent is not in memory.
    Otherwise returns output for the first (legacy single-agent compat).
    """
    from sova.dashboard.services.agent_pool import _get_project_agents
    from sova.dashboard.services.output_service import read_lines

    pa = _get_project_agents(slug)

    if run_id is not None:
        agent = pa.agents.get(run_id)
        if agent is not None:
            lines = list(agent.output_lines)
            return lines[since:]
        lines, _total = read_lines(pa.project_dir, run_id, since)
        return lines

    if not pa.agents:
        return []
    first = next(iter(pa.agents.values()))
    lines = list(first.output_lines)
    return lines[since:]


async def _read_output(agent: AgentState) -> None:
    """Background task to read stdout lines into the agent's deque and output file."""
    try:
        if agent.process is None:
            return
        async for line in agent.process.stdout_lines():
            text = _parse_stream_line(line, agent)
            if text:
                agent.output_lines.append(text)
                if agent.output_writer:
                    agent.output_writer.write_line(text)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("output_reader.failed", run_id=agent.run_id)
        msg = "[ERROR] Output reader crashed -- agent may still be running"
        agent.output_lines.append(msg)
        if agent.output_writer:
            try:
                agent.output_writer.write_line(msg)
            except (OSError, ValueError):
                log.debug("output_reader.error_line_write_failed", run_id=agent.run_id, exc_info=True)


def _parse_stream_line(line: str, agent: AgentState) -> str:
    """Extract readable text from a Claude stream-json line."""
    if not line.strip():
        return ""

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
    """Background task to capture stderr lines into the agent's output deque and file."""
    try:
        if agent.process is None:
            return
        async for line in agent.process.stderr_lines():
            if line.strip():
                text = f"[stderr] {line}"
                agent.output_lines.append(text)
                if agent.output_writer:
                    agent.output_writer.write_line(text)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("stderr_reader.failed", run_id=agent.run_id)
