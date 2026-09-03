"""Tests for cost_service compression savings aggregation (issue #897)."""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from sova.dashboard.services import cost_service
from sova.db.models import CostRecord
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


async def _add_records(*records: CostRecord) -> None:
    async with await get_session() as session:
        for record in records:
            session.add(record)
        await session.commit()


async def test_summary_sums_tokens_saved() -> None:
    await _add_records(
        CostRecord(phase="develop", model="claude-sonnet-5", cost_usd=Decimal("0.1"), tokens_saved=100),
        CostRecord(phase="develop", model="claude-sonnet-5", cost_usd=Decimal("0.1"), tokens_saved=50),
        CostRecord(phase="develop", model="claude-sonnet-5", cost_usd=Decimal("0.1"), tokens_saved=None),
    )

    async with await get_session() as session:
        summary = await cost_service.get_summary(session)

    assert summary["tokens_saved"] == 150


async def test_summary_all_null_savings_coerces_to_zero() -> None:
    await _add_records(
        CostRecord(phase="triage", model="claude-sonnet-5", cost_usd=Decimal("0.1"), tokens_saved=None),
    )

    async with await get_session() as session:
        summary = await cost_service.get_summary(session)

    assert summary["tokens_saved"] == 0
    assert summary["compression_savings_usd"] == Decimal("0")


async def test_summary_savings_usd_uses_configured_model_rate() -> None:
    await _add_records(
        CostRecord(phase="develop", model="claude-sonnet-5", cost_usd=Decimal("0.1"), tokens_saved=1_000_000),
    )

    cfg = MagicMock()
    cfg.llm.model = "claude-sonnet-5"
    cfg.agent.model = "opus"

    with patch("sova.config.loader.load_config", return_value=cfg):
        async with await get_session() as session:
            summary = await cost_service.get_summary(session)

    # claude-sonnet-5 input rate is $2/Mtok, so 1M tokens saved -> $2.00.
    assert summary["compression_savings_usd"] == Decimal("2")


async def test_summary_savings_usd_unknown_model_falls_back_to_zero() -> None:
    await _add_records(
        CostRecord(phase="develop", model="gpt-4o", cost_usd=Decimal("0.1"), tokens_saved=1_000_000),
    )

    cfg = MagicMock()
    cfg.llm.model = "gpt-4o"
    cfg.agent.model = "gpt-4o"

    with patch("sova.config.loader.load_config", return_value=cfg):
        async with await get_session() as session:
            summary = await cost_service.get_summary(session)

    assert summary["compression_savings_usd"] == Decimal("0")
