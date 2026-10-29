"""Tests for sova.dashboard.services.handoff_service: unreadable handoff surfacing."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sova.dashboard.services import handoff_service
from sova.ipc import handoff as ipc_handoff
from sova.utils.dedup_log import warn_key


def _control_dir(project_dir: Path) -> Path:
    d = project_dir / ".claude" / "agent-control"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestGetAllHandoffs:
    def test_valid_handoff_included(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        (control / "handoff-1.json").write_text('{"issue": "1", "created_at": "2024-01-01"}', encoding="utf-8")

        results = handoff_service.get_all_handoffs(tmp_path)

        assert len(results) == 1
        assert results[0]["issue"] == "1"

    def test_corrupt_handoff_reported_via_unreadable_out(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        bad = control / "handoff-2.json"
        bad.write_text("{not json", encoding="utf-8")

        unreadable: list[str] = []
        results = handoff_service.get_all_handoffs(tmp_path, unreadable_out=unreadable)

        assert results == []
        assert unreadable == [str(bad)]

    @pytest.mark.parametrize("content", ["[]", '"x"', "1", "null", "true"])
    def test_non_object_json_is_unreadable_not_a_crash(self, tmp_path: Path, content: str) -> None:
        """Valid JSON that is not an object used to reach the created_at sort and raise AttributeError."""
        control = _control_dir(tmp_path)
        (control / "handoff-1.json").write_text('{"issue": "1", "created_at": "2024-01-01"}', encoding="utf-8")
        odd = control / "handoff-2.json"
        odd.write_text(content, encoding="utf-8")

        unreadable: list[str] = []
        results = handoff_service.get_all_handoffs(tmp_path, unreadable_out=unreadable)

        assert [h["issue"] for h in results] == ["1"]
        assert unreadable == [str(odd)]

    def test_non_object_json_is_logged_with_its_path(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        odd = control / "handoff-3.json"
        odd.write_text("[]", encoding="utf-8")

        with patch.object(ipc_handoff, "log") as mock_log:
            assert handoff_service.read_handoff_json(odd) is None

        assert mock_log.warning.call_count == 1
        assert mock_log.warning.call_args.kwargs["path"] == str(odd)
        assert "list" in mock_log.warning.call_args.kwargs["reason"]

    def test_corrupt_handoff_does_not_abort_other_files(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        (control / "handoff-1.json").write_text('{"issue": "1", "created_at": "2024-01-01"}', encoding="utf-8")
        (control / "handoff-2.json").write_text("{not json", encoding="utf-8")

        results = handoff_service.get_all_handoffs(tmp_path)

        assert len(results) == 1
        assert results[0]["issue"] == "1"


class TestSchemaInvalidHandoffAcrossReaders:
    """The dashboard accepts any JSON object; the pipeline reader also checks DashboardHandoff."""

    def test_the_two_readers_warn_once_in_total_for_a_schema_invalid_object(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        bad = control / "handoff-15.json"
        bad.write_text('{"issue": "15", "source": "reviewer"}', encoding="utf-8")

        unreadable: list[str] = []
        schema_invalid: list[str] = []
        with patch.object(ipc_handoff, "log") as mock_log:
            for _ in range(3):
                schema_invalid.clear()
                dashboard = handoff_service.get_all_handoffs(
                    tmp_path, unreadable_out=unreadable, schema_invalid_out=schema_invalid
                )
                pipeline = ipc_handoff.read_all_handoff_files(tmp_path)

        assert mock_log.warning.call_count == 1
        assert pipeline == []
        # The dashboard is lenient by design, so it still renders the object and is not "unreadable"...
        assert [h["issue"] for h in dashboard] == ["15"]
        assert unreadable == []
        # ...but it reports the disagreement: the pipeline skips this file, so it must not look healthy.
        assert schema_invalid == [str(bad)]

    def test_a_rewritten_schema_invalid_object_warns_again(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        bad = control / "handoff-16.json"
        bad.write_text('{"issue": "16"}', encoding="utf-8")

        with patch.object(ipc_handoff, "log") as mock_log:
            ipc_handoff.read_all_handoff_files(tmp_path)
            handoff_service.get_all_handoffs(tmp_path)
            bad.write_text('{"issue": "16", "source": "still missing the rest"}', encoding="utf-8")
            handoff_service.get_all_handoffs(tmp_path)
            ipc_handoff.read_all_handoff_files(tmp_path)

        assert mock_log.warning.call_count == 2


class TestSchemaInvalidReporting:
    def test_a_schema_valid_handoff_is_not_reported(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        good = ipc_handoff.DashboardHandoff(source="developer", status="completed", issue="21", summary="done")
        ipc_handoff.write_handoff_file(tmp_path, good)
        assert (control / "handoff-21.json").exists()

        schema_invalid: list[str] = []
        results = handoff_service.get_all_handoffs(tmp_path, schema_invalid_out=schema_invalid)

        assert [h["issue"] for h in results] == ["21"]
        assert schema_invalid == []

    def test_a_legacy_shape_is_rendered_and_reported(self, tmp_path: Path) -> None:
        """An action keyed by `action` rather than `id` renders in the dashboard but fails the pipeline schema."""
        control = _control_dir(tmp_path)
        legacy = control / "handoff-22.json"
        legacy.write_text(
            '{"source": "developer", "status": "awaiting_action", "issue": "22", "summary": "s",'
            ' "next_actions": [{"action": "review", "label": "Review"}]}',
            encoding="utf-8",
        )

        schema_invalid: list[str] = []
        results = handoff_service.get_all_handoffs(tmp_path, schema_invalid_out=schema_invalid)

        assert [h["issue"] for h in results] == ["22"]
        assert schema_invalid == [str(legacy)]

    def test_an_unreadable_file_is_not_also_reported_as_schema_invalid(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        (control / "handoff-23.json").write_text("{not json", encoding="utf-8")

        unreadable: list[str] = []
        schema_invalid: list[str] = []
        handoff_service.get_all_handoffs(tmp_path, unreadable_out=unreadable, schema_invalid_out=schema_invalid)

        assert len(unreadable) == 1
        assert schema_invalid == []


class TestUnreadableWarningCarriesNoFileContent:
    """A traceback frame holds the file text and a validation error holds its input, so neither is logged."""

    SECRET = "ghp_supersecrettoken1234567890"

    def test_a_json_decode_failure_logs_a_short_error_without_exc_info(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        bad = control / "handoff-24.json"
        bad.write_text('{"summary": "' + self.SECRET + '"', encoding="utf-8")

        with patch.object(ipc_handoff, "log") as mock_log:
            ipc_handoff.read_all_handoff_files(tmp_path)

        kwargs = mock_log.warning.call_args.kwargs
        assert "exc_info" not in kwargs
        assert kwargs["error"].startswith("JSONDecodeError")
        assert self.SECRET not in kwargs["error"]

    def test_a_validation_failure_names_the_field_but_not_the_value(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        bad = control / "handoff-25.json"
        bad.write_text(
            '{"source": "developer", "status": "' + self.SECRET + '", "summary": "s"}',
            encoding="utf-8",
        )

        with patch.object(ipc_handoff, "log") as mock_log:
            ipc_handoff.read_all_handoff_files(tmp_path)

        kwargs = mock_log.warning.call_args.kwargs
        assert "exc_info" not in kwargs
        assert kwargs["error"].startswith("ValidationError: status")
        assert self.SECRET not in kwargs["error"]
        assert len(kwargs["error"]) <= 200


class TestNewestHandoffRace:
    def test_a_candidate_cleared_between_the_glob_and_the_stat_is_skipped(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        keep = ipc_handoff.DashboardHandoff(source="developer", status="completed", issue="26", summary="kept")
        ipc_handoff.write_handoff_file(tmp_path, keep)
        racing = control / "handoff-27.json"
        racing.write_text("{}", encoding="utf-8")
        real_glob = Path.glob

        def glob_then_clear(self: Path, pattern: str):
            found = list(real_glob(self, pattern))
            racing.unlink(missing_ok=True)
            return iter(found)

        with patch.object(Path, "glob", glob_then_clear):
            result = ipc_handoff.read_handoff_file(tmp_path)

        assert result is not None
        assert result.issue == "26"

    def test_every_candidate_cleared_reads_as_no_handoff(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        racing = control / "handoff-28.json"
        racing.write_text("{}", encoding="utf-8")
        real_glob = Path.glob

        def glob_then_clear(self: Path, pattern: str):
            found = list(real_glob(self, pattern))
            racing.unlink(missing_ok=True)
            return iter(found)

        with patch.object(Path, "glob", glob_then_clear):
            assert ipc_handoff.read_handoff_file(tmp_path) is None


class TestWarnKeysShareOneSpace:
    def test_the_dashboard_prunes_the_pipelines_entry_when_the_two_spell_the_directory_differently(
        self, tmp_path: Path
    ) -> None:
        real = tmp_path / "real"
        corrupt = _control_dir(real) / "handoff-29.json"
        corrupt.write_text("{not json", encoding="utf-8")
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        ipc_handoff.read_all_handoff_files(link)
        assert warn_key(corrupt) in ipc_handoff._warned_unreadable

        corrupt.unlink()
        handoff_service.get_all_handoffs(real)

        assert warn_key(corrupt) not in ipc_handoff._warned_unreadable


class TestCreatedAtSortIsTotal:
    def test_mixed_created_at_types_do_not_break_the_poll(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        (control / "handoff-17.json").write_text('{"issue": "17", "created_at": "2024-01-01"}', encoding="utf-8")
        (control / "handoff-18.json").write_text('{"issue": "18", "created_at": 1700000000}', encoding="utf-8")
        (control / "handoff-19.json").write_text('{"issue": "19", "created_at": null}', encoding="utf-8")

        results = handoff_service.get_all_handoffs(tmp_path)

        assert sorted(h["issue"] for h in results) == ["17", "18", "19"]


class TestStaleTempFileSweep:
    def test_archive_removes_a_temp_file_left_by_a_killed_write(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        leftover = control / ".handoff-20.json.deadbeef.tmp"
        leftover.write_text("{", encoding="utf-8")
        old = leftover.stat().st_mtime - 3600
        os.utime(leftover, (old, old))
        inflight = control / ".handoff-21.json.cafe.tmp"
        inflight.write_text("{", encoding="utf-8")

        handoff_service.archive_handoff(tmp_path)

        assert not leftover.exists()
        assert inflight.exists(), "a write that is happening right now must not be swept"


class TestReadCachedPath:
    def test_corrupt_file_warns_once_per_version(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        bad = control / "handoff-3.json"
        bad.write_text("{not json", encoding="utf-8")

        ck = "test-cache-key"
        with patch.object(ipc_handoff, "log") as mock_log:
            first = handoff_service._read_cached_path(bad, ck)
            second = handoff_service._read_cached_path(bad, ck)

        assert first is None
        assert second is None
        assert mock_log.warning.call_count == 1
        assert mock_log.warning.call_args.kwargs["path"] == str(bad)

    def test_failed_read_is_not_cached(self, tmp_path: Path) -> None:
        """A read that caught the file mid-write must not hide the completed write
        when the filesystem reports the same mtime for both."""
        control = _control_dir(tmp_path)
        hf = control / "handoff-4.json"
        hf.write_text("", encoding="utf-8")
        mtime = hf.stat().st_mtime

        ck = "test-cache-key-2"
        assert handoff_service._read_cached_path(hf, ck) is None

        hf.write_text('{"issue": "4"}', encoding="utf-8")
        os.utime(hf, (mtime, mtime))

        assert handoff_service._read_cached_path(hf, ck) == {"issue": "4"}


class TestUnreadableHandoffIsPreserved:
    """clear_handoff() deletes what archive_handoff() returns, so an unreadable file is copied first."""

    def test_clear_all_archives_a_non_utf8_handoff_before_deleting_it(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        raw = b'{"issue": "5", "note": "caf\xe9"}'
        (control / "handoff-5.json").write_bytes(raw)

        assert handoff_service.clear_handoff(tmp_path) is True

        assert not (control / "handoff-5.json").exists()
        archived = list((control / "handoff-archive").glob("*.json"))
        assert len(archived) == 1
        assert "unreadable" in archived[0].name
        assert archived[0].read_bytes() == raw

    def test_unreadable_does_not_replace_the_returned_handoff(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        (control / "handoff-1.json").write_text('{"issue": "1", "source": "developer"}', encoding="utf-8")
        (control / "handoff-2.json").write_bytes(b"\xff\xfe not json")

        archived = handoff_service.archive_handoff(tmp_path)

        assert archived == {"issue": "1", "source": "developer"}
        assert len(list((control / "handoff-archive").glob("*.json"))) == 2

    def test_readable_handoff_keeps_its_source_in_the_archive_name(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        (control / "handoff-7.json").write_text('{"issue": "7", "source": "reviewer"}', encoding="utf-8")

        handoff_service.archive_handoff(tmp_path)

        assert [p.name.split("_", 1)[1] for p in (control / "handoff-archive").glob("*.json")] == [
            "reviewer_7_handoff-7.json"
        ]


class TestReadHandoffFileEdges:
    def test_vanished_file_is_not_unreadable(self, tmp_path: Path) -> None:
        """A file deleted between the glob and the read was handled (archived), not corrupt."""
        gone = tmp_path / "handoff-9.json"

        with patch.object(ipc_handoff, "log") as mock_log:
            assert handoff_service.read_handoff_json(gone) is None

        mock_log.warning.assert_not_called()
        assert warn_key(gone) not in ipc_handoff._warned_unreadable

    def test_a_file_cleared_between_the_glob_and_the_read_is_not_listed(self, tmp_path: Path) -> None:
        """Auto-handoff clears a handoff before spawning the next agent, so this race is normal operation."""
        control = _control_dir(tmp_path)
        racing = control / "handoff-14.json"
        racing.write_text('{"issue": "14"}', encoding="utf-8")
        real_read = handoff_service.read_handoff_json

        def clear_then_read(path: Path) -> dict | None:
            path.unlink()
            return real_read(path)

        unreadable: list[str] = []
        with (
            patch.object(handoff_service, "read_handoff_json", side_effect=clear_then_read),
            patch.object(ipc_handoff, "log") as mock_log,
        ):
            results = handoff_service.get_all_handoffs(tmp_path, unreadable_out=unreadable)

        assert results == []
        assert unreadable == []
        mock_log.warning.assert_not_called()

    def test_a_recovered_file_clears_its_warned_version(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        hf = control / "handoff-8.json"
        hf.write_text("{not json", encoding="utf-8")
        assert handoff_service.read_handoff_json(hf) is None
        assert warn_key(hf) in ipc_handoff._warned_unreadable

        hf.write_text('{"issue": "8"}', encoding="utf-8")

        assert handoff_service.read_handoff_json(hf) == {"issue": "8"}
        assert warn_key(hf) not in ipc_handoff._warned_unreadable


class TestWarnedUnreadablePruning:
    def test_scan_drops_entries_for_files_that_no_longer_exist(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        gone = control / "handoff-10.json"
        kept = control / "handoff-11.json"
        gone.write_text("{not json", encoding="utf-8")
        kept.write_text("{not json", encoding="utf-8")
        assert handoff_service.get_all_handoffs(tmp_path) == []
        assert {warn_key(gone), warn_key(kept)} <= set(ipc_handoff._warned_unreadable)

        gone.unlink()
        handoff_service.get_all_handoffs(tmp_path)

        assert warn_key(gone) not in ipc_handoff._warned_unreadable
        assert warn_key(kept) in ipc_handoff._warned_unreadable

    def test_scan_of_a_missing_control_dir_drops_its_entries(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        hf = control / "handoff-12.json"
        hf.write_text("{not json", encoding="utf-8")
        handoff_service.get_all_handoffs(tmp_path)
        assert warn_key(hf) in ipc_handoff._warned_unreadable

        hf.unlink()
        control.rmdir()
        handoff_service.get_all_handoffs(tmp_path)

        assert warn_key(hf) not in ipc_handoff._warned_unreadable

    def test_scan_leaves_another_projects_entries_alone(self, tmp_path: Path) -> None:
        other = tmp_path / "other"
        other_hf = _control_dir(other) / "handoff-13.json"
        other_hf.write_text("{not json", encoding="utf-8")
        handoff_service.get_all_handoffs(other)
        mine = tmp_path / "mine"
        _control_dir(mine)

        handoff_service.get_all_handoffs(mine)

        assert warn_key(other_hf) in ipc_handoff._warned_unreadable


class TestReadCachedPathEdges:
    def test_stat_failure_reads_as_no_handoff(self) -> None:
        hf = MagicMock(spec=Path)
        hf.exists.return_value = True
        hf.stat.side_effect = OSError("stale file handle")

        assert handoff_service._read_cached_path(hf, "stat-failure-key") is None

    def test_unchanged_file_is_served_from_cache(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        hf = control / "handoff-6.json"
        hf.write_text('{"issue": "6"}', encoding="utf-8")
        ck = "cache-hit-key"

        with patch.object(handoff_service, "read_handoff_json", wraps=handoff_service.read_handoff_json) as spy:
            first = handoff_service._read_cached_path(hf, ck)
            second = handoff_service._read_cached_path(hf, ck)

        assert first == second == {"issue": "6"}
        spy.assert_called_once()
