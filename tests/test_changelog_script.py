"""Tests for scripts/changelog.py: CHANGELOG.md and the next version from conventional commits (#1388)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "changelog.py"

_spec = importlib.util.spec_from_file_location("changelog_script", SCRIPT)
assert _spec is not None and _spec.loader is not None
changelog = importlib.util.module_from_spec(_spec)
# Registered first: @dataclass resolves the module of the class it decorates through sys.modules.
sys.modules[_spec.name] = changelog
_spec.loader.exec_module(changelog)

Commit = changelog.Commit


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        pytest.param("feat(cli): add a flag", "", Commit("feat", "cli", "add a flag", False), id="scoped"),
        pytest.param("ci: pin actions", "", Commit("ci", None, "pin actions", False), id="scopeless"),
        pytest.param("fix(db)!: drop a column", "", Commit("fix", "db", "drop a column", True), id="bang"),
        pytest.param(
            "feat(core): new API",
            "Body.\n\nBREAKING CHANGE: old API gone",
            Commit("feat", "core", "new API", True),
            id="footer",
        ),
        pytest.param("Merge branch main", "", None, id="not-conventional"),
    ],
)
def test_parse_commit(subject: str, body: str, expected: object) -> None:
    assert changelog.parse_commit(subject, body) == expected


_FIX = Commit("fix", None, "f", False)
_FEAT = Commit("feat", None, "f", False)
_BREAKING = Commit("fix", None, "f", True)
_DOCS = Commit("docs", None, "d", False)


@pytest.mark.parametrize(
    ("current", "commits", "expected"),
    [
        pytest.param(None, [_FIX], (0, 1, 0), id="first-release"),
        pytest.param((0, 1, 0), [_DOCS, _FIX], (0, 1, 1), id="patch"),
        pytest.param((0, 1, 1), [_FIX, _FEAT], (0, 2, 0), id="minor"),
        pytest.param((0, 2, 3), [_BREAKING], (0, 3, 0), id="breaking-before-1.0-is-minor"),
        pytest.param((1, 2, 3), [_FEAT, _BREAKING], (2, 0, 0), id="breaking-after-1.0-is-major"),
    ],
)
def test_next_version(current: tuple[int, int, int] | None, commits: list[object], expected: tuple) -> None:
    assert changelog.next_version(current, commits) == expected


def test_render_lists_only_user_facing_commits_and_rewrites_double_dashes() -> None:
    section = changelog.render_section(
        "0.2.0 (2026-11-27)",
        [
            _DOCS,
            Commit("feat", "cli", "add x " + "-" * 2 + " and y", False),
            Commit("fix", None, "repair z", False),
            _BREAKING,
        ],
    )
    assert section.splitlines() == [
        "## 0.2.0 (2026-11-27)",
        "",
        "**Breaking Changes**",
        "",
        "- f",
        "",
        "**Features**",
        "",
        "- **cli**: add x: and y",
        "",
        "**Fixes**",
        "",
        "- repair z",
    ]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    _git(tmp_path, "init", "-q", "-b", "main")
    for subject in ("feat(cli): first feature", "docs: readme"):
        _git(tmp_path, "commit", "-q", "--allow-empty", "-m", subject)
    _git(tmp_path, "tag", "v0.1.0")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix(db): later fix")
    return tmp_path


def test_changelog_sections_follow_the_tags(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert changelog.main(["--repo", str(repo), "next-version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.1"

    assert changelog.main(["--repo", str(repo), "changelog", "--release", "0.1.1", "--date", "2026-12-01"]) == 0
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    titles = [line for line in text.splitlines() if line.startswith("## ")]
    assert titles[0] == "## 0.1.1 (2026-12-01)"
    assert titles[1].startswith("## 0.1.0 (")
    assert text.index("- **db**: later fix") < text.index("## 0.1.0") < text.index("- **cli**: first feature")
    assert "readme" not in text


def test_next_version_fails_when_nothing_is_unreleased(repo: Path) -> None:
    _git(repo, "tag", "v0.1.1")
    assert changelog.main(["--repo", str(repo), "next-version"]) == 1
