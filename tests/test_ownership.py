"""PR and issue ownership (#1594): the shared check, the start paths, auto-handoff, the resolver and gh login.

``tests/conftest.py:_no_real_ownership_lookups`` stubs every GitHub lookup these checks make; a test here
patches the one it needs back to an answer of its own.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from sova.config.models import ProjectConfig, SupervisorConfig
from sova.core.ownership import Ownership, check_ownership, issue_owned_by_other, pr_owned_by_other
from sova.dashboard.services.agent_validation import GH_LOGIN_MISMATCH, OWNED_BY_OTHER, check_write_ownership
from sova.dashboard.services.work_state import PRFacts, WorkItemState, resolve_next_action
from sova.ipc.handoff import DashboardHandoff
from sova.supervisor.gates.ownership import check_ownership_gate
from sova.utils.gh import get_acting_gh_login as _real_get_acting_gh_login  # before conftest stubs it
from sova.utils.shell import ShellResult

_AUTHOR = "sova.core.ownership.get_pr_author"
_CONFIG = "sova.dashboard.services.agent_validation.load_config"


def _config(*, respect_ownership: bool = True) -> ProjectConfig:
    return ProjectConfig(
        github_user="alice",
        github_repo="owner/repo",
        supervisor=SupervisorConfig(respect_ownership=respect_ownership),
    )


@pytest.mark.parametrize(
    ("author", "user", "expected"),
    [
        ("bob", "alice", True),
        ("Alice", "alice", False),  # a login differing only by case is the same owner
        ("dependabot[bot]", "alice", True),  # a bot is another login
        ("ghost", "alice", True),  # a deleted account is another login
        ("", "alice", False),  # no author is unknown, never another login
        ("bob", "", False),  # no configured user: the check is off
    ],
)
def test_pr_owned_by_other(author: str, user: str, expected: bool) -> None:
    assert pr_owned_by_other(author, user) is expected


@pytest.mark.parametrize(
    ("assignees", "expected"),
    [(["bob", "ALICE"], False), (["bob", "carol"], True), ([], False)],
)
def test_issue_owned_by_other(assignees: list[str], expected: bool) -> None:
    assert issue_owned_by_other(assignees, "alice") is expected


class TestCheckOwnership:
    async def test_pr_author_names_the_owner(self) -> None:
        with patch(_AUTHOR, AsyncMock(return_value="bob")):
            result = await check_ownership("7", github_user="alice", check_pr=True, repo="o/r", pr_number=12)
        assert result.ownership is Ownership.OTHER
        assert result.detail == "PR #12 is owned by bob (not alice)"

    async def test_lookup_failure_fails_open_and_logs(self) -> None:
        with patch(_AUTHOR, AsyncMock(side_effect=RuntimeError("gh down"))), capture_logs() as logs:
            result = await check_ownership("7", github_user="alice", check_pr=True, repo="o/r", pr_number=12)
        assert result.ownership is Ownership.UNKNOWN
        assert not result.refused
        failures = [entry for entry in logs if entry["event"] == "ownership.check_failed"]
        assert failures
        assert failures[0].get("exc_info")

    async def test_jira_never_compares_the_assignee(self) -> None:
        result = await check_ownership(
            "7", github_user="alice", check_pr=False, task_assignees=["Bob Smith"], compare_assignees=False
        )
        assert result.ownership is Ownership.UNKNOWN


class TestSharedSwitch:
    """The supervisor gate and the dashboard start paths share check_ownership() and its respect_ownership switch."""

    @pytest.mark.parametrize("respect", [True, False])
    async def test_gate_and_dashboard_agree(self, respect: bool, tmp_path: Path) -> None:
        adapter = MagicMock()
        with (
            patch(_AUTHOR, AsyncMock(return_value="bob")),
            patch(_CONFIG, return_value=_config(respect_ownership=respect)),
        ):
            # The supervisor only knows the issue: its PR is found by the issue lookup.
            with patch(
                "sova.core.ownership.find_pr_for_issue",
                AsyncMock(return_value=MagicMock(number=12, author_login="bob")),
            ):
                block, _ = await check_ownership_gate(
                    7, True, respect_ownership=respect, github_user="alice", repo="owner/repo", adapter=adapter
                )
            refusal = await check_write_ownership("7", tmp_path, pr_number=12)
        assert (block is not None) is respect
        assert (refusal is not None) is respect
        if respect:
            assert block.detail == refusal["error"] == "PR #12 is owned by bob (not alice)"


class TestJiraOwnership:
    """On Jira only the forge PR author is compared: the gate and the dashboard apply one rule (#1594)."""

    @staticmethod
    def _jira_config() -> ProjectConfig:
        return ProjectConfig(
            github_user="alice",
            github_repo="owner/repo",
            task_source={"type": "jira", "jira_project_key": "PROJ", "jira_base_url": "https://jira.example"},
        )

    @pytest.mark.parametrize("author", ["", "alice"])
    async def test_gate_allows_an_assigned_jira_issue_like_the_dashboard(self, author: str, tmp_path: Path) -> None:
        from sova.core.ownership import assignees_are_logins

        cfg = self._jira_config()
        compare = assignees_are_logins(cfg.task_source.type)
        assert compare is False
        pr = MagicMock(number=12, author_login=author)
        with (
            patch("sova.core.ownership.find_pr_for_issue", AsyncMock(return_value=pr)),
            patch(_AUTHOR, AsyncMock(return_value=author)),
            patch(_CONFIG, return_value=cfg),
        ):
            for is_integrate in (False, True):
                block, _ = await check_ownership_gate(
                    7,
                    is_integrate,
                    respect_ownership=True,
                    github_user="alice",
                    repo="owner/repo",
                    adapter=MagicMock(),
                    task_assignees=["Bob Smith"],
                    compare_assignees=compare,
                )
                assert block is None
            assert await check_write_ownership("7", tmp_path, task=MagicMock(assignees=["Bob Smith"])) is None


class TestGhLoginMismatch:
    async def test_refuses_a_write_naming_both_logins(self, tmp_path: Path) -> None:
        with (
            patch(_CONFIG, return_value=_config()),
            patch("sova.utils.gh.get_acting_gh_login", AsyncMock(return_value="carol")),
        ):
            refusal = await check_write_ownership("7", tmp_path, pr_number=12)
        assert refusal is not None
        assert refusal["code"] == GH_LOGIN_MISMATCH
        assert "carol" in refusal["error"]
        assert "alice" in refusal["error"]

    async def test_applies_to_a_start_with_no_issue_and_no_pr(self, tmp_path: Path) -> None:
        with (
            patch(_CONFIG, return_value=_config()),
            patch(_AUTHOR, AsyncMock(side_effect=AssertionError("no PR to read"))),
            patch("sova.utils.gh.get_acting_gh_login", AsyncMock(return_value="carol")),
        ):
            refusal = await check_write_ownership("", tmp_path)
        assert refusal is not None
        assert refusal["code"] == GH_LOGIN_MISMATCH
        assert "carol" in refusal["error"]

    async def test_start_command_with_no_issue_or_pr_is_refused_on_a_mismatch(self, tmp_path: Path) -> None:
        from sova.dashboard.services import agent_lifecycle

        create_run = AsyncMock()
        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=_pool(tmp_path)),
            patch.object(agent_lifecycle, "_command_request_rejection", return_value=None),
            patch.object(agent_lifecycle, "_resolve_command_context", AsyncMock(return_value=(None, ""))),
            patch.object(agent_lifecycle, "_create_task_run", create_run),
            patch(_CONFIG, return_value=_config()),
            patch("sova.utils.gh.get_acting_gh_login", AsyncMock(return_value="carol")),
        ):
            result = await agent_lifecycle.start_command("health-audit", {})
        assert result["code"] == GH_LOGIN_MISMATCH
        create_run.assert_not_awaited()

    async def test_is_independent_of_respect_ownership(self, tmp_path: Path) -> None:
        with (
            patch(_CONFIG, return_value=_config(respect_ownership=False)),
            patch("sova.utils.gh.get_acting_gh_login", AsyncMock(return_value="carol")),
        ):
            refusal = await check_write_ownership("7", tmp_path, pr_number=12)
        assert refusal is not None
        assert refusal["code"] == GH_LOGIN_MISMATCH

    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (ShellResult(returncode=0, stdout="Carol\n", stderr=""), "Carol"),
            (ShellResult(returncode=1, stdout="", stderr="not logged in"), None),
            (ShellResult(returncode=-1, stdout="", stderr="timed out", timed_out=True), None),
            (FileNotFoundError("gh"), None),  # gh not installed
        ],
    )
    async def test_acting_login_is_unknown_when_gh_cannot_answer(
        self, result: ShellResult | Exception, expected: str | None
    ) -> None:
        from sova.utils import gh

        gh._gh_login_cache.clear()
        run = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
        with patch.object(gh, "resolve_gh_env", AsyncMock(return_value=None)), patch.object(gh, "run", run):
            assert await _real_get_acting_gh_login("alice") == expected
        gh._gh_login_cache.clear()

    async def test_doctor_reports_the_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from sova.utils.diagnostics import _check_gh_login

        monkeypatch.setenv("SOVA_GITHUB_USER", "alice")
        with patch("sova.utils.gh.get_acting_gh_login", AsyncMock(return_value="carol")):
            [row] = await _check_gh_login(tmp_path)
        assert not row.passed
        assert "carol" in row.detail
        assert "alice" in row.detail


