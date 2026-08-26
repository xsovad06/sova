"""Tests for RTK integration -- hook injection, removal, install/uninstall, doctor."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sova.utils.rtk import inject_rtk_hook, is_rtk_available, remove_rtk_hook

# -- is_rtk_available --


def test_rtk_available_when_binary_exists() -> None:
    with patch("sova.utils.rtk.shutil.which", return_value="/usr/local/bin/rtk"):
        assert is_rtk_available() is True


def test_rtk_unavailable_when_binary_missing() -> None:
    with patch("sova.utils.rtk.shutil.which", return_value=None):
        assert is_rtk_available() is False


# -- inject_rtk_hook --


def test_inject_creates_settings_file(tmp_path: Path) -> None:
    """Inject creates settings.json if it doesn't exist."""
    result = inject_rtk_hook(tmp_path)
    assert result is True

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["hooks"]["PreToolUse"] == [{"type": "command", "command": "rtk hook claude"}]


def test_inject_merges_into_existing_settings(tmp_path: Path) -> None:
    """Inject preserves existing settings and hooks."""
    existing = {
        "permissions": {"allow": ["Read"]},
        "hooks": {
            "PreToolUse": [{"type": "command", "command": "my-hook"}],
        },
    }
    (tmp_path / "settings.json").write_text(json.dumps(existing))

    result = inject_rtk_hook(tmp_path)
    assert result is True

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["permissions"] == {"allow": ["Read"]}
    hooks = settings["hooks"]["PreToolUse"]
    assert len(hooks) == 2
    assert hooks[0] == {"type": "command", "command": "my-hook"}
    assert hooks[1] == {"type": "command", "command": "rtk hook claude"}


def test_inject_idempotent(tmp_path: Path) -> None:
    """Inject skips if RTK hook already present."""
    existing = {
        "hooks": {
            "PreToolUse": [{"type": "command", "command": "rtk hook claude"}],
        },
    }
    (tmp_path / "settings.json").write_text(json.dumps(existing))

    result = inject_rtk_hook(tmp_path)
    assert result is False

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert len(settings["hooks"]["PreToolUse"]) == 1


def test_inject_idempotent_nested_format(tmp_path: Path) -> None:
    """Inject skips if RTK hook is present in nested matcher format."""
    existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]},
            ],
        },
    }
    (tmp_path / "settings.json").write_text(json.dumps(existing))

    result = inject_rtk_hook(tmp_path)
    assert result is False

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert len(settings["hooks"]["PreToolUse"]) == 1


def test_inject_skips_malformed_json(tmp_path: Path) -> None:
    """Inject skips if settings.json has malformed JSON."""
    (tmp_path / "settings.json").write_text("{broken json!!!")

    result = inject_rtk_hook(tmp_path)
    assert result is False
    # File should be untouched
    assert (tmp_path / "settings.json").read_text() == "{broken json!!!"


def test_inject_adds_hooks_key_to_existing_file(tmp_path: Path) -> None:
    """Inject adds hooks key when settings.json exists but has no hooks."""
    existing = {"permissions": {"allow": ["Read"]}}
    (tmp_path / "settings.json").write_text(json.dumps(existing))

    result = inject_rtk_hook(tmp_path)
    assert result is True

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["hooks"]["PreToolUse"] == [{"type": "command", "command": "rtk hook claude"}]
    assert settings["permissions"] == {"allow": ["Read"]}


# -- remove_rtk_hook --


def test_remove_when_present(tmp_path: Path) -> None:
    """Remove strips RTK hook from settings.json."""
    existing = {
        "hooks": {
            "PreToolUse": [
                {"type": "command", "command": "my-hook"},
                {"type": "command", "command": "rtk hook claude"},
            ],
        },
    }
    (tmp_path / "settings.json").write_text(json.dumps(existing))

    result = remove_rtk_hook(tmp_path)
    assert result is True

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["hooks"]["PreToolUse"] == [{"type": "command", "command": "my-hook"}]


def test_remove_cleans_empty_structures(tmp_path: Path) -> None:
    """Remove cleans up empty hooks/PreToolUse when RTK was the only hook."""
    existing = {
        "hooks": {
            "PreToolUse": [{"type": "command", "command": "rtk hook claude"}],
        },
    }
    (tmp_path / "settings.json").write_text(json.dumps(existing))

    result = remove_rtk_hook(tmp_path)
    assert result is True

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "hooks" not in settings


