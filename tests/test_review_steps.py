"""Address-review, external-review and review-parsing steps.

Split from tests/test_core.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.core_helpers import _make_ctx

# Every test in this module runs against the shared per-worker in-memory DB
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


class TestAddressReviewStep:
    async def test_no_findings_returns_success(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(tmp_path, pr_number=42)
        ctx.project_dir = tmp_path / "nonexistent"
        step = AddressReviewStep()

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123")
            result = await step.execute(ctx)

        assert result.success
        assert "No review findings" in result.summary


class TestParseReviewBody:
    """Tests for _parse_review_body, which parses the ### Findings / ### Advisory

    markdown format emitted by sova.roles._review_format.format_review_body().
    """

    def test_structured_finding(self) -> None:
        from sova.core.steps.address_review import _parse_review_body
        from sova.roles._review_format import format_review_body

        body = format_review_body(
            [
                {
                    "file": "sova/core/steps/commit.py",
                    "line": 42,
                    "severity": 9,
                    "category": "correctness",
                    "description": "The variable could be None when accessed.",
                    "suggestion": "Add an explicit None check before use.",
                }
            ],
            "summary",
        )
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["severity"] == 9
        assert findings[0]["category"] == "correctness"
        assert findings[0]["file"] == "sova/core/steps/commit.py"
        assert findings[0]["line"] == 42
        assert "None" in findings[0]["description"]
        assert "check" in findings[0]["suggestion"]

    def test_critical_severity(self) -> None:
        from sova.core.steps.address_review import _parse_review_body
        from sova.roles._review_format import format_review_body

        body = format_review_body(
            [{"file": "app.py", "line": 10, "severity": 10, "category": "security", "description": "SQL injection"}],
            "summary",
        )
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["severity"] == 10

    def test_unstructured_fallback(self) -> None:
        from sova.core.steps.address_review import _parse_review_body

        body = "This code has several issues that need addressing. " * 3
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["category"] == "review"
        assert findings[0]["severity"] == 7

    def test_short_body_returns_empty(self) -> None:
        from sova.core.steps.address_review import _parse_review_body

        findings = _parse_review_body("LGTM")
        assert findings == []

    def test_empty_body_returns_empty(self) -> None:
        from sova.core.steps.address_review import _parse_review_body

        findings = _parse_review_body("")
        assert findings == []

    def test_no_findings_structured_body_returns_empty_not_fallback(self) -> None:
        """A body with a ### Findings section but zero findings parses empty, not the single-finding fallback."""
        from sova.core.steps.address_review import _parse_review_body
        from sova.roles._review_format import format_review_body

        body = format_review_body([], "Review of changes.")
        assert "No issues found after thorough review." in body
        findings = _parse_review_body(body)
        assert findings == []

    def test_advisory_only_findings_still_parsed(self) -> None:
        """A body with only advisory findings (no blocking ones) must still parse them."""
        from sova.core.steps.address_review import _parse_review_body
        from sova.roles._review_format import format_review_body

        body = format_review_body(
            [{"file": "a.py", "line": 1, "severity": 1, "category": "style", "description": "nitpick"}],
            "summary",
        )
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["description"] == "nitpick"
        assert findings[0]["severity"] == 1

    def test_colon_in_windows_style_file_path_does_not_break_location(self) -> None:
        from sova.core.steps.address_review import _parse_review_body
        from sova.roles._review_format import format_review_body

        body = format_review_body(
            [
                {
                    "file": r"C:\repo\foo.py",
                    "line": None,
                    "severity": 9,
                    "category": "correctness",
                    "description": "Something is wrong.",
                }
            ],
            "summary",
        )
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["file"] == r"C:\repo\foo.py"
        assert findings[0]["line"] is None

    def test_multiple_findings(self) -> None:
        from sova.core.steps.address_review import _parse_review_body
        from sova.roles._review_format import format_review_body

        body = format_review_body(
            [
                {"file": "a.py", "line": 1, "severity": 9, "category": "correctness", "description": "First issue"},
                {"file": "b.py", "line": 2, "severity": 1, "category": "style", "description": "Second issue"},
            ],
            "summary",
        )
        findings = _parse_review_body(body)
        assert len(findings) == 2
        severities = {f["severity"] for f in findings}
        assert severities == {9, 1}

    def test_multi_word_category(self) -> None:
        from sova.core.steps.address_review import _parse_review_body
        from sova.roles._review_format import format_review_body

        body = format_review_body(
            [
                {
                    "file": "sova/core/context.py",
                    "line": 55,
                    "severity": 9,
                    "category": "type safety",
                    "description": "Missing type annotation",
                }
            ],
            "summary",
        )
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["category"] == "type safety"
        assert findings[0]["severity"] == 9

    def test_collapsed_inline_comment_reference_still_parses(self) -> None:
        """A one-line 'see inline comment above' entry still yields a finding dict."""
        from sova.core.steps.address_review import _parse_review_body
        from sova.roles._review_format import format_review_body

        body = format_review_body(
            [{"file": "a.py", "line": 10, "severity": 9, "category": "bug", "description": "crash"}],
            "summary",
            inline_comment_keys={("a.py", 10)},
        )
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["file"] == "a.py"
        assert findings[0]["line"] == 10
        assert findings[0]["severity"] == 9

    def test_line_range_location_preserves_file_path(self) -> None:
        """A legacy 'Location: foo.py:10-15' keeps the file path and leaves the line unknown."""
        from sova.core.steps.address_review import _parse_review_body

        # The legacy format's separator is a literal dash pair, assembled here
        # because invariants/no-double-dash.sh rejects it as added prose.
        sep = "-" * 2
        body = f"[HIGH] Correctness {sep} Range location\nLocation: foo.py:10-15\nProblem: Something is wrong."
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["file"] == "foo.py"
        assert findings[0]["line"] is None

    def test_human_findings_heading_is_not_silently_dropped(self) -> None:
        """A non-SOVA body using a '### Findings' heading falls back instead of parsing to zero."""
        from sova.core.steps.address_review import _parse_review_body

        body = (
            "### Findings\n\n"
            "* The retry loop never resets its counter, so a transient failure "
            "permanently exhausts the budget.\n"
            "* The new config key is not registered in settings_meta.py.\n"
        )
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert "retry loop never resets" in findings[0]["description"]

    def test_sova_body_with_no_findings_is_trusted_as_empty(self) -> None:
        """The marker proves SOVA wrote the body, so an empty Findings section means empty."""
        from sova.core.steps.address_review import _parse_review_body
        from sova.roles._review_format import format_review_body

        body = format_review_body([], "A long enough summary to clear the 50-character fallback threshold.")
        assert "<!-- sova-review:" in body
        assert _parse_review_body(body) == []


