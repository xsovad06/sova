"""Tests for invariants/no-double-dash.sh.

Exercises all code paths in the invariant script by creating temporary
git repos with controlled diffs and running the script against them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "invariants" / "no-double-dash.sh"

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


def _run_invariant(repo: Path, base: str = "main") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(repo), base],
        capture_output=True,
        text=True,
        check=False,
    )


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
        md.write_text("# Title\n\n```bash\necho ok\n\nProse -- violation here\n")
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
