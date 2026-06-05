"""Team knowledge sharing -- export/import memories across SOVA installations.

Provides pure functions for markdown parsing/rendering of shared knowledge files,
plus async functions for exporting project memories and importing shared entries.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import select

from sova.db.models import Memory
from sova.db.session import get_session
from sova.knowledge import memory as memory_store
from sova.utils.logging import get_logger

log = get_logger(component="knowledge.sharing")

SHAREABLE_CATEGORIES: frozenset[str] = frozenset(
    {
        "review_pattern",
        "common_mistake",
        "codebase_pattern",
        "ci_pattern",
    }
)

CATEGORY_FILES: dict[str, str] = {
    "review_pattern": "review-patterns.md",
    "common_mistake": "common-mistakes.md",
    "codebase_pattern": "conventions.md",
    "ci_pattern": "ci-patterns.md",
}

_FILE_TO_CATEGORY: dict[str, str] = {v: k for k, v in CATEGORY_FILES.items()}

_METADATA_RE = re.compile(r"<!--\s*memory:(\S+)\s+hash:(\S+)\s+repo:(\S+)\s+date:(\S+)\s*-->")
_CONFIRMED_RE = re.compile(r"\[confirmed:\s*(\d+)\]")


@dataclass
class SharedEntry:
    """A single shared knowledge entry."""

    memory_id: int | None
    title: str
    content: str
    category: str
    tags: list[str] = field(default_factory=list)
    source_repo: str = ""
    source_date: str = ""
    content_hash: str = ""


@dataclass
class ExportResult:
    """Summary of an export operation."""

    exported: int = 0
    skipped: int = 0
    entries: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    """Summary of an import operation."""

    imported: int = 0
    skipped: int = 0
    ignored: int = 0
    entries: list[str] = field(default_factory=list)


def compute_content_hash(content: str) -> str:
    """SHA-256 prefix (16 chars) of stripped content."""
    return hashlib.sha256(content.strip().encode()).hexdigest()[:16]


def parse_shared_file(path: Path) -> list[SharedEntry]:
    """Parse a shared knowledge markdown file into entries.

    Handles both SOVA-formatted files (with metadata comments) and
    plain markdown files (without metadata). Pure function.
    """
    if not path.is_file():
        return []

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []

    category = _FILE_TO_CATEGORY.get(path.name, "learning")

    entries: list[SharedEntry] = []
    sections = re.split(r"(?m)^---\s*$", text)

    for section in sections:
        section = section.strip()
        if not section:
            continue

        title_match = re.match(r"^##\s+(.+)$", section, re.MULTILINE)
        if not title_match:
            continue

        title = title_match.group(1).strip()
        body_start = title_match.end()
        body = section[body_start:].strip()

        memory_id: int | None = None
        source_repo = ""
        source_date = ""
        content_hash = ""

        meta_match = _METADATA_RE.search(body)
        if meta_match:
            raw_id = meta_match.group(1)
            memory_id = int(raw_id) if raw_id.isdigit() else None
            content_hash = meta_match.group(2)
            source_repo = meta_match.group(3)
            source_date = meta_match.group(4)
            body = body[: meta_match.start()] + body[meta_match.end() :]
            body = body.strip()

        tags: list[str] = []
        tags_match = re.search(r"(?m)^Tags:\s*(.+)$", body)
        if tags_match:
            tags = [t.strip() for t in tags_match.group(1).split(",") if t.strip()]
            body = body[: tags_match.start()] + body[tags_match.end() :]
            body = body.strip()

        if not content_hash:
            content_hash = compute_content_hash(body)

        entries.append(
            SharedEntry(
                memory_id=memory_id,
                title=title,
                content=body,
                category=category,
                tags=tags,
                source_repo=source_repo,
                source_date=source_date,
                content_hash=content_hash,
            )
        )

    return entries


def render_shared_file(entries: list[SharedEntry]) -> str:
    """Render entries to a shared knowledge markdown file. Pure function."""
    if not entries:
        return ""

    sections: list[str] = []
    for entry in entries:
        lines: list[str] = []
        lines.append(f"## {entry.title}")
        lines.append("")

        mid = entry.memory_id if entry.memory_id is not None else "none"
        chash = entry.content_hash or compute_content_hash(entry.content)
        repo = entry.source_repo or "unknown"
        sdate = entry.source_date or date.today().isoformat()
        lines.append(f"<!-- memory:{mid} hash:{chash} repo:{repo} date:{sdate} -->")
        lines.append("")

        lines.append(entry.content)

        if entry.tags:
            lines.append("")
            lines.append(f"Tags: {', '.join(entry.tags)}")

        sections.append("\n".join(lines))

    return "\n\n---\n\n".join(sections) + "\n"


async def export_memories(
    shared_dir: Path,
    *,
    dry_run: bool = False,
    categories: list[str] | None = None,
    repo: str = "",
) -> ExportResult:
    """Export generalizable memories to shared knowledge directory.

    Selection criteria:
    - tier == "project" (not already shared or session-scoped)
    - category in SHAREABLE_CATEGORIES
    - superseded_by is None
    - Has been confirmed at least once ([confirmed: 1+] in content) OR tier already "shared"
    """
    result = ExportResult()

    target_categories = SHAREABLE_CATEGORIES
    if categories:
        target_categories = SHAREABLE_CATEGORIES & set(categories)

    async with await get_session() as session:
        async with session.begin():
            stmt = (
                select(Memory)
                .where(
                    Memory.tier == "project",
                    Memory.category.in_(target_categories),
                    Memory.superseded_by.is_(None),
                )
                .order_by(Memory.category, Memory.title)
            )
            rows = await session.execute(stmt)
            memories = list(rows.scalars().all())

    # Filter: must have [confirmed: 1+] in content
    exportable: list[Memory] = []
    for mem in memories:
        counter_match = _CONFIRMED_RE.search(mem.content)
        if counter_match and int(counter_match.group(1)) >= 1:
            exportable.append(mem)
        else:
            result.skipped += 1

    if not exportable:
        return result

    # Group by category
    by_category: dict[str, list[SharedEntry]] = {}
    for mem in exportable:
        content = _CONFIRMED_RE.sub("", mem.content).strip()
        entry = SharedEntry(
            memory_id=mem.id,
            title=mem.title,
            content=content,
            category=mem.category,
            tags=[t.strip() for t in mem.tags.split(",") if t.strip()] if mem.tags else [],
            source_repo=repo or mem.repo,
            source_date=date.today().isoformat(),
            content_hash=compute_content_hash(content),
        )
        by_category.setdefault(mem.category, []).append(entry)

    if dry_run:
        # In dry-run, report all eligible entries
        for entries in by_category.values():
            for e in entries:
                result.entries.append(e.title)
                result.exported += 1
        return result

    shared_dir.mkdir(parents=True, exist_ok=True)
    for cat, entries in by_category.items():
        filename = CATEGORY_FILES.get(cat, f"{cat}.md")
        filepath = shared_dir / filename
        existing = parse_shared_file(filepath)

        # Build lookup of existing entries by memory_id and content_hash
        existing_by_id: dict[int, int] = {}  # memory_id -> index in existing
        existing_hashes: set[str] = set()
        for idx, e in enumerate(existing):
            existing_hashes.add(e.content_hash)
            if e.memory_id is not None:
                existing_by_id[e.memory_id] = idx

        merged = list(existing)
        changed = False
        for e in entries:
            if e.memory_id is not None and e.memory_id in existing_by_id:
                old_idx = existing_by_id[e.memory_id]
                if merged[old_idx].content_hash != e.content_hash:
                    # Replace stale entry with updated content
                    merged[old_idx] = e
                    result.entries.append(e.title)
                    result.exported += 1
                    changed = True
                # Same hash -- already up to date, skip
            elif e.content_hash not in existing_hashes:
                merged.append(e)
                existing_hashes.add(e.content_hash)
                result.entries.append(e.title)
                result.exported += 1
                changed = True

        if not changed:
            continue
        filepath.write_text(render_shared_file(merged), encoding="utf-8")

    log.info("sharing.exported", count=result.exported, skipped=result.skipped)
    return result


async def import_memories(
    shared_dir: Path,
    *,
    dry_run: bool = False,
    ignored_hashes: list[str] | None = None,
) -> ImportResult:
    """Import new entries from shared knowledge directory.

    Dedup: skips entries whose content_hash matches any existing Memory's content hash.
    Stores new entries with tier="shared".
    """
    result = ImportResult()
    ignored_set = set(ignored_hashes) if ignored_hashes else set()

    if not shared_dir.is_dir():
        return result

    # Collect all shared entries from markdown files
    all_entries: list[SharedEntry] = []
    for md_file in sorted(shared_dir.glob("*.md")):
        all_entries.extend(parse_shared_file(md_file))

    if not all_entries:
        return result

    # Get existing content hashes from local DB
    existing_hashes = await _get_existing_content_hashes()

    for entry in all_entries:
        if entry.content_hash in ignored_set:
            result.ignored += 1
            continue

        if entry.content_hash in existing_hashes:
            result.skipped += 1
            continue

        if dry_run:
            result.imported += 1
            result.entries.append(entry.title)
            continue

        await memory_store.store(
            category=entry.category,
            title=entry.title,
            content=entry.content,
            tags=entry.tags,
            tier="shared",
            repo=entry.source_repo,
        )
        existing_hashes.add(entry.content_hash)
        result.imported += 1
        result.entries.append(entry.title)

    log.info(
        "sharing.imported",
        imported=result.imported,
        skipped=result.skipped,
        ignored=result.ignored,
    )
    return result


async def _get_existing_content_hashes() -> set[str]:
    """Compute content hashes for all existing non-superseded memories.

    Strips ``[confirmed: N]`` markers before hashing so that exported
    entries (which have the marker stripped) can be dedup-matched against
    the source DB.
    """
    async with await get_session() as session:
        async with session.begin():
            stmt = select(Memory.content).where(Memory.superseded_by.is_(None))
            rows = await session.execute(stmt)
            contents = rows.scalars().all()

    return {compute_content_hash(_CONFIRMED_RE.sub("", c).strip()) for c in contents}
