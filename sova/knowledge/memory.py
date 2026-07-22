"""Knowledge memory store -- CRUD, search, promote, supersede operations on the Memory model."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select

from sova.db.models import Memory, MemoryEdge
from sova.db.session import get_session
from sova.knowledge.embeddings import SIMILARITY_THRESHOLD, cosine_similarity, embed_text
from sova.utils.logging import get_logger

log = get_logger(component="knowledge.memory")

_UNSET = object()

_MUTABLE_FIELDS = frozenset(
    {
        "title",
        "content",
        "category",
        "tags",
        "tier",
        "repo",
        "issue_number",
        "superseded_by",
        "embedding",
        "retrieval_count",
        "last_retrieved_at",
        "archived",
        "health_score",
    }
)


async def store(
    *,
    category: str,
    title: str,
    content: str,
    tags: list[str],
    tier: str = "project",
    repo: str = "",
    issue_number: str = "",
    embedding: list[float] | None = _UNSET,  # type: ignore[assignment]
) -> Memory:
    """Create a new memory entry.

    Args:
        category: Memory category (learning, review, debugging, etc.).
        title: Short descriptive title.
        content: Full content of the memory.
        tags: List of tags for filtering.
        tier: Knowledge tier (project, shared).
        repo: Repository identifier (e.g., user/repo).
        issue_number: Related issue number.
        embedding: Pre-computed embedding vector. Auto-computed when omitted;
            pass ``None`` explicitly to store without an embedding.

    Returns:
        The created Memory record.
    """
    embedding_auto = embedding is _UNSET
    if embedding_auto:
        embedding = embed_text(f"{title} {content}")
    memory = Memory(
        category=category,
        title=title,
        content=content,
        tags=",".join(tags),
        tier=tier,
        repo=repo,
        issue_number=issue_number,
        embedding=embedding,
    )

    async with await get_session() as session:
        async with session.begin():
            session.add(memory)
            await session.flush()

    log.info("knowledge.stored", title=title, category=category, tier=tier)

    if memory.embedding is not None and is_available():
        try:
            from sova.knowledge.graph import auto_link

            await auto_link(memory.id)
        except Exception:
            log.warning("knowledge.auto_link_failed", memory_id=memory.id, exc_info=True)

    return memory


async def get(memory_id: int) -> Memory | None:
    """Retrieve a memory by ID.

    Returns:
        The Memory record, or None if not found.
    """
    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(select(Memory).where(Memory.id == memory_id))
            return result.scalar_one_or_none()


async def update(memory_id: int, **fields: object) -> Memory | None:
    """Update fields on an existing memory.

    Args:
        memory_id: ID of the memory to update.
        **fields: Fields to update (title, content, category, tags, tier, etc.).

    Returns:
        The updated Memory, or None if not found.
    """
    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(select(Memory).where(Memory.id == memory_id))
            memory = result.scalar_one_or_none()
            if memory is None:
                return None

            for key, value in fields.items():
                if key not in _MUTABLE_FIELDS:
                    raise ValueError(
                        f"Cannot update field '{key}' on Memory (allowed: {', '.join(sorted(_MUTABLE_FIELDS))})"
                    )
                setattr(memory, key, value)

            log.info("knowledge.updated", memory_id=memory_id, fields=list(fields.keys()))
            return memory


async def delete(memory_id: int) -> bool:
    """Delete a memory by ID.

    Returns:
        True if deleted, False if not found.
    """
    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(select(Memory).where(Memory.id == memory_id))
            memory = result.scalar_one_or_none()
            if memory is None:
                return False

            await session.delete(memory)
            log.info("knowledge.deleted", memory_id=memory_id)
            return True


async def search(
    *,
    query: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    tier: str | None = None,
    include_superseded: bool = False,
    include_archived: bool = False,
    expand: bool = False,
    limit: int | None = None,
) -> list[Memory]:
    """Search memories with optional filters.

    Args:
        query: Text to search in title and content.
        category: Filter by category.
        tags: Filter by tags (any match).
        tier: Filter by knowledge tier.
        include_superseded: If False (default), exclude superseded entries.
        include_archived: If False (default), exclude archived entries.
        expand: If True, include 1-hop graph neighbors of matching results.
        limit: Maximum number of results to return (None = unlimited).

    Returns:
        List of matching Memory records.
    """
    stmt = select(Memory)

    if not include_superseded:
        stmt = stmt.where(Memory.superseded_by.is_(None))

    if not include_archived:
        stmt = stmt.where(Memory.archived.is_(False))

    if category is not None:
        stmt = stmt.where(Memory.category == category)

    if tier is not None:
        stmt = stmt.where(Memory.tier == tier)

    if query is not None:
        pattern = f"%{query}%"
        stmt = stmt.where(
            (Memory.title.ilike(pattern)) | (Memory.content.ilike(pattern)) | (Memory.tags.ilike(pattern))
        )

    if tags:
        # Match any of the provided tags (OR logic)
        tag_conditions = [Memory.tags.ilike(f"%{tag}%") for tag in tags]
        stmt = stmt.where(or_(*tag_conditions))

    stmt = stmt.order_by(Memory.updated_at.desc())

    if limit is not None:
        stmt = stmt.limit(limit)

    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(stmt)
            results = list(result.scalars().all())

    if expand and results:
        results = await _expand_with_neighbors(results)

    return results


async def promote(memory_id: int, new_tier: str) -> Memory | None:
    """Promote a memory to a higher tier.

    Args:
        memory_id: ID of the memory to promote.
        new_tier: Target tier (e.g., "shared").

    Returns:
        The updated Memory, or None if not found.
    """
    result = await update(memory_id, tier=new_tier)
    if result is not None:
        log.info("knowledge.promoted", memory_id=memory_id, new_tier=new_tier)
    return result


async def supersede(old_id: int, new_id: int) -> bool:
    """Mark an old memory as superseded by a new one.

    Args:
        old_id: ID of the memory being replaced.
        new_id: ID of the replacement memory.

    Returns:
        True if the old entry was updated, False if not found.
    """
    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(select(Memory).where(Memory.id == old_id))
            memory = result.scalar_one_or_none()
            if memory is None:
                return False

            memory.superseded_by = new_id
            log.info("knowledge.superseded", old_id=old_id, new_id=new_id)
            return True


async def semantic_search(
    *,
    query: str,
    category: str | None = None,
    tier: str | None = None,
    limit: int = 10,
    threshold: float = 0.0,
    query_embedding: list[float] | None = None,
    expand: bool = False,
    include_archived: bool = False,
) -> list[tuple[Memory, float]]:
    """Search memories by semantic similarity using embeddings.

    Falls back to lexical search if embeddings are unavailable or query is empty.

    Args:
        query_embedding: Pre-computed embedding for the query. Computed automatically if None.

    Returns:
        List of (Memory, similarity_score) tuples, sorted by score descending.
    """
    if not query.strip():
        results = await search(category=category, tier=tier, include_archived=include_archived)
        return [(m, 0.0) for m in results[:limit]]

    if query_embedding is None:
        query_embedding = embed_text(query)
    if query_embedding is None:
        log.warning("semantic_search.fallback", reason="embedding unavailable")
        results = await search(query=query, category=category, tier=tier, include_archived=include_archived)
        return [(m, 0.0) for m in results[:limit]]

    stmt = select(Memory).where(Memory.superseded_by.is_(None))
    if not include_archived:
        stmt = stmt.where(Memory.archived.is_(False))
    if category is not None:
        stmt = stmt.where(Memory.category == category)
    if tier is not None:
        stmt = stmt.where(Memory.tier == tier)

    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(stmt)
            all_memories = list(result.scalars().all())

    top = _rank_by_similarity(all_memories, query_embedding, threshold, limit)

    if expand and top:
        top = await _append_neighbors(top)

    return top


def _rank_by_similarity(
    memories: list[Memory],
    query_embedding: list[float],
    threshold: float,
    limit: int,
) -> list[tuple[Memory, float]]:
    """Score memories against query embedding and return top matches."""
    scored: list[tuple[Memory, float]] = []
    for mem in memories:
        if mem.embedding is None:
            continue
        score = cosine_similarity(query_embedding, mem.embedding)
        if score >= threshold:
            scored.append((mem, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


async def _append_neighbors(top: list[tuple[Memory, float]]) -> list[tuple[Memory, float]]:
    """Expand top results with 1-hop graph neighbors."""
    memories_only = [m for m, _ in top]
    expanded = await _expand_with_neighbors(memories_only)
    existing_ids = {m.id for m, _ in top}
    for nb in expanded:
        if nb.id not in existing_ids:
            existing_ids.add(nb.id)
            top.append((nb, 0.0))
    return top


async def _expand_with_neighbors(memories: list[Memory]) -> list[Memory]:
    """Expand a list of memories with their 1-hop graph neighbors (batched)."""
    all_ids = [m.id for m in memories]
    existing_ids = set(all_ids)

    async with await get_session() as session:
        async with session.begin():
            edge_result = await session.execute(
                select(MemoryEdge).where(or_(MemoryEdge.source_id.in_(all_ids), MemoryEdge.target_id.in_(all_ids)))
            )
            neighbor_ids: set[int] = set()
            for edge in edge_result.scalars().all():
                neighbor_ids.add(edge.source_id)
                neighbor_ids.add(edge.target_id)
            neighbor_ids -= existing_ids

            if not neighbor_ids:
                return list(memories)

            mem_result = await session.execute(
                select(Memory).where(
                    Memory.id.in_(neighbor_ids),
                    Memory.superseded_by.is_(None),
                    Memory.archived.is_(False),
                )
            )
            neighbors = list(mem_result.scalars().all())

    expanded = list(memories)
    for nb in neighbors:
        if nb.id not in existing_ids:
            existing_ids.add(nb.id)
            expanded.append(nb)

    return expanded


async def find_similar(
    text: str,
    *,
    category: str | None = None,
    threshold: float | None = None,
    query_embedding: list[float] | None = None,
) -> list[tuple[Memory, float]]:
    """Find memories semantically similar to the given text.

    Used for deduplication during extraction. Returns matches above threshold.

    Args:
        query_embedding: Pre-computed embedding. Computed automatically if None.
    """
    if threshold is None:
        threshold = SIMILARITY_THRESHOLD

    return await semantic_search(
        query=text,
        category=category,
        threshold=threshold,
        limit=5,
        query_embedding=query_embedding,
    )


async def increment_retrieval(memory_ids: list[int]) -> None:
    """Bump retrieval_count and last_retrieved_at for the given memory IDs.

    Called by retrieve_relevant() after results are selected for prompt injection.
    Uses a single bulk UPDATE for efficiency (hot path).
    """
    if not memory_ids:
        return

    from sqlalchemy import update as sql_update

    now = datetime.now(timezone.utc)
    async with await get_session() as session:
        async with session.begin():
            stmt = (
                sql_update(Memory)
                .where(Memory.id.in_(memory_ids))
                .values(
                    retrieval_count=Memory.retrieval_count + 1,
                    last_retrieved_at=now,
                )
            )
            await session.execute(stmt)
