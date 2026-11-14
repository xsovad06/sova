"""Last-result snapshots for the work items and the handoff (issue #1459, the server half)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.dashboard.routers import agents as agents_router
from sova.dashboard.routers import handoff as handoff_router
from sova.dashboard.services import snapshot_service
from sova.dashboard.services.work_item_service import normalize_work_items_for_digest


@pytest.fixture(autouse=True)
def _listener():
    """Record the change notifications; restore the router's listener afterwards."""
    notified: list[tuple[str, Path | None]] = []

    async def listener(kind: str, project_dir: Path | None) -> None:
        notified.append((kind, project_dir))

    snapshot_service.set_change_listener(listener)
    yield notified
    snapshot_service.set_change_listener(agents_router._on_snapshot_changed)


class TestGetSnapshot:
    async def test_the_first_call_waits_for_the_build_and_later_calls_reuse_it(self) -> None:
        build = AsyncMock(return_value={"items": [1]})

        first = await snapshot_service.get_snapshot("work_items", None, build, ttl=60)
        second = await snapshot_service.get_snapshot("work_items", None, build, ttl=60)

        assert first.payload == second.payload == {"items": [1]}
        assert build.await_count == 1

    async def test_a_failed_first_build_raises_instead_of_returning_an_empty_payload(self) -> None:
        build = AsyncMock(side_effect=RuntimeError("github down"))

        with pytest.raises(RuntimeError, match="github down"):
            await snapshot_service.get_snapshot("work_items", None, build, ttl=60)

        # Nothing was stored, so the next call builds again rather than serving a placeholder.
        build.side_effect = None
        build.return_value = {"items": []}
        assert (await snapshot_service.get_snapshot("work_items", None, build, ttl=60)).payload == {"items": []}

    async def test_an_expired_snapshot_answers_at_once_while_a_blocked_rebuild_runs(self) -> None:
        await snapshot_service.get_snapshot("work_items", None, AsyncMock(return_value={"items": [1]}), ttl=0)
        blocked = asyncio.Event()
        started = asyncio.Event()

        async def github_blocked() -> dict:
            started.set()
            await blocked.wait()
            return {"items": [1, 2]}

        began = time.monotonic()
        snapshot = await snapshot_service.get_snapshot("work_items", None, github_blocked, ttl=0)
        elapsed = time.monotonic() - began

        assert snapshot.payload == {"items": [1]}
        assert elapsed < 0.1
        await asyncio.wait_for(started.wait(), timeout=5)
        blocked.set()
        await snapshot_service.cancel_snapshot_builds()

    async def test_concurrent_requests_during_a_rebuild_share_one_build(self) -> None:
        await snapshot_service.get_snapshot("work_items", None, AsyncMock(return_value={"items": [1]}), ttl=0)
        release = asyncio.Event()
        calls = 0

        async def slow() -> dict:
            nonlocal calls
            calls += 1
            await release.wait()
            return {"items": [2]}

        answers = await asyncio.gather(
            *(snapshot_service.get_snapshot("work_items", None, slow, ttl=0) for _ in range(5))
        )
        release.set()
        await asyncio.sleep(0)

        assert [a.payload for a in answers] == [{"items": [1]}] * 5
        assert calls == 1
        await snapshot_service.cancel_snapshot_builds()


