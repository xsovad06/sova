"""Tests for the shared PR-body helpers: prompt assembly, the closing ``## Issue`` section,
and section-based body assembly.
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
    apply_sections,
    compose_body,
    generate_pr_body_text,
    split_sections,
    split_title_tail,
    truncate_diff,
)
from sova.core.steps._pr_body import build_prompt as build_pr_body_prompt
from sova.llm.errors import LLMInvocationError, LLMTimeoutError
from sova.llm.models import LLMResult
from sova.utils.gh import IssueLinks, parse_issue_links


def _ctx(tmp_path: Path, issue_number: str = "42") -> ExecutionContext:
    return ExecutionContext(
        project_dir=tmp_path,
        config=ProjectConfig(),
        adapter=None,
        issue_number=issue_number,
        role="developer",
    )


class TestComposeBody:
    def test_github_appends_closes(self) -> None:
        body = compose_body("## Summary\n- stuff", "42", is_jira=False)
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
        body = compose_body(summary, "42", is_jira=False)
        assert body.count("#42") == 1
        assert "Closes #42" in body

    def test_github_does_not_strip_inline_mention_of_the_issue(self) -> None:
        """A sentence that merely mentions the issue inline must survive untouched:
        only a line that is *only* a closing reference is a stray echo."""
        body = compose_body("## Summary\n- This PR fixes #42 by rewiring the parser.", "42", is_jira=False)
        assert "This PR fixes #42 by rewiring the parser." in body
        assert "## Issue" in body
        assert "Closes #42" in body

    def test_model_text_with_verdict_lines_or_part_of_still_closes(self) -> None:
        """The closing section is rendered independently of the model text, so verdict-style
        lines and a "Part of" phrase in it are description only."""
        sections = (
            "## Summary\n- stuff\n\n<!-- ACCEPTANCE_VERDICTS -->\n- [unmet] First thing\n\n"
            "## Review guidance\nThis is Part of #42 and the rest is left undone."
        )
        body = compose_body(sections, "42", is_jira=False)
        assert split_sections(body)["Issue"] == "Closes #42"
        # The prose "Part of #42" sits outside the ## Issue section, which is the only place a "Part of"
        # link is read from, so it does not become a second, conflicting link.
        assert parse_issue_links(body) == IssueLinks(closes=("42",), part_of=())
        # Leftover verdict output from the model is published as description text, unchanged.
        assert "<!-- ACCEPTANCE_VERDICTS -->" in body

    @pytest.mark.parametrize(
        "summary",
        [
            pytest.param("## Summary\n- stuff\n\n```python\nx = 1", id="backtick-fence-no-trailing-newline"),
            pytest.param("## Summary\n- stuff\n\n```\n", id="backtick-fence-trailing-newline"),
            pytest.param("## Summary\n- stuff\n\n~~~text\nx", id="tilde-fence"),
            pytest.param("## Summary\n- stuff\n\n````\nx\n```\ny", id="four-backtick-opener-inner-three"),
            pytest.param("## Summary\n- stuff\n\n  ```\nx", id="indented-opener"),
            pytest.param("## Summary\n- item\n  ```python\n  x = 1", id="fence-inside-list-item"),
        ],
    )
    def test_model_output_ending_inside_an_open_fence_still_closes(self, summary: str) -> None:
        """An unterminated fence would swallow the ``## Issue`` section into a code block."""
        body = compose_body(summary, "42", is_jira=False)
        assert split_sections(body)["Issue"] == "Closes #42"
        assert parse_issue_links(body).closes == ("42",)

    def test_closing_line_is_its_own_addressable_section(self) -> None:
        """The closing line lives under its own ``## Issue`` heading, not as
        trailing text inside whichever section happens to be last, so a refresh
        can always find and replace it regardless of other heading drift."""
        body = compose_body("## Summary\n- stuff\n\n## Review guidance\n- more stuff", "42", is_jira=False)
        sections = split_sections(body)
        assert sections["Issue"] == "Closes #42"

    def test_closing_keyword_for_another_issue_is_put_in_a_code_span(self) -> None:
        """PR #1364's generated Test plan said "its original text closes #12 and #1340", so GitHub
        listed the PR as closing #12 and the dashboard showed it as issue 12's PR."""
        sections = "## Test plan\n- cases for the PR #1351 body (its original text closes #12 and #1340)"
        body = compose_body(sections, "1354", is_jira=False)
        assert "(its original text `closes #12` and #1340)" in body
        assert parse_issue_links(body) == IssueLinks(closes=("1354",), part_of=())

    def test_closing_keyword_for_another_issue_is_defused(self) -> None:
        body = compose_body("## Summary\n- Also fixes: #7, and Resolves\n#8.", "42", is_jira=False)
        assert "Also `fixes: #7`, and `Resolves #8`." in body
        assert parse_issue_links(body).closes == ("42",)

    def test_closing_keyword_already_in_code_is_left_alone(self) -> None:
        sections = "## Summary\n- quotes `Closes #7` verbatim\n\n```text\nCloses #8\n```"
        body = compose_body(sections, "42", is_jira=False)
        assert "- quotes `Closes #7` verbatim\n\n```text\nCloses #8\n```" in body

    def test_jira_adds_link_once(self) -> None:
        body = compose_body("## Summary\n- stuff", "42", is_jira=True, jira_link="JIRA: https://x/42")
        assert body.count("JIRA: https://x/42") == 1

    def test_jira_strips_closing_keyword_llm_wrote(self) -> None:
        body = compose_body("## Summary\n- stuff\n\nCloses #42", "42", is_jira=True, jira_link="JIRA: https://x/42")
        assert "Closes #42" not in body
        assert "JIRA: https://x/42" in body

    def test_jira_neutralizes_inline_closing_keyword(self) -> None:
        """A JIRA body never carries a live GitHub closing keyword in any
        position: an inline mid-sentence mention still refers to a real
        GitHub issue number in this repo and would auto-close it on merge."""
        body = compose_body(
            "## Summary\n- This change fixes #123 in the dashboard parser.",
            "123",
            is_jira=True,
            jira_link="JIRA: https://x/123",
        )
        assert "fixes #123" not in body.lower()
        assert "closes #123" not in body.lower()
        assert "This change addresses #123 in the dashboard parser." in body
        assert "JIRA: https://x/123" in body

    @pytest.mark.parametrize(
        "description",
        [
            pytest.param("## Summary\n- item\n  ```python\n  x = 1", id="fence-open-in-list-item"),
            pytest.param("## Summary\n```python\nx = 1", id="fence-open-at-top-level"),
            pytest.param("## Summary\n1. step\n   ~~~\n   y", id="tilde-fence-open-in-numbered-item"),
        ],
    )
    def test_closing_line_is_never_inside_a_code_block(self, description: str) -> None:
        """GitHub ignores a closing keyword inside a code block, so a description cut off mid-fence must not
        swallow the ``## Issue`` section."""
        markdown_it = pytest.importorskip("markdown_it")
        body = compose_body(description, "42", is_jira=False)
        tokens = markdown_it.MarkdownIt().parse(body)
        assert not [t for t in tokens if t.type == "fence" and "Closes #42" in t.content]
        assert any(t.type == "inline" and "Closes #42" in t.content for t in tokens)


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
    """The prompt's issue section: it never asks for acceptance verdicts."""

    def test_github_issue_prompt_carries_the_issue_and_no_verdict_request(self, tmp_path: Path) -> None:
        prompt = build_pr_body_prompt(
            _ctx(tmp_path),
            task_title="Fix the bug",
            issue_body="## Acceptance Criteria\n- [ ] First thing works\n",
            commit_log="abc123 fix: thing",
            diff_stat=" src/app.py | 1 +",
            diff_content="diff --git a/src/app.py",
        )

        assert 'Do NOT use "Closes #42"' in prompt
        assert "Fix the bug" in prompt
        assert "ACCEPTANCE_VERDICTS" not in prompt
        # The bare "Output ONLY the markdown body ... no preamble" sentence must not appear verbatim
        # when a trailing title block is also requested: it contradicts the later instruction.
        assert "no preamble). Start directly" not in prompt
        assert "and nothing else" in prompt

    def test_issueless_run_has_no_closing_keyword(self, tmp_path: Path) -> None:
        """An issue-less run has nothing to close."""
        prompt = build_pr_body_prompt(
            _ctx(tmp_path, issue_number=""),
            task_title="Ad-hoc cleanup",
            issue_body="## Acceptance Criteria\n- [ ] Ignored for an issue-less run\n",
            commit_log="abc123 chore: cleanup",
            diff_stat="",
            diff_content="",
        )

        assert "Task: Ad-hoc cleanup" in prompt
        assert "Closes #" not in prompt
        assert TITLE_MARKER in prompt


