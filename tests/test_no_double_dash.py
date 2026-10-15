"""Tests for invariants/no-double-dash.sh.

Exercises all code paths in the invariant script by creating temporary
git repos with controlled diffs and running the script against them, in both
modes: the default one that checks lines added against origin/<base>, and
``--all``, which checks every tracked file. ``TestRepositoryHasNoProseDoubleDash``
runs ``--all`` over this repository itself, so a prose occurrence cannot survive
just because no change touched its line.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "invariants" / "no-double-dash.sh"

_DD = " -" + "- "


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

    def test_non_prose_file_ignored(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "data.json").write_text(f'{{"key": "a{_DD}b"}}\n')
        _git(repo, "add", "data.json")
        _git(repo, "commit", "-m", "add json")
        result = _run_invariant(repo)
        assert result.returncode == 0


class TestPythonFileViolations:
    def test_python_double_dash_fails(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "app.py").write_text(f"# Fixed{_DD}updated logic\n")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "add py")
        result = _run_invariant(repo)
        assert result.returncode == 1
        assert "app.py" in result.stdout
        assert "FAIL" in result.stdout

    def test_python_no_violation_passes(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "app.py").write_text("# Fixed: updated logic\n")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "add py")
        result = _run_invariant(repo)
        assert result.returncode == 0

    def test_python_double_dash_no_spaces_passes(self, tmp_path: Path) -> None:
        """Double dashes without surrounding spaces are not flagged (e.g. --flag)."""
        repo = _init_repo(tmp_path)
        (repo / "app.py").write_text("parser.add_argument('--verbose')\n")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "add py")
        result = _run_invariant(repo)
        assert result.returncode == 0


class TestHtmlFileViolations:
    def test_html_double_dash_fails(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "page.html").write_text(f"<p>Hello{_DD}world</p>\n")
        _git(repo, "add", "page.html")
        _git(repo, "commit", "-m", "add html")
        result = _run_invariant(repo)
        assert result.returncode == 1
        assert "page.html" in result.stdout

    def test_html_clean_passes(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "page.html").write_text("<p>Hello, world</p>\n")
        _git(repo, "add", "page.html")
        _git(repo, "commit", "-m", "add html")
        result = _run_invariant(repo)
        assert result.returncode == 0


class TestMarkdownCodeBlockExclusion:
    def test_md_code_block_excluded(self, tmp_path: Path) -> None:
        """Double dashes inside fenced code blocks are not flagged."""
        repo = _init_repo(tmp_path)
        md = repo / "doc.md"
        md.write_text('# Title\n\n```bash\necho "a -- b"\n```\n')
        _git(repo, "add", "doc.md")
        _git(repo, "commit", "-m", "add doc")
        result = _run_invariant(repo)
        assert result.returncode == 0

    def test_md_tilde_code_block_excluded(self, tmp_path: Path) -> None:
        """Tilde-fenced code blocks are also excluded."""
        repo = _init_repo(tmp_path)
        md = repo / "doc.md"
        md.write_text(f"# Title\n\n~~~\ncommand{_DD}flag\n~~~\n")
        _git(repo, "add", "doc.md")
        _git(repo, "commit", "-m", "add doc")
        result = _run_invariant(repo)
        assert result.returncode == 0

    def test_md_prose_outside_code_block_fails(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        md = repo / "doc.md"
        md.write_text(f"# Title\n\nThis{_DD}is prose\n\n```bash\necho ok\n```\n")
        _git(repo, "add", "doc.md")
        _git(repo, "commit", "-m", "add doc")
        result = _run_invariant(repo)
        assert result.returncode == 1
        assert "doc.md" in result.stdout


class TestMarkdownInlineCodeExclusion:
    def test_inline_code_excluded(self, tmp_path: Path) -> None:
        """Double dashes inside backtick inline code are not flagged."""
        repo = _init_repo(tmp_path)
        md = repo / "doc.md"
        md.write_text("Use `a -- b` for the flag.\n")
        _git(repo, "add", "doc.md")
        _git(repo, "commit", "-m", "add doc")
        result = _run_invariant(repo)
        assert result.returncode == 0

    def test_double_backtick_inline_code_excluded(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        md = repo / "doc.md"
        md.write_text("Use ``a -- b`` for the flag.\n")
        _git(repo, "add", "doc.md")
        _git(repo, "commit", "-m", "add doc")
        result = _run_invariant(repo)
        assert result.returncode == 0

    def test_prose_around_inline_code_still_checked(self, tmp_path: Path) -> None:
        """Text outside inline code on the same line is still checked."""
        repo = _init_repo(tmp_path)
        md = repo / "doc.md"
        md.write_text(f"Check `flag` usage{_DD}see docs\n")
        _git(repo, "add", "doc.md")
        _git(repo, "commit", "-m", "add doc")
        result = _run_invariant(repo)
        assert result.returncode == 1


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


class TestNestedBacktickSpans:
    def test_single_backtick_inside_double_backtick_span(self, tmp_path: Path) -> None:
        """Double-backtick spans containing single backticks are fully stripped."""
        repo = _init_repo(tmp_path)
        md = repo / "doc.md"
        md.write_text("Use ``git reset --hard`` to reset.\n")
        _git(repo, "add", "doc.md")
        _git(repo, "commit", "-m", "add doc")
        result = _run_invariant(repo)
        assert result.returncode == 0

    def test_nested_backtick_with_dash_inside(self, tmp_path: Path) -> None:
        """A single backtick inside double-backtick span with ` -- ` is excluded."""
        repo = _init_repo(tmp_path)
        md = repo / "doc.md"
        md.write_text("Run ``a `b -- c` d`` here.\n")
        _git(repo, "add", "doc.md")
        _git(repo, "commit", "-m", "add doc")
        result = _run_invariant(repo)
        assert result.returncode == 0


class TestUnmatchedFence:
    def test_unmatched_fence_does_not_suppress_violations(self, tmp_path: Path) -> None:
        """An unclosed code fence must not hide violations after it."""
        repo = _init_repo(tmp_path)
        md = repo / "doc.md"
        md.write_text(f"# Title\n\n```bash\necho ok\n\nProse{_DD}violation here\n")
        _git(repo, "add", "doc.md")
        _git(repo, "commit", "-m", "add doc")
        result = _run_invariant(repo)
        assert result.returncode == 1
        assert "doc.md" in result.stdout


class TestPosixEndOfOpts:
    def test_git_checkout_double_dash_passes(self, tmp_path: Path) -> None:
        """POSIX end-of-options marker in Python comments is not flagged."""
        repo = _init_repo(tmp_path)
        (repo / "app.py").write_text("# Run: git checkout -- file.txt\n")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "add py")
        result = _run_invariant(repo)
        assert result.returncode == 0

    def test_grep_double_dash_passes(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "app.py").write_text("# grep -- pattern file\n")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "add py")
        result = _run_invariant(repo)
        assert result.returncode == 0

    def test_prose_double_dash_still_fails_in_python(self, tmp_path: Path) -> None:
        """Non-POSIX prose ` -- ` in Python files is still flagged."""
        repo = _init_repo(tmp_path)
        (repo / "app.py").write_text(f"# Fixed{_DD}updated logic\n")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "add py")
        result = _run_invariant(repo)
        assert result.returncode == 1

    def test_posix_heuristic_not_applied_to_markdown(self, tmp_path: Path) -> None:
        """POSIX heuristic only applies to non-markdown files."""
        repo = _init_repo(tmp_path)
        md = repo / "doc.md"
        md.write_text("Run git checkout -- file to reset.\n")
        _git(repo, "add", "doc.md")
        _git(repo, "commit", "-m", "add doc")
        result = _run_invariant(repo)
        assert result.returncode == 1

    def test_html_posix_passes(self, tmp_path: Path) -> None:
        """POSIX end-of-options in HTML comments is not flagged."""
        repo = _init_repo(tmp_path)
        (repo / "page.html").write_text("<!-- git diff -- file.txt -->\n")
        _git(repo, "add", "page.html")
        _git(repo, "commit", "-m", "add html")
        result = _run_invariant(repo)
        assert result.returncode == 0


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

    def test_clean_tree_passes(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "app.py", "# Fixed: updated logic\nparser.add_argument('--verbose')\n")
        assert _run_all(repo).returncode == 0

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

    def test_filename_with_spaces(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "my notes.md", f"a{_DD}b\n")
        result = _run_all(repo)
        assert result.returncode == 1
        assert "my notes.md:1" in result.stdout


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
        ],
    )
    def test_literal_syntax_passes(self, tmp_path: Path, relpath: str, content: str) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, relpath, content)
        result = _run_all(repo)
        assert result.returncode == 0, result.stdout

    def test_same_text_as_prose_fails(self, tmp_path: Path) -> None:
        """The control for the cases above: only the surroundings differ."""
        repo = _init_repo(tmp_path)
        _commit(repo, "doc.md", f"Intro\n\nSee the checkout{_DD}file section.\n")
        assert _run_all(repo).returncode == 1

    def test_markdown_prose_next_to_a_fence_still_fails(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "doc.md", f"```bash\necho ok\n```\n\nProse{_DD}after the fence\n")
        result = _run_all(repo)
        assert result.returncode == 1
        assert "doc.md:5" in result.stdout

    def test_unmatched_fence_does_not_hide_prose(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "doc.md", f"```bash\necho ok\n\nProse{_DD}violation\n")
        assert _run_all(repo).returncode == 1

    def test_end_of_options_heuristic_is_not_applied_to_markdown(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "doc.md", f"Run git checkout{_DD}file to reset.\n")
        assert _run_all(repo).returncode == 1

    def test_script_does_not_flag_itself(self, tmp_path: Path) -> None:
        """The invariant lives under invariants/ and names the marker, so it must stay clean."""
        repo = _init_repo(tmp_path)
        _commit(repo, "invariants/no-double-dash.sh", SCRIPT.read_text())
        assert _run_all(repo).returncode == 0


_DD_EOL = _DD.rstrip()  # the same marker with no trailing space, for end-of-line cases


class TestEndOfLineMarker:
    """A soft-wrapped prose dash has no trailing space, so the interior form of the marker
    never matches it. It must still be flagged when it is the last thing on the line."""

    def test_whole_tree_flags_prose_dash_at_end_of_line(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "doc.md", f"Intro\n\nSee the equivalent{_DD_EOL}\n   dependencies section.\n")
        result = _run_all(repo)
        assert result.returncode == 1
        assert "doc.md:3" in result.stdout

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

    def test_trailing_whitespace_after_marker_still_flagged(self, tmp_path: Path) -> None:
        """Trailing whitespace after the marker must not defeat the end-of-line match."""
        repo = _init_repo(tmp_path)
        _commit(repo, "doc.md", f"Intro\n\nSee the equivalent{_DD_EOL}   \n   dependencies section.\n")
        assert _run_all(repo).returncode == 1

    def test_posix_end_of_opts_at_end_of_line_passes(self, tmp_path: Path) -> None:
        """A real end-of-options marker at end of line is still exempt, not just the interior form."""
        repo = _init_repo(tmp_path)
        _commit(repo, "app.py", f"# Run: git checkout{_DD_EOL}\n#   then stage the usual files\n")
        assert _run_all(repo).returncode == 0

    def test_triple_dash_at_end_of_line_not_flagged(self, tmp_path: Path) -> None:
        """A line ending in a third dash is not the space-dash-dash marker and must not be flagged."""
        repo = _init_repo(tmp_path)
        _commit(repo, "doc.md", f"Intro\n\nHorizontal rule follows{_DD_EOL}-\n")
        assert _run_all(repo).returncode == 0


class TestRepositoryHasNoProseDoubleDash:
    """The permanent guard: this repository stays clean no matter which lines a change touches."""

    # Scans every tracked file, so its runtime scales with the repo, not with one
    # unit of work: measured 57s sharing a runner under `-n auto`, overrunning both
    # pyproject's 30s global and the 60s cap CI passes. A marker overrides both, so
    # the cap has to live here rather than on the command line.
    @pytest.mark.timeout(120)
    @pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
    def test_whole_tree_is_clean(self) -> None:
        if not (REPO_ROOT / ".git").exists():
            pytest.skip("not a git checkout (for example an sdist)")
        result = _run_all(REPO_ROOT)
        assert result.returncode == 0, (
            "Spaced double dashes used as punctuation. Replace each with a colon, period, comma or "
            "parentheses (AGENTS.md: No double dashes). Literal syntax such as `git checkout -- <path>` "
            "belongs in backticks or a fenced block.\n" + result.stdout
        )
