"""Tests for semantic memory search via embeddings."""

from __future__ import annotations

import os
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


# Deterministic fake vectors for test isolation (no model download in CI)
_VEC_A = [1.0, 0.0, 0.0]
_VEC_B = [0.0, 1.0, 0.0]
_VEC_C = [0.9, 0.1, 0.0]  # similar to A


# ---------------------------------------------------------------------------
# embeddings.py -- cosine_similarity
# ---------------------------------------------------------------------------


def test_cosine_identical_vectors() -> None:
    from sova.knowledge.embeddings import cosine_similarity

    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors() -> None:
    from sova.knowledge.embeddings import cosine_similarity

    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_vectors() -> None:
    from sova.knowledge.embeddings import cosine_similarity

    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_zero_vector() -> None:
    from sova.knowledge.embeddings import cosine_similarity

    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)


def test_cosine_similar_vectors() -> None:
    from sova.knowledge.embeddings import cosine_similarity

    score = cosine_similarity(_VEC_A, _VEC_C)
    assert score > 0.9


# ---------------------------------------------------------------------------
# embeddings.py -- embed_text fallback when no model
# ---------------------------------------------------------------------------


def test_embed_text_returns_none_without_model() -> None:
    from sova.knowledge.embeddings import embed_text

    with patch("sova.knowledge.embeddings._load_model", return_value=None):
        assert embed_text("hello") is None


# ---------------------------------------------------------------------------
# embeddings.py -- is_available
# ---------------------------------------------------------------------------


def test_is_available_false_without_model() -> None:
    from sova.knowledge.embeddings import is_available

    with patch("sova.knowledge.embeddings._load_model", return_value=None):
        assert is_available() is False


# ---------------------------------------------------------------------------
# memory.py -- store() saves embedding
# ---------------------------------------------------------------------------


async def test_store_saves_embedding() -> None:
    from sova.knowledge.memory import store

    with patch("sova.knowledge.memory.embed_text", return_value=_VEC_A):
        mem = await store(category="learning", title="Test", content="Content", tags=[])

    assert mem.embedding == _VEC_A


async def test_store_works_without_embedding() -> None:
    from sova.knowledge.memory import store

    with patch("sova.knowledge.memory.embed_text", return_value=None):
        mem = await store(category="learning", title="Test", content="Content", tags=[])

    assert mem.embedding is None
    assert mem.id is not None


# ---------------------------------------------------------------------------
# memory.py -- semantic_search
# ---------------------------------------------------------------------------


async def test_semantic_search_returns_scored_results() -> None:
    from sova.knowledge.memory import semantic_search, store

    with patch("sova.knowledge.memory.embed_text", side_effect=[_VEC_A, _VEC_B, _VEC_C]):
        await store(category="learning", title="Bash quoting", content="Quote vars.", tags=[])
        await store(category="learning", title="Python imports", content="Check stale.", tags=[])

    with patch("sova.knowledge.memory.embed_text", return_value=_VEC_C):
        results = await semantic_search(query="bash variables", limit=10)

    assert len(results) == 2
    # First result should be the one with VEC_A (most similar to VEC_C)
    mem, score = results[0]
    assert mem.title == "Bash quoting"
    assert score > 0.9


async def test_semantic_search_skips_memories_without_embeddings() -> None:
    from sova.knowledge.memory import semantic_search, store

    # One with embedding, one without
    with patch("sova.knowledge.memory.embed_text", side_effect=[_VEC_A, None]):
        await store(category="learning", title="Has embedding", content="Yes.", tags=[])
        await store(category="learning", title="No embedding", content="Nope.", tags=[])

    with patch("sova.knowledge.memory.embed_text", return_value=_VEC_A):
        results = await semantic_search(query="test")

    assert len(results) == 1
    assert results[0][0].title == "Has embedding"


async def test_semantic_search_empty_query_falls_back() -> None:
    from sova.knowledge.memory import semantic_search, store

    with patch("sova.knowledge.memory.embed_text", return_value=_VEC_A):
        await store(category="learning", title="Test", content="Content", tags=[])

    results = await semantic_search(query="", limit=10)
    assert len(results) == 1
    # Score should be 0.0 for fallback
    assert results[0][1] == 0.0


