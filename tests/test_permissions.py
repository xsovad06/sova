"""Unit tests for sova.utils.permissions module."""

from __future__ import annotations

import json
from pathlib import Path

from sova.utils.permissions import (
    _ADDED_MARKER_FILENAME,
    _REQUIRED_PERMISSIONS,
    PermissionRemovalResult,
    check_agent_permissions,
    inject_agent_permissions,
    remove_agent_permissions,
)

REQUIRED_PERMISSIONS = _REQUIRED_PERMISSIONS


def test_inject_agent_permissions_fresh_file(tmp_path: Path) -> None:
    """Test injecting permissions into a fresh .claude/settings.json."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()

    result = inject_agent_permissions(claude_dir)

    assert result is True
    settings_path = claude_dir / "settings.json"
    data = json.loads(settings_path.read_text())
    assert "permissions" in data
    assert "allow" in data["permissions"]
    assert data["permissions"]["allow"] == REQUIRED_PERMISSIONS


def test_inject_agent_permissions_merge_existing_permissions(tmp_path: Path) -> None:
    """Test merging permissions when some already exist."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)", "Read(*)"]}}))

    result = inject_agent_permissions(claude_dir)

    assert result is True
    data = json.loads(settings_path.read_text())
    assert sorted(data["permissions"]["allow"]) == sorted(REQUIRED_PERMISSIONS)


def test_inject_agent_permissions_dedup(tmp_path: Path) -> None:
    """Test that already-present permissions are not duplicated."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": REQUIRED_PERMISSIONS.copy()}}))

    result = inject_agent_permissions(claude_dir)

    assert result is False
    data = json.loads(settings_path.read_text())
    assert data["permissions"]["allow"] == REQUIRED_PERMISSIONS


def test_inject_agent_permissions_preserve_other_sections(tmp_path: Path) -> None:
    """Test that existing MCP servers and hooks are preserved."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    original = {
        "mcpServers": {"server1": {"command": "test"}},
        "hooks": {"PreToolUse": [{"type": "command", "command": "rtk"}]},
    }
    settings_path.write_text(json.dumps(original, indent=2))

    result = inject_agent_permissions(claude_dir)

    assert result is True
    data = json.loads(settings_path.read_text())
    assert "permissions" in data
    assert data["permissions"]["allow"] == REQUIRED_PERMISSIONS
    assert data["mcpServers"] == original["mcpServers"]
    assert data["hooks"] == original["hooks"]


def test_inject_agent_permissions_malformed_json(tmp_path: Path) -> None:
    """Test that malformed JSON is skipped with a warning."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text("{invalid json")

    result = inject_agent_permissions(claude_dir)

    assert result is False


def test_inject_agent_permissions_non_dict_root(tmp_path: Path) -> None:
    """Test that non-dict root is skipped."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps(["not", "a", "dict"]))

    result = inject_agent_permissions(claude_dir)

    assert result is False


def test_inject_agent_permissions_allow_not_list(tmp_path: Path) -> None:
    """Test that non-list permissions.allow is skipped."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": "not-a-list"}}))

    result = inject_agent_permissions(claude_dir)

    assert result is False


def test_check_agent_permissions_all_present(tmp_path: Path) -> None:
    """Test checking permissions when all are present."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": REQUIRED_PERMISSIONS.copy()}}))

    has_all, missing = check_agent_permissions(claude_dir)

    assert has_all is True
    assert missing == []


def test_check_agent_permissions_partial(tmp_path: Path) -> None:
    """Test checking permissions when some are missing."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)", "Read(*)"]}}))

    has_all, missing = check_agent_permissions(claude_dir)

    assert has_all is False
    assert sorted(missing) == ["Edit(*)", "Write(*)"]


def test_check_agent_permissions_missing_file(tmp_path: Path) -> None:
    """Test checking permissions when settings.json does not exist."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()

    has_all, missing = check_agent_permissions(claude_dir)

    assert has_all is False
    assert sorted(missing) == sorted(REQUIRED_PERMISSIONS)


