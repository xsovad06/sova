"""MCP server configuration management for .mcp.json and .claude/settings.json.

The Claude Code CLI reads project-scope MCP servers from ``<project>/.mcp.json``
(an ``mcpServers`` map); an ``mcpServers`` key inside ``.claude/settings.json``
is ignored.  Approval of ``.mcp.json`` servers is persisted in settings.json's
``enabledMcpjsonServers`` list, which headless agents need since they never see
the interactive approval prompt.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sova.config.models import AtlassianMCPConfig

logger = logging.getLogger(__name__)

MCP_JSON_FILENAME = ".mcp.json"
ATLASSIAN_MCP_NAME = "atlassian-mcp"
ATLASSIAN_TOKEN_ENV = "SOVA_MCP_ATLASSIAN_TOKEN"


def mcp_json_path(project_dir: Path) -> Path:
    """Path of the project-scope MCP server file the Claude CLI reads."""
    return project_dir / MCP_JSON_FILENAME


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    """Read a JSON object. Returns {} when the file is absent, None when unusable."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        logger.warning("Malformed %s, skipping MCP server update", path.name)
        return None
    except OSError as exc:
        logger.warning("Failed to read %s: %s", path.name, exc, exc_info=True)
        return None

    if not isinstance(data, dict):
        logger.warning("Unexpected top-level type in %s, skipping MCP server update", path.name)
        return None
    return data


def _write_json_dict(path: Path, data: dict[str, Any], *, private: bool = False) -> bool:
    """Write a JSON object, optionally restricting the file to owner-only (0600)."""
    try:
        path.write_text(json.dumps(data, indent=2) + chr(10))
        if private:
            path.chmod(0o600)
        return True
    except OSError as exc:
        logger.warning("Failed to write %s: %s", path.name, exc, exc_info=True)
        return False


def _servers_map(data: dict[str, Any], path: Path) -> dict[str, Any] | None:
    """Return the ``mcpServers`` map, creating it when missing. None when malformed."""
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        logger.warning("Unexpected type for mcpServers in %s, skipping MCP server update", path.name)
        return None
    return servers


def inject_mcp_server(
    project_dir: Path,
    server_name: str,
    server_config: dict[str, Any],
    *,
    private: bool = False,
) -> bool:
    """Add or update a server entry in ``<project_dir>/.mcp.json``.

    Uses read-modify-write so unrelated user entries survive.  An entry whose
    config already matches is left alone; a changed config (rotated token, new
    URL, different toolsets) is overwritten, so re-running ``sova install``
    after a settings change actually propagates it.

    Set ``private`` when the config embeds a credential: the file is then
    restricted to owner read/write.

    Returns True when the file was changed, False when already current or on error.
    """
    path = mcp_json_path(project_dir)

    data = _read_json_dict(path)
    if data is None:
        return False

    servers = _servers_map(data, path)
    if servers is None:
        return False

    if servers.get(server_name) == server_config:
        logger.debug("MCP server %s already current in %s", server_name, path.name)
        return False

    servers[server_name] = server_config
    return _write_json_dict(path, data, private=private)


def remove_mcp_server(project_dir: Path, server_name: str) -> bool:
    """Remove a server entry from ``<project_dir>/.mcp.json``.

    Deletes the file when it is left with no servers and no other keys, so an
    uninstall does not leave an empty ``.mcp.json`` behind.

    Returns True if a server was removed, False if not found or error.
    """
    path = mcp_json_path(project_dir)
    return _remove_server_entry(path, server_name, delete_when_empty=True)


def remove_settings_mcp_server(claude_dir: Path, server_name: str) -> bool:
    """Remove a legacy ``mcpServers`` entry from ``.claude/settings.json``.

    SOVA wrote MCP servers there before they were moved to ``.mcp.json`` (where
    the Claude CLI actually reads them); this cleans up those stale entries.

    Returns True if a server was removed, False if not found or error.
    """
    return _remove_server_entry(claude_dir / "settings.json", server_name, delete_when_empty=False)


def _remove_server_entry(path: Path, server_name: str, *, delete_when_empty: bool) -> bool:
    """Delete ``server_name`` from the ``mcpServers`` map of a JSON config file."""
    if not path.exists():
        return False

    data = _read_json_dict(path)
    if not data:
        return False

    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict) or server_name not in servers:
        return False

    del servers[server_name]

    if not servers:
        del data["mcpServers"]
        if delete_when_empty and not data:
            try:
                path.unlink()
                return True
            except OSError as exc:
                logger.warning("Failed to remove %s: %s", path.name, exc, exc_info=True)
                return False

    return _write_json_dict(path, data)


