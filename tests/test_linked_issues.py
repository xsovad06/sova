"""Tests for closing only the issues a merged PR closes (#1340).

Covers parse_issue_links() (sova/utils/gh.py), the `sova linked-issues` CLI that
/integrate-pr and /after-merge call, and the command text that tells them to.
handle_merged_pr_issues() itself is tested in tests/test_merge.py.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sova.adapters.base import TaskState
from sova.cli.app import app
from sova.git.merge import MergedIssues, OpenedPrIssues
from sova.utils.gh import IssueLinks, parse_issue_links
from sova.utils.shell import ShellResult

REPO_ROOT = Path(__file__).parent.parent
runner = CliRunner()


class TestParseIssueLinks:
    @pytest.mark.parametrize(
        "keyword", ["close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved"]
    )
    def test_every_github_closing_keyword_closes(self, keyword: str) -> None:
        assert parse_issue_links(f"{keyword.capitalize()} #42") == IssueLinks(closes=("42",), part_of=())

    def test_colon_after_the_keyword_still_closes(self) -> None:
        assert parse_issue_links("Fixes: #42").closes == ("42",)

    def test_part_of_never_closes(self) -> None:
        body = "## Summary\n- work\n\n## Issue\nPart of #779\n\nNot done in this PR:\n- host checks"
        assert parse_issue_links(body) == IssueLinks(closes=(), part_of=("779",))

    @pytest.mark.parametrize(
        "body",
        ["Related: #12", "See #12 and #13", "#12", "Follow-up to #12", "bugfix #12", "disclosed #12", "Fixes 12"],
    )
    def test_a_bare_or_non_keyword_reference_is_no_link(self, body: str) -> None:
        assert parse_issue_links(body) == IssueLinks(closes=(), part_of=())

    def test_no_reference(self) -> None:
        assert parse_issue_links("") == IssueLinks(closes=(), part_of=())

    def test_multiple_references_keep_order_without_duplicates(self) -> None:
        body = "Closes #3, fixes #1 and resolves #3.\n\n## Issue\nPart of #9\nPart of #8\nPart of #9"
        assert parse_issue_links(body) == IssueLinks(closes=("3", "1"), part_of=("9", "8"))

    def test_an_issue_both_closed_and_part_of_counts_as_closed(self) -> None:
        assert parse_issue_links("Part of #5\nCloses #5") == IssueLinks(closes=("5",), part_of=())

    def test_part_of_is_read_from_the_issue_section_when_there_is_one(self) -> None:
        """An epic mention in the summary must not get a "Part of" comment meant for the PR's own issue."""
        body = "## Summary\nPart of #1335, the efficiency epic.\n\n## Issue\nCloses #1340"
        assert parse_issue_links(body) == IssueLinks(closes=("1340",), part_of=())

    def test_part_of_is_read_from_the_whole_body_without_an_issue_section(self) -> None:
        assert parse_issue_links("Some summary.\n\nPart of #7").part_of == ("7",)

    def test_closing_keywords_count_anywhere_in_the_body(self) -> None:
        """GitHub closes from any section, so the whole body is read for them."""
        body = "## Summary\nCloses #4\n\n## Issue\nPart of #5"
        assert parse_issue_links(body) == IssueLinks(closes=("4",), part_of=("5",))

    def test_code_spans_and_fenced_blocks_are_not_links(self) -> None:
        body = "A body with `Closes #12` stays open.\n\n```text\nFixes #13\nPart of #14\n```\n\n## Issue\nCloses #15"
        assert parse_issue_links(body) == IssueLinks(closes=("15",), part_of=())

    @pytest.mark.parametrize(
        ("body", "closes"),
        [
            ("A body with `Closes #12` in backticks does not close #12.", ("12",)),
            ("Doesn't fix #7 yet", ("7",)),
            ("Not done in this PR:\n- Fix #3 regression", ("3",)),
        ],
    )
    def test_a_negated_or_quoted_keyword_phrase_is_still_a_closing_link(
        self, body: str, closes: tuple[str, ...]
    ) -> None:
        """The parser is at parity with GitHub, which closes on this text and ignores negation.

        Skipping it here would not prevent the close, so a PR body must phrase around it ("stays
        open") or put the phrase in a code span. This pins the limitation the docstring states.
        """
        assert parse_issue_links(body) == IssueLinks(closes=closes, part_of=())


def _invoke(*args: str) -> object:
    return runner.invoke(app, ["linked-issues", *args])


def _config(task_source: str = "github") -> MagicMock:
    cfg = MagicMock()
    cfg.github_repo = "o/r"
    cfg.github_user = "me"
    cfg.task_source.type = task_source
    cfg.integration.post_merge_state = "done"
    return cfg


