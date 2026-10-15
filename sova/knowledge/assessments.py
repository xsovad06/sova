"""Local suitability assessments (task_assessments): set/get/clear a manually-recorded
triage verdict for a ticket, used as a fallback when no `agent:` label is present.

Unlike a tracker label, this record is written even for a ticket the account
does not own: `/find-task` writes it for every triaged ticket (self-assigned or
unassigned), but only self-assigned tickets also get the tracker label. A label
always takes precedence over this record when both exist.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import TaskAssessmentRecord
from sova.utils.logging import get_logger

log = get_logger(component="knowledge.assessments")

# Mirrors the three suitability labels written by `/find-task`
# (agent:ready, agent:interactive-recommended, agent:human-only).
SUITABILITY_SHORTHANDS: dict[str, str] = {
    "ready": "ready",
    "interactive": "interactive_recommended",
    "human-only": "human_only",
}
_SHORTHAND_BY_SUITABILITY: dict[str, str] = {v: k for k, v in SUITABILITY_SHORTHANDS.items()}


def resolve_suitability(shorthand: str) -> str | None:
    """Map a CLI shorthand (ready/interactive/human-only) to its stored enum value."""
    return SUITABILITY_SHORTHANDS.get(shorthand)


def shorthand_for(suitability: str) -> str:
    """Map a stored enum value back to its CLI shorthand, falling back to the raw value."""
    return _SHORTHAND_BY_SUITABILITY.get(suitability, suitability)


async def set_assessment(
    session: AsyncSession,
    *,
    issue_number: str,
    project_slug: str,
    suitability: str,
    reasoning: str,
) -> TaskAssessmentRecord:
    """Record a new local suitability assessment. Append-only: the latest row wins.

    `estimated_complexity` is deliberately written empty rather than left to the
    model's `"moderate"` default: this row carries a suitability verdict only, and
    the dashboard complexity endpoint treats the latest row's truthy
    `estimated_complexity` as authoritative, skipping its own `assess_complexity()`
    call. A default-valued column here would pin every ticket `/find-task` triaged
    to the moderate tier.
    """
    record = TaskAssessmentRecord(
        issue_number=issue_number,
        project_slug=project_slug,
        suitability=suitability,
        confidence=1.0,
        reasoning=reasoning,
        estimated_complexity="",
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    log.info("assessments.set", issue=issue_number, project=project_slug, suitability=suitability)
    return record


async def get_latest_assessment(
    session: AsyncSession,
    *,
    issue_number: str,
    project_slug: str,
) -> TaskAssessmentRecord | None:
    """Return the most recently assessed row for this issue/project, or None.

    Ties on ``assessed_at`` (two rows written within the same timestamp
    tick) break on ``id`` descending: the table is append-only, so a higher
    id is always the later insert, making "latest wins" total rather than
    dependent on timestamp resolution.
    """
    stmt = (
        select(TaskAssessmentRecord)
        .where(
            TaskAssessmentRecord.issue_number == issue_number,
            TaskAssessmentRecord.project_slug == project_slug,
        )
        .order_by(TaskAssessmentRecord.assessed_at.desc(), TaskAssessmentRecord.id.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def clear_assessment(
    session: AsyncSession,
    *,
    issue_number: str,
    project_slug: str,
) -> int:
    """Delete every local assessment row for this issue/project. A no-op when none exist."""
    stmt = delete(TaskAssessmentRecord).where(
        TaskAssessmentRecord.issue_number == issue_number,
        TaskAssessmentRecord.project_slug == project_slug,
    )
    result = await session.execute(stmt)
    await session.commit()
    count = result.rowcount or 0
    log.info("assessments.cleared", issue=issue_number, project=project_slug, count=count)
    return count
