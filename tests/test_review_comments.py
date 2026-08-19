"""Tests for sova.roles._review_comments (direct imports, no facade)."""

from __future__ import annotations

from decimal import Decimal

from sova.adapters.base import Task
from sova.roles._review_comments import (
    ReviewFinding,
    ReviewResult,
    _build_review_comments,
    _build_review_prompt,
    _chunk_diff,
    _compact_spec_ref,
    _extract_json,
    _extract_spec_sections,
    _format_addressed_findings,
    _format_findings_body,
    _format_findings_comment,
    _format_inline_comment,
    _format_review_body,
    _parse_findings,
    _safe_severity,
    _severity_label,
    _sova_verdict_label_name,
    _verdict_label,
)


def _finding(
    severity: int = 5,
    file: str = "foo.py",
    line: int | None = 1,
    category: str = "bug",
    description: str = "desc",
    suggestion: str = "",
) -> ReviewFinding:
    return ReviewFinding(
        file=file,
        severity=severity,
        category=category,
        description=description,
        suggestion=suggestion,
        line=line,
    )


def _task(title: str = "Fix bug", body: str = "Details here") -> Task:
    return Task(id="42", title=title, body=body)


# -- Dataclasses --


class TestReviewFinding:
    def test_defaults(self) -> None:
        f = ReviewFinding(file="a.py", severity=3, category="bug", description="d")
        assert f.line is None
        assert f.suggestion == ""

    def test_all_fields(self) -> None:
        f = _finding(severity=8, file="b.py", line=10, category="security", suggestion="fix it")
        assert f.file == "b.py"
        assert f.severity == 8
        assert f.line == 10
        assert f.suggestion == "fix it"


class TestReviewResult:
    def test_defaults(self) -> None:
        r = ReviewResult()
        assert r.findings == []
        assert r.summary == ""
        assert r.total_cost == Decimal("0")
        assert r.post_failed is False

    def test_actionable_returns_copy(self) -> None:
        f1 = _finding(severity=3)
        r = ReviewResult(findings=[f1])
        copy = r.actionable
        assert copy == [f1]
        copy.append(_finding(severity=9))
        assert len(r.findings) == 1

    def test_actionable_includes_all_severities(self) -> None:
        low = _finding(severity=1)
        high = _finding(severity=9)
        r = ReviewResult(findings=[low, high])
        assert len(r.actionable) == 2
        assert r.actionable == [low, high]


# -- _safe_severity --


class TestSafeSeverity:
    def test_int_passthrough(self) -> None:
        assert _safe_severity(7) == 7

    def test_none_returns_default(self) -> None:
        assert _safe_severity(None) == 5

    def test_none_returns_custom_default(self) -> None:
        assert _safe_severity(None, default=3) == 3

    def test_non_numeric_string_returns_default(self) -> None:
        assert _safe_severity("HIGH") == 5

    def test_numeric_string(self) -> None:
        assert _safe_severity("8") == 8

    def test_float_truncates(self) -> None:
        assert _safe_severity(7.9) == 7

    def test_zero(self) -> None:
        assert _safe_severity(0) == 0


# -- _extract_json --


class TestExtractJson:
    def test_plain_json(self) -> None:
        text = '{"findings": [{"file": "a.py"}], "summary": "ok"}'
        result = _extract_json(text)
        assert result is not None
        assert "findings" in result

    def test_json_with_preamble(self) -> None:
        text = 'Here is my review:\n{"findings": [], "summary": "clean"}'
        result = _extract_json(text)
        assert result is not None
        assert result["summary"] == "clean"

    def test_multiple_braces_prefers_findings(self) -> None:
        text = '{"other": 1} some text {"findings": [{"file": "x.py"}]}'
        result = _extract_json(text)
        assert result is not None
        assert "findings" in result

    def test_no_findings_key_returns_first_valid(self) -> None:
        text = '{"a": 1} {"b": 2}'
        result = _extract_json(text)
        assert result == {"a": 1}

    def test_no_valid_json_returns_none(self) -> None:
        assert _extract_json("no json here at all") is None

    def test_invalid_brace_then_valid(self) -> None:
        text = '{broken {"findings": []}'
        result = _extract_json(text)
        assert result is not None
        assert "findings" in result


# -- _parse_findings --


