"""Task status state machine for SOVA.

Defines all valid states a task can be in. The orchestrator drives
transitions; agents never change state directly: they return results that
the workflow engine interprets.
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
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    PAUSED = "paused"
    FAILED = "failed"
    REJECTED = "rejected"


# Issue Lifecycle Phases ---------------------------------------------------


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
    STOPPED = "stopped"


PHASE_STATUS_TERMINAL = frozenset({PhaseStatus.COMPLETED, PhaseStatus.FAILED, PhaseStatus.SKIPPED, PhaseStatus.STOPPED})

# Terminal statuses for TaskRun records (shared across services).
# "stopped" is a deliberate stop (see sova.ipc.control.TerminationRecord),
# distinct from "interrupted" (dead-PID recovery, orphaned runs) which is
# always involuntary.
TASK_RUN_TERMINAL = frozenset({"done", "failed", "rejected", "interrupted", "paused", "awaiting_approval", "stopped"})

# Step statuses that count as completed progress (executed successfully or intentionally skipped).
# Legacy runs used "passed"; current WorkflowEngine uses "done".
STEP_DONE_STATUSES = frozenset({"done", "passed", "skipped"})