def _pool(tmp_path: Path) -> MagicMock:
    pa = MagicMock()
    pa.project_dir = tmp_path
    pa.agents = {}
    return pa


class TestStartPathsRefuse:
    async def test_start_agent_refuses_another_logins_pr_before_any_run(self, tmp_path: Path) -> None:
        from sova.dashboard.services import agent_lifecycle

        create_run = AsyncMock()
        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=_pool(tmp_path)),
            patch.object(agent_lifecycle, "_create_task_run", create_run),
            patch(_CONFIG, return_value=_config()),
            patch(_AUTHOR, AsyncMock(return_value="bob")),
        ):
            # force skips only the duplicate check, never ownership.
            result = await agent_lifecycle.start_agent("7", role="reviewer", pr_number=12, force=True)
        assert result["code"] == OWNED_BY_OTHER
        assert "bob" in result["error"]
        create_run.assert_not_awaited()

    async def test_start_command_refuses_another_logins_pr_before_any_run(self, tmp_path: Path) -> None:
        from sova.dashboard.services import agent_lifecycle

        create_run = AsyncMock()
        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=_pool(tmp_path)),
            patch.object(agent_lifecycle, "_command_request_rejection", return_value=None),
            patch.object(agent_lifecycle, "_resolve_command_context", AsyncMock(return_value=(12, "7"))),
            patch.object(agent_lifecycle, "_create_task_run", create_run),
            patch(_CONFIG, return_value=_config()),
            patch(_AUTHOR, AsyncMock(return_value="bob")),
        ):
            result = await agent_lifecycle.start_command("integrate-pr", {"pr": 12})
        assert result["code"] == OWNED_BY_OTHER
        assert "bob" in result["error"]
        create_run.assert_not_awaited()

    async def test_start_command_on_an_authorless_pr_falls_back_to_the_assignees(self, tmp_path: Path) -> None:
        """The same fallback the board's _item_owner() applies, so a direct start refuses what the board hides."""
        from sova.adapters.base import Task
        from sova.dashboard.services import agent_lifecycle

        adapter = MagicMock()
        adapter.get_task = AsyncMock(return_value=Task(id="7", title="t", assignees=["bob"]))
        create_run = AsyncMock()
        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=_pool(tmp_path)),
            patch.object(agent_lifecycle, "_command_request_rejection", return_value=None),
            patch.object(agent_lifecycle, "_resolve_command_context", AsyncMock(return_value=(12, "7"))),
            patch.object(agent_lifecycle, "_create_task_run", create_run),
            patch(_CONFIG, return_value=_config()),
            patch(_AUTHOR, AsyncMock(return_value="")),
            patch("sova.adapters.create_adapter", return_value=adapter),
        ):
            result = await agent_lifecycle.start_command("integrate-pr", {"pr": 12})
        assert result["code"] == OWNED_BY_OTHER
        assert "assigned to bob" in result["error"]
        create_run.assert_not_awaited()

    async def test_develop_start_checks_the_issue_assignee(self, tmp_path: Path) -> None:
        task = MagicMock(assignees=["bob"])
        with patch(_CONFIG, return_value=_config()):
            refusal = await check_write_ownership("7", tmp_path, task=task)
        assert refusal is not None
        assert "assigned to bob" in refusal["error"]


