"""The whole-tree invariant scans run in `make lint` and CI's invariants job, not in pytest.

`invariants/no-double-dash.sh --all` and `invariants/file-encoding.sh --all` used to run
inside the suite (issue #1191 moved them: 33 s of a local run for the double-dash scan alone).
Nothing else would notice if a later edit dropped them from both places, so this pins them.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANS = ("bash invariants/no-double-dash.sh --all .", "bash invariants/file-encoding.sh --all .")


def _make_target(name: str) -> tuple[list[str], list[str]]:
    """A Makefile target's prerequisites and recipe lines."""
    text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}:([^#\n]*).*\n((?:\t.*\n)*)", text, re.M)
    assert match is not None, f"Makefile has no {name} target"
    return match.group(1).split(), [line.strip() for line in match.group(2).splitlines()]


def _ci_job(name: str) -> str:
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(rf"^  {re.escape(name)}:\n((?:(?:    .*)?\n)*)", text, re.M)
    assert match is not None, f"ci.yml has no {name} job"
    return match.group(1)


def test_lint_tree_runs_both_whole_tree_scans() -> None:
    _prereqs, recipe = _make_target("lint-tree")
    assert recipe == list(SCANS)


def test_make_lint_includes_lint_tree() -> None:
    prereqs, _recipe = _make_target("lint")
    assert "lint-tree" in prereqs


def test_ci_invariants_job_runs_lint_tree() -> None:
    assert re.search(r"^\s+run: make lint-tree$", _ci_job("invariants"), re.M)


def test_ci_static_checks_job_runs_lint_tree() -> None:
    """Static Checks is a required status check on main and Invariants is not, so the scans stay merge-blocking."""
    assert re.search(r"^\s+run: make lint-tree$", _ci_job("lint-static"), re.M)
