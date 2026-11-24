"""The Work board answers from a snapshot and learns about changes without waiting on GitHub (#1534).

Covers the board snapshot behind GET /api/work/board, the shared open-PR fetch, the rerun of a refresh asked for
during a build, the status producer's run transitions, the invalidation order in the dashboard's finalize, the
conditional GitHub probe and the visible-Work-tab gate that decides whether it runs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.services import (
    agent_finalize,
    pr_service,
    queue_service,
    snapshot_service,
    work_board_service,
    work_item_service,
    work_refresh_service,
    work_verdict,
)
from sova.dashboard.services.work_refresh_service import RunMark, board_transitions
from sova.git.conditional import ConditionalProbe, ProbeOutcome
from sova.git.pr import PRLookupError
from sova.utils.shell import ShellResult
from tests.conftest import csrf_request_headers


@pytest.fixture
async def client(dashboard_app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=dashboard_app), base_url="http://test") as ac:
        ac.headers.update(csrf_request_headers(ac))
        yield ac


def _reset_module_state() -> None:
    work_board_service.reset_board_cache()
    work_refresh_service.reset_probes()
    snapshot_service.reset_snapshots()
    work_item_service.reset_graph_load_state()
    agent_finalize._finalized_runs.clear()
    pr_service._pr_cache.clear()
    pr_service._pr_fetch_failed_until.clear()
    pr_service._pr_inflight.clear()


@pytest.fixture(autouse=True)
async def _fresh_state() -> AsyncIterator[None]:
    # Snapshots under a fixed key (("kind", None)) would otherwise let a later test skip its first build.
    await snapshot_service.cancel_snapshot_builds()
    _reset_module_state()
    yield
    await snapshot_service.cancel_snapshot_builds()
    await work_item_service.cancel_graph_builds()
    _reset_module_state()


async def _settle_builds() -> None:
    """Wait until no snapshot build (a rerun included) is in flight."""
    for _ in range(50):
        running = [t for t in snapshot_service._builds.values() if not t.done()]
        if not running:
            await asyncio.sleep(0)
            if not any(not t.done() for t in snapshot_service._builds.values()):
                return
            continue
        await asyncio.gather(*running, return_exceptions=True)
    raise AssertionError("snapshot builds did not settle")


# Board snapshot ------------------------------------------------------------------


class TestBoardSnapshot:
    async def test_the_board_answers_from_its_snapshot_at_once_while_github_is_blocked(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        blocked = asyncio.Event()
        builds = []

        async def build(_project: Path, milestone: str = "", *, owner: str = "all") -> dict:
            builds.append(milestone)
            if len(builds) > 1:
                await blocked.wait()  # every later build waits on GitHub, which never answers
            return {"up_next": [{"number": 1}], "running": [{"number": 2, "elapsed_seconds": 10}], "stages": []}

        with (
            patch("sova.dashboard.routers.work.get_project_dir", return_value=tmp_path),
            patch.object(work_board_service, "build_board", side_effect=build),
        ):
            first = await client.get("/api/work/board")
            # The source caches lapsed long ago: the snapshot is old enough that this request starts a rebuild.
            kind = work_board_service._board_kind("", "all")
            snapshot_service.peek_snapshot(kind, tmp_path).built_at -= 3600
            started = time.monotonic()
            second = await client.get("/api/work/board")
            elapsed = time.monotonic() - started
            await snapshot_service.cancel_snapshot_builds()

        assert first.status_code == second.status_code == 200
        assert second.json()["up_next"] == [{"number": 1}]
        assert second.json()["snapshot"]["age_seconds"] >= 3600
        assert second.json()["running"][0]["elapsed_seconds"] >= 3610, "a running timer counts from now, not the build"
        assert elapsed < 0.1
        assert len(builds) == 2, "the lapsed snapshot started one background rebuild"

    async def test_concurrent_first_requests_share_one_build(self, client: AsyncClient, tmp_path: Path) -> None:
        release = asyncio.Event()

        async def build(_project: Path, milestone: str = "", *, owner: str = "all") -> dict:
            await release.wait()
            return {"up_next": []}

        builder = AsyncMock(side_effect=build)
        with (
            patch("sova.dashboard.routers.work.get_project_dir", return_value=tmp_path),
            patch.object(work_board_service, "build_board", builder),
        ):
            requests = [asyncio.create_task(client.get("/api/work/board")) for _ in range(3)]
            await asyncio.sleep(0.05)
            release.set()
            answers = await asyncio.gather(*requests)

        assert [a.status_code for a in answers] == [200, 200, 200]
        assert builder.await_count == 1

    async def test_a_changed_rebuild_sends_work_items_changed_and_an_unchanged_one_nothing(
        self, tmp_path: Path
    ) -> None:
        from sova.dashboard.routers import agents

        payloads = iter(
            [
                {"running": [{"number": 1, "elapsed_seconds": 10, "elapsed_formatted": "10s"}]},
                # Only the clock moved: not a change.
                {"running": [{"number": 1, "elapsed_seconds": 70, "elapsed_formatted": "1m 10s"}]},
                {"running": [{"number": 1, "elapsed_seconds": 75}], "review": [{"number": 2}]},
            ]
        )

        async def build(_project: Path, milestone: str = "", *, owner: str = "all") -> dict:
            return next(payloads)

        broadcast = AsyncMock()
        with (
            patch.object(work_board_service, "build_board", side_effect=build),
            patch.object(agents._ws_manager, "broadcast_event", broadcast),
        ):
            await work_board_service.get_board_snapshot(tmp_path)
            work_board_service.refresh_board_snapshots(tmp_path)
            await _settle_builds()
            unchanged = broadcast.await_count
            work_board_service.refresh_board_snapshots(tmp_path)
            await _settle_builds()

        assert unchanged == 0
        broadcast.assert_awaited_once_with("work_items_changed", tmp_path.resolve())

    async def test_a_failed_rebuild_keeps_serving_the_last_board(self, tmp_path: Path) -> None:
        builder = AsyncMock(side_effect=[{"up_next": [{"number": 1}]}, RuntimeError("github down")])
        with patch.object(work_board_service, "build_board", builder):
            await work_board_service.get_board_snapshot(tmp_path)
            work_board_service.refresh_board_snapshots(tmp_path)
            await _settle_builds()
            snapshot = await work_board_service.get_board_snapshot(tmp_path)

        assert snapshot.payload == {"up_next": [{"number": 1}]}
        assert snapshot.meta()["refresh_failed"] is True

    async def test_hiding_an_issue_rebuilds_the_board_before_the_writer_reloads(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        builder = AsyncMock(side_effect=[{"hidden": []}, {"hidden": [{"number": 5}]}])
        with (
            patch("sova.dashboard.routers.work.get_project_dir", return_value=tmp_path),
            patch("sova.dashboard.routers.work.set_issue_hidden", AsyncMock(return_value=[5])),
            patch.object(work_board_service, "build_board", builder),
        ):
            await client.get("/api/work/board")
            hide = await client.post("/api/work/hidden/5")
            after = await client.get("/api/work/board")

        assert hide.status_code == 200
        assert after.json()["hidden"] == [{"number": 5}]
        assert builder.await_count == 2, "the writer's reload is answered from the rebuild, not a third build"

    async def test_a_hide_keeps_the_board_answering_the_other_tabs_until_the_rebuild_lands(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """No tab but the writer's waits on the tracker after a hide, a queue edit or a priority change."""
        release = asyncio.Event()

        async def build(_project: Path, _milestone: str = "", *, owner: str = "all") -> dict:
            if builder.await_count > 1:
                await release.wait()  # the rebuild after the hide: GitHub is slow
                return {"hidden": [{"number": 5}]}
            return {"hidden": []}

        builder = AsyncMock(side_effect=build)
        broadcast = AsyncMock()
        with (
            patch("sova.dashboard.routers.work.get_project_dir", return_value=tmp_path),
            patch("sova.dashboard.routers.work.set_issue_hidden", AsyncMock(return_value=[5])),
            patch.object(work_board_service, "build_board", builder),
            patch("sova.dashboard.routers.agents._ws_manager.broadcast_event", broadcast),
        ):
            await client.get("/api/work/board")
            hide = asyncio.create_task(client.post("/api/work/hidden/5"))
            for _ in range(20):
                await asyncio.sleep(0)
            other_tab = await asyncio.wait_for(client.get("/api/work/board"), timeout=2)
            writer_waits = not hide.done()
            release.set()
            await asyncio.wait_for(hide, timeout=5)

        assert other_tab.json()["hidden"] == [], "the other tab is answered at once, from the previous board"
        assert writer_waits, "the writer's request returns only once the rebuild has landed"
        assert broadcast.await_args_list[0].args[:1] == ("work_items_changed",), "the rebuilt board tells the tabs"

    async def test_a_key_not_asked_for_recently_drops_its_snapshot_with_it(self, tmp_path: Path) -> None:
        # Otherwise a hide after a long idle finds no key to refresh, and the next load answers from before it.
        builder = AsyncMock(side_effect=[{"hidden": []}, {"hidden": [{"number": 5}]}])
        with patch.object(work_board_service, "build_board", builder):
            await work_board_service.get_board_snapshot(tmp_path)
            for key in work_board_service._board_requests:
                work_board_service._board_requests[key] -= work_board_service._BOARD_RECENT_SECONDS + 1
            assert work_board_service.refresh_board_snapshots(tmp_path) is False
            after = await work_board_service.get_board_snapshot(tmp_path)

        assert after.payload == {"hidden": [{"number": 5}]}
        assert builder.await_count == 2


