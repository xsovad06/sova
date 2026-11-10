"""Tests for invariants/no-double-dash.sh.

Exercises all code paths in the invariant script by creating temporary
git repos with controlled diffs and running the script against them, in both
modes: the default one that checks lines added against origin/<base>, and
``--all``, which checks every tracked file. ``--all`` over this repository itself
runs in ``make lint`` and CI's invariants job (``make lint-tree``), not here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "invariants" / "no-double-dash.sh"

_DD = " -" + "- "
_DD_EOL = _DD.rstrip()  # the same marker with no trailing space, for end-of-line cases


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(tmp_path: Path) -> Path:
    """Create a git repo with a bare origin and an initial commit on 'main'."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", str(bare))
    readme = repo / "README.md"
    readme.write_text("# Hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _run_invariant(repo: Path, base: str = "main") -> subprocess.CompletedProcess[str]:
    return _run_script(str(repo), base)


def _run_all(repo: Path) -> subprocess.CompletedProcess[str]:
    """Run the whole-tree mode: every tracked file, regardless of what a diff touches."""
    return _run_script("--all", str(repo))


def _commit(repo: Path, relpath: str, content: str) -> None:
    """Write ``content`` to ``relpath`` (creating parents) and commit it."""
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", relpath)
    _git(repo, "commit", "-m", f"add {relpath}")


class TestNoChangedFiles:
    def test_no_changes_exits_zero(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        result = _run_invariant(repo)
        assert result.returncode == 0


class TestChangedLinesVerdict:
    """One file added on the branch: whether the default mode flags it."""

    @pytest.mark.parametrize(
        ("relpath", "content"),
        [
            pytest.param("data.json", f'{{"key": "a{_DD}b"}}\n', id="non-prose-file"),
            pytest.param("app.py", "# Fixed: updated logic\n", id="python-clean"),
            # Double dashes without surrounding spaces are not the marker (e.g. --flag).
            pytest.param("app.py", "parser.add_argument('--verbose')\n", id="python-flag-without-spaces"),
            pytest.param("page.html", "<p>Hello, world</p>\n", id="html-clean"),
            pytest.param("doc.md", '# Title\n\n```bash\necho "a -- b"\n```\n', id="markdown-backtick-fence"),
            pytest.param("doc.md", f"# Title\n\n~~~\ncommand{_DD}flag\n~~~\n", id="markdown-tilde-fence"),
            pytest.param("doc.md", "Use `a -- b` for the flag.\n", id="markdown-inline-code"),
            pytest.param("doc.md", "Use ``a -- b`` for the flag.\n", id="markdown-double-backtick-code"),
            # A double-backtick span containing single backticks is stripped whole.
            pytest.param("doc.md", "Use ``git reset --hard`` to reset.\n", id="single-backtick-inside-double"),
            pytest.param("doc.md", "Run ``a `b -- c` d`` here.\n", id="nested-backtick-with-marker"),
            # The POSIX end-of-options marker is exempt outside markdown.
            pytest.param("app.py", "# Run: git checkout -- file.txt\n", id="python-git-end-of-options"),
            pytest.param("app.py", "# grep -- pattern file\n", id="python-grep-end-of-options"),
            pytest.param("page.html", "<!-- git diff -- file.txt -->\n", id="html-end-of-options"),
        ],
    )
    def test_passes(self, tmp_path: Path, relpath: str, content: str) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, relpath, content)
        assert _run_invariant(repo).returncode == 0

    @pytest.mark.parametrize(
        ("relpath", "content"),
        [
            pytest.param("app.py", f"# Fixed{_DD}updated logic\n", id="python-prose"),
            pytest.param("page.html", f"<p>Hello{_DD}world</p>\n", id="html-prose"),
            pytest.param(
                "doc.md", f"# Title\n\nThis{_DD}is prose\n\n```bash\necho ok\n```\n", id="markdown-prose-outside-fence"
            ),
            # Text outside inline code on the same line is still checked.
            pytest.param("doc.md", f"Check `flag` usage{_DD}see docs\n", id="markdown-prose-around-inline-code"),
            # An unclosed code fence must not hide violations after it.
            pytest.param(
                "doc.md", f"# Title\n\n```bash\necho ok\n\nProse{_DD}violation here\n", id="markdown-unmatched-fence"
            ),
            # The end-of-options heuristic applies only outside markdown.
            pytest.param("doc.md", "Run git checkout -- file to reset.\n", id="markdown-end-of-options-is-prose"),
        ],
    )
    def test_fails(self, tmp_path: Path, relpath: str, content: str) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, relpath, content)
        result = _run_invariant(repo)
        assert result.returncode == 1
        assert relpath in result.stdout