def test_remove_legacy_entry(tmp_path: Path) -> None:
    """Remove strips legacy 'rtk' entries from settings.json."""
    existing = {
        "hooks": {
            "PreToolUse": [{"type": "command", "command": "rtk"}],
        },
    }
    (tmp_path / "settings.json").write_text(json.dumps(existing))

    result = remove_rtk_hook(tmp_path)
    assert result is True

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "hooks" not in settings


def test_remove_when_not_present(tmp_path: Path) -> None:
    """Remove returns False if no RTK hook found."""
    existing = {
        "hooks": {
            "PreToolUse": [{"type": "command", "command": "other"}],
        },
    }
    (tmp_path / "settings.json").write_text(json.dumps(existing))

    result = remove_rtk_hook(tmp_path)
    assert result is False


def test_remove_when_no_file(tmp_path: Path) -> None:
    """Remove returns False when settings.json doesn't exist."""
    result = remove_rtk_hook(tmp_path)
    assert result is False


def test_remove_skips_malformed_json(tmp_path: Path) -> None:
    """Remove returns False for malformed JSON."""
    (tmp_path / "settings.json").write_text("{bad")
    result = remove_rtk_hook(tmp_path)
    assert result is False


# -- Config model --


def test_rtk_config_defaults() -> None:
    from sova.config.models import ProjectConfig

    cfg = ProjectConfig()
    assert cfg.rtk.enabled is True


def test_rtk_config_disabled_via_toml(tmp_path: Path) -> None:
    from sova.config.loader import load_config

    (tmp_path / "sova.toml").write_text("[rtk]\nenabled = false\n")
    cfg = load_config(tmp_path)
    assert cfg.rtk.enabled is False


# -- Install integration --


def test_configure_rtk_injects_when_available(tmp_path: Path) -> None:
    """_configure_rtk injects hook when RTK is available and enabled."""
    from sova.cli.commands.project import _configure_rtk
    from sova.config.models import ProjectConfig

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    cfg = ProjectConfig()  # rtk.enabled defaults to True

    with patch("sova.utils.rtk.shutil.which", return_value="/usr/local/bin/rtk"):
        _configure_rtk(cfg, claude_dir)

    settings = json.loads((claude_dir / "settings.json").read_text())
    assert any(h.get("command") == "rtk hook claude" for h in settings["hooks"]["PreToolUse"])


def test_configure_rtk_skips_when_disabled(tmp_path: Path) -> None:
    """_configure_rtk skips when rtk.enabled is false."""
    from sova.cli.commands.project import _configure_rtk
    from sova.config.models import ProjectConfig, RTKConfig

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    cfg = ProjectConfig(rtk=RTKConfig(enabled=False))

    with patch("sova.utils.rtk.shutil.which", return_value="/usr/local/bin/rtk"):
        _configure_rtk(cfg, claude_dir)

    assert not (claude_dir / "settings.json").exists()


def test_configure_rtk_skips_when_binary_missing(tmp_path: Path) -> None:
    """_configure_rtk skips when RTK binary is not installed."""
    from sova.cli.commands.project import _configure_rtk
    from sova.config.models import ProjectConfig

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    cfg = ProjectConfig()

    with patch("sova.utils.rtk.shutil.which", return_value=None):
        _configure_rtk(cfg, claude_dir)

    assert not (claude_dir / "settings.json").exists()


# -- Doctor check --


def test_doctor_check_rtk_available() -> None:
    from sova.cli.commands.doctor import _check_rtk

    with patch("sova.cli.commands.doctor.shutil.which", return_value="/usr/local/bin/rtk"):
        checks = _check_rtk()
    assert len(checks) == 1
    name, passed, detail, required = checks[0]
    assert name == "rtk"
    assert passed is True
    assert required is False


def test_doctor_check_rtk_missing() -> None:
    from sova.cli.commands.doctor import _check_rtk

    with patch("sova.cli.commands.doctor.shutil.which", return_value=None):
        checks = _check_rtk()
    assert len(checks) == 1
    name, passed, detail, required = checks[0]
    assert name == "rtk"
    assert passed is False
    assert required is False


# -- Non-dict JSON guards --


def test_inject_skips_non_dict_json(tmp_path: Path) -> None:
    """Inject returns False when settings.json is valid JSON but not a dict."""
    (tmp_path / "settings.json").write_text("[1, 2, 3]")
    result = inject_rtk_hook(tmp_path)
    assert result is False
    # File should be untouched
    assert json.loads((tmp_path / "settings.json").read_text()) == [1, 2, 3]


