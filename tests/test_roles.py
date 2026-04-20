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
    await init_db()
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _mock_adapter(state: TaskState = TaskState.BACKLOG) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_state.return_value = state
    adapter.get_task.return_value = Task(
        id="42", title="Test issue", body="Some description", state=state
    )
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
        adapter.transition_state.assert_awaited_with("42", TaskState.TRIAGED)

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

    async def test_posts_assessment_comment(self) -> None:
        from sova.roles.triage import TriageRole

        adapter = _mock_adapter(TaskState.BACKLOG)
        ctx = _make_ctx(role="triage", state=TaskState.BACKLOG, adapter=adapter)
        role = TriageRole()

        await role.execute(ctx)

        adapter.post_comment.assert_awaited_once()
        comment_body = adapter.post_comment.call_args[0][1]
        assert "triage" in comment_body.lower() or "assessment" in comment_body.lower()


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
        from sova.roles.researcher import ResearcherRole

        adapter = _mock_adapter(TaskState.TRIAGED)
        ctx = _make_ctx(role="researcher", state=TaskState.TRIAGED, adapter=adapter)
        role = ResearcherRole()

        result = await role.execute(ctx)

        assert result.success
        assert result.output_state == TaskState.RESEARCHED
        adapter.transition_state.assert_awaited_with("42", TaskState.RESEARCHED)

    async def test_rejects_backlog_state(self) -> None:
        from sova.roles.researcher import ResearcherRole

        adapter = _mock_adapter(TaskState.BACKLOG)
        ctx = _make_ctx(role="researcher", state=TaskState.BACKLOG, adapter=adapter)
        role = ResearcherRole()

        result = await role.execute(ctx)

        assert not result.success

    async def test_posts_research_comment(self) -> None:
        from sova.roles.researcher import ResearcherRole

        adapter = _mock_adapter(TaskState.TRIAGED)
        ctx = _make_ctx(role="researcher", state=TaskState.TRIAGED, adapter=adapter)
        role = ResearcherRole()

        await role.execute(ctx)

        adapter.post_comment.assert_awaited_once()


# ---------------------------------------------------------------------------
# Developer role
# ---------------------------------------------------------------------------


class TestDeveloperRole:
    def test_metadata(self) -> None:
        from sova.roles.developer import DeveloperRole

        role = DeveloperRole()
        assert role.name == "developer"
        assert TaskState.RESEARCHED in role.allowed_input_states
        assert role.output_state == TaskState.DONE

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

        mock_result = WorkflowResult(
            success=True, final_status=TaskStatus.DONE, task_run_id=1
        )
        with patch.object(WorkflowEngine, "run", new=AsyncMock(return_value=mock_result)):
            result = await role.execute(ctx)

        assert result.success

    def test_get_steps_returns_developer_pipeline(self) -> None:
        from sova.roles.developer import DeveloperRole

        role = DeveloperRole()
        steps = role.get_steps()
        names = [s.name for s in steps]
        assert "sync" in names
        assert "develop" in names
        assert "push" in names
        assert "create_pr" in names
        assert "complete" in names


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
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(
            role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter, pr_number=99
        )
        role = ReviewerRole()

        result = await role.execute(ctx)

        assert result.success

    async def test_execute_requires_pr_number(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        adapter = _mock_adapter(TaskState.IN_REVIEW)
        ctx = _make_ctx(role="reviewer", state=TaskState.IN_REVIEW, adapter=adapter)
        role = ReviewerRole()

        result = await role.execute(ctx)

        assert not result.success
        assert "pr" in result.error.lower()

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
                suitability="ready", confidence=0.5, reasoning="test",
                estimated_complexity=val,
            )
            assert a.estimated_complexity == val

    def test_invalid_complexity_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TaskAssessment(
                suitability="ready", confidence=0.5, reasoning="test",
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
        task = Task(id="1", title="Test", body="A description", state=TaskState.BACKLOG)
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

    async def test_researcher_assess(self) -> None:
        from sova.roles.researcher import ResearcherRole

        role = ResearcherRole()
        task = Task(id="1", title="Test", state=TaskState.TRIAGED)
        assessment = await role.assess_task(task)

        assert assessment.suitability == "needs_research"
        assert assessment.suggested_role == "researcher"

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

        adapter = _mock_adapter(TaskState.BACKLOG)
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="Has a description", state=TaskState.BACKLOG,
        )
        ctx = _make_ctx(role="triage", state=TaskState.BACKLOG, adapter=adapter)
        role = TriageRole()

        await role.execute(ctx)

        adapter.add_label.assert_awaited_once_with("42", "agent:ready")

    async def test_applies_needs_spec_label(self) -> None:
        from sova.roles.triage import TriageRole

        adapter = _mock_adapter(TaskState.BACKLOG)
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="", state=TaskState.BACKLOG,
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
