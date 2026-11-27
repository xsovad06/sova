"""Every sentence the activity feed says, from fixed templates: no model is called.

The voice is first person, brief and plain. This is the one file to review when the wording changes; the service
(``feed_activity_service``) decides what to say, and this module decides how. A sentence never carries an emoji or
a double dash. Numbers come in already counted; nothing here reads the database.
"""

from __future__ import annotations

import re

from sova.dashboard.services.run_outcome import first_sentence
from sova.dashboard.services.work_state import HistoryState

# A reason (an error message, a watchdog signal) is cut here so one long traceback line cannot fill a feed line.
_REASON_MAX = 140
NEED_YOU = "I need you:"
# A run's own failure text often opens with "Failed" or "Failed at Develop:"; the line already says it failed.
_FAILED_PREFIX = re.compile(r"^failed\b(?:\s+at\s+[\w-]+)?\s*[:,;.-]*\s*", re.IGNORECASE)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _reason(text: object) -> str:
    one = first_sentence(text).rstrip(".")
    return one if len(one) <= _REASON_MAX else one[: _REASON_MAX - 3].rstrip() + "..."


def elapsed_text(minutes: int) -> str:
    """How long a run has been going: "just started", "12 min in", "1 h 5 min in"."""
    if minutes < 1:
        return "just started"
    if minutes < 60:
        return f"{minutes} min in"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min in" if rest else f"{hours} h in"


# What a run in flight is doing, by action key: the phrase before "it now" in "Researching it now, 12 min in."
_RUNNING_PHRASE = {
    "develop": "Working on",
    "address-review": "Fixing the review findings on",
    "review": "Reviewing",
    "integrate": "Merging",
    "research": "Researching",
    "plan": "Planning",
    "rebase": "Rebasing",
    "triage": "Triaging",
    "spec": "Writing the spec for",
    "after-merge": "Cleaning up after",
}


def running(action_key: str, minutes: int) -> str:
    """The line of a run still going."""
    phrase = _RUNNING_PHRASE.get(action_key)
    head = f"{phrase} it now" if phrase else "Running a task on it now"
    return f"{head}, {elapsed_text(minutes)}."


def merged(review_rounds: int, address_rounds: int) -> str:
    """The line of a merged issue, with how many rounds it took."""
    if not review_rounds and not address_rounds:
        return "Merged."
    if not review_rounds:
        return f"Merged after {_plural(address_rounds, 'address round')}."
    if not address_rounds:
        return f"Merged after {_plural(review_rounds, 'review round')}."
    return f"Merged. {review_rounds} review and {_plural(address_rounds, 'address round')}."


def settled(state: HistoryState, pr_number: int | None, review_rounds: int, address_rounds: int) -> str:
    """The line of a group whose newest finished run left it in ``state`` (a state that does not need the user)."""
    if state == HistoryState.MERGED:
        return merged(review_rounds, address_rounds)
    if state == HistoryState.WAITING_REVIEW:
        return f"PR #{pr_number} is up and waiting for review." if pr_number else "Waiting for review."
    if state == HistoryState.WAITING_FIXES:
        return "The review asked for changes. Fixing them is next."
    if state == HistoryState.STOPPED:
        return "Stopped before it finished."
    return "Finished."


def did(action_key: str, pr_number: int | None) -> str:
    """What one finished run did, in the past tense. It says nothing of where the issue stands now: it is the line
    kept for a group that needs the user, which has since been reviewed, failed or run out of budget."""
    if action_key == "develop":
        return f"Opened PR #{pr_number}." if pr_number else "Developed it."
    if action_key == "rebase":
        return "Rebased it."
    return "Finished a run."


def _as_clause(reason: str) -> str:
    """``reason`` without its leading "Failed", opening in lower case so it reads mid-sentence ("CI" stays "CI")."""
    why = _FAILED_PREFIX.sub("", reason).strip()
    if len(why) > 1 and why[0].isupper() and why[1].islower():
        why = why[0].lower() + why[1:]
    return why


def failed(reason: object) -> str:
    """The attention line of a group whose newest run failed."""
    why = _as_clause(_reason(reason))
    if not why:
        return f"{NEED_YOU} it failed."
    # A cut reason already ends in the ellipsis: no period after it.
    return f"{NEED_YOU} it failed, {why}" + ("" if why.endswith("...") else ".")


