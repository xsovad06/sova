"""Tests for sova.dashboard.services.conversation_actions: propose, preview and confirm of conversation actions."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig
from sova.dashboard.services import conversation_actions as actions
from sova.dashboard.services.agent_db import find_failed_step, mark_failed_step_done
from sova.dashboard.services.conversation_actions import (
    KINDS,
    ActionContext,
    ActionFailed,
    ProposalRejected,
    confirm_action,
    propose_action,
    validate_proposal,
)
from sova.db.models import FailureRecord, StepExecution, TaskRun
from sova.db.session import get_session
from sova.git.board import BoardOutcome
from sova.git.worktree import GCEntry, GCResult
from sova.utils.shell import ShellResult
from sova.utils.shell import run as real_run

_MODULE = "sova.dashboard.services.conversation_actions"
# The real project check, kept before the autouse fixture below replaces it for tests that do not set up a pool.
_REAL_REQUIRE_PROJECT = actions._require_request_project

GOOD_BODY = """## Objective
Fix the thing.

## Detailed Description
It breaks in `sova/git/worktree.py`.

## Acceptance Criteria
- [ ] It no longer breaks

## Files / Modules to Change
- [ ] `sova/git/worktree.py`

## Out of Scope / Constraints
- Nothing else

## References
- #12
"""


@pytest.fixture
def ctx(tmp_path: Path) -> ActionContext:
    return ActionContext(project_dir=tmp_path, github_user="octo")


@pytest.fixture(autouse=True)
def _request_project_matches() -> Any:
    """Most tests have no request-scoped agent pool; TestRequestProject exercises the real check."""
    with patch(f"{_MODULE}._require_request_project"):
        yield


class TestValidateProposal:
    @pytest.mark.parametrize(
        ("kind", "params", "reason", "message"),
        [
            pytest.param("drop_database", {}, "why", "Unknown action kind 'drop_database'", id="unknown-kind"),
            pytest.param("cleanup_after_run", [], "why", "params must be an object", id="params-not-object"),
            pytest.param("cleanup_after_run", {}, "why", "issue must be an issue number", id="missing-issue"),
            pytest.param("cleanup_after_run", {"issue": "abc"}, "why", "issue must be", id="non-numeric-issue"),
            pytest.param("cleanup_after_run", {"issue": 42, "force": True}, "why", "Unknown parameter(s): force",
                         id="force-param"),
            pytest.param("stop_run", {"run_id": -1}, "why", "run_id must be a positive number", id="negative-run"),
            pytest.param("stop_run", {"run_id": True}, "why", "run_id must be a positive number", id="bool-run"),
            pytest.param("start_run", {"issue": 1, "role": "x; rm"}, "why", "role must be a role name", id="bad-role"),
            pytest.param("file_issue", {"title": "t", "body": "b", "labels": "bug"}, "why", "labels must be a list",
                         id="labels-not-list"),
            pytest.param("cleanup_after_run", {"issue": 42}, "  ", "reason is required", id="no-reason"),
        ],
    )  # fmt: skip
    def test_rejected_with_a_reason(self, kind: str, params: object, reason: str, message: str) -> None:
        with pytest.raises(ProposalRejected, match=message.replace("(", r"\(").replace(")", r"\)")):
            validate_proposal(kind, params, reason)

    def test_params_are_normalized(self) -> None:
        proposal = validate_proposal("cleanup_after_run", {"issue": "#42"}, " merged, then failed ")
        assert (proposal.params, proposal.reason) == ({"issue": "42"}, "merged, then failed")

    def test_no_kind_is_a_bulk_or_force_action(self) -> None:
        assert set(KINDS) == {
            "cleanup_after_run",
            "mark_step_done_and_resume",
            "reset_stuck_issue",
            "start_run",
            "stop_run",
            "resume_run",
            "handoff_action",
            "handoff_dismiss",
            "file_issue",
        }


# Clean up after a run: end to end against a temporary repository -----------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def merged_issue_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repo whose issue #42 branch was merged, with its worktree left behind (an integrate run that then failed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "feat/issue-42")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "feat")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "feat/issue-42")
    worktree = repo / ".claude" / "worktrees" / "42"
    _git(repo, "worktree", "add", "-q", str(worktree), "feat/issue-42")
    return repo, worktree


@pytest.fixture
def squash_merged_issue_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Issue #42's branch as an integrate run leaves it on GitHub: pushed, squash merged into main, the remote branch
    deleted on merge and pruned, so the branch has no upstream and its commits are no ancestor of main."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "push", "-q", "-u", "origin", "main")
    _git(repo, "checkout", "-q", "-b", "feat/issue-42")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "feat")
    _git(repo, "push", "-q", "-u", "origin", "feat/issue-42")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--squash", "feat/issue-42")
    _git(repo, "commit", "-q", "-m", "feat (#43)")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "push", "-q", "origin", "--delete", "feat/issue-42")
    _git(repo, "fetch", "-q", "--prune", "origin")
    worktree = repo / ".claude" / "worktrees" / "42"
    _git(repo, "worktree", "add", "-q", str(worktree), "feat/issue-42")
    return repo, worktree


def _fake_gh(state: str, merged_heads: tuple[str, ...] = ()) -> Callable[..., Awaitable[ShellResult]]:
    """Answer ``gh issue view`` with ``state`` and ``gh pr list --state merged`` with PRs headed at ``merged_heads``;
    every git command runs for real."""
    prs = json.dumps([{"headRefOid": head, "isCrossRepository": False} for head in merged_heads])

    async def fake(*args: str, **kwargs: Any) -> ShellResult:
        if args[0] == "gh":
            return ShellResult(0, f"{state}\n" if args[1] == "issue" else prs, "")
        return await real_run(*args, **kwargs)

    return fake


_gh_says_closed = _fake_gh("CLOSED")


@pytest.fixture
def closed_issue_42() -> Any:
    with (
        patch("sova.git.worktree.run", side_effect=_gh_says_closed),
        patch("sova.git.worktree.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
        patch(f"{_MODULE}.emit_safe") as emit,
    ):
        yield emit


class TestCleanupAfterRun:
    async def test_merged_then_failed_integrate_is_cleaned_up(
        self, merged_issue_repo: tuple[Path, Path], closed_issue_42: Any
    ) -> None:
        repo, worktree = merged_issue_repo
        ctx = ActionContext(project_dir=repo, github_user="octo")

        proposal, preview = await propose_action("cleanup_after_run", {"issue": 42}, "PR merged, run failed", ctx)

        assert preview.blocked == ""
        assert preview.lines == (f"Remove worktree {worktree} (feat/issue-42)", "Delete local branch feat/issue-42")
        assert worktree.exists()

        result = await confirm_action(proposal, preview.fingerprint, ctx)

        assert result.status == "executed", result.reason
        assert not worktree.exists()
        assert _git(repo, "branch", "--list", "--format=%(refname:short)", "feat/issue-42") == ""
        assert result.result["removed_worktrees"] == [[str(worktree), "feat/issue-42", "issue #42 is closed"]]
        closed_issue_42.assert_called_once()
        assert closed_issue_42.call_args.args[0] == "Done from a conversation: Clean up after #42"

    async def test_worktree_dirtied_after_the_preview_runs_nothing(
        self, merged_issue_repo: tuple[Path, Path], closed_issue_42: Any
    ) -> None:
        repo, worktree = merged_issue_repo
        ctx = ActionContext(project_dir=repo, github_user="octo")
        proposal, preview = await propose_action("cleanup_after_run", {"issue": "42"}, "merged", ctx)

        (worktree / "unsaved.txt").write_text("work\n", encoding="utf-8")
        result = await confirm_action(proposal, preview.fingerprint, ctx)

        assert result.status == "changed"
        assert worktree.exists()
        assert _git(repo, "branch", "--list", "--format=%(refname:short)", "feat/issue-42").strip() == "feat/issue-42"
        assert f"Skip {worktree}: it has uncommitted changes" in result.preview.lines
        closed_issue_42.assert_not_called()

        # Confirming the new preview still removes nothing: the dirty worktree is a skip, not a target.
        again = await confirm_action(proposal, result.preview.fingerprint, ctx)
        assert again.status == "refused"
        assert worktree.exists()


# Mark a failed step done and resume -------------------------------------------------


@pytest.mark.usefixtures("reset_db_rows")
class TestMarkStepDoneAndResume:
    @staticmethod
    async def _seed(status: str = "failed") -> int:
        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="42", role="developer", status=status)
                session.add(run)
                await session.flush()
                session.add_all(
                    [
                        StepExecution(task_run_id=run.id, step_name="develop", status="done"),
                        StepExecution(task_run_id=run.id, step_name="validate", status="failed"),
                        FailureRecord(task_run_id=run.id, step_name="validate", failure_type="gate", message="red"),
                    ]
                )
                return run.id

    async def test_records_the_user_and_reason_then_resumes_through_the_start_guards(self, ctx: ActionContext) -> None:
        run_id = await self._seed()
        start = AsyncMock(return_value={"status": "started", "run_id": run_id + 1})
        with patch("sova.dashboard.services.control_service.start_agent", start), patch(f"{_MODULE}.emit_safe"):
            proposal, preview = await propose_action(
                "mark_step_done_and_resume", {"run_id": run_id, "step": "validate"}, "fixed the lint by hand", ctx
            )
            result = await confirm_action(proposal, preview.fingerprint, ctx)

        assert result.status == "executed", result.reason
        start.assert_awaited_once_with("42", role="developer", resume_run_id=run_id, enforce_suitability=True)
        async with await get_session() as session:
            step = (await session.execute(_step_query(run_id, "validate"))).scalar_one()
            record = (await session.execute(_failure_query(run_id))).scalar_one()
        assert (step.status, step.output_summary) == ("done", "Marked done by octo: fixed the lint by hand")
        assert (record.resolved, record.resolved_by) == (True, "octo")
        assert record.context["manual_resolution"]["reason"] == "fixed the lint by hand"

    async def test_a_crashed_resume_says_the_step_is_already_marked_done(self, ctx: ActionContext) -> None:
        run_id = await self._seed()
        start = AsyncMock(side_effect=OSError("pool gone"))
        with patch("sova.dashboard.services.control_service.start_agent", start), patch(f"{_MODULE}.emit_safe"):
            proposal, preview = await propose_action(
                "mark_step_done_and_resume", {"run_id": run_id, "step": "validate"}, "fixed", ctx
            )
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert result.status == "failed"
        assert result.reason.startswith("Step validate is marked done, but the resume failed unexpectedly")
        assert "pool gone" not in result.reason
        found = await find_failed_step(run_id, project_dir=ctx.project_dir)
        assert found is not None
        assert found.marked_step == "validate"

    async def test_the_dashboard_user_who_confirmed_is_recorded(self, ctx: ActionContext) -> None:
        run_id = await self._seed()
        clicked = replace(ctx, confirmed_by="alice")
        start = AsyncMock(return_value={"status": "started", "run_id": run_id + 1})
        with patch("sova.dashboard.services.control_service.start_agent", start), patch(f"{_MODULE}.emit_safe"):
            proposal, preview = await propose_action(
                "mark_step_done_and_resume", {"run_id": run_id, "step": "validate"}, "fixed", clicked
            )
            result = await confirm_action(proposal, preview.fingerprint, clicked)
        assert result.status == "executed", result.reason
        async with await get_session() as session:
            step = (await session.execute(_step_query(run_id, "validate"))).scalar_one()
        assert step.output_summary.startswith("Marked done by alice: fixed")

    async def test_resumed_by_someone_else_refuses_the_confirm(self, ctx: ActionContext) -> None:
        run_id = await self._seed()
        start = AsyncMock()
        with patch("sova.dashboard.services.control_service.start_agent", start):
            proposal, preview = await propose_action(
                "mark_step_done_and_resume", {"run_id": run_id, "step": "validate"}, "fixed", ctx
            )
            async with await get_session() as session, session.begin():
                await session.execute(update(TaskRun).where(TaskRun.id == run_id).values(status="running"))
            result = await confirm_action(proposal, preview.fingerprint, ctx)

        assert result.status == "changed"
        assert result.preview.blocked == f"Run #{run_id} is running, not failed"
        start.assert_not_awaited()

    @pytest.mark.parametrize("status", ["done", "interrupted", "running"])
    async def test_only_a_failed_run_can_be_marked(self, ctx: ActionContext, status: str) -> None:
        run_id = await self._seed(status)
        _proposal, preview = await propose_action(
            "mark_step_done_and_resume", {"run_id": run_id, "step": "validate"}, "fixed", ctx
        )
        assert preview.blocked == f"Run #{run_id} is {status}, not failed"
        refusal = await mark_failed_step_done(
            run_id, step_name="validate", confirmed_by="octo", reason="fixed", project_dir=ctx.project_dir
        )
        assert refusal == f"Run #{run_id} is {status}, not failed"

    async def test_run_outside_the_project_is_rejected(self, ctx: ActionContext) -> None:
        with pytest.raises(ProposalRejected, match="Run #999 does not exist in this project"):
            await propose_action("mark_step_done_and_resume", {"run_id": 999, "step": "validate"}, "fixed", ctx)
        assert await find_failed_step(999, project_dir=ctx.project_dir) is None


def _step_query(run_id: int, name: str) -> Any:
    from sqlalchemy import select

    return select(StepExecution).where(StepExecution.task_run_id == run_id, StepExecution.step_name == name)


def _failure_query(run_id: int) -> Any:
    from sqlalchemy import select

    return select(FailureRecord).where(FailureRecord.task_run_id == run_id)


# Reset a stuck issue ----------------------------------------------------------------


class TestResetStuckIssue:
    async def test_blocked_preview_refuses_and_resets_nothing(self, ctx: ActionContext) -> None:
        engine = AsyncMock()
        engine.stuck_reset_blockers = AsyncMock(return_value=["it has an open PR #5"])
        with patch(f"{_MODULE}._progression_engine", AsyncMock(return_value=engine)):
            proposal, preview = await propose_action("reset_stuck_issue", {"issue": 42}, "no agent", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert (result.status, result.reason) == ("refused", "it has an open PR #5")
        engine.reset_stuck_issue.assert_not_awaited()


# File an issue ----------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.created: list[dict] = []
        self.comments: list[tuple[str, str]] = []
        self._fail = fail

    async def create_issue(self, *, title: str, body: str, labels: list[str]) -> Task:
        if self._fail:
            raise RuntimeError("HTTP 422: label 'nope' does not exist")
        self.created.append({"title": title, "body": body, "labels": labels})
        return Task(id="77", title=title, body=body, state=TaskState.BACKLOG, url="https://github.com/a/b/issues/77")

    async def post_comment(self, task_id: str, body: str) -> None:
        self.comments.append((task_id, body))


def _gh_json_stub(*, overlap: list[dict] | None, target_state: str = "OPEN") -> Any:
    async def stub(_project_dir: Path, *args: str) -> Any:
        if args[:2] == ("issue", "view"):
            return {"state": target_state}
        return overlap

    return stub


@pytest.fixture
def github_project() -> Any:
    cfg = ProjectConfig()
    cfg = cfg.model_copy(update={"task_source": cfg.task_source.model_copy(update={"github_project_number": 2})})
    with patch(f"{_MODULE}.load_config", return_value=cfg), patch(f"{_MODULE}.emit_safe") as emit:
        yield emit


@pytest.mark.usefixtures("github_project")
class TestFileIssue:
    async def test_card_shows_score_and_overlap_and_filing_adds_to_the_board(self, ctx: ActionContext) -> None:
        adapter = _FakeAdapter()
        board = AsyncMock(return_value=[BoardOutcome("https://github.com/a/b/issues/77", "added")])
        overlap = [{"number": 9, "title": "Worktree gc is slow"}]
        with (
            patch(f"{_MODULE}._gh_json", _gh_json_stub(overlap=overlap)),
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.git.board.add_issues_to_board", board),
        ):
            proposal, preview = await propose_action(
                "file_issue", {"title": "Fix gc", "body": GOOD_BODY, "labels": ["type: bug"]}, "found in run 3", ctx
            )
            result = await confirm_action(proposal, preview.fingerprint, ctx)

        assert "Quality score 8/8 (filing needs 4)" in preview.lines
        assert "Overlaps open issue #9: Worktree gc is slow" in preview.lines
        assert result.status == "executed", result.reason
        assert result.warning == ""
        assert adapter.created == [{"title": "Fix gc", "body": GOOD_BODY.strip(), "labels": ["type: bug"]}]
        board.assert_awaited_once_with(["https://github.com/a/b/issues/77"], 2, repo_owner="a")

    @pytest.mark.parametrize(
        "outcomes",
        [
            pytest.param([BoardOutcome("https://github.com/a/b/issues/77", "failed", "missing project scope")],
                         id="item-add-failed"),
            pytest.param([BoardOutcome("", "skip", "2 project boards found")], id="discovery-skipped"),
            pytest.param([], id="no-outcome"),
        ],
    )  # fmt: skip
    async def test_a_failed_board_add_is_a_visible_partial_outcome(
        self, ctx: ActionContext, github_project: Any, outcomes: list[BoardOutcome]
    ) -> None:
        with (
            patch(f"{_MODULE}._gh_json", _gh_json_stub(overlap=[])),
            patch("sova.adapters.create_adapter", return_value=_FakeAdapter()),
            patch("sova.git.board.add_issues_to_board", AsyncMock(return_value=outcomes)),
        ):
            proposal, preview = await propose_action("file_issue", {"title": "t", "body": GOOD_BODY}, "r", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)

        assert result.status == "executed"
        assert result.warning.startswith("Filed #77, not added to the board: ")
        assert "warning" not in result.result
        line = github_project.call_args.args[0]
        assert line.startswith("Partly done from a conversation: File issue: t.")
        assert result.warning in line

    async def test_a_board_step_crash_after_filing_is_a_warning_never_a_failure(
        self, ctx: ActionContext, github_project: Any
    ) -> None:
        with (
            patch(f"{_MODULE}._gh_json", _gh_json_stub(overlap=[])),
            patch("sova.adapters.create_adapter", return_value=_FakeAdapter()),
            patch("sova.git.board.add_issues_to_board", AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            proposal, preview = await propose_action("file_issue", {"title": "t", "body": GOOD_BODY}, "r", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)

        assert result.status == "executed"
        assert str(result.result["number"]) == "77"
        assert result.warning.startswith("Filed #77, not added to the board: adding it to the board failed")

    def test_a_label_over_the_github_limit_is_rejected(self) -> None:
        with pytest.raises(ProposalRejected, match="at most 50 characters"):
            validate_proposal("file_issue", {"title": "t", "body": "b", "labels": ["x" * 51]}, "r")

    async def test_comment_variant_posts_and_creates_nothing(self, ctx: ActionContext) -> None:
        adapter = _FakeAdapter()
        with (
            patch(f"{_MODULE}._gh_json", _gh_json_stub(overlap=[])),
            patch("sova.adapters.create_adapter", return_value=adapter),
        ):
            params = {"title": "Fix gc", "body": "low effort", "comment_on": 9}
            proposal, preview = await propose_action("file_issue", params, "same defect", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)

        assert result.status == "executed", result.reason
        assert adapter.comments == [("9", "low effort")]
        assert adapter.created == []

    async def test_closed_comment_target_is_refused(self, ctx: ActionContext) -> None:
        adapter = _FakeAdapter()
        with (
            patch(f"{_MODULE}._gh_json", _gh_json_stub(overlap=[], target_state="CLOSED")),
            patch("sova.adapters.create_adapter", return_value=adapter),
        ):
            proposal, preview = await propose_action(
                "file_issue", {"title": "t", "body": "b", "comment_on": 9}, "same defect", ctx
            )
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert (result.status, result.reason) == ("refused", "#9 is closed or could not be read")
        assert adapter.comments == []

    async def test_draft_below_the_quality_score_cannot_be_filed_until_revised(
        self, ctx: ActionContext, github_project: Any
    ) -> None:
        adapter = _FakeAdapter()
        with (
            patch(f"{_MODULE}._gh_json", _gh_json_stub(overlap=[])),
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.git.board.add_issues_to_board", AsyncMock(return_value=[])),
        ):
            weak, weak_preview = await propose_action("file_issue", {"title": "t", "body": "fix it"}, "r", ctx)
            refused = await confirm_action(weak, weak_preview.fingerprint, ctx)
            revised, revised_preview = await propose_action("file_issue", {"title": "t", "body": GOOD_BODY}, "r", ctx)
            filed = await confirm_action(revised, revised_preview.fingerprint, ctx)

        assert refused.status == "refused"
        assert "below triage.min_quality_score 4" in refused.reason
        assert filed.status == "executed"
        assert len(adapter.created) == 1
        github_project.assert_called_once()

    async def test_tracker_failure_is_the_reason_on_the_card(self, ctx: ActionContext, github_project: Any) -> None:
        with (
            patch(f"{_MODULE}._gh_json", _gh_json_stub(overlap=[])),
            patch("sova.adapters.create_adapter", return_value=_FakeAdapter(fail=True)),
        ):
            proposal, preview = await propose_action("file_issue", {"title": "t", "body": GOOD_BODY}, "r", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert result.status == "failed"
        # The exception text (a response body, a URL) stays in the server log, never on the card or the agent.
        assert result.reason == "The tracker refused the change; see the server log"
        assert "422" not in result.reason
        github_project.assert_not_called()

    async def test_jira_project_rejects_filing_at_propose_time(self, ctx: ActionContext) -> None:
        cfg = ProjectConfig()
        jira = cfg.model_copy(update={"task_source": cfg.task_source.model_copy(update={"type": "jira"})})
        with patch(f"{_MODULE}.load_config", return_value=jira):
            with pytest.raises(ProposalRejected, match="only supported for GitHub-backed projects"):
                await propose_action("file_issue", {"title": "t", "body": GOOD_BODY}, "r", ctx)


# Confirm semantics shared by every kind ------------------------------------------


class TestConfirm:
    async def test_service_refusal_is_a_failed_result_with_the_reason(self, ctx: ActionContext) -> None:
        kind = KINDS["stop_run"]
        refusing = replace(kind, execute=AsyncMock(side_effect=ActionFailed("Run 5 is not held by this server")))
        with (
            patch.dict(KINDS, {"stop_run": refusing}),
            patch(f"{_MODULE}._run_status", AsyncMock(return_value="running")),
            patch(f"{_MODULE}.emit_safe") as emit,
        ):
            proposal, preview = await propose_action("stop_run", {"run_id": 5}, "stuck", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert (result.status, result.reason) == ("failed", "Run 5 is not held by this server")
        emit.assert_not_called()

    @pytest.mark.parametrize("stage", ["preview", "execute"])
    async def test_unexpected_exception_is_a_failed_result_not_a_raise(self, ctx: ActionContext, stage: str) -> None:
        kind = KINDS["stop_run"]
        with patch(f"{_MODULE}._run_status", AsyncMock(return_value="running")), patch(f"{_MODULE}.emit_safe") as emit:
            proposal, preview = await propose_action("stop_run", {"run_id": 5}, "stuck", ctx)
            crashing = replace(kind, **{stage: AsyncMock(side_effect=OSError("disk gone"))})
            with patch.dict(KINDS, {"stop_run": crashing}):
                result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert result.status == "failed"
        assert "disk gone" not in result.reason
        assert result.reason.endswith("see the server log")
        emit.assert_not_called()

    @pytest.mark.parametrize("raw", [None, ["stopped"], {"status": "stopped"}])
    async def test_any_service_result_is_normalized_without_raising(self, ctx: ActionContext, raw: Any) -> None:
        kind = KINDS["stop_run"]
        returning = replace(kind, execute=AsyncMock(return_value=raw))
        with (
            patch.dict(KINDS, {"stop_run": returning}),
            patch(f"{_MODULE}._run_status", AsyncMock(return_value="running")),
            patch(f"{_MODULE}.emit_safe") as emit,
        ):
            proposal, preview = await propose_action("stop_run", {"run_id": 5}, "stuck", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert result.status == "executed"
        assert result.result == (raw if isinstance(raw, dict) else {"result": raw})
        emit.assert_called_once()

    async def test_the_service_result_is_copied_not_mutated(self, ctx: ActionContext) -> None:
        payload = {"started": True, actions._WARNING_KEY: "partly"}
        kind = KINDS["stop_run"]
        with (
            patch.dict(KINDS, {"stop_run": replace(kind, execute=AsyncMock(return_value=payload))}),
            patch(f"{_MODULE}._run_status", AsyncMock(return_value="running")),
            patch(f"{_MODULE}.emit_safe"),
        ):
            proposal, preview = await propose_action("stop_run", {"run_id": 5}, "stuck", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert (result.warning, result.result) == ("partly", {"started": True})
        assert actions._WARNING_KEY in payload

    async def test_stale_fingerprint_executes_nothing(self, ctx: ActionContext) -> None:
        stop = AsyncMock(return_value={"status": "stopped"})
        with (
            patch(f"{_MODULE}._run_status", AsyncMock(return_value="running")),
            patch("sova.dashboard.services.control_service.stop_agent", stop),
        ):
            proposal, _preview = await propose_action("stop_run", {"run_id": 5}, "stuck", ctx)
            result = await confirm_action(proposal, "not-what-the-card-showed", ctx)
        assert result.status == "changed"
        stop.assert_not_awaited()

    async def test_preview_is_deterministic(self, ctx: ActionContext) -> None:
        with patch(f"{_MODULE}._run_status", AsyncMock(return_value="running")):
            _p1, first = await propose_action("stop_run", {"run_id": 5}, "a", ctx)
            _p2, second = await propose_action("stop_run", {"run_id": "5"}, "b", ctx)
        assert first.fingerprint == second.fingerprint
        assert actions.Preview(**{**first.to_dict(), "lines": tuple(first.lines)}) == first


# Review follow-ups: parsing, start role, handoff binding, comment target, board, reset line ----------


class TestParsingEdges:
    @pytest.mark.parametrize(
        ("kind", "params"),
        [
            ("cleanup_after_run", {"issue": "\u00b2"}),
            ("stop_run", {"run_id": "\u00b2"}),
            ("file_issue", {"title": "t", "body": "b", "comment_on": False}),
            ("file_issue", {"title": "t", "body": "b", "comment_on": 0.0}),
        ],
    )
    def test_unparseable_values_are_rejected_not_raised(self, kind: str, params: dict) -> None:
        with pytest.raises(ProposalRejected):
            validate_proposal(kind, params, "why")

    def test_leading_zeros_are_dropped_from_an_issue_number(self) -> None:
        assert validate_proposal("cleanup_after_run", {"issue": "042"}, "r").params == {"issue": "42"}

    def test_a_jira_key_is_kept(self) -> None:
        assert validate_proposal("handoff_dismiss", {"issue": "PROJ-7"}, "r").params == {"issue": "PROJ-7"}

    @pytest.mark.parametrize("omitted", [None, "", 0])
    def test_an_omitted_comment_target_files_a_new_issue(self, omitted: object) -> None:
        params = {"title": "t", "body": "b", "comment_on": omitted}
        assert validate_proposal("file_issue", params, "r").params["comment_on"] is None


class TestStartRun:
    async def test_the_role_the_preview_names_is_the_role_started(self, ctx: ActionContext) -> None:
        start = AsyncMock(return_value={"run_id": 3})
        with (
            patch(f"{_MODULE}._active_run_detail", AsyncMock(return_value="")),
            patch("sova.dashboard.services.control_service.start_agent", start),
            patch(f"{_MODULE}.emit_safe"),
        ):
            proposal, preview = await propose_action("start_run", {"issue": 42}, "ready", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert preview.summary == "Start the developer agent on #42"
        assert result.result == {"run_id": 3}
        assert start.await_args.kwargs["role"] == "developer"

    async def test_a_running_agent_blocks_the_start(self, ctx: ActionContext) -> None:
        with patch(f"{_MODULE}._active_run_detail", AsyncMock(return_value="Agent already running for #42")):
            _proposal, preview = await propose_action("start_run", {"issue": 42, "role": "researcher"}, "r", ctx)
        assert preview.blocked == "Agent already running for #42"


_HANDOFF = {"next_actions": [{"id": "review", "label": "Review", "command": "/review-pr 5"}]}


class TestHandoffActions:
    async def test_a_changed_action_behind_the_same_name_changes_the_preview(self, ctx: ActionContext) -> None:
        changed = {"next_actions": [{"id": "review", "label": "Review", "command": "/integrate-pr 5"}]}
        execute = AsyncMock()
        with (
            patch("sova.dashboard.services.handoff_service.get_handoff", side_effect=[_HANDOFF, changed]),
            patch("sova.dashboard.services.handoff_service.execute_action", execute),
        ):
            proposal, preview = await propose_action("handoff_action", {"issue": 42, "action_id": "review"}, "r", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert "Command: /review-pr 5" in preview.lines
        assert preview.facts["selected"] == _HANDOFF["next_actions"][0]
        assert result.status == "changed"
        execute.assert_not_awaited()

    async def test_execute_refuses_a_handoff_action_rewritten_after_the_preview(self, ctx: ActionContext) -> None:
        proposal = validate_proposal("handoff_action", {"issue": 42, "action_id": "review"}, "r")
        draft = actions._Draft("s", (), "", {"actions": ["review"], "selected": _HANDOFF["next_actions"][0]})
        rewritten = {"next_actions": [{"id": "review", "label": "Review", "command": "/integrate-pr 5"}]}
        execute = AsyncMock()
        with (
            patch("sova.dashboard.services.handoff_service.get_handoff", return_value=rewritten),
            patch("sova.dashboard.services.handoff_service.execute_action", execute),
            patch(f"{_MODULE}._require_request_project"),
        ):
            with pytest.raises(ActionFailed, match="nothing was run"):
                await actions._execute_handoff(proposal, ctx, draft)
        execute.assert_not_awaited()

    async def test_an_unknown_action_is_blocked(self, ctx: ActionContext) -> None:
        with patch("sova.dashboard.services.handoff_service.get_handoff", return_value=_HANDOFF):
            _p, preview = await propose_action("handoff_action", {"issue": 42, "action_id": "merge"}, "r", ctx)
        assert preview.blocked == "#42 has no pending handoff action merge"

    async def test_dismiss_clears_only_the_reviewed_handoff(self, ctx: ActionContext) -> None:
        clear = patch("sova.dashboard.services.handoff_service.clear_handoff", return_value=True)
        with (
            patch("sova.dashboard.services.handoff_service.get_handoff", return_value=_HANDOFF),
            clear as cleared,
            patch("sova.dashboard.services.handoff_service.invalidate_handoff_snapshot"),
            patch(f"{_MODULE}.emit_safe"),
        ):
            proposal, preview = await propose_action("handoff_dismiss", {"issue": 42}, "r", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert result.status == "executed", result.reason
        assert cleared.call_args.kwargs == {"expected": _HANDOFF}

    async def test_dismiss_of_a_replaced_handoff_clears_nothing(self, ctx: ActionContext) -> None:
        replaced = {"next_actions": [{"id": "develop"}]}
        with (
            patch("sova.dashboard.services.handoff_service.get_handoff", side_effect=[_HANDOFF, replaced]),
            patch("sova.dashboard.services.handoff_service.clear_handoff") as cleared,
        ):
            proposal, preview = await propose_action("handoff_dismiss", {"issue": 42}, "r", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert result.status == "changed"
        cleared.assert_not_called()

    @pytest.mark.parametrize(("current", "clears"), [(None, True), (_HANDOFF, False)])
    async def test_dismiss_execute_refuses_a_handoff_gone_or_changed_on_disk(
        self, ctx: ActionContext, current: dict | None, clears: bool
    ) -> None:
        proposal = validate_proposal("handoff_dismiss", {"issue": 42}, "r")
        draft = actions._Draft("s", (), "", {"exists": True, "handoff": actions._handoff_digest(_HANDOFF)})
        with (
            patch("sova.dashboard.services.handoff_service.get_handoff", return_value=current),
            patch("sova.dashboard.services.handoff_service.clear_handoff", return_value=clears),
        ):
            with pytest.raises(ActionFailed, match="changed or is gone"):
                await actions._execute_handoff_dismiss(proposal, ctx, draft)


@pytest.mark.usefixtures("github_project")
class TestFileIssueTargets:
    async def test_the_comment_target_is_read_from_the_configured_repository(self, ctx: ActionContext) -> None:
        cfg = ProjectConfig(github_repo="acme/widgets")
        seen: list[tuple[str, ...]] = []

        async def gh(_project_dir: Path, *args: str) -> Any:
            seen.append(args)
            return {"state": "OPEN"}

        with patch(f"{_MODULE}.load_config", return_value=cfg), patch(f"{_MODULE}._gh_json", gh):
            await propose_action("file_issue", {"title": "t", "body": "b", "comment_on": 9}, "r", ctx)
        assert seen == [("issue", "view", "9", "--repo", "acme/widgets", "--json", "state")]

    async def test_the_board_is_on_the_card_and_in_the_fingerprint(self, ctx: ActionContext) -> None:
        cfg = ProjectConfig()
        unset = cfg.model_copy(update={"task_source": cfg.task_source.model_copy(update={"github_project_number": 0})})
        params = {"title": "t", "body": GOOD_BODY}
        with patch(f"{_MODULE}._gh_json", _gh_json_stub(overlap=[])):
            _p, on_two = await propose_action("file_issue", params, "r", ctx)
            with patch(f"{_MODULE}.load_config", return_value=unset):
                _p, discovered = await propose_action("file_issue", params, "r", ctx)
        assert "Add it to project board 2" in on_two.lines
        assert "Add it to the repository owner's only project board" in discovered.lines
        assert on_two.fingerprint != discovered.fingerprint


class TestResetLine:
    async def test_a_confirmed_reset_leaves_out_the_supervisor_line(self, ctx: ActionContext) -> None:
        engine = AsyncMock()
        engine.stuck_reset_blockers = AsyncMock(return_value=[])
        engine.reset_stuck_issue = AsyncMock(return_value={"reset": True, "issue": 42})
        with patch(f"{_MODULE}._progression_engine", AsyncMock(return_value=engine)), patch(f"{_MODULE}.emit_safe"):
            proposal, preview = await propose_action("reset_stuck_issue", {"issue": 42}, "no agent", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert result.status == "executed", result.reason
        engine.reset_stuck_issue.assert_awaited_once_with(42, emit_feed=False)

    async def test_a_reset_that_did_not_happen_fails_with_its_reason(self, ctx: ActionContext) -> None:
        engine = AsyncMock()
        engine.stuck_reset_blockers = AsyncMock(return_value=[])
        engine.reset_stuck_issue = AsyncMock(return_value={"error": "did not move it"})
        with patch(f"{_MODULE}._progression_engine", AsyncMock(return_value=engine)), patch(f"{_MODULE}.emit_safe"):
            proposal, preview = await propose_action("reset_stuck_issue", {"issue": 42}, "no agent", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert (result.status, result.reason) == ("failed", "did not move it")


class TestCleanupUnmergedBranch:
    async def test_an_unmerged_branch_is_a_skip_in_the_preview(
        self, merged_issue_repo: tuple[Path, Path], closed_issue_42: Any
    ) -> None:
        repo, worktree = merged_issue_repo
        (worktree / "c.txt").write_text("c\n", encoding="utf-8")
        _git(worktree, "add", "c.txt")
        _git(worktree, "commit", "-q", "-m", "unmerged")
        ctx = ActionContext(project_dir=repo, github_user="octo")

        proposal, preview = await propose_action("cleanup_after_run", {"issue": 42}, "merged", ctx)

        assert preview.lines == (
            f"Remove worktree {worktree} (feat/issue-42)",
            "Skip feat/issue-42: it is not merged where git branch -d checks (it may have been squash merged)",
        )

        # Confirming does exactly what the card said: the worktree goes, the unmerged branch stays as a skip.
        result = await confirm_action(proposal, preview.fingerprint, ctx)

        assert result.status == "executed", result.reason
        assert not worktree.exists()
        assert _git(repo, "branch", "--list", "--format=%(refname:short)", "feat/issue-42").strip() == "feat/issue-42"
        assert result.result["skipped"] == [
            ["feat/issue-42", "it is not merged where git branch -d checks (it may have been squash merged)"]
        ]
        assert result.result["removed_branches"] == []


@pytest.mark.usefixtures("reset_db_rows")
class TestMarkStepResumeOnly:
    async def test_a_refused_resume_can_be_retried_without_marking_again(self, ctx: ActionContext) -> None:
        run_id = await TestMarkStepDoneAndResume._seed()
        params = {"run_id": run_id, "step": "validate"}
        refused = AsyncMock(return_value={"error": "budget exhausted"})
        with patch("sova.dashboard.services.control_service.start_agent", refused), patch(f"{_MODULE}.emit_safe"):
            proposal, preview = await propose_action("mark_step_done_and_resume", params, "fixed by hand", ctx)
            first = await confirm_action(proposal, preview.fingerprint, ctx)
        assert first.status == "failed"
        assert first.reason.startswith("Step validate is marked done, but the resume was refused: budget exhausted")

        started = AsyncMock(return_value={"status": "started", "run_id": run_id + 1})
        with (
            patch("sova.dashboard.services.control_service.start_agent", started),
            patch("sova.dashboard.services.agent_db.mark_failed_step_done", new_callable=AsyncMock) as mark,
            patch(f"{_MODULE}.emit_safe"),
        ):
            again, again_preview = await propose_action("mark_step_done_and_resume", params, "guard cleared", ctx)
            second = await confirm_action(again, again_preview.fingerprint, ctx)

        assert again_preview.blocked == ""
        assert again_preview.lines[0] == f"Step validate of run #{run_id} is already marked done"
        assert second.status == "executed", second.reason
        mark.assert_not_awaited()
        started.assert_awaited_once_with("42", role="developer", resume_run_id=run_id, enforce_suitability=True)

    async def test_an_earlier_failure_that_was_retried_is_not_the_failed_step(self, ctx: ActionContext) -> None:
        async with await get_session() as session, session.begin():
            run = TaskRun(issue_number="42", role="developer", status="failed")
            session.add(run)
            await session.flush()
            for name, status in [("develop", "failed"), ("develop", "done"), ("validate", "failed")]:
                session.add(StepExecution(task_run_id=run.id, step_name=name, status=status))
                await session.flush()
            run_id = run.id
        found = await find_failed_step(run_id, project_dir=ctx.project_dir)
        assert found is not None
        assert found.step_name == "validate"

        assert (
            await mark_failed_step_done(
                run_id, step_name="validate", confirmed_by="octo", reason="fixed", project_dir=ctx.project_dir
            )
            is None
        )
        params = {"run_id": run_id, "step": "validate"}
        with patch("sova.dashboard.services.control_service.start_agent", AsyncMock()):
            _proposal, preview = await propose_action("mark_step_done_and_resume", params, "guard cleared", ctx)
        # The retried develop failure is history: the run resumes after validate, never asks to mark develop.
        assert (preview.blocked, preview.facts["failed_step"], preview.facts["resume_only"]) == ("", "", True)

    async def test_the_failure_summary_is_kept_after_the_note(self, ctx: ActionContext) -> None:
        run_id = await TestMarkStepDoneAndResume._seed()
        async with await get_session() as session, session.begin():
            step = (await session.execute(_step_query(run_id, "validate"))).scalar_one()
            step.output_summary = "ruff: 3 errors"
        assert (
            await mark_failed_step_done(
                run_id, step_name="validate", confirmed_by="octo", reason="fixed", project_dir=ctx.project_dir
            )
            is None
        )
        async with await get_session() as session:
            step = (await session.execute(_step_query(run_id, "validate"))).scalar_one()
        assert step.output_summary == "Marked done by octo: fixed\n\nOriginal: ruff: 3 errors"
        found = await find_failed_step(run_id, project_dir=ctx.project_dir)
        assert found is not None
        assert found.marked_step == "validate"

    async def test_the_step_update_runs_shielded_from_cancellation(self, ctx: ActionContext) -> None:
        import asyncio

        from sova.dashboard.services import agent_db

        run_id = await TestMarkStepDoneAndResume._seed()
        with patch.object(agent_db.asyncio, "shield", side_effect=asyncio.shield) as shield:
            refusal = await mark_failed_step_done(
                run_id, step_name="validate", confirmed_by="octo", reason="r", project_dir=ctx.project_dir
            )
        assert refusal is None
        shielded = [getattr(call.args[0], "__name__", "") for call in shield.call_args_list]
        assert "_mark_failed_step_done_tx" in shielded


# Review round 2: limits, attribution, sources, sanitized reasons, project binding --------


class TestNumberLimits:
    @pytest.mark.parametrize(
        ("kind", "params"),
        [
            pytest.param("stop_run", {"run_id": "9" * 19}, id="run-id-19-digits"),
            pytest.param("stop_run", {"run_id": 2**63}, id="run-id-past-int64"),
            pytest.param("stop_run", {"run_id": "1" + "0" * 5000}, id="run-id-past-int-digit-limit"),
            pytest.param("cleanup_after_run", {"issue": "1" + "0" * 5000}, id="issue-past-int-digit-limit"),
            pytest.param("cleanup_after_run", {"issue": 2**63}, id="issue-past-int64"),
            pytest.param("start_run", {"issue": "9" * 19}, id="start-issue-19-digits"),
        ],
    )
    def test_out_of_range_ids_are_rejected_with_a_reason(self, kind: str, params: dict) -> None:
        with pytest.raises(ProposalRejected):
            validate_proposal(kind, params, "why")

    def test_the_largest_id_is_accepted(self) -> None:
        assert validate_proposal("stop_run", {"run_id": "9" * 18}, "r").params == {"run_id": int("9" * 18)}

    @pytest.mark.parametrize(
        ("kind", "key", "expected"),
        [
            pytest.param("stop_run", "run_id", {"run_id": 1}, id="run-id"),
            pytest.param("cleanup_after_run", "issue", {"issue": "1"}, id="issue-ref"),
            pytest.param("reset_stuck_issue", "issue", {"issue": 1}, id="issue-number"),
        ],
    )
    def test_thousands_of_leading_zeros_never_reach_int_limits(self, kind: str, key: str, expected: dict) -> None:
        # int() counts leading zeros toward its 4300-digit limit, which would raise a plain ValueError.
        assert validate_proposal(kind, {key: "0" * 5000 + "1"}, "r").params == expected

    @pytest.mark.parametrize("kind", ["stop_run", "cleanup_after_run"])
    def test_only_zeros_is_rejected_with_a_reason(self, kind: str) -> None:
        key = "run_id" if kind == "stop_run" else "issue"
        with pytest.raises(ProposalRejected):
            validate_proposal(kind, {key: "0" * 5000}, "r")

    def test_a_long_value_is_shortened_in_the_reason(self) -> None:
        with pytest.raises(ProposalRejected) as raised:
            validate_proposal("stop_run", {"run_id": "x" * 500}, "r")
        assert len(str(raised.value)) < 120


@pytest.mark.usefixtures("reset_db_rows")
class TestMarkStepAttribution:
    async def test_no_configured_user_blocks_the_mark(self, tmp_path: Path) -> None:
        run_id = await TestMarkStepDoneAndResume._seed()
        anonymous = ActionContext(project_dir=tmp_path, github_user="")
        start = AsyncMock()
        with patch("sova.dashboard.services.control_service.start_agent", start):
            proposal, preview = await propose_action(
                "mark_step_done_and_resume", {"run_id": run_id, "step": "validate"}, "fixed", anonymous
            )
            result = await confirm_action(proposal, preview.fingerprint, anonymous)
        assert preview.blocked.startswith("github_user is not configured")
        assert result.status == "refused"
        start.assert_not_awaited()

    async def test_the_service_refuses_an_empty_confirming_user(self, tmp_path: Path) -> None:
        run_id = await TestMarkStepDoneAndResume._seed()
        refusal = await mark_failed_step_done(
            run_id, step_name="validate", confirmed_by=" ", reason="r", project_dir=tmp_path
        )
        assert refusal is not None
        assert "github_user" in refusal
        async with await get_session() as session:
            step = (await session.execute(_step_query(run_id, "validate"))).scalar_one()
        assert step.status == "failed"

    async def test_the_resume_starts_after_the_marked_step(self, ctx: ActionContext) -> None:
        """The checkpoint the resume loads counts the marked step as completed, so it is skipped."""

        from sova.cli.commands.run import _load_checkpoint
        from sova.core.steps.push import PushStep
        from sova.core.steps.validate import ValidateStep

        run_id = await TestMarkStepDoneAndResume._seed()
        assert "validate" not in (await _load_checkpoint(run_id, "42"))["completed_steps"]

        refusal = await mark_failed_step_done(
            run_id, step_name="validate", confirmed_by="octo", reason="fixed", project_dir=ctx.project_dir
        )

        assert refusal is None
        completed = (await _load_checkpoint(run_id, "42"))["completed_steps"]
        assert completed == {"develop", "validate"}
        resumed = SimpleNamespace(completed_steps=frozenset(completed))
        assert await ValidateStep().can_skip(resumed) is True
        assert await PushStep().can_skip(resumed) is False


class TestPreviewFailures:
    async def test_gh_missing_is_could_not_check_not_a_crash(self, ctx: ActionContext) -> None:
        with patch(f"{_MODULE}.run", AsyncMock(side_effect=FileNotFoundError("gh"))):
            assert await actions._gh_json(ctx.project_dir, "issue", "list") is None

    async def test_an_unexpected_preview_error_is_a_rejection_with_a_fixed_reason(self, ctx: ActionContext) -> None:
        with patch(f"{_MODULE}._run_status", AsyncMock(side_effect=RuntimeError("SELECT * FROM secret"))):
            with pytest.raises(ProposalRejected) as raised:
                await propose_action("stop_run", {"run_id": 5}, "stuck", ctx)
        assert "SELECT" not in str(raised.value)
        assert str(raised.value).endswith("see the server log")


@pytest.mark.usefixtures("github_project")
class TestOverlapRepository:
    async def test_the_overlap_search_uses_the_configured_repository(self, ctx: ActionContext) -> None:
        cfg = ProjectConfig(github_repo="acme/widgets")
        seen: list[tuple[str, ...]] = []

        async def gh(_project_dir: Path, *args: str) -> Any:
            seen.append(args)
            return []

        with patch(f"{_MODULE}.load_config", return_value=cfg), patch(f"{_MODULE}._gh_json", gh):
            await propose_action("file_issue", {"title": "t", "body": GOOD_BODY}, "r", ctx)
        assert seen
        assert all(args[:5] == ("issue", "list", "--repo", "acme/widgets", "--state") for args in seen)

    async def test_the_overlap_searches_run_concurrently(self, tmp_path: Path) -> None:
        import asyncio

        body = "Touches sova/a.py, sova/b.py and sova/c.py"
        in_flight = 0
        peak = 0

        async def gh(_project_dir: Path, *args: str) -> Any:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return []

        with patch(f"{_MODULE}._gh_json", gh):
            assert await actions._overlapping_issues(body, tmp_path, ()) == []
        assert peak == 3


class TestRedactedParams:
    async def test_the_draft_body_stays_out_of_logs_and_feed_metadata(self, ctx: ActionContext) -> None:
        assert actions._loggable({"title": "t", "body": "token=abc", "labels": []}) == {"title": "t", "labels": []}
        cfg = ProjectConfig()
        with (
            patch(f"{_MODULE}.load_config", return_value=cfg),
            patch(f"{_MODULE}._gh_json", _gh_json_stub(overlap=[])),
            patch("sova.adapters.create_adapter", return_value=_FakeAdapter()),
            patch("sova.git.board.add_issues_to_board", AsyncMock(return_value=[])),
            patch(f"{_MODULE}.emit_safe") as emit,
        ):
            proposal, preview = await propose_action("file_issue", {"title": "t", "body": GOOD_BODY}, "r", ctx)
            await confirm_action(proposal, preview.fingerprint, ctx)
        assert "body" not in emit.call_args.kwargs["metadata"]["params"]


class TestHandoffActionNames:
    def test_a_numeric_id_matches_its_text_form(self) -> None:
        handoff = {"next_actions": [{"id": 1, "label": "Review"}]}
        assert actions._handoff_action_ids(handoff) == ["1", "Review"]
        assert actions._selected_handoff_action(handoff, "1") == handoff["next_actions"][0]

    def test_an_unhashable_value_is_ignored_not_raised(self) -> None:
        handoff = {"next_actions": [{"id": ["x"], "action": {"a": 1}, "label": "Go"}]}
        assert actions._handoff_action_ids(handoff) == ["Go"]
        assert actions._selected_handoff_action(handoff, "x") is None


class TestCleanupExecute:
    async def test_execute_removes_only_the_previewed_targets(self, ctx: ActionContext) -> None:
        proposal = validate_proposal("cleanup_after_run", {"issue": 42}, "r")
        facts = {"worktrees": [["/wt/42", "feat/issue-42", "closed"]], "branches": [["feat/issue-42", "closed"]]}
        cleanup = AsyncMock(
            return_value=GCResult(removed_worktrees=[GCEntry(path="/wt/42", branch="", reason="closed")])
        )
        with patch("sova.git.worktree.cleanup_by_issue_state", cleanup):
            await actions._execute_cleanup(proposal, ctx, actions._Draft("s", (), "", facts))
        assert cleanup.await_args.kwargs["only"] == {"/wt/42", "feat/issue-42"}

    async def test_a_partial_failure_names_what_was_already_removed(self, ctx: ActionContext) -> None:
        proposal = validate_proposal("cleanup_after_run", {"issue": 42}, "r")
        gc = GCResult(
            removed_worktrees=[GCEntry(path="/wt/42", branch="feat/issue-42", reason="closed")],
            errors=["Failed to delete branch feat/issue-42: not merged"],
        )
        facts = {"worktrees": [["/wt/42", "feat/issue-42", "closed"]], "branches": [["feat/issue-42", "closed"]]}
        with patch("sova.git.worktree.cleanup_by_issue_state", AsyncMock(return_value=gc)):
            result = await actions._execute_cleanup(proposal, ctx, actions._Draft("s", (), "", facts))
        # Something was removed, so it is an executed action whose warning names what is gone and what failed.
        assert result[actions._WARNING_KEY] == (
            "Removed /wt/42; failed: Failed to delete branch feat/issue-42: not merged"
        )

    async def test_a_failure_that_removed_nothing_is_a_failed_action(self, ctx: ActionContext) -> None:
        proposal = validate_proposal("cleanup_after_run", {"issue": 42}, "r")
        gc = GCResult(errors=["Failed to remove worktree /wt/42: busy"])
        facts = {"worktrees": [["/wt/42", "feat/issue-42", "closed"]], "branches": []}
        with patch("sova.git.worktree.cleanup_by_issue_state", AsyncMock(return_value=gc)):
            with pytest.raises(ActionFailed, match="^Nothing was removed; failed: Failed to remove worktree"):
                await actions._execute_cleanup(proposal, ctx, actions._Draft("s", (), "", facts))

    async def test_a_run_that_only_skips_is_a_failed_action_not_an_executed_one(self, ctx: ActionContext) -> None:
        proposal = validate_proposal("cleanup_after_run", {"issue": 42}, "r")
        gc = GCResult(skipped=[GCEntry(path="/wt/42", branch="feat/issue-42", reason="it has uncommitted changes")])
        facts = {"worktrees": [["/wt/42", "feat/issue-42", "closed"]], "branches": []}
        with patch("sova.git.worktree.cleanup_by_issue_state", AsyncMock(return_value=gc)):
            with pytest.raises(ActionFailed, match="^Nothing was removed; skipped: /wt/42: it has uncommitted changes"):
                await actions._execute_cleanup(proposal, ctx, actions._Draft("s", (), "", facts))


class TestCleanupOpenIssueMergedPr:
    @pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
    async def test_a_squash_merged_branch_with_its_remote_deleted_is_cleaned_up(
        self, squash_merged_issue_repo: tuple[Path, Path], state: str
    ) -> None:
        repo, worktree = squash_merged_issue_repo
        tip = _git(repo, "rev-parse", "feat/issue-42").strip()
        ctx = ActionContext(project_dir=repo, github_user="")
        with (
            patch("sova.git.worktree.run", side_effect=_fake_gh(state, (tip,))),
            patch("sova.git.worktree.resolve_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
            patch(f"{_MODULE}.emit_safe"),
        ):
            proposal, preview = await propose_action("cleanup_after_run", {"issue": 42}, "merged, run failed", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)

        assert preview.lines == (f"Remove worktree {worktree} (feat/issue-42)", "Delete local branch feat/issue-42")
        assert result.status == "executed", result.reason
        assert not worktree.exists()
        assert _git(repo, "branch", "--list", "--format=%(refname:short)", "feat/issue-42") == ""

    async def test_local_commits_after_the_merged_pr_keep_the_branch(
        self, squash_merged_issue_repo: tuple[Path, Path]
    ) -> None:
        repo, worktree = squash_merged_issue_repo
        merged_head = _git(repo, "rev-parse", "feat/issue-42").strip()
        (worktree / "c.txt").write_text("c\n", encoding="utf-8")
        _git(worktree, "add", "c.txt")
        _git(worktree, "commit", "-q", "-m", "after the merge")
        ctx = ActionContext(project_dir=repo, github_user="")
        with (
            patch("sova.git.worktree.run", side_effect=_fake_gh("CLOSED", (merged_head,))),
            patch("sova.git.worktree.resolve_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
            patch(f"{_MODULE}.emit_safe"),
        ):
            proposal, preview = await propose_action("cleanup_after_run", {"issue": 42}, "merged, run failed", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)

        assert "Skip feat/issue-42: it has local commits after its merged pull request" in preview.lines
        assert result.status == "executed", result.reason
        assert not worktree.exists()  # the clean worktree of a closed issue goes; its commits stay on the branch
        assert _git(repo, "branch", "--list", "--format=%(refname:short)", "feat/issue-42").strip() == "feat/issue-42"

    async def test_merged_then_failed_integrate_with_the_issue_still_open_is_cleaned_up(
        self, merged_issue_repo: tuple[Path, Path]
    ) -> None:
        repo, worktree = merged_issue_repo
        ctx = ActionContext(project_dir=repo, github_user="")
        tip = _git(repo, "rev-parse", "feat/issue-42").strip()
        with (
            patch("sova.git.worktree.run", side_effect=_fake_gh("OPEN", (tip,))),
            patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
            patch(f"{_MODULE}.emit_safe"),
        ):
            proposal, preview = await propose_action("cleanup_after_run", {"issue": 42}, "merged, run failed", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)

        assert preview.lines == (f"Remove worktree {worktree} (feat/issue-42)", "Delete local branch feat/issue-42")
        assert result.status == "executed", result.reason
        assert not worktree.exists()
        assert _git(repo, "branch", "--list", "--format=%(refname:short)", "feat/issue-42") == ""


class TestRequestProject:
    def test_a_request_scoped_to_another_project_fails_the_confirm(self, ctx: ActionContext, tmp_path: Path) -> None:
        with patch("sova.dashboard.services.agent_pool.current_project_dir", return_value=tmp_path / "other"):
            with pytest.raises(ActionFailed, match="scoped to another project"):
                _REAL_REQUIRE_PROJECT(ctx)

    def test_the_matching_project_passes(self, ctx: ActionContext) -> None:
        with (
            patch("sova.dashboard.services.agent_pool.current_project_dir", return_value=ctx.project_dir),
            patch("sova.dashboard.services.handoff_service.current_project_dir", return_value=ctx.project_dir),
        ):
            _REAL_REQUIRE_PROJECT(ctx, handoffs=True)

    def test_a_foreign_handoff_store_fails_the_confirm(self, ctx: ActionContext, tmp_path: Path) -> None:
        with (
            patch("sova.dashboard.services.agent_pool.current_project_dir", return_value=ctx.project_dir),
            patch("sova.dashboard.services.handoff_service.current_project_dir", return_value=tmp_path / "other"),
        ):
            with pytest.raises(ActionFailed):
                _REAL_REQUIRE_PROJECT(ctx, handoffs=True)

    async def test_stop_runs_nothing_when_the_project_differs(self, ctx: ActionContext) -> None:
        stop = AsyncMock(return_value={"status": "stopped"})
        with (
            patch(f"{_MODULE}._run_status", AsyncMock(return_value="running")),
            patch(f"{_MODULE}._require_request_project", side_effect=ActionFailed("scoped to another project")),
            patch("sova.dashboard.services.control_service.stop_agent", stop),
            patch(f"{_MODULE}.emit_safe") as emit,
        ):
            proposal, preview = await propose_action("stop_run", {"run_id": 5}, "stuck", ctx)
            result = await confirm_action(proposal, preview.fingerprint, ctx)
        assert (result.status, result.reason) == ("failed", "scoped to another project")
        stop.assert_not_awaited()
        emit.assert_not_called()
