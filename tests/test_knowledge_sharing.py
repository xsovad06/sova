"""Tests for SOVA knowledge sharing -- export/import across installations."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh in-memory DB for each test."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


# ---------------------------------------------------------------------------
# Pure functions: compute_content_hash
# ---------------------------------------------------------------------------


def test_compute_content_hash_deterministic() -> None:
    """compute_content_hash() returns the same hash for the same content."""
    from sova.knowledge.sharing import compute_content_hash

    h1 = compute_content_hash("Hello world")
    h2 = compute_content_hash("Hello world")
    assert h1 == h2
    assert len(h1) == 16


def test_compute_content_hash_strips_whitespace() -> None:
    """compute_content_hash() strips surrounding whitespace before hashing."""
    from sova.knowledge.sharing import compute_content_hash

    assert compute_content_hash("  hello  ") == compute_content_hash("hello")


def test_compute_content_hash_differs_for_different_content() -> None:
    """compute_content_hash() produces different hashes for different content."""
    from sova.knowledge.sharing import compute_content_hash

    assert compute_content_hash("alpha") != compute_content_hash("beta")


# ---------------------------------------------------------------------------
# Pure functions: parse_shared_file / render_shared_file
# ---------------------------------------------------------------------------


def test_parse_shared_file_with_metadata(tmp_path: Path) -> None:
    """parse_shared_file() parses entries with metadata comments."""
    from sova.knowledge.sharing import parse_shared_file

    content = """## Avoid mutable default arguments

<!-- memory:42 hash:a1b2c3d4e5f6g7h8 repo:income-processor date:2026-05-20 -->

Functions should never use mutable defaults like `def foo(items=[])`.

Tags: python, common-mistake

---

## Always validate webhook signatures

<!-- memory:87 hash:9f8e7d6c5b4a3210 repo:ave-monorepo date:2026-05-21 -->

Webhook endpoints must verify HMAC signatures.

Tags: security, api
"""
    filepath = tmp_path / "common-mistakes.md"
    filepath.write_text(content)

    entries = parse_shared_file(filepath)
    assert len(entries) == 2

    e0 = entries[0]
    assert e0.title == "Avoid mutable default arguments"
    assert e0.memory_id == 42
    assert e0.content_hash == "a1b2c3d4e5f6g7h8"
    assert e0.source_repo == "income-processor"
    assert e0.source_date == "2026-05-20"
    assert "python" in e0.tags
    assert "common-mistake" in e0.tags
    assert e0.category == "common_mistake"

    e1 = entries[1]
    assert e1.title == "Always validate webhook signatures"
    assert e1.memory_id == 87
    assert "security" in e1.tags


def test_parse_shared_file_without_metadata(tmp_path: Path) -> None:
    """parse_shared_file() handles plain markdown files without metadata comments."""
    content = """## Use type hints everywhere

All function signatures should have type annotations.

---

## Prefer f-strings