def needs_approval() -> str:
    return f"{NEED_YOU} the spec is ready and waits for your approval."


def review_exhausted() -> str:
    return f"{NEED_YOU} the review budget ran out."


def watchdog_kill(signal: object) -> str:
    """The attention line of a run the watchdog stopped."""
    why = str(signal or "").replace("_", " ").strip()
    return f"{NEED_YOU} I stopped a stuck run, {why}." if why else f"{NEED_YOU} I stopped a stuck run."


# Background (not about one issue): what a kind of non-run event is called when counted.
_BACKGROUND_NOUN = {
    "connectivity": "connection change",
    "rate_limit": "GitHub rate limit event",
    "merge_queue": "merge queue update",
    "supervisor": "supervisor note",
    "watchdog": "watchdog warning",
    "budget": "budget warning",
    "batch": "batch job finished",
    "planner": "planner warning",
    "conversation": "confirmed conversation action",
}
OTHER_KIND = "other"


def known_background(kind: str) -> bool:
    return kind in _BACKGROUND_NOUN


def background_line(kind: str, count: int) -> str:
    """One folded background line: ``kind`` is a known event category, or anything else for the generic count."""
    noun = _BACKGROUND_NOUN.get(kind, "other event")
    return f"{_plural(count, noun)}."


def nothing_today() -> str:
    return "Nothing yet today."


def background_summary(total: int) -> str:
    """The fold's heading over the background lines: "Background, 3 things"."""
    return f"Background, {_plural(total, 'thing')}"


def labels() -> dict[str, str]:
    """The panel's section headings and fixed labels, so the client draws no wording of its own."""
    return {
        "attention": "Needs you",
        "today": "Today",
        "earlier": "Earlier",
        "open_in_work": "Open in Work",
        "no_activity": "No activity yet",
        "load_failed": "Could not load the activity. It retries on the next update.",
        "badge": "Activity Feed",
        # ``{count}`` is the only thing the client fills in.
        "badge_unread": "Activity Feed, {count} need you",
    }


def ref_label(ref: str) -> str:
    """How a handoff ref reads to the user: "#14" for an issue number, "PR #3" for ``pr:3``, else the ref as given
    (a tracker key such as PROJ-7)."""
    if ref.startswith("pr:"):
        return f"PR #{ref.removeprefix('pr:')}"
    return f"#{ref}" if ref.isdigit() else ref


def action_confirm(label: str, ref: str) -> str:
    """The question asked before a button that merges or is dangerous runs."""
    return f"{label} on {ref_label(ref)}?"


def action_started(label: str) -> str:
    return f"{label} started"


def action_failed(label: str) -> str:
    return f"Could not run {label}"


def conversation_action_done(summary: str, *, warning: str = "") -> str:
    """The Activity line for an action a user confirmed from a feed conversation; a partial outcome names its
    warning, so it never reads as plain done."""
    if warning:
        return f"Partly done from a conversation: {summary}. {warning}"
    return f"Done from a conversation: {summary}"


# Month names are fixed English: ``strftime("%B")`` follows the server's locale and would mix languages in a line.
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def period_label(kind: str, weeks: int = 0, month: int = 0, year: int | None = None) -> str:
    """The opening of a past period's line. ``kind`` is ``yesterday``, ``this_week``, ``weeks`` (``weeks`` ago) or
    ``month`` (``month`` 1 to 12, with ``year`` when it is not the current year)."""
    if kind == "yesterday":
        return "Yesterday"
    if kind == "this_week":
        return "Earlier this week"
    if kind == "weeks":
        return "Last week" if weeks == 1 else f"{weeks} weeks ago"
    name = _MONTHS[month - 1]
    return f"{name} {year}" if year is not None else name


def period(label: str, issues: int, merged_count: int, failed_runs: int, cost_usd: float) -> str:
    """The line of a past period, from counts: ``label`` ("Yesterday", "Last week", "March") opens it. ``issues``
    counts the issues (else PRs, else lone runs) worked on, ``failed_runs`` the runs that failed."""
    did = f"merged {merged_count}" if merged_count else "merged nothing"
    line = f"{label}: I worked on {_plural(issues, 'issue')} and {did}"
    if failed_runs:
        line += f", {_plural(failed_runs, 'run')} failed"
    return f"{line}, ${cost_usd:,.2f}."
