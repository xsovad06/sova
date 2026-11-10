"""Suitability check on a manual launch (issue #1231).

`start_agent(enforce_suitability=True)` refuses an epic or human-only issue unless
`override_suitability` is set; the dashboard routers turn the refusal into a structured 409, and
`sova run` applies the same check to an issue typed on the command line.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from httpx import ASGITransport, AsyncClient

from sova.adapters.base import Task, TaskState
from sova.config.models import RolesConfig
from sova.dashboard.services import agent_lifecycle, agent_validation
from sova.dashboard.services.agent_validation import (
    RolePinUnavailable,
    check_issue_suitability,
    classify_issue_suitability,
    pin_override_role,
    suitability_blocked_detail,
)
from sova.dashboard.services.batch_service import start_batch_run
from sova.dashboard.services.control_service import ProjectAgents, start_agent
from sova.supervisor.dependency_graph import DependencyGraph
from tests.conftest import csrf_request_headers as _csrf_request_headers

_BLOCKED = {
    "code": "suitability_blocked",
    "error": "Issue #7 is classified epic (label: type: epic) and is not meant for an agent.",
    "mode": "epic",
    "reasons": [{"source": "label", "value": "type: epic"}],
}


@asynccontextmanager
async def _no_session(*_args: object, **_kwargs: object) -> AsyncIterator[MagicMock]:
    yield MagicMock()


def _tracker(
    labels: list[str],
    *,
    local: str | None = None,
    fail: bool = False,
    nicknames: dict[str, str] | None = None,
    assessment: AsyncMock | None = None,
    state: TaskState = TaskState.BACKLOG,
) -> tuple[Any, Any, Any, Any]:
    """Patch the tracker, config and local-assessment lookups `check_issue_suitability` reads."""
    adapter = MagicMock()
    adapter.get_state = AsyncMock(return_value=state)
    if fail:
        adapter.get_task = AsyncMock(side_effect=RuntimeError("tracker down"))
    else:
        adapter.get_task = AsyncMock(return_value=Task(id="7", title="t", labels=labels, state=state))
    record = MagicMock(suitability=local) if local else None
    cfg = MagicMock(github_repo="o/r", roles=RolesConfig(nicknames=nicknames or {}))
    return (
        patch("sova.config.loader.load_config", return_value=cfg),
        patch("sova.adapters.create_adapter", return_value=adapter),
        patch("sova.db.session.get_session", new=AsyncMock(side_effect=lambda **_: _no_session())),
        patch(
            "sova.knowledge.assessments.get_latest_assessment",
            new=assessment if assessment is not None else AsyncMock(return_value=record),
        ),
    )


async def _check(labels: list[str], role: str | None = "developer", **kw: object) -> dict | None:
    p1, p2, p3, p4 = _tracker(labels, **kw)  # type: ignore[arg-type]
    with p1, p2, p3, p4:
        return await check_issue_suitability("7", role, Path("/tmp"))


class TestCheckIssueSuitability:
    async def test_epic_is_blocked_with_reasons(self) -> None:
        result = await _check(["type: epic"])
        assert result is not None
        assert result["code"] == "suitability_blocked"
        assert result["mode"] == "epic"
        assert result["reasons"] == [{"source": "label", "value": "type: epic"}]
        assert "#7" in result["error"]

    async def test_human_only_label_is_blocked(self) -> None:
        result = await _check(["agent:human-only"])
        assert result is not None
        assert result["mode"] == "human"

    async def test_local_human_only_record_is_blocked(self) -> None:
        result = await _check([], local="human_only")
        assert result is not None
        assert result["mode"] == "human"
        assert result["reasons"] == [{"source": "local_assessment", "value": "human_only"}]

    @pytest.mark.parametrize("labels", [["agent:interactive-recommended"], ["agent:on-hold"], ["agent:ready"], []])
    async def test_other_modes_are_not_blocked(self, labels: list[str]) -> None:
        assert await _check(labels) is None

    @pytest.mark.parametrize("role", ["triage", "reviewer"])
    async def test_triage_and_reviewer_are_never_blocked(self, role: str) -> None:
        assert await _check(["type: epic"], role=role) is None

    @pytest.mark.parametrize("role", ["developer", "researcher", "planner", "custom-role"])
    async def test_every_other_role_is_gated(self, role: str) -> None:
        assert await _check(["type: epic"], role=role) is not None

    @pytest.mark.parametrize("state", [TaskState.BACKLOG, TaskState.IN_REVIEW])
    async def test_no_role_on_a_state_the_dispatcher_triages_or_reviews_is_not_blocked(self, state: TaskState) -> None:
        assert await _check(["type: epic"], role=None, state=state) is None

    @pytest.mark.parametrize(
        "state", [TaskState.TRIAGED, TaskState.RESEARCHED, TaskState.IN_PROGRESS, TaskState.HUMAN_ONLY]
    )
    async def test_no_role_on_any_other_state_is_gated(self, state: TaskState) -> None:
        result = await _check(["type: epic"], role=None, state=state)
        assert result is not None
        assert result["mode"] == "epic"

    async def test_no_role_and_an_unreadable_issue_fails_open(self) -> None:
        p1, p2, p3, p4 = _tracker([], fail=True)
        with p1, p2, p3, p4:
            assert await check_issue_suitability("7", None, Path("/tmp")) is None

    async def test_no_issue_skips_the_check(self) -> None:
        with patch("sova.dashboard.services.agent_validation.classify_issue_suitability") as classify:
            assert await check_issue_suitability("", "developer", Path("/tmp")) is None
        classify.assert_not_called()

    async def test_classification_failure_fails_open_and_logs(self) -> None:
        p1, p2, p3, p4 = _tracker([], fail=True)
        with p1, p2, p3, p4, patch("sova.dashboard.services.agent_validation.log") as log:
            assert await check_issue_suitability("7", "developer", Path("/tmp")) is None
        assert log.warning.call_args.args[0] == "issue_suitability_check.failed"
        assert log.warning.call_args.kwargs["exc_info"] is True

    @pytest.mark.parametrize("nickname", ["rev", "tri"])
    async def test_nickname_of_an_exempt_role_is_never_blocked(self, nickname: str) -> None:
        result = await _check(["type: epic"], role=nickname, nicknames={"rev": "reviewer", "tri": "triage"})
        assert result is None

    async def test_nickname_of_a_gated_role_is_still_blocked(self) -> None:
        assert await _check(["type: epic"], role="dev", nicknames={"dev": "developer"}) is not None

    async def test_unreadable_config_still_gates_and_classifies(self) -> None:
        p1, p2, p3, p4 = _tracker(["type: epic"])
        with (
            p2,
            p3,
            p4,
            patch(
                "sova.config.loader.load_config", side_effect=[RuntimeError("bad config"), MagicMock(github_repo="o/r")]
            ),
            patch("sova.dashboard.services.agent_validation.log") as log,
        ):
            result = await check_issue_suitability("7", "developer", Path("/tmp"))
        assert result is not None
        assert log.warning.call_args_list[0].args[0] == "issue_suitability_check.config_failed"

    async def test_assessment_is_looked_up_by_the_trackers_canonical_id(self) -> None:
        records = {"7": MagicMock(suitability="human_only")}
        lookup = AsyncMock(side_effect=lambda _s, *, issue_number, project_slug: records.get(issue_number))
        p1, p2, p3, p4 = _tracker([], assessment=lookup)
        with p1, p2, p3, p4:
            result = await check_issue_suitability("007", "developer", Path("/tmp"))
        assert result is not None
        assert result["mode"] == "human"
        assert lookup.call_args_list[0].kwargs == {"issue_number": "7", "project_slug": "o/r"}

    async def test_assessment_under_the_typed_id_is_still_found(self) -> None:
        records = {"007": MagicMock(suitability="human_only")}
        lookup = AsyncMock(side_effect=lambda _s, *, issue_number, project_slug: records.get(issue_number))
        p1, p2, p3, p4 = _tracker([], assessment=lookup)
        with p1, p2, p3, p4:
            result = await check_issue_suitability("007", "developer", Path("/tmp"))
        assert result is not None
        assert [c.kwargs["issue_number"] for c in lookup.call_args_list] == ["7", "007"]

    async def test_no_assessment_under_either_id_is_unclassified(self) -> None:
        lookup = AsyncMock(return_value=None)
        p1, p2, p3, p4 = _tracker([], assessment=lookup)
        with p1, p2, p3, p4:
            assert await check_issue_suitability("7", "developer", Path("/tmp")) is None
        assert lookup.await_count == 1  # canonical and typed ids are the same, so it is read once

    async def test_stalled_classification_times_out_and_fails_open(self) -> None:
        async def _stall(*_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(10)

        with (
            patch.object(agent_validation, "_SUITABILITY_CHECK_TIMEOUT_S", 0.01),
            patch.object(agent_validation, "_read_issue_suitability", _stall),
            patch.object(agent_validation, "log") as log,
        ):
            assert await check_issue_suitability("7", "developer", Path("/tmp")) is None
        assert log.warning.call_args.args[0] == "issue_suitability_check.timed_out"
        assert log.warning.call_args.kwargs == {"issue": "7", "timeout_s": 0.01}

    async def test_task_fetch_and_classification_share_one_deadline(self) -> None:
        # Each step alone fits the timeout (0.06 < 0.1) but together they do not: with a deadline per step the
        # check would hold the project lock for the sum and still return a verdict.
        async def _slow_fetch(*_args: object, **_kwargs: object) -> Task:
            await asyncio.sleep(0.06)
            return Task(id="7", title="t", labels=["type: epic"], state=TaskState.TRIAGED)

        async def _slow_read(*_args: object, **_kwargs: object) -> object:
            await asyncio.sleep(0.06)
            return MagicMock(mode=agent_validation.SuitabilityMode.EPIC, reasons=[])

        with (
            patch.object(agent_validation, "_SUITABILITY_CHECK_TIMEOUT_S", 0.1),
            patch.object(agent_validation, "_fetch_launch_task", _slow_fetch),
            patch.object(agent_validation, "_read_issue_suitability", _slow_read),
            patch.object(agent_validation, "log") as log,
            patch("sova.config.loader.load_config", return_value=MagicMock(roles=RolesConfig())),
        ):
            result = await check_issue_suitability("7", None, Path("/tmp"))
        assert result is None  # unknown fails open
        assert log.warning.call_args.args[0] == "issue_suitability_check.timed_out"

    async def test_open_dependency_is_read_only_when_a_graph_is_passed(self) -> None:
        graph = DependencyGraph(
            [
                Task(id="7", title="t", body="## Dependencies\n\n- #3\n", state=TaskState.TRIAGED),
                Task(id="3", title="dep", state=TaskState.IN_PROGRESS),
            ]
        )
        p1, p2, p3, p4 = _tracker([], state=TaskState.TRIAGED)
        with p1, p2, p3, p4:
            without = await classify_issue_suitability("7", Path("/tmp"))
            with_graph = await classify_issue_suitability("7", Path("/tmp"), graph=graph)
        assert without is not None
        assert without.mode.value == "unclassified"
        assert with_graph is not None
        assert with_graph.mode.value == "blocked"
        assert with_graph.reasons == [{"source": "dependency", "value": 3}]

    async def test_graph_without_the_issue_does_not_fail_closed(self) -> None:
        graph = DependencyGraph([Task(id="99", title="other")])
        p1, p2, p3, p4 = _tracker(["agent:ready"], state=TaskState.TRIAGED)
        with p1, p2, p3, p4:
            result = await classify_issue_suitability("7", Path("/tmp"), graph=graph)
        assert result is not None
        assert result.mode.value == "autonomous"

    def test_detail_carries_the_code_message_and_reasons(self) -> None:
        assert suitability_blocked_detail(_BLOCKED) == {
            "code": "suitability_blocked",
            "message": _BLOCKED["error"],
            "mode": "epic",
            "reasons": _BLOCKED["reasons"],
        }


class TestStartAgentSuitability:
    def _patches(self, pa: ProjectAgents, check: AsyncMock) -> tuple[Any, ...]:
        return (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "check_issue_suitability", check),
            patch.object(agent_lifecycle, "_recover_last_pr_number", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_issue_worktree", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value=""),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=5),
            patch.object(agent_lifecycle, "spawn_direct", new_callable=AsyncMock),
        )

    async def _run(
        self, check: AsyncMock, project_dir: Path | None = None, **kwargs: object
    ) -> tuple[dict, ProjectAgents, tuple]:
        pa = ProjectAgents() if project_dir is None else ProjectAgents(project_dir=project_dir)
        patches = self._patches(pa, check)
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch_) for patch_ in patches]
            result = await start_agent("7", role="developer", **kwargs)  # type: ignore[arg-type]
        return result, pa, (mocks[3], mocks[5], mocks[6])

    async def test_blocked_start_touches_nothing(self) -> None:
        result, pa, (wt, create, spawn) = await self._run(
            AsyncMock(return_value=_BLOCKED), enforce_suitability=True, force=True
        )
        assert result is _BLOCKED
        wt.assert_not_called()
        create.assert_not_called()
        spawn.assert_not_called()
        assert pa.agents == {}

    async def test_override_skips_the_check(self) -> None:
        check = AsyncMock(return_value=_BLOCKED)
        budget = {"error": "over budget", "code": "budget_exceeded"}
        with patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value=budget):
            result, _pa, (_wt, create, _spawn) = await self._run(
                check, enforce_suitability=True, override_suitability=True
            )
        check.assert_not_called()
        assert result is budget  # reached the later budget check, past the skipped suitability one
        create.assert_not_called()

    async def test_not_enforced_never_classifies(self) -> None:
        check = AsyncMock(return_value=_BLOCKED)
        with patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value={"error": "x"}):
            await self._run(check)
        check.assert_not_called()

    async def test_enforced_unblocked_issue_proceeds_to_later_checks(self) -> None:
        check = AsyncMock(return_value=None)
        with patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value={"error": "x"}):
            result, *_ = await self._run(check, enforce_suitability=True)
        check.assert_awaited_once()
        assert result == {"error": "x"}

    async def test_role_and_project_dir_reach_the_check(self, tmp_path: Path) -> None:
        check = AsyncMock(return_value=_BLOCKED)
        await self._run(check, project_dir=tmp_path, enforce_suitability=True)
        check.assert_awaited_once_with("7", "developer", tmp_path)

    @pytest.mark.parametrize("override", [True, False])
    async def test_override_reaches_the_spawned_command_only_when_set(self, override: bool) -> None:
        # The spawn fails on purpose: the command line is all this test needs, and a failed spawn returns
        # before any output reader or finalizer task is started.
        check = AsyncMock(return_value=None)
        with (
            patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value={}),
            patch("sova.dashboard.services.agent_context.merge_mcp_env", return_value={}),
            patch.object(agent_lifecycle, "_finalize_orphaned_run", new_callable=AsyncMock),
        ):
            pa = ProjectAgents()
            with ExitStack() as stack:
                mocks = [stack.enter_context(patch_) for patch_ in self._patches(pa, check)]
                spawn = mocks[6]
                spawn.side_effect = RuntimeError("no spawn in tests")
                result = await start_agent(
                    "7",
                    role="developer",
                    force=True,
                    enforce_suitability=True,
                    override_suitability=override,
                    _skip_handoff_clear=True,
                )
        assert "Failed to spawn" in result["error"]
        cmd_parts = spawn.call_args.args[0]
        assert ("--override-suitability" in cmd_parts) is override
        # The override is never consulted when set, and gates the check when not.
        assert check.await_count == (0 if override else 1)


class TestResumeSuitability:
    """A resume inherits how the resumed run's suitability check went, so an overridden run can be resumed."""

    async def _resume(self, prior: str | None) -> tuple[dict, AsyncMock, Any, Any]:
        check = AsyncMock(return_value=_BLOCKED)
        pa = ProjectAgents()
        with (
            patch.object(agent_lifecycle, "_load_suitability_check", new=AsyncMock(return_value=prior)),
            patch.object(agent_lifecycle, "_check_issue_conflict", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value={}),
            patch("sova.dashboard.services.agent_context.merge_mcp_env", return_value={}),
            patch.object(agent_lifecycle, "_finalize_orphaned_run", new_callable=AsyncMock),
            ExitStack() as stack,
        ):
            mocks = [stack.enter_context(patch_) for patch_ in TestStartAgentSuitability()._patches(pa, check)]
            mocks[6].side_effect = RuntimeError("no spawn in tests")
            result = await start_agent(
                "7",
                role="developer",
                force=True,
                enforce_suitability=True,
                resume_run_id=4,
                _skip_handoff_clear=True,
            )
        return result, check, mocks[5], mocks[6]

    async def test_resume_of_an_overridden_run_is_not_refused_and_stays_overridden(self) -> None:
        result, check, create, spawn = await self._resume("overridden")
        assert "Failed to spawn" in result["error"]  # got past the suitability check to the spawn
        check.assert_not_called()
        cmd_parts = spawn.call_args.args[0]
        assert "--override-suitability" in cmd_parts  # the developer state gate still needs it
        assert cmd_parts[cmd_parts.index("--resume") + 1] == "4"
        assert create.call_args.kwargs["assessment"] == {"suitability_check": "overridden"}

    async def test_resume_of_an_autonomous_run_is_not_refused(self) -> None:
        result, check, create, spawn = await self._resume("skipped")
        assert "Failed to spawn" in result["error"]
        check.assert_not_called()
        assert "--override-suitability" not in spawn.call_args.args[0]
        assert create.call_args.kwargs["assessment"] == {"suitability_check": "skipped"}

    @pytest.mark.parametrize("prior", [None, "enforced"])
    async def test_resume_of_a_run_that_was_checked_is_checked_again(self, prior: str | None) -> None:
        result, check, create, spawn = await self._resume(prior)
        assert result is _BLOCKED
        check.assert_awaited_once()
        create.assert_not_called()
        spawn.assert_not_called()

    async def test_a_fresh_override_is_recorded_on_the_new_run(self) -> None:
        pa = ProjectAgents()
        with (
            patch.object(agent_lifecycle, "_check_issue_conflict", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value={}),
            patch("sova.dashboard.services.agent_context.merge_mcp_env", return_value={}),
            patch.object(agent_lifecycle, "_finalize_orphaned_run", new_callable=AsyncMock),
            ExitStack() as stack,
        ):
            mocks = [
                stack.enter_context(patch_)
                for patch_ in TestStartAgentSuitability()._patches(pa, AsyncMock(return_value=None))
            ]
            mocks[6].side_effect = RuntimeError("no spawn in tests")
            await start_agent(
                "7",
                role="developer",
                force=True,
                enforce_suitability=True,
                override_suitability=True,
                _skip_handoff_clear=True,
            )
        assert mocks[5].call_args.kwargs["assessment"] == {"suitability_check": "overridden"}

    async def test_an_enforced_launch_that_passed_records_nothing(self) -> None:
        pa = ProjectAgents()
        with (
            patch.object(agent_lifecycle, "_check_issue_conflict", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value={}),
            patch("sova.dashboard.services.agent_context.merge_mcp_env", return_value={}),
            patch.object(agent_lifecycle, "_finalize_orphaned_run", new_callable=AsyncMock),
            ExitStack() as stack,
        ):
            mocks = [
                stack.enter_context(patch_)
                for patch_ in TestStartAgentSuitability()._patches(pa, AsyncMock(return_value=None))
            ]
            mocks[6].side_effect = RuntimeError("no spawn in tests")
            await start_agent("7", role="developer", force=True, enforce_suitability=True, _skip_handoff_clear=True)
        assert mocks[5].call_args.kwargs["assessment"] is None

    async def test_marker_round_trips_through_the_task_run(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_db import _create_task_run, _load_suitability_check
        from sova.db.session import init_db

        await init_db(project_dir=tmp_path)
        overridden = await _create_task_run("7", "developer", tmp_path, assessment={"suitability_check": "overridden"})
        plain = await _create_task_run("8", "developer", tmp_path)
        assert overridden is not None
        assert plain is not None
        assert await _load_suitability_check(overridden, tmp_path) == "overridden"
        assert await _load_suitability_check(plain, tmp_path) is None
        assert await _load_suitability_check(9999, tmp_path) is None


class TestOverrideRolePin:
    """An override with no role on a HUMAN_ONLY issue names a role: dispatch() has none for that state."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [(TaskState.HUMAN_ONLY, "developer"), (TaskState.BACKLOG, None), (TaskState.IN_REVIEW, None)],
    )
    async def test_pin_follows_the_tracker_state(self, state: TaskState, expected: str | None) -> None:
        p1, p2, p3, p4 = _tracker([], state=state)
        with p1, p2, p3, p4:
            assert await pin_override_role("7", None, Path("/tmp")) == expected

    async def test_an_explicit_role_is_kept_without_reading_the_tracker(self) -> None:
        with patch("sova.adapters.create_adapter") as create_adapter:
            assert await pin_override_role("7", "researcher", Path("/tmp")) == "researcher"
        create_adapter.assert_not_called()

    async def test_unreadable_state_raises_instead_of_looking_like_route_by_state(self) -> None:
        p1, p2, p3, p4 = _tracker([], fail=True)
        with p1, p2 as create_adapter, p3, p4:
            create_adapter.return_value.get_state = AsyncMock(side_effect=RuntimeError("down"))
            with pytest.raises(RolePinUnavailable, match="explicit role"):
                await pin_override_role("7", None, Path("/tmp"))

    async def test_start_agent_refuses_a_roleless_override_when_the_state_is_unreadable(self) -> None:
        p1, p2, p3, p4 = _tracker(["agent:human-only"], state=TaskState.HUMAN_ONLY)
        pa = ProjectAgents()
        with p1, p2 as create_adapter, p3, p4, ExitStack() as stack:
            create_adapter.return_value.get_state = AsyncMock(side_effect=RuntimeError("down"))
            mocks = [
                stack.enter_context(patch_)
                for patch_ in TestStartAgentSuitability()._patches(pa, AsyncMock(return_value=None))
            ]
            result = await start_agent(
                "7", force=True, enforce_suitability=True, override_suitability=True, _skip_handoff_clear=True
            )
        assert result["code"] == "suitability_role_unresolved"
        assert "explicit role" in result["error"]
        mocks[5].assert_not_called()  # no TaskRun
        mocks[6].assert_not_called()  # nothing spawned, so no command without --role

    async def test_start_agent_spawns_a_named_role_for_a_roleless_human_only_override(self) -> None:
        p1, p2, p3, p4 = _tracker(["agent:human-only"], state=TaskState.HUMAN_ONLY)
        pa = ProjectAgents()
        with (
            p1,
            p2,
            p3,
            p4,
            patch.object(agent_lifecycle, "_check_issue_conflict", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value={}),
            patch("sova.dashboard.services.agent_context.merge_mcp_env", return_value={}),
            patch.object(agent_lifecycle, "_finalize_orphaned_run", new_callable=AsyncMock),
            ExitStack() as stack,
        ):
            mocks = [
                stack.enter_context(patch_)
                for patch_ in TestStartAgentSuitability()._patches(pa, AsyncMock(return_value=None))
            ]
            mocks[6].side_effect = RuntimeError("no spawn in tests")
            await start_agent(
                "7", force=True, enforce_suitability=True, override_suitability=True, _skip_handoff_clear=True
            )
        cmd_parts = mocks[6].call_args.args[0]
        assert cmd_parts[cmd_parts.index("--role") + 1] == "developer"
        assert "--override-suitability" in cmd_parts


@pytest.fixture
async def client(dashboard_app) -> AsyncIterator[AsyncClient]:
    app = dashboard_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.headers.update(_csrf_request_headers(ac))
        yield ac


class TestRouters:
    async def test_agents_start_maps_the_block_to_a_structured_409(self, client: AsyncClient) -> None:
        start = AsyncMock(return_value=_BLOCKED)
        with patch("sova.dashboard.services.control_service.start_agent", start):
            resp = await client.post("/api/agents/start", json={"issue": "7", "role": "researcher"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == suitability_blocked_detail(_BLOCKED)
        assert start.call_args.kwargs["enforce_suitability"] is True
        assert start.call_args.kwargs["override_suitability"] is False

    async def test_agents_start_forwards_the_override(self, client: AsyncClient) -> None:
        start = AsyncMock(return_value={"status": "started", "run_id": 1})
        with patch("sova.dashboard.services.control_service.start_agent", start):
            resp = await client.post(
                "/api/agents/start", json={"issue": "7", "role": "researcher", "override_suitability": True}
            )
        assert resp.status_code == 200
        assert start.call_args.kwargs["override_suitability"] is True
        assert start.call_args.kwargs["role"] == "researcher"

    async def test_agents_start_enforces_on_a_resume_from_the_run_page(self, client: AsyncClient) -> None:
        # run_detail.html's resumeRun() sends exactly this body: force does not imply the override.
        start = AsyncMock(return_value=_BLOCKED)
        with patch("sova.dashboard.services.control_service.start_agent", start):
            resp = await client.post(
                "/api/agents/start", json={"issue": "7", "role": None, "force": True, "resume_run_id": 1}
            )
        assert resp.status_code == 409
        assert resp.json()["detail"] == suitability_blocked_detail(_BLOCKED)
        assert start.call_args.kwargs["enforce_suitability"] is True
        assert start.call_args.kwargs["override_suitability"] is False
        assert start.call_args.kwargs["resume_run_id"] == 1

    async def test_agents_start_budget_409_is_structured(self, client: AsyncClient) -> None:
        budget = {"error": "over", "code": "budget_exceeded", "total_cost_usd": "5", "max_issue_budget": "4"}
        with patch("sova.dashboard.services.control_service.start_agent", AsyncMock(return_value=budget)):
            resp = await client.post("/api/agents/start", json={"issue": "7"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == {
            "code": "budget_exceeded",
            "message": "over",
            "total_cost_usd": "5",
            "max_issue_budget": "4",
        }

    async def test_agents_start_without_issue_or_role_is_a_400(self, client: AsyncClient) -> None:
        start = AsyncMock()
        with patch("sova.dashboard.services.control_service.start_agent", start):
            resp = await client.post("/api/agents/start", json={})
        assert resp.status_code == 400
        start.assert_not_called()

    async def test_agents_start_other_409_keeps_its_shape(self, client: AsyncClient) -> None:
        start = AsyncMock(return_value={"error": "Issue #7 already has a running agent"})
        with patch("sova.dashboard.services.control_service.start_agent", start):
            resp = await client.post("/api/agents/start", json={"issue": "7"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Issue #7 already has a running agent"

    async def test_queue_start_maps_the_block_and_offers_no_override(self, client: AsyncClient) -> None:
        start = AsyncMock(return_value=_BLOCKED)
        with patch("sova.dashboard.routers.queue.start_agent", start):
            resp = await client.post("/api/queue/start/7", json={"role": None, "override_suitability": True})
        assert resp.status_code == 409
        assert resp.json()["detail"]["message"] == _BLOCKED["error"]
        assert start.call_args.kwargs["enforce_suitability"] is True
        assert "override_suitability" not in start.call_args.kwargs


class TestComplexityEndpointSuitability:
    @pytest.fixture(autouse=True)
    def _no_graph(self) -> Any:
        # The shared graph build would call the mocked adapter's list_tasks; no graph is the tracker-down case.
        with patch("sova.dashboard.routers.agents.load_classification_graph", new=AsyncMock(return_value=None)) as m:
            self.load_graph = m
            yield

    async def _get(self, client: AsyncClient, labels: list[str]) -> dict:
        p1, p2, p3, p4 = _tracker(labels, state=TaskState.TRIAGED)
        with p1, p2, p3, p4:
            resp = await client.get("/api/agents/issue/7/complexity")
        assert resp.status_code == 200
        return resp.json()

    async def test_epic_is_reported_for_the_modal_warning(self, client: AsyncClient) -> None:
        data = await self._get(client, ["type: epic"])
        assert data["suitability"] == {"mode": "epic", "reasons": [{"source": "label", "value": "type: epic"}]}

    async def test_interactive_is_reported_but_not_blocked(self, client: AsyncClient) -> None:
        data = await self._get(client, ["agent:interactive-recommended"])
        assert data["suitability"]["mode"] == "interactive"

    async def test_autonomous_reports_no_warning(self, client: AsyncClient) -> None:
        assert (await self._get(client, ["agent:ready"]))["suitability"] is None

    @pytest.mark.parametrize("role", ["triage", "reviewer"])
    async def test_exempt_role_gets_no_warning_and_no_classification(self, client: AsyncClient, role: str) -> None:
        p1, p2, p3, p4 = _tracker(["type: epic"], state=TaskState.TRIAGED)
        with p1, p2, p3, p4, patch("sova.dashboard.services.agent_validation.classify_issue_suitability") as classify:
            resp = await client.get(f"/api/agents/issue/7/complexity?role={role}")
        assert resp.status_code == 200
        assert resp.json()["suitability"] is None
        classify.assert_not_called()

    async def test_nickname_of_an_exempt_role_gets_no_warning(self, client: AsyncClient) -> None:
        p1, p2, p3, p4 = _tracker(["type: epic"], nicknames={"checker": "reviewer"})
        with p1, p2, p3, p4:
            resp = await client.get("/api/agents/issue/7/complexity?role=checker")
        assert resp.json()["suitability"] is None

    @pytest.mark.parametrize("state", [TaskState.BACKLOG, TaskState.IN_REVIEW])
    async def test_no_role_on_a_triaged_or_reviewed_state_gets_no_warning(
        self, client: AsyncClient, state: TaskState
    ) -> None:
        p1, p2, p3, p4 = _tracker(["type: epic"], state=state)
        with p1, p2, p3, p4:
            resp = await client.get("/api/agents/issue/7/complexity")
        assert resp.json()["suitability"] is None

    async def test_gated_role_still_gets_the_warning(self, client: AsyncClient) -> None:
        p1, p2, p3, p4 = _tracker(["type: epic"], state=TaskState.TRIAGED)
        with p1, p2, p3, p4:
            resp = await client.get("/api/agents/issue/7/complexity?role=researcher")
        assert resp.json()["suitability"]["mode"] == "epic"

    async def test_loaded_config_is_passed_through_to_the_classifier(self, client: AsyncClient) -> None:
        p1, p2, p3, p4 = _tracker(["type: epic"], state=TaskState.TRIAGED)
        with p1 as load, p2, p3, p4:
            await client.get("/api/agents/issue/7/complexity")
        # One load for the endpoint; the classifier reuses it instead of loading the config again.
        assert load.call_count == 1

    async def test_issue_is_fetched_once_when_there_is_no_stored_assessment(self, client: AsyncClient) -> None:
        p1, p2, p3, p4 = _tracker(["type: epic"], state=TaskState.TRIAGED)
        with p1, p2 as create_adapter, p3, p4:
            resp = await client.get("/api/agents/issue/7/complexity")
        assert resp.json()["suitability"]["mode"] == "epic"
        create_adapter.return_value.get_task.assert_awaited_once()

    async def test_open_dependency_is_reported_as_blocked_with_dependency_reasons(self, client: AsyncClient) -> None:
        graph = DependencyGraph(
            [
                Task(id="7", title="t", body="## Dependencies\n\n- #3\n", state=TaskState.TRIAGED),
                Task(id="3", title="dep", state=TaskState.IN_PROGRESS),
            ]
        )
        self.load_graph.return_value = graph
        data = await self._get(client, [])
        assert data["suitability"] == {"mode": "blocked", "reasons": [{"source": "dependency", "value": 3}]}

    async def test_missing_graph_falls_back_to_labels_not_to_graph_unavailable(self, client: AsyncClient) -> None:
        self.load_graph.return_value = None
        assert (await self._get(client, ["agent:ready"]))["suitability"] is None

    async def test_graph_lacking_the_issue_does_not_report_blocked(self, client: AsyncClient) -> None:
        self.load_graph.return_value = DependencyGraph([Task(id="99", title="other")])
        assert (await self._get(client, ["agent:ready"]))["suitability"] is None

    async def test_stored_assessment_waits_on_a_slow_tracker_only_for_the_display_timeout(
        self, client: AsyncClient
    ) -> None:
        # The complexity comes from the DB, so only the warning depends on the tracker, and it gives up.
        async def _stall(*_args: object, **_kwargs: object) -> Task:
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

        p1, p2, p3, p4 = _tracker(["type: epic"], state=TaskState.TRIAGED)
        loop = asyncio.get_running_loop()
        with (
            p1,
            p2,
            p3,
            p4,
            patch("sova.dashboard.routers.agents._SUITABILITY_DISPLAY_TIMEOUT_S", 0.05),
            patch("sova.dashboard.routers.agents._fetch_task_or_none", _stall),
            patch("sova.dashboard.routers.agents.get_session", new=AsyncMock(side_effect=lambda *_a, **_k: _stored())),
        ):
            started = loop.time()
            resp = await client.get("/api/agents/issue/7/complexity")
            elapsed = loop.time() - started
        assert resp.status_code == 200
        assert resp.json()["suitability"] is None
        # The stall is 10s; 3s absorbs first-request setup on a slow runner yet still catches a real stall.
        assert elapsed < 3


@asynccontextmanager
async def _stored() -> AsyncIterator[MagicMock]:
    """A session whose assessment lookup finds a stored complexity."""
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = "simple"
    session.execute = AsyncMock(return_value=result)
    yield session


class TestBatchRun:
    async def test_blocked_issue_is_a_failed_entry_and_the_rest_continue(self, tmp_path: Path) -> None:
        start = AsyncMock(side_effect=[_BLOCKED, {"status": "started"}])
        with patch("sova.dashboard.services.control_service.start_agent", start):
            result = await start_batch_run(["7", "8", "9"], tmp_path)
        assert result["started"] == "8"
        assert result["remaining"] == ["9"]
        assert result["failed"] == [{"issue": "7", "error": _BLOCKED["error"]}]
        assert start.call_args.kwargs == {"issue": "8", "enforce_suitability": True}

    async def test_ordinary_launch_error_is_failed_and_nothing_is_started(self, tmp_path: Path) -> None:
        budget = {"error": "Issue #8 is over budget", "code": "budget_exceeded"}
        start = AsyncMock(side_effect=[_BLOCKED, budget])
        with patch("sova.dashboard.services.control_service.start_agent", start):
            result = await start_batch_run(["7", "8", "9"], tmp_path)
        assert result["started"] is None
        assert result["remaining"] == ["9"]
        assert result["agent_result"] is budget
        assert result["failed"] == [
            {"issue": "7", "error": _BLOCKED["error"]},
            {"issue": "8", "error": "Issue #8 is over budget"},
        ]
        assert start.await_count == 2

    async def test_status_error_without_text_gets_a_fallback_message(self, tmp_path: Path) -> None:
        start = AsyncMock(return_value={"status": "error"})
        with patch("sova.dashboard.services.control_service.start_agent", start):
            result = await start_batch_run(["7"], tmp_path)
        assert result["started"] is None
        assert result["failed"] == [{"issue": "7", "error": "Agent start failed"}]

    async def test_every_issue_blocked_starts_nothing(self, tmp_path: Path) -> None:
        start = AsyncMock(return_value=_BLOCKED)
        with patch("sova.dashboard.services.control_service.start_agent", start):
            result = await start_batch_run(["7", "8"], tmp_path)
        assert result["started"] is None
        assert [f["issue"] for f in result["failed"]] == ["7", "8"]


class TestRunCli:
    async def _run(
        self, tmp_path: Path, dispatch: AsyncMock | None = None, role_name: str | None = "developer", **kwargs: object
    ) -> AsyncMock:
        from sova.cli.commands.run import _run_workflow
        from sova.config.models import ProjectConfig
        from sova.roles.base import RoleResult

        role = MagicMock()
        role.name = "developer"
        if dispatch is None:
            dispatch = AsyncMock(return_value=(role, RoleResult(success=True, summary="done")))
        with (
            patch("sova.config.loader.load_config", return_value=ProjectConfig()),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch("sova.db.session.init_db", new=AsyncMock()),
            patch("sova.utils.logging.setup_logging"),
            patch("sova.roles.dispatcher.dispatch", dispatch),
            patch("sova.cli.commands.run._install_sigterm_handler"),
            patch("sova.cli.commands.run._persist_run_cost", new=AsyncMock()),
        ):
            await _run_workflow("7", project_dir=tmp_path, role_name=role_name, force=False, **kwargs)  # type: ignore[arg-type]
        return dispatch

    async def test_typed_issue_that_is_blocked_exits_1_without_dispatch(self, tmp_path: Path) -> None:
        check = AsyncMock(return_value=_BLOCKED)
        dispatch = AsyncMock()
        with patch("sova.dashboard.services.agent_validation.check_issue_suitability", check):
            with pytest.raises(typer.Exit) as exc:
                await self._run(tmp_path, dispatch=dispatch)
        assert exc.value.exit_code == 1
        dispatch.assert_not_called()
        assert check.call_args.args[:2] == ("7", "developer")

    async def test_no_role_reaches_the_check_as_none_not_the_configured_default(self, tmp_path: Path) -> None:
        check = AsyncMock(return_value=None)
        with patch("sova.dashboard.services.agent_validation.check_issue_suitability", check):
            await self._run(tmp_path, role_name=None)
        assert check.call_args.args[:2] == ("7", None)

    async def test_roleless_override_dispatches_with_a_pinned_role(self, tmp_path: Path) -> None:
        pin = AsyncMock(return_value="developer")
        with patch("sova.dashboard.services.agent_validation.pin_override_role", pin):
            dispatch = await self._run(tmp_path, role_name=None, override_suitability=True)
        assert dispatch.call_args.kwargs["role_name"] == "developer"

    async def test_roleless_override_with_an_unreadable_state_exits_1_without_dispatch(self, tmp_path: Path) -> None:
        pin = AsyncMock(side_effect=RolePinUnavailable("Pick an explicit role and launch again."))
        dispatch = AsyncMock()
        with patch("sova.dashboard.services.agent_validation.pin_override_role", pin):
            with pytest.raises(typer.Exit) as exc:
                await self._run(tmp_path, dispatch=dispatch, role_name=None, override_suitability=True)
        assert exc.value.exit_code == 1
        dispatch.assert_not_called()

    async def test_override_flag_skips_the_check(self, tmp_path: Path) -> None:
        check = AsyncMock(return_value=_BLOCKED)
        with patch("sova.dashboard.services.agent_validation.check_issue_suitability", check):
            dispatch = await self._run(tmp_path, override_suitability=True)
        check.assert_not_called()
        dispatch.assert_awaited_once()

    async def test_override_flag_reaches_the_execution_context(self, tmp_path: Path) -> None:
        dispatch = await self._run(tmp_path, override_suitability=True)
        assert dispatch.call_args.args[0].override_suitability is True

    async def test_unloadable_checkpoint_exits_1_without_dispatch(self, tmp_path: Path) -> None:
        with (
            patch("sova.cli.commands.run._load_checkpoint", new=AsyncMock(return_value={"error": "no such run"})),
            pytest.raises(typer.Exit) as exc,
        ):
            await self._run(tmp_path, resume_run_id=3)
        assert exc.value.exit_code == 1

    async def test_issueless_run_is_never_checked(self, tmp_path: Path) -> None:
        from sova.cli.commands.run import _refuse_unsuitable_issue

        check = AsyncMock(return_value=_BLOCKED)
        with patch("sova.dashboard.services.agent_validation.check_issue_suitability", check):
            await _refuse_unsuitable_issue("", "planner", tmp_path)
        check.assert_not_called()

    @pytest.mark.parametrize("kwargs", [{"task_run_id": 3}, {"resume_run_id": 3}])
    async def test_dashboard_spawned_and_resumed_runs_are_not_rechecked(
        self, tmp_path: Path, kwargs: dict[str, int]
    ) -> None:
        check = AsyncMock(return_value=_BLOCKED)
        with (
            patch("sova.dashboard.services.agent_validation.check_issue_suitability", check),
            patch("sova.cli.commands.run._load_checkpoint", new=AsyncMock(return_value={})),
        ):
            await self._run(tmp_path, **kwargs)
        check.assert_not_called()


class TestDeveloperStateGateOverride:
    """`override_suitability` lets the developer gate accept a human-only issue, and nothing else."""

    def _role(self) -> Any:
        from sova.roles.developer import DeveloperRole

        return DeveloperRole()

    def test_human_only_is_accepted_with_the_override(self) -> None:
        task = Task(id="7", title="t", state=TaskState.HUMAN_ONLY)
        assert self._role().validate_preconditions(task, override_suitability=True) is True

    def test_human_only_is_refused_without_the_override(self) -> None:
        task = Task(id="7", title="t", state=TaskState.HUMAN_ONLY)
        assert self._role().validate_preconditions(task) is False

    def test_other_unready_states_are_still_refused_with_the_override(self) -> None:
        task = Task(id="7", title="t", state=TaskState.BACKLOG)
        assert self._role().validate_preconditions(task, override_suitability=True) is False

    async def test_execute_passes_the_override_from_the_context(self) -> None:
        from sova.core.context import ExecutionContext

        adapter = MagicMock()
        adapter.get_task = AsyncMock(return_value=Task(id="7", title="t", state=TaskState.BACKLOG))
        ctx = ExecutionContext(
            project_dir=Path("/tmp"), config=MagicMock(), adapter=adapter, issue_number="7", override_suitability=True
        )
        role = self._role()
        with patch.object(role, "validate_preconditions", return_value=False) as gate:
            result = await role.execute(ctx)
        assert result.success is False
        gate.assert_called_once_with(adapter.get_task.return_value, force=False, override_suitability=True)


class TestFetchTaskStateGateOverride:
    """`FetchTaskStep` (the researcher pipeline's state gate) honours `override_suitability` like the developer."""

    async def _fetch(self, state: TaskState, **ctx_kwargs: Any) -> Any:
        from sova.core.context import ExecutionContext
        from sova.core.steps.fetch_task import FetchTaskStep

        adapter = MagicMock()
        adapter.get_task = AsyncMock(return_value=Task(id="7", title="t", state=state))
        ctx = ExecutionContext(
            project_dir=Path("/tmp"),
            config=MagicMock(),
            adapter=adapter,
            issue_number="7",
            allowed_input_states=frozenset({TaskState.TRIAGED}),
            **ctx_kwargs,
        )
        return await FetchTaskStep().execute(ctx)

    async def test_human_only_passes_with_the_override(self) -> None:
        result = await self._fetch(TaskState.HUMAN_ONLY, override_suitability=True)
        assert result.success is True

    async def test_human_only_is_refused_without_the_override(self) -> None:
        result = await self._fetch(TaskState.HUMAN_ONLY)
        assert result.success is False

    async def test_backlog_is_still_refused_with_the_override(self) -> None:
        result = await self._fetch(TaskState.BACKLOG, override_suitability=True)
        assert result.success is False
        assert "Precondition failed" in (result.error or "")

    async def test_force_still_bypasses_the_gate(self) -> None:
        assert (await self._fetch(TaskState.BACKLOG, force=True)).success is True


class TestCustomRoleStateGateOverride:
    async def test_execute_passes_the_override_from_the_context(self) -> None:
        from sova.core.context import ExecutionContext
        from sova.roles.custom import CustomRole

        adapter = MagicMock()
        adapter.get_task = AsyncMock(return_value=Task(id="7", title="t", state=TaskState.HUMAN_ONLY))
        ctx = ExecutionContext(
            project_dir=Path("/tmp"), config=MagicMock(), adapter=adapter, issue_number="7", override_suitability=True
        )
        role = CustomRole.__new__(CustomRole)
        with patch.object(role, "validate_preconditions", return_value=False) as gate:
            result = await role.execute(ctx)
        assert result.success is False
        gate.assert_called_once_with(adapter.get_task.return_value, force=False, override_suitability=True)


# Callers that launch on an issue nobody picked in the launch modal. They call start_agent() without
# enforce_suitability, so an epic or human-only issue launches exactly as before; the issue keeps these
# autonomous and resume paths out of scope.
_NON_ENFORCING_CALLERS = ("handoff_button", "spec_approve", "spec_revise", "spec_skip")


def _assert_not_enforced(start: AsyncMock) -> None:
    start.assert_awaited()
    kwargs = start.call_args.kwargs
    assert kwargs.get("enforce_suitability", False) is False
    assert kwargs.get("override_suitability", False) is False


class TestHumanOnlyOverrideConfirmText:
    """The override replaces the human-only label (the run moves the issue on), so both confirms must say so."""

    @pytest.mark.parametrize(
        "path",
        ["sova/dashboard/static/js/pages/agents/launch-modal.js", "sova/dashboard/templates/supervisor.html"],
    )
    def test_confirm_warns_that_the_human_only_label_is_replaced(self, path: str) -> None:
        text = (Path(__file__).resolve().parent.parent / path).read_text(encoding="utf-8")
        assert "agent:human-only label is replaced once the run moves the issue on" in text
        assert "mode === 'human'" in text


class TestNonEnforcingCallers:
    @pytest.mark.parametrize("caller", _NON_ENFORCING_CALLERS)
    async def test_router_callers_do_not_enforce(self, client: AsyncClient, caller: str) -> None:
        from sova.dashboard.services import control_service as cs

        start = AsyncMock(return_value={"status": "started", "run_id": 1})
        handoff = {
            "issue": "7",
            "source": "developer",
            "next_actions": [{"id": "go", "mode": "agent", "args": {"issue": "7", "role": "developer"}}],
        }
        paths = {
            "handoff_button": ("post", "/api/handoff/execute", {"action_id": "go"}),
            "spec_approve": ("post", "/api/spec/7/approve", None),
            "spec_revise": ("post", "/api/spec/7/revise", None),
            "spec_skip": ("post", "/api/spec/7/skip", None),
        }
        method, url, body = paths[caller]
        with (
            patch.object(cs, "start_agent", start),
            patch.object(cs, "complete_awaiting_approval_by_issue", new=AsyncMock(return_value=None)),
            patch("sova.dashboard.routers.handoff.handoff_service.get_all_handoffs", return_value=[handoff]),
            patch("sova.dashboard.routers.handoff.handoff_service.clear_handoff"),
            patch("sova.dashboard.routers.spec.handoff_service.clear_handoff"),
            patch("sova.dashboard.routers.spec.spec_service.approve_spec", return_value={"status": "approved"}),
            patch("sova.dashboard.routers.spec._transition_to_researched", new=AsyncMock(return_value=True)),
        ):
            resp = await getattr(client, method)(url, json=body) if body else await getattr(client, method)(url)
        assert resp.status_code == 200, resp.text
        _assert_not_enforced(start)

    async def test_auto_handoff_does_not_enforce(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction, write_handoff_file

        agent = type("AgentState", (), {"run_id": 1, "issue": "7", "project_dir": tmp_path})()
        write_handoff_file(
            tmp_path,
            DashboardHandoff(
                source="researcher",
                status="awaiting_action",
                issue="7",
                summary="s",
                next_actions=[
                    HandoffAction(
                        id="develop",
                        label="Develop",
                        auto_execute=True,
                        mode="agent",
                        args={"issue": "7", "role": "developer"},
                    )
                ],
            ),
        )
        start = AsyncMock(return_value={"run_id": 2})
        with (
            patch("sova.dashboard.services.agent_lifecycle.start_agent", start),
            patch("sova.dashboard.services.agent_handoff.check_memory_pressure", return_value=(None, None)),
        ):
            await _process_auto_handoff(agent)
        _assert_not_enforced(start)

    async def test_resume_from_approval_does_not_enforce(self) -> None:
        from sova.dashboard.services import agent_approval

        run = {"issue_number": "7", "role": "researcher", "pr_number": None}
        start = AsyncMock(return_value={"run_id": 2})
        with (
            patch.object(agent_approval, "_claim_awaiting_approval", new=AsyncMock(return_value=(run, None))),
            patch.object(agent_approval, "_clear_handoff_for_issue"),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", start),
        ):
            result = await agent_approval.resume_from_approval(5)
        assert result["resumed_from"] == 5
        _assert_not_enforced(start)

    async def test_self_heal_resume_does_not_enforce(self) -> None:
        from sova.dashboard.services import agent_recovery

        candidate = {
            "issue": "7",
            "role": "developer",
            "run_id": 3,
            "pr_number": None,
            "steps_done": 2,
        }
        start = AsyncMock(return_value={"run_id": 4})
        with (
            patch("sova.dashboard.services.agent_lifecycle.start_agent", start),
            patch("sova.config.registry.find_slug_for_path", return_value=None),
            patch.object(agent_recovery, "_emit_self_heal_event"),
        ):
            resumed = await agent_recovery._resume_one(candidate, None)
        assert resumed[0]["new_run_id"] == 4
        _assert_not_enforced(start)

    async def test_progression_dispatch_does_not_enforce(self) -> None:
        from sova.config.models import ProjectConfig
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision, TaskProgressionEngine

        engine = TaskProgressionEngine(
            config=ProjectConfig(), adapter=AsyncMock(), project_dir=None, session_factory=MagicMock()
        )
        decision = ProgressionDecision(issue_number=7, action=ProgressionAction.SPAWN_DEVELOPER, role="developer")
        start = AsyncMock(return_value={"run_id": 9})
        with patch("sova.dashboard.services.agent_lifecycle.start_agent", start):
            await engine.execute_decision(decision)
        _assert_not_enforced(start)
