"""Behavior of `/integrate-pr` Phase 4 (fast path and CI poll), run for real against a fake `gh`.

Issue #1321: the poll decided from whatever `gh pr checks` listed, so a partial all-green list (a required
check such as SonarCloud had not registered yet) read as `CI PASSED`, and the fast path skipped the poll
on the same list. Both blocks now read the base branch's required check names from the rulesets and
count a missing name as not passed. With required names the pass test covers those names only (a pending
non-required check such as CodeRabbit no longer blocks); with none it covers every check as before.
These tests extract the shipped bash blocks and execute them, sharing the fake `gh` with `/pr`'s tests.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sova.commands.catalog import get_canonical_dir
from tests.ci_wait_fakes import (
    FIVE,
    GH_ERROR,
    NO_CHECKS,
    NO_REQUIRED,
    NO_RULESETS_PLAN,
    SONAR,
    checks,
    five,
    install_fakes,
    paged_rules,
    read_calls,
    rules,
)

REPO_ROOT = Path(__file__).parent.parent
CANONICAL = get_canonical_dir() / "integrate-pr.md"
RENDERED_COPIES = [
    REPO_ROOT / ".claude" / "commands" / "integrate-pr.md",
    REPO_ROOT / ".agents" / "skills" / "sova-integrate-pr" / "SKILL.md",
]
PR_NUMBER = "42"
SKIPPING = "Skipping the poll"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="the CI wait blocks parse gh output with jq")


def _phase4_blocks(path: Path) -> tuple[str, str]:
    """Return Phase 4's fast path block and poll block, with the PR number filled in."""
    text = path.read_text(encoding="utf-8")
    start = text.index("### Phase 4: Wait for CI")
    end = text.index("### Phase 5", start)
    blocks = re.findall(r"```bash\n(.*?)\n```", text[start:end], re.S)
    fast = next(b for b in blocks if "integrate-pushed" in b)
    poll = next(b for b in blocks if "seq 1 16" in b)
    return fast.replace("<PR_NUMBER>", PR_NUMBER), poll.replace("<PR_NUMBER>", PR_NUMBER)


