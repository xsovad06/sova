"""Tests for sova.roles._review_format (shared review formatting)."""

from __future__ import annotations

import json

from sova.roles._review_format import (
    _format_finding_line,
    _verdict_action,
    _verdict_rationale,
    clamp_severity,
    format_from_json,
    format_review_body,
    severity_label,
    verdict_from_findings,
)


def _fd(
    severity: int = 5,
    file: str = "foo.py",
    line: int | None = 1,
    category: str = "bug",
    description: str = "desc",
    suggestion: str = "",
) -> dict:
    return {
        "file": file,
        "line": line,
        "severity": severity,
        "category": category,
        "description": description,
        "suggestion": suggestion,
    }


class TestClampSeverity:
    def test_within_range(self) -> None:
        assert clamp_severity(5) == 5

    def test_zero_clamps_to_one(self) -> None:
        assert clamp_severity(0) == 1

    def test_negative_clamps_to_one(self) -> None:
        assert clamp_severity(-3) == 1

    def test_above_ten_clamps(self) -> None:
        assert clamp_severity(15) == 10

    def test_boundary_one(self) -> None:
        assert clamp_severity(1) == 1

    def test_boundary_ten(self) -> None:
        assert clamp_severity(10) == 10


class TestSeverityLabel:
    def test_critical(self) -> None:
        assert severity_label(7) == "CRITICAL"
        assert severity_label(10) == "CRITICAL"

    def test_high(self) -> None:
        assert severity_label(5) == "HIGH"
        assert severity_label(6) == "HIGH"

    def test_medium(self) -> None:
        assert severity_label(3) == "MEDIUM"
        assert severity_label(4) == "MEDIUM"

    def test_low(self) -> None:
        assert severity_label(1) == "LOW"
        assert severity_label(2) == "LOW"

    def test_clamps_before_labeling(self) -> None:
        assert severity_label(0) == "LOW"
        assert severity_label(-1) == "LOW"
        assert severity_label(11) == "CRITICAL"


class TestVerdictFromFindings:
    def test_empty_approve(self) -> None:
        assert verdict_from_findings([]) == "APPROVE"

    def test_critical_block(self) -> None:
        assert verdict_from_findings([_fd(severity=7)]) == "BLOCK"
        assert verdict_from_findings([_fd(severity=10)]) == "BLOCK"

    def test_below_critical_revise(self) -> None:
        assert verdict_from_findings([_fd(severity=1)]) == "REVISE"
        assert verdict_from_findings([_fd(severity=6)]) == "REVISE"

    def test_mixed_uses_max(self) -> None:
        assert verdict_from_findings([_fd(severity=2), _fd(severity=8)]) == "BLOCK"

    def test_clamps_severity(self) -> None:
        assert verdict_from_findings([_fd(severity=0)]) == "REVISE"
        assert verdict_from_findings([_fd(severity=15)]) == "BLOCK"


