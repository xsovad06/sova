"""Reliability reporting: success rate, failure taxonomy, and spend by outcome.

Read-only aggregation over TaskRun, StepExecution, and CostRecord. No new tables,
columns, or collection: this reads what the pipeline already persists (see
sova/dashboard/services/cost_service.py for the sibling cost-reporting queries this
mirrors).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import ColumnElement, ScalarSelect, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.core.state import TASK_RUN_TERMINAL
from sova.db.models import CostRecord, StepExecution, TaskRun
from sova.llm.errors import (
    BillingError,
    ConfigurationError,
    LLMTimeoutError,
    ModelUnavailableError,
    ProviderUnavailableError,
    RateLimitError,
    classify_error,
)
from sova.utils.logging import get_logger
from sova.utils.network import looks_like_network_outage

log = get_logger(component="dashboard.reliability_service")

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
    ConfigurationError: "llm_configuration",
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
# guarded, so the two collections cannot drift apart. "llm_configuration" is
# excluded: unlike the others, that token ("sova_configuration_error") is
# namespaced and raised only by SOVA's own code (CodexRuntime.spawn()), never
# scraped from third-party text, so it needs no independent corroboration.
_AMBIGUOUS_LLM_LABELS = frozenset(_LLM_ERROR_LABELS.values()) - {"llm_configuration"}

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


# Step names that tell a developer run's pipeline apart (STEP_REGISTRY names in
# sova/core/steps/): the initial pipeline runs "develop", the address-review
# pipeline runs "address_review". The step is the only durable record of which
# pipeline ran; run_label and current_step are not.
_DEVELOP_STEP = "develop"
_ADDRESS_REVIEW_STEP = "address_review"

_UNKNOWN_ROLE = "unknown"
_COMMAND_ROLE_PREFIX = "command:"

# CostRecord.phase for an agent-session run (agent_db.py), which is not a pipeline
# step: a command:* run's usage is reported per command phase from phase_usage instead.
_AGENT_SESSION_PHASE = "agent"

_KIND_STEP = "step"
_KIND_COMMAND_PHASE = "command_phase"


@dataclass(frozen=True)
class _DoneRun:
    id: int
    role: str
    started_at: datetime
    ended_at: datetime | None
    cost: Decimal


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes even for timezone=True columns (they hold UTC);
    # PostgreSQL returns aware ones. Normalize both so month bucketing and duration
    # arithmetic never mix the two.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _month_window_start(months: int) -> datetime:
    """Aware UTC start of the earliest calendar month in a window of `months` months, current included."""
    now = datetime.now(timezone.utc)
    year, month_index = divmod(now.year * 12 + now.month - 1 - (months - 1), 12)
    return datetime(year, month_index + 1, 1, tzinfo=timezone.utc)


def _display_role(role: str | None, step_names: Collection[str]) -> str:
    """Normalize a raw TaskRun.role into the label the usage tables group by.

    - NULL or empty role -> "unknown".
    - "command:/integrate-pr 123" and "command:/integrate-pr" -> "command:integrate-pr" (the
      legacy strings carry a slash and an argument); a bare "command:" -> "unknown".
    - "developer" -> "developer:develop" or "developer:address-review", decided by which step
      the run executed. A run with both steps (resumed or merged) is "developer:develop":
      develop takes precedence, since it means the run started the PR. A run with neither
      (no StepExecution rows) stays plain "developer" rather than being dropped.
    - anything else is returned as stored.
    """
    if not role:
        return _UNKNOWN_ROLE
    if role.startswith(_COMMAND_ROLE_PREFIX):
        words = role[len(_COMMAND_ROLE_PREFIX) :].split()
        name = words[0].lstrip("/") if words else ""
        return f"{_COMMAND_ROLE_PREFIX}{name}" if name else _UNKNOWN_ROLE
    if role == "developer":
        if _DEVELOP_STEP in step_names:
            return "developer:develop"
        if _ADDRESS_REVIEW_STEP in step_names:
            return "developer:address-review"
    return role


def _done_window(cutoff: datetime) -> tuple[ColumnElement[bool], ...]:
    """WHERE clauses selecting the done runs started at or after `cutoff`."""
    return (TaskRun.started_at >= cutoff, TaskRun.status == "done")


def _done_run_ids(cutoff: datetime) -> ScalarSelect[int]:
    """Done run ids in the window as a subquery, not an id list (bound-parameter ceiling, see get_failure_taxonomy)."""
    return select(TaskRun.id).where(*_done_window(cutoff)).scalar_subquery()


async def _load_done_runs(session: AsyncSession, cutoff: datetime) -> list[_DoneRun]:
    """Done runs started at or after `cutoff`, each with its display role resolved."""
    runs = (
        await session.execute(
            select(TaskRun.id, TaskRun.role, TaskRun.started_at, TaskRun.ended_at, TaskRun.total_cost_usd)
            .where(*_done_window(cutoff))
            .order_by(TaskRun.id)
        )
    ).all()
    if not runs:
        return []

    window_ids = _done_run_ids(cutoff)
    step_rows = (
        await session.execute(
            select(StepExecution.task_run_id, StepExecution.step_name)
            .where(
                StepExecution.task_run_id.in_(window_ids),
                StepExecution.step_name.in_((_DEVELOP_STEP, _ADDRESS_REVIEW_STEP)),
            )
            .distinct()
        )
    ).all()
    steps_by_run: defaultdict[int, set[str]] = defaultdict(set)
    for row in step_rows:
        steps_by_run[row.task_run_id].add(row.step_name)

    return [
        _DoneRun(
            id=run.id,
            role=_display_role(run.role, steps_by_run.get(run.id, ())),
            started_at=_as_utc(run.started_at),
            ended_at=_as_utc(run.ended_at) if run.ended_at is not None else None,
            cost=Decimal(str(run.total_cost_usd or 0)),
        )
        for run in runs
    ]


def _mean(values: Iterable[int | float | Decimal], digits: int = 1) -> float | None:
    """Mean rounded to `digits`, or None when there is nothing to average (never 0)."""
    collected = list(values)
    return round(float(sum(collected)) / len(collected), digits) if collected else None


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


async def get_usage_by_role(session: AsyncSession, months: int = 6) -> list[dict]:
    """Average turns, tokens, cost and duration of done runs, per UTC month and role.

    One row per (month, role) with at least one done run, oldest month first then role. Only
    status "done" runs count (unlike get_success_by_role(), which counts every terminal run).
    The window starts at the first of the earliest of the last `months` UTC calendar months,
    current month included, and months are bucketed on TaskRun.started_at in Python so SQLite
    and PostgreSQL agree.

    Roles are normalized by _display_role(). Cost is TaskRun.total_cost_usd. Duration is
    ended_at minus started_at, falling back to the sum of the run's CostRecord.duration_ms
    when ended_at is NULL, and never negative; a run with neither is left out of the duration
    average rather than averaged in as zero (None when no run has one). Turns and tokens come
    from CostRecord rows:
    avg_turns divides by the runs that reported turns, the token averages by the runs that have
    any CostRecord, so a pre-#1336 or cost-less run still counts in done_runs, cost and
    duration but is not averaged in as a zero. avg_turns is None when no run reported turns.
    avg_context_tokens is input + cache_tokens (cache read + cache write, also set on rows
    from before the granular cache columns), as phase_usage.context_tokens is.
    """
    cutoff = _month_window_start(months)
    runs = await _load_done_runs(session, cutoff)
    if not runs:
        return []

    window_ids = _done_run_ids(cutoff)
    usage_rows = (
        await session.execute(
            select(
                CostRecord.task_run_id,
                func.sum(CostRecord.num_turns).label("turns"),
                func.sum(CostRecord.input_tokens).label("input"),
                func.sum(CostRecord.cache_tokens).label("cache"),
                func.sum(CostRecord.output_tokens).label("output"),
                func.sum(CostRecord.duration_ms).label("duration_ms"),
            )
            .where(CostRecord.task_run_id.in_(window_ids))
            .group_by(CostRecord.task_run_id)
        )
    ).all()
    usage_by_run = {row.task_run_id: row for row in usage_rows}

    buckets: defaultdict[tuple[str, str], list[_DoneRun]] = defaultdict(list)
    for run in runs:
        buckets[(f"{run.started_at.year:04d}-{run.started_at.month:02d}", run.role)].append(run)

    rows = []
    for (month, role), bucket in sorted(buckets.items()):
        usages = [usage_by_run[run.id] for run in bucket if run.id in usage_by_run]
        durations = []
        for run in bucket:
            if run.ended_at is not None:
                durations.append(max((run.ended_at - run.started_at).total_seconds(), 0.0))
                continue
            usage = usage_by_run.get(run.id)
            if usage is not None and usage.duration_ms:
                durations.append(usage.duration_ms / 1000)
        rows.append(
            {
                "month": month,
                "role": role,
                "done_runs": len(bucket),
                "avg_cost_usd": round(sum((run.cost for run in bucket), Decimal("0")) / len(bucket), 4),
                "avg_duration_seconds": _mean(durations),
                "avg_turns": _mean((u.turns for u in usages if u.turns is not None)),
                "avg_context_tokens": _mean(((u.input or 0) + (u.cache or 0) for u in usages), 0),
                "avg_output_tokens": _mean((u.output or 0 for u in usages), 0),
            }
        )
    return rows


def _phase_usage_entries(phase_usage: object) -> list[tuple[str, int, int, int]]:
    """Valid (phase, model_calls, context_tokens, duration_ms) entries of a TaskRun.phase_usage value.

    A value that is not a list, or an entry that is not a dict with a non-empty string phase and
    non-negative integer counts, is skipped rather than raised on: the column is written by the
    stream reader and read back from JSON, so its shape is not guaranteed.
    """
    if not isinstance(phase_usage, list):
        return []
    entries = []
    for entry in phase_usage:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("phase"), str)
            and entry["phase"]
            and _is_count(entry.get("model_calls"))
            and _is_count(entry.get("context_tokens"))
            and _is_count(entry.get("duration_ms"))
        ):
            entries.append((entry["phase"], entry["model_calls"], entry["context_tokens"], entry["duration_ms"]))
        else:
            log.debug("reliability.usage_by_step.skip_phase_entry", entry=repr(entry)[:200])
    return entries


async def get_usage_by_step(session: AsyncSession, days: int = 30, role: str | None = None) -> list[dict]:
    """Average usage per pipeline step and per command phase over done runs in the window.

    Pipeline steps ("step" rows) come from CostRecord rows keyed by CostRecord.phase (the step
    name), summed per run then averaged over the runs that executed the step: avg_turns (CLI
    num_turns, over the runs that reported it, None when none did), avg_context_tokens (input +
    cache_tokens), avg_output_tokens, avg_cost_usd and avg_duration_seconds. The
    "agent" phase of an agent-session run is not a step and is skipped.

    Command phases ("command_phase" rows) come from TaskRun.phase_usage of command:* runs, per
    (command, phase), with avg_model_calls instead of avg_turns: phase_usage tallies model
    calls, which are not the CLI's num_turns and are never derived from it (sova/llm/phase_usage.py).
    avg_cost_usd and avg_output_tokens are None for them, since no dollar or output figure is
    apportioned to a phase. A malformed phase_usage is skipped, not raised on.

    `role` keeps only runs whose _display_role() equals it (for example "developer:address-review"
    or "command:integrate-pr"), so the value a caller read from get_usage_by_role() round-trips.
    Only groups with at least one run are returned: steps first, then command phases, by name.
    """
    cutoff = _window_cutoff(days)
    runs = {run.id: run for run in await _load_done_runs(session, cutoff) if role is None or run.role == role}
    if not runs:
        return []

    window_ids = _done_run_ids(cutoff)
    rows = await _step_usage_rows(session, runs, window_ids)
    if any(run.role.startswith(_COMMAND_ROLE_PREFIX) for run in runs.values()):
        rows.extend(await _command_phase_usage_rows(session, runs, window_ids))
    return rows


async def _step_usage_rows(
    session: AsyncSession, runs: dict[int, _DoneRun], window_ids: ScalarSelect[int]
) -> list[dict]:
    """The "step" rows of get_usage_by_step(): CostRecord usage per step, averaged over the runs in `runs`."""
    step_rows = (
        await session.execute(
            select(
                CostRecord.task_run_id,
                CostRecord.phase,
                func.sum(CostRecord.num_turns).label("turns"),
                func.sum(CostRecord.input_tokens).label("input"),
                func.sum(CostRecord.cache_tokens).label("cache"),
                func.sum(CostRecord.output_tokens).label("output"),
                func.sum(CostRecord.cost_usd).label("cost"),
                func.sum(CostRecord.duration_ms).label("duration_ms"),
            )
            .where(CostRecord.task_run_id.in_(window_ids), CostRecord.phase != _AGENT_SESSION_PHASE)
            .group_by(CostRecord.task_run_id, CostRecord.phase)
        )
    ).all()
    by_step: defaultdict[str, list] = defaultdict(list)
    for row in step_rows:
        if row.task_run_id in runs:
            by_step[row.phase].append(row)

    return [
        {
            "kind": _KIND_STEP,
            "command": None,
            "name": name,
            "runs": len(group),
            "avg_turns": _mean(r.turns for r in group if r.turns is not None),
            "avg_model_calls": None,
            "avg_context_tokens": _mean(((r.input or 0) + (r.cache or 0) for r in group), 0),
            "avg_output_tokens": _mean((r.output or 0 for r in group), 0),
            "avg_cost_usd": round(sum((Decimal(str(r.cost or 0)) for r in group), Decimal("0")) / len(group), 4),
            "avg_duration_seconds": _mean(((r.duration_ms or 0) / 1000 for r in group)),
        }
        for name, group in sorted(by_step.items())
    ]


def _phase_totals(phase_usage: object) -> dict[str, list[int]]:
    """One run's phase_usage summed per phase into [model_calls, context_tokens, duration_ms]."""
    per_run: dict[str, list[int]] = {}
    for phase, model_calls, context_tokens, duration_ms in _phase_usage_entries(phase_usage):
        totals = per_run.setdefault(phase, [0, 0, 0])
        totals[0] += model_calls
        totals[1] += context_tokens
        totals[2] += duration_ms
    return per_run


