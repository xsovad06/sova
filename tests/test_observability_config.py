"""Tests for the baseline observability layer (issue #638).

These guard the structural contract of the CI/hook wiring for coverage
reporting and secrets scanning. They assert on raw file text (no YAML or
gitleaks dependency) so they run under the plain dev extras.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SONAR_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "sonarcloud.yml"
PRE_PUSH_HOOK = REPO_ROOT / ".githooks" / "pre-push"
GITLEAKS_CONFIG = REPO_ROOT / ".gitleaks.toml"
MAKEFILE = REPO_ROOT / "Makefile"


@pytest.fixture(scope="module")
def ci_text() -> str:
    return CI_WORKFLOW.read_text()


@pytest.fixture(scope="module")
def sonar_text() -> str:
    return SONAR_WORKFLOW.read_text()


@pytest.fixture(scope="module")
def makefile_text() -> str:
    return MAKEFILE.read_text()


def _job_block(ci_text: str, job_id: str) -> str:
    pattern = rf"^  {job_id}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)"
    match = re.search(pattern, ci_text, re.DOTALL | re.MULTILINE)
    assert match is not None, f"{job_id} job not found in ci.yml"
    return match.group(1)


@pytest.fixture(scope="module")
def hook_text() -> str:
    return PRE_PUSH_HOOK.read_text()


class TestCoverageReporting:
    def test_python_tests_run_with_coverage(self, makefile_text: str) -> None:
        # ci.yml's python-test-matrix job delegates to `make test-ci` (see its
        # own comment) rather than inlining the pytest invocation, so both
        # matrix legs stay byte-identical by construction; the flags live
        # here, in the Makefile target, not in ci.yml itself.
        assert "--cov=sova" in makefile_text
        assert "--cov-report=xml" in makefile_text

    def test_python_test_matrix_calls_the_shared_make_target(self, ci_text: str) -> None:
        job_block = _job_block(ci_text, "python-test-matrix")
        # Scope to the step itself (its env: block immediately followed by its
        # run: line), not the whole job, so COVERAGE_CORE set on a different
        # step (or only mentioned in a comment) cannot satisfy this assertion.
        step = re.search(r"env:\n\s+COVERAGE_CORE: sysmon\n\s+run: (make [^\n]*)", job_block)
        assert step is not None, "`make test-ci` with COVERAGE_CORE sysmon not found in python-test-matrix job"
        assert step.group(1).strip() == "make test-ci"

    def test_python_tests_run_parallel_with_sysmon_and_timings(self, makefile_text: str) -> None:
        # Assert against the pytest invocation itself (the `test-ci` target's
        # own command), so a comment naming a flag elsewhere cannot stand in
        # for the flag actually being passed.
        target = re.search(r"^test-ci:.*\n\t(.*)$", makefile_text, re.MULTILINE)
        assert target is not None, "test-ci target not found in Makefile"
        pytest_cmd = target.group(1)
        assert "--cov=sova" in pytest_cmd
        assert "-n auto" in pytest_cmd
        assert "--durations=25" in pytest_cmd
        # xdist worker contention breaches pyproject's 30s global timeout.
        assert "--timeout=60" in pytest_cmd

    def test_sonar_consumes_coverage_xml(self, sonar_text: str) -> None:
        assert "sonar.python.coverage.reportPaths=${{ runner.temp }}/sova-coverage/coverage.xml" in sonar_text

    def test_sonar_never_runs_pytest_itself(self, sonar_text: str) -> None:
        # sonarcloud.yml downloads the coverage-xml artifact ci.yml's python-test
        # job already produced (issue #1187) rather than re-running the suite
        # itself just to generate coverage.xml a second time. The "no pytest"
        # substring check itself lives only in
        # tests/test_ci_docs_only.py::TestSonarCloudWorkflow::test_no_install_or_test_steps_remain
        # (which also covers pip install / setup-python) to avoid asserting
        # the same thing in two files.
        assert "coverage-xml" in sonar_text
        assert "download-artifact" in sonar_text

    def test_python_tests_runs_on_push_to_main(self, ci_text: str) -> None:
        # sonarcloud.yml's workflow_run trigger needs an artifact on a push to
        # main too, not just on pull requests. Asserts the positive contract
        # (the job runs unconditionally, and its artifact upload step is
        # gated only on the matrix leg, never on the event) rather than the
        # absence of one literal string: excluding push via
        # `if: github.event_name == 'pull_request'`, a `needs` gate, or an
        # event-scoped `if:` added to the upload step would all pass a
        # substring-absence check while still breaking the artifact
        # workflow_run depends on.
        job_block = _job_block(ci_text, "python-test-matrix")
        assert re.search(r"^\s*if:\s*always\(\)\s*$", job_block, re.MULTILINE), (
            "python-test-matrix must run with if: always() so a push event is never excluded"
        )
        rest = job_block[job_block.index("- name: Upload coverage report") :]
        upload_step = rest[: rest.index("with:")]
        assert "github.event_name" not in upload_step, "the artifact upload must not exclude any event"


class TestSecretsScan:
    def test_gitleaks_config_exists_and_extends_default(self) -> None:
        text = GITLEAKS_CONFIG.read_text()
        assert "[extend]" in text
        assert "useDefault = true" in text

    def test_gitleaks_config_allowlists_egress_fixtures(self) -> None:
        text = GITLEAKS_CONFIG.read_text()
        assert "test_egress" in text

    def test_ci_has_secrets_scan_job(self, ci_text: str) -> None:
        assert "secrets-scan:" in ci_text
        assert "gitleaks git" in ci_text

    def test_ci_secrets_scan_not_gated_on_docs_only(self, ci_text: str) -> None:
        # The secrets-scan job must run on every change, so it must not declare
        # `needs: changes` (which would let a docs-only PR skip it). Extract the
        # job body by YAML structure (up to the next top-level, 2-space-indented
        # job key) rather than the name of whichever job happens to follow it:
        # otherwise renaming or removing that job would break this test before
        # it ever checks the condition it's meant to guard.
        match = re.search(r"^  secrets-scan:\n(.*?)(?=^  \S|\Z)", ci_text, re.DOTALL | re.MULTILINE)
        assert match is not None, "secrets-scan job not found in ci.yml"
        job_block = match.group(1)
        assert "needs: changes" not in job_block

    def test_hook_runs_gitleaks_with_skip_fallback(self, hook_text: str) -> None:
        assert "gitleaks git" in hook_text
        assert "SKIP (gitleaks not installed)" in hook_text

    def test_hook_scans_history_not_working_tree(self, hook_text: str) -> None:
        # `gitleaks git` scans committed history so gitignored real secrets
        # (.env) are never read; `gitleaks dir` would leak them into findings.
        assert "gitleaks dir" not in hook_text