class TestParseFindings:
    def test_valid_json(self) -> None:
        text = '{"findings": [{"file": "a.py", "severity": 3, "category": "bug", "description": "d"}], "summary": "ok"}'
        findings, summary = _parse_findings(text)
        assert len(findings) == 1
        assert findings[0].file == "a.py"
        assert summary == "ok"

    def test_fenced_json(self) -> None:
        text = '```json\n{"findings": [], "summary": "clean"}\n```'
        findings, summary = _parse_findings(text)
        assert findings == []
        assert summary == "clean"

    def test_fenced_no_lang(self) -> None:
        finding = '{"file": "b.py", "severity": 5, "category": "test", "description": "x"}'
        inner = f'{{"findings": [{finding}], "summary": "s"}}'
        text = f"```\n{inner}\n```"
        findings, _ = _parse_findings(text)
        assert len(findings) == 1

    def test_unparseable_returns_empty(self) -> None:
        findings, summary = _parse_findings("totally not json at all")
        assert findings == []
        assert summary == "Failed to parse review response"

    def test_missing_fields_use_defaults(self) -> None:
        text = '{"findings": [{}], "summary": ""}'
        findings, _ = _parse_findings(text)
        assert len(findings) == 1
        assert findings[0].file == "unknown"
        assert findings[0].severity == 5
        assert findings[0].category == "other"
        assert findings[0].line is None

    def test_severity_coerced_via_safe_severity(self) -> None:
        inner = '{"file": "a.py", "severity": "HIGH", "category": "bug", "description": "d"}'
        text = f'{{"findings": [{inner}], "summary": ""}}'
        findings, _ = _parse_findings(text)
        assert findings[0].severity == 5


# -- _chunk_diff --


class TestChunkDiff:
    def test_small_diff_single_chunk(self) -> None:
        diff = "diff --git a/f.py b/f.py\n+line\n"
        assert _chunk_diff(diff) == [diff]

    def test_exactly_at_chunk_size(self) -> None:
        diff = "x" * 100
        chunks = _chunk_diff(diff, chunk_size=100)
        assert len(chunks) == 1
        assert chunks[0] == diff

    def test_empty_string(self) -> None:
        assert _chunk_diff("") == [""]

    def test_splits_at_file_boundary(self) -> None:
        part1 = "diff --git a/a.py b/a.py\n" + "+" * 50 + "\n"
        part2 = "diff --git a/b.py b/b.py\n" + "+" * 50 + "\n"
        diff = part1 + part2
        chunks = _chunk_diff(diff, chunk_size=len(part1))
        assert len(chunks) == 2

    def test_no_file_boundary_stays_single(self) -> None:
        diff = "+" * 200 + "\n"
        chunks = _chunk_diff(diff, chunk_size=50)
        assert len(chunks) == 1

    def test_boundary_before_chunk_size_stays_single(self) -> None:
        part1 = "diff --git a/a.py b/a.py\n" + "+" * 30 + "\n"
        part2 = "diff --git a/b.py b/b.py\n" + "+" * 30 + "\n"
        diff = part1 + part2
        chunks = _chunk_diff(diff, chunk_size=len(diff) + 1)
        assert len(chunks) == 1

    def test_boundary_splits_when_accumulated_exceeds_limit(self) -> None:
        part1 = "diff --git a/a.py b/a.py\n" + "+" * 60 + "\n"
        part2 = "diff --git a/b.py b/b.py\n" + "+" * 60 + "\n"
        diff = part1 + part2
        chunks = _chunk_diff(diff, chunk_size=len(part1))
        assert len(chunks) == 2
        assert chunks[0] == part1
        assert chunks[1] == part2

    def test_boundary_under_limit_no_split(self) -> None:
        # part1 (60 chars) < chunk_size (100), boundary exists but accumulated
        # size hasn't reached the limit yet, so no split occurs even though
        # total (150) exceeds chunk_size.
        part1 = "diff --git a/a.py b/a.py\n" + "+" * 34 + "\n"  # 60 chars
        part2 = "diff --git a/b.py b/b.py\n" + "+" * 64 + "\n"  # 90 chars
        diff = part1 + part2
        assert len(part1) == 60
        assert len(part2) == 90
        chunks = _chunk_diff(diff, chunk_size=100)
        assert len(chunks) == 1
        assert chunks[0] == diff


# -- _severity_label --


class TestSeverityLabel:
    def test_critical(self) -> None:
        assert _severity_label(7) == "CRITICAL"
        assert _severity_label(10) == "CRITICAL"

    def test_high(self) -> None:
        assert _severity_label(5) == "HIGH"
        assert _severity_label(6) == "HIGH"

    def test_medium(self) -> None:
        assert _severity_label(3) == "MEDIUM"
        assert _severity_label(4) == "MEDIUM"

    def test_low(self) -> None:
        assert _severity_label(1) == "LOW"
        assert _severity_label(2) == "LOW"


# -- _verdict_label --


