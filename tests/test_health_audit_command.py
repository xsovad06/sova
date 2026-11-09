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
limit. Issue #1363 cut the command to a size cap: the report path check, the visibility and
policy lookup and the board add moved into deterministic `sova audit` and `sova board`
subcommands (tested in `test_audit_cli.py` and `test_board_cli.py`), and the command
now carries one instruction per verdict they print. These tests assert on the raw
command text (no LLM execution) that each gate is still present in both the canonical
`commands/` copy and the rendered `.claude/commands/` copy, following the content-assertion pattern in
`tests/test_ci_docs_only.py`. Assertions that could straddle a markdown line
wrap use the whitespace-normalized fixtures (`*_norm`) so a rewrap alone
cannot break the test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sova.commands.catalog import get_canonical_dir

REPO_ROOT = Path(__file__).parent.parent

HEALTH_SOVA = REPO_ROOT / ".claude" / "commands" / "health-audit.md"
HEALTH_DIST = get_canonical_dir() / "health-audit.md"

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
        assert "sova audit disclosure" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_every_disclosure_verdict_has_an_instruction(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """The verdict command prints three tokens; each needs its own branch in the command."""
        text = request.getfixturevalue(fixture)
        assert "- **`private`**" in text
        assert "- **`public-policy <path>`**" in text
        assert "- **`public-no-policy`**" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_security_policy_has_an_actual_effect(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A found policy must change the question, not just be detected and ignored."""
        text = request.getfixturevalue(fixture)
        assert "quote its private reporting instructions" in text
        assert "so withholding is not the only path to disclosure" in text

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
        assert (
            "A failed or unrecognised visibility check is reported as public, so the gate fails toward asking" in text
        )

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_failed_disclosure_call_counts_as_public(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A missing or older `sova` must not leave the pre-flight with no verdict, which would skip it."""
        text = request.getfixturevalue(fixture)
        assert "A failed call or any other last line counts as `public-no-policy`" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_only_the_last_line_is_the_disclosure_verdict(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A log line above the verdict must not be read as "any other output" and drop a found policy."""
        text = request.getfixturevalue(fixture)
        assert "The verdict is the last line printed:" in text

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
        assert "must gain an issue or fold and drop out of `withheld=` or `deferred=` in the same write" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_carry_over_groups_feed_the_preflight(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A withheld group carried in from Step 1 must reach this run's three-way question."""
        text = request.getfixturevalue(fixture)
        assert "carry-over group" in text
        assert "plus every withheld carry-over group still unresolved" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_private_or_internal_repo_still_resolves_carry_overs(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """A private/internal repo must not skip the pre-flight wholesale: a carry-over group
        never becomes resolvable later on its own, so it must be resolved here regardless of
        visibility, not left permanently stuck in `withheld=`."""
        text = request.getfixturevalue(fixture)
        assert "still resolve every carry-over group regardless of visibility" in text
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
        assert "record it as a **carry-over group**" in text
        assert "unresolved carry-over group is never silently dropped" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_withheld_field_is_a_union_not_a_fresh_overwrite(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """Phase A writes the file before Phase C's pre-flight decides anything, so `withheld=`
        must be written as a fail-safe placeholder in Phase A and only unioned with this run's
        decisions afterward in Phase C, never overwritten blind in between."""
        text = request.getfixturevalue(fixture)
        assert "write `withheld=`, `deferred=` and the open-target `folded=` entries" in text
        assert "rewrite `withheld=` as the union of" in text
        assert "cannot drop a carry-over name" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_action_plan_row_marks_withheld_status(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "`withheld` status" in text
        assert "`deferred` status" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_carry_over_content_captured_before_overwrite(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Regression guard: a carry-over group's findings must be captured from the existing
        report before Phase A overwrites it, not re-read from the (by then rewritten) file at
        the point Phase C needs them."""
        text = request.getfixturevalue(fixture)
        assert "Before anything overwrites the report, capture each carry-over group's verbatim finding blocks" in text
        assert "by the time Phase C executes, Phase A has already overwritten the file" in text

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
        assert "It is not re-surfaced on a future run" in text


class TestReportConfidentiality:
    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_report_path_verified_untracked_and_ignored_before_writing(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """A tracked or unignored report path would publish withheld exploit detail via a commit."""
        text = request.getfixturevalue(fixture)
        assert "sova audit report-path" in text
        for verdict in ("safe", "unsafe-untracked", "unsafe-tracked", "unsafe-not-a-git-repo"):
            assert f"- `{verdict}`:" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_confidentiality_check_precedes_writing_the_report(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        check = text.index("sova audit report-path")
        write = text.index("Write the full audit to `docs/HEALTH-AUDIT.md`")
        assert check < write

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_confidentiality_check_is_not_gated_on_report_existing(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """Regression guard: the check must run on a fresh project with no report file yet,
        which is exactly the `untracked-and-not-ignored` case the check itself calls the
        default state of a fresh project."""
        text = request.getfixturevalue(fixture)
        assert "whether or not `docs/HEALTH-AUDIT.md` exists yet" in text
        check = text.index("sova audit report-path")
        exists_check = text.index("check whether `docs/HEALTH-AUDIT.md` already exists")
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

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_step_one_does_not_untrack_the_report(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Step 1 runs on every audit, so an unconditional `git rm --cached` there would stage a deletion in a
        project that deliberately tracks its report. Only Phase C's Withhold or File neutrally choice untracks."""
        text = request.getfixturevalue(fixture)
        assert (
            "Do not untrack it now: only a Withhold or File neutrally choice makes Phase C run `git rm --cached`"
            in text
        )
        step_one = text[: text.index("### Step 2")]
        assert "must also leave version control" not in step_one

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_failed_report_path_call_counts_as_unsafe(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A missing or older `sova` must not leave the gate with no verdict, which would read as safe."""
        text = request.getfixturevalue(fixture)
        assert "a failed call or any other last line counts as `unsafe-untracked`" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_report_path_verdict_is_the_last_line_printed(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """Stderr notices share the Bash tool's output with the verdict, as the disclosure gate already allows for."""
        text = request.getfixturevalue(fixture)
        assert "The verdict is the last line printed; a failed call" in text


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
        assert "sova board add" in text
        assert "**`skip: ...`**: skip board membership and say why" in " ".join(text.split())

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_board_mechanics_live_in_the_subcommand_not_the_command(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """The gh calls, GraphQL fallback and board discovery are deterministic code now."""
        text = request.getfixturevalue(fixture)
        assert "gh project item-add" not in text
        assert "addProjectV2ItemById" not in text
        assert "gh project list" not in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_missing_project_scope_reported_not_skipped(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "gh auth refresh -s project" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_missing_project_scope_is_retried_after_the_refresh(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        assert "If the line says to run `gh auth refresh -s project`, run it, then re-run `sova board add`" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_missing_read_org_scope_names_the_remedy(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "**`failed` with a missing read:org scope**: report it, ask for a token with `read:org`," in text
        assert "and skip the board for this run" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_configuration_error_without_url_lines_skips_the_board(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        assert "Any other non-zero exit with no per-URL line: report it and skip the board" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_urlless_failed_board_line_follows_the_remedy_it_names(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """A board listing blocked by project scope needs the refresh remedy, not a read:org request or a skip."""
        text = request.getfixturevalue(fixture)
        assert "**A `failed:` line with no URL** is board-level: follow the bullet above its text matches" in text
        assert "This includes a `failed:` board line" not in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_discovered_board_is_named_and_used_by_phase_d(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "A `board:` line names the board used (with its title when known)" in text
        assert "Skip this check when `sova board add` printed `skip`" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_phase_d_takes_the_title_from_the_board_line_before_calling_gh_project_view(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """`gh project view` needs read:org, so the title printed by `sova board add` comes first."""
        text = request.getfixturevalue(fixture)
        assert "Take the title from the `board:` line, else run `gh project view" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_other_board_failures_are_reported_not_retried(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert 'report it as "could not add to board"' in text
        assert "Do not retry automatically" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_gh_token_set_changes_the_scope_remedy(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "If it names a token variable such as `GH_TOKEN`, `gh auth refresh` refuses to run" in text

    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_phase_d_verifies_board_membership_per_issue(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "gh issue view <n> --json projectItems" in text
        assert "sova config get task_source.github_project_number" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_phase_d_failure_not_read_as_absent(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A failed projectItems query must be reported as unverifiable, not as 'not on the board'."""
        text = request.getfixturevalue(fixture)
        assert "could not verify board membership" in text


class TestIncrementalMode:
    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_step_four_uses_the_recorded_mode_not_the_existing_report(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        assert "### Step 4: Incremental mode check" in text
        assert "Use the mode recorded in Step 1; an existing report alone does not select incremental mode" in text


class TestFoldBeforeFiling:
    """A group whose files an open issue already covers is commented there, not filed again."""

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_preflight_searches_open_issues_per_file(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert """gh issue list --state open --limit 1000 --search '"<path>" in:title,body'""" in text
        assert "Find fold targets, posting nothing yet." in text
        assert "(else its module or concern)" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_inconclusive_search_defers_the_group(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "A failed or 1000-result search defers the group." in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_staleness_check_reads_each_tracked_issue(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "gh issue view <n> --json state --jq .state" in text
        assert "`could not verify #N`, counted as neither open nor closed" in text
        assert "gh issue list --state open --limit 1000 --json number --jq '.[].number'" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_fold_targets_are_tracked_apart_from_created_issues(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """A fold target is someone else's issue: it must not decide staleness or join the board."""
        text = request.getfixturevalue(fixture)
        assert "`issues=` lists the issue numbers created in Phase C, never a fold target." in text
        assert "`folded=` lists `<group-slug>:<issue number>`" in text
        assert "Compare against the tracked `issues=` numbers (created issues only)" in text
        assert "a closed fold target proves nothing about the folded findings" in text
        assert "Only issues created in this run: a fold target is someone else's issue, never added." in text
        assert "Each issue created this run (never a fold target) is on the **resolved** board" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_fold_comment_is_posted_only_after_confirmation(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        confirmation = text.index("Nothing is published before it.")
        assert confirmation < text.index("gh issue comment <N> --body")
        assert "Only now, after the confirmation, post each folded group's findings" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_new_issues_are_capped_at_five(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "File at most 5 new issues per run" in text
        assert "Maximum 10 task groups and 5 new issues per run" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_folding_follows_the_disclosure_choice(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A fold publishes findings on a public issue, so withheld and neutral groups must not leak detail."""
        text = request.getfixturevalue(fixture)
        assert "**folded** (never a withheld group)" in text
        assert '"file neutrally" group without the exploit path' in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_cap_defers_into_its_own_field(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A group past the cap goes to `deferred=`, not `withheld=`, which means only a disclosure hold."""
        text = request.getfixturevalue(fixture)
        assert "Defer any further group: keep it in `deferred=`" in text
        assert "`deferred=` as every deferred group and any deferred carry-over still unresolved" in text
        assert "A deferred carry-over group that is not security-sensitive is asked only: file now" in text
        assert "never triggers the report-path check" in text
        assert "2 withheld, 2 deferred" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_cap_never_strands_a_carry_over_group(self, fixture: str, request: pytest.FixtureRequest) -> None:
        """A carry-over group the user chose to file must gain an issue, so the cap cannot defer it.

        Deferring it would leave it unfiled, a state outside Phase D's three carry-over end states.
        """
        text = request.getfixturevalue(fixture)
        assert "carry-over groups included" not in text
        assert (
            "A carry-over group the user chose to file is exempt from the cap and from deferral: "
            "it is always folded or filed (an inconclusive search files it)"
        ) in text
        assert (
            "A carry-over group the user chose to file (never deferred; a fold target counts) "
            "must gain an issue or fold and drop out of `withheld=` or `deferred=` in the same write"
        ) in text
        assert "filed (has an issue or fold target, in neither field)" in text
        assert "5 new issues per run (filed carry-over groups exempt)" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_folded_and_unfiled_groups_are_not_created(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert '"withhold" or "drop" and that was neither folded nor deferred, create an issue' in text


class TestLabelValidation:
    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_only_existing_labels_are_used(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "Only use labels and milestones that exist in the project" in text
        assert "If a label is missing, create it with `gh label create` or use the closest existing one" in text
        assert "Confirm it appears in the list before passing it to `--milestone`" in text


class TestClaimsLedger:
    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_claims_ledger_replaces_spot_check(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "Verify with a claims ledger" in text
        assert "spot-check 5-10 key findings" not in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_every_finding_re_derived(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "for every finding, not a sample of them" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_ledger_has_a_budget_shape_not_one_call_per_claim(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """~60 findings re-derived one claim at a time would be 100-200 sequential calls."""
        text = request.getfixturevalue(fixture)
        assert "Batch per file" in text
        assert "Or delegate" in text
        assert "Budget rule" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_traced_in_code_marker(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "traced in code, not executed" in text

    @pytest.mark.parametrize("fixture", HEALTH_NORM_FIXTURES)
    def test_unverifiable_claims_dropped(self, fixture: str, request: pytest.FixtureRequest) -> None:
        text = request.getfixturevalue(fixture)
        assert "drop the claim entirely" in text


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


class TestRationaleLivesOutsideTheCommand:
    @pytest.mark.parametrize("fixture", HEALTH_FIXTURES)
    def test_command_carries_no_pointer_to_the_maintainer_doc(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """Text the agent does not act on leaves the distributed surface entirely, pointer included."""
        text = request.getfixturevalue(fixture)
        assert "docs/commands/health-audit.md" not in text

    def test_maintainer_doc_exists(self) -> None:
        assert (REPO_ROOT / "docs" / "commands" / "health-audit.md").is_file()


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
