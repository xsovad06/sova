"""Tests for the shared PR-body helpers: acceptance-criteria extraction,
verdict parsing, owner-only overrides, and section-based body assembly.
"""

from __future__ import annotations

from pathlib import Path

from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.core.steps._pr_body import (
    VERDICT_MARKER,
    apply_owner_overrides,
    apply_sections,
    compose_body,
    extract_acceptance_criteria,
    parse_verdicts,
    process_llm_result,
    render_closing_block,
    split_sections,
    split_verdict_tail,
)
from sova.core.steps._pr_body import build_prompt as build_pr_body_prompt


def _ctx(tmp_path: Path, issue_number: str = "42") -> ExecutionContext:
    return ExecutionContext(
        project_dir=tmp_path,
        config=ProjectConfig(),
        adapter=None,
        issue_number=issue_number,
        role="developer",
    )


class TestExtractAcceptanceCriteria:
    def test_no_heading_returns_empty(self) -> None:
        assert extract_acceptance_criteria("## Objective\nDo the thing.\n") == []

    def test_empty_body_returns_empty(self) -> None:
        assert extract_acceptance_criteria("") == []

    def test_heading_with_no_items_returns_empty(self) -> None:
        body = "## Acceptance Criteria\n\n## Out of Scope\nNothing.\n"
        assert extract_acceptance_criteria(body) == []

    def test_extracts_checkbox_items(self) -> None:
        body = "## Acceptance Criteria\n- [ ] First thing works\n- [ ] Second thing works\n"
        assert extract_acceptance_criteria(body) == ["First thing works", "Second thing works"]

    def test_tolerant_of_checked_boxes_and_bare_dashes(self) -> None:
        body = "## Acceptance Criteria\n- [x] Done already\n- Bare dash item\n"
        assert extract_acceptance_criteria(body) == ["Done already", "Bare dash item"]

    def test_stops_at_next_heading(self) -> None:
        body = "## Acceptance Criteria\n- [ ] Only this one\n\n## Out of Scope\n- [ ] Not a criterion\n"
        assert extract_acceptance_criteria(body) == ["Only this one"]

    def test_stops_at_end_of_body_when_last_section(self) -> None:
        body = "## Acceptance Criteria\n- [ ] Only this one\n"
        assert extract_acceptance_criteria(body) == ["Only this one"]

    def test_heading_is_case_insensitive(self) -> None:
        body = "## Acceptance criteria\n- [ ] Lowercase c\n"
        assert extract_acceptance_criteria(body) == ["Lowercase c"]

    def test_heading_tolerates_extra_whitespace(self) -> None:
        body = "##  Acceptance Criteria\n- [ ] Extra space before heading text\n"
        assert extract_acceptance_criteria(body) == ["Extra space before heading text"]

    def test_fenced_block_does_not_truncate_section_or_leak_lines(self) -> None:
        """A ``## `` line and a ``- `` line inside a fenced example must not be
        mistaken for the section boundary or for a criterion: both would
        otherwise either truncate the real section early (dropping a later,
        genuine criterion) or invent a bogus criterion from example code."""
        body = (
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
        )
        assert extract_acceptance_criteria(body) == ["Do X", "Do Z"]

    def test_horizontal_rule_is_not_a_criterion(self) -> None:
        """A "---" horizontal rule has no whitespace after its marker, so it
        must not be parsed as a criterion with text "--"."""
        body = "## Acceptance Criteria\n- [ ] real one\n\n---\n"
        assert extract_acceptance_criteria(body) == ["real one"]

    def test_accepts_asterisk_and_plus_bullets(self) -> None:
        body = "## Acceptance Criteria\n* [ ] Star bullet\n+ [ ] Plus bullet\n"
        assert extract_acceptance_criteria(body) == ["Star bullet", "Plus bullet"]

    def test_indented_sub_bullet_is_not_promoted_to_a_criterion(self) -> None:
        """A nested sub-bullet or continuation line must never be promoted to
        a top-level criterion, even when it uses checkbox syntax itself."""
        body = "## Acceptance Criteria\n- [ ] Top level thing\n  - [ ] sub detail one\n  - sub detail two\n"
        assert extract_acceptance_criteria(body) == ["Top level thing"]


class TestParseVerdicts:
    def test_parses_matching_count(self) -> None:
        criteria = ["First", "Second"]
        tail = "- [met] First\n- [unmet] Second\n"
        assert parse_verdicts(tail, criteria) == [("First", "met"), ("Second", "unmet")]

    def test_count_mismatch_is_unparseable(self) -> None:
        assert parse_verdicts("- [met] Only one\n", ["First", "Second"]) is None

    def test_invalid_verdict_token_is_unparseable(self) -> None:
        assert parse_verdicts("- [maybe] First\n", ["First"]) is None

    def test_malformed_line_is_unparseable(self) -> None:
        assert parse_verdicts("First is met\n", ["First"]) is None

    def test_empty_criteria_and_empty_tail_parses_to_empty_list(self) -> None:
        assert parse_verdicts("", []) == []

    def test_tolerates_trailing_prose_after_well_formed_verdicts(self) -> None:
        """A cosmetic LLM deviation (a trailing summary sentence after the
        verdict list) must not degrade correctly-reported "met" verdicts to
        "all unmet": only lines actually shaped like a verdict are counted."""
        tail = "- [met] First\n- [met] Second\n\nAll criteria satisfied.\n"
        assert parse_verdicts(tail, ["First", "Second"]) == [("First", "met"), ("Second", "met")]


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

    def test_matching_pattern_forces_owner(self) -> None:
        verdicts = [("Deploy manually (owner only)", "met")]
        result = apply_owner_overrides(verdicts, ["(owner"])
        assert result == [("Deploy manually (owner only)", "owner")]

    def test_case_insensitive_match(self) -> None:
        verdicts = [("Needs HUMAN-ONLY review", "unmet")]
        result = apply_owner_overrides(verdicts, ["human-only"])
        assert result == [("Needs HUMAN-ONLY review", "owner")]

    def test_no_match_leaves_verdict_unchanged(self) -> None:
        verdicts = [("Automated fix works", "met")]
        assert apply_owner_overrides(verdicts, ["(owner", "(manual"]) == verdicts

    def test_empty_patterns_is_noop(self) -> None:
        verdicts = [("Something", "unmet")]
        assert apply_owner_overrides(verdicts, []) == verdicts


