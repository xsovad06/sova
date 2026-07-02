"""Tests for memory lifecycle management (health scoring, consolidation, decay, archival)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh in-memory DB for each test."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


async def _backdate_memory(memory_id: int, created_at: datetime) -> None:
    """Test helper: set a memory's created_at to a past date."""
    from sqlalchemy import select

    from sova.db.models import Memory

    async with await get_session() as session:
        async with session.begin():
            row = await session.execute(select(Memory).where(Memory.id == memory_id))
            m = row.scalar_one()
            m.created_at = created_at


# ---------------------------------------------------------------------------
# similarity.py
# ---------------------------------------------------------------------------


def test_titles_match_exact() -> None:
    from sova.knowledge.similarity import titles_match

    assert titles_match("Always quote bash variables", "Always quote bash variables")


def test_titles_match_case_insensitive() -> None:
    from sova.knowledge.similarity import titles_match

    assert titles_match("Always Quote Bash Variables", "always quote bash variables")


def test_titles_match_substring() -> None:
    from sova.knowledge.similarity import titles_match

    assert titles_match("Always quote bash variables in scripts", "Always quote bash variables")


def test_titles_match_short_no_match() -> None:
    from sova.knowledge.similarity import titles_match

    assert not titles_match("short", "different short")


def test_titles_match_different() -> None:
    from sova.knowledge.similarity import titles_match

    assert not titles_match("Use type hints everywhere", "Always quote bash variables")


def test_parse_confirmation_counter() -> None:
    from sova.knowledge.similarity import parse_confirmation_counter

    assert parse_confirmation_counter("Some content\n\n[confirmed: 5]") == 5
    assert parse_confirmation_counter("No counter here") == 0
    assert parse_confirmation_counter("[confirmed: 0]") == 0


def test_set_confirmation_counter_new() -> None:
    from sova.knowledge.similarity import set_confirmation_counter

    result = set_confirmation_counter("Some content", 3)
    assert result == "Some content\n\n[confirmed: 3]"


def test_set_confirmation_counter_replace() -> None:
    from sova.knowledge.similarity import set_confirmation_counter

    result = set_confirmation_counter("Content\n\n[confirmed: 2]", 5)
    assert result == "Content\n\n[confirmed: 5]"


# ---------------------------------------------------------------------------
# lifecycle.py -- _compute_score
# ---------------------------------------------------------------------------


def test_compute_score_new_memory() -> None:
    from sova.knowledge.lifecycle import _compute_score

    score = _compute_score(confirmation_count=0, days_since_update=0.0, retrieval_count=0)
    # Brand new: confirmation=0, recency=1.0 (just created), retrieval=0
    # Expected: 0.4*0 + 0.35*1.0 + 0.25*0 = 0.35
    assert abs(score - 0.35) < 0.01


def test_compute_score_high_confirmation() -> None:
    from sova.knowledge.lifecycle import _compute_score

    score = _compute_score(confirmation_count=10, days_since_update=0.0, retrieval_count=0)
    # High confirmation, fresh, no retrievals
    assert score > 0.35  # higher than new memory


def test_compute_score_old_memory() -> None:
    from sova.knowledge.lifecycle import _compute_score

    score = _compute_score(confirmation_count=0, days_since_update=365.0, retrieval_count=0)
    # Very old, no confirmations, no retrievals -- low score
    assert score < 0.1


def test_compute_score_well_used() -> None:
    from sova.knowledge.lifecycle import _compute_score

    score = _compute_score(confirmation_count=5, days_since_update=10.0, retrieval_count=15)
    # Well-confirmed, recent, frequently retrieved -- high score
    assert score > 0.5


# ---------------------------------------------------------------------------
# lifecycle.py -- compute_health_scores
# ---------------------------------------------------------------------------


async def test_compute_health_scores_empty_db() -> None:
    from sova.knowledge.lifecycle import compute_health_scores

    result = await compute_health_scores()
    assert result.updated == 0
    assert result.total == 0


async def test_compute_health_scores_updates_memories() -> None:
    from sova.knowledge.lifecycle import compute_health_scores
    from sova.knowledge.memory import store

    await store(category="learning", title="Test memory", content="Content", tags=["test"])

    result = await compute_health_scores()
    assert result.updated == 1
    assert result.total == 1

    # Verify score was stored
    from sqlalchemy import select

    from sova.db.models import Memory
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            rows = await session.execute(select(Memory))
            mem = rows.scalar_one()
            assert mem.health_score is not None
            assert float(mem.health_score) > 0


