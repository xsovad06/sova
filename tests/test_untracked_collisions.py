"""Untracked files that the target ref also tracks must not abort a sync, rebase or stash pop (review of #1402).

Every test runs against a real repository with a real ``origin``: the failures are git's own refusals, which a
mocked ``run`` cannot reproduce. The same holds for runtime paths git already ignores, which must not make the
stash itself fail.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from sova.git import untracked_collisions
from sova.git.artifact_exclusions import INFO_EXCLUDE_ENTRIES, STASH_EXCLUDED_PATHS, stash_pathspecs
from sova.git.branch import sync_branch
from sova.git.rebase import rebase_with_conflict_resolution
from sova.git.untracked_collisions import (
    DISPLACED_SPECS_DIR,
    clear_conflicting_runtime_files,
    runtime_stash_pathspecs,
    set_aside_conflicting_specs,
)
from tests.conftest import InvariantRepo

SPEC = ".claude/specs/7-thing.md"
BASELINE = ".sova/test-baseline.json"


@pytest.fixture
def repo(invariant_repo: InvariantRepo, monkeypatch: pytest.MonkeyPatch) -> InvariantRepo:
    """The repo with git's isolated config and identity exported to the process, as sync_branch reads os.environ."""
    for key, value in invariant_repo.env.items():
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)
    return invariant_repo


def _publish_then_rewind(repo: InvariantRepo, path: str, content: str) -> None:
    """Land *path* on origin/main, then rewind the local main so the file is absent locally."""
    repo.write("base.txt", "base\n")
    repo.land_on_main("chore(docs): base")
    repo.write(path, content)
    repo.commit("chore(docs): publish file")
    repo.git("push", "-q", "origin", "main")
    repo.git("reset", "-q", "--hard", "HEAD~1")


def _displaced(work: Path) -> list[Path]:
    directory = work / DISPLACED_SPECS_DIR
    return sorted(directory.iterdir()) if directory.exists() else []


class TestDifferingUntrackedSpec:
    async def test_sync_moves_the_local_copy_aside_and_leaves_no_stash_entry(self, repo: InvariantRepo) -> None:
        """The reviewer appends `## Review Rationale` in the worktree, so the local copy rarely matches origin's."""
        work = repo.dir
        _publish_then_rewind(repo, SPEC, "spec\n\n## Review Rationale\nmerged\n")
        (work / ".claude" / "specs").mkdir(parents=True, exist_ok=True)
        (work / SPEC).write_text("spec\n", encoding="utf-8")
        (work / "notes.txt").write_text("operator untracked\n", encoding="utf-8")

        await sync_branch("main", cwd=work)

        assert repo.git("stash", "list") == ""
        assert (work / SPEC).read_text(encoding="utf-8") == "spec\n\n## Review Rationale\nmerged\n"
        moved = _displaced(work)
        assert len(moved) == 1
        assert moved[0].read_text(encoding="utf-8") == "spec\n"
        assert (work / "notes.txt").read_text(encoding="utf-8") == "operator untracked\n"
        assert repo.git("status", "--porcelain", "--", ".claude/specs") == ""

    async def test_identical_copy_is_deleted_not_moved(self, repo: InvariantRepo) -> None:
        work = repo.dir
        _publish_then_rewind(repo, SPEC, "spec\n")
        (work / ".claude" / "specs").mkdir(parents=True, exist_ok=True)
        (work / SPEC).write_text("spec\n", encoding="utf-8")

        moved = await set_aside_conflicting_specs("origin/main", work)

        assert moved == []
        assert not (work / SPEC).exists()
        assert _displaced(work) == []

    async def test_spec_origin_does_not_track_is_left_alone(self, repo: InvariantRepo) -> None:
        work = repo.dir
        (work / ".claude" / "specs").mkdir(parents=True, exist_ok=True)
        (work / SPEC).write_text("local only\n", encoding="utf-8")

        assert await set_aside_conflicting_specs("origin/main", work) == []
        assert (work / SPEC).read_text(encoding="utf-8") == "local only\n"

    async def test_several_specs_are_each_handled(self, repo: InvariantRepo) -> None:
        work = repo.dir
        repo.write("base.txt", "base\n")
        repo.land_on_main("chore(docs): base")
        repo.write(".claude/specs/1-a.md", "a\n")
        repo.write(".claude/specs/2-b.md", "b upstream\n")
        repo.commit("chore(docs): publish specs")
        repo.git("push", "-q", "origin", "main")
        repo.git("reset", "-q", "--hard", "HEAD~1")
        (work / ".claude" / "specs").mkdir(parents=True, exist_ok=True)
        (work / ".claude/specs/1-a.md").write_text("a\n", encoding="utf-8")
        (work / ".claude/specs/2-b.md").write_text("b local\n", encoding="utf-8")

        moved = await set_aside_conflicting_specs("origin/main", work)

        assert len(moved) == 1
        assert (work / moved[0]).read_text(encoding="utf-8") == "b local\n"
        assert not (work / ".claude/specs/1-a.md").exists()
        assert not (work / ".claude/specs/2-b.md").exists()


