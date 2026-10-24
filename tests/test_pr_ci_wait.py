"""Behavior of `/pr`'s step 15 CI wait, run for real against a fake `gh`.

The wait used `gh pr checks --required` alone. A repository with no branch
protection (a private repository on a plan that offers none) has no required
checks, so that call prints `no required checks reported` and exits 1 on every
poll, and an agent could read "every required check passed" as vacuously true
and continue without waiting for CI. The block now falls back to every check on
the PR head in that case, keeps polling while no check has registered yet, and
never treats an empty check list as a pass. A head with neither a check nor a
workflow run (path filters that skip a docs-only change) ends early with its
own verdict instead of waiting out the 30 minute budget. These tests extract
the shipped bash block and execute it, so they exercise the snippet an agent
actually runs rather than the prose around it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from sova.commands.catalog import get_canonical_dir

REPO_ROOT = Path(__file__).parent.parent
CANONICAL_PR = get_canonical_dir() / "pr.md"
RENDERED_PR_COPIES = [
    REPO_ROOT / ".claude" / "commands" / "pr.md",
    REPO_ROOT / "plugins" / "sova" / "commands" / "pr.md",
    REPO_ROOT / ".agents" / "skills" / "sova-pr" / "SKILL.md",
]
HEAD_SHA = "abc123"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="the CI wait block parses gh output with jq")

# What `gh pr checks` prints to stderr, with an empty stdout and exit 1, in the two "nothing to report" cases.
NO_REQUIRED = {"stdout": "", "stderr": "no required checks reported on the 'feat/x' branch\n", "rc": 1}
NO_CHECKS = {"stdout": "", "stderr": "no checks reported on the 'feat/x' branch\n", "rc": 1}
GH_ERROR = {"stdout": "", "stderr": "HTTP 502: Bad Gateway\n", "rc": 1}

_FAKE_GH = """\
#!{python}
import json, os, sys

args = sys.argv[1:]
state = os.environ["FAKE_GH_STATE"]
with open(os.path.join(state, "scenario.json"), encoding="utf-8") as fh:
    scenario = json.load(fh)
if args[:2] == ["pr", "view"]:
    if "headRefOid" not in args:
        print("42")
        sys.exit(0)
    if scenario.get("head_fails"):
        sys.stderr.write("HTTP 502: Bad Gateway\\n")
        sys.exit(1)
    print("{head}")
    sys.exit(0)
if args[:2] == ["run", "list"]:
    if args[args.index("--commit") + 1] != "{head}":
        sys.stderr.write(f"run list not scoped to the PR head: {{args}}\\n")
        sys.exit(2)
    mode = "runs"
elif args[:2] == ["pr", "checks"]:
    mode = "required" if "--required" in args else "all"
else:
    sys.stderr.write(f"unexpected gh call: {{args}}\\n")
    sys.exit(2)
responses = scenario.get(mode)
if not responses:
    sys.stderr.write(f"unexpected gh call in {{mode}} mode\\n")
    sys.exit(2)
calls_path = os.path.join(state, mode + ".calls")
calls = int(open(calls_path, encoding="utf-8").read()) if os.path.exists(calls_path) else 0
with open(calls_path, "w", encoding="utf-8") as fh:
    fh.write(str(calls + 1))
response = responses[min(calls, len(responses) - 1)]
sys.stdout.write(response["stdout"])
sys.stderr.write(response["stderr"])
sys.exit(response["rc"])
"""


def _checks(*buckets: str) -> dict[str, object]:
    """A `gh pr checks --json name,bucket,link` response with gh's exit code for those buckets."""
    rows = [{"name": f"check-{i}", "bucket": b, "link": f"https://example.invalid/{i}"} for i, b in enumerate(buckets)]
    if "pending" in buckets:
        rc = 8
    elif any(b != "pass" and b != "skipping" for b in buckets):
        rc = 1
    else:
        rc = 0
    return {"stdout": json.dumps(rows) + "\n", "stderr": "", "rc": rc}


def _runs(*conclusions: str) -> dict[str, object]:
    """A `gh run list --commit <sha> --json status,conclusion,workflowName` response."""
    rows = [
        {"status": "completed", "conclusion": c, "workflowName": f"workflow-{i}"} for i, c in enumerate(conclusions)
    ]
    return {"stdout": json.dumps(rows) + "\n", "stderr": "", "rc": 0}


