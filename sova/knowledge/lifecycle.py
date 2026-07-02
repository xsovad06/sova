"""Memory lifecycle management -- health scoring, consolidation, decay, and archival.

Provides automated maintenance of the memory store:
- Health scoring: rank memories by value (confirmation, recency, retrieval).
- Consolidation: merge related memory fragments via LLM.
- Decay: flag unretrieved memories as stale.
- Archival: soft-delete low-value memories from active retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from sova.db.models import Memory
from sova.db.session import get_session
from sova.knowledge.similarity import parse_confirmation_counter, set_confirmation_counter, titles_match
from sova.utils.logging import get_logger

log = get_logger(component="knowledge.lifecycle")


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (SQLite may return naive datetimes)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# Health score weights (3-factor; edge_count deferred until #225)
_W_CONFIRMATION = 0.4
_W_RECENCY = 0.35
_W_RETRIEVAL = 0.25

# Decay / archival defaults
DEFAULT_STALENESS_DAYS = 90
DEFAULT_ARCHIVE_DAYS = 30
HIGH_CONFIRMATION_EXEMPT = 5

# Consolidation
MIN_CLUSTER_SIZE = 3


@dataclass
class HealthScoreResult:
    """Result from compute_health_scores."""

    updated: int = 0
    total: int = 0


@dataclass
class CleanupResult:
    """Summary of an auto_cleanup run."""

    stale_flagged: int = 0
    archived: int = 0
    consolidated: int = 0


@dataclass
class ConsolidationCluster:
    """A group of similar memories to consolidate."""

    representative_id: int
    member_ids: list[int]
    titles: list[str]


def _compute_score(
    *,
    confirmation_count: int,
    days_since_update: float,
    retrieval_count: int,
) -> float:
    """Compute a 0-1 health score from 3 factors.

    - Confirmation: log-scaled, saturates around 10.
    - Recency: exponential decay with 90-day half-life.
    - Retrieval: log-scaled, saturates around 20.
    """
    import math

    conf_score = min(1.0, math.log1p(confirmation_count) / math.log1p(10))
    recency_score = math.exp(-0.693 * days_since_update / 90.0)  # half-life = 90 days
    retrieval_score = min(1.0, math.log1p(retrieval_count) / math.log1p(20))

    return _W_CONFIRMATION * conf_score + _W_RECENCY * recency_score + _W_RETRIEVAL * retrieval_score


async def compute_health_scores() -> HealthScoreResult:
    """Recompute health_score for all active (non-superseded, non-archived) memories.

    Extracts primitive values inside the session scope per ORM guidelines.
    """
    result = HealthScoreResult()
    now = datetime.now(timezone.utc)

    async with await get_session() as session:
        async with session.begin():
            stmt = select(Memory).where(
                Memory.superseded_by.is_(None),
                Memory.archived.is_(False),
            )
            rows = await session.execute(stmt)
            memories = list(rows.scalars().all())
            result.total = len(memories)

            for mem in memories:
                confirmation_count = parse_confirmation_counter(mem.content)
                updated_at = _ensure_utc(mem.updated_at or mem.created_at)
                days_since_update = max(0.0, (now - updated_at).total_seconds() / 86400.0)
                retrieval_count = mem.retrieval_count or 0

                score = _compute_score(
                    confirmation_count=confirmation_count,
                    days_since_update=days_since_update,
                    retrieval_count=retrieval_count,
                )
                mem.health_score = round(score, 4)
                result.updated += 1

    log.info("lifecycle.health_scores_computed", updated=result.updated, total=result.total)
    return result


async def flag_stale_memories(
    *,
    staleness_days: int = DEFAULT_STALENESS_DAYS,
) -> list[int]:
    """Identify memories that haven't been retrieved in staleness_days.

    Guards:
    - Memories created within staleness_days are never flagged (too new).
    - Memories with confirmation count >= HIGH_CONFIRMATION_EXEMPT are exempt.

    Returns list of stale memory IDs (does NOT archive them).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=staleness_days)
    stale_ids: list[int] = []

    async with await get_session() as session:
        async with session.begin():
            stmt = select(Memory).where(
                Memory.superseded_by.is_(None),
                Memory.archived.is_(False),
                Memory.created_at < cutoff,  # not too new
            )
            rows = await session.execute(stmt)
            for mem in rows.scalars().all():
                if parse_confirmation_counter(mem.content) >= HIGH_CONFIRMATION_EXEMPT:
                    continue

                last_used = _ensure_utc(mem.last_retrieved_at or mem.created_at)
                if last_used < cutoff:
                    stale_ids.append(mem.id)

    log.info("lifecycle.stale_flagged", count=len(stale_ids), staleness_days=staleness_days)
    return stale_ids


