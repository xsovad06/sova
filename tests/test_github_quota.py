"""Tests for sova.supervisor.github_quota: GitHub API rate limit tracker."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from sova.supervisor.github_quota import (
    GitHubQuotaTracker,
    get_github_quota_status,
    get_github_quota_tracker,
)


class TestGitHubQuotaTracker:
    def test_initial_state_not_limited(self) -> None:
        tracker = GitHubQuotaTracker()
        assert not tracker.should_skip()
        status = tracker.get_status()
        assert not status.is_limited
        assert status.hits_in_window == 0
        assert status.cooldown_remaining_seconds == 0.0

    def test_record_hit_activates_cooldown(self) -> None:
        tracker = GitHubQuotaTracker(cooldown_seconds=300.0)
        with patch.object(tracker, "_emit_hit_event"):
            tracker.record_rate_limit_hit()
        assert tracker.should_skip()
        status = tracker.get_status()
        assert status.is_limited
        assert status.hits_in_window == 1
        assert status.cooldown_remaining_seconds > 0

    def test_multiple_hits_increment_counter(self) -> None:
        tracker = GitHubQuotaTracker(cooldown_seconds=300.0)
        with patch.object(tracker, "_emit_hit_event"):
            tracker.record_rate_limit_hit()
            tracker.record_rate_limit_hit()
            tracker.record_rate_limit_hit()
        assert tracker.get_status().hits_in_window == 3

    def test_cooldown_expires(self) -> None:
        tracker = GitHubQuotaTracker(cooldown_seconds=0.01)
        with patch.object(tracker, "_emit_hit_event"):
            tracker.record_rate_limit_hit()
        time.sleep(0.02)
        assert not tracker.should_skip()

    def test_record_success_resets_after_cooldown(self) -> None:
        tracker = GitHubQuotaTracker(cooldown_seconds=0.01)
        with patch.object(tracker, "_emit_hit_event"):
            tracker.record_rate_limit_hit()
        time.sleep(0.02)
        with patch.object(tracker, "_emit_recovery_event") as mock_recovery:
            tracker.record_success()
            mock_recovery.assert_called_once()
        assert tracker.get_status().hits_in_window == 0

    def test_record_success_noop_during_cooldown(self) -> None:
        tracker = GitHubQuotaTracker(cooldown_seconds=300.0)
        with patch.object(tracker, "_emit_hit_event"):
            tracker.record_rate_limit_hit()
        with patch.object(tracker, "_emit_recovery_event") as mock_recovery:
            tracker.record_success()
            mock_recovery.assert_not_called()
        assert tracker.get_status().hits_in_window == 1

    def test_first_hit_always_emits(self) -> None:
        tracker = GitHubQuotaTracker(cooldown_seconds=300.0)
        with patch.object(tracker, "_emit_hit_event") as mock_emit:
            tracker.record_rate_limit_hit()
            mock_emit.assert_called_once()

    def test_rapid_hits_throttle_emit(self) -> None:
        tracker = GitHubQuotaTracker(cooldown_seconds=300.0)
        with patch.object(tracker, "_emit_hit_event") as mock_emit:
            tracker.record_rate_limit_hit()
            tracker.record_rate_limit_hit()
            tracker.record_rate_limit_hit()
            assert mock_emit.call_count == 1

    def test_emit_hit_event_feed_import_error_is_contained(self) -> None:
        tracker = GitHubQuotaTracker(cooldown_seconds=300.0)
        with patch(
            "sova.supervisor.github_quota.GitHubQuotaTracker._emit_hit_event",
        ) as mock_emit:
            mock_emit.side_effect = None
            tracker.record_rate_limit_hit()
        assert tracker.should_skip()

    def test_emit_recovery_event_noop_when_not_limited(self) -> None:
        tracker = GitHubQuotaTracker(cooldown_seconds=0.01)
        with patch.object(tracker, "_emit_hit_event"):
            tracker.record_rate_limit_hit()
        time.sleep(0.02)
        with patch.object(tracker, "_emit_recovery_event") as mock_recovery:
            tracker.record_success()
            mock_recovery.assert_called_once()

    def test_get_status_remaining_decreases(self) -> None:
        tracker = GitHubQuotaTracker(cooldown_seconds=300.0)
        with patch.object(tracker, "_emit_hit_event"):
            tracker.record_rate_limit_hit()
        s1 = tracker.get_status()
        time.sleep(0.05)
        s2 = tracker.get_status()
        assert s2.cooldown_remaining_seconds < s1.cooldown_remaining_seconds


class TestTrackerRegistry:
    def setup_method(self) -> None:
        from sova.supervisor import github_quota

        github_quota._trackers.clear()

    def test_default_tracker(self) -> None:
        t1 = get_github_quota_tracker()
        t2 = get_github_quota_tracker()
        assert t1 is t2

    def test_identity_keyed_trackers(self) -> None:
        t1 = get_github_quota_tracker("alice")
        t2 = get_github_quota_tracker("bob")
        assert t1 is not t2
        assert get_github_quota_tracker("alice") is t1

    def test_get_status_shorthand(self) -> None:
        tracker = get_github_quota_tracker("user1")
        with patch.object(tracker, "_emit_hit_event"):
            tracker.record_rate_limit_hit()
        status = get_github_quota_status("user1")
        assert status.is_limited
        assert status.hits_in_window == 1


class TestEmitEvents:
    def test_emit_hit_event_calls_feed_service(self) -> None:
        with patch("sova.supervisor.github_quota.GitHubQuotaTracker._emit_hit_event") as mock:
            tracker = GitHubQuotaTracker()
            tracker.record_rate_limit_hit()
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_emit_hit_event_static_method_catches_import_error(self) -> None:
        with patch(
            "sova.supervisor.github_quota.GitHubQuotaTracker._emit_hit_event",
            wraps=GitHubQuotaTracker._emit_hit_event,
        ):
            GitHubQuotaTracker._emit_hit_event()

    @pytest.mark.asyncio
    async def test_emit_recovery_event_static_method_catches_import_error(self) -> None:
        with patch(
            "sova.supervisor.github_quota.GitHubQuotaTracker._emit_recovery_event",
            wraps=GitHubQuotaTracker._emit_recovery_event,
        ):
            GitHubQuotaTracker._emit_recovery_event()
