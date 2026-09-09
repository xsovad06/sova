"""Tests for MCP server configuration management."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sova.config.models import AtlassianMCPConfig
from sova.utils.mcp_config import (
    ATLASSIAN_TOKEN_ENV,
    atlassian_config_problems,
    atlassian_token_inlined,
    build_atlassian_mcp_server_config,
    inject_mcp_server,
    remove_mcp_server,
    remove_settings_mcp_server,
    set_project_mcp_approval,
)

_TEST_SERVER = {"command": "npx", "args": ["-y", "@test/mcp@latest"]}


def test_inject_creates_mcp_json_file(tmp_path: Path) -> None:
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is True
    settings = json.loads((tmp_path / ".mcp.json").read_text())
    assert settings["mcpServers"]["test-mcp"] == _TEST_SERVER


def test_inject_merges_into_existing(tmp_path: Path) -> None:
    existing = {"permissions": {"allow": ["Read"]}, "mcpServers": {"other": {"command": "x"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(existing))
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is True
    settings = json.loads((tmp_path / ".mcp.json").read_text())
    assert settings["permissions"] == {"allow": ["Read"]}
    assert "other" in settings["mcpServers"]
    assert settings["mcpServers"]["test-mcp"] == _TEST_SERVER


def test_inject_idempotent(tmp_path: Path) -> None:
    existing = {"mcpServers": {"test-mcp": _TEST_SERVER}}
    (tmp_path / ".mcp.json").write_text(json.dumps(existing))
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is False


def test_inject_updates_changed_config(tmp_path: Path) -> None:
    """A changed config is overwritten so re-install propagates rotated credentials."""
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"test-mcp": _TEST_SERVER}}))
    updated = {"command": "npx", "args": ["-y", "@test/mcp@latest"], "env": {"TOKEN": "rotated"}}
    result = inject_mcp_server(tmp_path, "test-mcp", updated)
    assert result is True
    servers = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    assert servers["test-mcp"] == updated


def test_inject_private_restricts_permissions(tmp_path: Path) -> None:
    """A credential-bearing entry leaves the file readable by its owner only."""
    assert inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER, private=True) is True
    assert (tmp_path / ".mcp.json").stat().st_mode & 0o777 == 0o600


def test_inject_skips_malformed_json(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{broken json!!!")
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is False


def test_inject_adds_mcpservers_key(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(json.dumps({"permissions": {}}))
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is True
    settings = json.loads((tmp_path / ".mcp.json").read_text())
    assert settings["mcpServers"]["test-mcp"] == _TEST_SERVER


def test_inject_skips_non_dict_json(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("[1, 2, 3]")
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is False


def test_inject_skips_non_dict_mcpservers(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": "bad"}))
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is False


def test_inject_handles_oserror(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{}")
    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is False


def test_remove_when_present(tmp_path: Path) -> None:
    existing = {"mcpServers": {"test-mcp": _TEST_SERVER, "other": {"command": "x"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(existing))
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is True
    settings = json.loads((tmp_path / ".mcp.json").read_text())
    assert "test-mcp" not in settings["mcpServers"]
    assert "other" in settings["mcpServers"]


def test_remove_cleans_empty_mcpservers(tmp_path: Path) -> None:
    existing = {"mcpServers": {"test-mcp": _TEST_SERVER}, "other": 1}
    (tmp_path / ".mcp.json").write_text(json.dumps(existing))
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is True
    settings = json.loads((tmp_path / ".mcp.json").read_text())
    assert "mcpServers" not in settings
    assert settings["other"] == 1


def test_remove_deletes_file_when_nothing_left(tmp_path: Path) -> None:
    """An uninstall does not leave an empty .mcp.json behind."""
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"test-mcp": _TEST_SERVER}}))
    assert remove_mcp_server(tmp_path, "test-mcp") is True
    assert not (tmp_path / ".mcp.json").exists()


def test_remove_when_not_present(tmp_path: Path) -> None:
    existing = {"mcpServers": {"other": {"command": "x"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(existing))
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is False


def test_remove_when_no_file(tmp_path: Path) -> None:
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is False


def test_remove_skips_malformed_json(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{bad")
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is False


def test_remove_skips_non_dict_json(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text('"string"')
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is False


def test_remove_skips_non_dict_mcpservers(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": 42}))
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is False


def test_remove_handles_oserror(tmp_path: Path) -> None:
    existing = {"mcpServers": {"test-mcp": _TEST_SERVER}, "other": 1}
    (tmp_path / ".mcp.json").write_text(json.dumps(existing))
    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is False


# -- build_atlassian_mcp_server_config --


def test_build_atlassian_config_api_token_auth() -> None:
    cfg = AtlassianMCPConfig(
        enabled=True,
        jira_url="https://acme.atlassian.net",
        confluence_url="https://acme.atlassian.net/wiki",
        auth_type="api_token",
        email="agent@acme.com",
        token="cloud-token",
    )
    server = build_atlassian_mcp_server_config(cfg)
    assert server["command"] == "uvx"
    assert server["args"] == ["mcp-atlassian"]
    env = server["env"]
    assert env["JIRA_URL"] == "https://acme.atlassian.net"
    assert env["JIRA_USERNAME"] == "agent@acme.com"
    assert env["JIRA_API_TOKEN"] == "cloud-token"
    assert env["CONFLUENCE_URL"] == "https://acme.atlassian.net/wiki"
    assert env["CONFLUENCE_USERNAME"] == "agent@acme.com"
    assert env["CONFLUENCE_API_TOKEN"] == "cloud-token"
    assert env["READ_ONLY_MODE"] == "true"
    assert env["TOOLSETS"] == "jira_read,confluence_read,confluence_search"
    assert "JIRA_PERSONAL_TOKEN" not in env
    assert "CONFLUENCE_PERSONAL_TOKEN" not in env


def test_build_atlassian_config_pat_auth() -> None:
    cfg = AtlassianMCPConfig(
        enabled=True,
        jira_url="https://issues.redhat.com",
        auth_type="pat",
        token="onprem-pat",
        read_only=False,
    )
    server = build_atlassian_mcp_server_config(cfg)
    env = server["env"]
    assert env["JIRA_PERSONAL_TOKEN"] == "onprem-pat"
    assert env["READ_ONLY_MODE"] == "false"
    assert "JIRA_USERNAME" not in env
    assert "JIRA_API_TOKEN" not in env
    # Confluence not configured, so no Confluence env vars at all.
    assert "CONFLUENCE_URL" not in env
    assert "CONFLUENCE_PERSONAL_TOKEN" not in env


def test_build_atlassian_config_empty_toolsets_omitted() -> None:
    cfg = AtlassianMCPConfig(enabled=True, jira_url="https://issues.redhat.com", toolsets=[])
    server = build_atlassian_mcp_server_config(cfg)
    assert "TOOLSETS" not in server["env"]


# -- remove_settings_mcp_server (legacy settings.json cleanup) --


def test_remove_legacy_settings_entry(tmp_path: Path) -> None:
    existing = {"mcpServers": {"test-mcp": _TEST_SERVER}, "permissions": {"allow": []}}
    (tmp_path / "settings.json").write_text(json.dumps(existing))
    assert remove_settings_mcp_server(tmp_path, "test-mcp") is True
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "mcpServers" not in settings
    assert settings["permissions"] == {"allow": []}
    # settings.json is never deleted, even when only SOVA-managed keys remain.
    assert (tmp_path / "settings.json").exists()


def test_remove_legacy_settings_entry_missing(tmp_path: Path) -> None:
    assert remove_settings_mcp_server(tmp_path, "test-mcp") is False


# -- set_project_mcp_approval --


def test_approval_added_to_empty_settings(tmp_path: Path) -> None:
    assert set_project_mcp_approval(tmp_path, "test-mcp", approved=True) is True
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["enabledMcpjsonServers"] == ["test-mcp"]


def test_approval_preserves_other_entries(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"enabledMcpjsonServers": ["other"], "model": "opus"}))
    assert set_project_mcp_approval(tmp_path, "test-mcp", approved=True) is True
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["enabledMcpjsonServers"] == ["other", "test-mcp"]
    assert settings["model"] == "opus"


def test_approval_idempotent(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"enabledMcpjsonServers": ["test-mcp"]}))
    assert set_project_mcp_approval(tmp_path, "test-mcp", approved=True) is False


def test_approval_revoked(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"enabledMcpjsonServers": ["test-mcp", "other"]}))
    assert set_project_mcp_approval(tmp_path, "test-mcp", approved=False) is True
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["enabledMcpjsonServers"] == ["other"]


def test_approval_revoke_drops_empty_key(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"enabledMcpjsonServers": ["test-mcp"], "model": "opus"}))
    assert set_project_mcp_approval(tmp_path, "test-mcp", approved=False) is True
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "enabledMcpjsonServers" not in settings
    assert settings["model"] == "opus"


def test_approval_revoke_when_absent(tmp_path: Path) -> None:
    assert set_project_mcp_approval(tmp_path, "test-mcp", approved=False) is False


def test_approval_skips_malformed_key(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"enabledMcpjsonServers": "bad"}))
    assert set_project_mcp_approval(tmp_path, "test-mcp", approved=True) is False


# -- atlassian_config_problems --


def test_atlassian_problems_none_when_complete() -> None:
    cfg = AtlassianMCPConfig(enabled=True, jira_url="https://issues.redhat.com", auth_type="pat", token="t")
    assert atlassian_config_problems(cfg) == []


def test_atlassian_problems_missing_urls_and_token() -> None:
    problems = atlassian_config_problems(AtlassianMCPConfig(enabled=True, auth_type="pat"))
    assert any("jira_url" in p for p in problems)
    assert any("token" in p for p in problems)


def test_atlassian_problems_api_token_requires_email() -> None:
    cfg = AtlassianMCPConfig(enabled=True, jira_url="https://acme.atlassian.net", token="t")
    problems = atlassian_config_problems(cfg)
    assert problems == ["set mcp.atlassian.email (required when auth_type is api_token)"]


def test_atlassian_problems_token_from_env(monkeypatch) -> None:
    """An exported token satisfies the credential check (placeholder expansion path)."""
    monkeypatch.setenv(ATLASSIAN_TOKEN_ENV, "env-token")
    cfg = AtlassianMCPConfig(enabled=True, jira_url="https://issues.redhat.com", auth_type="pat")
    assert atlassian_config_problems(cfg) == []


def test_build_atlassian_config_uses_env_placeholder_without_token() -> None:
    """With no stored token the secret stays out of .mcp.json entirely."""
    cfg = AtlassianMCPConfig(enabled=True, jira_url="https://issues.redhat.com", auth_type="pat")
    env = build_atlassian_mcp_server_config(cfg)["env"]
    assert env["JIRA_PERSONAL_TOKEN"] == "${SOVA_MCP_ATLASSIAN_TOKEN}"


def test_build_atlassian_config_uses_placeholder_for_env_token(monkeypatch) -> None:
    """A token that came from the environment is referenced, not copied into .mcp.json."""
    monkeypatch.setenv(ATLASSIAN_TOKEN_ENV, "env-token")
    cfg = AtlassianMCPConfig(enabled=True, jira_url="https://issues.redhat.com", auth_type="pat", token="env-token")
    assert atlassian_token_inlined(cfg) is False
    env = build_atlassian_mcp_server_config(cfg)["env"]
    assert env["JIRA_PERSONAL_TOKEN"] == "${SOVA_MCP_ATLASSIAN_TOKEN}"


def test_atlassian_token_inlined_for_stored_token(monkeypatch) -> None:
    monkeypatch.delenv(ATLASSIAN_TOKEN_ENV, raising=False)
    cfg = AtlassianMCPConfig(enabled=True, jira_url="https://issues.redhat.com", auth_type="pat", token="stored")
    assert atlassian_token_inlined(cfg) is True
    assert build_atlassian_mcp_server_config(cfg)["env"]["JIRA_PERSONAL_TOKEN"] == "stored"