def set_project_mcp_approval(claude_dir: Path, server_name: str, *, approved: bool) -> bool:
    """Add or drop ``server_name`` in settings.json's ``enabledMcpjsonServers``.

    Servers declared in ``.mcp.json`` stay pending until approved, and a headless
    agent never sees the approval prompt, so the approval has to be persisted in
    settings.json for the sidecar to be usable by agents.

    Returns True when settings.json was changed, False when already correct or on error.
    """
    settings_path = claude_dir / "settings.json"

    data = _read_json_dict(settings_path)
    if data is None:
        return False

    enabled = data.get("enabledMcpjsonServers", [])
    if not isinstance(enabled, list):
        logger.warning("Unexpected type for enabledMcpjsonServers, skipping MCP approval update")
        return False

    if approved == (server_name in enabled):
        return False

    if approved:
        enabled = [*enabled, server_name]
    else:
        enabled = [name for name in enabled if name != server_name]

    if enabled:
        data["enabledMcpjsonServers"] = enabled
    else:
        data.pop("enabledMcpjsonServers", None)

    return _write_json_dict(settings_path, data)


def atlassian_token_inlined(cfg: AtlassianMCPConfig) -> bool:
    """Whether the built config embeds the literal token rather than a placeholder.

    A token that came from ``SOVA_MCP_ATLASSIAN_TOKEN`` is already in the
    environment, so it is referenced rather than copied onto disk.  ``cfg.token``
    resolves from that same env var (Pydantic Settings prefix), which is why the
    two are compared instead of just checking whether the config value is set.
    """
    if not cfg.token:
        return False
    return cfg.token != os.environ.get(ATLASSIAN_TOKEN_ENV)


def build_atlassian_mcp_server_config(cfg: AtlassianMCPConfig) -> dict[str, Any]:
    """Build the ``.mcp.json`` entry for the mcp-atlassian sidecar (Confluence + Jira).

    Credentials are passed as env vars for the child process, not baked into the
    command line. Only the URLs the caller configured get their matching auth
    env vars, since mcp-atlassian treats an unset URL as "product disabled".

    When the token is unset or supplied through the environment, a
    ``${SOVA_MCP_ATLASSIAN_TOKEN}`` placeholder is emitted instead of a literal:
    the Claude CLI expands it at launch, keeping the secret out of the on-disk
    config entirely.
    """
    token = cfg.token if atlassian_token_inlined(cfg) else f"${{{ATLASSIAN_TOKEN_ENV}}}"
    env: dict[str, str] = {"READ_ONLY_MODE": "true" if cfg.read_only else "false"}

    for product, url in (("JIRA", cfg.jira_url), ("CONFLUENCE", cfg.confluence_url)):
        if not url:
            continue
        env[f"{product}_URL"] = url
        if cfg.auth_type == "pat":
            env[f"{product}_PERSONAL_TOKEN"] = token
        else:
            env[f"{product}_USERNAME"] = cfg.email
            env[f"{product}_API_TOKEN"] = token

    if cfg.toolsets:
        env["TOOLSETS"] = ",".join(cfg.toolsets)

    return {"command": "uvx", "args": ["mcp-atlassian"], "env": env}


def atlassian_config_problems(cfg: AtlassianMCPConfig) -> list[str]:
    """Return human-readable reasons the Atlassian sidecar cannot be configured.

    An empty list means the config is complete enough to launch.  Writing an
    entry that is missing a URL or credentials produces a sidecar that fails to
    start with no SOVA-side signal, so the install path checks first.
    """
    problems: list[str] = []

    if not cfg.jira_url and not cfg.confluence_url:
        problems.append("set mcp.atlassian.jira_url and/or mcp.atlassian.confluence_url")
    if not cfg.token and not os.environ.get(ATLASSIAN_TOKEN_ENV):
        problems.append(f"set mcp.atlassian.token or export {ATLASSIAN_TOKEN_ENV}")
    if cfg.auth_type == "api_token" and not cfg.email:
        problems.append("set mcp.atlassian.email (required when auth_type is api_token)")

    return problems
