"""Test baseline capture and regression diffing.

Captures a per-test snapshot of the project's test suite state before any
agent changes. Downstream steps diff current results against the baseline
to distinguish true regressions from pre-existing failures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="test_baseline")

BASELINE_FILENAME = "test-baseline.json"
BASELINE_DIR = ".sova"


@dataclass(frozen=True)
class SingleTestOutcome:
    """A single test's outcome."""

    nodeid: str
    outcome: str  # "passed", "failed", "error", "skipped"


@dataclass(frozen=True)
class BaselineSnapshot:
    """Captured test suite state."""

    mode: str  # "per_test" or "exit_code"
    exit_code: int
    tests: list[SingleTestOutcome]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "exit_code": self.exit_code,
            "tests": [{"nodeid": t.nodeid, "outcome": t.outcome} for t in self.tests],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BaselineSnapshot:
        return cls(
            mode=data["mode"],
            exit_code=data["exit_code"],
            tests=[SingleTestOutcome(nodeid=t["nodeid"], outcome=t["outcome"]) for t in data.get("tests", [])],
        )


@dataclass(frozen=True)
class RegressionReport:
    """Result of diffing current test results against a baseline."""

    regressions: list[SingleTestOutcome]  # tests that were passing but now fail
    fixed: list[SingleTestOutcome]  # tests that were failing but now pass
    new_failures: list[SingleTestOutcome]  # tests not in baseline that fail

    @property
    def has_regressions(self) -> bool:
        return len(self.regressions) > 0

    def summary(self) -> str:
        parts: list[str] = []
        if self.regressions:
            parts.append(f"{len(self.regressions)} regression(s)")
        if self.fixed:
            parts.append(f"{len(self.fixed)} fixed")
        if self.new_failures:
            parts.append(f"{len(self.new_failures)} new failure(s)")
        return ", ".join(parts) if parts else "no regressions"


def baseline_path(worktree_dir: Path) -> Path:
    """Return the path where the baseline file should live."""
    return worktree_dir / BASELINE_DIR / BASELINE_FILENAME


def _is_pytest_cmd(test_cmd: str) -> bool:
    """Check if the test command uses pytest."""
    return "pytest" in test_cmd


async def run_test_suite(
    test_cmd: str,
    cwd: Path,
    cmd_timeout: int = 300,
) -> BaselineSnapshot:
    """Run the project's test suite and capture results.

    For pytest projects with pytest-json-report installed, captures per-test
    results. Otherwise falls back to exit-code-only mode.
    """
    if not cwd or not cwd.exists():
        raise ValueError(f"Invalid cwd for test suite: {cwd}")

    if not _is_pytest_cmd(test_cmd):
        log.info("test_baseline.exit_code_mode", reason="non-pytest runner")
        return await _run_exit_code_only(test_cmd, cwd, cmd_timeout)

    # Try pytest-json-report for per-test results
    report_path = cwd / BASELINE_DIR / ".test-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    enriched_cmd = f"{test_cmd} --json-report --json-report-file={report_path} --tb=no -q"
    result = await run("sh", "-c", enriched_cmd, cwd=cwd, timeout=cmd_timeout)

    if report_path.exists():
        try:
            report = json.loads(report_path.read_text())
            tests = [SingleTestOutcome(nodeid=t["nodeid"], outcome=t["outcome"]) for t in report.get("tests", [])]
            log.info("test_baseline.per_test_captured", count=len(tests), exit_code=result.returncode)
            report_path.unlink(missing_ok=True)
            return BaselineSnapshot(mode="per_test", exit_code=result.returncode, tests=tests)
        except (json.JSONDecodeError, KeyError, TypeError):
            log.warning("test_baseline.report_parse_failed", exc_info=True)
            report_path.unlink(missing_ok=True)

    # Fallback: run a clean command without --json-report flags.
    # The enriched command may have exited with code 4 (usage error) if
    # pytest-json-report is not installed, so we can't trust its exit code.
    log.info("test_baseline.exit_code_fallback", reason="pytest-json-report unavailable")
    return await _run_exit_code_only(test_cmd, cwd, cmd_timeout)


async def _run_exit_code_only(test_cmd: str, cwd: Path, cmd_timeout: int) -> BaselineSnapshot:
    """Capture only the exit code from the test command."""
    result = await run("sh", "-c", test_cmd, cwd=cwd, timeout=cmd_timeout)
    return BaselineSnapshot(mode="exit_code", exit_code=result.returncode, tests=[])


def save_baseline(snapshot: BaselineSnapshot, worktree_dir: Path) -> Path:
    """Write baseline to disk and return the file path."""
    path = baseline_path(worktree_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot.to_dict(), indent=2) + "\n")
    log.info("test_baseline.saved", path=str(path), test_count=len(snapshot.tests))
    return path


def load_baseline(worktree_dir: Path) -> BaselineSnapshot | None:
    """Load baseline from disk, or None if not found/corrupt."""
    path = baseline_path(worktree_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return BaselineSnapshot.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        log.warning("test_baseline.load_failed", path=str(path), exc_info=True)
        return None


def diff_results(
    baseline: BaselineSnapshot,
    current: BaselineSnapshot,
) -> RegressionReport:
    """Compare current test results against the baseline.

    Only meaningful when both snapshots are in per_test mode.
    For exit_code mode, returns empty report (suite-level comparison
    is done by the caller via exit codes).
    """
    if baseline.mode != "per_test" or current.mode != "per_test":
        return RegressionReport(regressions=[], fixed=[], new_failures=[])

    baseline_by_id = {t.nodeid: t for t in baseline.tests}
    current_by_id = {t.nodeid: t for t in current.tests}

    regressions: list[SingleTestOutcome] = []
    fixed: list[SingleTestOutcome] = []
    new_failures: list[SingleTestOutcome] = []

    for nodeid, cur in current_by_id.items():
        base = baseline_by_id.get(nodeid)
        if base is None:
            if cur.outcome in ("failed", "error"):
                new_failures.append(cur)
        elif base.outcome == "passed" and cur.outcome in ("failed", "error"):
            regressions.append(cur)
        elif base.outcome in ("failed", "error") and cur.outcome == "passed":
            fixed.append(cur)

    return RegressionReport(regressions=regressions, fixed=fixed, new_failures=new_failures)
