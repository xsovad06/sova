"""Tests for the documentation-only CI optimization (issues #857, #1005).

These tests guard the contract that keeps a documentation change from
re-running the expensive CI suite while still satisfying the required status
checks in the main-protection ruleset. They assert on the raw workflow/script
text (no YAML dependency for the structural checks) so they run under the
plain dev extras.

The optimization has two layers, and the second is what makes it useful:

1. whole-pull-request: every file the PR touches is markdown. This alone never
   fires on a code-carrying PR, because the PR diff still holds the code that
   was pushed earlier.
2. delta-since-last-green: every file changed since the most recent successful
   run of this workflow on this branch is markdown. This is what makes a
   documentation push on top of already-green code free.

Two invariants are protected:

* the detector classifies paths correctly and fails open on every bad input
* both workflows use the shared detector and gate only their expensive steps,
  so required jobs still run and report success
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SONAR_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "sonarcloud.yml"
DETECTOR = REPO_ROOT / ".github" / "scripts" / "detect-code-changes.sh"

_JOB_KEY_RE = re.compile(r"^ {2}[A-Za-z0-9_-]+:")

# Jobs that are REQUIRED status checks in the main-protection ruleset. They
# must always run (never be skipped wholesale) so the required check reports.
REQUIRED_CI_JOB_NAMES = {
    "Python Lint",
    "Python Tests",
    "Integration Test",
    "Static Checks",
}

# Jobs whose expensive work is gated on the detector's verdict.
GATED_CI_JOBS = ("python-lint", "python-test", "integration")

# Jobs that must run their real work unconditionally. Markdown can carry a
# leaked credential, a banned em-dash, or a broken command frontmatter, so
# these stay ungated.
UNGATED_CI_JOBS = ("lint-static", "secrets-scan", "invariants")


@pytest.fixture(scope="module")
def ci_text() -> str:
    return CI_WORKFLOW.read_text()


@pytest.fixture(scope="module")
def sonar_text() -> str:
    return SONAR_WORKFLOW.read_text()


def _job_block(workflow_text: str, job_key: str) -> str:
    """Return the text of a single job block keyed by its YAML id.

    A job block runs from `  <job_key>:` up to the next top-level job: any line
    with a 2-space-indented YAML key. The terminator matches the key itself, not
    a trailing colon, so `  python-test:  # gated` (a key with a trailing
    comment) still ends the previous block instead of being absorbed into it.
    """
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


def _run_detector(*args: str, stdin: str = "", **env: str) -> subprocess.CompletedProcess[str]:
    """Run the detector script with a controlled environment."""
    child_env = {**os.environ, **env}
    return subprocess.run(
        [str(DETECTOR), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=child_env,
        check=False,
    )


def _parse_output(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.strip().splitlines() if "=" in line)


class TestDetectorScript:
    def test_exists_and_is_executable(self) -> None:
        assert DETECTOR.is_file()
        assert os.access(DETECTOR, os.X_OK), f"{DETECTOR} must stay executable"

    def test_help_exits_zero(self) -> None:
        result = _run_detector("--help")
        assert result.returncode == 0
        assert "detect-code-changes.sh" in result.stdout

    def test_unknown_mode_is_usage_error(self) -> None:
        assert _run_detector("nonsense").returncode == 2

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            # Documentation: only markdown, anywhere in the tree.
            (["AGENTS.md"], "false"),
            (["README.md", "docs/VISION.md"], "false"),
            ([".claude/agent-memory/cookbook.md", ".claude/rules/architecture.md"], "false"),
            (["docs/nested/deep/guide.md"], "false"),
            # Code: anything that is not markdown, including non-md files
            # living under docs/ and .claude/.
            (["sova/core/steps/develop.py"], "true"),
            (["AGENTS.md", "sova/core/steps/develop.py"], "true"),
            (["docs/pipeline-determinism.html"], "true"),
            ([".claude/benchmark/log.sh"], "true"),
            ([".claude/commands/.sova-manifest.json"], "true"),
            (["tests/test_ci_docs_only.py"], "true"),
            ([".github/workflows/ci.yml"], "true"),
            (["Makefile"], "true"),
            (["sova.toml"], "true"),
        ],
    )
    def test_classify(self, files: list[str], expected: str) -> None:
        result = _run_detector("classify", stdin="\n".join(files) + "\n")
        assert result.returncode == 0, result.stderr
        assert _parse_output(result.stdout)["code"] == expected

    def test_classify_empty_list_fails_open(self) -> None:
        """An empty list proves nothing, so it must not grant a skip."""
        result = _run_detector("classify", stdin="")
        assert _parse_output(result.stdout)["code"] == "true"

    def test_classify_ignores_blank_lines(self) -> None:
        result = _run_detector("classify", stdin="AGENTS.md\n\n\nREADME.md\n")
        assert _parse_output(result.stdout)["code"] == "false"

    def test_push_event_always_runs_full_suite(self) -> None:
        """A push to main has no pull request diff base."""
        out = _parse_output(_run_detector("detect", EVENT_NAME="push").stdout)
        assert out == {"code": "true", "reason": "non-pr"}

    def test_pagination_failure_discards_partial_page_list(self, tmp_path: Path) -> None:
        """A page printed before pagination fails must not count as the full list.

        `gh api --paginate` can print a complete first page and then fail on a
        later one (rate limit, network blip). If that partial output survived,
        a docs-only first page would mask a code-carrying page the failed
        request never reached, wrongly granting a skip.
        """
        fake_gh = tmp_path / "gh"
        fake_gh.write_text("#!/bin/sh\necho README.md\nexit 1\n")
        fake_gh.chmod(0o755)
        env = {
            "EVENT_NAME": "pull_request",
            "REPO": "owner/name",
            "PR_NUMBER": "1",
            "HEAD_SHA": "deadbeef",
            "HEAD_BRANCH": "feat/x",
            "WORKFLOW_FILE": "ci.yml",
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
        }
        out = _parse_output(_run_detector("detect", **env).stdout)
        assert out == {"code": "true", "reason": "no-files"}

    def test_mode_only_change_is_not_identical(self, tmp_path: Path) -> None:
        """A permission-only change must not be masked by a matching blob sha.

        A path whose content is byte-identical between the baseline and head
        trees but whose file mode changed (e.g. `chmod +x` on a `.sh` file) is
        a real change. Comparing on (path, blob sha) alone cannot see it, since
        both trees report the same sha for that path; the mode has to be part
        of the compared identity.
        """
        script_path = ".github/scripts/detect-code-changes.sh"
        baseline_tree = json.dumps(
            {"truncated": False, "tree": [{"path": script_path, "mode": "100644", "type": "blob", "sha": "blobsha1"}]}
        )
        head_tree = json.dumps(
            {"truncated": False, "tree": [{"path": script_path, "mode": "100755", "type": "blob", "sha": "blobsha1"}]}
        )
        fake_gh = tmp_path / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            f'  *"pulls/1/files"*) printf \'%s\\n\' "{script_path}" ;;\n'
            '  *"actions/workflows/ci.yml/runs"*) printf \'%s\\n\' "baselinesha" ;;\n'
            f"  *\"git/trees/baselinesha?recursive=1\"*) printf '%s' '{baseline_tree}' ;;\n"
            f"  *\"git/trees/deadbeef?recursive=1\"*) printf '%s' '{head_tree}' ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        fake_gh.chmod(0o755)
        env = {
            "EVENT_NAME": "pull_request",
            "REPO": "owner/name",
            "PR_NUMBER": "1",
            "HEAD_SHA": "deadbeef",
            "HEAD_BRANCH": "feat/x",
            "WORKFLOW_FILE": "ci.yml",
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
        }
        out = _parse_output(_run_detector("detect", **env).stdout)
        assert out == {"code": "true", "reason": "code-changed"}

    @pytest.mark.parametrize("missing", ["REPO", "PR_NUMBER", "HEAD_SHA", "HEAD_BRANCH", "WORKFLOW_FILE"])
    def test_missing_configuration_fails_open(self, missing: str) -> None:
        """A missing variable must run the suite, never skip it."""
        env = {
            "EVENT_NAME": "pull_request",
            "REPO": "owner/name",
            "PR_NUMBER": "1",
            "HEAD_SHA": "deadbeef",
            "HEAD_BRANCH": "feat/x",
            "WORKFLOW_FILE": "ci.yml",
        }
        env[missing] = ""
        out = _parse_output(_run_detector("detect", **env).stdout)
        assert out == {"code": "true", "reason": "bad-input"}

    def test_only_documented_reasons_can_skip(self) -> None:
        """code=false must be reachable from exactly three reason tokens.

        Anything else silently skipping the suite would be a correctness hole,
        so the script's emit sites are pinned here.
        """
        text = DETECTOR.read_text()
        skipping = set(re.findall(r"emit false (\S+)", text))
        assert skipping == {"docs-only-pr", "docs-only-delta", "identical"}

    def test_delta_compares_trees_not_commit_topology(self) -> None:
        """Documentation reaches a branch as an amend, which is a SIBLING.

        `git commit --amend` plus a force-push produces a commit with the same
        parent as the one that was tested, not a descendant. Every
        commit-topology test therefore reports "diverged" on exactly the case
        this optimization exists to catch, and the compare API (always
        three-dot, so it walks back to the merge base) reports the whole
        amended commit rather than what the amend changed. Only a tree
        comparison answers the real question.
        """
        text = DETECTOR.read_text()
        assert "git/trees/${sha}?recursive=1" in text
        # The commit-topology primitives must not creep back in.
        assert "/compare/" not in text
        assert "diverged" not in text.split("usage()")[-1].split("EOF\n}")[-1]

    def test_truncated_tree_fails_open(self) -> None:
        """A truncated listing cannot prove that nothing else changed."""
        text = DETECTOR.read_text()
        assert "truncated" in text
        assert 'if [ "$truncated" != "false" ]; then' in text

    def test_unreadable_tree_fails_open(self) -> None:
        text = DETECTOR.read_text()
        assert 'if ! baseline_tree="$(fetch_tree "$baseline")" || [ -z "$baseline_tree" ]; then' in text
        assert 'if ! head_tree="$(fetch_tree "$HEAD_SHA")" || [ -z "$head_tree" ]; then' in text

    def test_delta_is_a_symmetric_difference_of_path_and_blob(self) -> None:
        """A path is changed iff its (path, blob sha) pair appears in one tree.

        This catches additions, removals and content changes in one pass.
        """
        text = DETECTOR.read_text()
        assert "sort | uniq -u | cut -f1 | sort -u" in text

    def test_uses_api_not_local_git(self) -> None:
        """The script must not read a working tree.

        It is invoked from a pull_request_target workflow, where the checkout
        in the workspace is fork-controlled content.
        """
        text = DETECTOR.read_text()
        assert "git diff" not in text
        assert "git log" not in text
        assert "git rev-" not in text

    def test_json_is_never_passed_through_echo(self) -> None:
        """`echo` expands backslash escapes in some shells.

        The tree payload is full of them, and an expanded \\n turns valid JSON
        into a parse error. Every JSON hop must use printf.
        """
        text = DETECTOR.read_text()
        assert 'echo "$json"' not in text
        assert "printf '%s' \"$json\"" in text

    def test_documents_why_the_ruleset_keeps_it_sound(self) -> None:
        """A skip re-uses a green result produced against an older base tree.

        That stays sound only because main-protection requires branches to be
        up to date before merge: any later base-branch change must reach this
        branch, with its own real diff, before merge is allowed. The script
        must say so, and must say what breaks if that policy is ever reverted.
        """
        text = DETECTOR.read_text()
        assert "strict_required_status_checks_policy to true" in text
        assert "becomes unsound" in text


# ---------------------------------------------------------------------------
# CI workflow wiring
# ---------------------------------------------------------------------------


class TestCIWorkflowGate:
    def test_changes_job_uses_shared_detector(self, ci_text: str) -> None:
        block = _job_block(ci_text, "changes")
        assert '"$DETECTOR" detect >> "$GITHUB_OUTPUT"' in block
        assert "WORKFLOW_FILE: ci.yml" in block
        assert "code: ${{ steps.decide.outputs.code }}" in block

    def test_changes_job_fetches_detector_from_base_not_head(self, ci_text: str) -> None:
        """A fork must not be able to edit the script that gates its own tests.

        The script is fetched from the base branch through the contents API
        into RUNNER_TEMP, not read from a checkout of the PR itself, and not
        from a full working-tree checkout either (the script only ever calls
        gh api, so a checkout would buy nothing beyond the one file).
        """
        block = _job_block(ci_text, "changes")
        assert "contents/.github/scripts/detect-code-changes.sh?ref=${BASE_SHA}" in block
        assert "BASE_SHA: ${{ github.event.pull_request.base.sha || github.sha }}" in block
        assert "${RUNNER_TEMP}/detect-code-changes.sh" in block
        assert "actions/checkout" not in block

    def test_changes_job_fetch_failure_falls_back_to_full_suite(self, ci_text: str) -> None:
        """A fetch failure (e.g. this script's own introducing PR, before it
        exists on the base branch) must degrade to running the suite, not
        fail the job outright."""
        block = _job_block(ci_text, "changes")
        assert "reason=detector-unavailable" in block
        assert 'echo "code=true" >> "$GITHUB_OUTPUT"' in block

    def test_changes_job_can_read_workflow_runs(self, ci_text: str) -> None:
        """The baseline lookup needs actions:read."""
        block = _job_block(ci_text, "changes")
        assert "actions: read" in block

    @pytest.mark.parametrize("job", GATED_CI_JOBS)
    def test_gated_jobs_run_but_skip_their_work(self, ci_text: str, job: str) -> None:
        """The job itself must always run so the required check reports.

        A blanket job-level skip would leave the required check pending forever
        and strand the PR, which is the exact trap a paths-ignore would set.
        """
        block = _job_block(ci_text, job)
        assert "needs: changes" in block
        assert "if: always()" in block
        # Real work is skipped only on an explicit 'false'.
        assert "needs.changes.outputs.code != 'false'" in block
        assert "needs.changes.outputs.code == 'false'" in block

    @pytest.mark.parametrize("job", GATED_CI_JOBS)
    def test_gated_jobs_fail_open(self, ci_text: str, job: str) -> None:
        """Gating must never be expressed as `== 'true'` across jobs.

        If the `changes` job is skipped or fails, its output is the empty
        string, not 'false'. A `== 'true'` test would then skip the real work;
        a `!= 'false'` test runs it.
        """
        block = _job_block(ci_text, job)
        assert "needs.changes.outputs.code == 'true'" not in block

    @pytest.mark.parametrize("job", UNGATED_CI_JOBS)
    def test_ungated_jobs_never_consult_the_detector(self, ci_text: str, job: str) -> None:
        block = _job_block(ci_text, job)
        assert "needs.changes" not in block

    def test_required_jobs_have_no_skipping_job_level_condition(self, ci_text: str) -> None:
        """Every required check must report on every PR."""
        for job in GATED_CI_JOBS:
            block = _job_block(ci_text, job)
            job_level_ifs = [line.strip() for line in block.splitlines() if line.startswith("    if:")]
            for condition in job_level_ifs:
                assert "always()" in condition, f"{job}: job-level condition may skip the check: {condition}"

    def test_required_job_names_are_present(self, ci_text: str) -> None:
        for name in REQUIRED_CI_JOB_NAMES:
            assert f"name: {name}" in ci_text


# ---------------------------------------------------------------------------
# SonarCloud workflow wiring
# ---------------------------------------------------------------------------


class TestSonarCloudWorkflowGate:
    def test_fetches_detector_from_base_branch(self, sonar_text: str) -> None:
        """Under pull_request_target the workspace holds fork content.

        The detector must come from the base branch through the API and land
        in RUNNER_TEMP, outside the workspace.
        """
        assert "contents/.github/scripts/detect-code-changes.sh?ref=${BASE_SHA}" in sonar_text
        assert "${RUNNER_TEMP}/detect-code-changes.sh" in sonar_text
        assert "BASE_SHA: ${{ github.event.pull_request.base.sha || github.sha }}" in sonar_text

    def test_fetch_is_skipped_on_push_events(self, sonar_text: str) -> None:
        """A push event has no PR diff base, so detect() always returns non-pr.

        Fetching the detector anyway would spend an API call and a
        RUNNER_TEMP write on a result that is discarded unused.
        """
        fetch_step = sonar_text[
            sonar_text.index("- name: Fetch the detector from the base branch") : sonar_text.index(
                "- name: Detect code changes"
            )
        ]
        assert "if: github.event_name == 'pull_request_target'" in fetch_step

    def test_push_events_get_accurate_reason_not_detector_unavailable(self, sonar_text: str) -> None:
        """A push event must report reason=non-pr, not a misleading

        'detector-unavailable': the detector was never needed, not missing.
        """
        detect_step = sonar_text[
            sonar_text.index("- name: Detect code changes") : sonar_text.index("- name: Documentation-only change")
        ]
        assert 'if [ "$EVENT_NAME" != "pull_request_target" ]; then' in detect_step
        assert "reason=non-pr" in detect_step

    def test_missing_detector_runs_full_analysis(self, sonar_text: str) -> None:
        assert "reason=detector-unavailable" in sonar_text
        assert 'echo "code=true" >> "$GITHUB_OUTPUT"' in sonar_text

    def test_uses_its_own_workflow_as_baseline(self, sonar_text: str) -> None:
        """A SonarCloud skip must be justified by a green SonarCloud run."""
        assert "WORKFLOW_FILE: sonarcloud.yml" in sonar_text

    def test_can_read_workflow_runs(self, sonar_text: str) -> None:
        assert "actions: read" in sonar_text

    def test_expensive_steps_are_gated(self, sonar_text: str) -> None:
        """Coverage and the scan are the expensive halves."""
        assert "steps.changes.outputs.code == 'true' && steps.check-token.outputs.available == 'true'" in sonar_text
        assert sonar_text.count("steps.changes.outputs.code == 'true'") >= 5

    def test_job_still_reports_on_documentation_change(self, sonar_text: str) -> None:
        """The required check must be satisfied without the 15-minute run."""
        assert "Documentation-only change (skipping SonarCloud)" in sonar_text
        assert "steps.changes.outputs.code != 'true'" in sonar_text
        # No job-level condition that would skip the whole job.
        assert "\n    if:" not in sonar_text


# ---------------------------------------------------------------------------
# integrate-pr: never push on its own account
# ---------------------------------------------------------------------------
