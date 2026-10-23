"""Distributable templates must not name a personal GitHub account (issue #1251).

Everything under `sova/commands/data/` is installed into other people's
projects, where a maintainer's own account name is both a privacy leak and a
wrong instruction (it tells an agent the board belongs to someone it does not).
`health-audit.md` once named the board owner and the issue-filing account
literally. The test scans the canonical tree only: the rendered copies
(`.claude/commands/`, `.agents/skills/`, `plugins/sova/`) are regenerated from
it and guarded by their own drift tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sova.commands.catalog import get_canonical_dir

_DATA_DIR = get_canonical_dir().parent

# The maintainers' own accounts. Word boundaries keep an unrelated word that
# merely contains one of these from matching.
_PERSONAL_ACCOUNT_RE = re.compile(r"\b(?:xsovad06|dsova06|dsova)\b", re.IGNORECASE)


def _distributable_files() -> list[Path]:
    return sorted(p for p in _DATA_DIR.rglob("*.md") if p.is_file())


def test_distributable_tree_is_found() -> None:
    """Guard against a moved data directory turning the scan below into a silent no-op."""
    assert any(p.parent.name == "commands" for p in _distributable_files())


@pytest.mark.parametrize("path", _distributable_files(), ids=lambda p: str(p.relative_to(_DATA_DIR)))
def test_no_personal_account_names(path: Path) -> None:
    hits = [
        f"{lineno}: {line.strip()}"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _PERSONAL_ACCOUNT_RE.search(line)
    ]
    assert not hits, f"{path.relative_to(_DATA_DIR)} names a personal account:\n" + "\n".join(hits)
