"""Tests for reviewer body formatting functions and post-failure behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sova.roles.reviewer import (
    ReviewFinding,
    ReviewResult,
    _format_findings_body,
    _format_findings_comment,
    _format_review_body,
)


def _finding(severity: int, file: str = "foo.py") -> ReviewFinding:
    return ReviewFinding(file=file, line=1, severity=severity, category="test", description="desc")


class TestFormatFindingsBody:
    """_format_findings_body emits the sova-review marker as the first line."""

    def test_marker_present_with_no_findings(self) -> None:
        body = _format_findings_body([], "")
        first_line = body.split("\n")[0]
        assert first_line == "<!-- sova-review: approve -->"

    def test_marker_approve_when_no_findings(self) -> None:
        body = _format_findings_body([], "")
        assert "<!-- sova-review: approve -->" in body

    def test_marker_revise_when_low_severity_findings(self) -> None:
        findings = [_finding(severity=3)]
        body = _format_findings_body(findings, "")
        assert "<!-- sova-review: revise -->" in body

    def test_marker_block_when_critical_findings(self) -> None:
        findings = [_finding(severity=7)]
        body = _format_findings_body(findings, "")
        assert "<!-- sova-review: block -->" in body

    def test_marker_is_first_line(self) -> None:
        findings = [_finding(severity=3)]
        body = _format_findings_body(findings, "summary text")
        first_line = body.split("\n")[0]
        assert first_line.startswith("<!-- sova-review:")

    def test_marker_lowercase_verdict(self) -> None:
        body = _format_findings_body([], "")
        assert "<!-- sova-review: approve -->" in body
        assert "APPROVE" not in body.split("\n")[0]

    def test_body_still_contains_review_heading(self) -> None:
        body = _format_findings_body([], "")
        assert "## Review: APPROVE" in body


class TestFormatReviewBody:
    """_format_review_body delegates to _format_findings_body and includes the marker."""

    def test_includes_marker(self) -> None:
        body = _format_review_body([], "")
        assert "<!-- sova-review: approve -->" in body

    def test_block_verdict_marker(self) -> None:
        body = _format_review_body([_finding(severity=8)], "")
        assert "<!-- sova-review: block -->" in body


class TestFormatFindingsComment:
    """_format_findings_comment is the fallback path -- also includes the marker."""

    def test_includes_marker(self) -> None:
        comment = _format_findings_comment([], "")
        assert "<!-- sova-review: approve -->" in comment


class TestReviewResultPostFailed:
    """ReviewResult.post_failed defaults to False and is settable."""

    def test_post_failed_default_is_false(self) -> None:
        result = ReviewResult()
        assert result.post_failed is False

    def test_post_failed_can_be_set_true(self) -> None:
        result = ReviewResult(post_failed=True)
        assert result.post_failed is True

    def test_post_failed_false_on_findings_result(self) -> None:
        finding = ReviewFinding(file="x.py", line=1, severity=7, category="bug", description="issue")
        result = ReviewResult(findings=[finding])
        assert result.post_failed is False


class TestPostReviewReturnsBool:
    """_post_review returns True on success and False when all posting attempts fail."""

    @pytest.mark.asyncio
    async def test_post_review_returns_true_on_first_success(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        role = ReviewerRole()
        mock_ctx = MagicMock()
        mock_ctx.adapter.post_pr_review = AsyncMock()
        mock_ctx.pr_number = 1

        review = ReviewResult(findings=[], summary="clean")
        result = await role._post_review(mock_ctx, review, "diff content")

        assert result is True
        mock_ctx.adapter.post_pr_review.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_review_returns_false_when_all_attempts_fail(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        role = ReviewerRole()
        mock_ctx = MagicMock()
        mock_ctx.adapter.post_pr_review = AsyncMock(side_effect=RuntimeError("API error"))
        mock_ctx.adapter.post_pr_comment = AsyncMock(side_effect=RuntimeError("comment API error"))
        mock_ctx.pr_number = 1

        review = ReviewResult(findings=[], summary="clean")
        result = await role._post_review(mock_ctx, review, "diff content")

        assert result is False
        mock_ctx.adapter.post_pr_review.assert_awaited()
        mock_ctx.adapter.post_pr_comment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_review_falls_back_to_comment_after_review_api_failure(self) -> None:
        from sova.roles.reviewer import ReviewerRole

        role = ReviewerRole()
        mock_ctx = MagicMock()
        mock_ctx.adapter.post_pr_review = AsyncMock(side_effect=RuntimeError("review API error"))
        mock_ctx.adapter.post_pr_comment = AsyncMock()
        mock_ctx.pr_number = 1

        review = ReviewResult(findings=[], summary="clean")
        result = await role._post_review(mock_ctx, review, "diff content")

        assert result is True
        mock_ctx.adapter.post_pr_comment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_inline_failure_retries_body_only_and_succeeds(self) -> None:
        """When inline post_pr_review fails, the retry without inline comments succeeds."""
        from unittest.mock import patch

        from sova.roles.reviewer import ReviewerRole

        role = ReviewerRole()
        mock_ctx = MagicMock()
        # First call (with inline comments) raises; second call (body-only) succeeds.
        mock_ctx.adapter.post_pr_review = AsyncMock(side_effect=[RuntimeError("inline failed"), None])
        mock_ctx.adapter.post_pr_comment = AsyncMock()
        mock_ctx.pr_number = 1

        finding = ReviewFinding(file="foo.py", line=1, severity=5, category="bug", description="problem")
        review = ReviewResult(findings=[finding], summary="one finding")

        with patch("sova.roles.reviewer.parse_diff_lines", return_value={"foo.py": {1}}):
            result = await role._post_review(mock_ctx, review, "diff content")

        assert result is True
        assert mock_ctx.adapter.post_pr_review.await_count == 2, "must try inline then body-only"
        mock_ctx.adapter.post_pr_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inline_and_body_only_both_fail_falls_back_to_comment(self) -> None:
        """When both post_pr_review attempts fail, the comment fallback is used."""
        from unittest.mock import patch

        from sova.roles.reviewer import ReviewerRole

        role = ReviewerRole()
        mock_ctx = MagicMock()
        mock_ctx.adapter.post_pr_review = AsyncMock(side_effect=RuntimeError("review API down"))
        mock_ctx.adapter.post_pr_comment = AsyncMock()
        mock_ctx.pr_number = 1

        finding = ReviewFinding(file="foo.py", line=1, severity=5, category="bug", description="problem")
        review = ReviewResult(findings=[finding], summary="one finding")

        with patch("sova.roles.reviewer.parse_diff_lines", return_value={"foo.py": {1}}):
            result = await role._post_review(mock_ctx, review, "diff content")

        assert result is True
        assert mock_ctx.adapter.post_pr_review.await_count == 2, "must try inline then body-only"
        mock_ctx.adapter.post_pr_comment.assert_awaited_once()
