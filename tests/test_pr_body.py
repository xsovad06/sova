"""Tests for the shared PR-body helpers: acceptance-criteria extraction,
verdict parsing, owner-only overrides, and section-based body assembly.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import structlog

from sova.config.models import CommitConfig, ProjectConfig
from sova.core.context import ExecutionContext
from sova.core.steps._pr_body import (
    TITLE_MARKER,
    VERDICT_MARKER,
    apply_ci_gated_overrides,
    apply_owner_overrides,
    apply_sections,
    compose_body,
    extract_acceptance_criteria,
    generate_pr_body_text,
    parse_verdicts,
    process_llm_result,
    render_closing_block,
    split_sections,
    split_title_tail,
    split_verdict_tail,
    truncate_diff,
)
from sova.core.steps._pr_body import build_prompt as build_pr_body_prompt
from sova.llm.errors import LLMInvocationError, LLMTimeoutError
from sova.llm.models import LLMResult
from sova.utils.gh import IssueLinks, parse_issue_links


def _commit(*, owner: list[str] | None = None, ci_gated: list[str] | None = None) -> CommitConfig:
    """A CommitConfig with only the given override patterns (none by default)."""
    return CommitConfig(owner_only_patterns=owner or [], ci_gated_patterns=ci_gated or [])


def _ctx(tmp_path: Path, issue_number: str = "42") -> ExecutionContext:
    return ExecutionContext(
        project_dir=tmp_path,
        config=ProjectConfig(),
        adapter=None,
        issue_number=issue_number,
        role="developer",
    )


class TestExtractAcceptanceCriteria:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param("## Objective\nDo the thing.\n", [], id="no-heading-returns-empty"),
            pytest.param("", [], id="empty-body-returns-empty"),
            pytest.param(
                "## Acceptance Criteria\n\n## Out of Scope\nNothing.\n", [], id="heading-with-no-items-returns-empty"
            ),
            pytest.param(
                "## Acceptance Criteria\n- [ ] First thing works\n- [ ] Second thing works\n",
                ["First thing works", "Second thing works"],
                id="extracts-checkbox-items",
            ),
            pytest.param(
                "## Acceptance Criteria\n- [x] Done already\n- Bare dash item\n",
                ["Done already", "Bare dash item"],
                id="tolerant-of-checked-boxes-and-bare-dashes",
            ),
            pytest.param(
                "## Acceptance Criteria\n- [ ] Only this one\n\n## Out of Scope\n- [ ] Not a criterion\n",
                ["Only this one"],
                id="stops-at-next-heading",
            ),
            pytest.param(
                "## Acceptance Criteria\n- [ ] Only this one\n",
                ["Only this one"],
                id="stops-at-end-of-body-when-last-section",
            ),
            pytest.param(
                "## Acceptance criteria\n- [ ] Lowercase c\n", ["Lowercase c"], id="heading-is-case-insensitive"
            ),
            pytest.param(
                "##  Acceptance Criteria\n- [ ] Extra space before heading text\n",
                ["Extra space before heading text"],
                id="heading-tolerates-extra-whitespace",
            ),
            # A "## " line and a "- " line inside a fenced example must not be mistaken for the section boundary
            # or for a criterion: either would truncate the real section early or invent a bogus criterion.
            pytest.param(
                (
                    "## Acceptance Criteria\n"
                    "- Do X\n"
                    "The config reads like this:\n"
                    "```toml\n"
                    "- not_a_criterion = 1\n"
                    "## fake heading inside fence\n"
                    "```\n"
                    "- Do Z\n"
                    "\n"
                    "## Out of Scope\n"
                    "- Do Y (not in scope)\n"
                ),
                ["Do X", "Do Z"],
                id="fenced-block-does-not-truncate-section-or-leak-lines",
            ),
            # A "---" horizontal rule has no whitespace after its marker, so it must not be parsed as a
            # criterion with text "--".
            pytest.param(
                "## Acceptance Criteria\n- [ ] real one\n\n---\n", ["real one"], id="horizontal-rule-is-not-a-criterion"
            ),
            pytest.param(
                "## Acceptance Criteria\n* [ ] Star bullet\n+ [ ] Plus bullet\n",
                ["Star bullet", "Plus bullet"],
                id="accepts-asterisk-and-plus-bullets",
            ),
            # A nested sub-bullet or continuation line must never be promoted to a top-level criterion, even
            # when it uses checkbox syntax itself.
            pytest.param(
                "## Acceptance Criteria\n- [ ] Top level thing\n  - [ ] sub detail one\n  - sub detail two\n",
                ["Top level thing"],
                id="indented-sub-bullet-is-not-promoted-to-a-criterion",
            ),
        ],
    )
    def test_extract_acceptance_criteria(self, body: str, expected: list[str]) -> None:
        assert extract_acceptance_criteria(body) == expected


class TestParseVerdicts:
    @pytest.mark.parametrize(
        ("tail", "criteria", "expected"),
        [
            pytest.param(
                "- [met] First\n- [unmet] Second\n",
                ["First", "Second"],
                [("First", "met"), ("Second", "unmet")],
                id="parses-matching-count",
            ),
            pytest.param("- [met] Only one\n", ["First", "Second"], None, id="count-mismatch-is-unparseable"),
            pytest.param("- [maybe] First\n", ["First"], None, id="invalid-verdict-token-is-unparseable"),
            pytest.param("First is met\n", ["First"], None, id="malformed-line-is-unparseable"),
            pytest.param("", [], [], id="empty-criteria-and-empty-tail-parses-to-empty-list"),
            # A cosmetic LLM deviation (a trailing summary sentence after the verdict list) must not degrade
            # correctly-reported "met" verdicts to "all unmet": only lines shaped like a verdict are counted.
            pytest.param(
                "- [met] First\n- [met] Second\n\nAll criteria satisfied.\n",
                ["First", "Second"],
                [("First", "met"), ("Second", "met")],
                id="tolerates-trailing-prose-after-well-formed-verdicts",
            ),
        ],
    )
    def test_parse_verdicts(self, tail: str, criteria: list[str], expected: list[tuple[str, str]] | None) -> None:
        assert parse_verdicts(tail, criteria) == expected


class TestApplyOwnerOverrides:
    def test_default_patterns_match_plain_prose_criteria(self) -> None:
        """Acceptance criteria are usually written as plain sentences, not as
        a parenthetical annotation; the default pattern list must catch both,
        as long as the sentence actually asserts a requirement."""
        from sova.config.models import CommitConfig

        defaults = CommitConfig().owner_only_patterns
        verdicts = [
            ("This requires manual verification by the owner", "met"),
            ("Requires interactive confirmation before deploy", "met"),
            ("Automated fix works end to end", "met"),
        ]
        result = apply_owner_overrides(verdicts, defaults)
        assert result[0] == ("This requires manual verification by the owner", "owner")
        assert result[1] == ("Requires interactive confirmation before deploy", "owner")
        assert result[2] == ("Automated fix works end to end", "met")

    def test_default_patterns_do_not_match_incidental_prose(self) -> None:
        """Bare "interactive"/"manually" are too common in ordinary feature
        prose (this repo's own "interactive" suitability label, "manually
        override a setting") to use as owner-only defaults: only a
        requirement-phrased pattern should force the override, never a bare
        substring that merely co-occurs with a fully automatable criterion."""
        from sova.config.models import CommitConfig

        defaults = CommitConfig().owner_only_patterns
        verdicts = [
            ("Users can manually override the setting in the dashboard", "met"),
            ("The settings page updates interactively without a reload", "met"),
        ]
        assert apply_owner_overrides(verdicts, defaults) == verdicts

    @pytest.mark.parametrize(
        ("verdicts", "patterns", "expected"),
        [
            pytest.param(
                [("Deploy manually (owner only)", "met")],
                ["(owner"],
                [("Deploy manually (owner only)", "owner")],
                id="matching-pattern-forces-owner",
            ),
            pytest.param(
                [("Needs HUMAN-ONLY review", "unmet")],
                ["human-only"],
                [("Needs HUMAN-ONLY review", "owner")],
                id="case-insensitive-match",
            ),
            pytest.param(
                [("Automated fix works", "met")],
                ["(owner", "(manual"],
                [("Automated fix works", "met")],
                id="no-match-leaves-verdict-unchanged",
            ),
            pytest.param([("Something", "unmet")], [], [("Something", "unmet")], id="empty-patterns-is-noop"),
        ],
    )
    def test_explicit_patterns(
        self, verdicts: list[tuple[str, str]], patterns: list[str], expected: list[tuple[str, str]]
    ) -> None:
        assert apply_owner_overrides(verdicts, patterns) == expected


class TestApplyCiGatedOverrides:
    def test_default_patterns_match_the_template_check_criteria(self) -> None:
        """The issue template's two closing criteria are settled by the merge gate (required CI
        checks, and /integrate-pr waiting for green CI), not by anything the PR-body LLM can see."""
        from sova.config.models import CommitConfig

        defaults = CommitConfig().ci_gated_patterns
        verdicts = [
            ("All existing tests pass (`make check`)", "unmet"),
            ("No new lint warnings (`make lint`)", "unknown"),
            ("The resolver returns 1340", "unmet"),
        ]
        assert apply_ci_gated_overrides(verdicts, defaults) == [
            ("All existing tests pass (`make check`)", "met"),
            ("No new lint warnings (`make lint`)", "met"),
            ("The resolver returns 1340", "unmet"),
        ]

    def test_default_patterns_do_not_match_a_criterion_about_the_make_target(self) -> None:
        from sova.config.models import CommitConfig

        verdicts = [("Add a coverage threshold to the make check target", "unmet")]
        assert apply_ci_gated_overrides(verdicts, CommitConfig().ci_gated_patterns) == verdicts

    def test_case_insensitive_match(self) -> None:
        verdicts = [("ALL EXISTING TESTS PASS", "unmet")]
        assert apply_ci_gated_overrides(verdicts, ["all existing tests pass"]) == [("ALL EXISTING TESTS PASS", "met")]

    def test_empty_patterns_is_noop(self) -> None:
        verdicts = [("All existing tests pass (`make check`)", "unmet")]
        assert apply_ci_gated_overrides(verdicts, []) == verdicts

    def test_whitespace_only_patterns_are_ignored(self) -> None:
        verdicts = [("All existing tests pass (`make check`)", "unmet")]
        assert apply_ci_gated_overrides(verdicts, [" ", "\t"]) == verdicts

    def test_pattern_whitespace_is_trimmed_before_matching(self) -> None:
        verdicts = [("All existing tests pass", "unmet"), ("Something else", "unmet")]
        assert apply_ci_gated_overrides(verdicts, ["  all existing tests pass "]) == [
            ("All existing tests pass", "met"),
            ("Something else", "unmet"),
        ]


class TestProcessLlmResult:
    def test_unparseable_verdict_block_becomes_unknown_not_unmet(self) -> None:
        """An unparseable block must degrade to a distinct "unknown" token,
        not the model-reportable "unmet", so a human reviewer can tell the
        two apart in the rendered closing block."""
        raw = "## Summary\n- stuff\n"  # no verdict block at all
        _, verdicts = process_llm_result(raw, ["First", "Second"], _commit())
        assert verdicts == [("First", "unknown"), ("Second", "unknown")]

    def test_owner_patterns_still_apply_on_the_unparseable_path(self) -> None:
        raw = "## Summary\n- stuff\n"
        _, verdicts = process_llm_result(raw, ["Deploy it (owner only)"], _commit(owner=["(owner"]))
        assert verdicts == [("Deploy it (owner only)", "owner")]

    def test_ci_gated_criteria_are_met_on_the_unparseable_path(self) -> None:
        raw = "## Summary\n- stuff\n"
        _, verdicts = process_llm_result(
            raw, ["Feature works", "All existing tests pass"], _commit(ci_gated=["tests pass"])
        )
        assert verdicts == [("Feature works", "unknown"), ("All existing tests pass", "met")]

    def test_owner_pattern_wins_over_a_ci_gated_one(self) -> None:
        raw = f"## Summary\n- stuff\n{VERDICT_MARKER}\n- [unmet] Run make check in an interactive session\n"
        _, verdicts = process_llm_result(
            raw,
            ["Run make check in an interactive session"],
            _commit(owner=["interactive session"], ci_gated=["make check"]),
        )
        assert verdicts == [("Run make check in an interactive session", "owner")]

    def test_only_ci_gated_criteria_unmet_renders_closes(self) -> None:
        """PR #1364 implemented all of #1354 yet rendered "Part of #1354", listing only the two
        check criteria as not done, because the model cannot see test results when it writes the body."""
        criteria = ["Resolver picks the branch issue", "All existing tests pass (`make check`)"]
        raw = (
            f"## Summary\n- stuff\n{VERDICT_MARKER}\n"
            "- [met] Resolver picks the branch issue\n- [unmet] All existing tests pass (`make check`)\n"
        )
        _, verdicts = process_llm_result(raw, criteria, CommitConfig())
        assert render_closing_block("1354", verdicts) == "Closes #1354"


class TestRenderClosingBlock:
    def test_no_verdicts_is_unconditional_closes(self) -> None:
        assert render_closing_block("42", None) == "Closes #42"

    def test_all_met_is_closes(self) -> None:
        verdicts = [("A", "met"), ("B", "met")]
        assert render_closing_block("42", verdicts) == "Closes #42"

    def test_some_unmet_is_part_of_with_list(self) -> None:
        verdicts = [("A", "met"), ("B", "unmet")]
        result = render_closing_block("42", verdicts)
        assert result.startswith("Part of #42")
        assert "Not done in this PR:" in result
        assert "- B" in result
        assert "- A" not in result

    def test_owner_verdict_is_annotated_and_listed(self) -> None:
        verdicts = [("Deploy it manually", "owner")]
        result = render_closing_block("42", verdicts)
        assert "Part of #42" in result
        assert "- Deploy it manually (requires owner/manual action)" in result

    def test_multiple_unmet_and_owner_each_listed_once(self) -> None:
        verdicts = [("A", "met"), ("B", "unmet"), ("C", "owner"), ("D", "unmet")]
        result = render_closing_block("42", verdicts)
        assert result.count("- B") == 1
        assert result.count("- C") == 1
        assert result.count("- D") == 1
        assert "- A" not in result

    def test_unknown_verdict_gets_distinguishing_note_and_suffix(self) -> None:
        """An "unknown" verdict (unparseable LLM output) must read differently
        from a genuine "unmet", so a reviewer is not told two criteria were
        checked and failed when they were never actually verdicted."""
        verdicts = [("A", "unknown"), ("B", "unknown")]
        result = render_closing_block("42", verdicts)
        assert "could not be parsed" in result
        assert "- A (unverified)" in result
        assert "- B (unverified)" in result

    def test_genuine_unmet_has_no_unparseable_note(self) -> None:
        verdicts = [("A", "unmet")]
        result = render_closing_block("42", verdicts)
        assert "could not be parsed" not in result
        assert "(unverified)" not in result


class TestSplitVerdictTail:
    def test_no_marker_returns_whole_text_as_body(self) -> None:
        body, tail = split_verdict_tail("## Summary\n- stuff\n")
        assert body == "## Summary\n- stuff"
        assert tail == ""

    def test_marker_splits_body_from_tail(self) -> None:
        raw = f"## Summary\n- stuff\n{VERDICT_MARKER}\n- [met] First\n"
        body, tail = split_verdict_tail(raw)
        assert body == "## Summary\n- stuff"
        assert "- [met] First" in tail


class TestComposeBody:
    def test_github_no_verdicts_appends_closes(self) -> None:
        body = compose_body("## Summary\n- stuff", "42", None, is_jira=False)
        assert body == "## Summary\n- stuff\n\n## Issue\n\nCloses #42\n"

    @pytest.mark.parametrize(
        "summary",
        [
            pytest.param("## Summary\n- stuff\n\nFixes #42", id="llm-echoed-closing-keyword"),
            pytest.param("## Summary\n- stuff\n\n- Closes #42", id="list-item-closing-keyword"),
            pytest.param("## Summary\n- stuff\n\nCloses #42.", id="sentence-terminated-closing-keyword"),
            # A PR body edited in the GitHub web UI uses CRLF line endings, so a stale "Closes #42\r" line must
            # still be stripped (RefreshPrBodyStep applies this same pattern to the body it fetches from GitHub).
            pytest.param(
                "## Summary\r\n- stuff\r\n\r\nCloses #42\r", id="closing-keyword-with-trailing-carriage-return"
            ),
        ],
    )
    def test_github_strips_stray_closing_keyword(self, summary: str) -> None:
        body = compose_body(summary, "42", None, is_jira=False)
        assert body.count("#42") == 1
        assert "Closes #42" in body

    def test_github_does_not_strip_inline_mention_of_the_issue(self) -> None:
        """A sentence that merely mentions the issue inline must survive untouched:
        only a line that is *only* a closing reference is a stray echo."""
        body = compose_body("## Summary\n- This PR fixes #42 by rewiring the parser.", "42", None, is_jira=False)
        assert "This PR fixes #42 by rewiring the parser." in body
        assert "## Issue" in body
        assert "Closes #42" in body

    def test_github_unmet_criteria_neutralizes_inline_mention(self) -> None:
        """A "Part of" decision must never leave a live inline closing-keyword
        mention the LLM wrote in prose: GitHub auto-closes on an inline
        mention just as readily as on a standalone line."""
        verdicts = [("A", "unmet")]
        body = compose_body("## Summary\n- This PR fixes #42 by rewiring the parser.", "42", verdicts, is_jira=False)
        assert "closes #42" not in body.lower()
        assert "fixes #42" not in body.lower()
        assert "This PR addresses #42 by rewiring the parser." in body
        assert "Part of #42" in body

    def test_github_unmet_criteria_produces_part_of(self) -> None:
        verdicts = [("A", "unmet")]
        body = compose_body("## Summary\n- stuff", "42", verdicts, is_jira=False)
        assert "Part of #42" in body
        assert "Closes #42" not in body

    def test_closing_decision_is_its_own_addressable_section(self) -> None:
        """The closing decision lives under its own ``## Issue`` heading, not as
        trailing text inside whichever section happens to be last, so a refresh
        can always find and replace it regardless of other heading drift."""
        body = compose_body("## Summary\n- stuff\n\n## Review guidance\n- more stuff", "42", None, is_jira=False)
        sections = split_sections(body)
        assert sections["Issue"] == "Closes #42"

    def test_closing_keyword_for_another_issue_is_put_in_a_code_span(self) -> None:
        """PR #1364's generated Test plan said "its original text closes #12 and #1340", so GitHub
        listed the PR as closing #12 and the dashboard showed it as issue 12's PR."""
        sections = "## Test plan\n- cases for the PR #1351 body (its original text closes #12 and #1340)"
        body = compose_body(sections, "1354", [("A", "unmet")], is_jira=False)
        assert "(its original text `closes #12` and #1340)" in body
        assert parse_issue_links(body) == IssueLinks(closes=(), part_of=("1354",))

    def test_closing_keyword_for_another_issue_is_defused_under_a_closes_decision(self) -> None:
        body = compose_body("## Summary\n- Also fixes: #7, and Resolves\n#8.", "42", None, is_jira=False)
        assert "Also `fixes: #7`, and `Resolves #8`." in body
        assert parse_issue_links(body).closes == ("42",)

    def test_closing_keyword_already_in_code_is_left_alone(self) -> None:
        sections = "## Summary\n- quotes `Closes #7` verbatim\n\n```text\nCloses #8\n```"
        body = compose_body(sections, "42", None, is_jira=False)
        assert "- quotes `Closes #7` verbatim\n\n```text\nCloses #8\n```" in body

    def test_jira_adds_link_once(self) -> None:
        body = compose_body("## Summary\n- stuff", "42", None, is_jira=True, jira_link="JIRA: https://x/42")
        assert body.count("JIRA: https://x/42") == 1

    def test_jira_strips_closing_keyword_llm_wrote(self) -> None:
        body = compose_body(
            "## Summary\n- stuff\n\nCloses #42", "42", None, is_jira=True, jira_link="JIRA: https://x/42"
        )
        assert "Closes #42" not in body
        assert "JIRA: https://x/42" in body

    def test_jira_neutralizes_inline_closing_keyword(self) -> None:
        """A JIRA body never carries a live GitHub closing keyword in any
        position: an inline mid-sentence mention still refers to a real
        GitHub issue number in this repo and would auto-close it on merge."""
        body = compose_body(
            "## Summary\n- This change fixes #123 in the dashboard parser.",
            "123",
            None,
            is_jira=True,
            jira_link="JIRA: https://x/123",
        )
        assert "fixes #123" not in body.lower()
        assert "closes #123" not in body.lower()
        assert "This change addresses #123 in the dashboard parser." in body
        assert "JIRA: https://x/123" in body


