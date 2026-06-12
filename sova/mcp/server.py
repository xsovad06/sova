"""MCP server factory for SOVA."""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from sova.mcp.tools import register_tools


def create_server(*, project_dir: Path | None = None) -> FastMCP:
    """Create and configure the SOVA MCP server.

    Args:
        project_dir: If provided, all tools are bound to this project
            directory and path-traversal protection is enabled.

    Returns:
        A FastMCP server instance with all SOVA tools registered.
    """
    server = FastMCP(
        name="sova",
        instructions=(
            "SOVA (Software Orchestration Via Agents) provides tools for "
            "autonomous software development: TDD implementation, code review, "
            "testing, and project context reading."
        ),
    )
    register_tools(server, project_dir=project_dir)
    return server
