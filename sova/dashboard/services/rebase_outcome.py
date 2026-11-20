"""What a finished Work-card rebase tells the user: whether it worked, why not, and the action that fixes it.

``attempt_auto_rebase()`` reports a failure's ``cause`` (``AutoRebaseFailure`` for the stages around the rebase,
``RebaseFailure`` for the rebase itself). Each cause has one fixed explanation written here, and the card's fix
button runs the action that remedies it: a retry for a transient failure, ``/address-pr`` (an agent that rebases,
resolves conflicts and fixes failing checks) for one the auto-rebase cannot get past. The result's ``error`` is
never shown: it can carry exception text or git output, which only the server log records.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sova.dashboard.services.work_state import address_pr_action, retry_rebase_action
from sova.git.rebase import RebaseFailure
from sova.supervisor.rebase import REBASE_MAX_COMMITS, AutoRebaseFailure

# The most conflicted files a message names; the rest are counted.
_MAX_FILES_NAMED = 3

_Fix = Literal["retry", "address", "none"]

_UNEXPECTED: tuple[str, _Fix] = ("something unexpected went wrong; the server log has the details. Retry.", "retry")

# cause -> (why it failed and how to fix it, the fix button). ``address`` falls back to ``retry`` without a PR.
_FAILURES: dict[str, tuple[str, _Fix]] = {
    RebaseFailure.CONFLICTS: (
        "SOVA could not resolve the conflicts in {files}. Address PR resolves them with an agent that reads the PR.",
        "address",
    ),
    RebaseFailure.RESOLVER: (
        "the model resolving the conflicts did not answer. Retry; if it fails again, Address PR resolves them.",
        "retry",
    ),
    RebaseFailure.COMMIT_CAP: (
        f"the branch has more commits than an auto-rebase replays ({REBASE_MAX_COMMITS}). "
        "Address PR squashes and rebases it.",
        "address",
    ),
    RebaseFailure.FETCH: (
        "the base branch could not be fetched from GitHub. Check the network and the GitHub sign-in on the "
        "Connections page, then retry.",
        "retry",
    ),
    RebaseFailure.WORKTREE_FILES: (
        "untracked SOVA runtime files in the branch's worktree are in the way. Move or remove them (the server log "
        "names them), then retry.",
        "retry",
    ),
    RebaseFailure.STASH: (
        "the uncommitted changes in the branch's worktree could not be put back and are kept in git stash. Find "
        "the entry with git stash list, apply it in that worktree, then retry.",
        "none",
    ),
    RebaseFailure.GIT: (
        "git stopped the rebase for a reason other than a conflict. Address PR rebases it with an agent.",
        "address",
    ),
    AutoRebaseFailure.PR_LOOKUP: (
        "GitHub could not be asked for this issue's PR. Check the GitHub sign-in on the Connections page, then retry.",
        "retry",
    ),
    AutoRebaseFailure.SETUP: (
        "the project config or the database could not be read. Check the Settings page, then retry.",
        "retry",
    ),
    AutoRebaseFailure.WORKTREE_CHECK: (
        "SOVA could not check whether an agent is using the branch's worktree, so it left it alone. Retry.",
        "retry",
    ),
    AutoRebaseFailure.CHECKS: (
        "the branch rebased cleanly, but the pre-push checks failed on the result, so nothing was pushed. Address "
        "PR rebases it again and fixes what fails.",
        "address",
    ),
    AutoRebaseFailure.PUSH: (
        "GitHub refused the push of the rebased branch. Retry; if it is refused again, check the branch protection "
        "rules and the GitHub sign-in.",
        "retry",
    ),
    AutoRebaseFailure.UNEXPECTED: _UNEXPECTED,
}


@dataclass(frozen=True)
class RebaseOutcome:
    """The card's result line for a rebase: ``ok``, the text it shows, and the action its fix button runs."""

    ok: bool
    message: str
    fix: dict | None = None


def describe_rebase_result(result: dict, *, issue_number: int) -> RebaseOutcome:
    """The outcome of ``attempt_auto_rebase()``'s answer *result* for the card of *issue_number*."""
    status = str(result.get("status") or "")
    if status == "success":
        return RebaseOutcome(ok=True, message="Rebased")
    if status == "skipped":
        # A skip's reason is written by the rebase module itself (no exception text), and names what to wait for.
        reason = str(result.get("reason") or "")
        return RebaseOutcome(ok=True, message=f"Rebase skipped: {reason}" if reason else "Rebase skipped")
    template, fix = _FAILURES.get(str(result.get("cause") or ""), _UNEXPECTED)
    message = template.format(files=_named_files(result.get("unresolved_files") or []))
    pr_number = result.get("pr_number")
    return RebaseOutcome(ok=False, message=f"Rebase failed: {message}", fix=_fix_action(fix, issue_number, pr_number))


def describe_rebase_crash(issue_number: int) -> RebaseOutcome:
    """The outcome when the rebase could not even be run (the dashboard's own call raised)."""
    template, fix = _UNEXPECTED
    return RebaseOutcome(ok=False, message=f"Rebase failed: {template}", fix=_fix_action(fix, issue_number, None))


def _named_files(files: list[str]) -> str:
    if not files:
        return "the PR"
    named = ", ".join(files[:_MAX_FILES_NAMED])
    rest = len(files) - _MAX_FILES_NAMED
    return f"{named} (and {rest} more)" if rest > 0 else named


def _fix_action(fix: _Fix, issue_number: int, pr_number: object) -> dict | None:
    if fix == "none":
        return None
    if fix == "address" and isinstance(pr_number, int) and pr_number > 0:
        return address_pr_action(issue_number, pr_number)
    return retry_rebase_action(issue_number)
