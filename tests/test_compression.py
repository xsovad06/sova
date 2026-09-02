"""Tests for the optional Headroom prompt compression module and its config."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sova.config.loader import load_config
from sova.config.models import HeadroomConfig, ProjectConfig
from sova.dashboard.settings_meta import get_meta
from sova.llm import compression


def _config(*, enabled: bool = True, min_chars: int = 10) -> ProjectConfig:
    return ProjectConfig(compression=HeadroomConfig(enabled=enabled, min_chars=min_chars))


# is_compression_available


def test_is_compression_available_true() -> None:
    with patch.object(compression, "_HEADROOM_AVAILABLE", True):
        assert compression.is_compression_available() is True


def test_is_compression_available_false() -> None:
    with patch.object(compression, "_HEADROOM_AVAILABLE", False):
        assert compression.is_compression_available() is False


# compress: passthrough paths


def test_compress_passthrough_when_unavailable() -> None:
    text = "x" * 1000
    with (
        patch.object(compression, "_HEADROOM_AVAILABLE", False),
        patch.object(compression, "load_config") as mock_load,
    ):
        assert compression.compress(text) == text
    # No config read needed when the package is missing.
    mock_load.assert_not_called()


def test_compress_passthrough_when_disabled() -> None:
    text = "x" * 1000
    fake = MagicMock()
    with (
        patch.object(compression, "_HEADROOM_AVAILABLE", True),
        patch.object(compression, "headroom_compression", fake, create=True),
        patch.object(compression, "load_config", return_value=_config(enabled=False)),
    ):
        assert compression.compress(text) == text
    fake.compress.assert_not_called()


def test_compress_passthrough_below_min_chars() -> None:
    text = "short"
    fake = MagicMock()
    with (
        patch.object(compression, "_HEADROOM_AVAILABLE", True),
        patch.object(compression, "headroom_compression", fake, create=True),
        patch.object(compression, "load_config", return_value=_config(min_chars=500)),
    ):
        assert compression.compress(text) == text
    fake.compress.assert_not_called()


def test_compress_empty_string_passthrough() -> None:
    fake = MagicMock()
    with (
        patch.object(compression, "_HEADROOM_AVAILABLE", True),
        patch.object(compression, "headroom_compression", fake, create=True),
        patch.object(compression, "load_config", return_value=_config(min_chars=500)),
    ):
        assert compression.compress("") == ""
    fake.compress.assert_not_called()


# compress: happy path


def test_compress_happy_path_returns_compressed() -> None:
    text = "x" * 1000
    fake = MagicMock()
    result = MagicMock()
    result.compressed = "compressed"
    fake.compress.return_value = result
    with (
        patch.object(compression, "_HEADROOM_AVAILABLE", True),
        patch.object(compression, "headroom_compression", fake, create=True),
        patch.object(compression, "load_config", return_value=_config(min_chars=10)),
    ):
        assert compression.compress(text, content_type="json") == "compressed"
    fake.compress.assert_called_once_with(text, content_type="json")


@pytest.mark.parametrize("content_type", ["text", "json", "code", "log", "diff"])
def test_compress_known_content_type_maps_to_strategy(content_type: str) -> None:
    text = "x" * 1000
    fake = MagicMock()
    result = MagicMock()
    result.compressed = "c"
    fake.compress.return_value = result
    with (
        patch.object(compression, "_HEADROOM_AVAILABLE", True),
        patch.object(compression, "headroom_compression", fake, create=True),
        patch.object(compression, "load_config", return_value=_config()),
    ):
        compression.compress(text, content_type=content_type)
    fake.compress.assert_called_once_with(text, content_type=content_type)


def test_compress_unknown_content_type_falls_back_to_text() -> None:
    text = "x" * 1000
    fake = MagicMock()
    result = MagicMock()
    result.compressed = "c"
    fake.compress.return_value = result
    with (
        patch.object(compression, "_HEADROOM_AVAILABLE", True),
        patch.object(compression, "headroom_compression", fake, create=True),
        patch.object(compression, "load_config", return_value=_config()),
    ):
        compression.compress(text, content_type="yaml")
    fake.compress.assert_called_once_with(text, content_type="text")


# compress: graceful degradation on Headroom failure


def test_compress_returns_input_when_headroom_raises() -> None:
    text = "x" * 1000
    fake = MagicMock()
    fake.compress.side_effect = RuntimeError("boom")
    with (
        patch.object(compression, "_HEADROOM_AVAILABLE", True),
        patch.object(compression, "headroom_compression", fake, create=True),
        patch.object(compression, "load_config", return_value=_config()),
    ):
        assert compression.compress(text) == text


def test_compress_no_divide_by_zero_when_min_chars_zero() -> None:
    """Empty text with min_chars=0 must not raise ZeroDivisionError."""
    fake = MagicMock()
    result = MagicMock()
    result.compressed = ""
    fake.compress.return_value = result
    with (
        patch.object(compression, "_HEADROOM_AVAILABLE", True),
        patch.object(compression, "headroom_compression", fake, create=True),
        patch.object(compression, "load_config", return_value=_config(min_chars=0)),
    ):
        assert compression.compress("") == ""


# HeadroomConfig


def test_headroom_config_defaults() -> None:
    cfg = HeadroomConfig()
    assert cfg.enabled is False
    assert cfg.min_chars == 500


def test_project_config_has_compression_default() -> None:
    cfg = ProjectConfig()
    assert cfg.compression.enabled is False
    assert cfg.compression.min_chars == 500


def test_compression_section_loaded_from_toml(tmp_path: Path) -> None:
    toml_content = """
[project]
github_repo = "user/repo"

[compression]
enabled = true
min_chars = 250
"""
    (tmp_path / "sova.toml").write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.compression.enabled is True
    assert cfg.compression.min_chars == 250


def test_compression_defaults_when_missing(tmp_path: Path) -> None:
    (tmp_path / "sova.toml").write_text('[project]\ngithub_repo = "user/repo"\n')

    cfg = load_config(tmp_path)
    assert cfg.compression.enabled is False
    assert cfg.compression.min_chars == 500


# settings_meta registration


def test_compression_settings_registered() -> None:
    enabled = get_meta("compression.enabled")
    assert enabled is not None
    assert enabled.group == "agent"
    assert enabled.value_type == "boolean"

    min_chars = get_meta("compression.min_chars")
    assert min_chars is not None
    assert min_chars.group == "agent"
    assert min_chars.value_type == "number"


# Environment overrides


def test_compression_env_override_enabled(tmp_path: Path, monkeypatch) -> None:
    toml_content = """
[project]
github_repo = "user/repo"

[compression]
enabled = false
"""
    (tmp_path / "sova.toml").write_text(toml_content)
    monkeypatch.setenv("SOVA_COMPRESSION_ENABLED", "true")

    cfg = load_config(tmp_path)
    assert cfg.compression.enabled is True


def test_compression_env_override_min_chars(tmp_path: Path, monkeypatch) -> None:
    toml_content = """
[project]
github_repo = "user/repo"

[compression]
min_chars = 500
"""
    (tmp_path / "sova.toml").write_text(toml_content)
    monkeypatch.setenv("SOVA_COMPRESSION_MIN_CHARS", "1000")

    cfg = load_config(tmp_path)
    assert cfg.compression.min_chars == 1000
