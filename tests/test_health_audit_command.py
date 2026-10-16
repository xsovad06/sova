"""Tests for the health-audit command's disclosure gate, board membership, and
claim verification (issue #1223).

The 2026-10-14 health-audit run (issues #1204-#1213) exposed five gaps: no
disclosure check before filing issues on a public repo, nothing added created
issues to the project board, Step 6 spot-checked only 5-10 findings instead of
re-deriving every one, Phase C's issue body template scored low under
`compute_quality_score()`, and existence/duplicate checks truncated at a
default `--limit`. A follow-up review (same issue) found the withheld
carry-over loop was never closed, the report's gitignore status was never
verified before writing exploit detail into it, the canonical issue template
was unreachable from any branch, the `SECURITY.md` check looked in the wrong
places and had no effect, the board-scope remedy didn't distinguish `project`
from `read:org` failures, and `gh project list` truncated at the default
limit. These tests assert on the raw command text (no LLM execution) that
each fix is present in both the canonical `commands/` copy and the rendered
`.claude/commands/` copy, following the content-assertion pattern in
`tests/test_ci_docs_only.py`. Assertions that could straddle a markdown line
wrap use the whitespace-normalized fixtures (`*_norm`) so a rewrap alone
cannot break the test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

HEALTH_SOVA = REPO_ROOT / ".claude" / "commands" / "health-audit.md"
HEALTH_DIST = REPO_ROOT / "commands" / "health-audit.md"

HEALTH_FIXTURES = ["health_sova_text", "health_dist_text"]
HEALTH_NORM_FIXTURES = ["health_sova_norm", "health_dist_norm"]


@pytest.fixture(scope="module")
def health_sova_text() -> str:
    return HEALTH_SOVA.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def health_dist_text() -> str:
    return HEALTH_DIST.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def health_sova_norm(health_sova_text: str) -> str:
    """Whitespace-collapsed variant: immune to line-wrap/indentation changes."""
    return " ".join(health_sova_text.split())


@pytest.fixture(scope="module")
def health_dist_norm(health_dist_text: str) -> str:
    return " ".join(health_dist_text.split())


class TestDisclosurePreflight:
    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_checks_repo_visibility(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "gh repo view --json visibility" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_checks_security_policy_at_all_conventional_locations(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """GitHub honours SECURITY.md at the repo root, .github/, and docs/."""
        text = request.getfixturevalue(fixture)
        assert "SECURITY.md" in text
        assert ".github/SECURITY.md" in text
        assert "docs/SECURITY.md" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_security_policy_has_an_actual_effect(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A found policy must change the question, not just be detected and ignored."""
        text = request.getfixturevalue(fixture)
        assert "quote its private reporting instructions" in text
        assert "private reporting channel" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_three_way_disclosure_choice(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "File as-is" in text
        assert "File neutrally" in text
        assert "Withhold" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_failed_check_defaults_to_public(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A failed visibility/policy check must fail toward asking, not toward silent filing."""
        text = request.getfixturevalue(fixture)
        assert "treat visibility as `PUBLIC`" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_preflight_precedes_issue_creation(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        preflight = text.index("### Disclosure pre-flight")
        create = text.index("### Create the issues")
        assert preflight < create

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_carry_over_filing_is_actually_scoped_into_create_issues(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """Regression guard for 'a carry-over group the user chooses to FILE is never actually
        filed': a rewrap or simplification of 'Create the issues' could silently drop the
        carry-over clause and restore that exact bug with no test noticing."""
        text = request.getfixturevalue(fixture)
        assert "plus every carry-over group the user chose to file" in text
        assert "must gain an issue above and drop out of `withheld=` in the same write" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_carry_over_groups_feed_the_preflight(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A withheld group carried in from Step 1 must reach this run's three-way question."""
        text = request.getfixturevalue(fixture)
        assert "carry-over group" in text
        assert "carry-over group name Step 1 surfaced from the prior run's `withheld=` field" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_private_or_internal_repo_still_resolves_carry_overs(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """A private/internal repo must not skip the pre-flight wholesale: a carry-over group
        never becomes resolvable later on its own, so it must be resolved here regardless of
        visibility, not left permanently stuck in `withheld=`."""
        text = request.getfixturevalue(fixture)
        assert "resolve every carry-over group regardless of visibility" in text
        assert "Only once every carry-over group is resolved" in text


class TestWithheldTracking:
    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_withheld_tracker_field_documented(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "withheld=" in text
        assert "withheld group" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_withheld_findings_still_counted(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A withheld group's findings must stay in every total the report states."""
        text = request.getfixturevalue(fixture)
        assert "still count in every total the report states" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_step1_carries_over_unresolved_withheld_groups(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Step 1 must re-surface a prior withheld group as a carry-over, not silently drop it."""
        text = request.getfixturevalue(fixture)
        assert "record them as **carry-over groups** for this run" in text
        assert "unresolved carry-over group is never silently dropped" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_withheld_field_is_a_union_not_a_fresh_overwrite(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """Phase A writes the file before Phase C's pre-flight decides anything, so `withheld=`
        must be written as a fail-safe placeholder in Phase A and only unioned with this run's
        decisions afterward in Phase C, never overwritten blind in between."""
        text = request.getfixturevalue(fixture)
        assert "write `withheld=` verbatim as the carry-over set" in text
        assert "rewrite `withheld=` as the union of" in text
        assert "cannot drop a carry-over name" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_action_plan_row_marks_withheld_status(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "`withheld` status" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_carry_over_content_captured_before_overwrite(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Regression guard: a carry-over group's findings must be captured from the existing
        report before Phase A overwrites it, not re-read from the (by then rewritten) file at
        the point Phase C needs them."""
        text = request.getfixturevalue(fixture)
        assert "Carry-over group content" in text
        assert "before anything later overwrites the existing" in text
        assert "by the time Phase C executes, Phase A has already overwritten it" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_withheld_only_branch_records_full_audit_mode(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """The 'all issues closed but withheld=' branch must record a mode like its siblings,
        or Step 4's 'use the mode recorded in Step 1' has nothing to read on exactly the run
        carrying a withheld finding forward."""
        text = request.getfixturevalue(fixture)
        assert "the withheld carry-over does not make this an incremental run" in text
        assert "record mode = `full`" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_carry_over_can_be_dropped_on_a_public_repo(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Regression guard: without an explicit Drop choice, a public-repo carry-over group
        can only ever be re-asked forever, since Withhold just re-adds it to `withheld=`."""
        text = request.getfixturevalue(fixture)
        assert "**Drop** (carry-over groups only" in text
        assert "it is not" in text and "re-surfaced on a future run" in text


class TestReportConfidentiality:
    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_report_path_verified_untracked_and_ignored_before_writing(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """A tracked or unignored report path would publish withheld exploit detail via a commit."""
        text = request.getfixturevalue(fixture)
        assert "git check-ignore -q docs/HEALTH-AUDIT.md" in text
        assert "git ls-files --error-unmatch docs/HEALTH-AUDIT.md" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_confidentiality_check_precedes_writing_the_report(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        check = text.index("git check-ignore -q docs/HEALTH-AUDIT.md")
        write = text.index("write the full audit to `docs/HEALTH-AUDIT.md`")
        assert check < write

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_confidentiality_check_is_not_gated_on_report_existing(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """Regression guard: the check must run on a fresh project with no report file yet,
        which is exactly the `untracked-and-not-ignored` case the check itself calls the
        default state of a fresh project."""
        text = request.getfixturevalue(fixture)
        assert "regardless of whether `docs/HEALTH-AUDIT.md` exists yet" in text
        check = text.index("git check-ignore -q docs/HEALTH-AUDIT.md")
        exists_check = text.index("check if `docs/HEALTH-AUDIT.md` already exists")
        assert check < exists_check

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_unsafe_choice_remediates_the_already_written_file(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """Regression guard: by the time Phase C's unsafe check runs, Phase A has already
        written the exploit detail to the unsafe path, so the fix must clean up that existing
        copy, not just prevent a future one."""
        text = request.getfixturevalue(fixture)
        assert "Phase A has already written the full report" in text
        assert "git rm --cached docs/HEALTH-AUDIT.md" in text


class TestPhaseDCarryOverInvariant:
    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_three_terminal_states_for_carry_over_groups(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Regression guard: Phase D's invariant must recognize filed, still-withheld, and
        explicitly-dropped as all-valid terminal states, not just the first two, or a
        legitimately dropped carry-over group reads as a Phase D failure."""
        text = request.getfixturevalue(fixture)
        assert "exactly one of three states" in text
        assert "explicitly dropped" in text


class TestBoardMembership:
    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_board_add_step_present(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "### Add issues to the project board" in text
        assert "task_source.github_project_number" in text
        assert "gh project item-add" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_repo_owner_is_resolved_not_assumed(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """<repo-owner> must be derived, not left for the agent to guess (it is not github_user)."""
        text = request.getfixturevalue(fixture)
        assert "gh repo view --json owner" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_missing_project_scope_reported_not_skipped(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "gh auth refresh -s project" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_missing_read_org_scope_has_its_own_graphql_remedy(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """`gh auth refresh -s project` does not fix a missing read:org scope."""
        text = request.getfixturevalue(fixture)
        assert "Missing `read:org` scope" in text
        assert "addProjectV2ItemById" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_board_discovery_fallback_uses_limit_1000(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "gh project list --owner <repo-owner> --limit 1000 --format json" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_phase_d_verifies_board_membership_per_issue(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "gh issue view <n> --json projectItems" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_phase_d_failure_not_read_as_absent(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A failed projectItems query must be reported as unverifiable, not as 'not on the board'."""
        text = request.getfixturevalue(fixture)
        assert "could not verify board membership" in text


class TestClaimsLedger:
    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_claims_ledger_replaces_spot_check(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "Verify with a claims ledger" in text
        assert "spot-check 5-10 key findings" not in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_every_finding_re_derived(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "For every finding, not a sample of them" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_ledger_has_a_budget_shape_not_one_call_per_claim(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """~60 findings re-derived one claim at a time would be 100-200 sequential calls."""
        text = request.getfixturevalue(fixture)
        assert "Batch per file" in text
        assert "Or delegate" in text
        assert "Budget rule" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_traced_in_code_marker(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "traced in code, not executed" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_unverifiable_claims_dropped(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "drop the claim from the report entirely" in text


class TestNoTruncatedLimits:
    """`TestNoTruncatedLimits` is about duplicate-check truncation specifically, not a ban on
    any bounded `--limit` anywhere in the file: a whole-file negative regex for `--limit 50`/
    `--limit 100` would also fail on a legitimately bounded limit for something unrelated (a
    `gh run list --limit 50` for CI history, say), making this class a tripwire on unrelated
    edits rather than coverage of the fix it is named for. Scope the negative check to the
    three `gh issue list`/`gh label list`/`gh project list` calls the fix actually touched.
    """

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_no_truncated_existence_or_duplicate_checks(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        for match in re.finditer(r"^.*\b(?:gh issue list|gh label list|gh project list)\b.*$", text, re.MULTILINE):
            line = match.group(0)
            assert not re.search(r"--limit 50\b", line), line
            assert not re.search(r"--limit 100\b", line), line

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_duplicate_check_uses_limit_1000(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "gh issue list --state open --limit 1000" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_label_list_uses_limit_1000(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "gh label list --limit 1000" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_milestones_paginate(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "/milestones --paginate" in text


class TestIssueBodyTemplate:
    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_scorer_matching_structure_is_primary(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """The canonical template must have the headings compute_quality_score() scores."""
        text = request.getfixturevalue(fixture)
        assert "## Detailed Description" in text
        assert "## Acceptance Criteria" in text
        assert "## Out of Scope / Constraints" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_project_template_checked_first(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert ".github/ISSUE_TEMPLATE/*.md" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_canonical_structure_is_the_step_three_default(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Step 3 of the resolution order must bind directly to the canonical structure.

        A prior revision offered a separate, low-scoring 'fallback template' as
        step 3 and left the scorer-matching structure attached to no branch at
        all, so it was unreachable for exactly the portable project it exists
        for. There must be no second, lower-scoring template left to fall into.
        """
        text = request.getfixturevalue(fixture)
        assert "3. **The canonical structure below**" in text
        assert "fallback template" not in text.lower()
        assert "## Files to Modify" not in text
        assert "## Estimated Effort" not in text


# Whether `.claude/commands/health-audit.md` is a byte-identical render of
# `commands/health-audit.md` is already enforced, for all 28 canonical
# commands including this one, by
# `tests/test_command_render_drift.py::TestNoDrift.test_rendered_command_matches_canonical`,
# which renders through the same `render_self()` this repo's `make
# commands-render` uses. A second, independently-defined "rendered" check
# here would risk disagreeing with that one about what rendering means.
