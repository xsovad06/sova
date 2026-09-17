"""Tests for sova.utils.log_dedup."""

from __future__ import annotations

from unittest.mock import MagicMock

from sova.utils import log_dedup
from sova.utils.log_dedup import warn_once


class TestWarnOnce:
    def test_first_call_logs(self) -> None:
        log = MagicMock()
        warn_once(log, "some.event", "key1", detail="x")
        log.warning.assert_called_once_with("some.event", detail="x")

    def test_repeat_within_ttl_is_suppressed(self) -> None:
        log = MagicMock()
        warn_once(log, "some.event", "key1")
        warn_once(log, "some.event", "key1")
        log.warning.assert_called_once()

    def test_different_keys_both_log(self) -> None:
        log = MagicMock()
        warn_once(log, "some.event", "key1")
        warn_once(log, "some.event", "key2")
        assert log.warning.call_count == 2

    def test_different_events_same_key_both_log(self) -> None:
        log = MagicMock()
        warn_once(log, "event.a", "key1")
        warn_once(log, "event.b", "key1")
        assert log.warning.call_count == 2

    def test_repeat_after_ttl_expiry_logs_again(self) -> None:
        log = MagicMock()
        warn_once(log, "some.event", "key1", ttl_seconds=0.0)
        warn_once(log, "some.event", "key1", ttl_seconds=0.0)
        assert log.warning.call_count == 2

    def test_reset_clears_state(self) -> None:
        log = MagicMock()
        warn_once(log, "some.event", "key1")
        log_dedup.reset()
        warn_once(log, "some.event", "key1")
        assert log.warning.call_count == 2

    def test_oldest_entry_evicted_past_max_entries(self) -> None:
        """A long-uptime process must not grow this state without bound."""
        log = MagicMock()
        log_dedup.reset()
        try:
            for i in range(log_dedup._MAX_ENTRIES):
                warn_once(log, "some.event", f"key{i}")
            assert len(log_dedup._seen) == log_dedup._MAX_ENTRIES

            # One more distinct key evicts the oldest ("key0") rather than growing.
            warn_once(log, "some.event", "key_new")
            assert len(log_dedup._seen) == log_dedup._MAX_ENTRIES
            assert "some.event:key0" not in log_dedup._seen

            # The evicted key is no longer deduped: it logs again.
            log.reset_mock()
            warn_once(log, "some.event", "key0")
            log.warning.assert_called_once()
        finally:
            log_dedup.reset()
