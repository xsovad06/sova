"""Tests for PR tracker service."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.dashboard.services.pr_service import (
    ComputedPRState,
    _age_seconds,
    _count_pr_commits,
    _enrich_pr,
    _extract_all_linked_issues,
    _extract_latest_approval_at,
    _extract_linked_issue,
    _extract_pr_assignees,
    _extract_pr_labels,
    _extract_review_logins,
    _is_bot_review,
    _logged_closing_ties,
    _should_unblock_bot_reviews,
    _summarize_ci,
    compute_pr_state,
    get_unresolved_thread_count,
    list_open_prs_with_state,
    parse_linked_issue,
    resolve_head_verdict,
)
from sova.ipc.handoff import DashboardHandoff


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


_BOT_CR = {"state": "CHANGES_REQUESTED", "author": {"login": "coderabbitai[bot]"}}
_HUMAN_CR = {"state": "CHANGES_REQUESTED", "author": {"login": "alice"}}
_COMMENTED = {"state": "COMMENTED", "author": {"login": "reviewer"}}


class TestComputePrState:
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            pytest.param({"is_draft": True}, ComputedPRState.DRAFT, id="draft"),
            pytest.param(
                {"is_draft": True, "review_decision": "APPROVED", "ci_status": "passed"},
                ComputedPRState.DRAFT,
                id="draft-overrides-approved",
            ),
            pytest.param({"ci_status": "pending"}, ComputedPRState.CI_RUNNING, id="ci-running"),
            pytest.param({"ci_status": "failed"}, ComputedPRState.CI_FAILED, id="ci-failed"),
            pytest.param(
                {"review_decision": "CHANGES_REQUESTED", "ci_status": "passed"},
                ComputedPRState.CHANGES_REQUESTED,
                id="changes-requested",
            ),
            pytest.param(
                {"review_decision": "APPROVED", "ci_status": "passed", "mergeable": "MERGEABLE"},
                ComputedPRState.APPROVED_CI_GREEN,
                id="approved-ci-green",
            ),
            # A repository with no CI checks configured (ci_status="none") is neutral, not blocking.
            pytest.param(
                {"review_decision": "APPROVED", "ci_status": "none", "mergeable": "MERGEABLE"},
                ComputedPRState.APPROVED_CI_GREEN,
                id="approved-with-no-ci-is-green",
            ),
            # GitHub's mergeStateStatus BLOCKED (e.g. branch protection awaiting a required review) must
            # not report ready to merge: sova/supervisor/pr_monitor.py fires notify_on_ready_to_merge
            # from this state directly (CodeRabbit, PR #1114).
            pytest.param(
                {
                    "review_decision": "APPROVED",
                    "ci_status": "none",
                    "mergeable": "MERGEABLE",
                    "merge_state": "BLOCKED",
                },
                ComputedPRState.APPROVED,
                id="approved-no-ci-blocked-by-github-is-not-green",
            ),
            # Same BLOCKED guard applies even when CI actually passed.
            pytest.param(
                {
                    "review_decision": "APPROVED",
                    "ci_status": "passed",
                    "mergeable": "MERGEABLE",
                    "merge_state": "BLOCKED",
                },
                ComputedPRState.APPROVED,
                id="approved-ci-green-blocked-by-github-is-not-green",
            ),
            # merge_state DIRTY is the mergeStateStatus equivalent of mergeable CONFLICTING.
            pytest.param(
                {"review_decision": "APPROVED", "ci_status": "passed", "mergeable": "UNKNOWN", "merge_state": "DIRTY"},
                ComputedPRState.CONFLICTED,
                id="dirty-merge-state-is-conflicted",
            ),
            pytest.param({"review_decision": "APPROVED"}, ComputedPRState.APPROVED, id="approved-ci-not-green"),
            pytest.param(
                {"review_decision": "APPROVED", "ci_status": "passed", "mergeable": "CONFLICTING"},
                ComputedPRState.CONFLICTED,
                id="approved-not-mergeable",
            ),
            pytest.param(
                {"ci_status": "none", "mergeable": "MERGEABLE"},
                ComputedPRState.AWAITING_REVIEW,
                id="awaiting-review-default",
            ),
            pytest.param(
                {"review_decision": "CHANGES_REQUESTED", "ci_status": "failed"},
                ComputedPRState.CI_FAILED,
                id="ci-failed-beats-changes-requested",
            ),
            pytest.param(
                {"review_decision": "APPROVED", "ci_status": "pending", "mergeable": "MERGEABLE"},
                ComputedPRState.CI_RUNNING,
                id="ci-running-beats-approved",
            ),
            # A standing SOVA revise or block for the head keeps the PR out of every approved state; any
            # other verdict (approve, none, unknown) leaves the GitHub signals in charge.
            *[
                pytest.param(
                    {
                        "review_decision": "APPROVED",
                        "ci_status": "passed",
                        "mergeable": "MERGEABLE",
                        "sova_verdict": verdict,
                    },
                    expected,
                    id=f"sova-{verdict}-on-green-pr",
                )
                for verdict, expected in [
                    ("revise", ComputedPRState.SOVA_CHANGES),
                    ("block", ComputedPRState.SOVA_CHANGES),
                    ("approve", ComputedPRState.APPROVED_CI_GREEN),
                    ("none", ComputedPRState.APPROVED_CI_GREEN),
                    ("unknown", ComputedPRState.APPROVED_CI_GREEN),
                ]
            ],
            pytest.param(
                {"ci_status": "failed", "sova_verdict": "revise"},
                ComputedPRState.CI_FAILED,
                id="ci-failed-beats-sova-revise",
            ),
            pytest.param(
                {
                    "latest_reviews": [_COMMENTED],
                    "all_threads_resolved": True,
                    "ci_status": "passed",
                    "mergeable": "MERGEABLE",
                },
                ComputedPRState.APPROVED_CI_GREEN,
                id="threads-resolved-ci-green-ready-to-merge",
            ),
            pytest.param(
                {"latest_reviews": [_COMMENTED], "all_threads_resolved": True, "ci_status": "none"},
                ComputedPRState.APPROVED,
                id="threads-resolved-ci-not-green-approved",
            ),
            pytest.param(
                {"latest_reviews": [_COMMENTED], "all_threads_resolved": False, "ci_status": "passed"},
                ComputedPRState.REVIEW_ADDRESSED,
                id="threads-not-resolved-stays-in-review",
            ),
            pytest.param(
                {
                    "latest_reviews": [{"state": "CHANGES_REQUESTED", "author": {"login": "reviewer"}}],
                    "all_threads_resolved": True,
                    "ci_status": "passed",
                },
                ComputedPRState.CHANGES_REQUESTED,
                id="threads-resolved-with-changes-requested-stays-blocked",
            ),
            pytest.param(
                {"ci_status": "passed", "mergeable": "MERGEABLE", "latest_reviews": None},
                ComputedPRState.APPROVED_CI_GREEN,
                id="no-reviews-ci-green-mergeable-ready-to-ship",
            ),
            pytest.param(
                {"ci_status": "passed", "mergeable": "CONFLICTING", "latest_reviews": None},
                ComputedPRState.CONFLICTED,
                id="no-reviews-ci-green-not-mergeable-is-conflicted",
            ),
            pytest.param(
                {
                    "review_decision": "CHANGES_REQUESTED",
                    "latest_reviews": [_BOT_CR],
                    "all_threads_resolved": True,
                    "ci_status": "passed",
                    "mergeable": "MERGEABLE",
                    "superseded_by_new_commit": True,
                },
                ComputedPRState.APPROVED_CI_GREEN,
                id="bot-changes-requested-threads-resolved-ci-green",
            ),
            pytest.param(
                {
                    "review_decision": "CHANGES_REQUESTED",
                    "latest_reviews": [_BOT_CR],
                    "all_threads_resolved": True,
                    "ci_status": "none",
                    "superseded_by_new_commit": True,
                },
                ComputedPRState.APPROVED,
                id="bot-changes-requested-threads-resolved-ci-not-green",
            ),
            # New commits do not auto-dismiss a bot review: without one, stays blocked.
            pytest.param(
                {
                    "review_decision": "CHANGES_REQUESTED",
                    "latest_reviews": [_BOT_CR],
                    "all_threads_resolved": True,
                    "ci_status": "passed",
                    "mergeable": "MERGEABLE",
                    "superseded_by_new_commit": False,
                },
                ComputedPRState.CHANGES_REQUESTED,
                id="bot-changes-requested-threads-resolved-no-new-commit-stays-blocked",
            ),
            pytest.param(
                {
                    "review_decision": "CHANGES_REQUESTED",
                    "latest_reviews": [_BOT_CR],
                    "all_threads_resolved": False,
                    "ci_status": "passed",
                },
                ComputedPRState.CHANGES_REQUESTED,
                id="bot-changes-requested-zero-threads-stays-blocked",
            ),
            pytest.param(
                {
                    "review_decision": "CHANGES_REQUESTED",
                    "latest_reviews": [_BOT_CR],
                    "all_threads_resolved": False,
                    "ci_status": "passed",
                },
                ComputedPRState.CHANGES_REQUESTED,
                id="bot-changes-requested-threads-not-resolved-stays-blocked",
            ),
            pytest.param(
                {
                    "review_decision": "CHANGES_REQUESTED",
                    "latest_reviews": [_BOT_CR, _HUMAN_CR],
                    "all_threads_resolved": True,
                    "ci_status": "passed",
                    "mergeable": "MERGEABLE",
                },
                ComputedPRState.CHANGES_REQUESTED,
                id="mixed-bot-and-human-changes-requested-stays-blocked",
            ),
            pytest.param(
                {
                    "review_decision": "CHANGES_REQUESTED",
                    "latest_reviews": [
                        _BOT_CR,
                        {"state": "CHANGES_REQUESTED", "author": {"login": "dependabot[bot]"}},
                    ],
                    "all_threads_resolved": True,
                    "ci_status": "passed",
                    "mergeable": "MERGEABLE",
                    "superseded_by_new_commit": True,
                },
                ComputedPRState.APPROVED_CI_GREEN,
                id="multiple-bots-changes-requested-threads-resolved",
            ),
            pytest.param(
                {
                    "review_decision": "CHANGES_REQUESTED",
                    "latest_reviews": None,
                    "all_threads_resolved": True,
                    "ci_status": "passed",
                },
                ComputedPRState.CHANGES_REQUESTED,
                id="changes-requested-no-latest-reviews-stays-blocked",
            ),
            pytest.param(
                {
                    "review_decision": "",
                    "latest_reviews": [{"state": "CHANGES_REQUESTED", "author": {"login": "dependabot[bot]"}}],
                    "all_threads_resolved": True,
                    "ci_status": "passed",
                    "mergeable": "MERGEABLE",
                    "superseded_by_new_commit": True,
                },
                ComputedPRState.APPROVED_CI_GREEN,
                id="bot-changes-requested-in-latest-reviews-threads-resolved",
            ),
            pytest.param(
                {
                    "review_decision": "CHANGES_REQUESTED",
                    "latest_reviews": [_BOT_CR, {"state": "APPROVED", "author": {"login": "alice"}}],
                    "all_threads_resolved": True,
                    "ci_status": "passed",
                    "mergeable": "MERGEABLE",
                    "superseded_by_new_commit": True,
                },
                ComputedPRState.APPROVED_CI_GREEN,
                id="bot-changes-requested-human-approved-threads-resolved",
            ),
            pytest.param(
                {
                    "review_decision": "",
                    "latest_reviews": [_HUMAN_CR],
                    "all_threads_resolved": True,
                    "ci_status": "passed",
                    "mergeable": "MERGEABLE",
                },
                ComputedPRState.CHANGES_REQUESTED,
                id="human-changes-requested-in-latest-reviews-stays-blocked",
            ),
            # Human CHANGES_REQUESTED reviews are never auto-superseded, unlike bot reviews.
            pytest.param(
                {
                    "review_decision": "CHANGES_REQUESTED",
                    "latest_reviews": [_HUMAN_CR],
                    "all_threads_resolved": True,
                    "ci_status": "passed",
                    "mergeable": "MERGEABLE",
                    "superseded_by_new_commit": True,
                },
                ComputedPRState.CHANGES_REQUESTED,
                id="human-changes-requested-never-superseded-by-new-commit",
            ),
            # all_threads_resolved=None (unknown) must never authorize unblocking.
            pytest.param(
                {
                    "review_decision": "CHANGES_REQUESTED",
                    "latest_reviews": [_BOT_CR],
                    "all_threads_resolved": None,
                    "ci_status": "passed",
                    "mergeable": "MERGEABLE",
                    "superseded_by_new_commit": True,
                },
                ComputedPRState.CHANGES_REQUESTED,
                id="unknown-threads-never-unblocks-bot-review",
            ),
        ],
    )
    def test_computed_state(self, kwargs: dict[str, object], expected: ComputedPRState) -> None:
        assert _state(**kwargs) == expected


class TestConflictedState:
    """mergeable == CONFLICTING must short-circuit compute_pr_state() to CONFLICTED."""

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            pytest.param(
                {"review_decision": "APPROVED", "ci_status": "passed", "mergeable": "CONFLICTING"},
                ComputedPRState.CONFLICTED,
                id="conflicting-outranks-approved",
            ),
            pytest.param(
                {"ci_status": "passed", "mergeable": "CONFLICTING", "latest_reviews": None},
                ComputedPRState.CONFLICTED,
                id="conflicting-outranks-ci-green",
            ),
            pytest.param(
                {"review_decision": "CHANGES_REQUESTED", "ci_status": "passed", "mergeable": "CONFLICTING"},
                ComputedPRState.CONFLICTED,
                id="conflicting-outranks-changes-requested",
            ),
            pytest.param(
                {"ci_status": "failed", "mergeable": "CONFLICTING"},
                ComputedPRState.CONFLICTED,
                id="conflicting-outranks-ci-failed",
            ),
            pytest.param(
                {"mergeable": "CONFLICTING"}, ComputedPRState.CONFLICTED, id="conflicting-outranks-awaiting-review"
            ),
            pytest.param(
                {"review_decision": "APPROVED", "ci_status": "passed", "mergeable": "UNKNOWN"},
                ComputedPRState.APPROVED,
                id="unknown-mergeable-is-not-conflicted",
            ),
            pytest.param(
                {"is_draft": True, "mergeable": "CONFLICTING"}, ComputedPRState.DRAFT, id="draft-precedence-unchanged"
            ),
        ],
    )
    def test_conflict_precedence(self, kwargs: dict[str, object], expected: ComputedPRState) -> None:
        assert _state(**kwargs) == expected


class TestParseLinkedIssue:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param("Some text\nCloses #42\nMore text", 42, id="closes"),
            pytest.param("Fixes #123", 123, id="fixes"),
            pytest.param("Resolves #7", 7, id="resolves"),
            pytest.param("closes #99", 99, id="case-insensitive"),
            pytest.param("No issue linked here", None, id="no-match"),
            pytest.param(None, None, id="none-body"),
            pytest.param("", None, id="empty-body"),
            pytest.param("Closes #10\nFixes #20", 10, id="first-match"),
            pytest.param(
                "## JIRA\n[RHCLOUD-48767](https://issues.redhat.com/browse/RHCLOUD-48767)",
                48767,
                id="jira-markdown-link",
            ),
            pytest.param("JIRA: https://issues.redhat.com/browse/RHCLOUD-48767", 48767, id="jira-plain-link"),
            pytest.param(
                "Closes #42\nJIRA: https://issues.redhat.com/browse/RHCLOUD-48767", 42, id="github-preferred-over-jira"
            ),
            pytest.param("[ABC-123](https://jira.example.com/browse/ABC-123)", 123, id="jira-multi-letter-project"),
            pytest.param("Part of #42\n\nNot done in this PR:\n- Some thing", 42, id="part-of-marker"),
            # "Part of #N" is also this repo's epic-linkage marker, so a body that mentions an epic that way
            # before its own "Closes #N" must still resolve to the PR's own issue, not the epic.
            pytest.param(
                "## Summary\n- Part of #1300 (epic rollout)\n\n## Issue\n\nCloses #42",
                42,
                id="closes-preferred-over-epic-part-of",
            ),
            # Neither "Part of" reference is a closing keyword, so the plain whole-body search would otherwise
            # return the epic mentioned first in "## Summary" instead of the PR's own issue in "## Issue".
            pytest.param(
                "## Summary\n- Part of #1300 rollout: adds the parser\n\n## Issue\n\nPart of #42",
                42,
                id="part-of-in-issue-section-preferred-over-epic-mention-in-summary",
            ),
        ],
    )
    def test_parse_linked_issue(self, body: str | None, expected: int | None) -> None:
        assert parse_linked_issue(body) == expected


def _linked_raw(body: str, title: str, branch: str) -> dict:
    return {"closingIssuesReferences": [], "body": body, "title": title, "headRefName": branch}


class TestExtractLinkedIssue:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(
                {"closingIssuesReferences": [{"number": 42}], "body": "Fixes #99", "title": "feat(#77)"},
                42,
                id="closing-references-preferred",
            ),
            pytest.param(_linked_raw("Closes #88", "feat(#99)", "feat/issue-77"), 88, id="body-fallback"),
            pytest.param(
                _linked_raw("No link here", "feat(#374): merge conflict gate", "feat/issue-111"),
                374,
                id="title-fallback-parens",
            ),
            pytest.param(_linked_raw("", "[#55] fix something", "feat/issue-222"), 55, id="title-fallback-brackets"),
            pytest.param(_linked_raw("", "some fix", "feat/issue-314"), 314, id="branch-fallback"),
            pytest.param(_linked_raw("", "some fix", "feat/notissue-314"), None, id="branch-requires-segment-boundary"),
            pytest.param(
                _linked_raw("", "some fix", "feat/issue-314draft"), None, id="branch-requires-suffix-boundary"
            ),
            pytest.param(_linked_raw("", "some fix", "feat/issue-314-fix"), 314, id="branch-with-suffix-delimiter"),
            pytest.param(_linked_raw("nothing", "generic", "main"), None, id="no-match"),
            pytest.param({"closingIssuesReferences": None, "body": "Fixes #10"}, 10, id="empty-closing-refs-is-none"),
            pytest.param(
                {"closingIssuesReferences": [], "body": "", "title": "fix: handle (500) errors"},
                None,
                id="title-number-in-parens-without-hash-is-not-issue",
            ),
            pytest.param(
                _linked_raw("", "[RHCLOUD-49651] feat: eval() security fix", "feat/some-branch"),
                49651,
                id="title-jira-key-brackets",
            ),
            pytest.param(_linked_raw("", "[ABC-7] fix something", "main"), 7, id="title-jira-key-with-short-project"),
            pytest.param(
                _linked_raw("", "some fix", "feat/RHCLOUD-49651-turnpike-eval-security"), 49651, id="branch-jira-key"
            ),
            pytest.param(_linked_raw("", "some fix", "fix/ABC-123"), 123, id="branch-jira-key-at-end"),
            pytest.param(
                _linked_raw("", "some fix", "feat/rhcloud-49651-stuff"),
                None,
                id="branch-jira-key-does-not-match-lowercase",
            ),
            # A 'Part of #N' PR (sova/core/steps/_pr_body.py) still links to its issue, even though it is
            # deliberately not a GitHub auto-close keyword.
            pytest.param(
                {"closingIssuesReferences": [], "body": "Part of #42\n\nNot done in this PR:\n- Some thing"},
                42,
                id="part-of-links-without-closing-keyword",
            ),
            pytest.param(
                _linked_raw(
                    "JIRA: https://redhat.atlassian.net/browse/RHCLOUD-48767",
                    "[RHCLOUD-49651] feat: something",
                    "feat/RHCLOUD-49651-stuff",
                ),
                48767,
                id="body-preferred-over-title-jira-key",
            ),
        ],
    )
    def test_extract_linked_issue(self, raw: dict, expected: int | None) -> None:
        assert _extract_linked_issue(raw) == expected


def _check(conclusion: str, status: str = "COMPLETED") -> dict[str, str]:
    return {"status": status, "conclusion": conclusion}


def _status_context(state: str, **extra: str) -> dict[str, str]:
    return {"__typename": "StatusContext", "state": state, **extra}


class TestSummarizeCi:
    @pytest.mark.parametrize(
        ("checks", "expected"),
        [
            pytest.param(None, "none", id="none-checks"),
            pytest.param([], "none", id="empty-checks"),
            pytest.param([_check("SUCCESS")], "passed", id="all-success"),
            pytest.param([_check("SUCCESS"), _check("", status="IN_PROGRESS")], "pending", id="pending"),
            pytest.param([_check("FAILURE")], "failed", id="failed"),
            pytest.param(
                [_status_context("SUCCESS", context="CodeRabbit"), _check("SUCCESS")],
                "passed",
                id="status-context-success",
            ),
            pytest.param(
                [_status_context("PENDING", context="CodeRabbit"), _check("SUCCESS")],
                "pending",
                id="status-context-pending",
            ),
            pytest.param([_check("SKIPPED")], "passed", id="skipped-only"),
            pytest.param([_check("SKIPPED"), _check("SUCCESS")], "passed", id="skipped-plus-success"),
            pytest.param([_check("", status="IN_PROGRESS"), _check("FAILURE")], "failed", id="failed-beats-pending"),
            pytest.param([_check("ERROR")], "failed", id="error-conclusion"),
            pytest.param([_check("TIMED_OUT")], "failed", id="timed-out"),
            pytest.param([_check("", status="QUEUED")], "pending", id="queued"),
            pytest.param([_check("")], "passed", id="completed-no-conclusion"),
            pytest.param([_check("NEUTRAL")], "passed", id="neutral"),
            pytest.param([_status_context("FAILURE")], "failed", id="status-context-failure"),
            pytest.param([_status_context("ERROR")], "failed", id="status-context-error"),
            pytest.param([_check("", status="UNKNOWN")], "pending", id="unknown-status-defaults-pending"),
            pytest.param([_check("CANCELLED")], "passed", id="cancelled"),
            pytest.param([_check("ACTION_REQUIRED")], "failed", id="action-required"),
            pytest.param([_check("STALE")], "failed", id="stale"),
            pytest.param(
                [_status_context("SOMETHING_ELSE"), _check("SUCCESS")],
                "passed",
                id="status-context-unknown-state-ignored",
            ),
            # StatusContext items with unknown states are skipped, leaving the state set empty, which falls
            # through to "none".
            pytest.param(
                [_status_context("EXPECTED"), _status_context("RANDOM")],
                "none",
                id="only-unknown-status-contexts-is-none",
            ),
            # An unknown CheckRun status adds "pending", unlike an unknown StatusContext state.
            pytest.param([_check("", status="UNKNOWN")], "pending", id="unknown-check-run-status-defaults-to-pending"),
        ],
    )
    def test_summarize_ci(self, checks: list[dict[str, str]] | None, expected: str) -> None:
        assert _summarize_ci(checks) == expected


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

    @pytest.mark.parametrize(
        ("overrides", "field", "expected"),
        [
            pytest.param({"headRefOid": "abc1234"}, "head_sha", "abc1234", id="head-sha-populated"),
            pytest.param({}, "head_sha", "", id="head-sha-missing-defaults-empty"),
            pytest.param({"mergeStateStatus": "BEHIND"}, "merge_state", "BEHIND", id="merge-state-populated"),
            pytest.param({}, "merge_state", "", id="merge-state-missing-defaults-empty"),
        ],
    )
    def test_optional_string_field(self, overrides: dict[str, object], field: str, expected: str) -> None:
        result = _enrich_pr(self._raw_pr(**overrides), time.time())
        assert result[field] == expected

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
        assert result["state_label"] == "Draft PR"

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

    def test_zero_threads_bot_cr_review_unblocks_when_superseded(self) -> None:
        """A body-only bot review (zero threads) is vacuously resolved, and unblocks
        once a new commit supersedes it."""
        raw = self._raw_pr(
            reviewDecision="CHANGES_REQUESTED",
            latestReviews=[{"state": "CHANGES_REQUESTED", "author": {"login": "coderabbitai[bot]"}}],
            statusCheckRollup=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            _thread_counts=(0, 0),
            _superseded_by_new_commit=True,
        )
        result = _enrich_pr(raw, time.time())
        assert result["computed_state"] == ComputedPRState.APPROVED_CI_GREEN

    def test_zero_threads_bot_cr_review_stays_blocked_without_new_commit(self) -> None:
        """Regression guard: zero threads alone must not unblock a bot review that
        has not been superseded by a new commit."""
        raw = self._raw_pr(
            reviewDecision="CHANGES_REQUESTED",
            latestReviews=[{"state": "CHANGES_REQUESTED", "author": {"login": "coderabbitai[bot]"}}],
            statusCheckRollup=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            _thread_counts=(0, 0),
        )
        result = _enrich_pr(raw, time.time())
        assert result["computed_state"] == ComputedPRState.CHANGES_REQUESTED

    def test_unknown_thread_counts_stays_blocked_and_reports_none(self) -> None:
        """When thread counts could not be retrieved, thread_total/thread_resolved
        surface as None (unknown) rather than a silent zero, and the PR stays
        blocked rather than being treated as vacuously resolved."""
        raw = self._raw_pr(
            reviewDecision="CHANGES_REQUESTED",
            latestReviews=[{"state": "CHANGES_REQUESTED", "author": {"login": "coderabbitai[bot]"}}],
            statusCheckRollup=[{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            _thread_counts=None,
            _superseded_by_new_commit=True,
        )
        result = _enrich_pr(raw, time.time())
        assert result["computed_state"] == ComputedPRState.CHANGES_REQUESTED
        assert result["thread_total"] is None
        assert result["thread_resolved"] is None

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


class TestExtractPrLabels:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(
                {"labels": [{"name": "bug"}, {"name": "priority: high"}]},
                ["bug", "priority: high"],
                id="extracts-label-names",
            ),
            pytest.param({}, [], id="missing-labels-returns-empty-list"),
            pytest.param({"labels": None}, [], id="none-labels-returns-empty-list"),
        ],
    )
    def test_extract_pr_labels(self, raw: dict, expected: list[str]) -> None:
        assert _extract_pr_labels(raw) == expected


class TestExtractPrAssignees:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param({"assignees": [{"login": "alice"}, {"login": "bob"}]}, ["alice", "bob"], id="extracts-logins"),
            pytest.param(
                {"assignees": [{"login": "alice"}, {}, {"login": ""}]},
                ["alice"],
                id="filters-out-entries-without-login",
            ),
            pytest.param({}, [], id="missing-assignees-returns-empty-list"),
            pytest.param({"assignees": None}, [], id="none-assignees-returns-empty-list"),
        ],
    )
    def test_extract_pr_assignees(self, raw: dict, expected: list[str]) -> None:
        assert _extract_pr_assignees(raw) == expected


class TestCountPrCommits:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param({"commits": [{"oid": "a"}, {"oid": "b"}, {"oid": "c"}]}, 3, id="counts-commit-list"),
            pytest.param({}, 0, id="missing-commits-returns-zero"),
            pytest.param({"commits": None}, 0, id="none-commits-returns-zero"),
            pytest.param({"commits": "not-a-list"}, 0, id="non-list-commits-returns-zero"),
        ],
    )
    def test_count_pr_commits(self, raw: dict, expected: int) -> None:
        assert _count_pr_commits(raw) == expected


# ---------------------------------------------------------------------------
# get_pr_review_data
# ---------------------------------------------------------------------------


class TestGetPrReviewData:
    """Tests for get_pr_review_data in git/pr.py."""

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self) -> None:
        from sova.git.pr import get_pr_review_data

        result = await get_pr_review_data([], repo="owner/repo")
        assert result == {}

    @pytest.mark.asyncio
    async def test_parses_thread_counts(self, monkeypatch) -> None:
        from sova.git.pr import get_pr_review_data

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

        result = await get_pr_review_data([10, 20], repo="owner/repo")
        assert (result[10].thread_total, result[10].thread_resolved) == (3, 2)
        assert (result[20].thread_total, result[20].thread_resolved) == (1, 0)

    @pytest.mark.asyncio
    async def test_returns_none_per_pr_on_failure(self, monkeypatch) -> None:
        from sova.git.pr import get_pr_review_data

        mock_run = AsyncMock()
        mock_run.return_value.success = False
        mock_run.return_value.stderr = "error"
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result == {10: None}

    @pytest.mark.asyncio
    async def test_returns_none_per_pr_on_bad_json(self, monkeypatch) -> None:
        from sova.git.pr import get_pr_review_data

        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = "not json"
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result == {10: None}

    @pytest.mark.asyncio
    async def test_handles_missing_pr_data(self, monkeypatch) -> None:
        """A PR alias missing entirely from the GraphQL response is unknown, not (0, 0)."""
        from sova.git.pr import get_pr_review_data

        graphql_response = {"data": {"repository": {}}}
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result[10] is None

    @pytest.mark.asyncio
    async def test_handles_null_aliased_pr_data(self, monkeypatch) -> None:
        """A PR alias present but null (e.g. force-deleted mid-query) is unknown, not (0, 0)."""
        from sova.git.pr import get_pr_review_data

        graphql_response = {"data": {"repository": {"pr10": None}}}
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result[10] is None

    @pytest.mark.asyncio
    async def test_handles_data_null_response(self, monkeypatch) -> None:
        """A full-response GraphQL error (data: null) is unknown for every requested PR."""
        from sova.git.pr import get_pr_review_data

        graphql_response = {"data": None, "errors": [{"message": "something went wrong"}]}
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result[10] is None

    @pytest.mark.asyncio
    async def test_detects_bot_review_superseded_by_new_commit(self, monkeypatch) -> None:
        """get_pr_review_data surfaces head SHA and the bot CR review's commit SHA."""
        from sova.git.pr import get_pr_review_data

        graphql_response = {
            "data": {
                "repository": {
                    "pr10": {
                        "headRefOid": "new-sha",
                        "reviewThreads": {"totalCount": 0, "nodes": []},
                        "reviews": {
                            "nodes": [
                                {
                                    "state": "CHANGES_REQUESTED",
                                    "commit": {"oid": "old-sha"},
                                    "author": {"login": "coderabbitai[bot]"},
                                    "submittedAt": "2026-01-01T00:00:00Z",
                                }
                            ]
                        },
                    }
                }
            }
        }
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result[10].head_sha == "new-sha"
        assert result[10].bot_cr_commit_sha == "old-sha"
        assert result[10].thread_total == 0

    @pytest.mark.asyncio
    async def test_ignores_human_cr_review_commit_sha(self, monkeypatch) -> None:
        """Only bot CHANGES_REQUESTED reviews populate bot_cr_commit_sha."""
        from sova.git.pr import get_pr_review_data

        graphql_response = {
            "data": {
                "repository": {
                    "pr10": {
                        "headRefOid": "new-sha",
                        "reviewThreads": {"totalCount": 0, "nodes": []},
                        "reviews": {
                            "nodes": [
                                {
                                    "state": "CHANGES_REQUESTED",
                                    "commit": {"oid": "old-sha"},
                                    "author": {"login": "alice"},
                                    "submittedAt": "2026-01-01T00:00:00Z",
                                }
                            ]
                        },
                    }
                }
            }
        }
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result[10].bot_cr_commit_sha == ""

    @pytest.mark.asyncio
    async def test_review_history_carries_lowercased_author(self, monkeypatch) -> None:
        """Each review_history entry's author must be fetched and lowercased so
        parse_review_history() can filter forged markers by login."""
        from sova.git.pr import get_pr_review_data

        graphql_response = {
            "data": {
                "repository": {
                    "pr10": {
                        "headRefOid": "new-sha",
                        "reviewThreads": {"totalCount": 0, "nodes": []},
                        "reviews": {"nodes": []},
                        "history": {
                            "pageInfo": {"hasPreviousPage": False},
                            "nodes": [
                                {
                                    "state": "APPROVED",
                                    "body": "<!-- sova-review: approve sha=abc1234 -->",
                                    "submittedAt": "2026-01-01T00:00:00Z",
                                    "author": {"login": "SOVA-Bot"},
                                }
                            ],
                        },
                    }
                }
            }
        }
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result[10].review_history[0]["author"] == "sova-bot"
        assert result[10].review_history_truncated is False

    @pytest.mark.asyncio
    async def test_review_history_truncation_is_surfaced(self, monkeypatch) -> None:
        """A last-30 window missing older reviews must set review_history_truncated."""
        from sova.git.pr import get_pr_review_data

        graphql_response = {
            "data": {
                "repository": {
                    "pr10": {
                        "headRefOid": "new-sha",
                        "reviewThreads": {"totalCount": 0, "nodes": []},
                        "reviews": {"nodes": []},
                        "history": {
                            "pageInfo": {"hasPreviousPage": True},
                            "nodes": [],
                        },
                    }
                }
            }
        }
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result[10].review_history_truncated is True

    @pytest.mark.asyncio
    async def test_detects_classic_coderabbit_login_without_bot_suffix(self, monkeypatch) -> None:
        """CodeRabbit's classic login ('coderabbitai', no [bot] suffix) must still
        populate bot_cr_commit_sha, matching DEFAULT_CODERABBIT_AUTHORS."""
        from sova.git.pr import get_pr_review_data

        graphql_response = {
            "data": {
                "repository": {
                    "pr10": {
                        "headRefOid": "new-sha",
                        "reviewThreads": {"totalCount": 0, "nodes": []},
                        "reviews": {
                            "nodes": [
                                {
                                    "state": "CHANGES_REQUESTED",
                                    "commit": {"oid": "old-sha"},
                                    "author": {"login": "coderabbitai"},
                                    "submittedAt": "2026-01-01T00:00:00Z",
                                }
                            ]
                        },
                    }
                }
            }
        }
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result[10].bot_cr_commit_sha == "old-sha"

    @pytest.mark.asyncio
    async def test_truncated_threads_page_is_unknown(self, monkeypatch) -> None:
        """More than 100 threads (hasNextPage=True) makes the whole PR unknown,
        since totalCount minus the fetched-nodes resolved count could be wrong."""
        from sova.git.pr import get_pr_review_data

        graphql_response = {
            "data": {
                "repository": {
                    "pr10": {
                        "headRefOid": "new-sha",
                        "reviewThreads": {
                            "totalCount": 101,
                            "pageInfo": {"hasNextPage": True},
                            "nodes": [{"isResolved": True}] * 100,
                        },
                        "reviews": {"nodes": []},
                    }
                }
            }
        }
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result[10] is None

    @pytest.mark.asyncio
    async def test_truncated_reviews_page_blocks_supersession(self, monkeypatch) -> None:
        """A truncated reviews window (hasPreviousPage=True) may have pushed an
        older bot's outstanding CR review out of the last-10 slice, so
        supersession cannot be confirmed even when the visible bot's SHA differs
        from head."""
        from sova.git.pr import get_pr_review_data

        graphql_response = {
            "data": {
                "repository": {
                    "pr10": {
                        "headRefOid": "new-sha",
                        "reviewThreads": {"totalCount": 0, "nodes": []},
                        "reviews": {
                            "pageInfo": {"hasPreviousPage": True},
                            "nodes": [
                                {
                                    "state": "CHANGES_REQUESTED",
                                    "commit": {"oid": "old-sha"},
                                    "author": {"login": "coderabbitai[bot]"},
                                    "submittedAt": "2026-01-01T00:00:00Z",
                                }
                            ],
                        },
                    }
                }
            }
        }
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result[10].bot_cr_superseded is False

    @pytest.mark.asyncio
    async def test_stale_cr_superseded_by_approval_does_not_block(self, monkeypatch) -> None:
        """A bot whose latest opinionated review is now APPROVED must not have
        that review counted as an outstanding CR. Without client-side state
        filtering, coderabbitai's approval (submitted at head) would be
        misread as an unresolved CR whose SHA matches head, forcing
        bot_cr_superseded to False and blocking the PR even though only
        sourcery-ai's older, genuinely-stale CR remains."""
        from sova.git.pr import get_pr_review_data

        graphql_response = {
            "data": {
                "repository": {
                    "pr10": {
                        "headRefOid": "new-sha",
                        "reviewThreads": {"totalCount": 0, "nodes": []},
                        "reviews": {
                            "nodes": [
                                {
                                    "state": "APPROVED",
                                    "commit": {"oid": "new-sha"},
                                    "author": {"login": "coderabbitai[bot]"},
                                    "submittedAt": "2026-01-02T00:00:00Z",
                                },
                                {
                                    "state": "CHANGES_REQUESTED",
                                    "commit": {"oid": "old-sha"},
                                    "author": {"login": "sourcery-ai[bot]"},
                                    "submittedAt": "2026-01-01T00:00:00Z",
                                },
                            ]
                        },
                    }
                }
            }
        }
        mock_run = AsyncMock()
        mock_run.return_value.success = True
        mock_run.return_value.stdout = json.dumps(graphql_response)
        monkeypatch.setattr("sova.git.pr.run", mock_run)
        monkeypatch.setattr("sova.git.pr.resolve_gh_env", AsyncMock(return_value=None))

        result = await get_pr_review_data([10], repo="owner/repo")
        assert result[10].bot_cr_commit_sha == "old-sha"
        assert result[10].bot_cr_superseded is True


