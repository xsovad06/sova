"""Tests for PR tracker service."""

from __future__ import annotations

from sova.dashboard.services.pr_service import (
    ComputedPRState,
    _summarize_ci,
    compute_pr_state,
    parse_linked_issue,
)


def _state(**kwargs: object) -> str:
    defaults = {"is_draft": False, "review_decision": "", "ci_status": "none", "mergeable": ""}
    defaults.update(kwargs)
    return compute_pr_state(**defaults)  # type: ignore[arg-type]


class TestComputePrState:
    def test_draft(self) -> None:
        assert _state(is_draft=True) == ComputedPRState.DRAFT

    def test_draft_overrides_approved(self) -> None:
        assert _state(is_draft=True, review_decision="APPROVED", ci_status="passed") == ComputedPRState.DRAFT

    def test_ci_running(self) -> None:
        assert _state(ci_status="pending") == ComputedPRState.CI_RUNNING

    def test_ci_failed(self) -> None:
        assert _state(ci_status="failed") == ComputedPRState.CI_FAILED

    def test_changes_requested(self) -> None:
        assert _state(review_decision="CHANGES_REQUESTED", ci_status="passed") == ComputedPRState.CHANGES_REQUESTED

    def test_approved_ci_green(self) -> None:
        result = _state(review_decision="APPROVED", ci_status="passed", mergeable="MERGEABLE")
        assert result == ComputedPRState.APPROVED_CI_GREEN

    def test_approved_ci_not_green(self) -> None:
        assert _state(review_decision="APPROVED") == ComputedPRState.APPROVED

    def test_approved_not_mergeable(self) -> None:
        result = _state(review_decision="APPROVED", ci_status="passed", mergeable="CONFLICTING")
        assert result == ComputedPRState.APPROVED

    def test_awaiting_review_default(self) -> None:
        assert _state(ci_status="passed", mergeable="MERGEABLE") == ComputedPRState.AWAITING_REVIEW

    def test_ci_failed_beats_changes_requested(self) -> None:
        assert _state(review_decision="CHANGES_REQUESTED", ci_status="failed") == ComputedPRState.CI_FAILED

    def test_ci_running_beats_approved(self) -> None:
        result = _state(review_decision="APPROVED", ci_status="pending", mergeable="MERGEABLE")
        assert result == ComputedPRState.CI_RUNNING


class TestParseLinkedIssue:
    def test_closes(self) -> None:
        assert parse_linked_issue("Some text\nCloses #42\nMore text") == 42

    def test_fixes(self) -> None:
        assert parse_linked_issue("Fixes #123") == 123

    def test_resolves(self) -> None:
        assert parse_linked_issue("Resolves #7") == 7

    def test_case_insensitive(self) -> None:
        assert parse_linked_issue("closes #99") == 99

    def test_no_match(self) -> None:
        assert parse_linked_issue("No issue linked here") is None

    def test_none_body(self) -> None:
        assert parse_linked_issue(None) is None

    def test_empty_body(self) -> None:
        assert parse_linked_issue("") is None

    def test_first_match(self) -> None:
        assert parse_linked_issue("Closes #10\nFixes #20") == 10


class TestSummarizeCi:
    def test_empty(self) -> None:
        assert _summarize_ci(None) == "none"
        assert _summarize_ci([]) == "none"

    def test_all_success(self) -> None:
        checks = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
        assert _summarize_ci(checks) == "passed"

    def test_pending(self) -> None:
        checks = [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "IN_PROGRESS", "conclusion": ""},
        ]
        assert _summarize_ci(checks) == "pending"

    def test_failed(self) -> None:
        checks = [{"status": "COMPLETED", "conclusion": "FAILURE"}]
        assert _summarize_ci(checks) == "failed"

    def test_status_context_success(self) -> None:
        checks = [
            {"__typename": "StatusContext", "state": "SUCCESS", "context": "CodeRabbit"},
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
        assert _summarize_ci(checks) == "passed"

    def test_status_context_pending(self) -> None:
        checks = [
            {"__typename": "StatusContext", "state": "PENDING", "context": "CodeRabbit"},
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
        assert _summarize_ci(checks) == "pending"

    def test_skipped_only(self) -> None:
        checks = [{"status": "COMPLETED", "conclusion": "SKIPPED"}]
        assert _summarize_ci(checks) == "none"

    def test_skipped_plus_success(self) -> None:
        checks = [
            {"status": "COMPLETED", "conclusion": "SKIPPED"},
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
        assert _summarize_ci(checks) == "passed"
