"""Tests for `sova checks should-skip/record` (sova.cli.commands.checks).

CliRunner invokes these commands synchronously, and each command's own
implementation calls asyncio.run() internally, so these tests stay sync
(an async test here would already be inside a running loop, which
asyncio.run() rejects) and set up the fixture repo via subprocess directly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from sova.cli.app import app
from sova.core.check_cache import PIPELINE_STAGE_ENV_KEY

runner = CliRunner()

CHECK_CMD = "make check"


def _init_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("initial")
    subprocess.run(["git", "add", "file.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)


class TestShouldSkipCommand:
    def test_exit_1_when_no_marker(self, tmp_path: Path, monkeypatch) -> None:
        _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")

        result = runner.invoke(app, ["checks", "should-skip", CHECK_CMD, "--cwd", str(tmp_path)])

        assert result.exit_code == 1

    def test_exit_0_when_marker_matches(self, tmp_path: Path, monkeypatch) -> None:
        _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        runner.invoke(app, ["checks", "record", CHECK_CMD, "--passed", "--cwd", str(tmp_path)])

        result = runner.invoke(app, ["checks", "should-skip", CHECK_CMD, "--cwd", str(tmp_path)])

        assert result.exit_code == 0

    def test_exit_1_outside_pipeline_even_with_marker(self, tmp_path: Path, monkeypatch) -> None:
        _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        runner.invoke(app, ["checks", "record", CHECK_CMD, "--passed", "--cwd", str(tmp_path)])
        monkeypatch.delenv(PIPELINE_STAGE_ENV_KEY, raising=False)

        result = runner.invoke(app, ["checks", "should-skip", CHECK_CMD, "--cwd", str(tmp_path)])

        assert result.exit_code == 1


class TestRecordCommand:
    def test_passed_writes_marker(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)

        result = runner.invoke(app, ["checks", "record", CHECK_CMD, "--passed", "--cwd", str(tmp_path)])

        assert result.exit_code == 0
        assert (tmp_path / ".claude" / "agent-control" / "check-state.json").is_file()

    def test_failed_writes_marker_that_cannot_be_skipped(self, tmp_path: Path, monkeypatch) -> None:
        _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")

        runner.invoke(app, ["checks", "record", CHECK_CMD, "--failed", "--cwd", str(tmp_path)])
        result = runner.invoke(app, ["checks", "should-skip", CHECK_CMD, "--cwd", str(tmp_path)])

        assert result.exit_code == 1


class TestUnexpectedFailureDegradesSafely:
    """Both subcommands sit in a chain a real check command depends on: a lookup
    failure must mean "run the check", and a bookkeeping failure must never turn
    a passing check into a failing one."""

    def test_should_skip_exits_1_when_the_lookup_raises(self, tmp_path: Path, monkeypatch) -> None:
        _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")

        async def boom(*args, **kwargs):
            raise RuntimeError("git exploded")

        # checks.py imports should_skip inside the command body, so patch the source module.
        monkeypatch.setattr("sova.core.check_cache.should_skip", boom)

        result = runner.invoke(app, ["checks", "should-skip", CHECK_CMD, "--cwd", str(tmp_path)])

        assert result.exit_code == 1
        assert "cache lookup failed" in result.output

    def test_record_exits_0_when_recording_raises(self, tmp_path: Path, monkeypatch) -> None:
        _init_repo(tmp_path)

        async def boom(*args, **kwargs):
            raise RuntimeError("git exploded")

        # checks.py imports record_result inside the command body, so patch the source module.
        monkeypatch.setattr("sova.core.check_cache.record_result", boom)

        result = runner.invoke(app, ["checks", "record", CHECK_CMD, "--passed", "--cwd", str(tmp_path)])

        assert result.exit_code == 0
        assert "not recorded" in result.output
