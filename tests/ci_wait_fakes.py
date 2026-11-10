"""Shared fake `gh` for the tests that run the CI wait blocks of `/pr` and `/integrate-pr`.

Both commands ship an inline bash block that polls `gh pr checks` and, since issue #1321, reads the
base branch's required check names from `gh api repos/{owner}/{repo}/rules/branches/<base>`. The tests
extract that block and execute it against this fake, so both commands are exercised with one set of
responses and the two fakes cannot drift apart.

A scenario maps a mode to a list of responses (`{"stdout", "stderr", "rc"}`); call N gets response N,
and the last one repeats. The fake is a set of bash functions serving those responses from files, and the tests
run each block with its 16-poll budget cut to TEST_POLL_BUDGET (shorten_poll_budget()). Modes: `required`
(`gh pr checks --required`), `all` (`gh pr checks`), `runs` (`gh run list`) and `rules` (the rulesets
lookup, which defaults to an empty rule list). The rulesets lookup must be paginated. Classic branch
protection is deliberately not served: its endpoint needs the admin right (a push-only account gets a 404),
so a block that calls it fails the fake with exit 2.
"""

from __future__ import annotations

import json
import os
import re
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

# Bash functions, not an executable: the wait blocks call `gh` (and `sleep`) on every poll, and a
# Python fake started an interpreter per call, which made single tests take 9 to 16 s locally (issue
# #1191). install_fakes() writes these to a file that `bash -c` sources through BASH_ENV, so a call
# costs a fork at most and the responses are read with builtins. Each function returns rather than
# exits, which keeps a call outside a command substitution from ending the block.
_FAKE_FUNCTIONS = """\
_fake_gh_has() { local want=$1 a; shift; for a in "$@"; do [[ $a == "$want" ]] && return 0; done; return 1; }
_fake_gh_cat() { local content=; IFS= read -r -d '' content < "$1" || true; printf '%s' "$content"; }
gh() {
  local state=$FAKE_GH_STATE mode= a prev= path= commit= count calls=0 index rc responses
  case "$1 ${2:-}" in
  "pr view")
    if _fake_gh_has baseRefName "$@"; then echo "@BASE@"; return 0; fi
    if ! _fake_gh_has headRefOid "$@"; then echo 42; return 0; fi
    if [[ -e $state/head_fails ]]; then echo "HTTP 502: Bad Gateway" >&2; return 1; fi
    echo "@HEAD@"
    return 0 ;;
  "run list")
    for a in "$@"; do [[ $prev == --commit ]] && commit=$a; prev=$a; done
    if [[ $commit != "@HEAD@" ]]; then echo "run list not scoped to the PR head: $*" >&2; return 2; fi
    mode=runs ;;
  "pr checks")
    mode=all
    if _fake_gh_has --required "$@"; then mode=required; fi ;;
  *)
    if [[ $1 != api ]]; then echo "unexpected gh call: $*" >&2; return 2; fi
    for a in "${@:2}"; do [[ $a == -* || -n $path ]] || path=$a; done
    if [[ $path != "repos/{owner}/{repo}/rules/branches/@BASE@?per_page=100" ]]; then
      echo "api lookup not scoped to the base branch: $*" >&2
      return 2
    fi
    if ! _fake_gh_has --paginate "$@"; then echo "rules lookup reads only the first page: $*" >&2; return 2; fi
    mode=rules ;;
  esac
  responses=$state/responses/$mode
  if [[ ! -e $responses.count ]]; then echo "unexpected gh call in $mode mode" >&2; return 2; fi
  read -r count < "$responses.count"
  if [[ -e $state/$mode.calls ]]; then read -r calls < "$state/$mode.calls"; fi
  echo $((calls + 1)) > "$state/$mode.calls"
  index=$((calls < count ? calls : count - 1))
  _fake_gh_cat "$responses.$index.out"
  _fake_gh_cat "$responses.$index.err" >&2
  read -r rc < "$responses.$index.rc"
  return "$rc"
}
sleep() { :; }
"""

# On PATH behind the functions, so a call that ever bypassed them fails instead of reaching GitHub.
_GH_GUARD = '#!/bin/sh\necho "the fake gh function was bypassed: gh $*" >&2\nexit 2\n'

# The rules lookup a scenario does not configure: no ruleset, so no required check name.
_NO_RULES = {"stdout": "[]\n", "stderr": "", "rc": 0}

# Polls the tests let a wait block make, in place of the shipped 16 (8 or 30 minutes of CI). Ten
# still outlasts every poll count a test asserts short of the budget (the longest is 9: a five-poll
# grace that one failed lookup restarted), so a scenario that waits out the budget stays distinct.
TEST_POLL_BUDGET = 10


def shorten_poll_budget(block: str) -> str:
    """Rewrite a wait block's 16-poll loop to TEST_POLL_BUDGET polls, failing if the loop's shape changed."""
    replacements = [("seq 1 16", 1), ("-lt 16", None), ("-eq 16", None), ("/16", None)]
    for old, required in replacements:
        # A word boundary after the literal, so `-lt 160` or `/164` is not mistaken for the poll count.
        pattern = re.compile(re.escape(old) + r"\b")
        if required and not pattern.search(block):
            raise AssertionError(f"the wait block no longer has `{old}`; update shorten_poll_budget()")
        block = pattern.sub(old.replace("16", str(TEST_POLL_BUDGET)), block)
    if re.search(r"\b16\b", block):
        raise AssertionError("the wait block has a 16 shorten_poll_budget() does not rewrite")
    return block


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
    """Write the fake `gh` and `sleep` and their responses into tmp_path; return an environment that loads them."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gh", _GH_GUARD), ("sleep", "#!/bin/sh\nexit 0\n")):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    functions = tmp_path / "fakes.bash"
    functions.write_text(_FAKE_FUNCTIONS.replace("@HEAD@", HEAD_SHA).replace("@BASE@", BASE_BRANCH), encoding="utf-8")
    responses_dir = tmp_path / "responses"
    responses_dir.mkdir()
    modes = {mode: value for mode, value in scenario.items() if isinstance(value, list) and value}
    modes.setdefault("rules", [_NO_RULES])
    for mode, responses in modes.items():
        (responses_dir / f"{mode}.count").write_text(f"{len(responses)}\n", encoding="utf-8")
        for i, response in enumerate(responses):
            (responses_dir / f"{mode}.{i}.out").write_text(response["stdout"], encoding="utf-8")
            (responses_dir / f"{mode}.{i}.err").write_text(response["stderr"], encoding="utf-8")
            (responses_dir / f"{mode}.{i}.rc").write_text(f"{response['rc']}\n", encoding="utf-8")
    if scenario.get("head_fails"):
        (tmp_path / "head_fails").touch()
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "BASH_ENV": str(functions),
        "FAKE_GH_STATE": str(tmp_path),
    }


def read_calls(tmp_path: Path, modes: tuple[str, ...] = ("required", "all", "runs")) -> dict[str, int]:
    """How many times the fake `gh` served each mode."""
    result = {}
    for mode in modes:
        calls_path = tmp_path / f"{mode}.calls"
        result[mode] = int(calls_path.read_text(encoding="utf-8")) if calls_path.exists() else 0
    return result
