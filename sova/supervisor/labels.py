"""Label helpers shared by `dependency_graph.py` and `suitability.py`.

Kept in a separate leaf module (no imports of either) so that `dependency_graph`
can import the classifier in `suitability.py` and vice versa without a circular
import: both of those modules import from here instead of from each other for
these pure, graph-independent helpers.

Priority parsing is not here: it lives in `sova.adapters.priority.Priority`
(a leaf module shared by both trackers), reached via `Priority.from_labels()`
or the already-parsed `task.priority`.
"""

from __future__ import annotations


def is_epic(labels: list[str]) -> bool:
    """Return True if labels include 'type: epic' (case-insensitive).

    Epics are tracking containers that group related work. They should not be
    assigned to agents (excluded from ready tasks) and render with visual
    distinction in the dependency graph.
    """
    return any(label.lower().strip() == "type: epic" for label in labels)


def is_human_only(labels: list[str]) -> bool:
    """Return True if labels include 'agent:human-only' (case-insensitive)."""
    return any(label.lower().strip() == "agent:human-only" for label in labels)


def is_interactive_recommended(labels: list[str]) -> bool:
    """Return True if labels include 'agent:interactive-recommended' (case-insensitive)."""
    return any(label.lower().strip() == "agent:interactive-recommended" for label in labels)


def is_on_hold(labels: list[str]) -> bool:
    """Return True if labels include 'agent:on-hold' (case-insensitive).

    A non-state label: a held ticket keeps its real pipeline state, and only the
    suitability classifier reads this, reporting `blocked`.
    """
    return any(label.lower().strip() == "agent:on-hold" for label in labels)


def is_ready(labels: list[str]) -> bool:
    """Return True if labels include 'agent:ready' (case-insensitive)."""
    return any(label.lower().strip() == "agent:ready" for label in labels)


# Any of these pipeline-state labels mean the issue has moved past the point
# where a local suitability record applies, mirroring how a tracker label is
# stripped by `set_state()` on every real transition. Checked alongside
# `agent:ready` so the local-record fallback self-clears instead of needing
# `sova assess clear` to be remembered.
_PAST_PRE_DEVELOPMENT_LABELS = frozenset(
    {"agent:triaged", "agent:researched", "agent:in-progress", "agent:in-review", "agent:on-qa"}
)


def is_past_pre_development(labels: list[str]) -> bool:
    """Return True if labels include a pipeline-state label past pre-development."""
    normalized = {label.lower().strip() for label in labels}
    return bool(normalized & _PAST_PRE_DEVELOPMENT_LABELS)


def local_assessment_applies(labels: list[str]) -> bool:
    """Return True if a local `task_assessments` record could still change this issue's classification.

    False once any tracker label already determines the outcome on its own:
    `agent:ready`, `agent:human-only`, `agent:interactive-recommended`,
    `agent:on-hold`, `type: epic`, or any pipeline-state label past pre-development
    (`agent:triaged`, `agent:researched`, ...). Shared by
    `check_human_involvement_gate()` (to skip the DB lookup entirely when a
    label already decides the verdict) and `queue_maintenance._discover_ready()`
    /`_prune_unsuitable()` (to discard a stale local record for an issue a
    human has already moved past pre-development), so the three consumers
    cannot apply the same record under different conditions. On GitHub,
    `TaskState.RESEARCHED` is derived solely from the `agent:researched`
    label, so this is False for essentially every RESEARCHED issue there;
    it stays meaningful for a tracker (e.g. Jira) whose state is not itself
    label-derived.
    """
    return (
        not is_ready(labels)
        and not is_past_pre_development(labels)
        and not is_human_only(labels)
        and not is_interactive_recommended(labels)
        and not is_on_hold(labels)
        and not is_epic(labels)
    )
