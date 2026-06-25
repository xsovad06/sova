"""Task status state machine for SOVA.

Defines all valid states a task can be in and the allowed transitions
between them. The orchestrator drives transitions; agents never change
state directly -- they return results that the workflow engine interprets.
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    """All states in the task lifecycle."""

    RUNNING = "running"
    PENDING = "pending"
    ASSESSING = "assessing"
    RESEARCHED = "researched"
    IN_PROGRESS = "in_progress"
    DEVELOPING = "developing"
    SIMPLIFYING = "simplifying"
    REVIEWING = "reviewing"
    COMMITTING = "committing"
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
    TaskStatus.RUNNING: frozenset({TaskStatus.PENDING, TaskStatus.ADDRESSING_REVIEW}),
    TaskStatus.PENDING: frozenset({TaskStatus.ASSESSING}),
    TaskStatus.ASSESSING: frozenset({TaskStatus.IN_PROGRESS, TaskStatus.REJECTED}),
    TaskStatus.RESEARCHED: frozenset({TaskStatus.IN_PROGRESS}),
    TaskStatus.IN_PROGRESS: frozenset({TaskStatus.DEVELOPING}),
    TaskStatus.DEVELOPING: frozenset({TaskStatus.SIMPLIFYING}),
    TaskStatus.SIMPLIFYING: frozenset({TaskStatus.REVIEWING}),
    TaskStatus.REVIEWING: frozenset({TaskStatus.COMMITTING}),
    TaskStatus.COMMITTING: frozenset({TaskStatus.PUSHING}),
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


# -- Issue Lifecycle Phases ---------------------------------------------------


class LifecyclePhase(StrEnum):
    """Phases in the full issue lifecycle (development through merge)."""

    DEVELOPMENT = "development"
    POST_PR = "post_pr"
    REVIEW = "review"
    ADDRESS_REVIEW = "address_review"
    INTEGRATE = "integrate"
    POST_MERGE = "post_merge"


PHASE_ORDER: list[LifecyclePhase] = list(LifecyclePhase)

PHASE_TRANSITIONS: dict[str, set[str]] = {
    "development": {"post_pr"},
    "post_pr": {"review"},
    "review": {"address_review", "integrate"},
    "address_review": {"integrate"},
    "integrate": {"post_merge"},
    "post_merge": set(),
}


class PhaseStatus(StrEnum):
    """Status of a lifecycle phase."""

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


PHASE_STATUS_TERMINAL = frozenset({PhaseStatus.COMPLETED, PhaseStatus.FAILED, PhaseStatus.SKIPPED})

# Terminal statuses for TaskRun records (shared across services)
TASK_RUN_TERMINAL = frozenset({"done", "failed", "rejected", "interrupted"})

# Step statuses that mean "completed successfully".
# Legacy runs used "passed"; current WorkflowEngine uses "done".
STEP_DONE_STATUSES = frozenset({"done", "passed"})
