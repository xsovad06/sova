"""Tests for sova.roles._review_format (shared review formatting)."""

from __future__ import annotations

import json

import pytest

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
    @pytest.mark.parametrize(
        ("severity", "expected"),
        [
            pytest.param(5, 5, id="within-range"),
            pytest.param(0, 1, id="zero-clamps-to-one"),
            pytest.param(-3, 1, id="negative-clamps-to-one"),
            pytest.param(15, 10, id="above-ten-clamps"),
            pytest.param(1, 1, id="boundary-one"),
            pytest.param(10, 10, id="boundary-ten"),
        ],
    )
    def test_clamp(self, severity: int, expected: int) -> None:
        assert clamp_severity(severity) == expected


class TestSeverityLabel:
    @pytest.mark.parametrize(
        ("severity", "expected"),
        [
            pytest.param(7, "CRITICAL", id="critical-lower-bound"),
            pytest.param(10, "CRITICAL", id="critical-upper-bound"),
            pytest.param(5, "HIGH", id="high-lower-bound"),
            pytest.param(6, "HIGH", id="high-upper-bound"),
            pytest.param(3, "MEDIUM", id="medium-lower-bound"),
            pytest.param(4, "MEDIUM", id="medium-upper-bound"),
            pytest.param(1, "LOW", id="low-lower-bound"),
            pytest.param(2, "LOW", id="low-upper-bound"),
            # Out-of-range severities are clamped before labeling.
            pytest.param(0, "LOW", id="zero-clamps-to-low"),
            pytest.param(-1, "LOW", id="negative-clamps-to-low"),
            pytest.param(11, "CRITICAL", id="above-ten-clamps-to-critical"),
        ],
    )
    def test_label(self, severity: int, expected: str) -> None:
        assert severity_label(severity) == expected


class TestVerdictFromFindings:
    def test_empty_approve(self) -> None:
        assert verdict_from_findings([]) == "APPROVE"

    @pytest.mark.parametrize(
        ("severity", "expected"),
        [
            pytest.param(7, "BLOCK", id="critical-lower-bound-blocks"),
            pytest.param(10, "BLOCK", id="critical-upper-bound-blocks"),
            pytest.param(3, "REVISE", id="revise-lower-bound"),
            pytest.param(6, "REVISE", id="just-below-critical-revises"),
            # Below the default revise_severity (3): advisory, not blocking.
            pytest.param(1, "APPROVE", id="advisory-severity-one"),
            pytest.param(2, "APPROVE", id="advisory-severity-two"),
            pytest.param(0, "APPROVE", id="zero-clamps-to-approve"),
            pytest.param(15, "BLOCK", id="above-ten-clamps-to-block"),
        ],
    )
    def test_single_finding_verdict(self, severity: int, expected: str) -> None:
        assert verdict_from_findings([_fd(severity=severity)]) == expected

    def test_mixed_uses_max(self) -> None:
        assert verdict_from_findings([_fd(severity=2), _fd(severity=8)]) == "BLOCK"

    def test_explicit_thresholds(self) -> None:
        """Acceptance criteria: verdict_from_severities(..., revise_at=3, block_at=7)."""
        from sova.roles._review_format import verdict_from_severities

        assert verdict_from_severities([2], revise_at=3, block_at=7) == "APPROVE"
        assert verdict_from_severities([3], revise_at=3, block_at=7) == "REVISE"
        assert verdict_from_severities([7], revise_at=3, block_at=7) == "BLOCK"
        assert verdict_from_severities([], revise_at=3, block_at=7) == "APPROVE"

    def test_custom_thresholds_override_defaults(self) -> None:
        from sova.roles._review_format import verdict_from_severities

        # A project that raises revise_severity treats a severity-5 finding as advisory.
        assert verdict_from_severities([5], revise_at=6, block_at=9) == "APPROVE"
        assert verdict_from_severities([6], revise_at=6, block_at=9) == "REVISE"
        assert verdict_from_severities([9], revise_at=6, block_at=9) == "BLOCK"


