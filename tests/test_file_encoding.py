"""Tests for ``invariants/file-encoding.sh``.

``Path.read_text()``/``Path.write_text()`` with no ``encoding=`` argument uses
the platform's default encoding, not UTF-8. Code under ``sova/`` must pass
``encoding=`` explicitly. The default mode checks only lines a branch adds;
``--all`` checks every tracked file, and ``TestRepositoryHasNoUnencodedTextIo``
runs it over this repository itself, so a call cannot survive just because no
change touched its line.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests.conftest import INVARIANTS_DIR, InvariantRepo

REPO_ROOT = INVARIANTS_DIR.parent
SCRIPT = "file-encoding.sh"
MODULE = "sova/core/example.py"


def add_line(repo: InvariantRepo, line: str, path: str = MODULE) -> subprocess.CompletedProcess[str]:
    """On a new branch, add ``line`` to ``path`` and run the invariant."""
    repo.start_branch()
    repo.write(path, f'"""Example module."""\n\n{line}\n')
    repo.commit("feat(core): example")
    return repo.run(SCRIPT)


def test_script_is_executable_and_handles_help() -> None:
    assert os.access(INVARIANTS_DIR / SCRIPT, os.X_OK)
    result = subprocess.run(["bash", str(INVARIANTS_DIR / SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0


def test_no_changes_passes(invariant_repo: InvariantRepo) -> None:
    invariant_repo.start_branch()
    assert invariant_repo.run(SCRIPT).returncode == 0


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('p.read_text(encoding="utf-8")', id="encoded read"),
        pytest.param('p.read_text("utf-8")', id="positional encoded read"),
        pytest.param('p.read_text("UTF8", "ignore")', id="positional encoded read with errors"),
        pytest.param('p.write_text(text, "utf-8")', id="positional encoded write"),
        pytest.param('p.write_text("ok", encoding="utf-8")', id="a string payload is not the encoding"),
        pytest.param('p.write_text(text, encoding="utf-8")', id="encoded write"),
        pytest.param('p.read_text(encoding="utf-8", errors="ignore")', id="encoded with errors kwarg"),
        pytest.param("# never call p.read_text() in this module", id="a comment"),
        pytest.param('MESSAGE = "p.read_text()"', id="call text inside a string literal"),
        pytest.param('p.read_text(\n    encoding="utf-8",\n)', id="encoded multiline read"),
        pytest.param('p.write_text(\n    "# heading",\n    encoding="utf-8",\n)', id="encoded multiline write"),
        pytest.param("p.read_text()  # file-encoding: legacy compat", id="exempted with a reason"),
        pytest.param(
            "p.read_text(\n    errors='ignore',  # file-encoding: byte-for-byte\n)", id="exempted on an inner line"
        ),
    ],
)
def test_allowed(invariant_repo: InvariantRepo, line: str) -> None:
    result = add_line(invariant_repo, line)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("text = p.read_text()", id="unencoded read"),
        pytest.param('p.write_text("ok")', id="unencoded write"),
        pytest.param("text = p.read_text ()", id="space before the call"),
        pytest.param('p.read_text("latin-1")', id="positional non-utf-8 read"),
        pytest.param('p.write_text(text, "latin-1")', id="positional non-utf-8 write"),
        pytest.param('p.write_text("utf-8")', id="the data argument is not the encoding"),
        pytest.param("p.read_text(*args)", id="an unpacked argument list"),
        pytest.param("def load(p):\n    return p.read_text()", id="indented in a function"),
        pytest.param('p.write_text("# heading")', id="a hash inside the written text"),
        pytest.param("p.read_text(\n    errors='ignore',\n)", id="multiline without an encoding"),
        pytest.param('p.read_text(encoding="latin-1")', id="a non-utf-8 encoding"),
        pytest.param("p.read_text(encoding=enc)", id="an encoding that is not a literal"),
        pytest.param("p.read_text()  # file-encoding:", id="exemption without a reason"),
    ],
)
def test_rejected(invariant_repo: InvariantRepo, line: str) -> None:
    result = add_line(invariant_repo, line)
    assert result.returncode == 1
    assert f"{MODULE}:" in result.stdout


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("tests/test_example.py", id="tests"),
        pytest.param("scripts/tool.py", id="outside the package"),
    ],
)
def test_paths_outside_the_rule(invariant_repo: InvariantRepo, path: str) -> None:
    assert add_line(invariant_repo, "text = p.read_text()", path=path).returncode == 0


def test_an_existing_unencoded_call_does_not_fail_an_unrelated_edit(invariant_repo: InvariantRepo) -> None:
    invariant_repo.write(MODULE, "text = p.read_text()\n")
    invariant_repo.land_on_main("feat(core): legacy module")
    invariant_repo.start_branch()
    invariant_repo.write(MODULE, "text = p.read_text()\nVALUE = 1\n")
    invariant_repo.commit("feat(core): add a constant")
    assert invariant_repo.run(SCRIPT).returncode == 0


def test_a_line_the_base_rewrote_since_the_fork_is_not_blamed_on_the_branch(invariant_repo: InvariantRepo) -> None:
    """Added lines are measured from the fork point, not the base's tip.

    The branch inherits an unencoded call, and another PR then fixes that very line on main. Against the
    tip the branch's old copy of the line would read as a line the branch added.
    """
    invariant_repo.write(MODULE, "text = p.read_text()\n")
    invariant_repo.land_on_main("feat(core): legacy module")
    invariant_repo.start_branch()
    invariant_repo.write(MODULE, "text = p.read_text()\nVALUE = 1\n")
    invariant_repo.commit("feat(core): add a constant")
    invariant_repo.git("checkout", "-q", "main")
    invariant_repo.write(MODULE, 'text = p.read_text(encoding="utf-8")\n')
    invariant_repo.land_on_main("fix(core): encode the legacy read")
    invariant_repo.git("checkout", "-q", "feat/work")

    assert invariant_repo.run(SCRIPT).returncode == 0


def test_a_call_the_branch_adds_is_still_flagged_after_the_base_moved(invariant_repo: InvariantRepo) -> None:
    invariant_repo.start_branch()
    invariant_repo.write(MODULE, "text = p.read_text()\n")
    invariant_repo.commit("feat(core): add a read")
    invariant_repo.git("checkout", "-q", "main")
    invariant_repo.write("sova/core/other.py", "VALUE = 1\n")
    invariant_repo.land_on_main("feat(core): unrelated change on main")
    invariant_repo.git("checkout", "-q", "feat/work")

    assert invariant_repo.run(SCRIPT).returncode == 1


def test_uncommitted_changes_are_checked_too(invariant_repo: InvariantRepo) -> None:
    """Like the other diff-based invariants, it compares the working tree with the base."""
    invariant_repo.start_branch()
    invariant_repo.write(MODULE, "text = p.read_text()\n")
    invariant_repo.git("add", "-N", MODULE)
    assert invariant_repo.run(SCRIPT).returncode == 1


def test_a_failed_comparison_does_not_read_as_a_pass(invariant_repo: InvariantRepo) -> None:
    """With no resolvable base the added lines cannot be checked, which must fail, not pass."""
    invariant_repo.start_branch()
    invariant_repo.git("update-ref", "-d", "refs/remotes/origin/main")
    result = invariant_repo.run(SCRIPT)
    assert result.returncode == 1
    assert "origin/main" in result.stdout


def test_an_unparseable_changed_file_fails(invariant_repo: InvariantRepo) -> None:
    result = add_line(invariant_repo, "def broken(:")
    assert result.returncode == 1
    assert "cannot be parsed" in result.stdout


def test_the_failure_names_the_line_of_the_call(invariant_repo: InvariantRepo) -> None:
    result = add_line(invariant_repo, "VALUE = 1\ntext = p.read_text()")
    assert result.returncode == 1
    assert f"{MODULE}:4: text = p.read_text()" in result.stdout


def run_all(directory: object, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the whole-tree mode: every tracked file, regardless of what a diff touches."""
    return subprocess.run(
        ["bash", str(INVARIANTS_DIR / SCRIPT), "--all", str(directory)],
        env=env,
        capture_output=True,
        text=True,
    )