async def archive_memories(memory_ids: list[int]) -> int:
    """Soft-delete memories by setting archived=True.

    Returns number of memories archived.
    """
    if not memory_ids:
        return 0

    count = 0
    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(select(Memory).where(Memory.id.in_(memory_ids)))
            for mem in result.scalars().all():
                if not mem.archived:
                    mem.archived = True
                    count += 1

    log.info("lifecycle.archived", count=count)
    return count


async def find_archive_candidates(*, archive_days: int = DEFAULT_ARCHIVE_DAYS) -> list[Memory]:
    """Find memories eligible for archival (0 confirmations, older than archive_days).

    Returns full Memory objects for display/inspection. Used by both
    auto_archive() and CLI dry-run to avoid query duplication.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=archive_days)

    async with await get_session() as session:
        async with session.begin():
            stmt = select(Memory).where(
                Memory.superseded_by.is_(None),
                Memory.archived.is_(False),
                Memory.created_at < cutoff,
            )
            rows = await session.execute(stmt)
            candidates = []
            for m in rows.scalars().all():
                if parse_confirmation_counter(m.content) != 0:
                    continue
                last_used = _ensure_utc(m.last_retrieved_at or m.created_at)
                if last_used < cutoff:
                    candidates.append(m)
            return candidates


async def auto_archive(*, archive_days: int = DEFAULT_ARCHIVE_DAYS) -> int:
    """Archive memories with 0 confirmations older than archive_days.

    Safe: archival is soft-delete, recoverable via include_archived=True.
    """
    candidates = await find_archive_candidates(archive_days=archive_days)
    return await archive_memories([m.id for m in candidates])


async def find_consolidation_candidates() -> list[ConsolidationCluster]:
    """Find groups of 3+ similar memories that could be consolidated.

    Uses title-based matching (lexical). Upgrade path: add embedding cosine
    similarity when #224 lands.
    """
    async with await get_session() as session:
        async with session.begin():
            stmt = select(Memory).where(
                Memory.superseded_by.is_(None),
                Memory.archived.is_(False),
            )
            rows = await session.execute(stmt)
            memories = list(rows.scalars().all())

    if len(memories) < MIN_CLUSTER_SIZE:
        return []

    # Extract primitive data for clustering (ORM objects may be detached)
    items = [(m.id, m.title, m.content, m.category) for m in memories]

    clusters = _build_clusters(items)
    log.info("lifecycle.consolidation_candidates", clusters=len(clusters))
    return clusters


def _find_cluster_members(
    anchor: tuple[int, str, str, str],
    candidates: list[tuple[int, str, str, str]],
    assigned: set[int],
) -> list[tuple[int, str]]:
    """Find all memories matching the anchor by title within the same category."""
    id_a, title_a, _, cat_a = anchor
    members = [(id_a, title_a)]
    for id_b, title_b, _, cat_b in candidates:
        if id_b in assigned or cat_a != cat_b:
            continue
        if titles_match(title_a, title_b):
            members.append((id_b, title_b))
    return members


def _build_clusters(items: list[tuple[int, str, str, str]]) -> list[ConsolidationCluster]:
    """Build consolidation clusters via greedy matching."""
    assigned: set[int] = set()
    clusters: list[ConsolidationCluster] = []

    for i, anchor in enumerate(items):
        if anchor[0] in assigned:
            continue

        members = _find_cluster_members(anchor, items[i + 1 :], assigned)
        if len(members) >= MIN_CLUSTER_SIZE:
            cluster = ConsolidationCluster(
                representative_id=members[0][0],
                member_ids=[mid for mid, _ in members],
                titles=[t for _, t in members],
            )
            clusters.append(cluster)
            assigned.update(cluster.member_ids)

    return clusters


async def consolidate_cluster(
    cluster: ConsolidationCluster,
    *,
    cwd: Path | str,
) -> int | None:
    """Merge a cluster of similar memories into one via LLM.

    Returns the ID of the new consolidated memory, or None on failure.
    Non-fatal: logs warning and returns None on LLM error.
    """
    # Load full memory objects
    async with await get_session() as session:
        async with session.begin():
            stmt = select(Memory).where(Memory.id.in_(cluster.member_ids))
            rows = await session.execute(stmt)
            members = list(rows.scalars().all())

    if len(members) < MIN_CLUSTER_SIZE:
        return None

    # Sum confirmation counters
    total_confirmations = sum(parse_confirmation_counter(m.content) for m in members)

    # Extract member data for LLM prompt
    member_texts = []
    all_tags: set[str] = set()
    category = members[0].category
    repo = members[0].repo
    for m in members:
        member_texts.append(f"Title: {m.title}\nContent: {m.content}")
        if m.tags:
            all_tags.update(t.strip() for t in m.tags.split(",") if t.strip())

    prompt = f"""You are a knowledge consolidation assistant. Merge these {len(members)} related memory entries \