def _patched(*, cfg: MagicMock | None = None, pr_state: str = "MERGED", **merge_patches: object) -> list:
    """Patch everything `sova linked-issues` reaches: config, gh auth, the PR-state check and merge helpers."""
    state_result = ShellResult(returncode=0, stdout=f"{pr_state}\n", stderr="")
    patches = [
        patch("sova.config.loader.load_config", return_value=cfg or _config()),
        patch("sova.git.worktree.get_primary_worktree_root", new_callable=AsyncMock, return_value=REPO_ROOT),
        patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value={}),
        patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=state_result),
        patch("sova.adapters.create_adapter", return_value=MagicMock()),
    ]
    patches += [patch(f"sova.git.merge.{name}", new=value) for name, value in merge_patches.items()]
    return patches


def _run_with(patches: list, *args: str) -> object:
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return _invoke(*args)


class TestLinkedIssuesCli:
    def test_prints_the_links_as_json(self) -> None:
        links = AsyncMock(return_value=IssueLinks(closes=("1340",), part_of=("1228",)))
        result = _run_with(_patched(read_issue_links=links), "1343")

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"closes": ["1340"], "part_of": ["1228"]}

    def test_unreadable_body_exits_non_zero(self) -> None:
        result = _run_with(_patched(read_issue_links=AsyncMock(return_value=None)), "1343")
        assert result.exit_code == 1

    def test_apply_prints_one_line_per_issue(self) -> None:
        outcome = MergedIssues(transitioned=["1340"], commented=["1228"])
        handle = AsyncMock(return_value=outcome)
        result = _run_with(_patched(handle_merged_pr_issues=handle), "884", "--apply")

        assert result.exit_code == 0
        assert result.stdout.splitlines() == outcome.describe("done")
        assert handle.await_args.args == (884,)
        assert handle.await_args.kwargs["post_merge_state"] == "done"

    def test_apply_with_nothing_linked_says_so(self) -> None:
        handle = AsyncMock(return_value=MergedIssues())
        result = _run_with(_patched(handle_merged_pr_issues=handle), "884", "--apply")

        assert result.exit_code == 0
        assert "closes no issue" in result.stdout

    def test_apply_exits_non_zero_when_an_issue_was_not_updated(self) -> None:
        handle = AsyncMock(return_value=MergedIssues(transitioned=["1"], failed=["2"]))
        result = _run_with(_patched(handle_merged_pr_issues=handle), "884", "--apply")

        assert result.exit_code == 1
        assert "issue #2 not updated (see log)" in result.stdout

    def test_apply_exits_non_zero_when_the_body_is_unreadable(self) -> None:
        handle = AsyncMock(return_value=None)
        result = _run_with(_patched(handle_merged_pr_issues=handle), "884", "--apply")
        assert result.exit_code == 1

    def test_apply_refuses_an_unmerged_pr(self) -> None:
        handle = AsyncMock()
        result = _run_with(_patched(pr_state="OPEN", handle_merged_pr_issues=handle), "884", "--apply")

        assert result.exit_code == 1
        handle.assert_not_awaited()

    def test_apply_refuses_a_jira_project(self) -> None:
        handle = AsyncMock()
        result = _run_with(_patched(cfg=_config("jira"), handle_merged_pr_issues=handle), "884", "--apply")

        assert result.exit_code == 2
        handle.assert_not_awaited()


