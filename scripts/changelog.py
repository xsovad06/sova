#!/usr/bin/env python3
"""Generate CHANGELOG.md and the next release version from conventional commits.

Two subcommands:

- ``next-version`` prints the version the commits since the latest ``vX.Y.Z`` tag call for: a breaking
  change (``type!:`` or a ``BREAKING CHANGE:`` footer) bumps major, a ``feat`` bumps minor, anything else
  bumps patch. Before 1.0 a breaking change bumps minor instead, since 0.x makes no compatibility promise.
  With no tag yet, the first release is 0.1.0. The maintainer may still release under another number:
  pass it to ``changelog --release``, and tag that.
- ``changelog`` writes CHANGELOG.md: one section per tag, newest first, with the commits after the latest
  tag under "Unreleased" (or under ``--release X.Y.Z`` when cutting that release). Only ``feat`` and
  ``fix`` commits are listed, plus any breaking change, since the rest (docs, tests, chores, CI,
  refactors) does not change what a user installs.

The commit conventions this relies on are enforced by invariants/commit-format.sh. Standard library only.
Exit codes: 0 success, 1 error (no commits to release, git failure), 2 usage error.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

INITIAL_VERSION = (0, 1, 0)
SECTIONS = (("feat", "Features"), ("fix", "Fixes"))
BREAKING_HEADING = "Breaking Changes"
_TAG_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_SUBJECT_RE = re.compile(r"(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?: (?P<description>.+)")
_BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE: ", re.MULTILINE)
# The repo's prose rule (invariants/no-double-dash.sh) applies to CHANGELOG.md too; a handful of older
# commit subjects predate it.
_DOUBLE_DASH = " " + "-" * 2 + " "
_FIELD = "\x1f"
_RECORD = "\x1e"

Version = tuple[int, int, int]


@dataclass(frozen=True)
class Commit:
    type: str
    scope: str | None
    description: str
    breaking: bool


def parse_commit(subject: str, body: str = "") -> Commit | None:
    """Parse a conventional commit subject; None for a subject that does not follow the convention."""
    match = _SUBJECT_RE.fullmatch(subject.strip())
    if match is None:
        return None
    breaking = bool(match.group("bang")) or bool(_BREAKING_FOOTER_RE.search(body))
    return Commit(match.group("type"), match.group("scope"), match.group("description").strip(), breaking)


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def next_version(current: Version | None, commits: list[Commit]) -> Version:
    """Return the version the given commits call for after ``current`` (None: nothing released yet)."""
    if current is None:
        return INITIAL_VERSION
    major, minor, patch = current
    if any(commit.breaking for commit in commits):
        return (major, minor + 1, 0) if major == 0 else (major + 1, 0, 0)
    if any(commit.type == "feat" for commit in commits):
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def _entry(commit: Commit) -> str:
    scope = f"**{commit.scope}**: " if commit.scope else ""
    return f"- {scope}{commit.description.replace(_DOUBLE_DASH, ': ')}"


def render_section(title: str, commits: list[Commit]) -> str:
    """Render one release (or the unreleased commits) as a markdown section, newest commit first.

    The groups are bold labels rather than headings: every release repeats them, and a repeated heading
    makes GitHub number its anchors (docs/invariants.md, "Unique Markdown Headings").
    """
    lines = [f"## {title}", ""]
    groups = [(BREAKING_HEADING, [commit for commit in commits if commit.breaking])]
    groups += [(heading, [c for c in commits if c.type == kind and not c.breaking]) for kind, heading in SECTIONS]
    listed = False
    for heading, entries in groups:
        if entries:
            lines += [f"**{heading}**", "", *(_entry(commit) for commit in entries), ""]
            listed = True
    if not listed:
        lines += ["No user-facing changes.", ""]
    return "\n".join(lines)


def render_changelog(releases: list[tuple[str, list[Commit]]]) -> str:
    """Render the whole file; ``releases`` is newest first, each a (section title, commits) pair."""
    header = (
        "# Changelog\n\n"
        "Generated from the conventional commits by `make changelog` (scripts/changelog.py); do not edit by hand.\n"
        "Lists features, fixes and breaking changes; the full history is `git log`.\n\n"
    )
    return header + "\n".join(render_section(title, commits) for title, commits in releases).rstrip() + "\n"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def release_tags(repo: Path) -> list[tuple[str, Version]]:
    """Every ``vX.Y.Z`` tag, oldest version first."""
    tags = []
    for name in _git(repo, "tag", "--list", "v*").split():
        match = _TAG_RE.fullmatch(name)
        if match:
            tags.append((name, (int(match[1]), int(match[2]), int(match[3]))))
    return sorted(tags, key=lambda tag: tag[1])


def commits_between(repo: Path, start: str | None, end: str) -> list[Commit]:
    """Conventional commits in ``start..end`` (all of ``end``'s history when start is None), newest first."""
    revision = f"{start}..{end}" if start else end
    log = _git(repo, "log", "--no-merges", f"--format=%s{_FIELD}%b{_RECORD}", revision)
    commits = []
    for record in log.split(_RECORD):
        subject, _, body = record.strip("\n").partition(_FIELD)
        commit = parse_commit(subject, body)
        if commit is not None:
            commits.append(commit)
    return commits


def _tag_date(repo: Path, tag: str) -> str:
    return _git(repo, "log", "-1", "--format=%cs", tag).strip()


def build_releases(repo: Path, release: str | None, date: str) -> list[tuple[str, list[Commit]]]:
    tags = release_tags(repo)
    releases = []
    previous: str | None = None
    for name, version in tags:
        releases.append((f"{format_version(version)} ({_tag_date(repo, name)})", commits_between(repo, previous, name)))
        previous = name
    unreleased = commits_between(repo, previous, "HEAD")
    if release is not None:
        releases.append((f"{release} ({date})", unreleased))
    elif unreleased:
        releases.append(("Unreleased", unreleased))
    return list(reversed(releases))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Changelog and next release version from conventional commits.")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="git repository (default: current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("next-version", help="print the next release version the commits call for")
    changelog = commands.add_parser("changelog", help="write CHANGELOG.md")
    changelog.add_argument("--release", help="title the unreleased commits as this version (when cutting a release)")
    changelog.add_argument("--date", default=datetime.date.today().isoformat(), help="release date for --release")
    changelog.add_argument("--output", type=Path, default=Path("CHANGELOG.md"), help="file to write")
    args = parser.parse_args(argv)

    if args.command == "changelog" and args.release is not None and not re.fullmatch(r"\d+\.\d+\.\d+", args.release):
        parser.error(f"--release must be X.Y.Z, got {args.release!r}")

    try:
        if args.command == "next-version":
            tags = release_tags(args.repo)
            latest = tags[-1] if tags else None
            commits = commits_between(args.repo, latest[0] if latest else None, "HEAD")
            if latest is not None and not commits:
                print(f"changelog: no conventional commits since {latest[0]}; nothing to release", file=sys.stderr)
                return 1
            print(format_version(next_version(latest[1] if latest else None, commits)))
            return 0

        releases = build_releases(args.repo, args.release, args.date)
    except RuntimeError as exc:
        print(f"changelog: {exc}", file=sys.stderr)
        return 1
    output = args.output if args.output.is_absolute() else args.repo / args.output
    output.write_text(render_changelog(releases), encoding="utf-8")
    print(f"changelog: wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
