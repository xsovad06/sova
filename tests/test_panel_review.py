"""Tests for sova.roles.panel_review: combined and per-dimension review panel."""

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
from sova.llm.models import LLMResult
from sova.roles.panel_review import (
    _DIMENSION_PROMPTS,
    _build_combined_prompt,
    _build_dimension_prompt,
    _group_dimensions_by_model,
    _parse_multi_dimension_findings,
    deduplicate_findings,
    run_panel_review,
)
from sova.roles.reviewer import ReviewFinding, ReviewResult


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


def _combined_response(findings: list[dict] | None = None, summaries: dict[str, str] | None = None) -> str:
    return json.dumps({"findings": findings or [], "summaries": summaries or {}})


def _chunked_diff() -> str:
    """A diff large enough to split into two chunks at the file boundary."""
    return "diff --git a/a.py b/a.py\n" + "x" * 110_000 + "\ndiff --git a/b.py b/b.py\n" + "y" * 110_000


async def _run(panel_config: ReviewPanelConfig, **kwargs) -> ReviewResult:
    """Run the panel over a one-file diff; kwargs override the defaults."""
    kwargs.setdefault("diff", "small diff")
    kwargs.setdefault("files", ["a.py"])
    return await run_panel_review(task=_task(), panel_config=panel_config, **kwargs)


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
    async def test_combined_call_covers_all_dimensions(self) -> None:
        """Default combined mode reviews every same-model dimension in one call."""
        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "security"],
        )
        findings = [
            {"file": "a.py", "line": 5, "severity": 7, "category": "correctness", "description": "Bug"},
            {"file": "b.py", "line": 10, "severity": 6, "category": "security", "description": "Vuln"},
        ]
        response = _combined_response(findings, {"correctness": "One bug", "security": "One vuln"})

        calls: list[tuple[str | None, str | None]] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            calls.append((model, task_type))
            return LLMResult(text=response, model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(panel_config, files=["a.py", "b.py"])

        assert calls == [("sonnet", "review_panel")]
        assert len(result.findings) == 2
        assert result.total_cost == Decimal("0.01")
        assert "correctness: One bug" in result.summary
        assert "security: One vuln" in result.summary

    async def test_per_dimension_mode_calls_each_dimension(self) -> None:
        """combined=False keeps one call per dimension."""
        panel_config = ReviewPanelConfig(
            enabled=True,
            combined=False,
            dimensions=["correctness", "security"],
        )
        findings_a = [{"file": "a.py", "line": 5, "severity": 7, "category": "correctness", "description": "Bug"}]
        findings_b = [{"file": "b.py", "line": 10, "severity": 6, "category": "security", "description": "Vuln"}]

        task_types: list[str] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            task_types.append(task_type or "")
            if "correctness" in (task_type or ""):
                return LLMResult(text=_llm_response(findings_a), model="sonnet", cost_usd=Decimal("0.01"))
            return LLMResult(text=_llm_response(findings_b), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(panel_config, files=["a.py", "b.py"])

        assert task_types == ["review_correctness", "review_security"]
        assert len(result.findings) == 2
        assert result.total_cost == Decimal("0.02")

    async def test_model_groups_split_into_one_call_each(self) -> None:
        """Dimensions are grouped by resolved model, one combined call per group."""
        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "security", "design"],
            dimension_models={"security": "opus"},
        )

        calls: list[tuple[str | None, str]] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            calls.append((model, prompt))
            return LLMResult(text=_combined_response(), model=model or "sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            await _run(panel_config)

        assert [model for model, _ in calls] == ["sonnet", "opus"]
        sonnet_prompt, opus_prompt = calls[0][1], calls[1][1]
        assert "### correctness" in sonnet_prompt
        assert "### design" in sonnet_prompt
        assert "### security" not in sonnet_prompt
        # The opus group holds one dimension, so it keeps the focused prompt.
        assert "ONLY for security vulnerabilities" in opus_prompt

    async def test_single_dimension_group_uses_focused_prompt(self) -> None:
        """One call either way, so a lone dimension keeps its focused framing."""
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["security"])

        calls: list[tuple[str | None, str]] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            calls.append((task_type, prompt))
            return LLMResult(text=_llm_response([], "Clean"), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(panel_config)

        assert [task_type for task_type, _ in calls] == ["review_security"]
        assert "ONLY for security vulnerabilities" in calls[0][1]
        assert "other reviewers handle those" in calls[0][1]
        assert result.summary == "security: Clean"

    async def test_unparseable_combined_response_falls_back_to_dimensions(self) -> None:
        """A combined response with no recoverable JSON re-runs per dimension."""
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness", "security"])
        findings = [{"file": "a.py", "line": 5, "severity": 5, "category": "correctness", "description": "Issue"}]

        task_types: list[str] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            task_types.append(task_type or "")
            if task_type == "review_panel":
                return LLMResult(text="I could not review this diff.", model="sonnet", cost_usd=Decimal("0.01"))
            return LLMResult(text=_llm_response(findings), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(panel_config)

        assert task_types == ["review_panel", "review_correctness", "review_security"]
        assert len(result.findings) == 1
        # The wasted combined call is still charged
        assert result.total_cost == Decimal("0.03")

    async def test_clean_combined_response_does_not_fall_back(self) -> None:
        """Valid JSON with zero findings is a clean review, not a parse failure."""
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness", "security"])
        llm_result = LLMResult(text=_combined_response(), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock, return_value=llm_result) as mock_invoke:
            result = await _run(panel_config)

        mock_invoke.assert_awaited_once()
        assert result.findings == []
        assert result.summary

    async def test_group_failure_continues_with_next_group(self) -> None:
        """A raising group is logged and skipped without aborting the review."""
        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "security", "design", "test_coverage"],
            dimension_models={"correctness": "opus", "security": "opus"},
        )
        findings = [{"file": "a.py", "line": 5, "severity": 6, "category": "design", "description": "Coupling"}]

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            if model == "opus":
                raise RuntimeError("LLM unavailable")
            return LLMResult(text=_combined_response(findings), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(panel_config)

        assert len(result.findings) == 1
        assert result.findings[0].category == "design"

    async def test_critical_finding_in_single_dimension_group_stops_later_groups(self) -> None:
        """A lone dimension takes the per-dimension path but still short-circuits the panel."""
        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "security", "design"],
            dimension_models={"correctness": "opus"},
        )
        critical = [{"file": "a.py", "line": 1, "severity": 10, "category": "correctness", "description": "Critical"}]

        task_types: list[str] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            task_types.append(task_type or "")
            return LLMResult(text=_llm_response(critical), model="opus", cost_usd=Decimal("0.05"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(panel_config)

        assert task_types == ["review_correctness"]
        assert result.findings[0].severity == 10

    async def test_unknown_category_finding_is_kept(self) -> None:
        """A category outside the requested dimensions is reported, not dropped."""
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness", "security"])
        findings = [{"file": "a.py", "line": 5, "severity": 6, "category": "performance", "description": "Slow"}]
        llm_result = LLMResult(text=_combined_response(findings), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await _run(panel_config)

        assert [f.category for f in result.findings] == ["performance"]

    async def test_summaries_use_intersection_in_priority_order(self) -> None:
        """Unrequested summary keys are dropped and missing ones are simply absent."""
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness", "security", "design"])
        # "security" is omitted; "performance" was never requested.
        response = _combined_response(
            summaries={"design": "Design fine", "correctness": "Logic fine", "performance": "Fast"},
        )
        llm_result = LLMResult(text=response, model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await _run(panel_config)

        assert result.summary == "correctness: Logic fine | design: Design fine"

    async def test_flat_summary_attributed_to_group(self) -> None:
        """A flat summary string is kept under a joined dimension label."""
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness", "security"])
        llm_result = LLMResult(text=_llm_response([], "Looks clean"), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await _run(panel_config)

        assert result.summary == "correctness+security: Looks clean"

    async def test_budget_skips_whole_group(self) -> None:
        """A group the budget cannot cover issues no call at all."""
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness", "security"])
        llm_result = LLMResult(text=_combined_response(), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock, return_value=llm_result) as mock_invoke:
            result = await _run(panel_config, budget_remaining=Decimal("0.003"))

        mock_invoke.assert_not_awaited()
        assert result.findings == []

    async def test_budget_floor_scales_with_group_size(self) -> None:
        """A 3-dimension group needs 3x the single-dimension floor to be affordable."""
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness", "security", "design"])
        llm_result = LLMResult(text=_combined_response(), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock, return_value=llm_result) as mock_invoke:
            # Enough for the unscaled single-dimension floor (0.01) but not for
            # three dimensions bundled into one call (0.03).
            result = await _run(panel_config, budget_remaining=Decimal("0.02"))

        mock_invoke.assert_not_awaited()
        assert result.findings == []

    async def test_no_dimensions_makes_no_calls(self) -> None:
        """An empty dimension list produces no groups and no LLM calls."""
        panel_config = ReviewPanelConfig(enabled=True, dimensions=[])

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock) as mock_invoke:
            result = await _run(panel_config)

        mock_invoke.assert_not_awaited()
        assert result.findings == []
        assert result.summary == "All dimensions report no issues: code looks good."

    async def test_empty_dimension_responses(self) -> None:
        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "security"],
        )
        llm_result = LLMResult(text=_llm_response([], "Clean"), model="sonnet", cost_usd=Decimal("0.005"))

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await _run(panel_config)

        assert len(result.findings) == 0
        assert result.summary  # should have a summary even with no findings

    async def test_partial_dimension_failure(self) -> None:
        panel_config = ReviewPanelConfig(
            enabled=True,
            combined=False,
            dimensions=["correctness", "security"],
        )
        findings_a = [{"file": "a.py", "line": 5, "severity": 7, "category": "correctness", "description": "Bug"}]

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            if "security" in (task_type or ""):
                raise RuntimeError("LLM unavailable")
            return LLMResult(text=_llm_response(findings_a), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(panel_config)

        # Should still have findings from successful dimension
        assert len(result.findings) == 1
        assert result.findings[0].category == "correctness"

    async def test_budget_guard_skips_later_model_group(self) -> None:
        """The first group spends the budget, so the pricier second group never runs."""
        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "security", "design"],
            dimension_models={"security": "opus"},
        )
        findings = [{"file": "a.py", "line": 5, "severity": 5, "category": "correctness", "description": "Issue"}]
        llm_result = LLMResult(text=_combined_response(findings), model="sonnet", cost_usd=Decimal("0.01"))

        called_models: list[str | None] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            called_models.append(model)
            return llm_result

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(
                panel_config,
                # Covers the sonnet group (0.01) but not the opus group (0.05).
                budget_remaining=Decimal("0.02"),
            )

        assert called_models == ["sonnet"]
        assert len(result.findings) == 1

    async def test_deduplication_across_dimensions(self) -> None:
        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "error_handling"],
            line_proximity=3,
        )

        # Both dimensions report a same-category issue at nearby lines
        findings = [
            {"file": "a.py", "line": 10, "severity": 5, "category": "correctness", "description": "Bug A"},
            {"file": "a.py", "line": 11, "severity": 8, "category": "correctness", "description": "Bug B"},
        ]
        llm_result = LLMResult(text=_combined_response(findings), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await _run(panel_config)

        # Same file, nearby lines, same category; dedup keeps higher severity
        assert len(result.findings) == 1
        assert result.findings[0].severity == 8

    async def test_critical_finding_exits_early(self) -> None:
        """Severity >= 9 triggers early exit, skipping the remaining model groups."""
        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "security", "design"],
            dimension_models={"design": "haiku"},
        )
        critical = [{"file": "a.py", "line": 1, "severity": 10, "category": "correctness", "description": "Critical"}]

        called_models: list[str | None] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            called_models.append(model)
            return LLMResult(text=_combined_response(critical), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(panel_config)

        assert len(result.findings) >= 1
        assert result.findings[0].severity == 10
        # The haiku group never runs: the critical finding short-circuits it
        assert called_models == ["sonnet"]

    async def test_critical_finding_exits_early_per_dimension(self) -> None:
        """combined=False: a severity 10 finding stops the remaining dimensions."""
        panel_config = ReviewPanelConfig(enabled=True, combined=False, dimensions=["correctness", "security"])
        critical = [{"file": "a.py", "line": 1, "severity": 10, "category": "correctness", "description": "Critical"}]

        task_types: list[str] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            task_types.append(task_type or "")
            return LLMResult(text=_llm_response(critical), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(panel_config)

        assert task_types == ["review_correctness"]
        assert result.findings[0].severity == 10

    async def test_critical_finding_stops_later_chunks(self) -> None:
        """A critical finding in chunk 1 skips every remaining chunk."""
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness", "security"])
        critical = [{"file": "a.py", "line": 1, "severity": 9, "category": "correctness", "description": "Critical"}]
        llm_result = LLMResult(text=_combined_response(critical), model="sonnet", cost_usd=Decimal("0.01"))

        call_count = 0

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return llm_result

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(panel_config, diff=_chunked_diff())

        # Chunk 1 alone: the second chunk is never reviewed.
        assert call_count == 1
        assert result.total_cost == Decimal("0.01")

    async def test_critical_finding_in_fallback_stops_later_groups(self) -> None:
        """A critical finding found during the per-dimension fallback ends the review."""
        panel_config = ReviewPanelConfig(
            enabled=True,
            dimensions=["correctness", "security", "design"],
            dimension_models={"design": "haiku"},
        )
        critical = [{"file": "a.py", "line": 1, "severity": 10, "category": "correctness", "description": "Critical"}]

        task_types: list[str] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            task_types.append(task_type or "")
            if task_type == "review_panel":
                return LLMResult(text="I could not review this diff.", model="sonnet", cost_usd=Decimal("0.01"))
            return LLMResult(text=_llm_response(critical), model="sonnet", cost_usd=Decimal("0.01"))

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(panel_config)

        # Fallback runs correctness, hits the critical finding, and the haiku group never runs.
        assert task_types == ["review_panel", "review_correctness"]
        assert result.findings[0].severity == 10

    async def test_budget_skipped_dimension_stays_skipped_across_chunks(self) -> None:
        """A dimension skipped for budget in chunk 1 stays skipped in chunk 2."""
        panel_config = ReviewPanelConfig(enabled=True, combined=False, dimensions=["correctness", "security"])
        findings = [{"file": "a.py", "line": 5, "severity": 5, "category": "correctness", "description": "Issue"}]
        llm_result = LLMResult(text=_llm_response(findings), model="sonnet", cost_usd=Decimal("0.01"))

        invoked_types: list[str] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            invoked_types.append(task_type or "")
            return llm_result

        # Budget covers one call; correctness spends it in chunk 1.
        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            await _run(panel_config, diff=_chunked_diff(), budget_remaining=Decimal("0.015"))

        # Security is skipped in chunk 1 and never retried in chunk 2.
        assert invoked_types == ["review_correctness"]

    async def test_skipped_dimension_not_retried_in_later_chunks(self) -> None:
        """Cover the 'dim in skipped_dimensions' continue path across chunks."""
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness", "test_coverage"])
        findings = [{"file": "a.py", "line": 5, "severity": 5, "category": "correctness", "description": "Issue"}]
        llm_result = LLMResult(text=_llm_response(findings), model="sonnet", cost_usd=Decimal("0.01"))

        call_count = 0

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return llm_result

        # The 2-dimension group's estimated floor is 0.01 * 2 = 0.02; budget covers
        # chunk 1's call but not a second one after the actual 0.01 cost is charged.
        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            result = await _run(panel_config, diff=_chunked_diff(), budget_remaining=Decimal("0.025"))

        # Once skipped for budget the group stays skipped, so chunk 2 makes no call.
        assert call_count == 1
        assert result.total_cost == Decimal("0.01")

    async def test_addressed_findings_threaded_to_first_chunk_only(self) -> None:
        """Addressed findings appear in prompts for chunk 0 but not subsequent chunks."""
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness"])
        llm_result = LLMResult(text=_llm_response([], "Clean"), model="sonnet", cost_usd=Decimal("0.005"))
        addressed = [{"source": "SonarCloud", "severity": "MAJOR", "file_path": "a.py", "message": "Fix this"}]

        captured_prompts: list[str] = []

        async def mock_invoke(prompt, *, model=None, task_type=None, cwd=None, **kwargs):
            captured_prompts.append(prompt)
            return llm_result

        with patch("sova.roles.panel_review.invoke", side_effect=mock_invoke):
            await _run(panel_config, diff=_chunked_diff(), addressed_findings=addressed)

        assert len(captured_prompts) >= 2
        assert "Already Addressed" in captured_prompts[0]
        assert "SonarCloud" in captured_prompts[0]
        # Subsequent chunks should NOT include addressed findings
        for prompt in captured_prompts[1:]:
            assert "Already Addressed" not in prompt

    async def test_dimension_model_override(self) -> None:
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
            await _run(panel_config, diff="diff")

        assert captured_model == "opus"


# ---------------------------------------------------------------------------
# Combined prompt building
# ---------------------------------------------------------------------------


class TestBuildCombinedPrompt:
    def test_includes_a_section_per_dimension(self) -> None:
        prompt = _build_combined_prompt(["correctness", "security"], _task(), "diff", ["a.py"])
        assert "### correctness" in prompt
        assert "### security" in prompt
        assert "Injection attacks" in prompt
        assert "off-by-one" in prompt

    def test_omits_single_dimension_framing(self) -> None:
        """The per-dimension 'ignore everything else' framing must not leak in."""
        prompt = _build_combined_prompt(["correctness", "security"], _task(), "diff", ["a.py"])
        assert "other reviewers handle those" not in prompt

    def test_includes_diff_and_files(self) -> None:
        prompt = _build_combined_prompt(["correctness"], _task(), "diff content", ["a.py", "b.py"])
        assert "diff content" in prompt
        assert "- a.py" in prompt
        assert "- b.py" in prompt

    def test_requests_summaries_keyed_by_dimension(self) -> None:
        prompt = _build_combined_prompt(["correctness", "security"], _task(), "diff", ["a.py"])
        assert '"summaries"' in prompt
        assert '"correctness":' in prompt
        assert '"security":' in prompt

    def test_lists_allowed_categories(self) -> None:
        prompt = _build_combined_prompt(["correctness", "security"], _task(), "diff", ["a.py"])
        assert "correctness|security" in prompt

    def test_json_example_uses_one_concrete_category(self) -> None:
        """The example must not invite copying the pipe-joined list into a finding."""
        prompt = _build_combined_prompt(["correctness", "security"], _task(), "diff", ["a.py"])
        assert '"category": "correctness"' in prompt
        assert '"category": "correctness|security"' not in prompt

    def test_includes_spec_and_omits_body(self) -> None:
        spec = {"Solution": "Do X"}
        prompt = _build_combined_prompt(["correctness"], _task(), "diff", ["a.py"], spec_sections=spec)
        assert "Do X" in prompt
        assert "Some description" not in prompt

    def test_includes_body_without_spec(self) -> None:
        prompt = _build_combined_prompt(["correctness"], _task(), "diff", ["a.py"])
        assert "Some description" in prompt

    def test_includes_addressed_findings(self) -> None:
        findings = [{"source": "SonarCloud", "severity": "MAJOR", "file_path": "a.py", "message": "Complexity"}]
        prompt = _build_combined_prompt(["correctness"], _task(), "diff", ["a.py"], addressed_findings=findings)
        assert "Already Addressed" in prompt
        assert "SonarCloud" in prompt

    def test_unknown_dimension_uses_generic_focus(self) -> None:
        prompt = _build_combined_prompt(["foo_dimension"], _task(), "diff", ["a.py"])
        assert "### foo_dimension" in prompt
        assert "Any foo_dimension issues" in prompt


# ---------------------------------------------------------------------------
# Combined response parsing
# ---------------------------------------------------------------------------


class TestParseMultiDimensionFindings:
    def test_parses_findings_and_summaries(self) -> None:
        text = _combined_response(
            [{"file": "a.py", "line": 3, "severity": 6, "category": "security", "description": "Vuln"}],
            {"correctness": "Clean", "security": "One vuln"},
        )
        response = _parse_multi_dimension_findings(text)
        assert response.parsed
        assert len(response.findings) == 1
        assert response.findings[0].category == "security"
        assert response.summaries == {"correctness": "Clean", "security": "One vuln"}
        assert response.group_summary == ""

    def test_flat_summary_returned_separately(self) -> None:
        response = _parse_multi_dimension_findings(_llm_response([], "All good"))
        assert response.parsed
        assert response.summaries == {}
        assert response.group_summary == "All good"

    def test_fenced_json_is_parsed(self) -> None:
        text = "```json\n" + _combined_response([], {"correctness": "Clean"}) + "\n```"
        response = _parse_multi_dimension_findings(text)
        assert response.parsed
        assert response.summaries == {"correctness": "Clean"}

    def test_json_embedded_in_prose_is_recovered(self) -> None:
        text = "Here is my review:\n" + _combined_response([], {"correctness": "Clean"}) + "\nHope that helps."
        response = _parse_multi_dimension_findings(text)
        assert response.parsed
        assert response.summaries == {"correctness": "Clean"}

    def test_unparseable_response_reports_not_parsed(self) -> None:
        response = _parse_multi_dimension_findings("I was unable to complete the review.")
        assert not response.parsed
        assert response.findings == []
        assert response.summaries == {}

    def test_empty_findings_still_counts_as_parsed(self) -> None:
        response = _parse_multi_dimension_findings(_combined_response())
        assert response.parsed
        assert response.findings == []

    def test_non_dict_summaries_ignored(self) -> None:
        response = _parse_multi_dimension_findings(json.dumps({"findings": [], "summaries": "clean"}))
        assert response.parsed
        assert response.summaries == {}

    def test_non_review_json_object_reports_not_parsed(self) -> None:
        """An unrelated JSON object is a failed review, not a clean one."""
        response = _parse_multi_dimension_findings(json.dumps({"error": "context too long"}))
        assert not response.parsed
        assert response.findings == []

    def test_summaries_only_response_counts_as_parsed(self) -> None:
        """A clean review may omit the findings key entirely."""
        response = _parse_multi_dimension_findings(json.dumps({"summaries": {"correctness": "Clean"}}))
        assert response.parsed
        assert response.summaries == {"correctness": "Clean"}

    def test_bare_json_array_does_not_crash(self) -> None:
        text = json.dumps([{"file": "a.py", "line": 1, "severity": 5, "category": "correctness"}])
        response = _parse_multi_dimension_findings(text)
        assert response.findings == []
        assert response.summaries == {}

    def test_malformed_finding_entries_skipped(self) -> None:
        text = json.dumps(
            {
                "findings": [
                    "not a finding",
                    {"file": "a.py", "line": 1, "severity": 5, "category": "correctness", "description": "Bug"},
                ]
            }
        )
        response = _parse_multi_dimension_findings(text)
        assert len(response.findings) == 1
        assert response.findings[0].file == "a.py"


# ---------------------------------------------------------------------------
# Model grouping
# ---------------------------------------------------------------------------


class TestGroupDimensionsByModel:
    def test_default_config_yields_one_group(self) -> None:
        config = ReviewPanelConfig()
        groups = _group_dimensions_by_model(list(config.dimensions), config)
        assert groups == [("sonnet", list(config.dimensions))]

    def test_overrides_split_groups_in_order(self) -> None:
        config = ReviewPanelConfig(
            dimensions=["correctness", "security", "design"],
            dimension_models={"security": "opus"},
        )
        groups = _group_dimensions_by_model(["correctness", "security", "design"], config)
        assert groups == [("sonnet", ["correctness", "design"]), ("opus", ["security"])]

    def test_no_dimensions_yields_no_groups(self) -> None:
        assert _group_dimensions_by_model([], ReviewPanelConfig()) == []


# ---------------------------------------------------------------------------
# ReviewerRole integration with panel mode
# ---------------------------------------------------------------------------


class TestReviewerPanelIntegration:
    async def test_panel_enabled_delegates_to_panel_review(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        config = ProjectConfig()
        config.review.panel.enabled = True
        config.review.panel.dimensions = ["correctness"]

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
        assert config.combined
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