class TestLinkedIssuesMarkInReview:
    """`sova linked-issues <PR> --mark-in-review`, which /pr runs once the PR is open (#1281 follow-up)."""

    def test_prints_one_line_per_issue(self) -> None:
        outcome = OpenedPrIssues(moved=["1281"], left=[("7", TaskState.HUMAN_ONLY)])
        mark = AsyncMock(return_value=outcome)
        result = _run_with(_patched(pr_state="OPEN", mark_pr_issues_in_review=mark), "1389", "--mark-in-review")

        assert result.exit_code == 0
        assert result.stdout.splitlines() == outcome.describe()
        assert mark.await_args.args == (1389,)
        assert mark.await_args.kwargs["repo"] == "o/r"

    def test_with_nothing_linked_says_so(self) -> None:
        mark = AsyncMock(return_value=OpenedPrIssues())
        result = _run_with(_patched(pr_state="OPEN", mark_pr_issues_in_review=mark), "1389", "--mark-in-review")

        assert result.exit_code == 0
        assert "closes no issue" in result.stdout

    def test_exits_non_zero_when_an_issue_was_not_updated(self) -> None:
        mark = AsyncMock(return_value=OpenedPrIssues(moved=["1"], failed=["2"]))
        result = _run_with(_patched(pr_state="OPEN", mark_pr_issues_in_review=mark), "1389", "--mark-in-review")

        assert result.exit_code == 1
        assert "issue #2 not updated (see log)" in result.stdout

    def test_exits_non_zero_when_the_body_is_unreadable(self) -> None:
        mark = AsyncMock(return_value=None)
        result = _run_with(_patched(pr_state="OPEN", mark_pr_issues_in_review=mark), "1389", "--mark-in-review")
        assert result.exit_code == 1

    @pytest.mark.parametrize("pr_state", ["MERGED", "CLOSED", ""])
    def test_refuses_a_pr_that_is_not_open(self, pr_state: str) -> None:
        mark = AsyncMock()
        result = _run_with(_patched(pr_state=pr_state, mark_pr_issues_in_review=mark), "1389", "--mark-in-review")

        assert result.exit_code == 1
        mark.assert_not_awaited()

    def test_a_gh_failure_reason_reaches_the_user(self) -> None:
        mark = AsyncMock()
        patches = _patched(mark_pr_issues_in_review=mark)
        failed = ShellResult(returncode=1, stdout="", stderr="HTTP 401: Bad credentials")
        patches[3] = patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=failed)
        result = _run_with(patches, "1389", "--mark-in-review")

        assert result.exit_code == 1
        assert "HTTP 401: Bad credentials" in result.output
        mark.assert_not_awaited()

    def test_a_jira_project_is_skipped_without_failing(self) -> None:
        """/pr runs the step on every project, so a Jira one must not fail the command."""
        mark = AsyncMock()
        result = _run_with(
            _patched(cfg=_config("jira"), pr_state="OPEN", mark_pr_issues_in_review=mark), "1389", "--mark-in-review"
        )

        assert result.exit_code == 0
        mark.assert_not_awaited()

    def test_reads_the_primary_checkout_config_from_a_worktree(self) -> None:
        """/pr runs in a worktree, which has no sova.db: its config would have no repo and the step would fail."""
        mark = AsyncMock(return_value=OpenedPrIssues())
        loader = MagicMock(return_value=_config())
        primary = AsyncMock(return_value=Path("/primary"))
        patches = [
            *_patched(pr_state="OPEN", mark_pr_issues_in_review=mark),
            patch("sova.config.loader.load_config", new=loader),
            patch("sova.git.worktree.get_primary_worktree_root", new=primary),
        ]
        result = _run_with(patches, "1389", "--mark-in-review")

        assert result.exit_code == 0
        loader.assert_called_once_with(Path("/primary"))

    def test_cannot_be_combined_with_apply(self) -> None:
        mark, handle = AsyncMock(), AsyncMock()
        patches = _patched(pr_state="OPEN", mark_pr_issues_in_review=mark, handle_merged_pr_issues=handle)
        result = _run_with(patches, "1389", "--mark-in-review", "--apply")

        assert result.exit_code == 2
        mark.assert_not_awaited()
        handle.assert_not_awaited()


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


_PHASE_6 = re.compile(r"### Phase 6:.*?### Phase 7:", re.DOTALL)


class TestCommandsCloseOnlyOnClosingKeywords:
    @pytest.mark.parametrize(
        "path", ["sova/commands/data/commands/integrate-pr.md", ".claude/commands/integrate-pr.md"]
    )
    def test_integrate_pr_phase_6_applies_the_cli(self, path: str) -> None:
        phase_6 = _PHASE_6.search(_read(path)).group(0)
        assert "sova linked-issues <PR_NUMBER> --apply" in phase_6
        assert "`Part of #N`**: the issue stays open" in phase_6

    @pytest.mark.parametrize(
        "path", ["sova/commands/data/commands/integrate-pr.md", ".claude/commands/integrate-pr.md"]
    )
    def test_integrate_pr_never_closes_an_issue_itself(self, path: str) -> None:
        assert "gh issue close" not in _read(path)

    @pytest.mark.parametrize(
        "path", ["sova/commands/data/commands/integrate-pr.md", ".claude/commands/integrate-pr.md"]
    )
    def test_integrate_pr_has_no_title_fallback(self, path: str) -> None:
        text = _read(path)
        assert "or title (`#N`)" not in text
        assert "A bare `#N` in the body or the title is a mention, not a link." in text

    @pytest.mark.parametrize(
        "path",
        [
            "sova/commands/data/commands/pr.md",
            ".claude/commands/pr.md",
            ".agents/skills/sova-pr/SKILL.md",
            "plugins/sova/commands/pr.md",
        ],
    )
    def test_pr_marks_the_linked_issues_in_review_after_creating_the_pr(self, path: str) -> None:
        step_11 = re.search(r"11\. \*\*Create or update PR\*\*.*?\n12\. ", _read(path), re.DOTALL).group(0)
        create = step_11.index("gh pr create")
        assert step_11.index('sova linked-issues "$PR_NUM" --mark-in-review') > create

    @pytest.mark.parametrize(
        "path",
        [
            "sova/commands/data/commands/pr.md",
            ".claude/commands/pr.md",
            ".agents/skills/sova-pr/SKILL.md",
            "plugins/sova/commands/pr.md",
        ],
    )
    def test_pr_looks_the_pr_up_in_the_configured_repository(self, path: str) -> None:
        step_11 = re.search(r"11\. \*\*Create or update PR\*\*.*?\n12\. ", _read(path), re.DOTALL).group(0)
        assert '--repo "$REPO"' in step_11
        assert 'sova config get github_repo --project "$PRIMARY"' in step_11
        assert 'test -n "$REPO"' in step_11

    @pytest.mark.parametrize(
        "path",
        [
            "sova/commands/data/commands/pr.md",
            ".claude/commands/pr.md",
            ".agents/skills/sova-pr/SKILL.md",
            "plugins/sova/commands/pr.md",
        ],
    )
    def test_pr_stops_before_marking_issues_when_create_or_edit_fails(self, path: str) -> None:
        step_11 = re.search(r"11\. \*\*Create or update PR\*\*.*?\n12\. ", _read(path), re.DOTALL).group(0)
        for verb in ("create", "edit"):
            line = step_11[step_11.index(f"gh pr {verb}") :].split("\n\n")[0]
            assert f"PR {verb} failed: STOP" in line

    @pytest.mark.parametrize("path", ["sova/commands/data/commands/after-merge.md", ".claude/commands/after-merge.md"])
    def test_after_merge_applies_the_cli(self, path: str) -> None:
        text = _read(path)
        assert "sova linked-issues <PR_NUMBER> --apply" in text
        assert "gh issue close" not in text


