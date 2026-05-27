"""Tests for SOVA knowledge management module."""

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
# memory.py -- CRUD operations
# ---------------------------------------------------------------------------


async def test_store_creates_memory() -> None:
    """store() creates a Memory row and returns it."""
    from sova.knowledge.memory import store

    mem = await store(
        category="learning",
        title="Always quote bash variables",
        content="Unquoted variables split on whitespace.",
        tags=["bash", "quoting"],
        tier="project",
    )
    assert mem.id is not None
    assert mem.category == "learning"
    assert mem.title == "Always quote bash variables"
    assert mem.tags == "bash,quoting"
    assert mem.tier == "project"


async def test_store_with_optional_fields() -> None:
    """store() accepts repo and issue_number."""
    from sova.knowledge.memory import store

    mem = await store(
        category="review",
        title="Check edge cases",
        content="Off-by-one errors are common.",
        tags=["review"],
        repo="user/project",
        issue_number="42",
    )
    assert mem.repo == "user/project"
    assert mem.issue_number == "42"


async def test_get_by_id() -> None:
    """get() retrieves a memory by ID."""
    from sova.knowledge.memory import get, store

    created = await store(category="learning", title="Test title", content="Test content", tags=[])
    fetched = await get(created.id)
    assert fetched is not None
    assert fetched.title == "Test title"


async def test_get_nonexistent_returns_none() -> None:
    """get() returns None for nonexistent ID."""
    from sova.knowledge.memory import get

    assert await get(9999) is None


async def test_update_memory() -> None:
    """update() modifies fields on an existing memory."""
    from sova.knowledge.memory import get, store, update

    mem = await store(category="learning", title="Old title", content="Old content", tags=[])
    updated = await update(mem.id, title="New title", content="New content")
    assert updated is not None
    assert updated.title == "New title"
    assert updated.content == "New content"

    fetched = await get(mem.id)
    assert fetched is not None
    assert fetched.title == "New title"


async def test_update_nonexistent_returns_none() -> None:
    """update() returns None for nonexistent ID."""
    from sova.knowledge.memory import update

    assert await update(9999, title="x") is None


async def test_delete_memory() -> None:
    """delete() removes a memory and returns True."""
    from sova.knowledge.memory import delete, get, store

    mem = await store(category="learning", title="To delete", content="Gone soon.", tags=[])
    assert await delete(mem.id) is True
    assert await get(mem.id) is None


async def test_delete_nonexistent_returns_false() -> None:
    """delete() returns False for nonexistent ID."""
    from sova.knowledge.memory import delete

    assert await delete(9999) is False


# ---------------------------------------------------------------------------
# memory.py -- search
# ---------------------------------------------------------------------------


async def _seed_memories() -> None:
    """Seed test memories for search tests."""
    from sova.knowledge.memory import store

    await store(
        category="learning",
        title="Bash quoting",
        content="Always double-quote variables in bash.",
        tags=["bash", "shell"],
        tier="project",
    )
    await store(
        category="review",
        title="Check imports",
        content="Stale imports after refactoring are common.",
        tags=["python", "imports"],
        tier="project",
    )
    await store(
        category="learning",
        title="Async testing",
        content="Use pytest-asyncio with auto mode for async tests.",
        tags=["python", "testing"],
        tier="shared",
    )
    await store(
        category="debugging",
        title="JSON parse errors",
        content="gh CLI may return empty stdout, wrap in try/except.",
        tags=["bash", "github"],
        tier="project",
    )


async def test_search_by_category() -> None:
    """search() filters by category."""
    from sova.knowledge.memory import search

    await _seed_memories()
    results = await search(category="learning")
    assert len(results) == 2
    assert all(m.category == "learning" for m in results)


async def test_search_by_tags() -> None:
    """search() filters by tags (any match)."""
    from sova.knowledge.memory import search

    await _seed_memories()
    results = await search(tags=["python"])
    assert len(results) == 2


