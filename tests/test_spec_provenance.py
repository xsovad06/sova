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


async def test_extract_memory_step_spec_read_exception(tmp_path: Path) -> None:
    """Covers _read_spec_for_extraction except branch (returns None on error)."""
    from sova.core.steps.extract_memory import ExtractMemoryStep
    from sova.knowledge.extraction import ExtractionResult

    step = ExtractMemoryStep()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_result = ExtractionResult(memories_stored=0, cost_usd=Decimal("0.005"))

    with (
        patch(
            "sova.core.steps._spec_helpers.read_spec_sections",
            side_effect=OSError("disk error"),
        ),
        patch(
            "sova.knowledge.extraction.extract_memories",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_extract,
    ):
        result = await step.execute(ctx)

    assert result.success is True
    assert mock_extract.call_args.kwargs["spec_content"] is None


async def test_extract_memory_step_execute_exception(tmp_path: Path) -> None:
    """Covers the outer except in ExtractMemoryStep.execute."""
    from sova.core.steps.extract_memory import ExtractMemoryStep

    step = ExtractMemoryStep()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    with patch(
        "sova.knowledge.extraction.extract_memories",
        new_callable=AsyncMock,
        side_effect=RuntimeError("extraction boom"),
    ):
        result = await step.execute(ctx)

    assert result.success is True
    assert "non-fatal" in result.summary


async def test_extract_memory_step_error_in_result(tmp_path: Path) -> None:
    """Covers the result.error summary branch in ExtractMemoryStep.execute."""
    from sova.core.steps.extract_memory import ExtractMemoryStep
    from sova.knowledge.extraction import ExtractionResult

    step = ExtractMemoryStep()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_result = ExtractionResult(memories_stored=0, cost_usd=Decimal("0.005"), error="parse failed")

    with patch(
        "sova.knowledge.extraction.extract_memories",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        result = await step.execute(ctx)

    assert result.success is True
    assert "parse failed" in result.summary


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
# _spec_helpers: _replace_section edge cases
# ---------------------------------------------------------------------------


def test_replace_section_heading_not_found(tmp_path: Path) -> None:
    """Covers _replace_section returning unchanged text when heading not found."""
    from sova.core.steps._spec_helpers import _replace_section

    text = "# Spec\n\n## Solution\nPlan.\n"
    result = _replace_section(text, "Nonexistent Heading", "New content")
    assert result == text


def test_replace_section_last_section(tmp_path: Path) -> None:
    """Covers _replace_section when the target is the last section (no next heading)."""
    from sova.core.steps._spec_helpers import _replace_section

    text = "# Spec\n\n## Solution\nPlan.\n\n## Design Decisions\nOld decisions.\n"
    result = _replace_section(text, "Design Decisions", "New decisions.")
    assert "New decisions." in result
    assert "Old decisions." not in result
    assert "## Solution" in result


# ---------------------------------------------------------------------------
# AddressReviewStep: _load_spec_for_context
# ---------------------------------------------------------------------------


def test_load_spec_for_context_returns_sections(tmp_path: Path) -> None:
    from sova.core.steps.address_review import _load_spec_for_context

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    spec_file.write_text("# Spec\n\n## Design Decisions\nChose REST.\n\n## Implementation Notes\nUsed factory.\n")

    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)
    result = _load_spec_for_context(ctx)

    assert "Chose REST" in result
    assert "Used factory" in result


def test_load_spec_for_context_no_spec(tmp_path: Path) -> None:
    from sova.core.steps.address_review import _load_spec_for_context

    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)
    result = _load_spec_for_context(ctx)
    assert result == ""


def test_load_spec_for_context_exception_returns_empty() -> None:
    from sova.core.steps.address_review import _load_spec_for_context

    ctx = _make_ctx()
    with patch(
        "sova.core.steps._spec_helpers.read_spec_sections",
        side_effect=OSError("disk error"),
    ):
        result = _load_spec_for_context(ctx)
    assert result == ""


# ---------------------------------------------------------------------------
# DevelopStep: _append_implementation_notes git failure paths
# ---------------------------------------------------------------------------


