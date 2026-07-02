"""Tests for sova.roles -- role base class, built-in roles, and dispatcher."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig, RolesConfig
from sova.core.context import ExecutionContext
from sova.core.dag import DAGExecutor
from sova.core.state import TaskStatus
from sova.core.workflow import WorkflowEngine
from sova.db.session import close_db, init_db
from sova.roles.base import TaskAssessment


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for role tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _mock_adapter(state: TaskState = TaskState.BACKLOG) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_state.return_value = state
    adapter.get_task.return_value = Task(id="42", title="Test issue", body="Some description", state=state)
    return adapter


def _make_ctx(
    *,
    role: str = "developer",
    force: bool = False,
    state: TaskState = TaskState.BACKLOG,
    **kwargs,
) -> ExecutionContext:
    defaults = {
        "project_dir": Path("/tmp/test"),
        "config": ProjectConfig(),
        "adapter": _mock_adapter(state),
        "issue_number": "42",
        "role": role,
        "force": force,
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


# ---------------------------------------------------------------------------
# RoleResult
# ---------------------------------------------------------------------------


class TestRoleResult:
    def test_success_result(self) -> None:
        from sova.roles.base import RoleResult

        r = RoleResult(success=True, summary="Triaged issue")
        assert r.success
        assert r.summary == "Triaged issue"
        assert r.error is None
        assert r.output_state is None

    def test_failure_result(self) -> None:
        from sova.roles.base import RoleResult

        r = RoleResult(success=False, summary="Failed", error="Bad state")
        assert not r.success
        assert r.error == "Bad state"

    def test_result_with_output_state(self) -> None:
        from sova.roles.base import RoleResult

        r = RoleResult(success=True, summary="Done", output_state=TaskState.TRIAGED)
        assert r.output_state == TaskState.TRIAGED


# ---------------------------------------------------------------------------
# AgentRole base class
# ---------------------------------------------------------------------------


class TestAgentRoleBase:
    def test_validate_preconditions_passes_correct_state(self) -> None:
        from sova.roles.triage import TriageRole

        role = TriageRole()
        task = Task(id="1", title="Test", state=TaskState.BACKLOG)
        assert role.validate_preconditions(task)

    def test_validate_preconditions_rejects_wrong_state(self) -> None:
        from sova.roles.triage import TriageRole

        role = TriageRole()
        task = Task(id="1", title="Test", state=TaskState.IN_PROGRESS)
        assert not role.validate_preconditions(task)

    def test_validate_preconditions_with_force(self) -> None:
        from sova.roles.triage import TriageRole

        role = TriageRole()
        task = Task(id="1", title="Test", state=TaskState.IN_PROGRESS)
        assert role.validate_preconditions(task, force=True)


# ---------------------------------------------------------------------------
# Triage role
# ---------------------------------------------------------------------------


class TestTriageRole:
    def test_metadata(self) -> None:
        from sova.roles.triage import TriageRole

        role = TriageRole()
        assert role.name == "triage"
        assert TaskState.BACKLOG in role.allowed_input_states
        assert role.output_state == TaskState.TRIAGED

    async def test_execute_moves_to_triaged(self) -> None:
        from sova.roles.triage import TriageRole

        adapter = _mock_adapter(TaskState.BACKLOG)
        ctx = _make_ctx(role="triage", state=TaskState.BACKLOG, adapter=adapter)
        role = TriageRole()

        result = await role.execute(ctx)

        assert result.success
        assert result.output_state == TaskState.TRIAGED
        adapter.transition_state.assert_awaited_once_with("42", TaskState.TRIAGED)

    async def test_execute_rejects_wrong_state(self) -> None:
        from sova.roles.triage import TriageRole

        adapter = _mock_adapter(TaskState.IN_PROGRESS)
        ctx = _make_ctx(role="triage", state=TaskState.IN_PROGRESS, adapter=adapter)
        role = TriageRole()

        result = await role.execute(ctx)

        assert not result.success
        assert "precondition" in result.error.lower()

    async def test_execute_force_bypasses_state_check(self) -> None:
        from sova.roles.triage import TriageRole

        adapter = _mock_adapter(TaskState.IN_PROGRESS)
        ctx = _make_ctx(role="triage", state=TaskState.IN_PROGRESS, adapter=adapter, force=True)
        role = TriageRole()

        result = await role.execute(ctx)

        assert result.success

    async def test_appends_assessment_to_body(self) -> None:
        from sova.roles.triage import TriageRole

        adapter = _mock_adapter(TaskState.BACKLOG)
        ctx = _make_ctx(role="triage", state=TaskState.BACKLOG, adapter=adapter)
        role = TriageRole()

        await role.execute(ctx)

        adapter.edit_body.assert_awaited_once()
        updated_body = adapter.edit_body.call_args[0][1]
        assert "triage" in updated_body.lower() or "assessment" in updated_body.lower()


# ---------------------------------------------------------------------------
# Researcher role
# ---------------------------------------------------------------------------


class TestResearcherRole:
    def test_metadata(self) -> None:
        from sova.roles.researcher import ResearcherRole

        role = ResearcherRole()
        assert role.name == "researcher"
        assert TaskState.TRIAGED in role.allowed_input_states
        assert role.output_state == TaskState.RESEARCHED

    async def test_execute_moves_to_researched(self) -> None:
        from unittest.mock import patch

        from sova.core.workflow import WorkflowResult
        from sova.roles.researcher import ResearcherRole

        adapter = _mock_adapter(TaskState.TRIAGED)
        ctx = _make_ctx(role="researcher", state=TaskState.TRIAGED, adapter=adapter)
        role = ResearcherRole()

        mock_result = WorkflowResult(success=True, final_status=TaskStatus.DONE, task_run_id=1)
        with patch.object(WorkflowEngine, "run", new=AsyncMock(return_value=mock_result)):
            result = await role.execute(ctx)

        assert result.success
        assert result.output_state == TaskState.RESEARCHED
        adapter.transition_state.assert_awaited_once_with("42", TaskState.RESEARCHED)

    async def test_rejects_backlog_state(self) -> None:
        from sova.roles.researcher import ResearcherRole

        adapter = _mock_adapter(TaskState.BACKLOG)
        ctx = _make_ctx(role="researcher", state=TaskState.BACKLOG, adapter=adapter)
        role = ResearcherRole()

        result = await role.execute(ctx)

        assert not result.success

    async def test_force_bypasses_state_check(self) -> None:
        from unittest.mock import patch

        from sova.core.workflow import WorkflowResult
        from sova.roles.researcher import ResearcherRole

        adapter = _mock_adapter(TaskState.BACKLOG)
        ctx = _make_ctx(role="researcher", state=TaskState.BACKLOG, adapter=adapter, force=True)
        role = ResearcherRole()

        mock_result = WorkflowResult(success=True, final_status=TaskStatus.DONE, task_run_id=1)
        with patch.object(WorkflowEngine, "run", new=AsyncMock(return_value=mock_result)):
            result = await role.execute(ctx)

        assert result.success
        assert result.output_state == TaskState.RESEARCHED

    async def test_workflow_failure_returns_failure(self) -> None:
        from unittest.mock import patch

        from sova.core.workflow import WorkflowResult
        from sova.roles.researcher import ResearcherRole

        adapter = _mock_adapter(TaskState.TRIAGED)
        ctx = _make_ctx(role="researcher", state=TaskState.TRIAGED, adapter=adapter)
        role = ResearcherRole()

        mock_result = WorkflowResult(
            success=False, final_status=TaskStatus.FAILED, task_run_id=1, error="Research step failed"
        )
        with patch.object(WorkflowEngine, "run", new=AsyncMock(return_value=mock_result)):
            result = await role.execute(ctx)

        assert not result.success
        adapter.transition_state.assert_not_called()

    def test_get_steps_returns_researcher_pipeline(self) -> None:
        from sova.roles.researcher import ResearcherRole

        role = ResearcherRole()
        steps = role.get_steps()
        names = [s.name for s in steps]
        assert names == ["fetch_task", "research", "spec", "extract_memory"]


# ---------------------------------------------------------------------------
# Researcher pipeline steps
# ---------------------------------------------------------------------------


class TestFetchTaskStep:
    async def test_execute_populates_context(self) -> None:
        from sova.core.steps.fetch_task import FetchTaskStep

        adapter = _mock_adapter(TaskState.TRIAGED)
        ctx = _make_ctx(role="researcher", state=TaskState.TRIAGED, adapter=adapter)
        step = FetchTaskStep()

        result = await step.execute(ctx)

        assert result.success
        assert ctx.task is not None
        assert ctx.task.title == "Test issue"

    async def test_validate_output_passes_when_task_set(self) -> None:
        from sova.core.steps.fetch_task import FetchTaskStep

        ctx = _make_ctx(role="researcher", state=TaskState.TRIAGED)
        ctx.task = Task(id="42", title="Test", body="body", state=TaskState.TRIAGED)
        step = FetchTaskStep()

        gate = await step.validate_output(ctx)
        assert gate.passed

    async def test_validate_output_fails_when_task_missing(self) -> None:
        from sova.core.steps.fetch_task import FetchTaskStep

        ctx = _make_ctx(role="researcher", state=TaskState.TRIAGED)
        ctx.task = None
        step = FetchTaskStep()

        gate = await step.validate_output(ctx)
        assert not gate.passed


class TestResearchStep:
    async def test_execute_success(self) -> None:
        from decimal import Decimal
        from unittest.mock import patch

        from sova.core.steps.research import ResearchStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(role="researcher", state=TaskState.TRIAGED)
        step = ResearchStep()

        llm_result = LLMResult(
            text="done",
            model="sonnet",
            cost_usd=Decimal("0.02"),
            input_tokens=100,
            output_tokens=50,
        )
        with patch("sova.core.steps.research.invoke_command", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        assert result.cost_usd == Decimal("0.02")
        assert ctx.cost_usd == Decimal("0.02")

    async def test_execute_runtime_error(self) -> None:
        from unittest.mock import patch

        from sova.core.steps.research import ResearchStep

        ctx = _make_ctx(role="researcher", state=TaskState.TRIAGED)
        step = ResearchStep()

        with patch(
            "sova.core.steps.research.invoke_command",
            new_callable=AsyncMock,
            side_effect=RuntimeError("CLI failed"),
        ):
            result = await step.execute(ctx)

        assert not result.success
        assert "CLI failed" in result.error

    async def test_validate_output_passes_with_research_section(self) -> None:
        from sova.core.steps.research import ResearchStep

        adapter = _mock_adapter(TaskState.TRIAGED)
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="Some text\n\n## Research\n\nFindings here", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(role="researcher", state=TaskState.TRIAGED, adapter=adapter)
        step = ResearchStep()

        gate = await step.validate_output(ctx)
        assert gate.passed

    async def test_validate_output_fails_without_research_section(self) -> None:
        from sova.core.steps.research import ResearchStep

        adapter = _mock_adapter(TaskState.TRIAGED)
        adapter.get_task.return_value = Task(id="42", title="Test", body="No research here", state=TaskState.TRIAGED)
        ctx = _make_ctx(role="researcher", state=TaskState.TRIAGED, adapter=adapter)
        step = ResearchStep()

        gate = await step.validate_output(ctx)
        assert not gate.passed


# ---------------------------------------------------------------------------
# Developer role
# ---------------------------------------------------------------------------


class TestDeveloperRole:
    def test_metadata(self) -> None:
        from sova.roles.developer import DeveloperRole

        role = DeveloperRole()
        assert role.name == "developer"
        assert TaskState.RESEARCHED in role.allowed_input_states
        assert role.output_state == TaskState.IN_REVIEW

    def test_enforces_gate_3(self) -> None:
        """Developer must refuse non-Researched issues."""
        from sova.roles.developer import DeveloperRole

        role = DeveloperRole()
        task = Task(id="1", title="Test", state=TaskState.BACKLOG)
        assert not role.validate_preconditions(task)

        task_triaged = Task(id="1", title="Test", state=TaskState.TRIAGED)
        assert not role.validate_preconditions(task_triaged)

    def test_accepts_researched(self) -> None:
        from sova.roles.developer import DeveloperRole

        role = DeveloperRole()
        task = Task(id="1", title="Test", state=TaskState.RESEARCHED)
        assert role.validate_preconditions(task)

    def test_accepts_in_progress(self) -> None:
        """Allow resuming in-progress issues."""
        from sova.roles.developer import DeveloperRole

        role = DeveloperRole()
        task = Task(id="1", title="Test", state=TaskState.IN_PROGRESS)
        assert role.validate_preconditions(task)

    async def test_execute_rejects_non_researched(self) -> None:
        from sova.roles.developer import DeveloperRole

        adapter = _mock_adapter(TaskState.BACKLOG)
        ctx = _make_ctx(role="developer", state=TaskState.BACKLOG, adapter=adapter)
        role = DeveloperRole()

        result = await role.execute(ctx)

        assert not result.success
        assert "researched" in result.error.lower()

    async def test_execute_force_bypasses_gate_3(self) -> None:
        from unittest.mock import patch

        from sova.core.workflow import WorkflowResult
        from sova.roles.developer import DeveloperRole

        adapter = _mock_adapter(TaskState.BACKLOG)
        ctx = _make_ctx(role="developer", state=TaskState.BACKLOG, adapter=adapter, force=True)
        role = DeveloperRole()

        mock_result = WorkflowResult(success=True, final_status=TaskStatus.DONE, task_run_id=1)
        with patch.object(WorkflowEngine, "run", new=AsyncMock(return_value=mock_result)):
            result = await role.execute(ctx)

        assert result.success

    async def test_execute_transitions_to_in_progress(self) -> None:
        """Developer must move issue to IN_PROGRESS on the tracker before running steps."""
        from unittest.mock import patch

        from sova.core.workflow import WorkflowResult
        from sova.roles.developer import DeveloperRole

        adapter = _mock_adapter(TaskState.RESEARCHED)
        ctx = _make_ctx(role="developer", state=TaskState.RESEARCHED, adapter=adapter)
        role = DeveloperRole()

        mock_result = WorkflowResult(success=True, final_status=TaskStatus.DONE, task_run_id=1)
        with patch.object(WorkflowEngine, "run", new=AsyncMock(return_value=mock_result)):
            result = await role.execute(ctx)

        assert result.success
        adapter.transition_state.assert_called_once_with("42", TaskState.IN_PROGRESS)

    async def test_execute_routes_to_address_review_when_pr_number_set(self) -> None:
        """pr_number alone should route to address-review pipeline, regardless of issue state."""
        from unittest.mock import patch

        from sova.core.workflow import WorkflowResult
        from sova.roles.developer import DeveloperRole

        adapter = _mock_adapter(TaskState.IN_PROGRESS)
        ctx = _make_ctx(role="developer", state=TaskState.IN_PROGRESS, adapter=adapter, pr_number=88)
        role = DeveloperRole()

        mock_result = WorkflowResult(success=True, final_status=TaskStatus.DONE, task_run_id=1)
        with patch.object(WorkflowEngine, "run", new=AsyncMock(return_value=mock_result)):
            result = await role.execute(ctx)

        assert result.success
        assert result.output_state == TaskState.IN_REVIEW
        adapter.transition_state.assert_not_called()

    async def test_address_review_discovers_branch_from_pr(self) -> None:
        """Address-review should discover branch_name from the PR when not set."""
        from unittest.mock import patch

        from sova.core.workflow import WorkflowResult
        from sova.roles.developer import DeveloperRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="developer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=88)
        assert ctx.branch_name == ""
        role = DeveloperRole()

        mock_result = WorkflowResult(success=True, final_status=TaskStatus.DONE, task_run_id=1)
        with (
            patch.object(WorkflowEngine, "run", new=AsyncMock(return_value=mock_result)),
            patch(
                "sova.roles.developer.get_pr_branch",
                new_callable=AsyncMock,
                return_value="feat/issue-42",
            ),
        ):
            result = await role.execute(ctx)

        assert result.success
        assert ctx.branch_name == "feat/issue-42"

    async def test_address_review_discovers_existing_worktree(self, tmp_path: Path) -> None:
        """Address-review should discover and use an existing worktree for the issue."""
        from unittest.mock import patch

        from sova.core.workflow import WorkflowResult
        from sova.roles.developer import DeveloperRole

        worktree_path = tmp_path / ".claude" / "worktrees" / "42"
        worktree_path.mkdir(parents=True)

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(
            role="developer",
            state=TaskState.IN_REVIEW,
            adapter=adapter,
            pr_number=88,
            project_dir=tmp_path,
        )
        ctx.branch_name = "feat/issue-42"
        assert ctx.worktree_dir is None
        role = DeveloperRole()

        mock_result = WorkflowResult(success=True, final_status=TaskStatus.DONE, task_run_id=1)
        with patch.object(WorkflowEngine, "run", new=AsyncMock(return_value=mock_result)):
            result = await role.execute(ctx)

        assert result.success
        assert ctx.worktree_dir == worktree_path

    async def test_execute_routes_to_development_without_pr_number(self) -> None:
        """Without pr_number, developer should run the full development pipeline."""
        from unittest.mock import patch

        from sova.core.workflow import WorkflowResult
        from sova.roles.developer import DeveloperRole

        adapter = _mock_adapter(TaskState.RESEARCHED)
        ctx = _make_ctx(role="developer", state=TaskState.RESEARCHED, adapter=adapter)
        role = DeveloperRole()

        mock_result = WorkflowResult(success=True, final_status=TaskStatus.DONE, task_run_id=1)
        with patch.object(WorkflowEngine, "run", new=AsyncMock(return_value=mock_result)):
            result = await role.execute(ctx)

        assert result.success
        adapter.transition_state.assert_called_once_with("42", TaskState.IN_PROGRESS)

    def test_get_steps_returns_developer_pipeline(self) -> None:
        from sova.roles.developer import DeveloperRole

        role = DeveloperRole()
        steps = role.get_steps()
        names = [s.name for s in steps]
        assert "sync" in names
        assert "develop" in names
        assert "push" in names
        assert "create_pr" in names
        assert "handoff_to_reviewer" in names


# ---------------------------------------------------------------------------
# Reviewer role
# ---------------------------------------------------------------------------


class TestReviewerRole:
    def test_metadata(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        role = ReviewerRole()
        assert role.name == "reviewer"
        assert TaskState.IN_REVIEW in role.allowed_input_states

    async def test_execute_reviews_pr(self) -> None:
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)
        role = ReviewerRole()
        llm_resp = json.dumps({"findings": [], "summary": "OK"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        with (
            patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value="feat/issue-42"),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            result = await role.execute(ctx)

        assert result.success

    async def test_execute_discovers_pr_when_not_provided(self) -> None:
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.git.operations import PRInfo
        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter)
        role = ReviewerRole()
        llm_resp = json.dumps({"findings": [], "summary": "OK"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        with (
            patch(
                "sova.roles.reviewer.find_pr_for_issue",
                new_callable=AsyncMock,
                return_value=PRInfo(number=82, url="https://github.com/x/y/pull/82", branch="feat/issue-42"),
            ),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            result = await role.execute(ctx)

        assert result.success
        assert ctx.pr_number == 82
        assert ctx.branch_name == "feat/issue-42"

    async def test_reviewer_discovers_branch_when_pr_number_preset(self) -> None:
        """When pr_number is already set, reviewer should still discover branch_name."""
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)
        assert ctx.branch_name == ""
        role = ReviewerRole()
        llm_resp = json.dumps({"findings": [], "summary": "OK"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        with (
            patch(
                "sova.roles.reviewer.get_pr_branch",
                new_callable=AsyncMock,
                return_value="feat/issue-42",
            ),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            result = await role.execute(ctx)

        assert result.success
        assert ctx.branch_name == "feat/issue-42"

    async def test_reviewer_propagates_branch_name_in_handoff(self) -> None:
        """Reviewer must include branch_name in handoff so address-review can find the worktree."""
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.git.operations import PRInfo
        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter)
        ctx.task_run_id = 1
        role = ReviewerRole()

        findings = [{"file": "x.py", "severity": 5, "category": "bug", "description": "Bad"}]
        llm_resp = json.dumps({"findings": findings, "summary": "Found a bug"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        with (
            patch(
                "sova.roles.reviewer.find_pr_for_issue",
                new_callable=AsyncMock,
                return_value=PRInfo(number=82, url="https://github.com/x/y/pull/82", branch="feat/issue-42"),
            ),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["x.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock) as mock_db_handoff,
            patch("sova.roles.reviewer.write_handoff_file") as mock_file_handoff,
        ):
            await role.execute(ctx)

        db_handoff = mock_db_handoff.call_args[0][1]
        assert db_handoff.branch_name == "feat/issue-42"

        file_handoff = mock_file_handoff.call_args[0][1]
        assert file_handoff.branch == "feat/issue-42"

    async def test_execute_fails_when_no_pr_found(self) -> None:
        from unittest.mock import patch

        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter)
        role = ReviewerRole()

        with patch(
            "sova.roles.reviewer.find_pr_for_issue",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await role.execute(ctx)

        assert not result.success
        assert "no linked pr" in result.error.lower()

    async def test_execute_branch_discovery_failure_non_fatal(self) -> None:
        """Branch discovery failure should not prevent the review from proceeding."""
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)
        ctx.branch_name = ""
        role = ReviewerRole()
        llm_resp = json.dumps({"findings": [], "summary": "OK"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        with (
            patch(
                "sova.roles.reviewer.get_pr_branch",
                new_callable=AsyncMock,
                side_effect=RuntimeError("gh failed"),
            ),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            result = await role.execute(ctx)

        assert result.success

    async def test_execute_extract_memory_failure_non_fatal(self) -> None:
        """Memory extraction failure should not prevent reviewer from returning success."""
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)
        role = ReviewerRole()
        llm_resp = json.dumps({"findings": [], "summary": "OK"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        with (
            patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value="feat/x"),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
            patch(
                "sova.knowledge.extraction.extract_memories",
                new_callable=AsyncMock,
                side_effect=RuntimeError("extraction failed"),
            ),
        ):
            result = await role.execute(ctx)

        assert result.success

    async def test_execute_handoff_db_failure_non_fatal(self) -> None:
        """DB handoff write failure should not prevent reviewer from returning success."""
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)
        ctx.task_run_id = 1
        role = ReviewerRole()
        llm_resp = json.dumps({"findings": [], "summary": "OK"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        with (
            patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value="feat/x"),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch(
                "sova.roles.reviewer.write_handoff",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB write failed"),
            ),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            result = await role.execute(ctx)

        assert result.success

    async def test_execute_handoff_file_failure_non_fatal(self) -> None:
        """File handoff write failure should not prevent reviewer from returning success."""
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)
        role = ReviewerRole()
        llm_resp = json.dumps({"findings": [], "summary": "OK"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        with (
            patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value="feat/x"),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch(
                "sova.roles.reviewer.write_handoff_file",
                side_effect=OSError("disk full"),
            ),
        ):
            result = await role.execute(ctx)

        assert result.success

    async def test_execute_review_rationale_failure_non_fatal(self) -> None:
        """Review rationale spec append failure should not prevent success."""
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)
        role = ReviewerRole()
        findings = [{"file": "x.py", "severity": 5, "category": "bug", "description": "Issue"}]
        llm_resp = json.dumps({"findings": findings, "summary": "Found issue"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        with (
            patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value="feat/x"),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["x.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
            patch(
                "sova.core.steps._spec_helpers.append_spec_section",
                side_effect=OSError("spec write failed"),
            ),
        ):
            result = await role.execute(ctx)

        assert result.success
        assert "1 findings" in result.summary

    async def test_execute_posts_inline_review_comments(self) -> None:
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)
        role = ReviewerRole()

        findings = [
            {
                "file": "foo.py",
                "line": 2,
                "severity": 7,
                "category": "bug",
                "description": "Bug here",
                "suggestion": "Fix it",
            }
        ]
        llm_resp = json.dumps({"findings": findings, "summary": "Found a bug"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        real_diff = (
            "diff --git a/foo.py b/foo.py\n"
            "index 000..111 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "+new line\n"
            " line3\n"
            " line4\n"
        )

        with (
            patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value=""),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value=real_diff),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["foo.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            result = await role.execute(ctx)

        assert result.success
        adapter.post_pr_review.assert_called_once()
        call_kwargs = adapter.post_pr_review.call_args
        assert call_kwargs[1]["event"] == "COMMENT"
        comments = call_kwargs[1]["comments"]
        assert len(comments) == 1
        assert comments[0]["path"] == "foo.py"
        assert comments[0]["line"] == 2
        adapter.post_pr_comment.assert_not_called()

    async def test_execute_falls_back_to_comment_on_review_failure(self) -> None:
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        adapter.post_pr_review.side_effect = RuntimeError("API failed")
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)
        role = ReviewerRole()

        llm_resp = json.dumps({"findings": [], "summary": "OK"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        with (
            patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value=""),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            result = await role.execute(ctx)

        assert result.success
        adapter.post_pr_comment.assert_called_once()

    async def test_reviewer_retries_body_only_before_comment_fallback(self) -> None:
        """When post_pr_review fails with inline comments, reviewer retries without them."""
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        call_count = 0

        async def fail_with_inline_succeed_without(pr_number, body, event, comments):
            nonlocal call_count
            call_count += 1
            if comments:
                raise RuntimeError("Inline comments failed")

        adapter.post_pr_review = AsyncMock(side_effect=fail_with_inline_succeed_without)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)
        role = ReviewerRole()

        findings = [{"file": "foo.py", "line": 2, "severity": 6, "category": "bug", "description": "Issue"}]
        llm_resp = json.dumps({"findings": findings, "summary": "Found issue"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        real_diff = (
            "diff --git a/foo.py b/foo.py\n"
            "index 000..111 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "+new line\n"
            " line3\n"
            " line4\n"
        )

        with (
            patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value=""),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value=real_diff),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["foo.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            result = await role.execute(ctx)

        assert result.success
        assert call_count == 2
        second_call = adapter.post_pr_review.call_args_list[1]
        assert second_call[1]["comments"] == []
        adapter.post_pr_comment.assert_not_called()

    async def test_reviewer_falls_back_to_comment_when_body_only_also_fails(self) -> None:
        """Full fallback: inline fails -> body-only fails -> issue comment posted."""
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        adapter.post_pr_review = AsyncMock(side_effect=RuntimeError("API down"))
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)
        role = ReviewerRole()

        findings = [{"file": "foo.py", "line": 2, "severity": 6, "category": "bug", "description": "Issue"}]
        llm_resp = json.dumps({"findings": findings, "summary": "Found issue"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        real_diff = (
            "diff --git a/foo.py b/foo.py\n"
            "index 000..111 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "+new line\n"
            " line3\n"
            " line4\n"
        )

        with (
            patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value=""),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value=real_diff),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["foo.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            result = await role.execute(ctx)

        assert result.success
        assert adapter.post_pr_review.call_count == 2
        adapter.post_pr_comment.assert_called_once()

    async def test_rejects_wrong_state(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.BACKLOG)
        ctx = _make_ctx(role="reviewer", state=TaskState.BACKLOG, adapter=adapter)
        role = ReviewerRole()

        result = await role.execute(ctx)

        assert not result.success

    async def test_execute_includes_spec_in_review(self) -> None:
        """When a spec file exists, review prompt includes spec context."""
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)

        findings = [
            {
                "file": "x.py",
                "line": 10,
                "severity": 6,
                "category": "spec_alignment",
                "description": "Implementation deviates from spec",
                "suggestion": "Follow the spec",
            }
        ]
        llm_resp = json.dumps({"findings": findings, "summary": "Spec deviation found"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        spec_path = ctx.project_dir / ".claude" / "specs"
        spec_path.mkdir(parents=True, exist_ok=True)
        spec_file = spec_path / "42-test.md"
        spec_file.write_text(
            "# Spec: Test\n\n## Solution\nDo X and Y.\n\n## Edge Cases\nHandle Z.\n\n## Other\nIgnored."
        )

        captured_prompt = None

        async def _capture_invoke(prompt, **kwargs):
            nonlocal captured_prompt
            captured_prompt = prompt
            return llm_result

        try:
            with (
                patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value="feat/issue-42"),
                patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
                patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["x.py"]),
                patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, side_effect=_capture_invoke),
                patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
                patch("sova.roles.reviewer.write_handoff_file"),
            ):
                role = ReviewerRole()
                result = await role.execute(ctx)
        finally:
            spec_file.unlink(missing_ok=True)
            spec_path.rmdir()

        assert result.success
        assert captured_prompt is not None
        assert "Spec Context" in captured_prompt
        assert "Do X and Y." in captured_prompt
        assert "Handle Z." in captured_prompt
        assert "Spec alignment" in captured_prompt
        assert "spec_alignment" in captured_prompt

    async def test_execute_without_spec_is_backward_compatible(self) -> None:
        """When no spec file exists, review works normally without spec context."""
        import json
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)
        llm_resp = json.dumps({"findings": [], "summary": "OK"})
        llm_result = LLMResult(text=llm_resp, model="sonnet", cost_usd=Decimal("0"))

        captured_prompt = None

        async def _capture_invoke(prompt, **kwargs):
            nonlocal captured_prompt
            captured_prompt = prompt
            return llm_result

        with (
            patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value="feat/issue-42"),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, side_effect=_capture_invoke),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            role = ReviewerRole()
            result = await role.execute(ctx)

        assert result.success
        assert captured_prompt is not None
        assert "Spec Context" not in captured_prompt
        assert "Spec alignment" not in captured_prompt


# ---------------------------------------------------------------------------
# Spec-anchored review helpers
# ---------------------------------------------------------------------------


class TestSpecAnchoredReview:
    def test_extract_spec_sections_complete(self) -> None:
        from sova.roles.reviewer import _extract_spec_sections

        raw = (
            "# Spec: Test\n\n"
            "## Solution\nDo A and B.\n\n"
            "## Edge Cases\nHandle C.\n\n"
            "## Design Decisions\nUse pattern D.\n\n"
            "## Scope Boundaries\nExcludes E.\n\n"
            "## Other\nIgnored.\n"
        )
        sections = _extract_spec_sections(raw)
        assert "Solution" in sections
        assert "Do A and B." in sections["Solution"]
        assert "Edge Cases" in sections
        assert "Handle C." in sections["Edge Cases"]
        assert "Design Decisions" in sections
        assert "Use pattern D." in sections["Design Decisions"]
        assert "Scope Boundaries" in sections
        assert "Excludes E." in sections["Scope Boundaries"]
        assert "Other" not in sections

    def test_extract_spec_sections_partial(self) -> None:
        from sova.roles.reviewer import _extract_spec_sections

        raw = "# Spec\n\n## Solution\nDo X.\n\n## Implementation\nStep 1.\n"
        sections = _extract_spec_sections(raw)
        assert "Solution" in sections
        assert len(sections) == 1

    def test_extract_spec_sections_empty(self) -> None:
        from sova.roles.reviewer import _extract_spec_sections

        sections = _extract_spec_sections("# Spec\n\nJust some text.")
        assert sections == {}

    def test_build_review_prompt_with_spec(self) -> None:
        from sova.roles.reviewer import _build_review_prompt

        task = Task(id="1", title="Test", body="desc", state=TaskState.BACKLOG)
        spec_sections = {"Solution": "Do X.", "Edge Cases": "Handle Y."}
        prompt = _build_review_prompt(task, "diff", ["a.py"], spec_sections=spec_sections)

        assert "## Spec Context" in prompt
        assert "### Solution" in prompt
        assert "Do X." in prompt
        assert "### Edge Cases" in prompt
        assert "Handle Y." in prompt
        assert "9. **Spec alignment**" in prompt
        assert "spec_alignment" in prompt

    def test_build_review_prompt_without_spec(self) -> None:
        from sova.roles.reviewer import _build_review_prompt

        task = Task(id="1", title="Test", body="desc", state=TaskState.BACKLOG)
        prompt = _build_review_prompt(task, "diff", ["a.py"])

        assert "Spec Context" not in prompt
        assert "spec_alignment" not in prompt
        assert "Spec alignment" not in prompt
        # 8 categories present, no 9th
        assert "8. **Docs**" in prompt

    def test_build_review_prompt_with_addressed_findings(self) -> None:
        from sova.roles.reviewer import _build_review_prompt

        task = Task(id="1", title="Test", body="desc", state=TaskState.BACKLOG)
        addressed = [
            {
                "source": "sonarcloud",
                "severity": "MAJOR",
                "file_path": "sova/app.py",
                "tool_id": "S1192",
                "message": "Unused import",
            },
            {
                "source": "sonarcloud",
                "severity": "coverage",
                "file_path": "project-wide",
                "tool_id": "",
                "message": "Coverage gap remediation applied",
            },
            {
                "source": "coderabbit",
                "severity": "MAJOR",
                "file_path": "sova/cli.py",
                "tool_id": "",
                "message": "Bug found",
            },
        ]
        prompt = _build_review_prompt(task, "diff", ["a.py"], addressed_findings=addressed)

        assert "## Already Addressed by Static Tools" in prompt
        assert "sonarcloud (2 findings)" in prompt
        assert "coderabbit (1 finding)" in prompt
        assert "[S1192]" in prompt
        assert "Unused import" in prompt
        assert "Coverage gap remediation applied" in prompt
        assert "complementary dimensions" in prompt

    def test_build_review_prompt_without_addressed_findings(self) -> None:
        from sova.roles.reviewer import _build_review_prompt

        task = Task(id="1", title="Test", body="desc", state=TaskState.BACKLOG)
        prompt = _build_review_prompt(task, "diff", ["a.py"], addressed_findings=[])

        assert "Already Addressed" not in prompt

    def test_format_addressed_findings_empty(self) -> None:
        from sova.roles.reviewer import _format_addressed_findings

        assert _format_addressed_findings([]) == ""
        assert _format_addressed_findings(None) == ""

    def test_format_addressed_findings_groups_by_source(self) -> None:
        from sova.roles.reviewer import _format_addressed_findings

        findings = [
            {"source": "sonarcloud", "severity": "MAJOR", "file_path": "a.py", "tool_id": "S1", "message": "Issue A"},
            {"source": "coderabbit", "severity": "HIGH", "file_path": "b.py", "tool_id": "", "message": "Issue B"},
            {"source": "sonarcloud", "severity": "MINOR", "file_path": "c.py", "tool_id": "S2", "message": "Issue C"},
        ]
        result = _format_addressed_findings(findings)
        assert "coderabbit (1 finding)" in result
        assert "sonarcloud (2 findings)" in result
        assert "[S1]" in result
        assert "[S2]" in result
        # No tool_id tag for coderabbit entry with empty tool_id
        assert "[HIGH]" in result
        assert "Issue B" in result
        assert "`a.py`" in result
        assert "`b.py`" in result

    async def test_load_addressed_findings_from_file(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file
        from sova.roles.reviewer import ReviewerRole

        findings = [{"source": "sonarcloud", "severity": "MAJOR", "file_path": "a.py", "tool_id": "S1", "message": "X"}]
        h = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            summary="Done",
            issue="42",
            details={"addressed_findings": findings},
        )
        write_handoff_file(tmp_path, h)

        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW)
        ctx.project_dir = tmp_path
        ctx.issue_number = "42"

        role = ReviewerRole()
        result = await role._load_addressed_findings(ctx)
        assert len(result) == 1
        assert result[0]["source"] == "sonarcloud"

    async def test_load_addressed_findings_from_resume_run(self) -> None:
        from sova.db.models import TaskRun
        from sova.db.session import get_session
        from sova.ipc.handoff import AgentHandoff, write_handoff
        from sova.roles.reviewer import ReviewerRole

        session = await get_session()
        async with session.begin():
            tr = TaskRun(issue_number="42", role="developer", status="done")
            session.add(tr)
            await session.flush()
            run_id = tr.id

        findings = [{"source": "sonarcloud", "severity": "MAJOR", "file_path": "a.py", "tool_id": "S1", "message": "X"}]
        handoff = AgentHandoff(
            role="developer", phase="develop", summary="Done",
            next_action="review", branch_name="feat/42",
            addressed_findings=findings,
        )
        await write_handoff(run_id, handoff)

        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW)
        ctx.resume_run_id = run_id
        ctx.project_dir = Path("/nonexistent")  # force file source to fail

        role = ReviewerRole()
        result = await role._load_addressed_findings(ctx)
        assert len(result) == 1
        assert result[0]["source"] == "sonarcloud"

    async def test_load_addressed_findings_from_db_skips_empty_runs(self) -> None:
        from sova.db.models import TaskRun
        from sova.db.session import get_session
        from sova.roles.reviewer import ReviewerRole

        session = await get_session()
        async with session.begin():
            # Older run WITH addressed_findings
            findings_data = [
                {"source": "sonarcloud", "severity": "MAJOR", "file_path": "a.py", "tool_id": "S1", "message": "Found"},
            ]
            tr1 = TaskRun(
                issue_number="42", role="developer", status="done",
                handoff_json={"addressed_findings": findings_data},
            )
            session.add(tr1)
            # Newer run WITHOUT addressed_findings (address-review cycle)
            tr2 = TaskRun(
                issue_number="42", role="developer", status="done",
                handoff_json={"some_other_key": "value"},
            )
            session.add(tr2)

        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW)
        ctx.issue_number = "42"
        ctx.project_dir = Path("/nonexistent")

        role = ReviewerRole()
        result = await role._load_addressed_findings(ctx)
        assert len(result) == 1
        assert result[0]["message"] == "Found"

    async def test_load_addressed_findings_empty_issue(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW)
        ctx.issue_number = ""
        ctx.project_dir = Path("/nonexistent")

        role = ReviewerRole()
        result = await role._load_addressed_findings(ctx)
        assert result == []

    async def test_load_addressed_findings_all_sources_fail(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW)
        ctx.issue_number = "999"
        ctx.project_dir = Path("/nonexistent")

        role = ReviewerRole()
        result = await role._load_addressed_findings(ctx)
        assert result == []

    def test_spec_alignment_finding_parses(self) -> None:
        import json

        from sova.roles.reviewer import _parse_findings

        text = json.dumps(
            {
                "findings": [
                    {
                        "file": "x.py",
                        "line": 10,
                        "severity": 6,
                        "category": "spec_alignment",
                        "description": "Deviates from spec",
                        "suggestion": "Follow spec",
                    }
                ],
                "summary": "Spec deviation",
            }
        )
        findings, summary = _parse_findings(text)
        assert len(findings) == 1
        assert findings[0].category == "spec_alignment"
        assert findings[0].severity == 6

    def test_load_spec_sections_no_spec(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, pr_number=99)
        role = ReviewerRole()
        result = role._load_spec_sections(ctx)
        assert result is None

    def test_load_spec_sections_non_numeric_issue(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, pr_number=99, issue_number="pr-42")
        role = ReviewerRole()
        result = role._load_spec_sections(ctx)
        assert result is None

    def test_load_spec_sections_uses_working_dir(self, tmp_path: Path) -> None:
        from sova.roles.reviewer import ReviewerRole

        # Create a spec in the working_dir
        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec_file = specs_dir / "42-test.md"
        spec_file.write_text("## Solution\nDo X.\n\n## Edge Cases\nHandle Y.\n")

        ctx = _make_ctx(
            role="reviewer",
            state=TaskState.IN_REVIEW,
            pr_number=99,
            worktree_dir=tmp_path,
            project_dir=Path("/nonexistent"),
        )
        role = ReviewerRole()
        result = role._load_spec_sections(ctx)
        assert result is not None
        assert "Solution" in result
        assert "Do X." in result["Solution"]

    def test_load_spec_sections_exception_returns_none(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.roles.reviewer import ReviewerRole

        ctx = _make_ctx(
            role="reviewer",
            state=TaskState.IN_REVIEW,
            pr_number=99,
            worktree_dir=tmp_path,
        )
        role = ReviewerRole()
        with patch("sova.roles.reviewer.find_spec_file", side_effect=OSError("disk error")):
            result = role._load_spec_sections(ctx)
        assert result is None

    def test_compact_spec_ref_none(self) -> None:
        from sova.roles.reviewer import _compact_spec_ref

        assert _compact_spec_ref(None) is None
        assert _compact_spec_ref({}) is None

    def test_compact_spec_ref_short_content(self) -> None:
        from sova.roles.reviewer import _compact_spec_ref

        sections = {"Solution": "Short text.", "Edge Cases": "Also short."}
        result = _compact_spec_ref(sections)
        assert result == sections

    def test_compact_spec_ref_truncates_long_content(self) -> None:
        from sova.roles.reviewer import _compact_spec_ref

        long_content = "A" * 500
        sections = {"Solution": long_content, "Edge Cases": "Short."}
        result = _compact_spec_ref(sections)
        assert result is not None
        assert len(result["Solution"]) < len(long_content)
        assert result["Solution"].endswith("... (see full spec in chunk 1)")
        assert result["Edge Cases"] == "Short."

    def test_run_review_uses_compact_spec_for_subsequent_chunks(self) -> None:
        """Verify that multi-chunk reviews send full spec only for chunk 1."""
        from unittest.mock import patch

        from sova.roles.reviewer import DIFF_CHUNK_SIZE, ReviewerRole

        role = ReviewerRole()
        task = Task(id="1", title="Test", body="desc", state=TaskState.IN_REVIEW)
        adapter = _mock_adapter(TaskState.IN_REVIEW)

        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99)

        spec = {"Solution": "X" * 500, "Edge Cases": "Short."}
        # Build a diff with 2 file boundaries so _chunk_diff splits it
        chunk1 = "diff --git a/a.py b/a.py\n" + "+" * DIFF_CHUNK_SIZE
        chunk2 = "\ndiff --git a/b.py b/b.py\n" + "+" * 100
        large_diff = chunk1 + chunk2

        captured_prompts: list[str] = []

        async def _capture_invoke(prompt: str, **kwargs):
            captured_prompts.append(prompt)
            from decimal import Decimal as Dec

            from sova.llm.models import LLMResult

            return LLMResult(
                text='{"findings": [], "summary": "ok"}',
                cost_usd=Dec("0.01"),
            )

        import asyncio

        with (
            patch.object(role, "_load_spec_sections", return_value=spec),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, side_effect=_capture_invoke),
        ):
            asyncio.get_event_loop().run_until_complete(role._run_review(ctx, task, large_diff, ["a.py"]))

        assert len(captured_prompts) == 2
        # First chunk has full spec content
        assert "X" * 500 in captured_prompts[0]
        # Second chunk has truncated spec
        assert "... (see full spec in chunk 1)" in captured_prompts[1]


# ---------------------------------------------------------------------------
# Role dispatcher
# ---------------------------------------------------------------------------


class TestRoleDispatcher:
    def test_get_role_by_name(self) -> None:
        from sova.roles.dispatcher import get_role

        role = get_role("triage")
        assert role.name == "triage"

        role = get_role("developer")
        assert role.name == "developer"

    def test_get_role_unknown_raises(self) -> None:
        from sova.roles.dispatcher import get_role

        with pytest.raises(ValueError, match="Unknown role"):
            get_role("nonexistent")

    def test_get_role_by_nickname(self) -> None:
        from sova.roles.dispatcher import get_role

        config = RolesConfig(nicknames={"dev": "developer", "tri": "triage"})
        role = get_role("dev", config=config)
        assert role.name == "developer"

    def test_resolve_role_from_state_backlog(self) -> None:
        from sova.roles.dispatcher import resolve_role_for_state

        role = resolve_role_for_state(TaskState.BACKLOG)
        assert role.name == "triage"

    def test_resolve_role_from_state_triaged(self) -> None:
        from sova.roles.dispatcher import resolve_role_for_state

        role = resolve_role_for_state(TaskState.TRIAGED)
        assert role.name == "researcher"

    def test_resolve_role_from_state_researched(self) -> None:
        from sova.roles.dispatcher import resolve_role_for_state

        role = resolve_role_for_state(TaskState.RESEARCHED)
        assert role.name == "developer"

    def test_resolve_role_from_state_in_review(self) -> None:
        from sova.roles.dispatcher import resolve_role_for_state

        role = resolve_role_for_state(TaskState.IN_REVIEW)
        assert role.name == "reviewer"

    def test_resolve_role_from_state_in_progress(self) -> None:
        from sova.roles.dispatcher import resolve_role_for_state

        role = resolve_role_for_state(TaskState.IN_PROGRESS)
        assert role.name == "developer"

    def test_resolve_role_from_done_raises(self) -> None:
        from sova.roles.dispatcher import resolve_role_for_state

        with pytest.raises(ValueError, match="No role"):
            resolve_role_for_state(TaskState.DONE)

    def test_list_roles(self) -> None:
        from sova.roles.dispatcher import list_roles

        roles = list_roles()
        names = {r.name for r in roles}
        assert names == {"triage", "researcher", "developer", "reviewer", "planner"}

    async def test_dispatch_auto_selects_role(self) -> None:
        from sova.roles.dispatcher import dispatch

        adapter = _mock_adapter(TaskState.BACKLOG)
        ctx = _make_ctx(state=TaskState.BACKLOG, adapter=adapter)

        role, result = await dispatch(ctx)

        assert role.name == "triage"
        assert result.success

    async def test_dispatch_explicit_role(self) -> None:
        from sova.roles.dispatcher import dispatch

        adapter = _mock_adapter(TaskState.BACKLOG)
        ctx = _make_ctx(role="triage", state=TaskState.BACKLOG, adapter=adapter)

        role, result = await dispatch(ctx, role_name="triage")

        assert role.name == "triage"
        assert result.success

    async def test_dispatch_explicit_wrong_state_fails(self) -> None:
        from sova.roles.dispatcher import dispatch

        adapter = _mock_adapter(TaskState.IN_PROGRESS)
        ctx = _make_ctx(role="triage", state=TaskState.IN_PROGRESS, adapter=adapter)

        role, result = await dispatch(ctx, role_name="triage")

        assert not result.success


# ---------------------------------------------------------------------------
# TaskAssessment model validation
# ---------------------------------------------------------------------------


class TestTaskAssessment:
    def test_valid_assessment(self) -> None:
        a = TaskAssessment(
            suitability="ready",
            confidence=0.85,
            reasoning="Looks good",
        )
        assert a.suitability == "ready"
        assert a.confidence == 0.85
        assert a.missing_context == []
        assert a.estimated_complexity == "moderate"
        assert a.suggested_role == "developer"
        assert a.sub_tasks == []

    def test_all_suitability_values(self) -> None:
        for val in ("ready", "needs_spec", "needs_research", "human_only"):
            a = TaskAssessment(suitability=val, confidence=0.5, reasoning="test")
            assert a.suitability == val

    def test_invalid_suitability_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TaskAssessment(suitability="invalid", confidence=0.5, reasoning="test")

    def test_confidence_bounds(self) -> None:
        from pydantic import ValidationError

        TaskAssessment(suitability="ready", confidence=0.0, reasoning="min")
        TaskAssessment(suitability="ready", confidence=1.0, reasoning="max")

        with pytest.raises(ValidationError):
            TaskAssessment(suitability="ready", confidence=1.5, reasoning="too high")

        with pytest.raises(ValidationError):
            TaskAssessment(suitability="ready", confidence=-0.1, reasoning="negative")

    def test_all_complexity_values(self) -> None:
        for val in ("trivial", "simple", "moderate", "complex", "epic"):
            a = TaskAssessment(
                suitability="ready",
                confidence=0.5,
                reasoning="test",
                estimated_complexity=val,
            )
            assert a.estimated_complexity == val

    def test_invalid_complexity_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TaskAssessment(
                suitability="ready",
                confidence=0.5,
                reasoning="test",
                estimated_complexity="impossible",
            )

    def test_with_all_fields(self) -> None:
        a = TaskAssessment(
            suitability="needs_research",
            confidence=0.6,
            reasoning="Needs investigation",
            missing_context=["affected files", "root cause"],
            estimated_complexity="complex",
            suggested_role="researcher",
            sub_tasks=["explore module A", "check module B"],
        )
        assert len(a.missing_context) == 2
        assert len(a.sub_tasks) == 2


# ---------------------------------------------------------------------------
# assess_task() on each role
# ---------------------------------------------------------------------------


class TestAssessTask:
    async def test_triage_assess_with_body(self) -> None:
        from sova.roles.triage import TriageRole

        role = TriageRole()
        body = (
            "Implement the foobar feature.\n\n"
            "## Scope\n"
            "Update `src/handler.py` to support the new endpoint.\n\n"
            "## Acceptance Criteria\n"
            "- [ ] Endpoint returns 200\n"
            "- [ ] Tests pass\n"
        )
        task = Task(id="1", title="Test", body=body, state=TaskState.BACKLOG)
        assessment = await role.assess_task(task)

        assert assessment.suitability == "ready"
        assert assessment.confidence > 0

    async def test_triage_assess_without_body(self) -> None:
        from sova.roles.triage import TriageRole

        role = TriageRole()
        task = Task(id="1", title="Test", body="", state=TaskState.BACKLOG)
        assessment = await role.assess_task(task)

        assert assessment.suitability == "needs_spec"
        assert len(assessment.missing_context) > 0

    async def test_researcher_assess_default(self) -> None:
        from sova.roles.researcher import ResearcherRole

        role = ResearcherRole()
        task = Task(id="1", title="Test", body="Some issue description", state=TaskState.TRIAGED)
        assessment = await role.assess_task(task)

        assert assessment.suitability == "needs_research"
        assert assessment.suggested_role == "researcher"

    async def test_researcher_assess_empty_body(self) -> None:
        from sova.roles.researcher import ResearcherRole

        role = ResearcherRole()
        task = Task(id="1", title="Test", body="", state=TaskState.TRIAGED)
        assessment = await role.assess_task(task)

        assert assessment.suitability == "needs_spec"
        assert assessment.confidence == 0.8
        assert len(assessment.missing_context) > 0

    async def test_researcher_assess_already_researched(self) -> None:
        from sova.roles.researcher import ResearcherRole

        role = ResearcherRole()
        task = Task(
            id="1",
            title="Test",
            body="Some description\n\n## Research\n\nAlready done",
            state=TaskState.TRIAGED,
        )
        assessment = await role.assess_task(task)

        assert assessment.suitability == "ready"
        assert assessment.suggested_role == "developer"
        assert assessment.confidence == 0.9

    async def test_researcher_assess_human_only(self) -> None:
        from sova.roles.researcher import ResearcherRole

        role = ResearcherRole()
        task = Task(
            id="1",
            title="Test",
            body="Sensitive issue",
            state=TaskState.TRIAGED,
            labels=["agent:human-only"],
        )
        assessment = await role.assess_task(task)

        assert assessment.suitability == "human_only"
        assert assessment.confidence == 0.85

    async def test_researcher_assess_human_only_overrides_research_section(self) -> None:
        from sova.roles.researcher import ResearcherRole

        role = ResearcherRole()
        task = Task(
            id="1",
            title="Test",
            body="Some description\n\n## Research\n\nAlready done",
            state=TaskState.TRIAGED,
            labels=["agent:human-only"],
        )
        assessment = await role.assess_task(task)

        assert assessment.suitability == "human_only"

    async def test_developer_assess(self) -> None:
        from sova.roles.developer import DeveloperRole

        role = DeveloperRole()
        task = Task(id="1", title="Test", state=TaskState.RESEARCHED)
        assessment = await role.assess_task(task)

        assert assessment.suitability == "ready"
        assert assessment.suggested_role == "developer"

    async def test_reviewer_assess(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        role = ReviewerRole()
        task = Task(id="1", title="Test", state=TaskState.IN_REVIEW)
        assessment = await role.assess_task(task)

        assert assessment.suitability == "ready"
        assert assessment.suggested_role == "reviewer"


# ---------------------------------------------------------------------------
# Triage label application
# ---------------------------------------------------------------------------


class TestTriageLabelApplication:
    async def test_applies_ready_label(self) -> None:
        from sova.roles.triage import TriageRole

        body = (
            "Implement feature X.\n\n"
            "## Acceptance Criteria\n"
            "- [ ] Update `src/main.py` to handle new case\n"
            "- [ ] Tests pass\n"
        )
        adapter = _mock_adapter(TaskState.BACKLOG)
        adapter.get_task.return_value = Task(
            id="42",
            title="Test",
            body=body,
            state=TaskState.BACKLOG,
        )
        ctx = _make_ctx(role="triage", state=TaskState.BACKLOG, adapter=adapter)
        role = TriageRole()

        await role.execute(ctx)

        adapter.add_label.assert_awaited_once_with("42", "agent:ready")

    async def test_applies_needs_spec_label(self) -> None:
        from sova.roles.triage import TriageRole

        adapter = _mock_adapter(TaskState.BACKLOG)
        adapter.get_task.return_value = Task(
            id="42",
            title="Test",
            body="",
            state=TaskState.BACKLOG,
        )
        ctx = _make_ctx(role="triage", state=TaskState.BACKLOG, adapter=adapter)
        role = TriageRole()

        await role.execute(ctx)

        adapter.add_label.assert_awaited_once_with("42", "agent:needs-spec")

    async def test_label_matches_suitability(self) -> None:
        from sova.roles.triage import TriageRole

        role = TriageRole()
        expected = {
            "ready": "agent:ready",
            "needs_spec": "agent:needs-spec",
            "needs_research": "agent:needs-research",
            "human_only": "agent:human-only",
        }
        assert role.SUITABILITY_LABELS == expected

    def test_resolve_label_default(self) -> None:
        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        cfg = TriageConfig()
        assert role.resolve_label("ready", cfg) == "agent:ready"

    def test_resolve_label_custom_override(self) -> None:
        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        cfg = TriageConfig(labels={"ready": "team:approved"})
        assert role.resolve_label("ready", cfg) == "team:approved"

    def test_resolve_label_empty_string_skips(self) -> None:
        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        cfg = TriageConfig(labels={"ready": ""})
        assert role.resolve_label("ready", cfg) is None

    def test_resolve_label_unknown_suitability(self) -> None:
        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        cfg = TriageConfig()
        assert role.resolve_label("unknown_value", cfg) is None


class TestTriageExecuteConfig:
    async def test_dry_run_mode_writes_nothing(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        ctx = MagicMock()
        ctx.issue_number = "42"
        ctx.force = False
        ctx.config.triage = TriageConfig(mode="dry_run")

        adapter = AsyncMock()
        adapter.get_task.return_value = MagicMock(
            id="42",
            title="Test",
            body="test body",
            labels=[],
            state="backlog",
        )
        ctx.adapter = adapter

        result = await role.execute(ctx)
        assert result.success
        assert "dry run" in result.summary
        adapter.add_label.assert_not_called()
        adapter.edit_body.assert_not_called()
        adapter.post_comment.assert_not_called()
        adapter.transition_state.assert_not_called()

    async def test_write_body_false_skips_body_edit(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        ctx = MagicMock()
        ctx.issue_number = "42"
        ctx.force = False
        ctx.config.triage = TriageConfig(mode="full", write_body=False, auto_label=False, write_transition=False)

        adapter = AsyncMock()
        adapter.get_task.return_value = MagicMock(
            id="42",
            title="Test",
            body="test body",
            labels=[],
            state="backlog",
        )
        ctx.adapter = adapter

        result = await role.execute(ctx)
        assert result.success
        adapter.edit_body.assert_not_called()
        adapter.add_label.assert_not_called()
        adapter.transition_state.assert_not_called()


class TestTriageSkipPatterns:
    """Tests for title-prefix and label-based skip patterns in heuristic triage."""

    def _make_task(self, title: str = "Fix login bug", body: str = "Some description", labels: list[str] | None = None):
        from unittest.mock import MagicMock

        return MagicMock(id="42", title=title, body=body, labels=labels or [], state="backlog")

    def test_skip_title_prefix_qe(self) -> None:
        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        cfg = TriageConfig(skip_title_prefixes=["[QE]", "[Spike]"])
        task = self._make_task(title="[QE] [SUB] Verify RBAC permissions")
        assessment = role.heuristic_assess(task, cfg)
        assert assessment.suitability == "human_only"
        assert "title prefix" in assessment.reasoning.lower()

    def test_skip_title_prefix_case_insensitive(self) -> None:
        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        cfg = TriageConfig(skip_title_prefixes=["[qe]"])
        task = self._make_task(title="[QE] Check something")
        assessment = role.heuristic_assess(task, cfg)
        assert assessment.suitability == "human_only"

    def test_skip_label_post_mvp(self) -> None:
        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        cfg = TriageConfig(skip_labels=["post-mvp", "QE"])
        task = self._make_task(labels=["post-mvp", "some-other-label"])
        assessment = role.heuristic_assess(task, cfg)
        assert assessment.suitability == "human_only"
        assert "label" in assessment.reasoning.lower()

    def test_skip_label_case_insensitive(self) -> None:
        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        cfg = TriageConfig(skip_labels=["qe"])
        task = self._make_task(labels=["QE"])
        assessment = role.heuristic_assess(task, cfg)
        assert assessment.suitability == "human_only"

    def test_no_skip_when_no_match(self) -> None:
        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        cfg = TriageConfig(skip_title_prefixes=["[QE]"], skip_labels=["post-mvp"])
        task = self._make_task(title="Fix RBAC permission check", body="Detailed description with context", labels=[])
        assessment = role.heuristic_assess(task, cfg)
        assert assessment.suitability != "human_only"

    def test_no_skip_when_empty_config(self) -> None:
        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        cfg = TriageConfig()
        task = self._make_task(title="[QE] Something", labels=["post-mvp"])
        assessment = role.heuristic_assess(task, cfg)
        assert assessment.suitability != "human_only"

    def test_skip_multiple_labels_any_match(self) -> None:
        from sova.config.models import TriageConfig
        from sova.roles.triage import TriageRole

        role = TriageRole()
        cfg = TriageConfig(skip_labels=["form", "form-501"])
        task = self._make_task(labels=["form-501"])
        assessment = role.heuristic_assess(task, cfg)
        assert assessment.suitability == "human_only"


# ---------------------------------------------------------------------------
# TriageRole -- assess_task_with_llm
# ---------------------------------------------------------------------------


class TestTriageAssessWithLLM:
    """Tests for assess_task_with_llm (covers the simplified resolve_model call)."""

    async def test_successful_llm_assessment(self) -> None:
        import json
        from decimal import Decimal
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.llm.models import LLMResult
        from sova.roles.triage import TriageRole

        role = TriageRole()
        task = MagicMock(
            id="42", title="Add caching", body="Implement Redis caching layer.", labels=["feature"], state="backlog"
        )
        ctx = _make_ctx(role="triage", state=TaskState.BACKLOG)

        llm_response = json.dumps(
            {
                "suitability": "ready",
                "confidence": 0.9,
                "reasoning": "Well-specified task.",
                "missing_context": [],
                "estimated_complexity": "moderate",
                "suggested_role": "developer",
            }
        )
        mock_invoke = AsyncMock(return_value=LLMResult(text=llm_response, model="haiku", cost_usd=Decimal("0.01")))

        with patch("sova.llm.client.invoke", mock_invoke):
            assessment = await role.assess_task_with_llm(task, ctx)

        assert assessment.suitability == "ready"
        assert assessment.confidence == 0.9

    async def test_successful_llm_assessment_records_cost_with_reason(self) -> None:
        """assess_task_with_llm passes model_selection_reason to record_cost."""
        import json
        from decimal import Decimal
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.llm.models import LLMResult
        from sova.roles.triage import TriageRole

        role = TriageRole()
        task = MagicMock(
            id="42", title="Add caching", body="Implement Redis caching layer.", labels=["feature"], state="backlog"
        )
        ctx = _make_ctx(role="triage", state=TaskState.BACKLOG)

        llm_response = json.dumps(
            {
                "suitability": "ready",
                "confidence": 0.9,
                "reasoning": "Well-specified task.",
                "missing_context": [],
                "estimated_complexity": "moderate",
                "suggested_role": "developer",
            }
        )
        import sova.llm.client as _llm_client
        import sova.llm.cost as _llm_cost

        mock_invoke = AsyncMock(return_value=LLMResult(text=llm_response, model="haiku", cost_usd=Decimal("0.01")))
        mock_record_cost = AsyncMock()
        mock_resolve = MagicMock(return_value=("haiku", "role:triage->haiku"))

        with (
            patch.object(_llm_client, "invoke", mock_invoke),
            patch.object(_llm_client, "resolve_model", mock_resolve),
            patch.object(_llm_cost, "record_cost", mock_record_cost),
        ):
            assessment = await role.assess_task_with_llm(task, ctx)

        assert assessment.suitability == "ready"
        mock_record_cost.assert_awaited_once()
        call_kwargs = mock_record_cost.call_args
        assert call_kwargs.kwargs["phase"] == "triage"
        assert call_kwargs.kwargs["issue"] == "42"
        assert call_kwargs.kwargs["model_selection_reason"] == "role:triage->haiku"

    async def test_llm_failure_falls_back_to_heuristic(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.roles.triage import TriageRole

        role = TriageRole()
        task = MagicMock(id="42", title="Fix bug", body="Description of the bug.", labels=[], state="backlog")
        ctx = _make_ctx(role="triage", state=TaskState.BACKLOG)

        mock_invoke = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        with patch("sova.llm.client.invoke", mock_invoke):
            assessment = await role.assess_task_with_llm(task, ctx)

        # Falls back to heuristic (body exists but short, no criteria)
        assert assessment.suitability in ("ready", "needs_spec")

    async def test_empty_body_skips_llm(self) -> None:
        from unittest.mock import MagicMock

        from sova.roles.triage import TriageRole

        role = TriageRole()
        task = MagicMock(id="42", title="No body", body="", labels=[], state="backlog")
        ctx = _make_ctx(role="triage", state=TaskState.BACKLOG)

        assessment = await role.assess_task_with_llm(task, ctx)

        assert assessment.suitability == "needs_spec"


# ---------------------------------------------------------------------------
# ReviewerRole -- LLM-based review
# ---------------------------------------------------------------------------


class TestReviewerLLMReview:
    """Tests for the full LLM-based ReviewerRole implementation."""

    def _llm_response(self, findings: list[dict] | None = None, summary: str = "Looks good") -> str:
        import json

        data = {"findings": findings or [], "summary": summary}
        return json.dumps(data)

    async def test_successful_review_with_findings(self) -> None:
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=10)

        findings = [
            {
                "file": "foo.py",
                "line": 5,
                "severity": 7,
                "category": "bug",
                "description": "Null check missing",
                "suggestion": "Add check",
            },
            {"file": "bar.py", "severity": 2, "category": "style", "description": "Minor formatting"},
        ]
        llm_result = LLMResult(
            text=self._llm_response(findings, "Two issues found"),
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )

        with (
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff content"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["foo.py", "bar.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            role = ReviewerRole()
            result = await role.execute(ctx)

        assert result.success
        assert "2 findings" in result.summary
        adapter.post_pr_review.assert_awaited_once()
        call_kwargs = adapter.post_pr_review.call_args[1]
        assert "Null check missing" in call_kwargs["body"]
        assert ctx.cost_usd == Decimal("0.01")

    async def test_review_no_findings(self) -> None:
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=10)

        llm_result = LLMResult(text=self._llm_response([], "Clean code"), model="sonnet", cost_usd=Decimal("0.005"))

        with (
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            role = ReviewerRole()
            result = await role.execute(ctx)

        assert result.success
        assert "0 findings" in result.summary
        adapter.post_pr_review.assert_awaited_once()
        body = adapter.post_pr_review.call_args[1]["body"]
        assert "No issues found" in body
        assert "LGTM" in body

    async def test_llm_failure_graceful_fallback(self) -> None:
        from unittest.mock import patch

        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=10)

        with (
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, side_effect=RuntimeError("LLM unavailable")),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            role = ReviewerRole()
            result = await role.execute(ctx)

        assert result.success
        adapter.post_pr_review.assert_awaited_once()
        body = adapter.post_pr_review.call_args[1]["body"]
        assert "manual review" in body.lower() or "no issues found" in body.lower()

    async def test_large_diff_chunking(self) -> None:
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=10)

        # Create a diff larger than DIFF_CHUNK_SIZE (100KB)
        large_diff = "diff --git a/a.py b/a.py\n" + ("+" + "x" * 200 + "\n") * 600
        large_diff += "diff --git a/b.py b/b.py\n" + ("+" + "y" * 200 + "\n") * 600

        finding_a = [{"file": "a.py", "severity": 5, "category": "bug", "description": "Issue in a"}]
        finding_b = [{"file": "b.py", "severity": 4, "category": "bug", "description": "Issue in b"}]
        chunk1_result = LLMResult(
            text=self._llm_response(finding_a, "Chunk 1"),
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
        chunk2_result = LLMResult(
            text=self._llm_response(finding_b, "Chunk 2"),
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )

        side = [chunk1_result, chunk2_result]
        with (
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value=large_diff),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py", "b.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, side_effect=side) as mock_invoke,
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            role = ReviewerRole()
            result = await role.execute(ctx)

        assert result.success
        assert mock_invoke.call_count == 2
        assert "2 findings" in result.summary
        assert ctx.cost_usd == Decimal("0.02")

    async def test_handoff_address_review_when_actionable(self) -> None:
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=10)
        ctx.task_run_id = 1

        findings = [{"file": "x.py", "severity": 5, "category": "bug", "description": "Bad"}]
        llm_result = LLMResult(text=self._llm_response(findings), model="sonnet", cost_usd=Decimal("0"))

        with (
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["x.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock) as mock_db_handoff,
            patch("sova.roles.reviewer.write_handoff_file") as mock_file_handoff,
        ):
            role = ReviewerRole()
            await role.execute(ctx)

        mock_db_handoff.assert_awaited_once()
        handoff = mock_db_handoff.call_args[0][1]
        assert handoff.next_action == "address_review"
        assert len(handoff.pending_findings) == 1

        mock_file_handoff.assert_called_once()
        dashboard_handoff = mock_file_handoff.call_args[0][1]
        assert dashboard_handoff.next_actions[0].id == "address_review"

    async def test_handoff_all_findings_actionable(self) -> None:
        """All findings (including low-severity) are now actionable and sent to developer."""
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=10)
        ctx.task_run_id = 1

        findings = [{"file": "x.py", "severity": 1, "category": "style", "description": "Minor"}]
        llm_result = LLMResult(text=self._llm_response(findings), model="sonnet", cost_usd=Decimal("0"))

        with (
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["x.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock) as mock_db_handoff,
            patch("sova.roles.reviewer.write_handoff_file") as mock_file_handoff,
        ):
            role = ReviewerRole()
            await role.execute(ctx)

        handoff = mock_db_handoff.call_args[0][1]
        assert handoff.next_action == "address_review"
        assert len(handoff.pending_findings) == 1

        dashboard_handoff = mock_file_handoff.call_args[0][1]
        assert dashboard_handoff.next_actions[0].id == "address_review"
        assert dashboard_handoff.next_actions[0].label == "Address Review"

    async def test_handoff_auto_execute_disabled_by_config(self) -> None:
        """When pipeline.auto_address_review is False, handoff action has auto_execute=False."""
        from decimal import Decimal
        from unittest.mock import patch

        from sova.config.models import PipelineConfig
        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        config = ProjectConfig(pipeline=PipelineConfig(auto_address_review=False))
        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=10, config=config)
        ctx.task_run_id = 1

        findings = [{"file": "x.py", "severity": 5, "category": "bug", "description": "Bad"}]
        llm_result = LLMResult(text=self._llm_response(findings), model="sonnet", cost_usd=Decimal("0"))

        with (
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["x.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file") as mock_file_handoff,
        ):
            role = ReviewerRole()
            await role.execute(ctx)

        dashboard_handoff = mock_file_handoff.call_args[0][1]
        assert dashboard_handoff.next_actions[0].id == "address_review"
        assert dashboard_handoff.next_actions[0].auto_execute is False

    async def test_handoff_approve_when_zero_findings(self) -> None:
        """With zero findings, handoff recommends approve."""
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=10)
        ctx.task_run_id = 1

        llm_result = LLMResult(text=self._llm_response([]), model="sonnet", cost_usd=Decimal("0"))

        with (
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["x.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock) as mock_db_handoff,
            patch("sova.roles.reviewer.write_handoff_file") as mock_file_handoff,
        ):
            role = ReviewerRole()
            await role.execute(ctx)

        handoff = mock_db_handoff.call_args[0][1]
        assert handoff.next_action == "approve"
        assert len(handoff.pending_findings) == 0

        dashboard_handoff = mock_file_handoff.call_args[0][1]
        assert len(dashboard_handoff.next_actions) == 1
        assert dashboard_handoff.next_actions[0].id == "integrate"
        assert dashboard_handoff.next_actions[0].label == "Integrate PR"

    async def test_diff_fetch_failure(self) -> None:
        from unittest.mock import patch

        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=10)

        err = RuntimeError("Network error")
        with patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, side_effect=err):
            role = ReviewerRole()
            result = await role.execute(ctx)

        assert not result.success
        assert "Network error" in result.error

    async def test_clears_current_step_sentinel(self) -> None:
        """After execute(), current_step should be cleared from 'agent' to None."""
        from decimal import Decimal
        from unittest.mock import patch

        from sova.db.models import TaskRun
        from sova.db.session import get_session
        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        # Create a TaskRun with current_step="agent" (mimics dashboard creation)
        async with await get_session() as session:
            async with session.begin():
                task_run = TaskRun(
                    issue_number="42",
                    role="reviewer",
                    status="running",
                    current_step="agent",
                )
                session.add(task_run)
                await session.flush()
                run_id = task_run.id

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(
            role="reviewer",
            state=TaskState.IN_REVIEW,
            adapter=adapter,
            pr_number=10,
            task_run_id=run_id,
        )

        llm_result = LLMResult(
            text='{"findings": [], "summary": "Clean"}',
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )

        with (
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            role = ReviewerRole()
            result = await role.execute(ctx)

        assert result.success

        # Verify sentinel was cleared
        async with await get_session() as session:
            task_run = await session.get(TaskRun, run_id)
            assert task_run.current_step is None

    async def test_clear_current_step_db_failure_non_fatal(self) -> None:
        """DB errors in _clear_current_step should not block the review."""
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(
            role="reviewer",
            state=TaskState.IN_REVIEW,
            adapter=adapter,
            pr_number=10,
            task_run_id=999,  # non-existent, but we'll mock get_session to fail
        )

        llm_result = LLMResult(
            text='{"findings": [], "summary": "Clean"}',
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )

        with (
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
            patch("sova.roles.reviewer.get_session", side_effect=OSError("DB connection refused")),
        ):
            role = ReviewerRole()
            result = await role.execute(ctx)

        # Should still succeed despite DB error
        assert result.success

    async def test_no_clear_without_task_run_id(self) -> None:
        """When task_run_id is None, _clear_current_step is not called."""
        from decimal import Decimal
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(
            role="reviewer",
            state=TaskState.IN_REVIEW,
            adapter=adapter,
            pr_number=10,
        )
        assert ctx.task_run_id is None

        llm_result = LLMResult(
            text='{"findings": [], "summary": "Clean"}',
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )

        mock_clear_step = AsyncMock()
        with (
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
            patch("sova.roles.reviewer.read_handoff_file", return_value=None),
            patch("sova.roles.reviewer.get_session", new_callable=AsyncMock),
            patch.object(ReviewerRole, "_clear_current_step", mock_clear_step),
        ):
            role = ReviewerRole()
            result = await role.execute(ctx)

        assert result.success
        # _clear_current_step should not have been called when task_run_id is None
        mock_clear_step.assert_not_awaited()

    async def test_sentinel_cleared_on_precondition_failure(self) -> None:
        """Sentinel is cleared even when preconditions fail (try/finally)."""
        from sova.db.models import TaskRun
        from sova.db.session import get_session
        from sova.roles.reviewer import ReviewerRole

        async with await get_session() as session:
            async with session.begin():
                task_run = TaskRun(
                    issue_number="42",
                    role="reviewer",
                    status="running",
                    current_step="agent",
                )
                session.add(task_run)
                await session.flush()
                run_id = task_run.id

        # Task is in BACKLOG, not IN_REVIEW -- precondition will fail
        adapter = _mock_adapter(TaskState.BACKLOG)
        ctx = _make_ctx(
            role="reviewer",
            state=TaskState.BACKLOG,
            adapter=adapter,
            task_run_id=run_id,
        )

        role = ReviewerRole()
        result = await role.execute(ctx)

        assert not result.success

        async with await get_session() as session:
            task_run = await session.get(TaskRun, run_id)
            assert task_run.current_step is None

    async def test_sentinel_cleared_on_pr_not_found(self) -> None:
        """Sentinel is cleared when no PR is found for the issue."""
        from unittest.mock import patch

        from sova.db.models import TaskRun
        from sova.db.session import get_session
        from sova.roles.reviewer import ReviewerRole

        async with await get_session() as session:
            async with session.begin():
                task_run = TaskRun(
                    issue_number="42",
                    role="reviewer",
                    status="running",
                    current_step="agent",
                )
                session.add(task_run)
                await session.flush()
                run_id = task_run.id

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(
            role="reviewer",
            state=TaskState.IN_REVIEW,
            adapter=adapter,
            task_run_id=run_id,
        )
        # No pr_number set, and find_pr_for_issue returns None
        assert ctx.pr_number is None

        with patch("sova.roles.reviewer.find_pr_for_issue", new_callable=AsyncMock, return_value=None):
            role = ReviewerRole()
            result = await role.execute(ctx)

        assert not result.success
        assert "no linked PR" in result.summary

        async with await get_session() as session:
            task_run = await session.get(TaskRun, run_id)
            assert task_run.current_step is None

    async def test_sentinel_cleared_on_diff_fetch_failure(self) -> None:
        """Sentinel is cleared when diff fetch raises an exception."""
        from unittest.mock import patch

        from sova.db.models import TaskRun
        from sova.db.session import get_session
        from sova.roles.reviewer import ReviewerRole

        async with await get_session() as session:
            async with session.begin():
                task_run = TaskRun(
                    issue_number="42",
                    role="reviewer",
                    status="running",
                    current_step="agent",
                )
                session.add(task_run)
                await session.flush()
                run_id = task_run.id

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(
            role="reviewer",
            state=TaskState.IN_REVIEW,
            adapter=adapter,
            pr_number=10,
            task_run_id=run_id,
        )

        with patch(
            "sova.roles.reviewer.get_pr_diff",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Network error"),
        ):
            role = ReviewerRole()
            result = await role.execute(ctx)

        assert not result.success

        async with await get_session() as session:
            task_run = await session.get(TaskRun, run_id)
            assert task_run.current_step is None


class TestReviewerParsing:
    """Tests for reviewer pure functions."""

    def test_parse_findings_valid_json(self) -> None:
        from sova.roles.reviewer import _parse_findings

        text = (
            '{"findings": [{"file": "a.py", "severity": 5, "category": "bug", "description": "Bad"}], "summary": "OK"}'
        )
        findings, summary = _parse_findings(text)
        assert len(findings) == 1
        assert findings[0].severity == 5
        assert summary == "OK"

    def test_parse_findings_with_markdown_fences(self) -> None:
        from sova.roles.reviewer import _parse_findings

        text = '```json\n{"findings": [], "summary": "Clean"}\n```'
        findings, summary = _parse_findings(text)
        assert findings == []
        assert summary == "Clean"

    def test_parse_findings_invalid_json(self) -> None:
        from sova.roles.reviewer import _parse_findings

        findings, summary = _parse_findings("not json at all")
        assert findings == []
        assert "Failed" in summary

    def test_parse_findings_invalid_json_logs_warning(self) -> None:
        from unittest.mock import patch

        from sova.roles.reviewer import _parse_findings

        with patch("sova.roles.reviewer.log") as mock_log:
            _parse_findings("not json at all")
            mock_log.warning.assert_called_once()
            assert mock_log.warning.call_args[0][0] == "parse_findings.failed"
            assert "text_preview" in mock_log.warning.call_args[1]

    def test_parse_findings_bad_embedded_json_logs_warning(self) -> None:
        from unittest.mock import patch

        from sova.roles.reviewer import _parse_findings

        with patch("sova.roles.reviewer.log") as mock_log:
            _parse_findings("prefix {invalid json} suffix")
            mock_log.warning.assert_called_once()
            assert mock_log.warning.call_args[0][0] == "parse_findings.failed"

    def test_parse_findings_json_embedded_in_text(self) -> None:
        from sova.roles.reviewer import _parse_findings

        text = (
            'Here is the review: {"findings": [{"file": "x.py",'
            ' "severity": 3, "category": "style", "description": "D"}],'
            ' "summary": "S"} done'
        )
        findings, summary = _parse_findings(text)
        assert len(findings) == 1

    def test_parse_findings_preamble_json_before_real_review(self) -> None:
        from sova.roles.reviewer import _parse_findings

        text = (
            'Here\'s an example: {"bad": true} and the real review: '
            '{"findings": [{"file": "a.py", "severity": 5, "category": "bug", "description": "X"}], "summary": "S"}'
        )
        findings, summary = _parse_findings(text)
        assert len(findings) == 1
        assert findings[0].file == "a.py"
        assert summary == "S"

    def test_parse_findings_multiple_json_no_findings_key(self) -> None:
        from sova.roles.reviewer import _parse_findings

        text = 'Some text {"data": 1} more text {"other": 2}'
        findings, summary = _parse_findings(text)
        assert findings == []

    def test_parse_findings_severity_string_word(self) -> None:
        from sova.roles.reviewer import _parse_findings

        text = (
            '{"findings": [{"file": "a.py", "severity": "HIGH",'
            ' "category": "bug", "description": "D"}], "summary": "S"}'
        )
        findings, _ = _parse_findings(text)
        assert len(findings) == 1
        assert findings[0].severity == 5

    def test_parse_findings_severity_float(self) -> None:
        from sova.roles.reviewer import _parse_findings

        text = (
            '{"findings": [{"file": "a.py", "severity": 7.5, "category": "bug", "description": "D"}], "summary": "S"}'
        )
        findings, _ = _parse_findings(text)
        assert findings[0].severity == 7

    def test_parse_findings_severity_none(self) -> None:
        from sova.roles.reviewer import _parse_findings

        text = (
            '{"findings": [{"file": "a.py", "severity": null, "category": "bug", "description": "D"}], "summary": "S"}'
        )
        findings, _ = _parse_findings(text)
        assert findings[0].severity == 5

    def test_safe_severity_int(self) -> None:
        from sova.roles.reviewer import _safe_severity

        assert _safe_severity(7) == 7

    def test_safe_severity_float(self) -> None:
        from sova.roles.reviewer import _safe_severity

        assert _safe_severity(7.5) == 7

    def test_safe_severity_numeric_string(self) -> None:
        from sova.roles.reviewer import _safe_severity

        assert _safe_severity("3") == 3

    def test_safe_severity_word_string(self) -> None:
        from sova.roles.reviewer import _safe_severity

        assert _safe_severity("HIGH") == 5
        assert _safe_severity("critical") == 5

    def test_safe_severity_none(self) -> None:
        from sova.roles.reviewer import _safe_severity

        assert _safe_severity(None) == 5

    def test_safe_severity_missing_defaults(self) -> None:
        from sova.roles.reviewer import _safe_severity

        assert _safe_severity(None, default=3) == 3

    def test_extract_json_with_findings(self) -> None:
        from sova.roles.reviewer import _extract_json

        text = '{"bad": true} and {"findings": [{"file": "a.py"}], "summary": "S"}'
        result = _extract_json(text)
        assert result is not None
        assert "findings" in result

    def test_extract_json_no_findings_fallback(self) -> None:
        from sova.roles.reviewer import _extract_json

        result = _extract_json('prefix {"data": 1} suffix')
        assert result == {"data": 1}

    def test_extract_json_no_braces(self) -> None:
        from sova.roles.reviewer import _extract_json

        assert _extract_json("no json here") is None

    def test_extract_json_invalid_only(self) -> None:
        from sova.roles.reviewer import _extract_json

        assert _extract_json("{invalid json}") is None

    def test_chunk_diff_small(self) -> None:
        from sova.roles.reviewer import _chunk_diff

        diff = "diff --git a/f.py b/f.py\n+hello\n"
        chunks = _chunk_diff(diff, chunk_size=10000)
        assert len(chunks) == 1

    def test_chunk_diff_large(self) -> None:
        from sova.roles.reviewer import _chunk_diff

        diff = "diff --git a/a.py b/a.py\n" + ("+" + "x" * 99 + "\n") * 700
        diff += "diff --git a/b.py b/b.py\n" + ("+" + "y" * 99 + "\n") * 700
        chunks = _chunk_diff(diff, chunk_size=70000)
        assert len(chunks) == 2

    def test_format_findings_no_findings(self) -> None:
        from sova.roles.reviewer import _format_findings_comment

        comment = _format_findings_comment([], "All good", 42)
        assert "No issues found" in comment
        assert "PR #42" in comment
        assert "LGTM" in comment

    def test_format_findings_with_actionable(self) -> None:
        from sova.roles.reviewer import ReviewFinding, _format_findings_comment

        findings = [
            ReviewFinding(file="a.py", severity=7, category="bug", description="Null ref", suggestion="Add check"),
            ReviewFinding(file="b.py", severity=1, category="style", description="Whitespace"),
        ]
        comment = _format_findings_comment(findings, "Mixed", 10)
        assert "2 findings" in comment
        assert "all to be addressed" in comment
        assert "Null ref" in comment
        assert "Findings requiring action" in comment
        assert "BLOCK" in comment

    def test_format_findings_revise_assessment(self) -> None:
        """Findings with severity < 7 produce REVISE assessment."""
        from sova.roles.reviewer import ReviewFinding, _format_findings_comment

        findings = [
            ReviewFinding(file="a.py", severity=5, category="bug", description="Moderate issue"),
        ]
        comment = _format_findings_comment(findings, "Needs work", 50)
        assert "REVISE" in comment
        assert "actionable findings" in comment

    def test_parse_findings_nested_invalid_json_after_extraction(self) -> None:
        """When extracted JSON substring is also invalid, return empty findings."""
        from sova.roles.reviewer import _parse_findings

        text = "Here is some text {this is not valid json at all} end"
        findings, summary = _parse_findings(text)
        assert findings == []
        assert "Failed" in summary

    def test_parse_findings_no_braces(self) -> None:
        """When no braces exist at all, return empty findings."""
        from sova.roles.reviewer import _parse_findings

        findings, summary = _parse_findings("totally plain text with no json")
        assert findings == []
        assert "Failed" in summary


class TestReviewerExceptionPaths:
    """Cover except-Exception branches in ReviewerRole methods."""

    async def test_discover_pr_branch_exception_non_fatal(self) -> None:
        """_discover_pr handles get_pr_branch failure gracefully (lines 534-535)."""
        from unittest.mock import patch

        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=10)
        ctx.branch_name = None  # trigger branch discovery path
        role = ReviewerRole()

        with patch(
            "sova.roles.reviewer.get_pr_branch",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API down"),
        ):
            result = await role._discover_pr(ctx)

        # Should succeed (return None) but branch_name remains unset
        assert result is None
        assert ctx.branch_name is None

    async def test_extract_review_memories_exception_non_fatal(self) -> None:
        """_extract_review_memories handles extraction failure gracefully (lines 564-565)."""
        from unittest.mock import patch

        from sova.roles.reviewer import ReviewerRole, ReviewResult

        role = ReviewerRole()
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, pr_number=10)
        review = ReviewResult(findings=[], summary="OK", total_cost=0)

        with patch(
            "sova.knowledge.extraction.extract_memories",
            new_callable=AsyncMock,
            side_effect=RuntimeError("extraction failed"),
        ):
            # Should not raise
            await role._extract_review_memories(ctx, ctx.adapter.get_task.return_value, review)

    async def test_append_review_rationale_exception_non_fatal(self) -> None:
        """_append_review_rationale handles spec write failure gracefully (lines 699-700)."""
        from unittest.mock import patch

        from sova.roles.reviewer import ReviewerRole, ReviewFinding, ReviewResult

        role = ReviewerRole()
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, pr_number=10)
        finding = ReviewFinding(file="a.py", severity=8, category="bug", description="Bad")
        review = ReviewResult(findings=[finding], summary="Issues found", total_cost=0)

        with patch(
            "sova.core.steps._spec_helpers.append_spec_section",
            side_effect=OSError("write failed"),
        ):
            # Should not raise
            role._append_review_rationale(ctx, review)

    async def test_write_handoff_db_exception_non_fatal(self) -> None:
        """_write_handoff handles DB write failure gracefully (lines 733-734)."""
        from unittest.mock import patch

        from sova.roles.reviewer import ReviewerRole, ReviewResult

        role = ReviewerRole()
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, pr_number=10)
        ctx.task_run_id = 42
        review = ReviewResult(findings=[], summary="OK", total_cost=0)

        with (
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock, side_effect=RuntimeError("DB down")),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            # Should not raise
            await role._write_handoff(ctx, review)

    async def test_write_handoff_file_exception_non_fatal(self) -> None:
        """_write_handoff handles file write failure gracefully (lines 783-784)."""
        from unittest.mock import patch

        from sova.roles.reviewer import ReviewerRole, ReviewResult

        role = ReviewerRole()
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, pr_number=10)
        review = ReviewResult(findings=[], summary="OK", total_cost=0)

        with patch("sova.roles.reviewer.write_handoff_file", side_effect=OSError("disk full")):
            # Should not raise
            await role._write_handoff(ctx, review)


# ---------------------------------------------------------------------------
# CustomRole
# ---------------------------------------------------------------------------


class TestCustomRole:
    def test_custom_role_from_definition(self) -> None:
        """CustomRole sets attributes from WorkflowDefinition."""
        from sova.db.models import WorkflowDefinition
        from sova.roles.custom import CustomRole

        defn = WorkflowDefinition(
            id=1,
            name="my-role",
            description="A custom role",
            graph_json={"nodes": [{"id": "n1", "command": "develop"}], "edges": []},
            input_states=["researched"],
            output_state="in_review",
        )
        role = CustomRole(defn)
        assert role.name == "my-role"
        assert role.description == "A custom role"
        assert TaskState.RESEARCHED in role.allowed_input_states

    async def test_custom_role_assess(self) -> None:
        """CustomRole.assess_task returns a generic assessment."""
        from sova.db.models import WorkflowDefinition
        from sova.roles.custom import CustomRole

        defn = WorkflowDefinition(
            id=1,
            name="test",
            description="",
            graph_json={"nodes": [], "edges": []},
            input_states=[],
            output_state="",
        )
        role = CustomRole(defn)
        task = Task(id="1", title="Test", body="", state=TaskState.RESEARCHED)
        assessment = await role.assess_task(task)
        assert assessment.suitability == "ready"

    async def test_custom_role_rejects_wrong_state(self) -> None:
        """CustomRole.execute rejects tasks in wrong state."""
        from sova.db.models import WorkflowDefinition
        from sova.roles.custom import CustomRole

        defn = WorkflowDefinition(
            id=1,
            name="test",
            description="",
            input_states=["researched"],
            output_state="in_review",
            graph_json={"nodes": [{"id": "n1", "command": "develop"}], "edges": []},
        )
        role = CustomRole(defn)
        ctx = _make_ctx(state=TaskState.BACKLOG, force=False)
        result = await role.execute(ctx)
        assert not result.success
        assert "not ready" in result.summary.lower()

    async def test_custom_role_execute_success(self) -> None:
        """CustomRole.execute returns success when DAG completes."""
        from decimal import Decimal
        from unittest.mock import patch

        from sova.core.dag import DAGResult
        from sova.db.models import WorkflowDefinition
        from sova.roles.custom import CustomRole

        defn = WorkflowDefinition(
            id=1,
            name="test-role",
            description="",
            input_states=["researched"],
            output_state="in_review",
            graph_json={"nodes": [{"id": "n1", "command": "develop"}], "edges": []},
        )
        role = CustomRole(defn)
        ctx = _make_ctx(state=TaskState.RESEARCHED)

        mock_result = DAGResult(success=True, summary="DAG completed: 1 nodes executed", total_cost_usd=Decimal("0.5"))
        with patch.object(DAGExecutor, "execute", new=AsyncMock(return_value=mock_result)):
            result = await role.execute(ctx)

        assert result.success
        assert result.output_state == TaskState.IN_REVIEW
        assert "1 nodes" in result.summary

    async def test_custom_role_execute_dag_failure(self) -> None:
        """CustomRole.execute returns failure when DAG fails."""
        from unittest.mock import patch

        from sova.core.dag import DAGResult
        from sova.db.models import WorkflowDefinition
        from sova.roles.custom import CustomRole

        defn = WorkflowDefinition(
            id=1,
            name="test-role",
            description="",
            input_states=["researched"],
            output_state="in_review",
            graph_json={"nodes": [{"id": "n1", "command": "develop"}], "edges": []},
        )
        role = CustomRole(defn)
        ctx = _make_ctx(state=TaskState.RESEARCHED)

        mock_result = DAGResult(success=False, summary="DAG failed at node n1", error="develop step crashed")
        with patch.object(DAGExecutor, "execute", new=AsyncMock(return_value=mock_result)):
            result = await role.execute(ctx)

        assert not result.success
        assert "develop step crashed" in result.error

    def test_custom_role_empty_input_states_accepts_all(self) -> None:
        """When input_states is empty, any state is accepted."""
        from sova.db.models import WorkflowDefinition
        from sova.roles.custom import CustomRole

        defn = WorkflowDefinition(
            id=1,
            name="test",
            description="",
            input_states=[],
            output_state="",
            graph_json={"nodes": [{"id": "n1", "command": "develop"}], "edges": []},
        )
        role = CustomRole(defn)
        task = Task(id="1", title="Test", state=TaskState.BACKLOG)
        assert role.validate_preconditions(task)

    def test_custom_role_no_output_state(self) -> None:
        """When output_state is empty, output_state is None."""
        from sova.db.models import WorkflowDefinition
        from sova.roles.custom import CustomRole

        defn = WorkflowDefinition(
            id=1,
            name="test",
            description="",
            input_states=[],
            output_state="",
            graph_json={"nodes": [{"id": "n1", "command": "develop"}], "edges": []},
        )
        role = CustomRole(defn)
        assert role.output_state is None


# ---------------------------------------------------------------------------
# Dispatcher -- async fallback
# ---------------------------------------------------------------------------


class TestDispatcherAsyncFallback:
    async def test_get_role_async_builtin(self) -> None:
        """get_role_async returns built-in roles."""
        from sova.roles.dispatcher import get_role_async

        role = await get_role_async("developer")
        assert role.name == "developer"

    async def test_get_role_async_custom(self) -> None:
        """get_role_async falls back to DB for custom roles."""
        from sova.db.models import WorkflowDefinition
        from sova.db.session import get_session
        from sova.roles.dispatcher import get_role_async

        async with await get_session() as session:
            async with session.begin():
                defn = WorkflowDefinition(
                    name="my-custom",
                    description="Custom",
                    graph_json={"nodes": [{"id": "n1", "command": "test"}], "edges": []},
                    input_states=["researched"],
                    output_state="in_review",
                )
                session.add(defn)

        role = await get_role_async("my-custom")
        assert role.name == "my-custom"

    async def test_get_role_async_unknown(self) -> None:
        """get_role_async raises ValueError for unknown names."""
        from sova.roles.dispatcher import get_role_async

        with pytest.raises(ValueError, match="Unknown role"):
            await get_role_async("nonexistent")

    async def test_get_role_async_db_failure_propagates(self) -> None:
        """get_role_async re-raises DB failures instead of returning 'Unknown role'."""
        from unittest.mock import patch

        from sova.roles.dispatcher import get_role_async

        with patch("sova.db.session.get_session", side_effect=RuntimeError("DB down")):
            with pytest.raises(RuntimeError, match="DB down"):
                await get_role_async("some-custom-role")


# ---------------------------------------------------------------------------
# Planner role
# ---------------------------------------------------------------------------


class TestPlannerRole:
    def test_metadata(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        assert role.name == "planner"
        assert role.allowed_input_states == frozenset()
        assert role.output_state is None

    def test_validate_preconditions_always_true(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        task = Task(id="1", title="Test", state=TaskState.BACKLOG)
        assert role.validate_preconditions(task)
        task2 = Task(id="2", title="Test", state=TaskState.DONE)
        assert role.validate_preconditions(task2)

    async def test_assess_task(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        task = Task(id="1", title="Test", state=TaskState.BACKLOG)
        assessment = await role.assess_task(task)
        assert assessment.suitability == "ready"
        assert assessment.suggested_role == "planner"

    async def test_execute_happy_path(self) -> None:
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.planner import PlannerRole

        llm_response = json.dumps(
            [
                {
                    "title": "feat(cli): add health check command",
                    "description": "Add a health check subcommand.",
                    "priority": "medium",
                    "complexity": "simple",
                    "component": "cli",
                    "rationale": "Useful for monitoring.",
                    "dependencies": [],
                },
                {
                    "title": "fix(dashboard): improve error display",
                    "description": "Show better error messages.",
                    "priority": "high",
                    "complexity": "trivial",
                    "component": "dashboard",
                    "rationale": "User feedback.",
                    "dependencies": ["#100"],
                },
            ]
        )

        adapter = _mock_adapter()
        adapter.list_tasks.return_value = [
            Task(id="10", title="Existing issue", state=TaskState.BACKLOG),
        ]
        ctx = _make_ctx(role="planner", adapter=adapter, issue_number="")

        mock_result = LLMResult(text=llm_response, model="test", cost_usd=Decimal("0.01"))
        with patch("sova.llm.client.invoke", new=AsyncMock(return_value=mock_result)):
            role = PlannerRole()
            result = await role.execute(ctx)

        assert result.success
        assert "2 tasks" in result.summary
        assert len(result.findings) == 2
        assert "feat(cli): add health check command" in result.findings[0]

    async def test_execute_empty_response(self) -> None:
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.planner import PlannerRole

        adapter = _mock_adapter()
        adapter.list_tasks.return_value = []
        ctx = _make_ctx(role="planner", adapter=adapter, issue_number="")

        mock_result = LLMResult(text="[]", model="test", cost_usd=Decimal("0.005"))
        with patch("sova.llm.client.invoke", new=AsyncMock(return_value=mock_result)):
            role = PlannerRole()
            result = await role.execute(ctx)

        assert result.success
        assert "No tasks proposed" in result.summary
        assert result.findings == []

    async def test_execute_llm_failure(self) -> None:
        from unittest.mock import patch

        from sova.roles.planner import PlannerRole

        adapter = _mock_adapter()
        adapter.list_tasks.return_value = []
        ctx = _make_ctx(role="planner", adapter=adapter, issue_number="")

        with patch("sova.llm.client.invoke", new=AsyncMock(side_effect=RuntimeError("LLM down"))):
            role = PlannerRole()
            result = await role.execute(ctx)

        assert not result.success
        assert "LLM" in result.error

    async def test_execute_parse_failure(self) -> None:
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.planner import PlannerRole

        adapter = _mock_adapter()
        adapter.list_tasks.return_value = []
        ctx = _make_ctx(role="planner", adapter=adapter, issue_number="")

        mock_result = LLMResult(text="not valid json at all", model="test", cost_usd=Decimal("0.005"))
        with patch("sova.llm.client.invoke", new=AsyncMock(return_value=mock_result)):
            role = PlannerRole()
            result = await role.execute(ctx)

        assert not result.success
        assert "parse" in result.summary.lower()

    def test_dispatcher_get_role_planner(self) -> None:
        from sova.roles.dispatcher import get_role

        role = get_role("planner")
        assert role.name == "planner"

    def test_dispatcher_builtin_names_includes_planner(self) -> None:
        from sova.roles.dispatcher import BUILTIN_ROLE_NAMES

        assert "planner" in BUILTIN_ROLE_NAMES

    async def test_dispatch_issueless_with_planner(self) -> None:
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.dispatcher import dispatch

        adapter = _mock_adapter()
        adapter.list_tasks.return_value = []
        ctx = _make_ctx(role="planner", adapter=adapter, issue_number="")

        mock_result = LLMResult(text="[]", model="test", cost_usd=Decimal("0.005"))
        with patch("sova.llm.client.invoke", new=AsyncMock(return_value=mock_result)):
            role, result = await dispatch(ctx, role_name="planner")

        assert role.name == "planner"
        assert result.success

    def test_planned_task_model(self) -> None:
        from sova.roles.planner import PlannedTask

        task = PlannedTask(
            title="feat(cli): new command",
            description="Add a new CLI command",
            priority="medium",
            complexity="simple",
            component="cli",
            rationale="Needed for workflow",
        )
        assert task.title == "feat(cli): new command"
        assert task.dependencies == []

        dumped = task.model_dump()
        assert dumped["priority"] == "medium"
        assert dumped["complexity"] == "simple"

    def test_parse_response_strips_markdown_fencing(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        json_body = (
            '[{"title":"t","description":"d","priority":"low","complexity":"trivial","component":"c","rationale":"r"}]'
        )
        fenced = f"```json\n{json_body}\n```"
        result = role._parse_response(fenced)
        assert result is not None
        assert len(result) == 1
        assert result[0].title == "t"

    def test_parse_response_strips_markdown_fencing_no_closing(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        json_body = (
            '[{"title":"t","description":"d","priority":"low","complexity":"trivial","component":"c","rationale":"r"}]'
        )
        fenced = f"```json\n{json_body}"
        result = role._parse_response(fenced)
        assert result is not None
        assert len(result) == 1

    def test_parse_response_returns_none_for_non_list(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        result = role._parse_response('{"key": "value"}')
        assert result is None

    def test_parse_response_skips_invalid_items(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        # Missing required fields: item is skipped, returns empty list
        result = role._parse_response('[{"title": "t"}]')
        assert result is not None
        assert result == []

    def test_parse_response_empty_string_returns_empty_list(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        result = role._parse_response("")
        assert result is not None
        assert result == []

    def test_parse_response_whitespace_returns_empty_list(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        result = role._parse_response("   \n\n  ")
        assert result is not None
        assert result == []

    def test_parse_response_prose_before_json(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        json_body = (
            '[{"title":"t","description":"d","priority":"low","complexity":"trivial","component":"c","rationale":"r"}]'
        )
        text = f"Here are the tasks I propose:\n\n{json_body}"
        result = role._parse_response(text)
        assert result is not None
        assert len(result) == 1
        assert result[0].title == "t"

    def test_parse_response_prose_with_fenced_json(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        json_body = (
            '[{"title":"t","description":"d","priority":"low","complexity":"trivial","component":"c","rationale":"r"}]'
        )
        text = f"My analysis below:\n\n```json\n{json_body}\n```\n\nLet me know!"
        result = role._parse_response(text)
        assert result is not None
        assert len(result) == 1

    def test_parse_response_unwraps_object_wrapper(self) -> None:
        import json

        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        task = {
            "title": "t",
            "description": "d",
            "priority": "low",
            "complexity": "trivial",
            "component": "c",
            "rationale": "r",
        }
        text = json.dumps({"tasks": [task]})
        result = role._parse_response(text)
        assert result is not None
        assert len(result) == 1
        assert result[0].title == "t"

    def test_parse_response_unwraps_proposed_tasks_key(self) -> None:
        import json

        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        task = {
            "title": "t",
            "description": "d",
            "priority": "low",
            "complexity": "trivial",
            "component": "c",
            "rationale": "r",
        }
        text = json.dumps({"proposed_tasks": [task]})
        result = role._parse_response(text)
        assert result is not None
        assert len(result) == 1

    def test_parse_response_unknown_dict_returns_none(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        result = role._parse_response('{"unknown_key": "value"}')
        assert result is None

    @pytest.mark.asyncio
    async def test_execute_empty_llm_response_succeeds_with_no_tasks(self) -> None:
        from unittest.mock import patch

        from sova.llm.models import LLMResult
        from sova.roles.planner import PlannerRole

        adapter = _mock_adapter()
        adapter.list_tasks.return_value = []
        ctx = _make_ctx(role="planner", adapter=adapter, issue_number="")

        mock_result = LLMResult(text="", model="test", cost_usd=Decimal("0.005"))
        with patch("sova.llm.client.invoke", new=AsyncMock(return_value=mock_result)):
            role = PlannerRole()
            result = await role.execute(ctx)

        assert result.success
        assert "No tasks proposed" in result.summary

    async def test_gather_open_issues_exception_returns_empty(self) -> None:
        from sova.roles.planner import PlannerRole

        adapter = _mock_adapter()
        adapter.list_tasks.side_effect = RuntimeError("API down")
        ctx = _make_ctx(role="planner", adapter=adapter, issue_number="")

        role = PlannerRole()
        result = await role._gather_open_issues(ctx)
        assert result == []

    def test_read_vision_returns_content(self, tmp_path: Path) -> None:
        from sova.roles.planner import PlannerRole

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "VISION.md").write_text("# Vision\nGoals here.")
        ctx = _make_ctx(role="planner", issue_number="")
        ctx.project_dir = tmp_path

        role = PlannerRole()
        result = role._read_vision(ctx)
        assert "Vision" in result

    def test_read_vision_returns_empty_on_oserror(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.roles.planner import PlannerRole

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "VISION.md").write_text("content")
        ctx = _make_ctx(role="planner", issue_number="")
        ctx.project_dir = tmp_path

        role = PlannerRole()
        with patch.object(Path, "read_text", side_effect=OSError("Permission denied")):
            result = role._read_vision(ctx)
        assert result == ""

    def test_read_vision_returns_empty_when_missing(self, tmp_path: Path) -> None:
        from sova.roles.planner import PlannerRole

        ctx = _make_ctx(role="planner", issue_number="")
        ctx.project_dir = tmp_path

        role = PlannerRole()
        result = role._read_vision(ctx)
        assert result == ""

    async def test_write_handoff_file_exception_non_fatal(self) -> None:
        from unittest.mock import patch

        from sova.roles.planner import PlannedTask, PlannerRole

        role = PlannerRole()
        ctx = _make_ctx(role="planner", issue_number="")
        tasks = [
            PlannedTask(
                title="t",
                description="d",
                priority="low",
                complexity="trivial",
                component="c",
                rationale="r",
            ),
        ]
        with patch("sova.roles.planner.write_handoff_file", side_effect=OSError("disk full")):
            # Should not raise
            await role._write_handoff(ctx, tasks)

    async def test_write_handoff_db_path(self) -> None:
        from unittest.mock import patch

        from sova.roles.planner import PlannedTask, PlannerRole

        role = PlannerRole()
        ctx = _make_ctx(role="planner", issue_number="")
        ctx.task_run_id = 99
        tasks = [
            PlannedTask(
                title="t",
                description="d",
                priority="low",
                complexity="trivial",
                component="c",
                rationale="r",
            ),
        ]
        mock_write_file = AsyncMock()
        mock_write_handoff = AsyncMock()
        with (
            patch("sova.roles.planner.write_handoff_file", mock_write_file),
            patch("sova.roles.planner.write_handoff", mock_write_handoff),
        ):
            await role._write_handoff(ctx, tasks)

        mock_write_handoff.assert_called_once()
        call_args = mock_write_handoff.call_args
        assert call_args[0][0] == 99

    async def test_write_handoff_db_exception_non_fatal(self) -> None:
        from unittest.mock import patch

        from sova.roles.planner import PlannedTask, PlannerRole

        role = PlannerRole()
        ctx = _make_ctx(role="planner", issue_number="")
        ctx.task_run_id = 99
        tasks = [
            PlannedTask(
                title="t",
                description="d",
                priority="low",
                complexity="trivial",
                component="c",
                rationale="r",
            ),
        ]
        with (
            patch("sova.roles.planner.write_handoff_file"),
            patch("sova.roles.planner.write_handoff", new=AsyncMock(side_effect=RuntimeError("DB down"))),
        ):
            # Should not raise
            await role._write_handoff(ctx, tasks)

    async def test_execute_happy_path_writes_handoff_with_issue(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.ipc.handoff import DashboardHandoff
        from sova.llm.models import LLMResult
        from sova.roles.planner import PlannerRole

        llm_response = json.dumps(
            [
                {
                    "title": "feat(cli): add health check command",
                    "description": "Add a health check subcommand.",
                    "priority": "medium",
                    "complexity": "simple",
                    "component": "cli",
                    "rationale": "Useful for monitoring.",
                    "dependencies": [],
                },
            ]
        )

        adapter = _mock_adapter()
        adapter.list_tasks.return_value = []
        ctx = _make_ctx(role="planner", adapter=adapter, issue_number="")

        mock_result = LLMResult(text=llm_response, model="test", cost_usd=Decimal("0.01"))
        mock_write_file = MagicMock()
        with (
            patch("sova.llm.client.invoke", new=AsyncMock(return_value=mock_result)),
            patch("sova.roles.planner.write_handoff_file", mock_write_file),
        ):
            role = PlannerRole()
            result = await role.execute(ctx)

        assert result.success
        mock_write_file.assert_called_once()
        handoff_arg = mock_write_file.call_args[0][1]
        assert isinstance(handoff_arg, DashboardHandoff)
        assert handoff_arg.issue == "planner"
        assert "planned_tasks" in handoff_arg.details

    def test_parse_response_skips_invalid_schema_items(self) -> None:
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        # Valid JSON but invalid schema -- item is skipped, returns empty list
        result = role._parse_response(json.dumps([{"title": "test", "priority": "invalid"}]))
        assert result is not None
        assert result == []

    async def test_write_handoff_includes_create_issues_action(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.ipc.handoff import DashboardHandoff
        from sova.roles.planner import PlannedTask, PlannerRole

        role = PlannerRole()
        ctx = _make_ctx(role="planner", issue_number="")
        tasks = [
            PlannedTask(
                title="feat(cli): new cmd",
                description="Desc",
                priority="medium",
                complexity="simple",
                component="cli",
                rationale="Needed",
            ),
        ]
        mock_write_file = MagicMock()
        with patch("sova.roles.planner.write_handoff_file", mock_write_file):
            await role._write_handoff(ctx, tasks)

        mock_write_file.assert_called_once()
        handoff_arg = mock_write_file.call_args[0][1]
        assert isinstance(handoff_arg, DashboardHandoff)
        assert handoff_arg.status == "awaiting_action"
        assert len(handoff_arg.next_actions) == 1
        assert handoff_arg.next_actions[0].id == "create-issues"
        assert handoff_arg.next_actions[0].style == "approve"

    async def test_write_handoff_no_action_when_no_tasks(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.ipc.handoff import DashboardHandoff
        from sova.roles.planner import PlannerRole

        role = PlannerRole()
        ctx = _make_ctx(role="planner", issue_number="")
        mock_write_file = MagicMock()
        with patch("sova.roles.planner.write_handoff_file", mock_write_file):
            await role._write_handoff(ctx, [])

        mock_write_file.assert_called_once()
        handoff_arg = mock_write_file.call_args[0][1]
        assert isinstance(handoff_arg, DashboardHandoff)
        assert handoff_arg.status == "completed"
        assert handoff_arg.next_actions == []
