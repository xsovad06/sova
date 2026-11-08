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

Issue #1321: `gh pr checks --required` lists only the required checks that have registered, so a
partial list that is all green read as a pass while a required check (SonarCloud registers minutes
after CI) had not appeared. The block now reads the base branch's required names from the rulesets
and reports a pass only when none is missing; `TestRequiredNamesMissing` covers that.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from sova.commands.catalog import get_canonical_dir
from tests.ci_wait_fakes import (
    FIVE,
    GH_ERROR,
    HEAD_SHA,
    NO_CHECKS,
    NO_REQUIRED,
    NO_RULESETS_PLAN,
    SONAR,
    five,
    install_fakes,
    read_calls,
)
from tests.ci_wait_fakes import (
    checks as _checks,
)
from tests.ci_wait_fakes import (
    paged_rules as _paged_rules,
)
from tests.ci_wait_fakes import (
    rules as _rules,
)
from tests.ci_wait_fakes import (
    runs as _runs,
)

REPO_ROOT = Path(__file__).parent.parent
CANONICAL_PR = get_canonical_dir() / "pr.md"
RENDERED_PR_COPIES = [
    REPO_ROOT / ".claude" / "commands" / "pr.md",
    REPO_ROOT / "plugins" / "sova" / "commands" / "pr.md",
    REPO_ROOT / ".agents" / "skills" / "sova-pr" / "SKILL.md",
]
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="the CI wait block parses gh output with jq")


def _wait_block(path: Path) -> str:
    """Return step 15's bash block from a `/pr` command file, dedented out of its list item."""
    text = path.read_text(encoding="utf-8")
    start = text.index("**Wait for CI pipeline**")
    match = re.search(r"```bash\n(.*?)\n[ \t]*```", text[start:], re.S)
    assert match is not None, f"{path}: no bash block after the CI wait heading"
    return textwrap.dedent(match.group(1))