class TestFormatReviewBody:
    def test_marker_first_line(self) -> None:
        body = format_review_body([], "")
        assert body.split("\n")[0] == "<!-- sova-review: approve -->"

    def test_marker_revise(self) -> None:
        body = format_review_body([_fd(severity=4)], "")
        assert "<!-- sova-review: revise -->" in body

    def test_marker_block(self) -> None:
        body = format_review_body([_fd(severity=8)], "")
        assert "<!-- sova-review: block -->" in body

    def test_review_heading(self) -> None:
        body = format_review_body([], "")
        assert "## Review: APPROVE" in body

    def test_empty_summary_fallback(self) -> None:
        body = format_review_body([], "")
        assert "Review of changes." in body

    def test_provided_summary(self) -> None:
        body = format_review_body([], "Custom summary here")
        assert "Custom summary here" in body
        assert "Review of changes." not in body

    def test_no_findings_message(self) -> None:
        body = format_review_body([], "")
        assert "No issues found after thorough review." in body

    def test_findings_count(self) -> None:
        body = format_review_body([_fd(), _fd(severity=3)], "")
        assert "**2 findings**" in body

    def test_numeric_severity_in_label(self) -> None:
        body = format_review_body([_fd(severity=6)], "")
        assert "**[HIGH 6/10]**" in body

    def test_critical_numeric_label(self) -> None:
        body = format_review_body([_fd(severity=8)], "")
        assert "**[CRITICAL 8/10]**" in body

    def test_low_numeric_label(self) -> None:
        body = format_review_body([_fd(severity=2)], "")
        assert "**[LOW 2/10]**" in body

    def test_sorted_desc_by_severity(self) -> None:
        findings = [_fd(severity=2, file="low.py"), _fd(severity=9, file="high.py")]
        body = format_review_body(findings, "")
        assert body.index("high.py") < body.index("low.py")

    def test_finding_with_line(self) -> None:
        body = format_review_body([_fd(line=42, file="a.py")], "")
        assert "`a.py:42`" in body

    def test_finding_without_line(self) -> None:
        body = format_review_body([_fd(line=None, file="noln.py")], "")
        assert "`noln.py`" in body
        assert "`noln.py:`" not in body

    def test_finding_with_suggestion(self) -> None:
        body = format_review_body([_fd(suggestion="use X")], "")
        assert "Fix: use X" in body

    def test_finding_without_suggestion(self) -> None:
        body = format_review_body([_fd(suggestion="")], "")
        assert "Fix: " not in body

    def test_positives_section_present(self) -> None:
        body = format_review_body([], "", positives=["Good naming", "Clean structure"])
        assert "### What's Done Well" in body
        assert "- Good naming" in body
        assert "- Clean structure" in body

    def test_positives_section_omitted_when_empty(self) -> None:
        body = format_review_body([], "", positives=[])
        assert "### What's Done Well" not in body

    def test_positives_section_omitted_when_none(self) -> None:
        body = format_review_body([], "", positives=None)
        assert "### What's Done Well" not in body

    def test_verdict_section_approve(self) -> None:
        body = format_review_body([], "")
        assert "### Verdict" in body
        assert "**Approved**: no issues found." in body

    def test_verdict_section_block(self) -> None:
        body = format_review_body([_fd(severity=8, description="SQL injection")], "")
        assert "**Block**: SQL injection." in body

    def test_verdict_section_revise(self) -> None:
        body = format_review_body([_fd(severity=5, description="Missing check")], "")
        assert "**Request changes**: Missing check." in body

    def test_verdict_uses_highest_severity_description(self) -> None:
        findings = [
            _fd(severity=3, description="minor"),
            _fd(severity=7, description="critical issue"),
        ]
        body = format_review_body(findings, "")
        assert "**Block**: critical issue." in body

    def test_verdict_strips_trailing_punctuation(self) -> None:
        body = format_review_body([_fd(severity=8, description="SQL injection risk.")], "")
        assert "**Block**: SQL injection risk." in body
        assert "SQL injection risk.." not in body

    def test_severity_clamped_in_label(self) -> None:
        body = format_review_body([_fd(severity=0)], "")
        assert "**[LOW 1/10]**" in body

    def test_severity_clamped_above(self) -> None:
        body = format_review_body([_fd(severity=15)], "")
        assert "**[CRITICAL 10/10]**" in body

    def test_category_in_finding(self) -> None:
        body = format_review_body([_fd(category="security")], "")
        assert "[security]" in body

    def test_full_output_structure(self) -> None:
        findings = [_fd(severity=6, file="x.py", line=10, description="bad", suggestion="fix")]
        body = format_review_body(findings, "Overall good", positives=["Nice tests"])
        assert body.startswith("<!-- sova-review: revise -->")
        assert "## Review: REVISE" in body
        assert "Overall good" in body
        assert "**1 finding**" in body
        assert "**[HIGH 6/10]**" in body
        assert "### What's Done Well" in body
        assert "- Nice tests" in body
        assert "### Verdict" in body
        assert "**Request changes**: bad." in body


