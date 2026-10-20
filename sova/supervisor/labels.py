"""Label and priority helpers shared by `dependency_graph.py` and `suitability.py`.

Kept in a separate leaf module (no imports of either) so that `dependency_graph`
can import the classifier in `suitability.py` and vice versa without a circular
import: both of those modules import from here instead of from each other for
these pure, graph-independent helpers.
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


def is_ready(labels: list[str]) -> bool:
    """Return True if labels include 'agent:ready' (case-insensitive)."""
    return any(label.lower().strip() == "agent:ready" for label in labels)


_VALID_PRIORITIES: frozenset[str] = frozenset({"critical", "high", "medium", "low"})


def extract_priority(labels: list[str]) -> str:
    """Return the priority level from a list of labels, or '' if none found.

    Recognises labels of the form 'priority: <level>' (case-insensitive).
    Only the four canonical levels (critical/high/medium/low) are returned;
    unknown values are ignored so stale or mis-spelled labels don't surface.
    """
    for label in labels:
        lower = label.lower().strip()
        if lower.startswith("priority:"):
            value = lower[len("priority:") :].strip()
            if value in _VALID_PRIORITIES:
                return value
    return ""
