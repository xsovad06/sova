"""MCP server factory for SOVA."""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer

from sova.mcp.operator import register_operator_tools
from sova.mcp.tools import register_tools


def create_server(*, project_dir: Path | None = None) -> MCPServer:
    """Create and configure the SOVA MCP server.

    Args:
        project_dir: If provided, all tools are bound to this project
            directory and path-traversal protection is enabled.

    Returns:
        An MCPServer instance with all SOVA tools registered.
    """
    server = MCPServer(
        name="sova",
        instructions=(
            "SOVA (Software Orchestration Via Agents) provides tools for "
            "autonomous software development: TDD implementation, code review, "
            "testing, and project context reading. The operator tools (sova_status, "
            "sova_pending, sova_start_run, sova_stop_run, sova_resume_run, "
            "sova_handoff_dismiss, sova_handoff_action, sova_plan_decision) drive "
            "the running 'sova server' instead: what runs, what waits on a human, "
            "and starting, stopping or approving work through its own checks."
        ),
    )
    register_tools(server, project_dir=project_dir)
    register_operator_tools(server, project_dir=project_dir)
    return server
