"""Tests for ``invariants/silent-revert-guard.sh``.

A silent revert is a commit that drops code another PR already landed, usually
because a rebase, squash, or conflict resolution was done from a stale tree.
PR #1153 (issue #1148) did exactly that to PRs #1151 and #1152: both of its
commits were ``feat(core)``, nothing conflicted, and CI stayed green because the
reverted PRs' own tests disappeared with their code.

The guard runs two independent checks, and both are exercised here against real
throwaway git repositories (a bare ``origin`` plus a clone), so the exact
``origin/main..HEAD`` range logic is what is under test:

1. Out-of-scope deletion, ported from the Gwym guard: a commit with a narrow
   conventional-commit scope must not delete many lines outside that scope.
2. Undone recent work, new for this repo: a commit that removes most of what a
   recent base-branch commit added to a file is reverting that commit, whatever
   scope it declares. ``feat(core)`` is exempt from check 1 because SOVA uses it
   for almost everything, so this is the check that catches the real incident.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.conftest import INVARIANT_REPO_EPOCH as OLD
from tests.conftest import INVARIANTS_DIR, InvariantRepo

GUARD = "silent-revert-guard.sh"

# InvariantRepo dates base history at OLD, well outside the recency window, so
# check 2 only fires in the tests that mean it to. SETTLED is far enough after
# OLD that work dated OLD is outside the window.
SETTLED = "2020-02-01T12:00:00+00:00"

ADAPTER = "sova/adapters/github.py"
FINALIZE = "sova/dashboard/services/finalize.py"


def lines(n: int, prefix: str = "line") -> str:
    """``n`` distinct, long-enough lines (the guard ignores very short ones)."""
    return "".join(f"{prefix} {i} content here\n" for i in range(n))


@pytest.fixture
def repo(invariant_repo: InvariantRepo) -> InvariantRepo:
    return invariant_repo


def seed_on_main(repo: InvariantRepo, path: str, n: int = 80, message: str = "feat(core): seed") -> str:
    """Land ``n`` old lines in ``path`` on main (outside the recency window)."""
    repo.write(path, lines(n))
    return repo.land_on_main(message)


def rewrite_on_branch(
    repo: InvariantRepo, path: str, message: str, *, seed_lines: int = 80, content: str = "# reduced\n"
) -> None:
    """Seed ``path`` on main, then replace its content in one commit on a new branch."""
    seed_on_main(repo, path, n=seed_lines)
    repo.start_branch()
    repo.write(path, content)
    repo.commit(message)


def delete_on_branch(repo: InvariantRepo, path: str, message: str, *, seed_lines: int = 80) -> None:
    """Seed ``path`` on main, then delete it in one commit on a new branch."""
    seed_on_main(repo, path, n=seed_lines)
    repo.start_branch()
    (repo.dir / path).unlink()
    repo.commit(message)


def land_recent_feature(
    repo: InvariantRepo, path: str = FINALIZE, message: str = "Refresh the Tasks list (#1152)"
) -> str:
    """Land a just-merged PR that adds a block of code to ``path``."""
    repo.write(path, lines(10, "base") + lines(40, "feature"))
    return repo.land_on_main(message, when=None)


def revert_feature_in_branch(
    repo: InvariantRepo, path: str = FINALIZE, message: str = "feat(core): scope credentials"
) -> None:
    """Commit, on a new branch, a version of ``path`` that lacks the recent feature."""
    repo.start_branch()
    repo.write(path, lines(10, "base") + lines(5, "other"))
    repo.commit(message, when=None)


def test_script_is_executable_and_handles_help() -> None:
    assert os.access(INVARIANTS_DIR / GUARD, os.X_OK)
    result = subprocess.run(["bash", str(INVARIANTS_DIR / GUARD), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "SILENT_REVERT_RANGE" in result.stdout


class TestScopeAndProse:
    """Check 1: ported from the Gwym guard, mapped onto SOVA's scopes."""

    def test_no_commits_beyond_main_passes(self, repo: InvariantRepo) -> None:
        repo.start_branch()
        assert repo.run(GUARD).returncode == 0

    @pytest.mark.parametrize(
        ("path", "seed_lines", "message", "env", "expected"),
        [
            pytest.param("sova/db/models.py", 80, "fix(db): simplify", {}, 0, id="in-scope deletion passes"),
            pytest.param(ADAPTER, 80, "fix(db): simplify", {}, 1, id="out-of-scope deletion over threshold fails"),
            pytest.param(ADAPTER, 30, "fix(db): tweak", {}, 0, id="out-of-scope deletion below threshold passes"),
            pytest.param(
                ADAPTER, 80, "fix(db): tweak", {"SILENT_REVERT_THRESHOLD": "100"}, 0, id="threshold is configurable"
            ),
            pytest.param(ADAPTER, 80, "feat(core): rework", {}, 0, id="core is not judged by path"),
            pytest.param(ADAPTER, 80, "feat(dashboard): rework", {}, 0, id="dashboard is not judged by path"),
            pytest.param(
                "tests/test_models.py", 80, "fix(db): drop old tests", {}, 0, id="tests ride along with any scope"
            ),
            pytest.param("sova/db/models.py", 80, "quick fix", {}, 1, id="scopeless commit has nothing in scope"),
            pytest.param(
                "sova/db/models.py", 80, "feat(nonsense): cleanup", {}, 1, id="unrecognised scope has nothing in scope"
            ),
            pytest.param(
                ADAPTER, 80, "Trim the adapter (core)", {}, 1, id="a parenthesised word in a title is not a scope"
            ),
            pytest.param(
                ".github/workflows/ci.yml", 80, "ci: slim the pipeline", {}, 0, id="ci type may touch infrastructure"
            ),
            pytest.param("docs/guide.md", 200, "fix(db): refactor", {}, 0, id="docs below prose threshold pass"),
            pytest.param("docs/runbooks.md", 500, "fix(db): refactor", {}, 1, id="docs above prose threshold fail"),
            pytest.param(
                "docs/large.md",
                500,
                "fix(db): refactor",
                {"SILENT_REVERT_PROSE_THRESHOLD": "600"},
                0,
                id="prose threshold is configurable",
            ),
            pytest.param(
                ".claude/rules/rule.md", 60, "fix(db): cleanup", {}, 1, id="claude rules use the code threshold"
            ),
            pytest.param("docs/GUIDE.MD", 200, "fix(db): refactor", {}, 1, id="uppercase .MD uses the code threshold"),
            pytest.param(
                "docs/a => b.md", 80, "fix(db): cleanup", {}, 0, id="literal arrow in a filename is not a rename"
            ),
        ],
    )
    def test_rewrite_verdict(
        self, repo: InvariantRepo, path: str, seed_lines: int, message: str, env: dict[str, str], expected: int
    ) -> None:
        rewrite_on_branch(repo, path, message, seed_lines=seed_lines)
        assert repo.run(GUARD, **env).returncode == expected

    def test_out_of_scope_failure_names_the_file_and_scope(self, repo: InvariantRepo) -> None:
        rewrite_on_branch(repo, ADAPTER, "fix(db): simplify")
        result = repo.run(GUARD)
        assert result.returncode == 1
        assert "outside their declared scope" in result.stdout
        assert f"{ADAPTER} (scope: db)" in result.stdout

    def test_compound_scope_allows_both_paths(self, repo: InvariantRepo) -> None:
        seed_on_main(repo, "sova/db/models.py")
        seed_on_main(repo, "sova/ipc/handoff.py")
        repo.start_branch()
        repo.write("sova/db/models.py", "# reduced\n")
        repo.write("sova/ipc/handoff.py", "# reduced\n")
        repo.commit("fix(db,ipc): cleanup")
        assert repo.run(GUARD).returncode == 0

    def test_generated_files_are_exempt(self, repo: InvariantRepo) -> None:
        rewrite_on_branch(
            repo,
            "sova/dashboard/static/tailwind.min.css",
            "fix(db): rebuild css",
            seed_lines=300,
            content="/* small */\n",
        )
        assert repo.run(GUARD).returncode == 0

    @pytest.mark.parametrize(
        ("path", "seed_lines", "message", "expected", "fragment"),
        [
            pytest.param(
                "docs/small.md", 30, "fix(db): cleanup", 1, "docs/small.md entirely", id="docs file at any size"
            ),
            pytest.param(
                "invariants/tiny.sh", 10, "fix(db): cleanup", 1, "invariants/tiny.sh entirely", id="tiny invariant"
            ),
            pytest.param(
                "invariants/tiny.sh",
                10,
                "refactor(invariants): fold into another check",
                0,
                "",
                id="invariants scope may delete one",
            ),
            pytest.param(
                "invariants/tiny.sh",
                10,
                "feat(core): tidy up",
                1,
                "invariants/tiny.sh entirely",
                id="core may not delete an invariant",
            ),
            pytest.param(
                ".githooks/pre-push",
                10,
                "feat(dashboard): tidy up",
                1,
                ".githooks/pre-push entirely",
                id="dashboard may not delete a hook",
            ),
            pytest.param("docs/small.md", 30, "docs(docs): drop an old page", 0, "", id="docs scope may delete a doc"),
        ],
    )
    def test_whole_file_deletion_of_protected_files(
        self, repo: InvariantRepo, path: str, seed_lines: int, message: str, expected: int, fragment: str
    ) -> None:
        delete_on_branch(repo, path, message, seed_lines=seed_lines)
        result = repo.run(GUARD)
        assert result.returncode == expected
        assert fragment in result.stdout


