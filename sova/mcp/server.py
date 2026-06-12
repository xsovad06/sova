"""MCP server factory for SOVA."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from sova.mcp.tools import register_tools


def create_server() -> FastMCP:
    """Create and configure the SOVA MCP server.

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
    register_tools(server)
    return server