# ---------------------------------------------------------------------------
# lifecycle.py -- flag_stale_memories
# ---------------------------------------------------------------------------


async def test_flag_stale_empty_db() -> None:
    from sova.knowledge.lifecycle import flag_stale_memories

    stale = await flag_stale_memories(staleness_days=90)
    assert stale == []


async def test_flag_stale_new_memory_not_flagged() -> None:
    from sova.knowledge.lifecycle import flag_stale_memories
    from sova.knowledge.memory import store

    await store(category="learning", title="Fresh memory", content="Content", tags=["test"])
    stale = await flag_stale_memories(staleness_days=90)
    assert stale == []


async def test_flag_stale_old_unretrieved_memory() -> None:
    from sova.knowledge.lifecycle import flag_stale_memories
    from sova.knowledge.memory import store

    mem = await store(category="learning", title="Old memory", content="Content", tags=["test"])

    await _backdate_memory(mem.id, datetime.now(timezone.utc) - timedelta(days=90))

    stale = await flag_stale_memories(staleness_days=90)
    assert mem.id in stale


async def test_flag_stale_high_confirmation_exempt() -> None:
    from sova.knowledge.lifecycle import flag_stale_memories
    from sova.knowledge.memory import store

    mem = await store(
        category="learning",
        title="Well-confirmed memory",
        content="Content\n\n[confirmed: 7]",
        tags=["test"],
    )

    await _backdate_memory(mem.id, datetime.now(timezone.utc) - timedelta(days=90))

    stale = await flag_stale_memories(staleness_days=90)
    assert mem.id not in stale


# ---------------------------------------------------------------------------
# lifecycle.py -- archive_memories / auto_archive
# ---------------------------------------------------------------------------


async def test_archive_memories() -> None:
    from sova.knowledge.lifecycle import archive_memories
    from sova.knowledge.memory import store

    mem = await store(category="learning", title="To archive", content="Content", tags=["test"])
    count = await archive_memories([mem.id])
    assert count == 1

    # Verify archived
    from sqlalchemy import select

    from sova.db.models import Memory
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            row = await session.execute(select(Memory).where(Memory.id == mem.id))
            m = row.scalar_one()
            assert m.archived is True


async def test_archive_empty_list() -> None:
    from sova.knowledge.lifecycle import archive_memories

    count = await archive_memories([])
    assert count == 0


async def test_auto_archive_old_unconfirmed() -> None:
    from sova.knowledge.lifecycle import auto_archive
    from sova.knowledge.memory import store

    mem = await store(category="learning", title="Old unconfirmed", content="Content\n\n[confirmed: 0]", tags=["test"])

    await _backdate_memory(mem.id, datetime.now(timezone.utc) - timedelta(days=45))

    count = await auto_archive(archive_days=30)
    assert count == 1


async def test_auto_archive_skips_confirmed() -> None:
    from sova.knowledge.lifecycle import auto_archive
    from sova.knowledge.memory import store

    mem = await store(category="learning", title="Old confirmed", content="Content\n\n[confirmed: 3]", tags=["test"])

    await _backdate_memory(mem.id, datetime.now(timezone.utc) - timedelta(days=45))

    count = await auto_archive(archive_days=30)
    assert count == 0


# ---------------------------------------------------------------------------
# lifecycle.py -- search excludes archived
# ---------------------------------------------------------------------------


async def test_search_excludes_archived_by_default() -> None:
    from sova.knowledge.memory import search, store

    mem = await store(category="learning", title="Archived mem", content="Content", tags=["test"])

    # Archive it
    from sova.knowledge.lifecycle import archive_memories

    await archive_memories([mem.id])

    results = await search(query="Archived")
    assert len(results) == 0

    # With include_archived=True
    results = await search(query="Archived", include_archived=True)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# lifecycle.py -- find_consolidation_candidates
# ---------------------------------------------------------------------------


async def test_find_consolidation_no_candidates() -> None:
    from sova.knowledge.lifecycle import find_consolidation_candidates
    from sova.knowledge.memory import store

    await store(category="learning", title="Unique memory one", content="A", tags=["test"])
    await store(category="learning", title="Totally different topic", content="B", tags=["test"])

    clusters = await find_consolidation_candidates()
    assert clusters == []


