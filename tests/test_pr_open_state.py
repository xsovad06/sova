"""Tests for the three-valued PR open check and every site that carries a PR number forward (#1281)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import TaskState
from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.dashboard.services.work_item_service import _build_task_item, _safe_prs
from sova.git import pr as pr_module
from sova.git.pr import (
    OPEN_PR_LIST_LIMIT,
    PRLookupError,
    PROpenState,
    PRStatus,
    get_pr_open_state,
    list_open_prs,
    pr_open_state_from_list,
)
from sova.utils.shell import ShellResult


def _status(state: str) -> PRStatus:
    return PRStatus(number=7, state=state, mergeable="", review_decision="", url="", title="")


@pytest.fixture(autouse=True)
def _clear_open_cache() -> None:
    from sova.dashboard.services import pr_service, work_item_service

    pr_module._pr_open_cache.clear()
    pr_service._pr_fetch_failed_until.clear()
    work_item_service._confirm_backoff_until.clear()


class TestPrOpenStateFromList:
    def test_listed_pr_is_open(self) -> None:
        assert pr_open_state_from_list(7, {7, 8}) is PROpenState.OPEN

    def test_absent_pr_is_not_open(self) -> None:
        assert pr_open_state_from_list(9, {7, 8}) is PROpenState.NOT_OPEN

    def test_empty_list_from_successful_fetch_means_not_open(self) -> None:
        assert pr_open_state_from_list(7, set()) is PROpenState.NOT_OPEN

    def test_failed_fetch_is_unknown(self) -> None:
        assert pr_open_state_from_list(7, None) is PROpenState.UNKNOWN

    def test_length_alone_does_not_make_a_list_unknown(self) -> None:
        """Truncation is the fetcher's call (see TestOpenPrNumbers), not the classifier's."""
        big = set(range(1000, 1000 + OPEN_PR_LIST_LIMIT))
        assert pr_open_state_from_list(7, big) is PROpenState.NOT_OPEN


class TestGetPrOpenState:
    @pytest.mark.parametrize(
        ("gh_state", "expected"),
        [
            ("OPEN", PROpenState.OPEN),
            ("MERGED", PROpenState.NOT_OPEN),
            ("CLOSED", PROpenState.NOT_OPEN),
        ],
    )
    async def test_maps_github_state(self, gh_state: str, expected: PROpenState) -> None:
        with patch("sova.git.pr.get_pr_status", new=AsyncMock(return_value=_status(gh_state))):
            assert await get_pr_open_state(7, repo="o/r") is expected

    async def test_lookup_failure_is_unknown_and_not_cached(self) -> None:
        lookup = AsyncMock(side_effect=[RuntimeError("rate limited"), _status("OPEN")])
        with patch("sova.git.pr.get_pr_status", new=lookup):
            assert await get_pr_open_state(7, repo="o/r") is PROpenState.UNKNOWN
            assert await get_pr_open_state(7, repo="o/r") is PROpenState.OPEN
        assert lookup.await_count == 2

    @pytest.mark.parametrize("exc", [KeyError("number"), ValueError("bad payload"), OSError("gh missing")])
    async def test_malformed_payload_or_os_error_is_unknown(self, exc: Exception) -> None:
        with patch("sova.git.pr.get_pr_status", new=AsyncMock(side_effect=exc)):
            assert await get_pr_open_state(7, repo="o/r") is PROpenState.UNKNOWN

    async def test_confirmed_result_is_cached(self) -> None:
        lookup = AsyncMock(return_value=_status("MERGED"))
        with patch("sova.git.pr.get_pr_status", new=lookup):
            await get_pr_open_state(7, repo="o/r")
            await get_pr_open_state(7, repo="o/r")
        assert lookup.await_count == 1

    @pytest.mark.parametrize("gh_state", ["", "DRAFTED", "unexpected"])
    async def test_missing_or_unexpected_state_is_unknown_and_not_cached(self, gh_state: str) -> None:
        lookup = AsyncMock(side_effect=[_status(gh_state), _status("MERGED")])
        with patch("sova.git.pr.get_pr_status", new=lookup):
            assert await get_pr_open_state(7, repo="o/r") is PROpenState.UNKNOWN
            assert await get_pr_open_state(7, repo="o/r") is PROpenState.NOT_OPEN
        assert lookup.await_count == 2

    async def test_merged_is_not_looked_up_again_after_the_short_ttl(self) -> None:
        """A merged PR is terminal: only a CLOSED one can reopen, so only it is re-checked."""
        lookup = AsyncMock(return_value=_status("MERGED"))
        with patch("sova.git.pr.get_pr_status", new=lookup), patch("sova.git.pr.time.monotonic") as clock:
            clock.return_value = 0.0
            await get_pr_open_state(7, repo="o/r")
            clock.return_value = pr_module._PR_OPEN_CACHE_TTL + 1
            assert await get_pr_open_state(7, repo="o/r") is PROpenState.NOT_OPEN
            assert lookup.await_count == 1
            clock.return_value = pr_module._PR_MERGED_CACHE_TTL + 1
            await get_pr_open_state(7, repo="o/r")
        assert lookup.await_count == 2

    async def test_closed_is_rechecked_after_the_short_ttl(self) -> None:
        lookup = AsyncMock(return_value=_status("CLOSED"))
        with patch("sova.git.pr.get_pr_status", new=lookup), patch("sova.git.pr.time.monotonic") as clock:
            clock.return_value = 0.0
            await get_pr_open_state(7, repo="o/r")
            clock.return_value = pr_module._PR_OPEN_CACHE_TTL + 1
            await get_pr_open_state(7, repo="o/r")
        assert lookup.await_count == 2

    async def test_cache_expires_so_a_reopen_is_seen(self) -> None:
        lookup = AsyncMock(side_effect=[_status("CLOSED"), _status("OPEN")])
        with patch("sova.git.pr.get_pr_status", new=lookup), patch("sova.git.pr.time.monotonic") as clock:
            clock.return_value = 0.0
            assert await get_pr_open_state(7, repo="o/r") is PROpenState.NOT_OPEN
            clock.return_value = pr_module._PR_OPEN_CACHE_TTL + 1
            assert await get_pr_open_state(7, repo="o/r") is PROpenState.OPEN


class TestOpenStateCacheBound:
    def test_expired_entries_are_evicted_past_the_cap(self) -> None:
        cap = pr_module._PR_OPEN_CACHE_MAX
        for n in range(cap + 1):
            pr_module._pr_open_cache[("o/r", "u", n)] = (0.0, PROpenState.NOT_OPEN)
        pr_module._evict_expired_open_states(pr_module._PR_OPEN_CACHE_TTL + 1)
        assert pr_module._pr_open_cache == {}

    def test_live_entries_are_capped_oldest_first(self) -> None:
        cap = pr_module._PR_OPEN_CACHE_MAX
        for n in range(cap + 5):
            pr_module._pr_open_cache[("o/r", "u", n)] = (1000.0 + n * 0.01, PROpenState.OPEN)
        pr_module._evict_expired_open_states(999.0)
        assert len(pr_module._pr_open_cache) == cap
        assert ("o/r", "u", 0) not in pr_module._pr_open_cache
        assert ("o/r", "u", cap + 4) in pr_module._pr_open_cache

    def test_small_cache_is_left_alone(self) -> None:
        pr_module._pr_open_cache[("o/r", "u", 1)] = (0.0, PROpenState.OPEN)
        pr_module._evict_expired_open_states(pr_module._PR_OPEN_CACHE_TTL + 1)
        assert len(pr_module._pr_open_cache) == 1


class TestListOpenPrsRaiseOnError:
    async def test_cli_failure_raises_when_asked(self) -> None:
        failed = ShellResult(returncode=1, stdout="", stderr="boom")
        with patch("sova.git.pr.run", return_value=failed), patch("sova.git.pr.resolve_gh_env", return_value={}):
            with pytest.raises(PRLookupError):
                await list_open_prs(repo="o/r", raise_on_error=True)

    async def test_bad_json_raises_when_asked(self) -> None:
        bad = ShellResult(returncode=0, stdout="not json", stderr="")
        with patch("sova.git.pr.run", return_value=bad), patch("sova.git.pr.resolve_gh_env", return_value={}):
            with pytest.raises(PRLookupError):
                await list_open_prs(repo="o/r", raise_on_error=True)


class TestListOpenPrsWithStateFailureNotCached:
    async def test_failed_fetch_is_never_cached_as_no_open_prs(self, tmp_path: Path) -> None:
        """A non-raising caller's failed fetch must not serve [] to a later raise_on_error caller."""
        from sova.dashboard.services import pr_service

        async def _failing_list(*, raise_on_error: bool = False, **_kw: object) -> list[dict]:
            if raise_on_error:
                raise PRLookupError("down")
            return []

        cfg = MagicMock(github_repo="o/r", github_user="u")
        pr_service._pr_cache.clear()
        with (
            patch("sova.config.context.get_project_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.git.pr.list_open_prs", new=_failing_list),
        ):
            assert await pr_service.list_open_prs_with_state() == []
            with pytest.raises(PRLookupError):
                await pr_service.list_open_prs_with_state(raise_on_error=True)
        assert ("o/r", "u") not in pr_service._pr_cache