Use f-strings for string formatting in Python 3.6+.
"""
    filepath = tmp_path / "conventions.md"
    filepath.write_text(content)

    from sova.knowledge.sharing import parse_shared_file

    entries = parse_shared_file(filepath)
    assert len(entries) == 2
    assert entries[0].memory_id is None
    assert entries[0].title == "Use type hints everywhere"
    assert entries[0].content_hash  # auto-generated
    assert entries[1].title == "Prefer f-strings"


def test_parse_shared_file_empty(tmp_path: Path) -> None:
    """parse_shared_file() returns empty list for empty file."""
    from sova.knowledge.sharing import parse_shared_file

    filepath = tmp_path / "empty.md"
    filepath.write_text("")
    assert parse_shared_file(filepath) == []


def test_parse_shared_file_nonexistent(tmp_path: Path) -> None:
    """parse_shared_file() returns empty list for nonexistent file."""
    from sova.knowledge.sharing import parse_shared_file

    assert parse_shared_file(tmp_path / "missing.md") == []


def test_render_shared_file_produces_valid_markdown() -> None:
    """render_shared_file() produces valid markdown with metadata."""
    from sova.knowledge.sharing import SharedEntry, render_shared_file

    entries = [
        SharedEntry(
            memory_id=1,
            title="Test Pattern",
            content="Always test your code.",
            category="review_pattern",
            tags=["testing", "quality"],
            source_repo="my-repo",
            source_date="2026-06-01",
            content_hash="abcdef1234567890",
        ),
        SharedEntry(
            memory_id=2,
            title="Another Pattern",
            content="Use consistent naming.",
            category="review_pattern",
            tags=["naming"],
            source_repo="my-repo",
            source_date="2026-06-01",
            content_hash="1234567890abcdef",
        ),
    ]

    result = render_shared_file(entries)
    assert "## Test Pattern" in result
    assert "<!-- memory:1 hash:abcdef1234567890 repo:my-repo date:2026-06-01 -->" in result
    assert "Always test your code." in result
    assert "Tags: testing, quality" in result
    assert "---" in result
    assert "## Another Pattern" in result


def test_render_shared_file_empty() -> None:
    """render_shared_file() returns empty string for empty entries."""
    from sova.knowledge.sharing import render_shared_file

    assert render_shared_file([]) == ""


def test_round_trip_parse_render(tmp_path: Path) -> None:
    """parse(render(entries)) produces equivalent entries."""
    from sova.knowledge.sharing import SharedEntry, parse_shared_file, render_shared_file

    original = [
        SharedEntry(
            memory_id=10,
            title="Round Trip Test",
            content="This should survive a round trip.",
            category="review_pattern",
            tags=["roundtrip"],
            source_repo="test-repo",
            source_date="2026-06-01",
            content_hash="abcdef1234567890",
        ),
    ]

    rendered = render_shared_file(original)
    filepath = tmp_path / "review-patterns.md"
    filepath.write_text(rendered)
    parsed = parse_shared_file(filepath)

    assert len(parsed) == 1
    p = parsed[0]
    assert p.title == "Round Trip Test"
    assert p.content == "This should survive a round trip."
    assert p.memory_id == 10
    assert p.content_hash == "abcdef1234567890"
    assert p.source_repo == "test-repo"
    assert "roundtrip" in p.tags


# ---------------------------------------------------------------------------
# Async: export_memories
# ---------------------------------------------------------------------------


async def _seed_exportable_memories() -> None:
    """Seed memories for export tests."""
    from sova.knowledge.memory import store

    await store(
        category="review_pattern",
        title="Always check return values",
        content="Functions may return None.\n\n[confirmed: 2]",
        tags=["python", "safety"],
        tier="project",
        repo="test-repo",
    )
    await store(
        category="common_mistake",
        title="Off-by-one in loops",
        content="Use range(len(x)) carefully.\n\n[confirmed: 1]",
        tags=["python", "loops"],
        tier="project",
        repo="test-repo",
    )
    # Not confirmed -- should be skipped
    await store(
        category="review_pattern",
        title="Unconfirmed pattern",
        content="Not yet confirmed.\n\n[confirmed: 0]",
        tags=["test"],
        tier="project",
    )
    # Wrong category -- should be skipped
    await store(
        category="learning",
        title="General learning",
        content="Some learning.\n\n[confirmed: 3]",
        tags=["general"],
        tier="project",
    )


async def test_export_memories(tmp_path: Path) -> None:
    """export_memories() writes categorized markdown files."""
    from sova.knowledge.sharing import export_memories

    await _seed_exportable_memories()
    shared_dir = tmp_path / "shared"
    result = await export_memories(shared_dir, repo="test-repo")

    assert result.exported == 2
    assert result.skipped == 1  # [confirmed: 0]
    assert "Always check return values" in result.entries
    assert "Off-by-one in loops" in result.entries

    # Check files were created
    assert (shared_dir / "review-patterns.md").is_file()
    assert (shared_dir / "common-mistakes.md").is_file()

    review_content = (shared_dir / "review-patterns.md").read_text()
    assert "Always check return values" in review_content
    assert "[confirmed:" not in review_content  # Stripped from exported content


async def test_export_memories_dry_run(tmp_path: Path) -> None:
    """export_memories(dry_run=True) does not write files."""
    from sova.knowledge.sharing import export_memories

    await _seed_exportable_memories()
    shared_dir = tmp_path / "shared"
    result = await export_memories(shared_dir, dry_run=True, repo="test-repo")

    assert result.exported == 2
    assert not shared_dir.exists()


async def test_export_memories_excludes_shared_tier(tmp_path: Path) -> None:
    """export_memories() only exports tier=project, not already-shared memories."""
    from sova.knowledge.memory import store
    from sova.knowledge.sharing import export_memories

    await store(
        category="review_pattern",
        title="Already shared",
        content="Should not re-export.\n\n[confirmed: 3]",
        tags=[],
        tier="shared",
        repo="test-repo",
    )
    await store(
        category="review_pattern",
        title="Project-only",
        content="Should export.\n\n[confirmed: 2]",
        tags=[],
        tier="project",
        repo="test-repo",
    )

    shared_dir = tmp_path / "shared"
    result = await export_memories(shared_dir, repo="test-repo")
    assert result.exported == 1
    assert "Project-only" in result.entries
    assert "Already shared" not in result.entries


async def test_export_memories_empty_db(tmp_path: Path) -> None:
    """export_memories() returns zero exported when DB is empty."""
    from sova.knowledge.sharing import export_memories

    shared_dir = tmp_path / "shared"
    result = await export_memories(shared_dir)

    assert result.exported == 0
    assert result.skipped == 0


async def test_export_memories_merges_with_existing(tmp_path: Path) -> None:
    """export_memories() merges new entries with existing file entries."""
    from sova.knowledge.sharing import export_memories, parse_shared_file

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    # Pre-existing entry
    existing = (
        "## Pre-existing pattern\n\n"
        "<!-- memory:999 hash:aaaa000000000000 repo:other date:2026-01-01 -->\n\n"
        "Old content.\n"
    )
    (shared_dir / "review-patterns.md").write_text(existing)

    await _seed_exportable_memories()
    await export_memories(shared_dir, repo="test-repo")

    entries = parse_shared_file(shared_dir / "review-patterns.md")
    titles = [e.title for e in entries]
    assert "Pre-existing pattern" in titles
    assert "Always check return values" in titles


async def test_export_memories_updates_stale_entry(tmp_path: Path) -> None:
    """export_memories() replaces existing entry when content hash changes for same memory_id."""
    from sova.knowledge.memory import store
    from sova.knowledge.sharing import export_memories, parse_shared_file

    mem = await store(
        category="review_pattern",
        title="Evolving pattern",
        content="Updated content v2.\n\n[confirmed: 2]",
        tags=["test"],
        tier="project",
        repo="test-repo",
    )

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    # Pre-existing entry with same memory_id but old content hash
    existing = (
        f"## Evolving pattern\n\n"
        f"<!-- memory:{mem.id} hash:oldoldhash0000000 repo:test-repo date:2026-01-01 -->\n\n"
        f"Old content v1.\n"
    )
    (shared_dir / "review-patterns.md").write_text(existing)

    result = await export_memories(shared_dir, repo="test-repo")
    assert result.exported == 1

    entries = parse_shared_file(shared_dir / "review-patterns.md")
    assert len(entries) == 1
    assert "Updated content v2." in entries[0].content
    assert entries[0].content_hash != "oldoldhash0000000"


# ---------------------------------------------------------------------------
# Async: import_memories
# ---------------------------------------------------------------------------


async def test_import_memories(tmp_path: Path) -> None:
    """import_memories() stores new entries with tier='shared'."""
    from sova.knowledge.memory import search
    from sova.knowledge.sharing import SharedEntry, import_memories, render_shared_file

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    entries = [
        SharedEntry(
            memory_id=1,
            title="Imported Pattern",
            content="Use dependency injection.",
            category="review_pattern",
            tags=["python"],
            source_repo="other-repo",
            source_date="2026-05-01",
            content_hash="import123456789ab",
        ),
    ]
    (shared_dir / "review-patterns.md").write_text(render_shared_file(entries))

    result = await import_memories(shared_dir)
    assert result.imported == 1
    assert "Imported Pattern" in result.entries

    # Verify in DB
    db_mems = await search(tier="shared")
    assert len(db_mems) == 1
    assert db_mems[0].title == "Imported Pattern"
    assert db_mems[0].tier == "shared"


async def test_import_memories_dedup(tmp_path: Path) -> None:
    """import_memories() skips entries already present in local DB."""
    from sova.knowledge.memory import store
    from sova.knowledge.sharing import (
        SharedEntry,
        compute_content_hash,
        import_memories,
        render_shared_file,
    )

    # Store a memory with known content
    content = "Use dependency injection."
    await store(
        category="review_pattern",
        title="Existing Pattern",
        content=content,
        tags=["python"],
        tier="project",
    )

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    entries = [
        SharedEntry(
            memory_id=1,
            title="Same Content Different Title",
            content=content,
            category="review_pattern",
            tags=["python"],
            source_repo="other-repo",
            source_date="2026-05-01",
            content_hash=compute_content_hash(content),
        ),
    ]
    (shared_dir / "review-patterns.md").write_text(render_shared_file(entries))

    result = await import_memories(shared_dir)
    assert result.imported == 0
    assert result.skipped == 1


async def test_import_memories_ignored_hashes(tmp_path: Path) -> None:
    """import_memories() skips entries in ignored_hashes."""
    from sova.knowledge.sharing import SharedEntry, import_memories, render_shared_file

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    entries = [
        SharedEntry(
            memory_id=1,
            title="Rejected Pattern",
            content="User does not want this.",
            category="review_pattern",
            tags=[],
            source_repo="other-repo",
            source_date="2026-05-01",
            content_hash="ignoredHash12345",
        ),
    ]
    (shared_dir / "review-patterns.md").write_text(render_shared_file(entries))

    result = await import_memories(shared_dir, ignored_hashes=["ignoredHash12345"])
    assert result.imported == 0
    assert result.ignored == 1


async def test_import_memories_dry_run(tmp_path: Path) -> None:
    """import_memories(dry_run=True) does not modify the DB."""
    from sova.knowledge.memory import search
    from sova.knowledge.sharing import SharedEntry, import_memories, render_shared_file

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    entries = [
        SharedEntry(
            memory_id=1,
            title="Dry Run Pattern",
            content="Should not be stored.",
            category="review_pattern",
            tags=[],
            source_repo="other-repo",
            source_date="2026-05-01",
            content_hash="dryrun1234567890",
        ),
    ]
    (shared_dir / "review-patterns.md").write_text(render_shared_file(entries))

    result = await import_memories(shared_dir, dry_run=True)
    assert result.imported == 1
    assert "Dry Run Pattern" in result.entries

    # Verify NOT in DB
    db_mems = await search(tier="shared")
    assert len(db_mems) == 0


async def test_import_memories_empty_dir(tmp_path: Path) -> None:
    """import_memories() is a no-op for empty shared directory."""
    from sova.knowledge.sharing import import_memories

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    result = await import_memories(shared_dir)
    assert result.imported == 0
    assert result.skipped == 0


async def test_import_memories_nonexistent_dir(tmp_path: Path) -> None:
    """import_memories() is a no-op when shared directory does not exist."""
    from sova.knowledge.sharing import import_memories

    result = await import_memories(tmp_path / "nonexistent")
    assert result.imported == 0


# ---------------------------------------------------------------------------
# Round-trip: export then import
# ---------------------------------------------------------------------------


async def test_export_then_import_round_trip(tmp_path: Path) -> None:
    """Exporting memories then importing them into a fresh DB preserves entries."""
    from sova.knowledge.memory import search, store
    from sova.knowledge.sharing import export_memories, import_memories

    # Seed exportable memories
    await store(
        category="review_pattern",
        title="Always check return values",
        content="Functions may return None.\n\n[confirmed: 2]",
        tags=["python"],
        tier="project",
        repo="source-repo",
    )

    shared_dir = tmp_path / "shared"
    export_result = await export_memories(shared_dir, repo="source-repo")
    assert export_result.exported == 1

    # Reset DB to simulate a different installation
    from sova.db.session import close_db

    await close_db()
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)

    import_result = await import_memories(shared_dir)
    assert import_result.imported == 1

    db_mems = await search(tier="shared")
    assert len(db_mems) == 1
    assert db_mems[0].title == "Always check return values"
    assert "[confirmed:" not in db_mems[0].content


# ---------------------------------------------------------------------------
# Dashboard API endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shared_category,shared_title,project_category,project_title",
    [
        ("learning", "Shared mem", "learning", "Project mem"),
        ("review_pattern", "Shared entry", "learning", "Project entry"),
    ],
)
async def test_api_list_memories_with_tier_filter(
    shared_category: str,
    shared_title: str,
    project_category: str,
    project_title: str,
) -> None:
    """GET /api/memory?tier=shared filters to shared-tier only."""
    from httpx import ASGITransport, AsyncClient

    from sova.dashboard.app import create_app
    from sova.knowledge.memory import store

    await store(category=shared_category, title=shared_title, content="Shared.", tags=[], tier="shared")
    await store(category=project_category, title=project_title, content="Local.", tags=[], tier="project")

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/memory?tier=shared")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["memories"][0]["title"] == shared_title


async def test_api_export_memories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/memory/export exports project memories to shared dir."""
    from unittest.mock import MagicMock

    from httpx import ASGITransport, AsyncClient

    from sova.dashboard.app import create_app
    from sova.knowledge.memory import store

    await store(
        category="review_pattern",
        title="Export via API",
        content="Pattern content.\n\n[confirmed: 2]",
        tags=["test"],
        tier="project",
        repo="test-repo",
    )

    shared_dir = tmp_path / "shared"
    cfg = MagicMock()
    cfg.shared_knowledge_path = shared_dir
    cfg.shared_knowledge_categories = ["review_pattern", "common_mistake", "codebase_pattern", "ci_pattern"]
    cfg.github_repo = "test-repo"

    monkeypatch.setattr("sova.dashboard.routers.memory.get_project_dir", lambda: tmp_path)
    monkeypatch.setattr("sova.dashboard.routers.memory.load_config", lambda _: cfg)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/memory/export")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exported"] == 1
        assert "Export via API" in data["entries"]

    assert (shared_dir / "review-patterns.md").is_file()


