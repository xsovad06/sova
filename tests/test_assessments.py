"""Tests for sova.knowledge.assessments: local suitability record CRUD."""

from __future__ import annotations

import os

import pytest

from sova.db.session import close_db, get_session, init_db
from sova.knowledge.assessments import (
    clear_assessment,
    get_latest_assessment,
    resolve_suitability,
    set_assessment,
    shorthand_for,
)


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


class TestSuitabilityMapping:
    def test_resolve_known_shorthands(self) -> None:
        assert resolve_suitability("ready") == "ready"
        assert resolve_suitability("interactive") == "interactive_recommended"
        assert resolve_suitability("human-only") == "human_only"

    def test_resolve_unknown_shorthand_returns_none(self) -> None:
        assert resolve_suitability("bogus") is None

    def test_shorthand_for_known_values(self) -> None:
        assert shorthand_for("ready") == "ready"
        assert shorthand_for("interactive_recommended") == "interactive"
        assert shorthand_for("human_only") == "human-only"

    def test_shorthand_for_unknown_value_passes_through(self) -> None:
        assert shorthand_for("needs_spec") == "needs_spec"


class TestSetGetClear:
    async def test_get_with_no_rows_returns_none(self) -> None:
        async with await get_session() as session:
            record = await get_latest_assessment(session, issue_number="1", project_slug="owner/repo")
        assert record is None

    async def test_set_then_get_returns_latest(self) -> None:
        async with await get_session() as session:
            await set_assessment(
                session, issue_number="1", project_slug="owner/repo", suitability="ready", reasoning="r1"
            )
            record = await get_latest_assessment(session, issue_number="1", project_slug="owner/repo")
        assert record is not None
        assert record.suitability == "ready"

    async def test_second_set_wins_over_first(self) -> None:
        async with await get_session() as session:
            await set_assessment(
                session, issue_number="1", project_slug="owner/repo", suitability="human_only", reasoning="r1"
            )
            await set_assessment(
                session, issue_number="1", project_slug="owner/repo", suitability="ready", reasoning="r2"
            )
            record = await get_latest_assessment(session, issue_number="1", project_slug="owner/repo")
        assert record is not None
        assert record.suitability == "ready"

    async def test_get_scoped_to_project_slug(self) -> None:
        async with await get_session() as session:
            await set_assessment(
                session, issue_number="1", project_slug="other/repo", suitability="human_only", reasoning="r1"
            )
            record = await get_latest_assessment(session, issue_number="1", project_slug="owner/repo")
        assert record is None

    async def test_clear_removes_all_matching_rows(self) -> None:
        async with await get_session() as session:
            await set_assessment(
                session, issue_number="1", project_slug="owner/repo", suitability="human_only", reasoning="r1"
            )
            await set_assessment(
                session, issue_number="1", project_slug="owner/repo", suitability="ready", reasoning="r2"
            )
            count = await clear_assessment(session, issue_number="1", project_slug="owner/repo")
            record = await get_latest_assessment(session, issue_number="1", project_slug="owner/repo")
        assert count == 2
        assert record is None

    async def test_clear_with_no_rows_is_a_noop(self) -> None:
        async with await get_session() as session:
            count = await clear_assessment(session, issue_number="99", project_slug="owner/repo")
        assert count == 0
