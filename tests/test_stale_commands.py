"""Stale managed-command detection (issue #1251).

The #1251 incident ran a squash recipe whose fix had shipped in SOVA but never
reached the project, because nothing noticed that the project's installed copy
of `/address-pr` predated it. A safety-critical command (one that rewrites,
pushes or merges a PR branch) whose installed copy is out of date now blocks
the run, at the supervisor's gate and at `start_command()`, instead of
silently executing the unfixed recipe. A sync that overwrites an installed
command logs it, so the overwrite is traceable.

Staleness is judged against the copy a run would actually execute
(`_resolve_command_prompt()` prefers `.claude/commands/<name>.md`); when no
copy is installed, the run receives the freshly rendered canonical recipe, so
there is nothing stale to block.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from sova.agents.claude_code import ClaudeCodeAdapter
from sova.commands.catalog import get_canonical_dir
from sova.commands.distribution import diff_commands, install_commands, update_commands
from sova.commands.manifest import ManifestEntry, file_hash, read_manifest, write_manifest
from sova.commands.staleness import BRANCH_TEMPLATE_DIR, SAFETY_CRITICAL_COMMANDS, find_stale_command
from sova.config.models import ProjectConfig
from sova.utils.files import read_text_or_none


def _commands_dir(project: Path) -> Path:
    return project / ".claude" / "commands"


@pytest.fixture
def cfg() -> ProjectConfig:
    return ProjectConfig()


@pytest.fixture
def project(tmp_path: Path, cfg: ProjectConfig) -> Path:
    """A project with every canonical command freshly installed."""
    install_commands(get_canonical_dir(), _commands_dir(tmp_path), cfg, adapter=ClaudeCodeAdapter())
    return tmp_path


def _age_manifest_entry(project: Path, filename: str, old_content: str) -> None:
    """Make an installed command look synced from an older canonical version, with no local edits."""
    commands = _commands_dir(project)
    (commands / filename).write_text(old_content, encoding="utf-8")
    manifest = read_manifest(commands)
    assert manifest is not None
    manifest.commands[filename] = ManifestEntry(hash=file_hash(old_content), managed=True)
    write_manifest(commands, manifest)


class TestFindStaleCommand:
    def test_safety_critical_set_covers_every_history_rewriting_command(self) -> None:
        assert SAFETY_CRITICAL_COMMANDS == {"address-pr", "pr", "rearrange-commits", "review-full", "integrate-pr"}
        canonical = {p.stem for p in get_canonical_dir().glob("*.md")}
        assert canonical >= SAFETY_CRITICAL_COMMANDS, "a safety-critical command was renamed or removed"

    @pytest.mark.parametrize("command", sorted(SAFETY_CRITICAL_COMMANDS))
    def test_fresh_install_is_current(self, project: Path, cfg: ProjectConfig, command: str) -> None:
        assert find_stale_command(project, command, cfg) is None

    def test_copy_synced_from_older_canonical_is_stale(self, project: Path, cfg: ProjectConfig) -> None:
        _age_manifest_entry(project, "address-pr.md", "old recipe: git reset --soft origin/$BASE\n")

        reason = find_stale_command(project, "address-pr", cfg)

        assert reason is not None
        assert "/address-pr" in reason
        assert "sova commands update" in reason
        assert str(project) in reason

    def test_installed_copy_without_manifest_is_stale(self, project: Path, cfg: ProjectConfig) -> None:
        """No sync record means no evidence the project ever received the fixed recipe."""
        (_commands_dir(project) / ".sova-manifest.json").unlink()

        reason = find_stale_command(project, "integrate-pr", cfg)

        assert reason is not None
        assert "/integrate-pr" in reason

    def test_installed_copy_missing_from_manifest_is_stale(self, project: Path, cfg: ProjectConfig) -> None:
        commands = _commands_dir(project)
        manifest = read_manifest(commands)
        assert manifest is not None
        del manifest.commands["pr.md"]
        write_manifest(commands, manifest)

        assert find_stale_command(project, "pr", cfg) is not None

    def test_uninstalled_command_is_never_stale(self, tmp_path: Path, cfg: ProjectConfig) -> None:
        """Without an installed copy the run gets the freshly rendered canonical recipe."""
        assert find_stale_command(tmp_path, "address-pr", cfg) is None

    def test_local_customization_of_a_current_copy_is_not_stale(self, project: Path, cfg: ProjectConfig) -> None:
        """A local edit on top of the current canonical is the project's choice, not a missed fix."""
        target = _commands_dir(project) / "rearrange-commits.md"
        target.write_text(target.read_text(encoding="utf-8") + "\nProject-specific note.\n", encoding="utf-8")

        assert find_stale_command(project, "rearrange-commits", cfg) is None

    def test_non_safety_command_is_never_checked(self, project: Path, cfg: ProjectConfig) -> None:
        _age_manifest_entry(project, "standup.md", "old standup\n")

        assert find_stale_command(project, "standup", cfg) is None