class TestSplitAndApplySections:
    def test_split_sections_round_trips(self) -> None:
        markdown = "## Summary\n- a\n\n## Review guidance\n- b\n\n## Test plan\n- c"
        sections = split_sections(markdown)
        assert sections == {"Summary": "- a", "Review guidance": "- b", "Test plan": "- c"}

    def test_split_sections_concatenates_repeated_heading_instead_of_losing_it(self) -> None:
        """A heading the LLM restates (rather than only keeping the last
        occurrence) must not silently drop the earlier block's content."""
        markdown = "## Summary\n- first\n\n## Test plan\n- c\n\n## Summary\n- second"
        sections = split_sections(markdown)
        assert "- first" in sections["Summary"]
        assert "- second" in sections["Summary"]
        assert sections["Test plan"] == "- c"

    def test_split_sections_ignores_fenced_heading_lines(self) -> None:
        """A "## " line inside a fenced code block (e.g. an embedded commit
        log or markdown example) must not be read as a section boundary,
        which would both truncate the real section early and invent a bogus
        section from the fenced example's own content."""
        markdown = "## Summary\n- a\n\n## Test plan\n```md\n## Fake\nstuff\n```\n- ran tests\n"
        sections = split_sections(markdown)
        assert "Fake" not in sections
        assert "## Fake" in sections["Test plan"]
        assert "- ran tests" in sections["Test plan"]

    def test_apply_sections_leaves_other_headings_untouched(self) -> None:
        existing = (
            "## Summary\nold summary\n\n## Review guidance\nold guidance\n\n"
            "## Test plan\nold plan\n\nCloses #42\n\n## Confidence Score\n\n**85/100**\n"
        )
        fresh = "## Summary\nnew summary\n\n## Review guidance\nnew guidance\n\n## Test plan\nnew plan\n\nCloses #42"
        updated = apply_sections(existing, split_sections(fresh))

        assert "new summary" in updated
        assert "new guidance" in updated
        assert "new plan" in updated
        assert "old summary" not in updated
        assert "**85/100**" in updated
        assert "## Confidence Score" in updated

    def test_apply_sections_replaces_part_of_block_on_refresh(self) -> None:
        existing = (
            "## Summary\nold\n\n## Review guidance\nold\n\n"
            "## Test plan\nold\n\nPart of #42\n\nNot done in this PR:\n- stale item\n\n"
            "## Confidence Score\n\n**50/100**\n"
        )
        fresh = "## Summary\nnew\n\n## Review guidance\nnew\n\n## Test plan\nnew\n\nCloses #42"
        updated = apply_sections(existing, split_sections(fresh))

        assert "Closes #42" in updated
        assert "stale item" not in updated
        assert "**50/100**" in updated