class TestBuildPromptTitleRequest:
    """A PR title is requested for a non-JIRA project, never for JIRA."""

    def test_github_project_requests_a_title_scoped_to_configured_lists(self, tmp_path: Path) -> None:
        prompt = build_pr_body_prompt(
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
        prompt = build_pr_body_prompt(
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
        prompt = build_pr_body_prompt(
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

    def test_text_after_the_title_line_survives(self) -> None:
        """Only the marker and the title line are removed, not the rest of the response."""
        remainder, title = split_title_tail(
            f"## Summary\n- stuff\n{TITLE_MARKER}\nfeat(core): add widget\n## Test plan\n- t"
        )
        assert title == "feat(core): add widget"
        assert remainder == "## Summary\n- stuff\n\n## Test plan\n- t"

    def test_rejects_a_candidate_that_is_itself_another_marker(self) -> None:
        """A model that omits the title line leaves the next marker as the next non-blank line;
        consuming that as the "title" would destroy it."""
        raw = f"## Summary\n- stuff\n{TITLE_MARKER}\n\n<!-- OTHER -->\n- kept"
        remainder, title = split_title_tail(raw)

        assert title is None
        assert remainder == raw


class TestLineParsingIsWhitespaceTolerant:
    """The heading pattern is matched without lazy-quantifier backtracking, so its
    whitespace handling is pinned here rather than left to the regex's shape."""

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
        prompt = build_pr_body_prompt(
            _ctx(tmp_path),
            task_title="Rename",
            issue_body="",
            commit_log="abc rename",
            diff_stat=" a.png => b.png | 0",
            diff_content=truncate_diff("", True, 100),
        )
        assert "a.png => b.png" in prompt
        assert "(no changes detected)" in prompt


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