# ---------------------------------------------------------------------------
# _age_seconds / _extract_review_logins helpers
# ---------------------------------------------------------------------------


class TestAgeSeconds:
    def test_valid_timestamp(self) -> None:
        from datetime import datetime, timezone

        dt = datetime(2025, 6, 15, 6, 0, 0, tzinfo=timezone.utc)
        now = dt.timestamp() + 100
        assert _age_seconds("2025-06-15T06:00:00Z", now) == 100

    @pytest.mark.parametrize(
        "timestamp",
        [pytest.param("", id="empty-string"), pytest.param("not-a-date", id="invalid-date")],
    )
    def test_unparseable_timestamp_is_zero(self, timestamp: str) -> None:
        assert _age_seconds(timestamp, 1_750_000_000.0) == 0

    def test_zulu_suffix(self) -> None:
        result = _age_seconds("2026-01-01T00:00:00Z", time.time())
        assert result > 0


class TestExtractReviewLogins:
    @pytest.mark.parametrize(
        ("reviews", "expected"),
        [
            pytest.param(
                [{"author": {"login": "alice"}}, {"author": {"login": "bob"}}, {"author": {"login": "alice"}}],
                ["alice", "bob"],
                id="extracts-unique-logins",
            ),
            pytest.param(None, [], id="none-reviews"),
            pytest.param([], [], id="empty-reviews"),
            pytest.param([{"author": None}, {"author": {"login": "bob"}}], ["bob"], id="missing-author"),
            pytest.param([{"author": {"login": ""}}, {"author": {"login": "bob"}}], ["bob"], id="empty-login"),
        ],
    )
    def test_extract_review_logins(self, reviews: list[dict] | None, expected: list[str]) -> None:
        assert _extract_review_logins(reviews) == expected