class TestBuildPrompt:
    """The prompt's issue section, and when a verdict block is requested."""

    def test_github_issue_prompt_asks_for_verdicts(self, tmp_path: Path) -> None:
        prompt, criteria = build_pr_body_prompt(
            _ctx(tmp_path),
            task_title="Fix the bug",
            issue_body="## Acceptance Criteria\n- [ ] First thing works\n",
            commit_log="abc123 fix: thing",
            diff_stat=" src/app.py | 1 +",
            diff_content="diff --git a/src/app.py",
        )

        assert criteria == ["First thing works"]
        assert 'Do NOT use "Closes #42"' in prompt
        assert VERDICT_MARKER in prompt
        assert "- First thing works" in prompt
        # The bare "Output ONLY the markdown body ... no preamble" sentence
        # must not appear verbatim when a trailing verdict block is also
        # requested: it contradicts the later instruction in the same prompt.
        assert "no preamble). Start directly" not in prompt
        assert "and nothing else" in prompt

    def test_issue_without_criteria_omits_verdict_block(self, tmp_path: Path) -> None:
        prompt, criteria = build_pr_body_prompt(
            _ctx(tmp_path),
            task_title="Fix the bug",
            issue_body="## Objective\nJust do it.\n",
            commit_log="abc123 fix: thing",
            diff_stat="",
            diff_content="",
        )

        assert criteria == []
        assert VERDICT_MARKER not in prompt
        # A title block is still requested for a non-JIRA project even with
        # no verdict block, so the bare "Start directly" instruction (which
        # would contradict that trailing title request) must not appear.
        assert "no preamble). Start directly" not in prompt
        assert TITLE_MARKER in prompt

    def test_issueless_run_has_no_closing_keyword_and_no_criteria(self, tmp_path: Path) -> None:
        """An issue-less run has nothing to close and nothing to verdict."""
        prompt, criteria = build_pr_body_prompt(
            _ctx(tmp_path, issue_number=""),
            task_title="Ad-hoc cleanup",
            issue_body="## Acceptance Criteria\n- [ ] Ignored for an issue-less run\n",
            commit_log="abc123 chore: cleanup",
            diff_stat="",
            diff_content="",
        )

        assert criteria == []
        assert "Task: Ad-hoc cleanup" in prompt
        assert "Closes #" not in prompt
        assert VERDICT_MARKER not in prompt
        assert TITLE_MARKER in prompt