@pytest.mark.usefixtures("reset_db_rows")
async def test_auto_handoff_records_the_ownership_refusal_on_the_finished_run(tmp_path: Path) -> None:
    from sova.dashboard.services import agent_handoff
    from sova.db.models import TaskRun
    from sova.db.session import get_session
    from sova.ipc.handoff import HandoffAction, write_handoff_file

    async with await get_session() as session:
        async with session.begin():
            run = TaskRun(issue_number="7", role="developer", status="done", pr_number=12)
            session.add(run)
            await session.flush()
            run_id = run.id
    write_handoff_file(
        tmp_path,
        DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="7",
            pr_number=12,
            summary="ready for review",
            next_actions=[
                HandoffAction(id="review", label="Review", auto_execute=True, mode="agent", args={"role": "reviewer"})
            ],
        ),
    )
    agent = type("AgentState", (), {"run_id": run_id, "issue": "7", "project_dir": tmp_path, "pr_number": 12})()
    refusal = {"error": "PR #12 is owned by bob (not alice)", "code": OWNED_BY_OTHER, "owner": "bob"}

    async def _shared_db(**_kw: object) -> object:
        return await get_session()

    with (
        patch.object(agent_handoff, "check_memory_pressure", return_value=(None, None)),
        patch.object(agent_handoff, "_review_already_done", AsyncMock(return_value=False)),
        patch.object(agent_handoff, "_address_review_budget_blocked", AsyncMock(return_value=False)),
        patch.object(agent_handoff, "_start_follow_up", AsyncMock(return_value=refusal)),
        patch.object(agent_handoff, "get_session", side_effect=_shared_db),
    ):
        await agent_handoff._process_auto_handoff(agent)

    async with await get_session() as session:
        refreshed = await session.get(TaskRun, run_id)
        assert refreshed is not None
        recorded = (refreshed.handoff_json or {})[agent_handoff.AUTO_HANDOFF_REFUSED_KEY]
    assert recorded["owner"] == "bob"
    assert recorded["detail"] == refusal["error"]


