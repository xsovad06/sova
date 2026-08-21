"""Memory graph -- edge CRUD, neighbor traversal, auto-linking, and batch discovery."""

from __future__ import annotations

from collections import deque

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from sova.db.models import Memory, MemoryEdge
from sova.db.session import get_session
from sova.knowledge.embeddings import cosine_similarity, is_available
from sova.utils.logging import get_logger

log = get_logger(component="knowledge.graph")

RELATION_TYPES = frozenset({"relates_to", "refines", "depends_on", "supersedes", "contradicts"})

AUTO_LINK_THRESHOLD = 0.75
AUTO_LINK_MAX_EDGES = 5
_DISCOVER_BATCH_SIZE = 100


async def create_edge(
    source_id: int,
    target_id: int,
    relation: str = "relates_to",
    weight: float = 1.0,
) -> MemoryEdge | None:
    """Create a directed edge between two memories.

    Returns the created edge, or None if a duplicate already exists.
    Raises ValueError for self-edges or invalid relation types.
    """
    if source_id == target_id:
        raise ValueError("Self-edges are not allowed")
    if relation not in RELATION_TYPES:
        raise ValueError(f"Invalid relation '{relation}' (valid: {', '.join(sorted(RELATION_TYPES))})")

    async with await get_session() as session:
        async with session.begin():
            found = await session.execute(select(Memory.id).where(Memory.id.in_([source_id, target_id])))
            if len(list(found.scalars())) != 2:
                raise ValueError(f"One or both memory IDs do not exist: source={source_id}, target={target_id}")

            existing = await session.execute(
                select(MemoryEdge).where(
                    MemoryEdge.relation == relation,
                    or_(
                        (MemoryEdge.source_id == source_id) & (MemoryEdge.target_id == target_id),
                        (MemoryEdge.source_id == target_id) & (MemoryEdge.target_id == source_id),
                    ),
                )
            )
            if existing.scalar_one_or_none() is not None:
                return None
            edge = MemoryEdge(source_id=source_id, target_id=target_id, relation=relation, weight=weight)
            session.add(edge)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                return None
            log.info("graph.edge_created", source=source_id, target=target_id, relation=relation)
            return edge


async def delete_edge(edge_id: int) -> bool:
    """Delete an edge by ID. Returns True if deleted, False if not found."""
    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(select(MemoryEdge).where(MemoryEdge.id == edge_id))
            edge = result.scalar_one_or_none()
            if edge is None:
                return False
            await session.delete(edge)
            log.info("graph.edge_deleted", edge_id=edge_id)
            return True


async def get_edges(memory_id: int) -> list[MemoryEdge]:
    """Get all edges where the given memory is source or target."""
    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(
                select(MemoryEdge).where(or_(MemoryEdge.source_id == memory_id, MemoryEdge.target_id == memory_id))
            )
            return list(result.scalars().all())


async def _bfs_collect_neighbor_ids(
    session: object,
    memory_id: int,
    depth: int,
    relation: str | None,
) -> list[int]:
    """BFS traversal collecting neighbor IDs up to given depth."""
    visited: set[int] = {memory_id}
    queue: deque[tuple[int, int]] = deque([(memory_id, 0)])
    neighbor_ids: list[int] = []

    while queue:
        current_id, current_depth = queue.popleft()
        if current_depth >= depth:
            continue

        stmt = select(MemoryEdge).where(or_(MemoryEdge.source_id == current_id, MemoryEdge.target_id == current_id))
        if relation is not None:
            stmt = stmt.where(MemoryEdge.relation == relation)

        result = await session.execute(stmt)
        for edge in result.scalars().all():
            other_id = edge.target_id if edge.source_id == current_id else edge.source_id
            if other_id not in visited:
                visited.add(other_id)
                neighbor_ids.append(other_id)
                queue.append((other_id, current_depth + 1))

    return neighbor_ids