class TestBuildPromptTitleRequest:
    """A PR title is requested for a non-JIRA project, never for JIRA."""

    def test_github_project_requests_a_title_scoped_to_configured_lists(self, tmp_path: Path) -> None:
        prompt, _criteria = build_pr_body_prompt(
            _ctx(tmp_path),
            task_title="Fix the bug",
            issue_body="",
            commit_log="",
            diff_stat="",
            diff_content="",
        )

        assert TITLE_MARKER in prompt
        assert "feat, fix, refactor, test, docs, chore, ci" in prompt
        assert "core" in prompt

    def test_jira_project_never_requests_a_title(self, tmp_path: Path) -> None:
        from sova.config.models import TaskSourceConfig

        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=ProjectConfig(
                task_source=TaskSourceConfig(
                    type="jira", jira_project_key="PROJ", jira_base_url="https://jira.example.com"
                )
            ),
            adapter=None,
            issue_number="42",
            role="developer",
        )
        prompt, _criteria = build_pr_body_prompt(
            ctx,
            task_title="Fix the bug",
            issue_body="",
            commit_log="",
            diff_stat="",
            diff_content="",
        )

        assert TITLE_MARKER not in prompt

    def test_request_title_false_skips_the_block_for_a_github_project(self, tmp_path: Path) -> None:
        """A caller that discards the title (RefreshPrBodyStep) must be able
        to opt out of requesting it at all, not just strip it after the
        fact: the flag's own effect is what this test locks in."""
        prompt, _criteria = build_pr_body_prompt(
            _ctx(tmp_path),
            task_title="Fix the bug",
            issue_body="",
            commit_log="",
            diff_stat="",
            diff_content="",
            request_title=False,
        )

        assert TITLE_MARKER not in prompt


