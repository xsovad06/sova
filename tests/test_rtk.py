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
    assert settings["hooks"]["PreToolUse"] == [{"type": "command", "command": "rtk"}]


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
    assert hooks[1] == {"type": "command", "command": "rtk"}


def test_inject_idempotent(tmp_path: Path) -> None:
    """Inject skips if RTK hook already present."""
    existing = {
        "hooks": {
            "PreToolUse": [{"type": "command", "command": "rtk"}],
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
    assert settings["hooks"]["PreToolUse"] == [{"type": "command", "command": "rtk"}]
    assert settings["permissions"] == {"allow": ["Read"]}


# -- remove_rtk_hook --


def test_remove_when_present(tmp_path: Path) -> None:
    """Remove strips RTK hook from settings.json."""
    existing = {
        "hooks": {
            "PreToolUse": [
                {"type": "command", "command": "my-hook"},
                {"type": "command", "command": "rtk"},
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

    (tmp_path / "sova.toml").write_text("")
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()

    with patch("sova.utils.rtk.shutil.which", return_value="/usr/local/bin/rtk"):
        _configure_rtk(tmp_path, claude_dir)

    settings = json.loads((claude_dir / "settings.json").read_text())
    assert any(h.get("command") == "rtk" for h in settings["hooks"]["PreToolUse"])


def test_configure_rtk_skips_when_disabled(tmp_path: Path) -> None:
    """_configure_rtk skips when rtk.enabled is false."""
    from sova.cli.commands.project import _configure_rtk

    (tmp_path / "sova.toml").write_text("[rtk]\nenabled = false\n")
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()

    with patch("sova.utils.rtk.shutil.which", return_value="/usr/local/bin/rtk"):
        _configure_rtk(tmp_path, claude_dir)

    assert not (claude_dir / "settings.json").exists()


def test_configure_rtk_skips_when_binary_missing(tmp_path: Path) -> None:
    """_configure_rtk skips when RTK binary is not installed."""
    from sova.cli.commands.project import _configure_rtk

    (tmp_path / "sova.toml").write_text("")
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()

    with patch("sova.utils.rtk.shutil.which", return_value=None):
        _configure_rtk(tmp_path, claude_dir)

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
