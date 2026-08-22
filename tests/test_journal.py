"""Tests for sova.core.journal -- append-only hash-chained event journal."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sova.core.journal import _HASH_PREFIX, _SEED_HASH, JournalVerifyResult, RunJournal


@pytest.fixture
def journal_dir(tmp_path: Path) -> Path:
    return tmp_path


class TestRunJournal:
    def test_emit_creates_journal_file(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created", {"role": "developer"})
        assert journal.path.exists()

    def test_emit_writes_valid_jsonl(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created", {"role": "developer"})
        journal.emit("step_started", {"step": "develop"})

        lines = journal.path.read_text().strip().split("\n")
        assert len(lines) == 2

        for line in lines:
            entry = json.loads(line)
            assert "seq" in entry
            assert "ts" in entry
            assert "event" in entry
            assert "data" in entry
            assert "prev_hash" in entry

    def test_sequential_seq_numbers(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created")
        journal.emit("step_started")
        journal.emit("step_completed")

        lines = journal.path.read_text().strip().split("\n")
        seqs = [json.loads(line)["seq"] for line in lines]
        assert seqs == [1, 2, 3]

    def test_first_event_uses_seed_hash(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created")

        line = journal.path.read_text().strip()
        entry = json.loads(line)
        assert entry["prev_hash"] == _SEED_HASH

    def test_hash_chain_links_events(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created")
        journal.emit("step_started")

        lines = journal.path.read_text().strip().split("\n")
        first = json.loads(lines[0])
        second = json.loads(lines[1])

        import hashlib

        expected_hash = "sha256:" + hashlib.sha256(lines[0].encode()).hexdigest()
        assert second["prev_hash"] == expected_hash
        assert second["prev_hash"] != first["prev_hash"]

    def test_close_prevents_further_writes(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created")
        journal.close()
        journal.emit("step_started")

        lines = journal.path.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_emit_without_data(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created")

        line = journal.path.read_text().strip()
        entry = json.loads(line)
        assert entry["data"] == {}

    def test_emit_with_decimal_data(self, journal_dir: Path) -> None:
        from decimal import Decimal

        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("cost_recorded", {"cost_usd": Decimal("1.23")})

        line = journal.path.read_text().strip()
        entry = json.loads(line)
        assert entry["data"]["cost_usd"] == "1.23"

    def test_separate_run_directories(self, journal_dir: Path) -> None:
        j1 = RunJournal(journal_dir, run_id=1)
        j2 = RunJournal(journal_dir, run_id=2)
        j1.emit("run_created")
        j2.emit("run_created")

        assert j1.path.parent != j2.path.parent
        assert j1.path.exists()
        assert j2.path.exists()

    def test_emit_is_nonfatal_on_io_error(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal._journal_path = Path("/nonexistent/path/journal.jsonl")
        journal.emit("run_created")

    def test_emit_recovers_after_io_error(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created")

        real_path = journal._journal_path
        journal._journal_path = Path("/nonexistent/path/journal.jsonl")
        journal.emit("should_fail")

        journal._journal_path = real_path
        journal.emit("step_started")

        result = RunJournal.verify(journal_dir, run_id=1)
        assert result.valid
        assert result.event_count == 2

    def test_journal_path_structure(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=42)
        expected = journal_dir / ".claude" / "runs" / "42" / "journal.jsonl"
        assert journal.path == expected


class TestJournalVerify:
    def test_verify_valid_chain(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created", {"role": "developer"})
        journal.emit("step_started", {"step": "develop"})
        journal.emit("step_completed", {"step": "develop"})
        journal.emit("run_completed", {"total_cost_usd": "1.50"})

        result = RunJournal.verify(journal_dir, run_id=1)
        assert result.valid
        assert result.event_count == 4
        assert result.errors == []

    def test_verify_detects_tampered_event(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created")
        journal.emit("step_started", {"step": "develop"})
        journal.emit("step_completed", {"step": "develop"})

        lines = journal.path.read_text().strip().split("\n")
        tampered = json.loads(lines[1])
        tampered["data"]["step"] = "TAMPERED"
        lines[1] = json.dumps(tampered, separators=(",", ":"))
        journal.path.write_text("\n".join(lines) + "\n")

        result = RunJournal.verify(journal_dir, run_id=1)
        assert not result.valid
        assert any("hash chain broken" in e for e in result.errors)

    def test_verify_detects_deleted_event(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created")
        journal.emit("step_started")
        journal.emit("step_completed")

        lines = journal.path.read_text().strip().split("\n")
        journal.path.write_text(lines[0] + "\n" + lines[2] + "\n")

        result = RunJournal.verify(journal_dir, run_id=1)
        assert not result.valid

    def test_verify_detects_missing_journal(self, journal_dir: Path) -> None:
        result = RunJournal.verify(journal_dir, run_id=999)
        assert not result.valid
        assert any("not found" in e for e in result.errors)

    def test_verify_empty_journal_passes(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.path.write_text("")

        result = RunJournal.verify(journal_dir, run_id=1)
        assert result.valid
        assert result.event_count == 0

    def test_verify_detects_invalid_json(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.path.write_text("not valid json\n")

        result = RunJournal.verify(journal_dir, run_id=1)
        assert not result.valid
        assert any("invalid JSON" in e for e in result.errors)

    def test_verify_detects_seq_gap(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created")

        lines = journal.path.read_text().strip().split("\n")
        entry = json.loads(lines[0])
        entry["seq"] = 5
        journal.path.write_text(json.dumps(entry, separators=(",", ":")) + "\n")

        result = RunJournal.verify(journal_dir, run_id=1)
        assert not result.valid
        assert any("expected seq=1" in e for e in result.errors)

    def test_verify_single_event(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created")

        result = RunJournal.verify(journal_dir, run_id=1)
        assert result.valid
        assert result.event_count == 1


class TestJournalVerifyResult:
    def test_initial_state(self) -> None:
        result = JournalVerifyResult()
        assert result.valid
        assert result.event_count == 0
        assert result.errors == []

    def test_add_error_sets_invalid(self) -> None:
        result = JournalVerifyResult()
        result.add_error("something broke")
        assert not result.valid
        assert len(result.errors) == 1


class TestJournalConstants:
    def test_hash_prefix_value(self) -> None:
        assert _HASH_PREFIX == "sha256:"

    def test_seed_hash_uses_prefix(self) -> None:
        assert _SEED_HASH.startswith(_HASH_PREFIX)
        assert _SEED_HASH == _HASH_PREFIX + "0" * 64


class TestJournalEdgeCases:
    def test_ensure_dir_oserror_is_nonfatal(self, journal_dir: Path) -> None:
        with patch("sova.core.journal.Path.mkdir", side_effect=OSError("permission denied")):
            journal = RunJournal(journal_dir, run_id=1)
            assert journal._prev_hash == _SEED_HASH

    def test_verify_skips_blank_lines(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created")
        journal.emit("step_started")

        content = journal.path.read_text()
        lines = content.split("\n")
        lines.insert(1, "")
        journal.path.write_text("\n".join(lines))

        result = RunJournal.verify(journal_dir, run_id=1)
        assert result.valid
        assert result.event_count == 2

    def test_verify_oserror_on_read(self, journal_dir: Path) -> None:
        journal = RunJournal(journal_dir, run_id=1)
        journal.emit("run_created")

        with patch("builtins.open", side_effect=OSError("disk failure")):
            result = RunJournal.verify(journal_dir, run_id=1)
        assert not result.valid
        assert any("Failed to read" in e for e in result.errors)
