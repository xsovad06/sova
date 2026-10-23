"""The squash recipes' bash file-set check catches a reset to the wrong base (issue #1251).

`address-pr`, `pr` and `rearrange-commits` each soft-reset a branch and recommit
it. Resetting to `origin/$BASE` instead of the fork point, after the base moved,
recommits the branch's old tree on top of the newer base: every file the base
changed in between is silently reverted, while `git diff "$BACKUP"` stays empty
because the tree itself is unchanged. Each recipe therefore carries the same
file-set check, the bash counterpart of `sova/core/pr_diff_guard.py`'s
`check_silent_revert()`. These tests extract that block from each canonical
command and run it against a real repository, so a recipe edit that breaks the
check fails here rather than in someone's project. The same holds for the
recovery block each recipe runs when its verification fails: it must rescue
uncommitted edits to a branch before its hard reset discards them.
"""

from __future__ import annotations

import re
import subprocess
import textwrap

import pytest

from sova.commands.catalog import get_canonical_dir
from tests.conftest import InvariantRepo

_SQUASH_COMMANDS = ("address-pr", "pr", "rearrange-commits")
_CHECK_MARKER = "REVERTED="
# Everything before this line is per-recipe setup (each resolves the base branch
# and names its backup branch in its own step's terms); from it on, the check
# itself must be identical in every recipe.
_CHECK_START = "OLD_FORK="


_RESCUE_MARKER = "rescue/squash-"
_RESCUE_START = 'if [ -n "$(git status --porcelain)" ]; then'


def _recipe_block(command: str, marker: str, start: str) -> str:
    """Return the one fenced bash block holding `marker`, from `start` on (dropping the per-recipe setup lines)."""
    text = (get_canonical_dir() / f"{command}.md").read_text(encoding="utf-8")
    blocks = re.findall(r"^[ \t]*```bash\n(.*?)^[ \t]*```", text, re.MULTILINE | re.DOTALL)
    matches = [textwrap.dedent(b) for b in blocks if marker in b]
    assert len(matches) == 1, f"{command}.md must hold exactly one block containing {marker!r}, found {len(matches)}"
    block = matches[0]
    assert start in block, f"{command}.md's {marker!r} block must start at {start!r}"
    return block[block.index(start) :]


def _check_body(command: str) -> str:
    """Return the file-set check from its fenced bash block, without the per-recipe setup lines."""
    return _recipe_block(command, _CHECK_MARKER, _CHECK_START)


def _rescue_body(command: str) -> str:
    """Return the recovery block (rescue uncommitted edits, then hard reset), without its BACKUP= setup line."""
    return _recipe_block(command, _RESCUE_MARKER, _RESCUE_START)


def _runnable(command: str, backup: str) -> str:
    return f"set -euo pipefail\nBASE=main\nBACKUP={backup}\n{_check_body(command)}"


def test_every_squash_recipe_carries_the_same_check() -> None:
    bodies = {cmd: _check_body(cmd).strip() for cmd in _SQUASH_COMMANDS}
    assert len(set(bodies.values())) == 1, "the file-set check drifted between recipes"


def _branch_behind_a_moved_base(repo: InvariantRepo) -> str:
    """Fork a branch, then land a sibling PR on main. Returns the branch's own commit SHA."""
    repo.write("requirements.txt", "pkg==1.13.0\n")
    repo.land_on_main("chore(deps): pin pkg")
    repo.start_branch()
    repo.write("feature.py", "VALUE = 1\n")
    sha = repo.commit("feat(core): add feature")
    repo.git("checkout", "-q", "main")
    repo.write("requirements.txt", "pkg==1.15.0\n")
    repo.write("tests/test_sibling.py", "def test_sibling():\n    pass\n")
    repo.land_on_main("fix(core): sibling fix")
    repo.git("checkout", "-q", "feat/work")
    repo.git("fetch", "-q", "origin")
    repo.git("branch", "backup/test")
    return sha


def _run_check(repo: InvariantRepo, command: str, backup: str = "backup/test") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", _runnable(command, backup)],
        cwd=repo.dir,
        env=repo.env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("command", _SQUASH_COMMANDS)