class TestVerdictLabel:
    def test_no_findings_approve(self) -> None:
        assert _verdict_label([]) == "APPROVE"

    def test_low_severity_revise(self) -> None:
        assert _verdict_label([_finding(severity=3)]) == "REVISE"

    def test_critical_severity_block(self) -> None:
        assert _verdict_label([_finding(severity=7)]) == "BLOCK"

    def test_mixed_severities_uses_max(self) -> None:
        assert _verdict_label([_finding(severity=2), _finding(severity=8)]) == "BLOCK"


# -- _sova_verdict_label_name --


class TestSovaVerdictLabelName:
    def test_no_findings_approved(self) -> None:
        assert _sova_verdict_label_name([]) == "sova:approved"

    def test_severity_5_revise(self) -> None:
        assert _sova_verdict_label_name([_finding(severity=5)]) == "sova:revise"

    def test_severity_7_block(self) -> None:
        assert _sova_verdict_label_name([_finding(severity=7)]) == "sova:block"


# -- _format_findings_body --


class TestFormatFindingsBody:
    def test_no_findings_approve_marker(self) -> None:
        body = _format_findings_body([], "")
        assert body.startswith("<!-- sova-review: approve -->")
        assert "No issues found" in body

    def test_findings_sorted_by_severity_desc(self) -> None:
        f_low = _finding(severity=2, file="low.py")
        f_high = _finding(severity=9, file="high.py")
        body = _format_findings_body([f_low, f_high], "sum")
        high_pos = body.index("high.py")
        low_pos = body.index("low.py")
        assert high_pos < low_pos

    def test_summary_included(self) -> None:
        body = _format_findings_body([], "This is the summary")
        assert "This is the summary" in body

    def test_finding_with_suggestion(self) -> None:
        f = _finding(severity=5, suggestion="use X instead")
        body = _format_findings_body([f], "")
        assert "Fix: use X instead" in body

    def test_finding_without_line(self) -> None:
        f = _finding(severity=5, line=None, file="noln.py")
        body = _format_findings_body([f], "")
        assert "`noln.py`" in body
        assert "noln.py:" not in body.replace("`noln.py`:", "")

    def test_finding_with_line(self) -> None:
        f = _finding(severity=5, line=42, file="ln.py")
        body = _format_findings_body([f], "")
        assert "`ln.py:42`" in body


# -- _format_findings_comment --


class TestFormatFindingsComment:
    def test_delegates_to_format_findings_body(self) -> None:
        findings = [_finding(severity=4)]
        assert _format_findings_comment(findings, "s") == _format_findings_body(findings, "s")


# -- _format_review_body --


class TestFormatReviewBody:
    def test_delegates_to_format_findings_body(self) -> None:
        findings = [_finding(severity=6)]
        assert _format_review_body(findings, "s") == _format_findings_body(findings, "s")


# -- _format_inline_comment --


class TestFormatInlineComment:
    def test_basic(self) -> None:
        f = _finding(severity=7, category="security", description="SQL injection")
        comment = _format_inline_comment(f)
        assert "CRITICAL" in comment
        assert "security" in comment
        assert "SQL injection" in comment

    def test_with_suggestion(self) -> None:
        f = _finding(severity=3, suggestion="use parameterized query")
        comment = _format_inline_comment(f)
        assert "**Suggestion**: use parameterized query" in comment

    def test_without_suggestion(self) -> None:
        f = _finding(severity=3, suggestion="")
        comment = _format_inline_comment(f)
        assert "Suggestion" not in comment


# -- _build_review_comments --


class TestBuildReviewComments:
    def test_finding_in_diff_goes_inline(self) -> None:
        f = _finding(line=10, file="a.py")
        diff_lines = {"a.py": {10, 20}}
        inline, body_only = _build_review_comments([f], diff_lines)
        assert len(inline) == 1
        assert inline[0]["path"] == "a.py"
        assert inline[0]["line"] == 10
        assert inline[0]["side"] == "RIGHT"
        assert body_only == []

    def test_finding_not_in_diff_goes_body(self) -> None:
        f = _finding(line=99, file="a.py")
        diff_lines = {"a.py": {10, 20}}
        inline, body_only = _build_review_comments([f], diff_lines)
        assert inline == []
        assert body_only == [f]

    def test_finding_with_none_line_goes_body(self) -> None:
        f = _finding(line=None, file="a.py")
        diff_lines = {"a.py": {10}}
        inline, body_only = _build_review_comments([f], diff_lines)
        assert inline == []
        assert body_only == [f]

    def test_finding_file_not_in_diff_lines(self) -> None:
        f = _finding(line=5, file="missing.py")
        diff_lines = {"other.py": {5}}
        inline, body_only = _build_review_comments([f], diff_lines)
        assert inline == []
        assert body_only == [f]

    def test_empty_findings(self) -> None:
        inline, body_only = _build_review_comments([], {"a.py": {1}})
        assert inline == []
        assert body_only == []


