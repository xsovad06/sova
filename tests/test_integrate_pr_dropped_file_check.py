"""Tests for step 4b of ``/integrate-pr``'s Update subroutine (issue #1313).

A rebase conflict resolved by taking one side's whole file (``git checkout --ours
requirements.txt``) leaves no conflict marker and no failing test: the file simply
vanishes from the PR's diff, and a force-push publishes the loss. Step 4b compares
the PR's file list and per-file changed-line counts, read from the backup ref step 2
saved, against the rebased head.

The check is an inline bash block in the command (the command is distributed to
projects that have none of SOVA's repo scripts), so these tests extract that exact
block from the canonical command and run it against real throwaway git repositories.
Each scenario builds a base branch, a PR branch, a backup ref, a moved base and a
real ``git rebase``, so the ``BASE...BACKUP`` and ``BASE...HEAD`` diffs under test are
the ones the command computes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from sova.commands.catalog import get_canonical_dir

REPO_ROOT = Path(__file__).parent.parent
CANONICAL = get_canonical_dir() / "integrate-pr.md"
RENDERED = REPO_ROOT / ".claude" / "commands" / "integrate-pr.md"

PR_NUMBER = "42"
BACKUP_REF = f"refs/integrate-backup/{PR_NUMBER}"
SHELLS = [
    "bash",
    pytest.param("zsh", marks=pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")),
]


def _step_4b(text: str) -> str:
    return text[text.index("4b. **Check the rebase did not drop PR content") : text.index("5. When all commits")]


def _check_script(text: str) -> str:
    """The step's single fenced bash block, dedented, with the placeholders filled in."""
    match = re.search(r"```bash\n(.*?)```", _step_4b(text), re.DOTALL)
    assert match, "step 4b has no bash block"
    script = textwrap.dedent(match.group(1))
    return script.replace("<PR_NUMBER>", PR_NUMBER).replace("<BASE_BRANCH>", "main")


@pytest.fixture(scope="module")
def check_script() -> str:
    return _check_script(CANONICAL.read_text(encoding="utf-8"))


