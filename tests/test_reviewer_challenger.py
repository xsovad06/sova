"""Tests for sova.roles._reviewer_challenger -- challenger pass verification."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.db.session import close_db, init_db
from sova.roles._review_comments import ReviewFinding, ReviewResult
from sova.roles._reviewer_challenger import (
    _apply_challenger_response,
    _build_challenger_prompt,
    _filter_diff_for_findings,
)


@pytest.fixture
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _finding(
    file: str = "foo.py",
    line: int | None = 10,
    severity: int = 5,
    category: str = "correctness",
    description: str = "Issue found",
    suggestion: str = "Fix it",
) -> ReviewFinding:
    return ReviewFinding(
        file=file,
        line=line,
        severity=severity,
        category=category,
        description=description,
        suggestion=suggestion,
    )


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


class TestBuildChallengerPrompt:
    def test_includes_all_findings(self) -> None:
        findings = [_finding(description="Bug A"), _finding(description="Bug B")]
        prompt = _build_challenger_prompt(findings, "diff content")
        assert "Bug A" in prompt
        assert "Bug B" in prompt

    def test_includes_diff(self) -> None:
        prompt = _build_challenger_prompt([_finding()], "--- a/foo.py\n+++ b/foo.py")
        assert "--- a/foo.py" in prompt

    def test_findings_have_index(self) -> None:
        findings = [_finding(description="first"), _finding(description="second")]
        prompt = _build_challenger_prompt(findings, "diff")
        assert '"index": 0' in prompt
        assert '"index": 1' in prompt


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestApplyChallengerResponse:
    def test_keeps_all_findings(self) -> None:
        findings = [_finding(severity=7), _finding(severity=5)]
        response = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": 7, "action": "keep", "reason": "valid"},
                    {"index": 1, "severity": 5, "action": "keep", "reason": "valid"},
                ],
                "removed_findings": [],
            }
        )
        result = _apply_challenger_response(response, findings)
        assert len(result) == 2
        assert result[0].severity == 7
        assert result[1].severity == 5

    def test_removes_finding(self) -> None:
        findings = [_finding(severity=7), _finding(severity=3)]
        response = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": 7, "action": "keep", "reason": "valid"},
                ],
                "removed_findings": [
                    {"index": 1, "reason": "not supported by code"},
                ],
            }
        )
        result = _apply_challenger_response(response, findings)
        assert len(result) == 1
        assert result[0].severity == 7

    def test_downgrades_severity(self) -> None:
        findings = [_finding(severity=8)]
        response = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": 5, "action": "downgrade", "reason": "overstated"},
                ],
                "removed_findings": [],
            }
        )
        result = _apply_challenger_response(response, findings)
        assert len(result) == 1
        assert result[0].severity == 5

    def test_removes_all_findings(self) -> None:
        findings = [_finding(severity=3), _finding(severity=2)]
        response = json.dumps(
            {
                "adjudicated_findings": [],
                "removed_findings": [
                    {"index": 0, "reason": "speculative"},
                    {"index": 1, "reason": "not in diff"},
                ],
            }
        )
        result = _apply_challenger_response(response, findings)
        assert len(result) == 0

    def test_merge_findings(self) -> None:
        findings = [
            _finding(severity=7, description="Bug in foo"),
            _finding(severity=5, description="Same bug, design angle"),
        ]
        response = json.dumps(
            {
                "adjudicated_findings": [
                    {
                        "index": 0,
                        "severity": 7,
                        "description": "Bug in foo (consolidated)",
                        "action": "merge",
                        "reason": "merged with finding 1",
                    },
                ],
                "removed_findings": [
                    {"index": 1, "reason": "merged with finding 0"},
                ],
            }
        )
        result = _apply_challenger_response(response, findings)
        assert len(result) == 1
        assert result[0].severity == 7
        assert "consolidated" in result[0].description

    def test_malformed_json_returns_original(self) -> None:
        findings = [_finding(), _finding()]
        result = _apply_challenger_response("not json at all", findings)
        assert len(result) == 2

    def test_missing_adjudicated_key_returns_original(self) -> None:
        findings = [_finding()]
        response = json.dumps({"some_other_key": []})
        result = _apply_challenger_response(response, findings)
        assert len(result) == 1

    def test_preserves_file_and_category_from_original(self) -> None:
        findings = [_finding(file="bar.py", category="security")]
        response = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": 6, "action": "keep", "reason": "valid"},
                ],
                "removed_findings": [],
            }
        )
        result = _apply_challenger_response(response, findings)
        assert result[0].file == "bar.py"
        assert result[0].category == "security"

    def test_out_of_range_index_ignored(self) -> None:
        findings = [_finding()]
        response = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": 5, "action": "keep", "reason": "ok"},
                    {"index": 99, "severity": 5, "action": "keep", "reason": "invalid"},
                ],
                "removed_findings": [],
            }
        )
        result = _apply_challenger_response(response, findings)
        assert len(result) == 1

    def test_negative_index_ignored(self) -> None:
        findings = [_finding()]
        response = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": -1, "severity": 5, "action": "keep", "reason": "bad"},
                    {"index": 0, "severity": 5, "action": "keep", "reason": "ok"},
                ],
                "removed_findings": [],
            }
        )
        result = _apply_challenger_response(response, findings)
        assert len(result) == 1

    def test_duplicate_index_uses_first(self) -> None:
        findings = [_finding(severity=5)]
        response = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": 7, "action": "keep", "reason": "first"},
                    {"index": 0, "severity": 3, "action": "keep", "reason": "duplicate"},
                ],
                "removed_findings": [],
            }
        )
        result = _apply_challenger_response(response, findings)
        assert len(result) == 1
        assert result[0].severity == 7

    def test_markdown_fenced_json(self) -> None:
        findings = [_finding()]
        inner = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": 5, "action": "keep", "reason": "ok"},
                ],
                "removed_findings": [],
            }
        )
        response = f"```json\n{inner}\n```"
        result = _apply_challenger_response(response, findings)
        assert len(result) == 1

    def test_updates_description_and_suggestion(self) -> None:
        findings = [_finding(description="old desc", suggestion="old fix")]
        response = json.dumps(
            {
                "adjudicated_findings": [
                    {
                        "index": 0,
                        "severity": 5,
                        "description": "new desc",
                        "suggestion": "new fix",
                        "action": "keep",
                        "reason": "clarified",
                    },
                ],
                "removed_findings": [],
            }
        )
        result = _apply_challenger_response(response, findings)
        assert result[0].description == "new desc"
        assert result[0].suggestion == "new fix"

    def test_non_numeric_severity_falls_back(self) -> None:
        findings = [_finding(severity=6)]
        response = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": "HIGH", "action": "keep", "reason": "ok"},
                ],
                "removed_findings": [],
            }
        )
        result = _apply_challenger_response(response, findings)
        assert result[0].severity == 6  # preserves original severity on invalid input


# ---------------------------------------------------------------------------
# Integration: _run_challenger_pass on ReviewerRole
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("setup_db")
class TestRunChallengerPassIntegration:
    def _make_ctx(self, challenger_enabled: bool = True, challenger_model: str = "") -> ExecutionContext:
        config = ProjectConfig()
        config.review.challenger_enabled = challenger_enabled
        config.review.challenger_model = challenger_model
        adapter = AsyncMock()
        return ExecutionContext(
            issue_number="42",
            project_dir="/tmp/test",
            config=config,
            adapter=adapter,
            pr_number=1,
        )

    @pytest.mark.asyncio
    async def test_challenger_pass_filters_findings(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        role = ReviewerRole()
        ctx = self._make_ctx()
        review = ReviewResult(
            findings=[_finding(severity=7), _finding(severity=2, description="weak")],
            summary="test",
        )
        challenger_response = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": 7, "action": "keep", "reason": "valid bug"},
                ],
                "removed_findings": [
                    {"index": 1, "reason": "speculative, no code evidence"},
                ],
            }
        )

        mock_result = AsyncMock()
        mock_result.text = challenger_response
        mock_result.cost_usd = Decimal("0.01")

        with patch("sova.roles.reviewer.invoke", return_value=mock_result) as mock_invoke:
            result = await role._run_challenger_pass(ctx, review, "diff content")

        assert len(result.findings) == 1
        assert result.findings[0].severity == 7
        mock_invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_challenger_pass_skipped_on_budget(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        role = ReviewerRole()
        ctx = self._make_ctx()
        ctx.cost_usd = ctx.config.agent.max_budget  # exhaust budget
        review = ReviewResult(findings=[_finding()], summary="test")

        with patch("sova.roles.reviewer.invoke") as mock_invoke:
            result = await role._run_challenger_pass(ctx, review, "diff")

        mock_invoke.assert_not_called()
        assert len(result.findings) == 1

    @pytest.mark.asyncio
    async def test_challenger_llm_failure_returns_original(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        role = ReviewerRole()
        ctx = self._make_ctx()
        review = ReviewResult(findings=[_finding(), _finding()], summary="test")

        with patch("sova.roles.reviewer.invoke", side_effect=RuntimeError("LLM down")):
            result = await role._run_challenger_pass(ctx, review, "diff")

        assert len(result.findings) == 2

    @pytest.mark.asyncio
    async def test_challenger_uses_custom_model(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        role = ReviewerRole()
        ctx = self._make_ctx(challenger_model="opus")
        review = ReviewResult(findings=[_finding()], summary="test")

        mock_result = AsyncMock()
        mock_result.text = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": 5, "action": "keep", "reason": "ok"},
                ],
                "removed_findings": [],
            }
        )
        mock_result.cost_usd = Decimal("0.05")

        with patch("sova.roles.reviewer.invoke", return_value=mock_result) as mock_invoke:
            await role._run_challenger_pass(ctx, review, "diff")

        assert mock_invoke.call_args.kwargs["model"] == "opus"

    @pytest.mark.asyncio
    async def test_challenger_cost_added_to_review(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        role = ReviewerRole()
        ctx = self._make_ctx()
        review = ReviewResult(
            findings=[_finding()],
            summary="test",
            total_cost=Decimal("0.02"),
        )

        mock_result = AsyncMock()
        mock_result.text = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": 5, "action": "keep", "reason": "ok"},
                ],
                "removed_findings": [],
            }
        )
        mock_result.cost_usd = Decimal("0.01")

        with patch("sova.roles.reviewer.invoke", return_value=mock_result):
            result = await role._run_challenger_pass(ctx, review, "diff")

        assert result.total_cost == Decimal("0.03")


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestChallengerConfig:
    def test_default_config_values(self) -> None:
        config = ProjectConfig()
        assert config.review.challenger_enabled is True
        assert config.review.challenger_model == ""

    def test_config_override(self) -> None:
        from sova.config.models import ReviewConfig

        cfg = ReviewConfig(challenger_enabled=False, challenger_model="opus")
        assert cfg.challenger_enabled is False
        assert cfg.challenger_model == "opus"


# ---------------------------------------------------------------------------
# Diff filtering
# ---------------------------------------------------------------------------


class TestFilterDiffForFindings:
    def test_filters_to_referenced_files(self) -> None:
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/bar.py b/bar.py\n"
            "--- a/bar.py\n"
            "+++ b/bar.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-x\n"
            "+y\n"
        )
        findings = [_finding(file="foo.py")]
        result = _filter_diff_for_findings(diff, findings)
        assert "foo.py" in result
        assert "bar.py" not in result

    def test_returns_full_diff_when_no_sections_match(self) -> None:
        diff = "diff --git a/other.py b/other.py\n--- a/other.py\n+++ b/other.py\n"
        findings = [_finding(file="missing.py")]
        result = _filter_diff_for_findings(diff, findings)
        assert result == diff

    def test_multiple_findings_multiple_files(self) -> None:
        diff = "diff --git a/a.py b/a.py\nhunk a\ndiff --git a/b.py b/b.py\nhunk b\ndiff --git a/c.py b/c.py\nhunk c\n"
        findings = [_finding(file="a.py"), _finding(file="c.py")]
        result = _filter_diff_for_findings(diff, findings)
        assert "hunk a" in result
        assert "hunk b" not in result
        assert "hunk c" in result


# ---------------------------------------------------------------------------
# removed_findings type guard
# ---------------------------------------------------------------------------


class TestRemovedFindingsTypeGuard:
    def test_scalar_removed_findings_fails_open(self) -> None:
        findings = [_finding()]
        response = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": 5, "action": "keep", "reason": "ok"},
                ],
                "removed_findings": "not a list",
            }
        )
        result = _apply_challenger_response(response, findings)
        assert len(result) == 1

    def test_numeric_removed_findings_fails_open(self) -> None:
        findings = [_finding()]
        response = json.dumps(
            {
                "adjudicated_findings": [
                    {"index": 0, "severity": 5, "action": "keep", "reason": "ok"},
                ],
                "removed_findings": 42,
            }
        )
        result = _apply_challenger_response(response, findings)
        assert len(result) == 1