class TestRefreshDuringABuild:
    async def test_a_refresh_asked_for_during_a_build_runs_once_more_after_it(self) -> None:
        release = asyncio.Event()
        calls = 0

        async def build() -> dict:
            nonlocal calls
            calls += 1
            if calls == 2:
                await release.wait()
            return {"n": calls}

        await snapshot_service.get_snapshot("kind", None, build)
        snapshot_service.request_refresh("kind", None, build)
        await asyncio.sleep(0)
        # Two invalidations land while that build may have read the sources before them: one rerun covers both.
        snapshot_service.request_refresh("kind", None, build)
        snapshot_service.request_refresh("kind", None, build)
        release.set()
        await _settle_builds()

        assert calls == 3
        assert snapshot_service.peek_snapshot("kind", None).payload == {"n": 3}


# One shared open-PR fetch -----------------------------------------------------------


@pytest.fixture
def pr_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    cfg = SimpleNamespace(
        github_repo="owner/repo",
        github_user="alice",
        task_source=SimpleNamespace(type="github", jira_project_key=""),
    )
    monkeypatch.setattr(pr_service, "load_config", lambda _p: cfg)
    monkeypatch.setattr(work_item_service, "load_config", lambda _p: cfg)
    monkeypatch.setattr("sova.git.pr.get_pr_review_data", AsyncMock(return_value={}))
    monkeypatch.setattr(pr_service, "_enrich_pr", lambda pr, now: {**pr, "computed_state": "test"})
    monkeypatch.setattr(pr_service, "_record_state_transitions", AsyncMock())
    monkeypatch.setattr(pr_service, "get_active_gh_user", AsyncMock(return_value="alice"))
    monkeypatch.setattr(
        "sova.dashboard.services.agent_lifecycle.get_unified_agents",
        AsyncMock(return_value={"agents": [], "completed": []}),
    )
    return SimpleNamespace(project=tmp_path)


class TestSharedOpenPrFetch:
    async def test_the_board_sources_hitting_cold_caches_at_once_run_one_gh_pr_list(
        self, monkeypatch: pytest.MonkeyPatch, pr_sources: SimpleNamespace
    ) -> None:
        async def slow_list(**_kwargs: object) -> list[dict]:
            await asyncio.sleep(0.02)
            return [{"number": 7, "body": "Closes #3", "headRefName": "feat/x"}]

        listed = AsyncMock(side_effect=slow_list)
        monkeypatch.setattr("sova.git.pr.list_open_prs", listed)
        project = pr_sources.project

        work_items_prs, exclusions, queue_prs = await asyncio.gather(
            work_item_service._safe_prs(project),
            work_item_service.board_exclusions(project),
            queue_service._fetch_open_prs(project, None),
        )

        assert listed.await_count == 1
        assert [p["number"] for p in work_items_prs] == [7]
        assert [p["number"] for p in queue_prs] == [7]
        assert exclusions[2] is not None, "the exclusions read the shared fetch, not a failed one"

    async def test_a_failed_fetch_reaches_every_waiter_and_is_not_cached(
        self, monkeypatch: pytest.MonkeyPatch, pr_sources: SimpleNamespace
    ) -> None:
        async def failing_list(**_kwargs: object) -> list[dict]:
            await asyncio.sleep(0.02)
            raise PRLookupError("gh pr list failed")

        listed = AsyncMock(side_effect=failing_list)
        monkeypatch.setattr("sova.git.pr.list_open_prs", listed)

        results = await asyncio.gather(
            *(pr_service.list_open_prs_with_state(pr_sources.project, raise_on_error=True) for _ in range(3)),
            return_exceptions=True,
        )
        quiet = await pr_service.list_open_prs_with_state(pr_sources.project)

        assert [type(r) for r in results] == [PRLookupError] * 3
        assert listed.await_count == 1
        assert pr_service._pr_cache == {}
        assert quiet == []


# Local transitions ----------------------------------------------------------------


def _mark(pr: int | None = None, *steps: str, step: str = "develop") -> RunMark:
    return RunMark(pr_number=pr, finished_steps=frozenset(steps), status="running", current_step=step)


class TestBoardTransitions:
    @pytest.mark.parametrize(
        ("previous", "current", "expected"),
        [
            pytest.param(None, {1: _mark(10, "create_pr")}, (False, set()), id="first tick only records"),
            pytest.param({1: _mark(10)}, {}, (True, {10}), id="run leaves the active set"),
            pytest.param({1: _mark()}, {}, (True, set()), id="run with no PR leaves"),
            pytest.param({1: _mark()}, {1: _mark(10)}, (True, {10}), id="run gains a pr_number"),
            pytest.param({1: _mark(10)}, {1: _mark(11)}, (False, set()), id="pr_number set again"),
            pytest.param({1: _mark(10)}, {1: _mark(10, "create_pr")}, (True, {10}), id="create_pr finishes"),
            pytest.param(
                {1: _mark(10, "create_pr")}, {1: _mark(10, "create_pr", "monitor_ci")}, (True, {10}), id="monitor_ci"
            ),
            pytest.param({1: _mark(10)}, {1: _mark(10, step="validate")}, (False, set()), id="other step"),
            pytest.param({}, {2: _mark(10, "create_pr")}, (False, set()), id="new run already past create_pr"),
            pytest.param(
                {1: _mark(10), 2: _mark()}, {2: _mark(12)}, (True, {10, 12}), id="several transitions in one tick"
            ),
        ],
    )
    def test_board_transitions(self, previous: dict | None, current: dict, expected: tuple[bool, set[int]]) -> None:
        assert board_transitions(previous, current) == expected

    async def test_a_transition_invalidates_the_pr_queue_verdict_and_merged_caches(self, tmp_path: Path) -> None:
        project = tmp_path.resolve()
        pr_service._pr_cache[("owner/repo", "alice")] = (time.monotonic(), [])
        queue_service._queue_cache[str(project)] = (time.monotonic(), [])
        work_verdict._sova_verdict_cache[work_verdict._cache_key(project, 10)] = (time.monotonic(), {})
        work_board_service._merged_cache[(f"{project}:owner/repo", date.today())] = (time.monotonic(), [])
        cfg = SimpleNamespace(github_repo="owner/repo")

        try:
            with patch.object(agent_finalize, "load_config", return_value=cfg):
                work_refresh_service.apply_run_changes(project, {1: _mark(10)}, {})
        finally:
            work_verdict.clear_verdict_cache()

        assert pr_service._pr_cache == {}
        assert str(project) not in queue_service._queue_cache
        assert work_board_service._merged_cache == {}

    @pytest.mark.parametrize(
        ("current", "invalidated", "refreshed"),
        [
            pytest.param({}, 1, 0, id="a transition invalidates once"),
            pytest.param({1: _mark(10, step="validate")}, 0, 1, id="a step change only rebuilds the snapshots"),
            pytest.param({1: _mark(10)}, 0, 0, id="nothing moved"),
        ],
    )
    def test_what_the_producer_does_for_a_tick(
        self, tmp_path: Path, current: dict, invalidated: int, refreshed: int
    ) -> None:
        with (
            patch.object(agent_finalize, "invalidate_work_item_caches") as invalidate,
            patch.object(work_board_service, "refresh_work_snapshots") as refresh,
        ):
            work_refresh_service.apply_run_changes(tmp_path, {1: _mark(10)}, current)

        assert invalidate.call_count == invalidated
        assert refresh.call_count == refreshed

    async def test_the_producer_invalidates_before_the_tick_it_saw_the_run_leave(self, tmp_path: Path) -> None:
        """A CLI run leaving the active set is seen by the producer alone: it invalidates before sending the tick."""
        from sova.dashboard.routers import agents
        from sova.dashboard.services.agent_status import AgentStatus

        def status(run_id: int) -> AgentStatus:
            return AgentStatus(
                run_id=run_id,
                status="running",
                role="developer",
                pipeline_variant="developer",
                current_step="develop",
                step_index=0,
                total_steps=1,
                step_progress_pct=0.0,
                time_in_step_ms=0,
                time_in_step_known=True,
                is_stuck=False,
                estimated_remaining_ms=None,
                issue="3",
                pid=None,
                elapsed_seconds=0,
                cost_usd="0.00",
                pr_number=10,
            )

        ticks = iter([[status(1)], []])
        events: list[str] = []
        manager = agents._ConnectionManager()
        ws = MagicMock()
        manager._groups[tmp_path] = [ws]

        async def statuses(**_kwargs: object) -> list[AgentStatus]:
            try:
                return next(ticks)
            except StopIteration:
                manager._groups.clear()  # ends the loop after the second tick
                return []

        async def broadcast(message: dict, _project: Path | None) -> None:
            events.append(f"{message['type']}:{len(message.get('runs', []))}")

        with (
            patch("sova.dashboard.services.agent_status.get_all_agent_statuses", side_effect=statuses),
            patch.object(manager, "_broadcast", side_effect=broadcast),
            patch.object(agents, "_STATUS_BROADCAST_INTERVAL", 0),
            patch.object(agents, "_build_activity_message", AsyncMock(return_value={"type": "activity"})),
            patch.object(agents, "_build_handoff_message", AsyncMock(return_value={"type": "handoff"})),
            patch.object(agents, "_refresh_work_items_snapshot", AsyncMock()),
            patch.object(
                agent_finalize, "invalidate_work_item_caches", side_effect=lambda *a, **k: events.append("invalidate")
            ),
        ):
            await manager._produce_loop(tmp_path)

        assert events.index("invalidate") < events.index("status_update:0")
        assert events.count("invalidate") == 1


