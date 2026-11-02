"""Enforces the "unconditionally-loaded instruction surface" character budget.

Per issue #1131, the surface is defined as CLAUDE.md + AGENTS.md + every
`.claude/rules/**/*.md` file (none of them path-scoped today, so every file in
that directory tree counts, subfolders included). Issue #1338 lowered the combined budget from 100,000
to 60,000 characters and added a cap per file, so one file cannot absorb the
whole allowance. On-demand reference material (`docs/*.md`,
`.claude/agent-memory/`) is excluded by construction: relocating narrative and
incident history there is the intended mechanism for staying under budget,
not a loophole for this test to flag. What stays behind is a short rule line
with a `(full entry: docs/X.md, "Title")` pointer, and every such pointer must
resolve to a real heading or entry in that doc.

The six distributable guideline templates in `sova/commands/data/guidelines/`
are always loaded too, in every project SOVA installs into (they carry no
`paths:` frontmatter), so they get caps of their own.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from sova.utils.files import read_text_or_none

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUDGET_CHARS = 60_000
_ARCHITECTURE_RULES = _REPO_ROOT / ".claude" / "rules" / "architecture.md"
_DEEP_DIVE = _REPO_ROOT / "docs" / "architecture-deep-dive.md"
_GUIDELINES_DIR = _REPO_ROOT / "sova" / "commands" / "data" / "guidelines"

# Per-file caps (issue #1338). A `.claude/rules/**/*.md` file not listed here gets
# _DEFAULT_RULES_CAP, so a new rules file cannot grow unchecked either.
_FILE_CAPS = {
    "CLAUDE.md": 8_000,
    "AGENTS.md": 15_000,
    ".claude/rules/architecture.md": 20_000,
    ".claude/rules/bash-patterns.md": 8_000,
    ".claude/rules/workflow.md": 8_000,
}
_DEFAULT_RULES_CAP = 8_000
_GUIDELINE_FILE_CAP = 3_500
_GUIDELINES_BUDGET_CHARS = 20_000

# Matches an index bullet like "- **Topic name**: rest of the sentence" or
# "- Topic name" in a "grep this file for a topic" list.
_INDEX_BULLET_RE = re.compile(r"^- (?:\*\*(?P<bold>[^*]+)\*\*|(?P<plain>[^\n]+))", re.MULTILINE)

# A pointer left behind by a move: "full entry: docs/X.md" with an optional
# ', "Title"'. Without a title, the pointer names the entry its own bullet's
# bold label names.
_POINTER_RE = re.compile(r'full entry: (?P<doc>docs/[\w./-]+\.md)(?:, "(?P<title>[^"]+)")?')
_BOLD_LABEL_RE = re.compile(r"^\s*- \*\*(?P<label>.+?)\*\*", re.MULTILINE)
_TOP_LEVEL_BOLD_LABEL_RE = re.compile(r"^- \*\*(?P<label>.+?)\*\*", re.MULTILINE)
_DOC_PATH_RE = re.compile(r"docs/[\w./-]+\.md")

# An index title is matched against an entry label by its first characters, since the index may
# shorten or reword the tail of a long label.
_TOPIC_PREFIX_CHARS = 40
# Decisions whose body lives in a Supporting Modules entry rather than a Key Design Decisions bullet.
_SUPPORTING_MODULES_DECISIONS = ("Codex skills are mechanically rendered",)


def _unconditional_surface_files(root: Path = _REPO_ROOT) -> list[Path]:
    # Claude Code loads rules files from subfolders of .claude/rules/ as well, so the walk is recursive.
    rules_dir = root / ".claude" / "rules"
    return [root / "CLAUDE.md", root / "AGENTS.md", *sorted(rules_dir.rglob("*.md"))]


def _rel(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def _read(path: Path) -> str:
    """The file's text; a missing or undecodable file fails the test rather than reading as empty."""
    text = read_text_or_none(path)
    assert text is not None, f"{path} is missing or not valid UTF-8"
    return text