def test_check_agent_permissions_no_permissions_section(tmp_path: Path) -> None:
    """Test checking permissions when permissions section is missing."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"mcpServers": {}}))

    has_all, missing = check_agent_permissions(claude_dir)

    assert has_all is False
    assert sorted(missing) == sorted(REQUIRED_PERMISSIONS)


def test_check_agent_permissions_malformed_json(tmp_path: Path) -> None:
    """Test checking permissions with malformed JSON."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text("{invalid")

    has_all, missing = check_agent_permissions(claude_dir)

    assert has_all is False
    assert sorted(missing) == sorted(REQUIRED_PERMISSIONS)


def test_remove_agent_permissions_success(tmp_path: Path) -> None:
    """Test removing permissions that SOVA itself added."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    inject_agent_permissions(claude_dir)
    settings_path = claude_dir / "settings.json"

    result = remove_agent_permissions(claude_dir)

    assert result is PermissionRemovalResult.CHANGED
    # File should be deleted when permissions section is removed and no other content remains
    data = json.loads(settings_path.read_text())
    assert "permissions" not in data or not data.get("permissions", {}).get("allow")


def test_remove_agent_permissions_preserve_other_permissions(tmp_path: Path) -> None:
    """Test removing only the agent permissions SOVA added when other permissions exist."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": ["CustomTool(*)"]}}))
    inject_agent_permissions(claude_dir)

    result = remove_agent_permissions(claude_dir)

    assert result is PermissionRemovalResult.CHANGED
    data = json.loads(settings_path.read_text())
    assert data["permissions"]["allow"] == ["CustomTool(*)"]


def test_remove_agent_permissions_preserve_other_sections(tmp_path: Path) -> None:
    """Test that removing permissions preserves other sections."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    original = {"mcpServers": {"server1": {"command": "test"}}}
    settings_path.write_text(json.dumps(original))
    inject_agent_permissions(claude_dir)

    result = remove_agent_permissions(claude_dir)

    assert result is PermissionRemovalResult.CHANGED
    data = json.loads(settings_path.read_text())
    assert data["mcpServers"] == original["mcpServers"]
    assert "permissions" not in data or not data.get("permissions", {}).get("allow")


def test_remove_agent_permissions_preserves_pre_existing_entries(tmp_path: Path) -> None:
    """An entry that predates SOVA, and was never recorded as added, survives uninstall."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)"]}}))
    inject_agent_permissions(claude_dir)  # only adds Read(*), Edit(*), Write(*)

    result = remove_agent_permissions(claude_dir)

    assert result is PermissionRemovalResult.CHANGED
    data = json.loads(settings_path.read_text())
    assert data["permissions"]["allow"] == ["Bash(*)"]


def test_remove_agent_permissions_no_marker_leaves_entries_untouched(tmp_path: Path) -> None:
    """Without a record of what SOVA added, removal must not guess and delete entries."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": REQUIRED_PERMISSIONS.copy()}}))

    result = remove_agent_permissions(claude_dir)

    assert result is PermissionRemovalResult.UNCHANGED
    data = json.loads(settings_path.read_text())
    assert data["permissions"]["allow"] == REQUIRED_PERMISSIONS


def test_remove_agent_permissions_missing_file(tmp_path: Path) -> None:
    """Test removing permissions when file does not exist."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()

    result = remove_agent_permissions(claude_dir)

    assert result is PermissionRemovalResult.UNCHANGED


def test_remove_agent_permissions_already_removed(tmp_path: Path) -> None:
    """Test removing permissions that are already absent."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    inject_agent_permissions(claude_dir)
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"mcpServers": {}}))

    result = remove_agent_permissions(claude_dir)

    assert result is PermissionRemovalResult.UNCHANGED


def test_inject_agent_permissions_allow_contains_non_hashable(tmp_path: Path) -> None:
    """Test that non-hashable items in allow list are handled gracefully."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)", ["nested", "list"]]}}))

    result = inject_agent_permissions(claude_dir)

    assert result is False


def test_check_agent_permissions_allow_contains_non_hashable(tmp_path: Path) -> None:
    """Test that non-hashable items in allow list are handled gracefully."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)", ["nested", "list"]]}}))

    has_all, missing = check_agent_permissions(claude_dir)

    assert has_all is False
    assert sorted(missing) == sorted(REQUIRED_PERMISSIONS)


def test_remove_agent_permissions_allow_contains_non_hashable(tmp_path: Path) -> None:
    """Test that non-hashable items in allow list are handled gracefully."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(
        json.dumps({"permissions": {"allow": ["Bash(*)", "Read(*)", "Edit(*)", "Write(*)", ["nested", "list"]]}})
    )
    (claude_dir / _ADDED_MARKER_FILENAME).write_text(json.dumps({"added": REQUIRED_PERMISSIONS.copy()}))

    result = remove_agent_permissions(claude_dir)

    assert result is PermissionRemovalResult.ERROR