class TestFinalizeOrder:
    async def test_caches_are_invalidated_before_the_status_write_and_agent_finished(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        order: list[str] = []
        agent = AgentState(run_id=5, issue="77", role="developer", process=MagicMock(), project_dir=tmp_path)
        agent.pr_number = 42
        pa = ProjectAgents(project_dir=tmp_path)
        pa.agents[5] = agent

        async def finalize_run(*_args: object, **_kwargs: object) -> bool:
            order.append("status_written")
            return True

        async def broadcast(event: str, *_args: object, **_kwargs: object) -> None:
            order.append(event)

        with (
            patch.object(agent_finalize, "_wait_with_terminal_check", new=AsyncMock(return_value=0)),
            patch.object(agent_finalize, "_finalize_task_run", side_effect=finalize_run),
            patch.object(agent_finalize, "_validate_command_outcome", new=AsyncMock()) as validate,
            patch.object(agent_finalize, "_validate_pipeline_outcome", new=AsyncMock(return_value=None)),
            patch("sova.dashboard.services.agent_resource._finalize_resource_monitoring", new=AsyncMock()),
            patch.object(agent_finalize, "_get_task_run_status", new=AsyncMock(return_value="done")),
            patch("sova.dashboard.routers.agents._ws_manager.broadcast_event", side_effect=broadcast),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new=AsyncMock()),
            patch("sova.ipc.notifications.notify"),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new=AsyncMock()),
            patch.object(
                agent_finalize,
                "invalidate_work_item_caches",
                side_effect=lambda _p, *prs, **_k: order.append(f"invalidated:{list(prs)}"),
            ),
        ):
            validate.return_value = SimpleNamespace(failure_reason=None, network_blocked=False)
            await asyncio.wait_for(agent_finalize._wait_and_finalize(pa, agent), timeout=10)

        assert order.index("invalidated:[42]") < order.index("status_written") < order.index("agent_finished")


# GitHub probe -----------------------------------------------------------------------

_HEAD_200 = 'HTTP/2.0 200 OK\r\nEtag: "abc"\r\nContent-Type: application/json\r\n\r\n[{"number": 1}]'
_HEAD_304 = 'HTTP/2.0 304 Not Modified\r\nEtag: "abc"\r\n\r\n'


def _gh(*results: ShellResult) -> AsyncMock:
    return AsyncMock(side_effect=list(results))


class TestConditionalProbe:
    async def test_a_304_from_a_failing_gh_exit_is_unchanged_and_resends_the_etag(self) -> None:
        run = _gh(
            ShellResult(returncode=0, stdout=_HEAD_200, stderr=""),
            # gh api exits 1 on a 304: the status line, not the exit code, decides.
            ShellResult(returncode=1, stdout=_HEAD_304, stderr="gh: HTTP 304"),
        )
        probe = ConditionalProbe()
        with (
            patch("sova.git.conditional.run", run),
            patch("sova.git.conditional.resolve_gh_env", AsyncMock(return_value=None)),
        ):
            first = await probe.check("repos/o/r/pulls")
            probe.commit("repos/o/r/pulls")
            second = await probe.check("repos/o/r/pulls")

        assert (first, second) == (ProbeOutcome.CHANGED, ProbeOutcome.UNCHANGED)
        assert 'If-None-Match: "abc"' not in run.await_args_list[0].args
        assert 'If-None-Match: "abc"' in run.await_args_list[1].args

    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            pytest.param(ShellResult(1, "", "connection refused"), ProbeOutcome.FAILED, id="no answer"),
            pytest.param(ShellResult(1, "HTTP/2.0 500 Internal\r\n\r\n", ""), ProbeOutcome.FAILED, id="server error"),
            pytest.param(
                ShellResult(1, "HTTP/2.0 403 Forbidden\r\nX-Ratelimit-Remaining: 0\r\n\r\n", ""),
                ProbeOutcome.RATE_LIMITED,
                id="rate limit 403",
            ),
            pytest.param(ShellResult(1, "HTTP/2.0 429 Too Many\r\n\r\n", ""), ProbeOutcome.RATE_LIMITED, id="429"),
            pytest.param(ShellResult(0, "", "", timed_out=True), ProbeOutcome.FAILED, id="timed out"),
        ],
    )
    async def test_a_failed_probe_is_not_unchanged_and_keeps_the_etag(
        self, result: ShellResult, expected: ProbeOutcome
    ) -> None:
        run = _gh(ShellResult(0, _HEAD_200, ""), result, ShellResult(1, _HEAD_304, "gh: HTTP 304"))
        probe = ConditionalProbe()
        with (
            patch("sova.git.conditional.run", run),
            patch("sova.git.conditional.resolve_gh_env", AsyncMock(return_value=None)),
        ):
            await probe.check("repos/o/r/issues")
            probe.commit("repos/o/r/issues")
            failed = await probe.check("repos/o/r/issues")
            after = await probe.check("repos/o/r/issues")

        assert failed is expected
        assert after is ProbeOutcome.UNCHANGED
        assert 'If-None-Match: "abc"' in run.await_args_list[2].args


def _probe_config(tracker: str = "github") -> SimpleNamespace:
    return SimpleNamespace(github_repo="owner/repo", github_user="alice", task_source=SimpleNamespace(type=tracker))