async def test_develop_notes_git_diff_fails(tmp_path: Path) -> None:
    """Covers the diff_result.success=False path in _append_implementation_notes."""
    from sova.core.steps.develop import _append_implementation_notes
    from sova.llm.models import LLMResult

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    spec_file.write_text("# Spec\n\n## Solution\nPlan.\n\n## Design Decisions\nDecisions.\n")

    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_invoke = AsyncMock(
        return_value=LLMResult(
            text="- Used fallback data",
            model="haiku",
            cost_usd=Decimal("0.005"),
            input_tokens=100,
            output_tokens=50,
        )
    )
    # diff fails, log succeeds
    diff_fail = MagicMock(success=False, stdout="")
    log_ok = MagicMock(success=True, stdout="abc123 feat: something")
    mock_run = AsyncMock(side_effect=[diff_fail, log_ok])

    with patch("sova.core.steps.develop.invoke", mock_invoke), patch("sova.core.steps.develop.run", mock_run):
        await _append_implementation_notes(ctx)

    # Verify prompt contained fallback text
    prompt_arg = mock_invoke.call_args[0][0]
    assert "(unavailable)" in prompt_arg

    content = spec_file.read_text()
    assert "## Implementation Notes" in content


async def test_develop_notes_git_log_fails(tmp_path: Path) -> None:
    """Covers the log_result.success=False path in _append_implementation_notes."""
    from sova.core.steps.develop import _append_implementation_notes
    from sova.llm.models import LLMResult

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    spec_file.write_text("# Spec\n\n## Solution\nPlan.\n\n## Design Decisions\nDecisions.\n")

    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_invoke = AsyncMock(
        return_value=LLMResult(
            text="- Used fallback data",
            model="haiku",
            cost_usd=Decimal("0.005"),
            input_tokens=100,
            output_tokens=50,
        )
    )
    # diff succeeds, log fails
    diff_ok = MagicMock(success=True, stdout="file.py | 10 ++--")
    log_fail = MagicMock(success=False, stdout="")
    mock_run = AsyncMock(side_effect=[diff_ok, log_fail])

    with patch("sova.core.steps.develop.invoke", mock_invoke), patch("sova.core.steps.develop.run", mock_run):
        await _append_implementation_notes(ctx)

    prompt_arg = mock_invoke.call_args[0][0]
    assert "(no commits)" in prompt_arg


# ---------------------------------------------------------------------------
# DevelopStep.execute and validate_output
# ---------------------------------------------------------------------------


async def test_develop_step_execute_success(tmp_path: Path) -> None:
    """Covers DevelopStep.execute happy path including _append_implementation_notes call."""
    from sova.core.steps.develop import DevelopStep
    from sova.llm.models import LLMResult

    step = DevelopStep()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_result = LLMResult(
        text="done",
        model="sonnet",
        cost_usd=Decimal("0.10"),
        input_tokens=500,
        output_tokens=200,
        session_id="sess-1",
    )
    mock_invoke_cmd = AsyncMock(return_value=mock_result)
    mock_append = AsyncMock()

    with (
        patch("sova.core.steps.develop.invoke_command", mock_invoke_cmd),
        patch("sova.core.steps.develop._append_implementation_notes", mock_append),
    ):
        result = await step.execute(ctx)

    assert result.success is True
    assert "Development completed" in result.summary
    mock_append.assert_awaited_once_with(ctx)


async def test_develop_step_execute_runtime_error(tmp_path: Path) -> None:
    """Covers DevelopStep.execute RuntimeError path."""
    from sova.core.steps.develop import DevelopStep

    step = DevelopStep()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_invoke_cmd = AsyncMock(side_effect=RuntimeError("Claude CLI failed"))

    with patch("sova.core.steps.develop.invoke_command", mock_invoke_cmd):
        result = await step.execute(ctx)

    assert result.success is False
    assert "Development failed" in result.summary


async def test_develop_step_validate_output_has_changes(tmp_path: Path) -> None:
    """Covers validate_output when there are uncommitted changes."""
    from sova.core.steps.develop import DevelopStep

    step = DevelopStep()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_run = AsyncMock(
        side_effect=[
            MagicMock(success=True, stdout="file.py | 10 ++--"),  # diff
            MagicMock(success=True, stdout=""),  # staged
            MagicMock(success=True, stdout=""),  # log
        ]
    )
    with patch("sova.core.steps.develop.run", mock_run):
        result = await step.validate_output(ctx)

    assert result.passed is True


