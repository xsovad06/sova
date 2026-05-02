"""Knowledge memory store -- CRUD, search, promote, supersede operations on the Memory model."""

from __future__ import annotations

from sqlalchemy import or_, select

from sova.db.models import Memory
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="knowledge.memory")


async def store(
    *,
    category: str,
    title: str,
    content: str,
    tags: list[str],
    tier: str = "project",
    repo: str = "",
    issue_number: str = "",
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

    Returns:
        The created Memory record.
    """
    memory = Memory(
        category=category,
        title=title,
        content=content,
        tags=",".join(tags),
        tier=tier,
        repo=repo,
        issue_number=issue_number,
    )

    async with await get_session() as session:
        async with session.begin():
            session.add(memory)
            await session.flush()

    log.info("knowledge.stored", title=title, category=category, tier=tier)
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
) -> list[Memory]:
    """Search memories with optional filters.

    Args:
        query: Text to search in title and content.
        category: Filter by category.
        tags: Filter by tags (any match).
        tier: Filter by knowledge tier.
        include_superseded: If False (default), exclude superseded entries.

    Returns:
        List of matching Memory records.
    """
    stmt = select(Memory)

    if not include_superseded:
        stmt = stmt.where(Memory.superseded_by.is_(None))

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

    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(stmt)
            return list(result.scalars().all())


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
