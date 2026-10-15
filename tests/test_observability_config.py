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


@pytest.fixture(scope="module")
def ci_text() -> str:
    return CI_WORKFLOW.read_text()


@pytest.fixture(scope="module")
def sonar_text() -> str:
    return SONAR_WORKFLOW.read_text()


@pytest.fixture(scope="module")
def hook_text() -> str:
    return PRE_PUSH_HOOK.read_text()


class TestCoverageReporting:
    def test_python_tests_run_with_coverage(self, ci_text: str) -> None:
        assert "--cov=sova" in ci_text
        assert "--cov-report=xml" in ci_text

    def test_python_tests_run_parallel_with_sysmon_and_timings(self, ci_text: str) -> None:
        # Scope to the python-test job body (up to the next top-level, 2-space-indented
        # job key, skipping job-level comment lines) so another job cannot satisfy
        # these flags.
        job = re.search(r"^  python-test:\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)", ci_text, re.DOTALL | re.MULTILINE)
        assert job is not None, "python-test job not found in ci.yml"
        job_block = job.group(1)
        # Scope to the step itself (its env: block immediately followed by its run:
        # line), not the whole job, so COVERAGE_CORE set on a different step (or only
        # mentioned in a comment) cannot satisfy this assertion, and assert against
        # the pytest invocation itself so the step comment naming a flag cannot stand
        # in for the flag being passed.
        step = re.search(
            r"env:\n\s+COVERAGE_CORE: sysmon\n\s+run: (pytest [^\n]*--cov=sova[^\n]*)",
            job_block,
        )
        assert step is not None, "coverage pytest invocation with COVERAGE_CORE sysmon not found in python-test job"
        pytest_cmd = step.group(1)
        assert "-n auto" in pytest_cmd
        assert "--durations=25" in pytest_cmd
        # xdist worker contention breaches pyproject's 30s global timeout.
        assert "--timeout=60" in pytest_cmd

    def test_sonar_consumes_coverage_xml(self, sonar_text: str) -> None:
        assert "sonar.python.coverage.reportPaths=coverage.xml" in sonar_text

    def test_sonar_never_runs_pytest_itself(self, sonar_text: str) -> None:
        # sonarcloud.yml downloads the coverage-xml artifact ci.yml's python-test
        # job already produced (issue #1187) rather than re-running the suite
        # itself just to generate coverage.xml a second time.
        assert "pytest" not in sonar_text
        assert "coverage-xml" in sonar_text
        assert "download-artifact" in sonar_text

    def test_python_tests_runs_on_push_to_main(self, ci_text: str) -> None:
        # sonarcloud.yml's workflow_run trigger needs an artifact on a push to
        # main too, not just on pull requests.
        job = re.search(r"^  python-test:\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)", ci_text, re.DOTALL | re.MULTILINE)
        assert job is not None, "python-test job not found in ci.yml"
        job_block = job.group(1)
        assert "github.event_name != 'push'" not in job_block


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