def _wait_block(path: Path) -> str:
    """Return step 15's bash block from a `/pr` command file, dedented out of its list item."""
    text = path.read_text(encoding="utf-8")
    start = text.index("**Wait for CI pipeline**")
    match = re.search(r"```bash\n(.*?)\n[ \t]*```", text[start:], re.S)
    assert match is not None, f"{path}: no bash block after the CI wait heading"
    return textwrap.dedent(match.group(1))


def _run_wait(scenario: dict[str, object], tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], dict[str, int]]:
    """Run the canonical wait block against a fake `gh` and a no-op `sleep`; return the result and calls per mode."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(_FAKE_GH.format(python=sys.executable, head=HEAD_SHA), encoding="utf-8")
    fake_gh.chmod(0o755)
    fake_sleep = bin_dir / "sleep"
    fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)
    (tmp_path / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")

    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_STATE": str(tmp_path)}
    result = subprocess.run(
        ["bash", "-c", _wait_block(CANONICAL_PR)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = {}
    for mode in ("required", "all", "runs"):
        calls_path = tmp_path / f"{mode}.calls"
        calls[mode] = int(calls_path.read_text(encoding="utf-8")) if calls_path.exists() else 0
    return result, calls


def _verdict(result: subprocess.CompletedProcess[str]) -> str:
    lines = result.stdout.strip().splitlines()
    assert lines, f"no output; stderr: {result.stderr}"
    return lines[-1]


class TestRenderedCopiesRunTheSameWait:
    @pytest.mark.parametrize("path", RENDERED_PR_COPIES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
    def test_copy_ships_the_canonical_block(self, path: Path) -> None:
        """Every shipped copy must run the block these tests exercise, not an older one."""
        assert _wait_block(path) == _wait_block(CANONICAL_PR)


class TestRequiredChecksConfigured:
    def test_waits_on_required_checks_only(self, tmp_path: Path) -> None:
        result, calls = _run_wait({"required": [_checks("pending", "pass"), _checks("pass", "skipping")]}, tmp_path)
        assert result.returncode == 0, result.stderr
        assert _verdict(result) == "CI PASSED (required checks): all 2 passed or were skipped"
        assert calls == {"required": 2, "all": 0, "runs": 0}

    def test_canceled_check_is_not_a_pass(self, tmp_path: Path) -> None:
        result, _calls = _run_wait({"required": [_checks("pass", "cancel")]}, tmp_path)
        assert _verdict(result) == "CI CANCELED (required checks): 1 of 2 canceled"

    def test_unknown_bucket_is_not_a_pass(self, tmp_path: Path) -> None:
        result, _calls = _run_wait({"required": [_checks("pass", "stale")]}, tmp_path)
        assert _verdict(result) == "CI FAILED (required checks): 1 of 2 did not pass"

    def test_still_pending_after_one_call_says_so(self, tmp_path: Path) -> None:
        result, calls = _run_wait({"required": [_checks("pending")]}, tmp_path)
        assert _verdict(result) == "CI PENDING (required checks): 1 of 1 still pending"
        assert calls == {"required": 16, "all": 0, "runs": 0}


class TestNoRequiredChecksConfigured:
    def test_falls_back_to_every_check_on_the_head(self, tmp_path: Path) -> None:
        """The GWYM case: a private repository on a plan without branch protection."""
        result, calls = _run_wait({"required": [NO_REQUIRED], "all": [_checks("pending"), _checks("pass")]}, tmp_path)
        assert result.returncode == 0, result.stderr
        assert _verdict(result) == "CI PASSED (all checks): all 1 passed or were skipped"
        assert calls == {"required": 2, "all": 2, "runs": 0}

    def test_fallback_reports_a_failing_check(self, tmp_path: Path) -> None:
        result, _calls = _run_wait({"required": [NO_REQUIRED], "all": [_checks("pass", "fail")]}, tmp_path)
        assert _verdict(result) == "CI FAILED (all checks): 1 of 2 did not pass"
        assert "fail\tcheck-1\thttps://example.invalid/1" in result.stdout

    def test_required_checks_win_once_they_register(self, tmp_path: Path) -> None:
        """The scope is re-decided on every poll, so a late required check is not shadowed by the fallback."""
        scenario = {"required": [NO_REQUIRED, _checks("pass")], "all": [_checks("pending", "pending")]}
        result, calls = _run_wait(scenario, tmp_path)
        assert _verdict(result) == "CI PASSED (required checks): all 1 passed or were skipped"
        assert calls == {"required": 2, "all": 1, "runs": 0}


class TestNoChecksAtAll:
    def test_no_check_and_no_run_ends_early_as_no_ci_triggered(self, tmp_path: Path) -> None:
        """Path filters that skip a docs-only change: stop after the 5-poll grace, not the 30 minute budget."""
        result, calls = _run_wait({"required": [NO_CHECKS], "runs": [_runs()]}, tmp_path)
        assert result.returncode == 0, result.stderr
        assert _verdict(result) == f"NO CI TRIGGERED: no check and no workflow run for head {HEAD_SHA} after 2 minutes"
        assert "CI PASSED" not in result.stdout
        assert calls == {"required": 5, "all": 0, "runs": 5}

    def test_an_empty_check_list_with_no_run_is_no_ci_triggered(self, tmp_path: Path) -> None:
        empty = {"stdout": "[]\n", "stderr": "", "rc": 0}
        result, _calls = _run_wait({"required": [empty], "runs": [_runs()]}, tmp_path)
        assert _verdict(result).startswith("NO CI TRIGGERED: ")
        assert "CI PASSED" not in result.stdout

    def test_a_run_without_checks_waits_out_the_call(self, tmp_path: Path) -> None:
        """A workflow file error still creates a (failed) run, so it must not read as "no CI triggered"."""
        result, calls = _run_wait({"required": [NO_CHECKS], "runs": [_runs("failure")]}, tmp_path)
        assert _verdict(result) == (
            "NO CHECKS: none reported on the PR head, though a workflow run exists for it or could not be listed"
        )
        assert "run\tcompleted\tfailure\tworkflow-0" in result.stdout
        assert "NO CI TRIGGERED" not in result.stdout
        assert calls == {"required": 16, "all": 0, "runs": 16}

    def test_failed_run_lookup_restarts_the_grace(self, tmp_path: Path) -> None:
        """Only consecutive polls that confirmed zero runs count: 3 empty, 1 failed lookup, then 5 empty."""
        runs = [_runs(), _runs(), _runs(), GH_ERROR, _runs()]
        result, calls = _run_wait({"required": [NO_CHECKS], "runs": runs}, tmp_path)
        assert _verdict(result).startswith("NO CI TRIGGERED: ")
        assert "Unable to list the workflow runs for the PR head" in result.stdout
        assert calls == {"required": 9, "all": 0, "runs": 9}

    def test_unresolvable_head_never_reports_no_ci_triggered(self, tmp_path: Path) -> None:
        result, calls = _run_wait({"required": [NO_CHECKS], "runs": [_runs()], "head_fails": True}, tmp_path)
        assert _verdict(result).startswith("NO CHECKS: ")
        assert "NO CI TRIGGERED" not in result.stdout
        assert calls == {"required": 16, "all": 0, "runs": 0}

    def test_waits_for_checks_that_register_late(self, tmp_path: Path) -> None:
        scenario = {"required": [NO_CHECKS, NO_CHECKS, _checks("pass")], "runs": [_runs()]}
        result, calls = _run_wait(scenario, tmp_path)
        assert _verdict(result) == "CI PASSED (required checks): all 1 passed or were skipped"
        assert calls == {"required": 3, "all": 0, "runs": 2}


class TestUnreadableOutput:
    def test_gh_failure_stops_instead_of_passing(self, tmp_path: Path) -> None:
        result, calls = _run_wait({"required": [GH_ERROR]}, tmp_path)
        assert result.returncode == 1
        assert "Unable to read the PR's checks: HTTP 502: Bad Gateway" in result.stderr
        assert "CI PASSED" not in result.stdout
        assert calls == {"required": 1, "all": 0, "runs": 0}