async def test_search_by_query_in_content() -> None:
    """search() filters by text query matching title or content."""
    from sova.knowledge.memory import search

    await _seed_memories()
    results = await search(query="bash")
    assert len(results) >= 2  # "Bash quoting" + "JSON parse errors" (tag: bash)


async def test_search_by_tier() -> None:
    """search() filters by tier."""
    from sova.knowledge.memory import search

    await _seed_memories()
    results = await search(tier="shared")
    assert len(results) == 1
    assert results[0].title == "Async testing"


async def test_search_combined_filters() -> None:
    """search() combines category + tags."""
    from sova.knowledge.memory import search

    await _seed_memories()
    results = await search(category="learning", tags=["bash"])
    assert len(results) == 1
    assert results[0].title == "Bash quoting"


async def test_search_no_results() -> None:
    """search() returns empty list when nothing matches."""
    from sova.knowledge.memory import search

    await _seed_memories()
    results = await search(category="nonexistent")
    assert results == []


async def test_search_excludes_superseded() -> None:
    """search() excludes superseded memories by default."""
    from sova.knowledge.memory import search, store, supersede

    old = await store(category="learning", title="Old pattern", content="Outdated.", tags=["test"])
    new = await store(category="learning", title="New pattern", content="Current.", tags=["test"])
    await supersede(old.id, new.id)
    results = await search(category="learning")
    assert len(results) == 1
    assert results[0].id == new.id


# ---------------------------------------------------------------------------
# memory.py -- promote and supersede
# ---------------------------------------------------------------------------


async def test_promote_changes_tier() -> None:
    """promote() updates the tier field."""
    from sova.knowledge.memory import get, promote, store

    mem = await store(category="learning", title="Promotable", content="Good pattern.", tags=[], tier="project")
    result = await promote(mem.id, "shared")
    assert result is not None
    assert result.tier == "shared"

    fetched = await get(mem.id)
    assert fetched is not None
    assert fetched.tier == "shared"


async def test_promote_nonexistent_returns_none() -> None:
    """promote() returns None for nonexistent ID."""
    from sova.knowledge.memory import promote

    assert await promote(9999, "shared") is None


async def test_supersede_marks_old_entry() -> None:
    """supersede() sets superseded_by on the old entry."""
    from sova.knowledge.memory import get, store, supersede

    old = await store(category="learning", title="Old", content="Outdated.", tags=[])
    new = await store(category="learning", title="New", content="Current.", tags=[])
    result = await supersede(old.id, new.id)
    assert result is True

    old_fetched = await get(old.id)
    assert old_fetched is not None
    assert old_fetched.superseded_by == new.id


async def test_supersede_nonexistent_returns_false() -> None:
    """supersede() returns False if the old entry doesn't exist."""
    from sova.knowledge.memory import store, supersede

    new = await store(category="learning", title="New", content="Current.", tags=[])
    assert await supersede(9999, new.id) is False


# ---------------------------------------------------------------------------
# tiers.py -- tier loading
# ---------------------------------------------------------------------------


async def test_load_tier_returns_memories_for_tier() -> None:
    """load_tier() returns all non-superseded memories for a given tier."""
    from sova.knowledge.memory import store
    from sova.knowledge.tiers import load_tier

    await store(category="learning", title="P1", content="Project.", tags=[], tier="project")
    await store(category="learning", title="S1", content="Shared.", tags=[], tier="shared")
    await store(category="review", title="P2", content="Project too.", tags=[], tier="project")

    project_mems = await load_tier("project")
    assert len(project_mems) == 2
    assert all(m.tier == "project" for m in project_mems)


async def test_load_context_returns_relevant_knowledge(tmp_path: Path) -> None:
    """load_context() combines file-based tiers + DB tier filtering."""
    from sova.knowledge.memory import store
    from sova.knowledge.tiers import load_context

    await store(category="learning", title="L1", content="Learn.", tags=["python"], tier="project")
    await store(category="review", title="R1", content="Review.", tags=["python"], tier="project")
    await store(category="learning", title="L2", content="Shared.", tags=["python"], tier="shared")

    # Create a minimal config-like object
    class _Cfg:
        shared_knowledge_path = tmp_path / "nonexistent"

    ctx = await load_context(None, tmp_path, _Cfg(), tier="project", category="learning")
    assert "L1" in ctx
    assert "R1" not in ctx