def test_reset_to_moved_base_is_reported(invariant_repo: InvariantRepo, command: str) -> None:
    """The incident recipe: soft reset to origin/main after main moved, then recommit."""
    sha = _branch_behind_a_moved_base(invariant_repo)
    invariant_repo.git("reset", "-q", "--soft", "origin/main")
    invariant_repo.git("commit", "-q", "-C", sha)

    result = _run_check(invariant_repo, command)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "requirements.txt" in result.stderr
    assert "tests/test_sibling.py" in result.stderr


@pytest.mark.parametrize("command", _SQUASH_COMMANDS)
def test_reset_to_fork_point_passes(invariant_repo: InvariantRepo, command: str) -> None:
    sha = _branch_behind_a_moved_base(invariant_repo)
    fork_point = invariant_repo.git("merge-base", "HEAD", "origin/main")
    invariant_repo.git("reset", "-q", "--soft", fork_point)
    invariant_repo.git("commit", "-q", "-C", sha)

    result = _run_check(invariant_repo, command)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("command", _SQUASH_COMMANDS)
def test_unresolvable_backup_fails_closed(invariant_repo: InvariantRepo, command: str) -> None:
    """A typo'd backup name must stop the recipe, never read as "nothing reverted"."""
    _branch_behind_a_moved_base(invariant_repo)

    result = _run_check(invariant_repo, command, backup="backup/does-not-exist")

    assert result.returncode != 0
    assert "STOP" in result.stderr


def test_every_squash_recipe_carries_the_same_rescue() -> None:
    bodies = {cmd: _rescue_body(cmd).strip() for cmd in _SQUASH_COMMANDS}
    assert len(set(bodies.values())) == 1, "the recovery block drifted between recipes"


def _run_rescue(repo: InvariantRepo, command: str) -> subprocess.CompletedProcess[str]:
    script = f"set -euo pipefail\nBACKUP=backup/test\n{_rescue_body(command)}"
    return subprocess.run(["bash", "-c", script], cwd=repo.dir, env=repo.env, capture_output=True, text=True)


@pytest.mark.parametrize("command", _SQUASH_COMMANDS)
def test_recovery_rescues_edits_made_during_recreation(invariant_repo: InvariantRepo, command: str) -> None:
    """A failed verification with edits only in the working tree must not lose them to the hard reset."""
    _branch_behind_a_moved_base(invariant_repo)
    backup_sha = invariant_repo.git("rev-parse", "backup/test")
    fork_point = invariant_repo.git("merge-base", "HEAD", "origin/main")
    invariant_repo.git("reset", "-q", "--soft", fork_point)
    invariant_repo.git("reset", "-q", "HEAD", ".")
    # Recreation was interrupted: a tracked edit and a new file exist nowhere but the working tree.
    invariant_repo.write("feature.py", "VALUE = 2\n")
    invariant_repo.write("notes.txt", "made during recreation\n")

    result = _run_rescue(invariant_repo, command)

    assert result.returncode == 0, result.stdout + result.stderr
    assert invariant_repo.git("rev-parse", "HEAD") == backup_sha
    assert invariant_repo.git("status", "--porcelain") == ""
    rescues = invariant_repo.git("branch", "--list", "rescue/squash-*", "--format=%(refname:short)").splitlines()
    assert len(rescues) == 1, rescues
    assert invariant_repo.git("show", f"{rescues[0]}:feature.py") == "VALUE = 2"
    assert invariant_repo.git("show", f"{rescues[0]}:notes.txt") == "made during recreation"


@pytest.mark.parametrize("command", _SQUASH_COMMANDS)
def test_recovery_on_clean_tree_creates_no_rescue_branch(invariant_repo: InvariantRepo, command: str) -> None:
    _branch_behind_a_moved_base(invariant_repo)
    backup_sha = invariant_repo.git("rev-parse", "backup/test")
    invariant_repo.git("reset", "-q", "--soft", "origin/main")
    invariant_repo.git("commit", "-q", "-m", "feat(core): wrong-base squash")

    result = _run_rescue(invariant_repo, command)

    assert result.returncode == 0, result.stdout + result.stderr
    assert invariant_repo.git("rev-parse", "HEAD") == backup_sha
    assert invariant_repo.git("branch", "--list", "rescue/squash-*") == ""
