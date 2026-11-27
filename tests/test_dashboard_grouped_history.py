"""Grouped run history (#1479): ``GET /work/history?group=issue``, the state judged from all of a group's runs, the
one-line outcome of every run, and the bounded title lookup."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.dashboard.services import snapshot_service, work_history_service, work_title_service
from sova.dashboard.services.run_outcome import run_outcome
from sova.dashboard.services.work_history_service import attach_titles, get_grouped_history
from sova.dashboard.services.work_service import get_work_history
from sova.db.models import StepExecution, TaskRun
from sova.db.session import get_session
from sova.work.state import RUN_ACTIONS, run_action

pytestmark = pytest.mark.usefixtures("reset_db_rows")

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with await get_session() as s:
        yield s


@pytest.fixture
async def client(dashboard_app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=dashboard_app), base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _no_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here reaches a tracker: the history service gets no titles unless a test says otherwise."""
    work_title_service.reset_title_cache()
    snapshot_service.reset_snapshots()

    async def none(*_a: object, **_k: object) -> tuple[dict, dict]:
        return {}, {}

    monkeypatch.setattr(work_history_service, "resolve_titles", none)


async def add_run(
    session: AsyncSession,
    *,
    role: str = "developer",
    status: str = "done",
    hours: float = 0,
    issue: str | None = None,
    pr: int | None = None,
    cost: str = "0",
    error: str | None = None,
    handoff: dict | None = None,
    steps: tuple[str, ...] = (),
) -> TaskRun:
    started = T0 + timedelta(hours=hours)
    run = TaskRun(
        role=role,
        status=status,
        issue_number=issue,
        pr_number=pr,
        total_cost_usd=Decimal(cost),
        current_step="complete",
        started_at=started,
        ended_at=None if status == "running" else started + timedelta(minutes=10),
        error_message=error,
        handoff_json=handoff,
    )
    session.add(run)
    await session.flush()
    for name in steps:
        session.add(StepExecution(task_run_id=run.id, step_name=name, status="done", started_at=started))
    return run


async def grouped(session: AsyncSession, **kwargs: object) -> dict:
    async with session.begin():
        return await get_grouped_history(session, **kwargs)


def by_key(data: dict) -> dict[str, dict]:
    return {g["key"]: g for g in data["groups"]}