class TestFormatFindingLine:
    def test_with_line_number(self) -> None:
        result = _format_finding_line(_fd(severity=6, file="a.py", line=10, category="bug", description="bad"))
        assert result == "- **[HIGH 6/10]** [bug] `a.py:10`: bad"

    def test_without_line_number(self) -> None:
        result = _format_finding_line(_fd(line=None, file="b.py"))
        assert "`b.py`" in result
        assert "`b.py:`" not in result

    def test_with_suggestion(self) -> None:
        result = _format_finding_line(_fd(suggestion="use X"))
        assert result.endswith("Fix: use X")

    def test_without_suggestion(self) -> None:
        result = _format_finding_line(_fd(suggestion=""))
        assert "Fix: " not in result

    def test_defaults_for_missing_keys(self) -> None:
        result = _format_finding_line({})
        assert "`unknown`" in result
        assert "[other]" in result
        assert "**[HIGH 5/10]**" in result

    def test_severity_clamped(self) -> None:
        result = _format_finding_line(_fd(severity=0))
        assert "**[LOW 1/10]**" in result


class TestVerdictAction:
    def test_approve(self) -> None:
        assert _verdict_action("APPROVE") == "Approved"

    def test_block(self) -> None:
        assert _verdict_action("BLOCK") == "Block"

    def test_revise(self) -> None:
        assert _verdict_action("REVISE") == "Request changes"

    def test_unknown_defaults_to_request_changes(self) -> None:
        assert _verdict_action("OTHER") == "Request changes"


class TestVerdictRationale:
    def test_approve_verdict(self) -> None:
        assert _verdict_rationale("APPROVE", []) == "no issues found"

    def test_approve_with_findings_still_no_issues(self) -> None:
        assert _verdict_rationale("APPROVE", [_fd()]) == "no issues found"

    def test_non_approve_empty_findings(self) -> None:
        assert _verdict_rationale("REVISE", []) == "no issues found"

    def test_uses_highest_severity_description(self) -> None:
        findings = [_fd(severity=2, description="minor"), _fd(severity=8, description="critical")]
        assert _verdict_rationale("BLOCK", findings) == "critical"

    def test_strips_trailing_punctuation(self) -> None:
        assert _verdict_rationale("REVISE", [_fd(description="issue.")]) == "issue"
        assert _verdict_rationale("REVISE", [_fd(description="issue!")]) == "issue"
        assert _verdict_rationale("REVISE", [_fd(description="issue?")]) == "issue"

    def test_missing_description_fallback(self) -> None:
        assert _verdict_rationale("REVISE", [{}]) == "issue found"


class TestFormatFromJson:
    def test_basic(self) -> None:
        data = {
            "findings": [{"file": "a.py", "line": 1, "severity": 5, "category": "bug", "description": "d"}],
            "summary": "sum",
        }
        body = format_from_json(json.dumps(data))
        assert "## Review: REVISE" in body
        assert "**[HIGH 5/10]**" in body
        assert "sum" in body

    def test_with_positives(self) -> None:
        data = {"findings": [], "summary": "clean", "positives": ["well done"]}
        body = format_from_json(json.dumps(data))
        assert "### What's Done Well" in body
        assert "- well done" in body

    def test_missing_fields_defaults(self) -> None:
        body = format_from_json("{}")
        assert "<!-- sova-review: approve -->" in body
        assert "Review of changes." in body

    def test_positives_missing_omits_section(self) -> None:
        data = {"findings": [], "summary": "ok"}
        body = format_from_json(json.dumps(data))
        assert "### What's Done Well" not in body

    def test_invalid_json_raises(self) -> None:
        import pytest

        with pytest.raises(json.JSONDecodeError):
            format_from_json("not json")
