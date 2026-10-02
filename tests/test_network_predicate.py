"""Tests for the shared network-outage predicate.

The positive cases are the verbatim ``TaskRun.error_message`` values recorded
during a real 20-minute home-internet outage on 2026-10-01 (Gwym project runs
1610-1613, plus the dashboard's own queue-fetch failure from the same window).
Keeping them byte-exact is the point: a reworded pattern table that no longer
matches the text SOVA actually persists would silently stop detecting outages.
"""

from __future__ import annotations

import pytest

from sova.utils.network import looks_like_network_outage
from sova.utils.shell import ShellResult

# Verbatim from Gwym's .claude/sova.db, task_runs.error_message.
RUN_1610_CLAUDE_CLI_DNS = (
    "Claude CLI failed (exit 1): terminal_reason=api_error; is_error=true; "
    "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"
)
RUN_1611_GIT_PUSH_SSH = (
    "Command failed: git push -u origin feat/issue-659\n"
    "Exit code: 128\n"
    "stderr: ssh: connect to host github.com port 22: Operation timed out\n"
    "fatal: Could not read from remote repository.\n\n"
    "Please make sure you have the correct access rights\n"
    "and the repository exists.\n"
)
RUN_1612_ADAPTER_FETCH = (
    "Process exited with code 1; last output:   155 │   │   │   issue = json.loads(result.stdout) | "
    "[stderr] RuntimeError: Failed to fetch issue #659: error connecting to api.github.com | "
    "[stderr] check your internet connection or https://githubstatus.com"
)
DASHBOARD_QUEUE_FETCH = (
    "Failed to fetch issues from xsovad06/sova: error connecting to api.github.com\n"
    "check your internet connection or https://githubstatus.com\n"
)


class TestRealOutageMessages:
    @pytest.mark.parametrize(
        "message",
        [
            RUN_1610_CLAUDE_CLI_DNS,
            RUN_1611_GIT_PUSH_SSH,
            RUN_1612_ADAPTER_FETCH,
            DASHBOARD_QUEUE_FETCH,
        ],
    )
    def test_observed_outage_message_is_detected(self, message: str) -> None:
        assert looks_like_network_outage(message) is True

    def test_curly_apostrophe_variant(self) -> None:
        # The Claude CLI emits Unicode punctuation in the same sentence, so the
        # curly form has to match wherever the straight one does.
        assert looks_like_network_outage("API Error: Can’t reach the API server") is True


class TestAmbiguousPatternsRequireCorroboration:
    def test_bare_operation_timed_out_is_not_an_outage(self) -> None:
        # A lint or test step reporting a local timeout must not read as an outage.
        assert looks_like_network_outage("pytest: operation timed out after 300s") is False

    def test_operation_timed_out_with_remote_endpoint_is_an_outage(self) -> None:
        assert looks_like_network_outage("failed to connect to github.com port 443: Operation timed out") is True

    def test_bare_connection_refused_is_not_an_outage(self) -> None:
        assert looks_like_network_outage("test fixture: connection refused on 127.0.0.1:5432") is False


class TestFalsePositiveGuards:
    @pytest.mark.parametrize(
        "message",
        [
            # Missing CLI binary: a local misconfiguration that never self-heals
            # on reconnect, so it must never be classified as an outage.
            "FileNotFoundError: [Errno 2] No such file or directory: 'claude'",
            # SOVA's own .claude/ directory sits in the path of most git and
            # worktree errors; naming it corroborates nothing.
            "worktree directory does not exist: /repo/.claude/worktrees/659",
            # Names github.com over https, but the failure is authorization.
            "remote: Permission to xsovad06/sova.git denied\n"
            "fatal: unable to access 'https://github.com/xsovad06/sova/': "
            "The requested URL returned error: 403",
            # A real push rejection, not a transport failure.
            "! [rejected] main -> main (non-fast-forward)",
            "step_hard_timeout",
            "fix_llm_failed on cycle 1: bad response",
        ],
    )
    def test_non_network_failure_is_not_an_outage(self, message: str) -> None:
        assert looks_like_network_outage(message) is False

    def test_errno_matching_is_word_bounded(self) -> None:
        # Both halves of one trap, kept as a pair so neither can regress alone:
        # Python's FileNotFoundError lowercases to "fil-enotfound-error", so a
        # plain substring test on the errno code reports a missing CLI binary as
        # an outage, while the real Claude CLI message parenthesizes the code.
        assert looks_like_network_outage("FileNotFoundError: [Errno 2] No such file") is False
        assert looks_like_network_outage("API Error: check your internet or DNS (ENOTFOUND)") is True

    @pytest.mark.parametrize("message", [None, "", "   "])
    def test_missing_input_is_not_an_outage(self, message: str | None) -> None:
        # The contract is "does this name an outage", never "did something
        # fail": absent input is an unknown, not a positive.
        assert looks_like_network_outage(message) is False


class TestShellResultProperty:
    def test_detects_outage_in_stderr(self) -> None:
        result = ShellResult(returncode=1, stdout="", stderr="error connecting to api.github.com")
        assert result.is_network_unreachable is True

    def test_detects_outage_in_stdout(self) -> None:
        result = ShellResult(returncode=128, stdout="ssh: connect to host github.com port 22", stderr="")
        assert result.is_network_unreachable is True

    def test_successful_command_is_never_unreachable(self) -> None:
        # Guards the same way is_rate_limited does: a command that succeeded
        # cannot have failed for transport reasons, whatever its output says.
        result = ShellResult(returncode=0, stdout="", stderr="error connecting to api.github.com")
        assert result.is_network_unreachable is False

    def test_rate_limited_is_not_unreachable(self) -> None:
        # A throttled call reached the API; an unreachable one never left the
        # machine. The two states must not alias.
        result = ShellResult(returncode=1, stdout="", stderr="API rate limit exceeded for user")
        assert result.is_rate_limited is True
        assert result.is_network_unreachable is False