class TestBranchCopyStaleness:
    """A slash command resolves from the worktree, so its copy is judged there.

    `ensure_claude_artifacts()` never overwrites a tracked file, so a branch
    that commits `.claude/commands/` and was forked before a recipe fix keeps
    the old copy even when the primary checkout is current. An untracked copy
    is refreshed on reuse, so its message must not send the operator to rebase.
    """

    @pytest.fixture
    def worktree(self, tmp_path: Path, cfg: ProjectConfig) -> Path:
        wt = tmp_path / "worktree"
        install_commands(get_canonical_dir(), _commands_dir(wt), cfg, adapter=ClaudeCodeAdapter())
        return wt

    def test_outdated_branch_copy_is_stale_with_a_rebase_fix(
        self, project: Path, worktree: Path, cfg: ProjectConfig
    ) -> None:
        _age_manifest_entry(worktree, "address-pr.md", "old recipe: git reset --soft origin/$BASE\n")
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        subprocess.run(["git", "add", "-f", ".claude/commands"], cwd=worktree, check=True)

        assert find_stale_command(project, "address-pr", cfg) is None
        reason = find_stale_command(project, "address-pr", cfg, run_dir=worktree)

        assert reason is not None
        assert str(worktree) in reason
        assert "committed on the branch" in reason
        assert "Rebase the branch" in reason
        assert "sova commands update" not in reason

    def test_outdated_untracked_copy_does_not_suggest_a_rebase(
        self, project: Path, worktree: Path, cfg: ProjectConfig
    ) -> None:
        """A project that gitignores `.claude/` holds a copy no rebase can change."""
        _age_manifest_entry(worktree, "address-pr.md", "old recipe\n")
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)

        reason = find_stale_command(project, "address-pr", cfg, run_dir=worktree)

        assert reason is not None
        assert "committed" not in reason
        assert "Rebase" not in reason
        assert "not tracked by the branch" in reason
        assert str(_commands_dir(worktree) / "address-pr.md") in reason

    def test_unanswered_git_does_not_read_as_untracked(self, project: Path, worktree: Path, cfg: ProjectConfig) -> None:
        """A failed `git ls-files` must not tell the operator to delete a file the branch may commit."""
        _age_manifest_entry(worktree, "address-pr.md", "old recipe\n")

        with patch("sova.commands.staleness.git_tracked_paths", return_value=None):
            reason = find_stale_command(project, "address-pr", cfg, run_dir=worktree)

        assert reason is not None
        assert "could not be determined" in reason
        assert "rebase the branch" in reason
        assert "not tracked by the branch" not in reason
        assert ".claude/commands/address-pr.md" in reason

    def test_current_branch_copy_passes_without_asking_git(
        self, project: Path, worktree: Path, cfg: ProjectConfig
    ) -> None:
        """Tracking only picks the message, so a current copy must not pay for a git subprocess."""
        with patch("sova.commands.staleness.git_tracked_paths") as mock_tracked:
            assert find_stale_command(project, "integrate-pr", cfg, run_dir=worktree) is None

        mock_tracked.assert_not_called()

    def test_branch_copy_is_ignored_when_the_primary_checkout_has_none(
        self, tmp_path: Path, worktree: Path, cfg: ProjectConfig
    ) -> None:
        """Without a primary copy the prompt is inlined from SOVA, so the branch copy never runs."""
        primary = tmp_path / "primary"
        primary.mkdir()
        _age_manifest_entry(worktree, "pr.md", "old pr recipe\n")

        assert find_stale_command(primary, "pr", cfg, run_dir=worktree) is None

    def test_worktree_without_an_installed_copy_is_never_stale(
        self, project: Path, tmp_path: Path, cfg: ProjectConfig
    ) -> None:
        """No copy in the worktree means none executes there, whatever state the primary's copy is in."""
        _age_manifest_entry(project, "address-pr.md", "old recipe\n")
        bare = tmp_path / "bare-worktree"
        bare.mkdir()

        assert find_stale_command(project, "address-pr", cfg, run_dir=bare) is None

    def test_run_dir_equal_to_project_dir_keeps_the_sync_fix(self, project: Path, cfg: ProjectConfig) -> None:
        _age_manifest_entry(project, "address-pr.md", "old recipe\n")

        reason = find_stale_command(project, "address-pr", cfg, run_dir=project)

        assert reason is not None
        assert "sova commands update" in reason

    def test_config_is_loaded_from_the_project_not_the_worktree(
        self, project: Path, worktree: Path, cfg: ProjectConfig
    ) -> None:
        """A worktree has no sova.db; loading config there would render with defaults."""
        with patch("sova.config.loader.load_config", return_value=cfg) as mock_load:
            find_stale_command(project, "address-pr", run_dir=worktree)

        mock_load.assert_called_once_with(project)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-f", ".")
    _git(repo, "commit", "-q", "-m", message)


