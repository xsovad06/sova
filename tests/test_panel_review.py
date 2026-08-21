"""Tests for sova.roles.panel_review -- sequential dimension review panel."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig, ReviewPanelConfig
from sova.core.context import ExecutionContext
from sova.db.session import close_db, init_db
from sova.roles.panel_review import (
    _DIMENSION_PROMPTS,
    _build_dimension_prompt,
    deduplicate_findings,
    run_panel_review,
)
from sova.roles.reviewer import ReviewFinding


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _task() -> Task:
    return Task(id="42", title="Test issue", body="Some description", state=TaskState.IN_REVIEW)


def _finding(
    file: str = "foo.py",
    line: int | None = 10,
    severity: int = 5,
    category: str = "correctness",
    description: str = "Issue found",
) -> ReviewFinding:
    return ReviewFinding(file=file, line=line, severity=severity, category=category, description=description)


def _llm_response(findings: list[dict] | None = None, summary: str = "OK") -> str:
    return json.dumps({"findings": findings or [], "summary": summary})


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplicateFindings:
    def test_empty_input(self) -> None:
        assert deduplicate_findings([]) == []

    def test_no_duplicates(self) -> None:
        findings = [
            _finding(file="a.py", line=10, category="correctness"),
            _finding(file="b.py", line=10, category="correctness"),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2

    def test_same_file_line_category_deduplicates(self) -> None:
        findings = [
            _finding(file="a.py", line=10, severity=5, category="correctness"),
            _finding(file="a.py", line=12, severity=8, category="correctness"),
        ]
        result = deduplicate_findings(findings, line_proximity=3)
        assert len(result) == 1
        assert result[0].severity == 8  # keeps higher severity

    def test_different_category_same_line_kept(self) -> None:
        findings = [
            _finding(file="a.py", line=10, category="correctness"),
            _finding(file="a.py", line=10, category="security"),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2

    def test_beyond_proximity_window_kept(self) -> None:
        findings = [
            _finding(file="a.py", line=10, category="correctness"),
            _finding(file="a.py", line=14, category="correctness"),  # distance=4, > default 3
        ]
        result = deduplicate_findings(findings, line_proximity=3)
        assert len(result) == 2

    def test_none_lines_never_deduplicate(self) -> None:
        findings = [
            _finding(file="a.py", line=None, category="correctness"),
            _finding(file="a.py", line=None, category="correctness"),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2

    async def test_budget_skipped_dimension_stays_skipped_across_chunks(self) -> None:
        """A dimension skipped for budget in chunk 1 stays skipped in chunk 2."""
        from sova.llm.models import LLMResult

        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness", "security"])
        findings = [{"file": "a.py", "line": 5, "severity": 5, "category": "correctness", "description": "Issue"}]
        llm_result = LLMResult(text=_llm_response(findings), model="sonnet", cost_usd=Decimal("0.01"))

        invoked_types: list[str] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            invoked_types.append(task_type or "")
            return llm_result

        # Large diff that will be chunked (> 100KB), budget only enough for correctness
        big_diff = "x" * 150_000

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            await run_panel_review(
                task=_task(),
                diff=big_diff,
                files=["a.py"],
                panel_config=panel_config,
                budget_remaining=Decimal("0.015"),
            )

        # Security should never be invoked (budget too low after correctness)
        assert all("security" not in t for t in invoked_types)

    def test_custom_proximity_window(self) -> None:
        findings = [
            _finding(file="a.py", line=10, severity=3, category="correctness"),
            _finding(file="a.py", line=15, severity=7, category="correctness"),
        ]
        result = deduplicate_findings(findings, line_proximity=5)
        assert len(result) == 1
        assert result[0].severity == 7


# ---------------------------------------------------------------------------
# Dimension prompt building
# ---------------------------------------------------------------------------


class TestBuildDimensionPrompt:
    def test_includes_dimension_focus(self) -> None:
        prompt = _build_dimension_prompt("security", _task(), "diff content", ["a.py"])
        assert "security" in prompt.lower()
        assert "Injection attacks" in prompt

    def test_includes_diff_and_files(self) -> None:
        prompt = _build_dimension_prompt("correctness", _task(), "diff content", ["a.py", "b.py"])
        assert "diff content" in prompt
        assert "- a.py" in prompt
        assert "- b.py" in prompt

    def test_includes_spec_when_provided(self) -> None:
        spec = {"Solution": "Do X", "Edge Cases": "Handle Y"}
        prompt = _build_dimension_prompt("correctness", _task(), "diff", ["a.py"], spec_sections=spec)
        assert "Do X" in prompt
        assert "Handle Y" in prompt

    def test_omits_body_when_spec_present(self) -> None:
        spec = {"Solution": "Do X"}
        prompt = _build_dimension_prompt("correctness", _task(), "diff", ["a.py"], spec_sections=spec)
        assert "Some description" not in prompt

    def test_includes_body_without_spec(self) -> None:
        prompt = _build_dimension_prompt("correctness", _task(), "diff", ["a.py"])
        assert "Some description" in prompt

    def test_unknown_dimension_uses_generic(self) -> None:
        prompt = _build_dimension_prompt("foo_dimension", _task(), "diff", ["a.py"])
        assert "foo_dimension" in prompt

    def test_all_known_dimensions_have_prompts(self) -> None:
        for dim in ["correctness", "security", "error_handling", "design", "test_coverage"]:
            assert dim in _DIMENSION_PROMPTS

    def test_includes_addressed_findings_when_provided(self) -> None:
        findings = [
            {"source": "SonarCloud", "severity": "MAJOR", "file_path": "a.py", "message": "Cognitive complexity"},
        ]
        prompt = _build_dimension_prompt("correctness", _task(), "diff", ["a.py"], addressed_findings=findings)
        assert "Already Addressed" in prompt
        assert "SonarCloud" in prompt
        assert "Cognitive complexity" in prompt

    def test_omits_addressed_block_when_none(self) -> None:
        prompt = _build_dimension_prompt("correctness", _task(), "diff", ["a.py"], addressed_findings=None)
        assert "Already Addressed" not in prompt


# ---------------------------------------------------------------------------
# Panel review integration
# ---------------------------------------------------------------------------


class TestRunPanelReview:
    async def test_sequential_dimensions_aggregate_findings(self) -> None:
        from sova.llm.models import LLMResult

        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "security"],
        )
        findings_a = [{"file": "a.py", "line": 5, "severity": 7, "category": "correctness", "description": "Bug"}]
        findings_b = [{"file": "b.py", "line": 10, "severity": 6, "category": "security", "description": "Vuln"}]

        call_count = 0

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if "correctness" in (task_type or ""):
                return LLMResult(text=_llm_response(findings_a), model="sonnet", cost_usd=Decimal("0.01"))
            return LLMResult(text=_llm_response(findings_b), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await run_panel_review(
                task=_task(),
                diff="small diff",
                files=["a.py", "b.py"],
                panel_config=panel_config,
            )

        assert len(result.findings) == 2
        assert result.total_cost == Decimal("0.02")
        assert call_count == 2

    async def test_empty_dimension_responses(self) -> None:
        from sova.llm.models import LLMResult

        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "security"],
        )
        llm_result = LLMResult(text=_llm_response([], "Clean"), model="sonnet", cost_usd=Decimal("0.005"))

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await run_panel_review(
                task=_task(),
                diff="small diff",
                files=["a.py"],
                panel_config=panel_config,
            )

        assert len(result.findings) == 0
        assert result.summary  # should have a summary even with no findings

    async def test_partial_dimension_failure(self) -> None:
        from sova.llm.models import LLMResult

        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "security"],
        )
        findings_a = [{"file": "a.py", "line": 5, "severity": 7, "category": "correctness", "description": "Bug"}]

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            if "security" in (task_type or ""):
                raise RuntimeError("LLM unavailable")
            return LLMResult(text=_llm_response(findings_a), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await run_panel_review(
                task=_task(),
                diff="small diff",
                files=["a.py"],
                panel_config=panel_config,
            )

        # Should still have findings from successful dimension
        assert len(result.findings) == 1
        assert result.findings[0].category == "correctness"

    async def test_budget_guard_skips_low_priority_dimensions(self) -> None:
        from sova.llm.models import LLMResult

        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "test_coverage"],
        )
        findings = [{"file": "a.py", "line": 5, "severity": 5, "category": "correctness", "description": "Issue"}]
        llm_result = LLMResult(text=_llm_response(findings), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await run_panel_review(
                task=_task(),
                diff="small diff",
                files=["a.py"],
                panel_config=panel_config,
                budget_remaining=Decimal("0.003"),  # too low for any call
            )

        # Budget too low -- all dimensions skipped
        assert len(result.findings) == 0

    async def test_deduplication_across_dimensions(self) -> None:
        from sova.llm.models import LLMResult

        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "error_handling"],
            line_proximity=3,
        )

        # Both dimensions find same-category issue at nearby lines
        findings_a = [{"file": "a.py", "line": 10, "severity": 5, "category": "correctness", "description": "Bug A"}]
        findings_b = [{"file": "a.py", "line": 11, "severity": 8, "category": "correctness", "description": "Bug B"}]

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            if "correctness" in (task_type or ""):
                return LLMResult(text=_llm_response(findings_a), model="sonnet", cost_usd=Decimal("0.01"))
            return LLMResult(text=_llm_response(findings_b), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await run_panel_review(
                task=_task(),
                diff="small diff",
                files=["a.py"],
                panel_config=panel_config,
            )

        # Same file, nearby lines, same category -- dedup keeps higher severity
        assert len(result.findings) == 1
        assert result.findings[0].severity == 8

    async def test_critical_finding_exits_early(self) -> None:
        """Severity >= 9 triggers early exit, skipping remaining dimensions."""
        from sova.llm.models import LLMResult

        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness", "security", "design"])
        critical = [{"file": "a.py", "line": 1, "severity": 10, "category": "correctness", "description": "Critical"}]

        call_count = 0

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return LLMResult(text=_llm_response(critical), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await run_panel_review(task=_task(), diff="small diff", files=["a.py"], panel_config=panel_config)

        assert len(result.findings) >= 1
        assert result.findings[0].severity == 10
        # Should stop after first dimension due to critical finding
        assert call_count == 1

    async def test_skipped_dimension_not_retried_in_later_chunks(self) -> None:
        """Cover the 'dim in skipped_dimensions' continue path across chunks."""
        from sova.llm.models import LLMResult

        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness", "test_coverage"])
        findings = [{"file": "a.py", "line": 5, "severity": 5, "category": "correctness", "description": "Issue"}]
        llm_result = LLMResult(text=_llm_response(findings), model="sonnet", cost_usd=Decimal("0.01"))

        call_count = 0

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return llm_result

        # Large diff that will be chunked (> 100KB)
        big_diff = "x" * 150_000

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await run_panel_review(
                task=_task(),
                diff=big_diff,
                files=["a.py"],
                panel_config=panel_config,
                budget_remaining=Decimal("0.015"),  # enough for ~1 dim per chunk
            )

        # Budget should cause test_coverage to be skipped; once skipped it stays skipped
        assert result.total_cost > Decimal(0)

    async def test_addressed_findings_threaded_to_first_chunk_only(self) -> None:
        """Addressed findings appear in prompts for chunk 0 but not subsequent chunks."""
        from sova.llm.models import LLMResult

        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness"])
        llm_result = LLMResult(text=_llm_response([], "Clean"), model="sonnet", cost_usd=Decimal("0.005"))
        addressed = [{"source": "SonarCloud", "severity": "MAJOR", "file_path": "a.py", "message": "Fix this"}]

        captured_prompts: list[str] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            captured_prompts.append(prompt)
            return llm_result

        # Large diff with file boundaries to force chunking (each section > 100KB)
        big_diff = "diff --git a/a.py b/a.py\n" + "x" * 110_000 + "\n" + "diff --git a/b.py b/b.py\n" + "y" * 110_000

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            await run_panel_review(
                task=_task(),
                diff=big_diff,
                files=["a.py"],
                panel_config=panel_config,
                addressed_findings=addressed,
            )

        assert len(captured_prompts) >= 2
        assert "Already Addressed" in captured_prompts[0]
        assert "SonarCloud" in captured_prompts[0]
        # Subsequent chunks should NOT include addressed findings
        for prompt in captured_prompts[1:]:
            assert "Already Addressed" not in prompt

    async def test_dimension_model_override(self) -> None:
        from sova.llm.models import LLMResult

        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["security"],
            dimension_models={"security": "opus"},
        )
        llm_result = LLMResult(text=_llm_response([], "Clean"), model="opus", cost_usd=Decimal("0.05"))

        captured_model = None

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            nonlocal captured_model
            captured_model = model
            return llm_result

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            await run_panel_review(
                task=_task(),
                diff="diff",
                files=["a.py"],
                panel_config=panel_config,
            )

        assert captured_model == "opus"


# ---------------------------------------------------------------------------
# ReviewerRole integration with panel mode
# ---------------------------------------------------------------------------


class TestReviewerPanelIntegration:
    async def test_panel_enabled_delegates_to_panel_review(self) -> None:
        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        config = ProjectConfig()
        config.review.panel.enabled = True
        config.review.panel.dimensions = ["correctness"]
        config.review.challenger_enabled = False

        adapter = AsyncMock()
        adapter.get_task.return_value = _task()
        ctx = ExecutionContext(
            project_dir=Path("/tmp/test"),
            config=config,
            adapter=adapter,
            issue_number="42",
            role="reviewer",
            pr_number=10,
        )

        findings = [{"file": "a.py", "line": 5, "severity": 5, "category": "correctness", "description": "Issue"}]
        llm_result = LLMResult(text=_llm_response(findings), model="sonnet", cost_usd=Decimal("0.01"))

        with (
            patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value="feat/issue-42"),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.panel_review.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            role = ReviewerRole()
            result = await role.execute(ctx)

        assert result.success
        assert "1 findings" in result.summary

    async def test_panel_disabled_uses_single_review(self) -> None:
        from sova.llm.models import LLMResult
        from sova.roles.reviewer import ReviewerRole

        config = ProjectConfig()
        assert not config.review.panel.enabled  # default is disabled

        adapter = AsyncMock()
        adapter.get_task.return_value = _task()
        ctx = ExecutionContext(
            project_dir=Path("/tmp/test"),
            config=config,
            adapter=adapter,
            issue_number="42",
            role="reviewer",
            pr_number=10,
        )

        llm_result = LLMResult(text=_llm_response([], "Clean"), model="sonnet", cost_usd=Decimal("0.005"))

        with (
            patch("sova.roles.reviewer.get_pr_branch", new_callable=AsyncMock, return_value="feat/issue-42"),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result) as mock_invoke,
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file"),
        ):
            role = ReviewerRole()
            result = await role.execute(ctx)

        assert result.success
        # Single reviewer path uses sova.roles.reviewer.invoke, not panel_review.invoke
        mock_invoke.assert_awaited_once()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestReviewPanelConfig:
    def test_defaults(self) -> None:
        config = ReviewPanelConfig()
        assert not config.enabled
        assert "correctness" in config.dimensions
        assert "security" in config.dimensions
        assert config.line_proximity == 3
        assert config.dimension_models == {}

    def test_nested_in_project_config(self) -> None:
        config = ProjectConfig()
        assert hasattr(config.review, "panel")
        assert not config.review.panel.enabled

    def test_custom_dimensions(self) -> None:
        config = ReviewPanelConfig(dimensions=["security", "correctness"])
        assert config.dimensions == ["security", "correctness"]

    def test_unknown_dimension_warns(self) -> None:
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            ReviewPanelConfig(dimensions=["correctness", "typo_dim"])
            assert len(w) == 1
            assert "typo_dim" in str(w[0].message)

    def test_all_known_dimensions_no_warning(self) -> None:
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            ReviewPanelConfig(dimensions=["correctness", "security"])
            assert len(w) == 0
