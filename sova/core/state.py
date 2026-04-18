"""Task status state machine for SOVA.

Defines all valid states a task can be in and the allowed transitions
between them. The orchestrator drives transitions; agents never change
state directly -- they return results that the workflow engine interprets.
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    """All states in the task lifecycle."""

    PENDING = "pending"
    ASSESSING = "assessing"
    RESEARCHED = "researched"
    IN_PROGRESS = "in_progress"
    DEVELOPING = "developing"
    SIMPLIFYING = "simplifying"
    REVIEWING = "reviewing"
    PUSHING = "pushing"
    PR_CREATED = "pr_created"
    CI_MONITORING = "ci_monitoring"
    AUTOMATED_REVIEW = "automated_review"
    ADDRESSING_REVIEW = "addressing_review"
    DONE = "done"
    PAUSED = "paused"
    FAILED = "failed"
    REJECTED = "rejected"


# Terminal states -- no outgoing transitions
_TERMINAL = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.REJECTED})

# Non-terminal states that PAUSED can resume to
_RESUMABLE = frozenset(s for s in TaskStatus if s not in _TERMINAL and s != TaskStatus.PAUSED)

# Explicit forward transitions (happy path + branching)
_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.ASSESSING}),
    TaskStatus.ASSESSING: frozenset({TaskStatus.IN_PROGRESS, TaskStatus.REJECTED}),
    TaskStatus.RESEARCHED: frozenset({TaskStatus.IN_PROGRESS}),
    TaskStatus.IN_PROGRESS: frozenset({TaskStatus.DEVELOPING}),
    TaskStatus.DEVELOPING: frozenset({TaskStatus.SIMPLIFYING}),
    TaskStatus.SIMPLIFYING: frozenset({TaskStatus.REVIEWING}),
    TaskStatus.REVIEWING: frozenset({TaskStatus.PUSHING}),
    TaskStatus.PUSHING: frozenset({TaskStatus.PR_CREATED}),
    TaskStatus.PR_CREATED: frozenset({TaskStatus.CI_MONITORING}),
    TaskStatus.CI_MONITORING: frozenset({TaskStatus.AUTOMATED_REVIEW}),
    TaskStatus.AUTOMATED_REVIEW: frozenset({TaskStatus.ADDRESSING_REVIEW, TaskStatus.DONE}),
    TaskStatus.ADDRESSING_REVIEW: frozenset({TaskStatus.DONE}),
    TaskStatus.PAUSED: _RESUMABLE,
    TaskStatus.DONE: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.REJECTED: frozenset(),
}


def get_valid_transitions(status: TaskStatus) -> frozenset[TaskStatus]:
    """Return the set of states reachable from *status*.

    Every non-terminal state can also transition to PAUSED or FAILED.
    """
    explicit = _TRANSITIONS.get(status, frozenset())
    if status in _TERMINAL or status == TaskStatus.PAUSED:
        return explicit
    return explicit | frozenset({TaskStatus.PAUSED, TaskStatus.FAILED})


def validate_transition(current: TaskStatus, target: TaskStatus) -> None:
    """Raise if *current* -> *target* is not a valid transition."""
    valid = get_valid_transitions(current)
    if target not in valid:
        raise InvalidTransitionError(current, target)


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid transition: {current} -> {target}")
