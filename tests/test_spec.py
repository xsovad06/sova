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


class TestTextHasOpenQuestions:
    def test_no_section_returns_false(self) -> None:
        assert not _text_has_open_questions("# Spec\n\n## Solution\n\nDo stuff\n")

    def test_empty_section_returns_false(self) -> None:
        assert not _text_has_open_questions("# Spec\n\n## Open Questions\n\n(Omit this section)\n")

    def test_with_questions_returns_true(self) -> None:
        assert _text_has_open_questions("# Spec\n\n## Open Questions\n\n- Should we use X or Y?\n- What about Z?\n")

    def test_empty_text_returns_false(self) -> None:
        assert not _text_has_open_questions("")


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

    async def test_execute_success_auto_approve(self, tmp_path: Path) -> None:
        from sova.llm.models import LLMResult

        # Create a simple spec file
        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test.md"
        spec.write_text("# Spec: Test\n\n**Status**: draft\n**Complexity**: simple\n\n## Solution\n\nDo stuff\n")

        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
        )
        step = SpecStep()

        llm_result = LLMResult(text="done", model="opus", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=50)
        with patch("sova.core.steps.spec.invoke_command", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        assert "auto-approved" in result.summary
        assert spec.read_text().count("**Status**: approved") == 1

    async def test_execute_handoff_on_open_questions(self, tmp_path: Path) -> None:
        from sova.llm.models import LLMResult

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test.md"
        spec.write_text(
            "# Spec: Test\n\n**Status**: draft\n**Complexity**: simple\n\n"
            "## Open Questions\n\n- Should we use X or Y?\n"
        )

        # Need agent-control dir for handoff writing
        (tmp_path / ".claude" / "agent-control").mkdir(parents=True, exist_ok=True)

        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
        )
        step = SpecStep()

        llm_result = LLMResult(text="done", model="opus", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=50)
        with patch("sova.core.steps.spec.invoke_command", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        assert "awaiting approval" in result.summary

    async def test_execute_handoff_on_complexity(self, tmp_path: Path) -> None:
        from sova.llm.models import LLMResult

        specs_dir = tmp_path / ".claude" / "specs"
        specs_dir.mkdir(parents=True)
        spec = specs_dir / "42-test.md"
        spec.write_text("# Spec: Test\n\n**Status**: draft\n**Complexity**: complex\n\n## Solution\n\nDo stuff\n")

        (tmp_path / ".claude" / "agent-control").mkdir(parents=True, exist_ok=True)

        ctx = _make_ctx(
            project_dir=tmp_path,
            spec_config=SpecConfig(auto_approve_simple=True),
        )
        step = SpecStep()

        llm_result = LLMResult(text="done", model="opus", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=50)
        with patch("sova.core.steps.spec.invoke_command", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        assert "awaiting approval" in result.summary

    async def test_execute_runtime_error(self) -> None:
        ctx = _make_ctx()
        step = SpecStep()

        with patch(
            "sova.core.steps.spec.invoke_command",
            new_callable=AsyncMock,
            side_effect=RuntimeError("CLI failed"),
        ):
            result = await step.execute(ctx)

        assert not result.success
        assert "CLI failed" in result.error

    async def test_execute_no_spec_file_produced(self, tmp_path: Path) -> None:
        from sova.llm.models import LLMResult

        ctx = _make_ctx(project_dir=tmp_path)
        step = SpecStep()

        llm_result = LLMResult(text="done", model="opus", cost_usd=Decimal("0.05"), input_tokens=100, output_tokens=50)
        with patch("sova.core.steps.spec.invoke_command", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert not result.success
        assert "no spec file" in result.summary.lower()

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