def _facts(**overrides: object) -> PRFacts:
    values: dict = {
        "running_agent": False,
        "pr_state": "OPEN",
        "is_draft": False,
        "mergeable": "MERGEABLE",
        "merge_state": "CLEAN",
        "ci_status": "passed",
        "head_sha": "sha",
        "sova_verdict": "approve",
        "sova_verdict_sha": "sha",
        "sova_verdict_addressed": False,
        "external_changes_requested": False,
        "thread_signal": "clear",
        "external_reviews_enabled": True,
        "address_cycles": 0,
        "max_address_cycles": 0,
    }
    values.update(overrides)
    return PRFacts(**values)


class TestResolverOwnership:
    @pytest.mark.parametrize("ci_status", ["passed", "failed"])  # Integrate and Address are both withheld
    def test_another_logins_pr_has_no_write_action(self, ci_status: str) -> None:
        resolution = resolve_next_action(_facts(pr_author="bob", github_user="alice", ci_status=ci_status))
        assert resolution.state is WorkItemState.PR_OWNED_BY_OTHER
        assert resolution.action_id is None

    @pytest.mark.parametrize(
        "signals",
        [
            {"external_changes_requested": True},  # Address
            {"thread_signal": "pending"},  # Address
            {"sova_verdict": "revise"},  # Address
            {"sova_verdict": None, "sova_verdict_sha": None},  # Review
            {"mergeable": "CONFLICTING"},  # Rebase
        ],
    )
    def test_every_write_signal_is_withheld_on_another_logins_pr(self, signals: dict) -> None:
        own = resolve_next_action(_facts(pr_author="alice", github_user="alice", **signals))
        assert own.action_id is not None  # the same signals offer a write on an own PR
        resolution = resolve_next_action(_facts(pr_author="bob", github_user="alice", **signals))
        assert resolution.state is WorkItemState.PR_OWNED_BY_OTHER
        assert resolution.action_id is None

    def test_a_conflicting_pr_owned_by_another_login_offers_no_rebase(self) -> None:
        resolution = resolve_next_action(_facts(mergeable="CONFLICTING", pr_author="bob", github_user="alice"))
        assert resolution.state is WorkItemState.PR_OWNED_BY_OTHER
        assert resolution.action_id is None
        assert resolution.reason_chain[-1] == "owned_by_other"

    def test_a_login_differing_only_by_case_is_the_same_owner(self) -> None:
        resolution = resolve_next_action(_facts(mergeable="CONFLICTING", pr_author="Alice", github_user="alice"))
        assert resolution.state is not WorkItemState.PR_OWNED_BY_OTHER

    @pytest.mark.parametrize(
        ("overrides", "state"),
        [({"pr_state": "MERGED"}, WorkItemState.MERGED), ({"running_agent": True}, WorkItemState.AGENT_RUNNING)],
    )
    def test_merged_or_running_wins_over_another_logins_author(self, overrides: dict, state: WorkItemState) -> None:
        resolution = resolve_next_action(_facts(pr_author="bob", github_user="alice", **overrides))
        assert resolution.state is state

    @pytest.mark.parametrize(("author", "user"), [("", "alice"), ("bob", ""), ("ALICE", "alice")])
    def test_unknown_or_own_pr_routes_as_before(self, author: str, user: str) -> None:
        resolution = resolve_next_action(_facts(pr_author=author, github_user=user))
        assert resolution.state is WorkItemState.PR_READY_TO_MERGE
        assert resolution.action_id == "integrate"

    def test_work_item_label_names_the_owner_and_offers_no_buttons(self) -> None:
        from sova.dashboard.services.work_item_service import _build_pr_item

        pr = {"number": 12, "state": "OPEN", "mergeable": "MERGEABLE", "ci_status": "passed", "author": "bob"}
        item = _build_pr_item(pr, None, None, "7", github_user="alice")
        assert item["state"] == WorkItemState.PR_OWNED_BY_OTHER.value
        assert item["state_label"] == "Owned by bob"
        assert item["owner"] == "bob"
        assert item["primary_action"] is None
        assert item["secondary_actions"] == []

    def test_board_entry_of_another_logins_issue_has_no_start(self) -> None:
        from sova.dashboard.services.work_board_service import _item_entry
        from sova.dashboard.services.work_item_service import _build_task_item
        from sova.dashboard.services.work_state import WorkStage

        task = {"issue": "7", "state": "researched", "assignees": ["bob"], "title": "t"}
        item = _build_task_item(task, None, None, None, github_user="alice")
        entry = _item_entry(item, WorkStage.NEXT)
        assert entry["owner"] == "bob"
        assert entry["start"] is None
        assert entry["actions"] == []


