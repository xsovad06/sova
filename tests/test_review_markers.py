"""Tests for sova.utils.review_markers (the sova-addressed marker and address summary)."""

from __future__ import annotations

from sova.utils.review_markers import SOVA_ADDRESSED_MARKER_RE, addressed_marker, format_address_summary


class TestAddressedMarker:
    def test_anchored_marker_round_trips_through_regex(self) -> None:
        sha = "892372e3512475e42638ffe7cab9a14d0734a2b5"
        marker = addressed_marker(sha)
        m = SOVA_ADDRESSED_MARKER_RE.search(marker)
        assert m is not None
        assert m.group(1) == sha

    def test_unanchored_marker_when_sha_unknown(self) -> None:
        marker = addressed_marker(None)
        assert marker == "<!-- sova-addressed -->"
        m = SOVA_ADDRESSED_MARKER_RE.search(marker)
        assert m is not None
        assert m.group(1) is None

    def test_malformed_sha_is_dropped_not_embedded(self) -> None:
        assert addressed_marker("not a sha") == "<!-- sova-addressed -->"

    def test_regex_ignores_review_marker(self) -> None:
        assert SOVA_ADDRESSED_MARKER_RE.search("<!-- sova-review: revise sha=abcdef1 -->") is None


class TestFormatAddressSummary:
    def test_marker_is_first_line_and_table_lists_every_finding(self) -> None:
        findings = [
            {"file": "a.py", "line": 12, "severity": 6, "description": "first"},
            {"file": "b.py", "line": None, "severity": 3, "description": "second"},
        ]
        body = format_address_summary(findings, head_sha="892372e3512475e42638ffe7cab9a14d0734a2b5", round_no=2)
        lines = body.splitlines()
        assert lines[0] == "<!-- sova-addressed: sha=892372e3512475e42638ffe7cab9a14d0734a2b5 -->"
        assert "## Address Review: Round 2" in body
        assert "| 1 | [6/10] `a.py:12`: first | Addressed in 892372e. |" in body
        assert "| 2 | [3/10] `b.py`: second | Addressed in 892372e. |" in body

    def test_empty_findings_still_carries_marker(self) -> None:
        body = format_address_summary([], head_sha=None)
        assert body.startswith("<!-- sova-addressed -->")
        assert "|" not in body

    def test_pipes_and_newlines_cannot_break_the_table(self) -> None:
        findings = [{"file": "", "line": None, "description": "uses a | pipe\nand a newline"}]
        body = format_address_summary(findings, head_sha=None)
        row = next(line for line in body.splitlines() if line.startswith("| 1 |"))
        assert "\\|" in row
        assert "\n" not in row
        assert row.count("|") == 5  # 4 cell delimiters + 1 escaped pipe
