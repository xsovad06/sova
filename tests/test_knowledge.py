"""Tests for SOVA knowledge management module."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db(tmp_path, worker_id):
    """Initialize a fresh in-memory DB for each test.

    Uses file-based DB for parallel execution to avoid worker isolation issues.
    """
    if worker_id == "master":
        # Single-process execution
        os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    else:
        # Parallel execution: use unique file per worker to avoid conflicts
        db_file = tmp_path / f"test_{worker_id}.db"
        os.environ["SOVA_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file}"

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


async def test_store_explicit_embedding_skips_auto_link() -> None:
    """store() with explicit embedding does not call auto_link."""
    from unittest.mock import patch

    from sova.knowledge.memory import store

    with patch("sova.knowledge.graph.auto_link") as mock_al:
        mem = await store(
            category="learning",
            title="Explicit emb",
            content="Content",
            tags=[],
            embedding=[1.0, 0.0],
        )
    assert mem.id is not None
    mock_al.assert_not_called()


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
# personas.py -- _package_json_mention
# ---------------------------------------------------------------------------


def test_package_json_mention_found_in_deps(tmp_path: Path) -> None:
    """_package_json_mention detects package in dependencies."""
    pkg = json.dumps({"name": "test", "dependencies": {"@patternfly/react-core": "^5.0.0"}})
    (tmp_path / "package.json").write_text(pkg)

    from sova.knowledge.personas import package_json_mention

    assert package_json_mention(tmp_path, "@patternfly/react-core") is True


def test_package_json_mention_found_in_devdeps(tmp_path: Path) -> None:
    """_package_json_mention detects package in devDependencies."""
    pkg = json.dumps({"name": "test", "devDependencies": {"@patternfly/react-core": "^5.0.0"}})
    (tmp_path / "package.json").write_text(pkg)

    from sova.knowledge.personas import package_json_mention

    assert package_json_mention(tmp_path, "@patternfly/react-core") is True


def test_package_json_mention_not_found(tmp_path: Path) -> None:
    """_package_json_mention returns False when package not listed."""
    pkg = json.dumps({"name": "test", "dependencies": {"react": "^18.0.0"}})
    (tmp_path / "package.json").write_text(pkg)

    from sova.knowledge.personas import package_json_mention

    assert package_json_mention(tmp_path, "@patternfly/react-core") is False


def test_package_json_mention_no_file(tmp_path: Path) -> None:
    """_package_json_mention returns False when no package.json."""
    from sova.knowledge.personas import package_json_mention

    assert package_json_mention(tmp_path, "@patternfly/react-core") is False


def test_package_json_mention_malformed(tmp_path: Path) -> None:
    """_package_json_mention returns False for malformed JSON."""
    (tmp_path / "package.json").write_text("{bad json")

    from sova.knowledge.personas import package_json_mention

    assert package_json_mention(tmp_path, "@patternfly/react-core") is False


def test_package_json_mention_non_dict(tmp_path: Path) -> None:
    """_package_json_mention returns False when JSON is not a dict."""
    (tmp_path / "package.json").write_text("[1, 2, 3]")

    from sova.knowledge.personas import package_json_mention

    assert package_json_mention(tmp_path, "@patternfly/react-core") is False


# ---------------------------------------------------------------------------
# personas.py -- PatternFly detection
# ---------------------------------------------------------------------------


def test_detect_persona_patternfly(tmp_path: Path) -> None:
    """detect_persona() returns 'patternfly' for @patternfly/react-core in package.json."""
    pkg = json.dumps({"name": "test", "dependencies": {"@patternfly/react-core": "^5.0.0", "react": "^18.0.0"}})
    (tmp_path / "package.json").write_text(pkg)

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "patternfly"


def test_detect_persona_patternfly_over_node(tmp_path: Path) -> None:
    """PatternFly detection takes priority over generic node."""
    pkg = json.dumps({"name": "test", "dependencies": {"@patternfly/react-core": "^5.0.0"}})
    (tmp_path / "package.json").write_text(pkg)

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "patternfly"


def test_detect_persona_node_without_patternfly(tmp_path: Path) -> None:
    """Plain node project without PatternFly still returns 'node'."""
    pkg = json.dumps({"name": "test", "dependencies": {"express": "^4.0.0"}})
    (tmp_path / "package.json").write_text(pkg)

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "node"


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


# ---------------------------------------------------------------------------
# graph.py -- edge CRUD
# ---------------------------------------------------------------------------


async def _create_two_memories() -> tuple:
    """Helper: create two memories and return their IDs."""
    from sova.knowledge.memory import store

    m1 = await store(category="learning", title="Memory A", content="Content A", tags=["test"], embedding=None)
    m2 = await store(category="learning", title="Memory B", content="Content B", tags=["test"], embedding=None)
    return m1, m2


async def test_create_edge() -> None:
    """create_edge() creates a directed edge between two memories."""
    from sova.knowledge.graph import create_edge

    m1, m2 = await _create_two_memories()
    edge = await create_edge(m1.id, m2.id, relation="relates_to")
    assert edge is not None
    assert edge.source_id == m1.id
    assert edge.target_id == m2.id
    assert edge.relation == "relates_to"


async def test_create_edge_self_loop_rejected() -> None:
    """create_edge() raises ValueError for self-edges."""
    from sova.knowledge.graph import create_edge
    from sova.knowledge.memory import store

    m = await store(category="learning", title="Self", content="Self", tags=[], embedding=None)
    with pytest.raises(ValueError, match="Self-edges"):
        await create_edge(m.id, m.id)


async def test_create_edge_invalid_relation() -> None:
    """create_edge() raises ValueError for unknown relation types."""
    from sova.knowledge.graph import create_edge

    m1, m2 = await _create_two_memories()
    with pytest.raises(ValueError, match="Invalid relation"):
        await create_edge(m1.id, m2.id, relation="unknown_type")


async def test_create_edge_invalid_memory_ids() -> None:
    """create_edge() raises ValueError when memory IDs don't exist."""
    from sova.knowledge.graph import create_edge

    m1, _ = await _create_two_memories()
    with pytest.raises(ValueError, match="do not exist"):
        await create_edge(m1.id, 99999, relation="relates_to")
    with pytest.raises(ValueError, match="do not exist"):
        await create_edge(99999, m1.id, relation="relates_to")