def _run_wait(scenario: dict[str, object], tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], dict[str, int]]:
    """Run the canonical wait block against a fake `gh` and a no-op `sleep`; return the result and calls per mode."""
    env = install_fakes(tmp_path, scenario)
    result = subprocess.run(
        ["bash", "-c", _wait_block(CANONICAL_PR)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, read_calls(tmp_path)


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


class TestRequiredNamesMissing:
    """Issue #1321: a partial, all-green list of required checks must never read as a pass."""

    def test_missing_checks_are_never_a_pass_and_are_named_in_the_verdict(self, tmp_path: Path) -> None:
        scenario = {"rules": [_rules(*FIVE)], "required": [five("pass", "pass", "pass")]}
        result, calls = _run_wait(scenario, tmp_path)
        assert result.returncode == 0, result.stderr
        assert "CI PASSED" not in result.stdout
        assert (
            _verdict(result)
            == f"CI PENDING (required checks): 0 of 3 still pending; 2 required not registered: {SONAR}, docs"
        )
        assert f"missing\t{SONAR}" in result.stdout
        assert calls == {"required": 16, "all": 0, "runs": 0}

    def test_missing_checks_that_register_later_and_pass(self, tmp_path: Path) -> None:
        scenario = {
            "rules": [_rules(*FIVE)],
            "required": [
                five("pass", "pass", "pass"),
                five("pass", "pass", "pass", "pass", "pass"),
            ],
        }
        result, calls = _run_wait(scenario, tmp_path)
        assert _verdict(result) == "CI PASSED (required checks): all 5 passed or were skipped"
        assert calls["required"] == 2

    def test_missing_check_that_registers_as_skipping_counts_as_satisfied(self, tmp_path: Path) -> None:
        scenario = {
            "rules": [_rules(*FIVE)],
            "required": [
                five("pass", "pass", "pass", "pass"),
                five("pass", "pass", "pass", "pass", "skipping"),
            ],
        }
        result, _calls = _run_wait(scenario, tmp_path)
        assert _verdict(result) == "CI PASSED (required checks): all 5 passed or were skipped"

    def test_a_failure_wins_immediately_while_others_are_missing(self, tmp_path: Path) -> None:
        scenario = {"rules": [_rules(*FIVE)], "required": [five("pass", "fail", "pass")]}
        result, calls = _run_wait(scenario, tmp_path)
        assert _verdict(result) == "CI FAILED (required checks): 1 of 3 did not pass"
        assert calls["required"] == 1

    def test_a_cancel_wins_immediately_while_others_are_missing(self, tmp_path: Path) -> None:
        scenario = {"rules": [_rules(*FIVE)], "required": [five("pass", "cancel", "pass")]}
        result, calls = _run_wait(scenario, tmp_path)
        assert _verdict(result) == "CI CANCELED (required checks): 1 of 3 canceled"
        assert calls["required"] == 1

    def test_names_with_spaces_are_matched_whole(self, tmp_path: Path) -> None:
        """ "SonarCloud" alone must not satisfy "SonarCloud Analysis"."""
        scenario = {
            "rules": [_rules("SonarCloud Analysis", "unit tests")],
            "required": [_checks("pass", "pass", names=["SonarCloud", "unit tests"])],
        }
        result, _calls = _run_wait(scenario, tmp_path)
        assert "CI PASSED" not in result.stdout
        assert _verdict(result).endswith("1 required not registered: SonarCloud Analysis")

    def test_names_from_several_rulesets_are_deduplicated(self, tmp_path: Path) -> None:
        scenario = {
            "rules": [_rules("lint", "lint", "test")],
            "required": [_checks("pass", "pass", names=["lint", "test"])],
        }
        result, _calls = _run_wait(scenario, tmp_path)
        assert _verdict(result) == "CI PASSED (required checks): all 2 passed or were skipped"

    def test_all_required_registered_and_green_passes_on_the_first_poll(self, tmp_path: Path) -> None:
        scenario = {"rules": [_rules(*FIVE)], "required": [five("pass", "pass", "pass", "pass", "pass")]}
        result, calls = _run_wait(scenario, tmp_path)
        assert _verdict(result) == "CI PASSED (required checks): all 5 passed or were skipped"
        assert calls["required"] == 1

    def test_only_non_required_checks_registered_is_never_a_pass(self, tmp_path: Path) -> None:
        """`--required` reports none yet and the registered check is not a required name, so it is not counted."""
        scenario = {
            "rules": [_rules("lint", "test")],
            "required": [NO_REQUIRED],
            "all": [_checks("pass", names=["CodeRabbit"])],
        }
        result, _calls = _run_wait(scenario, tmp_path)
        assert "CI PASSED" not in result.stdout
        assert _verdict(result).endswith("0 of 0 still pending; 2 required not registered: lint, test")
        assert _verdict(result).startswith("CI PENDING (required checks): ")

    def test_zero_checks_with_required_names_keeps_the_no_ci_triggered_path(self, tmp_path: Path) -> None:
        scenario = {"rules": [_rules(*FIVE)], "required": [NO_CHECKS], "runs": [_runs()]}
        result, calls = _run_wait(scenario, tmp_path)
        assert _verdict(result).startswith("NO CI TRIGGERED: ")
        assert calls == {"required": 5, "all": 0, "runs": 5}


class TestRulesLookup:
    def test_an_empty_rule_list_keeps_todays_behavior(self, tmp_path: Path) -> None:
        scenario = {"rules": [{"stdout": "[]\n", "stderr": "", "rc": 0}], "required": [_checks("pass")]}
        result, _calls = _run_wait(scenario, tmp_path)
        assert _verdict(result) == "CI PASSED (required checks): all 1 passed or were skipped"
        assert "missing" not in result.stdout

    def test_a_plan_without_rulesets_counts_as_no_required_names(self, tmp_path: Path) -> None:
        scenario = {"rules": [NO_RULESETS_PLAN], "required": [NO_REQUIRED], "all": [_checks("pass")]}
        result, _calls = _run_wait(scenario, tmp_path)
        assert _verdict(result) == "CI PASSED (all checks): all 1 passed or were skipped"

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param(GH_ERROR, id="http-error"),
            pytest.param({"stdout": "not json\n", "stderr": "", "rc": 0}, id="non-json"),
            pytest.param({"stdout": '{"message": "Not Found"}\n', "stderr": "", "rc": 0}, id="non-array"),
        ],
    )
    def test_a_failed_or_unparseable_lookup_stops_without_a_pass(
        self, tmp_path: Path, response: dict[str, object]
    ) -> None:
        result, calls = _run_wait({"rules": [response], "required": [_checks("pass")]}, tmp_path)
        assert result.returncode == 1
        assert "Unable to " in result.stderr
        assert "CI PASSED" not in result.stdout
        assert calls == {"required": 0, "all": 0, "runs": 0}


