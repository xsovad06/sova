"""Tests for sova.utils.files: UTF-8 file reading helpers."""

from __future__ import annotations

from pathlib import Path

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