class TestReviewFollowUps:
    """Paths the first round of tests left out: timeouts, adapter lookups, routed roles and real follow-up starts."""

    async def test_slow_author_lookup_times_out_open_and_logs(self, tmp_path: Path) -> None:
        import asyncio

        async def _slow(*_args: object, **_kwargs: object) -> str:
            await asyncio.sleep(5)
            return "bob"

        with (
            patch(_CONFIG, return_value=_config()),
            patch(_AUTHOR, _slow),
            patch("sova.dashboard.services.agent_validation._OWNERSHIP_CHECK_TIMEOUT_S", 0.01),
            patch("sova.utils.gh.get_acting_gh_login", AsyncMock(return_value="alice")),
            capture_logs() as logs,
        ):
            refusal = await check_write_ownership("7", tmp_path, pr_number=12)
        assert refusal is None
        failures = [entry for entry in logs if entry["event"] == "write_ownership.check_timed_out"]
        assert failures
        assert failures[0].get("exc_info")

    async def test_issue_only_start_reads_the_assignees_through_the_adapter(self, tmp_path: Path) -> None:
        from sova.adapters.base import Task

        adapter = MagicMock()
        adapter.get_task = AsyncMock(return_value=Task(id="7", title="t", assignees=["bob"]))
        with patch(_CONFIG, return_value=_config()), patch("sova.adapters.create_adapter", return_value=adapter):
            refusal = await check_write_ownership("7", tmp_path)
        assert refusal is not None
        assert refusal["code"] == OWNED_BY_OTHER
        assert "assigned to bob" in refusal["error"]
        adapter.get_task.assert_awaited_once_with("7")

    async def test_roleless_start_routed_to_the_reviewer_checks_the_pr_author(self, tmp_path: Path) -> None:
        """An unassigned in-review issue still has a PR: dispatch() sends it to the reviewer, so its author decides."""
        from sova.adapters.base import Task, TaskState
        from sova.dashboard.services import agent_lifecycle
        from sova.git.pr import PRInfo

        launch_task = Task(id="7", title="t", state=TaskState.IN_REVIEW, assignees=[])
        create_run = AsyncMock()
        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=_pool(tmp_path)),
            patch.object(agent_lifecycle, "_recover_last_pr_number", AsyncMock(return_value=None)),
            patch.object(agent_lifecycle, "fetch_launch_task", AsyncMock(return_value=launch_task)),
            patch.object(agent_lifecycle, "_create_task_run", create_run),
            patch(_CONFIG, return_value=_config()),
            patch(
                "sova.core.ownership.find_pr_for_issue",
                AsyncMock(return_value=PRInfo(number=12, url="", author_login="bob")),
            ),
        ):
            result = await agent_lifecycle.start_agent("7")
        assert result["code"] == OWNED_BY_OTHER
        assert "PR #12 is owned by bob" in result["error"]
        create_run.assert_not_awaited()

    async def test_handoff_execute_reports_an_ownership_refusal_as_a_conflict(self) -> None:
        from sova.dashboard.services import handoff_service

        refusal = {"error": "PR #12 is owned by bob (not alice)", "code": OWNED_BY_OTHER, "owner": "bob"}
        outcome = handoff_service._failure_outcome(refusal)
        assert outcome.outcome is handoff_service.ActionOutcome.REFUSED
        assert outcome.detail == refusal["error"]
        mismatch = handoff_service._failure_outcome({"error": "gh is logged in as carol", "code": GH_LOGIN_MISMATCH})
        assert mismatch.outcome is handoff_service.ActionOutcome.REFUSED


