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

# Terminal statuses that count as a failed run. classify_run_outcome() buckets every TASK_RUN_TERMINAL status
# (tests/test_reliability_service.py, TestStatusPartition).
FAILURE_STATUSES = frozenset({"failed", "rejected", "interrupted"})

# TerminationRecord.cause of a stop the user asked for (the dashboard stop button). Any other cause
# (watchdog signal names, memory pressure, dispatch timeouts) is SOVA stopping its own run.
# Writers: agent_lifecycle.stop_agent() takes this constant as its default cause; sova.ipc sits below core and
# cannot import it, so AgentProcess.stop() repeats the literal (tests/test_agent_failure_capture.py pins both).
MANUAL_STOP_CAUSE = "manual_stop"


class RunOutcome(StrEnum):
    """What a finished TaskRun counts as in a success-rate or spend report.

    Only SUCCESS and FAILURE enter a success rate; the two halted buckets are reported beside it.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    PAUSED_BY_SOVA = "paused_by_sova"
    STOPPED_BY_USER = "stopped_by_user"


def classify_run_outcome(status: str, termination_reason: str | None = None) -> RunOutcome:
    """Bucket a terminal TaskRun status.

    ``paused`` and ``awaiting_approval`` are SOVA halting its own run. A ``stopped`` run is the user's stop only
    when its ``termination_reason`` is the manual-stop cause; any other reason, and a NULL one (a row written
    before the reason column existed), is SOVA pausing its own run. A status outside ``TASK_RUN_TERMINAL`` raises
    ``ValueError``: a non-terminal run has no outcome, and returning a bucket for it would let an unfinished run
    look finished.
    """
    if status == "done":
        return RunOutcome.SUCCESS
    if status in FAILURE_STATUSES:
        return RunOutcome.FAILURE
    if status in ("paused", "awaiting_approval"):
        return RunOutcome.PAUSED_BY_SOVA
    if status == "stopped":
        return RunOutcome.STOPPED_BY_USER if termination_reason == MANUAL_STOP_CAUSE else RunOutcome.PAUSED_BY_SOVA
    raise ValueError(f"{status!r} is not a terminal TaskRun status")