class TestSplitTitleTail:
    def test_splits_marker_and_candidate_title(self) -> None:
        raw = f"## Summary\n- stuff\n{TITLE_MARKER}\nfeat(core): add widget"
        body, title = split_title_tail(raw)
        assert body == "## Summary\n- stuff"
        assert title == "feat(core): add widget"

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("## Summary\n- stuff", id="no-marker"),
            pytest.param(f"## Summary\n- stuff\n{TITLE_MARKER}\n", id="marker-with-no-following-content"),
        ],
    )
    def test_returns_none_title(self, raw: str) -> None:
        body, title = split_title_tail(raw)
        assert body == "## Summary\n- stuff"
        assert title is None

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(
                f"## Summary\n- stuff\n{VERDICT_MARKER}\n- [met] First thing\n{TITLE_MARKER}\nfeat(core): add widget",
                id="title-tail-after-verdict-tail",
            ),
            # Only the marker and the title line are removed, not the rest of the response: a model that emits
            # the title block before the verdict block must not lose the verdicts, which drive `Closes #N`.
            pytest.param(
                f"## Summary\n- stuff\n{TITLE_MARKER}\nfeat(core): add widget\n{VERDICT_MARKER}\n- [met] First thing",
                id="title-block-emitted-before-verdict-block",
            ),
        ],
    )
    def test_title_and_verdict_tails_both_survive(self, raw: str) -> None:
        remainder, title = split_title_tail(raw)
        assert title == "feat(core): add widget"
        body, tail = split_verdict_tail(remainder)
        assert body == "## Summary\n- stuff"
        assert "- [met] First thing" in tail

    def test_rejects_a_candidate_that_is_itself_the_verdict_marker(self) -> None:
        """A model that emits the title block before the verdict block and
        omits the title line entirely leaves the verdict marker as the next
        non-blank line. Consuming that as the "title" would destroy the
        whole verdict block it immediately precedes; it must be rejected and
        left for split_verdict_tail() instead, which still finds and parses
        it correctly (the stray, unconsumed title marker text is cosmetic
        noise left in the body, not a parsing failure)."""
        raw = f"## Summary\n- stuff\n{TITLE_MARKER}\n\n{VERDICT_MARKER}\n- [met] First thing"
        remainder, title = split_title_tail(raw)

        assert title is None
        assert remainder == raw
        _, tail = split_verdict_tail(remainder)
        assert "- [met] First thing" in tail