async def test_api_export_memories_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/memory/export?dry_run=true does not write files."""
    from unittest.mock import MagicMock

    from httpx import ASGITransport, AsyncClient

    from sova.dashboard.app import create_app
    from sova.knowledge.memory import store

    await store(
        category="review_pattern",
        title="Dry Run Export",
        content="Content.\n\n[confirmed: 1]",
        tags=[],
        tier="project",
        repo="test-repo",
    )

    shared_dir = tmp_path / "shared"
    cfg = MagicMock()
    cfg.shared_knowledge_path = shared_dir
    cfg.shared_knowledge_categories = ["review_pattern"]
    cfg.github_repo = "test-repo"

    monkeypatch.setattr("sova.dashboard.routers.memory.get_project_dir", lambda: tmp_path)
    monkeypatch.setattr("sova.dashboard.routers.memory.load_config", lambda _: cfg)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/memory/export?dry_run=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exported"] == 1

    assert not shared_dir.exists()


async def test_api_import_memories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/memory/import imports entries from shared dir."""
    from unittest.mock import MagicMock

    from httpx import ASGITransport, AsyncClient

    from sova.dashboard.app import create_app
    from sova.knowledge.memory import search
    from sova.knowledge.sharing import SharedEntry, render_shared_file

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    entries = [
        SharedEntry(
            memory_id=1,
            title="API Import Pattern",
            content="Imported via API.",
            category="review_pattern",
            tags=["api"],
            source_repo="other-repo",
            source_date="2026-06-01",
            content_hash="apiimport12345678",
        ),
    ]
    (shared_dir / "review-patterns.md").write_text(render_shared_file(entries))

    cfg = MagicMock()
    cfg.shared_knowledge_path = shared_dir
    cfg.ignored_shared_hashes = []

    monkeypatch.setattr("sova.dashboard.routers.memory.get_project_dir", lambda: tmp_path)
    monkeypatch.setattr("sova.dashboard.routers.memory.load_config", lambda _: cfg)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/memory/import")
        assert resp.status_code == 200
        data = resp.json()
        assert data["imported"] == 1
        assert "API Import Pattern" in data["entries"]

    db_mems = await search(tier="shared")
    assert len(db_mems) == 1
    assert db_mems[0].title == "API Import Pattern"


