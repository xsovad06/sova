"""Tests for the docs-only CI optimization (issue #857).

These tests guard the structural contract that keeps a documentation-only
change from re-running the expensive CI suite while still satisfying the
required status checks in the main-protection ruleset. They assert on the
raw workflow/command text (no YAML dependency) so they run under the plain
dev extras.

Two invariants are protected:

1. Workflow gate job: the CI and SonarCloud workflows must detect whether
   non-doc code changed and gate their expensive steps on that signal, while
   the required jobs still run and report success on docs-only changes.
2. integrate-pr command: Phase 3 must only amend/force-push when something
   changed, and Phase 4 must skip the CI poll when nothing was re-pushed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from shlex import quote as shlex_quote

import pytest

REPO_ROOT = Path(__file__).parent.parent

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SONAR_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "sonarcloud.yml"
INTEGRATE_SOVA = REPO_ROOT / ".claude" / "commands" / "integrate-pr.md"
INTEGRATE_DIST = REPO_ROOT / "commands" / "integrate-pr.md"

# Jobs that are REQUIRED status checks in the main-protection ruleset. They
# must always run (never be skipped wholesale) so the required check reports.
REQUIRED_CI_JOB_NAMES = {
    "Python Lint",
    "Python Tests",
    "Integration Test",
    "Static Checks",
}


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


# ---------------------------------------------------------------------------
# Lever 2: CI workflow gate job
# ---------------------------------------------------------------------------


class TestCIWorkflowGate:
    def test_changes_job_exists(self, ci_text: str) -> None:
        """A `changes` job must exist to detect code vs docs-only changes."""
        assert "\n  changes:" in ci_text
        assert "Detect Changed Paths" in ci_text

    def test_changes_job_exposes_code_output(self, ci_text: str) -> None:
        """The gate job must expose a `code` output for downstream jobs."""
        assert "outputs:" in ci_text
        assert "code: ${{ steps.decide.outputs.code }}" in ci_text

    def test_filter_uses_inverse_code_detection(self, ci_text: str) -> None:
        """The filter must detect the code inverse, not an all-docs quantifier.

        paths-filter answers "did ANY file match" (an OR model). Expressing
        "every changed file is a doc" requires the inverse: a `code` filter
        with `some-with-excludes` that matches any non-doc file. The naive
        `predicate-quantifier: 'every'` over doc globs is a known trap
        (it ANDs patterns per file, so it is effectively always false).
        """
        assert "predicate-quantifier: 'some-with-excludes'" in ci_text
        assert "predicate-quantifier: 'every'" not in ci_text
        # Positive catch-all plus the single markdown exclusion.
        assert "- '**'" in ci_text
        assert "- '!**/*.md'" in ci_text

    def test_paths_filter_is_v4(self, ci_text: str) -> None:
        """some-with-excludes only exists in dorny/paths-filter v4.

        v3 supports only 'some' and 'every'; pinning @v3 with this quantifier
        fails the step at runtime with an unknown-value error.
        """
        assert "dorny/paths-filter@v4" in ci_text
        assert "dorny/paths-filter@v3" not in ci_text

    def test_only_markdown_is_excluded(self, ci_text: str) -> None:
        """Only *.md is documentation; docs/ and .claude/ subtrees contain code.

        Excluding whole subtrees (docs/**, .claude/**) would misclassify the
        .claude/benchmark/*.sh hooks, .claude/commands/.sova-manifest.json, and
        docs/pipeline-determinism.html as docs and skip the test suite for a
        real code change. The filter must exclude markdown only.
        """
        assert "- '!docs/**'" not in ci_text
        assert "- '!.claude/**'" not in ci_text

    def test_decide_step_fails_open(self, ci_text: str) -> None:
        """A non-'false' filter output (empty/malformed) must run the full suite.

        Only an explicit `code=false` from the filter (a genuine docs-only PR)
        may skip. An empty output from a partial action failure must not be
        read as docs-only, which would merge real code untested.
        """
        assert 'elif [ "${{ steps.filter.outputs.code }}" = "false" ]; then' in ci_text

    def test_push_events_always_run_full_suite(self, ci_text: str) -> None:
        """Push to main must never be treated as docs-only (no PR diff base)."""
        # The decide step forces code=true for non-pull_request events.
        assert 'if [ "${{ github.event_name }}" != "pull_request" ]; then' in ci_text
        assert 'echo "code=true"' in ci_text

    def test_expensive_jobs_depend_on_changes(self, ci_text: str) -> None:
        """Python Tests and Integration Test must gate on the changes job."""
        # Assert the declaration per job block (a bare file-wide count would be
        # satisfied by prose comments or an unrelated third job).
        for job in ("python-test", "integration"):
            decls = [ln.strip() for ln in _job_block(ci_text, job).splitlines()]
            assert "needs: changes" in decls, f"{job} must gate on changes"

    def test_expensive_jobs_gate_steps_on_code(self, ci_text: str) -> None:
        """Expensive steps run unless the change is explicitly docs-only.

        Steps guard on `code != 'false'` (run) and the skip step on
        `code == 'false'`. This is the fail-open direction: an empty/missing
        gate output (skipped or failed `changes` job) is not 'false', so the
        real steps run rather than falsely reporting docs-only success.
        """
        assert "needs.changes.outputs.code != 'false'" in ci_text
        # A docs-only branch must have an explicit success-reporting step.
        assert "needs.changes.outputs.code == 'false'" in ci_text

    def test_expensive_jobs_fail_open_when_gate_fails(self, ci_text: str) -> None:
        """A failed/skipped `changes` gate must not strand the required checks.

        With `needs: changes`, a failed gate would skip the dependent job by
        default, leaving the required status check pending forever. `always()`
        on the job-level `if` forces the job to run and report regardless.
        """
        test_block = _job_block(ci_text, "python-test")
        integ_block = _job_block(ci_text, "integration")
        assert "if: always()" in test_block
        assert "if: always()" in integ_block

    def test_required_jobs_not_skipped_wholesale(self, ci_text: str) -> None:
        """Required jobs must never carry a job-level paths-based skip that
        would leave the required check pending forever."""
        # A blanket paths-ignore on the workflow triggers would strand the
        # required checks. Ensure it is absent.
        assert "paths-ignore:" not in ci_text
        # Every required job name is still declared in the workflow.
        for name in REQUIRED_CI_JOB_NAMES:
            assert f"name: {name}" in ci_text

    def test_lint_and_static_always_run(self, ci_text: str) -> None:
        """Fast jobs that also cover markdown must not gate on code changes."""
        # Locate the lint-static and python-lint job blocks and confirm they
        # do not declare `needs: changes` (they run unconditionally). Match a
        # declaration LINE (stripped), not any substring, so prose comments
        # mentioning "needs: changes" do not trip the assertion.
        for job in ("lint-static", "python-lint"):
            decls = [ln.strip() for ln in _job_block(ci_text, job).splitlines()]
            assert "needs: changes" not in decls, f"{job} must not gate on changes"


class TestSonarCloudWorkflowGate:
    def test_detects_code_changes(self, sonar_text: str) -> None:
        """SonarCloud must detect code changes via the API (pull_request_target-safe)."""
        assert "Detect code changes" in sonar_text
        assert "id: changes" in sonar_text
        # Uses the GitHub API for changed files, not a working-tree diff.
        assert "pulls/${{ github.event.pull_request.number }}/files" in sonar_text

    def test_push_always_runs(self, sonar_text: str) -> None:
        """Push events must always run the full analysis."""
        assert 'if [ "${{ github.event_name }}" != "pull_request_target" ]; then' in sonar_text

    def test_expensive_steps_gated_on_code(self, sonar_text: str) -> None:
        """Coverage and scan steps must be gated on code having changed."""
        assert "steps.changes.outputs.code == 'true'" in sonar_text
        assert "steps.changes.outputs.code != 'true'" in sonar_text

    def test_only_markdown_classified_as_docs(self, sonar_text: str) -> None:
        """The classifier must treat only *.md as docs, not whole subtrees.

        docs/ and .claude/ hold non-md code, so a subtree glob would skip the
        scan for a real code change.
        """
        assert "*.md) ;;" in sonar_text
        assert "docs/*" not in sonar_text
        assert ".claude/*" not in sonar_text

    def test_no_blanket_paths_ignore(self, sonar_text: str) -> None:
        """A blanket paths-ignore would strand the required SonarCloud check."""
        assert "paths-ignore:" not in sonar_text

    def test_empty_file_list_fails_safe_to_run(self, sonar_text: str) -> None:
        """An empty/failed changed-file list must run the full analysis, not skip.

        Guards against the vacuous-truth hazard: an API hiccup or empty diff
        must not be misread as docs-only and skip the required scan.
        """
        assert 'if [ -z "$files" ]; then' in sonar_text
        # The empty branch forces code=true (run everything).
        empty_branch = sonar_text.split('if [ -z "$files" ]; then', 1)[1][:120]
        assert 'echo "code=true"' in empty_branch


# ---------------------------------------------------------------------------
# Lever 1: integrate-pr command logic
# ---------------------------------------------------------------------------


class TestIntegratePRDocsOnly:
    @pytest.mark.parametrize("fixture", ["integrate_sova_text", "integrate_dist_text"])
    def test_phase3_guards_no_op_amend(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Phase 3 must not amend/push when nothing staged (avoids no-op CI cycle)."""
        text = request.getfixturevalue(fixture)
        assert "git diff --cached --quiet" in text

    @pytest.mark.parametrize("fixture", ["integrate_sova_text", "integrate_dist_text"])
    def test_pushed_flag_persisted_to_state_file(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """The push flag must be written to a state file, not a shell variable.

        Phases 2 through 4 run as separate command invocations, and shell
        variables do not persist across them. The flag lives in
        .claude/agent-control/integrate-pushed (a gitignored control dir) so
        Phase 4 can read what Phases 2 and 3 wrote.
        """
        text = request.getfixturevalue(fixture)
        assert "echo 0 > .claude/agent-control/integrate-pushed" in text
        assert "echo 1 > .claude/agent-control/integrate-pushed" in text
        # No reliance on an in-memory shell variable across blocks.
        assert "PUSHED=1" not in text

    @pytest.mark.parametrize("fixture", ["integrate_sova_text", "integrate_dist_text"])
    def test_phase4_fast_path(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Phase 4 must skip the poll when nothing was re-pushed and CI is green."""
        text = request.getfixturevalue(fixture)
        # Reads the persisted flag, defaulting to 0 when the file is absent.
        assert "PUSHED=$(cat .claude/agent-control/integrate-pushed 2>/dev/null || echo 0)" in text
        assert 'if [ "$PUSHED" -eq 0 ]; then' in text
        assert "Skipping the poll" in text

    @pytest.mark.parametrize("fixture", ["integrate_sova_text", "integrate_dist_text"])
    def test_still_polls_when_pushed(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """The full poll loop must remain for the pushed / not-green path."""
        text = request.getfixturevalue(fixture)
        assert "CI poll attempt" in text
        assert "fall through to the poll" in text


# ---------------------------------------------------------------------------
# Behavioral: run the actual sonar classifier logic against real paths
# ---------------------------------------------------------------------------


def _extract_sonar_classifier() -> str:
    """Extract the changed-file classifier body verbatim from sonarcloud.yml.

    Pulls the shell lines from the `code="false"` assignment through the
    `echo "code=$code"` output line out of the workflow's run block. Executing
    this extracted text (rather than a hand-copied duplicate) is what makes the
    behavioral test a real regression guard: if someone broadens the doc
    exclusion in the workflow, the extracted logic changes and the test catches
    it. Fails loudly if the anchors move so the extraction cannot silently
    degrade to matching nothing.
    """
    lines = SONAR_WORKFLOW.read_text().splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if start is None and line.strip() == 'code="false"':
            start = i
        elif start is not None and line.strip().startswith('echo "code=$code"'):
            end = i
            break
    assert start is not None and end is not None, "sonar classifier anchors not found in sonarcloud.yml"
    # Strip the workflow's YAML indentation so the block is a valid script.
    body = [ln.strip() for ln in lines[start : end + 1]]
    # The workflow writes to $GITHUB_OUTPUT; redirect that to stdout for the test.
    body[-1] = 'echo "$code"'
    return "\n".join(body)


def _classify_sonar(files: list[str]) -> str:
    """Run the real sonarcloud.yml changed-file classifier in a shell.

    Feeds representative paths through the classifier body extracted from the
    workflow so the test exercises the same globbing the CI does, not a string
    match. Guards against a regression that broadens the doc exclusion and skips
    the scan for real code.
    """
    joined = "\n".join(files)
    classifier = _extract_sonar_classifier()
    script = f"""
    files={shlex_quote(joined)}
    if [ -z "$files" ]; then echo "true"; exit 0; fi
    {classifier}
    """
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return out.stdout.strip()


class TestSonarClassifierBehavior:
    @pytest.mark.parametrize(
        "files,expected",
        [
            (["README.md"], "false"),
            (["docs/VISION.md", "AGENTS.md"], "false"),
            ([".claude/rules/architecture.md"], "false"),
            (["sova/foo.py"], "true"),
            ([".claude/benchmark/log.sh"], "true"),
            ([".claude/commands/.sova-manifest.json"], "true"),
            (["docs/pipeline-determinism.html"], "true"),
            ([".github/workflows/ci.yml"], "true"),
            (["README.md", "sova/foo.py"], "true"),  # mixed -> code
            ([], "true"),  # empty -> fail-safe to run
        ],
    )
    def test_classifier(self, files: list[str], expected: str) -> None:
        assert _classify_sonar(files) == expected


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_JOB_KEY_RE = re.compile(r"^  [A-Za-z0-9_-]+:")


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
        # Next job at the same 2-space indent ends this block.
        if _JOB_KEY_RE.match(line):
            break
        block.append(line)
    return "\n".join(block)
