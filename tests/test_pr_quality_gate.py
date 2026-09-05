"""Tests for the PR spam quality gate (issue #818).

Two layers are covered:

1. Behaviour of `.github/scripts/detect-pr-spam.sh`, the standalone detector
   that decides whether a pull request matches a high-confidence spam signal.
   The script is invoked as a subprocess with a PR body and a changed-file
   list so every rule (and every documented false-positive guard) is exercised
   locally instead of only in CI.
2. Structural contracts of the workflows that call it. `PR Quality Gate` is
   intended to become a required status check, so the job must never carry a
   job-level `if:` (a skipped job never reports its check), must never check
   out pull request head code under `pull_request_target`, and must pass the
   PR body through the environment rather than interpolating it into a shell
   script.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from shlex import quote as shlex_quote

import pytest

REPO_ROOT = Path(__file__).parent.parent

DETECT_SCRIPT = REPO_ROOT / ".github" / "scripts" / "detect-pr-spam.sh"
GATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-quality-gate.yml"
LABEL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "label-gate.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"
CODERABBIT = REPO_ROOT / ".coderabbit.yaml"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
README = REPO_ROOT / "README.md"
PR_TEMPLATE = REPO_ROOT / ".github" / "pull_request_template.md"
ISSUE_TEMPLATES = sorted((REPO_ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.md"))

COMMUNITY_FILES = ["CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "CODEOWNERS", "LICENSE"]


def _detect(body: str, files: list[str]) -> dict[str, str]:
    """Run the detector and return its key=value verdict as a dict."""
    result = subprocess.run(
        ["bash", str(DETECT_SCRIPT)],
        input="\n".join(files),
        env={**os.environ, "PR_BODY": body},
        capture_output=True,
        text=True,
        check=True,
    )
    return dict(line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line)


@pytest.fixture(scope="module")
def gate_text() -> str:
    return GATE_WORKFLOW.read_text()


@pytest.fixture(scope="module")
def label_text() -> str:
    return LABEL_WORKFLOW.read_text()


# ---------------------------------------------------------------------------
# Rule (a): bounty slash command in the PR body
# ---------------------------------------------------------------------------


class TestBountyCommandRule:
    @pytest.mark.parametrize(
        "body",
        [
            "/claim",
            "/bounty",
            "/reward",
            "/claim #818",
            "  /claim this issue",
            "Hello\n/bounty\nthanks",
            "/CLAIM",
            "/claim-issue 818",
        ],
    )
    def test_bounty_command_is_spam(self, body: str) -> None:
        verdict = _detect(body, ["sova/core/workflow.py"])
        assert verdict == {"spam": "true", "reason": "bounty-command"}

    @pytest.mark.parametrize(
        "body",
        [
            "I found this on a bounty board and wanted to help.",
            "This does not /claim anything.",
            "/claimed by nobody",
            "See the /claims endpoint for details.",
            "The reward for good code is more code.",
            "",
        ],
    )
    def test_prose_mentions_are_not_spam(self, body: str) -> None:
        verdict = _detect(body, ["sova/core/workflow.py"])
        assert verdict["spam"] == "false"


# ---------------------------------------------------------------------------
# Rule (b): bounty-named files in the changed-file list
# ---------------------------------------------------------------------------


class TestBountyFileRule:
    @pytest.mark.parametrize(
        "path",
        [
            "BOUNTY_FIX.md",
            "bounty-fix.md",
            "docs/BOUNTY.md",
            "CLAIM.md",
            "reward.txt",
            "src/bounty.py",
        ],
    )
    def test_bounty_named_file_is_spam(self, path: str) -> None:
        verdict = _detect("Adds a file.", [path])
        assert verdict == {"spam": "true", "reason": "bounty-file"}

    @pytest.mark.parametrize(
        "path",
        [
            "boundary.md",
            "reclaim.md",
            "sova/claims.py",
            "docs/rewards-analysis.md",
            "README.md",
            "sova/core/workflow.py",
        ],
    )
    def test_similar_names_are_not_spam(self, path: str) -> None:
        verdict = _detect("Adds a file.", [path])
        assert verdict["spam"] == "false"

    def test_bounty_file_detected_beyond_the_first_entry(self) -> None:
        files = [f"sova/module_{i}.py" for i in range(200)] + ["BOUNTY.md"]
        verdict = _detect("Adds files.", files)
        assert verdict == {"spam": "true", "reason": "bounty-file"}


# ---------------------------------------------------------------------------
# Rule (c): closing keyword plus community-files-only diff
# ---------------------------------------------------------------------------


class TestCommunityFilesOnlyRule:
    @pytest.mark.parametrize("keyword", ["Closes", "closes", "Fixes", "fixed", "Resolves", "resolve"])
    def test_closing_keyword_with_community_files_only_is_spam(self, keyword: str) -> None:
        verdict = _detect(f"{keyword} #811", ["CODE_OF_CONDUCT.md"])
        assert verdict == {"spam": "true", "reason": "community-files-only"}

    @pytest.mark.parametrize("path", COMMUNITY_FILES)
    def test_each_community_file_counts(self, path: str) -> None:
        verdict = _detect("Closes #811", [path])
        assert verdict["spam"] == "true"

    def test_community_files_without_closing_keyword_are_allowed(self) -> None:
        verdict = _detect("Fixes a typo in the code of conduct.", ["CODE_OF_CONDUCT.md"])
        assert verdict["spam"] == "false"

    @pytest.mark.parametrize("path", ["README.md", "docs/VISION.md", "sova/core/workflow.py"])
    def test_real_change_with_closing_keyword_is_allowed(self, path: str) -> None:
        verdict = _detect("Closes #811", [path])
        assert verdict["spam"] == "false"

    def test_mixed_community_and_real_files_are_allowed(self) -> None:
        verdict = _detect("Closes #811", ["CONTRIBUTING.md", "sova/cli/app.py"])
        assert verdict["spam"] == "false"

    def test_closing_keyword_needs_a_word_boundary(self) -> None:
        """`prefixes #12` contains `fixes` but is not a closing keyword."""
        verdict = _detect("This prefixes #12 with a note.", ["CODE_OF_CONDUCT.md"])
        assert verdict["spam"] == "false"

    @pytest.mark.parametrize("path", [".github/CODEOWNERS", ".github/CONTRIBUTING.md", "docs/CODE_OF_CONDUCT.md"])
    def test_canonical_community_locations_count(self, path: str) -> None:
        """GitHub reads community health files from the root, .github/ and docs/."""
        verdict = _detect("Closes #811", [path])
        assert verdict == {"spam": "true", "reason": "community-files-only"}

    @pytest.mark.parametrize(
        "path",
        [
            "templates/CONTRIBUTING.md",
            "guidelines/CODE_OF_CONDUCT.md",
            "sova/dashboard/static/LICENSE",
            "a/b/CODEOWNERS",
        ],
    )
    def test_nested_lookalikes_are_not_community_files(self, path: str) -> None:
        """The rule matches full paths: a nested file must not put a PR in scope."""
        verdict = _detect("Closes #811", [path])
        assert verdict["spam"] == "false"


# ---------------------------------------------------------------------------
# Fail-open behaviour and CLI contract
# ---------------------------------------------------------------------------


class TestDetectorContract:
    def test_empty_file_list_fails_open(self) -> None:
        verdict = _detect("Closes #811", [])
        assert verdict == {"spam": "false", "reason": "no-files"}

    def test_body_only_rule_still_applies_without_a_file_list(self) -> None:
        """An API failure yields an empty list, but rule (a) needs only the body."""
        verdict = _detect("/claim #818", [])
        assert verdict == {"spam": "true", "reason": "bounty-command"}

    def test_file_rules_fail_open_without_a_file_list(self) -> None:
        """Rules (b) and (c) are skipped rather than guessed at."""
        assert _detect("Closes #811", []) == {"spam": "false", "reason": "no-files"}
        assert _detect("", []) == {"spam": "false", "reason": "no-files"}

    def test_empty_body_and_real_files_are_clean(self) -> None:
        verdict = _detect("", ["sova/core/workflow.py"])
        assert verdict == {"spam": "false", "reason": "none"}

    def test_missing_body_env_var_does_not_fail(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "PR_BODY"}
        result = subprocess.run(
            ["bash", str(DETECT_SCRIPT)],
            input="sova/core/workflow.py",
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "spam=false" in result.stdout

    def test_reason_is_from_a_fixed_token_set(self) -> None:
        """Reasons must never echo untrusted input into GITHUB_OUTPUT."""
        verdict = _detect("Closes #1", ["evil=1\nspam=true/BOUNTY.md"])
        assert verdict["reason"] in {"bounty-command", "bounty-file", "community-files-only", "no-files", "none"}

    def test_stdin_is_drained_before_rules_are_evaluated(self) -> None:
        """The workflow pipes the file list in under `set -o pipefail`.

        Exiting before stdin is consumed kills the producer with SIGPIPE, which
        fails the pipeline (exit 141) on exactly the clearest spam signal.
        """
        files = "\n".join(f"sova/module_{i}.py" for i in range(5000))
        script = f'set -euo pipefail; printf "%s\\n" "$FILES" | {shlex_quote(str(DETECT_SCRIPT))}'
        result = subprocess.run(
            ["bash", "-c", script],
            env={**os.environ, "PR_BODY": "/claim", "FILES": files},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "spam=true" in result.stdout

    def test_help_flag_exits_zero(self) -> None:
        result = subprocess.run(["bash", str(DETECT_SCRIPT), "--help"], capture_output=True, text=True, check=True)
        assert "Usage:" in result.stdout

    def test_script_is_executable(self) -> None:
        assert os.access(DETECT_SCRIPT, os.X_OK), f"{DETECT_SCRIPT} is not executable"


# ---------------------------------------------------------------------------
# Workflow structure
# ---------------------------------------------------------------------------


class TestGateWorkflowStructure:
    def test_job_name_matches_the_required_check(self, gate_text: str) -> None:
        assert "name: PR Quality Gate" in gate_text

    def test_runs_on_pull_request_target(self, gate_text: str) -> None:
        assert "pull_request_target:" in gate_text
        assert "pull_request:" not in gate_text

    @pytest.mark.parametrize("action", ["opened", "edited", "reopened", "synchronize"])
    def test_trigger_covers_body_edits_and_updates(self, gate_text: str, action: str) -> None:
        types_line = re.search(r"types: \[(.*?)\]", gate_text)
        assert types_line is not None
        assert action in types_line.group(1)

    def test_gate_job_has_no_job_level_if(self, gate_text: str) -> None:
        """A skipped job never reports its check, stranding a required check."""
        job = _job_block(gate_text, "quality-gate")
        header = job.split("steps:")[0]
        assert not re.search(r"^    if:", header, re.MULTILINE), "gate job must not be skippable at job level"

    def test_trusted_author_associations_are_filtered(self, gate_text: str) -> None:
        for association in ("OWNER", "MEMBER", "COLLABORATOR"):
            assert association in gate_text
        assert "evaluate=false" in gate_text

    def test_bot_authors_are_skipped(self, gate_text: str) -> None:
        """`[bot]` also appears in the comment jq filter, so pin the actual case arm."""
        assert "AUTHOR_LOGIN: ${{ github.event.pull_request.user.login }}" in gate_text
        assert '*"[bot]")' in gate_text

    def test_closed_pull_requests_are_skipped(self, gate_text: str) -> None:
        assert "PR_STATE: ${{ github.event.pull_request.state }}" in gate_text
        assert '[ "$PR_STATE" != "open" ]' in gate_text

    def test_does_not_check_out_pull_request_head(self, gate_text: str) -> None:
        assert "pull_request.head.sha" not in gate_text
        assert "head.ref" not in gate_text
        # Any explicit `ref:` on the checkout would move it off the base branch,
        # which is the whole privilege-escalation vector for pull_request_target.
        assert "ref:" not in gate_text

    def test_every_gh_api_call_is_paginated(self, gate_text: str) -> None:
        """A single unpaginated page (30 items) truncates both lookups."""
        calls = _gh_api_calls(gate_text)
        assert calls, "no gh api calls found"
        for call in calls:
            assert "--paginate" in call, call

    def test_pr_body_is_passed_through_the_environment(self, gate_text: str) -> None:
        """Interpolating the body into a run script allows shell injection."""
        assert "PR_BODY: ${{ github.event.pull_request.body }}" in gate_text
        for line in gate_text.splitlines():
            if "github.event.pull_request.body" in line:
                assert line.strip().startswith("PR_BODY:")

    def test_uses_the_shared_detection_script(self, gate_text: str) -> None:
        assert ".github/scripts/detect-pr-spam.sh" in gate_text

    def test_file_list_is_paginated(self, gate_text: str) -> None:
        assert "--paginate" in gate_text

    def test_enforcement_is_gated_on_the_verdict(self, gate_text: str) -> None:
        assert "steps.detect.outputs.spam == 'true'" in gate_text

    def test_comment_is_idempotent(self, gate_text: str) -> None:
        assert 'contains("PR Quality Gate")' in gate_text

    def test_labelling_is_best_effort(self, gate_text: str) -> None:
        # Join shell line continuations so a wrapped `|| echo ...` still counts.
        joined = gate_text.replace("\\\n", " ")
        label_stmt = next(line for line in joined.splitlines() if "--add-label" in line)
        assert "||" in label_stmt, "label failure must not fail the job or block the close"

    def test_comment_failure_does_not_block_the_close(self, gate_text: str) -> None:
        """A failed `gh pr comment` must not abort the script (set -e) before `gh pr close` runs."""
        joined = gate_text.replace("\\\n", " ")
        comment_stmt = next(line for line in joined.splitlines() if "gh pr comment" in line)
        assert "||" in comment_stmt, "comment failure must not fail the job or block the close"

    def test_permissions_are_minimal(self, gate_text: str) -> None:
        assert "pull-requests: write" in gate_text
        assert "contents: write" not in gate_text


class TestLabelGateWorkflow:
    def test_triggers_on_issue_labelled(self, label_text: str) -> None:
        assert "issues:" in label_text
        assert "types: [labeled]" in label_text

    def test_scoped_to_good_first_issue(self, label_text: str) -> None:
        assert "github.event.label.name == 'good first issue'" in label_text

    def test_comment_is_idempotent(self, label_text: str) -> None:
        assert "Contribution policy notice" in label_text

    def test_states_there_is_no_bounty_program(self, label_text: str) -> None:
        assert "no bounty" in label_text.lower()

    def test_every_gh_api_call_is_paginated(self, label_text: str) -> None:
        calls = _gh_api_calls(label_text)
        assert calls, "no gh api calls found"
        for call in calls:
            assert "--paginate" in call, call


# ---------------------------------------------------------------------------
# Lint coverage and documentation surfaces
# ---------------------------------------------------------------------------


class TestLintCoverage:
    def test_ci_shellcheck_covers_the_script(self) -> None:
        assert ".github/scripts/*.sh" in CI_WORKFLOW.read_text()

    def test_make_lint_bash_covers_the_script(self) -> None:
        assert ".github/scripts/*.sh" in MAKEFILE.read_text()

    def test_coderabbit_reviews_github_infrastructure(self) -> None:
        assert ".github/**" in CODERABBIT.read_text()


class TestDocumentationSurfaces:
    @pytest.mark.parametrize("path", [CONTRIBUTING, README, PR_TEMPLATE], ids=["contributing", "readme", "pr-template"])
    def test_states_there_is_no_bounty_program(self, path: Path) -> None:
        assert "bounty" in path.read_text().lower()

    @pytest.mark.parametrize("path", ISSUE_TEMPLATES, ids=lambda p: p.name)
    def test_issue_templates_state_there_is_no_bounty_program(self, path: Path) -> None:
        assert "bounty" in path.read_text().lower()

    def test_contributing_has_a_policy_section(self) -> None:
        assert "## No Bounty Program" in CONTRIBUTING.read_text()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_JOB_KEY_RE = re.compile(r"^  [A-Za-z0-9_-]+:")


def _gh_api_calls(workflow_text: str) -> list[str]:
    """Return every `gh api` invocation, with shell line continuations joined."""
    joined = workflow_text.replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines() if "gh api " in line]


def _job_block(workflow_text: str, job_key: str) -> str:
    """Return the text of a single job block keyed by its YAML id."""
    lines = workflow_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"  {job_key}:"):
            start = i
            break
    assert start is not None, f"job {job_key!r} not found"
    block: list[str] = [lines[start]]
    for line in lines[start + 1 :]:
        if _JOB_KEY_RE.match(line):
            break
        block.append(line)
    return "\n".join(block)