class TestRefresh:
    async def test_an_unchanged_rebuild_notifies_nothing_and_a_changed_one_does(self, _listener) -> None:
        payloads = iter([{"items": [1]}, {"items": [1]}, {"items": [2]}])

        async def build() -> dict:
            return next(payloads)

        results = [await snapshot_service.refresh_snapshot("work_items", None, build) for _ in range(3)]

        # The first build has no previous payload to differ from; the second equals it; the third changes.
        assert results == [False, False, True]
        assert _listener == [("work_items", None)]

    async def test_a_failed_rebuild_keeps_the_last_payload_and_records_the_failure(self) -> None:
        build = AsyncMock(side_effect=[{"items": [1]}, RuntimeError("tracker 502")])
        await snapshot_service.refresh_snapshot("work_items", None, build)

        assert await snapshot_service.refresh_snapshot("work_items", None, build) is False
        snapshot = await snapshot_service.get_snapshot("work_items", None, build, ttl=60)

        assert snapshot.payload == {"items": [1]}
        meta = snapshot.meta()
        assert meta["refresh_failed"] is True
        assert "tracker 502" in meta["last_error"]

    async def test_a_rebuild_that_recovers_from_a_failure_notifies_even_with_the_same_payload(self, _listener) -> None:
        build = AsyncMock(side_effect=[{"items": [1]}, RuntimeError("down"), {"items": [1]}])
        for _ in range(3):
            await snapshot_service.refresh_snapshot("work_items", None, build)

        snapshot = await snapshot_service.get_snapshot("work_items", None, build, ttl=60)
        assert snapshot.meta()["refresh_failed"] is False
        assert _listener == [("work_items", None)]

    async def test_a_listener_that_raises_does_not_fail_the_rebuild(self) -> None:
        async def broken(_kind: str, _project_dir: Path | None) -> None:
            raise RuntimeError("socket gone")

        snapshot_service.set_change_listener(broken)
        build = AsyncMock(side_effect=[{"items": [1]}, {"items": [2]}])
        await snapshot_service.refresh_snapshot("work_items", None, build)

        assert await snapshot_service.refresh_snapshot("work_items", None, build) is True

    async def test_a_build_that_started_before_an_invalidation_does_not_store_its_result(self) -> None:
        release = asyncio.Event()

        async def build() -> dict:
            await release.wait()
            return {"old": True}

        task = asyncio.create_task(snapshot_service.refresh_snapshot("handoff", None, build))
        await asyncio.sleep(0)
        snapshot_service.invalidate_snapshot("handoff", None)
        release.set()
        await task

        fresh = AsyncMock(return_value={"old": False})
        assert (await snapshot_service.get_snapshot("handoff", None, fresh, ttl=60)).payload == {"old": False}


class TestDigest:
    def test_clock_driven_fields_stay_out_of_the_work_items_digest(self) -> None:
        a = {
            "items": [
                {"running_agent": {"elapsed_seconds": 5, "run_id": 1}, "pr_details": {"age_seconds": 10, "n": 3}}
            ],
            "api_health": {"status": "rate_limited", "cooldown_seconds": 41, "hits": 2},
        }
        b = {
            "items": [
                {"running_agent": {"elapsed_seconds": 9, "run_id": 1}, "pr_details": {"age_seconds": 14, "n": 3}}
            ],
            "api_health": {"status": "rate_limited", "cooldown_seconds": 37, "hits": 2},
        }
        c = {**b, "items": [{"running_agent": {"elapsed_seconds": 9, "run_id": 2}, "pr_details": {"n": 3}}]}

        assert normalize_work_items_for_digest(a) == normalize_work_items_for_digest(b)
        assert normalize_work_items_for_digest(b) != normalize_work_items_for_digest(c)
        assert a["api_health"]["cooldown_seconds"] == 41, "the payload itself is not modified"

    async def test_a_rebuild_that_only_moves_a_clock_sends_nothing(self, _listener) -> None:
        payloads = iter(
            [
                {"items": [{"running_agent": {"elapsed_seconds": 5}}], "api_health": {"status": "ok"}},
                {"items": [{"running_agent": {"elapsed_seconds": 65}}], "api_health": {"status": "ok"}},
            ]
        )

        async def build() -> dict:
            return next(payloads)

        for _ in range(2):
            await snapshot_service.refresh_snapshot(
                "work_items", None, build, normalize=normalize_work_items_for_digest
            )

        assert _listener == []