class TestFileNotOnDisk:
    def test_deleted_file_skipped(self, tmp_path: Path) -> None:
        """Files that appear in diff but are deleted on disk are skipped."""
        repo = _init_repo(tmp_path)
        f = repo / "temp.md"
        f.write_text(f"content{_DD}here\n")
        _git(repo, "add", "temp.md")
        _git(repo, "commit", "-m", "add temp")
        _git(repo, "rm", "temp.md")
        _git(repo, "commit", "-m", "rm temp")
        _git(repo, "checkout", "-b", "feature")
        _git(repo, "checkout", "main")
        result = _run_invariant(repo)
        assert result.returncode == 0


class TestMultipleFiles:
    def test_violation_in_one_of_many_files(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "clean.py").write_text("# Good comment\n")
        (repo / "bad.py").write_text(f"# Bad{_DD}comment\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "add files")
        result = _run_invariant(repo)
        assert result.returncode == 1
        assert "bad.py" in result.stdout
        assert "clean.py" not in result.stdout

    def test_multiple_violations_all_reported(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "a.py").write_text(f"# First{_DD}bad\n")
        (repo / "b.md").write_text(f"Also{_DD}bad\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "add files")
        result = _run_invariant(repo)
        assert result.returncode == 1
        assert "a.py" in result.stdout
        assert "b.md" in result.stdout


class TestDiffOnlyNewLines:
    def test_only_added_lines_checked(self, tmp_path: Path) -> None:
        """Pre-existing lines with double dashes are not flagged."""
        repo = _init_repo(tmp_path)
        f = repo / "existing.py"
        f.write_text(f"# old{_DD}line\n")
        _git(repo, "add", "existing.py")
        _git(repo, "commit", "-m", "baseline")
        _git(repo, "push", "origin", "main")
        f.write_text(f"# old{_DD}line\n# new clean line\n")
        _git(repo, "add", "existing.py")
        _git(repo, "commit", "-m", "add clean line")
        result = _run_invariant(repo)
        assert result.returncode == 0

    def test_new_bad_line_in_existing_file(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        f = repo / "existing.py"
        f.write_text("# clean line\n")
        _git(repo, "add", "existing.py")
        _git(repo, "commit", "-m", "baseline")
        f.write_text(f"# clean line\n# bad{_DD}addition\n")
        _git(repo, "add", "existing.py")
        _git(repo, "commit", "-m", "add bad line")
        result = _run_invariant(repo)
        assert result.returncode == 1
        assert "existing.py:2" in result.stdout


class TestHelpFlag:
    def test_help_exits_zero(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert "Usage:" in result.stdout

    def test_help_documents_whole_tree_mode(self) -> None:
        assert "--all" in _run_script("--help").stdout

    @pytest.mark.parametrize("args", [[], ["--all"]])
    def test_missing_worktree_argument_is_a_usage_error(self, args: list[str]) -> None:
        result = _run_script(*args)
        assert result.returncode == 2
        assert "Usage:" in result.stderr


class TestOutputFormat:
    def test_failure_message_format(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "f.py").write_text(f"# x{_DD}y\n")
        _git(repo, "add", "f.py")
        _git(repo, "commit", "-m", "add")
        result = _run_invariant(repo)
        assert result.returncode == 1
        assert "FAIL: Double-dash separator" in result.stdout
        assert "use colon, period, or parentheses" in result.stdout


# One prose occurrence per file type the invariant covers, in that type's own comment or text syntax.
_PROSE_BY_PATH = [
    pytest.param("doc.md", f"Intro{_DD}text\n", id="markdown"),
    pytest.param("app.py", f"# x{_DD}y\n", id="python"),
    pytest.param("page.html", f"<p>a{_DD}b</p>\n", id="html"),
    pytest.param("run.sh", f"# x{_DD}y\n", id="shell"),
    pytest.param("app.js", f"// x{_DD}y\n", id="javascript"),
    pytest.param("ci.yml", f"# x{_DD}y\n", id="yaml"),
    pytest.param("cfg.toml", f"# x{_DD}y\n", id="toml"),
    pytest.param("style.css", f"/* x{_DD}y */\n", id="css"),
    pytest.param(".githooks/pre-push", f'echo "x{_DD}y"\n', id="git-hook"),
]


class TestFileTypesCoverage:
    """Both modes cover the same set of file types."""

    @pytest.mark.parametrize(("relpath", "content"), _PROSE_BY_PATH)
    def test_changed_lines_mode_flags_every_type(self, tmp_path: Path, relpath: str, content: str) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, relpath, content)
        result = _run_invariant(repo)
        assert result.returncode == 1
        assert relpath in result.stdout

    @pytest.mark.parametrize(("relpath", "content"), _PROSE_BY_PATH)
    def test_whole_tree_mode_flags_every_type(self, tmp_path: Path, relpath: str, content: str) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, relpath, content)
        result = _run_all(repo)
        assert result.returncode == 1
        assert relpath in result.stdout

    @pytest.mark.parametrize("relpath", ["data.json", "notes.txt"])
    def test_other_file_types_are_not_scanned(self, tmp_path: Path, relpath: str) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, relpath, f"a{_DD}b\n")
        assert _run_all(repo).returncode == 0
        assert _run_invariant(repo).returncode == 0


class TestWholeTreeMode:
    """``--all`` closes the gap the default mode leaves: it ignores what a diff touches."""

    def test_flags_prose_that_no_diff_touches(self, tmp_path: Path) -> None:
        """Negative control: a pre-existing prose occurrence passes the default mode and fails ``--all``."""
        repo = _init_repo(tmp_path)
        _commit(repo, "legacy.py", f"# Old{_DD}comment\n")
        _git(repo, "push", "origin", "main")
        assert _run_invariant(repo).returncode == 0
        result = _run_all(repo)
        assert result.returncode == 1
        assert "legacy.py:1" in result.stdout

    def test_failure_message_and_report_format(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "pkg/mod.py", f"x = 1\n# bad{_DD}comment\n")
        result = _run_all(repo)
        assert result.returncode == 1
        assert "FAIL: Double-dash separator" in result.stdout
        assert "use colon, period, or parentheses" in result.stdout
        assert "pkg/mod.py:2" in result.stdout

    def test_every_violation_is_reported(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "a.py", f"# one{_DD}x\n# two{_DD}y\n")
        _commit(repo, "b.md", f"Prose{_DD}here\n")
        result = _run_all(repo)
        assert "a.py:1" in result.stdout
        assert "a.py:2" in result.stdout
        assert "b.md:1" in result.stdout

    def test_empty_repo_passes(self, tmp_path: Path) -> None:
        repo = tmp_path / "empty"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        assert _run_all(repo).returncode == 0

    def test_needs_no_origin_remote(self, tmp_path: Path) -> None:
        repo = tmp_path / "lonely"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "test@test.com")
        _git(repo, "config", "user.name", "Test")
        _commit(repo, "x.md", f"a{_DD}b\n")
        assert _run_all(repo).returncode == 1

    def test_untracked_files_are_ignored(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "scratch.py").write_text(f"# x{_DD}y\n")
        assert _run_all(repo).returncode == 0

    def test_tracked_file_deleted_on_disk_is_skipped(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "gone.py", f"# x{_DD}y\n")
        (repo / "gone.py").unlink()
        assert _run_all(repo).returncode == 0


