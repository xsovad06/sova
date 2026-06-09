"""Tests for sova.roles -- role base class, built-in roles, and dispatcher."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig, RolesConfig
from sova.core.context import ExecutionContext
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
        assert names == ["fetch_task", "research", "extract_memory"]


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
                return_value=PRInfo(number=82, url="https://github.com/x/y/pull/82"),
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
        assert "no pr" in result.error.lower()

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
        assert names == {"triage", "researcher", "developer", "reviewer"}

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
        assert dashboard_handoff.next_actions[0].id == "integrate"
        assert dashboard_handoff.next_actions[0].label == "Integrate PR"
        assert dashboard_handoff.next_actions[1].id == "approve"
        assert dashboard_handoff.next_actions[1].label == "Merge Only"

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