class TestFormatReviewBody:
    def test_marker_first_line(self) -> None:
        body = format_review_body([], "")
        assert body.split("\n")[0] == "<!-- sova-review: approve -->"

    @pytest.mark.parametrize(
        ("severity", "marker"),
        [
            pytest.param(4, "<!-- sova-review: revise -->", id="revise"),
            pytest.param(8, "<!-- sova-review: block -->", id="block"),
        ],
    )
    def test_marker_for_single_finding(self, severity: int, marker: str) -> None:
        body = format_review_body([_fd(severity=severity)], "")
        assert marker in body

    @pytest.mark.parametrize(
        "expected",
        [
            pytest.param("## Review: APPROVE", id="review-heading"),
            pytest.param("Review of changes.", id="empty-summary-fallback"),
            pytest.param("No issues found after thorough review.", id="no-findings-message"),
        ],
    )
    def test_empty_review_contains(self, expected: str) -> None:
        body = format_review_body([], "")
        assert expected in body

    def test_provided_summary(self) -> None:
        body = format_review_body([], "Custom summary here")
        assert "Custom summary here" in body
        assert "Review of changes." not in body

    def test_findings_count(self) -> None:
        body = format_review_body([_fd(), _fd(severity=3)], "")
        assert "**2 findings**" in body

    @pytest.mark.parametrize(
        ("severity", "label"),
        [
            pytest.param(6, "**[HIGH 6/10]**", id="high"),
            pytest.param(8, "**[CRITICAL 8/10]**", id="critical"),
            pytest.param(2, "**[LOW 2/10]**", id="low"),
            pytest.param(0, "**[LOW 1/10]**", id="clamped-below"),
            pytest.param(15, "**[CRITICAL 10/10]**", id="clamped-above"),
        ],
    )
    def test_numeric_severity_label(self, severity: int, label: str) -> None:
        body = format_review_body([_fd(severity=severity)], "")
        assert label in body

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

    @pytest.mark.parametrize(
        ("findings", "expected"),
        [
            pytest.param(
                [_fd(severity=8, description="SQL injection")],
                "**Block**: 1 critical finding(s) require changes.",
                id="block",
            ),
            pytest.param(
                [_fd(severity=5, description="Missing check")],
                "**Request changes**: 1 high finding(s) require changes.",
                id="revise",
            ),
            pytest.param(
                [_fd(severity=7, description="critical issue"), _fd(severity=5, description="moderate")],
                "**Block**: 1 critical, 1 high finding(s) require changes.",
                id="counts-blocking-findings-by-severity",
            ),
            pytest.param(
                [_fd(severity=8, description="crash"), _fd(severity=2, description="nit")],
                "**Block**: 1 critical finding(s) require changes.",
                id="rationale-excludes-advisory-findings",
            ),
        ],
    )
    def test_verdict_section(self, findings: list[dict], expected: str) -> None:
        body = format_review_body(findings, "")
        assert expected in body

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
        assert "**Request changes**: 1 high finding(s) require changes." in body


class TestFormatReviewBodyAdvisorySplit:
    """Findings below revise_at are advisory: recorded, but never blocking."""

    def test_advisory_only_findings_approve_with_advisory_section(self) -> None:
        findings = [_fd(severity=1, description="nitpick")]
        body = format_review_body(findings, "")
        assert body.startswith("<!-- sova-review: approve -->")
        assert "## Review: APPROVE" in body
        assert "### Advisory (not blocking)" in body
        assert "nitpick" in body
        assert "No blocking issues found after thorough review." in body

    def test_mixed_severities_split_into_two_sections(self) -> None:
        findings = [
            _fd(severity=8, file="blocking.py", description="crash"),
            _fd(severity=2, file="advisory.py", description="nit"),
        ]
        body = format_review_body(findings, "")
        assert "## Review: BLOCK" in body
        assert "### Findings" in body
        assert "### Advisory (not blocking)" in body
        findings_idx = body.index("### Findings")
        advisory_idx = body.index("### Advisory (not blocking)")
        assert findings_idx < advisory_idx
        assert body.index("blocking.py") < advisory_idx
        assert body.index("advisory.py") > advisory_idx

    def test_no_advisory_section_when_everything_blocks(self) -> None:
        body = format_review_body([_fd(severity=6)], "")
        assert "### Advisory (not blocking)" not in body

    def test_no_findings_at_all_omits_advisory_and_uses_clean_text(self) -> None:
        body = format_review_body([], "")
        assert "### Advisory (not blocking)" not in body
        assert "No issues found after thorough review." in body

    def test_custom_thresholds_change_the_split(self) -> None:
        """A severity-5 finding is advisory when revise_at is raised to 6."""
        body = format_review_body([_fd(severity=5)], "", revise_at=6, block_at=9)
        assert "### Advisory (not blocking)" in body
        assert body.startswith("<!-- sova-review: approve -->")


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

    def test_collapses_when_location_has_inline_comment(self) -> None:
        result = _format_finding_line(_fd(file="a.py", line=10, description="bad"), {("a.py", 10)})
        assert "bad" not in result
        assert "see inline comment above" in result
        assert "`a.py:10`" in result

    @pytest.mark.parametrize(
        ("line", "inline_keys"),
        [
            pytest.param(10, {("b.py", 10)}, id="location-not-in-inline-keys"),
            # A finding with line=None can never have an inline comment (those require a diff line).
            pytest.param(None, {("a.py", 10)}, id="file-level-finding-never-collapses"),
        ],
    )
    def test_keeps_full_text(self, line: int | None, inline_keys: set[tuple[str, int]]) -> None:
        result = _format_finding_line(_fd(file="a.py", line=line, description="bad"), inline_keys)
        assert "bad" in result
        assert "see inline comment above" not in result

    def test_no_inline_keys_keeps_full_text(self) -> None:
        result = _format_finding_line(_fd(file="a.py", line=10, description="bad"), None)
        assert "bad" in result


