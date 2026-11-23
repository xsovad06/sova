"""The closing handoff summary the /address-pr and /integrate-pr commands write (#1479): the run history reads it
as the run's result line, so it must merge into an existing handoff, create one when there is none, never touch
another issue's, and reach the file through `sova handoff-summary` with the free text on stdin."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sova.cli.app import app
from sova.dashboard.services.run_outcome import run_outcome
from sova.dashboard.services.work_state import run_action
from sova.ipc.handoff import ClosingSummaryError, read_handoff_file, record_closing_summary

COMMANDS = ("address-pr", "integrate-pr")
ROOT = Path(__file__).resolve().parent.parent
COMMANDS_DIR = ROOT / "sova" / "commands" / "data" / "commands"
# The canonical command and its two rendered copies (Claude Code commands, Codex skills).
COPIES = (
    lambda c: COMMANDS_DIR / f"{c}.md",
    lambda c: ROOT / ".claude" / "commands" / f"{c}.md",
    lambda c: ROOT / ".agents" / "skills" / f"sova-{c}" / "SKILL.md",
)
INVOCATION = {
    "address-pr": 'sova handoff-summary --source command:address-pr --issue "<ISSUE_NUMBER or empty>" '
    "--pr <PR_NUMBER> <<'SUMMARY'",
    "integrate-pr": 'sova handoff-summary --source command:integrate-pr --issue "<ISSUE_NUMBER or empty>" '
    "--pr <PR_NUMBER> <MERGED_FLAG> <<'SUMMARY'",
}


def _control(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "agent-control"


def _record(tmp_path: Path, command: str, issue: str, pr: int, summary: str, merged: bool | None = None) -> Path:
    return record_closing_summary(
        tmp_path, source=f"command:{command}", issue=issue, pr_number=pr, summary=summary, merged=merged
    )


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("command", COMMANDS)
class TestCommandWiring:
    def test_the_command_pipes_the_summary_through_a_quoted_heredoc(self, command: str) -> None:
        text = COPIES[0](command).read_text(encoding="utf-8")
        match = re.search(r"(sova handoff-summary [^\n]*)\n\s*<SUMMARY>\n\s*SUMMARY\n", text)
        assert match, command
        assert match.group(1) == INVOCATION[command]
        # No free text rides in a shell word: the summary is only ever the heredoc body.
        assert '"<SUMMARY>"' not in text

    def test_rendered_copies_carry_the_same_invocation(self, command: str) -> None:
        for copy in COPIES[1:]:
            assert INVOCATION[command] in copy(command).read_text(encoding="utf-8"), copy(command)

    def test_no_inline_python_copy_of_the_merge_is_left_in_the_command(self, command: str) -> None:
        for copy in COPIES:
            assert "python3 - " not in copy(command).read_text(encoding="utf-8").split("Closing summary")[-1]


def test_integrate_tells_the_agent_to_pass_not_merged_unless_it_merged() -> None:
    text = COPIES[0]("integrate-pr").read_text(encoding="utf-8")
    assert "`--merged` only when this run merged the PR, else `--not-merged`" in text


def test_integrate_records_the_summary_on_every_exit_and_before_the_report() -> None:
    text = COPIES[0]("integrate-pr").read_text(encoding="utf-8")
    assert "runs on **every** exit once the PR is known" in text
    assert "**before** printing the report" in text
    # The rule is stated where an agent meets a STOP, not only in the last phase.
    assert "**Every STOP records first.**" in text.split("### Phase 1:")[0]
    assert "run Phase 8 first" in text.split("## Error Recovery")[1].split("## Cross-References")[0]


def test_address_records_the_summary_before_the_report_and_on_every_exit() -> None:
    text = COPIES[0]("address-pr").read_text(encoding="utf-8")
    assert text.index("18. **Record the closing summary**") < text.index("19. **Print summary**")
    assert "**Every exit records a closing summary first**" in text.split("## Instructions")[0]
    assert "**Run this on every exit once the PR is known**" in text


@pytest.mark.parametrize("command", COMMANDS)
class TestClosingSummary:
    def test_it_creates_a_handoff_the_dashboard_reads(self, command: str, tmp_path: Path) -> None:
        _record(tmp_path, command, "42", 7, "Merged PR #7, issue closed")
        handoff = read_handoff_file(tmp_path, issue="42")
        assert handoff is not None
        assert (handoff.source, handoff.status, handoff.pr_number) == (f"command:{command}", "completed", 7)
        assert handoff.summary == handoff.details["summary"] == "Merged PR #7, issue closed"
        assert handoff.details["summary_source"] == f"command:{command}"
        # No command or step removes a handoff file, so what is left behind must be inert: nothing to act on.
        assert handoff.next_actions == []

    def test_it_merges_into_an_existing_handoff_and_keeps_its_next_actions(self, command: str, tmp_path: Path) -> None:
        control = _control(tmp_path)
        control.mkdir(parents=True)
        existing = {
            "source": "reviewer",
            "status": "awaiting_action",
            "issue": "42",
            "summary": "old",
            "details": {"next_action": "review", "pending_findings": [{"a": 1}]},
            "next_actions": [{"id": "go", "label": "Go", "auto_execute": True}],
        }
        (control / "handoff-42.json").write_text(json.dumps(existing), encoding="utf-8")
        _record(tmp_path, command, "42", 7, "Fixed 2 of 3 comments")
        data = _load(control / "handoff-42.json")
        assert data["summary"] == data["details"]["summary"] == "Fixed 2 of 3 comments"
        assert data["details"]["next_action"] == "review"
        assert data["details"]["pending_findings"] == [{"a": 1}]
        assert data["next_actions"] == existing["next_actions"]
        assert data["status"] == "awaiting_action"
        assert list(control.glob("*.tmp")) == []

    def test_a_matching_legacy_handoff_is_carried_into_the_issue_file(self, command: str, tmp_path: Path) -> None:
        control = _control(tmp_path)
        control.mkdir(parents=True)
        legacy = {
            "source": "reviewer",
            "status": "awaiting_action",
            "issue": "42",
            "next_actions": [{"id": "go", "label": "Go", "auto_execute": True}],
        }
        (control / "handoff.json").write_text(json.dumps(legacy), encoding="utf-8")
        _record(tmp_path, command, "42", 7, "Fixed 2 of 3 comments")
        data = _load(control / "handoff-42.json")
        assert data["next_actions"] == legacy["next_actions"]
        assert data["summary"] == "Fixed 2 of 3 comments"

    def test_another_issues_legacy_handoff_is_not_carried_into_the_issue_file(
        self, command: str, tmp_path: Path
    ) -> None:
        control = _control(tmp_path)
        control.mkdir(parents=True)
        other = {"source": "reviewer", "issue": "5", "next_actions": [{"id": "go"}]}
        (control / "handoff.json").write_text(json.dumps(other), encoding="utf-8")
        _record(tmp_path, command, "42", 7, "Merged PR #7")
        assert _load(control / "handoff-42.json").get("next_actions", []) == []
        assert _load(control / "handoff.json") == other

    def test_a_pr_with_no_issue_gets_its_own_file_and_leaves_the_legacy_one_alone(
        self, command: str, tmp_path: Path
    ) -> None:
        control = _control(tmp_path)
        control.mkdir(parents=True)
        other = json.dumps({"source": "reviewer", "status": "awaiting_action", "issue": "5", "summary": "keep"})
        (control / "handoff.json").write_text(other, encoding="utf-8")
        path = _record(tmp_path, command, "", 9, "Merged PR #9")
        assert path.name == "handoff-pr-9.json"
        assert _load(path)["pr_number"] == 9
        assert (control / "handoff.json").read_text(encoding="utf-8") == other

    def test_two_sequential_prs_with_no_issue_each_keep_their_own_summary_and_marker(
        self, command: str, tmp_path: Path
    ) -> None:
        """A Dependabot PR after another one must not be refused for the first one's handoff."""
        first = _record(tmp_path, command, "", 8, "Merged PR #8", merged=True)
        second = _record(tmp_path, command, "", 9, "Stopped: CI failing on PR #9", merged=False)
        assert first != second
        assert (_load(first)["details"]["summary"], _load(first)["details"]["merged"]) == ("Merged PR #8", True)
        assert (_load(second)["details"]["summary"], _load(second)["details"]["merged"]) == (
            "Stopped: CI failing on PR #9",
            False,
        )

    def test_the_same_prs_legacy_handoff_is_carried_into_its_file(self, command: str, tmp_path: Path) -> None:
        control = _control(tmp_path)
        control.mkdir(parents=True)
        legacy = {"status": "awaiting_action", "pr_number": 9, "next_actions": [{"id": "go"}]}
        (control / "handoff.json").write_text(json.dumps(legacy), encoding="utf-8")
        path = _record(tmp_path, command, "", 9, "Merged PR #9")
        assert _load(path)["status"] == "awaiting_action"
        assert _load(path)["next_actions"] == [{"id": "go"}]

    def test_an_unsafe_issue_is_refused(self, command: str, tmp_path: Path) -> None:
        with pytest.raises(ClosingSummaryError, match="not an issue number"):
            _record(tmp_path, command, "../etc", 9, "x")

    def test_an_empty_summary_is_refused(self, command: str, tmp_path: Path) -> None:
        with pytest.raises(ClosingSummaryError, match="empty"):
            _record(tmp_path, command, "1", 9, "  \n ")