class TestLineParsingIsWhitespaceTolerant:
    """The criterion, verdict, and heading patterns are matched without
    lazy-quantifier backtracking, so each one's whitespace handling is
    pinned here rather than left to the regex's shape."""

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param(
                "## Acceptance Criteria\n- [ ] First thing works   \n-    Second thing works\t\n",
                ["First thing works", "Second thing works"],
                id="criterion-line-tolerates-trailing-whitespace",
            ),
            pytest.param(
                "## Acceptance Criteria\n-   \n- [ ] Real one\n",
                ["Real one"],
                id="dash-with-only-whitespace-is-not-a-criterion",
            ),
        ],
    )
    def test_criterion_line_whitespace(self, body: str, expected: list[str]) -> None:
        assert extract_acceptance_criteria(body) == expected

    @pytest.mark.parametrize(
        ("tail", "criteria", "expected"),
        [
            pytest.param(
                "-  [MET]   First thing works  \n- [unmet]Second thing works\n",
                ["First", "Second"],
                [("First", "met"), ("Second", "unmet")],
                id="verdict-line-tolerates-padding-around-the-token",
            ),
            pytest.param("- [met]\n", ["First"], None, id="verdict-line-with-no-text-is-unparseable"),
        ],
    )
    def test_verdict_line_whitespace(
        self, tail: str, criteria: list[str], expected: list[tuple[str, str]] | None
    ) -> None:
        assert parse_verdicts(tail, criteria) == expected

    def test_split_sections_strips_heading_whitespace(self) -> None:
        sections = split_sections("## Summary   \n- a\n\n##\tTest plan\n- b\n")
        assert sections == {"Summary": "- a", "Test plan": "- b"}

    def test_bare_hashes_do_not_borrow_the_next_line_as_a_heading(self) -> None:
        """ "##" alone on a line is not a heading: under re.MULTILINE a "\\s+"
        separator would have let it swallow the newline and claim "Summary"."""
        sections = split_sections("## Summary\n- a\n\n##\n\nTest plan\n- b\n")
        assert list(sections) == ["Summary"]
        assert "Test plan" in sections["Summary"]