async def test_find_consolidation_with_cluster() -> None:
    from sova.knowledge.lifecycle import find_consolidation_candidates
    from sova.knowledge.memory import store

    # All 3 titles contain the same 28-char core substring
    await store(category="learning", title="Always quote bash variables", content="A", tags=["bash"])
    await store(category="learning", title="Always quote bash variables (important)", content="B", tags=["bash"])
    await store(category="learning", title="Always quote bash variables in scripts", content="C", tags=["bash"])

    clusters = await find_consolidation_candidates()
    assert len(clusters) == 1
    assert len(clusters[0].member_ids) == 3


async def test_find_consolidation_two_members_not_clustered() -> None:
    from sova.knowledge.lifecycle import find_consolidation_candidates
    from sova.knowledge.memory import store

    # Only 2 similar -- below MIN_CLUSTER_SIZE=3
    await store(category="learning", title="Always quote bash variables", content="A", tags=["bash"])
    await store(category="learning", title="Always quote bash variables (important)", content="B", tags=["bash"])

    clusters = await find_consolidation_candidates()
    assert clusters == []


# ---------------------------------------------------------------------------
# lifecycle.py -- consolidate_cluster
# ---------------------------------------------------------------------------


async def test_consolidate_cluster_success() -> None:
    from sova.knowledge.lifecycle import ConsolidationCluster, consolidate_cluster
    from sova.knowledge.memory import get, store

    m1 = await store(
        category="learning",
        title="Quote bash vars A",
        content="Always quote\n\n[confirmed: 2]",
        tags=["bash"],
    )
    m2 = await store(
        category="learning",
        title="Quote bash vars B",
        content="Must quote\n\n[confirmed: 3]",
        tags=["shell"],
    )
    m3 = await store(
        category="learning",
        title="Quote bash vars C",
        content="Quote them\n\n[confirmed: 1]",
        tags=["bash"],
    )

    cluster = ConsolidationCluster(
        representative_id=m1.id,
        member_ids=[m1.id, m2.id, m3.id],
        titles=[m1.title, m2.title, m3.title],
    )

    mock_result = AsyncMock()
    mock_result.text = (
        '{"title": "Always quote bash variables", "content": "Quote all bash variables to prevent word splitting."}'
    )
    mock_result.cost_usd = 0.001

    with patch("sova.llm.client.invoke", return_value=mock_result):
        new_id = await consolidate_cluster(cluster, cwd="/tmp")

    assert new_id is not None

    # Check new memory has summed confirmations
    new_mem = await get(new_id)
    assert new_mem is not None
    assert "[confirmed: 6]" in new_mem.content  # 2+3+1

    # Check old memories are superseded
    old_m1 = await get(m1.id)
    assert old_m1 is not None
    assert old_m1.superseded_by == new_id


async def test_consolidate_cluster_llm_failure() -> None:
    from sova.knowledge.lifecycle import ConsolidationCluster, consolidate_cluster
    from sova.knowledge.memory import store

    m1 = await store(category="learning", title="Quote vars A", content="A", tags=[])
    m2 = await store(category="learning", title="Quote vars B", content="B", tags=[])
    m3 = await store(category="learning", title="Quote vars C", content="C", tags=[])

    cluster = ConsolidationCluster(
        representative_id=m1.id,
        member_ids=[m1.id, m2.id, m3.id],
        titles=[m1.title, m2.title, m3.title],
    )

    with patch("sova.llm.client.invoke", side_effect=RuntimeError("LLM unavailable")):
        new_id = await consolidate_cluster(cluster, cwd="/tmp")

    assert new_id is None


# ---------------------------------------------------------------------------
# lifecycle.py -- auto_cleanup
# ---------------------------------------------------------------------------


async def test_auto_cleanup_empty_db() -> None:
    from sova.knowledge.lifecycle import auto_cleanup

    result = await auto_cleanup()
    assert result.stale_flagged == 0
    assert result.archived == 0
    assert result.consolidated == 0


# ---------------------------------------------------------------------------
# memory.py -- increment_retrieval
# ---------------------------------------------------------------------------


