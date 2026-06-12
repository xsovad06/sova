"""SOVA MCP server -- provider-agnostic agent tools.

Exposes SOVA capabilities as MCP (Model Context Protocol) tools that any
compliant agent runtime can discover and invoke.
"""

from __future__ import annotations

from sova.mcp.server import create_server

__all__ = ["create_server"]