@pytest.mark.usefixtures("reset_db_rows")
async def test_auto_handoff_follow_up_start_reaches_the_ownership_check(tmp_path: Path) -> None:
    """The real _start_follow_up() -> start_agent() path, patched only at the PR author lookup."""
    from sova.dashboard.services import agent_handoff, agent_lifecycle
    from sova.db.models import TaskRun
    from sova.db.session import get_session
    from sova.ipc.handoff import HandoffAction, write_handoff_file

    async with await get_session() as session:
        async with session.begin():
            run = TaskRun(issue_number="7", role="developer", status="done", pr_number=12)
            session.add(run)
            await session.flush()
            run_id = run.id
    write_handoff_file(
        tmp_path,
        DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="7",
            pr_number=12,
            summary="ready for review",
            next_actions=[
                HandoffAction(id="review", label="Review", auto_execute=True, mode="agent", args={"role": "reviewer"})
            ],
        ),
    )
    agent = type("AgentState", (), {"run_id": run_id, "issue": "7", "project_dir": tmp_path, "pr_number": 12})()
    create_run = AsyncMock()

    async def _shared_db(**_kw: object) -> object:
        return await get_session()

    with (
        patch.object(agent_handoff, "check_memory_pressure", return_value=(None, None)),
        patch.object(agent_handoff, "_review_already_done", AsyncMock(return_value=False)),
        patch.object(agent_handoff, "_address_review_budget_blocked", AsyncMock(return_value=False)),
        patch.object(agent_handoff, "get_session", side_effect=_shared_db),
        patch.object(agent_lifecycle, "_get_project_agents", return_value=_pool(tmp_path)),
        patch.object(agent_lifecycle, "_create_task_run", create_run),
        patch(_CONFIG, return_value=_config()),
        patch(_AUTHOR, AsyncMock(return_value="bob")),
    ):
        await agent_handoff._process_auto_handoff(agent)

    create_run.assert_not_awaited()
    async with await get_session() as session:
        refreshed = await session.get(TaskRun, run_id)
        assert refreshed is not None
        recorded = (refreshed.handoff_json or {})[agent_handoff.AUTO_HANDOFF_REFUSED_KEY]
    assert recorded["owner"] == "bob"
    assert recorded["detail"] == "PR #12 is owned by bob (not alice)"


