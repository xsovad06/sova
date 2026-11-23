"""Work header figures: run outcomes, spend windows and the cached merged-PR count (issue #1307)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from sova.core.state import TASK_RUN_TERMINAL, RunOutcome, classify_run_outcome
from sova.dashboard.services import cost_service, overview_stats_service, reliability_service
from sova.dashboard.services.overview_stats_service import MergedPR, MergedPRs
from sova.db.models import FailureRecord, TaskRun
from sova.db.session import get_session
from sova.git.pr import PRLookupError

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
PRAGUE = cost_service.resolve_timezone("Europe/Prague")

pytestmark = pytest.mark.usefixtures("reset_db_rows")


class TestClassifyRunOutcome:
    @pytest.mark.parametrize(
        ("status", "reason", "expected"),
        [
            pytest.param("done", None, RunOutcome.SUCCESS, id="done"),
            pytest.param("failed", None, RunOutcome.FAILURE, id="failed"),
            pytest.param("rejected", None, RunOutcome.FAILURE, id="rejected-a2a-cancel"),
            pytest.param("interrupted", "external_signal", RunOutcome.FAILURE, id="interrupted-stays-failure"),
            pytest.param("paused", None, RunOutcome.PAUSED_BY_SOVA, id="paused"),
            pytest.param("awaiting_approval", None, RunOutcome.PAUSED_BY_SOVA, id="awaiting-approval"),
            pytest.param("stopped", "manual_stop", RunOutcome.STOPPED_BY_USER, id="manual-stop"),
            pytest.param("stopped", "memory_pressure", RunOutcome.PAUSED_BY_SOVA, id="watchdog-stop"),
            pytest.param("stopped", None, RunOutcome.PAUSED_BY_SOVA, id="stopped-null-reason"),
        ],
    )
    def test_buckets(self, status: str, reason: str | None, expected: RunOutcome) -> None:
        assert classify_run_outcome(status, reason) is expected

    @pytest.mark.parametrize("status", ["running", "pending", "developing", "nonsense"])
    def test_non_terminal_status_raises(self, status: str) -> None:
        with pytest.raises(ValueError, match="not a terminal"):
            classify_run_outcome(status)

    def test_every_terminal_status_has_an_outcome(self) -> None:
        assert {classify_run_outcome(s, "manual_stop") for s in TASK_RUN_TERMINAL} == set(RunOutcome)


class TestTimezone:
    @pytest.mark.parametrize(
        "name",
        [None, "", "Not/AZone", "../../etc/passwd", "/etc/localtime", "Europe/Prague\n", "A" * 200, "Europe//Prague"],
    )
    def test_invalid_name_falls_back_to_utc(self, name: str | None) -> None:
        zone = cost_service.resolve_timezone(name)
        assert cost_service.timezone_name(zone) == "UTC"

    def test_valid_name_is_kept(self) -> None:
        assert cost_service.timezone_name(PRAGUE) == "Europe/Prague"

    @pytest.mark.parametrize(
        ("now", "expected"),
        [
            pytest.param(NOW, datetime(2026, 11, 21, 23, tzinfo=timezone.utc), id="ordinary-day"),
            # 23-hour day: Prague springs forward on 2026-03-29, so local midnight is still CET (UTC+1).
            pytest.param(
                datetime(2026, 3, 29, 12, tzinfo=timezone.utc),
                datetime(2026, 3, 28, 23, tzinfo=timezone.utc),
                id="spring",
            ),
            # 25-hour day: Prague falls back on 2026-10-25, so local midnight is still CEST (UTC+2).
            pytest.param(
                datetime(2026, 10, 25, 12, tzinfo=timezone.utc),
                datetime(2026, 10, 24, 22, tzinfo=timezone.utc),
                id="autumn",
            ),
        ],
    )
    def test_local_midnight_follows_the_local_date(self, now: datetime, expected: datetime) -> None:
        assert cost_service.local_midnight(now, PRAGUE) == expected

    def test_a_local_day_ahead_of_utc(self) -> None:
        late = datetime(2026, 11, 21, 23, 30, tzinfo=timezone.utc)  # already the 22nd in Prague
        assert cost_service.local_midnight(late, PRAGUE) == datetime(2026, 11, 21, 23, tzinfo=timezone.utc)


def _run(
    status: str,
    *,
    days_ago: float = 1,
    cost: str = "1",
    reason: str | None = None,
    pr: int | None = None,
    role: str = "developer",
    at: datetime | None = None,
) -> TaskRun:
    return TaskRun(
        role=role,
        status=status,
        started_at=at if at is not None else NOW - timedelta(days=days_ago),
        total_cost_usd=Decimal(cost),
        termination_reason=reason,
        pr_number=pr,
    )


async def _seed(*runs: TaskRun, failures: tuple[FailureRecord, ...] = ()) -> list[int]:
    async with await get_session() as session:
        session.add_all(runs)
        await session.flush()
        ids = [r.id for r in runs]
        session.add_all(failures)
        await session.commit()
    return ids


async def _stats(zone=PRAGUE, merged: MergedPRs | None = None):
    """The run stats; with ``merged`` they also carry the SOVA-linked PR numbers, as the router builds them."""
    async with await get_session() as session:
        stats = await overview_stats_service.get_run_stats(session, zone=zone, now=NOW)
        if merged is not None:
            stats = await overview_stats_service.with_sova_links(session, stats, merged)
        return stats


class TestGetRunStats:
    async def test_windows_and_spend(self) -> None:
        await _seed(
            *[_run("done", days_ago=1, cost="2") for _ in range(8)],
            _run("failed", days_ago=2, cost="10"),
            _run("running", days_ago=0.1, cost="3"),
            _run("done", days_ago=10, cost="5"),
            _run("done", days_ago=20, cost="7"),
            _run("done", days_ago=40, cost="99"),
        )
        stats = await _stats()
        assert stats.week.total_spend == Decimal("29")  # 16 + 10 + 3 (running counts toward spend)
        assert stats.week.rated == 9  # the running run is not finished
        assert stats.previous_week.total_spend == Decimal("5")
        assert stats.month.total_spend == Decimal("41")
        assert stats.month.rated == 11
        payload = overview_stats_service.build_header_stats(stats, MergedPRs("unsupported"), PRAGUE)
        assert payload["spend"]["month_usd"] == Decimal("41")

    async def test_today_starts_at_local_midnight(self) -> None:
        # NOW (12:00 UTC) is 13:00 in Prague, so local midnight is 23:00 UTC the day before. 23:30 UTC is inside
        # Prague's day, 21:30 UTC is not; neither is inside the UTC day, which starts at 00:00 UTC.
        await _seed(
            _run("done", cost="1", at=datetime(2026, 11, 21, 23, 30, tzinfo=timezone.utc)),
            _run("done", cost="2", at=datetime(2026, 11, 21, 21, 30, tzinfo=timezone.utc)),
        )
        assert (await _stats()).today_spend == Decimal("1")
        assert (await _stats(timezone.utc)).today_spend == Decimal("0")

    async def test_rate_is_null_below_ten_rated_runs(self) -> None:
        await _seed(*[_run("done") for _ in range(4)], _run("failed"), *[_run("paused") for _ in range(6)])
        week = (await _stats()).week.summary()
        assert (week["success"], week["rated"], week["success_rate"]) == (4, 5, None)

    async def test_rate_at_ten_finished_runs(self) -> None:
        await _seed(*[_run("done") for _ in range(7)], *[_run("failed") for _ in range(3)])
        assert (await _stats()).week.summary()["success_rate"] == 0.7

    async def test_halted_runs_leave_the_rate_unchanged(self) -> None:
        fixed = [*[_run("done") for _ in range(9)], *[_run("failed") for _ in range(3)]]
        await _seed(*fixed)
        before = (await _stats()).week.summary()["success_rate"]
        await _seed(_run("paused"), _run("awaiting_approval"), _run("stopped", reason="manual_stop"))
        after = (await _stats()).week.summary()
        assert before == after["success_rate"] == 0.75
        assert (after["paused_by_sova"], after["stopped_by_user"]) == (2, 1)

    async def test_developer_rate_matches_the_reliability_page(self) -> None:
        # The Reliability page windows on the real clock, so both sides read the same "now".
        runs = [
            *[_run("done") for _ in range(9)],
            *[_run("failed") for _ in range(2)],
            _run("paused"),
            _run("stopped", reason="manual_stop"),
            *[_run("failed", role="reviewer") for _ in range(5)],
        ]
        now = datetime.now(timezone.utc)
        for run in runs:
            run.started_at = now - timedelta(days=1)
        await _seed(*runs)
        async with await get_session() as session:
            rows = await reliability_service.get_success_by_role(session, days=overview_stats_service.WINDOW_DAYS)
            stats = await overview_stats_service.get_run_stats(session, zone=PRAGUE, now=now)
        reliability_dev = next(r for r in rows if r["role"] == "developer")
        header_dev = stats.developer_week.summary()
        assert header_dev["success_rate"] == reliability_dev["success_rate"] == round(9 / 11, 4)
        assert stats.week.summary()["success_rate"] == round(9 / 16, 4)

    async def test_outcome_buckets_and_unfinished_spend(self) -> None:
        await _seed(
            _run("done", cost="4"),
            _run("failed", cost="1"),
            _run("rejected", cost="1"),
            _run("paused", cost="2"),
            _run("stopped", cost="3", reason="manual_stop"),
            _run("stopped", cost="5", reason=None),
            _run("awaiting_approval", cost="9"),
        )
        week = (await _stats()).week
        assert week.summary() == {
            "rated": 3,
            "success": 1,
            "failure": 2,
            "paused_by_sova": 3,
            "stopped_by_user": 1,
            "success_rate": None,
        }
        # failure 2 + paused 2 + stopped by you 3 + stopped by SOVA 5 + awaiting approval 9
        assert week.unfinished_spend == Decimal("21")
        assert week.total_spend == Decimal("25")

    async def test_paused_reason_is_the_latest_failure_record(self) -> None:
        paused, _, _, _ = await _seed(
            _run("paused"), _run("paused"), _run("stopped", reason="memory_pressure"), _run("awaiting_approval")
        )
        await _seed(
            failures=(
                FailureRecord(
                    task_run_id=paused,
                    step_name="develop",
                    failure_type="old",
                    message="m",
                    created_at=NOW - timedelta(hours=3),
                ),
                FailureRecord(
                    task_run_id=paused,
                    step_name="develop",
                    failure_type="runaway_guard",
                    message="m",
                    created_at=NOW - timedelta(hours=1),
                ),
            )
        )
        reasons = (await _stats()).paused_reasons
        assert dict(reasons) == {"runaway_guard": 1, "unknown": 1, "memory_pressure": 1, "awaiting_approval": 1}

    async def test_sova_pr_numbers_are_those_of_the_merged_prs_only(self) -> None:
        await _seed(_run("done", pr=11), _run("done"), _run("failed", pr=12), _run("done", pr=99))
        merged = MergedPRs("ok", tuple(MergedPR(n, NOW - timedelta(days=1), False) for n in (11, 12, 13)))
        assert (await _stats(merged=merged)).sova_pr_numbers == {11, 12}

    async def test_sova_pr_numbers_have_no_lookback(self) -> None:
        await _seed(_run("done", days_ago=90, pr=13))
        merged = MergedPRs("ok", (MergedPR(13, NOW - timedelta(days=1), False),))
        assert (await _stats(merged=merged)).sova_pr_numbers == {13}

    @pytest.mark.parametrize("merged", [MergedPRs("unavailable"), MergedPRs("unsupported"), MergedPRs("ok")])
    async def test_no_merged_prs_means_no_lookup(self, merged: MergedPRs) -> None:
        await _seed(_run("done", pr=11))
        assert (await _stats(merged=merged)).sova_pr_numbers == frozenset()


def _cfg(task_type: str = "github", repo: str = "o/r") -> SimpleNamespace:
    return SimpleNamespace(task_source=SimpleNamespace(type=task_type), github_repo=repo, github_user="")


def _pr(number: int, days_ago: float, *, author: dict | None = None, has_author: bool = True) -> dict:
    pr = {"number": number, "mergedAt": (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()}
    if has_author:
        pr["author"] = author if author is not None else {"login": "dev", "is_bot": False}
    return pr


@pytest.fixture
def merged_env():
    overview_stats_service.reset_merged_cache()
    fetch = AsyncMock(return_value=[_pr(1, 1)])
    with (
        patch.object(overview_stats_service, "load_config", return_value=_cfg()),
        patch.object(overview_stats_service, "list_merged_prs_since", fetch),
    ):
        yield fetch
    overview_stats_service.reset_merged_cache()


class TestFetchMergedPRs:
    async def test_cache_serves_later_calls_without_gh(self, merged_env: AsyncMock) -> None:
        first = await overview_stats_service.fetch_merged_prs(None)
        second = await overview_stats_service.fetch_merged_prs(None)
        assert first.status == second.status == "ok"
        assert merged_env.await_count == 1

    async def test_concurrent_cold_requests_share_one_call(self, merged_env: AsyncMock) -> None:
        async def slow(**_kwargs):
            await asyncio.sleep(0.05)
            return [_pr(1, 1)]

        merged_env.side_effect = slow
        results = await asyncio.gather(*[overview_stats_service.fetch_merged_prs(None) for _ in range(3)])
        assert [r.status for r in results] == ["ok"] * 3
        assert merged_env.await_count == 1

    async def test_expired_cache_refetches(self, merged_env: AsyncMock) -> None:
        await overview_stats_service.fetch_merged_prs(None)
        key = next(iter(overview_stats_service._cache))
        stamp, prs = overview_stats_service._cache[key]
        overview_stats_service._cache[key] = (stamp - overview_stats_service.MERGED_CACHE_TTL_SECONDS - 1, prs)
        await overview_stats_service.fetch_merged_prs(None)
        assert merged_env.await_count == 2

    @pytest.mark.parametrize(
        "error",
        [PRLookupError("gh timed out"), PRLookupError("bad json"), RuntimeError("subprocess layer"), ValueError("x")],
        ids=["timeout", "parse", "runtime", "value"],
    )
    async def test_failure_is_unavailable_and_not_cached(self, merged_env: AsyncMock, error: Exception) -> None:
        merged_env.side_effect = [error, [_pr(1, 1)]]
        assert (await overview_stats_service.fetch_merged_prs(None)).status == "unavailable"
        assert overview_stats_service._cache == {}
        assert (await overview_stats_service.fetch_merged_prs(None)).status == "ok"

    async def test_reset_while_in_flight_keeps_the_newer_task_registered(self, merged_env: AsyncMock) -> None:
        gates = [asyncio.Event(), asyncio.Event()]
        calls = iter(gates)

        async def slow(**_kwargs: object) -> list[dict]:
            await next(calls).wait()
            return [_pr(1, 1)]

        merged_env.side_effect = slow
        first = asyncio.create_task(overview_stats_service.fetch_merged_prs(None))
        await asyncio.sleep(0.01)
        overview_stats_service.reset_merged_cache()
        second = asyncio.create_task(overview_stats_service.fetch_merged_prs(None))
        await asyncio.sleep(0.01)
        newer = overview_stats_service._inflight["o/r"]
        gates[0].set()
        await first
        # The first fetch's done callback must not evict the second fetch's entry.
        assert overview_stats_service._inflight.get("o/r") is newer
        gates[1].set()
        await second

    async def test_result_at_the_limit_is_truncated_so_unavailable(self, merged_env: AsyncMock) -> None:
        merged_env.return_value = [_pr(n, 1) for n in range(overview_stats_service.MERGED_FETCH_LIMIT)]
        assert (await overview_stats_service.fetch_merged_prs(None)).status == "unavailable"
        assert overview_stats_service._cache == {}

    @pytest.mark.parametrize("task_type", ["github", "jira"])
    async def test_no_github_repo_is_unsupported(self, merged_env: AsyncMock, task_type: str) -> None:
        with patch.object(overview_stats_service, "load_config", return_value=_cfg(task_type, "")):
            assert (await overview_stats_service.fetch_merged_prs(None)).status == "unsupported"
        merged_env.assert_not_awaited()

    async def test_jira_project_with_a_repo_counts_its_merges(self, merged_env: AsyncMock) -> None:
        with patch.object(overview_stats_service, "load_config", return_value=_cfg("jira", "o/r")):
            assert (await overview_stats_service.fetch_merged_prs(None)).status == "ok"
        assert merged_env.await_args.kwargs["fields"] == "number,mergedAt,author"

    async def test_refresh_merged_past_deadline_is_unavailable_and_not_cached(self, merged_env: AsyncMock) -> None:
        cancelled = asyncio.Event()

        async def hang(**_kwargs):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        merged_env.side_effect = hang
        with patch.object(overview_stats_service, "MERGED_FETCH_TIMEOUT_SECONDS", 0.01):
            assert (await overview_stats_service.fetch_merged_prs(None)).status == "unavailable"
        assert cancelled.is_set()
        assert overview_stats_service._cache == {}
        assert "timeout" not in merged_env.await_args.kwargs

    async def test_missing_gh_binary_is_unavailable(self, merged_env: AsyncMock) -> None:
        merged_env.side_effect = FileNotFoundError("gh")
        assert (await overview_stats_service.fetch_merged_prs(None)).status == "unavailable"
        assert overview_stats_service._cache == {}

    async def test_config_failure_is_unavailable(self, merged_env: AsyncMock) -> None:
        with patch.object(overview_stats_service, "load_config", side_effect=RuntimeError("bad config")):
            assert (await overview_stats_service.fetch_merged_prs(None)).status == "unavailable"

    async def test_authors(self, merged_env: AsyncMock) -> None:
        merged_env.return_value = [
            _pr(1, 1),
            _pr(2, 1, author={"login": "app/dependabot", "is_bot": True}),
            _pr(3, 1, has_author=False),
            _pr(4, 1, author={"login": "ghost"}),
        ]
        result = await overview_stats_service.fetch_merged_prs(None)
        assert {pr.number: pr.is_bot for pr in result.prs} == {1: False, 2: True, 3: False, 4: False}

    @pytest.mark.parametrize("payload", [None, "not a list", 7], ids=["none", "str", "int"])
    async def test_non_list_payload_is_unavailable_and_not_cached(self, merged_env: AsyncMock, payload: object) -> None:
        merged_env.return_value = payload
        assert (await overview_stats_service.fetch_merged_prs(None)).status == "unavailable"
        assert overview_stats_service._cache == {}

    async def test_non_dict_author_is_not_a_bot(self, merged_env: AsyncMock) -> None:
        merged_env.return_value = [{**_pr(1, 1), "author": "x"}, {**_pr(2, 1), "author": ["is_bot"]}]
        result = await overview_stats_service.fetch_merged_prs(None)
        assert result.status == "ok"
        assert {pr.number: pr.is_bot for pr in result.prs} == {1: False, 2: False}

    async def test_parse_merged_skips_malformed_rows(self, merged_env: AsyncMock) -> None:
        merged_env.return_value = [
            _pr(1, 1),
            {"number": 2},
            {"number": "x", "mergedAt": "2026-11-20T10:00:00Z"},
            {"number": 4, "mergedAt": "not a date"},
            {"number": 5, "mergedAt": None},
        ]
        result = await overview_stats_service.fetch_merged_prs(None)
        assert result.status == "ok"
        assert [pr.number for pr in result.prs] == [1]


class TestBuildHeaderStats:
    def _merged(self, *prs: tuple[int, float, bool]) -> MergedPRs:
        return MergedPRs("ok", tuple(MergedPR(n, NOW - timedelta(days=d), bot) for n, d, bot in prs))

    async def test_merged_windows_cost_per_pr_and_sova_link(self) -> None:
        await _seed(_run("done", cost="10", pr=1), _run("done", days_ago=10, cost="6"))
        merged = self._merged((1, 1, False), (2, 2, False), (3, 3, True), (4, 9, False), (5, 20, False))
        payload = overview_stats_service.build_header_stats(await _stats(merged=merged), merged, PRAGUE)
        assert payload["merged"] == {
            "status": "ok",
            "count": 2,
            "previous_count": 1,
            "bots_left_out": 1,
            "with_sova_run": 1,
            "cost_per_pr_usd": Decimal("5"),
            "previous_cost_per_pr_usd": Decimal("6"),
        }
        assert payload["timezone"] == {"name": "Europe/Prague", "abbreviation": "CET"}
        assert payload["spend"]["week_usd"] == Decimal("10")
        assert payload["spend"]["unfinished_share"] == 0

    async def test_zero_merged_means_no_cost_per_pr(self) -> None:
        await _seed(_run("done", cost="10"))
        payload = overview_stats_service.build_header_stats(await _stats(), self._merged(), PRAGUE)
        assert payload["merged"]["count"] == 0
        assert payload["merged"]["cost_per_pr_usd"] is None

    @pytest.mark.parametrize("status", ["unavailable", "unsupported"])
    async def test_merged_not_counted_carries_null_figures(self, status: str) -> None:
        payload = overview_stats_service.build_header_stats(await _stats(), MergedPRs(status), PRAGUE)
        assert payload["merged"] == {
            "status": status,
            "count": None,
            "previous_count": None,
            "bots_left_out": None,
            "with_sova_run": None,
            "cost_per_pr_usd": None,
            "previous_cost_per_pr_usd": None,
        }

    async def test_unfinished_share(self) -> None:
        await _seed(_run("done", cost="80"), _run("failed", cost="15"), _run("paused", cost="5"))
        payload = overview_stats_service.build_header_stats(await _stats(), MergedPRs("unsupported"), PRAGUE)
        assert payload["spend"]["unfinished_usd"] == Decimal("20")
        assert payload["spend"]["unfinished_share"] == 0.2

    async def test_no_spend_means_no_share(self) -> None:
        payload = overview_stats_service.build_header_stats(await _stats(), MergedPRs("unsupported"), PRAGUE)
        assert payload["spend"]["unfinished_share"] is None


class TestOverviewStatsAPI:
    async def test_endpoint_returns_payload_and_echoes_zone(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.routers.overview.overview_stats_service.fetch_merged_prs",
            AsyncMock(return_value=MergedPRs("unsupported")),
        ):
            resp = await client.get("/api/overview/stats", params={"tz": "Europe/Prague"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["timezone"]["name"] == "Europe/Prague"
        assert body["merged"]["status"] == "unsupported"
        assert body["merged"]["count"] is None
        assert body["outcomes"]["week"]["success_rate"] is None

    async def test_unexpected_gh_error_still_returns_spend_and_success(self, client: AsyncClient) -> None:
        overview_stats_service.reset_merged_cache()
        await _seed(_run("done", cost="2"))
        with (
            patch.object(overview_stats_service, "load_config", return_value=_cfg()),
            patch.object(overview_stats_service, "list_merged_prs_since", AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            resp = await client.get("/api/overview/stats")
        overview_stats_service.reset_merged_cache()
        assert resp.status_code == 200
        body = resp.json()
        assert body["merged"]["status"] == "unavailable"
        assert body["merged"]["count"] is None
        assert body["spend"]["week_usd"] == 2
        assert body["outcomes"]["week"]["success"] == 1

    async def test_malformed_gh_payload_still_returns_spend_and_success(self, client: AsyncClient) -> None:
        overview_stats_service.reset_merged_cache()
        await _seed(_run("done", cost="2"))
        bad = [{**_pr(1, 1), "author": "x"}]
        for payload in (None, bad):
            with (
                patch.object(overview_stats_service, "load_config", return_value=_cfg()),
                patch.object(overview_stats_service, "list_merged_prs_since", AsyncMock(return_value=payload)),
            ):
                resp = await client.get("/api/overview/stats")
            overview_stats_service.reset_merged_cache()
            assert resp.status_code == 200
            body = resp.json()
            assert body["spend"]["week_usd"] == 2
            assert body["outcomes"]["week"]["success"] == 1
            assert body["merged"]["status"] == ("unavailable" if payload is None else "ok")

    async def test_failing_merged_task_reads_as_unavailable(self, client: AsyncClient) -> None:
        await _seed(_run("done", cost="2"))
        with patch(
            "sova.dashboard.routers.overview.overview_stats_service.fetch_merged_prs",
            AsyncMock(side_effect=AttributeError("surprise")),
        ):
            resp = await client.get("/api/overview/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["merged"]["status"] == "unavailable"
        assert body["spend"]["week_usd"] == 2

    async def test_invalid_zone_reads_as_utc(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.routers.overview.overview_stats_service.fetch_merged_prs",
            AsyncMock(return_value=MergedPRs("unavailable")),
        ):
            resp = await client.get("/api/overview/stats", params={"tz": "../../etc/passwd"})
        assert resp.status_code == 200
        assert resp.json()["timezone"] == {"name": "UTC", "abbreviation": "UTC"}

    async def test_costs_summary_today_starts_at_local_midnight(self) -> None:
        now = datetime.now(timezone.utc)
        midnight = cost_service.local_midnight(now, PRAGUE)
        inside, before = _run("done", cost="2"), _run("done", cost="5")
        inside.started_at = min(midnight + timedelta(seconds=1), now)
        before.started_at = midnight - timedelta(minutes=1)
        await _seed(inside, before)
        async with await get_session() as session:
            summary = await cost_service.get_summary(session, PRAGUE)
        assert summary["today_cost_usd"] == 2

    async def test_costs_summary_accepts_tz(self, client: AsyncClient) -> None:
        resp = await client.get("/api/costs/summary", params={"tz": "Europe/Prague"})
        assert resp.status_code == 200
        assert resp.json()["today_cost_usd"] == 0

    @pytest.mark.parametrize("path", ["/api/overview/stats", "/api/costs/summary"])
    async def test_tz_over_max_length_is_rejected(self, client: AsyncClient, path: str) -> None:
        resp = await client.get(path, params={"tz": "x" * 201})
        assert resp.status_code == 422

    async def test_database_failure_cancels_the_merged_fetch(self, client_no_raise: AsyncClient) -> None:
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def slow_fetch(_project_dir: object) -> MergedPRs:
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return MergedPRs("unavailable")

        async def failing_stats(*_args: object, **_kwargs: object) -> None:
            await started.wait()
            raise RuntimeError("db down")

        with (
            patch("sova.dashboard.routers.overview.overview_stats_service.fetch_merged_prs", slow_fetch),
            patch("sova.dashboard.routers.overview.overview_stats_service.get_run_stats", failing_stats),
        ):
            resp = await client_no_raise.get("/api/overview/stats")
        assert resp.status_code == 500
        await asyncio.wait_for(cancelled.wait(), timeout=1)