def test_the_summary_is_one_line(tmp_path: Path) -> None:
    path = _record(tmp_path, "address-pr", "1", 2, "Fixed 3.\nSecond   line.")
    assert _load(path)["summary"] == "Fixed 3. Second line."


def test_integrate_records_whether_it_merged(tmp_path: Path) -> None:
    """The run history reads Merged only from an Integrate run that merged, not one that stopped at a gate."""
    path = _record(tmp_path, "integrate-pr", "42", 7, "Stopped: CI failing", merged=False)
    assert _load(path)["details"]["merged"] is False
    _record(tmp_path, "integrate-pr", "42", 7, "Merged PR #7", merged=True)
    assert _load(path)["details"]["merged"] is True


def test_address_pr_leaves_the_merged_marker_alone(tmp_path: Path) -> None:
    path = _record(tmp_path, "address-pr", "42", 7, "Fixed 1 comment")
    assert "merged" not in _load(path)["details"]


def test_a_command_summary_wins_over_counters_left_by_an_earlier_pipeline_run(tmp_path: Path) -> None:
    """The closing block merges into a file an earlier run wrote: its stale counts must not describe this run."""
    control = _control(tmp_path)
    control.mkdir(parents=True)
    stale = {
        "source": "reviewer",
        "status": "awaiting_action",
        "issue": "42",
        "details": {"next_action": "address_review", "addressed_findings": [{}, {}], "pending_findings": [{}]},
    }
    (control / "handoff-42.json").write_text(json.dumps(stale), encoding="utf-8")
    path = _record(tmp_path, "address-pr", "42", 7, "Fixed 1 of 3 comments, 2 need your input")
    details = _load(path)["details"]
    action = run_action("command:address-pr")
    assert run_outcome(action, "done", None, details, 7) == "Fixed 1 of 3 comments, 2 need your input"


