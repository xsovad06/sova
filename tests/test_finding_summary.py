"""Tests for _build_finding_summary in the reviewer role."""

from __future__ import annotations

from sova.roles._review_comments import ReviewFinding, ReviewResult
from sova.roles.reviewer import _build_finding_summary


class TestBuildFindingSummary:
    def test_empty_review(self) -> None:
        review = ReviewResult()
        summary = _build_finding_summary(review)
        assert summary == {
            "total": 0,
            "actionable": 0,
            "by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        }

    def test_all_actionable(self) -> None:
        review = ReviewResult(
            findings=[
                ReviewFinding(file="a.py", severity=9, category="bug", description="critical bug"),
                ReviewFinding(file="b.py", severity=6, category="style", description="style issue"),
                ReviewFinding(file="c.py", severity=3, category="nit", description="nit"),
            ]
        )
        summary = _build_finding_summary(review)
        assert summary["total"] == 3
        assert summary["actionable"] == 3
        assert summary["by_severity"]["critical"] == 1
        assert summary["by_severity"]["high"] == 1
        assert summary["by_severity"]["medium"] == 1

    def test_protected_path_excluded_from_actionable(self) -> None:
        review = ReviewResult(
            findings=[
                ReviewFinding(file="a.py", severity=5, category="bug", description="real bug"),
                ReviewFinding(
                    file=".github/workflows/ci.yml",
                    severity=3,
                    category="protected-path",
                    description="protected",
                ),
            ]
        )
        summary = _build_finding_summary(review)
        assert summary["total"] == 2
        assert summary["actionable"] == 1
        assert summary["by_severity"]["high"] == 1

    def test_severity_buckets(self) -> None:
        review = ReviewResult(
            findings=[
                ReviewFinding(file="a.py", severity=10, category="bug", description="sev 10"),
                ReviewFinding(file="b.py", severity=8, category="bug", description="sev 8"),
                ReviewFinding(file="c.py", severity=7, category="bug", description="sev 7"),
                ReviewFinding(file="d.py", severity=6, category="bug", description="sev 6"),
                ReviewFinding(file="e.py", severity=5, category="bug", description="sev 5"),
                ReviewFinding(file="f.py", severity=4, category="bug", description="sev 4"),
                ReviewFinding(file="g.py", severity=3, category="bug", description="sev 3"),
                ReviewFinding(file="h.py", severity=1, category="bug", description="sev 1"),
            ]
        )
        summary = _build_finding_summary(review)
        assert summary["by_severity"]["critical"] == 3  # 10, 8, 7 (>= 7)
        assert summary["by_severity"]["high"] == 2  # 6, 5 (>= 5)
        assert summary["by_severity"]["medium"] == 2  # 4, 3 (>= 3)
        assert summary["by_severity"]["low"] == 1  # 1 (< 3)
