"""Tests for sova.knowledge.retrieval -- query-aware, token-budgeted retrieval."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

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
# build_context_query
# ---------------------------------------------------------------------------


def test_build_context_query_with_task() -> None:
    """build_context_query() uses task title+body and role."""
    from sova.knowledge.retrieval import build_context_query

    task = _make_task(title="Fix login bug", body="Users cannot log in after password reset")
    query = build_context_query(role="developer", task=task, files_changed=["auth.py"])
    assert "Fix login bug" in query
    assert "password reset" in query
    assert "developer" in query
    assert "auth.py" in query


def test_build_context_query_no_task() -> None:
    """build_context_query() returns role-only query when task is None."""
    from sova.knowledge.retrieval import build_context_query

    query = build_context_query(role="reviewer", task=None, files_changed=[])
    assert query == "reviewer"
    assert len(query) > 0


def test_build_context_query_empty_role() -> None:
    """build_context_query() never returns empty string even with empty role."""
    from sova.knowledge.retrieval import build_context_query

    query = build_context_query(role="", task=None, files_changed=[])
    assert len(query) > 0


def test_build_context_query_with_files_only() -> None:
    """build_context_query() includes file names when no task."""
    from sova.knowledge.retrieval import build_context_query

    query = build_context_query(role="developer", task=None, files_changed=["api.py", "models.py"])
    assert "api.py" in query
    assert "models.py" in query


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty() -> None:
    from sova.knowledge.retrieval import estimate_tokens

    assert estimate_tokens("") == 0


def test_estimate_tokens_words() -> None:
    from sova.knowledge.retrieval import estimate_tokens

    # 4 words * 1.3 = 5.2 -> int(5.2) = 5
    assert estimate_tokens("hello world foo bar") == 5


# ---------------------------------------------------------------------------
# retrieve_relevant
# ---------------------------------------------------------------------------


async def test_retrieve_relevant_returns_shared_first() -> None:
    """Shared-tier memories are always included, even when budget is tight."""
    from sova.knowledge.memory import store
    from sova.knowledge.retrieval import retrieve_relevant

    await store(category="learning", title="Shared insight", content="Important.", tags=["test"], tier="shared")
    await store(category="learning", title="Project detail", content="Less important.", tags=["test"], tier="project")

    results = await retrieve_relevant(query="insight", max_context_tokens=5)
    titles = [m.title for m, _ in results]
    assert "Shared insight" in titles


async def test_retrieve_relevant_empty_query() -> None:
    """retrieve_relevant() with empty query returns all shared + budget-limited project."""
    from sova.knowledge.memory import store
    from sova.knowledge.retrieval import retrieve_relevant

    await store(category="learning", title="S1", content="Shared.", tags=[], tier="shared")
    await store(category="learning", title="P1", content="Project.", tags=[], tier="project")

    results = await retrieve_relevant(query="", max_context_tokens=10000)
    assert len(results) >= 1


async def test_retrieve_relevant_no_memories() -> None:
    """retrieve_relevant() returns empty list when no memories exist."""
    from sova.knowledge.retrieval import retrieve_relevant

    results = await retrieve_relevant(query="anything", max_context_tokens=10000)
    assert results == []


async def test_retrieve_relevant_respects_token_budget() -> None:
    """retrieve_relevant() stops adding project memories when budget is exceeded (semantic path)."""
    from sova.knowledge.memory import store
    from sova.knowledge.retrieval import retrieve_relevant

    # Create several project memories with known content size
    memories = []
    for i in range(10):
        mem = await store(
            category="learning",
            title=f"Memory {i}",
            content=f"Content for memory number {i}. " * 20,
            tags=["test"],
            tier="project",
        )
        memories.append(mem)

    # Mock _search_project_tier to return semantic results (used_semantic=True)
    # so the budget-limiting path is exercised
    async def _mock_search(*, query, category):
        return [(m, 0.9 - i * 0.05) for i, m in enumerate(memories)], True

    with patch("sova.knowledge.retrieval._search_project_tier", side_effect=_mock_search):
        # Tiny budget: should get fewer than all 10
        results = await retrieve_relevant(query="memory", max_context_tokens=50)
        assert len(results) < 10


async def test_retrieve_relevant_shared_exceeds_budget_still_included() -> None:
    """When shared memories alone exceed the budget, they are all included with a warning."""
    from sova.knowledge.memory import store
    from sova.knowledge.retrieval import retrieve_relevant

    # Create shared memories with lots of content
    await store(
        category="learning",
        title="Big shared",
        content="word " * 200,
        tags=[],
        tier="shared",
    )

    results = await retrieve_relevant(query="word", max_context_tokens=10)
    # Shared memory is included even though it exceeds the budget
    assert len(results) >= 1
    assert results[0][0].tier == "shared"


async def test_retrieve_relevant_semantic_fallback_to_lexical() -> None:
    """When semantic search returns empty, retrieve_relevant falls back to lexical search."""
    from sova.knowledge.memory import store
    from sova.knowledge.retrieval import retrieve_relevant

    await store(category="learning", title="Bash quoting", content="Always quote variables.", tags=["bash"])

    # With embeddings unavailable, semantic_search falls back to lexical internally
    with patch("sova.knowledge.retrieval.is_available", return_value=False):
        results = await retrieve_relevant(query="quoting", max_context_tokens=10000)
        titles = [m.title for m, _ in results]
        assert "Bash quoting" in titles


# ---------------------------------------------------------------------------
# format_relevant_context
# ---------------------------------------------------------------------------


def test_format_relevant_context_empty() -> None:
    """format_relevant_context() returns empty string for empty results."""
    from sova.knowledge.retrieval import format_relevant_context

    assert format_relevant_context([]) == ""


async def test_format_relevant_context_with_results() -> None:
    """format_relevant_context() formats memories with scores."""
    from sova.knowledge.memory import store
    from sova.knowledge.retrieval import format_relevant_context

    mem = await store(category="learning", title="Test pattern", content="Do X.", tags=["py"])
    formatted = format_relevant_context([(mem, 0.92)])
    assert "Test pattern" in formatted
    assert "Do X." in formatted


# ---------------------------------------------------------------------------
# load_context with ExecutionContext integration
# ---------------------------------------------------------------------------


async def test_load_context_with_ctx_uses_relevance(tmp_path: Path) -> None:
    """load_context() uses relevance filtering when ctx is provided.

    Exercises the real retrieve_relevant() with a mocked _search_project_tier
    that returns scored results, verifying that budget truncation actually
    drops low-scored memories while keeping high-scored ones.
    """
    from sova.knowledge.memory import store
    from sova.knowledge.tiers import load_context

    mem_relevant = await store(
        category="learning", title="Relevant memory", content="Auth patterns.", tags=["auth"]
    )
    mem_unrelated = await store(
        category="learning", title="Unrelated memory", content="CSS styling tips.", tags=["css"]
    )

    class _KnowledgeCfg:
        max_context_tokens = 5  # Tiny budget: fits one memory, not both

    class _Cfg:
        shared_knowledge_path = str(tmp_path / "nonexistent")
        knowledge = _KnowledgeCfg()

    ctx = _make_execution_context(role="developer", task_title="Fix auth bug")

    # Mock _search_project_tier to return both memories with different scores
    # so retrieve_relevant's budget truncation decides which to keep
    async def _mock_search(*, query, category):
        return [(mem_relevant, 0.95), (mem_unrelated, 0.1)], True

    with patch("sova.knowledge.retrieval._search_project_tier", side_effect=_mock_search):
        result = await load_context(None, tmp_path, _Cfg(), ctx=ctx)
        # Relevance filtering should include the higher-scored auth memory
        assert "Auth patterns" in result
        assert "Relevant memory" in result
        # Lower-scored memory excluded by budget truncation
        assert "CSS styling" not in result


async def test_load_context_without_ctx_unchanged(tmp_path: Path) -> None:
    """load_context() without ctx uses exhaustive path (backward compat)."""
    from sova.knowledge.memory import store
    from sova.knowledge.tiers import load_context

    await store(category="learning", title="L1", content="Learn.", tags=["python"], tier="project")

    class _Cfg:
        shared_knowledge_path = str(tmp_path / "nonexistent")

    result = await load_context(None, tmp_path, _Cfg(), tier="project")
    assert "L1" in result


# ---------------------------------------------------------------------------
# _search_project_tier semantic path
# ---------------------------------------------------------------------------


async def test_search_project_tier_semantic_path() -> None:
    """_search_project_tier returns semantic results with used_semantic=True."""
    from sova.knowledge.memory import store
    from sova.knowledge.retrieval import _search_project_tier

    mem = await store(category="learning", title="Semantic hit", content="Match.", tags=["test"], tier="project")

    async def _mock_semantic(*, query, tier, category, limit, expand):
        return [(mem, 0.85)]

    with (
        patch("sova.knowledge.retrieval.is_available", return_value=True),
        patch("sova.knowledge.retrieval.semantic_search", side_effect=_mock_semantic),
    ):
        results, used_semantic = await _search_project_tier(query="match", category=None)
        assert used_semantic is True
        assert len(results) == 1
        assert results[0][1] == 0.85


async def test_search_project_tier_semantic_empty_falls_through() -> None:
    """When semantic search returns empty, _search_project_tier falls back to lexical."""
    from sova.knowledge.memory import store
    from sova.knowledge.retrieval import _search_project_tier

    await store(category="learning", title="Lexical hit", content="Fallthrough.", tags=["test"], tier="project")

    async def _mock_semantic(*, query, tier, category, limit, expand):
        return []  # Empty semantic results

    with (
        patch("sova.knowledge.retrieval.is_available", return_value=True),
        patch("sova.knowledge.retrieval.semantic_search", side_effect=_mock_semantic),
    ):
        results, used_semantic = await _search_project_tier(query="Fallthrough", category=None)
        assert used_semantic is False
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# retrieve_relevant: shared/project overlap dedup
# ---------------------------------------------------------------------------


async def test_retrieve_relevant_deduplicates_shared_and_project() -> None:
    """retrieve_relevant skips project memories already in shared results."""
    from sova.knowledge.memory import store
    from sova.knowledge.retrieval import retrieve_relevant

    mem = await store(category="learning", title="Overlap", content="Appears in both.", tags=[], tier="shared")

    # Mock _search_project_tier to return the same memory as a project result
    async def _mock_search(*, query, category):
        return [(mem, 0.7)], True  # Semantic path to exercise budget logic

    with patch("sova.knowledge.retrieval._search_project_tier", side_effect=_mock_search):
        results = await retrieve_relevant(query="overlap", max_context_tokens=10000)
        # Memory should appear only once (from shared), not duplicated
        ids = [m.id for m, _ in results]
        assert ids.count(mem.id) == 1


# ---------------------------------------------------------------------------
# retrieve_relevant: exhaustive fallback includes all
# ---------------------------------------------------------------------------


async def test_retrieve_relevant_exhaustive_fallback_includes_all() -> None:
    """When embeddings unavailable (lexical fallback), all project memories are included."""
    from sova.knowledge.memory import store
    from sova.knowledge.retrieval import retrieve_relevant

    for i in range(5):
        await store(
            category="learning",
            title=f"Exhaustive {i}",
            content=f"Content {i}. " * 20,
            tags=["test"],
            tier="project",
        )

    with patch("sova.knowledge.retrieval.is_available", return_value=False):
        results = await retrieve_relevant(query="Exhaustive", max_context_tokens=10)
        # All 5 should be included despite tiny budget (exhaustive fallback)
        assert len(results) == 5


# ---------------------------------------------------------------------------
# load_context: file-based tiers
# ---------------------------------------------------------------------------


async def test_load_context_loads_shared_knowledge(tmp_path: Path) -> None:
    """load_context includes Tier 0 shared knowledge from files."""
    from sova.knowledge.tiers import load_context

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "patterns.md").write_text("Shared pattern content")

    class _Cfg:
        shared_knowledge_path = str(shared_dir)

    result = await load_context(None, tmp_path, _Cfg())
    assert "Shared pattern content" in result
    assert "Shared Knowledge" in result


async def test_load_context_loads_project_rules(tmp_path: Path) -> None:
    """load_context includes Tier 1 project rules from .claude/rules/*.md."""
    from sova.knowledge.tiers import load_context

    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "arch.md").write_text("Architecture rule content")

    class _Cfg:
        shared_knowledge_path = str(tmp_path / "nonexistent")

    result = await load_context(None, tmp_path, _Cfg())
    assert "Architecture rule content" in result
    assert "Project Rules" in result


async def test_load_context_md_read_error(tmp_path: Path) -> None:
    """_load_md_files logs warning on OSError and skips the file."""
    from sova.knowledge.tiers import load_context

    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    md_file = rules_dir / "bad.md"
    md_file.write_text("content")

    class _Cfg:
        shared_knowledge_path = str(tmp_path / "nonexistent")

    # Make the file unreadable by mocking read_text to raise
    with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
        result = await load_context(None, tmp_path, _Cfg())
        # Should not crash, just skip the file
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# load_tier
# ---------------------------------------------------------------------------


async def test_load_tier_returns_memories() -> None:
    """load_tier returns all non-superseded memories for a tier."""
    from sova.knowledge.memory import store
    from sova.knowledge.tiers import load_tier

    await store(category="learning", title="T1", content="Tier content.", tags=[], tier="project")

    result = await load_tier("project")
    assert len(result) >= 1
    assert any(m.title == "T1" for m in result)


# ---------------------------------------------------------------------------
# _load_relevant_memories with config
# ---------------------------------------------------------------------------


async def test_load_relevant_memories_uses_config_budget(tmp_path: Path) -> None:
    """_load_relevant_memories passes max_context_tokens from config."""
    from sova.knowledge.memory import store
    from sova.knowledge.tiers import load_context

    await store(category="learning", title="Cfg test", content="Config budget.", tags=[], tier="project")

    class _KnowledgeCfg:
        max_context_tokens = 500

    class _Cfg:
        shared_knowledge_path = str(tmp_path / "nonexistent")
        knowledge = _KnowledgeCfg()

    ctx = _make_execution_context(role="developer", task_title="Config budget")

    # Patch retrieve_relevant to capture the max_context_tokens arg
    captured = {}

    async def _mock_retrieve(*, query, max_context_tokens=2000, category=None):
        captured["max_context_tokens"] = max_context_tokens
        return []

    with patch("sova.knowledge.retrieval.retrieve_relevant", side_effect=_mock_retrieve):
        await load_context(None, tmp_path, _Cfg(), ctx=ctx)
        assert captured["max_context_tokens"] == 500


# ---------------------------------------------------------------------------
# memory.search with limit
# ---------------------------------------------------------------------------


async def test_search_with_limit() -> None:
    """memory.search() respects the limit parameter."""
    from sova.knowledge.memory import search, store

    for i in range(5):
        await store(category="learning", title=f"Lim {i}", content=f"Content {i}", tags=[], tier="project")

    results = await search(tier="project", limit=2)
    assert len(results) == 2


async def test_search_without_limit() -> None:
    """memory.search() returns all results when limit is None."""
    from sova.knowledge.memory import search, store

    for i in range(5):
        await store(category="learning", title=f"NoLim {i}", content=f"Content {i}", tags=[], tier="project")

    results = await search(tier="project")
    assert len(results) == 5


# ---------------------------------------------------------------------------
# KnowledgeConfig
# ---------------------------------------------------------------------------


def test_knowledge_config_defaults() -> None:
    """KnowledgeConfig has correct defaults."""
    from sova.config.models import KnowledgeConfig

    cfg = KnowledgeConfig()
    assert cfg.max_context_tokens == 2000


def test_knowledge_config_in_project_config() -> None:
    """ProjectConfig includes KnowledgeConfig as a nested section."""
    from sova.config.models import ProjectConfig

    cfg = ProjectConfig()
    assert hasattr(cfg, "knowledge")
    assert cfg.knowledge.max_context_tokens == 2000


# ---------------------------------------------------------------------------
# build_context_query: body truncation
# ---------------------------------------------------------------------------


def test_build_context_query_truncates_body() -> None:
    """build_context_query truncates task body to 200 chars."""
    from sova.knowledge.retrieval import build_context_query

    long_body = "x" * 500
    task = _make_task(title="Title", body=long_body)
    query = build_context_query(role="dev", task=task, files_changed=[])
    # Body part should be at most 200 chars of 'x'
    assert "x" * 200 in query
    assert "x" * 201 not in query


def test_build_context_query_limits_files() -> None:
    """build_context_query includes at most 10 files."""
    from sova.knowledge.retrieval import build_context_query

    files = [f"file{i}.py" for i in range(15)]
    query = build_context_query(role="dev", task=None, files_changed=files)
    assert "file9.py" in query
    assert "file10.py" not in query


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(title: str = "", body: str = ""):
    """Create a minimal Task-like object for testing."""
    from dataclasses import dataclass, field

    @dataclass
    class _Task:
        id: str = "1"
        title: str = ""
        body: str = ""
        state: str = "backlog"
        labels: list[str] = field(default_factory=list)
        assignees: list[str] = field(default_factory=list)
        url: str = ""
        milestone: str = ""
        metadata: dict = field(default_factory=dict)
        issue_type: str = ""
        story_points: float | None = None
        sprint: str = ""

    return _Task(title=title, body=body)


def _make_execution_context(role: str = "developer", task_title: str = ""):
    """Create a minimal ExecutionContext-like object for testing."""

    class _Ctx:
        def __init__(self):
            self.role = role
            self.task = _make_task(title=task_title) if task_title else None
            self.files_changed: list[str] = []

    return _Ctx()