class TestPaginatedRules:
    def test_a_required_name_on_a_later_page_is_never_missed(self, tmp_path: Path) -> None:
        scenario = {
            "rules": [_paged_rules(["lint", "test"], ["build", "docs", SONAR])],
            "required": [five("pass", "pass", "pass")],
        }
        result, _calls = _run_wait(scenario, tmp_path)
        assert "CI PASSED" not in result.stdout
        assert _verdict(result).endswith(f"2 required not registered: {SONAR}, docs")

    def test_every_page_is_combined_and_deduplicated(self, tmp_path: Path) -> None:
        scenario = {
            "rules": [_paged_rules(["lint"], ["lint", "test"])],
            "required": [_checks("pass", "pass", names=["lint", "test"])],
        }
        result, _calls = _run_wait(scenario, tmp_path)
        assert "CI PASSED (required checks)" in _verdict(result)
        assert "missing" not in result.stdout


class TestClassicProtectionIsNotConsulted:
    """The classic endpoint needs the admin right (a push-only account gets a 404), so the block never calls it."""

    @pytest.mark.parametrize("path", [CANONICAL_PR, *RENDERED_PR_COPIES], ids=lambda p: p.parent.name)
    def test_no_copy_reads_branch_protection(self, path: Path) -> None:
        assert "protection/required_status_checks" not in _wait_block(path)

    def test_the_wait_completes_when_only_the_rulesets_endpoint_answers(self, tmp_path: Path) -> None:
        scenario = {"rules": [_rules("lint", "test")], "required": [_checks("pass", "pass", names=["lint", "test"])]}
        result, _calls = _run_wait(scenario, tmp_path)
        assert result.returncode == 0, result.stderr
        assert _verdict(result) == "CI PASSED (required checks): all 2 passed or were skipped"


class TestNonRequiredChecksAreIgnored:
    """With required names known, a check that does not gate the merge neither fails nor blocks the wait."""

    def test_a_failing_non_required_check_before_the_required_ones_register_is_not_a_failure(
        self, tmp_path: Path
    ) -> None:
        scenario = {
            "rules": [_rules("lint", "test")],
            "required": [NO_REQUIRED, _checks("pass", "pass", names=["lint", "test"])],
            "all": [_checks("fail", names=["CodeRabbit"])],
        }
        result, calls = _run_wait(scenario, tmp_path)
        assert "CI FAILED" not in result.stdout
        assert _verdict(result) == "CI PASSED (required checks): all 2 passed or were skipped"
        assert calls["runs"] == 0

    def test_only_non_required_checks_ever_registering_ends_as_pending_with_the_missing_names(
        self, tmp_path: Path
    ) -> None:
        scenario = {
            "rules": [_rules("lint", "test")],
            "required": [NO_REQUIRED],
            "all": [_checks("fail", names=["CodeRabbit"])],
        }
        result, calls = _run_wait(scenario, tmp_path)
        assert (
            _verdict(result)
            == "CI PENDING (required checks): 0 of 0 still pending; 2 required not registered: lint, test"
        )
        assert calls["runs"] == 0

    def test_a_failing_non_required_check_next_to_passing_required_ones_is_ignored(self, tmp_path: Path) -> None:
        # GitHub reports no required check, so the block falls back to every check and narrows it to the rules.
        rows = _checks("pass", "fail", names=["lint", "flaky-extra"])
        result, _calls = _run_wait({"rules": [_rules("lint")], "required": [NO_REQUIRED], "all": [rows]}, tmp_path)
        assert _verdict(result) == "CI PASSED (required checks): all 1 passed or were skipped"


class TestClassicAndRulesetProtection:
    """A check `--required` reports but the rulesets do not list (classic protection) still decides."""

    def test_a_failing_classic_required_check_is_a_failure(self, tmp_path: Path) -> None:
        rows = _checks("pass", "fail", names=["lint", "classic-required-test"])
        result, _calls = _run_wait({"rules": [_rules("lint")], "required": [rows]}, tmp_path)
        assert "CI PASSED" not in result.stdout
        assert _verdict(result) == "CI FAILED (required checks): 1 of 2 did not pass"

    def test_a_pending_classic_required_check_blocks_the_pass(self, tmp_path: Path) -> None:
        rows = _checks("pass", "pending", names=["lint", "classic-required-test"])
        result, _calls = _run_wait({"rules": [_rules("lint")], "required": [rows]}, tmp_path)
        assert _verdict(result) == "CI PENDING (required checks): 1 of 2 still pending"

    def test_passing_classic_and_ruleset_checks_pass(self, tmp_path: Path) -> None:
        rows = _checks("pass", "pass", names=["lint", "classic-required-test"])
        result, _calls = _run_wait({"rules": [_rules("lint")], "required": [rows]}, tmp_path)
        assert _verdict(result) == "CI PASSED (required checks): all 2 passed or were skipped"