def _run(
    block: str, scenario: dict[str, object], tmp_path: Path
) -> tuple[subprocess.CompletedProcess[str], dict[str, int]]:
    # Unless a scenario says what `gh pr checks --required` reports, GitHub reports no required check.
    env = install_fakes(tmp_path, {"required": [NO_REQUIRED], **scenario})
    result = subprocess.run(["bash", "-c", block], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    return result, read_calls(tmp_path, ("all", "rules"))


def _verdict(result: subprocess.CompletedProcess[str]) -> str:
    lines = result.stdout.strip().splitlines()
    assert lines, f"no output; stderr: {result.stderr}"
    return lines[-1]


class TestRenderedCopiesRunTheSamePhase:
    @pytest.mark.parametrize("path", RENDERED_COPIES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
    def test_copy_ships_the_canonical_blocks(self, path: Path) -> None:
        assert _phase4_blocks(path) == _phase4_blocks(CANONICAL)


class TestPoll:
    def test_partial_green_list_is_never_a_pass_and_names_the_missing(self, tmp_path: Path) -> None:
        scenario = {"rules": [rules(*FIVE)], "all": [five("pass", "pass", "pass")]}
        result, calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert "CI PASSED" not in result.stdout
        assert _verdict(result) == (
            f"CI TIMEOUT: checks still pending after 8 minutes; 2 required not registered: {SONAR}, docs"
        )
        assert calls == {"all": 16, "rules": 1}

    def test_missing_checks_that_register_later_and_pass(self, tmp_path: Path) -> None:
        scenario = {"rules": [rules(*FIVE)], "all": [five("pass", "pass", "pass"), five(*["pass"] * 5)]}
        result, calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert _verdict(result) == "CI PASSED: all 5 required checks passed or were skipped (5 checks total)"
        assert calls["all"] == 2

    def test_missing_check_that_registers_as_skipping_counts_as_satisfied(self, tmp_path: Path) -> None:
        scenario = {"rules": [rules(*FIVE)], "all": [five(*["pass"] * 4), five(*["pass"] * 4, "skipping")]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert _verdict(result) == "CI PASSED: all 5 required checks passed or were skipped (5 checks total)"

    def test_a_failure_wins_immediately_while_others_are_missing(self, tmp_path: Path) -> None:
        scenario = {"rules": [rules(*FIVE)], "all": [five("pass", "fail", "pass")]}
        result, calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert _verdict(result) == "CI FAILED: 1 check(s) failed"
        assert calls["all"] == 1

    def test_a_pending_non_required_check_does_not_block_the_pass(self, tmp_path: Path) -> None:
        rows = checks("pass", "pass", "pending", names=["lint", "test", "CodeRabbit"])
        result, _calls = _run(_phase4_blocks(CANONICAL)[1], {"rules": [rules("lint", "test")], "all": [rows]}, tmp_path)
        assert _verdict(result) == "CI PASSED: all 2 required checks passed or were skipped (3 checks total)"

    def test_a_failing_non_required_check_is_ignored(self, tmp_path: Path) -> None:
        rows = checks("pass", "fail", names=["lint", "flaky-extra"])
        result, _calls = _run(_phase4_blocks(CANONICAL)[1], {"rules": [rules("lint")], "all": [rows]}, tmp_path)
        assert _verdict(result) == "CI PASSED: all 1 required checks passed or were skipped (2 checks total)"

    def test_names_with_spaces_are_matched_whole(self, tmp_path: Path) -> None:
        rows = checks("pass", "pass", names=["SonarCloud", "unit tests"])
        scenario = {"rules": [rules(SONAR, "unit tests")], "all": [rows]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert "CI PASSED" not in result.stdout
        assert _verdict(result).endswith(f"1 required not registered: {SONAR}")

    def test_no_required_checks_polls_every_check_as_before(self, tmp_path: Path) -> None:
        scenario = {"all": [checks("pass", "pending"), checks("pass", "pass")]}
        result, calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert _verdict(result) == "CI PASSED: all 2 checks passed"
        assert calls["all"] == 2

    def test_zero_checks_keeps_the_grace_period_and_no_checks_exit(self, tmp_path: Path) -> None:
        empty = {"stdout": "[]\n", "stderr": "", "rc": 0}
        result, calls = _run(_phase4_blocks(CANONICAL)[1], {"all": [empty]}, tmp_path)
        assert _verdict(result).startswith("NO_CHECKS: ")
        assert calls["all"] == 5

    def test_zero_checks_with_required_names_is_never_no_checks(self, tmp_path: Path) -> None:
        """NO_CHECKS proceeds to the merge, so it must not fire while required names are still missing."""
        empty = {"stdout": "[]\n", "stderr": "", "rc": 0}
        result, calls = _run(_phase4_blocks(CANONICAL)[1], {"rules": [rules(*FIVE)], "all": [empty]}, tmp_path)
        assert "NO_CHECKS" not in result.stdout
        missing = f"5 required not registered: {SONAR}, build, docs, lint, test"
        assert _verdict(result) == f"CI TIMEOUT: checks still pending after 8 minutes; {missing}"
        assert calls["all"] == 16

    def test_a_plan_without_rulesets_counts_as_no_required_names(self, tmp_path: Path) -> None:
        scenario = {"rules": [NO_RULESETS_PLAN], "all": [checks("pass")]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert _verdict(result) == "CI PASSED: all 1 checks passed"

    def test_empty_output_from_gh_is_treated_as_no_checks(self, tmp_path: Path) -> None:
        result, _calls = _run(_phase4_blocks(CANONICAL)[1], {"rules": [rules("lint")], "all": [NO_CHECKS]}, tmp_path)
        assert "CI PASSED" not in result.stdout

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
        result, calls = _run(_phase4_blocks(CANONICAL)[1], {"rules": [response], "all": [checks("pass")]}, tmp_path)
        assert result.returncode == 1
        assert "Unable to " in result.stderr
        assert "CI PASSED" not in result.stdout
        assert calls["all"] == 0


class TestFastPath:
    def test_all_required_registered_and_green_skips_the_poll(self, tmp_path: Path) -> None:
        scenario = {"rules": [rules(*FIVE)], "all": [five(*["pass"] * 5)]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[0], scenario, tmp_path)
        assert SKIPPING in result.stdout

    def test_partial_green_list_does_not_skip_the_poll(self, tmp_path: Path) -> None:
        scenario = {"rules": [rules(*FIVE)], "all": [five("pass", "pass", "pass")]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[0], scenario, tmp_path)
        assert SKIPPING not in result.stdout

    def test_pending_non_required_check_does_not_prevent_the_skip(self, tmp_path: Path) -> None:
        rows = checks("pass", "pending", names=["lint", "CodeRabbit"])
        result, _calls = _run(_phase4_blocks(CANONICAL)[0], {"rules": [rules("lint")], "all": [rows]}, tmp_path)
        assert SKIPPING in result.stdout

    def test_no_required_checks_skips_on_a_fully_green_list(self, tmp_path: Path) -> None:
        result, _calls = _run(_phase4_blocks(CANONICAL)[0], {"all": [checks("pass", "skipping")]}, tmp_path)
        assert SKIPPING in result.stdout

    def test_no_required_checks_does_not_skip_on_a_pending_list(self, tmp_path: Path) -> None:
        result, _calls = _run(_phase4_blocks(CANONICAL)[0], {"all": [checks("pass", "pending")]}, tmp_path)
        assert SKIPPING not in result.stdout

    def test_failed_lookup_leaves_the_fast_path_unmet(self, tmp_path: Path) -> None:
        scenario = {"rules": [GH_ERROR], "all": [checks("pass")]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[0], scenario, tmp_path)
        assert SKIPPING not in result.stdout

    def test_a_plan_without_rulesets_still_runs_the_check_lookup(self, tmp_path: Path) -> None:
        scenario = {"rules": [NO_RULESETS_PLAN], "all": [checks("pass", "skipping")]}
        result, calls = _run(_phase4_blocks(CANONICAL)[0], scenario, tmp_path)
        assert SKIPPING in result.stdout
        assert calls["all"] == 1

    def test_a_plan_without_rulesets_does_not_skip_on_a_pending_list(self, tmp_path: Path) -> None:
        scenario = {"rules": [NO_RULESETS_PLAN], "all": [checks("pass", "pending")]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[0], scenario, tmp_path)
        assert SKIPPING not in result.stdout

    def test_a_required_name_on_a_later_rules_page_does_not_skip_the_poll(self, tmp_path: Path) -> None:
        scenario = {
            "rules": [paged_rules(["lint", "test"], ["build", "docs", SONAR])],
            "all": [five("pass", "pass", "pass")],
        }
        result, _calls = _run(_phase4_blocks(CANONICAL)[0], scenario, tmp_path)
        assert SKIPPING not in result.stdout


class TestClassicAndRulesetProtection:
    """A check that `gh pr checks --required` reports but the rulesets do not list (classic protection) decides too."""

    def test_a_failing_classic_required_check_fails_the_poll(self, tmp_path: Path) -> None:
        required = checks("pass", "fail", names=["lint", "classic-required-test"])
        scenario = {"rules": [rules("lint")], "required": [required], "all": [required]}
        result, calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert "CI PASSED" not in result.stdout
        assert _verdict(result) == "CI FAILED: 1 check(s) failed"
        assert calls["all"] == 1

    def test_a_pending_classic_required_check_blocks_the_pass(self, tmp_path: Path) -> None:
        required = checks("pass", "pending", names=["lint", "classic-required-test"])
        scenario = {"rules": [rules("lint")], "required": [required], "all": [required]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert _verdict(result).startswith("CI TIMEOUT")

    def test_a_failing_classic_required_check_does_not_skip_the_poll(self, tmp_path: Path) -> None:
        required = checks("pass", "fail", names=["lint", "classic-required-test"])
        scenario = {"rules": [rules("lint")], "required": [required], "all": [required]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[0], scenario, tmp_path)
        assert SKIPPING not in result.stdout

    def test_passing_classic_and_ruleset_checks_pass(self, tmp_path: Path) -> None:
        rows = checks("pass", "pass", "pending", names=["lint", "classic-required-test", "CodeRabbit"])
        required = checks("pass", "pass", names=["lint", "classic-required-test"])
        scenario = {"rules": [rules("lint")], "required": [required], "all": [rows]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert _verdict(result) == "CI PASSED: all 2 required checks passed or were skipped (3 checks total)"

    def test_an_unreadable_required_lookup_stops_without_a_pass(self, tmp_path: Path) -> None:
        scenario = {"rules": [rules("lint")], "required": [GH_ERROR], "all": [checks("pass", names=["lint"])]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert result.returncode == 1
        assert "Unable to read the required checks of PR" in result.stderr
        assert "HTTP 502" in result.stderr
        assert "CI PASSED" not in result.stdout

    def test_an_unreadable_required_lookup_leaves_the_fast_path_unmet(self, tmp_path: Path) -> None:
        scenario = {"rules": [rules("lint")], "required": [GH_ERROR], "all": [checks("pass", names=["lint"])]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[0], scenario, tmp_path)
        assert SKIPPING not in result.stdout

    def test_a_failed_rules_lookup_reports_the_gh_error(self, tmp_path: Path) -> None:
        result, _calls = _run(_phase4_blocks(CANONICAL)[1], {"rules": [GH_ERROR], "all": [checks("pass")]}, tmp_path)
        assert "Unable to read the required checks of main: HTTP 502" in result.stderr


class TestPollPagination:
    def test_a_required_name_on_a_later_rules_page_is_named_as_missing(self, tmp_path: Path) -> None:
        scenario = {
            "rules": [paged_rules(["lint", "test"], ["build", "docs", SONAR])],
            "all": [five("pass", "pass", "pass")],
        }
        result, _calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert "CI PASSED" not in result.stdout
        assert _verdict(result).endswith(f"2 required not registered: {SONAR}, docs")

    @pytest.mark.parametrize("index", [0, 1], ids=["fast-path", "poll"])
    def test_neither_block_reads_branch_protection(self, index: int) -> None:
        # The classic endpoint needs the admin right (a push-only account gets a 404), so it is never called.
        assert "protection/required_status_checks" not in _phase4_blocks(CANONICAL)[index]

    def test_the_poll_completes_when_only_the_rulesets_endpoint_answers(self, tmp_path: Path) -> None:
        scenario = {"rules": [rules("lint")], "all": [checks("pass", names=["lint"])]}
        result, _calls = _run(_phase4_blocks(CANONICAL)[1], scenario, tmp_path)
        assert result.returncode == 0, result.stderr
        assert "CI PASSED" in _verdict(result)