class TestRenames:
    @pytest.mark.parametrize(
        ("src", "dst", "seed_lines", "new_lines", "message", "expected"),
        [
            pytest.param("docs/old.md", "docs/new.md", 100, None, "fix(db): rename doc", 0, id="pure docs rename"),
            pytest.param(
                "docs/verbose.md",
                "docs/concise.md",
                500,
                350,
                "fix(db): rewrite doc",
                0,
                id="docs rename trimmed under prose limit",
            ),
            pytest.param(
                "NOTES.md",
                "docs/guide.md",
                200,
                140,
                "fix(db): move notes",
                0,
                id="rename into docs uses the destination",
            ),
            pytest.param(
                "docs/guide.md",
                "NOTES.md",
                200,
                100,
                "fix(db): move doc out",
                1,
                id="rename out of docs uses the code limit",
            ),
            pytest.param(
                "sova/adapters/helper.py",
                "sova/db/helper.py",
                80,
                60,
                "refactor(db): move helper",
                0,
                id="code rename into scope",
            ),
            pytest.param(
                "sova/db/helper.py",
                "sova/adapters/helper.py",
                200,
                140,
                "refactor(db): move helper out",
                1,
                id="code rename out of scope",
            ),
        ],
    )
    def test_rename_verdict(
        self,
        repo: InvariantRepo,
        src: str,
        dst: str,
        seed_lines: int,
        new_lines: int | None,
        message: str,
        expected: int,
    ) -> None:
        seed_on_main(repo, src, n=seed_lines)
        repo.start_branch()
        (repo.dir / dst).parent.mkdir(parents=True, exist_ok=True)
        repo.git("mv", src, dst)
        if new_lines is not None:
            repo.write(dst, lines(new_lines))
        repo.commit(message)
        assert repo.run(GUARD).returncode == expected


