"""Tests for sova.dashboard.services.handoff_service: unreadable handoff surfacing."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sova.dashboard.services import handoff_service
from sova.dashboard.services.handoff_service import ActionOutcome
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

        handoff_service.archive_all_handoffs(tmp_path)

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
    """The clear functions delete the files they archive, so an unreadable file is copied first."""

    def test_clear_all_archives_a_non_utf8_handoff_before_deleting_it(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        raw = b'{"issue": "5", "note": "caf\xe9"}'
        (control / "handoff-5.json").write_bytes(raw)

        assert handoff_service.clear_all_handoffs(tmp_path) is True

        assert not (control / "handoff-5.json").exists()
        archived = list((control / "handoff-archive").glob("*.json"))
        assert len(archived) == 1
        assert "unreadable" in archived[0].name
        assert archived[0].read_bytes() == raw

    def test_unreadable_does_not_replace_the_returned_handoff(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        (control / "handoff-1.json").write_text('{"issue": "1", "source": "developer"}', encoding="utf-8")
        (control / "handoff-2.json").write_bytes(b"\xff\xfe not json")

        archived = handoff_service.archive_all_handoffs(tmp_path)

        assert archived == {"issue": "1", "source": "developer"}
        assert len(list((control / "handoff-archive").glob("*.json"))) == 2

    def test_readable_handoff_keeps_its_source_in_the_archive_name(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        (control / "handoff-7.json").write_text('{"issue": "7", "source": "reviewer"}', encoding="utf-8")

        handoff_service.archive_all_handoffs(tmp_path)

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


def _write(control: Path, name: str, **data: object) -> Path:
    import json

    path = control / name
    path.write_text(json.dumps({"source": "developer", "status": "awaiting_action", **data}), encoding="utf-8")
    return path


class TestClearHandoffNeedsAKey:
    """An empty key clears nothing; only clear_all_handoffs() clears everything (issue #1569)."""

    @pytest.mark.parametrize("issue", ["", None, "   ", "#", " # "])
    def test_an_empty_key_clears_and_archives_nothing(self, tmp_path: Path, issue: str | None) -> None:
        control = _control_dir(tmp_path)
        files = [_write(control, "handoff-1.json", issue="1"), _write(control, "handoff.json", pr_number=7)]

        assert handoff_service.clear_handoff(tmp_path, issue=issue) is False

        assert all(f.exists() for f in files)
        assert not (control / "handoff-archive").exists()

    def test_a_legacy_file_naming_the_issue_is_cleared_by_that_key(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        legacy = _write(control, "handoff.json", issue="#9")
        other = _write(control, "handoff-1.json", issue="1")

        assert handoff_service.clear_handoff(tmp_path, issue="9") is True

        assert not legacy.exists()
        assert other.exists()

    def test_clear_all_removes_every_file_and_resets_every_cache_entry(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        _write(control, "handoff-1.json", issue="1")
        _write(control, "handoff.json", pr_number=7)
        assert handoff_service.get_handoff(tmp_path, issue="1") is not None

        before = (control / "handoff-1.json").stat()

        assert handoff_service.clear_all_handoffs(tmp_path) is True

        assert handoff_service.get_all_handoffs(tmp_path) == []
        # A rewrite carrying the very same mtime must still be read fresh, not answered from the cache.
        rewritten = _write(control, "handoff-1.json", issue="1", summary="rewritten")
        os.utime(rewritten, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert handoff_service.get_handoff(tmp_path, issue="1")["summary"] == "rewritten"


class TestArchiveHandoffNeedsAKey:
    @pytest.mark.parametrize("issue", ["", None, "   ", "#"])
    def test_an_empty_key_archives_nothing(self, tmp_path: Path, issue: str | None) -> None:
        control = _control_dir(tmp_path)
        _write(control, "handoff-1.json", issue="1")
        _write(control, "handoff.json", pr_number=7)

        assert handoff_service.archive_handoff(tmp_path, issue=issue) is None
        assert not (control / "handoff-archive").exists()

    def test_an_issue_archives_only_its_own_file(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        _write(control, "handoff-1.json", issue="1")
        _write(control, "handoff-2.json", issue="2")

        assert handoff_service.archive_handoff(tmp_path, issue="#1")["issue"] == "1"
        assert len(list((control / "handoff-archive").glob("*.json"))) == 1


class TestClearIssuelessHandoff:
    def test_clears_only_the_pr_only_file(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        legacy = _write(control, "handoff.json", pr_number=7)
        _write(control, "handoff-1.json", issue="1")
        _write(control, "handoff-2.json", issue="2")

        assert handoff_service.clear_issueless_handoff(tmp_path) is True

        assert not legacy.exists()
        assert sorted(h["issue"] for h in handoff_service.get_all_handoffs(tmp_path)) == ["1", "2"]
        assert len(list((control / "handoff-archive").glob("*.json"))) == 1

    def test_keeps_a_legacy_file_that_names_an_issue(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        legacy = _write(control, "handoff.json", issue="3")

        assert handoff_service.clear_issueless_handoff(tmp_path) is False
        assert legacy.exists()


_AGENT_ACTION = {"id": "go", "label": "Go", "mode": "agent", "args": {"issue": "", "pr": 7, "role": "reviewer"}}


class TestExecuteAction:
    """execute_action() clears the handoff only after its start succeeded (issue #1569)."""

    @pytest.fixture(autouse=True)
    def snapshot_invalidate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        monkeypatch.setattr(handoff_service, "get_synthesized_handoff", AsyncMock(return_value=None))
        handoff_service._handoff_caches.clear()
        snapshot = MagicMock()
        monkeypatch.setattr(handoff_service, "invalidate_handoff_snapshot", snapshot)
        return snapshot

    async def test_a_pr_only_handoff_clears_its_own_file_and_leaves_two_others_pending(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import control_service

        control = _control_dir(tmp_path)
        legacy = _write(control, "handoff.json", pr_number=7, next_actions=[_AGENT_ACTION])
        _write(control, "handoff-1.json", issue="1", next_actions=[_AGENT_ACTION])
        _write(control, "handoff-2.json", issue="2", next_actions=[_AGENT_ACTION])

        with patch.object(control_service, "start_agent", AsyncMock(return_value={"status": "started"})) as start:
            result = await handoff_service.execute_action("go", "pr:7")

        # start_agent() would clear before its spawn, which can still fail; execute_action() clears after it.
        assert start.await_args.kwargs["_skip_handoff_clear"] is True
        assert result.outcome is ActionOutcome.STARTED
        assert result.payload["action"] == "Go"
        assert not legacy.exists()
        assert sorted(h["issue"] for h in handoff_service.get_all_handoffs()) == ["1", "2"]

    @pytest.mark.parametrize(
        "refusal",
        [
            {"error": "busy", "code": "no_free_slot"},
            {"status": "error", "code": "issue_conflict", "detail": "busy"},
        ],
    )
    async def test_a_refused_start_leaves_the_handoff_pending_and_a_retry_clears_it(
        self, tmp_path: Path, snapshot_invalidate: MagicMock, refusal: dict
    ) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import control_service

        hf = _write(_control_dir(tmp_path), "handoff-1.json", issue="1", next_actions=[_AGENT_ACTION])
        start = AsyncMock(side_effect=[refusal, {"status": "started"}])

        with patch.object(control_service, "start_agent", start):
            refused = await handoff_service.execute_action("go", "1")
            assert (refused.outcome, refused.detail) == (ActionOutcome.REFUSED, "busy")
            assert hf.exists()
            snapshot_invalidate.assert_not_called()

            retried = await handoff_service.execute_action("go", "1")

        assert retried.outcome is ActionOutcome.STARTED
        assert not hf.exists()
        snapshot_invalidate.assert_called_once()

    @pytest.mark.parametrize(
        ("start_result", "invalidated"), [({"status": "started"}, True), ({"error": "conflict"}, False)]
    )
    async def test_a_synthesized_handoff_drops_its_cache_only_after_a_start(
        self, monkeypatch: pytest.MonkeyPatch, start_result: dict, invalidated: bool
    ) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import control_service

        synthesized = {"source": "pr-review-state", "issue": "4", "pr_number": 9, "next_actions": [_AGENT_ACTION]}
        monkeypatch.setattr(handoff_service, "get_synthesized_handoff", AsyncMock(return_value=synthesized))
        invalidate = MagicMock()
        monkeypatch.setattr(handoff_service, "invalidate_synthesis_cache", invalidate)
        clear = MagicMock()
        monkeypatch.setattr(handoff_service, "clear_handoff", clear)

        with patch.object(control_service, "start_agent", AsyncMock(return_value=start_result)):
            await handoff_service.execute_action("go", "4")

        assert invalidate.called is invalidated
        clear.assert_not_called()

    async def test_an_unsupported_mode_starts_nothing_and_keeps_the_handoff(self, tmp_path: Path) -> None:
        hf = _write(_control_dir(tmp_path), "handoff-1.json", issue="1", next_actions=[{"id": "sh", "mode": "shell"}])

        result = await handoff_service.execute_action("sh", "1")

        assert result.outcome is ActionOutcome.UNSUPPORTED
        assert hf.exists()

    @pytest.mark.parametrize(
        ("start_result", "outcome"),
        [
            ({"error": "Failed to create task run record", "code": "run_record_failed"}, ActionOutcome.FAILED),
            ({"error": "Failed to spawn runtime: boom", "code": "spawn_failed"}, ActionOutcome.FAILED),
            ({"error": "Cannot start PR-based run: worktree isolation failed for PR 7"}, ActionOutcome.FAILED),
            ({"error": "unsupported model", "code": "llm_model_override_unsupported"}, ActionOutcome.FAILED),
            ({"error": "Maximum concurrent agents reached (2)", "code": "no_free_slot"}, ActionOutcome.REFUSED),
            ({"error": "Issue #1 already has an active agent", "code": "issue_conflict"}, ActionOutcome.REFUSED),
            ({"error": "Insufficient memory", "code": "memory_pressure"}, ActionOutcome.REFUSED),
        ],
    )
    async def test_only_a_known_conflict_is_refused_and_anything_else_failed(
        self, tmp_path: Path, start_result: dict, outcome: ActionOutcome
    ) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import control_service

        hf = _write(_control_dir(tmp_path), "handoff-1.json", issue="1", next_actions=[_AGENT_ACTION])

        with patch.object(control_service, "start_agent", AsyncMock(return_value=start_result)):
            result = await handoff_service.execute_action("go", "1")

        assert result.outcome is outcome
        assert result.detail == start_result["error"]
        assert hf.exists()

    @pytest.mark.parametrize(
        ("name", "ref", "fields"),
        [("handoff.json", "pr:7", {"pr_number": 7}), ("handoff-1.json", "1", {"issue": "1"})],
    )
    async def test_a_replacement_written_during_the_start_stays_pending(
        self, tmp_path: Path, name: str, ref: str, fields: dict
    ) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import control_service

        control = _control_dir(tmp_path)
        _write(control, name, next_actions=[_AGENT_ACTION], **fields)

        async def _start(*_args: object, **_kwargs: object) -> dict:
            _write(control, name, summary="newer", **fields)
            return {"status": "started"}

        with patch.object(control_service, "start_agent", AsyncMock(side_effect=_start)):
            result = await handoff_service.execute_action("go", ref)

        assert result.outcome is ActionOutcome.STARTED
        assert [h.get("summary") for h in handoff_service.get_all_handoffs()] == ["newer"]
        assert not (control / "handoff-archive").exists()

    async def test_a_handoff_annotated_by_another_reader_is_still_cleared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reader adding a derived key (``age_seconds``) to the dict it got must not make the clear a no-op."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services import control_service

        hf = _write(_control_dir(tmp_path), "handoff-1.json", issue="1", next_actions=[_AGENT_ACTION])
        real = handoff_service.get_all_handoffs

        def _annotated(*args: object, **kwargs: object) -> list[dict]:
            return [{**h, "age_seconds": 12} for h in real(*args, **kwargs)]

        monkeypatch.setattr(handoff_service, "get_all_handoffs", _annotated)

        with patch.object(control_service, "start_agent", AsyncMock(return_value={"status": "started"})):
            result = await handoff_service.execute_action("go", "1")

        assert result.outcome is ActionOutcome.STARTED
        assert not hf.exists()

    async def test_concurrent_requests_for_one_handoff_start_it_once(self, tmp_path: Path) -> None:
        """The handoff stays listed during the start, so a second request must be refused, not started again."""
        import asyncio
        from unittest.mock import AsyncMock

        from sova.dashboard.services import control_service

        hf = _write(_control_dir(tmp_path), "handoff-1.json", issue="1", next_actions=[_AGENT_ACTION])
        release = asyncio.Event()

        async def _slow_start(*_args: object, **_kwargs: object) -> dict:
            await release.wait()
            return {"status": "started"}

        start = AsyncMock(side_effect=_slow_start)
        with patch.object(control_service, "start_agent", start):
            first = asyncio.create_task(handoff_service.execute_action("go", "1"))
            await asyncio.sleep(0)
            second = await handoff_service.execute_action("go", "1")
            release.set()
            first_result = await first

        start.assert_awaited_once()
        assert second.outcome is ActionOutcome.REFUSED
        assert first_result.outcome is ActionOutcome.STARTED
        assert not hf.exists()

    async def test_a_failed_start_releases_the_handoff_for_a_retry(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import control_service

        _write(_control_dir(tmp_path), "handoff-1.json", issue="1", next_actions=[_AGENT_ACTION])
        start = AsyncMock(side_effect=[RuntimeError("spawn exploded"), {"status": "started"}])

        with patch.object(control_service, "start_agent", start):
            with pytest.raises(RuntimeError):
                await handoff_service.execute_action("go", "1")
            retried = await handoff_service.execute_action("go", "1")

        assert retried.outcome is ActionOutcome.STARTED

    @pytest.mark.parametrize("name", ["start_agent", "start_command"])
    def test_the_facade_forwards_the_skip_clear_flag(self, name: str) -> None:
        """execute_action() patches nothing at the agent_lifecycle level, so the facade must take the flag itself."""
        import inspect

        from sova.dashboard.services import agent_lifecycle, control_service

        assert "_skip_handoff_clear" in inspect.signature(getattr(control_service, name)).parameters
        assert getattr(control_service, name) is getattr(agent_lifecycle, name)

    async def test_a_command_action_starts_without_clearing_first(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import control_service

        action = {"id": "merge", "label": "Merge", "mode": "claude-command", "command": "integrate-pr", "args": {}}
        hf = _write(_control_dir(tmp_path), "handoff-1.json", issue="1", next_actions=[action])

        with patch.object(control_service, "start_command", AsyncMock(return_value={"status": "started"})) as start:
            result = await handoff_service.execute_action("merge", "1")

        assert start.await_args.kwargs["_skip_handoff_clear"] is True
        assert result.outcome is ActionOutcome.STARTED
        assert not hf.exists()

    async def test_an_unknown_mode_is_unsupported(self, tmp_path: Path) -> None:
        hf = _write(_control_dir(tmp_path), "handoff-1.json", issue="1", next_actions=[{"id": "x", "mode": "teleport"}])

        result = await handoff_service.execute_action("x", "1")

        assert result.outcome is ActionOutcome.UNSUPPORTED
        assert result.detail == "Unknown execution type: unknown"
        assert hf.exists()

    @pytest.mark.parametrize("ref", ["pr:", "pr:abc", "pr:٣"])
    async def test_a_malformed_pr_ref_matches_nothing(self, tmp_path: Path, ref: str) -> None:
        _write(_control_dir(tmp_path), "handoff.json", pr_number=7, next_actions=[_AGENT_ACTION])

        result = await handoff_service.execute_action("go", ref)

        assert result.outcome is ActionOutcome.NOT_FOUND

    async def test_a_failed_clear_after_a_start_is_logged_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot_invalidate: MagicMock
    ) -> None:
        from unittest.mock import AsyncMock

        from structlog.testing import capture_logs

        from sova.dashboard.services import control_service

        _write(_control_dir(tmp_path), "handoff-1.json", issue="1", next_actions=[_AGENT_ACTION])
        monkeypatch.setattr(handoff_service, "clear_handoff", MagicMock(side_effect=OSError("read-only")))

        with (
            patch.object(control_service, "start_agent", AsyncMock(return_value={"status": "started"})),
            capture_logs() as logs,
        ):
            result = await handoff_service.execute_action("go", "1")

        assert result.outcome is ActionOutcome.STARTED
        assert any(e["event"] == "handoff.clear_after_start_failed" for e in logs)
        snapshot_invalidate.assert_called_once()


class TestClearExpectedVersion:
    """A clear given the handoff it read leaves a file rewritten since then pending."""

    def test_clear_handoff_skips_a_changed_file_and_clears_an_unchanged_one(self, tmp_path: Path) -> None:
        import json

        control = _control_dir(tmp_path)
        hf = _write(control, "handoff-1.json", issue="1", summary="read")
        read = json.loads(hf.read_text(encoding="utf-8"))

        _write(control, "handoff-1.json", issue="1", summary="newer")
        assert handoff_service.clear_handoff(tmp_path, issue="1", expected=read) is False
        assert hf.exists()

        _write(control, "handoff-1.json", issue="1", summary="read")
        assert handoff_service.clear_handoff(tmp_path, issue="1", expected=read) is True
        assert not hf.exists()

    def test_clear_issueless_handoff_skips_a_changed_file(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        legacy = _write(control, "handoff.json", pr_number=7, summary="newer")

        assert handoff_service.clear_issueless_handoff(tmp_path, expected={"pr_number": 7}) is False
        assert legacy.exists()
        assert not (control / "handoff-archive").exists()


class TestArchivePruning:
    def test_the_archive_keeps_only_the_newest_fifty(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        archive = control / "handoff-archive"
        archive.mkdir()
        old = [archive / f"20260101-0000{i:02d}_old.json" for i in range(55)]
        for i, path in enumerate(old):
            path.write_text("{}", encoding="utf-8")
            # Distinct mtimes in name order, so the result does not depend on how ties would sort.
            os.utime(path, (1_767_225_600 + i, 1_767_225_600 + i))
        _write(control, "handoff-1.json", issue="1")

        handoff_service.clear_handoff(tmp_path, issue="1")

        kept = {p.name for p in archive.glob("*.json")}
        assert len(kept) == 50
        assert kept.isdisjoint(p.name for p in old[:6])
        assert {p.name for p in old[6:]} <= kept
        assert len([n for n in kept if not n.endswith("_old.json")]) == 1


class TestReadOwnedHandoff:
    """read_owned_handoff() never hands an issue-less run another issue's handoff (issue #1569)."""

    def test_an_issue_less_read_takes_only_the_legacy_file_naming_no_issue(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        _write(control, "handoff.json", pr_number=7, summary="pr only")
        newer = _write(control, "handoff-9.json", issue="9", summary="issue 9")
        os.utime(newer, (newer.stat().st_atime, newer.stat().st_mtime + 10))

        owned = ipc_handoff.read_owned_handoff(tmp_path, "", pr_number=7)

        assert owned is not None
        handoff, data = owned
        assert (handoff.summary, handoff.pr_number) == ("pr only", 7)
        assert data["summary"] == "pr only"

    @pytest.mark.parametrize("pr_number", [9, None])
    def test_an_issue_less_read_rejects_another_prs_handoff(self, tmp_path: Path, pr_number: int | None) -> None:
        """Every PR-only handoff shares handoff.json, so the PR must match too."""
        _write(_control_dir(tmp_path), "handoff.json", pr_number=7, summary="pr 7")

        assert ipc_handoff.read_owned_handoff(tmp_path, "", pr_number=pr_number) is None

    @pytest.mark.parametrize("issue", ["", None])
    def test_an_issue_less_read_rejects_a_legacy_file_naming_an_issue(self, tmp_path: Path, issue: str | None) -> None:
        control = _control_dir(tmp_path)
        _write(control, "handoff.json", issue="3")
        _write(control, "handoff-9.json", issue="9")

        assert ipc_handoff.read_owned_handoff(tmp_path, issue) is None

    def test_an_issue_read_prefers_its_own_file(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        _write(control, "handoff-9.json", issue="9", summary="own")
        _write(control, "handoff.json", issue="9", summary="legacy")

        owned = ipc_handoff.read_owned_handoff(tmp_path, "#9")

        assert owned is not None
        assert owned[0].summary == "own"

    def test_an_issue_read_falls_back_to_a_legacy_file_naming_it(self, tmp_path: Path) -> None:
        control = _control_dir(tmp_path)
        _write(control, "handoff.json", issue="9", summary="legacy")

        owned = ipc_handoff.read_owned_handoff(tmp_path, "9")
        assert owned is not None
        assert owned[0].summary == "legacy"
        assert ipc_handoff.read_owned_handoff(tmp_path, "8") is None

    def test_no_files_and_a_schema_invalid_file_read_as_none(self, tmp_path: Path) -> None:
        assert ipc_handoff.read_owned_handoff(tmp_path, "") is None
        control = _control_dir(tmp_path)
        (control / "handoff-9.json").write_text('{"status": 5}', encoding="utf-8")

        assert ipc_handoff.read_owned_handoff(tmp_path, "9") is None