class TestRunOutcome:
    @pytest.mark.parametrize(
        ("role", "status", "error", "handoff", "pr", "expected"),
        [
            (
                "reviewer",
                "done",
                None,
                {
                    "next_action": "address_review",
                    "pending_findings": [{}, {}, {}],
                    "summary": "Missing test for X. Second sentence.",
                },
                None,
                "Changes requested, 3 findings. Missing test for X.",
            ),
            (
                "reviewer",
                "done",
                None,
                {"next_action": "address_review", "pending_findings": [{}], "summary": ""},
                None,
                "Changes requested, 1 finding.",
            ),
            (
                "reviewer",
                "done",
                None,
                {"next_action": "approve", "summary": "Looks fine."},
                None,
                "No blocking defects",
            ),
            (
                "reviewer",
                "done",
                None,
                {"next_action": "review_incomplete"},
                None,
                "Review incomplete, some files were not reviewed",
            ),
            ("reviewer", "done", None, None, None, "Review finished"),
            ("reviewer", "done", None, {"next_action": "something_new"}, None, "No blocking defects"),
            ("reviewer", "done", None, {"summary": "Partial review"}, None, "Review finished"),
            ("developer", "done", None, {"next_action": "review", "addressed_findings": [{}, {}]}, 7, "Opened PR #7"),
            ("developer", "done", None, None, 9, "Opened PR #9"),
            ("developer", "done", None, None, None, "Develop finished"),
            ("researcher", "done", None, None, None, "Spec ready, waiting for your approval"),
            ("researcher", "awaiting_approval", None, {}, None, "Spec ready, waiting for your approval"),
            ("supervisor:rebase", "done", None, None, 33, "Rebased PR #33 onto main"),
            ("supervisor:rebase", "done", None, None, None, "Rebased"),
            (
                "command:integrate-pr",
                "done",
                None,
                {"summary": "Merged PR #5 as abc123.\nCleaned up."},
                5,
                "Merged PR #5 as abc123. Cleaned up.",
            ),
            ("command:integrate-pr", "done", None, {"details": "no summary key"}, 5, "Integrate finished"),
            ("command:integrate-pr", "done", None, "not a dict", 5, "Integrate finished"),
            ("command:address-pr", "done", None, {"summary": "Fixed two threads"}, 5, "Fixed two threads"),
            ("developer", "failed", "Timed out after 1500s", {"summary": "x"}, None, "Timed out after 1500s"),
            ("developer", "failed", "", None, None, "Failed"),
            ("developer", "failed", None, None, None, "Failed"),
            ("developer", "interrupted", None, None, None, "Interrupted"),
        ],
    )
    def test_each_kind_of_run_reads_as_one_line(
        self, role: str, status: str, error: str | None, handoff: object, pr: int | None, expected: str
    ) -> None:
        assert run_outcome(run_action(role), status, error, handoff, pr) == expected

    def test_a_pipeline_address_review_run_counts_the_addressed_findings(self) -> None:
        action = RUN_ACTIONS["address-review"]
        text = run_outcome(action, "done", None, {"addressed_findings": [{}, {}, {}]})
        assert text == "Fixed 3 findings, sent back for re-review"
        assert run_outcome(action, "done", None, {"addressed_findings": [{}]}) == (
            "Fixed 1 finding, sent back for re-review"
        )

    async def test_history_rows_carry_the_outcome(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="1", pr=5, handoff=None)
            await add_run(session, issue="2", status="failed", hours=1)
        data = await get_work_history(session)
        assert {t["issue_number"]: t["outcome"] for t in data["tasks"]} == {"1": "Opened PR #5", "2": "Failed"}
        assert "groups" not in data


