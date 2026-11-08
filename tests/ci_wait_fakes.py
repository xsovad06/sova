"""Shared fake `gh` for the tests that run the CI wait blocks of `/pr` and `/integrate-pr`.

Both commands ship an inline bash block that polls `gh pr checks` and, since issue #1321, reads the
base branch's required check names from `gh api repos/{owner}/{repo}/rules/branches/<base>`. The tests
extract that block and execute it against this fake, so both commands are exercised with one set of
responses and the two fakes cannot drift apart.

A scenario maps a mode to a list of responses (`{"stdout", "stderr", "rc"}`); call N gets response N,
and the last one repeats. Modes: `required` (`gh pr checks --required`), `all` (`gh pr checks`), `runs`
(`gh run list`) and `rules` (the rulesets lookup, which defaults to an empty rule list). The rulesets lookup must
be paginated. Classic branch protection is deliberately not served: its endpoint needs the admin right (a push-only
account gets a 404), so a block that calls it fails the fake with exit 2.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HEAD_SHA = "abc123"
BASE_BRANCH = "main"

# What `gh pr checks` prints to stderr, with an empty stdout and exit 1, in the two "nothing to report" cases.
NO_REQUIRED = {"stdout": "", "stderr": "no required checks reported on the 'feat/x' branch\n", "rc": 1}
NO_CHECKS = {"stdout": "", "stderr": "no checks reported on the 'feat/x' branch\n", "rc": 1}
GH_ERROR = {"stdout": "", "stderr": "HTTP 502: Bad Gateway\n", "rc": 1}
# The rules lookup on a private repository whose plan has no rulesets: gh prints the 403 body to stdout.
NO_RULESETS_PLAN = {
    "stdout": '{"message":"Upgrade to GitHub Pro or make this repository public to enable this feature."}',
    "stderr": "gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)\n",
    "rc": 1,
}

# Required check names for the issue #1321 scenarios; SonarCloud is the one that registers late.
SONAR = "SonarCloud Analysis"
FIVE = ["lint", "test", "build", "docs", SONAR]

_FAKE_GH = """\
#!{python}
import json, os, sys

args = sys.argv[1:]
state = os.environ["FAKE_GH_STATE"]
with open(os.path.join(state, "scenario.json"), encoding="utf-8") as fh:
    scenario = json.load(fh)
if args[:2] == ["pr", "view"]:
    if "baseRefName" in args:
        print("{base}")
        sys.exit(0)
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
elif args[:1] == ["api"]:
    path = next(a for a in args[1:] if not a.startswith("-"))
    if path == "repos/{{owner}}/{{repo}}/rules/branches/{base}?per_page=100":
        if "--paginate" not in args:
            sys.stderr.write(f"rules lookup reads only the first page: {{args}}\\n")
            sys.exit(2)
        mode = "rules"
    else:
        sys.stderr.write(f"api lookup not scoped to the base branch: {{args}}\\n")
        sys.exit(2)
else:
    sys.stderr.write(f"unexpected gh call: {{args}}\\n")
    sys.exit(2)
responses = scenario.get(mode)
if not responses and mode == "rules":
    responses = [{{"stdout": "[]\\n", "stderr": "", "rc": 0}}]
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


def checks(*buckets: str, names: list[str] | None = None) -> dict[str, object]:
    """A `gh pr checks --json name,bucket,link` response with gh's exit code for those buckets."""
    labels = names if names is not None else [f"check-{i}" for i in range(len(buckets))]
    rows = [
        {"name": n, "bucket": b, "link": f"https://example.invalid/{i}"}
        for i, (n, b) in enumerate(zip(labels, buckets))
    ]
    if "pending" in buckets:
        rc = 8
    elif any(b != "pass" and b != "skipping" for b in buckets):
        rc = 1
    else:
        rc = 0
    return {"stdout": json.dumps(rows) + "\n", "stderr": "", "rc": rc}


def five(*buckets: str) -> dict[str, object]:
    """A `checks()` response naming the first len(buckets) entries of FIVE."""
    return checks(*buckets, names=FIVE[: len(buckets)])


def runs(*conclusions: str) -> dict[str, object]:
    """A `gh run list --commit <sha> --json status,conclusion,workflowName` response."""
    rows = [
        {"status": "completed", "conclusion": c, "workflowName": f"workflow-{i}"} for i, c in enumerate(conclusions)
    ]
    return {"stdout": json.dumps(rows) + "\n", "stderr": "", "rc": 0}


def rules(*names: str) -> dict[str, object]:
    """A rulesets response: one `required_status_checks` rule per name, plus an unrelated rule."""
    rows: list[dict[str, object]] = [{"type": "deletion"}]
    rows += [
        {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": n}]}} for n in names
    ]
    return {"stdout": json.dumps(rows) + "\n", "stderr": "", "rc": 0}


def paged_rules(*pages: list[str]) -> dict[str, object]:
    """A `gh api --paginate` rulesets response: one JSON array per page, printed back to back."""
    out = ""
    for names in pages:
        out += rules(*names)["stdout"]  # type: ignore[operator]
    return {"stdout": out, "stderr": "", "rc": 0}


def install_fakes(tmp_path: Path, scenario: dict[str, object]) -> dict[str, str]:
    """Write a fake `gh` and a no-op `sleep` into tmp_path/bin and return an environment that puts them first."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(_FAKE_GH.format(python=sys.executable, head=HEAD_SHA, base=BASE_BRANCH), encoding="utf-8")
    fake_gh.chmod(0o755)
    fake_sleep = bin_dir / "sleep"
    fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)
    (tmp_path / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_STATE": str(tmp_path)}


def read_calls(tmp_path: Path, modes: tuple[str, ...] = ("required", "all", "runs")) -> dict[str, int]:
    """How many times the fake `gh` served each mode."""
    result = {}
    for mode in modes:
        calls_path = tmp_path / f"{mode}.calls"
        result[mode] = int(calls_path.read_text(encoding="utf-8")) if calls_path.exists() else 0
    return result