async def test_increment_retrieval() -> None:
    from sova.knowledge.memory import increment_retrieval, store

    mem = await store(category="learning", title="Test", content="Content", tags=["test"])

    await increment_retrieval([mem.id])

    from sqlalchemy import select

    from sova.db.models import Memory
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            row = await session.execute(select(Memory).where(Memory.id == mem.id))
            m = row.scalar_one()
            assert m.retrieval_count == 1
            assert m.last_retrieved_at is not None

    # Increment again
    await increment_retrieval([mem.id])

    async with await get_session() as session:
        async with session.begin():
            row = await session.execute(select(Memory).where(Memory.id == mem.id))
            m = row.scalar_one()
            assert m.retrieval_count == 2


async def test_increment_retrieval_empty_list() -> None:
    from sova.knowledge.memory import increment_retrieval

    # Should not raise
    await increment_retrieval([])


# ---------------------------------------------------------------------------
# CLI commands -- memory health, consolidate, archive
# ---------------------------------------------------------------------------


async def test_cli_health_empty() -> None:
    from sova.cli.commands.memory import _health

    with patch("sova.db.session.init_db", new_callable=AsyncMock):
        await _health(project_dir=None)


async def test_cli_health_with_memories() -> None:
    from sova.cli.commands.memory import _health
    from sova.knowledge.memory import store

    await store(category="learning", title="Test", content="Content", tags=["test"])
    with patch("sova.db.session.init_db", new_callable=AsyncMock):
        await _health(project_dir=None)


async def test_cli_consolidate_dry_run_empty() -> None:
    from sova.cli.commands.memory import _consolidate

    with patch("sova.db.session.init_db", new_callable=AsyncMock):
        await _consolidate(project_dir=None, dry_run=True)


async def test_cli_consolidate_dry_run_with_candidates() -> None:
    from sova.cli.commands.memory import _consolidate
    from sova.knowledge.memory import store

    for i in range(4):
        await store(category="learning", title=f"Quote bash variables {i}", content=f"Content {i}", tags=[])

    with patch("sova.db.session.init_db", new_callable=AsyncMock):
        await _consolidate(project_dir=None, dry_run=True)


async def test_cli_consolidate_runs_merge() -> None:
    from sova.cli.commands.memory import _consolidate
    from sova.knowledge.memory import store

    for i in range(4):
        await store(category="learning", title=f"Quote bash variables {i}", content=f"Content {i}", tags=[])

    llm_result = AsyncMock()
    llm_result.text = '{"title": "Merged", "content": "Merged content"}'
    with (
        patch("sova.llm.client.invoke", return_value=llm_result),
        patch("sova.db.session.init_db", new_callable=AsyncMock),
    ):
        await _consolidate(project_dir=None, dry_run=False)


async def test_cli_archive_dry_run() -> None:
    from sova.cli.commands.memory import _archive
    from sova.knowledge.memory import store

    m = await store(category="learning", title="Old", content="Content", tags=[])
    await _backdate_memory(m.id, datetime.now(timezone.utc) - timedelta(days=100))

    with patch("sova.db.session.init_db", new_callable=AsyncMock):
        await _archive(project_dir=None, archive_days=30, dry_run=True)


async def test_cli_archive_runs() -> None:
    from sova.cli.commands.memory import _archive
    from sova.knowledge.memory import store

    m = await store(category="learning", title="Old", content="Content", tags=[])
    await _backdate_memory(m.id, datetime.now(timezone.utc) - timedelta(days=100))

    with patch("sova.db.session.init_db", new_callable=AsyncMock):
        await _archive(project_dir=None, archive_days=30, dry_run=False)


# ---------------------------------------------------------------------------
# memory.py -- semantic_search include_archived
# ---------------------------------------------------------------------------


async def test_semantic_search_excludes_archived_by_default() -> None:
    from sova.knowledge.memory import store

    m = await store(category="learning", title="Archived mem", content="Content", tags=[])

    # Archive the memory
    from sova.knowledge.lifecycle import archive_memories

    await archive_memories([m.id])

    from sova.knowledge.memory import semantic_search

    results = await semantic_search(query="Archived mem")
    assert all(mem.id != m.id for mem, _ in results)


async def test_semantic_search_includes_archived_with_flag() -> None:
    from sova.knowledge.memory import store

    m = await store(category="learning", title="Archived mem", content="Content", tags=[])

    from sova.knowledge.lifecycle import archive_memories

    await archive_memories([m.id])

    from sova.knowledge.memory import semantic_search

    results = await semantic_search(query="Archived mem", include_archived=True)
    found_ids = {mem.id for mem, _ in results}
    assert m.id in found_ids