async def test_format_knowledge_for_prompt() -> None:
    """format_for_prompt() formats memories into a prompt-friendly string."""
    from sova.knowledge.memory import store
    from sova.knowledge.tiers import format_for_prompt, load_tier

    await store(category="learning", title="Pattern A", content="Do X.", tags=["bash"], tier="project")
    await store(category="review", title="Pattern B", content="Do Y.", tags=["python"], tier="project")

    memories = await load_tier("project")
    formatted = format_for_prompt(memories)
    assert "Pattern A" in formatted
    assert "Do X." in formatted
    assert "Pattern B" in formatted


# ---------------------------------------------------------------------------
# personas.py -- detect_persona
# ---------------------------------------------------------------------------


def test_detect_persona_django(tmp_path: Path) -> None:
    """detect_persona() returns 'django' for manage.py + django in requirements."""
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python")
    (tmp_path / "requirements.txt").write_text("django>=4.2\ncelery\n")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "django"


def test_detect_persona_node(tmp_path: Path) -> None:
    """detect_persona() returns 'node' for package.json."""
    (tmp_path / "package.json").write_text('{"name": "test"}')

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "node"


def test_detect_persona_go(tmp_path: Path) -> None:
    """detect_persona() returns 'go' for go.mod."""
    (tmp_path / "go.mod").write_text("module example.com/test")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "go"


def test_detect_persona_rust(tmp_path: Path) -> None:
    """detect_persona() returns 'rust' for Cargo.toml."""
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "test"')

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "rust"


def test_detect_persona_python(tmp_path: Path) -> None:
    """detect_persona() returns 'python' for pyproject.toml."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "python"


def test_detect_persona_ruby(tmp_path: Path) -> None:
    """detect_persona() returns 'ruby' for Gemfile."""
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "ruby"


def test_detect_persona_fastapi(tmp_path: Path) -> None:
    """detect_persona() returns 'fastapi' when fastapi is in requirements."""
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["fastapi>=0.100"]\n')

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "fastapi"


def test_detect_persona_fastapi_in_nested_setup(tmp_path: Path) -> None:
    """detect_persona() finds fastapi in a nested setup.py (monorepo layout)."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n")
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "setup.py").write_text('install_requires=["fastapi>=0.104"]\n')

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "fastapi"


def test_detect_persona_odoo(tmp_path: Path) -> None:
    """detect_persona() returns 'odoo' for __manifest__.py with Odoo keys."""
    mod_dir = tmp_path / "my_module"
    mod_dir.mkdir()
    (mod_dir / "__manifest__.py").write_text("{'name': 'Test', 'installable': True, 'depends': ['base']}")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "odoo"


def test_detect_persona_odoo_requires_odoo_keys(tmp_path: Path) -> None:
    """__manifest__.py without Odoo-specific keys does not trigger Odoo detection."""
    mod_dir = tmp_path / "my_module"
    mod_dir.mkdir()
    (mod_dir / "__manifest__.py").write_text("{'name': 'Test'}")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "python"


def test_detect_persona_django_over_fastapi(tmp_path: Path) -> None:
    """Django detection takes priority over FastAPI when both present."""
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python")
    (tmp_path / "requirements.txt").write_text("django>=4.2\nfastapi\n")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "django"


def test_detect_persona_no_match(tmp_path: Path) -> None:
    """detect_persona() returns None when no markers found."""
    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) is None


# ---------------------------------------------------------------------------
# personas.py -- load_persona
# ---------------------------------------------------------------------------


def test_load_persona_found(tmp_path: Path) -> None:
    """load_persona() reads persona markdown when file exists."""
    (tmp_path / "django.md").write_text("# Django\nUse class-based views.")

    from sova.knowledge.personas import load_persona

    content = load_persona("django", personas_dir=tmp_path)
    assert "class-based views" in content