async def test_create_edge_duplicate_returns_none() -> None:
    """create_edge() returns None for duplicate (source, target, relation)."""
    from sova.knowledge.graph import create_edge

    m1, m2 = await _create_two_memories()
    first = await create_edge(m1.id, m2.id, relation="relates_to")
    assert first is not None
    duplicate = await create_edge(m1.id, m2.id, relation="relates_to")
    assert duplicate is None


async def test_create_edge_reversed_duplicate_returns_none() -> None:
    """create_edge() returns None when reversed (target, source) edge exists."""
    from sova.knowledge.graph import create_edge

    m1, m2 = await _create_two_memories()
    first = await create_edge(m1.id, m2.id, relation="relates_to")
    assert first is not None
    reversed_dup = await create_edge(m2.id, m1.id, relation="relates_to")
    assert reversed_dup is None


async def test_create_edge_different_relation_allowed() -> None:
    """Same source/target pair with different relations are distinct edges."""
    from sova.knowledge.graph import create_edge

    m1, m2 = await _create_two_memories()
    e1 = await create_edge(m1.id, m2.id, relation="relates_to")
    e2 = await create_edge(m1.id, m2.id, relation="refines")
    assert e1 is not None
    assert e2 is not None
    assert e1.id != e2.id


async def test_delete_edge() -> None:
    """delete_edge() removes an edge and returns True."""
    from sova.knowledge.graph import create_edge, delete_edge, get_edges

    m1, m2 = await _create_two_memories()
    edge = await create_edge(m1.id, m2.id)
    assert edge is not None
    assert await delete_edge(edge.id) is True
    assert await get_edges(m1.id) == []


async def test_delete_edge_nonexistent() -> None:
    """delete_edge() returns False for nonexistent edge."""
    from sova.knowledge.graph import delete_edge

    assert await delete_edge(9999) is False


async def test_get_edges() -> None:
    """get_edges() returns all edges for a memory (both directions)."""
    from sova.knowledge.graph import create_edge, get_edges
    from sova.knowledge.memory import store

    m1, m2 = await _create_two_memories()
    m3 = await store(category="learning", title="Memory C", content="Content C", tags=[], embedding=None)
    await create_edge(m1.id, m2.id)
    await create_edge(m3.id, m1.id)

    edges = await get_edges(m1.id)
    assert len(edges) == 2


# ---------------------------------------------------------------------------
# graph.py -- neighbor traversal
# ---------------------------------------------------------------------------