class TestTruncateDiff:
    def test_diff_exactly_at_the_cap_is_untouched(self) -> None:
        assert truncate_diff("x" * 100, True, 100) == "x" * 100

    def test_longer_diff_is_cut_to_the_cap_and_marked(self) -> None:
        trimmed = truncate_diff("x" * 101, True, 100)
        assert trimmed.startswith("x" * 100)
        assert "x" * 101 not in trimmed
        assert "...(truncated)" in trimmed

    def test_a_cut_logs_one_event_with_the_sizes_and_the_cap(self) -> None:
        with structlog.testing.capture_logs() as logs:
            truncate_diff("x" * 101, True, 100)
        events = [e for e in logs if e["event"] == "llm.payload.trimmed"]
        assert len(events) == 1
        assert events[0]["label"] == "pr_body_diff"
        assert (events[0]["original_chars"], events[0]["kept_chars"], events[0]["limit"]) == (101, 100, 100)

    def test_a_diff_at_the_cap_logs_nothing(self) -> None:
        with structlog.testing.capture_logs() as logs:
            assert truncate_diff("x" * 100, True, 100) == "x" * 100
        assert [e for e in logs if e["event"] == "llm.payload.trimmed"] == []

    def test_empty_successful_diff_reports_no_changes(self) -> None:
        assert truncate_diff("", True, 100) == "(no changes detected)"

    def test_stat_only_input_still_builds_a_valid_prompt(self, tmp_path: Path) -> None:
        prompt, _ = build_pr_body_prompt(
            _ctx(tmp_path),
            task_title="Rename",
            issue_body="",
            commit_log="abc rename",
            diff_stat=" a.png => b.png | 0",
            diff_content=truncate_diff("", True, 100),
        )
        assert "a.png => b.png" in prompt
        assert "(no changes detected)" in prompt