class TestAllMode:
    def test_flags_an_unencoded_call_no_diff_touches(self, invariant_repo: InvariantRepo) -> None:
        """The default mode grandfathers this call; --all does not."""
        invariant_repo.write(MODULE, "text = p.read_text()\n")
        invariant_repo.land_on_main("feat(core): legacy module")
        invariant_repo.start_branch()
        assert invariant_repo.run(SCRIPT).returncode == 0

        result = run_all(invariant_repo.dir, invariant_repo.env)

        assert result.returncode == 1
        assert f"{MODULE}:1: text = p.read_text()" in result.stdout

    def test_passes_when_every_call_is_encoded_or_exempt(self, invariant_repo: InvariantRepo) -> None:
        invariant_repo.write(
            MODULE,
            'a = p.read_text(encoding="utf-8")\nb = p.write_text("x", encoding="utf-8")\n'
            "c = p.read_text()  # file-encoding: byte-for-byte\n",
        )
        invariant_repo.commit("feat(core): encoded module")

        assert run_all(invariant_repo.dir, invariant_repo.env).returncode == 0

    def test_needs_no_resolvable_base(self, invariant_repo: InvariantRepo) -> None:
        """The whole-tree check never compares against origin/main, so it does not need it."""
        invariant_repo.git("update-ref", "-d", "refs/remotes/origin/main")

        assert run_all(invariant_repo.dir, invariant_repo.env).returncode == 0

    def test_ignores_files_outside_the_rule(self, invariant_repo: InvariantRepo) -> None:
        invariant_repo.write("tests/test_example.py", "text = p.read_text()\n")
        invariant_repo.write("scripts/tool.py", "text = p.read_text()\n")
        invariant_repo.commit("chore(docs): outside the package")

        assert run_all(invariant_repo.dir, invariant_repo.env).returncode == 0

    def test_an_unparseable_tracked_file_fails(self, invariant_repo: InvariantRepo) -> None:
        invariant_repo.write(MODULE, "def broken(:\n")
        invariant_repo.commit("feat(core): broken module")

        result = run_all(invariant_repo.dir, invariant_repo.env)

        assert result.returncode == 1
        assert "cannot be parsed" in result.stdout

    def test_help_documents_the_mode(self) -> None:
        result = subprocess.run(["bash", str(INVARIANTS_DIR / SCRIPT), "--help"], capture_output=True, text=True)
        assert "--all" in result.stdout


class TestRepositoryHasNoUnencodedTextIo:
    """The permanent guard: no ``read_text()``/``write_text()`` under ``sova/`` lacks ``encoding="utf-8"``."""

    # Parses every tracked sova/*.py file, so its runtime scales with the package rather than one unit of work.
    @pytest.mark.timeout(120)
    @pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
    def test_whole_tree_is_clean(self) -> None:
        if not (REPO_ROOT / ".git").exists():
            pytest.skip("not a git checkout (for example an sdist)")

        result = run_all(REPO_ROOT)

        assert result.returncode == 0, (
            'read_text()/write_text() without encoding="utf-8" (AGENTS.md: reading text files). '
            "Pass the encoding, or put '# file-encoding: <reason>' on a line of the call.\n" + result.stdout
        )
