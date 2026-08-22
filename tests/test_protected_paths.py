"""Tests for protected paths reviewer verdict capping."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.roles._review_comments import (
    ReviewFinding,
    ReviewResult,
    _make_protected_path_finding,
    _verdict_label,
)
from sova.roles.reviewer import ReviewerRole, _check_protected_paths


class TestCheckProtectedPaths:
    def test_empty_config_returns_empty(self) -> None:
        assert _check_protected_paths(["src/main.py"], []) == []

    def test_no_files_returns_empty(self) -> None:
        assert _check_protected_paths([], [".github/"]) == []

    def test_directory_prefix_match(self) -> None:
        files = ["src/main.py", ".github/workflows/ci.yml", "README.md"]
        result = _check_protected_paths(files, [".github/"])
        assert result == [".github/workflows/ci.yml"]

    def test_exact_file_match(self) -> None:
        files = ["CODEOWNERS", "src/app.py"]
        result = _check_protected_paths(files, ["CODEOWNERS"])
        assert result == ["CODEOWNERS"]

    def test_multiple_protected_paths(self) -> None:
        files = [".github/workflows/ci.yml", "deploy/prod.yaml", "src/main.py"]
        result = _check_protected_paths(files, [".github/", "deploy/"])
        assert result == [".github/workflows/ci.yml", "deploy/prod.yaml"]

    def test_no_match_returns_empty(self) -> None:
        files = ["src/main.py", "tests/test_foo.py"]
        result = _check_protected_paths(files, [".github/", "deploy/"])
        assert result == []

    def test_multiple_files_same_prefix(self) -> None:
        files = [".github/workflows/ci.yml", ".github/CODEOWNERS"]
        result = _check_protected_paths(files, [".github/"])
        assert result == [".github/workflows/ci.yml", ".github/CODEOWNERS"]


class TestMakeProtectedPathFinding:
    def test_single_file(self) -> None:
        finding = _make_protected_path_finding([".github/workflows/ci.yml"])
        assert finding.severity == 1
        assert finding.category == "protected-path"
        assert finding.file == ".github/workflows/ci.yml"
        assert "Human approval required" in finding.description

    def test_multiple_files_sorted(self) -> None:
        finding = _make_protected_path_finding(["deploy/prod.yaml", ".github/ci.yml"])
        assert ".github/ci.yml, deploy/prod.yaml" in finding.description

    def test_finding_prevents_approve_verdict(self) -> None:
        finding = _make_protected_path_finding([".github/workflows/ci.yml"])
        assert _verdict_label([finding]) == "REVISE"

    def test_finding_does_not_override_block(self) -> None:
        protected = _make_protected_path_finding([".github/workflows/ci.yml"])
        critical = ReviewFinding(file="x.py", severity=8, category="bug", description="crash")
        assert _verdict_label([protected, critical]) == "BLOCK"

    def test_finding_combined_with_medium_severity(self) -> None:
        protected = _make_protected_path_finding(["deploy/prod.yaml"])
        medium = ReviewFinding(file="y.py", severity=4, category="style", description="naming")
        assert _verdict_label([protected, medium]) == "REVISE"


class TestActionableExcludesProtectedPath:
    def test_protected_path_excluded_from_actionable(self) -> None:
        result = ReviewResult()
        result.findings.append(_make_protected_path_finding([".github/ci.yml"]))
        assert result.actionable == []

    def test_code_findings_remain_actionable(self) -> None:
        result = ReviewResult()
        bug = ReviewFinding(file="x.py", severity=7, category="bug", description="crash")
        result.findings.append(bug)
        result.findings.append(_make_protected_path_finding([".github/ci.yml"]))
        assert result.actionable == [bug]

    def test_verdict_still_uses_all_findings(self) -> None:
        result = ReviewResult()
        result.findings.append(_make_protected_path_finding([".github/ci.yml"]))
        assert _verdict_label(result.findings) == "REVISE"
        assert result.actionable == []

    def test_multiple_categories_mixed_with_protected(self) -> None:
        result = ReviewResult()
        bug = ReviewFinding(file="a.py", severity=7, category="bug", description="crash")
        style = ReviewFinding(file="b.py", severity=4, category="style", description="naming")
        security = ReviewFinding(file="c.py", severity=6, category="security", description="leak")
        protected = _make_protected_path_finding([".github/ci.yml"])
        result.findings.extend([protected, bug, style, security])
        assert len(result.actionable) == 3
        assert all(f.category != "protected-path" for f in result.actionable)


class TestMakeProtectedPathFindingValidation:
    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            _make_protected_path_finding([])


class TestProtectedPathIntegration:
    """Integration test: reviewer flow with protected paths config."""

    @pytest.mark.asyncio
    async def test_protected_path_only_verdict_is_approved(self) -> None:
        """When a PR touches only protected paths with no code issues,
        the verdict label should be sova:approved and handoff should
        use needs_human_review (not address_review).
        """
        from sova.config.models import ProjectConfig, ReviewConfig

        config = ProjectConfig()
        config.review = ReviewConfig(protected_paths=[".github/"])

        adapter = AsyncMock()
        adapter.get_task.return_value = MagicMock(
            title="Test issue",
            body="Test body",
            state="in_review",
        )
        adapter.repo = "owner/repo"

        ctx = MagicMock()
        ctx.issue_number = "42"
        ctx.pr_number = 123
        ctx.pr_url = "https://github.com/owner/repo/pull/123"
        ctx.branch_name = "feat/test"
        ctx.repo = "owner/repo"
        ctx.config = config
        ctx.project_dir = "/tmp/test"
        ctx.working_dir = "/tmp/test"
        ctx.adapter = adapter
        ctx.force = False
        ctx.task_run_id = None
        ctx.resume_run_id = None
        ctx.cost_usd = 0
        ctx.add_cost = MagicMock()

        review = ReviewResult(
            findings=[],
            summary="Code looks clean.",
        )
        protected = _make_protected_path_finding([".github/workflows/ci.yml"])
        review.findings.append(protected)

        role = ReviewerRole.__new__(ReviewerRole)

        captured_label = {}

        async def fake_add_label(issue: str, label: str) -> None:
            captured_label["label"] = label

        adapter.add_label = fake_add_label
        adapter.remove_label = AsyncMock()

        await role._write_verdict_label(ctx, review)
        assert captured_label["label"] == "sova:approved"

        with (
            patch("sova.roles.reviewer.write_handoff") as mock_db_handoff,
            patch("sova.roles.reviewer.write_handoff_file") as mock_file_handoff,
        ):
            await role._write_handoff(ctx, review)
            assert not mock_db_handoff.called
            mock_file_handoff.assert_called_once()
            dashboard_handoff = mock_file_handoff.call_args[0][1]
            assert dashboard_handoff.details["next_action"] == "needs_human_review"
            assert len(dashboard_handoff.next_actions) == 1
            assert dashboard_handoff.next_actions[0].id == "integrate"
            assert dashboard_handoff.next_actions[0].auto_execute is False
