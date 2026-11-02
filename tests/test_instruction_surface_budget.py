"""Enforces the "unconditionally-loaded instruction surface" character budget.

Per issue #1131, the surface is defined as CLAUDE.md + AGENTS.md + every
`.claude/rules/*.md` file (none of them path-scoped today, so every file in
that directory counts). It must stay under 100,000 characters combined.
On-demand reference material (`docs/*.md`, `.claude/agent-memory/`) is
excluded by construction: relocating narrative/cookbook-style content there
is the intended mechanism for staying under budget, not a loophole for this
test to flag.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUDGET_CHARS = 100_000
_ARCHITECTURE_RULES = _REPO_ROOT / ".claude" / "rules" / "architecture.md"
_DEEP_DIVE = _REPO_ROOT / "docs" / "architecture-deep-dive.md"

# Matches an index bullet like "- **Topic name**: rest of the sentence" or
# "- Topic name" in architecture.md's two "grep this file for a topic" lists.
_INDEX_BULLET_RE = re.compile(r"^- (?:\*\*(?P<bold>[^*]+)\*\*|(?P<plain>[^\n]+))", re.MULTILINE)


def _unconditional_surface_files() -> list[Path]:
    rules_dir = _REPO_ROOT / ".claude" / "rules"
    return [_REPO_ROOT / "CLAUDE.md", _REPO_ROOT / "AGENTS.md", *sorted(rules_dir.glob("*.md"))]


def test_unconditional_instruction_surface_under_budget() -> None:
    files = _unconditional_surface_files()
    sizes = {p.relative_to(_REPO_ROOT): len(p.read_text(encoding="utf-8")) for p in files}
    total = sum(sizes.values())
    assert total < _BUDGET_CHARS, (
        f"Always-loaded instruction surface is {total} chars (budget: {_BUDGET_CHARS}): {sizes}. "
        "Relocate narrative/cookbook-style content into docs/ (see docs/architecture-deep-dive.md "
        "for the established pattern: a topic index stays in .claude/rules/architecture.md, full "
        "entries move to the on-demand doc) instead of growing these files further."
    )


def test_on_demand_docs_are_not_part_of_the_measured_surface() -> None:
    """The split into docs/architecture-deep-dive.md must still be load-bearing.

    A bare "docs/ isn't in the list" check is true by construction from how
    `_unconditional_surface_files()` is written and can never fail. Instead,
    assert the property that actually matters: the deep-dive doc holds real
    relocated content, and the always-loaded surface stays under budget only
    because that content lives there instead of in the surface. If this
    assertion fails, the split has stopped doing its job (the deep-dive doc
    emptied out, or the surface grew enough that re-merging it would no
    longer matter), and `test_unconditional_instruction_surface_under_budget`
    alone would not catch a future edit folding the content back in until
    that edit actually happened.
    """
    for path in _unconditional_surface_files():
        rel = path.relative_to(_REPO_ROOT)
        assert rel.parts[0] != "docs", f"{rel} must not be part of the always-loaded surface"
    assert _DEEP_DIVE not in _unconditional_surface_files()

    deep_dive_text = _DEEP_DIVE.read_text(encoding="utf-8")
    assert deep_dive_text, f"{_DEEP_DIVE} must not be empty: relocated content lives here"

    surface_total = sum(len(p.read_text(encoding="utf-8")) for p in _unconditional_surface_files())
    assert surface_total + len(deep_dive_text) >= _BUDGET_CHARS, (
        "the always-loaded surface stays under budget only because "
        f"{_DEEP_DIVE.name}'s content was relocated out of it; this assertion failing means "
        "the split is no longer load-bearing"
    )


def _index_topics(text: str) -> list[str]:
    topics = []
    for match in _INDEX_BULLET_RE.finditer(text):
        label = match.group("bold") or match.group("plain")
        topics.append(label.strip().rstrip(":"))
    return topics


def _section(text: str, heading: str) -> str:
    """The body of a "## heading" section, up to the next "## " heading."""
    start = text.index(f"\n{heading}\n") + len(heading) + 2
    return text[start:].split("\n## ", 1)[0]


def _missing_topics(index_text: str, body_text: str) -> list[str]:
    topics = _index_topics(index_text)
    assert topics, "indexed section has no topics: index bullets may have been removed"
    # A short, distinctive slice of the topic label (not the whole sentence,
    # which may be paraphrased slightly between the index and the deep-dive
    # heading) must appear somewhere in the body.
    return [topic for topic in topics if topic[:40] not in body_text]


def test_architecture_index_topics_resolve_to_deep_dive_entries() -> None:
    """Every topic the two indexes point readers at must exist in the deep-dive doc.

    architecture.md keeps the Supporting Modules index and tells readers to
    "grep that file for a topic below to read its full entry"; the Key Design
    Decisions index moved into the deep-dive itself (issue #1338).
    Relocation is only lossless if every indexed topic still resolves to a
    real body. A topic silently dropped from the deep-dive during a future
    edit would otherwise go unnoticed, since nothing else checks that the
    index and the content it points to stay in sync.
    """
    architecture_text = _ARCHITECTURE_RULES.read_text(encoding="utf-8")
    deep_dive_text = _DEEP_DIVE.read_text(encoding="utf-8")

    marker = "Grep that file for a topic below to read its full entry:"
    assert marker in architecture_text, "architecture.md lost its Supporting Modules index"
    supporting_index = architecture_text.split(marker, 1)[1].split("\n## ", 1)[0]
    missing = _missing_topics(supporting_index, deep_dive_text)
    assert not missing, f"Supporting Modules topics missing from {_DEEP_DIVE.name}: {missing}"

    # Searched in the doc with the index itself cut out, or each index title
    # would trivially match its own index line. (One decision's body lives in
    # a Supporting Modules entry, so the search cannot be narrowed further.)
    decisions_index = _section(deep_dive_text, "## Key Design Decisions Index")
    missing = _missing_topics(decisions_index, deep_dive_text.replace(decisions_index, ""))
    assert not missing, f"Key Design Decisions Index topics missing an entry in {_DEEP_DIVE.name}: {missing}"
