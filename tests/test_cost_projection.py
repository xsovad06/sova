"""Tests for cost_service.get_monthly_projection (monthly cost projection)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sova.dashboard.services import cost_service
from sova.db.models import TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db(monkeypatch):
    monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
    await init_db(run_migrations=False)
    yield
    await close_db()


@pytest.fixture
async def session() -> AsyncSession:
    async with await get_session() as sess:
        yield sess


async def test_insufficient_data_when_no_cost(session: AsyncSession) -> None:
    result = await cost_service.get_monthly_projection(session)
    assert result["insufficient_data"] is True
    assert result["projected_monthly_usd"] == Decimal("0")
    assert result["observed_days"] == 0


async def test_insufficient_data_ignores_zero_cost_runs(session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    session.add(TaskRun(issue_number="1", role="developer", status="done", total_cost_usd=Decimal("0"), started_at=now))
    await session.commit()

    result = await cost_service.get_monthly_projection(session)
    assert result["insufficient_data"] is True


async def test_projection_scales_daily_average(session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    # $2/day for 10 days -> ~$60/month projection.
    session.add(
        TaskRun(
            issue_number="1",
            role="developer",
            status="done",
            total_cost_usd=Decimal("20.00"),
            started_at=now - timedelta(days=10),
        )
    )
    await session.commit()

    result = await cost_service.get_monthly_projection(session)
    assert result["insufficient_data"] is False
    assert result["observed_days"] == 10
    assert result["window_total_usd"] == Decimal("20.0000")
    assert result["daily_avg_usd"] == Decimal("2.0000")
    assert result["projected_monthly_usd"] == Decimal("60.00")


async def test_projection_excludes_costs_outside_window(session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        TaskRun(
            issue_number="old",
            role="developer",
            status="done",
            total_cost_usd=Decimal("100.00"),
            started_at=now - timedelta(days=45),
        )
    )
    session.add(
        TaskRun(
            issue_number="new",
            role="developer",
            status="done",
            total_cost_usd=Decimal("5.00"),
            started_at=now - timedelta(days=5),
        )
    )
    await session.commit()

    result = await cost_service.get_monthly_projection(session)
    assert result["window_total_usd"] == Decimal("5.0000")
    assert result["observed_days"] == 5


async def test_single_day_projection_does_not_crash(session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        TaskRun(
            issue_number="1",
            role="developer",
            status="done",
            total_cost_usd=Decimal("3.00"),
            started_at=now,
        )
    )
    await session.commit()

    result = await cost_service.get_monthly_projection(session)
    assert result["insufficient_data"] is False
    assert result["observed_days"] == 1
    assert result["projected_monthly_usd"] == Decimal("90.00")
