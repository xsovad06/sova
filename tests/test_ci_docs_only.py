"""Tests for the documentation-only CI optimization (issues #857, #1005).

These tests guard the contract that keeps a documentation change from
re-running the expensive CI suite while still satisfying the required status
checks in the main-protection ruleset. They assert on the raw
workflow/command/script text (no YAML dependency for the structural checks) so
they run under the plain dev extras.

The optimization has two layers, and the second is what makes it useful:

1. whole-pull-request: every file the PR touches is markdown. This alone never
   fires on a code-carrying PR, because the PR diff still holds the code that
   was pushed earlier.
2. delta-since-last-green: every file changed since the most recent successful
   run of this workflow on this branch is markdown. This is what makes a
   documentation push on top of already-green code free.

Four invariants are protected:

* the detector classifies paths correctly and fails open on every bad input
* both workflows use the shared detector and gate only their expensive steps,
  so required jobs still run and report success
* `/integrate-pr` never pushes on its own account (a push there spends a CI
  cycle on an otherwise-ready PR and delays the merge by the length of the suite)
* `/address-pr` captures documentation and knowledge BEFORE its squash, so the
  content rides the push that branch makes anyway
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
INTEGRATE_SOVA = REPO_ROOT / ".claude" / "commands" / "integrate-pr.md"
INTEGRATE_DIST = REPO_ROOT / "commands" / "integrate-pr.md"
ADDRESS_SOVA = REPO_ROOT / ".claude" / "commands" / "address-pr.md"
ADDRESS_DIST = REPO_ROOT / "commands" / "address-pr.md"

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


@pytest.fixture(scope="module")
def integrate_sova_text() -> str:
    return INTEGRATE_SOVA.read_text()


@pytest.fixture(scope="module")
def integrate_dist_text() -> str:
    return INTEGRATE_DIST.read_text()


@pytest.fixture(scope="module")
def address_sova_text() -> str:
    return ADDRESS_SOVA.read_text()


@pytest.fixture(scope="module")
def address_dist_text() -> str:
    return ADDRESS_DIST.read_text()


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


# ---------------------------------------------------------------------------
# The detector script
# ---------------------------------------------------------------------------


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


INTEGRATE_FIXTURES = ["integrate_sova_text", "integrate_dist_text"]


class TestIntegratePRDoesNotPush:
    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_states_the_no_push_default(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "**This command does not push by default.**" in text

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_rebase_is_conditional_not_unconditional(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A clean, up-to-date PR must never be rebased.

        The main-protection ruleset sets strict_required_status_checks_policy
        to true, so a branch DOES need to be up to date to merge, but that is
        a distinct question from whether it merely has conflicts. A rebase
        must only fire on `mergeable: CONFLICTING` or `mergeStateStatus:
        BEHIND`, never unconditionally on every run.
        """
        text = request.getfixturevalue(fixture)
        assert "mergeable,mergeStateStatus" in text
        assert "strict_required_status_checks_policy: true" in text
        assert "`mergeable: MERGEABLE` and anything else" in text
        assert "Do NOT rebase, do NOT push" in text
        assert "`mergeable: CONFLICTING`" in text
        assert "`mergeStateStatus: BEHIND`" in text

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_behind_state_is_a_push_justification(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A clean-but-stale branch must be updated, not left to fail at merge.

        Under the strict policy, GitHub refuses to merge a BEHIND branch no
        matter how green its existing checks are, so this is a real,
        unavoidable cost distinct from routine hygiene.
        """
        text = request.getfixturevalue(fixture)
        assert "#### Update subroutine" in text
        assert "Handles both a real conflict" in text
        assert "clean-but-stale" in text

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_documentation_check_runs_before_phase2_push_not_after(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """The documentation check must run BEFORE Phase 2's own push, not after it.

        The first version of this fix put the fold-in in Phase 3, timed as
        "before the Phase 2 push", but Phase 3 textually and procedurally
        runs AFTER Phase 2, so that push has already gone out by the time
        Phase 3 could act. Amending afterward only rewrote the local commit
        and never reached the remote PR head that Phase 5 actually merges.
        The fix moves the check into the Update subroutine's own step 7,
        immediately before ITS push, so folding in is actually free rather
        than impossible.
        """
        text = request.getfixturevalue(fixture)
        step7 = text[text.index("7. **Before pushing") : text.index("### Phase 3:")]
        assert "fold in any stale documentation for free" in step7
        assert "run the Phase 3 check" in step7 or "Phase 3 documentation" in step7 or "Phase 3 check" in step7
        assert "git add -A .claude/agent-memory/" in step7
        assert "git commit --amend --no-edit" in step7
        assert 'git push --force-with-lease && echo 1 > "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed"' in step7

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_phase3_is_a_no_op_when_phase2_already_pushed(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Phase 3 must skip entirely once Phase 2's push has already happened.

        Running the check a second time here would find nothing new (Phase 2's
        step 7 already folded it in) and risks a confusing, unjustified second
        amend after the push has already gone out.
        """
        text = request.getfixturevalue(fixture)
        phase3 = text[text.index("### Phase 3:") : text.index("### Phase 4:")]
        assert "Skip this phase entirely if Phase 2 already pushed" in phase3
        assert 'PUSHED=$(cat "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed" 2>/dev/null || echo 0)' in phase3
        assert "pending-docs.md" in phase3

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_pending_docs_queue_resolves_primary_checkout_explicitly(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """The queue write must not assume CWD is the primary checkout.

        Worktree isolation means CWD is usually a per-issue worktree, not the
        primary checkout, and .claude/agent-control/ is not mirrored into
        worktrees, so a bare relative path silently writes to (or reads from)
        the wrong directory's queue file.
        """
        text = request.getfixturevalue(fixture)
        assert "git rev-parse --git-common-dir" in text
        assert 'PRIMARY_ROOT="${COMMON_DIR%/.git}"' in text
        assert "PRIMARY_ROOT/.claude/agent-control/pending-docs.md" in text

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_pushed_flag_persisted_to_state_file(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """The push flag must be a file, not a shell variable.

        Phases 2 through 4 run as separate command invocations, and shell
        variables do not persist across them.
        """
        text = request.getfixturevalue(fixture)
        assert 'echo 0 > "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed"' in text
        assert 'echo 1 > "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed"' in text
        assert "PUSHED=1" not in text

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_state_file_path_is_absolute_everywhere(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """No phase may read or write the state file with a bare relative path.

        The Update subroutine can leave the agent's CWD in a per-issue
        worktree, and .claude/agent-control/ is not mirrored into worktrees,
        so any relative reference to integrate-pushed reads or writes a
        different file than the one the other phases use.
        """
        text = request.getfixturevalue(fixture)
        assert "> .claude/agent-control/integrate-pushed" not in text
        assert "cat .claude/agent-control/integrate-pushed" not in text
        assert text.count('PRIMARY_ROOT/.claude/agent-control/integrate-pushed"') >= 4

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_update_subroutine_returns_to_primary_checkout(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Later phases must not inherit the Update subroutine's worktree cwd.

        Phase 6 checks out the base branch, which is normally already checked
        out in the primary checkout; running that command from a worktree the
        Update subroutine cd'd into fails and stops cleanup before it starts.
        """
        text = request.getfixturevalue(fixture)
        step8 = text[text.index("8. **Return to the primary checkout") : text.index("### Phase 3:")]
        assert 'cd "$PRIMARY_ROOT"' in step8

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_phase6_returns_to_primary_before_checkout(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Phase 6 must not assume Phase 2 left it in the primary checkout."""
        text = request.getfixturevalue(fixture)
        phase6 = text[text.index("### Phase 6:") : text.index("### Phase 7:")]
        # rindex: the phase's own prose mentions "git checkout <BASE_BRANCH>"
        # once before the code block; the actual command is the last match.
        checkout_idx = phase6.rindex("git checkout <BASE_BRANCH>")
        common_dir_idx = phase6.index("git rev-parse --git-common-dir")
        assert common_dir_idx < checkout_idx

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_fork_head_branch_deletion_is_skipped(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A fork PR's HEAD_BRANCH name must never be deleted from `origin`.

        HEAD_BRANCH names a branch, not a repository; `origin` is the base
        repository, so an unconditional delete could remove an unrelated
        upstream branch that happens to share the fork contributor's branch
        name.
        """
        text = request.getfixturevalue(fixture)
        phase6 = text[text.index("### Phase 6:") : text.index("### Phase 7:")]
        assert "isCrossRepository" in phase6
        delete_idx = phase6.index("git push origin --delete <HEAD_BRANCH>")
        guard_idx = phase6.index('if [ "$IS_FORK" != "true" ]; then')
        assert guard_idx < delete_idx

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_phase4_fast_path(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Phase 4 must skip the poll when nothing was re-pushed and CI is green."""
        text = request.getfixturevalue(fixture)
        assert 'PUSHED=$(cat "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed" 2>/dev/null || echo 0)' in text
        assert 'if [ "$PUSHED" -eq 0 ]; then' in text
        assert "Skipping the poll" in text

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_still_polls_when_pushed(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """The full poll loop must remain for the conflict-resolved path."""
        text = request.getfixturevalue(fixture)
        assert "CI poll attempt" in text
        assert "fall through to the poll" in text

    @pytest.mark.parametrize("fixture", INTEGRATE_FIXTURES)
    def test_rule_forbids_gratuitous_push(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "Never push unless GitHub refuses to merge the PR as it stands." in text

    def test_variants_stay_in_sync(self, integrate_sova_text: str, integrate_dist_text: str) -> None:
        """Both copies must be byte-identical, matching the address-pr precedent.

        A substring-only check previously let real drift through: the SOVA
        variant (the one that actually drives this repo's own pipeline) and
        the distributable variant disagreed on CI-failure wording, JSON field
        quoting, and the distributable variant was missing an entire
        post-merge cleanup block (remote branch delete fallback, sova cleanup
        --all). None of that is content that should differ between the two
        copies. CodeRabbit reviews only commands/, so drift in the .claude/
        variant goes undetected by human review too.
        """
        assert integrate_sova_text == integrate_dist_text


# ---------------------------------------------------------------------------
# address-pr: capture documentation before the squash
# ---------------------------------------------------------------------------


ADDRESS_FIXTURES = ["address_sova_text", "address_dist_text"]


class TestAddressPRCapturesEarly:
    @pytest.mark.parametrize("fixture", ADDRESS_FIXTURES)
    def test_capture_step_precedes_the_squash(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Knowledge written after the push cannot ride it.

        The old ordering wrote the cookbook in the last step, long after the
        push, leaving the edits dirty in the worktree for /integrate-pr to
        sweep up into an amend that cost a full CI cycle.
        """
        text = request.getfixturevalue(fixture)
        capture = text.index("**Capture knowledge and documentation updates NOW, before the squash**")
        squash = text.index("**Squash fixes into original commits**")
        push = text.index("**Push and wait for CI**")
        assert capture < squash < push

    @pytest.mark.parametrize("fixture", ADDRESS_FIXTURES)
    def test_capture_step_drains_the_pending_queue(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert ".claude/agent-control/pending-docs.md" in text

    @pytest.mark.parametrize("fixture", ADDRESS_FIXTURES)
    def test_no_trailing_memory_step(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """The old step 16 must be gone, not duplicated."""
        text = request.getfixturevalue(fixture)
        assert "**Update memory**" not in text

    @pytest.mark.parametrize("fixture", ADDRESS_FIXTURES)
    def test_steps_are_numbered_contiguously(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        numbers = [int(m) for m in re.findall(r"^(\d+)\. ", text, re.MULTILINE)]
        assert numbers == list(range(1, len(numbers) + 1)), numbers

    def test_variants_stay_in_sync(self, address_sova_text: str, address_dist_text: str) -> None:
        assert address_sova_text == address_dist_text