# ---------------------------------------------------------------------------
def _gh_reviews_json(*reviews: dict) -> str:
    """Build a JSON string mimicking ``gh api repos/.../pulls/N/reviews``."""
    return json.dumps(reviews)


class TestHydrateCollapsedFindings:
    """Tests for recovering collapsed finding text from inline PR review comments."""

    def test_parse_inline_comment_body_splits_sova_shape(self) -> None:
        from sova.core.steps.address_review import _parse_inline_comment_body

        description, suggestion = _parse_inline_comment_body(
            "**[CRITICAL] bug**: The counter is never reset.\n\n**Suggestion**: Reset it in the finally block."
        )
        assert description == "The counter is never reset."
        assert suggestion == "Reset it in the finally block."

    def test_parse_inline_comment_body_keeps_human_text_whole(self) -> None:
        from sova.core.steps.address_review import _parse_inline_comment_body

        description, suggestion = _parse_inline_comment_body("This looks wrong to me, can you double check?")
        assert description == "This looks wrong to me, can you double check?"
        assert suggestion == ""

    async def test_collapsed_finding_is_hydrated_from_inline_comment(self, tmp_path: Path) -> None:
        """A 'see inline comment above' body entry gets its real text back from the inline comment."""
        from sova.core.steps.address_review import _load_findings_from_github_reviews
        from sova.roles._review_format import format_review_body

        ctx = _make_ctx(tmp_path, pr_number=11)
        body = format_review_body(
            [{"file": "a.py", "line": 7, "severity": 9, "category": "bug", "description": "crash on empty input"}],
            "summary",
            inline_comment_keys={("a.py", 7)},
        )
        reviews_json = _gh_reviews_json({"user": {"login": "dev", "type": "User"}, "state": "COMMENTED", "body": body})
        comments_json = json.dumps(
            [
                {
                    "user": {"login": "dev", "type": "User"},
                    "path": "a.py",
                    "line": 7,
                    "body": "**[CRITICAL] bug**: crash on empty input\n\n**Suggestion**: guard the empty case",
                },
                {
                    "user": {"login": "coderabbitai", "type": "Bot"},
                    "path": "a.py",
                    "line": 7,
                    "body": "bot noise that must not win",
                },
            ]
        )
        with patch(
            "sova.core.steps.address_review.run",
            new_callable=AsyncMock,
            side_effect=[
                MagicMock(success=True, stdout=reviews_json),
                MagicMock(success=True, stdout=comments_json),
            ],
        ):
            findings = await _load_findings_from_github_reviews(ctx)

        assert len(findings) == 1
        assert findings[0]["description"] == "crash on empty input"
        assert findings[0]["suggestion"] == "guard the empty case"

    async def test_unhydratable_stub_keeps_its_reference(self, tmp_path: Path) -> None:
        """A stub with no matching inline comment keeps the stub rather than becoming empty."""
        from sova.core.steps.address_review import _load_findings_from_github_reviews
        from sova.roles._review_format import format_review_body
        from sova.utils.review_markers import INLINE_COMMENT_STUB

        ctx = _make_ctx(tmp_path, pr_number=12)
        body = format_review_body(
            [{"file": "a.py", "line": 7, "severity": 9, "category": "bug", "description": "crash"}],
            "summary",
            inline_comment_keys={("a.py", 7)},
        )
        reviews_json = _gh_reviews_json({"user": {"login": "dev", "type": "User"}, "state": "COMMENTED", "body": body})
        with patch(
            "sova.core.steps.address_review.run",
            new_callable=AsyncMock,
            side_effect=[
                MagicMock(success=True, stdout=reviews_json),
                MagicMock(success=False, stdout=""),
            ],
        ):
            findings = await _load_findings_from_github_reviews(ctx)

        assert len(findings) == 1
        assert findings[0]["description"] == INLINE_COMMENT_STUB

    async def test_no_stub_means_no_inline_comment_fetch(self, tmp_path: Path) -> None:
        """An uncollapsed body never spends a second API call on inline comments."""
        from sova.core.steps.address_review import _load_findings_from_github_reviews
        from sova.roles._review_format import format_review_body

        ctx = _make_ctx(tmp_path, pr_number=13)
        body = format_review_body(
            [{"file": "a.py", "line": 7, "severity": 9, "category": "bug", "description": "crash"}],
            "summary",
        )
        reviews_json = _gh_reviews_json({"user": {"login": "dev", "type": "User"}, "state": "COMMENTED", "body": body})
        mock_run = AsyncMock(return_value=MagicMock(success=True, stdout=reviews_json))
        with patch("sova.core.steps.address_review.run", mock_run):
            findings = await _load_findings_from_github_reviews(ctx)

        assert mock_run.await_count == 1
        assert findings[0]["description"] == "crash"

    async def test_hydrates_from_concatenated_paginated_comments(self, tmp_path: Path) -> None:
        """`gh api --paginate` emits one JSON array per page; a >30-comment PR concatenates them."""
        from sova.core.steps.address_review import _load_findings_from_github_reviews
        from sova.roles._review_format import format_review_body

        ctx = _make_ctx(tmp_path, pr_number=14)
        body = format_review_body(
            [{"file": "a.py", "line": 7, "severity": 9, "category": "bug", "description": "crash"}],
            "summary",
            inline_comment_keys={("a.py", 7)},
        )
        reviews_json = _gh_reviews_json({"user": {"login": "dev", "type": "User"}, "state": "COMMENTED", "body": body})
        page_one = json.dumps(
            [{"user": {"login": "noise", "type": "User"}, "path": "b.py", "line": 1, "body": "unrelated"}]
        )
        page_two = json.dumps(
            [{"user": {"login": "dev", "type": "User"}, "path": "a.py", "line": 7, "body": "**[CRITICAL] bug**: crash"}]
        )
        with patch(
            "sova.core.steps.address_review.run",
            new_callable=AsyncMock,
            side_effect=[
                MagicMock(success=True, stdout=reviews_json),
                MagicMock(success=True, stdout=page_one + page_two),
            ],
        ):
            findings = await _load_findings_from_github_reviews(ctx)

        assert findings[0]["description"] == "crash"

    def test_parse_paginated_gh_json_keeps_pages_before_truncation(self) -> None:
        """A truncated trailing page must not discard the earlier, fully-decoded pages."""
        from sova.core.steps.address_review import _parse_paginated_gh_json

        page_one = json.dumps([{"id": 1}])
        truncated_page_two = '[{"id": 2}'

        result = _parse_paginated_gh_json(page_one + truncated_page_two)

        assert result == [{"id": 1}]

    async def test_hydrates_despite_null_user_comment(self, tmp_path: Path) -> None:
        """A `"user": null` comment (deleted/ghost author) must not abort hydration for the whole PR."""
        from sova.core.steps.address_review import _load_findings_from_github_reviews
        from sova.roles._review_format import format_review_body

        ctx = _make_ctx(tmp_path, pr_number=15)
        body = format_review_body(
            [{"file": "a.py", "line": 7, "severity": 9, "category": "bug", "description": "crash"}],
            "summary",
            inline_comment_keys={("a.py", 7)},
        )
        reviews_json = _gh_reviews_json({"user": {"login": "dev", "type": "User"}, "state": "COMMENTED", "body": body})
        comments_json = json.dumps(
            [
                {"user": None, "path": "b.py", "line": 3, "body": "ghost author comment"},
                {
                    "user": {"login": "dev", "type": "User"},
                    "path": "a.py",
                    "line": 7,
                    "body": "**[CRITICAL] bug**: crash",
                },
            ]
        )
        with patch(
            "sova.core.steps.address_review.run",
            new_callable=AsyncMock,
            side_effect=[
                MagicMock(success=True, stdout=reviews_json),
                MagicMock(success=True, stdout=comments_json),
            ],
        ):
            findings = await _load_findings_from_github_reviews(ctx)

        assert findings[0]["description"] == "crash"

    async def test_sova_shaped_comment_wins_over_human_note_at_same_location(self, tmp_path: Path) -> None:
        """A human's top-level note at the same (path, line) must not outrank SOVA's own comment."""
        from sova.core.steps.address_review import _load_findings_from_github_reviews
        from sova.roles._review_format import format_review_body

        ctx = _make_ctx(tmp_path, pr_number=16)
        body = format_review_body(
            [{"file": "a.py", "line": 7, "severity": 9, "category": "bug", "description": "crash"}],
            "summary",
            inline_comment_keys={("a.py", 7)},
        )
        reviews_json = _gh_reviews_json({"user": {"login": "dev", "type": "User"}, "state": "COMMENTED", "body": body})
        comments_json = json.dumps(
            [
                {"user": {"login": "human", "type": "User"}, "path": "a.py", "line": 7, "body": "unrelated note"},
                {
                    "user": {"login": "dev", "type": "User"},
                    "path": "a.py",
                    "line": 7,
                    "body": "**[CRITICAL] bug**: crash",
                },
            ]
        )
        with patch(
            "sova.core.steps.address_review.run",
            new_callable=AsyncMock,
            side_effect=[
                MagicMock(success=True, stdout=reviews_json),
                MagicMock(success=True, stdout=comments_json),
            ],
        ):
            findings = await _load_findings_from_github_reviews(ctx)

        assert findings[0]["description"] == "crash"

    async def test_two_matching_comments_at_same_location_stay_ambiguous(self, tmp_path: Path) -> None:
        """Two distinct findings landing on the same (path, line) keep the stub rather than guess."""
        from sova.core.steps.address_review import _load_findings_from_github_reviews
        from sova.roles._review_format import format_review_body
        from sova.utils.review_markers import INLINE_COMMENT_STUB

        ctx = _make_ctx(tmp_path, pr_number=17)
        body = format_review_body(
            [{"file": "a.py", "line": 7, "severity": 9, "category": "bug", "description": "crash"}],
            "summary",
            inline_comment_keys={("a.py", 7)},
        )
        reviews_json = _gh_reviews_json({"user": {"login": "dev", "type": "User"}, "state": "COMMENTED", "body": body})
        comments_json = json.dumps(
            [
                {
                    "user": {"login": "dev", "type": "User"},
                    "path": "a.py",
                    "line": 7,
                    "body": "**[CRITICAL] bug**: crash",
                },
                {
                    "user": {"login": "dev", "type": "User"},
                    "path": "a.py",
                    "line": 7,
                    "body": "**[HIGH] security**: leak",
                },
            ]
        )
        with patch(
            "sova.core.steps.address_review.run",
            new_callable=AsyncMock,
            side_effect=[
                MagicMock(success=True, stdout=reviews_json),
                MagicMock(success=True, stdout=comments_json),
            ],
        ):
            findings = await _load_findings_from_github_reviews(ctx)

        assert findings[0]["description"] == INLINE_COMMENT_STUB