async def test_get_neighbors_depth_1() -> None:
    """get_neighbors() returns direct neighbors at depth=1."""
    from sova.knowledge.graph import create_edge, get_neighbors

    m1, m2 = await _create_two_memories()
    await create_edge(m1.id, m2.id)

    neighbors = await get_neighbors(m1.id, depth=1)
    assert len(neighbors) == 1
    assert neighbors[0].id == m2.id


async def test_get_neighbors_bidirectional() -> None:
    """get_neighbors() traverses edges in both directions."""
    from sova.knowledge.graph import create_edge, get_neighbors

    m1, m2 = await _create_two_memories()
    await create_edge(m1.id, m2.id)

    # Query from target side
    neighbors = await get_neighbors(m2.id, depth=1)
    assert len(neighbors) == 1
    assert neighbors[0].id == m1.id


async def test_get_neighbors_depth_2() -> None:
    """get_neighbors() traverses 2 hops."""
    from sova.knowledge.graph import create_edge, get_neighbors
    from sova.knowledge.memory import store

    m1, m2 = await _create_two_memories()
    m3 = await store(category="learning", title="Memory C", content="Content C", tags=[], embedding=None)
    await create_edge(m1.id, m2.id)
    await create_edge(m2.id, m3.id)

    neighbors = await get_neighbors(m1.id, depth=2)
    neighbor_ids = {n.id for n in neighbors}
    assert m2.id in neighbor_ids
    assert m3.id in neighbor_ids


async def test_get_neighbors_circular() -> None:
    """get_neighbors() handles circular edges without infinite loops."""
    from sova.knowledge.graph import create_edge, get_neighbors
    from sova.knowledge.memory import store

    m1, m2 = await _create_two_memories()
    m3 = await store(category="learning", title="Memory C", content="Content C", tags=[], embedding=None)
    await create_edge(m1.id, m2.id)
    await create_edge(m2.id, m3.id)
    await create_edge(m3.id, m1.id)

    neighbors = await get_neighbors(m1.id, depth=2)
    assert len(neighbors) == 2  # m2 and m3, not m1 again


async def test_get_neighbors_filters_superseded() -> None:
    """get_neighbors() excludes superseded memories."""
    from sova.knowledge.graph import create_edge, get_neighbors
    from sova.knowledge.memory import store, supersede

    m1, m2 = await _create_two_memories()
    m3 = await store(category="learning", title="Replacement", content="New", tags=[], embedding=None)
    await create_edge(m1.id, m2.id)
    await supersede(m2.id, m3.id)

    neighbors = await get_neighbors(m1.id, depth=1)
    assert len(neighbors) == 0


async def test_get_neighbors_invalid_depth() -> None:
    """get_neighbors() raises ValueError for depth outside 1-2."""
    from sova.knowledge.graph import get_neighbors

    with pytest.raises(ValueError, match="depth must be 1 or 2"):
        await get_neighbors(1, depth=0)
    with pytest.raises(ValueError, match="depth must be 1 or 2"):
        await get_neighbors(1, depth=3)


async def test_get_neighbors_filter_by_relation() -> None:
    """get_neighbors() can filter by relation type."""
    from sova.knowledge.graph import create_edge, get_neighbors
    from sova.knowledge.memory import store

    m1, m2 = await _create_two_memories()
    m3 = await store(category="learning", title="Memory C", content="Content C", tags=[], embedding=None)
    await create_edge(m1.id, m2.id, relation="relates_to")
    await create_edge(m1.id, m3.id, relation="refines")

    neighbors = await get_neighbors(m1.id, depth=1, relation="refines")
    assert len(neighbors) == 1
    assert neighbors[0].id == m3.id


# ---------------------------------------------------------------------------
# graph.py -- auto_link (requires embeddings mock)
# ---------------------------------------------------------------------------


async def test_auto_link_no_embeddings() -> None:
    """auto_link() returns empty list when embeddings unavailable."""
    from unittest.mock import patch

    from sova.knowledge.graph import auto_link

    m1, _ = await _create_two_memories()
    with patch("sova.knowledge.graph.is_available", return_value=False):
        result = await auto_link(m1.id)
    assert result == []