class TestLeakedRuntimeFile:
    async def test_sync_removes_an_untracked_baseline_that_origin_tracks(self, repo: InvariantRepo) -> None:
        """The leak this issue fixes: once a PR carrying the baseline merges, every untracked copy collides."""
        work = repo.dir
        _publish_then_rewind(repo, BASELINE, '{"origin": true}\n')
        (work / ".sova").mkdir(exist_ok=True)
        (work / BASELINE).write_text('{"local": true}\n', encoding="utf-8")
        (work / "notes.txt").write_text("operator untracked\n", encoding="utf-8")

        await sync_branch("main", cwd=work)

        assert (work / BASELINE).read_text(encoding="utf-8") == '{"origin": true}\n'
        assert (work / "notes.txt").read_text(encoding="utf-8") == "operator untracked\n"
        assert repo.git("stash", "list") == ""

    async def test_sync_refuses_to_overwrite_the_database_and_names_it(self, repo: InvariantRepo) -> None:
        work = repo.dir
        _publish_then_rewind(repo, ".claude/sova.db", "leaked\n")
        (work / ".claude").mkdir(exist_ok=True)
        (work / ".claude" / "sova.db").write_text("live", encoding="utf-8")
        (work / "notes.txt").write_text("operator untracked\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match=r"\.claude/sova\.db"):
            await sync_branch("main", cwd=work)

        assert (work / ".claude" / "sova.db").read_text(encoding="utf-8") == "live"
        assert (work / "notes.txt").read_text(encoding="utf-8") == "operator untracked\n"
        assert repo.git("stash", "list") == ""

    async def test_untracked_runtime_file_origin_does_not_track_is_untouched(self, repo: InvariantRepo) -> None:
        work = repo.dir
        (work / ".sova").mkdir(exist_ok=True)
        (work / BASELINE).write_text("{}\n", encoding="utf-8")

        assert await clear_conflicting_runtime_files("origin/main", work) == []
        assert (work / BASELINE).exists()

    async def test_unknown_ref_is_not_an_error(self, repo: InvariantRepo) -> None:
        """A failed lookup defers to the caller's own git command, which reports the real error."""
        work = repo.dir
        (work / ".sova").mkdir(exist_ok=True)
        (work / BASELINE).write_text("{}\n", encoding="utf-8")

        assert await clear_conflicting_runtime_files("origin/missing", work) == []
        assert (work / BASELINE).exists()

    async def test_rebase_clears_an_untracked_baseline_the_base_branch_picked_up(self, repo: InvariantRepo) -> None:
        work = repo.dir
        repo.write("base.txt", "base\n")
        repo.land_on_main("chore(docs): base")
        repo.start_branch("feat/work")
        repo.write("feature.txt", "feature\n")
        repo.commit("feat(core): feature")
        repo.git("checkout", "-q", "main")
        repo.write(BASELINE, '{"origin": true}\n')
        repo.land_on_main("chore(docs): base picks up the leaked baseline")
        repo.git("checkout", "-q", "feat/work")
        (work / ".sova").mkdir(exist_ok=True)
        (work / BASELINE).write_text('{"local": true}\n', encoding="utf-8")

        result, _cost = await rebase_with_conflict_resolution("main", cwd=work)

        assert result.success, result.error
        assert (work / BASELINE).read_text(encoding="utf-8") == '{"origin": true}\n'
        assert repo.git("stash", "list") == ""

    async def test_rebase_reports_a_tracked_database_by_name(self, repo: InvariantRepo) -> None:
        work = repo.dir
        repo.write("base.txt", "base\n")
        repo.land_on_main("chore(docs): base")
        repo.start_branch("feat/work")
        repo.write("feature.txt", "feature\n")
        repo.commit("feat(core): feature")
        repo.git("checkout", "-q", "main")
        repo.write(".claude/sova.db", "leaked\n")
        repo.land_on_main("chore(docs): base tracks a database")
        repo.git("checkout", "-q", "feat/work")
        (work / ".claude").mkdir(exist_ok=True)
        (work / ".claude" / "sova.db").write_text("live", encoding="utf-8")

        result, _cost = await rebase_with_conflict_resolution("main", cwd=work)

        assert not result.success
        assert ".claude/sova.db" in (result.error or "")
        assert (work / ".claude" / "sova.db").read_text(encoding="utf-8") == "live"


class TestDashboardBranchFreeing:
    async def test_ignored_database_the_default_branch_tracks_is_never_overwritten(self, repo: InvariantRepo) -> None:
        """`sova install` ignores the database, so `git status` looks clean and git would overwrite it silently."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        work = repo.dir
        repo.write("base.txt", "base\n")
        repo.land_on_main("chore(docs): base")
        repo.start_branch("feat/work")
        repo.write("feature.txt", "feature\n")
        repo.commit("feat(core): feature")
        repo.git("checkout", "-q", "main")
        repo.write(".claude/sova.db", "leaked\n")
        repo.land_on_main("chore(docs): default branch tracks a database")
        repo.git("checkout", "-q", "feat/work")
        (work / ".claude").mkdir(exist_ok=True)
        (work / ".claude" / "sova.db").write_text("live", encoding="utf-8")
        exclude = work / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text("/.claude/sova.db*\n", encoding="utf-8")
        assert repo.git("status", "--porcelain") == ""

        create = AsyncMock(return_value=SimpleNamespace(path=work / "wt"))
        with (
            patch("sova.dashboard.services.agent_context.find_worktree_by_branch", AsyncMock(return_value=work)),
            patch("sova.git.worktree.create_worktree", create),
        ):
            result = await _resolve_issue_worktree("", work, branch_name="feat/work", pr_number=99)

        assert result == work
        create.assert_not_awaited()
        assert (work / ".claude" / "sova.db").read_text(encoding="utf-8") == "live"
        assert repo.git("branch", "--show-current") == "feat/work"


# No `sova.db-wal`: the rebase loads SOVA's config, and SQLite deletes a WAL beside a database it cannot read.
RUNTIME_FILES = (
    ".claude/sova.db",
    ".claude/sova.log",
    ".claude/agent-control/state.json",
    ".claude/mcp_secret",
    BASELINE,
)


def _install_ignore_rules_and_runtime_files(work: Path) -> None:
    """Write the `.git/info/exclude` entries `sova install` writes, and the runtime files a live server keeps."""
    exclude = work / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text("".join(f"{entry}\n" for entry in INFO_EXCLUDE_ENTRIES), encoding="utf-8")
    for rel in RUNTIME_FILES:
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text("live", encoding="utf-8")


def _assert_runtime_files_untouched(work: Path) -> None:
    for rel in RUNTIME_FILES:
        assert (work / rel).read_text(encoding="utf-8") == "live", rel


class TestIgnoredRuntimePaths:
    """A pathspec stash whose exclude names an ignored path that exists fails after writing the stash entry.

    `sova install` ignores every runtime path, so before the excludes were pruned, every sync with operator changes
    failed and stranded one stash entry per attempt.
    """

    async def test_drops_only_the_excludes_git_would_refuse(self, repo: InvariantRepo) -> None:
        work = repo.dir
        _install_ignore_rules_and_runtime_files(work)
        repo.write("staged.txt", "staged\n")
        repo.git("add", "staged.txt")

        pathspecs = await runtime_stash_pathspecs(STASH_EXCLUDED_PATHS, work)

        # The runtime files that exist are ignored, so their excludes go; the absent ones are kept.
        assert ":(exclude).claude/sova.db*" not in pathspecs
        assert ":(exclude).claude/agent-control/" not in pathspecs
        assert ":(exclude).sova/" not in pathspecs
        assert ":(exclude).claude/worktrees/" in pathspecs
        repo.git("add", "--dry-run", "--", *pathspecs)  # raises if git still refuses an exclude

    async def test_nothing_ignored_keeps_every_exclude(self, repo: InvariantRepo) -> None:
        (repo.dir / ".claude").mkdir()
        (repo.dir / ".claude" / "sova.db").write_text("live", encoding="utf-8")

        assert await runtime_stash_pathspecs(STASH_EXCLUDED_PATHS, repo.dir) == stash_pathspecs(STASH_EXCLUDED_PATHS)

    async def test_probe_failure_raises_instead_of_guessing(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="probe failed"):
            await runtime_stash_pathspecs(STASH_EXCLUDED_PATHS, tmp_path)

    async def test_scan_failure_raises(self, repo: InvariantRepo) -> None:
        _install_ignore_rules_and_runtime_files(repo.dir)
        real_run = untracked_collisions.run

        async def failing_scan(*args: str, **kwargs: Any) -> Any:
            if args[:2] == ("git", "ls-files"):
                return SimpleNamespace(success=False, stdout="", stderr="boom")
            return await real_run(*args, **kwargs)

        with (
            patch("sova.git.untracked_collisions.run", side_effect=failing_scan),
            pytest.raises(RuntimeError, match="ls-files scan failed"),
        ):
            await runtime_stash_pathspecs(STASH_EXCLUDED_PATHS, repo.dir)

    async def test_sync_refuses_a_modified_tracked_database_before_stashing(self, repo: InvariantRepo) -> None:
        """No exclude can keep a tracked file under an ignored directory out of the stash, so the sync stops."""
        work = repo.dir
        repo.write(".gitignore", ".claude/\n")
        repo.write(".claude/sova.db", "committed")
        repo.git("add", "-f", ".claude/sova.db")
        repo.land_on_main("chore(docs): a leaked database")
        _install_ignore_rules_and_runtime_files(work)
        repo.write("staged.txt", "staged\n")
        repo.git("add", "staged.txt")

        with pytest.raises(RuntimeError, match=r"\.claude/sova\.db"):
            await sync_branch("main", cwd=work)

        assert repo.git("stash", "list") == ""
        assert (work / ".claude" / "sova.db").read_text(encoding="utf-8") == "live"

    async def test_rebase_reports_a_failed_probe_without_stashing(self, repo: InvariantRepo) -> None:
        repo.write("base.txt", "base\n")
        repo.land_on_main("chore(docs): base")
        repo.start_branch("feat/work")
        _install_ignore_rules_and_runtime_files(repo.dir)

        with patch("sova.git.rebase.runtime_stash_pathspecs", AsyncMock(side_effect=RuntimeError("probe failed"))):
            result, _cost = await rebase_with_conflict_resolution("main", cwd=repo.dir)

        assert not result.success
        assert "probe failed" in (result.error or "")
        assert repo.git("stash", "list") == ""

    async def test_sync_leaves_an_unignored_wal_in_place(self, repo: InvariantRepo) -> None:
        """A `.gitignore` naming only `.claude/sova.db` drops that exclude, but its WAL must still stay out."""
        import sova.git.branch as branch_module

        work = repo.dir
        repo.write(".gitignore", ".claude/sova.db\n")
        repo.land_on_main("chore(docs): ignore the database")
        (work / ".claude").mkdir()
        (work / ".claude" / "sova.db").write_text("live", encoding="utf-8")
        (work / ".claude" / "sova.db-wal").write_text("wal", encoding="utf-8")
        repo.write("staged.txt", "staged\n")
        repo.git("add", "staged.txt")

        wal_during_stash: list[bool] = []
        real_run = branch_module.run

        async def spy_run(*args: str, **kwargs: Any) -> Any:
            if args[:2] == ("git", "checkout"):
                wal_during_stash.append((work / ".claude" / "sova.db-wal").exists())
            return await real_run(*args, **kwargs)

        with patch("sova.git.branch.run", side_effect=spy_run):
            await sync_branch("main", cwd=work)

        assert wal_during_stash == [True]
        assert repo.git("stash", "list") == ""
        assert repo.git("diff", "--cached", "--name-only") == "staged.txt"

    async def test_a_wal_created_after_discovery_stays_out_of_the_stash(self, repo: InvariantRepo) -> None:
        """The sidecar exclude is a pattern, not the file list scanned earlier, so a later WAL is covered too."""
        work = repo.dir
        repo.write(".gitignore", ".claude/sova.db\n")
        repo.land_on_main("chore(docs): ignore the database")
        (work / ".claude").mkdir()
        (work / ".claude" / "sova.db").write_text("live", encoding="utf-8")
        repo.write("staged.txt", "staged\n")
        repo.git("add", "staged.txt")

        pathspecs = await runtime_stash_pathspecs(STASH_EXCLUDED_PATHS, work)
        (work / ".claude" / "sova.db-wal").write_text("wal", encoding="utf-8")
        repo.git("stash", "--include-untracked", "--", *pathspecs)

        assert (work / ".claude" / "sova.db-wal").read_text(encoding="utf-8") == "wal"
        assert ":(exclude,glob).claude/sova.db-*" in pathspecs

    async def test_a_staged_only_tracked_runtime_file_is_refused(self, repo: InvariantRepo) -> None:
        """The callers unstage first, but a failed unstage must not let a staged runtime change into the stash."""
        work = repo.dir
        repo.write(".gitignore", ".claude/\n")
        repo.write(".claude/sova.db", "committed")
        repo.git("add", "-f", ".claude/sova.db")
        repo.land_on_main("chore(docs): a leaked database")
        (work / ".claude" / "sova.db").write_text("live", encoding="utf-8")
        repo.git("add", "-f", ".claude/sova.db")
        repo.write("staged.txt", "staged\n")
        repo.git("add", "staged.txt")

        with pytest.raises(RuntimeError, match=r"\.claude/sova\.db"):
            await runtime_stash_pathspecs(STASH_EXCLUDED_PATHS, work)

    async def test_sync_with_a_tracked_file_under_an_ignored_runtime_dir(self, repo: InvariantRepo) -> None:
        """No pathspec can exclude it, so it is stashed and restored with the operator's work."""
        work = repo.dir
        repo.write(BASELINE, "{}\n")
        repo.land_on_main("chore(docs): a leaked baseline")
        _install_ignore_rules_and_runtime_files(work)
        (work / BASELINE).write_text('{"local": true}\n', encoding="utf-8")
        repo.write("staged.txt", "staged\n")
        repo.git("add", "staged.txt")

        await sync_branch("main", cwd=work)

        assert repo.git("stash", "list") == ""
        assert (work / BASELINE).read_text(encoding="utf-8") == '{"local": true}\n'
        assert repo.git("diff", "--cached", "--name-only") == "staged.txt"

    async def test_sync_stashes_and_restores_operator_work(self, repo: InvariantRepo) -> None:
        work = repo.dir
        repo.write("tracked.txt", "v1\n")
        repo.land_on_main("chore(docs): add tracked file")
        _install_ignore_rules_and_runtime_files(work)
        (work / "tracked.txt").write_text("operator edit\n", encoding="utf-8")
        repo.write("staged.txt", "staged\n")
        repo.git("add", "staged.txt")

        await sync_branch("main", cwd=work)

        assert repo.git("stash", "list") == ""
        assert (work / "tracked.txt").read_text(encoding="utf-8") == "operator edit\n"
        assert repo.git("diff", "--cached", "--name-only") == "staged.txt"
        _assert_runtime_files_untouched(work)

    async def test_rebase_stashes_and_restores_operator_work(self, repo: InvariantRepo) -> None:
        work = repo.dir
        repo.write("base.txt", "base\n")
        repo.land_on_main("chore(docs): base")
        repo.start_branch("feat/work")
        repo.write("feature.txt", "feature\n")
        repo.commit("feat(core): feature")
        repo.git("checkout", "-q", "main")
        repo.write("upstream.txt", "upstream\n")
        repo.land_on_main("chore(docs): upstream")
        repo.git("checkout", "-q", "feat/work")
        _install_ignore_rules_and_runtime_files(work)
        (work / "feature.txt").write_text("operator edit\n", encoding="utf-8")

        result, _cost = await rebase_with_conflict_resolution("main", cwd=work)

        assert result.success, result.error
        assert repo.git("stash", "list") == ""
        assert (work / "upstream.txt").exists()
        assert (work / "feature.txt").read_text(encoding="utf-8") == "operator edit\n"
        _assert_runtime_files_untouched(work)

    async def test_dashboard_branch_freeing_restores_operator_work(self, repo: InvariantRepo) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        work = repo.dir
        repo.write("base.txt", "base\n")
        repo.land_on_main("chore(docs): base")
        repo.start_branch("feat/work")
        repo.write("feature.txt", "feature\n")
        repo.commit("feat(core): feature")
        _install_ignore_rules_and_runtime_files(work)
        (work / "base.txt").write_text("operator edit\n", encoding="utf-8")

        create = AsyncMock(return_value=SimpleNamespace(path=work / "wt"))
        with (
            patch("sova.dashboard.services.agent_context.find_worktree_by_branch", AsyncMock(return_value=work)),
            patch("sova.git.worktree.create_worktree", create),
        ):
            result = await _resolve_issue_worktree("", work, branch_name="feat/work", pr_number=99)

        assert result == work / "wt"
        assert repo.git("branch", "--show-current") == "main"
        assert repo.git("stash", "list") == ""
        assert (work / "base.txt").read_text(encoding="utf-8") == "operator edit\n"
        _assert_runtime_files_untouched(work)