async def test_api_import_memories_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/memory/import?dry_run=true does not modify DB."""
    from unittest.mock import MagicMock

    from httpx import ASGITransport, AsyncClient

    from sova.dashboard.app import create_app
    from sova.knowledge.memory import search
    from sova.knowledge.sharing import SharedEntry, render_shared_file

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    entries = [
        SharedEntry(
            memory_id=1,
            title="Dry Run Import",
            content="Should not persist.",
            category="review_pattern",
            tags=[],
            source_repo="other-repo",
            source_date="2026-06-01",
            content_hash="dryrunimport1234",
        ),
    ]
    (shared_dir / "review-patterns.md").write_text(render_shared_file(entries))

    cfg = MagicMock()
    cfg.shared_knowledge_path = shared_dir
    cfg.ignored_shared_hashes = []

    monkeypatch.setattr("sova.dashboard.routers.memory.get_project_dir", lambda: tmp_path)
    monkeypatch.setattr("sova.dashboard.routers.memory.load_config", lambda _: cfg)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/memory/import?dry_run=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["imported"] == 1

    db_mems = await search(tier="shared")
    assert len(db_mems) == 0


# ---------------------------------------------------------------------------
# Pure function edge cases: compute_content_hash
# ---------------------------------------------------------------------------


def test_compute_content_hash_unicode() -> None:
    """compute_content_hash() handles unicode consistently."""
    from sova.knowledge.sharing import compute_content_hash

    h1 = compute_content_hash("Prevence chyb v kodu")
    h2 = compute_content_hash("Prevence chyb v kodu")
    assert h1 == h2
    assert len(h1) == 16


def test_compute_content_hash_empty_string() -> None:
    """compute_content_hash() handles empty string."""
    from sova.knowledge.sharing import compute_content_hash

    result = compute_content_hash("")
    assert len(result) == 16
    # Empty and whitespace-only should hash the same (both strip to "")
    assert result == compute_content_hash("   ")


# ---------------------------------------------------------------------------
# Pure function edge cases: parse_shared_file
# ---------------------------------------------------------------------------


def test_parse_shared_file_crlf_line_endings(tmp_path: Path) -> None:
    """parse_shared_file() handles Windows CRLF line endings."""
    from sova.knowledge.sharing import parse_shared_file

    content = "## CRLF Pattern\r\n\r\nContent with Windows line endings.\r\n"
    filepath = tmp_path / "conventions.md"
    filepath.write_bytes(content.encode("utf-8"))

    entries = parse_shared_file(filepath)
    assert len(entries) == 1
    assert entries[0].title == "CRLF Pattern"


def test_parse_shared_file_sections_without_heading_skipped(tmp_path: Path) -> None:
    """parse_shared_file() skips sections that have no ## heading."""
    from sova.knowledge.sharing import parse_shared_file

    content = """Just some text without heading.

---

## Valid Section

Content here.
"""
    filepath = tmp_path / "conventions.md"
    filepath.write_text(content)

    entries = parse_shared_file(filepath)
    assert len(entries) == 1
    assert entries[0].title == "Valid Section"