async def test_auto_link_creates_edges() -> None:
    """auto_link() creates edges for memories with similar embeddings."""
    from unittest.mock import patch

    from sova.knowledge.graph import auto_link
    from sova.knowledge.memory import store

    # Create memories with controlled embeddings (high similarity)
    emb_a = [1.0, 0.0, 0.0]
    emb_b = [0.95, 0.05, 0.0]  # Very similar
    emb_c = [0.0, 1.0, 0.0]  # Very different

    m1 = await store(category="learning", title="A", content="A", tags=[], embedding=emb_a)
    m2 = await store(category="learning", title="B", content="B", tags=[], embedding=emb_b)
    await store(category="learning", title="C", content="C", tags=[], embedding=emb_c)

    with patch("sova.knowledge.graph.is_available", return_value=True):
        edges = await auto_link(m1.id)

    assert len(edges) == 1
    assert edges[0].target_id == m2.id


async def test_auto_link_caps_at_max() -> None:
    """auto_link() limits to AUTO_LINK_MAX_EDGES edges."""
    from unittest.mock import patch

    from sova.knowledge.graph import AUTO_LINK_MAX_EDGES, auto_link
    from sova.knowledge.memory import store

    base_emb = [1.0, 0.0, 0.0]
    source = await store(category="learning", title="Source", content="Source", tags=[], embedding=base_emb)

    # Create more candidates than the max, all with high similarity
    for i in range(AUTO_LINK_MAX_EDGES + 3):
        emb = [0.95 - i * 0.01, 0.05 + i * 0.01, 0.0]
        await store(category="learning", title=f"T{i}", content=f"T{i}", tags=[], embedding=emb)

    with patch("sova.knowledge.graph.is_available", return_value=True):
        edges = await auto_link(source.id)

    # Must create exactly MAX_EDGES, proving the cap is enforced
    assert len(edges) == AUTO_LINK_MAX_EDGES
    # Verify edges are ordered by highest similarity (descending weight)
    weights = [e.weight for e in edges]
    assert weights == sorted(weights, reverse=True)


# ---------------------------------------------------------------------------
# graph.py -- discover_edges
# ---------------------------------------------------------------------------


async def test_discover_edges_no_embeddings() -> None:
    """discover_edges() returns 0 when embeddings unavailable."""
    from unittest.mock import patch

    from sova.knowledge.graph import discover_edges

    with patch("sova.knowledge.graph.is_available", return_value=False):
        assert await discover_edges() == 0


async def test_discover_edges_same_category() -> None:
    """discover_edges() links similar memories within the same category."""
    from unittest.mock import patch

    from sova.knowledge.graph import discover_edges, get_edges
    from sova.knowledge.memory import store

    emb_a = [1.0, 0.0, 0.0]
    emb_b = [0.95, 0.05, 0.0]
    m1 = await store(category="learning", title="A", content="A", tags=[], embedding=emb_a)
    await store(category="learning", title="B", content="B", tags=[], embedding=emb_b)

    with patch("sova.knowledge.graph.is_available", return_value=True):
        count = await discover_edges(category="learning")

    assert count == 1
    edges = await get_edges(m1.id)
    assert len(edges) == 1


async def test_discover_edges_batch_boundaries() -> None:
    """discover_edges() handles memories spanning multiple batches.

    Creates memories across batch boundaries and verifies that only
    within-batch pairs are compared (cross-batch pairs are not linked).
    """
    from unittest.mock import patch

    from sova.knowledge.graph import _DISCOVER_BATCH_SIZE, discover_edges, get_edges
    from sova.knowledge.memory import store

    batch1_ids = []
    for i in range(_DISCOVER_BATCH_SIZE):
        # All batch-1 memories are very similar to each other
        emb = [1.0 - i * 0.0001, i * 0.0001, 0.0]
        m = await store(category="batch_test", title=f"B1_{i}", content=f"B1_{i}", tags=[], embedding=emb)
        batch1_ids.append(m.id)

    # Add a few memories in a second batch that are dissimilar to batch 1
    batch2_ids = []
    for i in range(5):
        emb = [0.0, 1.0 - i * 0.001, i * 0.001]
        m = await store(category="batch_test", title=f"B2_{i}", content=f"B2_{i}", tags=[], embedding=emb)
        batch2_ids.append(m.id)

    with patch("sova.knowledge.graph.is_available", return_value=True):
        total = await discover_edges(category="batch_test")

    # Edges should be created (within-batch similar pairs exist)
    assert total > 0

    # Cross-batch pairs should NOT be linked (dissimilar embeddings)
    for b2_id in batch2_ids:
        edges = await get_edges(b2_id)
        linked_ids = {e.source_id if e.target_id == b2_id else e.target_id for e in edges}
        assert not linked_ids.intersection(batch1_ids), "Cross-batch edge found between dissimilar memories"


