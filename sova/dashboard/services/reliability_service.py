"""Reliability reporting: success rate, failure taxonomy, and spend by outcome.

Read-only aggregation over TaskRun, StepExecution, and CostRecord. No new tables,
columns, or collection: this reads what the pipeline already persists (see
sova/dashboard/services/cost_service.py for the sibling cost-reporting queries this
mirrors).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.core.state import TASK_RUN_TERMINAL
from sova.db.models import CostRecord, StepExecution, TaskRun
from sova.llm.errors import (
    BillingError,
    LLMTimeoutError,
    ModelUnavailableError,
    ProviderUnavailableError,
    RateLimitError,
    classify_error,
)
from sova.utils.network import looks_like_network_outage

# Below this many terminal runs in the window, a role's success rate is too
# noisy to act on. Mirrors get_monthly_projection()'s insufficient_data
# convention in cost_service.py rather than inventing a new one.
_MIN_RUNS_FOR_RATE = 10

# Terminal but not a failure: excluded from the failure taxonomy, reported as
# their own bucket in the per-role breakdown so the counts reconcile. "stopped"
# is a deliberate user-initiated stop (see TASK_RUN_TERMINAL in
# sova/core/state.py), not a pipeline failure like "interrupted".
_INCOMPLETE_STATUSES = frozenset({"paused", "awaiting_approval", "stopped"})
_FAILURE_STATUSES = frozenset({"failed", "rejected", "interrupted"})

_STEP_TIMEOUT_MARKER = "step_hard_timeout"
_FIX_LLM_TIMEOUT_PREFIX = "fix_llm_timeout on cycle"
_FIX_LLM_FAILED_PREFIX = "fix_llm_failed on cycle"
# Optional sign: _build_exit_failure_message() (agent_db.py) is fed the raw
# process exit_code, which can be negative under asyncio's signal-termination
# convention (e.g. -9/-15 for SIGKILL/SIGTERM, including an OOM kill).
_PROCESS_EXIT_RE = re.compile(r"^process exited with code -?\d+")

# Free-text GateCheckResult.reason markers (sova/core/steps/*.py). These do not
# share a common prefix, so they are matched as an unordered substring set
# rather than a single regex. Not exhaustive: a gate reason with no match here
# falls into "other", which is itself the signal to extend this list.
_GATE_CHECK_MARKERS: tuple[str, ...] = (
    "gate check timed out",
    "verification timed out",
    "no commits ahead",
    "uncommitted changes",
    "staged but uncommitted",
    "worktree directory does not exist",
    "no pr number after",
    "no tasks were generated",
    "not populated in context",
    "rebase still in progress",
    "cannot determine git directory",
    "unexpected rev-list output",
    "git status failed",
    "failed to count commits",
    "lost during review",
    "reverted during simplification",
    "scan result not populated",
    "no changes after addressing review findings",
)

# Delegates to classify_error()'s own ordered, terminal-first substring table
# rather than re-implementing it (see sova/llm/errors.py).
_LLM_ERROR_LABELS: dict[type, str] = {
    BillingError: "llm_billing",
    ModelUnavailableError: "llm_model_unavailable",
    RateLimitError: "llm_rate_limit",
    ProviderUnavailableError: "llm_provider_unavailable",
    LLMTimeoutError: "llm_timeout",
}

# classify_error() is scoped to LLM *invocation* details; sova/llm/errors.py says
# so explicitly of its "no such file or directory" pattern. Its category patterns
# match text that plenty of non-LLM subsystems in this codebase also produce:
# "command not found"/"timed out" from a failed git worktree, a missing
# shellcheck, or a CI polling timeout; "rate limit"/"429"/"quota"/"budget" from
# GitHub API rate limiting (sova/supervisor/github_quota.py), CodeRabbit review
# quota (coderabbit_quota.py), or CI minutes budget (ci_budget.py); "model" from
# an unrelated "model not found" style message. None of that text is LLM-specific
# on its own, so every label here is only accepted when the message independently
# names the LLM layer. Derived from _LLM_ERROR_LABELS.values() rather than
# re-listed: every current and future label added there is automatically
# guarded, so the two collections cannot drift apart.
_AMBIGUOUS_LLM_LABELS = frozenset(_LLM_ERROR_LABELS.values())

# Deliberately excludes a bare "claude": SOVA's own .claude/ directory sits in the
# path of nearly every git, worktree, and config error message, so it corroborates
# nothing. The quoted form is the errno-2 text a missing CLI binary produces
# (FileNotFoundError: [Errno 2] No such file or directory: 'claude'). "llm" also
# covers "litellm". Deliberately excludes a bare "provider" too: it is generic
# enough to appear in a non-LLM adapter/network error (e.g. a Jira/GitHub
# TaskAdapter message naming its own "provider"), which would corroborate
# nothing and defeat the point of this list.
_LLM_CONTEXT_MARKERS: tuple[str, ...] = (
    "llm",
    "anthropic",
    "vertex",
    "bedrock",
    "'claude'",
    "claude cli",
    "claude code",
)


def _window_cutoff(days: int) -> datetime:
    # Timezone-aware UTC cutoff. Verified against SQLAlchemy's default SQLite
    # DateTime bind/result processors (sova/db/session.py: sqlite+aiosqlite is
    # the default dialect): they format a bound datetime using only its raw
    # year/month/day/hour/minute/second/microsecond fields, never a tzinfo
    # suffix, so an aware cutoff compares correctly against both aware- and
    # naive-inserted started_at rows representing the same UTC instant (the
    # naive-row edge case get_monthly_projection() also has to account for).
    # A naive cutoff would be actively wrong for a PostgreSQL deployment
    # (SOVA_DATABASE_URL), where started_at is a real TIMESTAMPTZ column and
    # asyncpg requires an aware value for it.
    return datetime.now(timezone.utc) - timedelta(days=days)


def classify_failure_cause(error_message: str | None) -> str:
    """Classify a TaskRun.error_message into a failure-taxonomy bucket.

    First-match-wins over an ordered rule table. A NULL message (a crash
    before any message was written) is its own "unclassified" bucket rather
    than being silently dropped. Anything that matches none of the known
    markers falls into "other", which is displayed rather than hidden so a
    growing "other" bucket signals the rule table needs extending.

    The trailing delegation to classify_error() is conditional for the two
    labels in _AMBIGUOUS_LLM_LABELS: see the comment on that constant for why a
    generic shell or filesystem message must not be reported as an LLM outage.
    """
    if not error_message:
        return "unclassified"

    lower = error_message.strip().lower()

    # Checked before every other rule, for two reasons. Mechanically, a
    # connectivity failure usually arrives wrapped in another rule's shape:
    # _build_exit_failure_message() prefixes the captured cause with "Process
    # exited with code 1", which _PROCESS_EXIT_RE below would otherwise claim
    # first. Substantively, an outage is an external root cause, and leaving it
    # in the fix_llm_* or step_timeout buckets corrupts exactly the measurement
    # those buckets exist for (how well SOVA's own fix loop converges).
    if looks_like_network_outage(error_message):
        return "network_unreachable"

    if lower == _STEP_TIMEOUT_MARKER:
        return "step_timeout"
    if lower.startswith(_FIX_LLM_TIMEOUT_PREFIX):
        return "fix_llm_timeout"
    if lower.startswith(_FIX_LLM_FAILED_PREFIX):
        return "fix_llm_failed"
    if _PROCESS_EXIT_RE.match(lower):
        return "process_exit"
    if any(marker in lower for marker in _GATE_CHECK_MARKERS):
        return "gate_check"

    label = _LLM_ERROR_LABELS.get(classify_error(error_message))
    if label and (label not in _AMBIGUOUS_LLM_LABELS or any(m in lower for m in _LLM_CONTEXT_MARKERS)):
        return label

    return "other"


async def get_success_by_role(session: AsyncSession, days: int = 30) -> list[dict]:
    """Per-role success rate over the trailing window.

    Denominator: every run started in the window whose status is terminal
    (TASK_RUN_TERMINAL). Still-running rows are excluded so an in-flight run
    never depresses the rate. paused/awaiting_approval/stopped count toward
    the denominator as "incomplete", not as a failure. A role string present in
    the DB but not in any current role set still appears here: grouping is by
    the raw column value, never filtered against a hard-coded role list.
    """
    cutoff = _window_cutoff(days)
    stmt = (
        select(TaskRun.role, TaskRun.status, func.count(TaskRun.id).label("count"))
        .where(TaskRun.started_at >= cutoff, TaskRun.status.in_(TASK_RUN_TERMINAL))
        .group_by(TaskRun.role, TaskRun.status)
    )
    result = await session.execute(stmt)

    # A NULL TaskRun.role (a run that crashed before role assignment, or
    # historical data corruption) is normalized to a sentinel string here so
    # it groups and sorts predictably instead of crashing sorted() below,
    # which raises TypeError comparing None to str.
    by_role: dict[str, dict[str, int]] = {}
    for row in result.all():
        by_role.setdefault(row.role or "unknown", {})[row.status] = row.count

    rows = []
    for role in sorted(by_role):
        status_counts = by_role[role]
        # Always >= 1: by_role only ever gains a role key from a grouped row
        # with count >= 1 (COUNT never yields a zero-row group), so total_runs
        # is never 0 for a role present in this dict.
        total = sum(status_counts.values())
        done = status_counts.get("done", 0)
        failures = sum(status_counts.get(s, 0) for s in _FAILURE_STATUSES)
        incomplete = sum(status_counts.get(s, 0) for s in _INCOMPLETE_STATUSES)
        rows.append(
            {
                "role": role,
                "total_runs": total,
                "done": done,
                "failures": failures,
                "incomplete": incomplete,
                "success_rate": round(done / total, 4),
                "insufficient_data": total < _MIN_RUNS_FOR_RATE,
            }
        )
    return rows


async def get_failure_taxonomy(session: AsyncSession, days: int = 30) -> list[dict]:
    """Failure cause breakdown for failed/rejected/interrupted runs in the window.

    Groups by classify_failure_cause(TaskRun.error_message). Each cause row also
    reports the most common failing step name (from StepExecution.status="failed"
    on the same runs), or null when no StepExecution ever reached "failed" for
    those runs (e.g. a run marked "interrupted" by process death, not a step
    failure). Highest count first.
    """
    cutoff = _window_cutoff(days)
    window = (TaskRun.started_at >= cutoff, TaskRun.status.in_(_FAILURE_STATUSES))
    rows = (await session.execute(select(TaskRun.id, TaskRun.error_message).where(*window))).all()
    if not rows:
        return []

    # Correlated subquery rather than a materialized id list: a busy window can
    # hold more failed runs than SQLite's bound-parameter ceiling, and blowing
    # that limit would 500 the whole panel. Mirrors agent_status.py's
    # StepExecution.task_run_id.in_(select(...)) for the same reason.
    failed_run_ids = select(TaskRun.id).where(*window).scalar_subquery()
    step_stmt = (
        select(StepExecution.task_run_id, StepExecution.step_name)
        .where(StepExecution.task_run_id.in_(failed_run_ids), StepExecution.status == "failed")
        .order_by(StepExecution.id)
    )
    # First failed step per run, not an arbitrary one: a run with several failed
    # StepExecutions broke at the earliest, and an unordered dict build would let
    # the reported step differ between two identical requests.
    failing_step_by_run: dict[int, str] = {}
    for row in (await session.execute(step_stmt)).all():
        failing_step_by_run.setdefault(row.task_run_id, row.step_name)

    cause_counts: Counter[str] = Counter()
    steps_by_cause: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        cause = classify_failure_cause(row.error_message)
        cause_counts[cause] += 1
        step_name = failing_step_by_run.get(row.id)
        if step_name:
            steps_by_cause[cause][step_name] += 1

    taxonomy = []
    for cause, count in cause_counts.most_common():
        top_steps = steps_by_cause[cause].most_common(1)
        taxonomy.append({"cause": cause, "count": count, "top_failing_step": top_steps[0][0] if top_steps else None})
    return taxonomy


async def get_spend_by_outcome(session: AsyncSession, days: int = 30) -> dict:
    """Completing versus non-completing spend for terminal runs in the window.

    Cost comes from TaskRun.total_cost_usd (always written, matching
    cost_service.get_summary()'s documented reason). Tokens come from
    CostRecord via an explicit join on task_run_id, since CostRecord.task_run_id
    is nullable and TaskRun has no token column of its own; orphan CostRecord
    rows (no task_run_id) are ignored by the inner join rather than assumed to
    belong to a run.
    """
    cutoff = _window_cutoff(days)

    cost_stmt = (
        select(TaskRun.status, func.sum(TaskRun.total_cost_usd).label("cost"), func.count(TaskRun.id).label("count"))
        .where(TaskRun.started_at >= cutoff, TaskRun.status.in_(TASK_RUN_TERMINAL))
        .group_by(TaskRun.status)
    )
    cost_by_status = {row.status: (row.cost, row.count) for row in (await session.execute(cost_stmt)).all()}

    token_stmt = (
        select(
            TaskRun.status,
            func.sum(CostRecord.input_tokens).label("tokens_in"),
            func.sum(CostRecord.output_tokens).label("tokens_out"),
        )
        .join(TaskRun, CostRecord.task_run_id == TaskRun.id)
        .where(TaskRun.started_at >= cutoff, TaskRun.status.in_(TASK_RUN_TERMINAL))
        .group_by(TaskRun.status)
    )
    tokens_by_status = {
        row.status: (row.tokens_in or 0, row.tokens_out or 0) for row in (await session.execute(token_stmt)).all()
    }

    def _bucket(statuses: Collection[str]) -> dict:
        cost = Decimal("0")
        count = 0
        tokens_in = 0
        tokens_out = 0
        for status in statuses:
            row_cost, row_count = cost_by_status.get(status, (0, 0))
            cost += Decimal(row_cost or 0)
            count += row_count
            row_tokens_in, row_tokens_out = tokens_by_status.get(status, (0, 0))
            tokens_in += row_tokens_in
            tokens_out += row_tokens_out
        return {
            "cost_usd": round(cost, 4),
            "run_count": count,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }

    return {
        "window_days": days,
        "completing": _bucket({"done"}),
        "non_completing": _bucket(TASK_RUN_TERMINAL - {"done"}),
        # Sorted for deterministic rendering. The template derives its "Non-Completing"
        # card label from this list rather than hardcoding the status set, so a future
        # addition to TASK_RUN_TERMINAL (already forced into _FAILURE_STATUSES or
        # _INCOMPLETE_STATUSES by TestStatusPartition) cannot leave the UI label stale.
        "non_completing_statuses": sorted(TASK_RUN_TERMINAL - {"done"}),
    }