def test_parse_shared_file_code_block_with_triple_dash(tmp_path: Path) -> None:
    """parse_shared_file() splits on --- inside code blocks (known limitation).

    The regex `re.split(r'(?m)^---\\s*$', text)` splits on any line that is
    just `---`, even inside fenced code blocks. This test documents the behavior.
    """
    from sova.knowledge.sharing import parse_shared_file

    content = """## Code Example

Some text.

```yaml
key: value
---
another: value
```

---

## Second Entry

More content.
"""
    filepath = tmp_path / "conventions.md"
    filepath.write_text(content)

    entries = parse_shared_file(filepath)
    # Known limitation: the --- inside the code block splits the section,
    # so "Code Example" section is truncated. We get at least the second entry.
    titles = [e.title for e in entries]
    assert "Second Entry" in titles


def test_parse_shared_file_non_numeric_memory_id(tmp_path: Path) -> None:
    """parse_shared_file() sets memory_id=None for non-numeric IDs."""
    from sova.knowledge.sharing import parse_shared_file

    content = """## Non-numeric ID

<!-- memory:abc hash:a1b2c3d4e5f6g7h8 repo:test date:2026-06-01 -->

Content here.
"""
    filepath = tmp_path / "review-patterns.md"
    filepath.write_text(content)

    entries = parse_shared_file(filepath)
    assert len(entries) == 1
    assert entries[0].memory_id is None