class Repo:
    """A throwaway repository with a ``main`` branch, an ``origin/main`` ref and a PR branch."""

    def __init__(self, path: Path) -> None:
        self.dir = path
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_EDITOR": "true",
        }
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.dir, env=self.env, capture_output=True, text=True, check=check)

    def write(self, name: str, content: str | bytes) -> None:
        target = self.dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def land_on_main(self, message: str) -> None:
        """Commit on main and publish it as ``origin/main``, as a fetch would."""
        if self.git("symbolic-ref", "--short", "HEAD").stdout.strip() != "main":
            self.git("checkout", "-q", "main")
        self.commit(message)
        self.git("update-ref", "refs/remotes/origin/main", "main")

    def start_pr(self) -> None:
        self.git("checkout", "-q", "-b", "feat")

    def back_up(self) -> None:
        self.git("update-ref", BACKUP_REF, "HEAD")

    def rebase(self) -> subprocess.CompletedProcess[str]:
        self.git("checkout", "-q", "feat")
        return self.git("rebase", "origin/main", check=False)

    def resolve_taking_base(self, *names: str) -> None:
        """Resolve each conflicted file the way the incident did: take the base's whole file."""
        for name in names:
            self.git("checkout", "--ours", "--", name)
        self.git("add", "-A")
        self.git("rebase", "--continue")

    def run_check(self, script: str, shell: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([shell, "-c", script], cwd=cwd or self.dir, env=self.env, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def pins(prefix: str, n: int) -> str:
    return "".join(f"{prefix}-{i}==1.{i}.0\n" for i in range(n))


def conflicting_pins(repo: Repo, names: tuple[str, ...], pr_pins: int = 30) -> None:
    """Base and PR each append their own pins to the same files, so the rebase conflicts."""
    for name in names:
        repo.write(name, pins("old", 5))
    repo.land_on_main("seed")
    repo.start_pr()
    for name in names:
        repo.write(name, pins("old", 5) + pins("pr", pr_pins))
    repo.commit("pr: pin dependencies")
    repo.back_up()
    repo.git("checkout", "-q", "main")
    for name in names:
        repo.write(name, pins("old", 5) + pins("base", 3))
    repo.land_on_main("base: bump")
    assert repo.rebase().returncode != 0, "the scenario must conflict"


@pytest.mark.parametrize("shell", SHELLS)
class TestPassingRebases:
    def test_clean_rebase_passes_with_the_pass_marker(self, repo: Repo, check_script: str, shell: str) -> None:
        repo.write("lock.txt", pins("old", 5))
        repo.write("app.py", "x = 1\n")
        repo.land_on_main("seed")
        repo.start_pr()
        repo.write("app.py", "x = 1\n" + "y = 2\n" * 20)
        repo.write("lock.txt", pins("old", 5) + pins("pr", 10))
        repo.commit("pr: change")
        repo.back_up()
        repo.git("checkout", "-q", "main")
        repo.write("other.py", "z = 3\n")
        repo.land_on_main("base: unrelated")
        assert repo.rebase().returncode == 0

        result = repo.run_check(check_script, shell)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "DROPPED_FILE_CHECK_PASS\n"
        assert result.stderr == ""

    def test_correctly_resolved_lockfile_conflict_passes(self, repo: Repo, check_script: str, shell: str) -> None:
        conflicting_pins(repo, ("requirements.txt",))
        repo.write("requirements.txt", pins("old", 5) + pins("base", 3) + pins("pr", 30))
        repo.git("add", "-A")
        repo.git("rebase", "--continue")

        result = repo.run_check(check_script, shell)

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_multi_commit_pr_compares_the_whole_diff(self, repo: Repo, check_script: str, shell: str) -> None:
        repo.write("lock.txt", pins("old", 5))
        repo.land_on_main("seed")
        repo.start_pr()
        repo.write("lock.txt", pins("old", 5) + pins("pr", 20))
        repo.commit("pr: add pins")
        repo.write("notes.md", "notes\n" * 10)
        repo.commit("pr: add notes")
        repo.back_up()
        repo.git("checkout", "-q", "main")
        repo.write("other.py", "z = 3\n")
        repo.land_on_main("base: unrelated")
        assert repo.rebase().returncode == 0

        assert repo.run_check(check_script, shell).returncode == 0

    def test_file_the_base_already_carries_in_the_prs_exact_version_passes(
        self, repo: Repo, check_script: str, shell: str
    ) -> None:
        repo.write("lock.txt", pins("old", 5))
        repo.write("app.py", "x = 1\n")
        repo.land_on_main("seed")
        repo.start_pr()
        repo.write("lock.txt", pins("old", 5) + pins("pr", 20))
        repo.write("app.py", "x = 2\n")
        repo.commit("pr: change")
        repo.back_up()
        repo.git("checkout", "-q", "main")
        repo.write("lock.txt", pins("old", 5) + pins("pr", 20))
        repo.land_on_main("base: same lockfile")
        assert repo.rebase().returncode == 0

        result = repo.run_check(check_script, shell)

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_file_deleted_by_both_sides_passes(self, repo: Repo, check_script: str, shell: str) -> None:
        repo.write("legacy.py", "old = 1\n" * 30)
        repo.write("app.py", "x = 1\n")
        repo.land_on_main("seed")
        repo.start_pr()
        (repo.dir / "legacy.py").unlink()
        repo.write("app.py", "x = 2\n")
        repo.commit("pr: remove legacy")
        repo.back_up()
        repo.git("checkout", "-q", "main")
        (repo.dir / "legacy.py").unlink()
        repo.land_on_main("base: remove legacy")
        assert repo.rebase().returncode == 0

        result = repo.run_check(check_script, shell)

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_binary_file_is_checked_for_presence_only(self, repo: Repo, check_script: str, shell: str) -> None:
        repo.write("seed.txt", "seed\n")
        repo.land_on_main("seed")
        repo.start_pr()
        repo.write("logo.png", b"\x89PNG\x00\x01\x02" * 50)
        repo.commit("pr: add logo")
        repo.back_up()
        repo.git("checkout", "-q", "main")
        repo.write("other.py", "z = 3\n")
        repo.land_on_main("base: unrelated")
        assert repo.rebase().returncode == 0

        result = repo.run_check(check_script, shell)

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_text_file_that_becomes_binary_is_checked_for_presence_only(
        self, repo: Repo, check_script: str, shell: str
    ) -> None:
        """numstat prints "-" for the rebased side alone: that must not reach the shrink arithmetic."""
        repo.write("data.txt", "seed\n")
        repo.land_on_main("seed")
        repo.start_pr()
        repo.write("data.txt", "seed\n" + "row\n" * 20)
        repo.commit("pr: add rows")
        repo.back_up()
        repo.git("checkout", "-q", "main")
        repo.write("data.txt", b"\x00base\x00")
        repo.land_on_main("base: binary data")
        assert repo.rebase().returncode != 0
        repo.write("data.txt", b"\x00merged\x00")
        repo.git("add", "-A")
        repo.git("rebase", "--continue")

        result = repo.run_check(check_script, shell)

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_prs_with_no_changed_files_pass_trivially(self, repo: Repo, check_script: str, shell: str) -> None:
        repo.write("seed.txt", "seed\n")
        repo.land_on_main("seed")
        repo.start_pr()
        repo.back_up()

        result = repo.run_check(check_script, shell)

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_small_loss_inside_the_tolerance_passes(self, repo: Repo, check_script: str, shell: str) -> None:
        """A one-line conflict that costs a line or two must not trip the shrink rule."""
        conflicting_pins(repo, ("requirements.txt",), pr_pins=6)
        repo.write("requirements.txt", pins("old", 5) + pins("base", 3) + pins("pr", 5))
        repo.git("add", "-A")
        repo.git("rebase", "--continue")

        assert repo.run_check(check_script, shell).returncode == 0

    def test_loss_of_under_half_the_changed_lines_passes(self, repo: Repo, check_script: str, shell: str) -> None:
        conflicting_pins(repo, ("requirements.txt",), pr_pins=40)
        repo.write("requirements.txt", pins("old", 5) + pins("base", 3) + pins("pr", 24))
        repo.git("add", "-A")
        repo.git("rebase", "--continue")

        assert repo.run_check(check_script, shell).returncode == 0

    def test_file_identical_to_the_backup_passes_though_its_diff_shrank(
        self, repo: Repo, check_script: str, shell: str
    ) -> None:
        """The base landed two of the PR's three hunks: the diff shrinks, but nothing the PR wrote was lost."""
        lines = [f"line {i}\n" for i in range(60)]
        repo.write("config.txt", "".join(lines))
        repo.land_on_main("seed")
        changed = [f"pr {i}\n" if i < 10 or 20 <= i < 30 or 40 <= i < 50 else line for i, line in enumerate(lines)]
        repo.start_pr()
        repo.write("config.txt", "".join(changed))
        repo.commit("pr: three hunks")
        repo.back_up()
        repo.git("checkout", "-q", "main")
        repo.write("config.txt", "".join(changed[:30] + lines[30:]))
        repo.land_on_main("base: lands two of the hunks")
        assert repo.rebase().returncode == 0

        result = repo.run_check(check_script, shell)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "DROPPED_FILE_CHECK_PASS\n"


@pytest.mark.parametrize("shell", SHELLS)
class TestFailingRebases:
    def test_incident_replay_reports_both_dropped_lockfiles(self, repo: Repo, check_script: str, shell: str) -> None:
        names = ("requirements.txt", "requirements-dev.txt")
        conflicting_pins(repo, names)
        repo.resolve_taking_base(*names)

        result = repo.run_check(check_script, shell)

        assert result.returncode == 1
        for name in names:
            assert f"DROPPED: {name}" in result.stderr
        assert "DROPPED_FILE_CHECK_FAIL" in result.stderr
        assert "step 6" in result.stderr
        assert "DROPPED_FILE_CHECK_PASS" not in result.stdout

    def test_check_run_from_a_subdirectory_still_reports(self, repo: Repo, check_script: str, shell: str) -> None:
        """Regenerating a lockfile leaves the shell in its directory; the repo-relative paths must still resolve."""
        repo.write("frontend/package.json", "{}\n")
        names = ("requirements.txt", "requirements-dev.txt")
        conflicting_pins(repo, names)
        repo.resolve_taking_base(*names)

        result = repo.run_check(check_script, shell, cwd=repo.dir / "frontend")

        assert result.returncode == 1
        for name in names:
            assert f"DROPPED: {name}" in result.stderr

    def test_only_the_dropped_file_is_reported_when_other_pr_files_survive(
        self, repo: Repo, check_script: str, shell: str
    ) -> None:
        repo.write("requirements.txt", pins("old", 5))
        repo.write("app.py", "x = 1\n")
        repo.land_on_main("seed")
        repo.start_pr()
        repo.write("requirements.txt", pins("old", 5) + pins("pr", 30))
        repo.write("app.py", "x = 1\n" + "y = 2\n" * 20)
        repo.commit("pr: change")
        repo.back_up()
        repo.git("checkout", "-q", "main")
        repo.write("requirements.txt", pins("old", 5) + pins("base", 3))
        repo.land_on_main("base: bump")
        assert repo.rebase().returncode != 0
        repo.resolve_taking_base("requirements.txt")

        result = repo.run_check(check_script, shell)

        assert result.returncode == 1
        assert "DROPPED: requirements.txt" in result.stderr
        assert "app.py" not in result.stderr

    def test_sharp_shrink_is_reported_with_both_counts(self, repo: Repo, check_script: str, shell: str) -> None:
        conflicting_pins(repo, ("requirements.txt",), pr_pins=40)
        repo.write("requirements.txt", pins("old", 5) + pins("base", 3) + pins("pr", 10))
        repo.git("add", "-A")
        repo.git("rebase", "--continue")

        result = repo.run_check(check_script, shell)

        assert result.returncode == 1
        assert "SHRUNK: requirements.txt had 40 changed lines before the rebase and has 10 after it" in result.stderr

    def test_dropped_binary_file_is_reported(self, repo: Repo, check_script: str, shell: str) -> None:
        repo.write("seed.txt", "seed\n")
        repo.land_on_main("seed")
        repo.start_pr()
        repo.write("logo.png", b"\x89PNG\x00\x01\x02" * 50)
        repo.commit("pr: add logo")
        repo.back_up()
        repo.git("checkout", "-q", "main")
        repo.write("logo.png", b"\x89PNG\x00\x09\x09" * 50)
        repo.land_on_main("base: different logo")
        assert repo.rebase().returncode != 0
        repo.resolve_taking_base("logo.png")

        result = repo.run_check(check_script, shell)

        assert result.returncode == 1
        assert "DROPPED: logo.png" in result.stderr

    def test_unusual_file_names_are_matched_literally(self, repo: Repo, check_script: str, shell: str) -> None:
        """A name with spaces, quotes and glob characters is neither mis-quoted nor expanded as a pathspec."""
        name = 'lock [1] "x" *.txt'
        conflicting_pins(repo, (name,))
        repo.resolve_taking_base(name)

        result = repo.run_check(check_script, shell)

        assert result.returncode == 1
        assert f"DROPPED: {name}" in result.stderr

    def test_names_with_tabs_and_newlines_are_read_whole(self, repo: Repo, check_script: str, shell: str) -> None:
        """A leading or trailing tab, or a newline, must not split or trim the path the NUL-separated list carries."""
        names = ("\tlead.txt", "trail.txt\t", "new\nline.txt")
        conflicting_pins(repo, names)
        repo.resolve_taking_base(*names)

        result = repo.run_check(check_script, shell)

        assert result.returncode == 1
        for name in names:
            assert f"DROPPED: {name} is in the PR" in result.stderr

    def test_missing_backup_ref_fails_closed(self, repo: Repo, check_script: str, shell: str) -> None:
        repo.write("seed.txt", "seed\n")
        repo.land_on_main("seed")
        repo.start_pr()

        result = repo.run_check(check_script, shell)

        assert result.returncode == 1
        assert f"{BACKUP_REF} is missing" in result.stderr

    def test_unresolvable_base_ref_fails_closed(self, repo: Repo, check_script: str, shell: str) -> None:
        """No origin/main means no diff can be taken: that is a failure, not an empty file list."""
        repo.write("seed.txt", "seed\n")
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "seed")
        repo.back_up()

        result = repo.run_check(check_script, shell)

        assert result.returncode == 1
        assert "could not read the PR's file list" in result.stderr

    def test_rebase_still_in_progress_fails(self, repo: Repo, check_script: str, shell: str) -> None:
        conflicting_pins(repo, ("requirements.txt",))

        result = repo.run_check(check_script, shell)

        assert result.returncode == 1
        assert "rebase is still in progress" in result.stderr


class TestCommandText:
    """The surrounding prose: where the step sits and how a failure is routed."""

    @pytest.fixture(params=[CANONICAL, RENDERED], ids=["canonical", "rendered"])
    def text(self, request: pytest.FixtureRequest) -> str:
        return request.param.read_text(encoding="utf-8")

    def test_step_sits_between_conflict_resolution_and_the_test_run(self, text: str) -> None:
        assert (
            text.index("4. After each resolution")
            < text.index("4b. **Check the rebase did not drop PR content")
            < text.index("5. When all commits are replayed")
        )

    def test_step_numbering_that_other_tests_slice_on_is_unchanged(self, text: str) -> None:
        assert "7. **Before pushing" in text
        assert "8. **Return to the primary checkout" in text
        assert "6. **On any failure**" in text

    def test_failure_routes_through_the_existing_reset_to_backup_path(self, text: str) -> None:
        step6 = text[text.index("6. **On any failure**") : text.index("7. **Before pushing")]
        assert "dropped-file check failing" in step6
        assert 'git reset --hard "refs/integrate-backup/<PR_NUMBER>"' in step6

    def test_lockfile_resolution_rule_forbids_taking_one_sides_whole_file(self, text: str) -> None:
        step3 = text[
            text.index("3. **Resolve only conflicts you fully understand") : text.index("4. After each resolution")
        ]
        assert "edit the\n   conflicting hunk" in step3
        assert "regenerate the file from\n   the rebased manifests" in step3
        assert "`git checkout --ours <file>` or `--theirs <file>`" in step3

    def test_whole_file_ban_has_no_exception_for_a_sole_pr_change(self, text: str) -> None:
        step3 = text[
            text.index("3. **Resolve only conflicts you fully understand") : text.index("4. After each resolution")
        ]
        assert "has other changes in that file" not in step3
        assert "even when the conflicting hunk is\n   the PR's only change to it" in step3
        assert "it cannot see the\n   `--theirs` way at all" in step3

    def test_check_adds_no_push(self, text: str) -> None:
        assert "git push" not in _step_4b(text)

    def test_only_the_pass_marker_means_continue(self, text: str) -> None:
        step = _step_4b(text)
        assert "`DROPPED_FILE_CHECK_PASS` on stdout: continue to\n   step 5" in step
        assert "Anything else, including no output at all, is a failure" in step

    def test_block_does_not_depend_on_variables_from_an_earlier_block(self, text: str) -> None:
        script = _check_script(text)
        assert script.lstrip().startswith('BACKUP="refs/integrate-backup/42"')
        assert 'BASE="origin/main"' in script