into a single, comprehensive entry.

## Entries to merge
{chr(10).join(f"---{chr(10)}{t}" for t in member_texts)}
---

## Rules
- Combine all unique information from all entries
- Remove redundancy -- say it once clearly
- Preserve actionable details and the WHY behind patterns
- Keep the same tone: concise, technical, imperative

## Output Format
Return ONLY a JSON object (no markdown fences, no extra text):
{{"title": "Consolidated title (max 100 chars)", "content": "Merged content"}}"""

    try:
        from sova.llm.client import invoke

        llm_result = await invoke(prompt, model="haiku", cwd=cwd, timeout=60)
        import json

        text = llm_result.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        data = json.loads(text)
        new_title = data.get("title", cluster.titles[0])
        if not isinstance(new_title, str):
            new_title = cluster.titles[0]
        new_title = new_title[:200]
        new_content = data.get("content", "")
        if not isinstance(new_content, str) or not new_content:
            log.warning("lifecycle.consolidation_empty_content", cluster_id=cluster.representative_id)
            return None

    except Exception:
        log.warning("lifecycle.consolidation_llm_failed", cluster_id=cluster.representative_id, exc_info=True)
        return None

    # Store consolidated memory and supersede old entries atomically
    new_content = set_confirmation_counter(new_content, total_confirmations)

    member_ids = [m.id for m in members]
    from sqlalchemy import update as sql_update

    async with await get_session() as session:
        async with session.begin():
            new_mem = Memory(
                category=category,
                title=new_title,
                content=new_content,
                tags=",".join(sorted(all_tags)) if all_tags else None,
                repo=repo,
                tier="project",
            )
            session.add(new_mem)
            await session.flush()  # Get the new ID

            stmt = sql_update(Memory).where(Memory.id.in_(member_ids)).values(superseded_by=new_mem.id)
            await session.execute(stmt)

    log.info(
        "lifecycle.consolidated",
        new_id=new_mem.id,
        merged_count=len(members),
        total_confirmations=total_confirmations,
    )
    return new_mem.id


async def auto_cleanup(
    *,
    staleness_days: int = DEFAULT_STALENESS_DAYS,
    archive_days: int = DEFAULT_ARCHIVE_DAYS,
    cwd: Path | str | None = None,
) -> CleanupResult:
    """Run all lifecycle maintenance operations.

    1. Compute health scores.
    2. Flag stale memories.
    3. Auto-archive low-value memories.
    4. Find and consolidate duplicate clusters (if cwd provided).

    Returns a CleanupResult summary.
    """
    result = CleanupResult()

    await compute_health_scores()

    stale_ids = await flag_stale_memories(staleness_days=staleness_days)
    result.stale_flagged = len(stale_ids)

    result.archived = await auto_archive(archive_days=archive_days)

    if cwd is not None:
        clusters = await find_consolidation_candidates()
        for cluster in clusters:
            new_id = await consolidate_cluster(cluster, cwd=cwd)
            if new_id is not None:
                result.consolidated += 1

    log.info(
        "lifecycle.auto_cleanup",
        stale=result.stale_flagged,
        archived=result.archived,
        consolidated=result.consolidated,
    )
    return result
