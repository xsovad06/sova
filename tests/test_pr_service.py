"""Tests for PR tracker service."""

from __future__ import annotations

import time

from sova.dashboard.services.pr_service import (
    ComputedPRState,
    _enrich_pr,
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
        assert _summarize_ci(checks) == "passed"

    def test_skipped_plus_success(self) -> None:
        checks = [
            {"status": "COMPLETED", "conclusion": "SKIPPED"},
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
        assert _summarize_ci(checks) == "passed"

    def test_failed_beats_pending(self) -> None:
        checks = [
            {"status": "IN_PROGRESS", "conclusion": ""},
            {"status": "COMPLETED", "conclusion": "FAILURE"},
        ]
        assert _summarize_ci(checks) == "failed"

    def test_error_conclusion(self) -> None:
        checks = [{"status": "COMPLETED", "conclusion": "ERROR"}]
        assert _summarize_ci(checks) == "failed"

    def test_timed_out(self) -> None:
        checks = [{"status": "COMPLETED", "conclusion": "TIMED_OUT"}]
        assert _summarize_ci(checks) == "failed"

    def test_queued(self) -> None:
        checks = [{"status": "QUEUED", "conclusion": ""}]
        assert _summarize_ci(checks) == "pending"

    def test_completed_no_conclusion(self) -> None:
        checks = [{"status": "COMPLETED", "conclusion": ""}]
        assert _summarize_ci(checks) == "passed"

    def test_neutral(self) -> None:
        checks = [{"status": "COMPLETED", "conclusion": "NEUTRAL"}]
        assert _summarize_ci(checks) == "passed"

    def test_status_context_failure(self) -> None:
        checks = [{"__typename": "StatusContext", "state": "FAILURE"}]
        assert _summarize_ci(checks) == "failed"

    def test_status_context_error(self) -> None:
        checks = [{"__typename": "StatusContext", "state": "ERROR"}]
        assert _summarize_ci(checks) == "failed"

    def test_unknown_status_defaults_pending(self) -> None:
        checks = [{"status": "UNKNOWN", "conclusion": ""}]
        assert _summarize_ci(checks) == "pending"

    def test_cancelled(self) -> None:
        checks = [{"status": "COMPLETED", "conclusion": "CANCELLED"}]
        assert _summarize_ci(checks) == "failed"

    def test_action_required(self) -> None:
        checks = [{"status": "COMPLETED", "conclusion": "ACTION_REQUIRED"}]
        assert _summarize_ci(checks) == "failed"

    def test_stale(self) -> None:
        checks = [{"status": "COMPLETED", "conclusion": "STALE"}]
        assert _summarize_ci(checks) == "failed"

    def test_status_context_unknown_state_ignored(self) -> None:
        checks = [
            {"__typename": "StatusContext", "state": "SOMETHING_ELSE"},
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
        assert _summarize_ci(checks) == "passed"


class TestEnrichPr:
    def _raw_pr(self, **overrides: object) -> dict:
        base: dict = {
            "number": 42,
            "title": "Test PR",
            "headRefName": "feat/test",
            "url": "https://github.com/org/repo/pull/42",
            "reviewDecision": "",
            "isDraft": False,
            "author": {"login": "dev"},
            "labels": [{"name": "feature"}],
            "createdAt": "2026-06-01T12:00:00Z",
            "body": "Closes #10",
            "state": "OPEN",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "mergeable": "MERGEABLE",
        }
        base.update(overrides)
        return base

    def test_basic_enrichment(self) -> None:
        result = _enrich_pr(self._raw_pr(), time.time())
        assert result["number"] == 42
        assert result["title"] == "Test PR"
        assert result["branch"] == "feat/test"
        assert result["linked_issue"] == 10
        assert result["author"] == "dev"
        assert result["labels"] == ["feature"]
        assert result["ci_status"] == "passed"
        assert result["mergeable"] == "MERGEABLE"
        assert result["is_draft"] is False

    def test_draft_state(self) -> None:
        result = _enrich_pr(self._raw_pr(isDraft=True), time.time())
        assert result["computed_state"] == ComputedPRState.DRAFT
        assert result["state_label"] == "Draft"

    def test_approved_ci_green_mergeable(self) -> None:
        result = _enrich_pr(self._raw_pr(reviewDecision="APPROVED"), time.time())
        assert result["computed_state"] == ComputedPRState.APPROVED_CI_GREEN

    def test_no_body_linked_issue(self) -> None:
        result = _enrich_pr(self._raw_pr(body=None), time.time())
        assert result["linked_issue"] is None

    def test_no_author(self) -> None:
        result = _enrich_pr(self._raw_pr(author=None), time.time())
        assert result["author"] == ""

    def test_no_labels(self) -> None:
        result = _enrich_pr(self._raw_pr(labels=None), time.time())
        assert result["labels"] == []

    def test_age_seconds(self) -> None:
        now = time.time()
        result = _enrich_pr(self._raw_pr(createdAt="2026-06-01T12:00:00Z"), now)
        assert result["age_seconds"] > 0

    def test_invalid_created_at(self) -> None:
        result = _enrich_pr(self._raw_pr(createdAt="not-a-date"), time.time())
        assert result["age_seconds"] == 0

    def test_no_ci_rollup(self) -> None:
        result = _enrich_pr(self._raw_pr(statusCheckRollup=None), time.time())
        assert result["ci_status"] == "none"

    def test_changes_requested(self) -> None:
        result = _enrich_pr(
            self._raw_pr(reviewDecision="CHANGES_REQUESTED", statusCheckRollup=None),
            time.time(),
        )
        assert result["computed_state"] == ComputedPRState.CHANGES_REQUESTED
