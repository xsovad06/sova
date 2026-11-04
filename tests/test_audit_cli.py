"""Tests for `sova audit report-path` and `sova audit disclosure` (sova.cli.commands.audit).

The verdicts replace shell snippets the health-audit command used to carry, so each
mapping that keeps the command's confidentiality behaviour is pinned here, including the
fail-closed ones: an unknown visibility is public and an unexpected git error is unsafe.
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sova.cli.app import app
from sova.cli.commands import audit
from sova.utils.shell import ShellResult

runner = CliRunner()

# Captured before the autouse fixture replaces it, so the root-callback tests can run the real function.
_REAL_INIT_LLM_PROVIDER = importlib.import_module("sova.cli.app")._init_llm_provider


@pytest.fixture(autouse=True)
def _no_provider_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app callback would load the real config and build an LLM provider; these commands never use either."""
    # sova.cli.app names the Typer object, so the module is looked up by import.
    monkeypatch.setattr(importlib.import_module("sova.cli.app"), "_init_llm_provider", lambda: None)


REPORT = "docs/HEALTH-AUDIT.md"


def _git(tmp_path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "commit.gpgsign", "false")


class TestReportPathVerdict:
    def test_ignored_path_is_safe(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / ".gitignore").write_text(f"{REPORT}\n", encoding="utf-8")

        assert asyncio.run(audit.report_path_verdict(REPORT, tmp_path)) == "safe"

    def test_absent_and_not_ignored_path_is_unsafe_untracked(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)

        assert asyncio.run(audit.report_path_verdict(REPORT, tmp_path)) == "unsafe-untracked"

    def test_tracked_path_is_unsafe_tracked_even_when_also_ignored(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / REPORT).write_text("report\n", encoding="utf-8")
        _git(tmp_path, "add", REPORT)
        (tmp_path / ".gitignore").write_text(f"{REPORT}\n", encoding="utf-8")

        assert asyncio.run(audit.report_path_verdict(REPORT, tmp_path)) == "unsafe-tracked"

    def test_outside_a_git_repo_is_unsafe_not_a_git_repo(self, tmp_path: Path) -> None:
        assert asyncio.run(audit.report_path_verdict(REPORT, tmp_path)) == "unsafe-not-a-git-repo"

    def test_missing_git_binary_is_never_safe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async def no_git(*args: str, **kwargs: object) -> ShellResult:
            raise FileNotFoundError("git")

        monkeypatch.setattr(audit, "run", no_git)

        assert asyncio.run(audit.report_path_verdict(REPORT, tmp_path)).startswith("unsafe")

    def test_cli_prints_the_verdict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["audit", "report-path"])

        assert result.exit_code == 0
        # The verdict is the last line: shell.run debug logs share the captured stream under test.
        assert result.stdout.strip().splitlines()[-1] == "unsafe-untracked"

    def test_cli_from_a_subdirectory_judges_the_report_at_the_git_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_repo(tmp_path)
        (tmp_path / ".gitignore").write_text(f"{REPORT}\n", encoding="utf-8")
        (tmp_path / "src").mkdir()
        monkeypatch.chdir(tmp_path / "src")

        result = runner.invoke(app, ["audit", "report-path"])

        assert result.stdout.strip().splitlines()[-1] == "safe"

    def test_project_root_falls_back_to_cwd_outside_a_repo(self, tmp_path: Path) -> None:
        assert asyncio.run(audit.project_root(tmp_path)) == tmp_path

    def test_nested_sova_project_is_judged_from_its_own_directory(self, tmp_path: Path) -> None:
        """The agent writes the report under the SOVA project dir, so a root-anchored .gitignore entry that
        matches only the git root's copy must not make the nested path read as safe."""
        _init_repo(tmp_path)
        project = tmp_path / "apps" / "web"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "sova.db").write_bytes(b"")
        (project / "pkg").mkdir()
        (tmp_path / ".gitignore").write_text(f"/{REPORT}\n", encoding="utf-8")

        root = asyncio.run(audit.project_root(project / "pkg"))

        assert root == project
        assert asyncio.run(audit.report_path_verdict(REPORT, root)) == "unsafe-untracked"

    def test_unreadable_config_does_not_crash_the_root_callback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_config can raise OSError, which the callback's config-error handler does not catch."""
        _init_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        app_module = importlib.import_module("sova.cli.app")

        def unreadable(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("sova.toml")

        monkeypatch.setattr(app_module, "_init_llm_provider", _REAL_INIT_LLM_PROVIDER)
        monkeypatch.setattr(app_module, "load_config", unreadable)

        result = runner.invoke(app, ["audit", "report-path"])

        assert result.exit_code == 0
        assert result.exception is None
        assert result.stdout.strip().splitlines()[-1] == "unsafe-untracked"

    def test_broken_provider_config_does_not_block_the_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate never uses the LLM provider, so an unloadable provider setting must not hide its verdict."""
        _init_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        def broken() -> None:
            raise ValueError("unknown llm.provider")

        monkeypatch.setattr(importlib.import_module("sova.cli.app"), "_init_llm_provider", broken)

        result = runner.invoke(app, ["audit", "report-path"])

        assert result.exit_code == 0
        assert result.stdout.strip().splitlines()[-1] == "unsafe-untracked"


def _fake_gh(monkeypatch: pytest.MonkeyPatch, result: ShellResult) -> None:
    async def fake(program: str, *args: str, cwd: Path) -> ShellResult:
        if program == "git":
            return ShellResult(1, "", "not a git repo")
        assert program == "gh"
        return result

    monkeypatch.setattr(audit, "_exec", fake)


class TestDisclosureVerdict:
    @pytest.mark.parametrize("visibility", ["PRIVATE", "INTERNAL", "private"])
    def test_private_and_internal_are_private(
        self, visibility: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_gh(monkeypatch, ShellResult(0, f"{visibility}\n", ""))

        assert asyncio.run(audit.disclosure_verdict(tmp_path)) == "private"

    def test_public_without_a_policy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_gh(monkeypatch, ShellResult(0, "PUBLIC\n", ""))

        assert asyncio.run(audit.disclosure_verdict(tmp_path)) == "public-no-policy"

    @pytest.mark.parametrize("location", ["SECURITY.md", ".github/SECURITY.md", "docs/SECURITY.md"])
    def test_public_with_a_policy_at_each_location(
        self, location: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_gh(monkeypatch, ShellResult(0, "PUBLIC\n", ""))
        (tmp_path / location).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / location).write_text("report to security@example.com\n", encoding="utf-8")

        assert asyncio.run(audit.disclosure_verdict(tmp_path)) == f"public-policy {location}"

    def test_failed_check_is_public_not_private(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_gh(monkeypatch, ShellResult(1, "PRIVATE\n", "gh: not logged in"))

        assert asyncio.run(audit.disclosure_verdict(tmp_path)) == "public-no-policy"

    @pytest.mark.parametrize("output", ["", "SECRET", "{}"])
    def test_unknown_visibility_is_public(self, output: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_gh(monkeypatch, ShellResult(0, output, ""))

        assert asyncio.run(audit.disclosure_verdict(tmp_path)) == "public-no-policy"

    def test_failed_check_still_reports_a_policy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_gh(monkeypatch, ShellResult(127, "", "could not run gh"))
        (tmp_path / "SECURITY.md").write_text("policy\n", encoding="utf-8")

        assert asyncio.run(audit.disclosure_verdict(tmp_path)) == "public-policy SECURITY.md"

    def test_cli_prints_only_the_verdict_when_the_visibility_check_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_gh(monkeypatch, ShellResult(1, "", "gh: not logged in"))
        (tmp_path / "SECURITY.md").write_text("policy\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["audit", "disclosure"])

        assert result.exit_code == 0
        # The structured warning is emitted as event audit.visibility_check_failed and may share captured
        # stdout under CliRunner, so the verdict is the last line.
        assert result.stdout.strip().splitlines()[-1] == "public-policy SECURITY.md"
        assert "audit.visibility_check_failed" in result.output

    def test_cli_prints_the_verdict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_gh(monkeypatch, ShellResult(0, "PRIVATE\n", ""))
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["audit", "disclosure"])

        assert result.exit_code == 0
        assert result.stdout.strip() == "private"