class TestIsBotReview:
    @pytest.mark.parametrize(
        ("review", "expected"),
        [
            pytest.param({"author": {"login": "coderabbitai[bot]"}}, True, id="bot-suffix"),
            pytest.param({"author": {"login": "dependabot[bot]"}}, True, id="dependabot"),
            pytest.param({"author": {"login": "alice"}}, False, id="human-reviewer"),
            pytest.param({"author": None}, False, id="no-author"),
            pytest.param({"author": {"login": ""}}, False, id="empty-login"),
            pytest.param({"author": {}}, False, id="no-login-field"),
        ],
    )
    def test_is_bot_review(self, review: dict, expected: bool) -> None:
        assert _is_bot_review(review) is expected


class TestExtractLatestApprovalAt:
    @pytest.mark.parametrize(
        ("reviews", "expected"),
        [
            pytest.param(
                [
                    {"state": "APPROVED", "submittedAt": "2026-07-18T10:00:00Z"},
                    {"state": "APPROVED", "submittedAt": "2026-07-20T12:00:00Z"},
                    {"state": "COMMENTED", "submittedAt": "2026-07-21T08:00:00Z"},
                ],
                "2026-07-20T12:00:00Z",
                id="picks-latest-approved",
            ),
            pytest.param(
                [{"state": "CHANGES_REQUESTED", "submittedAt": "2026-07-20T10:00:00Z"}], None, id="no-approvals"
            ),
            pytest.param(None, None, id="none-reviews"),
            pytest.param([], None, id="empty-reviews"),
            pytest.param([{"state": "APPROVED"}], None, id="missing-submitted-at"),
            # A bot approval (e.g. dependabot) must never count as a human sign-off.
            pytest.param(
                [{"state": "APPROVED", "submittedAt": "2026-07-20T12:00:00Z", "author": {"login": "dependabot[bot]"}}],
                None,
                id="excludes-bot-suffix-approval",
            ),
            # CodeRabbit's classic-bot login has no [bot] suffix; must still be excluded.
            pytest.param(
                [{"state": "APPROVED", "submittedAt": "2026-07-20T12:00:00Z", "author": {"login": "coderabbitai"}}],
                None,
                id="excludes-coderabbit-classic-login-approval",
            ),
            pytest.param(
                [
                    {"state": "APPROVED", "submittedAt": "2026-07-21T12:00:00Z", "author": {"login": "coderabbitai"}},
                    {"state": "APPROVED", "submittedAt": "2026-07-20T10:00:00Z", "author": {"login": "alice"}},
                ],
                "2026-07-20T10:00:00Z",
                id="human-approval-after-bot-approval-still-counted",
            ),
        ],
    )
    def test_extract_latest_approval_at(self, reviews: list[dict] | None, expected: str | None) -> None:
        assert _extract_latest_approval_at(reviews) == expected