@pytest.mark.parametrize(
    "filename,expected_category",
    [
        ("review-patterns.md", "review_pattern"),
        ("common-mistakes.md", "common_mistake"),
        ("conventions.md", "codebase_pattern"),
        ("ci-patterns.md", "ci_pattern"),
    ],
)
def test_parse_shared_file_category_from_filename(tmp_path: Path, filename: str, expected_category: str) -> None:
    """parse_shared_file() maps known filenames to categories."""
    from sova.knowledge.sharing import parse_shared_file

    filepath = tmp_path / filename
    filepath.write_text("## Test\n\nContent.\n")
    entries = parse_shared_file(filepath)
    assert len(entries) == 1
    assert entries[0].category == expected_category


def test_parse_shared_file_unknown_filename_defaults_to_learning(tmp_path: Path) -> None:
    """parse_shared_file() uses 'learning' category for unknown filenames."""
    from sova.knowledge.sharing import parse_shared_file

    filepath = tmp_path / "random-notes.md"
    filepath.write_text("## Note\n\nSome note.\n")

    entries = parse_shared_file(filepath)
    assert len(entries) == 1
    assert entries[0].category == "learning"


# ---------------------------------------------------------------------------
# Pure function edge cases: render_shared_file
# ---------------------------------------------------------------------------