class TestWholeTreeExemptions:
    """Positive controls: the same text that fails as prose passes where it is literal syntax."""

    @pytest.mark.parametrize(
        ("relpath", "content"),
        [
            pytest.param("doc.md", f"Intro\n\n```bash\ngit checkout{_DD}file\n```\n", id="markdown-fence"),
            pytest.param("doc.md", f"Intro\n\n~~~\nfoo{_DD}bar\n~~~\n", id="markdown-tilde-fence"),
            pytest.param("doc.md", f"Run `git checkout{_DD}file` to reset.\n", id="markdown-inline-code"),
            pytest.param("doc.md", f"Run ``a `b{_DD}c` d`` here.\n", id="markdown-double-backtick"),
            pytest.param("app.py", f"# Run `git log{_DD}path` for history\n", id="python-inline-code"),
            pytest.param("app.py", f"# Run: git checkout{_DD}file.txt\n", id="python-cli-command"),
            pytest.param("run.sh", f'out=$(git -C "$dir" diff main{_DD}"$file")\n', id="shell-cli-command"),
            pytest.param("run.sh", f'git_in_worktree log -n 1{_DD}"$file"\n', id="shell-git-wrapper"),
            pytest.param("ci.yml", f"      - run: git diff --name-only origin/main...HEAD{_DD}docs/\n", id="yaml-cli"),
            pytest.param("app.py", "# Fixed: updated logic\nparser.add_argument('--verbose')\n", id="clean-tree"),
            # A real end-of-options marker at end of line stays exempt, not just the interior form.
            pytest.param(
                "app.py", f"# Run: git checkout{_DD_EOL}\n#   then stage the usual files\n", id="end-of-options-at-eol"
            ),
            # A line ending in a third dash is not the space-dash-dash marker.
            pytest.param("doc.md", f"Intro\n\nHorizontal rule follows{_DD_EOL}-\n", id="triple-dash-at-eol"),
        ],
    )
    def test_literal_syntax_passes(self, tmp_path: Path, relpath: str, content: str) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, relpath, content)
        result = _run_all(repo)
        assert result.returncode == 0, result.stdout

    @pytest.mark.parametrize(
        ("relpath", "content", "location"),
        [
            # The control for the passing cases above: only the surroundings differ.
            pytest.param("doc.md", f"Intro\n\nSee the checkout{_DD}file section.\n", None, id="same-text-as-prose"),
            pytest.param(
                "doc.md", f"```bash\necho ok\n```\n\nProse{_DD}after the fence\n", "doc.md:5", id="prose-next-to-fence"
            ),
            pytest.param("doc.md", f"```bash\necho ok\n\nProse{_DD}violation\n", None, id="unmatched-fence"),
            pytest.param(
                "doc.md", f"Run git checkout{_DD}file to reset.\n", None, id="markdown-end-of-options-is-prose"
            ),
            pytest.param("my notes.md", f"a{_DD}b\n", "my notes.md:1", id="filename-with-spaces"),
            # A soft-wrapped prose dash has no trailing space, so only the end-of-line form of the marker matches it.
            pytest.param(
                "doc.md",
                f"Intro\n\nSee the equivalent{_DD_EOL}\n   dependencies section.\n",
                "doc.md:3",
                id="prose-dash-at-eol",
            ),
            # Trailing whitespace after the marker must not defeat the end-of-line match.
            pytest.param(
                "doc.md",
                f"Intro\n\nSee the equivalent{_DD_EOL}   \n   dependencies section.\n",
                None,
                id="eol-trailing-space",
            ),
        ],
    )
    def test_prose_fails(self, tmp_path: Path, relpath: str, content: str, location: str | None) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, relpath, content)
        result = _run_all(repo)
        assert result.returncode == 1
        if location is not None:
            assert location in result.stdout

    def test_script_does_not_flag_itself(self, tmp_path: Path) -> None:
        """The invariant lives under invariants/ and names the marker, so it must stay clean."""
        repo = _init_repo(tmp_path)
        _commit(repo, "invariants/no-double-dash.sh", SCRIPT.read_text())
        assert _run_all(repo).returncode == 0


class TestEndOfLineMarker:
    """A soft-wrapped prose dash has no trailing space, so the interior form of the marker
    never matches it. It must still be flagged when it is the last thing on the line."""

    def test_changed_lines_mode_flags_prose_dash_at_end_of_line(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        f = repo / "app.py"
        f.write_text(f"# Fixed{_DD}updated logic\n")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "add prose")
        f.write_text(f"# Fixed{_DD}updated logic\n# or equivalent{_DD_EOL}\n#   see details\n")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "add eol variant")
        result = _run_invariant(repo)
        assert result.returncode == 1
        assert "app.py:2" in result.stdout