def _sova_checkout(root: Path, cfg: ProjectConfig) -> Path:
    """A SOVA-shaped branch: tracked templates plus the copies rendered from them, forked from `origin/<base>`."""
    wt = root / "sova-worktree"
    templates = wt / BRANCH_TEMPLATE_DIR
    shutil.copytree(get_canonical_dir(), templates)
    install_commands(templates, _commands_dir(wt), cfg, adapter=ClaudeCodeAdapter())
    _git(wt, "init", "-q", "-b", cfg.base_branch)
    _git(wt, "config", "user.name", "test")
    _git(wt, "config", "user.email", "test@example.com")
    _git(wt, "config", "commit.gpgsign", "false")
    _commit_all(wt, "base")
    _git(wt, "update-ref", f"refs/remotes/origin/{cfg.base_branch}", "HEAD")
    _git(wt, "checkout", "-q", "-b", "feat/fix")
    return wt


def _edit_branch_template(wt: Path, cfg: ProjectConfig, *filenames: str) -> None:
    """Change templates on the branch and re-render their copies, the way `make commands-render` does."""
    for filename in filenames:
        template = wt / BRANCH_TEMPLATE_DIR / filename
        template.write_text(template.read_text(encoding="utf-8") + "\nBranch fix.\n", encoding="utf-8")
    update_commands(wt / BRANCH_TEMPLATE_DIR, _commands_dir(wt), cfg, adapter=ClaudeCodeAdapter())
    _commit_all(wt, "branch template fix")


def _land_on_base(wt: Path, cfg: ProjectConfig, filename: str) -> None:
    """Move `origin/<base>` past the fork point with a change to one template, leaving the branch where it is."""
    fork = _git(wt, "rev-parse", f"origin/{cfg.base_branch}")
    _git(wt, "checkout", "-q", "-b", "base-work", fork)
    template = wt / BRANCH_TEMPLATE_DIR / filename
    template.write_text(template.read_text(encoding="utf-8") + "\nBase fix.\n", encoding="utf-8")
    _commit_all(wt, "base template fix")
    _git(wt, "update-ref", f"refs/remotes/origin/{cfg.base_branch}", "HEAD")
    _git(wt, "checkout", "-q", "feat/fix")