def test_render_shared_file_none_memory_id() -> None:
    """render_shared_file() renders memory_id=None as 'memory:none'."""
    from sova.knowledge.sharing import SharedEntry, render_shared_file

    entries = [
        SharedEntry(
            memory_id=None,
            title="No ID",
            content="Content.",
            category="review_pattern",
            content_hash="abcdef1234567890",
            source_repo="repo",
            source_date="2026-06-01",
        ),
    ]
    result = render_shared_file(entries)
    assert "memory:none" in result


def test_render_shared_file_no_tags_omits_tags_line() -> None:
    """render_shared_file() does not add a Tags line when tags is empty."""
    from sova.knowledge.sharing import SharedEntry, render_shared_file

    entries = [
        SharedEntry(
            memory_id=1,
            title="No Tags",
            content="Content.",
            category="review_pattern",
            tags=[],
            source_repo="repo",
            source_date="2026-06-01",
            content_hash="abcdef1234567890",
        ),
    ]
    result = render_shared_file(entries)
    assert "Tags:" not in result


def test_render_shared_file_auto_computes_hash() -> None:
    """render_shared_file() auto-computes hash when content_hash is empty."""
    from sova.knowledge.sharing import SharedEntry, compute_content_hash, render_shared_file

    entries = [
        SharedEntry(
            memory_id=1,
            title="Auto Hash",
            content="Hash me.",
            category="review_pattern",
            content_hash="",
            source_repo="repo",
            source_date="2026-06-01",
        ),
    ]
    result = render_shared_file(entries)
    expected_hash = compute_content_hash("Hash me.")
    assert f"hash:{expected_hash}" in result