def test_remove_skips_non_dict_json(tmp_path: Path) -> None:
    """Remove returns False when settings.json is valid JSON but not a dict."""
    (tmp_path / "settings.json").write_text('"just a string"')
    result = remove_rtk_hook(tmp_path)
    assert result is False


def test_inject_skips_when_hooks_is_not_dict(tmp_path: Path) -> None:
    """Inject returns False when hooks key exists but is not a dict."""
    (tmp_path / "settings.json").write_text(json.dumps({"hooks": "not a dict"}))
    result = inject_rtk_hook(tmp_path)
    assert result is False
    # File should be untouched
    assert json.loads((tmp_path / "settings.json").read_text())["hooks"] == "not a dict"


def test_inject_skips_when_pretooluse_is_not_list(tmp_path: Path) -> None:
    """Inject returns False when PreToolUse exists but is not a list."""
    (tmp_path / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": "bad"}}))
    result = inject_rtk_hook(tmp_path)
    assert result is False


def test_remove_skips_when_hooks_is_not_dict(tmp_path: Path) -> None:
    """Remove returns False when hooks key is not a dict."""
    (tmp_path / "settings.json").write_text(json.dumps({"hooks": 42}))
    result = remove_rtk_hook(tmp_path)
    assert result is False


def test_remove_skips_when_pretooluse_is_not_list(tmp_path: Path) -> None:
    """Remove returns False when PreToolUse is not a list."""
    (tmp_path / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": {}}}))
    result = remove_rtk_hook(tmp_path)
    assert result is False


# -- OSError handling --


def test_inject_handles_oserror(tmp_path: Path) -> None:
    """Inject returns False on OSError during write."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")

    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        result = inject_rtk_hook(tmp_path)
    assert result is False


def test_remove_handles_oserror(tmp_path: Path) -> None:
    """Remove returns False on OSError during write."""
    existing = {"hooks": {"PreToolUse": [{"type": "command", "command": "rtk"}]}}
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(existing))

    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        result = remove_rtk_hook(tmp_path)
    assert result is False


# -- _is_rtk_entry edge cases --


def test_is_rtk_entry_non_dict() -> None:
    """_is_rtk_entry returns False for non-dict entries."""
    from sova.utils.rtk import _is_rtk_entry

    assert _is_rtk_entry("not a dict") is False
    assert _is_rtk_entry(42) is False
    assert _is_rtk_entry(None) is False
    assert _is_rtk_entry({"command": "other"}) is False
    assert _is_rtk_entry({"command": "rtk hook claude"}) is True
    assert _is_rtk_entry({"command": "rtk"}) is False


def test_is_rtk_entry_nested_matcher_format() -> None:
    """_is_rtk_entry detects the nested matcher-based format."""
    from sova.utils.rtk import _is_rtk_entry

    nested = {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}
    assert _is_rtk_entry(nested) is True
    nested_legacy = {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk"}]}
    assert _is_rtk_entry(nested_legacy) is False


def test_is_legacy_rtk_entry() -> None:
    """_is_legacy_rtk_entry matches only the bare 'rtk' command."""
    from sova.utils.rtk import _is_legacy_rtk_entry

    assert _is_legacy_rtk_entry({"command": "rtk"}) is True
    assert _is_legacy_rtk_entry({"command": "rtk hook claude"}) is False
    assert _is_legacy_rtk_entry({"command": "other"}) is False
    assert _is_legacy_rtk_entry("not a dict") is False
    nested = {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk"}]}
    assert _is_legacy_rtk_entry(nested) is True


def test_inject_upgrades_legacy_entry(tmp_path: Path) -> None:
    """inject_rtk_hook upgrades legacy 'rtk' entries to 'rtk hook claude'."""
    existing = {
        "hooks": {
            "PreToolUse": [{"type": "command", "command": "rtk"}],
        },
    }
    (tmp_path / "settings.json").write_text(json.dumps(existing))

    result = inject_rtk_hook(tmp_path)
    assert result is True

    settings = json.loads((tmp_path / "settings.json").read_text())
    hooks = settings["hooks"]["PreToolUse"]
    assert len(hooks) == 1
    assert hooks[0]["command"] == "rtk hook claude"


def test_inject_upgrades_nested_legacy_entry(tmp_path: Path) -> None:
    """inject_rtk_hook upgrades legacy 'rtk' inside a nested matcher entry."""
    existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk"}]},
            ],
        },
    }
    (tmp_path / "settings.json").write_text(json.dumps(existing))

    result = inject_rtk_hook(tmp_path)
    assert result is True

    settings = json.loads((tmp_path / "settings.json").read_text())
    nested_hooks = settings["hooks"]["PreToolUse"][0]["hooks"]
    assert nested_hooks[0]["command"] == "rtk hook claude"


def test_inject_idempotent_after_upgrade(tmp_path: Path) -> None:
    """After upgrading a legacy entry, a second inject is idempotent."""
    existing = {
        "hooks": {
            "PreToolUse": [{"type": "command", "command": "rtk"}],
        },
    }
    (tmp_path / "settings.json").write_text(json.dumps(existing))

    assert inject_rtk_hook(tmp_path) is True
    assert inject_rtk_hook(tmp_path) is False


# -- MCP auto-configuration in install --


def test_configure_mcp_injects_patternfly(tmp_path: Path) -> None:
    """_configure_mcp_servers injects PatternFly MCP when PF detected."""
    from sova.cli.commands.project import _configure_mcp_servers

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir()
    pkg = {"name": "test", "dependencies": {"@patternfly/react-core": "^5.0.0"}}
    (project_dir / "package.json").write_text(json.dumps(pkg))

    _configure_mcp_servers(project_dir, claude_dir)

    settings = json.loads((claude_dir / "settings.json").read_text())
    assert "patternfly-mcp" in settings["mcpServers"]
    assert settings["mcpServers"]["patternfly-mcp"]["command"] == "npx"


def test_configure_mcp_skips_without_patternfly(tmp_path: Path) -> None:
    """_configure_mcp_servers does nothing when no PatternFly detected."""
    from sova.cli.commands.project import _configure_mcp_servers

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir()
    pkg = {"name": "test", "dependencies": {"react": "^18.0.0"}}
    (project_dir / "package.json").write_text(json.dumps(pkg))

    _configure_mcp_servers(project_dir, claude_dir)

    assert not (claude_dir / "settings.json").exists()


def test_remove_mcp_server_cleans_settings(tmp_path: Path) -> None:
    """remove_mcp_server removes a named MCP entry from settings.json."""
    from sova.utils.mcp_config import remove_mcp_server

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    pf_config = {"command": "npx", "args": ["-y", "@patternfly/patternfly-mcp@latest"]}
    settings = {"mcpServers": {"patternfly-mcp": pf_config}}
    (claude_dir / "settings.json").write_text(json.dumps(settings))

    result = remove_mcp_server(claude_dir, "patternfly-mcp")
    assert result is True
    data = json.loads((claude_dir / "settings.json").read_text())
    assert "mcpServers" not in data


def test_uninstall_removes_patternfly_mcp(tmp_path: Path) -> None:
    """_uninstall removes PatternFly MCP server from settings.json."""
    import asyncio

    from sova.cli.commands.project import _uninstall

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir()
    pf_config = {"command": "npx", "args": ["-y", "@patternfly/patternfly-mcp@latest"]}
    settings = {"mcpServers": {"patternfly-mcp": pf_config}}
    (claude_dir / "settings.json").write_text(json.dumps(settings))

    asyncio.run(_uninstall(path=project_dir, remove_config=False, remove_memory=False))

    data = json.loads((claude_dir / "settings.json").read_text())
    assert "patternfly-mcp" not in data.get("mcpServers", {})


# -- _detect_tech_stack PatternFly detection --


def test_detect_tech_stack_patternfly_in_deps(tmp_path: Path) -> None:
    """_detect_tech_stack returns patternfly when listed in dependencies."""
    from sova.dashboard.services.setup_service import _detect_tech_stack

    pkg = {"name": "test", "dependencies": {"@patternfly/react-core": "^5.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))

    stack = _detect_tech_stack(tmp_path)
    assert "patternfly" in stack
    assert "javascript" in stack


def test_detect_tech_stack_patternfly_in_devdeps(tmp_path: Path) -> None:
    """_detect_tech_stack returns patternfly when listed in devDependencies."""
    from sova.dashboard.services.setup_service import _detect_tech_stack

    pkg = {"name": "test", "devDependencies": {"@patternfly/react-core": "^5.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))

    stack = _detect_tech_stack(tmp_path)
    assert "patternfly" in stack


def test_detect_tech_stack_no_patternfly(tmp_path: Path) -> None:
    """_detect_tech_stack does not return patternfly when package is absent."""
    from sova.dashboard.services.setup_service import _detect_tech_stack

    pkg = {"name": "test", "dependencies": {"react": "^18.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))

    stack = _detect_tech_stack(tmp_path)
    assert "patternfly" not in stack
    assert "javascript" in stack