class TestBranchTemplateExemption:
    """A branch that fixes a command template carries the fixed copy, which the primary's canonical lacks.

    Comparing that copy with the primary checkout's canonical can only say
    "stale", and rebasing never helps because the fix is the branch's own. The
    copy is current when it matches the branch's own template and the base has
    not changed that template since the fork point. Any doubt keeps it stale.
    """

    @pytest.fixture
    def sova_branch(self, tmp_path: Path, cfg: ProjectConfig) -> Path:
        return _sova_checkout(tmp_path, cfg)

    def test_copy_rendered_from_the_branchs_own_template_edit_is_current(
        self, project: Path, sova_branch: Path, cfg: ProjectConfig
    ) -> None:
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        differs = diff_commands(get_canonical_dir(), _commands_dir(sova_branch), cfg, adapter=ClaudeCodeAdapter())
        assert "address-pr.md" in differs.changed, "the branch copy must differ from the primary canonical"

        with capture_logs() as logs:
            assert find_stale_command(project, "address-pr", cfg, run_dir=sova_branch) is None

        events = [e for e in logs if e["event"] == "staleness.branch_template_current"]
        assert [e["filename"] for e in events] == ["address-pr.md"]

    def test_base_change_to_the_same_template_keeps_the_rebase_message(
        self, project: Path, sova_branch: Path, cfg: ProjectConfig
    ) -> None:
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        _land_on_base(sova_branch, cfg, "address-pr.md")

        reason = find_stale_command(project, "address-pr", cfg, run_dir=sova_branch)

        assert reason is not None
        assert "committed on the branch" in reason
        assert "Rebase the branch" in reason

    def test_base_change_to_another_template_leaves_each_copy_judged_on_its_own_path(
        self, project: Path, sova_branch: Path, cfg: ProjectConfig
    ) -> None:
        _edit_branch_template(sova_branch, cfg, "address-pr.md", "pr.md")
        _land_on_base(sova_branch, cfg, "pr.md")

        assert find_stale_command(project, "address-pr", cfg, run_dir=sova_branch) is None
        reason = find_stale_command(project, "pr", cfg, run_dir=sova_branch)
        assert reason is not None
        assert "Rebase the branch" in reason

    def test_copy_matching_neither_the_branch_template_nor_the_canonical_stays_stale(
        self, project: Path, sova_branch: Path, cfg: ProjectConfig
    ) -> None:
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        _age_manifest_entry(sova_branch, "address-pr.md", "recipe from neither source\n")
        _commit_all(sova_branch, "stale copy")

        reason = find_stale_command(project, "address-pr", cfg, run_dir=sova_branch)

        assert reason is not None
        assert "Rebase the branch" in reason

    def test_copy_with_no_manifest_entry_stays_stale(
        self, project: Path, sova_branch: Path, cfg: ProjectConfig
    ) -> None:
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        commands = _commands_dir(sova_branch)
        manifest = read_manifest(commands)
        assert manifest is not None
        del manifest.commands["address-pr.md"]
        write_manifest(commands, manifest)

        assert find_stale_command(project, "address-pr", cfg, run_dir=sova_branch) is not None

    def test_missing_base_ref_stays_stale(self, project: Path, sova_branch: Path, cfg: ProjectConfig) -> None:
        """No `origin/<base>` means no way to prove the base left the template alone."""
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        _git(sova_branch, "update-ref", "-d", f"refs/remotes/origin/{cfg.base_branch}")

        reason = find_stale_command(project, "address-pr", cfg, run_dir=sova_branch)

        assert reason is not None
        assert "Rebase the branch" in reason

    def test_unrelated_histories_stay_stale(self, project: Path, sova_branch: Path, cfg: ProjectConfig) -> None:
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        _git(sova_branch, "checkout", "-q", "--orphan", "unrelated")
        _git(sova_branch, "commit", "-q", "--allow-empty", "-m", "unrelated root")
        _git(sova_branch, "update-ref", f"refs/remotes/origin/{cfg.base_branch}", "HEAD")
        _git(sova_branch, "checkout", "-q", "-f", "feat/fix")

        assert find_stale_command(project, "address-pr", cfg, run_dir=sova_branch) is not None

    def test_base_ref_follows_the_configured_base_branch(self, tmp_path: Path, project: Path) -> None:
        develop = ProjectConfig(base_branch="develop")
        wt = _sova_checkout(tmp_path, develop)
        _edit_branch_template(wt, develop, "address-pr.md")
        assert find_stale_command(project, "address-pr", develop, run_dir=wt) is None

        _land_on_base(wt, develop, "address-pr.md")

        assert find_stale_command(project, "address-pr", develop, run_dir=wt) is not None

    def test_base_fix_in_the_canonical_stays_stale_when_the_base_ref_lags(
        self, project: Path, sova_branch: Path, tmp_path: Path, cfg: ProjectConfig
    ) -> None:
        """`origin/<base>` is never fetched, so it can still equal the fork point after the base fixed the template.

        The base fix has reached the primary checkout's canonical template, but
        the remote-tracking ref shows nothing changed since the fork. The ref
        alone would clear the branch's older copy; the canonical must agree too.
        """
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        advanced = tmp_path / "advanced-canonical"
        shutil.copytree(get_canonical_dir(), advanced)
        fixed = advanced / "address-pr.md"
        fixed.write_text(fixed.read_text(encoding="utf-8") + "\nBase fix.\n", encoding="utf-8")

        with (
            capture_logs() as logs,
            patch("sova.commands.staleness.get_canonical_dir", return_value=advanced),
        ):
            reason = find_stale_command(project, "address-pr", cfg, run_dir=sova_branch)

        assert reason is not None
        assert "Rebase the branch" in reason
        # Distinguishes this from a plain base change in the logs: the rebase advice
        # does not cover a primary checkout that is itself behind the fork point.
        differs = [e for e in logs if e["event"] == "staleness.canonical_differs_from_fork"]
        assert [e["template"] for e in differs] == [(BRANCH_TEMPLATE_DIR / "address-pr.md").as_posix()]

    @pytest.mark.parametrize("command", ["rev-parse", "hash-object"])
    @pytest.mark.parametrize("failure", ["exit", "raise"])
    def test_unanswered_template_comparison_stays_stale(
        self, project: Path, sova_branch: Path, cfg: ProjectConfig, command: str, failure: str
    ) -> None:
        """Neither blob id can be read, or git cannot run: no proof the canonical matches the fork, so no exemption."""
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        real_run = subprocess.run

        def fail_command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["git", command]:
                if failure == "raise":
                    raise subprocess.TimeoutExpired(args, 10)
                return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal")
            return real_run(args, **kwargs)  # type: ignore[call-overload,no-any-return]

        with (
            capture_logs() as logs,
            patch("sova.commands.staleness.subprocess.run", side_effect=fail_command),
        ):
            reason = find_stale_command(project, "address-pr", cfg, run_dir=sova_branch)

        assert reason is not None
        assert "Rebase the branch" in reason
        failed = [e["args"][0] for e in logs if e["event"] == "staleness.git_failed"]
        assert failed == ([command] if failure == "raise" else [])

    def test_diff_exit_code_other_than_zero_or_one_stays_stale(
        self, project: Path, sova_branch: Path, cfg: ProjectConfig
    ) -> None:
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        real_run = subprocess.run

        def fail_diff(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["git", "diff"]:
                return subprocess.CompletedProcess(args, 129, stdout="", stderr="usage")
            return real_run(args, **kwargs)  # type: ignore[call-overload,no-any-return]

        with patch("sova.commands.staleness.subprocess.run", side_effect=fail_diff):
            assert find_stale_command(project, "address-pr", cfg, run_dir=sova_branch) is not None

    def test_git_that_cannot_run_stays_stale(self, project: Path, sova_branch: Path, cfg: ProjectConfig) -> None:
        """Only merge-base fails, so the template is still read as tracked and the failure is the fork-point lookup."""
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        real_run = subprocess.run

        def fail_merge_base(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["git", "merge-base"]:
                raise subprocess.TimeoutExpired(args, 10)
            return real_run(args, **kwargs)  # type: ignore[call-overload,no-any-return]

        with (
            capture_logs() as logs,
            patch("sova.commands.staleness.subprocess.run", side_effect=fail_merge_base),
        ):
            reason = find_stale_command(project, "address-pr", cfg, run_dir=sova_branch)

        assert reason is not None
        assert "Rebase the branch" in reason
        assert [e["args"][0] for e in logs if e["event"] == "staleness.git_failed"] == ["merge-base"]

    def test_unanswered_ls_files_stays_stale_with_the_unanswered_message(
        self, project: Path, sova_branch: Path, cfg: ProjectConfig
    ) -> None:
        _edit_branch_template(sova_branch, cfg, "address-pr.md")

        with patch("sova.commands.staleness.git_tracked_paths", return_value=None):
            reason = find_stale_command(project, "address-pr", cfg, run_dir=sova_branch)

        assert reason is not None
        assert "could not be determined" in reason

    def test_untracked_template_stays_stale(self, project: Path, sova_branch: Path, cfg: ProjectConfig) -> None:
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        _git(sova_branch, "rm", "-q", "--cached", f"{BRANCH_TEMPLATE_DIR.as_posix()}/address-pr.md")

        assert find_stale_command(project, "address-pr", cfg, run_dir=sova_branch) is not None

    def test_template_without_frontmatter_stays_stale(
        self, project: Path, sova_branch: Path, cfg: ProjectConfig
    ) -> None:
        """An unparseable template is skipped by the comparison, which must not read as "nothing changed"."""
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        (sova_branch / BRANCH_TEMPLATE_DIR / "address-pr.md").write_text("no frontmatter\n", encoding="utf-8")
        _commit_all(sova_branch, "break template")

        assert find_stale_command(project, "address-pr", cfg, run_dir=sova_branch) is not None

    def test_template_with_non_utf8_bytes_stays_stale(
        self, project: Path, sova_branch: Path, cfg: ProjectConfig
    ) -> None:
        """The real unreadable-template path: the comparison skips the file, which must not read as a match."""
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        (sova_branch / BRANCH_TEMPLATE_DIR / "address-pr.md").write_bytes(b"---\nname: x\n---\n\xff\xfe")
        _commit_all(sova_branch, "corrupt template")

        with capture_logs() as logs:
            reason = find_stale_command(project, "address-pr", cfg, run_dir=sova_branch)

        assert reason is not None
        assert "Rebase the branch" in reason
        assert not [e for e in logs if e["event"] == "staleness.branch_template_current"]

    def test_template_that_becomes_unreadable_during_the_comparison_stays_stale(
        self, project: Path, sova_branch: Path, cfg: ProjectConfig
    ) -> None:
        """Readable when discovered but not when hashed: skipped by the diff, so there is no positive match."""
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        template = sova_branch / BRANCH_TEMPLATE_DIR / "address-pr.md"
        real_read = read_text_or_none

        def unreadable_template(path: Path) -> str | None:
            return None if path == template else real_read(path)

        with patch("sova.commands.distribution.read_text_or_none", side_effect=unreadable_template):
            reason = find_stale_command(project, "address-pr", cfg, run_dir=sova_branch)

        assert reason is not None
        assert "Rebase the branch" in reason

    def test_comparison_that_raises_stays_stale_without_raising(
        self, project: Path, sova_branch: Path, cfg: ProjectConfig
    ) -> None:
        """Defensive floor only: the real diff swallows unreadable files, so this guards a future regression there."""
        _edit_branch_template(sova_branch, cfg, "address-pr.md")
        # The first call is the primary comparison, which must keep working; only the branch one fails.
        primary = diff_commands(get_canonical_dir(), _commands_dir(sova_branch), cfg, adapter=ClaudeCodeAdapter())

        with (
            capture_logs() as logs,
            patch("sova.commands.staleness.diff_commands", side_effect=[primary, OSError("disk gone")]),
        ):
            reason = find_stale_command(project, "address-pr", cfg, run_dir=sova_branch)

        assert reason is not None
        assert [e["filename"] for e in logs if e["event"] == "staleness.branch_template_unreadable"] == [
            "address-pr.md"
        ]

    def test_checkout_without_the_template_directory_is_unchanged(
        self, project: Path, tmp_path: Path, cfg: ProjectConfig
    ) -> None:
        """A non-SOVA project has nothing to compare against, so the exemption never asks git anything."""
        wt = tmp_path / "plain-worktree"
        install_commands(get_canonical_dir(), _commands_dir(wt), cfg, adapter=ClaudeCodeAdapter())
        _age_manifest_entry(wt, "address-pr.md", "old recipe\n")
        _git(wt, "init", "-q")
        _git(wt, "add", "-f", ".claude/commands")
        real_run = subprocess.run
        git_calls: list[list[str]] = []

        def record(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            git_calls.append(args[:2])
            return real_run(args, **kwargs)  # type: ignore[call-overload,no-any-return]

        with patch("sova.commands.staleness.subprocess.run", side_effect=record):
            reason = find_stale_command(project, "address-pr", cfg, run_dir=wt)

        assert reason is not None
        assert "Rebase the branch" in reason
        assert ["git", "merge-base"] not in git_calls

    def test_template_directory_constant_matches_the_packaged_layout(self) -> None:
        """A directory move must not silently disable the exemption for this repository."""
        repo_root = Path(__file__).resolve().parents[1]

        assert (repo_root / BRANCH_TEMPLATE_DIR).resolve() == get_canonical_dir().resolve()


class TestStaleCommandsGate:
    def test_stale_command_blocks(self, project: Path, cfg: ProjectConfig) -> None:
        from sova.supervisor.gates.stale_commands import check_stale_commands_gate

        _age_manifest_entry(project, "address-pr.md", "old recipe\n")

        block = check_stale_commands_gate(project, "address-pr", cfg)

        assert block is not None
        assert block.gate == "stale_commands"
        assert "/address-pr" in block.detail

    def test_current_command_passes(self, project: Path, cfg: ProjectConfig) -> None:
        from sova.supervisor.gates.stale_commands import check_stale_commands_gate

        assert check_stale_commands_gate(project, "integrate-pr", cfg) is None

    def test_unevaluable_gate_fails_open(self, project: Path, cfg: ProjectConfig) -> None:
        from sova.supervisor.gates.stale_commands import check_stale_commands_gate

        with patch("sova.supervisor.gates.stale_commands.find_stale_command", side_effect=RuntimeError("boom")):
            assert check_stale_commands_gate(project, "address-pr", cfg) is None


class TestStaleCommandPreSpawnCheck:
    def test_stale_command_blocks(self, project: Path, cfg: ProjectConfig) -> None:
        from sova.dashboard.services.agent_validation import check_stale_command

        _age_manifest_entry(project, "rearrange-commits.md", "old recipe\n")

        with patch("sova.config.loader.load_config", return_value=cfg):
            block = check_stale_command(project, "rearrange-commits")

        assert block is not None
        assert block["code"] == "stale_command"
        assert "/rearrange-commits" in block["error"]

    def test_current_command_passes(self, project: Path, cfg: ProjectConfig) -> None:
        from sova.dashboard.services.agent_validation import check_stale_command

        with patch("sova.config.loader.load_config", return_value=cfg):
            assert check_stale_command(project, "review-full") is None

    def test_non_safety_command_skips_config_load(self, project: Path) -> None:
        """Every dashboard command passes through here: only the safety-critical ones may pay for a config load."""
        from sova.dashboard.services.agent_validation import check_stale_command

        with patch("sova.config.loader.load_config") as mock_load:
            assert check_stale_command(project, "agent-resume") is None
        mock_load.assert_not_called()

    def test_unevaluable_check_fails_open(self, project: Path) -> None:
        from sova.dashboard.services.agent_validation import check_stale_command

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("boom")):
            assert check_stale_command(project, "address-pr") is None


class TestStartCommandStaleBlock:
    async def test_start_command_returns_block_without_spawning(self) -> None:
        from sova.dashboard.services.agent_lifecycle import start_command

        block = {"code": "stale_command", "error": "/address-pr is out of date"}
        capture_sha = AsyncMock()
        with (
            patch(
                "sova.dashboard.services.agent_lifecycle._get_project_agents",
                return_value=MagicMock(project_dir=Path("/tmp")),
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_command_context",
                new_callable=AsyncMock,
                return_value=(7, "42"),
            ),
            patch("sova.dashboard.services.agent_lifecycle.check_memory_pressure", return_value=(None, None)),
            patch("sova.dashboard.services.agent_lifecycle.check_network_connectivity", return_value=(None, None)),
            patch("sova.dashboard.services.agent_lifecycle.check_stale_command", return_value=block) as mock_stale,
            patch("sova.dashboard.services.agent_db._capture_pr_head_sha", capture_sha),
        ):
            result = await start_command("address-pr", {"pr": 7})

        assert result == block
        mock_stale.assert_called_once_with(Path("/tmp"), "address-pr")
        capture_sha.assert_not_awaited()

    async def test_start_command_blocks_an_outdated_copy_on_the_worktree_branch(self, tmp_path: Path) -> None:
        import asyncio

        from sova.dashboard.services.agent_lifecycle import start_command

        project_dir = tmp_path / "primary"
        worktree = tmp_path / "worktree"
        project_dir.mkdir()
        worktree.mkdir()
        pa = MagicMock(project_dir=project_dir, agents={}, max_concurrent=3, _lock=asyncio.Lock())
        block = {"code": "stale_command", "error": "branch copy of /address-pr is out of date"}
        create_run = AsyncMock()
        with (
            patch("sova.dashboard.services.agent_lifecycle._get_project_agents", return_value=pa),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_command_context",
                new_callable=AsyncMock,
                return_value=(7, "42"),
            ),
            patch("sova.dashboard.services.agent_lifecycle.check_memory_pressure", return_value=(None, None)),
            patch("sova.dashboard.services.agent_lifecycle.check_network_connectivity", return_value=(None, None)),
            patch(
                "sova.dashboard.services.agent_lifecycle.check_stale_command", side_effect=[None, block]
            ) as mock_stale,
            patch("sova.dashboard.services.agent_db._capture_pr_head_sha", AsyncMock(return_value="abc")),
            patch("sova.dashboard.services.agent_lifecycle._check_issue_conflict", AsyncMock(return_value=None)),
            patch("sova.dashboard.services.agent_lifecycle._evict_completed_for_issue"),
            patch("sova.dashboard.services.agent_lifecycle._resolve_branch_name", AsyncMock(return_value="feat/x")),
            patch("sova.dashboard.services.agent_lifecycle._resolve_issue_worktree", AsyncMock(return_value=worktree)),
            patch("sova.dashboard.services.agent_lifecycle._create_task_run", create_run),
        ):
            result = await start_command("address-pr", {"pr": 7})

        assert result == block
        assert mock_stale.call_args_list[1].args == (project_dir, "address-pr", worktree)
        create_run.assert_not_awaited()


class TestSyncOverwriteLog:
    def test_overwriting_an_out_of_date_copy_is_logged(self, project: Path, cfg: ProjectConfig) -> None:
        old = "old recipe\n"
        _age_manifest_entry(project, "address-pr.md", old)

        with capture_logs() as logs:
            result = update_commands(get_canonical_dir(), _commands_dir(project), cfg, adapter=ClaudeCodeAdapter())

        assert "address-pr.md" in result.updated_files
        events = [e for e in logs if e["event"] == "commands.sync.overwritten"]
        assert len(events) == 1
        assert events[0]["filename"] == "address-pr.md"
        assert events[0]["old_hash"] == file_hash(old)
        new_text = (_commands_dir(project) / "address-pr.md").read_text(encoding="utf-8")
        assert events[0]["new_hash"] == file_hash(new_text)

    def test_up_to_date_sync_logs_no_overwrite(self, project: Path, cfg: ProjectConfig) -> None:
        with capture_logs() as logs:
            update_commands(get_canonical_dir(), _commands_dir(project), cfg, adapter=ClaudeCodeAdapter())

        assert not [e for e in logs if e["event"] == "commands.sync.overwritten"]

    def test_forced_overwrite_of_an_untracked_copy_is_logged(self, project: Path, cfg: ProjectConfig) -> None:
        commands = _commands_dir(project)
        custom = "hand-written pr recipe\n"
        (commands / "pr.md").write_text(custom, encoding="utf-8")
        manifest = read_manifest(commands)
        assert manifest is not None
        del manifest.commands["pr.md"]
        write_manifest(commands, manifest)

        with capture_logs() as logs:
            update_commands(get_canonical_dir(), commands, cfg, force=True, adapter=ClaudeCodeAdapter())

        events = [e for e in logs if e["event"] == "commands.sync.overwritten"]
        assert [e["filename"] for e in events] == ["pr.md"]
        assert events[0]["old_hash"] == file_hash(custom)
