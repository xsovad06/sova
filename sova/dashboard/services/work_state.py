"""Work item state machine: state enum, labels, colors, actions, compute.

Contains the pure-logic state machine for computing a work item's unified
dashboard state from GitHub labels, PR status, running agents, and SOVA verdicts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from sova.core.state import TaskStatus


class WorkItemState(StrEnum):
    """Unified dashboard state for a work item (issue or standalone PR)."""

    # Pre-development
    BACKLOG = "backlog"
    NEEDS_SPEC = "needs_spec"
    TRIAGED = "triaged"
    RESEARCHED = "researched"

    # Active development
    IN_PROGRESS = "in_progress"
    AGENT_RUNNING = "agent_running"

    # PR lifecycle
    PR_DRAFT = "pr_draft"
    PR_CONFLICTED = "pr_conflicted"
    PR_CI_RUNNING = "pr_ci_running"
    PR_CI_FAILED = "pr_ci_failed"
    PR_AWAITING_REVIEW = "pr_awaiting_review"
    # kept for backward compat; new code should use PR_SOVA_CHANGES or PR_EXTERNAL_CHANGES
    PR_CHANGES_REQUESTED = "pr_changes_requested"
    # SOVA reviewer said revise/block; developer agent addresses via handoff
    PR_SOVA_CHANGES = "pr_sova_changes"
    # external reviewer (CodeRabbit/human) requested changes; /address-pr command handles thread management
    PR_EXTERNAL_CHANGES = "pr_external_changes"
    PR_REVIEW_ADDRESSED = "pr_review_addressed"
    PR_APPROVED = "pr_approved"
    PR_READY_TO_MERGE = "pr_ready_to_merge"
    PR_SOVA_PENDING = "pr_sova_pending"

    # Handoff states
    SPEC_REVIEW = "spec_review"

    # Terminal
    MERGED = "merged"
    DONE = "done"
    HUMAN_ONLY = "human_only"


_STATE_LABELS: dict[WorkItemState, str] = {
    WorkItemState.BACKLOG: "Backlog",
    WorkItemState.NEEDS_SPEC: "Needs Spec",
    WorkItemState.TRIAGED: "Triaged",
    WorkItemState.RESEARCHED: "Researched",
    WorkItemState.IN_PROGRESS: "In Progress",
    WorkItemState.AGENT_RUNNING: "Agent Running",
    WorkItemState.PR_DRAFT: "Draft PR",
    WorkItemState.PR_CONFLICTED: "Conflicts",
    WorkItemState.PR_CI_RUNNING: "CI Running",
    WorkItemState.PR_CI_FAILED: "CI Failed",
    WorkItemState.PR_AWAITING_REVIEW: "Awaiting Review",
    WorkItemState.PR_CHANGES_REQUESTED: "Changes Requested",
    WorkItemState.PR_SOVA_CHANGES: "SOVA Changes Requested",
    WorkItemState.PR_EXTERNAL_CHANGES: "Changes Requested",
    WorkItemState.PR_REVIEW_ADDRESSED: "Review Addressed",
    WorkItemState.PR_APPROVED: "Approved",
    WorkItemState.PR_READY_TO_MERGE: "Ready to Merge",
    WorkItemState.PR_SOVA_PENDING: "Sova Review Pending",
    WorkItemState.SPEC_REVIEW: "Spec Review",
    WorkItemState.MERGED: "Merged",
    WorkItemState.DONE: "Done",
    WorkItemState.HUMAN_ONLY: "Human Only",
}

_CLR_GRAY = "bg-gray-600/30 text-gray-400"
_CLR_YELLOW = "bg-accent-yellow/20 text-accent-yellow"
_CLR_PEACH = "bg-accent-peach/20 text-accent-peach"
_CLR_GREEN = "bg-accent-green/20 text-accent-green"
_CLR_GREEN_STRONG = "bg-accent-green/30 text-accent-green"

_STATE_COLORS: dict[WorkItemState, str] = {
    WorkItemState.BACKLOG: _CLR_GRAY,
    WorkItemState.NEEDS_SPEC: _CLR_YELLOW,
    WorkItemState.TRIAGED: _CLR_YELLOW,
    WorkItemState.RESEARCHED: "bg-accent-purple/20 text-accent-purple",
    WorkItemState.IN_PROGRESS: "bg-accent/20 text-accent",
    WorkItemState.AGENT_RUNNING: _CLR_YELLOW,
    WorkItemState.PR_DRAFT: _CLR_GRAY,
    WorkItemState.PR_CONFLICTED: "bg-accent-red/20 text-accent-red",
    WorkItemState.PR_CI_RUNNING: _CLR_YELLOW,
    WorkItemState.PR_CI_FAILED: "bg-accent-red/20 text-accent-red",
    WorkItemState.PR_AWAITING_REVIEW: "bg-accent/20 text-accent",
    WorkItemState.PR_CHANGES_REQUESTED: _CLR_PEACH,
    WorkItemState.PR_SOVA_CHANGES: _CLR_PEACH,
    WorkItemState.PR_EXTERNAL_CHANGES: _CLR_PEACH,
    WorkItemState.PR_REVIEW_ADDRESSED: "bg-accent-lavender/20 text-accent-lavender",
    WorkItemState.PR_APPROVED: _CLR_GREEN,
    WorkItemState.PR_READY_TO_MERGE: _CLR_GREEN,
    WorkItemState.PR_SOVA_PENDING: _CLR_PEACH,
    WorkItemState.SPEC_REVIEW: _CLR_PEACH,
    WorkItemState.MERGED: _CLR_GREEN_STRONG,
    WorkItemState.DONE: _CLR_GREEN_STRONG,
    WorkItemState.HUMAN_ONLY: "bg-gray-600/30 text-gray-500",
}

_SPEC_ACTION_IDS = frozenset({"approve-spec", "revise-spec", "skip-spec", "reject-spec"})
_AWAITING_APPROVAL = TaskStatus.AWAITING_APPROVAL

_ROLE_LABELS: dict[str, str] = {
    "developer": "Developing",
    "reviewer": "Reviewing",
    "researcher": "Researching",
    "triage": "Triaging",
    "command:address-pr": "Addressing",
    "command:integrate-pr": "Integrating",
    "command:review-pr": "Reviewing",
    "command:after-merge": "Cleaning up",
    "command:spec": "Writing Spec",
}

_LABEL_STATE_MAP: dict[str, WorkItemState] = {
    "backlog": WorkItemState.BACKLOG,
    "triaged": WorkItemState.TRIAGED,
    "researched": WorkItemState.RESEARCHED,
    "in_progress": WorkItemState.IN_PROGRESS,
    "in_review": WorkItemState.PR_AWAITING_REVIEW,
    "needs_spec": WorkItemState.NEEDS_SPEC,
    "human_only": WorkItemState.HUMAN_ONLY,
    "done": WorkItemState.DONE,
}


def _build_action(
    action_id: str,
    label: str,
    style: str,
    handler: str,
    handler_args: dict,
) -> dict:
    return {
        "id": action_id,
        "label": label,
        "style": style,
        "handler": handler,
        "handler_args": handler_args,
    }


def _get_actions(
    state: WorkItemState,
    *,
    issue_number: str | None,
    pr_number: int | None,
) -> tuple[dict | None, list[dict]]:
    """Return (primary_action, secondary_actions) for the given state."""
    i = issue_number or ""
    p = pr_number or 0

    def agent(aid: str, label: str, style: str, role: str) -> dict:
        args: dict = {"role": role}
        if i:
            args["issue"] = i
        if p:
            args["pr"] = p
        return _build_action(aid, label, style, "start_agent", args)

    def cmd(aid: str, label: str, style: str, command: str, *, pr_only: bool = False) -> dict:
        args: dict = {"command": command}
        if not pr_only and i:
            args["issue"] = i
        if p:
            args["pr"] = p
        return _build_action(aid, label, style, "run_command", args)

    S = WorkItemState
    review = cmd("review_pr", "Review PR", "neutral", "review-pr")
    address = cmd("address_pr", "Address PR", "neutral", "address-pr")
    integrate = cmd("integrate", "Integrate PR", "neutral", "integrate-pr")

    def rebase() -> dict | None:
        if not i:
            return None
        return _build_action("rebase", "Rebase", "danger", "trigger_rebase", {"issue": i})

    actions: dict[WorkItemState, tuple[dict | None, list[dict]]] = {
        S.BACKLOG: (agent("triage", "Triage", "warning", "triage"), []),
        S.NEEDS_SPEC: (agent("research", "Research", "purple", "researcher"), []),
        S.TRIAGED: (agent("research", "Research", "purple", "researcher"), []),
        S.RESEARCHED: (agent("develop", "Develop", "primary", "developer"), []),
        S.IN_PROGRESS: (agent("resume", "Resume", "primary", "developer"), []),
        S.PR_DRAFT: (cmd("review_pr", "Review", "neutral", "review-pr"), [address]),
        S.PR_CONFLICTED: (rebase(), [review, address]),
        S.PR_CI_RUNNING: (cmd("review_pr", "Review", "neutral", "review-pr"), [address]),
        S.PR_CI_FAILED: (cmd("address_pr", "Address PR", "danger", "address-pr"), [review]),
        S.PR_AWAITING_REVIEW: (cmd("review_pr", "Review", "success", "review-pr"), [address, integrate]),
        S.PR_SOVA_PENDING: (cmd("review_pr", "Review PR", "warning", "review-pr"), [address, integrate]),
        S.PR_CHANGES_REQUESTED: (agent("address_review", "Address", "warning", "developer"), [review, integrate]),
        S.PR_SOVA_CHANGES: (agent("address_review", "Address", "warning", "developer"), [review, integrate]),
        S.PR_EXTERNAL_CHANGES: (cmd("address_pr", "Address PR", "warning", "address-pr"), [review, integrate]),
        S.PR_REVIEW_ADDRESSED: (cmd("review_pr", "Review", "purple", "review-pr"), [address, integrate]),
        S.PR_APPROVED: (cmd("integrate", "Integrate", "success", "integrate-pr"), [review, address]),
        S.PR_READY_TO_MERGE: (
            cmd("integrate", "Integrate", "success", "integrate-pr"),
            [review, address],
        ),
        S.MERGED: (cmd("after_merge", "Post-Merge", "purple", "after-merge"), []),
    }
    return actions.get(state, (None, []))


@dataclass(frozen=True)
class PRFacts:
    """Pure snapshot of everything resolve_next_action() needs to decide a PR's next action.

    Built by the caller (dashboard or supervisor) from already-fetched data;
    resolve_next_action() itself performs no I/O.
    """

    running_agent: bool
    pr_state: str  # "OPEN" | "MERGED" | "CLOSED"
    is_draft: bool
    mergeable: str  # "MERGEABLE" | "CONFLICTING" | "UNKNOWN"
    ci_status: str  # "" | "pending" | "running" | "passed" | "failed"
    head_sha: str
    sova_verdict: str | None  # "approve" | "revise" | "block" | "post_failed" | None
    sova_verdict_sha: str | None  # commit SHA the verdict was anchored to (#987)
    sova_verdict_addressed: bool  # True once an address cycle superseded this verdict (#988)
    external_changes_requested: bool  # standing CHANGES_REQUESTED from a bot/human, not dismissed
    thread_signal: str  # "clear" | "pending" | "unknown" (#989, three-valued)
    external_reviews_enabled: bool


@dataclass(frozen=True)
class Resolution:
    state: WorkItemState
    action_id: str | None  # e.g. "integrate", "address_review", "review_pr", "rebase"
    reason_chain: tuple[str, ...]  # every rule name evaluated, last entry is the match


def _verdict_is_stale(facts: PRFacts) -> bool:
    """True when the verdict is anchored to an older commit than the current head.

    A verdict anchored to an older commit has been superseded by new pushes and
    not yet re-reviewed: resolve_next_action() treats it as "no current review"
    rather than acting on stale findings. An unanchored verdict (either sha
    unknown) is reported as fresh, not stale, since there is nothing to compare.
    """
    return (
        facts.sova_verdict is not None
        and bool(facts.sova_verdict_sha)
        and bool(facts.head_sha)
        and facts.sova_verdict_sha != facts.head_sha
    )


def _has_standing_sova_changes(facts: PRFacts) -> bool:
    """True when a SOVA revise/block verdict still stands against the current head.

    Shared by the ladder's "sova_standing_changes" rule and its renderer so the
    two cannot describe different conditions.
    """
    return (
        facts.sova_verdict in ("revise", "block") and not facts.sova_verdict_addressed and not _verdict_is_stale(facts)
    )


def _unmet_merge_conditions(facts: PRFacts) -> list[str]:
    """Every merge precondition the PR currently fails, phrased for the reason chain.

    Empty means the PR satisfies all of them. Shared by the ladder's
    "ready_to_merge" rule and its renderer so the two cannot diverge.
    """
    unmet: list[str] = []
    if facts.sova_verdict != "approve":
        unmet.append(f"verdict is {facts.sova_verdict or 'none'}, not approve")
    if facts.thread_signal != "clear":
        unmet.append(f"threads are {facts.thread_signal}")
    if facts.ci_status != "passed":
        unmet.append(f"CI status is {facts.ci_status or 'unknown'}")
    if facts.mergeable != "MERGEABLE":
        unmet.append(f"mergeable status is {facts.mergeable}")
    return unmet


def resolve_next_action(facts: PRFacts) -> Resolution:
    """Pure, ordered, first-match-wins resolver for a PR's next action.

    Both the dashboard (compute_work_item_state()) and the supervisor
    (_refine_in_review_action()) delegate to this single function so they
    cannot disagree about what a work item should do next. No I/O.
    """
    chain: list[str] = []

    chain.append("agent_running")
    if facts.running_agent:
        return Resolution(WorkItemState.AGENT_RUNNING, None, tuple(chain))

    chain.append("merged")
    if facts.pr_state == "MERGED":
        return Resolution(WorkItemState.MERGED, None, tuple(chain))

    chain.append("conflicting")
    if facts.mergeable == "CONFLICTING":
        return Resolution(WorkItemState.PR_CONFLICTED, "rebase", tuple(chain))

    chain.append("draft")
    if facts.is_draft:
        return Resolution(WorkItemState.PR_DRAFT, None, tuple(chain))

    chain.append("ci_failed")
    if facts.ci_status == "failed":
        return Resolution(WorkItemState.PR_CI_FAILED, "address_pr", tuple(chain))

    chain.append("ci_running")
    if facts.ci_status in ("pending", "running"):
        return Resolution(WorkItemState.PR_CI_RUNNING, None, tuple(chain))

    # A stale verdict falls through to "no current review" (rule 10).
    verdict_stale = _verdict_is_stale(facts)

    chain.append("sova_standing_changes")
    if _has_standing_sova_changes(facts):
        return Resolution(WorkItemState.PR_SOVA_CHANGES, "address_review", tuple(chain))

    chain.append("sova_verdict_stale")
    # No direct match: a stale verdict falls through to "no current review" (below).

    chain.append("sova_verdict_addressed")
    if facts.sova_verdict_addressed:
        return Resolution(WorkItemState.PR_REVIEW_ADDRESSED, "review_pr", tuple(chain))

    chain.append("no_sova_review")
    if facts.sova_verdict is None or verdict_stale:
        state = WorkItemState.PR_SOVA_PENDING if facts.external_reviews_enabled else WorkItemState.PR_AWAITING_REVIEW
        return Resolution(state, "review_pr", tuple(chain))

    chain.append("external_changes_or_unresolved_threads")
    if facts.external_changes_requested or facts.thread_signal in ("pending", "unknown"):
        return Resolution(WorkItemState.PR_EXTERNAL_CHANGES, "address_pr", tuple(chain))

    chain.append("ready_to_merge")
    if not _unmet_merge_conditions(facts):
        return Resolution(WorkItemState.PR_READY_TO_MERGE, "integrate", tuple(chain))

    chain.append("awaiting_review")
    return Resolution(WorkItemState.PR_AWAITING_REVIEW, "review_pr", tuple(chain))


def _short_sha(sha: str | None) -> str:
    """First 7 characters of a commit SHA, or 'unknown' when empty/None."""
    return sha[:7] if sha else "unknown"


def _fact_agent_running(facts: PRFacts) -> str:
    return "an agent is currently running" if facts.running_agent else "no agent is currently running"


def _fact_merged(facts: PRFacts) -> str:
    return f"PR state is {facts.pr_state}"


def _fact_conflicting(facts: PRFacts) -> str:
    if facts.mergeable == "UNKNOWN":
        return "mergeable status is unknown"
    if facts.mergeable == "CONFLICTING":
        return "PR has merge conflicts"
    return f"PR mergeable status is {facts.mergeable}"


def _fact_draft(facts: PRFacts) -> str:
    return "PR is a draft" if facts.is_draft else "PR is not a draft"


def _fact_ci_failed(facts: PRFacts) -> str:
    if not facts.ci_status:
        return "CI status is unknown"
    if facts.ci_status == "failed":
        return "CI failed"
    return f"CI status is {facts.ci_status}"


def _fact_ci_running(facts: PRFacts) -> str:
    if not facts.ci_status:
        return "CI status is unknown"
    if facts.ci_status in ("pending", "running"):
        return f"CI is {facts.ci_status}"
    return f"CI is not pending or running (status: {facts.ci_status})"


def _fact_sova_standing_changes(facts: PRFacts) -> str:
    if _has_standing_sova_changes(facts):
        return f"SOVA has a standing '{facts.sova_verdict}' verdict on the current head"
    return "no standing SOVA revise/block verdict on the current head"


def _fact_sova_verdict_stale(facts: PRFacts) -> str:
    if facts.sova_verdict is None:
        return "no SOVA verdict to compare against head"
    if not facts.sova_verdict_sha or not facts.head_sha:
        return "verdict anchor is unknown"
    if facts.sova_verdict_sha != facts.head_sha:
        return f"SOVA reviewed {_short_sha(facts.sova_verdict_sha)}, head is now {_short_sha(facts.head_sha)} (stale)"
    return f"verdict is anchored to the current head ({_short_sha(facts.head_sha)})"


def _fact_sova_verdict_addressed(facts: PRFacts) -> str:
    if facts.sova_verdict_addressed:
        return "an address cycle has superseded the review"
    return "no address cycle has run since the review"


def _fact_no_sova_review(facts: PRFacts) -> str:
    if facts.sova_verdict is None:
        return "no SOVA review exists yet"
    if _verdict_is_stale(facts):
        return "the standing SOVA verdict is stale and treated as no current review"
    return f"a current SOVA verdict exists ({facts.sova_verdict})"


def _fact_external_changes_or_unresolved_threads(facts: PRFacts) -> str:
    if facts.external_changes_requested:
        return "an external reviewer has requested changes"
    if facts.thread_signal == "unknown":
        return "review thread status is unknown"
    if facts.thread_signal == "pending":
        return "review threads are still unresolved"
    return "no external changes requested and review threads are clear"


def _fact_ready_to_merge(facts: PRFacts) -> str:
    unmet = _unmet_merge_conditions(facts)
    if not unmet:
        return "approved, threads clear, CI passed, and mergeable: ready to merge"
    return "not ready to merge (" + "; ".join(unmet) + ")"


def _fact_awaiting_review(facts: PRFacts) -> str:
    return "no earlier rule matched; defaulting to awaiting review"


_RULE_RENDERERS: dict[str, Callable[[PRFacts], str]] = {
    "agent_running": _fact_agent_running,
    "merged": _fact_merged,
    "conflicting": _fact_conflicting,
    "draft": _fact_draft,
    "ci_failed": _fact_ci_failed,
    "ci_running": _fact_ci_running,
    "sova_standing_changes": _fact_sova_standing_changes,
    "sova_verdict_stale": _fact_sova_verdict_stale,
    "sova_verdict_addressed": _fact_sova_verdict_addressed,
    "no_sova_review": _fact_no_sova_review,
    "external_changes_or_unresolved_threads": _fact_external_changes_or_unresolved_threads,
    "ready_to_merge": _fact_ready_to_merge,
    "awaiting_review": _fact_awaiting_review,
}


def describe_reason_chain(reason_chain: tuple[str, ...], facts: PRFacts) -> list[str]:
    """Render each rule identifier in reason_chain to one fact-phrased sentence.

    Every renderer states the PRFacts value the rule inspects, not the rule's
    pass/fail outcome, so the same renderer serves both a matching (final) entry
    and a non-matching (earlier) entry: the chain reads as an audit trail, not a
    list of negations. A rule identifier with no renderer (a future ladder rule
    added without updating this map) renders as the identifier itself, so the
    payload and template never crash on an unrecognised entry.
    """
    sentences: list[str] = []
    for rule_id in reason_chain:
        renderer = _RULE_RENDERERS.get(rule_id)
        sentences.append(renderer(facts) if renderer else rule_id)
    return sentences


def _thread_signal(pr_data: dict) -> str:
    """Classify review thread resolution as clear/pending/unknown (#989).

    Lazy import avoids pulling pr_service's git/asyncio dependencies into this
    pure-logic module at import time.
    """
    from sova.dashboard.services.pr_service import get_unresolved_thread_count

    unresolved = get_unresolved_thread_count(pr_data)
    if unresolved is None:
        return "unknown"
    return "pending" if unresolved > 0 else "clear"


def _build_pr_facts(
    pr_data: dict,
    sova_verdict: dict | None,
    *,
    external_reviews_enabled: bool,
) -> PRFacts:
    """Translate raw pr_data/sova_verdict dicts into a PRFacts snapshot."""
    verdict = sova_verdict or {}
    has_review = verdict.get("has_sova_review", False)
    raw_verdict = verdict.get("verdict") if has_review else None
    sova_verdict_addressed = raw_verdict == "addressed"

    return PRFacts(
        running_agent=False,
        pr_state=pr_data.get("state", "OPEN"),
        is_draft=bool(pr_data.get("is_draft", False)),
        mergeable=pr_data.get("mergeable") or "UNKNOWN",
        ci_status=pr_data.get("ci_status", ""),
        head_sha=pr_data.get("head_sha", ""),
        sova_verdict=None if sova_verdict_addressed else raw_verdict,
        sova_verdict_sha=verdict.get("review_head_sha") if has_review else None,
        sova_verdict_addressed=sova_verdict_addressed,
        external_changes_requested=pr_data.get("computed_state") == "changes_requested",
        thread_signal=_thread_signal(pr_data),
        external_reviews_enabled=external_reviews_enabled,
    )


def compute_work_item_resolution(
    *,
    task_state: str | None,
    pr_data: dict | None,
    running_agent: dict | None,
    sova_verdict: dict | None = None,
    external_reviews_enabled: bool = True,
) -> tuple[Resolution, PRFacts | None]:
    """Compute the full Resolution plus the PRFacts it was built from (#992).

    Same priority cascade and inputs as compute_work_item_state(), but returns
    the intermediate Resolution (state, action_id, reason_chain) and PRFacts
    instead of discarding everything but the state, so callers can render the
    reason chain. Returns (Resolution(state, None, ()), None) when the ladder
    never ran (label-only item, no PR): there is no PR-routing decision to explain.

    The running-agent path builds PRFacts from pr_data (or {} when no PR is
    linked yet) and flips running_agent=True via dataclasses.replace, then calls
    resolve_next_action() normally, so the "agent_running" entry comes from the
    ladder itself rather than a second, potentially-divergent code path.
    """
    if running_agent is not None:
        facts = replace(
            _build_pr_facts(pr_data or {}, sova_verdict, external_reviews_enabled=external_reviews_enabled),
            running_agent=True,
        )
        return resolve_next_action(facts), facts

    if pr_data is not None:
        facts = _build_pr_facts(pr_data, sova_verdict, external_reviews_enabled=external_reviews_enabled)
        return resolve_next_action(facts), facts

    if task_state is not None:
        state = _LABEL_STATE_MAP.get(task_state, WorkItemState.BACKLOG)
        return Resolution(state, None, ()), None

    return Resolution(WorkItemState.BACKLOG, None, ()), None


def compute_work_item_state(
    *,
    task_state: str | None,
    pr_data: dict | None,
    running_agent: dict | None,
    sova_verdict: dict | None = None,
    external_reviews_enabled: bool = True,
) -> WorkItemState:
    """Compute the unified dashboard state for a work item.

    Priority: running agent > PR state (via resolve_next_action()) > GitHub label.
    """
    resolution, _facts = compute_work_item_resolution(
        task_state=task_state,
        pr_data=pr_data,
        running_agent=running_agent,
        sova_verdict=sova_verdict,
        external_reviews_enabled=external_reviews_enabled,
    )
    return resolution.state


_STATE_SORT_ORDER: dict[str, int] = {
    WorkItemState.AGENT_RUNNING: 0,
    WorkItemState.SPEC_REVIEW: 1,
    WorkItemState.PR_READY_TO_MERGE: 2,
    WorkItemState.PR_APPROVED: 2,
    WorkItemState.PR_CONFLICTED: 3,
    WorkItemState.PR_CI_FAILED: 3,
    WorkItemState.PR_CHANGES_REQUESTED: 3,
    WorkItemState.PR_SOVA_CHANGES: 3,
    WorkItemState.PR_EXTERNAL_CHANGES: 3,
    WorkItemState.PR_SOVA_PENDING: 3,
}


def _sort_items(items: list[dict]) -> None:
    """Sort: running first, then handoff pending, then by priority."""
    items.sort(
        key=lambda i: (
            _STATE_SORT_ORDER.get(i["state"], 10),
            i["priority"],
        )
    )