def test_render_shared_file_defaults_repo_and_date() -> None:
    """render_shared_file() uses 'unknown' repo and today's date when missing."""
    from datetime import date

    from sova.knowledge.sharing import SharedEntry, render_shared_file

    entries = [
        SharedEntry(
            memory_id=1,
            title="Defaults",
            content="Content.",
            category="review_pattern",
            source_repo="",
            source_date="",
            content_hash="abcdef1234567890",
        ),
    ]
    result = render_shared_file(entries)
    assert "repo:unknown" in result
    assert f"date:{date.today().isoformat()}" in result


# ---------------------------------------------------------------------------
# Async edge cases: export / import
# ---------------------------------------------------------------------------


async def test_export_memories_category_filter(tmp_path: Path) -> None:
    """export_memories(categories=[...]) only exports matching categories."""
    from sova.knowledge.memory import store
    from sova.knowledge.sharing import export_memories

    await store(
        category="review_pattern",
        title="Review only",
        content="Pattern.\n\n[confirmed: 2]",
        tags=[],
        tier="project",
        repo="test-repo",
    )
    await store(
        category="common_mistake",
        title="Mistake only",
        content="Mistake.\n\n[confirmed: 2]",
        tags=[],
        tier="project",
        repo="test-repo",
    )

    shared_dir = tmp_path / "shared"
    result = await export_memories(shared_dir, categories=["review_pattern"], repo="test-repo")

    assert result.exported == 1
    assert "Review only" in result.entries
    assert "Mistake only" not in result.entries


async def test_import_memories_multiple_files(tmp_path: Path) -> None:
    """import_memories() reads entries from multiple .md files."""
    from sova.knowledge.memory import search
    from sova.knowledge.sharing import SharedEntry, import_memories, render_shared_file

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    (shared_dir / "review-patterns.md").write_text(
        render_shared_file(
            [
                SharedEntry(
                    memory_id=1,
                    title="Review Entry",
                    content="Review content.",
                    category="review_pattern",
                    content_hash="review123456789a",
                    source_repo="repo",
                    source_date="2026-06-01",
                ),
            ]
        )
    )
    (shared_dir / "common-mistakes.md").write_text(
        render_shared_file(
            [
                SharedEntry(
                    memory_id=2,
                    title="Mistake Entry",
                    content="Mistake content.",
                    category="common_mistake",
                    content_hash="mistake12345678ab",
                    source_repo="repo",
                    source_date="2026-06-01",
                ),
            ]
        )
    )

    result = await import_memories(shared_dir)
    assert result.imported == 2

    db_mems = await search(tier="shared")
    titles = {m.title for m in db_mems}
    assert "Review Entry" in titles
    assert "Mistake Entry" in titles