class TestLoadFindingsFromGithubReviews:
    """Tests for _load_findings_from_github_reviews (calls gh api directly)."""

    def _patch_run(self, gh_json: str):
        return patch(
            "sova.core.steps.address_review.run",
            new_callable=AsyncMock,
            return_value=MagicMock(success=True, stdout=gh_json),
        )

    async def test_filters_bot_reviews(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import _load_findings_from_github_reviews
        from sova.roles._review_format import format_review_body

        ctx = _make_ctx(tmp_path, pr_number=10)
        real_body = format_review_body(
            [{"file": "a.py", "line": 1, "severity": 9, "category": "bug", "description": "real issue"}],
            "summary",
        )
        gh_json = _gh_reviews_json(
            {"user": {"login": "bot", "type": "Bot"}, "state": "COMMENTED", "body": real_body},
            {
                "user": {"login": "dev", "type": "User"},
                "state": "COMMENTED",
                "body": real_body,
            },
        )
        with self._patch_run(gh_json):
            findings = await _load_findings_from_github_reviews(ctx)
        assert len(findings) == 1
        assert findings[0]["description"] == "real issue"

    async def test_filters_dismissed_reviews(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import _load_findings_from_github_reviews
        from sova.roles._review_format import format_review_body

        ctx = _make_ctx(tmp_path, pr_number=10)
        body = format_review_body(
            [{"file": "a.py", "line": 1, "severity": 9, "category": "bug", "description": "old issue"}],
            "summary",
        )
        gh_json = _gh_reviews_json(
            {"user": {"login": "dev", "type": "User"}, "state": "DISMISSED", "body": body},
        )
        with self._patch_run(gh_json):
            findings = await _load_findings_from_github_reviews(ctx)
        assert findings == []

    async def test_filters_empty_body(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import _load_findings_from_github_reviews

        ctx = _make_ctx(tmp_path, pr_number=10)
        gh_json = _gh_reviews_json(
            {"user": {"login": "dev", "type": "User"}, "state": "COMMENTED", "body": "  "},
        )
        with self._patch_run(gh_json):
            findings = await _load_findings_from_github_reviews(ctx)
        assert findings == []

    async def test_gh_api_failure_returns_empty(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import _load_findings_from_github_reviews

        ctx = _make_ctx(tmp_path, pr_number=10)
        mock_result = MagicMock(success=False, stdout="", stderr="API error")
        with patch(
            "sova.core.steps.address_review.run",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            findings = await _load_findings_from_github_reviews(ctx)
        assert findings == []

    async def test_freeform_human_review_falls_back_to_single_finding(self, tmp_path: Path) -> None:
        """A human reviewer's free-text comment (no ### Findings section) is not
        structured, so it degrades to the single-finding fallback rather than
        being silently dropped."""
        from sova.core.steps.address_review import _load_findings_from_github_reviews

        ctx = _make_ctx(tmp_path, pr_number=10)
        body = (
            "This has an XSS vulnerability in app.py:42 where unsanitized input "
            "reaches the response. Please use escape() before rendering it."
        )
        gh_json = _gh_reviews_json(
            {"user": {"login": "reviewer", "type": "User"}, "state": "COMMENTED", "body": body},
        )
        with self._patch_run(gh_json):
            findings = await _load_findings_from_github_reviews(ctx)
        assert len(findings) == 1
        assert findings[0]["severity"] == 7
        assert findings[0]["category"] == "review"
        assert "XSS" in findings[0]["description"]


# ---------------------------------------------------------------------------
# AddressReviewStep: findings loading and execute
# ---------------------------------------------------------------------------


class TestAddressReviewStepExecute:
    async def test_execute_loads_findings_from_file(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import AddressReviewStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=42, worktree_dir=tmp_path / "worktree")
        step = AddressReviewStep()

        findings = [
            {"file": "foo.py", "line": 10, "severity": 7, "category": "bug", "description": "Null check"},
        ]
        mock_handoff = MagicMock()
        mock_handoff.details = {"findings": findings}

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.address_review.read_handoff_file", return_value=mock_handoff),
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch("sova.core.steps.address_review.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123")
            mock_invoke.return_value = LLMResult(
                text="Fixed",
                model="sonnet",
                cost_usd=Decimal("0.05"),
            )
            result = await step.execute(ctx)

        assert result.success
        assert "1 review findings" in result.summary
        assert ctx.cost_usd == Decimal("0.05")
        prompt = mock_invoke.call_args[0][1]
        assert "Null check" in prompt
        assert "foo.py:10" in prompt
        assert mock_invoke.call_args.kwargs["task_type"] == "address_review"

    async def test_execute_includes_coderabbit_findings(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import AddressReviewStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=42, worktree_dir=tmp_path / "worktree")
        step = AddressReviewStep()

        sova_findings = [
            {"file": "foo.py", "severity": 7, "category": "bug", "description": "SOVA finding"},
        ]
        cr_findings = [
            {
                "file": "bar.py",
                "line": 5,
                "severity": 6,
                "category": "external-review",
                "description": "CodeRabbit finding",
                "suggestion": "",
                "source": "coderabbit",
            },
        ]
        mock_handoff = MagicMock()
        mock_handoff.details = {"findings": sova_findings}

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.address_review.read_handoff_file", return_value=mock_handoff),
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=(cr_findings, ["thread-1"]),
            ),
            patch("sova.core.steps.address_review.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123")
            mock_invoke.return_value = LLMResult(text="Fixed", model="sonnet", cost_usd=Decimal("0.05"))
            result = await step.execute(ctx)

        assert result.success
        assert "2 review findings" in result.summary
        prompt = mock_invoke.call_args[0][1]
        assert "SOVA finding" in prompt
        assert "CodeRabbit finding" in prompt
        assert "coderabbit" in prompt

    async def test_execute_captures_head_before_llm(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import AddressReviewStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=42, worktree_dir=tmp_path / "worktree")
        step = AddressReviewStep()

        findings = [{"file": "x.py", "severity": 5, "category": "bug", "description": "Issue"}]
        mock_handoff = MagicMock()
        mock_handoff.details = {"findings": findings}

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.address_review.read_handoff_file", return_value=mock_handoff),
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch("sova.core.steps.address_review.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123\n")
            mock_invoke.return_value = LLMResult(text="Fixed", model="sonnet", cost_usd=Decimal("0.01"))
            await step.execute(ctx)

        assert step._head_before_llm == "abc123"

    async def test_execute_falls_back_to_db(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import AddressReviewStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=42, worktree_dir=tmp_path / "worktree")
        ctx.resume_run_id = 99
        step = AddressReviewStep()

        db_findings = [
            {"file": "bar.py", "severity": 5, "category": "style", "description": "Formatting"},
        ]

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.address_review.read_handoff_file", return_value=None),
            patch("sova.core.steps.address_review.read_handoff", new_callable=AsyncMock) as mock_db,
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch("sova.core.steps.address_review.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123")
            mock_db_handoff = MagicMock()
            mock_db_handoff.pending_findings = db_findings
            mock_db.return_value = mock_db_handoff
            mock_invoke.return_value = LLMResult(
                text="Fixed",
                model="sonnet",
                cost_usd=Decimal("0.04"),
            )
            result = await step.execute(ctx)

        assert result.success
        assert "1 review findings" in result.summary
        prompt = mock_invoke.call_args[0][1]
        assert "Formatting" in prompt

    async def test_execute_handles_llm_failure(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(tmp_path, pr_number=42, worktree_dir=tmp_path / "worktree")
        step = AddressReviewStep()

        findings = [{"file": "x.py", "severity": 5, "category": "bug", "description": "Issue"}]
        mock_handoff = MagicMock()
        mock_handoff.details = {"findings": findings}

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.address_review.read_handoff_file", return_value=mock_handoff),
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch("sova.core.steps.address_review.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123")
            mock_invoke.side_effect = RuntimeError("LLM crashed")
            result = await step.execute(ctx)

        assert not result.success
        assert "LLM crashed" in result.error

    async def test_validate_output_passes_with_staged_changes(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = AddressReviewStep()

        with patch("sova.core.steps.address_review.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # unstaged diff
                MagicMock(success=True, stdout=" bar.py | 3 +++\n"),  # staged diff
                MagicMock(success=True, stdout="abc123"),  # rev-parse HEAD
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_passes_when_head_moved(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = AddressReviewStep()
        step._head_before_llm = "abc123"

        with patch("sova.core.steps.address_review.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # unstaged diff
                MagicMock(success=True, stdout=""),  # staged diff
                MagicMock(success=True, stdout="def456"),  # rev-parse HEAD (different)
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_passes_when_findings_already_fixed(self, tmp_path: Path) -> None:
        """When LLM produces no changes but branch has prior commits, findings are already fixed."""
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = AddressReviewStep()
        step._head_before_llm = "abc123"

        with patch("sova.core.steps.address_review.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # unstaged diff
                MagicMock(success=True, stdout=""),  # staged diff
                MagicMock(success=True, stdout="abc123"),  # rev-parse HEAD (same)
                MagicMock(success=True, stdout="abc123 fix: prior work\n"),  # git log base..HEAD
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_fails_with_no_changes_and_no_prior_commits(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = AddressReviewStep()
        step._head_before_llm = "abc123"

        with patch("sova.core.steps.address_review.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # unstaged diff
                MagicMock(success=True, stdout=""),  # staged diff
                MagicMock(success=True, stdout="abc123"),  # rev-parse HEAD (same)
                MagicMock(success=True, stdout=""),  # git log base..HEAD (empty)
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "No changes" in gate.reason


# ---------------------------------------------------------------------------
# AddressReviewStep helper functions
# ---------------------------------------------------------------------------


class TestAddressReviewHelpers:
    def test_format_findings_prompt(self) -> None:
        from sova.core.steps.address_review import _format_findings_prompt

        findings = [
            {
                "file": "a.py",
                "line": 5,
                "severity": 7,
                "category": "bug",
                "description": "Null ref",
                "suggestion": "Add check",
            },
            {"file": "b.py", "severity": 3, "category": "style", "description": "Whitespace"},
        ]
        prompt = _format_findings_prompt(findings)
        assert "a.py:5" in prompt
        assert "Null ref" in prompt
        assert "Add check" in prompt
        assert "b.py" in prompt
        assert "Whitespace" in prompt
        assert "1." in prompt
        assert "2." in prompt

    def test_load_review_findings_from_handoff(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import _load_review_findings

        mock_handoff = MagicMock()
        mock_handoff.details = {"findings": [{"file": "x.py"}]}

        with patch("sova.core.steps.address_review.read_handoff_file", return_value=mock_handoff):
            findings = _load_review_findings(tmp_path)

        assert len(findings) == 1

    def test_load_review_findings_returns_empty_when_no_handoff(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import _load_review_findings

        with patch("sova.core.steps.address_review.read_handoff_file", return_value=None):
            findings = _load_review_findings(tmp_path)

        assert findings == []

    async def test_load_review_findings_from_db(self) -> None:
        from sova.core.steps.address_review import _load_review_findings_from_db

        mock_handoff = MagicMock()
        mock_handoff.pending_findings = [{"file": "y.py"}]

        with patch("sova.core.steps.address_review.read_handoff", new_callable=AsyncMock, return_value=mock_handoff):
            findings = await _load_review_findings_from_db(99)

        assert len(findings) == 1

    async def test_load_review_findings_from_db_returns_empty_on_none_id(self) -> None:
        from sova.core.steps.address_review import _load_review_findings_from_db

        findings = await _load_review_findings_from_db(None)
        assert findings == []

    async def test_load_review_findings_by_issue(self) -> None:
        from datetime import datetime, timezone

        from sova.core.steps.address_review import _load_review_findings_by_issue
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="200",
                    role="reviewer",
                    status="failed",
                    handoff_json={
                        "pending_findings": [
                            {"file": "a.py", "severity": 8, "description": "bug"},
                        ],
                    },
                    started_at=datetime.now(timezone.utc),
                )
            )

        findings = await _load_review_findings_by_issue("200")
        assert len(findings) == 1
        assert findings[0]["file"] == "a.py"

    async def test_execute_falls_back_to_issue_query(self, tmp_path: Path) -> None:
        """When file and resume_run_id both miss, finds reviewer run by issue."""
        from datetime import datetime, timezone

        from sova.core.steps.address_review import AddressReviewStep
        from sova.db.models import TaskRun
        from sova.db.session import get_session
        from sova.llm.models import LLMResult

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="201",
                    role="reviewer",
                    status="failed",
                    handoff_json={
                        "next_action": "address_review",
                        "pending_findings": [
                            {"file": "z.py", "severity": 6, "category": "style", "description": "Needs fix"},
                        ],
                    },
                    started_at=datetime.now(timezone.utc),
                )
            )

        ctx = _make_ctx(tmp_path, pr_number=42, worktree_dir=tmp_path / "worktree")
        ctx.issue_number = "201"
        ctx.resume_run_id = None
        step = AddressReviewStep()

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.address_review.read_handoff_file", return_value=None),
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch("sova.core.steps.address_review.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123")
            mock_invoke.return_value = LLMResult(text="Fixed", model="sonnet", cost_usd=Decimal("0.03"))
            result = await step.execute(ctx)

        assert result.success
        assert "1 review findings" in result.summary
        prompt = mock_invoke.call_args[0][1]
        assert "Needs fix" in prompt


# ---------------------------------------------------------------------------
# ResolveExternalReviewsStep
# ---------------------------------------------------------------------------


class TestResolveExternalReviewsStep:
    async def test_skips_when_no_pr(self, tmp_path: Path) -> None:
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _make_ctx(tmp_path)
        ctx.pr_number = None
        step = ResolveExternalReviewsStep()

        result = await step.execute(ctx)
        assert result.success
        assert "No PR" in result.summary

    async def test_dismisses_bot_reviews_only(self, tmp_path: Path) -> None:
        """Thread resolution and the address summary moved to resolve_review_threads."""
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _make_ctx(tmp_path, pr_number=42)
        ctx.pipeline_variant = "address_review"
        step = ResolveExternalReviewsStep()

        with (
            patch("sova.adapters.external_reviews.fetch_review_threads", new_callable=AsyncMock) as mock_threads,
            patch(
                "sova.core.steps.resolve_external_reviews._dismiss_bot_reviews",
                new_callable=AsyncMock,
                return_value=1,
            ),
        ):
            result = await step.execute(ctx)

        assert result.success
        assert result.summary == "1 bot reviews dismissed"
        mock_threads.assert_not_awaited()
        ctx.adapter.post_pr_review.assert_not_awaited()

    async def test_nothing_to_dismiss(self, tmp_path: Path) -> None:
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _make_ctx(tmp_path, pr_number=42)
        step = ResolveExternalReviewsStep()

        with patch(
            "sova.core.steps.resolve_external_reviews._dismiss_bot_reviews",
            new_callable=AsyncMock,
            return_value=0,
        ):
            result = await step.execute(ctx)

        assert result.success
        assert result.summary == "No bot reviews to dismiss"

    async def test_can_skip_without_pr(self, tmp_path: Path) -> None:
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _make_ctx(tmp_path)
        ctx.pr_number = None
        step = ResolveExternalReviewsStep()
        assert await step.can_skip(ctx)

    async def test_gate_always_passes(self, tmp_path: Path) -> None:
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _make_ctx(tmp_path, pr_number=42)
        step = ResolveExternalReviewsStep()
        gate = await step.validate_output(ctx)
        assert gate.passed


class TestDismissBotReviews:
    async def test_dismisses_bot_changes_requested(self) -> None:
        from sova.core.steps.resolve_external_reviews import _dismiss_bot_reviews

        reviews_json = json.dumps(
            [
                {"id": 1001, "state": "CHANGES_REQUESTED", "user": {"login": "coderabbitai[bot]", "type": "Bot"}},
                {"id": 1002, "state": "APPROVED", "user": {"login": "human-dev", "type": "User"}},
                {"id": 1003, "state": "CHANGES_REQUESTED", "user": {"login": "human-reviewer", "type": "User"}},
            ]
        )

        with patch("sova.core.steps.resolve_external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=reviews_json),  # fetch reviews
                MagicMock(success=True, stdout=""),  # dismiss review 1001
            ]
            count = await _dismiss_bot_reviews(42, repo="user/repo")

        assert count == 1
        dismiss_call = mock_run.call_args_list[1]
        assert "1001" in dismiss_call[0][4]
        assert "dismissals" in dismiss_call[0][4]

    async def test_skips_human_reviews(self) -> None:
        from sova.core.steps.resolve_external_reviews import _dismiss_bot_reviews

        reviews_json = json.dumps(
            [
                {"id": 1001, "state": "CHANGES_REQUESTED", "user": {"login": "human", "type": "User"}},
            ]
        )

        with patch("sova.core.steps.resolve_external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=reviews_json)
            count = await _dismiss_bot_reviews(42, repo="user/repo")

        assert count == 0

    async def test_handles_fetch_failure(self) -> None:
        from sova.core.steps.resolve_external_reviews import _dismiss_bot_reviews

        with patch("sova.core.steps.resolve_external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stderr="API error")
            count = await _dismiss_bot_reviews(42, repo="user/repo")

        assert count == 0


class TestFetchThreadsWithAuthorsFilter:
    async def test_filters_by_custom_authors(self) -> None:
        from sova.adapters.external_reviews import _fetch_coderabbit_threads

        graphql_response = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "t1",
                                        "isResolved": False,
                                        "path": "a.py",
                                        "line": 10,
                                        "comments": {"nodes": [{"body": "fix", "author": {"login": "xsovad06"}}]},
                                    },
                                    {
                                        "id": "t2",
                                        "isResolved": False,
                                        "path": "b.py",
                                        "line": 20,
                                        "comments": {"nodes": [{"body": "issue", "author": {"login": "coderabbitai"}}]},
                                    },
                                    {
                                        "id": "t3",
                                        "isResolved": False,
                                        "path": "c.py",
                                        "line": 30,
                                        "comments": {"nodes": [{"body": "nit", "author": {"login": "other-human"}}]},
                                    },
                                    {
                                        "id": "t4",
                                        "isResolved": True,
                                        "path": "d.py",
                                        "line": 40,
                                        "comments": {"nodes": [{"body": "old", "author": {"login": "xsovad06"}}]},
                                    },
                                ]
                            }
                        }
                    }
                }
            }
        )

        with patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=graphql_response)
            result = await _fetch_coderabbit_threads(
                "user/repo",
                42,
                authors={"xsovad06", "coderabbitai"},
                github_user="xsovad06",
            )

        assert sorted(result.thread_ids) == ["t1", "t2"]

    async def test_returns_empty_on_failure(self) -> None:
        from sova.adapters.external_reviews import _fetch_coderabbit_threads

        with patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stderr="error")
            result = await _fetch_coderabbit_threads("user/repo", 42, authors={"x"}, github_user="x")

        assert result.thread_ids == []
        assert result.findings == []

    async def test_returns_empty_on_bad_json(self) -> None:
        from sova.adapters.external_reviews import _fetch_coderabbit_threads

        with patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="not json")
            result = await _fetch_coderabbit_threads("user/repo", 42, authors={"x"}, github_user="x")

        assert result.thread_ids == []

    async def test_skips_threads_without_comments(self) -> None:
        from sova.adapters.external_reviews import _fetch_coderabbit_threads

        graphql_response = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "t1",
                                        "isResolved": False,
                                        "path": "a.py",
                                        "line": 1,
                                        "comments": {"nodes": []},
                                    },
                                ]
                            }
                        }
                    }
                }
            }
        )

        with patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=graphql_response)
            result = await _fetch_coderabbit_threads("user/repo", 42, authors={"x"}, github_user="x")

        assert result.thread_ids == []