# ---------------------------------------------------------------------------
# memory.py -- search with expand
# ---------------------------------------------------------------------------


async def test_search_expand_includes_neighbors() -> None:
    """search(expand=True) includes graph neighbors in results."""
    from sova.knowledge.graph import create_edge
    from sova.knowledge.memory import search, store

    m1 = await store(category="learning", title="Direct hit", content="Searchable", tags=["test"], embedding=None)
    m2 = await store(category="learning", title="Neighbor", content="Related context", tags=["other"], embedding=None)
    await create_edge(m1.id, m2.id)

    results = await search(query="Searchable", expand=True)
    result_ids = {m.id for m in results}
    assert m1.id in result_ids
    assert m2.id in result_ids


async def test_search_expand_false_default() -> None:
    """search(expand=False) does not include neighbors (backward compat)."""
    from sova.knowledge.graph import create_edge
    from sova.knowledge.memory import search, store

    m1 = await store(category="learning", title="Direct hit", content="Searchable", tags=["test"], embedding=None)
    m2 = await store(category="learning", title="Neighbor", content="Unrelated title", tags=["other"], embedding=None)
    await create_edge(m1.id, m2.id)

    results = await search(query="Searchable")
    result_ids = {m.id for m in results}
    assert m1.id in result_ids
    assert m2.id not in result_ids


# ---------------------------------------------------------------------------
# Coverage: graph.py -- edge cases
# ---------------------------------------------------------------------------


async def test_get_neighbors_isolated_node() -> None:
    """get_neighbors() returns empty list for a node with no edges."""
    from sova.knowledge.graph import get_neighbors
    from sova.knowledge.memory import store

    m = await store(category="learning", title="Isolated", content="No edges", tags=[], embedding=None)
    assert await get_neighbors(m.id, depth=1) == []


async def test_auto_link_memory_not_found() -> None:
    """auto_link() returns empty list when memory ID does not exist."""
    from unittest.mock import patch

    from sova.knowledge.graph import auto_link

    with patch("sova.knowledge.graph.is_available", return_value=True):
        assert await auto_link(99999) == []


async def test_auto_link_memory_no_embedding() -> None:
    """auto_link() returns empty list when the memory has no embedding."""
    from unittest.mock import patch

    from sova.knowledge.graph import auto_link
    from sova.knowledge.memory import store

    m = await store(category="learning", title="No emb", content="C", tags=[], embedding=None)
    with patch("sova.knowledge.graph.is_available", return_value=True):
        assert await auto_link(m.id) == []


async def test_discover_edges_no_memories() -> None:
    """discover_edges() returns 0 when no memories with embeddings exist."""
    from unittest.mock import patch

    from sova.knowledge.graph import discover_edges

    with patch("sova.knowledge.graph.is_available", return_value=True):
        assert await discover_edges(category="nonexistent") == 0


async def test_discover_edges_below_threshold() -> None:
    """discover_edges() skips pairs below similarity threshold."""
    from unittest.mock import patch

    from sova.knowledge.graph import discover_edges, get_edges
    from sova.knowledge.memory import store

    # Very different embeddings -- should not link
    m1 = await store(category="learning", title="A", content="A", tags=[], embedding=[1.0, 0.0, 0.0])
    await store(category="learning", title="B", content="B", tags=[], embedding=[0.0, 1.0, 0.0])

    with patch("sova.knowledge.graph.is_available", return_value=True):
        count = await discover_edges(category="learning")

    assert count == 0
    assert await get_edges(m1.id) == []


# ---------------------------------------------------------------------------
# Coverage: memory.py -- update invalid field
# ---------------------------------------------------------------------------


async def test_update_invalid_field_raises() -> None:
    """update() raises ValueError for non-mutable fields."""
    from sova.knowledge.memory import store, update

    mem = await store(category="learning", title="T", content="C", tags=[])
    with pytest.raises(ValueError, match="Cannot update field"):
        await update(mem.id, id=999)


# ---------------------------------------------------------------------------
# Coverage: memory.py -- semantic_search and find_similar
# ---------------------------------------------------------------------------