class TestEscapeHatch:
    @pytest.mark.parametrize(
        ("body", "env", "expected", "fragment"),
        [
            pytest.param(
                "silent-revert-ok: adapter folded into sova/db in this change", {}, 0, "", id="a reason exempts"
            ),
            pytest.param("silent-revert-ok:", {}, 1, "", id="bare trailer does not exempt"),
            pytest.param("silent-revert-ok: ok", {}, 1, "too short to justify", id="one word does not exempt"),
            pytest.param(
                "silent-revert-ok:   done ", {}, 1, "too short to justify", id="padded one word does not exempt"
            ),
            pytest.param(
                "I considered silent-revert-ok: this is fine and intentional", {}, 1, "", id="trailer must start a line"
            ),
            pytest.param(
                "silent-revert-ok: tidy up",
                {"SILENT_REVERT_MIN_REASON": "5"},
                0,
                "",
                id="reason length is configurable",
            ),
        ],
    )
    def test_trailer(self, repo: InvariantRepo, body: str, env: dict[str, str], expected: int, fragment: str) -> None:
        rewrite_on_branch(repo, ADAPTER, f"fix(db): simplify\n\n{body}")
        result = repo.run(GUARD, **env)
        assert result.returncode == expected
        assert fragment in result.stdout


class TestRange:
    @pytest.mark.parametrize("integrate", ["rebase", "merge"])
    def test_default_range_does_not_blame_a_branch_for_the_base_branchs_commits(
        self, repo: InvariantRepo, integrate: str
    ) -> None:
        """Pulling a newer main into a branch must not make main's own commits part of the push.

        An ``@{u}..HEAD`` range does exactly that after a rebase and force-push:
        the old remote tip is no longer an ancestor, so every commit main gained
        since then looks like part of the push.
        """
        seed_on_main(repo, ADAPTER)
        repo.start_branch()
        repo.write("sova/db/models.py", "# work\n")
        repo.commit("feat(db): add models")
        repo.git("push", "-q", "-u", "origin", "feat/work")
        repo.git("checkout", "-q", "main")
        repo.write(ADAPTER, "# reduced\n")
        repo.land_on_main("fix(adapters): trim github adapter")
        repo.git("checkout", "-q", "feat/work")
        repo.git(integrate, "-q", "main", *(["--no-edit"] if integrate == "merge" else []))
        assert repo.run(GUARD).returncode == 0

    def test_explicit_range_catches_what_the_default_range_cannot_see(self, repo: InvariantRepo) -> None:
        seed_on_main(repo, ADAPTER)
        pre_tip = repo.git("rev-parse", "HEAD")
        repo.write(ADAPTER, "# reduced\n")
        repo.land_on_main("fix(db): simplify")
        post_tip = repo.git("rev-parse", "HEAD")
        assert repo.run(GUARD).returncode == 0
        assert repo.run(GUARD, SILENT_REVERT_RANGE=f"{pre_tip}..{post_tip}").returncode == 1

    def test_explicit_range_with_no_violation_passes(self, repo: InvariantRepo) -> None:
        seed_on_main(repo, "sova/db/models.py")
        pre_tip = repo.git("rev-parse", "HEAD")
        repo.write("sova/db/models.py", "# reduced\n")
        repo.land_on_main("fix(db): simplify", when=SETTLED)
        post_tip = repo.git("rev-parse", "HEAD")
        assert repo.run(GUARD, SILENT_REVERT_RANGE=f"{pre_tip}..{post_tip}").returncode == 0

    def test_unresolvable_explicit_range_is_a_hard_failure(self, repo: InvariantRepo) -> None:
        """Negative control: a typo'd range must never degrade into a silent pass."""
        result = repo.run(GUARD, SILENT_REVERT_RANGE="not-a-real-ref..also-not-real")
        assert result.returncode == 1
        assert "could not resolve" in result.stdout

    def test_missing_base_ref_fails_open_but_says_nothing_was_checked(self, repo: InvariantRepo) -> None:
        repo.git("update-ref", "-d", "refs/remotes/origin/main")
        result = repo.run(GUARD)
        assert result.returncode == 0
        assert "nothing was checked" in result.stderr


