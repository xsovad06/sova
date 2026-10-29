"""Tests for sova.utils.process: PID liveness checking."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sova.utils.process import is_process_alive


class TestIsProcessAlive:
    def test_process_lookup_error_means_dead(self) -> None:
        with patch("sova.utils.process.os.kill", side_effect=ProcessLookupError):
            assert is_process_alive(12345) is False

    def test_permission_error_means_alive(self) -> None:
        with patch("sova.utils.process.os.kill", side_effect=PermissionError):
            assert is_process_alive(12345) is True

    def test_successful_signal_means_alive(self) -> None:
        with patch("sova.utils.process.os.kill", return_value=None):
            assert is_process_alive(12345) is True

    def test_other_oserror_means_dead(self) -> None:
        """Any OSError that is not PermissionError keeps the original "dead" reading."""
        with patch("sova.utils.process.os.kill", side_effect=OSError(22, "Invalid argument")):
            assert is_process_alive(12345) is False

    @pytest.mark.parametrize("pid", [0, -1])
    def test_non_positive_pid_is_dead_without_signalling(self, pid: int) -> None:
        """os.kill(0, 0) and os.kill(-1, 0) address process groups and always succeed."""
        with patch("sova.utils.process.os.kill", return_value=None) as kill:
            assert is_process_alive(pid) is False
        kill.assert_not_called()