class TestVerdictAction:
    @pytest.mark.parametrize(
        ("verdict", "expected"),
        [
            pytest.param("APPROVE", "Approved", id="approve"),
            pytest.param("BLOCK", "Block", id="block"),
            pytest.param("REVISE", "Request changes", id="revise"),
            pytest.param("OTHER", "Request changes", id="unknown-defaults-to-request-changes"),
        ],
    )
    def test_action(self, verdict: str, expected: str) -> None:
        assert _verdict_action(verdict) == expected


class TestVerdictRationale:
    @pytest.mark.parametrize(
        ("verdict", "findings", "expected"),
        [
            pytest.param("APPROVE", [], "no issues found", id="approve-verdict"),
            pytest.param("APPROVE", [_fd()], "no issues found", id="approve-with-findings-still-no-issues"),
            pytest.param("REVISE", [], "no issues found", id="non-approve-empty-findings"),
            # Advisory findings (below revise_at) never drove a non-APPROVE verdict.
            pytest.param("REVISE", [_fd(severity=1)], "no issues found", id="non-approve-only-advisory-findings"),
            pytest.param(
                "BLOCK", [_fd(severity=8)], "1 critical finding(s) require changes", id="single-blocking-finding"
            ),
            pytest.param(
                "BLOCK",
                [_fd(severity=2), _fd(severity=8), _fd(severity=5), _fd(severity=9)],
                "2 critical, 1 high finding(s) require changes",
                id="counts-grouped-by-severity-label",
            ),
            pytest.param(
                "BLOCK",
                [_fd(severity=3), _fd(severity=8)],
                "1 critical, 1 medium finding(s) require changes",
                id="ordered-most-severe-first",
            ),
        ],
    )
    def test_rationale(self, verdict: str, findings: list[dict], expected: str) -> None:
        assert _verdict_rationale(verdict, findings) == expected


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
        with pytest.raises(json.JSONDecodeError):
            format_from_json("not json")


class TestFormatFromJsonSha:
    """The /review-pr command anchors its verdict to the reviewed commit via the ``sha`` field."""

    @pytest.mark.parametrize(
        ("extra", "marker"),
        [
            pytest.param(
                {"sha": "892372e3512475e42638ffe7cab9a14d0734a2b5"},
                "<!-- sova-review: approve sha=892372e3512475e42638ffe7cab9a14d0734a2b5 -->",
                id="sha-is-embedded-in-marker",
            ),
            pytest.param({}, "<!-- sova-review: approve -->", id="missing-sha-leaves-marker-unanchored"),
            pytest.param(
                {"sha": "<headRefOid from Step 1>"}, "<!-- sova-review: approve -->", id="malformed-sha-is-not-embedded"
            ),
            pytest.param(
                {"sha": "892372E3"},
                "<!-- sova-review: approve sha=892372e3 -->",
                id="uppercase-sha-is-lowercased-to-match-github-head",
            ),
        ],
    )
    def test_marker(self, extra: dict[str, str], marker: str) -> None:
        body = format_from_json(json.dumps({"findings": [], "summary": "s", **extra}))
        assert body.splitlines()[0] == marker