class TestGrouping:
    async def test_runs_group_by_issue_then_pr_then_alone(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="10", hours=0, pr=100)
            await add_run(session, role="reviewer", issue="10", hours=1, pr=100)
            await add_run(session, role="command:integrate-pr", hours=2, pr=100)  # PR only: joins issue 10
            await add_run(session, role="command:integrate-pr", hours=3, pr=200)  # no run links PR 200 to an issue
            await add_run(session, role="command:health-audit", hours=4)  # neither: its own row
        data = await grouped(session)
        assert data["total"] == 3
        groups = by_key(data)
        assert groups["issue:10"]["run_count"] == 3
        assert groups["pr:200"]["run_count"] == 1
        assert groups["pr:200"]["issue_number"] is None
        run_keys = [k for k in groups if k.startswith("run:")]
        assert len(run_keys) == 1
        assert groups[run_keys[0]]["run_count"] == 1

    async def test_a_run_with_neither_issue_nor_pr_never_joins_an_issue_with_prless_runs(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            await add_run(session, role="researcher", issue="5", hours=0)  # an issue run with no PR
            await add_run(session, role="command:health-audit", hours=1)  # no issue, no PR
        groups = by_key(await grouped(session))
        assert groups["issue:5"]["run_count"] == 1
        run_keys = [k for k in groups if k.startswith("run:")]
        assert len(run_keys) == 1
        assert groups[run_keys[0]]["run_count"] == 1
        assert len(groups) == 2

    async def test_a_pr_shared_by_two_issues_joins_the_issue_of_its_newest_run(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="9", hours=0, pr=100)
            await add_run(session, issue="10", hours=1, pr=100)
            await add_run(session, role="command:integrate-pr", hours=2, pr=100)
        data = await grouped(session)
        # Newest issue-carrying run of PR 100 is issue 10's (a text MAX would pick "9").
        assert by_key(data)["issue:10"]["run_count"] == 2
        assert by_key(data)["issue:9"]["run_count"] == 1

    async def test_a_pr_only_run_alone_in_range_still_names_its_issue(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="10", hours=0, pr=100)
            await add_run(session, role="command:integrate-pr", hours=48, pr=100, handoff=MERGED)
        data = await grouped(session, since=T0 + timedelta(hours=24))
        group = data["groups"][0]
        assert (group["key"], group["issue_number"], group["run_count"]) == ("issue:10", "10", 1)
        assert group["state"] == "merged"
        assert data["summary"]["merged"] == 1

    async def test_groups_are_ordered_by_their_newest_matching_run(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="1", hours=5)
            await add_run(session, issue="2", hours=1)
            await add_run(session, issue="2", hours=9)
            await add_run(session, issue="3", hours=7)
        data = await grouped(session)
        assert [g["key"] for g in data["groups"]] == ["issue:2", "issue:3", "issue:1"]
        assert [r["started_at"] for r in data["groups"][0]["runs"]] == sorted(
            (r["started_at"] for r in data["groups"][0]["runs"]), reverse=True
        )

    async def test_paging_covers_groups_and_a_page_past_the_end_is_empty(self, session: AsyncSession) -> None:
        async with session.begin():
            for n in range(5):
                await add_run(session, issue=str(n), hours=n)
        first = await grouped(session, limit=2, offset=0)
        last = await grouped(session, limit=2, offset=4)
        beyond = await grouped(session, limit=2, offset=10)
        assert [g["key"] for g in first["groups"]] == ["issue:4", "issue:3"]
        assert [g["key"] for g in last["groups"]] == ["issue:0"]
        assert beyond["groups"] == []
        assert beyond["total"] == 5

    async def test_a_group_counts_only_its_matching_runs(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="4", hours=0, cost="1.5")
            await add_run(session, role="reviewer", issue="4", hours=30, cost="0.5")
        data = await grouped(session, action="review")
        group = data["groups"][0]
        assert group["run_count"] == 1
        assert group["total_cost_usd"] == "0.500000"
        assert data["runs_total"] == 1

    async def test_a_filter_that_hides_the_run_carrying_the_pr_keeps_the_groups_pr(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="3", hours=0, pr=33)
            await add_run(session, role="researcher", issue="3", hours=30)  # no PR on this run
        group = (await grouped(session, action="research"))["groups"][0]
        assert (group["run_count"], group["pr_number"]) == (1, 33)
        narrow = (await grouped(session, since=T0 + timedelta(hours=24)))["groups"][0]
        assert narrow["pr_number"] == 33

    async def test_the_number_filter_matches_an_issue_or_a_pr_group(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="7", pr=70)
            await add_run(session, role="command:integrate-pr", hours=1, pr=70)
            await add_run(session, issue="8", hours=2, pr=80)
        by_pr = await grouped(session, number="70")
        assert [g["key"] for g in by_pr["groups"]] == ["issue:7"]
        assert by_pr["groups"][0]["run_count"] == 2
        by_issue = await grouped(session, number="#8")
        assert [g["key"] for g in by_issue["groups"]] == ["issue:8"]

    async def test_the_result_filter_and_number_filter_combine(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="1", status="failed", hours=0)
            await add_run(session, issue="1", hours=1)
            await add_run(session, issue="2", status="failed", hours=2)
        data = await grouped(session, status="failed", number="1")
        assert [g["key"] for g in data["groups"]] == ["issue:1"]
        assert data["groups"][0]["run_count"] == 1
        assert data["failed_total"] == 1

    async def test_an_inverted_range_returns_nothing(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="1")
        data = await grouped(session, since=T0 + timedelta(days=2), until=T0)
        assert data["groups"] == []
        assert data["total"] == 0
        assert data["summary"]["groups"] == 0

    async def test_the_group_keeps_a_failed_run_with_an_empty_error_red(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="1", status="failed", error="")
        group = (await grouped(session))["groups"][0]
        assert group["result"] == {"text": "Failed", "color": "red"}


MERGED = {"summary": "Merged PR", "merged": True}


def _state(data: dict, key: str) -> str:
    return by_key(data)[key]["state"]


class TestState:
    async def test_merged_when_an_integrate_run_succeeded(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="1", hours=0, pr=10)
            await add_run(session, role="command:integrate-pr", issue="1", hours=1, pr=10, handoff=MERGED)
            await add_run(session, role="command:after-merge", issue="1", hours=2)
        group = by_key(await grouped(session))["issue:1"]
        assert (group["state"], group["state_label"], group["state_color"]) == ("merged", "Merged", "green-strong")
        assert group["state_inputs"]["merged"] is True

    async def test_an_integrate_run_that_stopped_at_a_gate_does_not_merge(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="1", hours=0, pr=10)
            stopped = {"summary": "CI failing on lint", "merged": False}
            await add_run(session, role="command:integrate-pr", issue="1", hours=1, pr=10, handoff=stopped)
        data = await grouped(session)
        assert _state(data, "issue:1") == "finished"
        assert data["summary"]["merged"] == 0

    @pytest.mark.parametrize("handoff", [None, {}, {"summary": "Done"}, {"merged": "yes"}, "not a dict"])
    async def test_an_integrate_run_with_no_merged_marker_is_not_claimed_as_merged(
        self, session: AsyncSession, handoff: object
    ) -> None:
        """A run that stopped before its closing block, or predates the marker, must not put Merged on the PR."""
        async with session.begin():
            await add_run(session, issue="1", hours=0, pr=10)
            await add_run(session, role="command:integrate-pr", issue="1", hours=1, pr=10, handoff=handoff)
        data = await grouped(session)
        group = by_key(data)["issue:1"]
        assert group["state"] == "finished"
        assert group["state_inputs"]["merged"] is False
        assert data["summary"]["merged"] == 0

    async def test_a_failed_integrate_run_does_not_merge(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="1", hours=0, pr=10)
            await add_run(session, role="command:integrate-pr", issue="1", hours=1, status="failed")
        assert _state(await grouped(session), "issue:1") == "failed"

    async def test_a_narrow_range_keeps_the_real_state(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="1", hours=0, pr=10)
            await add_run(session, role="command:integrate-pr", issue="1", hours=48, pr=10, handoff=MERGED)
        data = await grouped(session, since=T0 - timedelta(hours=1), until=T0 + timedelta(hours=1))
        group = by_key(data)["issue:1"]
        assert group["state"] == "merged"
        assert group["run_count"] == 1
        assert group["state_inputs"]["newest_action"] == "integrate"

    @pytest.mark.parametrize("status", ["stopped", "interrupted", "paused", "rejected"])
    async def test_a_run_that_did_not_finish_its_job_reads_stopped(self, session: AsyncSession, status: str) -> None:
        async with session.begin():
            await add_run(session, issue="1", hours=0, pr=10, status=status)
        group = by_key(await grouped(session))["issue:1"]
        assert (group["state"], group["state_label"], group["state_color"]) == ("stopped", "Stopped", "gray-dim")

    async def test_the_newest_failed_run_makes_the_group_failed(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="1", hours=0)
            await add_run(session, issue="1", hours=1, status="failed", error="boom")
            await add_run(session, issue="2", hours=0, status="failed")
            await add_run(session, issue="2", hours=1)
        data = await grouped(session)
        assert _state(data, "issue:1") == "failed"
        assert _state(data, "issue:2") == "waiting_review"

    async def test_a_successful_researcher_run_needs_approval(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, role="researcher", issue="1", steps=("research",))
        group = by_key(await grouped(session))["issue:1"]
        assert (group["state"], group["state_label"]) == ("needs_approval", "Needs your approval")

    async def test_a_reviewer_run_decides_between_fixes_and_review(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, role="reviewer", issue="1", handoff={"next_action": "address_review"})
            await add_run(session, role="reviewer", issue="2", handoff={"next_action": "approve"})
            await add_run(session, role="reviewer", issue="3", handoff=None)
        data = await grouped(session)
        assert [_state(data, f"issue:{n}") for n in (1, 2, 3)] == ["waiting_fixes", "waiting_review", "waiting_review"]

    @pytest.mark.parametrize("role", ["developer", "supervisor:rebase", "command:address-pr"])
    async def test_develop_address_and_rebase_runs_wait_for_review(self, session: AsyncSession, role: str) -> None:
        async with session.begin():
            await add_run(session, role=role, issue="1")
        assert _state(await grouped(session), "issue:1") == "waiting_review"

    async def test_a_run_in_flight_does_not_change_the_state(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="1", hours=0, status="failed")
            await add_run(session, issue="1", hours=1, status="running")
        group = by_key(await grouped(session))["issue:1"]
        assert group["state"] == "failed"
        assert group["run_count"] == 1

    async def test_the_summary_covers_every_group_not_the_page(self, session: AsyncSession) -> None:
        async with session.begin():
            await add_run(session, issue="1", hours=0)
            await add_run(session, role="command:integrate-pr", issue="1", hours=1, handoff=MERGED)
            await add_run(session, issue="2", hours=2, status="failed")
            await add_run(session, issue="3", hours=3, status="failed")
            await add_run(session, role="researcher", issue="4", hours=4)
            await add_run(session, issue="5", hours=5)
        page_one = await grouped(session, limit=1)
        page_three = await grouped(session, limit=1, offset=2)
        expected = {"groups": 5, "merged": 1, "need_you": 1, "failed": 2}
        assert page_one["summary"] == page_three["summary"] == expected


class TestGroupTitles:
    async def test_titles_reach_the_groups_and_an_untitled_group_is_null(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[tuple[list[str], list[int]]] = []

        async def titles(_dir: object, issues: object, prs: object) -> tuple[dict, dict]:
            asked.append((sorted(issues), sorted(prs)))
            return {"1": "First issue"}, {9: "A dependabot bump"}

        monkeypatch.setattr(work_history_service, "resolve_titles", titles)
        async with session.begin():
            await add_run(session, issue="1", pr=5)
            await add_run(session, issue="2", hours=1)
            await add_run(session, role="command:integrate-pr", hours=2, pr=9)
        data = await grouped(session)
        assert all(g["title"] is None for g in data["groups"])  # the query itself never asks a tracker
        assert asked == []
        groups = by_key(await attach_titles(None, data))
        assert asked == [(["1", "2"], [9])]
        assert groups["issue:1"]["title"] == "First issue"
        assert groups["issue:2"]["title"] is None
        assert groups["pr:9"]["title"] == "A dependabot bump"


class TestEndpoint:
    async def test_group_issue_returns_groups_and_the_plain_list_is_unchanged(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        async with session.begin():
            await add_run(session, issue="1", pr=2)
        plain = (await client.get("/api/work/history")).json()
        grouped_body = (await client.get("/api/work/history", params={"group": "issue"})).json()
        assert set(plain) == {"tasks", "total", "total_cost_usd", "failed_total"}
        assert [g["key"] for g in grouped_body["groups"]] == ["issue:1"]
        assert "tasks" not in grouped_body

    async def test_the_default_page_is_25_groups_not_runs(self, client: AsyncClient, session: AsyncSession) -> None:
        async with session.begin():
            for n in range(27):
                await add_run(session, issue=str(n), hours=n)
                await add_run(session, role="reviewer", issue=str(n), hours=n + 0.5)  # two runs per group
        first = (await client.get("/api/work/history", params={"group": "issue"})).json()
        second = (await client.get("/api/work/history", params={"group": "issue", "offset": 25})).json()
        assert (len(first["groups"]), first["total"]) == (25, 27)
        assert [len(g["runs"]) for g in first["groups"]] == [2] * 25
        assert len(second["groups"]) == 2
        assert first["groups"][0]["key"] == "issue:26"

    @pytest.mark.parametrize("fault", ["raises", "hangs"])
    async def test_a_failing_or_slow_tracker_leaves_null_titles_and_answers_promptly(
        self, client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, fault: str
    ) -> None:
        import sova.adapters as adapters

        adapter = _Adapter({}, delay=5.0 if fault == "hangs" else 0.0, fail=fault == "raises")
        # The autouse fixture stubs the title lookup out: this test wants the real service.
        monkeypatch.setattr(work_history_service, "resolve_titles", work_title_service.resolve_titles)
        work_title_service.reset_title_cache()
        monkeypatch.setattr(adapters, "create_adapter", lambda _cfg: adapter)

        async def close(_adapter: object) -> None:
            return None

        monkeypatch.setattr(adapters, "close_adapter", close)
        monkeypatch.setattr(
            work_title_service, "load_config", lambda _dir: SimpleNamespace(github_repo="o/r", github_user="")
        )
        monkeypatch.setattr(work_title_service, "LOOKUP_DEADLINE_SECONDS", 0.2)
        async with session.begin():
            await add_run(session, issue="1", pr=5)
        started = time.monotonic()
        body = (await client.get("/api/work/history", params={"group": "issue"})).json()
        assert time.monotonic() - started < 2.0
        group = body["groups"][0]
        assert (group["title"], group["issue_number"], group["pr_number"]) == (None, "1", 5)

    async def test_an_unknown_group_is_a_422(self, client: AsyncClient) -> None:
        assert (await client.get("/api/work/history", params={"group": "pr"})).status_code == 422


class _Adapter:
    def __init__(self, titles: dict[str, str], delay: float = 0.0, fail: bool = False) -> None:
        self.titles, self.delay, self.fail, self.calls = titles, delay, fail, []

    async def get_task(self, task_id: str) -> SimpleNamespace:
        self.calls.append(task_id)
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("tracker down")
        return SimpleNamespace(title=self.titles[task_id])


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch) -> dict:
    """A fake tracker the title service reaches through ``create_adapter``."""
    import sova.adapters as adapters

    state: dict = {"adapter": _Adapter({"1": "First issue", "2": "Second issue"})}
    monkeypatch.setattr(adapters, "create_adapter", lambda _cfg: state["adapter"])

    async def close(_adapter: object) -> None:
        return None

    monkeypatch.setattr(adapters, "close_adapter", close)
    monkeypatch.setattr(
        work_title_service, "load_config", lambda _dir: SimpleNamespace(github_repo="o/r", github_user="")
    )
    return state


class TestTitles:
    async def test_a_snapshot_title_needs_no_tracker_call(self, tracker: dict, tmp_path: Path) -> None:
        async def build() -> dict:
            return {
                "items": [
                    {"issue_number": "1", "pr_number": None, "title": "From the board"},
                    {"issue_number": None, "pr_number": 9, "title": "A standalone PR"},
                ]
            }

        await snapshot_service.get_snapshot("work_items", tmp_path, build)
        issues, prs = await work_title_service.resolve_titles(tmp_path, ["1"], [9])
        assert (issues, prs) == ({"1": "From the board"}, {9: "A standalone PR"})
        assert tracker["adapter"].calls == []

    async def test_a_found_title_is_cached(self, tracker: dict, tmp_path: Path) -> None:
        first, _ = await work_title_service.resolve_titles(tmp_path, ["1", "2"], [])
        again, _ = await work_title_service.resolve_titles(tmp_path, ["1", "2"], [])
        assert first == again == {"1": "First issue", "2": "Second issue"}
        assert sorted(tracker["adapter"].calls) == ["1", "2"]

    async def test_an_expired_title_is_asked_again_and_kept_when_the_tracker_is_down(
        self, tracker: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await work_title_service.resolve_titles(tmp_path, ["1"], [])
        monkeypatch.setattr(work_title_service, "TITLE_TTL_SECONDS", -1.0)
        tracker["adapter"] = _Adapter({"1": "Renamed"})
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({"1": "Renamed"}, {})
        tracker["adapter"] = _Adapter({}, fail=True)
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({"1": "Renamed"}, {})
        assert tracker["adapter"].calls == ["1"]

    async def test_a_failed_lookup_is_remembered_for_the_miss_window_then_retried(
        self, tracker: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracker["adapter"] = _Adapter({}, fail=True)
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({}, {})
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({}, {})
        assert tracker["adapter"].calls == ["1"]  # the second request inside the window skipped the tracker
        work_title_service._retry_after.clear()  # the window has passed
        tracker["adapter"] = _Adapter({"1": "Back up"})
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({"1": "Back up"}, {})

    async def test_a_failed_lookup_is_asked_again_once_its_window_has_passed(
        self, tracker: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(work_title_service, "MISS_TTL_SECONDS", 0.0)
        tracker["adapter"] = _Adapter({}, fail=True)
        await work_title_service.resolve_titles(tmp_path, ["1"], [])
        await work_title_service.resolve_titles(tmp_path, ["1"], [])
        assert tracker["adapter"].calls == ["1", "1"]

    async def test_a_timed_out_lookup_is_remembered_so_the_next_request_answers_at_once(
        self, tracker: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(work_title_service, "LOOKUP_DEADLINE_SECONDS", 0.1)
        tracker["adapter"] = _Adapter({"1": "Too late"}, delay=5.0)
        await work_title_service.resolve_titles(tmp_path, ["1"], [])
        started = time.monotonic()
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({}, {})
        assert time.monotonic() - started < 0.05
        assert tracker["adapter"].calls == ["1"]

    async def test_a_failure_keeps_a_stale_title_showing_and_does_not_renew_it(
        self, tracker: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await work_title_service.resolve_titles(tmp_path, ["1"], [])
        monkeypatch.setattr(work_title_service, "TITLE_TTL_SECONDS", -1.0)
        tracker["adapter"] = _Adapter({}, fail=True)
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({"1": "First issue"}, {})
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({"1": "First issue"}, {})
        assert tracker["adapter"].calls == ["1"]

    async def test_a_hung_config_read_is_inside_the_deadline(
        self, tracker: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        release = threading.Event()
        monkeypatch.setattr(work_title_service, "LOOKUP_DEADLINE_SECONDS", 0.1)
        monkeypatch.setattr(work_title_service, "load_config", lambda _dir: release.wait(5) or None)
        started = time.monotonic()
        try:
            assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({}, {})
            assert time.monotonic() - started < 2.0
        finally:
            release.set()

    async def test_a_hung_adapter_close_does_not_delay_the_answer(
        self, tracker: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sova.adapters as adapters

        async def hang(_adapter: object) -> None:
            await asyncio.sleep(5)

        monkeypatch.setattr(adapters, "close_adapter", hang)
        monkeypatch.setattr(work_title_service, "LOOKUP_DEADLINE_SECONDS", 0.2)
        started = time.monotonic()
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({"1": "First issue"}, {})
        assert time.monotonic() - started < 1.0
        for task in list(work_title_service._closing):
            task.cancel()

    async def test_an_empty_answer_is_a_miss_remembered_for_the_miss_window(
        self, tracker: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracker["adapter"] = _Adapter({"1": ""})
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({}, {})
        await work_title_service.resolve_titles(tmp_path, ["1"], [])
        assert tracker["adapter"].calls == ["1"]  # inside the window: not asked again
        monkeypatch.setattr(work_title_service, "MISS_TTL_SECONDS", -1.0)
        tracker["adapter"] = _Adapter({"1": "Now titled"})
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({"1": "Now titled"}, {})

    async def test_a_slow_tracker_does_not_hold_the_response(
        self, tracker: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(work_title_service, "LOOKUP_DEADLINE_SECONDS", 0.1)
        tracker["adapter"] = _Adapter({"1": "Too late"}, delay=5.0)
        started = time.monotonic()
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({}, {})
        assert time.monotonic() - started < 2.0

    async def test_an_adapter_that_cannot_be_built_leaves_bare_numbers(
        self, tracker: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(_cfg: object) -> None:
            raise RuntimeError("no adapter")

        import sova.adapters as adapters

        monkeypatch.setattr(adapters, "create_adapter", broken)
        assert await work_title_service.resolve_titles(tmp_path, ["1"], []) == ({}, {})
