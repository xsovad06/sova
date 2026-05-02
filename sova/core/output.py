"""Output persistence -- write and read agent output to per-run log files.

Each agent run gets a plain-text log file at:
  <project_dir>/.claude/agent-output/<run_id>.log

Dashboard-spawned agents write lines as they stream (in addition to the
in-memory deque for real-time display).  CLI-spawned agents write step
markers and summaries during WorkflowEngine execution.

Files are append-only and survive dashboard restarts.
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from sova.utils.logging import get_logger

log = get_logger(component="output")

_OUTPUT_DIR = ".claude/agent-output"


def _output_dir(project_dir: Path) -> Path:
    d = project_dir / _OUTPUT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_path(project_dir: Path, run_id: int) -> Path:
    return _output_dir(project_dir) / f"{run_id}.log"


class OutputWriter:
    """Append-only writer for a single run's output file."""

    def __init__(self, project_dir: Path, run_id: int) -> None:
        self._path = output_path(project_dir, run_id)
        self._fh = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
        self._lock = Lock()

    @property
    def path(self) -> Path:
        return self._path

    def write_line(self, text: str) -> None:
        with self._lock:
            self._fh.write(text.rstrip("\n") + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def read_lines(project_dir: Path, run_id: int, since: int = 0) -> tuple[list[str], int]:
    """Read output lines from a run's log file.

    Returns (lines_from_offset, total_line_count).
    """
    path = output_path(project_dir, run_id)
    if not path.exists():
        return [], 0

    try:
        with open(path, encoding="utf-8") as f:
            all_lines = [line.rstrip("\n") for line in f]
    except OSError:
        return [], 0

    total = len(all_lines)
    return all_lines[since:], total
