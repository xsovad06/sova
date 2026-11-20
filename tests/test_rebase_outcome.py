"""The Work card's line for a finished rebase: why it failed, from its cause, and the action that fixes it."""

from __future__ import annotations

import pytest

from sova.dashboard.services.rebase_outcome import _FAILURES, describe_rebase_crash, describe_rebase_result
from sova.git.rebase import RebaseFailure
from sova.supervisor.rebase import AutoRebaseFailure

_SECRET = "fatal: could not read Username for 'https://github.com': token=ghp_secret"


def _failed(cause: str, **extra: object) -> dict:
    return {"status": "failed", "pr_number": 77, "error": _SECRET, "cause": cause, **extra}


class TestEveryCauseIsExplained:
    @pytest.mark.parametrize("cause", [*RebaseFailure, *AutoRebaseFailure])
    def test_each_cause_has_its_own_explanation(self, cause: str) -> None:
        """A new cause value must get an entry: the unexpected fallback would hide what actually went wrong."""
        assert cause in _FAILURES

        outcome = describe_rebase_result(_failed(cause), issue_number=42)

        assert outcome.ok is False
        assert outcome.message.startswith("Rebase failed: ")

    @pytest.mark.parametrize("cause", [*RebaseFailure, *AutoRebaseFailure, "", "no-such-cause"])
    def test_the_error_text_never_reaches_the_card(self, cause: str) -> None:
        outcome = describe_rebase_result(_failed(cause), issue_number=42)

        assert "token" not in outcome.message
        assert "fatal" not in outcome.message


class TestFixActions:
    @pytest.mark.parametrize(
        "cause",
        [RebaseFailure.CONFLICTS, RebaseFailure.COMMIT_CAP, RebaseFailure.GIT, AutoRebaseFailure.CHECKS],
    )
    def test_what_an_auto_rebase_cannot_get_past_is_fixed_by_address_pr(self, cause: str) -> None:
        outcome = describe_rebase_result(_failed(cause), issue_number=42)

        assert outcome.fix is not None
        assert (outcome.fix["label"], outcome.fix["handler"]) == ("Address PR", "run_command")
        assert outcome.fix["handler_args"] == {"command": "address-pr", "pr": 77, "issue": "42"}

    @pytest.mark.parametrize(
        "cause",
        [
            RebaseFailure.FETCH,
            RebaseFailure.RESOLVER,
            RebaseFailure.WORKTREE_FILES,
            AutoRebaseFailure.PR_LOOKUP,
            AutoRebaseFailure.SETUP,
            AutoRebaseFailure.WORKTREE_CHECK,
            AutoRebaseFailure.PUSH,
            AutoRebaseFailure.UNEXPECTED,
        ],
    )
    def test_a_transient_failure_is_fixed_by_a_retry(self, cause: str) -> None:
        outcome = describe_rebase_result(_failed(cause), issue_number=42)

        assert outcome.fix is not None
        assert (outcome.fix["label"], outcome.fix["handler"]) == ("Retry rebase", "trigger_rebase")
        assert outcome.fix["handler_args"] == {"issue": "42"}

    def test_address_pr_falls_back_to_a_retry_without_a_pr(self) -> None:
        outcome = describe_rebase_result(
            {"status": "failed", "pr_number": None, "cause": RebaseFailure.CONFLICTS}, issue_number=42
        )

        assert outcome.fix is not None
        assert outcome.fix["handler"] == "trigger_rebase"

    def test_changes_left_in_the_stash_offer_no_button(self) -> None:
        """Only the user can restore the stashed changes; a retry would stash on top of them."""
        outcome = describe_rebase_result(_failed(RebaseFailure.STASH), issue_number=42)

        assert outcome.fix is None
        assert "git stash list" in outcome.message


class TestMessages:
    def test_unresolved_conflicts_name_the_files(self) -> None:
        outcome = describe_rebase_result(
            _failed(RebaseFailure.CONFLICTS, unresolved_files=["a.py", "b.py"]), issue_number=42
        )

        assert "a.py, b.py" in outcome.message

    def test_a_long_file_list_is_counted(self) -> None:
        files = [f"f{i}.py" for i in range(5)]
        outcome = describe_rebase_result(_failed(RebaseFailure.CONFLICTS, unresolved_files=files), issue_number=42)

        assert "f0.py, f1.py, f2.py (and 2 more)" in outcome.message
        assert "f3.py" not in outcome.message

    def test_a_failure_without_a_cause_is_unexpected(self) -> None:
        outcome = describe_rebase_result({"status": "failed", "error": _SECRET}, issue_number=42)

        assert "server log has the details" in outcome.message
        assert outcome.fix is not None
        assert outcome.fix["handler"] == "trigger_rebase"

    def test_success_and_skip(self) -> None:
        assert describe_rebase_result({"status": "success"}, issue_number=42).message == "Rebased"
        skipped = describe_rebase_result({"status": "skipped", "reason": "Branch HEAD changed"}, issue_number=42)
        assert (skipped.ok, skipped.message, skipped.fix) == (True, "Rebase skipped: Branch HEAD changed", None)

    def test_a_crash_before_the_rebase_ran_offers_a_retry(self) -> None:
        outcome = describe_rebase_crash(42)

        assert outcome.ok is False
        assert outcome.fix is not None
        assert outcome.fix["handler"] == "trigger_rebase"