# -- _format_addressed_findings --


class TestFormatAddressedFindings:
    def test_none_returns_empty(self) -> None:
        assert _format_addressed_findings(None) == ""

    def test_empty_list_returns_empty(self) -> None:
        assert _format_addressed_findings([]) == ""

    def test_single_finding(self) -> None:
        findings = [{"source": "ruff", "severity": "W", "file_path": "a.py", "message": "unused import"}]
        result = _format_addressed_findings(findings)
        assert "ruff" in result
        assert "a.py" in result
        assert "unused import" in result

    def test_groups_by_source(self) -> None:
        findings = [
            {"source": "ruff", "file_path": "a.py", "message": "m1"},
            {"source": "mypy", "file_path": "b.py", "message": "m2"},
            {"source": "ruff", "file_path": "c.py", "message": "m3"},
        ]
        result = _format_addressed_findings(findings)
        assert "ruff (2 findings)" in result
        assert "mypy (1 finding)" in result

    def test_tool_id_tag(self) -> None:
        findings = [{"source": "ruff", "tool_id": "F401", "file_path": "a.py", "message": "unused"}]
        result = _format_addressed_findings(findings)
        assert "[F401]" in result


# -- _build_review_prompt --


class TestBuildReviewPrompt:
    def test_includes_task_title(self) -> None:
        prompt = _build_review_prompt(_task(title="Fix login"), "diff content", ["auth.py"])
        assert "Fix login" in prompt

    def test_includes_diff(self) -> None:
        prompt = _build_review_prompt(_task(), "my diff here", ["f.py"])
        assert "my diff here" in prompt

    def test_includes_file_list(self) -> None:
        prompt = _build_review_prompt(_task(), "diff", ["a.py", "b.py"])
        assert "- a.py" in prompt
        assert "- b.py" in prompt

    def test_without_spec_includes_body(self) -> None:
        prompt = _build_review_prompt(_task(body="Issue details"), "diff", ["f.py"])
        assert "Issue details" in prompt

    def test_with_spec_omits_body(self) -> None:
        spec = {"Solution": "Do X"}
        prompt = _build_review_prompt(_task(body="Issue details"), "diff", ["f.py"], spec_sections=spec)
        assert "Issue details" not in prompt
        assert "Do X" in prompt

    def test_with_spec_adds_spec_alignment_category(self) -> None:
        spec = {"Solution": "Do X"}
        prompt = _build_review_prompt(_task(), "diff", ["f.py"], spec_sections=spec)
        assert "spec_alignment" in prompt

    def test_without_spec_no_spec_alignment(self) -> None:
        prompt = _build_review_prompt(_task(), "diff", ["f.py"])
        assert "spec_alignment" not in prompt

    def test_addressed_findings_included(self) -> None:
        addressed = [{"source": "ruff", "file_path": "a.py", "message": "unused"}]
        prompt = _build_review_prompt(_task(), "diff", ["f.py"], addressed_findings=addressed)
        assert "Already Addressed by Static Tools" in prompt

    def test_empty_body_no_description(self) -> None:
        prompt = _build_review_prompt(_task(body=""), "diff", ["f.py"])
        assert "**Description**" not in prompt


# -- _compact_spec_ref --


class TestCompactSpecRef:
    def test_none_returns_none(self) -> None:
        assert _compact_spec_ref(None) is None

    def test_empty_dict_returns_none(self) -> None:
        assert _compact_spec_ref({}) is None

    def test_short_content_unchanged(self) -> None:
        sections = {"Solution": "short"}
        result = _compact_spec_ref(sections)
        assert result == {"Solution": "short"}

    def test_exactly_at_limit_not_truncated(self) -> None:
        content = "x" * 300
        result = _compact_spec_ref({"Solution": content})
        assert result is not None
        assert result["Solution"] == content

    def test_over_limit_truncated(self) -> None:
        content = "x" * 301
        result = _compact_spec_ref({"Solution": content})
        assert result is not None
        assert result["Solution"] != content
        suffix = "... (see full spec in chunk 1)"
        assert result["Solution"] == "x" * 300 + suffix
        assert len(result["Solution"]) == 300 + len(suffix)


# -- _extract_spec_sections --


class TestExtractSpecSections:
    def test_extracts_known_sections(self) -> None:
        content = "## Solution\nDo the thing\n\n## Edge Cases\nHandle nulls\n"
        result = _extract_spec_sections(content)
        assert "Solution" in result
        assert "Edge Cases" in result

    def test_ignores_unknown_sections(self) -> None:
        content = "## Random Heading\nStuff\n"
        result = _extract_spec_sections(content)
        assert result == {}

    def test_empty_content(self) -> None:
        assert _extract_spec_sections("") == {}
