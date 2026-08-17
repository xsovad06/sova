"""Tests for sova.core.steps.spec, sova.dashboard.services.spec_service, and spec router."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig, SpecConfig
from sova.core.context import ExecutionContext
from sova.core.steps.spec import (
    SpecStep,
    _complexity_rank,
    _extract_complexity,
    _extract_spec_content,
    _make_slug,
    _research_says_implemented,
    _sanitize_issue_number,
    _text_has_open_questions,
)
from sova.dashboard.services.spec_service import find_spec_file
from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for step tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _mock_adapter(state: TaskState = TaskState.TRIAGED) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_state.return_value = state
    adapter.get_task.return_value = Task(
        id="42",
        title="Test issue",
        body="Some description\n\n**Complexity**: moderate",
        state=state,
    )
    return adapter


def _make_ctx(
    *,
    role: str = "researcher",
    project_dir: Path | None = None,
    spec_config: SpecConfig | None = None,
    **kwargs,
) -> ExecutionContext:
    config = ProjectConfig()
    if spec_config:
        config = ProjectConfig(spec=spec_config)
    defaults = {
        "project_dir": project_dir or Path("/tmp/test"),
        "config": config,
        "adapter": _mock_adapter(),
        "issue_number": "42",
        "role": role,
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


class TestComplexityRank:
    def test_ordering(self) -> None:
        assert _complexity_rank("always") < _complexity_rank("trivial")
        assert _complexity_rank("trivial") < _complexity_rank("simple")
        assert _complexity_rank("simple") < _complexity_rank("moderate")
        assert _complexity_rank("moderate") < _complexity_rank("complex")
        assert _complexity_rank("complex") < _complexity_rank("never")

    def test_unknown_defaults_to_moderate(self) -> None:
        assert _complexity_rank("unknown") == _complexity_rank("moderate")

    def test_case_insensitive(self) -> None:
        assert _complexity_rank("SIMPLE") == _complexity_rank("simple")


class TestExtractComplexity:
    def test_extracts_complexity(self) -> None:
        body = "Some text\n\n**Complexity**: complex\n\nMore text"
        assert _extract_complexity(body) == "complex"

    def test_defaults_to_moderate(self) -> None:
        assert _extract_complexity("No complexity here") == "moderate"

    def test_case_insensitive(self) -> None:
        body = "**complexity**: Simple"
        assert _extract_complexity(body) == "simple"


class TestResearchSaysImplemented:
    def test_detects_already_fully_implemented(self) -> None:
        body = "## Research\n\nThe feature is already fully implemented in the codebase.\n"
        assert _research_says_implemented(body)

    def test_detects_already_implemented(self) -> None:
        body = "## Research\n\nThis has already been implemented via PR #42.\n"
        assert _research_says_implemented(body)

    def test_detects_already_complete(self) -> None:
        body = "## Research\n\nThe work is already complete.\n"
        assert _research_says_implemented(body)

    def test_detects_no_remaining_work(self) -> None:
        body = "## Research\n\nThere is no remaining work for this issue.\n"
        assert _research_says_implemented(body)

    def test_returns_false_without_research_section(self) -> None:
        body = "Some description\n\n**Complexity**: moderate\n"
        assert not _research_says_implemented(body)

    def test_returns_false_with_normal_research(self) -> None:
        body = "## Research\n\nThis requires adding a new endpoint to the API.\n"
        assert not _research_says_implemented(body)

    def test_returns_false_with_empty_body(self) -> None:
        assert not _research_says_implemented("")

    def test_case_insensitive(self) -> None:
        body = "## Research\n\nThe feature is ALREADY FULLY IMPLEMENTED.\n"
        assert _research_says_implemented(body)

    def test_detects_has_been_implemented(self) -> None:
        body = "## Research\n\nThe feature has been implemented in the latest release.\n"
        assert _research_says_implemented(body)

    def test_ignores_implemented_outside_research(self) -> None:
        body = "Already implemented in v1.\n\n## Research\n\nNeeds new endpoint.\n"
        assert not _research_says_implemented(body)


class TestTextHasOpenQuestions:
    def test_no_section_returns_false(self) -> None:
        assert not _text_has_open_questions("# Spec\n\n## Solution\n\nDo stuff\n")

    def test_empty_section_returns_false(self) -> None:
        assert not _text_has_open_questions("# Spec\n\n## Open Questions\n\n(Omit this section)\n")

    def test_with_questions_returns_true(self) -> None:
        assert _text_has_open_questions("# Spec\n\n## Open Questions\n\n- Should we use X or Y?\n- What about Z?\n")

    def test_empty_text_returns_false(self) -> None:
        assert not _text_has_open_questions("")


class TestMakeSlug:
    def test_simple_title(self) -> None:
        assert _make_slug("Add user authentication") == "add-user-authentication"

    def test_truncates_long_title(self) -> None:
        slug = _make_slug("A" * 100)
        assert len(slug) <= 40

    def test_strips_special_characters(self) -> None:
        assert _make_slug("feat(core): add new feature!") == "feat-core-add-new-feature"

    def test_collapses_multiple_hyphens(self) -> None:
        assert _make_slug("some---weird   title") == "some-weird-title"

    def test_strips_leading_trailing_hyphens(self) -> None:
        assert _make_slug("--leading and trailing--") == "leading-and-trailing"

    def test_empty_title(self) -> None:
        assert _make_slug("") == "spec"

    def test_all_special_chars(self) -> None:
        assert _make_slug("!@#$%") == "spec"


class TestExtractSpecContent:
    def test_extracts_fenced_markdown(self) -> None:
        text = "Here is the spec:\n\n```markdown\n# Spec: Test\n\n**Status**: draft\n```\n\nDone."
        result = _extract_spec_content(text)
        assert result.startswith("# Spec: Test")
        assert "**Status**: draft" in result

    def test_extracts_fenced_md(self) -> None:
        text = "```md\n# Spec\ncontent\n```"
        result = _extract_spec_content(text)
        assert result == "# Spec\ncontent"

    def test_extracts_raw_spec_heading(self) -> None:
        text = "Some preamble.\n\n# Spec: Feature\n\n**Status**: draft\n\n## Solution\n\nDo things."
        result = _extract_spec_content(text)
        assert result.startswith("# Spec: Feature")

    def test_prefers_fenced_block(self) -> None:
        text = "# Spec: Wrong\n\n```markdown\n# Spec: Right\n**Status**: draft\n```"
        result = _extract_spec_content(text)
        assert "Right" in result

    def test_returns_full_text_as_fallback(self) -> None:
        text = "Just some text without a spec heading or fence."
        result = _extract_spec_content(text)
        assert result == text

    def test_handles_empty_text(self) -> None:
        assert _extract_spec_content("") == ""


class TestFindSpecFile:
    def test_finds_matching_file(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test-feature.md"
        spec.write_text("# Spec")
        assert find_spec_file("42", project_dir=tmp_path) == spec

    def test_returns_none_when_no_match(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "99-other.md"
        spec.write_text("# Spec")
        assert find_spec_file("42", project_dir=tmp_path) is None

    def test_returns_none_when_no_dir(self, tmp_path: Path) -> None:
        assert find_spec_file("42", project_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# SpecStep
# ---------------------------------------------------------------------------


class TestSanitizeIssueNumber:
    def test_strips_forward_slash(self) -> None:
        assert _sanitize_issue_number("../etc/passwd") == "etcpasswd"

    def test_strips_backslash(self) -> None:
        assert _sanitize_issue_number("..\\etc\\passwd") == "etcpasswd"

    def test_strips_leading_dots(self) -> None:
        assert _sanitize_issue_number("..42") == "42"

    def test_preserves_normal_issue_number(self) -> None:
        assert _sanitize_issue_number("42") == "42"

    def test_preserves_alphanumeric(self) -> None:
        assert _sanitize_issue_number("RHCLOUD-42") == "RHCLOUD-42"


class TestSpecStep:
    async def test_can_skip_when_threshold_never(self) -> None:
        ctx = _make_ctx(spec_config=SpecConfig(threshold="never"))
        step = SpecStep()
        assert await step.can_skip(ctx)

    async def test_cannot_skip_when_threshold_always(self) -> None:
        ctx = _make_ctx(spec_config=SpecConfig(threshold="always"))
        step = SpecStep()
        assert not await step.can_skip(ctx)

    async def test_skips_when_task_below_threshold(self) -> None:
        # Task is "trivial", threshold is "moderate" -> skip
        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="**Complexity**: trivial", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(spec_config=SpecConfig(threshold="moderate"), adapter=adapter)
        step = SpecStep()
        assert await step.can_skip(ctx)

    async def test_does_not_skip_when_task_at_threshold(self) -> None:
        # Task is "moderate", threshold is "moderate" -> do not skip
        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="**Complexity**: moderate", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(spec_config=SpecConfig(threshold="moderate"), adapter=adapter)
        step = SpecStep()
        assert not await step.can_skip(ctx)

    async def test_does_not_skip_when_task_above_threshold(self) -> None:
        # Task is "complex", threshold is "simple" -> do not skip
        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="**Complexity**: complex", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(spec_config=SpecConfig(threshold="simple"), adapter=adapter)
        step = SpecStep()
        assert not await step.can_skip(ctx)

    async def test_skips_when_research_says_implemented(self) -> None:
        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42",
            title="Test",
            body="## Research\n\nThis is already fully implemented.\n\n**Complexity**: moderate",
            state=TaskState.TRIAGED,
        )
        ctx = _make_ctx(spec_config=SpecConfig(threshold="always"), adapter=adapter)
        step = SpecStep()
        assert await step.can_skip(ctx)

    async def test_does_not_skip_when_research_is_normal(self) -> None:
        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42",
            title="Test",
            body="## Research\n\nNeeds a new API endpoint.\n\n**Complexity**: moderate",
            state=TaskState.TRIAGED,
        )
        ctx = _make_ctx(spec_config=SpecConfig(threshold="always"), adapter=adapter)
        step = SpecStep()
        assert not await step.can_skip(ctx)

    async def test_skips_when_already_completed(self) -> None:
        ctx = _make_ctx(spec_config=SpecConfig(threshold="always"))
        ctx = ExecutionContext(
            project_dir=ctx.project_dir,
            config=ctx.config,
            adapter=ctx.adapter,
            issue_number="42",
            completed_steps=frozenset({"spec"}),
        )
        step = SpecStep()
        assert await step.can_skip(ctx)

    async def test_execute_writes_spec_from_llm_response(self, tmp_path: Path) -> None:
        """Step writes the spec file from the LLM response text."""
        from sova.llm.models import LLMResult

        (tmp_path / ".claude" / "agent-control").mkdir(parents=True, exist_ok=True)

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42",
            title="Add widget feature",
            body="Add a widget.\n\n**Complexity**: simple",
            state=TaskState.TRIAGED,
        )
        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
            adapter=adapter,
        )

        step = SpecStep()
        spec_text = (
            "# Spec: Add widget feature\n\n"
            "**Status**: draft\n**Complexity**: simple\n\n"
            "## Solution\n\nAdd the widget.\n"
        )
        llm_result = LLMResult(
            text=spec_text, model="sonnet", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=200
        )
        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        spec_path = find_spec_file("42", project_dir=tmp_path)
        assert spec_path is not None
        assert "Add widget feature" in spec_path.read_text()

    async def test_execute_writes_fenced_spec(self, tmp_path: Path) -> None:
        """Step extracts spec from markdown-fenced LLM response."""
        from sova.llm.models import LLMResult

        (tmp_path / ".claude" / "agent-control").mkdir(parents=True, exist_ok=True)

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="body\n\n**Complexity**: simple", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
            adapter=adapter,
        )

        step = SpecStep()
        llm_response = (
            "Here is the spec:\n\n```markdown\n"
            "# Spec: Test\n\n**Status**: draft\n**Complexity**: simple\n\n"
            "## Solution\n\nDo things.\n```\n\nLet me know if changes needed."
        )
        llm_result = LLMResult(
            text=llm_response, model="sonnet", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=200
        )
        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        spec_path = find_spec_file("42", project_dir=tmp_path)
        assert spec_path is not None
        content = spec_path.read_text()
        assert content.startswith("# Spec: Test")
        assert "Let me know" not in content

    async def test_execute_success_auto_approve(self, tmp_path: Path) -> None:
        from sova.llm.models import LLMResult

        (tmp_path / ".claude" / "agent-control").mkdir(parents=True, exist_ok=True)

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Simple task", body="Do it.\n\n**Complexity**: simple", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
            adapter=adapter,
        )
        step = SpecStep()

        spec_text = "# Spec: Simple task\n\n**Status**: draft\n**Complexity**: simple\n\n## Solution\n\nDo stuff\n"
        llm_result = LLMResult(
            text=spec_text, model="sonnet", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=50
        )
        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        assert "auto-approved" in result.summary

        spec_path = find_spec_file("42", project_dir=tmp_path)
        assert spec_path is not None
        assert spec_path.read_text().count("**Status**: approved") == 1

        import json

        handoff_files = list((tmp_path / ".claude" / "agent-control").glob("handoff*.json"))
        assert handoff_files, "Expected handoff file to be written for auto-approve"
        handoff = json.loads(handoff_files[0].read_text())
        actions = handoff.get("next_actions", [])
        develop_action = next((a for a in actions if a["id"] == "develop"), None)
        assert develop_action is not None, "Expected 'develop' action in handoff"
        assert develop_action["auto_execute"] is True, "develop action must have auto_execute=True"

    async def test_execute_auto_approve_already_approved(self, tmp_path: Path) -> None:
        """Auto-approve succeeds when LLM returns spec already marked approved."""
        from sova.llm.models import LLMResult

        (tmp_path / ".claude" / "agent-control").mkdir(parents=True, exist_ok=True)

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="body\n\n**Complexity**: simple", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
            adapter=adapter,
        )
        step = SpecStep()

        spec_text = "# Spec: Test\n\n**Status**: approved\n**Complexity**: simple\n\n## Solution\n\nDo stuff\n"
        llm_result = LLMResult(
            text=spec_text, model="sonnet", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=50
        )
        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success, f"Expected success but got: {result.error}"
        assert "auto-approved" in result.summary

    async def test_execute_auto_approve_no_status_line(self, tmp_path: Path) -> None:
        """Auto-approve fails when spec has no Status line at all."""
        from sova.llm.models import LLMResult

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="body\n\n**Complexity**: simple", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
            adapter=adapter,
        )
        step = SpecStep()

        spec_text = "# Spec: Test\n\n**Complexity**: simple\n\n## Solution\n\nDo stuff\n"
        llm_result = LLMResult(
            text=spec_text, model="sonnet", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=50
        )
        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert not result.success
        assert "status line not found" in result.summary.lower()

    async def test_execute_handoff_on_open_questions(self, tmp_path: Path) -> None:
        from sova.llm.models import LLMResult

        (tmp_path / ".claude" / "agent-control").mkdir(parents=True, exist_ok=True)

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="body\n\n**Complexity**: simple", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
            adapter=adapter,
        )
        step = SpecStep()

        spec_text = (
            "# Spec: Test\n\n**Status**: draft\n**Complexity**: simple\n\n"
            "## Open Questions\n\n- Should we use X or Y?\n"
        )
        llm_result = LLMResult(
            text=spec_text, model="sonnet", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=50
        )
        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        assert result.awaiting_approval
        assert "awaiting approval" in result.summary

    async def test_execute_handoff_on_complexity(self, tmp_path: Path) -> None:
        from sova.llm.models import LLMResult

        (tmp_path / ".claude" / "agent-control").mkdir(parents=True, exist_ok=True)

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="body\n\n**Complexity**: complex", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
            adapter=adapter,
        )
        step = SpecStep()

        spec_text = "# Spec: Test\n\n**Status**: draft\n**Complexity**: complex\n\n## Solution\n\nDo stuff\n"
        llm_result = LLMResult(
            text=spec_text, model="sonnet", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=50
        )
        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        assert result.awaiting_approval
        assert "awaiting approval" in result.summary

    async def test_spec_step_pauses_pipeline_for_manual_approval(self, tmp_path: Path) -> None:
        """Integration: SpecStep returns awaiting_approval=True for complex specs,
        causing WorkflowEngine to set TaskRun status to 'awaiting_approval'."""
        from sova.core.steps.base import GateCheckResult as GCR
        from sova.core.workflow import WorkflowEngine
        from sova.db.models import TaskRun
        from sova.db.session import get_session
        from sova.llm.models import LLMResult

        (tmp_path / ".claude" / "agent-control").mkdir(parents=True, exist_ok=True)

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Complex task", body="body\n\n**Complexity**: complex", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
            adapter=adapter,
        )

        class NeverReachedStep:
            name = "should_not_run"
            max_retries = 0

            async def can_skip(self, ctx_inner):
                return False

            async def execute(self, ctx_inner):
                raise AssertionError("Pipeline should have paused before reaching this step")

            async def validate_output(self, ctx_inner):
                return GCR(passed=True)

        spec_step = SpecStep()
        spec_text = "# Spec: Complex task\n\n**Status**: draft\n**Complexity**: complex\n\n## Solution\n\nDo stuff\n"
        llm_result = LLMResult(
            text=spec_text, model="sonnet", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=50
        )

        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result):
            engine = WorkflowEngine(steps=[spec_step, NeverReachedStep()], ctx=ctx)
            result = await engine.run()

        from sova.core.state import TaskStatus

        assert result.final_status == TaskStatus.AWAITING_APPROVAL
        assert not result.success

        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.status == "awaiting_approval"
            assert task_run.current_step == "spec"

    async def test_auto_approved_spec_does_not_pause(self, tmp_path: Path) -> None:
        """Auto-approved specs should NOT set awaiting_approval."""
        from sova.llm.models import LLMResult

        (tmp_path / ".claude" / "agent-control").mkdir(parents=True, exist_ok=True)

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Simple task", body="body\n\n**Complexity**: simple", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
            adapter=adapter,
        )
        step = SpecStep()

        spec_text = "# Spec: Simple task\n\n**Status**: draft\n**Complexity**: simple\n\n## Solution\n\nDo stuff\n"
        llm_result = LLMResult(
            text=spec_text, model="sonnet", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=50
        )
        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        assert not result.awaiting_approval

    async def test_execute_runtime_error(self) -> None:
        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(id="42", title="Test", body="body", state=TaskState.TRIAGED)
        ctx = _make_ctx(adapter=adapter)
        step = SpecStep()

        with patch(
            "sova.core.steps.spec.invoke",
            new_callable=AsyncMock,
            side_effect=RuntimeError("CLI failed"),
        ):
            result = await step.execute(ctx)

        assert not result.success
        assert "CLI failed" in result.error

    async def test_execute_prompt_injection_error(self) -> None:
        from sova.llm.guard import PromptInjectionError, ScanResult

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(id="42", title="Test", body="body", state=TaskState.TRIAGED)
        ctx = _make_ctx(adapter=adapter)
        step = SpecStep()

        scan = ScanResult(safe=False, risk_score=0.95)
        with patch(
            "sova.core.steps.spec.invoke",
            new_callable=AsyncMock,
            side_effect=PromptInjectionError(scan),
        ):
            result = await step.execute(ctx)

        assert not result.success
        assert "Spec generation failed" in result.summary

    async def test_execute_creates_specs_directory(self, tmp_path: Path) -> None:
        """Step creates .claude/specs/ directory if it doesn't exist."""
        from sova.llm.models import LLMResult

        (tmp_path / ".claude" / "agent-control").mkdir(parents=True, exist_ok=True)

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="New feature", body="body\n\n**Complexity**: simple", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
            adapter=adapter,
        )
        step = SpecStep()

        spec_text = "# Spec: New feature\n\n**Status**: draft\n**Complexity**: simple\n\n## Solution\n\nDo it.\n"
        llm_result = LLMResult(
            text=spec_text, model="sonnet", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=200
        )
        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        assert (tmp_path / ".claude" / "specs").is_dir()
        spec_path = find_spec_file("42", project_dir=tmp_path)
        assert spec_path is not None

    async def test_execute_includes_task_body_in_prompt(self, tmp_path: Path) -> None:
        """Step passes the issue body to the LLM prompt."""
        from sova.llm.models import LLMResult

        (tmp_path / ".claude" / "agent-control").mkdir(parents=True, exist_ok=True)

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42",
            title="Test",
            body="Custom body with details.\n\n## Research\n\nFindings here.\n\n**Complexity**: simple",
            state=TaskState.TRIAGED,
        )
        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
            adapter=adapter,
        )
        step = SpecStep()

        spec_text = "# Spec: Test\n\n**Status**: draft\n**Complexity**: simple\n\n## Solution\n\nDo stuff\n"
        llm_result = LLMResult(
            text=spec_text, model="sonnet", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=50
        )
        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result) as mock_invoke:
            await step.execute(ctx)

        prompt = mock_invoke.call_args[0][0]
        assert "Custom body with details" in prompt
        assert "Findings here" in prompt

    async def test_execute_empty_llm_response(self, tmp_path: Path) -> None:
        """Step fails gracefully when LLM returns empty text."""
        from sova.llm.models import LLMResult

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(id="42", title="Test", body="body", state=TaskState.TRIAGED)
        ctx = _make_ctx(project_dir=tmp_path, adapter=adapter)
        step = SpecStep()

        llm_result = LLMResult(text="", model="sonnet", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=0)
        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert not result.success
        assert "empty" in result.error.lower()

    async def test_execute_write_spec_ioerror(self, tmp_path: Path) -> None:
        """IOError during spec file write returns graceful failure with cost."""
        from sova.llm.models import LLMResult

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(
            id="42", title="Test", body="body\n\n**Complexity**: simple", state=TaskState.TRIAGED
        )
        ctx = _make_ctx(project_dir=tmp_path, adapter=adapter)
        step = SpecStep()

        spec_text = "# Spec: Test\n\n**Status**: draft\n**Complexity**: simple\n"
        llm_result = LLMResult(
            text=spec_text, model="sonnet", cost_usd=Decimal("0.04"), input_tokens=100, output_tokens=50
        )
        with (
            patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.core.steps.spec._write_spec_file", side_effect=IOError("disk full")),
        ):
            result = await step.execute(ctx)

        assert not result.success
        assert "Failed to write spec file" in result.summary
        assert result.cost_usd == Decimal("0.04")

    async def test_execute_empty_llm_response_preserves_cost(self, tmp_path: Path) -> None:
        """Empty response failure path reports the accrued cost."""
        from sova.llm.models import LLMResult

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(id="42", title="Test", body="body", state=TaskState.TRIAGED)
        ctx = _make_ctx(project_dir=tmp_path, adapter=adapter)
        step = SpecStep()

        llm_result = LLMResult(text="", model="sonnet", cost_usd=Decimal("0.03"), input_tokens=100, output_tokens=0)
        with patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert not result.success
        assert result.cost_usd == Decimal("0.03")

    async def test_validate_output_passes(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        (specs_dir / "42-test.md").write_text("# Spec")

        ctx = _make_ctx(project_dir=tmp_path)
        step = SpecStep()
        gate = await step.validate_output(ctx)
        assert gate.passed

    async def test_validate_output_fails(self, tmp_path: Path) -> None:
        ctx = _make_ctx(project_dir=tmp_path)
        step = SpecStep()
        gate = await step.validate_output(ctx)
        assert not gate.passed

    async def test_step_name(self) -> None:
        step = SpecStep()
        assert step.name == "spec"


# ---------------------------------------------------------------------------
# Step registry
# ---------------------------------------------------------------------------


class TestStepRegistryWithSpec:
    def test_researcher_pipeline_includes_spec(self) -> None:
        from sova.core.steps import get_researcher_steps

        steps = get_researcher_steps()
        names = [s.name for s in steps]
        assert "spec" in names
        # Spec comes after research and before extract_memory
        assert names.index("spec") == names.index("research") + 1
        assert names.index("spec") < names.index("extract_memory")

    def test_developer_pipeline_does_not_include_spec(self) -> None:
        from sova.core.steps import get_developer_steps

        steps = get_developer_steps()
        names = [s.name for s in steps]
        assert "spec" not in names


# ---------------------------------------------------------------------------
# Spec service
# ---------------------------------------------------------------------------


class TestSpecService:
    def test_read_spec(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import read_spec

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        (specs_dir / "42-test-feature.md").write_text(
            "# Spec: Test Feature\n\n"
            "**Issue**: #42\n"
            "**Status**: draft\n"
            "**Created**: 2026-06-17\n"
            "**Complexity**: moderate\n\n"
            "## Problem\n\nSome problem\n\n"
            "## Open Questions\n\n- Question 1?\n- Question 2?\n"
        )

        result = read_spec("42", project_dir=tmp_path)
        assert result is not None
        assert result["status"] == "draft"
        assert result["complexity"] == "moderate"
        assert result["created"] == "2026-06-17"
        assert result["title"] == "Test Feature"
        assert result["has_open_questions"]
        assert len(result["open_questions"]) == 2

    def test_read_spec_not_found(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import read_spec

        result = read_spec("99", project_dir=tmp_path)
        assert result is None

    def test_approve_spec(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import approve_spec

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test.md"
        spec.write_text("# Spec\n\n**Status**: draft\n**Complexity**: simple\n")

        result = approve_spec("42", project_dir=tmp_path)
        assert "error" not in result
        assert result["status"] == "approved"
        assert "**Status**: approved" in spec.read_text()

    def test_approve_spec_not_found(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import approve_spec

        result = approve_spec("99", project_dir=tmp_path)
        assert "error" in result

    def test_reject_spec(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import reject_spec

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test.md"
        spec.write_text("# Spec\n\n**Status**: draft\n")

        result = reject_spec("42", project_dir=tmp_path)
        assert result["status"] == "rejected"
        assert "**Status**: rejected" in spec.read_text()

    def test_list_pending_specs(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import list_pending_specs

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        (specs_dir / "42-draft.md").write_text("# Spec\n\n**Status**: draft\n**Complexity**: simple\n")
        (specs_dir / "43-approved.md").write_text("# Spec\n\n**Status**: approved\n**Complexity**: simple\n")
        (specs_dir / "44-another.md").write_text("# Spec\n\n**Status**: draft\n**Complexity**: moderate\n")

        results = list_pending_specs(project_dir=tmp_path)
        assert len(results) == 2
        issue_numbers = {r["issue_number"] for r in results}
        assert issue_numbers == {"42", "44"}

    def test_list_all_specs(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import list_all_specs

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        draft = "# Spec\n\n**Status**: draft\n**Complexity**: simple\n**Created**: 2026-06-20\n"
        approved = "# Spec\n\n**Status**: approved\n**Complexity**: moderate\n**Created**: 2026-06-18\n"
        rejected = "# Spec\n\n**Status**: rejected\n**Complexity**: complex\n**Created**: 2026-06-19\n"
        (specs_dir / "42-draft.md").write_text(draft)
        (specs_dir / "43-approved.md").write_text(approved)
        (specs_dir / "44-rejected.md").write_text(rejected)

        results = list_all_specs(project_dir=tmp_path)
        assert len(results) == 3
        # Draft first, then approved, then rejected
        assert results[0]["status"] == "draft"
        assert results[1]["status"] == "approved"
        assert results[2]["status"] == "rejected"

    def test_list_all_specs_empty_dir(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import list_all_specs

        results = list_all_specs(project_dir=tmp_path)
        assert results == []

    def test_list_all_specs_sort_within_status(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import list_all_specs

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        (specs_dir / "10-old.md").write_text("# Spec\n\n**Status**: draft\n**Created**: 2026-06-10\n")
        (specs_dir / "20-new.md").write_text("# Spec\n\n**Status**: draft\n**Created**: 2026-06-20\n")

        results = list_all_specs(project_dir=tmp_path)
        assert len(results) == 2
        # Newest first within the draft group
        assert results[0]["issue_number"] == "20"
        assert results[1]["issue_number"] == "10"

    def test_approve_spec_non_draft_rejected(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import approve_spec

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test.md"
        spec.write_text("# Spec\n\n**Status**: approved\n**Complexity**: simple\n")

        result = approve_spec("42", project_dir=tmp_path)
        assert "error" in result
        assert "already" in result["error"]

    def test_reject_spec_not_found(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import reject_spec

        result = reject_spec("99", project_dir=tmp_path)
        assert "error" in result

    def test_reject_spec_non_draft_non_rejected(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import reject_spec

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test.md"
        spec.write_text("# Spec\n\n**Status**: approved\n**Complexity**: simple\n")

        result = reject_spec("42", project_dir=tmp_path)
        assert "error" in result
        assert "cannot reject" in result["error"]

    def test_iter_all_specs_skips_non_md_files(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import list_all_specs

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        (specs_dir / "42-draft.md").write_text("# Spec\n\n**Status**: draft\n")
        (specs_dir / "notes.txt").write_text("not a spec")

        results = list_all_specs(project_dir=tmp_path)
        assert len(results) == 1

    def test_iter_all_specs_skips_no_issue_prefix(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import list_all_specs

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        (specs_dir / "no-issue-number.md").write_text("# Spec\n\n**Status**: draft\n")

        results = list_all_specs(project_dir=tmp_path)
        assert results == []

    def test_iter_all_specs_handles_parse_error(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import list_all_specs

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        (specs_dir / "42-good.md").write_text("# Spec\n\n**Status**: draft\n")

        with patch("sova.dashboard.services.spec_service._parse_spec", side_effect=ValueError("bad")):
            results = list_all_specs(project_dir=tmp_path)
        assert results == []

    def test_write_answers(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import write_answers

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test.md"
        spec.write_text(
            "# Spec\n\n**Status**: draft\n\n## Open Questions\n\n- Should we use X or Y?\n- What about Z?\n"
        )

        write_answers("42", {"0": "Use X", "1": "Z is fine"}, project_dir=tmp_path)

        text = spec.read_text()
        assert "Q: Should we use X or Y? A: Use X" in text
        assert "Q: What about Z? A: Z is fine" in text

    def test_write_answers_round_trip(self, tmp_path: Path) -> None:
        """Answers written by write_answers are recovered by _extract_open_questions."""
        from sova.dashboard.services.spec_service import _extract_open_questions, write_answers

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test.md"
        spec.write_text(
            "# Spec\n\n**Status**: draft\n\n## Open Questions\n\n- Should we use X or Y?\n- What about Z?\n"
        )

        write_answers("42", {"0": "Use X", "1": "Z is fine"}, project_dir=tmp_path)

        text = spec.read_text()
        questions = _extract_open_questions(text)
        assert len(questions) == 2
        assert questions[0]["text"] == "Should we use X or Y?"
        assert questions[0]["answer"] == "Use X"
        assert questions[1]["text"] == "What about Z?"
        assert questions[1]["answer"] == "Z is fine"

    def test_write_answers_no_spec_file(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import write_answers

        # Should not raise
        write_answers("99", {"0": "answer"}, project_dir=tmp_path)

    def test_write_answers_empty_answers(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import write_answers

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test.md"
        original = "# Spec\n\n**Status**: draft\n\n## Open Questions\n\n- Q1?\n"
        spec.write_text(original)

        write_answers("42", {}, project_dir=tmp_path)
        # File unchanged (early return on empty answers)
        assert spec.read_text() == original

    def test_write_answers_no_questions(self, tmp_path: Path) -> None:
        from sova.dashboard.services.spec_service import write_answers

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test.md"
        original = "# Spec\n\n**Status**: draft\n"
        spec.write_text(original)

        write_answers("42", {"0": "answer"}, project_dir=tmp_path)
        assert spec.read_text() == original

    def test_extract_open_questions_skips_paren_lines(self) -> None:
        from sova.dashboard.services.spec_service import _extract_open_questions

        text = "## Open Questions\n\n(this is a note)\n- Real question?\n"
        questions = _extract_open_questions(text)
        assert len(questions) == 1
        assert questions[0]["text"] == "Real question?"
        assert questions[0]["answer"] == ""

    def test_extract_open_questions_parses_qa_format(self) -> None:
        """Answered questions (Q: ... A: ...) are split into text + answer fields."""
        from sova.dashboard.services.spec_service import _extract_open_questions

        text = "## Open Questions\n\n- Q: Should we use X or Y? A: Use X\n- Q: What about Z? A: Z is fine\n"
        questions = _extract_open_questions(text)
        assert len(questions) == 2
        assert questions[0]["text"] == "Should we use X or Y?"
        assert questions[0]["answer"] == "Use X"
        assert questions[1]["text"] == "What about Z?"
        assert questions[1]["answer"] == "Z is fine"

    def test_extract_open_questions_qa_format_with_complex_answer(self) -> None:
        """Non-greedy split on first ' A: ' preserves complex answers with parentheses."""
        from sova.dashboard.services.spec_service import _extract_open_questions

        text = "## Open Questions\n\n- Q: Which approach? A: Option 1 (sounddevice + POST to /transcribe)\n"
        questions = _extract_open_questions(text)
        assert len(questions) == 1
        assert questions[0]["text"] == "Which approach?"
        assert questions[0]["answer"] == "Option 1 (sounddevice + POST to /transcribe)"

    def test_extract_open_questions_answer_with_a_substring(self) -> None:
        """Answers containing ' A: ' are correctly parsed (non-greedy regex)."""
        from sova.dashboard.services.spec_service import _extract_open_questions

        text = "## Open Questions\n\n- Q: Which approach? A: Option 1 A: use POST\n"
        questions = _extract_open_questions(text)
        assert len(questions) == 1
        assert questions[0]["text"] == "Which approach?"
        assert questions[0]["answer"] == "Option 1 A: use POST"

    def test_write_answers_reanswer_no_corruption(self, tmp_path: Path) -> None:
        """Re-answering an already-answered question does not corrupt the file."""
        from sova.dashboard.services.spec_service import _extract_open_questions, write_answers

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test.md"
        # Start with an already-answered question
        spec.write_text("# Spec\n\n**Status**: draft\n\n## Open Questions\n\n- Q: Use X or Y? A: Old answer\n")

        # Re-answer it
        write_answers("42", {"0": "New answer"}, project_dir=tmp_path)

        text = spec.read_text()
        # Should have exactly one Q/A entry with the new answer
        assert "Q: Use X or Y? A: New answer" in text
        assert "Old answer" not in text
        # Verify no corruption (no duplicate Q: or A: prefixes)
        assert "Q: Q:" not in text
        assert "A: New answer A:" not in text

        # Verify round-trip parsing still works
        questions = _extract_open_questions(text)
        assert len(questions) == 1
        assert questions[0]["text"] == "Use X or Y?"
        assert questions[0]["answer"] == "New answer"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestSpecConfig:
    def test_default_values(self) -> None:
        config = ProjectConfig()
        assert config.spec.threshold == "moderate"
        assert config.spec.auto_approve_simple is True

    def test_custom_values(self) -> None:
        config = ProjectConfig(spec=SpecConfig(threshold="always", auto_approve_simple=False))
        assert config.spec.threshold == "always"
        assert config.spec.auto_approve_simple is False

    def test_threshold_validation(self) -> None:
        # Valid values should work
        for value in ("always", "trivial", "simple", "moderate", "complex", "never"):
            config = SpecConfig(threshold=value)
            assert config.threshold == value

    def test_invalid_threshold_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SpecConfig(threshold="invalid")

    def test_settings_meta_exists(self) -> None:
        from sova.dashboard.settings_meta import get_meta

        assert get_meta("spec.threshold") is not None
        assert get_meta("spec.auto_approve_simple") is not None

    def test_settings_group_exists(self) -> None:
        from sova.dashboard.settings_meta import GROUP_ORDER, GROUPS

        assert "spec" in GROUPS
        assert "spec" in GROUP_ORDER

    def test_loader_includes_spec(self) -> None:
        from sova.config.loader import _NESTED_SECTIONS

        assert "spec" in _NESTED_SECTIONS


# ---------------------------------------------------------------------------
# Router endpoints
# ---------------------------------------------------------------------------


class TestSpecRouter:
    @pytest.fixture()
    def _spec_dir(self, tmp_path: Path) -> Path:
        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        return specs_dir

    async def test_list_all_endpoint(self, tmp_path: Path, _spec_dir: Path) -> None:
        """GET /spec/all returns all specs."""
        from sova.dashboard.routers.spec import list_all
        from sova.dashboard.services import spec_service

        with patch.object(
            spec_service,
            "list_all_specs",
            return_value=[
                {"issue_number": "42", "status": "draft"},
                {"issue_number": "43", "status": "approved"},
            ],
        ):
            result = await list_all()

        assert len(result["specs"]) == 2
        assert result["specs"][0]["status"] == "draft"

    async def test_spec_all_route_not_captured_as_param(self, tmp_path: Path, _spec_dir: Path) -> None:
        """Verify /spec/all is routed to list_all, not get_spec(issue_number='all')."""
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.routers.spec import router as spec_router

        app = FastAPI()
        app.include_router(spec_router, prefix="/api")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            with patch(
                "sova.dashboard.services.spec_service.list_all_specs",
                return_value=[{"issue_number": "42", "status": "draft"}],
            ):
                resp = await client.get("/api/spec/all")

        assert resp.status_code == 200
        data = resp.json()
        assert "specs" in data
        assert isinstance(data["specs"], list)

    async def test_approve_clears_handoff_after_spawn(self, tmp_path: Path, _spec_dir: Path) -> None:
        """Handoff is only cleared AFTER start_agent succeeds."""
        spec = _spec_dir / "42-test.md"
        spec.write_text("# Spec\n\n**Status**: draft\n**Complexity**: simple\n")

        from sova.dashboard.routers.spec import approve_spec
        from sova.dashboard.services import control_service, handoff_service, spec_service

        call_order: list[str] = []

        with (
            patch.object(spec_service, "approve_spec", return_value={"status": "approved"}),
            patch.object(spec_service, "write_answers"),
            patch.object(
                control_service,
                "start_agent",
                new_callable=AsyncMock,
                return_value={"pid": 123},
                side_effect=lambda *a, **kw: call_order.append("start_agent") or {"pid": 123},
            ),
            patch.object(
                handoff_service,
                "clear_handoff",
                side_effect=lambda **kw: call_order.append("clear_handoff"),
            ),
        ):
            result = await approve_spec("42")

        assert "agent" in result
        assert call_order == ["start_agent", "clear_handoff"]

    async def test_approve_preserves_handoff_on_spawn_failure(self, tmp_path: Path, _spec_dir: Path) -> None:
        """Handoff is NOT cleared if start_agent raises."""
        spec = _spec_dir / "42-test.md"
        spec.write_text("# Spec\n\n**Status**: draft\n**Complexity**: simple\n")

        from sova.dashboard.routers.spec import approve_spec
        from sova.dashboard.services import control_service, handoff_service, spec_service

        with (
            patch.object(spec_service, "approve_spec", return_value={"status": "approved"}),
            patch.object(spec_service, "write_answers"),
            patch.object(
                control_service,
                "start_agent",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Budget exceeded"),
            ),
            patch.object(handoff_service, "clear_handoff") as mock_clear,
        ):
            with pytest.raises(RuntimeError, match="Budget exceeded"):
                await approve_spec("42")

        mock_clear.assert_not_called()

    async def test_approve_passes_answers(self, _spec_dir: Path) -> None:
        """Approve endpoint passes answers to write_answers."""
        spec = _spec_dir / "42-test.md"
        spec.write_text("# Spec\n\n**Status**: draft\n**Complexity**: simple\n")

        from sova.dashboard.routers.spec import ApproveRequest, approve_spec
        from sova.dashboard.services import control_service, handoff_service, spec_service

        with (
            patch.object(spec_service, "approve_spec", return_value={"status": "approved"}),
            patch.object(spec_service, "write_answers") as mock_write,
            patch.object(control_service, "start_agent", new_callable=AsyncMock, return_value={"pid": 1}),
            patch.object(handoff_service, "clear_handoff"),
        ):
            req = ApproveRequest(answers={"0": "Use X"})
            await approve_spec("42", req=req)

        mock_write.assert_called_once_with("42", {"0": "Use X"})

    async def test_revise_clears_handoff_after_spawn(self) -> None:
        from sova.dashboard.routers.spec import revise_spec
        from sova.dashboard.services import control_service, handoff_service

        call_order: list[str] = []

        with (
            patch.object(
                control_service,
                "start_agent",
                new_callable=AsyncMock,
                side_effect=lambda *a, **kw: call_order.append("start_agent") or {"pid": 1},
            ),
            patch.object(
                handoff_service,
                "clear_handoff",
                side_effect=lambda **kw: call_order.append("clear_handoff"),
            ),
        ):
            await revise_spec("42")

        assert call_order == ["start_agent", "clear_handoff"]

    async def test_skip_preserves_handoff_on_failure(self) -> None:
        from sova.dashboard.routers.spec import skip_spec
        from sova.dashboard.services import control_service, handoff_service

        with (
            patch.object(
                control_service,
                "start_agent",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Conflict"),
            ),
            patch.object(handoff_service, "clear_handoff") as mock_clear,
        ):
            with pytest.raises(RuntimeError, match="Conflict"):
                await skip_spec("42")

        mock_clear.assert_not_called()

    async def test_reject_spec_not_found(self) -> None:
        from fastapi import HTTPException

        from sova.dashboard.routers.spec import reject_spec
        from sova.dashboard.services import handoff_service, spec_service

        with (
            patch.object(spec_service, "reject_spec", return_value={"error": "Not found"}),
            patch.object(handoff_service, "clear_handoff") as mock_clear,
        ):
            with pytest.raises(HTTPException):
                await reject_spec("99")

        mock_clear.assert_not_called()

    async def test_list_pending_endpoint(self) -> None:
        from sova.dashboard.routers.spec import list_pending
        from sova.dashboard.services import spec_service

        with patch.object(
            spec_service,
            "list_pending_specs",
            return_value=[{"issue_number": "42", "status": "draft"}],
        ):
            result = await list_pending()

        assert len(result["specs"]) == 1
        assert result["specs"][0]["status"] == "draft"

    async def test_get_spec_endpoint(self) -> None:
        from sova.dashboard.routers.spec import get_spec
        from sova.dashboard.services import spec_service

        with patch.object(
            spec_service,
            "read_spec",
            return_value={"issue_number": "42", "status": "draft"},
        ):
            result = await get_spec("42")

        assert result["issue_number"] == "42"

    async def test_get_spec_not_found(self) -> None:
        from fastapi import HTTPException

        from sova.dashboard.routers.spec import get_spec
        from sova.dashboard.services import spec_service

        with patch.object(spec_service, "read_spec", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                await get_spec("99")
        assert exc_info.value.status_code == 404

    async def test_approve_spec_not_found(self) -> None:
        from fastapi import HTTPException

        from sova.dashboard.routers.spec import approve_spec
        from sova.dashboard.services import spec_service

        with patch.object(spec_service, "approve_spec", return_value={"error": "Not found"}):
            with pytest.raises(HTTPException) as exc_info:
                await approve_spec("99")
        assert exc_info.value.status_code == 404

    async def test_revise_spec_spawn_failure(self) -> None:
        from sova.dashboard.routers.spec import revise_spec
        from sova.dashboard.services import control_service, handoff_service

        with (
            patch.object(
                control_service,
                "start_agent",
                new_callable=AsyncMock,
                side_effect=RuntimeError("No slots"),
            ),
            patch.object(handoff_service, "clear_handoff") as mock_clear,
        ):
            with pytest.raises(RuntimeError, match="No slots"):
                await revise_spec("42")

        mock_clear.assert_not_called()

    async def test_approve_transitions_issue_to_researched(self, _spec_dir: Path) -> None:
        """approve_spec calls adapter.transition_state(RESEARCHED) before spawning developer."""
        spec = _spec_dir / "42-test.md"
        spec.write_text("# Spec\n\n**Status**: draft\n**Complexity**: simple\n")

        from sova.dashboard.routers.spec import approve_spec
        from sova.dashboard.services import control_service, handoff_service, spec_service

        mock_adapter = AsyncMock()
        call_order: list[str] = []
        mock_adapter.transition_state.side_effect = lambda *a, **kw: call_order.append("transition")

        async def _track_start(*a: object, **kw: object) -> dict:
            call_order.append("start_agent")
            return {"pid": 1}

        with (
            patch.object(spec_service, "approve_spec", return_value={"status": "approved"}),
            patch.object(spec_service, "write_answers"),
            patch("sova.config.context.get_project_dir", return_value=_spec_dir.parent),
            patch("sova.config.loader.load_config"),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch.object(control_service, "start_agent", new_callable=AsyncMock, side_effect=_track_start),
            patch.object(handoff_service, "clear_handoff"),
        ):
            await approve_spec("42")

        mock_adapter.transition_state.assert_awaited_once()
        call_args = mock_adapter.transition_state.call_args
        assert call_args[0][0] == "42"
        assert call_args[0][1].value == "researched"
        assert call_order == ["transition", "start_agent"]

    async def test_approve_blocks_agent_on_transition_failure(self, _spec_dir: Path) -> None:
        """approve_spec raises HTTP 500 and does not spawn developer when transition fails."""
        from fastapi import HTTPException

        spec = _spec_dir / "42-test.md"
        spec.write_text("# Spec\n\n**Status**: draft\n**Complexity**: simple\n")

        from sova.dashboard.routers.spec import approve_spec
        from sova.dashboard.services import control_service, handoff_service, spec_service

        mock_adapter = AsyncMock()
        mock_adapter.transition_state.side_effect = RuntimeError("API error")

        with (
            patch.object(spec_service, "approve_spec", return_value={"status": "approved"}),
            patch.object(spec_service, "write_answers"),
            patch("sova.config.context.get_project_dir", return_value=_spec_dir.parent),
            patch("sova.config.loader.load_config"),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch.object(control_service, "start_agent", new_callable=AsyncMock) as mock_start,
            patch.object(handoff_service, "clear_handoff"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await approve_spec("42")
            assert exc_info.value.status_code == 500

        mock_start.assert_not_called()

    async def test_skip_transitions_issue_to_researched(self) -> None:
        """skip_spec calls adapter.transition_state(RESEARCHED) before spawning developer."""
        from sova.dashboard.routers.spec import skip_spec
        from sova.dashboard.services import control_service, handoff_service

        mock_adapter = AsyncMock()
        call_order: list[str] = []
        mock_adapter.transition_state.side_effect = lambda *a, **kw: call_order.append("transition")

        async def _track_start(*a: object, **kw: object) -> dict:
            call_order.append("start_agent")
            return {"pid": 1}

        with (
            patch("sova.config.context.get_project_dir", return_value=Path("/tmp")),
            patch("sova.config.loader.load_config"),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch.object(control_service, "start_agent", new_callable=AsyncMock, side_effect=_track_start),
            patch.object(handoff_service, "clear_handoff"),
        ):
            await skip_spec("42")

        mock_adapter.transition_state.assert_awaited_once()
        call_args = mock_adapter.transition_state.call_args
        assert call_args[0][0] == "42"
        assert call_args[0][1].value == "researched"
        assert call_order == ["transition", "start_agent"]

    async def test_skip_blocks_agent_on_transition_failure(self) -> None:
        """skip_spec raises HTTP 500 and does not spawn developer when transition fails."""
        from fastapi import HTTPException

        from sova.dashboard.routers.spec import skip_spec
        from sova.dashboard.services import control_service, handoff_service

        mock_adapter = AsyncMock()
        mock_adapter.transition_state.side_effect = RuntimeError("API error")

        with (
            patch("sova.config.context.get_project_dir", return_value=Path("/tmp")),
            patch("sova.config.loader.load_config"),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch.object(control_service, "start_agent", new_callable=AsyncMock) as mock_start,
            patch.object(handoff_service, "clear_handoff"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await skip_spec("42")
            assert exc_info.value.status_code == 500

        mock_start.assert_not_called()

    async def test_skip_spec_success(self) -> None:
        from sova.dashboard.routers.spec import skip_spec
        from sova.dashboard.services import control_service, handoff_service

        with (
            patch.object(
                control_service,
                "start_agent",
                new_callable=AsyncMock,
                return_value={"pid": 456},
            ),
            patch.object(handoff_service, "clear_handoff") as mock_clear,
        ):
            result = await skip_spec("42")

        assert result["status"] == "skipped"
        assert result["agent"]["pid"] == 456
        mock_clear.assert_called_once_with(issue="42")

    async def test_reject_spec_success_clears_handoff(self) -> None:
        from sova.dashboard.routers.spec import reject_spec
        from sova.dashboard.services import handoff_service, spec_service

        with (
            patch.object(
                spec_service,
                "reject_spec",
                return_value={"status": "rejected", "issue_number": "42"},
            ),
            patch.object(handoff_service, "clear_handoff") as mock_clear,
        ):
            result = await reject_spec("42")

        assert result["status"] == "rejected"
        mock_clear.assert_called_once_with(issue="42")


class TestSpecsDirNoneGuard:
    """_specs_dir returns None when no project dir is available."""

    def test_specs_dir_returns_none_when_project_dir_none(self) -> None:
        from sova.dashboard.services.spec_service import _specs_dir

        with patch("sova.dashboard.services.spec_service.get_project_dir", return_value=None):
            assert _specs_dir(None) is None

    def test_find_spec_file_none_project_dir(self) -> None:
        with patch("sova.dashboard.services.spec_service.get_project_dir", return_value=None):
            assert find_spec_file("42", project_dir=None) is None

    def test_read_spec_none_project_dir(self) -> None:
        from sova.dashboard.services.spec_service import read_spec

        with patch("sova.dashboard.services.spec_service.get_project_dir", return_value=None):
            assert read_spec("42", project_dir=None) is None

    def test_list_all_specs_none_project_dir(self) -> None:
        from sova.dashboard.services.spec_service import list_all_specs

        with patch("sova.dashboard.services.spec_service.get_project_dir", return_value=None):
            assert list_all_specs(project_dir=None) == []

    def test_list_pending_specs_none_project_dir(self) -> None:
        from sova.dashboard.services.spec_service import list_pending_specs

        with patch("sova.dashboard.services.spec_service.get_project_dir", return_value=None):
            assert list_pending_specs(project_dir=None) == []


# ---------------------------------------------------------------------------
# complete_awaiting_approval_by_issue
# ---------------------------------------------------------------------------


class TestCompleteAwaitingApprovalByIssue:
    """Tests for the function that transitions awaiting_approval TaskRuns."""

    async def _create_awaiting_run(self, issue: str, role: str = "researcher") -> int:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session, session.begin():
            run = TaskRun(issue_number=issue, role=role, status="awaiting_approval", current_step="spec")
            session.add(run)
            await session.flush()
            return run.id

    async def test_transitions_to_done(self) -> None:
        from sova.dashboard.services.agent_lifecycle import complete_awaiting_approval_by_issue
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        run_id = await self._create_awaiting_run("42")

        result = await complete_awaiting_approval_by_issue("42", "done")

        assert result == run_id
        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.status == "done"
            assert run.ended_at is not None

    async def test_transitions_to_rejected(self) -> None:
        from sova.dashboard.services.agent_lifecycle import complete_awaiting_approval_by_issue
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        run_id = await self._create_awaiting_run("42")

        result = await complete_awaiting_approval_by_issue("42", "rejected")

        assert result == run_id
        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.status == "rejected"
            assert run.ended_at is not None

    async def test_returns_none_when_no_matching_run(self) -> None:
        from sova.dashboard.services.agent_lifecycle import complete_awaiting_approval_by_issue

        result = await complete_awaiting_approval_by_issue("999", "done")

        assert result is None

    async def test_strips_hash_prefix(self) -> None:
        from sova.dashboard.services.agent_lifecycle import complete_awaiting_approval_by_issue
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        run_id = await self._create_awaiting_run("42")

        result = await complete_awaiting_approval_by_issue("#42", "done")

        assert result == run_id
        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.status == "done"

    async def test_picks_most_recent_run(self) -> None:
        from sova.dashboard.services.agent_lifecycle import complete_awaiting_approval_by_issue
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        old_id = await self._create_awaiting_run("42")
        new_id = await self._create_awaiting_run("42")
        assert new_id > old_id

        result = await complete_awaiting_approval_by_issue("42", "done")

        assert result == new_id
        async with await get_session() as session:
            new_run = await session.get(TaskRun, new_id)
            old_run = await session.get(TaskRun, old_id)
            assert new_run.status == "done"
            assert old_run.status == "awaiting_approval"

    async def test_ignores_non_awaiting_runs(self) -> None:
        from sova.dashboard.services.agent_lifecycle import complete_awaiting_approval_by_issue
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session, session.begin():
            run = TaskRun(issue_number="42", role="researcher", status="done", current_step="spec")
            session.add(run)

        result = await complete_awaiting_approval_by_issue("42", "done")
        assert result is None

    async def test_ignores_non_researcher_awaiting_runs(self) -> None:
        from sova.dashboard.services.agent_lifecycle import complete_awaiting_approval_by_issue
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        researcher_id = await self._create_awaiting_run("42", role="researcher")
        developer_id = await self._create_awaiting_run("42", role="developer")
        assert developer_id > researcher_id

        result = await complete_awaiting_approval_by_issue("42", "done")

        assert result == researcher_id
        async with await get_session() as session:
            researcher = await session.get(TaskRun, researcher_id)
            developer = await session.get(TaskRun, developer_id)
            assert researcher.status == "done"
            assert developer.status == "awaiting_approval"

    async def test_picks_highest_id_on_equal_started_at(self) -> None:
        from datetime import datetime, timezone

        from sova.dashboard.services.agent_lifecycle import complete_awaiting_approval_by_issue
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        fixed_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        async with await get_session() as session, session.begin():
            r1 = TaskRun(
                issue_number="42",
                role="researcher",
                status="awaiting_approval",
                current_step="spec",
                started_at=fixed_ts,
            )
            r2 = TaskRun(
                issue_number="42",
                role="researcher",
                status="awaiting_approval",
                current_step="spec",
                started_at=fixed_ts,
            )
            session.add_all([r1, r2])
            await session.flush()
            old_id, new_id = r1.id, r2.id
        assert new_id > old_id

        result = await complete_awaiting_approval_by_issue("42", "done")

        assert result == new_id
        async with await get_session() as session:
            assert (await session.get(TaskRun, new_id)).status == "done"
            assert (await session.get(TaskRun, old_id)).status == "awaiting_approval"

    async def test_nonfatal_on_db_error(self) -> None:
        from sova.dashboard.services.agent_lifecycle import complete_awaiting_approval_by_issue

        with patch("sova.db.session.get_session", side_effect=RuntimeError("DB down")):
            result = await complete_awaiting_approval_by_issue("42", "done")

        assert result is None


# ---------------------------------------------------------------------------
# Spec router -- TaskRun transition on approve/reject/skip
# ---------------------------------------------------------------------------


class TestSpecRouterTaskRunTransition:
    """Tests that spec router endpoints transition the awaiting_approval TaskRun."""

    async def _create_awaiting_run(self, issue: str) -> int:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session, session.begin():
            run = TaskRun(issue_number=issue, role="researcher", status="awaiting_approval", current_step="spec")
            session.add(run)
            await session.flush()
            return run.id

    async def test_approve_transitions_taskrun_to_done(self) -> None:
        from sova.dashboard.routers.spec import approve_spec
        from sova.dashboard.services import control_service, handoff_service, spec_service
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        run_id = await self._create_awaiting_run("42")

        with (
            patch.object(spec_service, "approve_spec", return_value={"status": "approved"}),
            patch.object(spec_service, "write_answers"),
            patch.object(control_service, "start_agent", new_callable=AsyncMock, return_value={"pid": 123}),
            patch.object(handoff_service, "clear_handoff"),
        ):
            await approve_spec("42")

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.status == "done"

    async def test_reject_transitions_taskrun_to_rejected(self) -> None:
        from sova.dashboard.routers.spec import reject_spec
        from sova.dashboard.services import handoff_service, spec_service
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        run_id = await self._create_awaiting_run("42")

        with (
            patch.object(spec_service, "reject_spec", return_value={"status": "rejected", "issue_number": "42"}),
            patch.object(handoff_service, "clear_handoff"),
        ):
            await reject_spec("42")

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.status == "rejected"

    async def test_skip_transitions_taskrun_to_done(self) -> None:
        from sova.dashboard.routers.spec import skip_spec
        from sova.dashboard.services import control_service, handoff_service
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        run_id = await self._create_awaiting_run("42")

        with (
            patch.object(control_service, "start_agent", new_callable=AsyncMock, return_value={"pid": 456}),
            patch.object(handoff_service, "clear_handoff"),
        ):
            await skip_spec("42")

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.status == "done"

    async def test_revise_transitions_taskrun_to_rejected(self) -> None:
        from sova.dashboard.routers.spec import revise_spec
        from sova.dashboard.services import control_service, handoff_service
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        run_id = await self._create_awaiting_run("42")

        with (
            patch.object(control_service, "start_agent", new_callable=AsyncMock, return_value={"pid": 789}),
            patch.object(handoff_service, "clear_handoff"),
        ):
            await revise_spec("42")

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.status == "rejected"

    async def test_approve_succeeds_without_awaiting_run(self) -> None:
        """Approve works even if no awaiting_approval TaskRun exists (non-fatal)."""
        from sova.dashboard.routers.spec import approve_spec
        from sova.dashboard.services import control_service, handoff_service, spec_service

        with (
            patch.object(spec_service, "approve_spec", return_value={"status": "approved"}),
            patch.object(spec_service, "write_answers"),
            patch.object(control_service, "start_agent", new_callable=AsyncMock, return_value={"pid": 123}),
            patch.object(handoff_service, "clear_handoff"),
        ):
            result = await approve_spec("42")

        assert result["status"] == "approved"