async def test_develop_step_validate_output_has_commits(tmp_path: Path) -> None:
    """Covers validate_output when there are commits ahead of base."""
    from sova.core.steps.develop import DevelopStep

    step = DevelopStep()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_run = AsyncMock(
        side_effect=[
            MagicMock(success=True, stdout=""),  # diff
            MagicMock(success=True, stdout=""),  # staged
            MagicMock(success=True, stdout="abc123 feat: something"),  # log
        ]
    )
    with patch("sova.core.steps.develop.run", mock_run):
        result = await step.validate_output(ctx)

    assert result.passed is True


async def test_develop_step_validate_output_no_changes(tmp_path: Path) -> None:
    """Covers validate_output when there are no changes at all."""
    from sova.core.steps.develop import DevelopStep

    step = DevelopStep()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    mock_run = AsyncMock(
        side_effect=[
            MagicMock(success=True, stdout=""),  # diff
            MagicMock(success=True, stdout=""),  # staged
            MagicMock(success=True, stdout=""),  # log
        ]
    )
    with patch("sova.core.steps.develop.run", mock_run):
        result = await step.validate_output(ctx)

    assert result.passed is False
    assert "no code changes" in result.reason.lower()


# ---------------------------------------------------------------------------
# ReviewerRole: _append_review_rationale edge cases
# ---------------------------------------------------------------------------


def test_reviewer_rationale_with_suggestion(tmp_path: Path) -> None:
    """Covers the f.suggestion branch in _append_review_rationale."""
    from sova.roles.reviewer import ReviewerRole, ReviewFinding, ReviewResult

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    spec_file.write_text("# Spec\n\n## Solution\nPlan.\n")

    reviewer = ReviewerRole()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    review = ReviewResult(
        findings=[
            ReviewFinding(
                file="api.py",
                severity=8,
                category="security",
                description="SQL injection risk",
                line=42,
                suggestion="Use parameterized queries",
            ),
        ],
        summary="Security issue",
    )

    reviewer._append_review_rationale(ctx, review)

    content = spec_file.read_text()
    assert "SQL injection risk" in content
    assert "Use parameterized queries" in content


def test_reviewer_rationale_no_line_number(tmp_path: Path) -> None:
    """Covers the loc branch when finding has no line number."""
    from sova.roles.reviewer import ReviewerRole, ReviewFinding, ReviewResult

    specs_dir = tmp_path / ".claude" / "specs"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "42-feature.md"
    spec_file.write_text("# Spec\n\n## Solution\nPlan.\n")

    reviewer = ReviewerRole()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    review = ReviewResult(
        findings=[
            ReviewFinding(
                file="config.py",
                severity=6,
                category="design",
                description="Missing validation",
                line=None,
            ),
        ],
        summary="Design issue",
    )

    reviewer._append_review_rationale(ctx, review)

    content = spec_file.read_text()
    assert "`config.py`" in content
    assert "Missing validation" in content


def test_reviewer_rationale_exception_nonfatal(tmp_path: Path) -> None:
    """Covers the except branch in _append_review_rationale."""
    from sova.roles.reviewer import ReviewerRole, ReviewFinding, ReviewResult

    reviewer = ReviewerRole()
    ctx = _make_ctx(project_dir=tmp_path, working_dir=tmp_path)

    review = ReviewResult(
        findings=[
            ReviewFinding(file="x.py", severity=9, category="bug", description="Critical", line=1),
        ],
        summary="Critical issue",
    )

    with patch(
        "sova.core.steps._spec_helpers.append_spec_section",
        side_effect=OSError("disk full"),
    ):
        # Should not raise
        reviewer._append_review_rationale(ctx, review)


# ---------------------------------------------------------------------------
# extraction.py: _build_extraction_prompt with review_findings
# ---------------------------------------------------------------------------


def test_extraction_prompt_includes_review_findings() -> None:
    from sova.knowledge.extraction import _build_extraction_prompt

    prompt = _build_extraction_prompt(
        role="reviewer",
        task_title="Review PR #5",
        files_changed=["api.py"],
        step_summaries=["review: completed"],
        review_findings=[
            {"file": "api.py", "line": 10, "severity": 7, "category": "bug", "description": "Missing null check"},
        ],
    )

    assert "Review Findings" in prompt
    assert "Missing null check" in prompt
    assert "api.py:10" in prompt


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
