"""Oversight dashboard service: queries for runs, findings, and status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import OversightFinding, OversightRun


async def get_status(session: AsyncSession, *, enabled: bool, agent_running: bool, wake_interval_minutes: int) -> dict:
    """Build the status response for the stat tiles."""
    last_run_row = (
        await session.execute(select(OversightRun).order_by(OversightRun.started_at.desc()).limit(1))
    ).scalar_one_or_none()

    last_run: dict | None = None
    next_wake_approx: str | None = None
    if last_run_row is not None:
        last_run = {
            "id": last_run_row.id,
            "status": last_run_row.status,
            "started_at": last_run_row.started_at.isoformat() if last_run_row.started_at else None,
            "duration_ms": last_run_row.duration_ms,
        }
        if last_run_row.started_at and agent_running:
            next_dt = last_run_row.started_at + timedelta(minutes=wake_interval_minutes)
            next_wake_approx = next_dt.isoformat()

    pending_count = (
        await session.scalar(
            select(func.count(OversightFinding.id)).where(
                OversightFinding.dismissed.is_(False),
                OversightFinding.github_issue_number.is_(None),
            )
        )
        or 0
    )

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    issues_this_week = (
        await session.scalar(
            select(func.count(OversightFinding.id)).where(
                OversightFinding.github_issue_number.isnot(None),
                OversightFinding.created_at >= week_ago,
            )
        )
        or 0
    )

    return {
        "enabled": enabled,
        "running": agent_running,
        "last_run": last_run,
        "next_wake_approx": next_wake_approx,
        "pending_findings_count": pending_count,
        "issues_proposed_this_week": issues_this_week,
    }


async def get_runs(session: AsyncSession, *, limit: int = 20) -> list[dict]:
    """Return recent OversightRun records with finding counts."""
    findings_count = (
        select(func.count(OversightFinding.id))
        .where(OversightFinding.run_id == OversightRun.id)
        .correlate(OversightRun)
        .scalar_subquery()
    )
    issues_created = (
        select(func.count(OversightFinding.id))
        .where(
            OversightFinding.run_id == OversightRun.id,
            OversightFinding.github_issue_number.isnot(None),
        )
        .correlate(OversightRun)
        .scalar_subquery()
    )

    stmt = (
        select(
            OversightRun,
            findings_count.label("findings_count"),
            issues_created.label("issues_created"),
        )
        .order_by(OversightRun.started_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()

    return [
        {
            "id": row.OversightRun.id,
            "status": row.OversightRun.status,
            "cycle_number": row.OversightRun.cycle_number,
            "started_at": row.OversightRun.started_at.isoformat() if row.OversightRun.started_at else None,
            "ended_at": row.OversightRun.ended_at.isoformat() if row.OversightRun.ended_at else None,
            "duration_ms": row.OversightRun.duration_ms,
            "findings_count": row.findings_count,
            "issues_created": row.issues_created,
            "error": row.OversightRun.error,
        }
        for row in rows
    ]


async def get_findings(session: AsyncSession, *, status: str = "pending", limit: int = 50) -> list[dict]:
    """Return OversightFinding records filtered by logical status."""
    stmt = select(OversightFinding)

    if status == "pending":
        stmt = stmt.where(
            OversightFinding.dismissed.is_(False),
            OversightFinding.github_issue_number.is_(None),
        )
    elif status == "created":
        stmt = stmt.where(OversightFinding.github_issue_number.isnot(None))
    elif status == "dismissed":
        stmt = stmt.where(OversightFinding.dismissed.is_(True))

    stmt = stmt.order_by(OversightFinding.created_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()

    return [_finding_to_dict(f) for f in rows]


async def dismiss_finding(session: AsyncSession, finding_id: int) -> dict | None:
    """Set dismissed=True on a finding. Returns the updated dict, or None if not found."""
    finding = await session.get(OversightFinding, finding_id)
    if finding is None:
        return None
    finding.dismissed = True
    await session.flush()
    return _finding_to_dict(finding)


async def get_finding_by_id(session: AsyncSession, finding_id: int) -> OversightFinding | None:
    """Load a single finding by primary key."""
    return await session.get(OversightFinding, finding_id)


async def update_finding_issue_number(session: AsyncSession, finding_id: int, issue_number: int) -> None:
    """Set github_issue_number on a finding after issue creation."""
    finding = await session.get(OversightFinding, finding_id)
    if finding is not None:
        finding.github_issue_number = issue_number
        await session.flush()


def _finding_to_dict(f: OversightFinding) -> dict:
    return {
        "id": f.id,
        "run_id": f.run_id,
        "title": f.title,
        "scope": f.scope,
        "severity": f.severity,
        "description": f.description,
        "recommendation": f.recommendation,
        "confidence": float(f.confidence),
        "project_slug": f.project_slug,
        "dismissed": f.dismissed,
        "github_issue_number": f.github_issue_number,
        "created_at": f.created_at.isoformat() if f.created_at else None,
    }
