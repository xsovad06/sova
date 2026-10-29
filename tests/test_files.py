"""Tests for sova.utils.files: UTF-8 file reading helpers."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from sova.utils.files import read_json_or_none, read_text_or_none


class TestReadTextOrNone:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert read_text_or_none(tmp_path / "missing.txt") is None

    def test_reads_utf8_content(self, tmp_path: Path) -> None:
        path = tmp_path / "file.txt"
        path.write_text("héllo", encoding="utf-8")
        assert read_text_or_none(path) == "héllo"

    def test_non_utf8_content_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "file.txt"
        path.write_bytes(b"\xff\xfe\x00bad")
        assert read_text_or_none(path) is None


class TestReadJsonOrNone:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert read_json_or_none(tmp_path / "missing.json") is None

    def test_parses_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "file.json"
        path.write_text('{"a": 1}', encoding="utf-8")
        assert read_json_or_none(path) == {"a": 1}

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "file.json"
        path.write_text("{not json", encoding="utf-8")
        assert read_json_or_none(path) is None

    def test_non_utf8_content_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "file.json"
        path.write_bytes(b"\xff\xfe\x00bad")
        assert read_json_or_none(path) is None


class TestWriteTextAtomic:
    def test_writes_the_content_as_utf8(self, tmp_path: Path) -> None:
        from sova.utils.files import write_text_atomic

        target = tmp_path / "out.json"
        write_text_atomic(target, '{"note": "café"}')

        assert target.read_bytes() == '{"note": "café"}'.encode()

    def test_replaces_an_existing_file_and_keeps_its_mode(self, tmp_path: Path) -> None:
        import stat

        from sova.utils.files import write_text_atomic

        target = tmp_path / "out.json"
        target.write_text("old", encoding="utf-8")
        target.chmod(0o644)

        write_text_atomic(target, "new")

        assert target.read_text(encoding="utf-8") == "new"
        assert stat.S_IMODE(target.stat().st_mode) == 0o644

    def test_leaves_no_temp_file_behind(self, tmp_path: Path) -> None:
        from sova.utils.files import write_text_atomic

        write_text_atomic(tmp_path / "out.json", "x")

        assert [p.name for p in tmp_path.iterdir()] == ["out.json"]

    def test_a_failed_replace_keeps_the_old_file_and_removes_the_temp_file(self, tmp_path: Path) -> None:
        """A reader never sees a partial file, and a failed write never destroys the previous one."""
        from unittest.mock import patch

        from sova.utils.files import write_text_atomic

        target = tmp_path / "out.json"
        target.write_text("old", encoding="utf-8")

        with patch("sova.utils.files.os.replace", side_effect=OSError("disk full")), pytest.raises(OSError):
            write_text_atomic(target, "new")

        assert target.read_text(encoding="utf-8") == "old"
        assert [p.name for p in tmp_path.iterdir()] == ["out.json"]

    def test_the_descriptor_is_closed_and_the_temp_file_removed_when_fdopen_fails(self, tmp_path: Path) -> None:
        from sova.utils import files
        from sova.utils.files import write_text_atomic

        opened: list[int] = []
        closed: list[int] = []
        real_open, real_close = os.open, os.close

        def spy_open(*args: object, **kwargs: object) -> int:
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def spy_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        with (
            patch.object(files.os, "open", spy_open),
            patch.object(files.os, "close", spy_close),
            patch.object(files.os, "fdopen", side_effect=ValueError("bad mode")),
        ):
            with pytest.raises(ValueError, match="bad mode"):
                write_text_atomic(tmp_path / "out.json", "x")

        assert opened and closed == opened
        assert list(tmp_path.iterdir()) == []

    def test_the_content_is_synced_before_the_rename(self, tmp_path: Path) -> None:
        from sova.utils import files
        from sova.utils.files import write_text_atomic

        order: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def spy_fsync(fd: int) -> None:
            order.append("fsync")
            real_fsync(fd)

        def spy_replace(src: Path, dst: Path) -> None:
            order.append("replace")
            real_replace(src, dst)

        with patch.object(files.os, "fsync", spy_fsync), patch.object(files.os, "replace", spy_replace):
            write_text_atomic(tmp_path / "out.json", "x")

        assert order == ["fsync", "replace"]

    def test_temp_file_is_hidden_from_a_handoff_glob(self, tmp_path: Path) -> None:
        """The reader globs ``handoff-*.json``; the in-flight temp file must not match it."""
        import os
        from unittest.mock import patch

        from sova.utils.files import write_text_atomic

        real_replace = os.replace
        seen: list[list[str]] = []

        def spy_replace(src: str, dst: Path) -> None:
            seen.append(sorted(p.name for p in tmp_path.glob("handoff-*.json")))
            real_replace(src, dst)

        with patch("sova.utils.files.os.replace", side_effect=spy_replace):
            write_text_atomic(tmp_path / "handoff-1.json", "x")

        assert seen == [[]]

    def test_a_new_file_follows_the_umask_like_write_text(self, tmp_path: Path) -> None:
        """mkstemp's fixed 0600 would hide a new handoff from another local reader of agent-control/."""
        import os
        import stat

        from sova.utils.files import write_text_atomic

        old_umask = os.umask(0o022)
        try:
            write_text_atomic(tmp_path / "open.json", "x")
            os.umask(0o077)
            write_text_atomic(tmp_path / "private.json", "x")
        finally:
            os.umask(old_umask)

        assert stat.S_IMODE((tmp_path / "open.json").stat().st_mode) == 0o644
        assert stat.S_IMODE((tmp_path / "private.json").stat().st_mode) == 0o600


class TestRemoveStaleTempFiles:
    def test_removes_old_temp_files_and_keeps_fresh_ones(self, tmp_path: Path) -> None:
        import os
        import time

        from sova.utils.files import remove_stale_temp_files

        stale = tmp_path / ".handoff-1.json.aaaa.tmp"
        fresh = tmp_path / ".handoff-2.json.bbbb.tmp"
        stale.write_text("x", encoding="utf-8")
        fresh.write_text("x", encoding="utf-8")
        old = time.time() - 3600
        os.utime(stale, (old, old))

        assert remove_stale_temp_files(tmp_path, ".handoff*.tmp") == 1

        assert not stale.exists()
        assert fresh.exists()

    def test_leaves_files_that_do_not_match_the_pattern(self, tmp_path: Path) -> None:
        import os
        import time

        from sova.utils.files import remove_stale_temp_files

        real = tmp_path / "handoff-1.json"
        real.write_text("{}", encoding="utf-8")
        old = time.time() - 3600
        os.utime(real, (old, old))

        assert remove_stale_temp_files(tmp_path, ".handoff*.tmp") == 0
        assert real.exists()

    def test_a_missing_directory_removes_nothing(self, tmp_path: Path) -> None:
        from sova.utils.files import remove_stale_temp_files

        assert remove_stale_temp_files(tmp_path / "absent", ".handoff*.tmp") == 0