async def test_semantic_search_empty_query() -> None:
    """semantic_search() falls back to search() for empty query."""
    from sova.knowledge.memory import semantic_search, store

    await store(category="learning", title="A", content="Something", tags=[])
    results = await semantic_search(query="", category="learning")
    assert len(results) == 1
    assert results[0][1] == 0.0  # score is 0.0 for fallback


async def test_semantic_search_no_embedding_fallback() -> None:
    """semantic_search() falls back to lexical search when embedding is unavailable."""
    from unittest.mock import patch

    from sova.knowledge.memory import semantic_search, store

    await store(category="learning", title="Bash tips", content="Quote vars", tags=[])
    with patch("sova.knowledge.memory.embed_text", return_value=None):
        results = await semantic_search(query="Bash")
    assert len(results) == 1
    assert results[0][0].title == "Bash tips"
    assert results[0][1] == 0.0


async def test_semantic_search_with_embeddings() -> None:
    """semantic_search() scores by cosine similarity when embeddings available."""
    from sova.knowledge.memory import semantic_search, store

    emb_a = [1.0, 0.0, 0.0]
    emb_b = [0.0, 1.0, 0.0]
    await store(category="learning", title="Close", content="C", tags=[], embedding=emb_a)
    await store(category="learning", title="Far", content="F", tags=[], embedding=emb_b)

    query_emb = [0.95, 0.05, 0.0]
    results = await semantic_search(query="test", query_embedding=query_emb, category="learning")
    assert len(results) == 2
    assert results[0][0].title == "Close"
    assert results[0][1] > results[1][1]


async def test_semantic_search_with_expand() -> None:
    """semantic_search(expand=True) includes graph neighbors."""
    from sova.knowledge.graph import create_edge
    from sova.knowledge.memory import semantic_search, store

    emb = [1.0, 0.0, 0.0]
    m1 = await store(category="learning", title="Hit", content="C", tags=[], embedding=emb)
    m2 = await store(category="learning", title="Neighbor", content="N", tags=[], embedding=None)
    await create_edge(m1.id, m2.id)

    results = await semantic_search(query="test", query_embedding=[0.99, 0.01, 0.0], expand=True)
    result_ids = {m.id for m, _ in results}
    assert m1.id in result_ids
    assert m2.id in result_ids


async def test_semantic_search_filters_by_tier() -> None:
    """semantic_search() filters results by tier."""
    from sova.knowledge.memory import semantic_search, store

    emb = [1.0, 0.0, 0.0]
    await store(category="learning", title="Project", content="P", tags=[], tier="project", embedding=emb)
    await store(category="learning", title="Shared", content="S", tags=[], tier="shared", embedding=emb)

    results = await semantic_search(query="test", query_embedding=[0.99, 0.01, 0.0], tier="project")
    assert len(results) == 1
    assert results[0][0].title == "Project"


async def test_semantic_search_threshold() -> None:
    """semantic_search() filters by threshold."""
    from sova.knowledge.memory import semantic_search, store

    await store(category="learning", title="A", content="C", tags=[], embedding=[1.0, 0.0, 0.0])
    await store(category="learning", title="B", content="C", tags=[], embedding=[0.0, 1.0, 0.0])

    results = await semantic_search(query="test", query_embedding=[0.99, 0.01, 0.0], threshold=0.9)
    assert len(results) == 1
    assert results[0][0].title == "A"


async def test_find_similar() -> None:
    """find_similar() delegates to semantic_search with defaults."""
    from sova.knowledge.memory import find_similar, store

    emb = [1.0, 0.0, 0.0]
    await store(category="learning", title="Match", content="C", tags=[], embedding=emb)

    results = await find_similar("test", category="learning", query_embedding=[0.99, 0.01, 0.0])
    assert len(results) == 1
    assert results[0][0].title == "Match"


async def test_find_similar_default_threshold() -> None:
    """find_similar() uses SIMILARITY_THRESHOLD when threshold not given."""
    from sova.knowledge.memory import find_similar, store

    await store(category="learning", title="A", content="C", tags=[], embedding=[1.0, 0.0, 0.0])
    await store(category="learning", title="B", content="C", tags=[], embedding=[0.0, 1.0, 0.0])

    # With default threshold, very dissimilar memories should be filtered
    results = await find_similar("test", category="learning", query_embedding=[0.99, 0.01, 0.0])
    assert all(score >= 0.5 for _, score in results)