async def get_neighbors(
    memory_id: int,
    *,
    depth: int = 1,
    relation: str | None = None,
) -> list[Memory]:
    """Get neighbor memories via BFS traversal up to the given depth.

    Filters out superseded memories. Treats all edges as bidirectional for traversal
    (even directional ones like refines/depends_on), since the goal is context discovery.
    """
    if depth < 1 or depth > 2:
        raise ValueError("depth must be 1 or 2")

    async with await get_session() as session:
        async with session.begin():
            neighbor_ids = await _bfs_collect_neighbor_ids(session, memory_id, depth, relation)

            if not neighbor_ids:
                return []

            mem_result = await session.execute(
                select(Memory).where(
                    Memory.id.in_(neighbor_ids),
                    Memory.superseded_by.is_(None),
                )
            )
            return list(mem_result.scalars().all())


async def auto_link(memory_id: int) -> list[MemoryEdge]:
    """Auto-discover and create edges for a memory based on embedding similarity.

    Returns empty list if embeddings are unavailable or the memory has no embedding.
    """
    if not is_available():
        return []

    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(select(Memory).where(Memory.id == memory_id))
            source = result.scalar_one_or_none()
            if source is None or source.embedding is None:
                return []

            source_embedding = source.embedding

            candidates = await session.execute(
                select(Memory).where(
                    Memory.id != memory_id,
                    Memory.category == source.category,
                    Memory.superseded_by.is_(None),
                    Memory.embedding.isnot(None),
                )
            )
            candidate_data = [(c.id, c.embedding) for c in candidates.scalars().all()]

    scored: list[tuple[int, float]] = []
    for cand_id, cand_embedding in candidate_data:
        score = cosine_similarity(source_embedding, cand_embedding)
        if score >= AUTO_LINK_THRESHOLD:
            scored.append((cand_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    scored = scored[:AUTO_LINK_MAX_EDGES]

    created: list[MemoryEdge] = []
    for target_id, score in scored:
        edge = await create_edge(memory_id, target_id, relation="relates_to", weight=round(score, 4))
        if edge is not None:
            created.append(edge)

    log.info("graph.auto_linked", memory_id=memory_id, edges_created=len(created))
    return created


async def _compare_category_batch(cat_memories: list[tuple[int, list[float]]]) -> int:
    """Compare memory pairs within a single category and create edges for similar ones.

    Receives list of (id, embedding) tuples (primitives extracted inside session).
    Processes in batches of _DISCOVER_BATCH_SIZE to limit per-iteration work.
    """
    created = 0
    for batch_start in range(0, len(cat_memories), _DISCOVER_BATCH_SIZE):
        batch_end = min(batch_start + _DISCOVER_BATCH_SIZE, len(cat_memories))
        for i in range(batch_start, batch_end):
            id_a, emb_a = cat_memories[i]
            for j in range(i + 1, batch_end):
                id_b, emb_b = cat_memories[j]
                score = cosine_similarity(emb_a, emb_b)
                if score < AUTO_LINK_THRESHOLD:
                    continue
                edge = await create_edge(id_a, id_b, relation="relates_to", weight=round(score, 4))
                if edge is not None:
                    created += 1
    return created


async def _fetch_memories_with_embeddings(category: str | None) -> list[tuple[int, str, list[float]]]:
    """Fetch non-superseded memories that have embeddings, optionally filtered by category.

    Returns list of (id, category, embedding) tuples to avoid detached instance errors.
    """
    async with await get_session() as session:
        async with session.begin():
            stmt = select(Memory).where(
                Memory.superseded_by.is_(None),
                Memory.embedding.isnot(None),
            )
            if category is not None:
                stmt = stmt.where(Memory.category == category)
            result = await session.execute(stmt)
            return [(m.id, m.category, m.embedding) for m in result.scalars().all()]


async def discover_edges(category: str | None = None) -> int:
    """Batch-discover edges across all memories in a category.

    Compares within same category only. Processes in batches of 100 for large corpora.
    Returns the total number of new edges created.
    """
    if not is_available():
        log.info("graph.discover_skipped", reason="embeddings unavailable")
        return 0

    all_memories = await _fetch_memories_with_embeddings(category)
    if not all_memories:
        return 0

    by_category: dict[str, list[tuple[int, list[float]]]] = {}
    for mem_id, mem_cat, mem_emb in all_memories:
        by_category.setdefault(mem_cat, []).append((mem_id, mem_emb))

    total_created = 0
    for cat_memories in by_category.values():
        total_created += await _compare_category_batch(cat_memories)

    log.info("graph.discover_complete", total_edges=total_created, memories_scanned=len(all_memories))
    return total_created
