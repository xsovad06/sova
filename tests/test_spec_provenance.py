"""Tests for spec provenance threading (#246)."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh in-memory DB for each test."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


# ---------------------------------------------------------------------------
# _spec_helpers: append_spec_section
# ---------------------------------------------------------------------------


def test_append_section_creates_new(tmp_path: Path) -> None:
    from sova.core.steps._spec_helpers import append_spec_section

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    spec_file.write_text("# Spec: Feature\n**Status**: approved\n\n## Solution\nDo the thing.\n")

    result = append_spec_section("42", "Implementation Notes", "- Chose approach B", tmp_path)

    assert result is True
    content = spec_file.read_text()
    assert "## Implementation Notes" in content
    assert "- Chose approach B" in content


def test_append_section_replaces_existing(tmp_path: Path) -> None:
    from sova.core.steps._spec_helpers import append_spec_section

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    spec_file.write_text(
        "# Spec: Feature\n\n## Solution\nDo the thing.\n\n"
        "## Implementation Notes\nOld content.\n\n## Review Rationale\nStuff.\n"
    )

    result = append_spec_section("42", "Implementation Notes", "New content.", tmp_path)

    assert result is True
    content = spec_file.read_text()
    assert "New content." in content
    assert "Old content." not in content
    # Verify other sections preserved
    assert "## Review Rationale" in content
    assert "Stuff." in content


def test_append_section_returns_false_no_spec(tmp_path: Path) -> None:
    from sova.core.steps._spec_helpers import append_spec_section

    result = append_spec_section("99", "Implementation Notes", "content", tmp_path)
    assert result is False


# ---------------------------------------------------------------------------
# _spec_helpers: read_spec_sections
# ---------------------------------------------------------------------------


def test_read_spec_sections(tmp_path: Path) -> None:
    from sova.core.steps._spec_helpers import read_spec_sections

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    spec_file.write_text(
        "# Spec: Feature\n\n## Design Decisions\nDecision A.\n\n"
        "## Implementation Notes\nNote B.\n\n## Unrelated\nIgnored.\n"
    )

    result = read_spec_sections("42", tmp_path, ("Design Decisions", "Implementation Notes", "Missing"))

    assert "Decision A." in result
    assert "Note B." in result
    assert "Ignored." not in result
    assert "Missing" not in result


def test_read_spec_sections_no_spec(tmp_path: Path) -> None:
    from sova.core.steps._spec_helpers import read_spec_sections

    result = read_spec_sections("99", tmp_path, ("Solution",))
    assert result == ""


# ---------------------------------------------------------------------------
# CommitStep: _is_agent_artifact
# ---------------------------------------------------------------------------


def test_spec_files_not_agent_artifact() -> None:
    from sova.core.steps.commit import _is_agent_artifact

    assert _is_agent_artifact(".claude/specs/42-feature.md") is False
    assert _is_agent_artifact(".claude/specs/100-another.md") is False


def test_other_claude_files_still_artifacts() -> None:
    from sova.core.steps.commit import _is_agent_artifact

    assert _is_agent_artifact(".claude/settings.json") is True
    assert _is_agent_artifact(".claude/agent-control/handoff.json") is True
    assert _is_agent_artifact(".agent-summary.md") is True


# ---------------------------------------------------------------------------
# DevelopStep: _append_implementation_notes
# ---------------------------------------------------------------------------


async def test_develop_appends_notes_on_success(tmp_path: Path) -> None:
    from sova.core.steps.develop import _append_implementation_notes
    from sova.llm.models import LLMResult

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    spec_file.write_text("# Spec: Feature\n\n## Solution\nPlan.\n\n## Design Decisions\nDecisions.\n")

    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_invoke = AsyncMock(
        return_value=LLMResult(
            text="- Chose approach B over A\n- Added validation layer",
            model="haiku",
            cost_usd=Decimal("0.005"),
            input_tokens=100,
            output_tokens=50,
        )
    )
    mock_run = AsyncMock(return_value=MagicMock(success=True, stdout="file.py | 10 ++--"))

    with patch("sova.core.steps.develop.invoke", mock_invoke), patch("sova.core.steps.develop.run", mock_run):
        await _append_implementation_notes(ctx)

    content = spec_file.read_text()
    assert "## Implementation Notes" in content
    assert "Chose approach B" in content


async def test_develop_no_spec_silently_skips(tmp_path: Path) -> None:
    from sova.core.steps.develop import _append_implementation_notes

    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    # Should not raise, even with no spec
    await _append_implementation_notes(ctx)


async def test_develop_llm_failure_nonfatal(tmp_path: Path) -> None:
    from sova.core.steps.develop import _append_implementation_notes

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    spec_file.write_text("# Spec: Feature\n\n## Solution\nPlan.\n")

    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_invoke = AsyncMock(side_effect=RuntimeError("LLM failed"))
    mock_run = AsyncMock(return_value=MagicMock(success=True, stdout=""))

    with patch("sova.core.steps.develop.invoke", mock_invoke), patch("sova.core.steps.develop.run", mock_run):
        # Should not raise
        await _append_implementation_notes(ctx)

    # Spec unchanged
    assert "Implementation Notes" not in spec_file.read_text()


# ---------------------------------------------------------------------------
# ReviewerRole: _append_review_rationale
# ---------------------------------------------------------------------------


def test_reviewer_appends_rationale(tmp_path: Path) -> None:
    from sova.roles.reviewer import ReviewerRole, ReviewFinding, ReviewResult

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    spec_file.write_text("# Spec: Feature\n\n## Solution\nPlan.\n")

    reviewer = ReviewerRole()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    review = ReviewResult(
        findings=[
            ReviewFinding(file="api.py", severity=7, category="bug", description="Missing null check", line=10),
            ReviewFinding(file="util.py", severity=3, category="docs", description="Typo in comment", line=5),
        ],
        summary="Found issues",
    )

    reviewer._append_review_rationale(ctx, review)

    content = spec_file.read_text()
    assert "## Review Rationale" in content
    assert "Missing null check" in content
    # Severity 3 should not appear (threshold is 5)
    assert "Typo in comment" not in content


def test_reviewer_no_significant_findings_skips(tmp_path: Path) -> None:
    from sova.roles.reviewer import ReviewerRole, ReviewFinding, ReviewResult

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    original = "# Spec: Feature\n\n## Solution\nPlan.\n"
    spec_file.write_text(original)

    reviewer = ReviewerRole()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    review = ReviewResult(
        findings=[
            ReviewFinding(file="util.py", severity=3, category="docs", description="Minor", line=5),
        ],
        summary="Minor issues",
    )

    reviewer._append_review_rationale(ctx, review)

    # Spec unchanged since no findings >= 5
    assert spec_file.read_text() == original


# ---------------------------------------------------------------------------
# AddressReviewStep: _format_findings_prompt with spec context
# ---------------------------------------------------------------------------


def test_format_findings_with_spec_context() -> None:
    from sova.core.steps.address_review import _format_findings_prompt

    findings = [{"file": "api.py", "line": 10, "severity": 7, "category": "bug", "description": "Issue"}]
    prompt = _format_findings_prompt(findings, spec_context="## Design Decisions\nChose REST over GraphQL.")

    assert "Decision Context" in prompt
    assert "Chose REST over GraphQL" in prompt
    assert "api.py:10" in prompt


def test_format_findings_without_spec_context() -> None:
    from sova.core.steps.address_review import _format_findings_prompt

    findings = [{"file": "api.py", "line": 10, "severity": 7, "category": "bug", "description": "Issue"}]
    prompt = _format_findings_prompt(findings)

    assert "Decision Context" not in prompt
    assert "api.py:10" in prompt


# ---------------------------------------------------------------------------
# extraction.py: spec_content parameter
# ---------------------------------------------------------------------------


def test_extraction_prompt_includes_spec_content() -> None:
    from sova.knowledge.extraction import _build_extraction_prompt

    prompt = _build_extraction_prompt(
        role="developer",
        task_title="Feature X",
        files_changed=["main.py"],
        step_summaries=["develop: completed"],
        spec_content="## Design Decisions\nChose approach B.\n\n## Implementation Notes\nUsed factory pattern.",
    )

    assert "Spec Decision Chain" in prompt
    assert "Chose approach B" in prompt
    assert "factory pattern" in prompt


def test_extraction_prompt_without_spec_content() -> None:
    from sova.knowledge.extraction import _build_extraction_prompt

    prompt = _build_extraction_prompt(
        role="developer",
        task_title="Feature X",
        files_changed=[],
        step_summaries=[],
    )

    assert "Spec Decision Chain" not in prompt


# ---------------------------------------------------------------------------
# ExtractMemoryStep: reads spec and passes it
# ---------------------------------------------------------------------------


async def test_extract_memory_step_passes_spec(tmp_path: Path) -> None:
    from sova.core.steps.extract_memory import ExtractMemoryStep
    from sova.knowledge.extraction import ExtractionResult

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    spec_file.write_text("# Spec\n\n## Design Decisions\nDecision A.\n\n## Implementation Notes\nNote B.\n")

    step = ExtractMemoryStep()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_result = ExtractionResult(memories_stored=1, cost_usd=Decimal("0.01"))

    extract_path = "sova.knowledge.extraction.extract_memories"
    with patch(extract_path, new_callable=AsyncMock, return_value=mock_result) as mock_extract:
        result = await step.execute(ctx)

    assert result.success is True
    # Verify spec_content was passed
    call_kwargs = mock_extract.call_args.kwargs
    assert call_kwargs["spec_content"] is not None
    assert "Decision A" in call_kwargs["spec_content"]


async def test_extract_memory_step_no_spec(tmp_path: Path) -> None:
    from sova.core.steps.extract_memory import ExtractMemoryStep
    from sova.knowledge.extraction import ExtractionResult

    step = ExtractMemoryStep()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_result = ExtractionResult(memories_stored=0, cost_usd=Decimal("0.005"))

    extract_path = "sova.knowledge.extraction.extract_memories"
    with patch(extract_path, new_callable=AsyncMock, return_value=mock_result) as mock_extract:
        result = await step.execute(ctx)

    assert result.success is True
    call_kwargs = mock_extract.call_args.kwargs
    assert call_kwargs["spec_content"] is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    *,
    project_dir: Path | None = None,
    working_dir: Path | None = None,
    completed_steps: frozenset[str] | None = None,
) -> MagicMock:
    """Create a minimal mock ExecutionContext."""
    from sova.adapters.base import Task

    ctx = MagicMock()
    ctx.role = "developer"
    ctx.issue_number = "42"
    ctx.repo = "user/repo"
    ctx.base_branch = "main"
    ctx.task = Task(id="42", title="Test task", body="", state="in_progress", labels=[], url="")
    ctx.files_changed = ["src/main.py"]
    ctx.working_dir = working_dir or Path("/tmp")
    ctx.project_dir = project_dir or Path("/tmp")
    ctx.completed_steps = completed_steps or frozenset()
    ctx.pr_number = None
    ctx.cost_usd = Decimal("0")
    ctx.config = MagicMock()
    ctx.config.agent.max_budget = Decimal("5")
    ctx.add_cost = MagicMock()
    return ctx
