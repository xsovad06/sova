"""The activity feed as narration (#1543): ``GET /feed/activity`` builds today's lines, what needs the user, the
background counts and the past periods from ``task_runs`` and today's feed events, with every sentence from the fixed
templates in ``feed_narration``."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.dashboard.services import (
    feed_activity_service,
    feed_narration,
    snapshot_service,
    work_history_service,
    work_title_service,
)
from sova.dashboard.services.feed_activity_service import build_activity, local_midnight, resolve_zone
from sova.dashboard.services.work_state import HistoryState, history_state_color
from sova.db.models import FeedEventRecord, StepExecution, TaskRun
from sova.db.session import get_session

pytestmark = pytest.mark.usefixtures("reset_db_rows")

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
MERGED = {"merged": True}


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with await get_session() as s:
        yield s


@pytest.fixture
async def client(dashboard_app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=dashboard_app), base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """No tracker is reached, and no snapshot or cached period survives from another test."""
    feed_activity_service.reset_activity_cache()
    snapshot_service.reset_snapshots()

    async def no_titles(*_a: object, **_k: object) -> tuple[dict, dict]:
        return {}, {}

    monkeypatch.setattr(work_history_service, "resolve_titles", no_titles)


async def add_run(
    session: AsyncSession,
    *,
    role: str = "developer",
    status: str = "done",
    at: datetime = NOW - timedelta(hours=2),
    issue: str | None = None,
    pr: int | None = None,
    cost: str = "0",
    error: str | None = None,
    handoff: dict | None = None,
    steps: tuple[str, ...] = (),
) -> TaskRun:
    run = TaskRun(
        role=role,
        status=status,
        issue_number=issue,
        pr_number=pr,
        total_cost_usd=Decimal(cost),
        current_step="complete",
        started_at=at,
        ended_at=None if status == "running" else at + timedelta(minutes=10),
        error_message=error,
        handoff_json=handoff,
    )
    session.add(run)
    await session.flush()
    for name in steps:
        session.add(StepExecution(task_run_id=run.id, step_name=name, status="done", started_at=at))
    await session.commit()
    return run


async def add_event(
    session: AsyncSession, category: str, *, at: datetime = NOW - timedelta(hours=1), **meta: object
) -> None:
    session.add(
        FeedEventRecord(
            severity="info", title=f"{category} event", category=category, metadata_json=meta, created_at=at
        )
    )
    await session.commit()


def by_key(payload: dict, section: str = "today") -> dict[str, dict]:
    return {entry["key"]: entry for entry in payload[section]}


class TestNarration:
    @pytest.mark.parametrize(
        ("review", "address", "expected"),
        [
            (0, 0, "Merged."),
            (2, 1, "Merged. 2 review and 1 address round."),
            (1, 2, "Merged. 1 review and 2 address rounds."),
            (0, 2, "Merged after 2 address rounds."),
            (1, 0, "Merged after 1 review round."),
        ],
    )
    def test_merged_says_how_many_rounds_it_took(self, review: int, address: int, expected: str) -> None:
        assert feed_narration.merged(review, address) == expected

    @pytest.mark.parametrize(
        ("action", "minutes", "expected"),
        [
            ("research", 12, "Researching it now, 12 min in."),
            ("develop", 0, "Working on it now, just started."),
            ("review", 65, "Reviewing it now, 1 h 5 min in."),
            ("review", 120, "Reviewing it now, 2 h in."),
            ("health-audit", 3, "Running a task on it now, 3 min in."),
        ],
    )
    def test_a_running_line_names_the_work_and_the_elapsed_time(self, action: str, minutes: int, expected: str) -> None:
        assert feed_narration.running(action, minutes) == expected

    def test_a_long_failure_reason_is_cut_to_one_line(self) -> None:
        text = feed_narration.failed("x" * 400 + ". Second sentence.")
        assert text.startswith("I need you: it failed, xxx")
        assert text.endswith("...")
        assert not text.endswith("....")
        assert "Second" not in text

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            ("Failed at Develop: Tests failed. See the log.", "I need you: it failed, tests failed."),
            ("Failed: boom", "I need you: it failed, boom."),
            ("failed", "I need you: it failed."),
            ("CI broke on the lint job", "I need you: it failed, CI broke on the lint job."),
            ("Tests failed", "I need you: it failed, tests failed."),
        ],
    )
    def test_a_failure_reason_reads_mid_sentence_without_repeating_failed(self, reason: str, expected: str) -> None:
        assert feed_narration.failed(reason) == expected

    @pytest.mark.parametrize(
        ("action", "pr", "expected"),
        [
            ("develop", 60, "Opened PR #60."),
            ("develop", None, "Developed it."),
            ("rebase", 60, "Rebased it."),
            ("integrate", 60, "Finished a run."),
        ],
    )
    def test_a_kept_line_says_what_the_run_did_not_where_the_issue_stands(
        self, action: str, pr: int | None, expected: str
    ) -> None:
        assert feed_narration.did(action, pr) == expected

    def test_a_failure_with_no_reason_still_asks_for_the_user(self) -> None:
        assert feed_narration.failed("") == "I need you: it failed."

    def test_no_sentence_has_an_emoji_or_a_double_dash(self) -> None:
        sentences = [
            *(feed_narration.settled(s, 7, 1, 1) for s in HistoryState),
            feed_narration.failed("boom"),
            feed_narration.failed("x" * 400),
            feed_narration.did("develop", 3),
            feed_narration.did("rebase", None),
            feed_narration.needs_approval(),
            feed_narration.review_exhausted(),
            feed_narration.watchdog_kill("no_output"),
            feed_narration.running("develop", 4),
            feed_narration.nothing_today(),
            feed_narration.background_line("rate_limit", 2),
            feed_narration.background_line("whatever", 1),
            feed_narration.period("Yesterday", 5, 3, 1, 4.2),
            feed_narration.background_summary(1),
            feed_narration.background_summary(4),
            *feed_narration.labels().values(),
        ]
        for sentence in sentences:
            assert "--" not in sentence
            assert re.fullmatch(r"[\x20-\x7e]+", sentence), sentence


class TestPeriodLabels:
    def test_every_period_label_is_worded_in_the_narration_module(self) -> None:
        assert feed_narration.period_label("yesterday") == "Yesterday"
        assert feed_narration.period_label("this_week") == "Earlier this week"
        assert feed_narration.period_label("weeks", weeks=1) == "Last week"
        assert feed_narration.period_label("weeks", weeks=3) == "3 weeks ago"
        assert feed_narration.period_label("month", month=6) == "June"
        assert feed_narration.period_label("month", month=12, year=2025) == "December 2025"

    def test_month_names_do_not_follow_the_locale(self) -> None:
        import locale

        old = locale.setlocale(locale.LC_TIME)
        # Whichever non-English locale the machine has: none installed means the check has nothing to disturb.
        for name in ("sk_SK.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8"):
            try:
                locale.setlocale(locale.LC_TIME, name)
                break
            except locale.Error:
                continue
        try:
            labels = [r[1] for r in feed_activity_service._period_ranges(date(2026, 3, 15), timezone.utc)]
        finally:
            locale.setlocale(locale.LC_TIME, old)
        assert "February" in labels
        assert "December 2025" in labels


class TestActionWording:
    @pytest.mark.parametrize(("ref", "label"), [("14", "#14"), ("pr:3", "PR #3"), ("PROJ-7", "PROJ-7")])
    def test_a_ref_reads_as_an_issue_or_a_pr_never_as_a_raw_key(self, ref: str, label: str) -> None:
        assert feed_narration.ref_label(ref) == label
        assert feed_narration.action_confirm("Merge", ref) == f"Merge on {label}?"

    def test_an_offered_action_carries_its_own_words(self) -> None:
        offered = feed_activity_service._offered_action({"id": "merge", "label": "Merge", "style": "danger"}, "pr:3")
        assert offered is not None
        assert offered["confirm_text"] == "Merge on PR #3?"
        assert offered["started_text"] == "Merge started"
        assert offered["failed_text"] == "Could not run Merge"

    def test_the_fixed_labels_include_the_load_failure_and_the_badge_wording(self) -> None:
        labels = feed_narration.labels()
        assert labels["load_failed"]
        assert "{count}" in labels["badge_unread"]


class TestZone:
    @pytest.mark.parametrize("name", [None, "", "Nope/Zone", "../../etc/passwd", "x" * 500])
    def test_a_missing_or_unknown_zone_reads_as_utc(self, name: str | None) -> None:
        assert resolve_zone(name) is timezone.utc

    def test_a_day_edge_is_a_local_midnight_not_a_fixed_offset(self) -> None:
        zone = resolve_zone("America/New_York")
        # The 1 Nov 2026 day has 25 hours: it starts at EDT midnight and the next day at EST midnight.
        assert local_midnight(datetime(2026, 11, 1).date(), zone) == datetime(2026, 11, 1, 4, tzinfo=timezone.utc)
        assert local_midnight(datetime(2026, 11, 2).date(), zone) == datetime(2026, 11, 2, 5, tzinfo=timezone.utc)


class TestToday:
    async def test_an_issue_with_several_runs_is_one_entry_following_its_newest_state(
        self, session: AsyncSession
    ) -> None:
        await add_run(session, issue="7", pr=70, at=NOW - timedelta(hours=5))
        await add_run(
            session, role="reviewer", issue="7", at=NOW - timedelta(hours=4), handoff={"next_action": "address_review"}
        )
        await add_run(session, issue="7", pr=70, at=NOW - timedelta(hours=3), steps=("address_review",))
        payload = await build_activity(None, "UTC", now=NOW)
        assert [e["key"] for e in payload["today"]] == ["issue:7"]
        assert payload["today"][0]["state"] == "waiting_review"
        assert payload["today"][0]["text"] == "PR #70 is up and waiting for review."
        assert payload["attention"] == []

    async def test_a_merged_issue_counts_review_and_address_rounds_over_its_whole_history(
        self, session: AsyncSession
    ) -> None:
        days_ago = NOW - timedelta(days=3)
        await add_run(session, issue="9", pr=90, at=days_ago)
        await add_run(session, role="reviewer", issue="9", at=days_ago + timedelta(hours=1))
        await add_run(session, role="command:address-pr", issue="9", at=days_ago + timedelta(hours=2))
        await add_run(session, role="reviewer", issue="9", at=NOW - timedelta(hours=3))
        await add_run(
            session, role="command:integrate-pr", issue="9", at=NOW - timedelta(hours=1), handoff=MERGED, cost="1.25"
        )
        payload = await build_activity(None, "UTC", now=NOW)
        assert by_key(payload)["issue:9"]["text"] == "Merged. 2 review and 1 address round."
        # Today's cost: the two runs of today, not the ones from three days ago.
        assert Decimal(by_key(payload)["issue:9"]["cost_usd"]) == Decimal("1.25")
        assert by_key(payload)["issue:9"]["state_color"] == "green-strong"

    async def test_a_review_that_asked_for_changes_with_no_address_run_after_it_is_waiting_for_fixes(
        self, session: AsyncSession
    ) -> None:
        await add_run(session, issue="7", pr=70, at=NOW - timedelta(hours=5))
        await add_run(
            session, role="reviewer", issue="7", at=NOW - timedelta(hours=4), handoff={"next_action": "address_review"}
        )
        payload = await build_activity(None, "UTC", now=NOW)
        entry = by_key(payload)["issue:7"]
        assert entry["state"] == "waiting_fixes"
        assert entry["text"] == "The review asked for changes. Fixing them is next."
        assert payload["attention"] == []

    async def test_a_review_that_failed_or_stopped_is_not_a_round(self, session: AsyncSession) -> None:
        earlier = NOW - timedelta(days=2)
        await add_run(session, issue="9", pr=90, at=earlier)
        await add_run(session, role="reviewer", issue="9", status="failed", at=earlier + timedelta(hours=1))
        await add_run(session, role="reviewer", issue="9", status="stopped", at=earlier + timedelta(hours=2))
        await add_run(session, role="command:address-pr", issue="9", status="failed", at=earlier + timedelta(hours=3))
        await add_run(session, role="reviewer", issue="9", at=earlier + timedelta(hours=4))
        await add_run(session, role="command:integrate-pr", issue="9", at=NOW - timedelta(hours=1), handoff=MERGED)
        assert by_key(await build_activity(None, "UTC", now=NOW))["issue:9"]["text"] == "Merged after 1 review round."

    async def test_a_run_still_going_reads_as_in_progress_with_the_elapsed_time(self, session: AsyncSession) -> None:
        await add_run(session, role="researcher", status="running", issue="3", at=NOW - timedelta(minutes=12))
        payload = await build_activity(None, "UTC", now=NOW)
        entry = by_key(payload)["issue:3"]
        assert entry["text"] == "Researching it now, 12 min in."
        assert entry["running"] is True
        assert entry["state"] == "agent_running"
        assert entry["cost_usd"] is None
        assert entry["started_at"].startswith("2026-11-23T11:48")
        assert len(payload["today"]) == 1

    async def test_a_running_run_wins_over_the_groups_finished_runs(self, session: AsyncSession) -> None:
        await add_run(session, issue="4", pr=40, at=NOW - timedelta(hours=3))
        await add_run(session, role="reviewer", status="running", issue="4", at=NOW - timedelta(minutes=2))
        payload = await build_activity(None, "UTC", now=NOW)
        assert [(e["key"], e["running"]) for e in payload["today"]] == [("issue:4", True)]

    async def test_a_run_with_no_issue_is_one_entry_keyed_by_its_pr(self, session: AsyncSession) -> None:
        await add_run(session, role="command:integrate-pr", pr=55, handoff=MERGED)
        entry = by_key(await build_activity(None, "UTC", now=NOW))["pr:55"]
        assert (entry["issue_number"], entry["pr_number"], entry["number"]) == (None, 55, "55")

    async def test_entries_are_newest_first(self, session: AsyncSession) -> None:
        await add_run(session, issue="1", at=NOW - timedelta(hours=6))
        await add_run(session, issue="2", at=NOW - timedelta(hours=1))
        await add_run(session, issue="3", at=NOW - timedelta(hours=3))
        payload = await build_activity(None, "UTC", now=NOW)
        assert [e["key"] for e in payload["today"]] == ["issue:2", "issue:3", "issue:1"]

    async def test_a_run_from_before_local_midnight_is_not_today(self, session: AsyncSession) -> None:
        # 04:30 UTC on 2 Nov is 23:30 on 1 Nov in New York, where that 25-hour day ends at 05:00 UTC.
        await add_run(session, issue="5", at=datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc))
        payload = await build_activity(None, "America/New_York", now=datetime(2026, 11, 2, 15, tzinfo=timezone.utc))
        assert payload["today"] == []
        assert [p["key"] for p in payload["periods"]] == ["yesterday"]
        utc = await build_activity(None, "UTC", now=datetime(2026, 11, 2, 15, tzinfo=timezone.utc))
        assert [e["key"] for e in utc["today"]] == ["issue:5"]

    async def test_no_activity_gives_empty_sections_and_the_empty_line(self) -> None:
        payload = await build_activity(None, "UTC", now=NOW)
        assert (payload["today"], payload["attention"], payload["background"], payload["periods"]) == ([], [], [], [])
        assert payload["empty_today_text"] == "Nothing yet today."
        assert payload["tz"] == "UTC"

    async def test_an_unknown_zone_is_utc_not_an_error(self, session: AsyncSession) -> None:
        await add_run(session, issue="1")
        payload = await build_activity(None, "Nope/Zone", now=NOW)
        assert payload["tz"] == "UTC"
        assert len(payload["today"]) == 1


class TestAttention:
    async def test_a_failed_run_is_attention_only(self, session: AsyncSession) -> None:
        run = await add_run(session, issue="6", status="failed", error="Tests failed. See the log.")
        payload = await build_activity(None, "UTC", now=NOW)
        assert payload["today"] == []
        [item] = payload["attention"]
        assert (item["kind"], item["identity"], item["number"]) == ("failed", f"failed:issue:6:run:{run.id}", "6")
        assert item["text"] == "I need you: it failed, tests failed."
        assert item["action"] is None

    async def test_a_second_failure_on_the_same_issue_is_a_new_identity(self, session: AsyncSession) -> None:
        await add_run(session, issue="6", status="failed", at=NOW - timedelta(hours=4))
        [first] = (await build_activity(None, "UTC", now=NOW))["attention"]
        await add_run(session, issue="6", status="failed", at=NOW - timedelta(hours=1))
        [second] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert first["key"] == second["key"]
        assert first["identity"] != second["identity"]

    async def test_a_failed_issue_with_another_finished_run_today_keeps_a_line_that_skips_the_failure(
        self, session: AsyncSession
    ) -> None:
        await add_run(session, issue="6", pr=60, at=NOW - timedelta(hours=4))
        await add_run(session, role="reviewer", issue="6", status="failed", at=NOW - timedelta(hours=2), error="boom")
        payload = await build_activity(None, "UTC", now=NOW)
        assert by_key(payload)["issue:6"]["text"] == "Opened PR #60."
        assert "fail" not in by_key(payload)["issue:6"]["text"]
        assert [a["kind"] for a in payload["attention"]] == ["failed"]

    async def test_a_failed_group_with_an_earlier_successful_develop_run_today_keeps_that_line(
        self, session: AsyncSession
    ) -> None:
        await add_run(session, issue="6", pr=60, status="done", at=NOW - timedelta(hours=4))
        await add_run(session, issue="6", status="failed", at=NOW - timedelta(hours=2), error="boom")
        payload = await build_activity(None, "UTC", now=NOW)
        # A past-tense line with a neutral state: the failure after it is what the attention line says.
        assert (by_key(payload)["issue:6"]["state"], by_key(payload)["issue:6"]["text"]) == (
            "finished",
            "Opened PR #60.",
        )

    async def test_a_spec_waiting_for_approval_is_attention_only(self, session: AsyncSession) -> None:
        await add_run(session, role="researcher", status="awaiting_approval", issue="8")
        payload = await build_activity(None, "UTC", now=NOW)
        assert payload["today"] == []
        assert [(a["kind"], a["text"]) for a in payload["attention"]] == [
            ("needs_approval", "I need you: the spec is ready and waits for your approval.")
        ]

    async def test_a_failed_issue_being_retried_is_in_progress_not_attention(self, session: AsyncSession) -> None:
        await add_run(session, issue="6", status="failed", at=NOW - timedelta(hours=3))
        await add_run(session, status="running", issue="6", at=NOW - timedelta(minutes=5))
        payload = await build_activity(None, "UTC", now=NOW)
        assert payload["attention"] == []
        assert [e["running"] for e in payload["today"]] == [True]

    async def test_a_waiting_handoff_action_becomes_the_items_button(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await add_run(session, role="researcher", status="awaiting_approval", issue="8")
        handoffs = [
            {"issue": "99", "next_actions": [{"id": "other", "label": "Other"}]},
            {
                "issue": "8",
                "next_actions": [
                    {"id": "shell-thing", "label": "Shell", "mode": "shell"},
                    {"id": "approve-spec", "label": "Approve spec", "style": "approve", "mode": "agent"},
                ],
            },
        ]
        monkeypatch.setattr(
            feed_activity_service,
            "peek_snapshot",
            lambda kind, _dir: SimpleNamespace(payload={"handoffs": handoffs}) if kind == "handoff" else None,
        )
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert item["action"] == {
            "id": "approve-spec",
            "label": "Approve spec",
            "ref": "8",
            "confirm": False,
            "confirm_text": "Approve spec on #8?",
            "started_text": "Approve spec started",
            "failed_text": "Could not run Approve spec",
        }

    async def test_a_merge_action_asks_for_confirmation(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await add_run(session, status="failed", pr=12)
        handoffs = [
            {"pr_number": 12, "next_actions": [{"id": "merge", "label": "Merge", "command": "/integrate-pr 12"}]}
        ]
        monkeypatch.setattr(
            feed_activity_service, "peek_snapshot", lambda kind, _d: SimpleNamespace(payload={"handoffs": handoffs})
        )
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert item["action"] == {
            "id": "merge",
            "label": "Merge",
            "ref": "pr:12",
            "confirm": True,
            "confirm_text": "Merge on PR #12?",
            "started_text": "Merge started",
            "failed_text": "Could not run Merge",
        }

    async def test_a_malformed_handoff_snapshot_gives_no_button_and_does_not_raise(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await add_run(session, status="failed", issue="2")
        monkeypatch.setattr(
            feed_activity_service,
            "peek_snapshot",
            lambda kind, _d: SimpleNamespace(payload={"handoffs": ["junk", {"issue": "2", "next_actions": "no"}]}),
        )
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert item["action"] is None

    async def test_a_review_budget_that_ran_out_comes_from_the_work_items_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.work_items(
            monkeypatch,
            [
                {"issue_number": 31, "pr_number": 310, "state": "pr_review_exhausted"},
                {"issue_number": 32, "pr_number": 320, "state": "pr_awaiting_review"},
            ],
        )
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert (item["kind"], item["key"], item["pr_number"]) == ("review_exhausted", "issue:31", 310)
        assert item["identity"] == "review_exhausted:issue:31:pr:310"
        assert item["text"] == "I need you: the review budget ran out."

    @staticmethod
    def work_items(monkeypatch: pytest.MonkeyPatch, items: list[dict] | None) -> None:
        """Make the last work-items snapshot ``items``, or absent (None)."""
        monkeypatch.setattr(
            feed_activity_service,
            "peek_snapshot",
            lambda kind, _d: SimpleNamespace(payload={"items": items}) if kind == "work_items" and items else None,
        )

    async def test_an_exhausted_group_has_no_today_line_that_contradicts_the_attention(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await add_run(
            session,
            role="reviewer",
            issue="31",
            pr=310,
            at=NOW - timedelta(hours=2),
            handoff={"next_action": "address_review"},
        )
        self.work_items(monkeypatch, [{"issue_number": 31, "pr_number": 310, "state": "pr_review_exhausted"}])
        payload = await build_activity(None, "UTC", now=NOW)
        assert [a["kind"] for a in payload["attention"]] == ["review_exhausted"]
        assert payload["today"] == []

    async def test_an_exhausted_group_keeps_only_the_line_of_a_run_of_another_kind(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await add_run(session, issue="31", pr=310, at=NOW - timedelta(hours=4))
        await add_run(
            session, role="reviewer", issue="31", at=NOW - timedelta(hours=2), handoff={"next_action": "address_review"}
        )
        self.work_items(monkeypatch, [{"issue_number": 31, "pr_number": 310, "state": "pr_review_exhausted"}])
        payload = await build_activity(None, "UTC", now=NOW)
        assert by_key(payload)["issue:31"]["text"] == "Opened PR #310."
        assert [a["kind"] for a in payload["attention"]] == ["review_exhausted"]

    async def test_a_killed_group_has_no_today_line_that_restates_the_kill(self, session: AsyncSession) -> None:
        run = await add_run(session, issue="14", status="stopped")
        await add_event(session, "watchdog", run_id=run.id, signal="no_output", action="kill")
        payload = await build_activity(None, "UTC", now=NOW)
        assert [a["kind"] for a in payload["attention"]] == ["watchdog_kill"]
        assert payload["today"] == []

    async def test_a_kill_is_coloured_by_the_work_state_token_for_a_failure(self, session: AsyncSession) -> None:
        run = await add_run(session, issue="14", status="failed")
        await add_event(session, "watchdog", run_id=run.id, signal="no_output", action="kill")
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert item["color"] == history_state_color(HistoryState.FAILED)
        assert item["identity"] == f"watchdog_kill:issue:14:run:{run.id}"

    async def test_a_review_budget_is_kept_when_the_snapshot_is_gone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A write drops the snapshot (and a fresh server has none): the item last seen must not vanish and re-badge.
        items = [{"issue_number": 31, "pr_number": 310, "state": "pr_review_exhausted"}]
        self.work_items(monkeypatch, items)
        [before] = (await build_activity(None, "UTC", now=NOW))["attention"]
        self.work_items(monkeypatch, None)
        [after] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert after["identity"] == before["identity"]
        # A snapshot that no longer lists it ends it.
        self.work_items(monkeypatch, [{"issue_number": 31, "state": "pr_awaiting_review"}])
        assert (await build_activity(None, "UTC", now=NOW))["attention"] == []

    async def test_an_exhausted_group_with_a_run_going_is_in_progress_not_attention(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await add_run(session, role="command:address-pr", status="running", issue="31", at=NOW - timedelta(minutes=3))
        self.work_items(monkeypatch, [{"issue_number": 31, "pr_number": 310, "state": "pr_review_exhausted"}])
        payload = await build_activity(None, "UTC", now=NOW)
        assert payload["attention"] == []
        assert [e["running"] for e in payload["today"]] == [True]

    async def test_an_exhausted_pr_with_no_issue_joins_the_issue_its_runs_belong_to(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await add_run(session, issue="50", pr=500, status="failed", at=NOW - timedelta(hours=2))
        self.work_items(monkeypatch, [{"issue_number": None, "pr_number": 500, "state": "pr_review_exhausted"}])
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert (item["kind"], item["key"], item["issue_number"], item["pr_number"]) == (
            "review_exhausted",
            "issue:50",
            "50",
            500,
        )

    async def test_an_exhausted_pr_nothing_links_to_an_issue_is_keyed_by_the_pr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.work_items(monkeypatch, [{"issue_number": None, "pr_number": 501, "state": "pr_review_exhausted"}])
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert (item["key"], item["issue_number"]) == ("pr:501", None)

    async def test_a_failure_and_an_approval_wait_from_yesterday_stay_in_attention_not_today(
        self, session: AsyncSession
    ) -> None:
        yesterday = NOW - timedelta(hours=26)
        failed = await add_run(session, issue="40", status="failed", at=yesterday, error="boom")
        waiting = await add_run(session, role="researcher", status="awaiting_approval", issue="41", at=yesterday)
        payload = await build_activity(None, "UTC", now=NOW)
        assert payload["today"] == []
        assert {a["key"]: a["identity"] for a in payload["attention"]} == {
            "issue:40": f"failed:issue:40:run:{failed.id}",
            "issue:41": f"needs_approval:issue:41:run:{waiting.id}",
        }

    async def test_a_failure_that_a_later_run_settled_is_not_attention(self, session: AsyncSession) -> None:
        await add_run(session, issue="40", status="failed", at=NOW - timedelta(days=3))
        await add_run(session, issue="40", pr=400, at=NOW - timedelta(days=2))
        assert (await build_activity(None, "UTC", now=NOW))["attention"] == []

    async def test_an_unsettled_failure_expires_after_the_attention_window_and_not_before(
        self, session: AsyncSession
    ) -> None:
        """The one explicit expiry (``_ATTENTION_DAYS``): a failure nothing settled stays until then, and no longer."""
        days = feed_activity_service._ATTENTION_DAYS
        await add_run(session, issue="41", status="failed", at=NOW - timedelta(days=days - 1))
        await add_run(session, issue="42", status="failed", at=NOW - timedelta(days=days + 2))
        assert [a["key"] for a in (await build_activity(None, "UTC", now=NOW))["attention"]] == ["issue:41"]

    async def test_a_watchdog_kill_is_attention_and_replaces_the_plain_failure(self, session: AsyncSession) -> None:
        run = await add_run(session, issue="14", status="failed", error="killed")
        await add_event(session, "watchdog", run_id=run.id, signal="no_output", action="kill")
        payload = await build_activity(None, "UTC", now=NOW)
        [item] = payload["attention"]
        assert item["kind"] == "watchdog_kill"
        assert item["text"] == "I need you: I stopped a stuck run, no output."
        assert item["issue_number"] == "14"

    async def test_a_kill_of_an_issue_that_since_did_well_is_not_attention(self, session: AsyncSession) -> None:
        killed = await add_run(session, issue="14", status="failed", at=NOW - timedelta(hours=5))
        await add_run(session, issue="14", pr=140, at=NOW - timedelta(hours=1))
        await add_event(session, "watchdog", run_id=killed.id, signal="no_output", action="kill")
        assert (await build_activity(None, "UTC", now=NOW))["attention"] == []


class TestKillAttention:
    async def test_a_later_failure_on_a_killed_issue_is_its_own_line_with_its_own_identity(
        self, session: AsyncSession
    ) -> None:
        killed = await add_run(session, issue="14", status="stopped", at=NOW - timedelta(hours=5))
        await add_event(session, "watchdog", run_id=killed.id, signal="no_output", action="kill")
        [kill] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert kill["kind"] == "watchdog_kill"
        later = await add_run(session, issue="14", status="failed", at=NOW - timedelta(hours=1), error="boom")
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert (item["kind"], item["identity"]) == ("failed", f"failed:issue:14:run:{later.id}")
        assert item["identity"] != kill["identity"]
        assert item["text"] == "I need you: it failed, boom."

    async def test_a_kill_from_before_midnight_whose_run_ended_stopped_is_still_attention(
        self, session: AsyncSession
    ) -> None:
        run = await add_run(session, issue="14", status="stopped", at=NOW - timedelta(hours=26))
        await add_event(session, "watchdog", at=NOW - timedelta(hours=25), run_id=run.id, signal="x", action="kill")
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert (item["kind"], item["key"]) == ("watchdog_kill", "issue:14")

    async def test_a_kill_older_than_the_window_is_not_offered(self, session: AsyncSession) -> None:
        old = NOW - timedelta(days=feed_activity_service._ATTENTION_DAYS + 3)
        run = await add_run(session, issue="14", status="stopped", at=old)
        await add_event(session, "watchdog", at=old, run_id=run.id, signal="x", action="kill")
        assert (await build_activity(None, "UTC", now=NOW))["attention"] == []

    async def test_a_kill_with_no_run_does_not_collide_with_a_lone_run_group(self, session: AsyncSession) -> None:
        lone = await add_run(session, status="failed", error="boom")
        # An event id that equals the lone run's id: different id spaces, so different keys.
        session.add(
            FeedEventRecord(
                id=lone.id,
                severity="info",
                title="kill",
                category="watchdog",
                metadata_json={"action": "kill", "signal": "no_output"},
                created_at=NOW - timedelta(hours=1),
            )
        )
        await session.commit()
        items = {a["key"]: a for a in (await build_activity(None, "UTC", now=NOW))["attention"]}
        assert set(items) == {f"run:{lone.id}", f"event:{lone.id}"}
        assert items[f"run:{lone.id}"]["kind"] == "failed"
        assert items[f"event:{lone.id}"]["kind"] == "watchdog_kill"
        assert items[f"event:{lone.id}"]["number"] is None

    async def test_a_kill_whose_row_stays_running_is_attention_and_not_a_running_line(
        self, session: AsyncSession
    ) -> None:
        await add_run(session, issue="14", status="failed", at=NOW - timedelta(hours=6), error="boom")
        killed = await add_run(session, issue="14", status="running", at=NOW - timedelta(hours=2))
        await add_event(session, "watchdog", run_id=killed.id, signal="no_output", action="kill")
        payload = await build_activity(None, "UTC", now=NOW)
        [item] = payload["attention"]
        assert (item["kind"], item["identity"]) == ("watchdog_kill", f"watchdog_kill:issue:14:run:{killed.id}")
        assert all(not e["running"] for e in payload["today"])

    async def test_a_kill_of_a_non_terminal_row_with_an_older_finished_success_is_still_shown(
        self, session: AsyncSession
    ) -> None:
        await add_run(session, issue="14", status="done", at=NOW - timedelta(hours=6))
        killed = await add_run(session, issue="14", status="running", at=NOW - timedelta(hours=2))
        await add_event(session, "watchdog", run_id=killed.id, signal="no_output", action="kill")
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert item["kind"] == "watchdog_kill"

    async def test_a_kill_of_a_non_terminal_row_is_settled_by_a_later_finished_run(self, session: AsyncSession) -> None:
        killed = await add_run(session, issue="14", status="running", at=NOW - timedelta(hours=5))
        await add_event(session, "watchdog", run_id=killed.id, signal="no_output", action="kill")
        await add_run(session, issue="14", status="done", at=NOW - timedelta(hours=1))
        assert (await build_activity(None, "UTC", now=NOW))["attention"] == []

    async def test_a_kill_naming_its_run_by_a_string_id_is_judged_like_any_other(self, session: AsyncSession) -> None:
        killed = await add_run(session, issue="14", status="stopped", at=NOW - timedelta(hours=5))
        await add_event(session, "watchdog", run_id=str(killed.id), signal="no_output", action="kill")
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert (item["key"], item["identity"]) == ("issue:14", f"watchdog_kill:issue:14:run:{killed.id}")
        await add_run(session, issue="14", status="done", at=NOW - timedelta(hours=1))
        assert (await build_activity(None, "UTC", now=NOW))["attention"] == []

    async def test_a_kill_followed_by_a_run_still_going_is_in_progress_not_attention(
        self, session: AsyncSession
    ) -> None:
        killed = await add_run(session, issue="14", status="stopped", at=NOW - timedelta(hours=5))
        await add_event(session, "watchdog", run_id=killed.id, signal="no_output", action="kill")
        await add_run(session, issue="14", status="running", at=NOW - timedelta(minutes=5))
        payload = await build_activity(None, "UTC", now=NOW)
        assert payload["attention"] == []
        assert [e["running"] for e in payload["today"]] == [True]

    async def test_a_kill_followed_by_a_stale_running_row_stays_in_attention(self, session: AsyncSession) -> None:
        killed = await add_run(session, issue="14", status="stopped", at=NOW - timedelta(hours=30))
        await add_event(session, "watchdog", run_id=killed.id, signal="no_output", action="kill")
        await add_run(session, issue="14", status="running", at=NOW - timedelta(hours=20))
        payload = await build_activity(None, "UTC", now=NOW)
        [item] = payload["attention"]
        assert item["kind"] == "watchdog_kill"
        assert all(not e["running"] for e in payload["today"])

    async def test_a_kill_with_no_run_expires_sooner_than_one_a_later_run_can_settle(
        self, session: AsyncSession
    ) -> None:
        await add_event(session, "watchdog", at=NOW - timedelta(days=1), signal="x", action="kill")
        await add_event(session, "watchdog", at=NOW - timedelta(days=5), signal="x", action="kill")
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert item["kind"] == "watchdog_kill"

    async def test_a_warning_is_not_read_as_a_kill(self, session: AsyncSession) -> None:
        run = await add_run(session, issue="14", status="stopped", at=NOW - timedelta(hours=5))
        await add_event(session, "watchdog", run_id=run.id, signal="no_output", action="warn")
        assert (await build_activity(None, "UTC", now=NOW))["attention"] == []


class TestFailureAfterExhaustedBudget:
    async def test_a_failure_after_the_budget_ran_out_is_its_own_line_and_identity(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await add_run(session, role="command:address-pr", issue="31", pr=310, at=NOW - timedelta(hours=5))
        TestAttention.work_items(monkeypatch, [{"issue_number": 31, "pr_number": 310, "state": "pr_review_exhausted"}])
        [before] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert before["kind"] == "review_exhausted"
        later = await add_run(
            session, role="command:integrate-pr", issue="31", pr=310, status="failed", at=NOW - timedelta(hours=1)
        )
        [after] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert (after["kind"], after["identity"]) == ("failed", f"failed:issue:31:run:{later.id}")
        assert after["identity"] != before["identity"]


class TestUnsettledKeys:
    async def test_stopped_groups_do_not_use_up_the_cap(self, session: AsyncSession) -> None:
        await add_run(session, issue="1", status="failed", at=NOW - timedelta(hours=9))
        for n in range(2, 6):
            await add_run(session, issue=str(n), status="stopped", at=NOW - timedelta(hours=n))
        keys = await work_history_service.unsettled_keys(session, NOW - timedelta(days=2), 1)
        assert keys == ["issue:1"]


class TestOneDefinitionOfFinished:
    def test_the_history_and_the_feed_share_one_terminal_set(self) -> None:
        from sova.core.state import TASK_RUN_TERMINAL
        from sova.dashboard.services import work_service

        assert work_service._TERMINAL == TASK_RUN_TERMINAL
        assert "awaiting_approval" in TASK_RUN_TERMINAL

    async def test_a_research_run_awaiting_approval_is_attention_and_not_a_running_line(
        self, session: AsyncSession
    ) -> None:
        await add_run(session, role="researcher", status="awaiting_approval", issue="8", at=NOW - timedelta(hours=3))
        payload = await build_activity(None, "UTC", now=NOW)
        assert [a["kind"] for a in payload["attention"]] == ["needs_approval"]
        assert all(not e["running"] for e in payload["today"])


class TestStaleAndMergedStates:
    async def test_a_days_old_running_row_is_not_working_and_does_not_hide_the_failure(
        self, session: AsyncSession
    ) -> None:
        await add_run(session, issue="9", status="failed", at=NOW - timedelta(hours=40), error="boom")
        await add_run(session, issue="9", status="running", at=NOW - timedelta(hours=30))
        payload = await build_activity(None, "UTC", now=NOW)
        assert [a["kind"] for a in payload["attention"]] == ["failed"]
        assert all(not e["running"] for e in payload["today"])

    async def test_a_recent_running_row_still_hides_the_failure(self, session: AsyncSession) -> None:
        await add_run(session, issue="9", status="failed", at=NOW - timedelta(hours=3), error="boom")
        await add_run(session, issue="9", status="running", at=NOW - timedelta(minutes=5))
        payload = await build_activity(None, "UTC", now=NOW)
        assert payload["attention"] == []
        assert [e["running"] for e in payload["today"]] == [True]

    async def test_an_exhausted_item_of_a_group_that_merged_is_dropped_and_forgotten(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        items = [{"issue_number": 31, "pr_number": 310, "state": "pr_review_exhausted"}]
        TestAttention.work_items(monkeypatch, items)
        [before] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert before["kind"] == "review_exhausted"
        # The merge write drops the snapshot, and the group has merged since.
        await add_run(session, role="command:integrate-pr", issue="31", pr=310, handoff=MERGED)
        TestAttention.work_items(monkeypatch, None)
        payload = await build_activity(None, "UTC", now=NOW)
        assert payload["attention"] == []
        assert by_key(payload)["issue:31"]["text"] == "Merged."
        # Forgotten: with the snapshot still absent, a later read has no budget left to offer.
        assert (await build_activity(None, "UTC", now=NOW))["attention"] == []

    async def test_the_exhausted_identity_survives_an_unrelated_later_run(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await add_run(session, role="command:address-pr", issue="31", pr=310, at=NOW - timedelta(hours=3))
        TestAttention.work_items(monkeypatch, [{"issue_number": 31, "pr_number": 310, "state": "pr_review_exhausted"}])
        [before] = (await build_activity(None, "UTC", now=NOW))["attention"]
        await add_run(session, role="command:rebase", issue="31", pr=310, at=NOW - timedelta(hours=1))
        [after] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert after["identity"] == before["identity"]
        assert before["identity"].startswith("review_exhausted:issue:31:pr:310:run:")


class TestNoModelCalls:
    def test_the_modules_do_not_import_a_provider_or_runtime(self) -> None:
        """A module that bound ``get_provider`` by name would bypass the patches below, so the imports are checked."""
        import inspect

        for module in (feed_activity_service, feed_narration):
            source = inspect.getsource(module)
            assert not re.search(r"^\s*(from|import)\s+sova\.(llm|core\.agent_dispatch)", source, re.MULTILINE)
            assert not {"get_provider", "get_runtime", "dispatch_prompt"} & set(vars(module))

    async def test_building_the_payload_calls_no_provider_or_runtime(
        self, session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.core import agent_dispatch
        from sova.llm import client as llm_client

        def forbidden(*_a: object, **_k: object) -> None:
            raise AssertionError("the activity payload must not call a model")

        monkeypatch.setattr(llm_client, "get_provider", forbidden)
        monkeypatch.setattr(agent_dispatch, "get_runtime", forbidden)
        monkeypatch.setattr(agent_dispatch, "create_runtime", forbidden)
        monkeypatch.setattr(agent_dispatch, "dispatch_runtime_prompt", forbidden)
        monkeypatch.setattr(agent_dispatch, "dispatch_prompt", forbidden)
        # A fixed clock, never the wall clock: near 00:00 UTC the rows below would fall before local midnight.
        await add_run(session, issue="1", pr=10, at=NOW - timedelta(hours=1))
        await add_run(session, issue="2", status="failed", at=NOW - timedelta(hours=1))
        await add_run(session, role="researcher", status="running", issue="3", at=NOW - timedelta(minutes=5))
        await add_event(session, "rate_limit", at=NOW - timedelta(minutes=30))
        payload = await build_activity(None, "UTC", now=NOW)
        assert (len(payload["today"]), len(payload["attention"]), len(payload["background"])) == (2, 1, 1)
        # Only the wiring is checked through the endpoint: its clock is the real one, so no count is asserted.
        res = await client.get("/api/feed/activity")
        assert res.status_code == 200


class TestBackground:
    async def test_events_are_counted_by_kind_and_a_warning_is_not_a_kill(self, session: AsyncSession) -> None:
        await add_event(session, "rate_limit")
        await add_event(session, "rate_limit")
        await add_event(session, "watchdog", run_id=1, signal="slow_step", action="warn")
        payload = await build_activity(None, "UTC", now=NOW)
        assert payload["attention"] == []
        assert {b["kind"]: (b["count"], b["text"]) for b in payload["background"]} == {
            "rate_limit": (2, "2 GitHub rate limit events."),
            "watchdog": (1, "1 watchdog warning."),
        }

    async def test_an_event_kind_the_narration_does_not_know_goes_into_the_generic_count(
        self, session: AsyncSession
    ) -> None:
        await add_event(session, "brand_new_kind")
        await add_event(session, "another_one")
        payload = await build_activity(None, "UTC", now=NOW)
        assert payload["attention"] == []
        assert [(b["kind"], b["text"]) for b in payload["background"]] == [("other", "2 other events.")]

    async def test_events_that_narrate_a_run_and_yesterdays_events_are_not_background(
        self, session: AsyncSession
    ) -> None:
        await add_event(session, "agent")
        await add_event(session, "handoff")
        await add_event(session, "rate_limit", at=NOW - timedelta(days=1))
        assert (await build_activity(None, "UTC", now=NOW))["background"] == []

    async def test_a_batch_job_and_a_planner_warning_leave_no_run_so_they_are_background(
        self, session: AsyncSession
    ) -> None:
        await add_event(session, "batch", batch_id="b1", action="triage", count=8)
        await add_event(session, "batch", batch_id="b1", failed=0, total=8)
        await add_event(session, "planner", attempts=3)
        payload = await build_activity(None, "UTC", now=NOW)
        assert {b["kind"]: b["text"] for b in payload["background"]} == {
            "batch": "1 batch job finished.",
            "planner": "1 planner warning.",
        }


class TestPeriods:
    async def test_past_days_weeks_and_months_come_from_runs_older_than_the_feed_retention(
        self, session: AsyncSession
    ) -> None:
        # NOW is Monday 23 Nov 2026.
        await add_run(
            session,
            role="command:integrate-pr",
            issue="1",
            at=datetime(2026, 11, 22, 10, tzinfo=timezone.utc),
            handoff=MERGED,
            cost="1.5",
        )
        await add_run(session, issue="2", status="failed", at=datetime(2026, 11, 22, 11, tzinfo=timezone.utc), cost="2")
        await add_run(session, issue="3", at=datetime(2026, 11, 18, 10, tzinfo=timezone.utc))
        await add_run(session, issue="4", at=datetime(2026, 6, 3, 10, tzinfo=timezone.utc), cost="0.25")
        payload = await build_activity(None, "UTC", now=NOW)
        lines = {p["key"]: (p["text"], p["from"], p["to"]) for p in payload["periods"]}
        assert lines == {
            "yesterday": (
                "Yesterday: I worked on 2 issues and merged 1, 1 run failed, $3.50.",
                "2026-11-22",
                "2026-11-22",
            ),
            "week-1": ("Last week: I worked on 1 issue and merged nothing, $0.00.", "2026-11-16", "2026-11-21"),
            "month-2026-06": ("June: I worked on 1 issue and merged nothing, $0.25.", "2026-06-01", "2026-06-30"),
        }

    async def test_a_period_counts_an_issue_once_however_many_runs_it_had(self, session: AsyncSession) -> None:
        yesterday = NOW - timedelta(days=1)
        await add_run(session, issue="5", pr=50, at=yesterday)
        await add_run(session, role="reviewer", issue="5", at=yesterday + timedelta(minutes=30))
        await add_run(session, role="command:address-pr", pr=50, at=yesterday + timedelta(hours=1))
        [line] = (await build_activity(None, "UTC", now=NOW))["periods"]
        assert line["text"] == "Yesterday: I worked on 1 issue and merged nothing, $0.00."

    async def test_periods_neither_overlap_nor_leave_a_gap(self) -> None:
        """Every day from yesterday back to the oldest month falls in exactly one range, for every weekday, a first
        of the month, and the daylight saving days: a gap would silently drop runs from every line."""
        zone = resolve_zone("America/New_York")
        start = datetime(2026, 2, 27).date()
        for offset in range(0, 40):
            today = start + timedelta(days=offset)
            ranges = feed_activity_service._period_ranges(today, zone)
            assert ranges[0][3] == local_midnight(today, zone)
            for (_, _, start_a, end_a), (_, _, start_b, end_b) in zip(ranges, ranges[1:], strict=False):
                assert start_a < end_a
                assert end_b == start_a, (today, ranges)

    async def test_a_second_request_inside_the_ttl_reuses_the_period_lines(self, session: AsyncSession) -> None:
        await add_run(session, issue="1", at=NOW - timedelta(days=1))
        first = await build_activity(None, "UTC", now=NOW)
        await add_run(session, issue="2", at=NOW - timedelta(days=1))
        assert (await build_activity(None, "UTC", now=NOW))["periods"] == first["periods"]
        feed_activity_service.reset_activity_cache()
        assert (await build_activity(None, "UTC", now=NOW))["periods"] != first["periods"]

    async def test_the_cache_is_keyed_by_the_local_day_so_a_midnight_inside_the_ttl_shifts_the_periods(
        self, session: AsyncSession
    ) -> None:
        await add_run(session, issue="1", at=datetime(2026, 11, 22, 10, tzinfo=timezone.utc))
        await add_run(session, issue="2", at=datetime(2026, 11, 23, 10, tzinfo=timezone.utc))
        before = await build_activity(None, "UTC", now=datetime(2026, 11, 23, 23, 59, tzinfo=timezone.utc))
        after = await build_activity(None, "UTC", now=datetime(2026, 11, 24, 0, 1, tzinfo=timezone.utc))
        assert [(p["key"], p["from"]) for p in before["periods"]][0] == ("yesterday", "2026-11-22")
        assert [(p["key"], p["from"]) for p in after["periods"]][0] == ("yesterday", "2026-11-23")

    async def test_a_past_days_entries_are_evicted_when_a_new_one_is_stored(self, session: AsyncSession) -> None:
        await add_run(session, issue="1", at=NOW - timedelta(days=1))
        await build_activity(None, "UTC", now=NOW)
        await build_activity(None, "Europe/Bratislava", now=NOW)
        assert len(feed_activity_service._periods_cache) == 2
        await build_activity(None, "UTC", now=NOW + timedelta(days=1))
        assert {key[2] for key in feed_activity_service._periods_cache} == {(NOW + timedelta(days=1)).date()}

    async def test_period_lines_with_no_ranges_runs_no_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feed_activity_service, "_period_ranges", lambda *_a: [])
        # A session that would fail on any use proves the guard returns before querying.
        assert await feed_activity_service._period_lines(None, NOW.date(), resolve_zone("UTC")) == []  # type: ignore[arg-type]


class TestHandoffMatching:
    def test_matches_handoff_rejects_a_non_dict_entry(self) -> None:
        assert not feed_activity_service._matches_handoff(["#1"], "1", None)

    def test_matches_handoff_by_issue_ignores_the_pr(self) -> None:
        assert feed_activity_service._matches_handoff({"issue": "#7", "pr_number": 9}, "7", 3)
        assert not feed_activity_service._matches_handoff({"issue": "8", "pr_number": 3}, "7", 3)

    def test_matches_handoff_by_pr_only_without_an_issue(self) -> None:
        assert feed_activity_service._matches_handoff({"pr_number": 3}, None, 3)
        assert not feed_activity_service._matches_handoff({"pr_number": 3}, None, None)

    def test_matches_handoff_reads_a_pr_number_stored_as_text(self) -> None:
        assert feed_activity_service._matches_handoff({"pr_number": "3"}, None, 3)
        assert feed_activity_service._matches_handoff({"pr_number": "#3"}, None, 3)

    @pytest.mark.parametrize("pr_number", [70, "70"])
    async def test_an_issue_keyed_item_falls_back_to_a_handoff_started_from_the_pr(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, pr_number: object
    ) -> None:
        await add_run(session, role="researcher", status="awaiting_approval", issue="8", pr=70)
        handoffs = [{"pr_number": pr_number, "next_actions": [{"id": "address-review", "label": "Address review"}]}]
        monkeypatch.setattr(
            feed_activity_service,
            "peek_snapshot",
            lambda kind, _dir: SimpleNamespace(payload={"handoffs": handoffs}) if kind == "handoff" else None,
        )
        [item] = (await build_activity(None, "UTC", now=NOW))["attention"]
        assert item["key"] == "issue:8"
        assert item["action"]["ref"] == "pr:70"
        assert item["action"]["confirm_text"] == "Address review on PR #70?"

    def test_offered_action_skips_a_non_dict_or_idless_action(self) -> None:
        assert feed_activity_service._offered_action("integrate", "1") is None
        assert feed_activity_service._offered_action({"label": "Go"}, "1") is None

    def test_offered_action_asks_to_confirm_an_integrate_command(self) -> None:
        offered = feed_activity_service._offered_action({"id": "merge", "command": "/integrate-pr 5"}, "pr:5")
        assert offered is not None
        assert {k: offered[k] for k in ("id", "label", "ref", "confirm")} == {
            "id": "merge",
            "label": "merge",
            "ref": "pr:5",
            "confirm": True,
        }


class TestTitles:
    async def test_a_title_reaches_the_entry_and_a_failed_lookup_leaves_the_bare_number(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await add_run(session, issue="1", pr=10)
        await add_run(session, issue="2", pr=20)

        async def titles(_dir: object, issues: object, _prs: object) -> tuple[dict, dict]:
            return {"1": "Add the thing"}, {}

        monkeypatch.setattr(work_history_service, "resolve_titles", titles)
        payload = await build_activity(None, "UTC", now=NOW)
        assert {k: e["title"] for k, e in by_key(payload).items()} == {"issue:1": "Add the thing", "issue:2": None}


class TestTitleFailures:
    @pytest.fixture(autouse=True)
    def _real_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Use the real title resolution (its deadline and error handling), with only the tracker call replaced."""
        monkeypatch.setattr(work_history_service, "resolve_titles", work_title_service.resolve_titles)
        monkeypatch.setattr(work_title_service, "LOOKUP_DEADLINE_SECONDS", 0.05)
        work_title_service.reset_title_cache()

    async def test_a_lookup_that_raises_leaves_the_bare_number(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("tracker down")

        monkeypatch.setattr(work_title_service, "_lookup_all", boom)
        await add_run(session, issue="1", pr=10)
        payload = await build_activity(None, "UTC", now=NOW)
        assert by_key(payload)["issue:1"]["title"] is None

    async def test_a_lookup_that_hangs_does_not_delay_the_response(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def hang(*_a: object, **_k: object) -> None:
            await asyncio.sleep(10)

        monkeypatch.setattr(work_title_service, "_lookup_all", hang)
        await add_run(session, issue="1", pr=10)
        started = time.monotonic()
        payload = await build_activity(None, "UTC", now=NOW)
        assert time.monotonic() - started < 3
        assert by_key(payload)["issue:1"]["title"] is None


class TestEndpoint:
    async def test_the_endpoint_answers_for_the_request_zone(self, client: AsyncClient, session: AsyncSession) -> None:
        await add_run(session, issue="1", at=datetime.now(timezone.utc) - timedelta(minutes=1))
        res = await client.get("/api/feed/activity", params={"tz": "Europe/Bratislava"})
        assert res.status_code == 200
        body = res.json()
        assert body["tz"] == "Europe/Bratislava"
        assert set(body) >= {"today", "attention", "background", "periods", "generated_at"}

    @pytest.mark.parametrize("tz", [None, "Nope/Zone"])
    async def test_a_missing_or_unknown_zone_is_not_a_client_error(self, client: AsyncClient, tz: str | None) -> None:
        res = await client.get("/api/feed/activity", params={"tz": tz} if tz else None)
        assert res.status_code == 200
        assert res.json()["tz"] == "UTC"
