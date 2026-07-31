"""Tests for PR tracker service."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest

from sova.config.models import IntegrationGatesConfig, ProjectConfig
from sova.dashboard.services.pr_service import (
    ComputedPRState,
    _age_seconds,
    _check_coderabbit_from_pr_data,
    _check_threads_from_pr_data,
    _enrich_pr,
    _extract_latest_approval_at,
    _extract_linked_issue,
    _extract_review_logins,
    _summarize_ci,
    check_integration_gates,
    compute_pr_state,
    parse_linked_issue,
)


def _state(**kwargs: object) -> str:
    defaults: dict[str, object] = {
        "is_draft": False,
        "review_decision": "",
        "ci_status": "none",
        "mergeable": "",
        "latest_reviews": None,
        "all_threads_resolved": False,
    }
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
        assert _state(ci_status="none", mergeable="MERGEABLE") == ComputedPRState.AWAITING_REVIEW

    def test_ci_failed_beats_changes_requested(self) -> None:
        assert _state(review_decision="CHANGES_REQUESTED", ci_status="failed") == ComputedPRState.CI_FAILED

    def test_ci_running_beats_approved(self) -> None:
        result = _state(review_decision="APPROVED", ci_status="pending", mergeable="MERGEABLE")
        assert result == ComputedPRState.CI_RUNNING

    def test_threads_resolved_ci_green_ready_to_merge(self) -> None:
        reviews = [{"state": "COMMENTED", "author": {"login": "reviewer"}}]
        result = _state(
            latest_reviews=reviews,
            all_threads_resolved=True,
            ci_status="passed",
            mergeable="MERGEABLE",
        )
        assert result == ComputedPRState.APPROVED_CI_GREEN

    def test_threads_resolved_ci_not_green_approved(self) -> None:
        reviews = [{"state": "COMMENTED", "author": {"login": "reviewer"}}]
        result = _state(latest_reviews=reviews, all_threads_resolved=True, ci_status="none")
        assert result == ComputedPRState.APPROVED

    def test_threads_not_resolved_stays_in_review(self) -> None:
        reviews = [{"state": "COMMENTED", "author": {"login": "reviewer"}}]
        result = _state(latest_reviews=reviews, all_threads_resolved=False, ci_status="passed")
        assert result == ComputedPRState.REVIEW_ADDRESSED

    def test_threads_resolved_with_changes_requested_stays_blocked(self) -> None:
        reviews = [{"state": "CHANGES_REQUESTED", "author": {"login": "reviewer"}}]
        result = _state(latest_reviews=reviews, all_threads_resolved=True, ci_status="passed")
        assert result == ComputedPRState.CHANGES_REQUESTED

    def test_no_reviews_ci_green_mergeable_ready_to_ship(self) -> None:
        result = _state(ci_status="passed", mergeable="MERGEABLE", latest_reviews=None)
        assert result == ComputedPRState.APPROVED_CI_GREEN

    def test_no_reviews_ci_green_not_mergeable_stays_awaiting(self) -> None:
        result = _state(ci_status="passed", mergeable="CONFLICTING", latest_reviews=None)
        assert result == ComputedPRState.AWAITING_REVIEW


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

    def test_jira_markdown_link(self) -> None:
        body = "## JIRA\n[RHCLOUD-48767](https://issues.redhat.com/browse/RHCLOUD-48767)"
        assert parse_linked_issue(body) == 48767

    def test_jira_plain_link(self) -> None:
        body = "JIRA: https://issues.redhat.com/browse/RHCLOUD-48767"
        assert parse_linked_issue(body) == 48767

    def test_github_preferred_over_jira(self) -> None:
        body = "Closes #42\nJIRA: https://issues.redhat.com/browse/RHCLOUD-48767"
        assert parse_linked_issue(body) == 42

    def test_jira_multi_letter_project(self) -> None:
        body = "[ABC-123](https://jira.example.com/browse/ABC-123)"
        assert parse_linked_issue(body) == 123


class TestExtractLinkedIssue:
    def test_closing_references_preferred(self) -> None:
        raw = {"closingIssuesReferences": [{"number": 42}], "body": "Fixes #99", "title": "feat(#77)"}
        assert _extract_linked_issue(raw) == 42

    def test_body_fallback(self) -> None:
        raw = {
            "closingIssuesReferences": [],
            "body": "Closes #88",
            "title": "feat(#99)",
            "headRefName": "feat/issue-77",
        }
        assert _extract_linked_issue(raw) == 88

    def test_title_fallback_parens(self) -> None:
        raw = {
            "closingIssuesReferences": [],
            "body": "No link here",
            "title": "feat(#374): merge conflict gate",
            "headRefName": "feat/issue-111",
        }
        assert _extract_linked_issue(raw) == 374

    def test_title_fallback_brackets(self) -> None:
        raw = {
            "closingIssuesReferences": [],
            "body": "",
            "title": "[#55] fix something",
            "headRefName": "feat/issue-222",
        }
        assert _extract_linked_issue(raw) == 55

    def test_branch_fallback(self) -> None:
        raw = {"closingIssuesReferences": [], "body": "", "title": "some fix", "headRefName": "feat/issue-314"}
        assert _extract_linked_issue(raw) == 314

    def test_branch_requires_segment_boundary(self) -> None:
        raw = {"closingIssuesReferences": [], "body": "", "title": "some fix", "headRefName": "feat/notissue-314"}
        assert _extract_linked_issue(raw) is None

    def test_branch_requires_suffix_boundary(self) -> None:
        raw = {"closingIssuesReferences": [], "body": "", "title": "some fix", "headRefName": "feat/issue-314draft"}
        assert _extract_linked_issue(raw) is None

    def test_branch_with_suffix_delimiter(self) -> None:
        raw = {"closingIssuesReferences": [], "body": "", "title": "some fix", "headRefName": "feat/issue-314-fix"}
        assert _extract_linked_issue(raw) == 314

    def test_no_match(self) -> None:
        raw = {"closingIssuesReferences": [], "body": "nothing", "title": "generic", "headRefName": "main"}
        assert _extract_linked_issue(raw) is None

    def test_empty_closing_refs_is_none(self) -> None:
        raw = {"closingIssuesReferences": None, "body": "Fixes #10"}
        assert _extract_linked_issue(raw) == 10

    def test_title_number_in_parens_without_hash_is_not_issue(self) -> None:
        raw = {"closingIssuesReferences": [], "body": "", "title": "fix: handle (500) errors"}
        assert _extract_linked_issue(raw) is None

    def test_title_jira_key_brackets(self) -> None:
        raw = {
            "closingIssuesReferences": [],
            "body": "",
            "title": "[RHCLOUD-49651] feat: eval() security fix",
            "headRefName": "feat/some-branch",
        }
        assert _extract_linked_issue(raw) == 49651

    def test_title_jira_key_with_short_project(self) -> None:
        raw = {
            "closingIssuesReferences": [],
            "body": "",
            "title": "[ABC-7] fix something",
            "headRefName": "main",
        }
        assert _extract_linked_issue(raw) == 7

    def test_branch_jira_key(self) -> None:
        raw = {
            "closingIssuesReferences": [],
            "body": "",
            "title": "some fix",
            "headRefName": "feat/RHCLOUD-49651-turnpike-eval-security",
        }
        assert _extract_linked_issue(raw) == 49651

    def test_branch_jira_key_at_end(self) -> None:
        raw = {
            "closingIssuesReferences": [],
            "body": "",
            "title": "some fix",
            "headRefName": "fix/ABC-123",
        }
        assert _extract_linked_issue(raw) == 123

    def test_branch_jira_key_does_not_match_lowercase(self) -> None:
        raw = {
            "closingIssuesReferences": [],
            "body": "",
            "title": "some fix",
            "headRefName": "feat/rhcloud-49651-stuff",
        }
        assert _extract_linked_issue(raw) is None

    def test_body_preferred_over_title_jira_key(self) -> None:
        raw = {
            "closingIssuesReferences": [],
            "body": "JIRA: https://redhat.atlassian.net/browse/RHCLOUD-48767",
            "title": "[RHCLOUD-49651] feat: something",
            "headRefName": "feat/RHCLOUD-49651-stuff",
        }
        assert _extract_linked_issue(raw) == 48767


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
        assert _summarize_ci(checks) == "passed"

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
            "updatedAt": "2026-06-02T12:00:00Z",
            "body": "Closes #10",
            "state": "OPEN",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "mergeable": "MERGEABLE",
            "additions": 50,
            "deletions": 10,
            "changedFiles": 3,
            "assignees": [{"login": "dev"}],
            "commits": [{"oid": "abc"}, {"oid": "def"}],
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

    def test_diff_stats_enrichment(self) -> None:
        result = _enrich_pr(self._raw_pr(), time.time())
        assert result["additions"] == 50
        assert result["deletions"] == 10
        assert result["changed_files"] == 3
        assert result["assignees"] == ["dev"]
        assert result["commit_count"] == 2
        assert result["updated_at"] == "2026-06-02T12:00:00Z"

    def test_diff_stats_missing(self) -> None:
        result = _enrich_pr(
            self._raw_pr(additions=None, deletions=None, changedFiles=None, assignees=None, commits=None),
            time.time(),
        )
        assert result["additions"] == 0
        assert result["deletions"] == 0
        assert result["changed_files"] == 0
        assert result["assignees"] == []
        assert result["commit_count"] == 0

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

    def test_threads_resolved_ready_to_merge(self) -> None:
        raw = self._raw_pr(
            reviewDecision="",
            latestReviews=[{"state": "COMMENTED", "author": {"login": "reviewer"}}],
            _thread_counts=(5, 5),
        )
        result = _enrich_pr(raw, time.time())
        assert result["computed_state"] == ComputedPRState.APPROVED_CI_GREEN

    def test_threads_partially_resolved_in_review(self) -> None:
        raw = self._raw_pr(
            reviewDecision="",
            latestReviews=[{"state": "COMMENTED", "author": {"login": "reviewer"}}],
            _thread_counts=(5, 3),
        )
        result = _enrich_pr(raw, time.time())
        assert result["computed_state"] == ComputedPRState.REVIEW_ADDRESSED

    def test_latest_approval_at_populated(self) -> None:
        raw = self._raw_pr(
            latestReviews=[
                {"state": "APPROVED", "submittedAt": "2026-07-20T12:00:00Z", "author": {"login": "alice"}},
            ],
        )
        result = _enrich_pr(raw, time.time())
        assert result["latest_approval_at"] == "2026-07-20T12:00:00Z"

    def test_latest_approval_at_none_without_approvals(self) -> None:
        result = _enrich_pr(self._raw_pr(), time.time())
        assert result["latest_approval_at"] is None


# ---------------------------------------------------------------------------
# get_review_thread_counts
# ---------------------------------------------------------------------------


class TestGetReviewThreadCounts:
    """Tests for get_review_thread_counts in git/pr.py."""

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self) -> None:
        from sova.git.pr import get_review_thread_counts

        result = await get_review_thread_counts([], repo="owner/repo")
        assert result == {}

    @pytest.mark.asyncio
    async def test_parses_thread_counts(self, monkeypatch) -> None:
        from sova.git.pr import get_review_thread_counts

        graphql_response = {
            "data": {
                "repository": {
                    "pr10": {
                        "reviewThreads": {
                            "totalCount": 3,
                            "nodes": [
                                {"isResolved": True},
                                {"isResolved": False},
                                {"isResolved": True},
                            ],
                        }
                    },
                    "pr20": {
                        "reviewThreads": {
                            "totalCount": 1,
                            "nodes": [{"isResolved": False}],
                        }
                    },
                }
            }
        }
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_review_thread_counts([10, 20], repo="owner/repo")
        assert result[10] == (3, 2)
        assert result[20] == (1, 0)

    @pytest.mark.asyncio
    async def test_returns_empty_on_failure(self, monkeypatch) -> None:
        from sova.git.pr import get_review_thread_counts

        mock_run = AsyncMock()
        mock_run.return_value.success = False
        mock_run.return_value.stderr = "error"
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_review_thread_counts([10], repo="owner/repo")
        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_empty_on_bad_json(self, monkeypatch) -> None:
        from sova.git.pr import get_review_thread_counts

        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = "not json"
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_review_thread_counts([10], repo="owner/repo")
        assert result == {}

    @pytest.mark.asyncio
    async def test_handles_missing_pr_data(self, monkeypatch) -> None:
        from sova.git.pr import get_review_thread_counts

        graphql_response = {"data": {"repository": {}}}
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_review_thread_counts([10], repo="owner/repo")
        assert result[10] == (0, 0)

    @pytest.mark.asyncio
    async def test_handles_data_null_response(self, monkeypatch) -> None:
        from sova.git.pr import get_review_thread_counts

        graphql_response = {"data": None, "errors": [{"message": "something went wrong"}]}
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_review_thread_counts([10], repo="owner/repo")
        assert result[10] == (0, 0)


# ---------------------------------------------------------------------------
# check_integration_gates
# ---------------------------------------------------------------------------


def _make_config(**gate_overrides: bool) -> ProjectConfig:
    gates = IntegrationGatesConfig(**gate_overrides)
    return ProjectConfig(
        github_repo="owner/repo",
        github_user="testuser",
        integration_gates=gates,
    )


def _pr_data(**overrides: object) -> dict:
    base: dict = {
        "number": 42,
        "ci_status": "passed",
        "mergeable": "MERGEABLE",
        "computed_state": "approved_ci_green",
        "state_label": "Ready to Merge",
    }
    base.update(overrides)
    return base


class TestCheckIntegrationGates:
    """Tests for configurable integration gates."""

    @pytest.mark.asyncio
    async def test_all_gates_disabled_passes(self) -> None:
        cfg = _make_config()
        result = await check_integration_gates(pr_data=_pr_data(), issue_number="10", config=cfg)
        assert result["passed"] is True
        assert all(g["passed"] for g in result["gates"])
        assert all(not g["enabled"] for g in result["gates"])

    @pytest.mark.asyncio
    async def test_ci_gate_passes_when_ci_green(self) -> None:
        cfg = _make_config(ci_passed=True)
        result = await check_integration_gates(pr_data=_pr_data(ci_status="passed"), issue_number="10", config=cfg)
        ci_gate = next(g for g in result["gates"] if g["name"] == "ci_passed")
        assert ci_gate["enabled"] is True
        assert ci_gate["passed"] is True

    @pytest.mark.asyncio
    async def test_ci_gate_fails_when_ci_pending(self) -> None:
        cfg = _make_config(ci_passed=True)
        result = await check_integration_gates(pr_data=_pr_data(ci_status="pending"), issue_number="10", config=cfg)
        assert result["passed"] is False
        ci_gate = next(g for g in result["gates"] if g["name"] == "ci_passed")
        assert ci_gate["passed"] is False
        assert "pending" in ci_gate["reason"]

    @pytest.mark.asyncio
    async def test_ci_gate_fails_when_ci_failed(self) -> None:
        cfg = _make_config(ci_passed=True)
        result = await check_integration_gates(pr_data=_pr_data(ci_status="failed"), issue_number="10", config=cfg)
        ci_gate = next(g for g in result["gates"] if g["name"] == "ci_passed")
        assert ci_gate["passed"] is False

    @pytest.mark.asyncio
    async def test_sova_review_gate_skipped_without_issue_or_pr(self) -> None:
        # When neither issue_number nor pr_data["number"] is available, the gate skips.
        cfg = _make_config(sova_reviewed=True)
        result = await check_integration_gates(pr_data=_pr_data(number=None), issue_number=None, config=cfg)
        sova_gate = next(g for g in result["gates"] if g["name"] == "sova_reviewed")
        assert sova_gate["passed"] is True
        assert "skipped" in sova_gate["reason"].lower()

    @pytest.mark.asyncio
    async def test_sova_review_gate_checks_pr_when_no_issue(self, monkeypatch) -> None:
        # When issue_number is None but pr_data has a number, the gate still queries by PR.
        cfg = _make_config(sova_reviewed=True)
        mock_verdict = AsyncMock(return_value={"has_sova_review": False, "verdict": None, "finding_count": 0})
        monkeypatch.setattr("sova.dashboard.services.agent_recovery.get_sova_review_verdict", mock_verdict)
        result = await check_integration_gates(pr_data=_pr_data(), issue_number=None, config=cfg)
        sova_gate = next(g for g in result["gates"] if g["name"] == "sova_reviewed")
        assert sova_gate["passed"] is False
        assert "No SOVA review" in sova_gate["reason"]
        mock_verdict.assert_called_once_with(None, pr_number=42)

    @pytest.mark.asyncio
    async def test_sova_review_gate_fails_no_review(self, monkeypatch) -> None:
        cfg = _make_config(sova_reviewed=True)
        mock_verdict = AsyncMock(return_value={"has_sova_review": False, "verdict": None, "finding_count": 0})
        monkeypatch.setattr("sova.dashboard.services.agent_recovery.get_sova_review_verdict", mock_verdict)

        result = await check_integration_gates(pr_data=_pr_data(), issue_number="10", config=cfg)
        sova_gate = next(g for g in result["gates"] if g["name"] == "sova_reviewed")
        assert sova_gate["passed"] is False
        assert "No SOVA review" in sova_gate["reason"]

    @pytest.mark.asyncio
    async def test_sova_review_gate_passes_approved(self, monkeypatch) -> None:
        cfg = _make_config(sova_reviewed=True)
        mock_verdict = AsyncMock(
            return_value={
                "has_sova_review": True,
                "verdict": "approve",
                "finding_count": 0,
            }
        )
        monkeypatch.setattr("sova.dashboard.services.agent_recovery.get_sova_review_verdict", mock_verdict)

        result = await check_integration_gates(pr_data=_pr_data(), issue_number="10", config=cfg)
        sova_gate = next(g for g in result["gates"] if g["name"] == "sova_reviewed")
        assert sova_gate["passed"] is True

    @pytest.mark.asyncio
    async def test_sova_review_gate_fails_revise(self, monkeypatch) -> None:
        cfg = _make_config(sova_reviewed=True)
        mock_verdict = AsyncMock(
            return_value={
                "has_sova_review": True,
                "verdict": "revise",
                "finding_count": 3,
            }
        )
        monkeypatch.setattr("sova.dashboard.services.agent_recovery.get_sova_review_verdict", mock_verdict)

        result = await check_integration_gates(pr_data=_pr_data(), issue_number="10", config=cfg)
        sova_gate = next(g for g in result["gates"] if g["name"] == "sova_reviewed")
        assert sova_gate["passed"] is False
        assert "revise" in sova_gate["reason"]

    @pytest.mark.asyncio
    async def test_coderabbit_gate_passes(self) -> None:
        cfg = _make_config(coderabbit_reviewed=True)
        result = await check_integration_gates(
            pr_data=_pr_data(review_logins=["coderabbitai"]),
            issue_number="10",
            config=cfg,
        )
        cr_gate = next(g for g in result["gates"] if g["name"] == "coderabbit_reviewed")
        assert cr_gate["passed"] is True

    @pytest.mark.asyncio
    async def test_coderabbit_gate_fails_no_review(self) -> None:
        cfg = _make_config(coderabbit_reviewed=True)
        result = await check_integration_gates(
            pr_data=_pr_data(review_logins=["someuser"]),
            issue_number="10",
            config=cfg,
        )
        cr_gate = next(g for g in result["gates"] if g["name"] == "coderabbit_reviewed")
        assert cr_gate["passed"] is False

    @pytest.mark.asyncio
    async def test_threads_gate_passes_all_resolved(self) -> None:
        cfg = _make_config(threads_resolved=True)
        result = await check_integration_gates(
            pr_data=_pr_data(thread_total=5, thread_resolved=5),
            issue_number="10",
            config=cfg,
        )
        thr_gate = next(g for g in result["gates"] if g["name"] == "threads_resolved")
        assert thr_gate["passed"] is True

    @pytest.mark.asyncio
    async def test_threads_gate_fails_unresolved(self) -> None:
        cfg = _make_config(threads_resolved=True)
        result = await check_integration_gates(
            pr_data=_pr_data(thread_total=5, thread_resolved=3),
            issue_number="10",
            config=cfg,
        )
        thr_gate = next(g for g in result["gates"] if g["name"] == "threads_resolved")
        assert thr_gate["passed"] is False
        assert "2 of 5" in thr_gate["reason"]

    @pytest.mark.asyncio
    async def test_threads_gate_passes_no_threads(self) -> None:
        cfg = _make_config(threads_resolved=True)
        result = await check_integration_gates(
            pr_data=_pr_data(thread_total=0, thread_resolved=0),
            issue_number="10",
            config=cfg,
        )
        thr_gate = next(g for g in result["gates"] if g["name"] == "threads_resolved")
        assert thr_gate["passed"] is True

    @pytest.mark.asyncio
    async def test_multiple_gates_one_fails(self, monkeypatch) -> None:
        cfg = _make_config(ci_passed=True, sova_reviewed=True)
        mock_verdict = AsyncMock(
            return_value={
                "has_sova_review": True,
                "verdict": "approve",
                "finding_count": 0,
            }
        )
        monkeypatch.setattr("sova.dashboard.services.agent_recovery.get_sova_review_verdict", mock_verdict)

        result = await check_integration_gates(
            pr_data=_pr_data(ci_status="failed"),
            issue_number="10",
            config=cfg,
        )
        assert result["passed"] is False
        ci_gate = next(g for g in result["gates"] if g["name"] == "ci_passed")
        sova_gate = next(g for g in result["gates"] if g["name"] == "sova_reviewed")
        assert ci_gate["passed"] is False
        assert sova_gate["passed"] is True

    @pytest.mark.asyncio
    async def test_all_enabled_gates_pass(self, monkeypatch) -> None:
        cfg = _make_config(ci_passed=True, sova_reviewed=True, coderabbit_reviewed=True, threads_resolved=True)
        monkeypatch.setattr(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            AsyncMock(return_value={"has_sova_review": True, "verdict": "approve", "finding_count": 0}),
        )
        result = await check_integration_gates(
            pr_data=_pr_data(review_logins=["coderabbitai"], thread_total=3, thread_resolved=3),
            issue_number="10",
            config=cfg,
        )
        assert result["passed"] is True
        assert all(g["passed"] for g in result["gates"])


# ---------------------------------------------------------------------------
# _age_seconds / _extract_review_logins helpers
# ---------------------------------------------------------------------------


class TestAgeSeconds:
    def test_valid_timestamp(self) -> None:
        from datetime import datetime, timezone

        dt = datetime(2025, 6, 15, 6, 0, 0, tzinfo=timezone.utc)
        now = dt.timestamp() + 100
        assert _age_seconds("2025-06-15T06:00:00Z", now) == 100

    def test_empty_string(self) -> None:
        assert _age_seconds("", 1_750_000_000.0) == 0

    def test_invalid_date(self) -> None:
        assert _age_seconds("not-a-date", 1_750_000_000.0) == 0

    def test_zulu_suffix(self) -> None:
        result = _age_seconds("2026-01-01T00:00:00Z", time.time())
        assert result > 0


class TestExtractReviewLogins:
    def test_extracts_unique_logins(self) -> None:
        reviews = [
            {"author": {"login": "alice"}},
            {"author": {"login": "bob"}},
            {"author": {"login": "alice"}},
        ]
        assert _extract_review_logins(reviews) == ["alice", "bob"]

    def test_none_reviews(self) -> None:
        assert _extract_review_logins(None) == []

    def test_empty_reviews(self) -> None:
        assert _extract_review_logins([]) == []

    def test_missing_author(self) -> None:
        reviews = [{"author": None}, {"author": {"login": "bob"}}]
        assert _extract_review_logins(reviews) == ["bob"]

    def test_empty_login(self) -> None:
        reviews = [{"author": {"login": ""}}, {"author": {"login": "bob"}}]
        assert _extract_review_logins(reviews) == ["bob"]


class TestExtractLatestApprovalAt:
    def test_picks_latest_approved(self) -> None:
        reviews = [
            {"state": "APPROVED", "submittedAt": "2026-07-18T10:00:00Z"},
            {"state": "APPROVED", "submittedAt": "2026-07-20T12:00:00Z"},
            {"state": "COMMENTED", "submittedAt": "2026-07-21T08:00:00Z"},
        ]
        assert _extract_latest_approval_at(reviews) == "2026-07-20T12:00:00Z"

    def test_no_approvals(self) -> None:
        reviews = [
            {"state": "CHANGES_REQUESTED", "submittedAt": "2026-07-20T10:00:00Z"},
        ]
        assert _extract_latest_approval_at(reviews) is None

    def test_none_reviews(self) -> None:
        assert _extract_latest_approval_at(None) is None

    def test_empty_reviews(self) -> None:
        assert _extract_latest_approval_at([]) is None

    def test_missing_submitted_at(self) -> None:
        reviews = [{"state": "APPROVED"}]
        assert _extract_latest_approval_at(reviews) is None


# ---------------------------------------------------------------------------
# _check_coderabbit_from_pr_data / _check_threads_from_pr_data helpers
# ---------------------------------------------------------------------------


class TestCheckCoderabbitFromPrData:
    def test_detects_coderabbit_login(self) -> None:
        assert _check_coderabbit_from_pr_data({"review_logins": ["coderabbitai", "alice"]}) is True

    def test_detects_coderabbit_bot(self) -> None:
        assert _check_coderabbit_from_pr_data({"review_logins": ["coderabbitai[bot]"]}) is True

    def test_no_coderabbit(self) -> None:
        assert _check_coderabbit_from_pr_data({"review_logins": ["alice", "bob"]}) is False

    def test_empty_logins(self) -> None:
        assert _check_coderabbit_from_pr_data({"review_logins": []}) is False

    def test_none_logins(self) -> None:
        assert _check_coderabbit_from_pr_data({}) is False


class TestCheckThreadsFromPrData:
    def test_all_resolved(self) -> None:
        gate = _check_threads_from_pr_data({"thread_total": 5, "thread_resolved": 5})
        assert gate["passed"] is True

    def test_some_unresolved(self) -> None:
        gate = _check_threads_from_pr_data({"thread_total": 5, "thread_resolved": 3})
        assert gate["passed"] is False
        assert "2 of 5" in gate["reason"]

    def test_no_threads(self) -> None:
        gate = _check_threads_from_pr_data({"thread_total": 0, "thread_resolved": 0})
        assert gate["passed"] is True


# ---------------------------------------------------------------------------
# PR router endpoint tests
# ---------------------------------------------------------------------------


class TestPRGatesRouter:
    @pytest.mark.asyncio
    async def test_gates_endpoint_no_project_falls_back_to_cwd(self, monkeypatch) -> None:
        from pathlib import Path

        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        monkeypatch.setattr("sova.dashboard.routers.prs.get_project_dir", lambda: None)

        seen_paths: list[Path] = []

        def _raise_no_config(path: Path) -> None:
            seen_paths.append(path)
            raise RuntimeError

        monkeypatch.setattr("sova.dashboard.routers.prs.load_config", _raise_no_config)
        app = create_app(multi_project=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/prs/42/gates")
            assert resp.status_code == 400
            assert resp.json()["detail"] == "Failed to load project configuration"
            assert seen_paths == [Path.cwd()]

    @pytest.mark.asyncio
    async def test_gates_endpoint_config_load_failure(self, monkeypatch, tmp_path) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        monkeypatch.setattr("sova.dashboard.routers.prs.get_project_dir", lambda: tmp_path)

        def _bad_config(_path):
            raise RuntimeError("bad config")

        monkeypatch.setattr("sova.dashboard.routers.prs.load_config", _bad_config)
        app = create_app(multi_project=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/prs/42/gates")
            assert resp.status_code == 400
            assert "configuration" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_gates_endpoint_pr_not_found(self, monkeypatch, tmp_path) -> None:
        from unittest.mock import MagicMock

        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        monkeypatch.setattr("sova.dashboard.routers.prs.get_project_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        monkeypatch.setattr("sova.dashboard.routers.prs.load_config", lambda _: mock_cfg)
        monkeypatch.setattr(
            "sova.dashboard.routers.prs.list_open_prs_with_state",
            AsyncMock(return_value=[]),
        )
        app = create_app(multi_project=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/prs/99/gates")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_gates_endpoint_success(self, monkeypatch, tmp_path) -> None:
        from unittest.mock import MagicMock

        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        monkeypatch.setattr("sova.dashboard.routers.prs.get_project_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        mock_cfg.integration_gates.ci_passed = True
        mock_cfg.integration_gates.sova_reviewed = False
        mock_cfg.integration_gates.coderabbit_reviewed = False
        mock_cfg.integration_gates.threads_resolved = False
        monkeypatch.setattr("sova.dashboard.routers.prs.load_config", lambda _: mock_cfg)

        pr_data = {
            "number": 42,
            "linked_issue": 10,
            "ci_status": "passed",
            "review_logins": [],
            "thread_total": 0,
            "thread_resolved": 0,
        }
        monkeypatch.setattr(
            "sova.dashboard.routers.prs.list_open_prs_with_state",
            AsyncMock(return_value=[pr_data]),
        )
        monkeypatch.setattr(
            "sova.dashboard.routers.prs.check_integration_gates",
            AsyncMock(return_value={"passed": True, "gates": []}),
        )
        app = create_app(multi_project=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/prs/42/gates")
            assert resp.status_code == 200
            data = resp.json()
            assert data["passed"] is True