class TestActingLoginReadFailures:
    async def test_credential_store_failure_fails_open_and_logs(self) -> None:
        from sova.utils import gh

        gh._gh_login_cache.clear()
        with (
            patch.object(gh, "resolve_gh_env", AsyncMock(side_effect=RuntimeError("keychain locked"))),
            capture_logs() as logs,
        ):
            assert await _real_get_acting_gh_login("alice") is None
        failures = [entry for entry in logs if entry["event"] == "gh.acting_login_failed"]
        assert failures
        assert failures[0].get("exc_info")

    @pytest.mark.parametrize(("login", "reads"), [("carol", 2), ("Alice", 1)])
    async def test_only_a_matching_login_is_cached(self, login: str, reads: int) -> None:
        """A mismatch is re-read, so `gh auth switch` takes effect on the very next start."""
        from sova.utils import gh

        gh._gh_login_cache.clear()
        run = AsyncMock(return_value=ShellResult(returncode=0, stdout=f"{login}\n", stderr=""))
        with patch.object(gh, "resolve_gh_env", AsyncMock(return_value=None)), patch.object(gh, "run", run):
            assert await _real_get_acting_gh_login("alice") == login
            assert await _real_get_acting_gh_login("alice") == login
        gh._gh_login_cache.clear()
        assert run.await_count == reads


class TestBoardOwnershipFallbacks:
    def test_pr_author_object_is_flattened_to_its_login(self) -> None:
        from sova.dashboard.services.work_state import _build_pr_facts

        facts = _build_pr_facts(
            {"number": 12, "author": {"login": "bob"}}, None, external_reviews_enabled=False, github_user="alice"
        )
        assert facts.pr_author == "bob"
        assert resolve_next_action(facts).state is WorkItemState.PR_OWNED_BY_OTHER

    def test_task_with_an_authorless_pr_falls_back_to_the_assignees(self) -> None:
        """The same order check_ownership() uses, so the board offers no write the start path refuses."""
        from sova.dashboard.services.work_item_service import _build_task_item

        task = {"issue": "7", "state": "in_review", "assignees": ["bob"], "title": "t"}
        pr = {"number": 12, "state": "OPEN", "mergeable": "MERGEABLE", "ci_status": "passed"}
        item = _build_task_item(task, pr, None, None, github_user="alice")
        assert item["owner"] == "bob"
        assert item["primary_action"] is None

    def test_a_running_agent_keeps_its_controls_on_another_logins_issue(self) -> None:
        """Stop and status are never refused: an assignee change mid-run must not clear them."""
        from sova.dashboard.services.work_item_service import _build_task_item

        task = {"issue": "7", "state": "in_progress", "assignees": ["bob"], "title": "t"}
        running = {"run_id": 5, "role": "developer", "elapsed_seconds": 60}
        item = _build_task_item(task, None, running, None, github_user="alice")
        assert item["state"] == "agent_running"
        assert item["owner"] == ""
