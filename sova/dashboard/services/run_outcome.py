"""The one-line result of a finished run, for the run history.

Built from the run's status, its error message and the handoff it left (``TaskRun.handoff_json``). A handoff that is
missing, unreadable or of an unexpected shape never raises and never yields an empty line: the run falls back to a
generic text for its action.
"""

from __future__ import annotations

import re

from sova.dashboard.services.work_state import RunAction
from sova.ipc.handoff import COMMAND_SOURCE_PREFIX, SUMMARY_SOURCE_KEY

# A reviewer handoff's ``next_action`` that is not "no blocking defects" and not "address the review".
_REVIEW_GAP_TEXT = {
    "review_incomplete": "Review incomplete, some files were not reviewed",
    "needs_human_review": "Needs a human review",
    "review_post_failed": "Review could not be posted to the PR",
}

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")

ADDRESS_REVIEW_NEXT_ACTION = "address_review"


def _one_line(text: object) -> str:
    return " ".join(str(text or "").split())


def first_sentence(text: object) -> str:
    """The first sentence of ``text`` on one line, or an empty string."""
    return _SENTENCE_END.split(_one_line(text), maxsplit=1)[0]


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _count(handoff: dict, key: str) -> int:
    value = handoff.get(key)
    return len(value) if isinstance(value, list) else 0


def _written_by_command(handoff: object) -> bool:
    """Whether a command (/address-pr, /integrate-pr) wrote the closing summary in this handoff.

    The command merges its summary into the file an earlier pipeline run left, so counters and a ``next_action``
    in it may describe that earlier run, not this one.
    """
    source = handoff.get(SUMMARY_SOURCE_KEY) if isinstance(handoff, dict) else None
    return isinstance(source, str) and source.startswith(COMMAND_SOURCE_PREFIX)


def next_action_of(handoff: object) -> str:
    """The handoff's ``next_action``, or an empty string when there is no readable one (or a command wrote it)."""
    if _written_by_command(handoff):
        return ""
    value = handoff.get("next_action") if isinstance(handoff, dict) else None
    return value if isinstance(value, str) else ""


def _review_outcome(handoff: dict) -> str:
    next_action = next_action_of(handoff)
    if next_action == ADDRESS_REVIEW_NEXT_ACTION:
        head = f"Changes requested, {_plural(_count(handoff, 'pending_findings'), 'finding')}."
        summary = first_sentence(handoff.get("summary"))
        return f"{head} {summary}" if summary else head
    if next_action in _REVIEW_GAP_TEXT:
        return _REVIEW_GAP_TEXT[next_action]
    # A reviewer that finished with any other next action (``approve``, or one this table does not know) found
    # nothing blocking; with no next action at all there is nothing to say.
    return "No blocking defects" if next_action else "Review finished"


def _pr_number(handoff: dict, pr_number: int | None) -> int | None:
    value = handoff.get("pr_number")
    return value if isinstance(value, int) and value > 0 else pr_number


def _done_outcome(action: RunAction, handoff: object, pr_number: int | None) -> str:
    """The line for a run that finished: by action, from the handoff when it left a usable one."""
    data = handoff if isinstance(handoff, dict) else {}
    generic = f"{action.label} finished"
    if action.key == "develop":
        pr = _pr_number(data, pr_number)
        return f"Opened PR #{pr}" if pr else generic
    if action.key == "rebase":
        pr = _pr_number(data, pr_number)
        return f"Rebased PR #{pr} onto main" if pr else "Rebased"
    if not data:
        return generic
    if _written_by_command(data):
        return _one_line(data.get("summary")) or generic
    if action.key == "review":
        return _review_outcome(data)
    if action.key == "address-review":
        fixed = _count(data, "addressed_findings")
        if fixed:
            return f"Fixed {_plural(fixed, 'finding')}, sent back for re-review"
    return _one_line(data.get("summary")) or generic


def run_outcome(
    action: RunAction,
    status: str,
    error_message: str | None,
    handoff: object,
    pr_number: int | None = None,
) -> str:
    """One line saying what the run achieved, or why it did not."""
    if status == "failed":
        return _one_line(error_message) or "Failed"
    approval = status == "awaiting_approval"
    if action.key == "research" and (approval or status == "done"):
        return "Spec ready, waiting for your approval"
    if approval:
        return "Waiting for your approval"
    if status != "done":
        return status.replace("_", " ").capitalize()
    return _done_outcome(action, handoff, pr_number)