class TestListOpenPrsWithStateFailurePause:
    """A failed listing pauses further `gh pr list` calls briefly instead of re-firing on every poll."""

    @staticmethod
    def _patches(tmp_path: Path, listing: AsyncMock):
        cfg = MagicMock(github_repo="o/r", github_user="u")
        return (
            patch("sova.config.context.get_project_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.git.pr.list_open_prs", new=listing),
        )

    async def test_two_calls_after_a_failure_make_one_gh_call(self, tmp_path: Path) -> None:
        from sova.dashboard.services import pr_service

        pr_service._pr_cache.clear()
        pr_service._pr_fetch_failed_until.clear()
        listing = AsyncMock(side_effect=PRLookupError("down"))
        first, second, third = self._patches(tmp_path, listing)
        with first, second, third:
            assert await pr_service.list_open_prs_with_state() == []
            with pytest.raises(PRLookupError):
                await pr_service.list_open_prs_with_state(raise_on_error=True)
            assert await pr_service.list_open_prs_with_state() == []
        assert listing.await_count == 1

    async def test_pause_ends_and_a_success_clears_it(self, tmp_path: Path) -> None:
        from sova.dashboard.services import pr_service

        pr_service._pr_cache.clear()
        pr_service._pr_fetch_failed_until.clear()
        listing = AsyncMock(side_effect=[PRLookupError("down"), []])
        first, second, third = self._patches(tmp_path, listing)
        with first, second, third, patch("sova.dashboard.services.pr_service.time.monotonic") as clock:
            clock.return_value = 100.0
            assert await pr_service.list_open_prs_with_state() == []
            clock.return_value = 100.0 + pr_service._PR_FETCH_FAILURE_PAUSE + 1
            assert await pr_service.list_open_prs_with_state(raise_on_error=True) == []
        assert listing.await_count == 2
        assert ("o/r", "u") not in pr_service._pr_fetch_failed_until

    async def test_invalidation_lifts_the_pause(self, tmp_path: Path) -> None:
        from sova.dashboard.services import pr_service

        pr_service._pr_cache.clear()
        pr_service._pr_fetch_failed_until.clear()
        listing = AsyncMock(side_effect=[PRLookupError("down"), []])
        first, second, third = self._patches(tmp_path, listing)
        with first, second, third:
            await pr_service.list_open_prs_with_state()
            pr_service.invalidate_pr_cache("o/r")
            assert await pr_service.list_open_prs_with_state(raise_on_error=True) == []
        assert listing.await_count == 2


