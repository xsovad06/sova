"""MCP server configuration management for .claude/settings.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def inject_mcp_server(claude_dir: Path, server_name: str, server_config: dict[str, Any]) -> bool:
    """Add an MCP server entry to .claude/settings.json.

    Uses read-modify-write with dedup to avoid destroying user config.
    Returns True if the server was added, False if skipped (already present or error).
    """
    settings_path = claude_dir / "settings.json"

    try:
        try:
            data = json.loads(settings_path.read_text())
        except FileNotFoundError:
            data = {}
        except json.JSONDecodeError:
            logger.warning("Malformed .claude/settings.json, skipping MCP server injection")
            return False

        if not isinstance(data, dict):
            logger.warning("Malformed .claude/settings.json, skipping MCP server injection")
            return False

        mcp_servers = data.setdefault("mcpServers", {})
        if not isinstance(mcp_servers, dict):
            logger.warning("Unexpected type for mcpServers in settings.json, skipping MCP server injection")
            return False

        if server_name in mcp_servers:
            logger.debug("MCP server %s already present in settings.json", server_name)
            return False

        mcp_servers[server_name] = server_config
        settings_path.write_text(json.dumps(data, indent=2) + chr(10))
        return True
    except OSError as exc:
        logger.warning("Failed to inject MCP server %s: %s", server_name, exc, exc_info=True)
        return False


def remove_mcp_server(claude_dir: Path, server_name: str) -> bool:
    """Remove an MCP server entry from .claude/settings.json.

    Returns True if a server was removed, False if not found or error.
    """
    settings_path = claude_dir / "settings.json"

    try:
        try:
            data = json.loads(settings_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return False

        if not isinstance(data, dict):
            return False

        mcp_servers = data.get("mcpServers", {})
        if not isinstance(mcp_servers, dict):
            return False

        if server_name not in mcp_servers:
            return False

        del mcp_servers[server_name]

        if not mcp_servers:
            del data["mcpServers"]

        settings_path.write_text(json.dumps(data, indent=2) + chr(10))
        return True
    except OSError as exc:
        logger.warning("Failed to remove MCP server %s: %s", server_name, exc, exc_info=True)
        return False
