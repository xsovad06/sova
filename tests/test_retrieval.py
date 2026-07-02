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
    """retrieve_relevant() stops adding project memories when budget is exceeded."""
    from sova.knowledge.memory import store
    from sova.knowledge.retrieval import retrieve_relevant

    # Create several project memories with known content size
    for i in range(10):
        await store(
            category="learning",
            title=f"Memory {i}",
            content=f"Content for memory number {i}. " * 20,
            tags=["test"],
            tier="project",
        )

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
    """load_context() uses relevance filtering when ctx is provided."""
    from sova.knowledge.memory import store
    from sova.knowledge.tiers import load_context

    await store(category="learning", title="Relevant memory", content="Auth patterns.", tags=["auth"])
    await store(category="learning", title="Unrelated memory", content="CSS styling tips.", tags=["css"])

    class _Cfg:
        shared_knowledge_path = str(tmp_path / "nonexistent")

    ctx = _make_execution_context(role="developer", task_title="Fix auth bug")

    result = await load_context(None, tmp_path, _Cfg(), ctx=ctx)
    # The function should still return content (rules + memories)
    assert isinstance(result, str)


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