class TestProbeOnce:
    @pytest.mark.parametrize(
        ("prs", "issues", "expected_kwargs"),
        [
            pytest.param(ProbeOutcome.UNCHANGED, ProbeOutcome.UNCHANGED, None, id="304s invalidate nothing"),
            pytest.param(ProbeOutcome.CHANGED, ProbeOutcome.UNCHANGED, {"every_verdict": True}, id="pr list changed"),
            pytest.param(ProbeOutcome.UNCHANGED, ProbeOutcome.CHANGED, {"prs": False}, id="issue list changed"),
            pytest.param(ProbeOutcome.CHANGED, ProbeOutcome.CHANGED, {"every_verdict": True}, id="both, once"),
            pytest.param(ProbeOutcome.FAILED, ProbeOutcome.FAILED, None, id="failures invalidate nothing"),
        ],
    )
    async def test_only_a_changed_list_invalidates(
        self, tmp_path: Path, prs: ProbeOutcome, issues: ProbeOutcome, expected_kwargs: dict | None
    ) -> None:
        check = AsyncMock(side_effect=[prs, issues])
        listed = AsyncMock(return_value=[])
        with (
            patch.object(work_refresh_service, "load_config", return_value=_probe_config()),
            patch.object(ConditionalProbe, "check", check),
            patch.object(agent_finalize, "invalidate_work_item_caches") as invalidate,
            patch("sova.git.pr.list_open_prs", listed),
            patch("sova.supervisor.dependency_graph.invalidate_graph_cache") as invalidate_graph,
        ):
            await work_refresh_service.probe_github_once(tmp_path)

        if expected_kwargs is None:
            invalidate.assert_not_called()
        else:
            invalidate.assert_called_once_with(tmp_path, **expected_kwargs)
        # A changed issue list also refetches the graph Up next ranks from.
        assert invalidate_graph.call_count == (1 if issues is ProbeOutcome.CHANGED else 0)
        listed.assert_not_called()

    async def test_a_project_not_tracked_on_github_is_not_probed(self, tmp_path: Path) -> None:
        check = AsyncMock()
        with (
            patch.object(work_refresh_service, "load_config", return_value=_probe_config("jira")),
            patch.object(ConditionalProbe, "check", check),
        ):
            assert await work_refresh_service.probe_github_once(tmp_path) is None
        check.assert_not_called()

    async def test_a_changed_repo_starts_over_with_no_etags(self, tmp_path: Path) -> None:
        first = work_refresh_service._probe_for(tmp_path, ("github", "owner/repo", "alice"))
        same = work_refresh_service._probe_for(tmp_path, ("github", "owner/repo", "alice"))
        moved = work_refresh_service._probe_for(tmp_path, ("github", "owner/other", "alice"))

        assert first is same
        assert moved is not first

    async def test_a_rate_limited_probe_backs_off(self, tmp_path: Path) -> None:
        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            sleeps.append(delay)
            if len(sleeps) == 2:
                raise asyncio.CancelledError

        outcomes = AsyncMock(side_effect=[ProbeOutcome.RATE_LIMITED, ProbeOutcome.UNCHANGED])
        with (
            patch.object(work_refresh_service, "probe_github_once", outcomes),
            patch("sova.dashboard.services.work_refresh_service.asyncio.sleep", side_effect=sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            await work_refresh_service.run_github_probe(tmp_path, interval=15)

        assert sleeps == [work_refresh_service.PROBE_RATE_LIMIT_BACKOFF_SECONDS, 15]


class TestProbeGate:
    @pytest.fixture
    def probes(self) -> Iterator[list[Path | None]]:
        from sova.dashboard.routers import agents

        started: list[Path | None] = []

        async def fake_probe(project_dir: Path | None, _slug: str | None = None) -> None:
            started.append(project_dir)
            await asyncio.Event().wait()

        with patch.object(agents, "_run_probe", side_effect=fake_probe):
            yield started

    async def test_no_visible_work_tab_runs_no_probe(self, tmp_path: Path, probes: list) -> None:
        from sova.dashboard.routers import agents

        manager = agents._ConnectionManager()
        sidebar_only = MagicMock()
        manager._groups[tmp_path] = [sidebar_only]  # a socket that never reports a visible Work page

        with patch.object(agents, "_ws_manager", manager):
            agents._handle_client_message(sidebar_only, tmp_path, "not json")
            agents._handle_client_message(sidebar_only, tmp_path, '{"type": "work_visible", "visible": false}')
            await asyncio.sleep(0)

        assert manager._probe_tasks == {}
        assert probes == []

    async def test_one_probe_per_project_until_the_last_visible_tab_leaves(self, tmp_path: Path, probes: list) -> None:
        from sova.dashboard.routers import agents

        manager = agents._ConnectionManager()
        tab_a, tab_b = MagicMock(), MagicMock()
        manager._groups[tmp_path] = [tab_a, tab_b]

        manager.set_work_visible(tab_a, tmp_path, True)
        manager.set_work_visible(tab_b, tmp_path, True)
        await asyncio.sleep(0)
        task = manager._probe_tasks[tmp_path]
        manager.set_work_visible(tab_a, tmp_path, False)  # hidden: tab_b still shows the page
        still_running = not task.done()
        manager._groups[tmp_path].remove(tab_b)
        manager.disconnect(tab_b, tmp_path)
        await asyncio.sleep(0)

        assert probes == [tmp_path]
        assert still_running
        assert task.cancelled()
        assert manager._probe_tasks == {}

    async def test_the_socket_route_reads_the_visibility_message(self, tmp_path: Path, probes: list) -> None:
        from sova.dashboard.routers import agents

        ws = MagicMock()
        with patch.object(agents._ws_manager, "set_work_visible") as visible:
            agents._handle_client_message(ws, tmp_path, '{"type": "work_visible", "visible": true}')

        visible.assert_called_once_with(ws, tmp_path, True)


# Races between a write and a build in flight ----------------------------------------------


class TestInvalidationDuringABuild:
    async def test_a_first_read_joined_to_an_obsolete_build_waits_for_the_next_one(self) -> None:
        release = asyncio.Event()
        calls = 0

        async def build() -> dict:
            nonlocal calls
            calls += 1
            if calls == 1:
                await release.wait()
            return {"n": calls}

        try:
            reader = asyncio.create_task(snapshot_service.get_snapshot("race", None, build))
            await asyncio.sleep(0)
            snapshot_service.invalidate_snapshot("race", None)  # a hide lands while the first build reads
            release.set()
            snapshot = await asyncio.wait_for(reader, timeout=5)
        finally:
            snapshot_service.reset_snapshots()

        assert snapshot.payload == {"n": 2}, "the obsolete build's result is discarded, not served or raised"

    async def test_a_first_read_gives_up_when_every_build_is_invalidated(self) -> None:
        async def build() -> dict:
            snapshot_service.invalidate_snapshot("race", None)
            return {}

        try:
            with pytest.raises(RuntimeError, match="invalidated"):
                await snapshot_service.get_snapshot("race", None, build)
        finally:
            snapshot_service.reset_snapshots()

    async def test_a_board_refresh_reruns_a_first_build_still_in_flight(self, tmp_path: Path) -> None:
        release = asyncio.Event()
        graphs = iter(["old", "new"])

        async def build(_project: Path, milestone: str = "", *, owner: str = "all") -> dict:
            graph = next(graphs)
            if graph == "old":
                await release.wait()
            return {"graph": graph}

        with patch.object(work_board_service, "build_board", side_effect=build):
            first = asyncio.create_task(work_board_service.get_board_snapshot(tmp_path))
            await asyncio.sleep(0)
            work_board_service.refresh_board_snapshots(tmp_path)  # the graph changed under the first build
            release.set()
            await first
            await _settle_builds()
            snapshot = await work_board_service.get_board_snapshot(tmp_path)

        assert snapshot.payload == {"graph": "new"}

    async def test_the_work_items_refresh_reruns_a_first_build_still_in_flight(self, tmp_path: Path) -> None:
        with (
            patch.object(work_board_service, "is_building", return_value=True),
            patch.object(work_board_service, "request_refresh") as refresh,
        ):
            work_board_service.refresh_work_snapshots(tmp_path)

        assert refresh.call_args_list[0].args[0] == "work_items"

    async def test_the_writer_waits_for_the_rerun_behind_a_build_in_flight(self) -> None:
        """A write during a build (which may predate it) is shown only by the rerun: the writer waits for that."""
        release = asyncio.Event()
        calls = 0

        async def build() -> dict:
            nonlocal calls
            calls += 1
            if calls == 2:
                await release.wait()
            return {"n": calls}

        await snapshot_service.get_snapshot("kind", None, build)
        snapshot_service.request_refresh("kind", None, build)  # a tick's rebuild, in flight
        await asyncio.sleep(0)
        snapshot_service.request_refresh("kind", None, build)  # the write lands during it
        writer = asyncio.create_task(snapshot_service.wait_for_refresh("kind", None))
        await asyncio.sleep(0)
        waiting = not writer.done()
        release.set()
        await asyncio.wait_for(writer, timeout=5)

        assert waiting
        assert snapshot_service.peek_snapshot("kind", None).payload == {"n": 3}

    async def test_waiting_with_nothing_in_flight_returns_at_once(self) -> None:
        await asyncio.wait_for(snapshot_service.wait_for_refresh("kind", None), timeout=1)

    async def test_a_merged_fetch_overtaken_by_a_merge_is_not_cached(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from datetime import datetime, timezone

        cfg = SimpleNamespace(github_repo="owner/repo", github_user="alice", task_source=None)
        monkeypatch.setattr(work_board_service, "load_config", lambda _p: cfg)

        async def fetch(**_kwargs: object) -> list[dict]:
            work_board_service.invalidate_merged_cache(tmp_path)  # finalize saw a merge while this fetch ran
            return []

        listed = AsyncMock(side_effect=fetch)
        monkeypatch.setattr(work_board_service, "list_merged_prs_since", listed)
        now = datetime.now(timezone.utc)

        assert await work_board_service._done_today(tmp_path, now) == []
        assert work_board_service._merged_cache == {}

        listed.side_effect = None
        listed.return_value = []
        await work_board_service._done_today(tmp_path, now)
        assert len(work_board_service._merged_cache) == 1, "an undisturbed fetch still caches"


class TestHandoffChange:
    @pytest.mark.parametrize(
        ("previous", "message", "refreshed"),
        [
            pytest.param(None, {"type": "handoff", "data": 1}, False, id="first send only records"),
            pytest.param({"type": "handoff", "data": 1}, {"type": "handoff", "data": 1}, False, id="unchanged"),
            pytest.param({"type": "handoff", "data": 1}, {"type": "handoff", "data": 2}, True, id="changed"),
            pytest.param(
                {"type": "handoff", "data": {"handoffs": [{"issue": 1, "age_seconds": 3}]}},
                {"type": "handoff", "data": {"handoffs": [{"issue": 1, "age_seconds": 9}]}},
                False,
                id="only a clock-driven field moved",
            ),
            pytest.param(
                {"type": "handoff", "data": {"handoffs": [{"issue": 1, "age_seconds": 3}]}},
                {"type": "handoff", "data": {"handoffs": [{"issue": 2, "age_seconds": 3}]}},
                True,
                id="the content changed",
            ),
        ],
    )
    async def test_a_changed_handoff_rebuilds_the_work_snapshots(
        self, tmp_path: Path, previous: dict | None, message: dict, refreshed: bool
    ) -> None:
        import json

        from sova.dashboard.routers import agents

        manager = agents._ConnectionManager()
        last_sent = (
            {}
            if previous is None
            else {
                "handoff": json.dumps(previous, sort_keys=True),
                agents._HANDOFF_DIGEST_KEY: agents._handoff_digest(previous["data"]),
            }
        )
        with (
            patch.object(manager, "_broadcast", AsyncMock()),
            patch.object(work_board_service, "refresh_work_snapshots") as refresh,
        ):
            await manager._push_changed(tmp_path, "handoff", AsyncMock(return_value=message), last_sent)

        assert refresh.called is refreshed

    async def test_the_same_handoff_after_an_unavailable_one_rebuilds_nothing(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import agents

        manager = agents._ConnectionManager()
        last_sent: dict[str, str] = {}
        messages = iter(
            [{"type": "handoff", "data": 1}, RuntimeError("synthesis down"), {"type": "handoff", "data": 1}]
        )

        async def build() -> dict:
            item = next(messages)
            if isinstance(item, Exception):
                raise item
            return item

        with (
            patch.object(manager, "_broadcast", AsyncMock()) as broadcast,
            patch.object(work_board_service, "refresh_work_snapshots") as refresh,
        ):
            for _ in range(3):
                await manager._push_changed(tmp_path, "handoff", build, last_sent)

        assert broadcast.await_count == 3, "the unavailable message and the recovery both go out"
        refresh.assert_not_called()

    def test_a_failed_snapshot_refresh_does_not_stop_the_handoff(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import agents

        with patch.object(work_board_service, "refresh_work_snapshots", side_effect=RuntimeError("boom")):
            agents._refresh_snapshots_on_handoff_change(tmp_path)  # logged, not raised


class TestAnnounceInvalidatesAgain:
    async def test_the_announcement_drops_the_caches_after_the_rollback(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import agents
        from sova.dashboard.services.agent_pool import AgentState

        agent = AgentState(run_id=5, issue="77", role="developer", process=None, project_dir=tmp_path)
        agent.pr_number = 42
        order: list[str] = []
        with (
            patch.object(
                agent_finalize,
                "invalidate_work_item_caches",
                side_effect=lambda _p, *prs, **kwargs: order.append(f"invalidate:{list(prs)}:{kwargs}"),
            ),
            patch.object(
                agents._ws_manager,
                "broadcast_event",
                AsyncMock(side_effect=lambda event, *_a, **_k: order.append(event)),
            ),
        ):
            await agent_finalize._announce_agent_finished(agent, status_changed=False, status="failed")

        # The PR list was dropped before the status write: the second pass keeps it (one gh pr list per exit).
        assert order == ["invalidate:[42]:{'prs': False}", "agent_finished"]


class TestEtagParsing:
    @pytest.mark.parametrize(
        ("etag_line", "stored"),
        [
            pytest.param('Etag:   W/"a b"  ', 'W/"a b"', id="surrounding whitespace stripped"),
            pytest.param("Etag:   ", None, id="empty value not stored"),
        ],
    )
    async def test_the_etag_value_is_stripped(self, etag_line: str, stored: str | None) -> None:
        head = f"HTTP/2.0 200 OK\r\n{etag_line}\r\n\r\n[]"
        probe = ConditionalProbe()
        with (
            patch("sova.git.conditional.run", _gh(ShellResult(0, head, ""))),
            patch("sova.git.conditional.resolve_gh_env", AsyncMock(return_value=None)),
        ):
            assert await probe.check("repos/o/r/pulls") is ProbeOutcome.CHANGED
            probe.commit("repos/o/r/pulls")

        assert probe._etags.get("repos/o/r/pulls") == stored


# Review follow-ups: the probe commits only what it acted on, PR-only issue pages, the board's graph -------------

_PULLS = "repos/owner/repo/pulls?state=all&sort=updated&direction=desc&per_page=20"


def _issues_body(*items: tuple[int, str, bool]) -> str:
    import json

    return json.dumps(
        [{"number": n, "updated_at": at, **({"pull_request": {}} if is_pr else {})} for n, at, is_pr in items]
    )


def _ok(body: str = "[]", etag: str = '"e1"') -> ShellResult:
    return ShellResult(0, f"HTTP/2.0 200 OK\r\nEtag: {etag}\r\n\r\n{body}", "")


_NOT_MODIFIED = ShellResult(1, "HTTP/2.0 304 Not Modified\r\n\r\n", "gh: HTTP 304")


class TestProbeCommitsWhatItActedOn:
    async def test_a_probe_cancelled_between_its_reads_sees_the_pr_change_again(self, tmp_path: Path) -> None:
        """Hiding the tab cancels the probe; the PR list's new ETag must not outlive the invalidation it skipped."""
        hang = asyncio.Event()
        sent: list[tuple[str, ...]] = []

        async def run(*cmd: str, **_kwargs: object) -> ShellResult:
            sent.append(cmd)
            if len(sent) == 2:
                await hang.wait()  # the issue read, when the tab hides
            if "pulls" in cmd[-1]:
                return _NOT_MODIFIED if any("If-None-Match" in part for part in cmd) else _ok()
            return _NOT_MODIFIED

        with (
            patch.object(work_refresh_service, "load_config", return_value=_probe_config()),
            patch("sova.git.conditional.run", side_effect=run),
            patch("sova.git.conditional.resolve_gh_env", AsyncMock(return_value=None)),
            patch.object(agent_finalize, "invalidate_work_item_caches") as invalidate,
        ):
            first = asyncio.create_task(work_refresh_service.probe_github_once(tmp_path))
            for _ in range(10):
                await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            invalidated_by_the_cancelled = invalidate.call_count
            await work_refresh_service.probe_github_once(tmp_path)
            await work_refresh_service.probe_github_once(tmp_path)

        assert invalidated_by_the_cancelled == 0
        assert not any("If-None-Match" in part for part in sent[2]), "the next probe asks without the new ETag"
        invalidate.assert_called_once_with(tmp_path, every_verdict=True)
        assert any("If-None-Match" in part for part in sent[4]), "committed once the caches were invalidated"

    @pytest.mark.parametrize(("rebuilding", "told"), [(False, 1), (True, 0)])
    async def test_a_change_no_rebuild_reports_reaches_the_sockets(
        self, tmp_path: Path, rebuilding: bool, told: int
    ) -> None:
        """A lapsed board key requests no rebuild, so nothing would send work_items_changed but the probe itself."""
        check = AsyncMock(side_effect=[ProbeOutcome.CHANGED, ProbeOutcome.UNCHANGED])
        on_unreported = AsyncMock()
        with (
            patch.object(work_refresh_service, "load_config", return_value=_probe_config()),
            patch.object(ConditionalProbe, "check", check),
            patch.object(agent_finalize, "invalidate_work_item_caches", return_value=rebuilding),
        ):
            await work_refresh_service.probe_github_once(tmp_path, on_unreported=on_unreported)

        assert on_unreported.await_count == told
        if told:
            on_unreported.assert_awaited_once_with(tmp_path)

    async def test_a_page_moved_by_prs_alone_skips_the_issue_side_refetch(self, tmp_path: Path) -> None:
        first_page = _issues_body((5, "2026-11-24T10:00:00Z", False), (4, "2026-11-24T09:00:00Z", False))
        # A bot commented on PR #9: it tops the issues endpoint too, which also lists pull requests.
        pr_only = _issues_body(
            (9, "2026-11-24T10:05:00Z", True), (5, "2026-11-24T10:00:00Z", False), (4, "2026-11-24T09:00:00Z", False)
        )
        an_issue = _issues_body(
            (6, "2026-11-24T10:06:00Z", False), (9, "2026-11-24T10:05:00Z", True), (5, "2026-11-24T10:00:00Z", False)
        )
        responses = iter(
            [_ok(), _ok(first_page), _ok(etag='"p2"'), _ok(pr_only, '"i2"'), _NOT_MODIFIED, _ok(an_issue, '"i3"')]
        )
        with (
            patch.object(work_refresh_service, "load_config", return_value=_probe_config()),
            patch("sova.git.conditional.run", AsyncMock(side_effect=lambda *_a, **_k: next(responses))),
            patch("sova.git.conditional.resolve_gh_env", AsyncMock(return_value=None)),
            patch.object(agent_finalize, "invalidate_work_item_caches") as invalidate,
            patch("sova.supervisor.dependency_graph.invalidate_graph_cache") as invalidate_graph,
            patch.object(work_item_service, "request_graph_rebuild") as rebuild_graph,
        ):
            await work_refresh_service.probe_github_once(tmp_path)  # the first read: everything counts as changed
            graph_after_first = invalidate_graph.call_count
            await work_refresh_service.probe_github_once(tmp_path)  # the PR moved both lists
            graph_after_pr = invalidate_graph.call_count
            await work_refresh_service.probe_github_once(tmp_path)  # an issue changed, behind no new PR

        assert (graph_after_first, graph_after_pr, invalidate_graph.call_count) == (1, 1, 2)
        assert rebuild_graph.call_count == 2
        assert [c.kwargs for c in invalidate.call_args_list] == [{"every_verdict": True}] * 2 + [{"prs": False}]

    @pytest.mark.parametrize(
        ("page", "expected"),
        [
            pytest.param(None, True, id="unreadable counts as changed"),
            pytest.param([(1, "b", True)], False, id="only a new PR"),
            pytest.param([(2, "b", False)], True, id="a new issue"),
            pytest.param([(1, "a", False)], False, id="the same page"),
            pytest.param([(3, "a", False), (1, "a", False)], True, id="an issue at the mark's own second"),
            pytest.param(
                [(n, "b", True) for n in range(100, 100 + work_refresh_service._ISSUES_PAGE_SIZE)],
                True,
                id="a page of nothing but new PRs may hide an issue past its end",
            ),
        ],
    )
    def test_what_counts_as_an_issue_change(self, page: list | None, expected: bool) -> None:
        state = work_refresh_service._ProjectProbe(identity=("github", "o/r", "u"))
        state.remember([(1, "a", False)])

        assert state.issues_changed(page) is expected


class TestIssueChangeRefreshesTheBoardGraph:
    async def test_a_changed_issue_list_rebuilds_the_graph_up_next_ranks_from(self, tmp_path: Path) -> None:
        from sova.adapters.base import Task
        from sova.supervisor.dependency_graph import DependencyGraph

        old, new = DependencyGraph([Task(id="1", title="old")]), DependencyGraph([Task(id="1", title="new")])
        loads = AsyncMock(side_effect=[old, new])
        listener = AsyncMock()
        work_item_service.set_graph_change_listener(listener)
        try:
            with (
                patch.object(work_item_service, "load_dependency_graph", loads),
                patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=tmp_path),
                patch.object(work_refresh_service, "load_config", return_value=_probe_config()),
                patch.object(
                    ConditionalProbe, "check", AsyncMock(side_effect=[ProbeOutcome.UNCHANGED, ProbeOutcome.CHANGED])
                ),
                patch.object(agent_finalize, "invalidate_work_item_caches"),
            ):
                await work_item_service.load_ranking_graph(tmp_path)  # what the board ranked from so far
                await work_refresh_service.probe_github_once(tmp_path)
                await asyncio.gather(*work_item_service._graph_builds.values())
                await asyncio.gather(*work_item_service._graph_notify_tasks)
                ranked = await work_item_service.load_ranking_graph(tmp_path)
        finally:
            from sova.dashboard.routers import agents

            work_item_service.set_graph_change_listener(agents._on_graph_changed)

        assert ranked.get_task(1).title == "new", "Up next no longer ranks from the graph before the change"
        listener.assert_awaited_once()  # which rebuilds the board snapshots (_on_graph_changed)

    async def test_a_graph_build_in_flight_is_retired_and_built_again(self, tmp_path: Path) -> None:
        from sova.adapters.base import Task
        from sova.supervisor.dependency_graph import DependencyGraph

        release = asyncio.Event()
        titles = iter(["old", "stale", "new"])

        async def load(_project: Path | None) -> DependencyGraph:
            title = next(titles)
            if title == "stale":
                await release.wait()  # read the issues before the change
            return DependencyGraph([Task(id="1", title=title)])

        with (
            patch.object(work_item_service, "load_dependency_graph", side_effect=load),
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=tmp_path),
        ):
            await work_item_service.load_ranking_graph(tmp_path)
            await work_item_service.load_ranking_graph(tmp_path)  # starts the stale rebuild behind the old graph
            await asyncio.sleep(0)
            work_item_service.request_graph_rebuild(tmp_path)
            release.set()
            await asyncio.gather(*work_item_service._graph_builds.values(), *work_item_service._retired_graph_builds)
            ranked = await work_item_service.load_ranking_graph(tmp_path)

        assert ranked.get_task(1).title == "new"

    def test_a_project_with_no_graph_yet_is_left_to_its_first_ranking(self, tmp_path: Path) -> None:
        with patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=tmp_path):
            work_item_service.request_graph_rebuild(tmp_path)  # no running loop needed: nothing starts

        assert work_item_service._graph_builds == {}


class TestProbeTaskNeverDiesSilently:
    async def test_a_failure_outside_the_probe_loop_is_logged(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import agents

        with (
            patch.object(agents, "set_project_context"),
            patch.object(agents, "_resolve_group_project", side_effect=RuntimeError("no project")),
            patch.object(agents, "log") as log,
        ):
            await agents._run_probe(tmp_path)  # returns: nothing left on the task for nobody to retrieve

        assert log.error.call_args.args[0] == "agents.github_probe_crashed"
        assert log.error.call_args.kwargs["exc_info"] is True

    async def test_the_probe_tells_the_sockets_of_a_change_no_rebuild_reports(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import agents

        run_probe = AsyncMock()
        with (
            patch.object(agents, "set_project_context"),
            patch.object(agents, "_resolve_group_project", return_value=tmp_path),
            patch.object(agents, "run_github_probe", run_probe),
        ):
            await agents._run_probe(tmp_path)

        assert run_probe.await_args.kwargs["on_unreported"] is agents._broadcast_work_items_changed


class TestNoSlugSocketsHearTheBoard:
    async def test_a_socket_in_the_none_group_gets_a_changed_board(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import agents

        manager = agents._ConnectionManager()
        ws = MagicMock()
        ws.send_json = AsyncMock()
        manager._groups[None] = [ws]  # connected without a slug: shows the default project
        with (
            patch.object(agents, "_ws_manager", manager),
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=tmp_path),
        ):
            await agents._on_snapshot_changed("board:all:", tmp_path.resolve())

        ws.send_json.assert_awaited_once_with({"type": "work_items_changed"})

    async def test_another_projects_board_does_not_reach_the_none_group(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import agents

        manager = agents._ConnectionManager()
        ws = MagicMock()
        ws.send_json = AsyncMock()
        manager._groups[None] = [ws]
        with (
            patch.object(agents, "_ws_manager", manager),
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=tmp_path),
        ):
            await agents._on_snapshot_changed("board:all:", (tmp_path / "other").resolve())

        ws.send_json.assert_not_awaited()


class TestOneReloadPerGraphChange:
    async def test_a_graph_change_with_snapshots_to_rebuild_leaves_the_notice_to_the_rebuild(
        self, tmp_path: Path
    ) -> None:
        from sova.dashboard.routers import agents

        broadcast = AsyncMock()
        with (
            patch.object(work_board_service, "refresh_work_snapshots", return_value=True),
            patch.object(agents._ws_manager, "broadcast_event", broadcast),
        ):
            await agents._on_graph_changed(tmp_path)

        broadcast.assert_not_awaited()

    async def test_with_nothing_to_rebuild_the_sockets_are_told_at_once(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import agents

        broadcast = AsyncMock()
        with (
            patch.object(work_board_service, "refresh_work_snapshots", return_value=False),
            patch.object(agents._ws_manager, "broadcast_event", broadcast),
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=tmp_path / "x"),
        ):
            await agents._on_graph_changed(tmp_path)

        broadcast.assert_awaited_once_with("work_items_changed", tmp_path)


class TestOneInvalidationPerExit:
    def test_the_producer_skips_an_exit_the_dashboard_finalize_covered(self, tmp_path: Path) -> None:
        agent_finalize._remember_finalized(1)
        with (
            patch.object(agent_finalize, "invalidate_work_item_caches") as invalidate,
            patch.object(work_board_service, "refresh_work_snapshots") as refresh,
        ):
            work_refresh_service.apply_run_changes(tmp_path, {1: _mark(10)}, {})

        invalidate.assert_not_called()
        refresh.assert_called_once_with(tmp_path)  # the Running column still changed

    def test_a_cli_run_leaving_is_still_invalidated(self, tmp_path: Path) -> None:
        agent_finalize._remember_finalized(2)
        with patch.object(agent_finalize, "invalidate_work_item_caches") as invalidate:
            work_refresh_service.apply_run_changes(tmp_path, {1: _mark(10)}, {})

        invalidate.assert_called_once_with(tmp_path, 10)

    def test_a_finalized_run_is_forgotten_after_a_minute(self) -> None:
        agent_finalize._remember_finalized(1)
        agent_finalize._finalized_runs[1] -= agent_finalize._FINALIZED_RUN_MEMORY_SECONDS + 1

        assert agent_finalize.finalized_run_ids() == frozenset()


class TestColdBoardBuild:
    async def test_one_cold_board_build_lists_open_prs_once(
        self, monkeypatch: pytest.MonkeyPatch, pr_sources: SimpleNamespace
    ) -> None:
        """The criterion's "exactly one gh pr list" is the open-PR list every board source reads (#1534).

        Done today's merged-PR list is a second, different listing (``--state merged``, its own 60 s cache): the
        open list cannot answer it, so it is counted on its own here rather than folded into the criterion.
        """
        from sova.supervisor.dependency_graph import DependencyGraph

        listings: list[str] = []

        async def run(*cmd: str, **_kwargs: object) -> ShellResult:
            if cmd[:3] == ("gh", "pr", "list"):
                await asyncio.sleep(0.01)  # every source reaches the cold cache before the first answer lands
                listings.append(cmd[cmd.index("--state") + 1])
            return ShellResult(0, "[]", "")

        from sova.config.models import ProjectConfig

        project = pr_sources.project
        cfg = ProjectConfig(github_repo="owner/repo", github_user="alice")
        for module in ("agent_pool", "pr_service", "queue_service", "work_board_service", "work_item_service"):
            monkeypatch.setattr(f"sova.dashboard.services.{module}.load_config", lambda *_a, **_k: cfg)
        monkeypatch.setattr("sova.dashboard.services.work_verdict.load_config", lambda *_a, **_k: cfg)
        monkeypatch.setattr("sova.git.pr.run", run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))
        monkeypatch.setattr(work_board_service, "load_ranking_graph", AsyncMock(return_value=DependencyGraph([])))
        monkeypatch.setattr(work_board_service, "load_task_queue", AsyncMock(return_value=[]))
        monkeypatch.setattr(work_board_service, "load_hidden_issues", AsyncMock(return_value=[]))
        monkeypatch.setattr("sova.dashboard.services.agent_pool.get_default_project_dir", lambda: project)

        board = await work_board_service.build_board(project)

        assert "up_next" in board
        assert listings.count("open") == 1, listings
        assert listings.count("merged") == 1, listings
        assert len(listings) == 2, listings


# Third review round: bounded writer wait, lapsed keys, retried transitions, PR pages ----------------------------


class TestWriterWaitIsBounded:
    async def test_the_hide_route_answers_within_the_bound_while_the_board_build_hangs(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        hang = asyncio.Event()
        builds = 0

        async def build(_project: Path, milestone: str = "", *, owner: str = "all") -> dict:
            nonlocal builds
            builds += 1
            if builds > 1:
                await hang.wait()  # GitHub never answers the rebuild the write asked for
            return {"up_next": [], "stages": []}

        with (
            patch("sova.dashboard.routers.work.get_project_dir", return_value=tmp_path),
            patch.object(work_board_service, "build_board", side_effect=build),
            patch.object(work_board_service, "WRITER_WAIT_SECONDS", 0.2),
            patch("sova.dashboard.routers.work.set_issue_hidden", AsyncMock(return_value=[5])),
        ):
            assert (await client.get("/api/work/board")).status_code == 200
            started = time.monotonic()
            response = await client.post("/api/work/hidden/5")
            elapsed = time.monotonic() - started
            still_building = snapshot_service.is_building(work_board_service._board_kind("", "all"), tmp_path)
            await snapshot_service.cancel_snapshot_builds()

        assert response.status_code == 200
        assert response.json() == {"hidden": [5]}
        assert elapsed < 2.0
        assert still_building, "giving up the wait leaves the rebuild running"

    async def test_a_failed_board_refresh_does_not_fail_a_saved_hide(self, client: AsyncClient, tmp_path: Path) -> None:
        with (
            patch("sova.dashboard.routers.work.get_project_dir", return_value=tmp_path),
            patch("sova.dashboard.routers.work.set_issue_hidden", AsyncMock(return_value=[5])),
            patch.object(work_board_service, "refresh_board_snapshots", side_effect=RuntimeError("no project")),
        ):
            response = await client.post("/api/work/hidden/5")

        assert response.status_code == 200

    async def test_a_failed_board_refresh_does_not_fail_a_saved_queue(self, tmp_path: Path) -> None:
        from sova.dashboard.services import supervisor_service

        with (
            patch("sova.config.db_loader.save_task_queue", AsyncMock(return_value=[1, 2])),
            patch.object(work_board_service, "refresh_board_snapshots", side_effect=RuntimeError("no loop")),
        ):
            assert await supervisor_service.persist_queue(tmp_path, [1, 2], based_on=None) == [1, 2]


class TestUnsetProjectSharesTheBoardKey:
    async def test_a_refresh_for_no_project_reaches_the_board_get_board_built(self, tmp_path: Path) -> None:
        builds = 0

        async def build(_project: Path, milestone: str = "", *, owner: str = "all") -> dict:
            nonlocal builds
            builds += 1
            return {"up_next": [], "n": builds}

        with (
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=tmp_path),
            patch.object(work_board_service, "build_board", side_effect=build),
        ):
            await work_board_service.get_board_snapshot(tmp_path)
            assert work_board_service.refresh_board_snapshots(None) is True
            await _settle_builds()

        assert builds == 2


class TestVisibleBoardsDoNotLapse:
    async def _board(self, tmp_path: Path) -> None:
        async def build(_project: Path, milestone: str = "", *, owner: str = "all") -> dict:
            return {"up_next": []}

        with patch.object(work_board_service, "build_board", side_effect=build):
            await work_board_service.get_board_snapshot(tmp_path)
        for key in work_board_service._board_requests:
            work_board_service._board_requests[key] -= work_board_service._BOARD_RECENT_SECONDS + 1

    async def test_a_step_change_on_a_lapsed_board_still_tells_the_sockets(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import agents

        await self._board(tmp_path)
        broadcast = AsyncMock()
        with (
            patch.object(agents, "_resolve_group_project", return_value=tmp_path.resolve()),
            patch.object(agents._ws_manager, "broadcast_event", broadcast),
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=tmp_path),
        ):
            kept = await agents._invalidate_on_run_transitions(
                tmp_path, {1: _mark(10)}, {1: _mark(10, step="validate")}
            )

        assert kept == {1: _mark(10, step="validate")}
        assert ("work_items_changed", tmp_path.resolve()) in [c.args for c in broadcast.await_args_list]

    async def test_a_visible_work_tab_keeps_its_board_key_recent(self, tmp_path: Path) -> None:
        await self._board(tmp_path)

        work_board_service.keep_board_keys_alive(tmp_path)
        with patch.object(work_board_service, "build_board", AsyncMock(return_value={"up_next": [1]})):
            assert work_board_service.refresh_board_snapshots(tmp_path) is True
            await _settle_builds()

    async def test_the_producer_keeps_boards_alive_only_while_a_work_tab_is_visible(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import agents

        manager = agents._ConnectionManager()
        ws = MagicMock()
        ws.send_json = AsyncMock()
        manager._groups[tmp_path] = [ws]
        manager._work_viewers[tmp_path] = {ws}
        ticks = 0

        async def statuses(**_kwargs: object) -> list:
            nonlocal ticks
            ticks += 1
            if ticks > 1:
                manager._groups[tmp_path] = []  # the socket left: the producer ends
            return []

        with (
            patch("sova.dashboard.services.agent_status.get_all_agent_statuses", side_effect=statuses),
            patch.object(agents, "_STATUS_BROADCAST_INTERVAL", 0),
            patch.object(agents, "_keep_visible_boards_alive") as keep,
            patch.object(agents, "_refresh_work_items_snapshot", AsyncMock()),
            patch.object(agents, "_build_activity_message", AsyncMock(return_value={"type": "activity"})),
            patch.object(agents, "_build_handoff_message", AsyncMock(return_value={"type": "handoff", "data": 1})),
            patch.object(agents, "_invalidate_on_run_transitions", AsyncMock(return_value={})),
        ):
            await manager._produce_loop(tmp_path)

        assert keep.call_count >= 1


class TestFailedTransitionIsRetried:
    async def test_a_failed_invalidation_keeps_the_previous_marks(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import agents

        previous = {1: _mark(10)}
        with (
            patch.object(agents, "_resolve_group_project", return_value=tmp_path),
            patch.object(agents, "apply_run_changes", side_effect=RuntimeError("db locked")),
        ):
            kept = await agents._invalidate_on_run_transitions(tmp_path, previous, {})

        assert kept is previous, "the next tick sees the run leave again and retries"

    async def test_the_work_items_refresh_runs_when_the_board_refresh_fails(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import agents

        refresh = AsyncMock(return_value=False)
        with (
            patch.object(agents, "refresh_board_snapshots", side_effect=RuntimeError("boom")),
            patch.object(agents, "refresh_snapshot", refresh),
        ):
            await agents._refresh_work_items_snapshot(tmp_path)

        refresh.assert_awaited_once()


class TestStepChangeReachesTheBoard:
    async def _tick(self, tmp_path: Path, current: dict, payloads: list[dict]) -> AsyncMock:
        """Build a board, then apply one producer tick; returns the broadcast mock."""
        from sova.dashboard.routers import agents

        answers = iter(payloads)

        async def build(_project: Path, milestone: str = "", *, owner: str = "all") -> dict:
            return next(answers)

        broadcast = AsyncMock()
        with (
            patch.object(work_board_service, "build_board", side_effect=build),
            patch.object(agents, "_resolve_group_project", return_value=tmp_path.resolve()),
            patch.object(agents._ws_manager, "broadcast_event", broadcast),
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=tmp_path / "x"),
        ):
            await work_board_service.get_board_snapshot(tmp_path)
            await agents._invalidate_on_run_transitions(tmp_path, {1: _mark(10)}, current)
            await _settle_builds()
        return broadcast

    async def test_a_run_moving_step_sends_one_work_items_changed(self, tmp_path: Path) -> None:
        broadcast = await self._tick(
            tmp_path,
            {1: _mark(10, step="validate")},
            [{"running": [{"run_id": 1, "step": "develop"}]}, {"running": [{"run_id": 1, "step": "validate"}]}],
        )

        assert [c.args for c in broadcast.await_args_list] == [("work_items_changed", tmp_path.resolve())]

    async def test_a_tick_with_no_movement_sends_nothing(self, tmp_path: Path) -> None:
        broadcast = await self._tick(tmp_path, {1: _mark(10)}, [{"running": [{"run_id": 1, "step": "develop"}]}])

        broadcast.assert_not_awaited()


class TestBoardKeyCap:
    async def test_the_least_recently_requested_key_is_dropped_past_the_cap(self, tmp_path: Path) -> None:
        with (
            patch.object(work_board_service, "build_board", AsyncMock(return_value={"up_next": []})),
            patch.object(work_board_service, "_BOARD_MAX_KEYS_PER_PROJECT", 2),
        ):
            for milestone in ("a", "b", "c"):
                await work_board_service.get_board_snapshot(tmp_path, milestone)

        milestones = sorted(m for _p, m, _o in work_board_service._board_requests)
        assert milestones == ["b", "c"]
        assert snapshot_service.peek_snapshot(work_board_service._board_kind("a", "all"), tmp_path) is None


class TestServedElapsedAgrees:
    def test_elapsed_formatted_follows_the_advanced_seconds(self) -> None:
        snapshot = snapshot_service.Snapshot(
            payload={"running": [{"elapsed_seconds": 10, "elapsed_formatted": "10s"}]},
            digest="d",
            built_at=time.monotonic() - 3600,
        )

        entry = work_board_service.board_response(snapshot)["running"][0]

        assert entry["elapsed_seconds"] >= 3610
        assert entry["elapsed_formatted"] != "10s"
        assert entry["elapsed_formatted"].startswith("1h")


class TestPrProbeDropsOnlyMovedVerdicts:
    async def test_a_changed_pr_page_drops_the_verdicts_of_the_prs_that_moved(self, tmp_path: Path) -> None:
        first = _issues_body((7, "2026-11-24T10:00:00Z", True), (6, "2026-11-24T09:00:00Z", True))
        moved = _issues_body(
            (6, "2026-11-24T10:05:00Z", True), (7, "2026-11-24T10:00:00Z", True)
        )  # a push to PR #6 moved it to the top
        responses = iter([_ok(first, '"p1"'), _NOT_MODIFIED, _ok(moved, '"p2"'), _NOT_MODIFIED])
        with (
            patch.object(work_refresh_service, "load_config", return_value=_probe_config()),
            patch("sova.git.conditional.run", AsyncMock(side_effect=lambda *_a, **_k: next(responses))),
            patch("sova.git.conditional.resolve_gh_env", AsyncMock(return_value=None)),
            patch.object(agent_finalize, "invalidate_work_item_caches") as invalidate,
        ):
            await work_refresh_service.probe_github_once(tmp_path)
            await work_refresh_service.probe_github_once(tmp_path)

        assert [(c.args, c.kwargs) for c in invalidate.call_args_list] == [
            ((tmp_path,), {"every_verdict": True}),  # the first page: which PRs moved cannot be told
            ((tmp_path, 6), {}),
        ]

    @pytest.mark.parametrize(
        ("page", "expected"),
        [
            pytest.param(None, None, id="unreadable drops every verdict"),
            pytest.param([(1, "a", True)], [], id="the same page"),
            pytest.param([(2, "b", True), (1, "a", True)], [2], id="a new PR"),
            pytest.param(
                [(n, "b", True) for n in range(100, 100 + work_refresh_service._PULLS_PAGE_SIZE)],
                None,
                id="a full page of new PRs may hide more past its end",
            ),
        ],
    )
    def test_which_prs_moved(self, page: list | None, expected: list[int] | None) -> None:
        state = work_refresh_service._ProjectProbe(identity=("github", "o/r", "u"))
        state.pulls.remember([(1, "a", True)])

        assert state.changed_prs(page) == expected


class TestPrFailurePauseLiftsOnInvalidation:
    def test_an_invalidation_lifts_the_failure_pause(self) -> None:
        pr_service._pr_fetch_failed_until[("owner/repo", "alice")] = time.monotonic() + 30

        pr_service.invalidate_pr_cache("owner/repo")

        assert ("owner/repo", "alice") not in pr_service._pr_fetch_failed_until


class TestSeveralResponseHeads:
    def test_the_status_and_etag_come_from_the_last_head(self) -> None:
        from sova.git.conditional import _split_response

        out = (
            "HTTP/1.1 100 Continue\r\n\r\nHTTP/2.0 301 Moved\r\nLocation: x\r\n\r\n"
            'HTTP/2.0 200 OK\r\nEtag: "z"\r\n\r\n[]'
        )

        head, body = _split_response(out)

        assert head.startswith("HTTP/2.0 200")
        assert 'Etag: "z"' in head
        assert body == "[]"
