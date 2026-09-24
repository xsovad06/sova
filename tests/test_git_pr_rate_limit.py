"""Tests for rate limit tracking in sova.git.pr.

Verifies that _track_gh_rate_limit feeds the GitHubQuotaTracker from
gh CLI calls made outside the GitHubAdapter (list_open_prs,
find_pr_for_issue, get_review_thread_counts, etc.).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sova.git.pr import _track_gh_rate_limit
from sova.supervisor.github_quota import get_github_quota_tracker
from sova.utils.shell import ShellResult


class TestTrackGhRateLimit:
    def setup_method(self) -> None:
        from sova.supervisor import github_quota

        github_quota._trackers.clear()

    def test_rate_limited_result_records_hit(self) -> None:
        result = ShellResult(returncode=1, stdout="", stderr="API rate limit exceeded")
        _track_gh_rate_limit(result, "testuser")
        tracker = get_github_quota_tracker("testuser")
        assert tracker.should_skip()
        assert tracker.get_status().hits_in_window == 1

    def test_success_records_success(self) -> None:
        tracker = get_github_quota_tracker("testuser")
        with patch.object(tracker, "_emit_hit_event"):
            tracker.record_rate_limit_hit()

        tracker._cooldown_seconds = 0.0
        result = ShellResult(returncode=0, stdout="[]", stderr="")
        _track_gh_rate_limit(result, "testuser")
        assert tracker.get_status().hits_in_window == 0

    def test_non_rate_limit_failure_does_not_record(self) -> None:
        result = ShellResult(returncode=1, stdout="", stderr="authentication failed")
        _track_gh_rate_limit(result, "testuser")
        tracker = get_github_quota_tracker("testuser")
        assert not tracker.should_skip()
        assert tracker.get_status().hits_in_window == 0

    def test_empty_user_uses_default_tracker(self) -> None:
        result = ShellResult(returncode=1, stdout="", stderr="rate limit exceeded")
        _track_gh_rate_limit(result, "")
        tracker = get_github_quota_tracker("")
        assert tracker.should_skip()


class TestListOpenPrsTracking:
    """list_open_prs should feed the rate limit tracker."""

    def setup_method(self) -> None:
        from sova.supervisor import github_quota

        github_quota._trackers.clear()

    @pytest.mark.asyncio
    async def test_list_open_prs_tracks_rate_limit_on_failure(self) -> None:
        from sova.git.pr import list_open_prs

        rate_limited = ShellResult(returncode=1, stdout="", stderr="API rate limit exceeded")
        with patch("sova.git.pr.run", new_callable=AsyncMock, return_value=rate_limited):
            with patch("sova.git.pr.resolve_gh_env", new_callable=AsyncMock, return_value={}):
                result = await list_open_prs(repo="owner/repo", github_user="testuser")

        assert result == []
        tracker = get_github_quota_tracker("testuser")
        assert tracker.should_skip()

    @pytest.mark.asyncio
    async def test_list_open_prs_tracks_success(self) -> None:
        from sova.git.pr import list_open_prs

        success = ShellResult(returncode=0, stdout="[]", stderr="")
        with patch("sova.git.pr.run", new_callable=AsyncMock, return_value=success):
            with patch("sova.git.pr.resolve_gh_env", new_callable=AsyncMock, return_value={}):
                await list_open_prs(repo="owner/repo", github_user="testuser")

        tracker = get_github_quota_tracker("testuser")
        assert not tracker.should_skip()


class TestFindPrForIssueTracking:
    """find_pr_for_issue should feed the rate limit tracker."""

    def setup_method(self) -> None:
        from sova.supervisor import github_quota

        github_quota._trackers.clear()

    @pytest.mark.asyncio
    async def test_find_pr_tracks_rate_limit(self) -> None:
        from sova.git.pr import find_pr_for_issue

        rate_limited = ShellResult(returncode=1, stdout="", stderr="API rate limit exceeded")
        with patch("sova.git.pr.run", new_callable=AsyncMock, return_value=rate_limited):
            with patch("sova.git.pr.resolve_gh_env", new_callable=AsyncMock, return_value={}):
                result = await find_pr_for_issue("42", repo="owner/repo", github_user="testuser")

        assert result is None
        tracker = get_github_quota_tracker("testuser")
        assert tracker.should_skip()


class TestFindPrForIssueChecked:
    """find_pr_for_issue_checked should distinguish a lookup failure from 'no PR found'."""

    def setup_method(self) -> None:
        from sova.git import pr as pr_module
        from sova.supervisor import github_quota

        github_quota._trackers.clear()
        pr_module._find_pr_cache.clear()

    @pytest.mark.asyncio
    async def test_find_pr_checked_raises_on_total_failure(self) -> None:
        from sova.git.pr import PRLookupError, find_pr_for_issue_checked

        rate_limited = ShellResult(returncode=1, stdout="", stderr="API rate limit exceeded")
        with patch("sova.git.pr.run", new_callable=AsyncMock, return_value=rate_limited):
            with patch("sova.git.pr.resolve_gh_env", new_callable=AsyncMock, return_value={}):
                with pytest.raises(PRLookupError):
                    await find_pr_for_issue_checked("42", repo="owner/repo", github_user="testuser")

        tracker = get_github_quota_tracker("testuser")
        assert tracker.should_skip()

    @pytest.mark.asyncio
    async def test_find_pr_checked_returns_none_on_confirmed_empty(self) -> None:
        from sova.git.pr import find_pr_for_issue_checked

        empty = ShellResult(returncode=0, stdout="[]", stderr="")
        with patch("sova.git.pr.run", new_callable=AsyncMock, return_value=empty):
            with patch("sova.git.pr.resolve_gh_env", new_callable=AsyncMock, return_value={}):
                result = await find_pr_for_issue_checked("42", repo="owner/repo", github_user="testuser")

        assert result is None

    @pytest.mark.asyncio
    async def test_find_pr_checked_raises_when_only_body_search_fails(self) -> None:
        """A PR linked only in its body is invisible to the branch search, so a failed body
        search plus an empty branch search must not be read as a confirmed negative."""
        from sova.git.pr import PRLookupError, find_pr_for_issue_checked

        body_failed = ShellResult(returncode=1, stdout="", stderr="network error")
        branch_empty = ShellResult(returncode=0, stdout="[]", stderr="")
        side_effect = [body_failed, branch_empty, branch_empty, branch_empty]
        with patch("sova.git.pr.run", new_callable=AsyncMock, side_effect=side_effect):
            with patch("sova.git.pr.resolve_gh_env", new_callable=AsyncMock, return_value={}):
                with pytest.raises(PRLookupError):
                    await find_pr_for_issue_checked("42", repo="owner/repo", github_user="testuser")

        tracker = get_github_quota_tracker("testuser")
        assert not tracker.should_skip()

    @pytest.mark.asyncio
    async def test_find_pr_checked_raises_when_only_branch_search_fails(self) -> None:
        """A PR linked only by branch naming is invisible to the body search, so an empty
        body search plus a failed branch search must not be read as a confirmed negative."""
        from sova.git.pr import PRLookupError, find_pr_for_issue_checked

        body_empty = ShellResult(returncode=0, stdout="[]", stderr="")
        branch_failed = ShellResult(returncode=1, stdout="", stderr="network error")
        side_effect = [body_empty, branch_failed, branch_failed, branch_failed]
        with patch("sova.git.pr.run", new_callable=AsyncMock, side_effect=side_effect):
            with patch("sova.git.pr.resolve_gh_env", new_callable=AsyncMock, return_value={}):
                with pytest.raises(PRLookupError):
                    await find_pr_for_issue_checked("42", repo="owner/repo", github_user="testuser")

    @pytest.mark.asyncio
    async def test_find_pr_checked_raises_when_one_of_three_branch_prefixes_fails(self) -> None:
        """A confirmed negative requires every branch prefix search to succeed: a match on
        the one unchecked prefix would otherwise be missed."""
        from sova.git.pr import PRLookupError, find_pr_for_issue_checked

        body_empty = ShellResult(returncode=0, stdout="[]", stderr="")
        branch_empty = ShellResult(returncode=0, stdout="[]", stderr="")
        branch_failed = ShellResult(returncode=1, stdout="", stderr="network error")
        with patch(
            "sova.git.pr.run",
            new_callable=AsyncMock,
            side_effect=[body_empty, branch_empty, branch_failed, branch_empty],
        ):
            with patch("sova.git.pr.resolve_gh_env", new_callable=AsyncMock, return_value={}):
                with pytest.raises(PRLookupError):
                    await find_pr_for_issue_checked("42", repo="owner/repo", github_user="testuser")

    @pytest.mark.asyncio
    async def test_find_pr_checked_raises_on_invalid_body_search_json(self) -> None:
        """Invalid JSON from a successful body search is a failure, not a confirmed empty."""
        from sova.git.pr import PRLookupError, find_pr_for_issue_checked

        body_bad_json = ShellResult(returncode=0, stdout="not json", stderr="")
        branch_empty = ShellResult(returncode=0, stdout="[]", stderr="")
        with patch(
            "sova.git.pr.run",
            new_callable=AsyncMock,
            side_effect=[body_bad_json, branch_empty, branch_empty, branch_empty],
        ):
            with patch("sova.git.pr.resolve_gh_env", new_callable=AsyncMock, return_value={}):
                with pytest.raises(PRLookupError):
                    await find_pr_for_issue_checked("42", repo="owner/repo", github_user="testuser")


class TestGetReviewThreadCountsTracking:
    """get_review_thread_counts should feed the rate limit tracker."""

    def setup_method(self) -> None:
        from sova.supervisor import github_quota

        github_quota._trackers.clear()

    @pytest.mark.asyncio
    async def test_review_threads_tracks_rate_limit(self) -> None:
        from sova.git.pr import get_review_thread_counts

        rate_limited = ShellResult(returncode=1, stdout="", stderr="API rate limit exceeded")
        with patch("sova.git.pr.run", new_callable=AsyncMock, return_value=rate_limited):
            with patch("sova.git.pr.resolve_gh_env", new_callable=AsyncMock, return_value={}):
                result = await get_review_thread_counts([1, 2], repo="owner/repo", github_user="testuser")

        assert result == {1: None, 2: None}
        tracker = get_github_quota_tracker("testuser")
        assert tracker.should_skip()
