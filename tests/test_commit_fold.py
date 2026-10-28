"""Tests for sova.core.steps._commit_fold: folding a hook fix into its branch commit."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from sova.core.steps._commit_fold import fold_fix_into_branch, resolve_fixup_target
from sova.utils.shell import run

BASE = "main"


async def _git(*args: str, cwd: Path) -> None:
    result = await run("git", *args, cwd=cwd)
    assert result.success, f"git {args} failed: {result.stderr}"


async def _init_repo(cwd: Path) -> None:
    """Init a repo with one base commit on main, mirrored to refs/remotes/origin/main."""
    await _git("init", "-q", "-b", BASE, cwd=cwd)
    await _git("config", "user.email", "test@example.com", cwd=cwd)
    await _git("config", "user.name", "Test User", cwd=cwd)
    # Neutralize a developer's global git config: commit.gpgsign would make
    # every `git commit` here try to sign (hang on a GPG prompt, or fail),
    # and core.hooksPath would run this repository's own pre-commit hooks
    # against these throwaway trees.
    await _git("config", "commit.gpgsign", "false", cwd=cwd)
    await _git("config", "core.hooksPath", "/dev/null", cwd=cwd)
    (cwd / "base.txt").write_text("base\n")
    await _git("add", "base.txt", cwd=cwd)
    await _git("commit", "-m", "chore(core): base", cwd=cwd)
    head = (await run("git", "rev-parse", "HEAD", cwd=cwd)).stdout.strip()
    await _git("update-ref", "refs/remotes/origin/main", head, cwd=cwd)


async def _commit_file(cwd: Path, name: str, content: str, message: str) -> str:
    (cwd / name).write_text(content)
    await _git("add", name, cwd=cwd)
    await _git("commit", "-m", message, cwd=cwd)
    return (await run("git", "rev-parse", "HEAD", cwd=cwd)).stdout.strip()


class TestFoldFixIntoBranch:
    async def test_no_diff_and_no_fixup_commit_is_a_noop(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        await _commit_file(tmp_path, "a.py", "a\n", "feat(core): add a")

        folded = await fold_fix_into_branch(tmp_path, BASE)

        assert folded is False
        log_result = await run("git", "log", "--oneline", f"origin/{BASE}..HEAD", cwd=tmp_path)
        assert len(log_result.stdout.strip().splitlines()) == 1

    async def test_folds_uncommitted_fix_into_the_commit_that_touched_the_file(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        await _commit_file(tmp_path, "a.py", "a\n", "feat(core): add a")
        await _commit_file(tmp_path, "b.py", "b\n", "feat(core): add b")

        # Simulate the fix agent editing a.py and leaving it uncommitted.
        (tmp_path / "a.py").write_text("a\nfixed\n")

        folded = await fold_fix_into_branch(tmp_path, BASE)

        assert folded is True
        log_result = await run("git", "log", "--format=%s", f"origin/{BASE}..HEAD", cwd=tmp_path)
        subjects = log_result.stdout.strip().splitlines()
        assert subjects == ["feat(core): add b", "feat(core): add a"]
        content = (tmp_path / "a.py").read_text()
        assert content == "a\nfixed\n"
        status = await run("git", "status", "--porcelain", cwd=tmp_path)
        assert status.stdout.strip() == ""

    async def test_fix_touching_multiple_commits_falls_back_to_head(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        await _commit_file(tmp_path, "a.py", "a\n", "feat(core): add a")
        await _commit_file(tmp_path, "b.py", "b\n", "feat(core): add b")

        (tmp_path / "a.py").write_text("a\nfixed\n")
        (tmp_path / "b.py").write_text("b\nfixed\n")

        folded = await fold_fix_into_branch(tmp_path, BASE)

        assert folded is True
        log_result = await run("git", "log", "--format=%s", f"origin/{BASE}..HEAD", cwd=tmp_path)
        subjects = log_result.stdout.strip().splitlines()
        # Both fixes landed in HEAD ("add b"), not split across commits.
        assert subjects == ["feat(core): add b", "feat(core): add a"]
        diff = await run("git", "diff", "HEAD~1", "HEAD", "--name-only", cwd=tmp_path)
        assert "a.py" in diff.stdout
        assert "b.py" in diff.stdout

    async def test_new_untracked_file_falls_back_to_head(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        await _commit_file(tmp_path, "a.py", "a\n", "feat(core): add a")

        (tmp_path / "new_file.py").write_text("new\n")

        folded = await fold_fix_into_branch(tmp_path, BASE)

        assert folded is True
        log_result = await run("git", "log", "--format=%s", f"origin/{BASE}..HEAD", cwd=tmp_path)
        assert log_result.stdout.strip().splitlines() == ["feat(core): add a"]

    async def test_first_branch_commit_can_still_be_folded(self, tmp_path: Path) -> None:
        """Edge case: the fixup target is the only commit on the branch, so the
        rebase root is the merge-base itself, not a parent within base..HEAD."""
        await _init_repo(tmp_path)
        await _commit_file(tmp_path, "a.py", "a\n", "feat(core): add a")

        (tmp_path / "a.py").write_text("a\nfixed\n")

        folded = await fold_fix_into_branch(tmp_path, BASE)

        assert folded is True
        log_result = await run("git", "log", "--format=%s", f"origin/{BASE}..HEAD", cwd=tmp_path)
        assert log_result.stdout.strip().splitlines() == ["feat(core): add a"]
        assert (tmp_path / "a.py").read_text() == "a\nfixed\n"

    async def test_folds_a_reword_commit_the_agent_made_itself(self, tmp_path: Path) -> None:
        """When the fix agent reworded a commit's message directly via
        `git commit --fixup=reword:<sha>`, SOVA still runs the autosquash."""
        await _init_repo(tmp_path)
        target = await _commit_file(tmp_path, "a.py", "a\n", "Add a (bad subject)")
        await _commit_file(tmp_path, "b.py", "b\n", "feat(core): add b")

        editor = tmp_path.parent / "editor.sh"
        editor.write_text(
            '#!/bin/sh\nFILE="$1"\nHEADER=$(head -n1 "$FILE")\n'
            '{ printf "%s\\n\\n" "$HEADER"; echo "feat(core): add a"; } > "$FILE"\n'
        )
        editor.chmod(0o755)
        reword = await run(
            "git",
            "commit",
            "--fixup",
            f"reword:{target}",
            "--allow-empty",
            cwd=tmp_path,
            env={"GIT_EDITOR": str(editor), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
        assert reword.success, reword.stderr

        folded = await fold_fix_into_branch(tmp_path, BASE)

        assert folded is True
        log_result = await run("git", "log", "--format=%s", f"origin/{BASE}..HEAD", cwd=tmp_path)
        assert log_result.stdout.strip().splitlines() == ["feat(core): add b", "feat(core): add a"]

    async def test_never_folds_into_a_commit_already_on_the_base_branch(self, tmp_path: Path) -> None:
        """With no branch commit of its own, there is nothing to fold into.

        HEAD is itself the base commit here, so falling back to HEAD would
        target a commit on origin/main: autosquash cannot reach it (it sits
        outside the merge-base..HEAD range) and would leave the ``fixup!``
        commit on the branch.
        """
        await _init_repo(tmp_path)
        # Modify the base-branch file without any new branch commit: the only
        # commit that touched base.txt is the one already on origin/main.
        (tmp_path / "base.txt").write_text("base\nfixed\n")

        assert await resolve_fixup_target(tmp_path, BASE) is None

    async def test_no_branch_commit_leaves_the_fix_staged_and_folds_nothing(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        (tmp_path / "base.txt").write_text("base\nfixed\n")
        head_before = (await run("git", "rev-parse", "HEAD", cwd=tmp_path)).stdout.strip()

        assert await fold_fix_into_branch(tmp_path, BASE) is False

        head_after = (await run("git", "rev-parse", "HEAD", cwd=tmp_path)).stdout.strip()
        assert head_after == head_before, "the base commit must never be rewritten"
        subjects = await run("git", "log", "--format=%s", "-5", cwd=tmp_path)
        assert "fixup!" not in subjects.stdout
        assert (tmp_path / "base.txt").read_text() == "base\nfixed\n"

    async def test_failed_autosquash_unwinds_the_fixup_commit(self, tmp_path: Path) -> None:
        """A fixup that cannot be replayed must leave no marker commit behind.

        `_autosquash` is stubbed to the aborted-rebase outcome (a conflict
        leaves the pre-rebase HEAD, fixup commit included). The fold must
        report failure and restore the pre-fold state (the fix staged, no
        extra commit) rather than letting a "fixup! ..." commit, which no
        conventional-commit hook accepts, reach git push.
        """
        await _init_repo(tmp_path)
        await _commit_file(tmp_path, "a.py", "a\n", "feat(core): add a")
        head_before = (await run("git", "rev-parse", "HEAD", cwd=tmp_path)).stdout.strip()

        (tmp_path / "a.py").write_text("a\nfixed\n")

        with patch("sova.core.steps._commit_fold._autosquash", new_callable=AsyncMock, return_value=False):
            assert await fold_fix_into_branch(tmp_path, BASE) is False

        assert (await run("git", "rev-parse", "HEAD", cwd=tmp_path)).stdout.strip() == head_before
        log_result = await run("git", "log", "--format=%s", f"origin/{BASE}..HEAD", cwd=tmp_path)
        assert log_result.stdout.strip().splitlines() == ["feat(core): add a"]
        # The fix itself survives, staged, for the hook retry.
        assert (tmp_path / "a.py").read_text() == "a\nfixed\n"
        staged = await run("git", "diff", "--cached", "--name-only", cwd=tmp_path)
        assert staged.stdout.strip() == "a.py"

    async def test_marker_commit_surviving_a_clean_rebase_is_reported_as_not_folded(self, tmp_path: Path) -> None:
        """A fixup! whose target is outside the rebase range is replayed as an
        ordinary commit, so the rebase exits 0 while the marker survives.

        The marker here is empty (created with --allow-empty, carrying no
        real content), so the fold drops it outright rather than leaving an
        unpushable "fixup! ..." subject on the branch.
        """
        await _init_repo(tmp_path)
        base_sha = (await run("git", "rev-parse", "HEAD", cwd=tmp_path)).stdout.strip()
        await _commit_file(tmp_path, "a.py", "a\n", "feat(core): add a")
        # An agent-authored fixup aimed at a commit already on origin/main.
        await _git("commit", "--allow-empty", "--fixup", base_sha, cwd=tmp_path)

        assert await fold_fix_into_branch(tmp_path, BASE) is False

        log_result = await run("git", "log", "--format=%s", f"origin/{BASE}..HEAD", cwd=tmp_path)
        assert "fixup!" not in log_result.stdout
        assert log_result.stdout.strip().splitlines() == ["feat(core): add a"]

    async def test_non_empty_stranded_marker_is_logged_and_left_in_place(self, tmp_path: Path) -> None:
        """A surviving marker that carries real content cannot be safely
        dropped or rewritten here, so it is left in place rather than lost,
        but the fold still reports failure."""
        await _init_repo(tmp_path)
        base_sha = (await run("git", "rev-parse", "HEAD", cwd=tmp_path)).stdout.strip()
        await _commit_file(tmp_path, "a.py", "a\n", "feat(core): add a")
        # A non-empty fixup aimed at a commit already on origin/main: its
        # target is unreachable, so autosquash can never place it, and its
        # real content means it is never dropped either.
        (tmp_path / "b.py").write_text("b\n")
        await _git("add", "b.py", cwd=tmp_path)
        await _git("commit", "--fixup", base_sha, cwd=tmp_path)

        assert await fold_fix_into_branch(tmp_path, BASE) is False

        log_result = await run("git", "log", "--format=%s", f"origin/{BASE}..HEAD", cwd=tmp_path)
        subjects = log_result.stdout.strip().splitlines()
        assert any(s.startswith("fixup!") for s in subjects)
        assert (tmp_path / "b.py").read_text() == "b\n"

    async def test_non_ascii_path_folds_into_the_commit_that_touched_it(self, tmp_path: Path) -> None:
        """A non-ASCII filename must not be matched as a quoted, C-escaped
        literal string against branch history, which would silently fall
        back to HEAD instead of the commit that actually owns the file."""
        await _init_repo(tmp_path)
        await _commit_file(tmp_path, "héllo.txt", "hello\n", "feat(core): add hello")
        await _commit_file(tmp_path, "b.py", "b\n", "feat(core): add b")

        (tmp_path / "héllo.txt").write_text("hello\nfixed\n")

        folded = await fold_fix_into_branch(tmp_path, BASE)

        assert folded is True
        log_result = await run("git", "log", "--format=%s", f"origin/{BASE}..HEAD", cwd=tmp_path)
        subjects = log_result.stdout.strip().splitlines()
        assert subjects == ["feat(core): add b", "feat(core): add hello"]
        # The fix must land in the earlier commit (HEAD~1, "add hello"), not
        # be stapled into HEAD ("add b") because the quoted path failed to
        # match any branch commit and fell back there.
        diff = await run("git", "diff", "HEAD~1", "HEAD", "--name-only", cwd=tmp_path)
        assert diff.stdout.strip() == "b.py"
        show = await run("git", "show", "HEAD~1:héllo.txt", cwd=tmp_path)
        assert show.stdout == "hello\nfixed\n"

    async def test_suspicious_staged_file_blocks_the_fold(self, tmp_path: Path) -> None:
        """`git add -A` sweeps in whatever else the agent left behind, so the
        shared suspicious-path guard must stop a credential file being folded
        into a branch commit and pushed with it."""
        await _init_repo(tmp_path)
        await _commit_file(tmp_path, "a.py", "a\n", "feat(core): add a")

        (tmp_path / "a.py").write_text("a\nfixed\n")
        (tmp_path / ".env").write_text("TOKEN=secret\n")

        assert await fold_fix_into_branch(tmp_path, BASE) is False

        log_result = await run("git", "log", "--format=%s", f"origin/{BASE}..HEAD", cwd=tmp_path)
        assert log_result.stdout.strip().splitlines() == ["feat(core): add a"]
        tracked = await run("git", "ls-tree", "-r", "--name-only", "HEAD", cwd=tmp_path)
        assert ".env" not in tracked.stdout
        # The index must be restored to its pre-fold state, not left staged:
        # `git add -A` swept in a.py alongside .env, and the abort must not
        # leave the legitimate fix staged while hiding the real blocker.
        staged = await run("git", "diff", "--cached", "--name-only", cwd=tmp_path)
        assert staged.stdout.strip() == ""

    async def test_our_fixup_folds_even_when_an_unrelated_empty_marker_survives_autosquash(
        self, tmp_path: Path
    ) -> None:
        """Our own fixup commit must not be unwound just because a stray,
        unrelated agent-authored marker also needed cleanup.

        The autosquash itself succeeds (our fixup for "add a" is squashed
        correctly); a separate empty marker aimed at the base commit (outside
        the rebase range) is replayed verbatim and then dropped by the
        stranded-marker cleanup. The completed fold must still be reported
        as folded, not reset back to a staged diff.
        """
        await _init_repo(tmp_path)
        base_sha = (await run("git", "rev-parse", "HEAD", cwd=tmp_path)).stdout.strip()
        await _commit_file(tmp_path, "a.py", "a\n", "feat(core): add a")
        # An unrelated, agent-authored empty marker aimed at a commit already
        # on origin/main: autosquash can never place it, so it survives.
        await _git("commit", "--allow-empty", "--fixup", base_sha, cwd=tmp_path)

        (tmp_path / "a.py").write_text("a\nfixed\n")

        folded = await fold_fix_into_branch(tmp_path, BASE)

        assert folded is True
        log_result = await run("git", "log", "--format=%s", f"origin/{BASE}..HEAD", cwd=tmp_path)
        subjects = log_result.stdout.strip().splitlines()
        assert subjects == ["feat(core): add a"]
        assert (tmp_path / "a.py").read_text() == "a\nfixed\n"
        status = await run("git", "status", "--porcelain", cwd=tmp_path)
        assert status.stdout.strip() == ""

    async def test_unavailable_repo_fails_open(self, tmp_path: Path) -> None:
        """A nonexistent cwd must not raise; the caller treats it as nothing folded."""
        missing = tmp_path / "does-not-exist"
        assert await fold_fix_into_branch(missing, BASE) is False