class TestWorkItemsEndpoint:
    async def test_the_endpoint_answers_after_the_ttl_with_the_tracker_blocked(self, tmp_path: Path) -> None:
        release = asyncio.Event()
        builds = 0

        async def build_work_items(_project_dir: Path | None) -> dict:
            nonlocal builds
            builds += 1
            if builds > 1:
                await release.wait()  # the tracker is blocked from the second build on
            return {"items": [{"issue_number": builds}], "running_count": 0}

        with (
            patch("sova.config.context.get_project_dir", return_value=tmp_path),
            patch("sova.dashboard.services.work_item_service.build_work_items", build_work_items),
        ):
            first = await agents_router.get_work_items()
            began = time.monotonic()
            # Age the snapshot past its TTL, as five minutes of an idle tab would.
            snapshot_service._snapshots[snapshot_service._key("work_items", tmp_path)].built_at -= 600
            second = await agents_router.get_work_items()
            elapsed = time.monotonic() - began
            release.set()
            await snapshot_service.cancel_snapshot_builds()

        assert elapsed < 0.1
        assert second["items"] == first["items"] == [{"issue_number": 1}]
        assert second["snapshot"]["age_seconds"] >= 600
        assert second["snapshot"]["refresh_failed"] is False

    async def test_the_first_request_fails_loudly_when_the_first_build_fails(self, tmp_path: Path) -> None:
        with (
            patch("sova.config.context.get_project_dir", return_value=tmp_path),
            patch(
                "sova.dashboard.services.work_item_service.build_work_items",
                AsyncMock(side_effect=RuntimeError("no tracker")),
            ),
            pytest.raises(RuntimeError, match="no tracker"),
        ):
            await agents_router.get_work_items()

    async def test_a_failed_rebuild_is_reported_in_the_snapshot_metadata(self, tmp_path: Path) -> None:
        builder = AsyncMock(side_effect=[{"items": [1]}, RuntimeError("tracker 502")])
        with (
            patch("sova.config.context.get_project_dir", return_value=tmp_path),
            patch("sova.dashboard.services.work_item_service.build_work_items", builder),
        ):
            first = await agents_router.get_work_items()
            await snapshot_service.refresh_snapshot("work_items", tmp_path, lambda: builder(tmp_path))
            second = await agents_router.get_work_items()

        assert first["snapshot"]["refresh_failed"] is False
        assert second["items"] == [1]
        assert second["snapshot"]["refresh_failed"] is True


class TestHandoffEndpoint:
    async def test_the_project_wide_read_is_answered_from_the_last_result_while_a_rebuild_is_blocked(
        self, tmp_path: Path
    ) -> None:
        release = asyncio.Event()
        reads = 0

        async def read(issue: str | None) -> dict:
            nonlocal reads
            reads += 1
            if reads > 1:
                await release.wait()  # the synthesis waits on GitHub
            return {"has_handoff": False, "handoffs": [], "n": reads}

        with (
            patch.object(handoff_router, "_project_dir", return_value=tmp_path),
            patch.object(handoff_router, "_read_handoff", read),
        ):
            first = await handoff_router.get_handoff(issue=None)
            snapshot_service._snapshots[snapshot_service._key("handoff", tmp_path)].built_at -= 600
            began = time.monotonic()
            second = await handoff_router.get_handoff(issue=None)
            elapsed = time.monotonic() - began
            release.set()
            await snapshot_service.cancel_snapshot_builds()

        assert second == first
        assert elapsed < 0.1

    async def test_a_read_for_one_issue_is_built_on_the_spot(self, tmp_path: Path) -> None:
        read = AsyncMock(return_value={"has_handoff": False, "handoffs": []})
        with (
            patch.object(handoff_router, "_project_dir", return_value=tmp_path),
            patch.object(handoff_router, "_read_handoff", read),
        ):
            await handoff_router.get_handoff(issue="7")
            await handoff_router.get_handoff(issue="7")

        assert read.await_count == 2

    async def test_clearing_a_handoff_drops_the_snapshot_so_the_next_read_is_fresh(self, tmp_path: Path) -> None:
        reads = iter([{"has_handoff": True, "handoffs": [1]}, {"has_handoff": False, "handoffs": []}])
        with (
            patch.object(handoff_router, "_project_dir", return_value=tmp_path),
            patch.object(handoff_router, "_read_handoff", AsyncMock(side_effect=lambda issue: next(reads))),
            patch.object(handoff_router.handoff_service, "clear_handoff", return_value=1),
        ):
            before = await handoff_router.get_handoff(issue=None)
            await handoff_router.clear_handoff(None)
            after = await handoff_router.get_handoff(issue=None)

        assert before["has_handoff"] is True
        assert after["has_handoff"] is False

    async def test_the_producer_build_raises_when_its_rebuild_failed_so_the_socket_reports_unavailable(
        self, tmp_path: Path
    ) -> None:
        reads = [{"has_handoff": False, "handoffs": []}, RuntimeError("github 502")]
        with (
            patch.object(handoff_router, "_project_dir", return_value=tmp_path),
            patch.object(handoff_router, "_read_handoff", AsyncMock(side_effect=reads)),
        ):
            assert await handoff_router.build_handoff_snapshot() == {"has_handoff": False, "handoffs": []}
            with pytest.raises(RuntimeError, match="handoff rebuild failed"):
                await handoff_router.build_handoff_snapshot()
