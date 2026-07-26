"""Tests for MCP server configuration management."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sova.utils.mcp_config import inject_mcp_server, remove_mcp_server

_TEST_SERVER = {"command": "npx", "args": ["-y", "@test/mcp@latest"]}


def test_inject_creates_settings_file(tmp_path: Path) -> None:
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is True
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["mcpServers"]["test-mcp"] == _TEST_SERVER


def test_inject_merges_into_existing(tmp_path: Path) -> None:
    existing = {"permissions": {"allow": ["Read"]}, "mcpServers": {"other": {"command": "x"}}}
    (tmp_path / "settings.json").write_text(json.dumps(existing))
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is True
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["permissions"] == {"allow": ["Read"]}
    assert "other" in settings["mcpServers"]
    assert settings["mcpServers"]["test-mcp"] == _TEST_SERVER


def test_inject_idempotent(tmp_path: Path) -> None:
    existing = {"mcpServers": {"test-mcp": _TEST_SERVER}}
    (tmp_path / "settings.json").write_text(json.dumps(existing))
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is False


def test_inject_skips_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{broken json!!!")
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is False


def test_inject_adds_mcpservers_key(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"permissions": {}}))
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is True
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["mcpServers"]["test-mcp"] == _TEST_SERVER


def test_inject_skips_non_dict_json(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("[1, 2, 3]")
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is False


def test_inject_skips_non_dict_mcpservers(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"mcpServers": "bad"}))
    result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is False


def test_inject_handles_oserror(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{}")
    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        result = inject_mcp_server(tmp_path, "test-mcp", _TEST_SERVER)
    assert result is False


def test_remove_when_present(tmp_path: Path) -> None:
    existing = {"mcpServers": {"test-mcp": _TEST_SERVER, "other": {"command": "x"}}}
    (tmp_path / "settings.json").write_text(json.dumps(existing))
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is True
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "test-mcp" not in settings["mcpServers"]
    assert "other" in settings["mcpServers"]


def test_remove_cleans_empty_mcpservers(tmp_path: Path) -> None:
    existing = {"mcpServers": {"test-mcp": _TEST_SERVER}}
    (tmp_path / "settings.json").write_text(json.dumps(existing))
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is True
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "mcpServers" not in settings


def test_remove_when_not_present(tmp_path: Path) -> None:
    existing = {"mcpServers": {"other": {"command": "x"}}}
    (tmp_path / "settings.json").write_text(json.dumps(existing))
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is False


def test_remove_when_no_file(tmp_path: Path) -> None:
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is False


def test_remove_skips_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{bad")
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is False


def test_remove_skips_non_dict_json(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text('"string"')
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is False


def test_remove_skips_non_dict_mcpservers(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"mcpServers": 42}))
    result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is False


def test_remove_handles_oserror(tmp_path: Path) -> None:
    existing = {"mcpServers": {"test-mcp": _TEST_SERVER}}
    (tmp_path / "settings.json").write_text(json.dumps(existing))
    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        result = remove_mcp_server(tmp_path, "test-mcp")
    assert result is False
