"""Append-only, hash-chained event journal for agent runs.

Each run produces a JSONL file at ``.claude/runs/<run_id>/journal.jsonl``
where every event includes a SHA256 hash of the previous event, creating
a tamper-evident chain.  Journal failures are non-fatal: they are logged
but never block the pipeline.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sova.utils.logging import get_logger

log = get_logger(component="journal")

_HASH_PREFIX = "sha256:"
_SEED_HASH = _HASH_PREFIX + "0" * 64


class JournalVerifyResult:
    """Result of verifying a journal's hash chain."""

    def __init__(self) -> None:
        self.valid: bool = True
        self.event_count: int = 0
        self.errors: list[str] = []

    def add_error(self, msg: str) -> None:
        self.valid = False
        self.errors.append(msg)


class RunJournal:
    """Append-only event writer for a single run.

    Events are written synchronously (one small JSON line per call).
    All I/O is wrapped in try/except so failures never propagate.
    """

    def __init__(self, project_dir: Path, run_id: int) -> None:
        self._run_dir = project_dir / ".claude" / "runs" / str(run_id)
        self._journal_path = self._run_dir / "journal.jsonl"
        self._seq: int = 0
        self._prev_hash: str = _SEED_HASH
        self._closed = False
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        try:
            self._run_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.warning("journal.mkdir_failed", path=str(self._run_dir), exc_info=True)

    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Append a single event to the journal."""
        if self._closed:
            return
        try:
            next_seq = self._seq + 1
            entry = {
                "seq": next_seq,
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "data": data or {},
                "prev_hash": self._prev_hash,
            }
            line = json.dumps(entry, separators=(",", ":"), default=str)
            with open(self._journal_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._seq = next_seq
            self._prev_hash = _HASH_PREFIX + hashlib.sha256(line.encode()).hexdigest()
        except Exception:
            log.warning("journal.emit_failed", event_type=event, exc_info=True)

    def close(self) -> None:
        """Mark the journal as closed (no further writes)."""
        self._closed = True

    @property
    def path(self) -> Path:
        return self._journal_path

    @staticmethod
    def verify(project_dir: Path, run_id: int) -> JournalVerifyResult:
        """Validate the hash chain of an existing journal.

        Returns a ``JournalVerifyResult`` with ``valid=True`` if the chain
        is intact, or ``valid=False`` with error details if corruption is
        detected.
        """
        result = JournalVerifyResult()
        journal_path = project_dir / ".claude" / "runs" / str(run_id) / "journal.jsonl"

        if not journal_path.exists():
            result.add_error(f"Journal file not found: {journal_path}")
            return result

        prev_hash = _SEED_HASH
        expected_seq = 1

        try:
            with open(journal_path, encoding="utf-8") as f:
                for line_num, raw_line in enumerate(f, start=1):
                    raw_line = raw_line.rstrip("\n")
                    if not raw_line:
                        continue

                    try:
                        entry = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        result.add_error(f"Line {line_num}: invalid JSON: {exc}")
                        return result

                    seq = entry.get("seq")
                    if seq != expected_seq:
                        result.add_error(f"Line {line_num}: expected seq={expected_seq}, got seq={seq}")
                        return result

                    recorded_prev_hash = entry.get("prev_hash")
                    if recorded_prev_hash != prev_hash:
                        result.add_error(
                            f"Line {line_num} (seq={seq}): hash chain broken. "
                            f"Expected prev_hash={prev_hash}, got {recorded_prev_hash}"
                        )
                        return result

                    prev_hash = _HASH_PREFIX + hashlib.sha256(raw_line.encode()).hexdigest()
                    expected_seq += 1
                    result.event_count += 1
        except OSError as exc:
            result.add_error(f"Failed to read journal: {exc}")

        return result
