"""Behavioural tests for `sova commands ...` (`sova.cli.commands.commands`), driven through `CliRunner`.

Each test installs the real packaged commands and skills into a real temporary project and reads the result back
from disk and from the command's own report. Nothing is patched: the project registry used by `sync` and
`skills-sync` lives in the fake home that `tests/conftest.py` gives every test, and the Rich console writes to
stderr, so a report is asserted on `result.stderr`. `backport` without `--dry-run` writes into the packaged
canonical directory (this checkout), so it is exercised only through its refusals and its dry run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sova.cli.app import app
from sova.config.registry import register_project

pytestmark = pytest.mark.usefixtures("no_cli_provider_init", "quiet_logs")

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def _commands(*args: str, project: Path | None = None) -> Any:
    extra = ["--project", str(project)] if project is not None else []
    return runner.invoke(app, ["commands", *args, *extra])


def _installed(project: Path) -> list[str]:
    return sorted(path.name for path in (project / ".claude" / "commands").glob("*.md"))


class TestList:
    def test_an_empty_project_has_no_commands(self, project: Path) -> None:
        result = _commands("list", project=project)

        assert result.exit_code == 0
        assert "Managed: 0  |  Local: 0" in result.stderr

    def test_separates_managed_commands_from_local_ones(self, project: Path) -> None:
        _commands("update", project=project)
        (project / ".claude" / "commands" / "mine.md").write_text("---\nname: mine\n---\nMine.\n", encoding="utf-8")

        result = _commands("list", project=project)

        assert result.exit_code == 0
        assert f"Managed: {len(_installed(project)) - 1}  |  Local: 1" in result.stderr
        assert "mine.md" in result.stderr
        assert "local (project)" in result.stderr

    def test_defaults_to_the_working_directory(self, project: Path) -> None:
        result = runner.invoke(app, ["commands", "list"])

        assert result.exit_code == 0
        assert "Managed: 0" in result.stderr


class TestUpdateAndDiff:
    def test_diff_offers_every_command_to_a_project_that_has_none(self, project: Path) -> None:
        result = _commands("diff", project=project)

        assert result.exit_code == 0
        assert "New commands available:" in result.stderr
        assert "+ develop.md" in result.stderr

    def test_update_installs_the_commands_and_a_second_update_changes_nothing(self, project: Path) -> None:
        first = _commands("update", project=project)
        installed = _installed(project)
        second = _commands("update", project=project)

        assert first.exit_code == 0
        assert "develop.md" in installed
        assert "Updated: 0" not in first.stderr
        assert second.exit_code == 0
        assert "Updated: 0" in second.stderr
        assert "Skipped (unchanged):" in second.stderr
        assert _installed(project) == installed

    def test_diff_reports_an_installed_project_as_up_to_date(self, project: Path) -> None:
        _commands("update", project=project)

        result = _commands("diff", project=project)

        assert result.exit_code == 0
        assert "All commands are up to date." in result.stderr

    def test_diff_reports_a_command_whose_source_changed_since_the_install(self, project: Path) -> None:
        _commands("update", project=project)
        manifest = project / ".claude" / "commands" / ".sova-manifest.json"
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        entries["commands"]["develop.md"]["hash"] = "0000000000000000"
        manifest.write_text(json.dumps(entries), encoding="utf-8")

        result = _commands("diff", project=project)

        assert result.exit_code == 0
        assert "Changed commands:" in result.stderr
        assert "~ develop.md" in result.stderr

    def test_diff_reports_a_managed_command_that_canonical_no_longer_has(self, project: Path) -> None:
        _commands("update", project=project)
        commands = project / ".claude" / "commands"
        manifest = commands / ".sova-manifest.json"
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        entries["commands"]["retired.md"] = {"hash": "0000000000000000", "managed": True}
        manifest.write_text(json.dumps(entries), encoding="utf-8")
        (commands / "retired.md").write_text("---\nname: retired\n---\nRetired.\n", encoding="utf-8")

        result = _commands("diff", project=project)

        assert "Removed from canonical:" in result.stderr
        assert "- retired.md" in result.stderr

    def test_update_flags_a_conflict_when_the_file_and_its_source_both_changed(self, project: Path) -> None:
        _commands("update", project=project)
        commands = project / ".claude" / "commands"
        manifest = commands / ".sova-manifest.json"
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        entries["commands"]["develop.md"]["hash"] = "0000000000000000"
        manifest.write_text(json.dumps(entries), encoding="utf-8")
        develop = commands / "develop.md"
        develop.write_text(develop.read_text(encoding="utf-8") + "\nlocal note\n", encoding="utf-8")

        result = _commands("update", project=project)

        assert "Conflicts (1):" in result.stderr
        assert "! develop.md: locally modified, source also changed" in result.stderr
        assert "Use --force to overwrite, or manually merge." in result.stderr
        assert "local note" in develop.read_text(encoding="utf-8")

    def test_a_locally_edited_command_is_kept_without_force_and_restored_with_it(self, project: Path) -> None:
        _commands("update", project=project)
        develop = project / ".claude" / "commands" / "develop.md"
        canonical_text = develop.read_text(encoding="utf-8")
        develop.write_text(canonical_text + "\nlocal note\n", encoding="utf-8")

        _commands("update", project=project)
        kept = develop.read_text(encoding="utf-8")
        forced = _commands("update", "--force", project=project)

        assert "local note" in kept
        assert forced.exit_code == 0
        assert develop.read_text(encoding="utf-8") == canonical_text

    def test_autonomous_commands_are_left_out_on_request(self, project: Path) -> None:
        _commands("update", project=project)
        with_autonomous = set(_installed(project))
        other = project.parent / "other"
        other.mkdir()

        _commands("update", "--no-autonomous", project=other)

        assert set(_installed(other)) <= with_autonomous
        assert len(_installed(other)) < len(with_autonomous)


class TestDrift:
    def test_a_clean_install_has_no_drift(self, project: Path) -> None:
        _commands("update", project=project)

        result = _commands("drift", project=project)

        assert result.exit_code == 0
        assert "No local drift detected." in result.stderr

    def test_a_local_edit_is_reported_with_its_diff(self, project: Path) -> None:
        _commands("update", project=project)
        develop = project / ".claude" / "commands" / "develop.md"
        develop.write_text(develop.read_text(encoding="utf-8") + "\nBack-port candidate line\n", encoding="utf-8")

        result = _commands("drift", project=project)

        assert result.exit_code == 0
        assert "Locally modified (candidates for back-port):" in result.stderr
        assert "~ develop.md" in result.stderr
        assert "+Back-port candidate line" in result.stderr

    def test_no_show_diff_lists_the_file_without_its_diff(self, project: Path) -> None:
        _commands("update", project=project)
        develop = project / ".claude" / "commands" / "develop.md"
        develop.write_text(develop.read_text(encoding="utf-8") + "\nBack-port candidate line\n", encoding="utf-8")

        result = _commands("drift", "--no-show-diff", project=project)

        assert "~ develop.md" in result.stderr
        assert "Back-port candidate line" not in result.stderr

    def test_a_deleted_managed_file_and_an_unmanaged_one_are_listed(self, project: Path) -> None:
        _commands("update", project=project)
        commands = project / ".claude" / "commands"
        (commands / "develop.md").unlink()
        (commands / "mine.md").write_text("---\nname: mine\n---\nMine.\n", encoding="utf-8")

        result = _commands("drift", project=project)

        assert "Managed files deleted locally:" in result.stderr
        assert "- develop.md" in result.stderr
        assert "Unmanaged files (project-local, not tracked by SOVA):" in result.stderr
        assert "? mine.md" in result.stderr

    def test_skills_are_checked_only_on_request(self, project: Path) -> None:
        _commands("skills-update", project=project)
        skills = project / ".claude" / "skills"
        skill = next(path for path in sorted(skills.iterdir()) if (path / "SKILL.md").is_file())
        (skill / "SKILL.md").write_text("changed locally\n", encoding="utf-8")

        without = _commands("drift", project=project)
        with_skills = _commands("drift", "--skills", project=project)

        assert "Skills" not in without.stderr
        assert "No local drift detected." in without.stderr
        assert "Skills" in with_skills.stderr
        assert f"~ {skill.name}/SKILL.md" in with_skills.stderr or skill.name in with_skills.stderr


class TestBackport:
    def test_an_unknown_kind_exits_1(self, project: Path) -> None:
        result = _commands("backport", "develop.md", "--kind", "widgets", project=project)

        assert result.exit_code == 1
        assert "Unknown kind: widgets. Use commands, guidelines, or skills." in result.stderr

    @pytest.mark.parametrize("kind", ["commands", "guidelines", "skills"])
    def test_a_file_that_is_not_installed_exits_1(self, project: Path, kind: str) -> None:
        result = _commands("backport", "missing.md", "--kind", kind, project=project)

        assert result.exit_code == 1
        assert "File not found:" in result.stderr

    def test_dry_run_shows_the_reversed_content_and_writes_nothing(self, project: Path) -> None:
        _commands("update", project=project)
        develop = project / ".claude" / "commands" / "develop.md"
        develop.write_text(develop.read_text(encoding="utf-8") + "\nBack-port candidate line\n", encoding="utf-8")

        result = _commands("backport", "develop.md", "--dry-run", project=project)

        assert result.exit_code == 0
        assert "Would write to:" in result.stderr
        assert "Back-port candidate line" in result.stderr
        assert "Back-ported" not in result.stderr


class TestSkills:
    def test_skills_list_reports_no_skills_before_an_install(self, project: Path) -> None:
        result = _commands("skills-list", project=project)

        assert result.exit_code == 0
        assert "No skills installed." in result.stderr

    def test_skills_update_installs_skills_that_skills_list_then_names(self, project: Path) -> None:
        update = _commands("skills-update", project=project)
        listing = _commands("skills-list", project=project)
        installed = sorted(path.name for path in (project / ".claude" / "skills").iterdir() if path.is_dir())

        assert update.exit_code == 0
        assert "Updated:" in update.stderr
        assert installed
        assert all(name in listing.stderr for name in installed)

    def test_skills_diff_offers_skills_then_reports_up_to_date(self, project: Path) -> None:
        before = _commands("skills-diff", project=project)
        _commands("skills-update", project=project)
        after = _commands("skills-diff", project=project)

        assert "New skills available:" in before.stderr
        assert "All skills are up to date." in after.stderr

    def test_a_leftover_file_from_a_prior_layout_is_flagged(self, project: Path) -> None:
        _commands("skills-update", project=project)
        skill = next(path for path in sorted((project / ".claude" / "skills").iterdir()) if path.is_dir())
        (skill / "old-layout.md").write_text("leftover\n", encoding="utf-8")

        result = _commands("skills-update", project=project)

        assert "has leftover file(s) from a prior layout" in result.stderr
        assert "old-layout.md" in result.stderr


class TestSync:
    def test_without_registered_projects_it_says_so(self, project: Path) -> None:
        result = runner.invoke(app, ["commands", "sync"])

        assert result.exit_code == 0
        assert "No projects registered. Run 'sova install' first." in result.stderr

    def test_syncs_every_registered_project_and_reports_a_missing_directory(self, tmp_path: Path) -> None:
        first, second, gone = (tmp_path / name for name in ("first", "second", "gone"))
        for directory in (first, second, gone):
            directory.mkdir()
            register_project(directory)
        gone.rmdir()

        result = runner.invoke(app, ["commands", "sync"])

        assert result.exit_code == 0
        assert "first: +" in result.stderr
        assert "second: +" in result.stderr
        assert "gone: directory not found" in result.stderr
        assert "across 3 project(s)" in result.stderr
        assert "develop.md" in _installed(first)
        assert "develop.md" in _installed(second)

    def test_skills_sync_installs_skills_in_every_registered_project(self, tmp_path: Path) -> None:
        first, second = tmp_path / "first", tmp_path / "second"
        for directory in (first, second):
            directory.mkdir()
            register_project(directory)

        result = runner.invoke(app, ["commands", "skills-sync"])

        assert result.exit_code == 0
        assert "across 2 project(s)" in result.stderr
        for directory in (first, second):
            assert any((directory / ".claude" / "skills").glob("*/SKILL.md"))