def test_unconditional_instruction_surface_under_budget() -> None:
    files = _unconditional_surface_files()
    sizes = {_rel(p): len(_read(p)) for p in files}
    total = sum(sizes.values())
    assert total <= _BUDGET_CHARS, (
        f"Always-loaded instruction surface is {total} chars (budget: {_BUDGET_CHARS}): {sizes}. "
        "Relocate narrative/cookbook-style content into docs/ (see docs/architecture-deep-dive.md "
        "for the established pattern: a short rule line with a `(full entry: docs/X.md)` pointer "
        "stays in the always-loaded file, the full entry moves to the on-demand doc) instead of "
        "growing these files further."
    )


def test_each_surface_file_is_under_its_own_cap() -> None:
    over = {}
    for path in _unconditional_surface_files():
        rel = _rel(path)
        cap = _FILE_CAPS.get(rel, _DEFAULT_RULES_CAP)
        size = len(_read(path))
        if size > cap:
            over[rel] = (size, cap)
    assert not over, (
        f"Always-loaded files over their cap, as {{file: (size, cap)}}: {over}. Move incident history "
        "and narrative to a docs/*.md file and leave a pointer line behind."
    )


def test_every_capped_file_exists() -> None:
    """A renamed or deleted file must not silently drop out of its cap."""
    missing = [rel for rel in _FILE_CAPS if not (_REPO_ROOT / rel).is_file()]
    assert not missing, f"_FILE_CAPS names files that no longer exist: {missing}"


def test_surface_includes_rules_files_in_subfolders(tmp_path: Path) -> None:
    """Claude Code loads `.claude/rules/**/*.md`, so a nested file must count toward the budget and its cap."""
    nested = tmp_path / ".claude" / "rules" / "dashboard" / "design.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("# nested rule\n", encoding="utf-8")
    flat = tmp_path / ".claude" / "rules" / "flat.md"
    flat.write_text("# flat rule\n", encoding="utf-8")

    surface = _unconditional_surface_files(tmp_path)

    assert nested in surface
    assert flat in surface