# ---------------------------------------------------------------------------
# get_unresolved_thread_count helper
# ---------------------------------------------------------------------------


class TestGetUnresolvedThreadCount:
    @pytest.mark.parametrize(
        ("pr", "expected"),
        [
            pytest.param({"thread_total": 5, "thread_resolved": 3}, 2, id="some-unresolved"),
            pytest.param({"thread_total": 5, "thread_resolved": 5}, 0, id="all-resolved"),
            pytest.param({"thread_total": 0, "thread_resolved": 0}, 0, id="no-threads"),
            # Absent thread-count keys are treated as zero, not unknown, so a synthetic dict without them is
            # never reclassified as an unknown state.
            pytest.param({}, 0, id="missing-keys"),
            # Thread-count keys explicitly set to None are genuinely unknown.
            pytest.param({"thread_total": None, "thread_resolved": None}, None, id="explicit-none-is-unknown"),
            pytest.param({"thread_total": 3, "thread_resolved": 5}, 0, id="resolved-exceeds-total"),
        ],
    )
    def test_unresolved_thread_count(self, pr: dict[str, int | None], expected: int | None) -> None:
        assert get_unresolved_thread_count(pr) == expected


# ---------------------------------------------------------------------------
# Coverage gap tests for uncovered paths
# ---------------------------------------------------------------------------


