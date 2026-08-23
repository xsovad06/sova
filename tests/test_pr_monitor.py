"""Tests for PR monitor -- state change detection, notifications, CodeRabbit retry."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.config.models import NotificationConfig, PRMonitorConfig
from sova.dashboard.services.pr_service import ComputedPRState
from sova.supervisor.pr_monitor import PRMonitor, PRSnapshot, _is_coderabbit_rate_limited
from sova.utils.shell import ShellResult


@pytest.fixture(autouse=True)
def _clear_rate_limit_trackers() -> None:
    """Ensure rate limit tracker state does not leak between tests."""
    from sova.supervisor import github_quota
    from sova.supervisor.pr_monitor import _rate_check_cache

    github_quota._trackers.clear()
    _rate_check_cache.clear()


def _make_monitor(**overrides: object) -> PRMonitor:
    defaults = {
        "project_dir": Path("/tmp/test"),
        "monitor_config": PRMonitorConfig(enabled=True),
        "notification_config": NotificationConfig(desktop=True),
        "repo": "owner/repo",
        "github_user": "testuser",
    }
    defaults.update(overrides)
    return PRMonitor(**defaults)


def _make_pr(number: int, state: str = "awaiting_review", title: str = "Test PR") -> dict:
    return {
        "number": number,
        "computed_state": state,
        "title": title,
        "branch": "feat/test",
        "url": f"https://github.com/owner/repo/pull/{number}",
        "state": "OPEN",
    }


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------


class TestPRMonitorConfig:
    def test_defaults(self) -> None:
        cfg = PRMonitorConfig()
        assert cfg.enabled is False
        assert cfg.poll_interval == 120
        assert cfg.notify_on_approval is True
        assert cfg.notify_on_changes_requested is True
        assert cfg.notify_on_ci_failure is True
        assert cfg.notify_on_ready_to_merge is True
        assert cfg.auto_retry_coderabbit is True

    def test_custom_interval(self) -> None:
        cfg = PRMonitorConfig(poll_interval=60)
        assert cfg.poll_interval == 60


# ---------------------------------------------------------------------------
# First cycle suppression
# ---------------------------------------------------------------------------


class TestFirstCycleSuppression:
    @pytest.mark.asyncio
    async def test_first_cycle_no_notifications(self) -> None:
        monitor = _make_monitor()
        prs = [_make_pr(1, ComputedPRState.APPROVED)]

        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            await monitor._poll_cycle()

        mock_notify.assert_not_called()
        assert monitor._initialized is True
        assert 1 in monitor._last_state

    @pytest.mark.asyncio
    async def test_second_cycle_fires_notification(self) -> None:
        monitor = _make_monitor()

        prs_initial = [_make_pr(1, ComputedPRState.AWAITING_REVIEW)]
        prs_changed = [_make_pr(1, ComputedPRState.APPROVED)]

        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                side_effect=[prs_initial, prs_changed],
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            await monitor._poll_cycle()  # first -- populate
            await monitor._poll_cycle()  # second -- detect change

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args
        assert "Approved" in call_kwargs.kwargs.get("subtitle", "") or "Approved" in str(call_kwargs)


# ---------------------------------------------------------------------------
# State transition notifications
# ---------------------------------------------------------------------------


class TestStateTransitionNotifications:
    @pytest.mark.asyncio
    async def test_approval_notification(self) -> None:
        monitor = _make_monitor()
        monitor._initialized = True
        monitor._last_state = {
            1: PRSnapshot(number=1, computed_state=ComputedPRState.AWAITING_REVIEW, title="Test"),
        }

        prs = [_make_pr(1, ComputedPRState.APPROVED)]
        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            await monitor._poll_cycle()

        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_ci_failure_notification(self) -> None:
        monitor = _make_monitor()
        monitor._initialized = True
        monitor._last_state = {
            1: PRSnapshot(number=1, computed_state=ComputedPRState.CI_RUNNING, title="Test"),
        }

        prs = [_make_pr(1, ComputedPRState.CI_FAILED)]
        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            await monitor._poll_cycle()

        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_ready_to_merge_notification(self) -> None:
        monitor = _make_monitor()
        monitor._initialized = True
        monitor._last_state = {
            1: PRSnapshot(number=1, computed_state=ComputedPRState.CI_RUNNING, title="Test"),
        }

        prs = [_make_pr(1, ComputedPRState.APPROVED_CI_GREEN)]
        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            await monitor._poll_cycle()

        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_notification_when_disabled(self) -> None:
        cfg = PRMonitorConfig(enabled=True, notify_on_approval=False)
        monitor = _make_monitor(monitor_config=cfg)
        monitor._initialized = True
        monitor._last_state = {
            1: PRSnapshot(number=1, computed_state=ComputedPRState.AWAITING_REVIEW, title="Test"),
        }

        prs = [_make_pr(1, ComputedPRState.APPROVED)]
        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            await monitor._poll_cycle()

        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_notification_for_same_state(self) -> None:
        monitor = _make_monitor()
        monitor._initialized = True
        monitor._last_state = {
            1: PRSnapshot(number=1, computed_state=ComputedPRState.APPROVED, title="Test"),
        }

        prs = [_make_pr(1, ComputedPRState.APPROVED)]
        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            await monitor._poll_cycle()

        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_notification_for_non_notifiable_transition(self) -> None:
        """Transitions to states not in _NOTIFY_STATES should not notify."""
        monitor = _make_monitor()
        monitor._initialized = True
        monitor._last_state = {
            1: PRSnapshot(number=1, computed_state=ComputedPRState.AWAITING_REVIEW, title="Test"),
        }

        prs = [_make_pr(1, ComputedPRState.CI_RUNNING)]
        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            await monitor._poll_cycle()

        mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# CodeRabbit auto-retry
# ---------------------------------------------------------------------------


class TestCodeRabbitAutoRetry:
    @pytest.mark.asyncio
    async def test_retry_when_rate_limit_clears(self) -> None:
        monitor = _make_monitor()
        monitor._initialized = True
        monitor._last_state = {
            1: PRSnapshot(
                number=1,
                computed_state=ComputedPRState.AWAITING_REVIEW,
                title="Test",
                rate_limited=True,
            ),
        }

        prs = [_make_pr(1, ComputedPRState.AWAITING_REVIEW)]
        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(monitor, "_retry_coderabbit_review", new_callable=AsyncMock) as mock_retry,
        ):
            await monitor._poll_cycle()

        mock_retry.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_no_retry_when_still_rate_limited(self) -> None:
        monitor = _make_monitor()
        monitor._initialized = True
        monitor._last_state = {
            1: PRSnapshot(
                number=1,
                computed_state=ComputedPRState.AWAITING_REVIEW,
                title="Test",
                rate_limited=True,
            ),
        }

        prs = [_make_pr(1, ComputedPRState.AWAITING_REVIEW)]
        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(monitor, "_retry_coderabbit_review", new_callable=AsyncMock) as mock_retry,
        ):
            await monitor._poll_cycle()

        mock_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_retry_when_auto_retry_disabled(self) -> None:
        cfg = PRMonitorConfig(enabled=True, auto_retry_coderabbit=False)
        monitor = _make_monitor(monitor_config=cfg)
        monitor._initialized = True
        monitor._last_state = {
            1: PRSnapshot(
                number=1,
                computed_state=ComputedPRState.AWAITING_REVIEW,
                title="Test",
                rate_limited=True,
            ),
        }

        prs = [_make_pr(1, ComputedPRState.AWAITING_REVIEW)]
        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(monitor, "_retry_coderabbit_review", new_callable=AsyncMock) as mock_retry,
        ):
            await monitor._poll_cycle()

        mock_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_posts_comment(self) -> None:
        monitor = _make_monitor()
        success = ShellResult(returncode=0, stdout="", stderr="")

        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=success) as mock_run,
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            await monitor._retry_coderabbit_review(42)

        mock_run.assert_called_once()
        args = mock_run.call_args.args
        assert "42" in args
        assert "@coderabbitai review" in args

    @pytest.mark.asyncio
    async def test_retry_logs_warning_on_failure(self) -> None:
        monitor = _make_monitor()
        failure = ShellResult(returncode=1, stdout="", stderr="gh: command failed")

        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=failure),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.supervisor.pr_monitor.log") as mock_log,
        ):
            await monitor._retry_coderabbit_review(7)

        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args.args[0] == "pr_monitor.retry_coderabbit_failed"


# ---------------------------------------------------------------------------
# run_loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_run_loop_calls_poll_cycle_and_sleeps(self) -> None:
        monitor = _make_monitor()
        call_count = 0

        async def _poll_then_cancel() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError

        with (
            patch.object(monitor, "_poll_cycle", side_effect=_poll_then_cancel),
            patch.object(monitor, "_interruptible_sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(asyncio.CancelledError):
                await monitor.run_loop()

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_run_loop_catches_exceptions_and_continues(self) -> None:
        monitor = _make_monitor()
        call_count = 0

        async def _error_then_cancel() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            raise asyncio.CancelledError

        with (
            patch.object(monitor, "_poll_cycle", side_effect=_error_then_cancel),
            patch.object(monitor, "_interruptible_sleep", new_callable=AsyncMock),
            patch("sova.supervisor.pr_monitor.log") as mock_log,
        ):
            with pytest.raises(asyncio.CancelledError):
                await monitor.run_loop()

        assert call_count == 2
        mock_log.warning.assert_called()
        assert mock_log.warning.call_args.args[0] == "pr_monitor.cycle_error"


# ---------------------------------------------------------------------------
# Rate limit detection
# ---------------------------------------------------------------------------


class TestRateLimitDetection:
    @pytest.mark.asyncio
    async def test_detects_rate_limit_comment(self) -> None:
        import json

        gh_output = json.dumps(
            {
                "comments": [
                    {
                        "author": {"login": "coderabbitai"},
                        "body": "I'm sorry, you have exceeded the hourly quota for reviews.",
                    },
                ]
            }
        )
        result = ShellResult(returncode=0, stdout=gh_output, stderr="")

        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=result),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            assert await _is_coderabbit_rate_limited(1, repo="o/r") is True

    @pytest.mark.asyncio
    async def test_no_rate_limit_normal_comment(self) -> None:
        import json

        gh_output = json.dumps(
            {
                "comments": [
                    {
                        "author": {"login": "coderabbitai"},
                        "body": "Here is my review of your changes.",
                    },
                ]
            }
        )
        result = ShellResult(returncode=0, stdout=gh_output, stderr="")

        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=result),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            assert await _is_coderabbit_rate_limited(1, repo="o/r") is False

    @pytest.mark.asyncio
    async def test_ignores_non_coderabbit_rate_limit_comment(self) -> None:
        import json

        gh_output = json.dumps(
            {
                "comments": [
                    {
                        "author": {"login": "someuser"},
                        "body": "We hit a rate limit on the API.",
                    },
                ]
            }
        )
        result = ShellResult(returncode=0, stdout=gh_output, stderr="")

        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=result),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            assert await _is_coderabbit_rate_limited(1, repo="o/r") is False

    @pytest.mark.asyncio
    async def test_no_comments(self) -> None:
        import json

        result = ShellResult(returncode=0, stdout=json.dumps({"comments": []}), stderr="")

        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=result),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            assert await _is_coderabbit_rate_limited(1, repo="o/r") is False

    @pytest.mark.asyncio
    async def test_gh_command_failure(self) -> None:
        result = ShellResult(returncode=1, stdout="", stderr="error")

        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=result),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            assert await _is_coderabbit_rate_limited(1, repo="o/r") is False

    @pytest.mark.asyncio
    async def test_malformed_json_returns_false(self) -> None:
        result = ShellResult(returncode=0, stdout="not valid json{", stderr="")

        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=result),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            assert await _is_coderabbit_rate_limited(1, repo="o/r") is False


# ---------------------------------------------------------------------------
# Exception isolation
# ---------------------------------------------------------------------------


class TestExceptionIsolation:
    @pytest.mark.asyncio
    async def test_failing_pr_does_not_block_others(self) -> None:
        monitor = _make_monitor()
        monitor._initialized = True
        monitor._last_state = {}

        prs = [
            _make_pr(1, ComputedPRState.APPROVED),
            _make_pr(2, ComputedPRState.APPROVED),
        ]

        call_count = 0

        async def _rate_limit_side_effect(pr_number: int, **kwargs: object) -> bool:
            nonlocal call_count
            call_count += 1
            if pr_number == 1:
                raise RuntimeError("API error")
            return False

        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                side_effect=_rate_limit_side_effect,
            ),
            patch("sova.ipc.notifications.notify"),
        ):
            await monitor._poll_cycle()

        # PR 2 should still be processed despite PR 1 failing
        assert 2 in monitor._last_state

    @pytest.mark.asyncio
    async def test_transition_error_does_not_block_state_update(self) -> None:
        """If _handle_transition raises for one PR, others still get processed."""
        monitor = _make_monitor()
        monitor._initialized = True
        monitor._last_state = {
            1: PRSnapshot(number=1, computed_state=ComputedPRState.AWAITING_REVIEW, title="A"),
            2: PRSnapshot(number=2, computed_state=ComputedPRState.AWAITING_REVIEW, title="B"),
        }

        prs = [
            _make_pr(1, ComputedPRState.APPROVED),
            _make_pr(2, ComputedPRState.APPROVED),
        ]

        original_handle = monitor._handle_transition
        call_order: list[int] = []

        async def _failing_handle(prev: PRSnapshot | None, curr: PRSnapshot) -> None:
            call_order.append(curr.number)
            if curr.number == 1:
                raise RuntimeError("oops")
            await original_handle(prev, curr)

        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(monitor, "_handle_transition", side_effect=_failing_handle),
            patch("sova.ipc.notifications.notify"),
        ):
            await monitor._poll_cycle()

        assert 1 in monitor._last_state
        assert 2 in monitor._last_state


# ---------------------------------------------------------------------------
# Empty PR list
# ---------------------------------------------------------------------------


class TestEmptyPRList:
    @pytest.mark.asyncio
    async def test_no_prs(self) -> None:
        monitor = _make_monitor()

        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            await monitor._poll_cycle()

        assert monitor._initialized is True
        assert len(monitor._last_state) == 0
        mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# New PR detection (PR appears after initialization)
# ---------------------------------------------------------------------------


class TestNewPRDetection:
    @pytest.mark.asyncio
    async def test_new_pr_with_notifiable_state(self) -> None:
        """A new PR appearing in a notifiable state should trigger notification."""
        monitor = _make_monitor()
        monitor._initialized = True
        monitor._last_state = {}

        prs = [_make_pr(5, ComputedPRState.CI_FAILED, title="Broken build")]

        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=prs,
            ),
            patch(
                "sova.supervisor.pr_monitor._is_coderabbit_rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            await monitor._poll_cycle()

        mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# resolve_gh_env failure handling
# ---------------------------------------------------------------------------


class TestGhEnvFailure:
    @pytest.mark.asyncio
    async def test_retry_handles_gh_env_failure(self) -> None:
        """_retry_coderabbit_review should not crash if resolve_gh_env raises."""
        monitor = _make_monitor()

        with (
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                side_effect=RuntimeError("auth failed"),
            ),
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
        ):
            await monitor._retry_coderabbit_review(1)

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Multi-project PR monitor lifespan
# ---------------------------------------------------------------------------


class TestMultiProjectMonitor:
    def test_failing_project_config_does_not_block_others(self) -> None:
        """One project failing config load should not prevent other monitors."""
        from sova.config.models import NotificationConfig as _NC
        from sova.config.models import PRMonitorConfig as _PMC
        from sova.config.models import ProjectConfig
        from sova.supervisor.pr_monitor import create_monitors_for_projects

        good_cfg = ProjectConfig(
            github_repo="owner/good",
            github_user="user1",
            pr_monitor=_PMC(enabled=True),
            notification=_NC(),
        )

        def _load_config(path: Path) -> ProjectConfig:
            if "bad" in str(path):
                raise RuntimeError("config load failed")
            return good_cfg

        with (
            patch(
                "sova.config.registry.list_projects",
                return_value={"good": "/tmp/good", "bad": "/tmp/bad"},
            ),
            patch("sova.config.loader.load_config", side_effect=_load_config),
            patch("pathlib.Path.is_dir", return_value=True),
        ):
            monitors = create_monitors_for_projects()

        assert len(monitors) == 1
        assert monitors[0].repo == "owner/good"

    def test_skips_disabled_and_unconfigured_projects(self) -> None:
        """Projects with pr_monitor disabled or no github_repo are skipped."""
        from sova.config.models import NotificationConfig as _NC
        from sova.config.models import PRMonitorConfig as _PMC
        from sova.config.models import ProjectConfig
        from sova.supervisor.pr_monitor import create_monitors_for_projects

        disabled_cfg = ProjectConfig(
            github_repo="owner/disabled",
            github_user="user1",
            pr_monitor=_PMC(enabled=False),
            notification=_NC(),
        )
        no_repo_cfg = ProjectConfig(
            github_repo="",
            github_user="user1",
            pr_monitor=_PMC(enabled=True),
            notification=_NC(),
        )

        configs = {"/tmp/disabled": disabled_cfg, "/tmp/norepo": no_repo_cfg}

        with (
            patch(
                "sova.config.registry.list_projects",
                return_value={"disabled": "/tmp/disabled", "norepo": "/tmp/norepo"},
            ),
            patch("sova.config.loader.load_config", side_effect=lambda p: configs[str(p)]),
            patch("pathlib.Path.is_dir", return_value=True),
        ):
            monitors = create_monitors_for_projects()

        assert len(monitors) == 0


# ---------------------------------------------------------------------------
# _NOTIFY_STATES validation
# ---------------------------------------------------------------------------


class TestNotifyStatesValidation:
    def test_all_notify_states_map_to_real_config_fields(self) -> None:
        """Every value in _NOTIFY_STATES must be a valid PRMonitorConfig field."""
        from sova.supervisor.pr_monitor import _NOTIFY_STATES

        for state, flag in _NOTIFY_STATES.items():
            assert flag in PRMonitorConfig.model_fields, f"_NOTIFY_STATES[{state!r}] references unknown field {flag!r}"


# ---------------------------------------------------------------------------
# GitHub API rate limit gate in PR monitor
# ---------------------------------------------------------------------------


class TestPRMonitorRateLimitGate:
    @pytest.mark.asyncio
    async def test_poll_cycle_skips_when_rate_limited(self) -> None:
        """PR monitor should skip its entire poll cycle when the GitHub API is rate limited."""
        from sova.supervisor.github_quota import get_github_quota_tracker

        tracker = get_github_quota_tracker("testuser")
        with patch.object(tracker, "_emit_hit_event"):
            tracker.record_rate_limit_hit()

        monitor = _make_monitor()

        with patch(
            "sova.dashboard.services.pr_service.list_open_prs_with_state",
            new_callable=AsyncMock,
        ) as mock_list:
            await monitor._poll_cycle()

        mock_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_cycle_runs_when_not_rate_limited(self) -> None:
        """PR monitor should run normally when the GitHub API is not rate limited."""
        monitor = _make_monitor()

        with (
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_list,
        ):
            await monitor._poll_cycle()

        mock_list.assert_called_once()


class TestRateCheckCache:
    """Tests for the per-PR CodeRabbit rate-limit check cache."""

    @pytest.mark.asyncio
    async def test_cached_result_skips_api(self) -> None:
        """Second call within TTL should return cached result without API call."""
        import json as _json

        rate_limited_comment = {
            "author": {"login": "coderabbitai"},
            "body": "rate limit exceeded",
        }
        gh_result = ShellResult(
            stdout='{"comments": [' + _json.dumps(rate_limited_comment) + "]}",
            stderr="",
            returncode=0,
        )

        call_count = 0

        async def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return gh_result

        with (
            patch("sova.utils.shell.run", side_effect=mock_run),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            result1 = await _is_coderabbit_rate_limited(42, repo="o/r", github_user="u")
            assert result1 is True
            assert call_count == 1

            result2 = await _is_coderabbit_rate_limited(42, repo="o/r", github_user="u")
            assert result2 is True
            assert call_count == 1  # no additional API call

    @pytest.mark.asyncio
    async def test_different_prs_cached_independently(self) -> None:
        """Cache entries are per-PR, not global."""
        from sova.supervisor.pr_monitor import _rate_check_cache

        gh_result = ShellResult(stdout='{"comments": []}', stderr="", returncode=0)

        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=gh_result),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            await _is_coderabbit_rate_limited(10, repo="o/r")
            await _is_coderabbit_rate_limited(20, repo="o/r")

        assert ("o/r", 10) in _rate_check_cache
        assert ("o/r", 20) in _rate_check_cache

    @pytest.mark.asyncio
    async def test_expired_cache_calls_api_again(self) -> None:
        """Expired cache entries should trigger a fresh API call."""
        import time

        from sova.supervisor.pr_monitor import _rate_check_cache

        _rate_check_cache[("o/r", 99)] = (time.monotonic() - 500, False)  # expired

        gh_result = ShellResult(stdout='{"comments": []}', stderr="", returncode=0)
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=gh_result),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            result = await _is_coderabbit_rate_limited(99, repo="o/r")
        assert result is False
        assert _rate_check_cache[("o/r", 99)][1] is False

    @pytest.mark.asyncio
    async def test_same_pr_different_repos_cached_independently(self) -> None:
        """Same PR number in different repos should have independent cache entries."""
        from sova.supervisor.pr_monitor import _rate_check_cache

        gh_result = ShellResult(stdout='{"comments": []}', stderr="", returncode=0)

        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=gh_result),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            await _is_coderabbit_rate_limited(1, repo="org/repo-a")
            await _is_coderabbit_rate_limited(1, repo="org/repo-b")

        assert ("org/repo-a", 1) in _rate_check_cache
        assert ("org/repo-b", 1) in _rate_check_cache