class TestDismissBotReviewsEdgeCases:
    async def test_handles_bad_json(self) -> None:
        from sova.core.steps.resolve_external_reviews import _dismiss_bot_reviews

        with patch("sova.core.steps.resolve_external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="not json")
            count = await _dismiss_bot_reviews(42, repo="user/repo")

        assert count == 0

    async def test_handles_non_list_response(self) -> None:
        from sova.core.steps.resolve_external_reviews import _dismiss_bot_reviews

        with patch("sova.core.steps.resolve_external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout='{"error": "something"}')
            count = await _dismiss_bot_reviews(42, repo="user/repo")

        assert count == 0

    async def test_handles_dismiss_failure(self) -> None:
        from sova.core.steps.resolve_external_reviews import _dismiss_bot_reviews

        reviews_json = json.dumps(
            [
                {"id": 1001, "state": "CHANGES_REQUESTED", "user": {"login": "bot[bot]", "type": "Bot"}},
            ]
        )

        with patch("sova.core.steps.resolve_external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=reviews_json),
                MagicMock(success=False, stderr="403 Forbidden"),
            ]
            count = await _dismiss_bot_reviews(42, repo="user/repo")

        assert count == 0


# ---------------------------------------------------------------------------
# AddressReviewStep: _load_coderabbit_findings
# ---------------------------------------------------------------------------


class TestLoadCoderabbitFindings:
    async def test_returns_findings_from_external_reviews(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import _load_coderabbit_findings

        ctx = _make_ctx(tmp_path, pr_number=42)

        mock_result = MagicMock()
        mock_result.findings = [
            MagicMock(file_path="foo.py", line=10, message="Fix this"),
        ]
        mock_result.thread_ids = ["t1"]

        with patch(
            "sova.adapters.external_reviews._fetch_coderabbit_threads",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            findings, thread_ids = await _load_coderabbit_findings(ctx)

        assert len(findings) == 1
        assert findings[0]["file"] == "foo.py"
        assert findings[0]["source"] == "coderabbit"
        assert thread_ids == ["t1"]

    async def test_returns_empty_without_pr(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import _load_coderabbit_findings

        ctx = _make_ctx(tmp_path)
        ctx.pr_number = None
        findings, thread_ids = await _load_coderabbit_findings(ctx)
        assert findings == []
        assert thread_ids == []

    async def test_returns_empty_on_exception(self, tmp_path: Path) -> None:
        from sova.core.steps.address_review import _load_coderabbit_findings

        ctx = _make_ctx(tmp_path, pr_number=42)

        with patch(
            "sova.adapters.external_reviews._fetch_coderabbit_threads",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Network error"),
        ):
            findings, thread_ids = await _load_coderabbit_findings(ctx)

        assert findings == []
        assert thread_ids == []