class TestCli:
    def _run(self, tmp_path: Path, args: list[str], stdin: str) -> object:
        return CliRunner().invoke(app, ["handoff-summary", "--project", str(tmp_path), *args], input=stdin)

    def test_the_summary_comes_from_stdin_and_shell_metacharacters_are_kept_verbatim(self, tmp_path: Path) -> None:
        text = 'Fixed `foo` with $(echo pwned) and "quotes", $HOME stays'
        result = self._run(tmp_path, ["--source", "command:address-pr", "--issue", "42", "--pr", "7"], text + "\n")
        assert result.exit_code == 0, result.output
        assert _load(_control(tmp_path) / "handoff-42.json")["summary"] == text

    def test_a_hash_prefixed_pr_number_is_accepted(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, ["--source", "command:integrate-pr", "--pr", "#524", "--merged"], "Merged PR #524")
        assert result.exit_code == 0, result.output
        details = _load(_control(tmp_path) / "handoff-pr-524.json")["details"]
        assert details["merged"] is True

    @pytest.mark.parametrize("pr", ["", "abc", "0"])
    def test_a_bad_pr_number_exits_with_a_clear_message(self, tmp_path: Path, pr: str) -> None:
        result = self._run(tmp_path, ["--source", "command:address-pr", "--pr", pr], "x")
        assert result.exit_code == 1
        assert "not a PR number" in result.output
        assert "Traceback" not in result.output
        assert not _control(tmp_path).exists()

    def test_a_source_that_is_not_a_command_is_refused(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, ["--source", "reviewer", "--pr", "7"], "x")
        assert result.exit_code == 1
        assert "not a command source" in result.output

    def test_not_merged_is_recorded_as_false(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, ["--source", "command:integrate-pr", "--pr", "7", "--not-merged"], "Stopped")
        assert result.exit_code == 0, result.output
        assert _load(_control(tmp_path) / "handoff-pr-7.json")["details"]["merged"] is False
