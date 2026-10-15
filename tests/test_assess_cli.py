"""Tests for `sova assess set/get/clear` (sova.cli.commands.assess)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from sova.cli.app import app

runner = CliRunner()


def _invoke(*args: str, project: Path) -> object:
    return runner.invoke(app, ["assess", *args, "--project", str(project)])


def _write_repo(project: Path, repo: str = "owner/repo") -> None:
    (project / "sova.toml").write_text(f'github_repo = "{repo}"\n')


class TestAssessSetGet:
    def test_set_then_get_ready(self, tmp_path: Path) -> None:
        _write_repo(tmp_path)
        result = _invoke("set", "42", "ready", project=tmp_path)
        assert result.exit_code == 0

        result = _invoke("get", "42", project=tmp_path)
        assert result.exit_code == 0
        assert result.output.strip() == "ready"

    def test_set_then_get_interactive(self, tmp_path: Path) -> None:
        _write_repo(tmp_path)
        _invoke("set", "42", "interactive", project=tmp_path)

        result = _invoke("get", "42", project=tmp_path)
        assert result.output.strip() == "interactive"

    def test_set_then_get_human_only(self, tmp_path: Path) -> None:
        _write_repo(tmp_path)
        _invoke("set", "42", "human-only", project=tmp_path)

        result = _invoke("get", "42", project=tmp_path)
        assert result.output.strip() == "human-only"

    def test_get_with_no_record_prints_none(self, tmp_path: Path) -> None:
        _write_repo(tmp_path)
        result = _invoke("get", "99", project=tmp_path)
        assert result.exit_code == 0
        assert result.output.strip().splitlines()[-1] == "none"

    def test_get_prints_value_on_stdout_only(self, tmp_path: Path) -> None:
        """A value-returning command must be pipeable: $(sova assess get N) must capture it."""
        _write_repo(tmp_path)
        _invoke("set", "42", "ready", project=tmp_path)

        result = _invoke("get", "42", project=tmp_path)
        assert result.stdout.strip() == "ready"

    def test_get_none_prints_on_stdout_only(self, tmp_path: Path) -> None:
        _write_repo(tmp_path)
        result = _invoke("get", "99", project=tmp_path)
        assert result.stdout.strip() == "none"

    def test_set_unknown_suitability_fails(self, tmp_path: Path) -> None:
        _write_repo(tmp_path)
        result = _invoke("set", "42", "bogus", project=tmp_path)
        assert result.exit_code == 1
        assert "Unknown suitability" in result.output

    def test_latest_set_wins(self, tmp_path: Path) -> None:
        _write_repo(tmp_path)
        _invoke("set", "42", "human-only", project=tmp_path)
        _invoke("set", "42", "ready", project=tmp_path)

        result = _invoke("get", "42", project=tmp_path)
        assert result.output.strip() == "ready"

    def test_jira_key_prefix_is_normalized(self, tmp_path: Path) -> None:
        _write_repo(tmp_path, repo="RHCLOUD")
        _invoke("set", "RHCLOUD-1234", "ready", project=tmp_path)

        result = _invoke("get", "1234", project=tmp_path)
        assert result.output.strip() == "ready"

    def test_hash_prefix_is_normalized(self, tmp_path: Path) -> None:
        _write_repo(tmp_path)
        _invoke("set", "#42", "ready", project=tmp_path)

        result = _invoke("get", "42", project=tmp_path)
        assert result.output.strip() == "ready"

    def test_non_numeric_issue_is_rejected(self, tmp_path: Path) -> None:
        """The gate reads by integer issue id, so a non-numeric row is unreadable."""
        _write_repo(tmp_path)
        result = _invoke("set", "RHCLOUD-ABC", "ready", project=tmp_path)
        assert result.exit_code == 1
        assert "Unrecognized issue number" in result.output

    def test_empty_issue_is_rejected(self, tmp_path: Path) -> None:
        _write_repo(tmp_path)
        result = _invoke("set", "", "ready", project=tmp_path)
        assert result.exit_code == 1

    def test_set_records_no_complexity_opinion(self, tmp_path: Path) -> None:
        """A suitability-only row must not shadow the dashboard's own complexity assessment."""
        import asyncio

        from sova.db.session import close_db, get_session
        from sova.knowledge.assessments import get_latest_assessment

        _write_repo(tmp_path)
        _invoke("set", "42", "ready", project=tmp_path)

        async def _read() -> str:
            try:
                async with await get_session(tmp_path) as session:
                    record = await get_latest_assessment(session, issue_number="42", project_slug="owner/repo")
                assert record is not None
                return record.estimated_complexity
            finally:
                await close_db()

        assert asyncio.run(_read()) == ""


class TestProjectValidation:
    def test_nonexistent_project_rejected_by_option(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        result = _invoke("get", "1", project=missing)
        assert result.exit_code == 2

    def test_uninstalled_project_rejected_before_db_creation(self, tmp_path: Path) -> None:
        """A directory with no sova.db, sova.toml, or registry entry must not get a throwaway DB."""
        result = _invoke("get", "1", project=tmp_path)
        assert result.exit_code == 1
        assert not (tmp_path / ".claude" / "sova.db").exists()

    @staticmethod
    def _write_jira_repo(project: Path, *, jira_project_key: str = "RHCLOUD") -> None:
        (project / "sova.toml").write_text(
            f'github_repo = "owner/repo"\n[task_source]\ntype = "jira"\njira_project_key = "{jira_project_key}"\n'
        )

    def test_foreign_jira_prefix_rejected(self, tmp_path: Path) -> None:
        """A key from a different tracker project must not silently write under this project's id."""
        self._write_jira_repo(tmp_path)

        result = _invoke("set", "ABC-1234", "ready", project=tmp_path)
        assert result.exit_code == 1
        assert "does not belong to this project" in result.output

    def test_matching_jira_prefix_accepted(self, tmp_path: Path) -> None:
        self._write_jira_repo(tmp_path)

        result = _invoke("set", "RHCLOUD-1234", "ready", project=tmp_path)
        assert result.exit_code == 0, result.output


class TestProjectResolution:
    def test_no_project_option_resolves_primary_worktree_root(self, tmp_path: Path, monkeypatch) -> None:
        """`.claude/sova.db` lives in the primary checkout, not in a linked worktree."""
        primary = tmp_path / "primary"
        (primary / ".claude").mkdir(parents=True)
        _write_repo(primary)
        worktree = primary / ".claude" / "worktrees" / "7"
        worktree.mkdir(parents=True)
        monkeypatch.chdir(worktree)

        async def _fake_root(cwd: Path | None = None) -> Path:
            return primary

        monkeypatch.setattr("sova.git.worktree.get_primary_worktree_root", _fake_root)

        result = runner.invoke(app, ["assess", "set", "42", "ready"])
        assert result.exit_code == 0, result.output
        assert (primary / ".claude" / "sova.db").exists()
        assert not (worktree / ".claude").exists()


class TestAssessClear:
    def test_clear_removes_record(self, tmp_path: Path) -> None:
        _write_repo(tmp_path)
        _invoke("set", "42", "ready", project=tmp_path)

        result = _invoke("clear", "42", project=tmp_path)
        assert result.exit_code == 0
        assert "Cleared" in result.output

        result = _invoke("get", "42", project=tmp_path)
        assert result.output.strip() == "none"

    def test_clear_with_no_record_is_a_noop(self, tmp_path: Path) -> None:
        _write_repo(tmp_path)
        result = _invoke("clear", "99", project=tmp_path)
        assert result.exit_code == 0
        assert "No local suitability record" in result.output