class TestVerdictOnlyPrompt:
    def test_asks_for_the_verdict_block_and_no_body_sections(self, tmp_path: Path) -> None:
        prompt, criteria = build_pr_body_prompt(
            _ctx(tmp_path),
            task_title="T",
            issue_body="## Acceptance Criteria\n- Works\n",
            commit_log="log",
            diff_stat="stat",
            diff_content="diff",
            request_title=False,
            verdict_only=True,
        )
        assert criteria == ["Works"]
        assert VERDICT_MARKER in prompt
        assert "- Works" in prompt
        assert "## Review guidance" not in prompt
        assert "## Test plan" not in prompt

    def test_without_criteria_the_full_prompt_is_kept(self, tmp_path: Path) -> None:
        prompt, criteria = build_pr_body_prompt(
            _ctx(tmp_path),
            task_title="T",
            issue_body="no criteria here",
            commit_log="log",
            diff_stat="stat",
            diff_content="diff",
            request_title=False,
            verdict_only=True,
        )
        assert criteria == []
        assert "## Review guidance" in prompt


class TestGeneratePrBodyText:
    @staticmethod
    def _call(tmp_path: Path, invoke: AsyncMock, *, full_diff: str = "x" * 1000, diff_succeeded: bool = True, **commit):
        ctx = _ctx(tmp_path)
        ctx.config.commit = CommitConfig(**commit)
        return generate_pr_body_text(
            ctx,
            invoke,
            task_title="Title",
            issue_body="",
            commit_log="log",
            diff_stat="stat",
            full_diff=full_diff,
            diff_succeeded=diff_succeeded,
            request_title=False,
        )

    @staticmethod
    def _result() -> LLMResult:
        return LLMResult(text="## Summary", model="m", cost_usd=Decimal(0))

    async def test_uses_the_configured_timeout_and_diff_cap(self, tmp_path: Path) -> None:
        invoke = AsyncMock(return_value=self._result())
        await self._call(tmp_path, invoke, pr_body_timeout=77, pr_body_diff_cap=200)

        assert invoke.await_count == 1
        assert invoke.call_args.kwargs["timeout"] == 77
        assert invoke.call_args.kwargs["task_type"] == "pr_body"
        assert "x" * 200 in invoke.call_args.args[0]
        assert "x" * 201 not in invoke.call_args.args[0]

    async def test_timeout_is_retried_once_with_half_the_diff_cap(self, tmp_path: Path) -> None:
        invoke = AsyncMock(side_effect=[LLMTimeoutError("t"), self._result()])
        await self._call(tmp_path, invoke, pr_body_diff_cap=200)

        assert invoke.await_count == 2
        retry_prompt = invoke.call_args.args[0]
        assert "x" * 100 in retry_prompt
        assert "x" * 101 not in retry_prompt

    async def test_retry_keeps_the_timeout_and_sends_at_most_half_the_cap(self, tmp_path: Path) -> None:
        invoke = AsyncMock(side_effect=[LLMTimeoutError("t"), self._result()])
        await self._call(tmp_path, invoke, full_diff="x" * 5000, pr_body_timeout=77, pr_body_diff_cap=200)

        first, second = invoke.call_args_list
        assert first.kwargs["timeout"] == second.kwargs["timeout"] == 77
        assert "x" * 200 in first.args[0]
        assert "x" * 100 in second.args[0]
        assert "x" * 101 not in second.args[0]

    async def test_retry_of_a_small_diff_is_smaller_than_the_first_input(self, tmp_path: Path) -> None:
        invoke = AsyncMock(side_effect=[LLMTimeoutError("t"), self._result()])
        await self._call(tmp_path, invoke, full_diff="x" * 60, pr_body_diff_cap=8000)

        first, second = invoke.call_args_list
        assert "x" * 60 in first.args[0]
        assert "x" * 30 in second.args[0]
        assert "x" * 31 not in second.args[0]

    async def test_timeout_on_an_empty_diff_is_not_retried_with_the_same_prompt(self, tmp_path: Path) -> None:
        invoke = AsyncMock(side_effect=LLMTimeoutError("t"))
        with pytest.raises(LLMTimeoutError):
            await self._call(tmp_path, invoke, full_diff="")
        assert invoke.await_count == 1

    async def test_timeout_on_a_failed_diff_is_not_retried_with_a_garbled_placeholder(self, tmp_path: Path) -> None:
        invoke = AsyncMock(side_effect=LLMTimeoutError("t"))
        with pytest.raises(LLMTimeoutError):
            await self._call(tmp_path, invoke, full_diff="(unavailable)", diff_succeeded=False)
        assert invoke.await_count == 1
        assert "(unavailable)" in invoke.call_args.args[0]

    async def test_second_timeout_propagates_without_a_third_call(self, tmp_path: Path) -> None:
        invoke = AsyncMock(side_effect=LLMTimeoutError("t"))
        with pytest.raises(LLMTimeoutError):
            await self._call(tmp_path, invoke)
        assert invoke.await_count == 2

    async def test_other_failures_are_not_retried(self, tmp_path: Path) -> None:
        invoke = AsyncMock(side_effect=LLMInvocationError("boom"))
        with pytest.raises(LLMInvocationError):
            await self._call(tmp_path, invoke)
        assert invoke.await_count == 1