_PY_HEREDOC = re.compile(r"<<'PY'\n(.*?)\n\s*PY\n", re.DOTALL)


class TestPrCommandStopsOnStrayClosingReference:
    """/pr step 9 runs parse_issue_links() on the body file before push or create (#1354)."""

    @staticmethod
    def _run_check(
        path: str, tmp_path: Path, body: str, issue: str, *, hide_sova: bool = False
    ) -> subprocess.CompletedProcess:
        scripts = [m for m in _PY_HEREDOC.findall(_read(path)) if "parse_issue_links" in m]
        assert len(scripts) == 1, f"expected exactly one closing-reference check in {path}"
        script = scripts[0]
        if hide_sova:
            # A None entry in sys.modules makes the import raise ImportError, as a python3 without the install does.
            script = 'import sys\nsys.modules["sova.utils.gh"] = None\n' + script
        script = "\n".join(line[3:] if line.startswith("   ") else line for line in script.splitlines())
        body_file = tmp_path / "body.md"
        body_file.write_text(body, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-c", script, str(body_file), issue],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
        )

    @pytest.mark.parametrize(
        "path",
        [
            "sova/commands/data/commands/pr.md",
            ".claude/commands/pr.md",
            ".agents/skills/sova-pr/SKILL.md",
            "plugins/sova/commands/pr.md",
        ],
    )
    def test_pr_1351_body_is_stopped(self, path: str, tmp_path: Path) -> None:
        body = '## Review guidance\n- "does not close #12" still closes #12.\n\n## Issue\n\nCloses #1340\n'
        result = self._run_check(path, tmp_path, body, "1340")
        assert result.returncode == 1
        assert "#12" in result.stdout

    @pytest.mark.parametrize(
        ("body", "issue"),
        [
            ("## Issue\n\nCloses #1340\n", "1340"),
            ("## Issue\n\nCloses #1340\n", "#1340"),
            ("Quoted `Closes #12` stays open.\n\n## Issue\n\nPart of #1340\n", "1340"),
            ("Quoted `Closes #12` stays open.\n", ""),
        ],
    )
    def test_safe_body_prints_nothing(self, body: str, issue: str, tmp_path: Path) -> None:
        result = self._run_check("sova/commands/data/commands/pr.md", tmp_path, body, issue)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_closing_reference_without_an_issue_argument_is_stopped(self, tmp_path: Path) -> None:
        """With no issue given step 8 writes no closing link, so any closing reference is stray."""
        result = self._run_check("sova/commands/data/commands/pr.md", tmp_path, "Closes #12\n", "")
        assert result.returncode == 1
        assert result.stdout.strip() == "STOP: the PR body would close #12, but no issue was given"

    def test_unimportable_sova_is_a_distinct_skip_not_a_stop(self, tmp_path: Path) -> None:
        """A python3 that cannot see the sova install (an active virtualenv) must not look like a stray close."""
        result = self._run_check(
            "sova/commands/data/commands/pr.md",
            tmp_path,
            "## Issue\n\nCloses #1340\n",
            "1340",
            hide_sova=True,
        )
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr.startswith("SKIP:")