def test_distributable_guideline_templates_under_budget() -> None:
    templates = sorted(_GUIDELINES_DIR.glob("*.md"))
    assert templates, f"no guideline templates found in {_GUIDELINES_DIR}"
    sizes = {p.name: len(_read(p)) for p in templates}
    over = {name: size for name, size in sizes.items() if size > _GUIDELINE_FILE_CAP}
    assert not over, f"Guideline templates over {_GUIDELINE_FILE_CAP} chars each: {over}"
    total = sum(sizes.values())
    assert total <= _GUIDELINES_BUDGET_CHARS, (
        f"Guideline templates total {total} chars (budget: {_GUIDELINES_BUDGET_CHARS}): {sizes}. "
        "These are written into every target project's .claude/rules/ and always loaded there."
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

    deep_dive_text = _read(_DEEP_DIVE)
    assert deep_dive_text, f"{_DEEP_DIVE} must not be empty: relocated content lives here"

    surface_total = sum(len(_read(p)) for p in _unconditional_surface_files())
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
    assert f"\n{heading}\n" in text, f"{heading!r} missing from {_DEEP_DIVE.name}"
    start = text.index(f"\n{heading}\n") + len(heading) + 2
    return text[start:].split("\n## ", 1)[0]


def _missing_topics(index_text: str, is_present: Callable[[str], bool]) -> list[str]:
    topics = _index_topics(index_text)
    assert topics, "indexed section has no topics: index bullets may have been removed"
    return [topic for topic in topics if not is_present(topic)]


def _has_bold_label(topic: str, section: str) -> bool:
    """True when a bullet of `section` carries the topic as its bold label.

    The index may shorten or reword the tail of a label, so only the first
    `_TOPIC_PREFIX_CHARS` characters are compared. Matching bold labels in the
    one section alone (not the whole document) is what keeps a topic from
    passing on an incidental mention elsewhere, such as a Component Notes bullet
    or another entry's prose, after its own entry was deleted.
    """
    prefix = topic[:_TOPIC_PREFIX_CHARS]
    return any(m.group("label").startswith(prefix) for m in _BOLD_LABEL_RE.finditer(section))


def _has_decision_entry(topic: str, decisions: str, supporting_modules: str) -> bool:
    """True when a Key Design Decisions bullet carries the topic as its bold label.

    The `_SUPPORTING_MODULES_DECISIONS` exception is looked for inside the Supporting Modules section only:
    its body is prose in another module's entry, so there is no bold label to match.
    """
    if _has_bold_label(topic, decisions):
        return True
    return topic.startswith(_SUPPORTING_MODULES_DECISIONS) and topic[:_TOPIC_PREFIX_CHARS] in supporting_modules


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
    architecture_text = _read(_ARCHITECTURE_RULES)
    deep_dive_text = _read(_DEEP_DIVE)

    marker = "Grep that file for a topic below to read its full entry:"
    assert marker in architecture_text, "architecture.md lost its Supporting Modules index"
    supporting_index = architecture_text.split(marker, 1)[1].split("\n## ", 1)[0]
    # Each topic must be a bold-labelled bullet of the section it points at, not a mention elsewhere.
    decisions_index = _section(deep_dive_text, "## Key Design Decisions Index")
    decisions = _section(deep_dive_text, "## Key Design Decisions")
    supporting_modules = _section(deep_dive_text, "## Supporting Modules")
    missing = _missing_topics(supporting_index, lambda topic: _has_bold_label(topic, supporting_modules))
    assert not missing, f"Supporting Modules topics missing from {_DEEP_DIVE.name}: {missing}"

    missing = _missing_topics(decisions_index, lambda topic: _has_decision_entry(topic, decisions, supporting_modules))
    assert not missing, f"Key Design Decisions Index topics missing an entry in {_DEEP_DIVE.name}: {missing}"

    # The index is the only list of decision titles, so the other direction must hold too.
    indexed = _index_topics(decisions_index)
    unindexed = [
        label
        for label in (m.group("label") for m in _TOP_LEVEL_BOLD_LABEL_RE.finditer(decisions))
        if not any(topic[:_TOPIC_PREFIX_CHARS] == label[:_TOPIC_PREFIX_CHARS] for topic in indexed)
    ]
    assert not unindexed, f"Key Design Decisions entries missing from the index in {_DEEP_DIVE.name}: {unindexed}"


def _resolves(doc_text: str, title: str) -> bool:
    """True when the title is a heading's text (optionally followed by a parenthetical) or an entry's bold label."""
    for line in doc_text.splitlines():
        heading = re.match(r"^#{1,6} (.+)$", line)
        if heading:
            text = heading.group(1).strip()
            if text == title or text.startswith(f"{title} ("):
                return True
        if line.lstrip().startswith(f"- **{title}**"):
            return True
    return False


def test_every_surface_pointer_resolves_to_a_doc_entry() -> None:
    """Each "(full entry: docs/X.md[, "Title"])" pointer names a real heading or entry.

    Issue #1338 moved about 50,000 characters out of the always-loaded files,
    leaving one pointer per moved block. A pointer that no longer resolves
    (the entry renamed, the doc moved) sends an agent looking for a rule's
    detail to nothing, with no other signal that the text was lost.
    """
    pointers = 0
    broken: list[str] = []
    for path in _unconditional_surface_files():
        for line in _read(path).splitlines():
            for match in _POINTER_RE.finditer(line):
                pointers += 1
                doc = _REPO_ROOT / match.group("doc")
                title = match.group("title")
                if title is None:
                    label = _BOLD_LABEL_RE.match(line)
                    if label is None:
                        broken.append(f"{_rel(path)}: untitled pointer outside a bold-labelled bullet: {line[:80]}")
                        continue
                    title = label.group("label")
                if not doc.is_file():
                    broken.append(f"{_rel(path)}: {match.group('doc')} does not exist")
                elif not _resolves(_read(doc), title):
                    broken.append(f"{_rel(path)}: {title!r} is not a heading or entry in {match.group('doc')}")
    assert pointers, "no pointers found: _POINTER_RE no longer matches the surface files' pointer format"
    assert not broken, "Unresolvable pointers:\n" + "\n".join(broken)


def test_every_doc_path_named_in_the_surface_exists() -> None:
    missing = sorted(
        {
            f"{_rel(path)}: {doc}"
            for path in _unconditional_surface_files()
            for doc in _DOC_PATH_RE.findall(_read(path))
            if not (_REPO_ROOT / doc).is_file()
        }
    )
    assert not missing, f"Always-loaded files name docs that do not exist: {missing}"
