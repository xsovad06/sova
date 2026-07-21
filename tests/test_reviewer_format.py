"""Tests for reviewer body formatting functions."""

from __future__ import annotations

from sova.roles.reviewer import ReviewFinding, _format_findings_body, _format_findings_comment, _format_review_body


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