class TestProcessLlmResult:
    def test_unparseable_verdict_block_becomes_unknown_not_unmet(self) -> None:
        """An unparseable block must degrade to a distinct "unknown" token,
        not the model-reportable "unmet", so a human reviewer can tell the
        two apart in the rendered closing block."""
        raw = "## Summary\n- stuff\n"  # no verdict block at all
        _, verdicts = process_llm_result(raw, ["First", "Second"], owner_only_patterns=[])
        assert verdicts == [("First", "unknown"), ("Second", "unknown")]

    def test_owner_patterns_still_apply_on_the_unparseable_path(self) -> None:
        raw = "## Summary\n- stuff\n"
        _, verdicts = process_llm_result(raw, ["Deploy it (owner only)"], owner_only_patterns=["(owner"])
        assert verdicts == [("Deploy it (owner only)", "owner")]


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

    def test_github_strips_llm_echoed_closing_keyword(self) -> None:
        body = compose_body("## Summary\n- stuff\n\nFixes #42", "42", None, is_jira=False)
        assert body.count("#42") == 1
        assert "Closes #42" in body

    def test_github_does_not_strip_inline_mention_of_the_issue(self) -> None:
        """A sentence that merely mentions the issue inline must survive untouched:
        only a line that is *only* a closing reference is a stray echo."""
        body = compose_body("## Summary\n- This PR fixes #42 by rewiring the parser.", "42", None, is_jira=False)
        assert "This PR fixes #42 by rewiring the parser." in body
        assert "## Issue" in body
        assert "Closes #42" in body

    def test_github_strips_list_item_closing_keyword(self) -> None:
        body = compose_body("## Summary\n- stuff\n\n- Closes #42", "42", None, is_jira=False)
        assert body.count("#42") == 1
        assert "Closes #42" in body

    def test_github_strips_sentence_terminated_closing_keyword(self) -> None:
        body = compose_body("## Summary\n- stuff\n\nCloses #42.", "42", None, is_jira=False)
        assert body.count("#42") == 1
        assert "Closes #42" in body

    def test_github_strips_closing_keyword_with_trailing_carriage_return(self) -> None:
        """A PR body edited in the GitHub web UI uses CRLF line endings, so a
        stale "Closes #42\\r" line must still be recognized and stripped
        (RefreshPrBodyStep applies this same pattern to the existing PR body
        it fetches from the GitHub API)."""
        body = compose_body("## Summary\r\n- stuff\r\n\r\nCloses #42\r", "42", None, is_jira=False)
        assert body.count("#42") == 1
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
        assert "no preamble). Start directly" in prompt

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


class TestLineParsingIsWhitespaceTolerant:
    """The criterion, verdict, and heading patterns are matched without
    lazy-quantifier backtracking, so each one's whitespace handling is
    pinned here rather than left to the regex's shape."""

    def test_criterion_line_tolerates_trailing_whitespace(self) -> None:
        body = "## Acceptance Criteria\n- [ ] First thing works   \n-    Second thing works\t\n"
        assert extract_acceptance_criteria(body) == ["First thing works", "Second thing works"]

    def test_dash_with_only_whitespace_is_not_a_criterion(self) -> None:
        body = "## Acceptance Criteria\n-   \n- [ ] Real one\n"
        assert extract_acceptance_criteria(body) == ["Real one"]

    def test_verdict_line_tolerates_padding_around_the_token(self) -> None:
        tail = "-  [MET]   First thing works  \n- [unmet]Second thing works\n"
        assert parse_verdicts(tail, ["First", "Second"]) == [("First", "met"), ("Second", "unmet")]

    def test_verdict_line_with_no_text_is_unparseable(self) -> None:
        assert parse_verdicts("- [met]\n", ["First"]) is None

    def test_split_sections_strips_heading_whitespace(self) -> None:
        sections = split_sections("## Summary   \n- a\n\n##\tTest plan\n- b\n")
        assert sections == {"Summary": "- a", "Test plan": "- b"}

    def test_bare_hashes_do_not_borrow_the_next_line_as_a_heading(self) -> None:
        """ "##" alone on a line is not a heading: under re.MULTILINE a "\\s+"
        separator would have let it swallow the newline and claim "Summary"."""
        sections = split_sections("## Summary\n- a\n\n##\n\nTest plan\n- b\n")
        assert list(sections) == ["Summary"]
        assert "Test plan" in sections["Summary"]