class TestExtractAllLinkedIssues:
    """Tests for _extract_all_linked_issues helper."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Early return when closingIssuesReferences has items.
            pytest.param(
                {"closingIssuesReferences": [{"number": 42}, {"number": 43}, {"number": None}], "body": "Closes #99"},
                [42, 43],
                id="returns-closing-references-when-present",
            ),
            pytest.param(
                {"closingIssuesReferences": [{"number": None}, {"number": 10}, {"foo": "bar"}]},
                [10],
                id="filters-none-from-closing-references",
            ),
            # Falls back to parse_linked_issue when closingIssuesReferences is empty.
            pytest.param(
                {"closingIssuesReferences": [], "body": "Fixes #88"}, [88], id="falls-back-to-body-when-no-closing-refs"
            ),
            pytest.param(
                {"closingIssuesReferences": [], "body": "No issue here"},
                [],
                id="returns-empty-when-no-refs-and-no-body-match",
            ),
            # With no closingIssuesReferences and no body keyword (e.g. a future 'Part of #N' phrasing this
            # function doesn't recognize), must still resolve via title/branch, matching
            # _extract_linked_issue's fallback chain rather than silently returning [].
            pytest.param(
                _linked_raw("", "some fix", "feat/issue-314"),
                [314],
                id="falls-back-to-title-and-branch-like-extract-linked-issue",
            ),
            pytest.param({"closingIssuesReferences": [], "body": "Part of #88"}, [88], id="part-of-in-body-is-linked"),
        ],
    )
    def test_extract_all_linked_issues(self, raw: dict, expected: list[int]) -> None:
        assert _extract_all_linked_issues(raw) == expected


class TestPrimaryClosingReference:
    """With several closingIssuesReferences, the ## Issue section then the branch pick the primary."""

    @staticmethod
    def _raw(body: str = "", branch: str = "", numbers: tuple[int, ...] = (12, 1354)) -> dict:
        return {
            "number": 7,
            "closingIssuesReferences": [{"number": n} for n in numbers],
            "body": body,
            "headRefName": branch,
        }

    @pytest.mark.parametrize(
        ("body", "branch", "numbers", "expected"),
        [
            pytest.param(
                "## Summary\nmentions Closes #12\n\n## Issue\n\nCloses #1354",
                "",
                (12, 1354),
                1354,
                id="issue-section-beats-first-listed",
            ),
            pytest.param(
                "## Issue\n\nCloses #1354", "feat/issue-12", (12, 1354), 1354, id="issue-section-wins-over-branch"
            ),
            pytest.param(
                "## Issue\n\nCloses #12\nCloses #1354",
                "feat/issue-1354",
                (12, 1354),
                1354,
                id="branch-breaks-tie-inside-issue-section",
            ),
            pytest.param(
                "## Issue\n\nCloses #12\nCloses #1354",
                "refactor/cleanup",
                (12, 1354),
                12,
                id="section-naming-several-without-branch-keeps-first-listed",
            ),
            pytest.param(
                "## Issue\n\nPart of #1354\n\nNot done in this PR:\n- A",
                "",
                (12, 1354),
                1354,
                id="part-of-section-names-the-primary",
            ),
            pytest.param(
                "Closes #12\nCloses #1354",
                "fix/issue-1354",
                (12, 1354),
                1354,
                id="branch-breaks-tie-without-issue-section",
            ),
            pytest.param(
                "Closes #12\nCloses #1354",
                "refactor/cleanup",
                (12, 1354),
                12,
                id="branch-without-issue-number-leaves-first-listed",
            ),
            pytest.param(
                "## Issue\n\nCloses #999",
                "feat/issue-1354",
                (12, 1354),
                1354,
                id="section-issue-not-listed-by-github-is-not-a-candidate",
            ),
            pytest.param(
                "", "feat/issue-999", (12, 1354), 12, id="branch-issue-not-listed-by-github-is-not-a-candidate"
            ),
            pytest.param(
                "## Issue\n\n[PROJ-7](https://jira.example.com/browse/PROJ-7)",
                "feat/issue-1354",
                (12, 1354),
                1354,
                id="jira-only-section-names-no-issue",
            ),
            pytest.param("## Issue\n\nsee `Closes #1354`", "", (12, 1354), 12, id="code-span-in-section-is-not-read"),
            pytest.param("## Issue\n\nCloses #1354", "feat/issue-1354", (12,), 12, id="single-reference-is-unchanged"),
            pytest.param(
                "## issue\n\nCloses #1354", "", (12, 1354), 12, id="lowercase-issue-heading-is-not-the-section"
            ),
        ],
    )
    def test_primary_closing_reference(self, body: str, branch: str, numbers: tuple[int, ...], expected: int) -> None:
        raw = self._raw(body=body, branch=branch, numbers=numbers)
        assert _extract_linked_issue(raw) == expected

    def test_entries_without_number_are_skipped(self) -> None:
        raw = {
            "closingIssuesReferences": [{"foo": "bar"}, {"number": 12}, {"number": 1354}],
            "body": "## Issue\n\nCloses #1354",
        }
        assert _extract_linked_issue(raw) == 1354

    @pytest.fixture(autouse=True)
    def _fresh_tie_log(self) -> None:
        _logged_closing_ties.clear()

    def test_disagreement_is_logged(self) -> None:
        raw = self._raw(body="## Issue\n\nCloses #1354")
        with patch("sova.dashboard.services.pr_service.log") as mock_log:
            _extract_linked_issue(raw)
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args.args[0] == "pr_service.multiple_closing_references"
        assert mock_log.warning.call_args.kwargs["chosen"] == 1354
        assert mock_log.warning.call_args.kwargs["first_listed"] == 12
        assert mock_log.warning.call_args.kwargs["signal"] == "section"

    def test_untied_choice_is_logged(self) -> None:
        raw = self._raw()
        with patch("sova.dashboard.services.pr_service.log") as mock_log:
            assert _extract_linked_issue(raw) == 12
        mock_log.info.assert_called_once()
        assert mock_log.info.call_args.kwargs["signal"] == "none"
        mock_log.warning.assert_not_called()

    def test_primary_listed_first_still_logs_the_extra_closing_reference(self) -> None:
        raw = self._raw(body="## Issue\n\nCloses #1340", branch="fix/issue-1340", numbers=(1340, 12))
        with patch("sova.dashboard.services.pr_service.log") as mock_log:
            assert _extract_linked_issue(raw) == 1340
        mock_log.info.assert_called_once()
        assert mock_log.info.call_args.args[0] == "pr_service.multiple_closing_references"
        assert mock_log.info.call_args.kwargs["issues"] == [1340, 12]
        mock_log.warning.assert_not_called()

    def test_repeated_resolution_of_the_same_pr_head_logs_once(self) -> None:
        raw = {**self._raw(body="## Issue\n\nCloses #1354"), "headRefOid": "abc"}
        with patch("sova.dashboard.services.pr_service.log") as mock_log:
            _extract_linked_issue(raw)
            _extract_all_linked_issues(raw)
            _extract_linked_issue(raw)
        mock_log.warning.assert_called_once()

    def test_new_head_sha_logs_again(self) -> None:
        raw = self._raw(body="## Issue\n\nCloses #1354")
        with patch("sova.dashboard.services.pr_service.log") as mock_log:
            _extract_linked_issue({**raw, "headRefOid": "abc"})
            _extract_linked_issue({**raw, "headRefOid": "def"})
        assert mock_log.warning.call_count == 2

    def test_all_linked_issues_lists_primary_first(self) -> None:
        raw = self._raw(body="## Issue\n\nCloses #1354")
        assert _extract_all_linked_issues(raw) == [1354, 12]

    def test_pr_1351_body_resolves_to_its_own_issue(self) -> None:
        """The incident: GitHub listed #12 (from review-guidance prose) before #1340."""
        body = (
            "## Summary\n- Integrate and after-merge stop closing issues by hand.\n\n"
            '## Review guidance\n- Keyword matching is textual: "does not close #12" still closes #12.\n\n'
            "## Issue\n\nCloses #1340\n"
        )
        raw = self._raw(body=body, branch="fix/issue-1340", numbers=(12, 1340))
        assert _extract_linked_issue(raw) == 1340
        assert _extract_all_linked_issues(raw) == [1340, 12]

    def test_both_extractors_agree_on_primary(self) -> None:
        for raw in (
            self._raw(body="## Issue\n\nCloses #1354"),
            self._raw(branch="feat/issue-1354"),
            self._raw(),
            self._raw(numbers=(5, 6, 7), branch="feat/issue-7"),
        ):
            assert _extract_all_linked_issues(raw)[0] == _extract_linked_issue(raw)


