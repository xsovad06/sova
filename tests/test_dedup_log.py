"""Tests for sova.utils.dedup_log: warn once per file version, debug on repeats."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

from sova.utils import dedup_log
from sova.utils.dedup_log import (
    FileVersion,
    file_version,
    forget,
    forget_under,
    forget_unless_unchanged,
    prune_directory,
    warn_key,
    warn_once_per_version,
)


def _broken(tmp_path: Path, name: str = "a.json", content: bytes = b"{") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


class TestWarnOncePerVersion:
    def test_first_sight_warns_and_a_repeat_logs_at_debug(self, tmp_path: Path) -> None:
        log = MagicMock()
        seen: dict[str, FileVersion] = {}
        path = _broken(tmp_path)

        warn_once_per_version(log, "event.bad", path, seen, error="boom")
        warn_once_per_version(log, "event.bad", path, seen, error="boom")

        log.warning.assert_called_once_with("event.bad", path=str(path), error="boom")
        log.debug.assert_called_once_with("event.bad", path=str(path), error="boom")

    def test_a_new_write_is_a_new_version_and_warns_again(self, tmp_path: Path) -> None:
        log = MagicMock()
        seen: dict[str, FileVersion] = {}
        path = _broken(tmp_path)

        warn_once_per_version(log, "event.bad", path, seen)
        path.write_bytes(b"{{")
        warn_once_per_version(log, "event.bad", path, seen)

        assert log.warning.call_count == 2
        log.debug.assert_not_called()

    def test_a_file_that_cannot_be_stated_always_warns_and_is_not_recorded(self, tmp_path: Path) -> None:
        log = MagicMock()
        seen: dict[str, FileVersion] = {}
        missing = tmp_path / "gone.json"

        warn_once_per_version(log, "event.bad", missing, seen)
        warn_once_per_version(log, "event.bad", missing, seen)

        assert log.warning.call_count == 2
        assert seen == {}

    def test_two_spellings_of_one_directory_share_an_entry(self, tmp_path: Path) -> None:
        log = MagicMock()
        seen: dict[str, FileVersion] = {}
        real = tmp_path / "real"
        real.mkdir()
        path = _broken(real)
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        warn_once_per_version(log, "event.bad", path, seen)
        warn_once_per_version(log, "event.bad", link / path.name, seen)

        log.warning.assert_called_once()
        assert len(seen) == 1


class TestForgetting:
    def test_forget_drops_the_entry(self, tmp_path: Path) -> None:
        seen: dict[str, FileVersion] = {}
        path = _broken(tmp_path)
        warn_once_per_version(MagicMock(), "e", path, seen)

        forget(path, seen)

        assert seen == {}

    def test_forget_unless_unchanged_keeps_the_entry_of_the_same_version(self, tmp_path: Path) -> None:
        seen: dict[str, FileVersion] = {}
        path = _broken(tmp_path)
        warn_once_per_version(MagicMock(), "e", path, seen)

        forget_unless_unchanged(path, seen)

        assert warn_key(path) in seen

    def test_forget_unless_unchanged_drops_the_entry_of_an_older_version(self, tmp_path: Path) -> None:
        seen: dict[str, FileVersion] = {}
        path = _broken(tmp_path)
        warn_once_per_version(MagicMock(), "e", path, seen)
        os.utime(path, (1, 1))

        forget_unless_unchanged(path, seen)

        assert seen == {}

    def test_prune_directory_drops_only_files_in_that_directory_that_are_not_live(self, tmp_path: Path) -> None:
        seen: dict[str, FileVersion] = {}
        here = tmp_path / "here"
        there = tmp_path / "there"
        here.mkdir()
        there.mkdir()
        gone, kept, other = _broken(here, "gone.json"), _broken(here, "kept.json"), _broken(there, "other.json")
        for path in (gone, kept, other):
            warn_once_per_version(MagicMock(), "e", path, seen)

        prune_directory(here, [kept], seen)

        assert set(seen) == {warn_key(kept), warn_key(other)}

    def test_forget_under_drops_entries_at_any_depth_below_a_directory_and_no_others(self, tmp_path: Path) -> None:
        seen: dict[str, FileVersion] = {}
        project = tmp_path / "project"
        (project / "sub" / "deep").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        inside = [_broken(project, "a.json"), _broken(project / "sub" / "deep", "b.json")]
        outside = _broken(elsewhere, "c.json")
        for path in (*inside, outside):
            warn_once_per_version(MagicMock(), "e", path, seen)

        forget_under(project, seen)

        assert set(seen) == {warn_key(outside)}


def test_file_version_is_none_for_a_missing_path(tmp_path: Path) -> None:
    assert file_version(tmp_path / "nope") is None
    assert dedup_log.file_version(_broken(tmp_path)) is not None