class TestUndoneRecentWork:
    """Check 2: scope independent, names the commit that was undone."""

    def test_wide_scope_commit_that_reverts_a_recent_pr_fails_and_names_it(self, repo: InvariantRepo) -> None:
        victim = land_recent_feature(repo)
        revert_feature_in_branch(repo)
        result = repo.run(GUARD)
        assert result.returncode == 1
        assert "undo recent work" in result.stdout
        assert victim[:8] in result.stdout
        assert "(#1152)" in result.stdout
        assert FINALIZE in result.stdout

    def test_narrow_in_scope_commit_is_also_caught(self, repo: InvariantRepo) -> None:
        land_recent_feature(repo, "sova/db/models.py")
        revert_feature_in_branch(repo, "sova/db/models.py", "fix(db): scope credentials")
        assert repo.run(GUARD).returncode == 1

    def test_a_whole_file_deleted_right_after_it_landed_is_caught(self, repo: InvariantRepo) -> None:
        land_recent_feature(repo)
        repo.start_branch()
        (repo.dir / FINALIZE).unlink()
        repo.commit("feat(core): scope credentials", when=None)
        assert repo.run(GUARD).returncode == 1

    def test_every_victim_is_named(self, repo: InvariantRepo) -> None:
        first = land_recent_feature(repo, "sova/dashboard/a.py", "first PR (#1150)")
        repo.write("sova/dashboard/b.py", lines(10, "base") + lines(40, "other feature"))
        second = repo.land_on_main("feat(core): second PR (#1151)", when=None)
        repo.start_branch()
        repo.write("sova/dashboard/a.py", lines(10, "base"))
        repo.write("sova/dashboard/b.py", lines(10, "base"))
        repo.commit("feat(core): scope credentials", when=None)
        result = repo.run(GUARD)
        assert result.returncode == 1
        assert first[:8] in result.stdout
        assert second[:8] in result.stdout

    def test_work_older_than_the_window_is_not_a_victim(self, repo: InvariantRepo) -> None:
        repo.write(FINALIZE, lines(10, "base") + lines(40, "feature"))
        repo.land_on_main("feat(core): long-settled feature (#900)", when=OLD)
        revert_feature_in_branch(repo)
        assert repo.run(GUARD).returncode == 0

    def test_window_is_configurable(self, repo: InvariantRepo) -> None:
        repo.write(FINALIZE, lines(10, "base") + lines(40, "feature"))
        repo.land_on_main("feat(core): settled feature (#900)", when="2026-01-01T12:00:00+00:00")
        repo.start_branch()
        repo.write(FINALIZE, lines(10, "base") + lines(5, "other"))
        repo.commit("feat(core): scope credentials", when="2026-01-03T12:00:00+00:00")
        # The feature landed 48 hours before the commit: inside the default 72 hour window.
        assert repo.run(GUARD).returncode == 1
        assert repo.run(GUARD, SILENT_REVERT_RECENT_HOURS="24").returncode == 0

    def test_lines_moved_to_another_file_are_not_a_revert(self, repo: InvariantRepo) -> None:
        """A module split removes lines from one file and adds them to another."""
        land_recent_feature(repo)
        repo.start_branch()
        repo.write(FINALIZE, lines(10, "base"))
        repo.write("sova/dashboard/services/finalize_helpers.py", lines(40, "feature"))
        repo.commit("refactor(core): split finalize", when=None)
        assert repo.run(GUARD).returncode == 0

    def test_refactoring_your_own_branch_is_not_a_revert(self, repo: InvariantRepo) -> None:
        repo.start_branch()
        repo.write(FINALIZE, lines(10, "base") + lines(40, "feature"))
        repo.commit("feat(core): first pass", when=None)
        repo.write(FINALIZE, lines(10, "base") + lines(5, "other"))
        repo.commit("refactor(core): second pass", when=None)
        assert repo.run(GUARD).returncode == 0

    def test_a_small_removal_is_not_a_revert(self, repo: InvariantRepo) -> None:
        land_recent_feature(repo)
        repo.start_branch()
        repo.write(FINALIZE, lines(10, "base") + lines(30, "feature"))
        repo.commit("feat(core): trim", when=None)
        assert repo.run(GUARD).returncode == 0

    def test_removing_only_a_minority_of_the_pr_is_not_a_revert(self, repo: InvariantRepo) -> None:
        repo.write(FINALIZE, lines(100, "feature"))
        repo.land_on_main("feat(core): big feature (#1000)", when=None)
        repo.start_branch()
        repo.write(FINALIZE, lines(70, "feature"))
        repo.commit("feat(core): trim", when=None)
        assert repo.run(GUARD).returncode == 0

    def test_indentation_changes_do_not_fake_a_revert(self, repo: InvariantRepo) -> None:
        repo.write(FINALIZE, lines(40, "feature"))
        repo.land_on_main("feat(core): feature (#1000)", when=None)
        repo.start_branch()
        repo.write(FINALIZE, "".join(f"    {line}" for line in lines(40, "feature").splitlines(keepends=True)))
        repo.commit("refactor(core): indent", when=None)
        assert repo.run(GUARD).returncode == 0

    def test_reason_exempts_an_intentional_removal(self, repo: InvariantRepo) -> None:
        land_recent_feature(repo)
        revert_feature_in_branch(
            repo,
            message="feat(core): drop the announcer\n\nsilent-revert-ok: superseded by the websocket push in this PR",
        )
        assert repo.run(GUARD).returncode == 0

    def test_a_bare_trailer_does_not_exempt_a_revert(self, repo: InvariantRepo) -> None:
        land_recent_feature(repo)
        revert_feature_in_branch(repo, message="feat(core): tidy\n\nsilent-revert-ok: yes")
        result = repo.run(GUARD)
        assert result.returncode == 1
        assert "too short to justify" in result.stdout

    @pytest.mark.parametrize("env", [{"SILENT_REVERT_MIN_LINES": "60"}, {"SILENT_REVERT_MIN_PERCENT": "99"}])
    def test_thresholds_are_configurable(self, repo: InvariantRepo, env: dict[str, str]) -> None:
        land_recent_feature(repo)
        revert_feature_in_branch(repo)
        assert repo.run(GUARD).returncode == 1
        assert repo.run(GUARD, **env).returncode == 0