_PLAIN_BOT_CR = {"state": "CHANGES_REQUESTED", "author": {"login": "bot[bot]"}}


class TestShouldUnblockBotReviews:
    """Test _should_unblock_bot_reviews early return paths."""

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            # Early return when cr_reviews is an empty list.
            pytest.param({"cr_reviews": [], "all_threads_resolved": True}, None, id="cr-reviews-empty"),
            pytest.param(
                {"cr_reviews": [_PLAIN_BOT_CR], "all_threads_resolved": False}, None, id="threads-not-resolved"
            ),
            # Early return when not all reviews are from bots.
            pytest.param(
                {
                    "cr_reviews": [_PLAIN_BOT_CR, {"state": "CHANGES_REQUESTED", "author": {"login": "alice"}}],
                    "all_threads_resolved": True,
                },
                None,
                id="human-reviewer-present",
            ),
            # all_threads_resolved=None (unknown) must never unblock.
            pytest.param(
                {"cr_reviews": [_PLAIN_BOT_CR], "all_threads_resolved": None, "superseded_by_new_commit": True},
                None,
                id="unresolved-unknown",
            ),
            # Threads resolved is not enough on its own: needs a new commit too.
            pytest.param(
                {"cr_reviews": [_PLAIN_BOT_CR], "all_threads_resolved": True, "superseded_by_new_commit": False},
                None,
                id="not-superseded-by-new-commit",
            ),
            # All bot reviews, threads resolved, CI passed, and the review superseded by a new commit.
            pytest.param(
                {"cr_reviews": [_PLAIN_BOT_CR], "all_threads_resolved": True, "superseded_by_new_commit": True},
                ComputedPRState.APPROVED_CI_GREEN,
                id="all-conditions-met",
            ),
            # CodeRabbit's classic login ('coderabbitai', no [bot] suffix) must still be recognized as a bot,
            # matching the DEFAULT_CODERABBIT_AUTHORS convention of review_completed.py's _has_human_approval().
            pytest.param(
                {
                    "cr_reviews": [{"state": "CHANGES_REQUESTED", "author": {"login": "coderabbitai"}}],
                    "all_threads_resolved": True,
                    "superseded_by_new_commit": True,
                },
                ComputedPRState.APPROVED_CI_GREEN,
                id="classic-coderabbit-login-without-bot-suffix",
            ),
        ],
    )
    def test_should_unblock_bot_reviews(self, kwargs: dict[str, object], expected: ComputedPRState | None) -> None:
        result = _should_unblock_bot_reviews(ci_status="passed", mergeable="MERGEABLE", **kwargs)  # type: ignore[arg-type]
        assert result == expected


