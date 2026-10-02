"""Tests for the connectivity tracker and DNS probe (network outage resilience)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from sova.supervisor import network_health
from sova.supervisor.network_health import ConnectivityTracker, probe_connectivity, track_connectivity
from sova.utils.shell import ShellResult


@pytest.fixture(autouse=True)
def _silence_feed():
    # The tracker emits feed events on every transition; the import is lazy and
    # inside a try/except, but patching keeps the tests off the feed service.
    with patch("sova.dashboard.services.feed_service.emit_safe"):
        yield


def _unreachable() -> ShellResult:
    return ShellResult(returncode=1, stdout="", stderr="error connecting to api.github.com")


def _ok() -> ShellResult:
    return ShellResult(returncode=0, stdout="{}", stderr="")


def _rate_limited() -> ShellResult:
    return ShellResult(returncode=1, stdout="", stderr="API rate limit exceeded for user")


class TestFailureThreshold:
    def test_single_failure_does_not_declare_outage(self) -> None:
        # One failed call is a blip. Flapping the operator-facing banner on it
        # would make the banner untrustworthy exactly when it matters.
        tracker = ConnectivityTracker()
        tracker.record_failure("github")
        assert tracker.is_down() is False

    def test_second_consecutive_failure_declares_outage(self) -> None:
        tracker = ConnectivityTracker()
        tracker.record_failure("github")
        tracker.record_failure("github")
        assert tracker.is_down() is True

    def test_success_resets_the_streak(self) -> None:
        tracker = ConnectivityTracker()
        tracker.record_failure("github")
        tracker.record_success("github")
        tracker.record_failure("github")
        assert tracker.is_down() is False

    def test_decisive_failure_skips_the_threshold(self) -> None:
        # The probe resolving none of its hosts is already a multi-signal
        # result, so it does not wait for a second observation.
        tracker = ConnectivityTracker()
        tracker.record_failure("probe", decisive=True)
        assert tracker.is_down() is True


class TestRecovery:
    def test_success_clears_the_outage(self) -> None:
        tracker = ConnectivityTracker()
        tracker.record_failure("probe", decisive=True)
        tracker.record_success("github")
        assert tracker.is_down() is False

    def test_healthy_for_seconds_is_zero_while_down(self) -> None:
        tracker = ConnectivityTracker()
        tracker.record_failure("probe", decisive=True)
        assert tracker.healthy_for_seconds() == 0.0

    def test_healthy_for_seconds_restarts_at_recovery(self) -> None:
        # The self-heal pass uses this as its flap guard, so a connection that
        # just came back must not report a long healthy streak.
        tracker = ConnectivityTracker()
        tracker.record_failure("probe", decisive=True)
        tracker.record_success("github")
        assert tracker.healthy_for_seconds() < 1.0

    def test_status_snapshot_shape(self) -> None:
        tracker = ConnectivityTracker()
        tracker.record_failure("github")
        tracker.record_failure("github")
        status = tracker.get_status()
        assert status.is_down is True
        assert status.consecutive_failures == 2
        assert status.last_source == "github"
        assert status.down_for_seconds >= 0.0


class TestEmitOnTransition:
    def test_restored_emits_even_right_after_lost(self) -> None:
        """A connection that flaps back within seconds must still report
        "restored": a shared time-based throttle previously suppressed
        whichever transition landed within 60s of the other, leaving the feed
        showing a stale "lost" message after the tracker itself had already
        recovered."""
        with patch("sova.dashboard.services.feed_service.emit_safe") as mock_emit:
            tracker = ConnectivityTracker()
            tracker.record_failure("probe", decisive=True)
            tracker.record_success("github")

        titles = [call.args[0] for call in mock_emit.call_args_list]
        assert titles == ["Network connection lost", "Network connection restored"]

    def test_repeated_flaps_each_emit_their_own_transition(self) -> None:
        with patch("sova.dashboard.services.feed_service.emit_safe") as mock_emit:
            tracker = ConnectivityTracker()
            for _ in range(3):
                tracker.record_failure("probe", decisive=True)
                tracker.record_success("github")

        titles = [call.args[0] for call in mock_emit.call_args_list]
        assert titles == ["Network connection lost", "Network connection restored"] * 3


class TestTrackConnectivity:
    def test_unreachable_result_records_failure(self) -> None:
        tracker = ConnectivityTracker()
        with patch.object(network_health, "_tracker", tracker):
            track_connectivity(_unreachable())
            track_connectivity(_unreachable())
        assert tracker.is_down() is True

    def test_successful_result_records_success(self) -> None:
        tracker = ConnectivityTracker()
        with patch.object(network_health, "_tracker", tracker):
            track_connectivity(_unreachable())
            track_connectivity(_unreachable())
            track_connectivity(_ok())
        assert tracker.is_down() is False

    def test_rate_limited_result_is_ignored(self) -> None:
        # A throttled call proves the network works, but it is not a success
        # either. It must neither declare an outage nor clear one.
        tracker = ConnectivityTracker()
        with patch.object(network_health, "_tracker", tracker):
            track_connectivity(_rate_limited())
            track_connectivity(_rate_limited())
        assert tracker.is_down() is False
        assert tracker.get_status().consecutive_failures == 0

    def test_non_network_failure_is_ignored(self) -> None:
        # A 404 reached GitHub; it says nothing about reachability either way.
        tracker = ConnectivityTracker()
        result = ShellResult(returncode=1, stdout="", stderr="GraphQL: Could not resolve to an Issue")
        with patch.object(network_health, "_tracker", tracker):
            track_connectivity(result)
            track_connectivity(result)
        assert tracker.is_down() is False


class TestProbe:
    async def test_first_resolvable_host_reports_reachable(self) -> None:
        tracker = ConnectivityTracker()
        with (
            patch.object(network_health, "_tracker", tracker),
            patch.object(network_health, "_PROBE_HOSTS", ("a.invalid", "b.invalid")),
            patch("asyncio.get_running_loop") as mock_loop,
        ):
            mock_loop.return_value.getaddrinfo = lambda *a, **k: asyncio.sleep(0, result=[("x",)])
            assert await probe_connectivity() is True
        assert tracker.is_down() is False

    async def test_all_hosts_failing_declares_outage(self) -> None:
        tracker = ConnectivityTracker()

        async def _boom(*_args, **_kwargs):
            raise OSError("Name or service not known")

        with (
            patch.object(network_health, "_tracker", tracker),
            patch.object(network_health, "_PROBE_HOSTS", ("a.invalid", "b.invalid")),
            patch("asyncio.get_running_loop") as mock_loop,
        ):
            mock_loop.return_value.getaddrinfo = _boom
            assert await probe_connectivity() is False
        # Decisive: one probe pass is enough, no second observation needed.
        assert tracker.is_down() is True

    async def test_one_vendor_down_is_not_an_outage(self) -> None:
        # Two independent hosts exist precisely so one provider's DNS or edge
        # problem is not reported as the operator's internet being down.
        tracker = ConnectivityTracker()
        calls: list[str] = []

        async def _first_fails(host, *_args, **_kwargs):
            calls.append(host)
            if host == "down.invalid":
                raise OSError("Name or service not known")
            return [("ok",)]

        with (
            patch.object(network_health, "_tracker", tracker),
            patch.object(network_health, "_PROBE_HOSTS", ("down.invalid", "up.invalid")),
            patch("asyncio.get_running_loop") as mock_loop,
        ):
            mock_loop.return_value.getaddrinfo = _first_fails
            assert await probe_connectivity() is True
        assert calls == ["down.invalid", "up.invalid"]
        assert tracker.is_down() is False

    async def test_timeout_counts_as_host_failure(self) -> None:
        tracker = ConnectivityTracker()

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(10)

        with (
            patch.object(network_health, "_tracker", tracker),
            patch.object(network_health, "_PROBE_HOSTS", ("slow.invalid",)),
            patch.object(network_health, "_PROBE_TIMEOUT_SECONDS", 0.01),
            patch("asyncio.get_running_loop") as mock_loop,
        ):
            mock_loop.return_value.getaddrinfo = _hang
            assert await probe_connectivity() is False
        assert tracker.is_down() is True