async def _command_phase_usage_rows(
    session: AsyncSession, runs: dict[int, _DoneRun], window_ids: ScalarSelect[int]
) -> list[dict]:
    """The "command_phase" rows of get_usage_by_step(): phase_usage per (command, phase) over command:* runs."""
    usage_rows = (
        await session.execute(
            select(TaskRun.id, TaskRun.phase_usage).where(TaskRun.id.in_(window_ids), TaskRun.phase_usage.is_not(None))
        )
    ).all()
    # Per (command, phase), one summed (model_calls, context_tokens, duration_ms) per run.
    by_phase: defaultdict[tuple[str, str], list[list[int]]] = defaultdict(list)
    for run_id, phase_usage in usage_rows:
        run = runs.get(run_id)
        if run is None or not run.role.startswith(_COMMAND_ROLE_PREFIX):
            continue
        for phase, totals in _phase_totals(phase_usage).items():
            by_phase[(run.role, phase)].append(totals)

    return [
        {
            "kind": _KIND_COMMAND_PHASE,
            "command": command,
            "name": phase,
            "runs": len(group),
            "avg_turns": None,
            "avg_model_calls": _mean(g[0] for g in group),
            "avg_context_tokens": _mean((g[1] for g in group), 0),
            "avg_output_tokens": None,
            "avg_cost_usd": None,
            "avg_duration_seconds": _mean(g[2] / 1000 for g in group),
        }
        for (command, phase), group in sorted(by_phase.items())
    ]
