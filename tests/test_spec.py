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
        assert "auto-approved" in result.summary
        assert spec.read_text().count("**Status**: approved") == 1

        # Verify handoff file was written with auto_execute=True develop action
        import json

        handoff_files = list((tmp_path / ".claude" / "agent-control").glob("handoff*.json"))
        assert handoff_files, "Expected handoff file to be written for auto-approve"
        handoff = json.loads(handoff_files[0].read_text())
        actions = handoff.get("next_actions", [])
        develop_action = next((a for a in actions if a["id"] == "develop"), None)
        assert develop_action is not None, "Expected 'develop' action in handoff"
        assert develop_action["auto_execute"] is True, "develop action must have auto_execute=True"

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