class TestListOpenPrsWithState:
    """Test list_open_prs_with_state exception paths and background tasks."""

    @pytest.mark.asyncio
    async def test_returns_empty_on_config_load_exception(self, monkeypatch, tmp_path) -> None:
        """Test lines 472-474: config load exception returns empty list."""
        # list_open_prs_with_state() imports get_project_dir inside the function, so patching the
        # module attribute reaches it; asserting what load_config() received proves the patch is
        # what supplied the directory, not whatever the real contextvar or cwd happened to be.
        monkeypatch.setattr("sova.config.context.get_project_dir", lambda: tmp_path)
        seen_dirs: list[object] = []

        def _raise_config(project_dir: object) -> None:
            seen_dirs.append(project_dir)
            raise RuntimeError("config error")

        monkeypatch.setattr("sova.config.loader.load_config", _raise_config)
        result = await list_open_prs_with_state()
        assert result == []
        assert seen_dirs == [tmp_path]

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_github_repo(self, monkeypatch, tmp_path) -> None:
        """Test lines 476-477: returns empty when github_repo is empty."""
        from unittest.mock import MagicMock

        monkeypatch.setattr("sova.config.context.get_project_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        mock_cfg.github_repo = ""
        load_config = MagicMock(return_value=mock_cfg)
        monkeypatch.setattr("sova.config.loader.load_config", load_config)
        result = await list_open_prs_with_state()
        assert result == []
        load_config.assert_called_once_with(tmp_path)

    @pytest.mark.asyncio
    async def test_uses_unknown_thread_counts_on_exception(self, monkeypatch, tmp_path) -> None:
        """Thread/review-data fetch exceptions leave counts unknown (None), not (0, 0)."""
        from unittest.mock import MagicMock

        monkeypatch.setattr("sova.config.context.get_project_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)

        raw_prs = [{"number": 10, "title": "Test"}]
        monkeypatch.setattr("sova.git.pr.list_open_prs", AsyncMock(return_value=raw_prs))
        monkeypatch.setattr(
            "sova.git.pr.get_pr_review_data",
            AsyncMock(side_effect=RuntimeError("thread count error")),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.pr_service._enrich_pr",
            lambda pr, now: {**pr, "computed_state": "test"},
        )

        result = await list_open_prs_with_state()
        assert len(result) == 1
        assert result[0]["_thread_counts"] is None

    @pytest.mark.asyncio
    async def test_local_handoff_verdict_reaches_pr_state_when_github_has_none(self, monkeypatch, tmp_path) -> None:
        """A revise only the local handoff recorded (its post failed) keeps the listed PR out of approved."""
        from sova.ipc.handoff import write_handoff_file

        monkeypatch.setattr("sova.config.context.get_project_dir", lambda: tmp_path)
        mock_cfg = MagicMock(github_repo="owner/verdict-wiring", github_user="sova-bot")
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)
        write_handoff_file(tmp_path, _handoff("revise", _HEAD, pr=10))
        raw_prs = [{"number": 10, "headRefOid": _HEAD}]
        monkeypatch.setattr("sova.git.pr.list_open_prs", AsyncMock(return_value=raw_prs))
        monkeypatch.setattr("sova.git.pr.get_pr_review_data", AsyncMock(return_value={10: _review_data([])}))
        monkeypatch.setattr(
            "sova.dashboard.services.pr_service._enrich_pr",
            lambda pr, now: {**pr, "computed_state": "test"},
        )

        result = await list_open_prs_with_state()

        assert result[0]["_sova_verdict"] == "revise"

    @pytest.mark.asyncio
    async def test_executes_without_exception_when_enriching_prs(self, monkeypatch, tmp_path) -> None:
        """Test lines 503-505: background task setup doesn't raise exceptions."""
        from unittest.mock import MagicMock

        monkeypatch.setattr("sova.config.context.get_project_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)

        raw_prs = [{"number": 10}]
        monkeypatch.setattr("sova.git.pr.list_open_prs", AsyncMock(return_value=raw_prs))
        monkeypatch.setattr("sova.git.pr.get_pr_review_data", AsyncMock(return_value={}))
        monkeypatch.setattr(
            "sova.dashboard.services.pr_service._enrich_pr",
            lambda pr, now: {**pr, "computed_state": "test"},
        )

        # Mock _record_state_transitions to avoid side effects
        async def mock_record(prs, *, repo, project_dir) -> None:
            pass

        monkeypatch.setattr("sova.dashboard.services.pr_service._record_state_transitions", mock_record)

        # Should not raise
        result = await list_open_prs_with_state()
        assert len(result) == 1


class TestRecordStateTransitions:
    """Test _record_state_transitions early returns and event write logic."""

    @pytest.mark.asyncio
    async def test_skips_prs_with_no_previous_state(self, monkeypatch, tmp_path: Path) -> None:
        """Test lines 523-524: early continue when prev is None."""
        from sova.dashboard.services import pr_service
        from sova.dashboard.services.pr_service import _record_state_transitions

        # Clear any previous state
        pr_service._last_known_states.clear()

        prs = [{"number": 10, "computed_state": "approved"}]
        await _record_state_transitions(prs, repo="owner/repo", project_dir=tmp_path)
        # No exception = success, no events written because prev is None

    @pytest.mark.asyncio
    async def test_skips_prs_with_same_state(self, monkeypatch, tmp_path: Path) -> None:
        """Test lines 525-526: early continue when state == prev."""
        from sova.dashboard.services import pr_service
        from sova.dashboard.services.pr_service import _record_state_transitions

        # Set previous state
        pr_service._last_known_states[10] = "approved"

        prs = [{"number": 10, "computed_state": "approved", "updated_at": "2026-08-01T12:00:00Z"}]
        await _record_state_transitions(prs, repo="owner/repo", project_dir=tmp_path)
        # No exception = success, no events written because state unchanged

    @pytest.mark.asyncio
    async def test_skips_state_with_no_event_mapping(self, monkeypatch, tmp_path: Path) -> None:
        """Test lines 528-530: early continue when state has no event type mapping."""
        from sova.dashboard.services import pr_service
        from sova.dashboard.services.pr_service import _record_state_transitions

        # Set previous state
        pr_service._last_known_states[10] = "some_old_state"

        prs = [{"number": 10, "computed_state": "unknown_state", "updated_at": "2026-08-01T12:00:00Z"}]
        await _record_state_transitions(prs, repo="owner/repo", project_dir=tmp_path)
        # No exception = success, no events written because no mapping

    @pytest.mark.asyncio
    async def test_handles_event_write_exception_gracefully(self, monkeypatch, tmp_path) -> None:
        """Test lines 555-556: individual event write exception is logged but doesn't fail."""
        from sova.dashboard.services import pr_service
        from sova.dashboard.services.pr_service import _record_state_transitions

        # Mock get_session to raise on commit
        async def mock_session(*args, **kwargs):
            class MockSession:
                def add(self, obj):
                    pass

                async def commit(self):
                    raise RuntimeError("DB error")

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

            return MockSession()

        monkeypatch.setattr("sova.db.session.get_session", mock_session)

        # Set previous state so transition happens
        pr_service._last_known_states[10] = "draft"

        prs = [
            {
                "number": 10,
                "computed_state": "approved_ci_green",
                "updated_at": "2026-08-01T12:00:00Z",
                "author": "alice",
            }
        ]

        # Should not raise despite DB error
        await _record_state_transitions(prs, repo="owner/repo", project_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_handles_session_exception_gracefully(self, monkeypatch, tmp_path) -> None:
        """Test outer exception handler catches session errors."""
        from sova.dashboard.services import pr_service
        from sova.dashboard.services.pr_service import _record_state_transitions

        async def _raise_session(*args, **kwargs):
            raise RuntimeError("session error")

        monkeypatch.setattr("sova.db.session.get_session", _raise_session)

        pr_service._last_known_states[10] = "draft"
        prs = [{"number": 10, "computed_state": "approved_ci_green", "updated_at": "2026-08-01T12:00:00Z"}]

        # Should not raise
        await _record_state_transitions(prs, repo="owner/repo", project_dir=tmp_path)


class TestInvalidatePrCache:
    def setup_method(self) -> None:
        from sova.dashboard.services import pr_service

        pr_service._pr_cache.clear()
        pr_service._pr_cache_generation.clear()

    def teardown_method(self) -> None:
        from sova.dashboard.services import pr_service

        pr_service._pr_cache.clear()
        pr_service._pr_cache_generation.clear()

    def test_drops_every_user_entry_for_the_repo(self) -> None:
        from sova.dashboard.services import pr_service

        pr_service._pr_cache[("owner/repo", "alice")] = (time.monotonic(), [])
        pr_service._pr_cache[("owner/repo", "bob")] = (time.monotonic(), [])

        pr_service.invalidate_pr_cache("owner/repo")

        assert pr_service._pr_cache == {}

    def test_keeps_other_repos(self) -> None:
        from sova.dashboard.services import pr_service

        pr_service._pr_cache[("owner/other", "alice")] = (time.monotonic(), [])

        pr_service.invalidate_pr_cache("owner/repo")

        assert ("owner/other", "alice") in pr_service._pr_cache

    def test_bumps_generation_for_dropped_entries(self) -> None:
        from sova.dashboard.services import pr_service

        pr_service._pr_cache[("owner/repo", "alice")] = (time.monotonic(), [])

        pr_service.invalidate_pr_cache("owner/repo")

        assert pr_service._pr_cache_generation[("owner/repo", "alice")] == 1

    @pytest.mark.asyncio
    async def test_in_flight_fetch_does_not_resurrect_stale_entry_after_invalidation(
        self, monkeypatch, tmp_path
    ) -> None:
        """A fetch started before invalidation must not repopulate the cache once it lands."""
        from unittest.mock import MagicMock

        from sova.dashboard.services import pr_service

        monkeypatch.setattr("sova.config.context.get_project_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        mock_cfg.github_repo = "owner/repo"
        mock_cfg.github_user = "testuser"
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)

        async def _slow_list_open_prs(*_args: object, **_kwargs: object) -> list:
            # Invalidate mid-fetch, simulating an agent finishing while this
            # repo's PR list is already being refetched for another request.
            pr_service.invalidate_pr_cache("owner/repo")
            return [{"number": 10}]

        monkeypatch.setattr("sova.git.pr.list_open_prs", _slow_list_open_prs)
        monkeypatch.setattr("sova.git.pr.get_pr_review_data", AsyncMock(return_value={}))
        monkeypatch.setattr(
            "sova.dashboard.services.pr_service._enrich_pr",
            lambda pr, now: {**pr, "computed_state": "test"},
        )

        async def mock_record(prs, *, repo, project_dir) -> None:
            pass

        monkeypatch.setattr("sova.dashboard.services.pr_service._record_state_transitions", mock_record)

        await pr_service.list_open_prs_with_state()
        # The invalidation fired after the fetch had already started, so the
        # result must not have been cached.
        assert ("owner/repo", "testuser") not in pr_service._pr_cache


_HEAD = "a" * 40
_OLD_HEAD = "b" * 40


def _marker_review(verdict: str, sha: str, at: str, *, author: str = "sova-bot") -> dict:
    return {
        "state": "COMMENTED",
        "body": f"<!-- sova-review: {verdict} sha={sha} -->",
        "submitted_at": at,
        "author": author,
    }


def _review_data(history: list[dict], *, truncated: bool = False) -> MagicMock:
    return MagicMock(review_history=history, review_history_truncated=truncated)


def _handoff(verdict: str, sha: str | None, *, pr: int = 7) -> DashboardHandoff:
    return DashboardHandoff(
        source="reviewer",
        status="awaiting_action",
        pr_number=pr,
        summary="review",
        details={"verdict": verdict, "review_head_sha": sha},
    )


class TestResolveHeadVerdict:
    def _resolve(self, review_data: object, handoffs: list[DashboardHandoff] | None = None, head: str = _HEAD) -> str:
        return resolve_head_verdict(
            review_data,  # type: ignore[arg-type]
            pr_number=7,
            head_sha=head,
            trusted_login="sova-bot",
            handoffs=handoffs or [],
        )

    @pytest.mark.parametrize(
        ("history", "expected"),
        [
            pytest.param([_marker_review("revise", _HEAD, "2026-01-01")], "revise", id="revise-on-head"),
            pytest.param([_marker_review("block", _HEAD, "2026-01-01")], "block", id="block-on-head"),
            pytest.param([_marker_review("approve", _HEAD, "2026-01-01")], "approve", id="approve-on-head"),
            pytest.param([], "none", id="no-review"),
            pytest.param([_marker_review("revise", _OLD_HEAD, "2026-01-01")], "none", id="revise-on-older-head"),
            pytest.param(
                [
                    _marker_review("revise", _OLD_HEAD, "2026-01-01"),
                    _marker_review("approve", _HEAD, "2026-01-02"),
                ],
                "approve",
                id="newer-approve-beats-older-revise",
            ),
            pytest.param(
                [_marker_review("revise", _HEAD, "2026-01-01", author="stranger")],
                "none",
                id="forged-marker-from-another-login-ignored",
            ),
            pytest.param(
                [
                    _marker_review("revise", _HEAD, "2026-01-01"),
                    {
                        "state": "COMMENTED",
                        "body": "<!-- sova-addressed: sha=" + _HEAD + " -->",
                        "submitted_at": "2026-01-02",
                        "author": "sova-bot",
                    },
                ],
                "none",
                id="addressed-revise-awaits-re-review",
            ),
        ],
    )
    def test_github_marker(self, history: list[dict], expected: str) -> None:
        assert self._resolve(_review_data(history)) == expected

    def test_truncated_history_without_marker_is_unknown(self) -> None:
        assert self._resolve(_review_data([], truncated=True)) == "unknown"

    def test_unreachable_github_is_unknown(self) -> None:
        assert self._resolve(None) == "unknown"

    @pytest.mark.parametrize(
        ("review_data", "handoff", "expected"),
        [
            pytest.param(_review_data([]), _handoff("revise", _HEAD), "revise", id="no-marker-local-revise"),
            pytest.param(None, _handoff("block", _HEAD), "block", id="github-down-local-block"),
            pytest.param(None, _handoff("approve", _HEAD), "unknown", id="github-down-local-approve-is-not-a-block"),
            pytest.param(_review_data([]), _handoff("revise", _OLD_HEAD), "none", id="local-handoff-other-head"),
            pytest.param(_review_data([]), _handoff("revise", _HEAD, pr=8), "none", id="local-handoff-other-pr"),
            pytest.param(_review_data([]), _handoff("revise", None), "none", id="local-handoff-without-head"),
            pytest.param(
                _review_data([_marker_review("approve", _HEAD, "2026-01-01")]),
                _handoff("revise", _HEAD),
                "approve",
                id="github-marker-for-head-wins-over-local",
            ),
        ],
    )
    def test_local_handoff_fallback(self, review_data: object, handoff: DashboardHandoff, expected: str) -> None:
        assert self._resolve(review_data, [handoff]) == expected

    @pytest.mark.parametrize("order", ["older-first", "newer-first"])
    def test_newest_matching_handoff_decides_whatever_the_order(self, order: str) -> None:
        older = _handoff("revise", _HEAD).model_copy(update={"created_at": "2026-01-01T00:00:00Z"})
        newer = _handoff("approve", _HEAD).model_copy(update={"created_at": "2026-01-02T00:00:00Z"})
        handoffs = [older, newer] if order == "older-first" else [newer, older]

        assert self._resolve(_review_data([]), handoffs) == "none"

        newer_revise = _handoff("revise", _HEAD).model_copy(update={"created_at": "2026-01-03T00:00:00Z"})
        assert self._resolve(_review_data([]), [*handoffs, newer_revise]) == "revise"

    def test_abbreviated_marker_sha_matches_full_head(self) -> None:
        assert self._resolve(_review_data([_marker_review("revise", _HEAD[:7], "2026-01-01")])) == "revise"
