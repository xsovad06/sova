"""Tests for sova.cli.commands.doctor: the Typer command and Rich-table rendering.

The sixteen `_check_*` functions themselves live in sova.utils.diagnostics and are
tested in tests/test_diagnostics.py; this file only covers doctor.py's own glue
(`_doctor()` calling `run_all_checks()` then `_render_results()`) and the table
rendering/exit-code logic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer import Exit

from sova.cli.commands.doctor import _doctor, _render_results, doctor
from sova.utils.diagnostics import DiagnosticCheck


def test_doctor_runs_all_checks_and_renders(tmp_path: Path) -> None:
    """_doctor() must pass run_all_checks()'s result straight to _render_results()."""
    checks = [DiagnosticCheck("database schema", True, "at head (050)", False)]
    rendered: list = []

    with (
        patch("sova.cli.commands.doctor.run_all_checks", new_callable=AsyncMock, return_value=checks) as mock_run,
        patch("sova.cli.commands.doctor._render_results", side_effect=rendered.append),
    ):
        asyncio.run(_doctor(tmp_path))

    mock_run.assert_called_once_with(tmp_path.resolve())
    assert rendered == [checks]


def test_doctor_command_defaults_to_the_current_directory(tmp_path: Path) -> None:
    """The sync Typer entrypoint resolves `--project` (None means cwd) and drives _doctor()."""
    with (
        patch("sova.cli.commands.doctor.run_all_checks", new_callable=AsyncMock, return_value=[]) as mock_run,
        patch("sova.cli.commands.doctor._render_results"),
        patch("sova.cli.commands.doctor.Path.cwd", return_value=tmp_path),
    ):
        doctor(project=None)

    mock_run.assert_called_once_with(tmp_path.resolve())


def test_render_results_all_pass() -> None:
    checks = [DiagnosticCheck("test", True, "ok", True)]
    _render_results(checks)


def test_render_results_required_failure() -> None:
    checks = [DiagnosticCheck("test", False, "fail", True)]
    with pytest.raises(Exit):
        _render_results(checks)


def test_render_results_optional_warning() -> None:
    checks = [DiagnosticCheck("test", False, "warn", False)]
    _render_results(checks)