async def test_semantic_search_falls_back_when_unavailable() -> None:
    from sova.knowledge.memory import semantic_search, store

    with patch("sova.knowledge.memory.embed_text", return_value=None):
        await store(category="learning", title="Bash tips", content="Quote.", tags=["bash"])

    with patch("sova.knowledge.memory.embed_text", return_value=None):
        results = await semantic_search(query="bash")

    assert len(results) == 1
    assert results[0][0].title == "Bash tips"
    assert results[0][1] == 0.0


async def test_semantic_search_filters_by_category() -> None:
    from sova.knowledge.memory import semantic_search, store

    with patch("sova.knowledge.memory.embed_text", side_effect=[_VEC_A, _VEC_A]):
        await store(category="learning", title="Learning", content="L.", tags=[])
        await store(category="review", title="Review", content="R.", tags=[])

    with patch("sova.knowledge.memory.embed_text", return_value=_VEC_A):
        results = await semantic_search(query="test", category="learning")

    assert len(results) == 1
    assert results[0][0].category == "learning"


# ---------------------------------------------------------------------------
# memory.py -- find_similar (dedup helper)
# ---------------------------------------------------------------------------


async def test_find_similar_returns_matches_above_threshold() -> None:
    from sova.knowledge.memory import find_similar, store

    with patch("sova.knowledge.memory.embed_text", return_value=_VEC_A):
        await store(category="learning", title="Pattern A", content="Do X.", tags=[])

    with patch("sova.knowledge.memory.embed_text", return_value=_VEC_C):
        results = await find_similar("Pattern A is about doing X")

    assert len(results) >= 1
    assert results[0][1] > 0.85


async def test_find_similar_no_match_below_threshold() -> None:
    from sova.knowledge.memory import find_similar, store

    with patch("sova.knowledge.memory.embed_text", return_value=_VEC_A):
        await store(category="learning", title="Pattern A", content="Do X.", tags=[])

    with patch("sova.knowledge.memory.embed_text", return_value=_VEC_B):
        results = await find_similar("Completely different topic")

    assert len(results) == 0


# ---------------------------------------------------------------------------
# extraction.py -- embedding-based dedup
# ---------------------------------------------------------------------------


async def test_dedup_uses_embeddings_when_available() -> None:
    from sova.knowledge.extraction import ExtractedMemory, _deduplicate_and_store
    from sova.knowledge.memory import search

    with patch("sova.knowledge.memory.embed_text", return_value=_VEC_A):
        from sova.knowledge.memory import store

        await store(
            category="learning",
            title="Always quote bash variables",
            content="Unquoted vars split on whitespace.\n\n[confirmed: 0]",
            tags=["bash"],
        )

    mem = ExtractedMemory(
        category="learning",
        title="Quote variables in bash scripts",
        content="Variables without quotes cause word splitting.",
        tags=["bash"],
    )

    # find_similar returns the existing memory as a match
    with patch("sova.knowledge.memory.find_similar") as mock_find:
        existing = (await search(category="learning"))[0]
        mock_find.return_value = [(existing, 0.92)]
        result = await _deduplicate_and_store(mem, repo="test/repo", issue_number="42")

    assert result == "confirmed"


async def test_dedup_falls_back_to_title_match() -> None:
    from sova.knowledge.extraction import ExtractedMemory, _deduplicate_and_store

    with patch("sova.knowledge.memory.embed_text", return_value=None):
        from sova.knowledge.memory import store

        await store(
            category="learning",
            title="Always quote bash variables in scripts",
            content="Unquoted vars bad.\n\n[confirmed: 0]",
            tags=["bash"],
        )

    mem = ExtractedMemory(
        category="learning",
        title="Always quote bash variables in scripts",
        content="Quoting prevents splitting.",
        tags=["bash"],
    )

    with patch("sova.knowledge.memory.find_similar", return_value=[]):
        result = await _deduplicate_and_store(mem, repo="test/repo", issue_number="42")

    assert result == "confirmed"


# ---------------------------------------------------------------------------
# dashboard -- semantic_list_memories
# ---------------------------------------------------------------------------


async def test_semantic_list_memories_returns_scores() -> None:
    from sova.dashboard.services.memory_service import semantic_list_memories
    from sova.knowledge.memory import store

    with patch("sova.knowledge.memory.embed_text", return_value=_VEC_A):
        await store(category="learning", title="Test memory", content="Content here.", tags=["test"])

    with patch("sova.knowledge.memory.embed_text", return_value=_VEC_A):
        entries, total = await semantic_list_memories(query="test")

    assert total == 1
    assert entries[0]["similarity"] > 0.0
    assert entries[0]["title"] == "Test memory"