def test_load_persona_not_found(tmp_path: Path) -> None:
    """load_persona() returns empty string when persona file missing."""
    from sova.knowledge.personas import load_persona

    assert load_persona("nonexistent", personas_dir=tmp_path) == ""


# ---------------------------------------------------------------------------
# review_patterns.py
# ---------------------------------------------------------------------------


async def test_record_review_finding() -> None:
    """record_review_finding() stores a review_pattern memory."""
    from sova.knowledge.review_patterns import record_review_finding

    mem = await record_review_finding(None, category="style", pattern="Use f-strings over .format()", source_pr="#10")
    assert mem.id is not None
    assert mem.category == "review_pattern"
    assert "style" in mem.tags
    assert "#10" in mem.tags
    assert mem.content == "Use f-strings over .format()"


async def test_record_review_finding_no_pr() -> None:
    """record_review_finding() works without source_pr."""
    from sova.knowledge.review_patterns import record_review_finding

    mem = await record_review_finding(None, category="bug", pattern="Check null before access")
    assert "#" not in mem.tags


async def test_get_common_patterns() -> None:
    """get_common_patterns() returns review_pattern memories sorted by updated_at."""
    from sova.knowledge.review_patterns import get_common_patterns, record_review_finding

    await record_review_finding(None, category="style", pattern="Pattern A")
    await record_review_finding(None, category="perf", pattern="Pattern B")

    patterns = await get_common_patterns(None)
    assert len(patterns) == 2
    assert all(m.category == "review_pattern" for m in patterns)


async def test_get_common_patterns_empty() -> None:
    """get_common_patterns() returns empty list when no patterns exist."""
    from sova.knowledge.review_patterns import get_common_patterns

    assert await get_common_patterns(None) == []


# ---------------------------------------------------------------------------
# tiers.py -- load_context with file-based tiers
# ---------------------------------------------------------------------------


async def test_load_context_with_rules_files(tmp_path: Path) -> None:
    """load_context() includes .claude/rules/*.md content."""
    from sova.knowledge.tiers import load_context

    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "coding.md").write_text("Always use type hints.")

    class _Cfg:
        shared_knowledge_path = tmp_path / "nonexistent"

    result = await load_context(None, tmp_path, _Cfg())
    assert "Always use type hints." in result
    assert "Project Rules (Tier 1)" in result


async def test_load_context_with_shared_knowledge(tmp_path: Path) -> None:
    """load_context() includes shared knowledge when path exists."""
    from sova.knowledge.tiers import load_context

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "patterns.md").write_text("Avoid global state.")

    class _Cfg:
        shared_knowledge_path = shared_dir

    result = await load_context(None, tmp_path, _Cfg())
    assert "Avoid global state." in result
    assert "Shared Knowledge (Tier 0)" in result


async def test_load_context_combines_all_tiers(tmp_path: Path) -> None:
    """load_context() combines file-based tiers with DB memories."""
    from sova.knowledge.memory import store
    from sova.knowledge.tiers import load_context

    # Tier 0: shared knowledge
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "global.md").write_text("Shared rule.")

    # Tier 1: project rules
    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "project.md").write_text("Project rule.")

    # Tier 2: DB memories
    await store(category="learning", title="DB Pattern", content="DB content.", tags=[], tier="project")

    class _Cfg:
        shared_knowledge_path = shared_dir

    result = await load_context(None, tmp_path, _Cfg(), tier="project")
    assert "Shared rule." in result
    assert "Project rule." in result
    assert "DB Pattern" in result


async def test_load_context_empty_project(tmp_path: Path) -> None:
    """load_context() returns empty string when no knowledge sources exist."""
    from sova.knowledge.tiers import load_context

    class _Cfg:
        shared_knowledge_path = tmp_path / "nonexistent"

    result = await load_context(None, tmp_path, _Cfg())
    assert result == ""