class TestOpenPrsEndpoint:
    async def test_failed_listing_is_an_error_not_an_empty_list(self) -> None:
        from fastapi import HTTPException

        from sova.dashboard.routers.prs import get_open_prs

        with patch(
            "sova.dashboard.routers.prs.list_open_prs_with_state",
            new=AsyncMock(side_effect=PRLookupError("down")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_open_prs()
        assert exc_info.value.status_code == 502

    async def test_success_asks_for_raised_failures(self) -> None:
        from sova.dashboard.routers.prs import get_open_prs

        listing = AsyncMock(return_value=[])
        with patch("sova.dashboard.routers.prs.list_open_prs_with_state", new=listing):
            assert await get_open_prs() == {"prs": []}
        listing.assert_awaited_once_with(raise_on_error=True)


class TestOpenPrNumbers:
    def test_fetched_list_gives_its_numbers(self) -> None:
        from sova.dashboard.services.work_item_service import _open_pr_numbers

        assert _open_pr_numbers([{"number": 1}, {"number": 2}], MagicMock(github_repo="o/r")) == {1, 2}

    def test_list_at_the_fetch_limit_is_none(self) -> None:
        from sova.dashboard.services.work_item_service import _open_pr_numbers

        full = [{"number": n} for n in range(1000, 1000 + OPEN_PR_LIST_LIMIT)]
        assert _open_pr_numbers(full, MagicMock(github_repo="o/r")) is None
        assert _open_pr_numbers(full[:-1], MagicMock(github_repo="o/r")) == {pr["number"] for pr in full[:-1]}

    @pytest.mark.parametrize(
        ("prs", "cfg"),
        [(None, MagicMock(github_repo="o/r")), ([], None), ([], MagicMock(github_repo=""))],
    )
    def test_unknown_open_state_is_none(self, prs: list[dict] | None, cfg: MagicMock | None) -> None:
        from sova.dashboard.services.work_item_service import _open_pr_numbers

        assert _open_pr_numbers(prs, cfg) is None


class TestSafePrs:
    async def test_failed_fetch_is_none_not_an_empty_list(self) -> None:
        with patch(
            "sova.dashboard.services.pr_service.list_open_prs_with_state",
            new=AsyncMock(side_effect=PRLookupError("down")),
        ):
            assert await _safe_prs(None) is None


class TestBuildTaskItemStalePr:
    @staticmethod
    def _task(pr_number: int | None) -> dict:
        task = {"issue": "42", "title": "t", "state": "researched", "labels": [], "priority": 2}
        task["last_run"] = {"id": 1, "status": "failed", "role": "developer", "pr_number": pr_number}
        return task

    def _build(self, pr_number: int | None, open_prs: set[int] | None) -> dict:
        return _build_task_item(self._task(pr_number), None, None, None, open_pr_numbers=open_prs)

    def test_merged_pr_is_dropped(self) -> None:
        item = self._build(100, {200})
        assert item["pr_number"] is None
        assert "pr" not in item["primary_action"].get("params", {})

    def test_open_pr_is_kept(self) -> None:
        assert self._build(100, {100, 200})["pr_number"] == 100

    def test_empty_successful_list_drops_the_pr(self) -> None:
        assert self._build(100, set())["pr_number"] is None

    def test_failed_fetch_keeps_todays_behavior(self) -> None:
        assert self._build(100, None)["pr_number"] == 100

    def test_no_pr_number_makes_no_check(self) -> None:
        assert self._build(None, set())["pr_number"] is None

    def test_default_without_open_list_keeps_the_pr(self) -> None:
        assert _build_task_item(self._task(100), None, None, None)["pr_number"] == 100


class TestRecoverLastPrNumber:
    @staticmethod
    def _states(mapping: dict[int, PROpenState]) -> AsyncMock:
        async def _lookup(pr: int, **_kw: object) -> PROpenState:
            return mapping[pr]

        return AsyncMock(side_effect=_lookup)

    @staticmethod
    def _cfg(repo: str = "o/r") -> MagicMock:
        return MagicMock(github_repo=repo, github_user="u")

    async def test_older_open_pr_wins_over_newer_merged_one(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_lifecycle import _first_open_pr

        states = self._states({300: PROpenState.NOT_OPEN, 200: PROpenState.OPEN, 100: PROpenState.OPEN})
        with (
            patch("sova.config.loader.load_config", return_value=self._cfg()),
            patch("sova.git.pr.get_pr_open_state", new=states),
        ):
            assert await _first_open_pr([300, 200, 100], tmp_path) == 200

    async def test_all_not_open_recovers_nothing(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_lifecycle import _first_open_pr

        states = self._states({300: PROpenState.NOT_OPEN})
        with (
            patch("sova.config.loader.load_config", return_value=self._cfg()),
            patch("sova.git.pr.get_pr_open_state", new=states),
        ):
            assert await _first_open_pr([300], tmp_path) is None

    async def test_unknown_falls_back_to_the_newest_unconfirmed(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_lifecycle import _first_open_pr

        states = self._states({300: PROpenState.UNKNOWN, 200: PROpenState.NOT_OPEN})
        with (
            patch("sova.config.loader.load_config", return_value=self._cfg()),
            patch("sova.git.pr.get_pr_open_state", new=states),
        ):
            assert await _first_open_pr([300, 200], tmp_path) == 300

    async def test_no_repo_makes_no_github_call(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_lifecycle import _first_open_pr

        lookup = AsyncMock()
        with (
            patch("sova.config.loader.load_config", return_value=self._cfg("")),
            patch("sova.git.pr.get_pr_open_state", new=lookup),
        ):
            assert await _first_open_pr([300, 200], tmp_path) == 300
        lookup.assert_not_awaited()

    async def test_deadline_returns_the_candidate_being_looked_up(self, tmp_path: Path) -> None:
        from sova.dashboard.services import agent_lifecycle

        async def _lookup(pr: int, **_kw: object) -> PROpenState:
            if pr == 300:
                return PROpenState.NOT_OPEN
            await asyncio.sleep(10)
            return PROpenState.OPEN

        with (
            patch("sova.config.loader.load_config", return_value=self._cfg()),
            patch("sova.git.pr.get_pr_open_state", new=AsyncMock(side_effect=_lookup)),
            patch.object(agent_lifecycle, "_RECOVER_PR_DEADLINE", 0.01),
        ):
            assert await agent_lifecycle._first_open_pr([300, 200, 100], tmp_path) == 200

    async def test_deadline_keeps_an_earlier_unconfirmed_candidate(self, tmp_path: Path) -> None:
        from sova.dashboard.services import agent_lifecycle

        async def _lookup(pr: int, **_kw: object) -> PROpenState:
            if pr == 300:
                return PROpenState.UNKNOWN
            await asyncio.sleep(10)
            return PROpenState.OPEN

        with (
            patch("sova.config.loader.load_config", return_value=self._cfg()),
            patch("sova.git.pr.get_pr_open_state", new=AsyncMock(side_effect=_lookup)),
            patch.object(agent_lifecycle, "_RECOVER_PR_DEADLINE", 0.01),
        ):
            assert await agent_lifecycle._first_open_pr([300, 200], tmp_path) == 300

    async def test_unreadable_config_keeps_the_newest_candidate(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_lifecycle import _first_open_pr

        with patch("sova.config.loader.load_config", side_effect=ValueError("bad config")):
            assert await _first_open_pr([300, 200], tmp_path) == 300


class TestRunKeepsStoredPrForDispatch:
    """`sova run` hands the stored PR to dispatch() untouched: only DeveloperRole resets on a closed PR."""

    @staticmethod
    async def _dispatched_ctx(
        tmp_path: Path, role_name: str | None, pr_number: int | None, checkpoint_role: str | None = None
    ) -> ExecutionContext:
        from sova.cli.commands.run import _run_workflow
        from sova.roles.base import RoleResult

        worktree = tmp_path / "wt"
        worktree.mkdir()
        checkpoint = {
            "role": checkpoint_role,
            "pr_number": 88,
            "branch_name": "feat/issue-42",
            "worktree_dir": worktree,
            "completed_steps": {"create_worktree", "validate", "push"},
        }
        role = MagicMock()
        role.name = "reviewer"
        dispatch = AsyncMock(return_value=(role, RoleResult(success=True, summary="done")))
        lookup = AsyncMock(return_value=PROpenState.NOT_OPEN)
        with (
            patch("sova.config.loader.load_config", return_value=ProjectConfig(github_repo="o/r")),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch("sova.db.session.init_db", new=AsyncMock()),
            patch("sova.utils.logging.setup_logging"),
            patch("sova.roles.dispatcher.dispatch", new=dispatch),
            patch("sova.cli.commands.run._install_sigterm_handler"),
            patch("sova.cli.commands.run._load_checkpoint", new=AsyncMock(return_value=checkpoint)),
            patch("sova.git.pr.get_pr_open_state", new=lookup),
        ):
            await _run_workflow(
                "42", project_dir=tmp_path, role_name=role_name, force=False, resume_run_id=5, pr_number=pr_number
            )
        lookup.assert_not_awaited()
        return dispatch.await_args.args[0]

    @pytest.mark.parametrize(
        ("role_name", "checkpoint_role"),
        [(None, None), (None, "reviewer"), ("reviewer", None), (None, "custom"), ("developer", "developer")],
    )
    @pytest.mark.parametrize("pr_number", [None, 99])
    async def test_a_not_open_pr_reaches_dispatch_unchanged(
        self, tmp_path: Path, role_name: str | None, checkpoint_role: str | None, pr_number: int | None
    ) -> None:
        """No --role on an IN_REVIEW issue lets dispatch() pick the reviewer, which needs the real PR and branch."""
        ctx = await self._dispatched_ctx(tmp_path, role_name, pr_number, checkpoint_role)
        assert ctx.pr_number == (pr_number or 88)
        assert ctx.branch_name == "feat/issue-42"
        assert ctx.worktree_dir == tmp_path / "wt"
        assert ctx.completed_steps == frozenset({"create_worktree", "validate", "push"})


class TestStartFreshAfterClosedPr:
    @staticmethod
    def _ctx(tmp_path: Path, **kwargs: object) -> ExecutionContext:
        return ExecutionContext(
            project_dir=tmp_path,
            config=ProjectConfig(),
            adapter=AsyncMock(),
            pr_number=88,
            branch_name="feat/issue-42",
            worktree_dir=tmp_path,
            completed_steps=frozenset({"push"}),
            **kwargs,
        )

    def test_issue_run_gets_a_branch_the_closed_pr_never_used(self, tmp_path: Path) -> None:
        from sova.core.steps.create_worktree import start_fresh_after_closed_pr

        ctx = self._ctx(tmp_path, issue_number="42")
        start_fresh_after_closed_pr(ctx, 88)
        assert (ctx.pr_number, ctx.worktree_dir, ctx.completed_steps) == (None, None, frozenset())
        assert ctx.branch_name == "feat/issue-42-after-pr-88"

    def test_issue_less_run_uses_its_label(self, tmp_path: Path) -> None:
        from sova.core.steps.create_worktree import start_fresh_after_closed_pr

        ctx = self._ctx(tmp_path, run_label="developer 1")
        start_fresh_after_closed_pr(ctx, 7)
        assert ctx.branch_name == "feat/developer-1-after-pr-7"


class TestDeveloperRoutesOnPrState:
    @staticmethod
    def _ctx(tmp_path: Path, pr_number: int | None, repo: str = "o/r") -> ExecutionContext:
        adapter = AsyncMock()
        adapter.get_task.return_value = MagicMock(state=TaskState.IN_REVIEW)
        return ExecutionContext(
            project_dir=tmp_path,
            config=ProjectConfig(github_repo=repo),
            adapter=adapter,
            issue_number="42",
            role="developer",
            force=True,
            pr_number=pr_number,
        )

    @pytest.mark.parametrize(
        ("state", "address_review"),
        [(PROpenState.NOT_OPEN, False), (PROpenState.OPEN, True), (PROpenState.UNKNOWN, True)],
    )
    async def test_routing(self, tmp_path: Path, state: PROpenState, address_review: bool) -> None:
        from sova.roles.developer import DeveloperRole

        role = DeveloperRole()
        ctx = self._ctx(tmp_path, 88)
        with (
            patch("sova.roles.developer.get_pr_open_state", new=AsyncMock(return_value=state)),
            patch.object(role, "_execute_address_review", new=AsyncMock()) as address,
            patch.object(role, "_execute_development", new=AsyncMock()) as develop,
        ):
            await role.execute(ctx)

        assert address.await_count == int(address_review)
        assert develop.await_count == int(not address_review)
        assert ctx.pr_number == (88 if address_review else None)

    async def test_closed_pr_leaves_its_branch_and_worktree(self, tmp_path: Path) -> None:
        from sova.roles.developer import DeveloperRole

        role = DeveloperRole()
        ctx = self._ctx(tmp_path, 88)
        ctx.branch_name = "feat/issue-42"
        ctx.worktree_dir = tmp_path
        ctx.completed_steps = frozenset({"create_worktree", "push"})
        with (
            patch("sova.roles.developer.get_pr_open_state", new=AsyncMock(return_value=PROpenState.NOT_OPEN)),
            patch.object(role, "_execute_development", new=AsyncMock()),
        ):
            await role.execute(ctx)

        assert ctx.branch_name == "feat/issue-42-after-pr-88"
        assert ctx.worktree_dir is None
        assert ctx.completed_steps == frozenset()

    async def test_merged_pr_then_new_run_replays_the_1251_sequence(self, tmp_path: Path) -> None:
        """A later run carrying a merged PR's number runs the development pipeline on a fresh branch."""
        from sova.core.workflow import WorkflowResult
        from sova.roles.developer import DeveloperRole

        seen: dict[str, object] = {}

        class _RecordingEngine:
            def __init__(self, steps: list, ctx: ExecutionContext) -> None:
                seen["ctx"] = ctx
                seen["steps"] = [step.name for step in steps]

            async def run(self) -> WorkflowResult:
                ctx = seen["ctx"]
                seen["at_run"] = (ctx.pr_number, ctx.branch_name, ctx.worktree_dir, ctx.completed_steps)
                return WorkflowResult(success=True, final_status="done")

        role = DeveloperRole()
        ctx = self._ctx(tmp_path, 88)
        ctx.branch_name = "feat/issue-42"
        ctx.worktree_dir = tmp_path
        ctx.completed_steps = frozenset({"create_worktree", "develop", "push", "create_pr"})
        with (
            patch("sova.roles.developer.get_pr_open_state", new=AsyncMock(return_value=PROpenState.NOT_OPEN)),
            patch("sova.roles.developer.WorkflowEngine", new=_RecordingEngine),
            patch.object(role, "_execute_address_review", new=AsyncMock()) as address,
        ):
            result = await role.execute(ctx)

        address.assert_not_awaited()
        assert result.success
        assert "create_worktree" in seen["steps"]
        assert "address_review" not in seen["steps"]
        assert seen["at_run"] == (None, "feat/issue-42-after-pr-88", None, frozenset())

    async def test_no_pr_number_makes_no_check(self, tmp_path: Path) -> None:
        from sova.roles.developer import DeveloperRole

        role = DeveloperRole()
        lookup = AsyncMock()
        with (
            patch("sova.roles.developer.get_pr_open_state", new=lookup),
            patch.object(role, "_execute_development", new=AsyncMock()),
        ):
            await role.execute(self._ctx(tmp_path, None))
        lookup.assert_not_awaited()

    async def test_no_repo_makes_no_check(self, tmp_path: Path) -> None:
        from sova.roles.developer import DeveloperRole

        role = DeveloperRole()
        lookup = AsyncMock()
        with (
            patch("sova.roles.developer.get_pr_open_state", new=lookup),
            patch.object(role, "_execute_address_review", new=AsyncMock()) as address,
        ):
            await role.execute(self._ctx(tmp_path, 88, repo=""))
        lookup.assert_not_awaited()
        address.assert_awaited_once()


class TestGetWorkItemsStalePr:
    """get_work_items() derives the open list from the one PR fetch it already makes."""

    @staticmethod
    async def _pr_number_for_issue(
        prs: list[dict] | None, repo: str = "o/r", lookup: PROpenState = PROpenState.NOT_OPEN
    ) -> int | None:
        from sova.dashboard.services.work_item_service import get_work_items

        task = {
            "issue": "42",
            "title": "t",
            "state": "researched",
            "labels": [],
            "priority": 2,
            "last_run": {"id": 1, "status": "failed", "role": "developer", "pr_number": 100},
        }
        cfg = MagicMock(github_repo=repo, github_user="u")
        cfg.pipeline.max_address_review_cycles = 0
        with (
            patch(
                "sova.dashboard.services.work_item_service._fetch_all_sources",
                new=AsyncMock(return_value=([task], prs, [], {"agents": [], "completed": []})),
            ),
            patch("sova.dashboard.services.work_item_service._load_graph_or_none", new=AsyncMock(return_value=None)),
            patch("sova.dashboard.services.work_item_service._fetch_sova_verdicts", new=AsyncMock(return_value={})),
            patch("sova.dashboard.services.agent_pool._get_project_agents", MagicMock(return_value=None)),
            patch("sova.config.loader.load_config", return_value=cfg),
            patch(
                "sova.dashboard.services.work_item_service.get_pr_open_state",
                new=AsyncMock(return_value=lookup),
            ),
        ):
            result = await get_work_items(project_dir=None)
        return next(i for i in result["items"] if i["issue_number"] == "42")["pr_number"]

    async def test_successful_fetch_without_the_pr_drops_it(self) -> None:
        assert await self._pr_number_for_issue([]) is None

    async def test_failed_fetch_keeps_the_pr(self) -> None:
        assert await self._pr_number_for_issue(None) == 100

    async def test_unconfigured_repo_keeps_the_pr(self) -> None:
        assert await self._pr_number_for_issue([], repo="") == 100

    async def test_pr_missing_from_a_stale_cached_list_is_confirmed_open(self) -> None:
        """A PR opened after the cached list was built is kept once a direct lookup says it is open."""
        assert await self._pr_number_for_issue([], lookup=PROpenState.OPEN) == 100

    async def test_lookup_outage_keeps_the_pr(self) -> None:
        assert await self._pr_number_for_issue([], lookup=PROpenState.UNKNOWN) == 100

    async def test_lookup_confirming_closed_drops_the_pr(self) -> None:
        assert await self._pr_number_for_issue([], lookup=PROpenState.NOT_OPEN) is None


class TestConfirmOpenPrNumbers:
    """The per-row lookup is limited to PRs the cached open list cannot settle."""

    @staticmethod
    def _queue(*prs: int) -> list[dict]:
        return [{"issue": str(i), "last_run": {"pr_number": pr}} for i, pr in enumerate(prs, start=1)]

    @staticmethod
    async def _confirm(open_numbers: set[int] | None, queue: list[dict], lookup: AsyncMock) -> set[int] | None:
        from sova.dashboard.services.work_item_service import _confirm_open_pr_numbers

        cfg = MagicMock(github_repo="o/r", github_user="u")
        with patch("sova.dashboard.services.work_item_service.get_pr_open_state", new=lookup):
            return await _confirm_open_pr_numbers(open_numbers, queue, cfg)

    async def test_pr_below_the_newest_listed_needs_no_lookup(self) -> None:
        lookup = AsyncMock(return_value=PROpenState.NOT_OPEN)
        result = await self._confirm({500}, self._queue(100, 200, 300), lookup)
        assert result == {500}
        lookup.assert_not_awaited()

    async def test_pr_above_the_newest_listed_is_looked_up(self) -> None:
        lookup = AsyncMock(return_value=PROpenState.OPEN)
        assert await self._confirm({500}, self._queue(100, 501), lookup) == {500, 501}
        lookup.assert_awaited_once()

    async def test_unknown_lookup_backs_off_instead_of_retrying_every_poll(self) -> None:
        lookup = AsyncMock(return_value=PROpenState.UNKNOWN)
        queue = self._queue(601, 602)
        assert await self._confirm({500}, queue, lookup) == {500, 601, 602}
        assert lookup.await_count == 2
        assert await self._confirm({500}, queue, lookup) == {500, 601, 602}
        assert lookup.await_count == 2

    async def test_lookups_are_bounded(self) -> None:
        from sova.dashboard.services.work_item_service import _CONFIRM_CONCURRENCY

        running = peak = 0

        async def lookup(*_args: object, **_kwargs: object) -> PROpenState:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            running -= 1
            return PROpenState.NOT_OPEN

        await self._confirm(set(), self._queue(*range(1, 21)), AsyncMock(side_effect=lookup))
        assert 0 < peak <= _CONFIRM_CONCURRENCY

    async def test_confirmed_candidates_do_not_make_a_short_list_unknown(self) -> None:
        """99 fetched PRs plus 2 confirmed last-run PRs must still drop a PR that is not open."""
        from sova.dashboard.services.work_item_service import _open_pr_numbers

        fetched = [{"number": n} for n in range(1000, 1000 + OPEN_PR_LIST_LIMIT - 1)]
        cfg = MagicMock(github_repo="o/r", github_user="u")
        open_numbers = _open_pr_numbers(fetched, cfg)
        lookup = AsyncMock(return_value=PROpenState.OPEN)
        merged = await self._confirm(open_numbers, self._queue(2000, 2001), lookup)
        assert merged is not None and len(merged) >= OPEN_PR_LIST_LIMIT
        assert pr_open_state_from_list(5, merged) is PROpenState.NOT_OPEN

    async def test_truncated_list_makes_no_lookups(self) -> None:
        from sova.dashboard.services.work_item_service import _open_pr_numbers

        full = [{"number": n} for n in range(1000, 1000 + OPEN_PR_LIST_LIMIT)]
        lookup = AsyncMock()
        open_numbers = _open_pr_numbers(full, MagicMock(github_repo="o/r"))
        assert await self._confirm(open_numbers, self._queue(2000), lookup) is None
        lookup.assert_not_awaited()
