"""Fleet insights service: cross-project aggregation from local SOVA databases.

Reads ~/.config/sova/projects.json to discover registered projects, opens
short-lived read-only async sessions to each project's SQLite database, runs
aggregation queries, and merges results in Python. Entirely read-only: no
writes, no migrations.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import aiosqlite

from sova.config.models import FleetConfig
from sova.config.registry import list_projects
from sova.utils.logging import get_logger

log = get_logger(component="fleet")

_DB_FILENAME = "sova.db"
_MAX_CONCURRENT_SCANS = 10

# Regex patterns for error message normalization.
# UUID must precede generic hex to avoid partial UUID matches.
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_HEX_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", re.I)
_PATH_RE = re.compile(r"(?<![:\w])/[\w./\-]+")
_NUMERIC_RE = re.compile(r"\b\d+\b")


def _normalize_error(message: str) -> str:
    """Normalize an error message for failure clustering.

    Replaces variable parts (UUIDs, hex IDs, file paths, numeric IDs) with
    placeholders so semantically identical failures with different identifiers
    cluster together. Takes only the first line to strip stack traces.
    Regex order: UUID before generic hex to prevent partial UUID matches.
    """
    msg = message.split("\n")[0].strip()
    msg = _UUID_RE.sub("<UUID>", msg)
    msg = _HEX_RE.sub("<HEX>", msg)
    msg = _PATH_RE.sub("<PATH>", msg)
    msg = _NUMERIC_RE.sub("<N>", msg)
    return msg[:120]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepFailureStat:
    """Failure rate for a single pipeline step across the fleet."""

    step_name: str
    total_count: int
    failure_count: int
    failure_rate: float


@dataclass(frozen=True, slots=True)
class FailureCluster:
    """A recurring failure pattern (grouped by message prefix)."""

    pattern: str
    count: int
    projects: list[str]


@dataclass(frozen=True, slots=True)
class ProjectCostStat:
    """Cost statistics for a single project."""

    slug: str
    run_count: int
    total_cost_usd: Decimal
    avg_cost_per_run: Decimal


@dataclass(frozen=True, slots=True)
class FleetInsights:
    """Aggregated insights across all registered SOVA projects."""

    generated_at: float
    projects_scanned: list[str]
    projects_skipped: list[str]

    total_runs: int
    total_cost_usd: Decimal
    success_rate: float
    retry_success_rate: float

    step_failure_stats: list[StepFailureStat]
    failure_clusters: list[FailureCluster]
    cost_by_project: list[ProjectCostStat]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class FleetService:
    """Cross-project fleet aggregation service.

    Instantiated by consumers (e.g. a router in issue #431). Not wired into
    the app factory or lifespan in this issue.
    """

    def __init__(self, config: FleetConfig | None = None) -> None:
        self._cfg = config or FleetConfig()
        self._cache: FleetInsights | None = None
        self._cache_time: float = 0.0
        self._refresh_lock = asyncio.Lock()

    async def get_insights(self, *, force_refresh: bool = False) -> FleetInsights:
        """Return aggregated fleet insights, using cache when valid."""
        now = time.monotonic()
        if not force_refresh and self._cache is not None and (now - self._cache_time) < self._cfg.cache_ttl_seconds:
            return self._cache

        async with self._refresh_lock:
            # Re-check after acquiring lock (another caller may have refreshed)
            now = time.monotonic()
            if not force_refresh and self._cache is not None and (now - self._cache_time) < self._cfg.cache_ttl_seconds:
                return self._cache

            insights = await self._scan_all_projects()
            self._cache = insights
            self._cache_time = now
            return insights

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _scan_all_projects(self) -> FleetInsights:
        """Scan every registered project and merge results."""
        registry = list_projects()
        try:
            remote_runs, remote_steps = await self._query_telemetry_events()
        except Exception:
            log.debug("fleet.telemetry_query_failed", exc_info=True)
            remote_runs, remote_steps = [], []

        if not registry:
            if not remote_runs:
                return self._empty_insights()
            return self._merge([], [], remote_runs, remote_steps, [], [])

        skipped, queryable = self._partition_projects(registry)

        if not queryable:
            return self._merge([], skipped, remote_runs, remote_steps, [], [])

        scanned, all_runs, all_steps, all_failures, all_resumed, query_skipped = await self._run_project_queries(
            queryable
        )
        skipped.extend(query_skipped)

        all_runs.extend(remote_runs)
        all_steps.extend(remote_steps)

        return self._merge(scanned, skipped, all_runs, all_steps, all_failures, all_resumed)

    @staticmethod
    def _partition_projects(registry: dict[str, str]) -> tuple[list[str], list[tuple[str, Path]]]:
        """Split registry into skipped (no DB) and queryable (has DB) lists."""
        skipped: list[str] = []
        queryable: list[tuple[str, Path]] = []
        for slug, path_str in registry.items():
            db_path = Path(path_str) / ".claude" / _DB_FILENAME
            if not db_path.exists():
                skipped.append(slug)
            else:
                queryable.append((slug, db_path))
        return skipped, queryable

    async def _run_project_queries(
        self, queryable: list[tuple[str, Path]]
    ) -> tuple[list[str], list[_RunRow], list[_StepRow], list[_FailureRow], list[_ResumedRow], list[str]]:
        """Run queries across all queryable projects with concurrency control."""
        sem = asyncio.Semaphore(_MAX_CONCURRENT_SCANS)

        async def _safe_query(slug: str, db_path: Path) -> tuple[str, _ProjectResult | None]:
            async with sem:
                try:
                    result = await asyncio.wait_for(
                        self._query_project(slug, db_path),
                        timeout=self._cfg.query_timeout_seconds,
                    )
                    return slug, result
                except (TimeoutError, asyncio.TimeoutError):
                    log.warning("fleet.query_timeout", slug=slug, exc_info=True)
                    return slug, None
                except Exception:
                    log.warning("fleet.query_failed", slug=slug, exc_info=True)
                    return slug, None

        results = await asyncio.gather(*[_safe_query(s, p) for s, p in queryable])

        scanned: list[str] = []
        skipped: list[str] = []
        all_runs: list[_RunRow] = []
        all_steps: list[_StepRow] = []
        all_failures: list[_FailureRow] = []
        all_resumed: list[_ResumedRow] = []

        for slug, result in results:
            if result is None:
                skipped.append(slug)
                continue
            scanned.append(slug)
            runs, steps, failures, resumed = result
            all_runs.extend(runs)
            all_steps.extend(steps)
            all_failures.extend(failures)
            all_resumed.extend(resumed)

        return scanned, all_runs, all_steps, all_failures, all_resumed, skipped

    async def _query_project(
        self,
        slug: str,
        db_path: Path,
    ) -> _ProjectResult:
        """Open a raw aiosqlite connection, run all queries, close.

        Uses aiosqlite directly (not SQLAlchemy sessions) so that a query
        failure on an incompatible schema does not leave a pending transaction
        that causes session.close() to hang on Python 3.12.
        """
        timeout_ms = int(self._cfg.query_timeout_seconds * 1000)
        # Open read-only via SQLite URI; timeout= bounds lock-acquisition on connect.
        uri = db_path.as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True, timeout=self._cfg.query_timeout_seconds) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(f"PRAGMA busy_timeout = {timeout_ms}")
            await db.execute("PRAGMA query_only = ON")

            # Schema check: database must have the task_runs table
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'") as cur:
                if await cur.fetchone() is None:
                    raise RuntimeError(f"SOVA tables not found in {db_path}")

            runs = await self._query_runs(db, slug)
            steps = await self._query_steps(db)
            failures = await self._query_failures(db, slug)
            resumed = await self._query_resumed_runs(db)

        return runs, steps, failures, resumed

    # ------------------------------------------------------------------
    # Per-project queries (read-only, raw SQL via aiosqlite)
    # ------------------------------------------------------------------

    @staticmethod
    async def _query_runs(db: aiosqlite.Connection, slug: str) -> list[_RunRow]:
        """Count runs by status for success rate calculation."""
        sql = """
            SELECT
                COUNT(id)                                              AS total,
                SUM(CASE WHEN status = 'done'   THEN 1 ELSE 0 END)   AS done,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)   AS failed,
                COALESCE(SUM(total_cost_usd), 0)                      AS cost
            FROM task_runs
        """
        try:
            async with db.execute(sql) as cur:
                row = await cur.fetchone()
            return [
                _RunRow(
                    slug=slug,
                    total=row["total"] or 0,
                    done=int(row["done"] or 0),
                    failed=int(row["failed"] or 0),
                    cost=Decimal(str(row["cost"] or 0)),
                )
            ]
        except Exception:
            log.warning("fleet.query_runs_failed", slug=slug, exc_info=True)
            return []

    @staticmethod
    async def _query_steps(db: aiosqlite.Connection) -> list[_StepRow]:
        """Step-level failure rates."""
        sql = """
            SELECT
                step_name,
                COUNT(id)                                              AS total,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)   AS failures
            FROM step_executions
            GROUP BY step_name
        """
        try:
            async with db.execute(sql) as cur:
                rows = await cur.fetchall()
            return [
                _StepRow(step_name=row["step_name"], total=row["total"], failures=int(row["failures"] or 0))
                for row in rows
            ]
        except Exception:
            log.warning("fleet.query_steps_failed", exc_info=True)
            return []

    @staticmethod
    async def _query_failures(db: aiosqlite.Connection, slug: str) -> list[_FailureRow]:
        """Top failure messages for clustering."""
        sql = """
            SELECT message, COUNT(id) AS cnt
            FROM failure_records
            GROUP BY message
            ORDER BY cnt DESC
            LIMIT 50
        """
        try:
            async with db.execute(sql) as cur:
                rows = await cur.fetchall()
            return [_FailureRow(slug=slug, message=row["message"], count=row["cnt"]) for row in rows]
        except Exception:
            log.warning("fleet.query_failures_failed", slug=slug, exc_info=True)
            return []

    @staticmethod
    async def _query_resumed_runs(db: aiosqlite.Connection) -> list[_ResumedRow]:
        """Resumed-run success rate: runs linked via resumed_from_id."""
        sql = """
            SELECT
                COUNT(id)                                              AS resumed_total,
                SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END)      AS resumed_done
            FROM task_runs
            WHERE resumed_from_id IS NOT NULL
        """
        try:
            async with db.execute(sql) as cur:
                row = await cur.fetchone()
            return [_ResumedRow(resumed_total=row["resumed_total"] or 0, resumed_done=int(row["resumed_done"] or 0))]
        except Exception:
            # resumed_from_id column may not exist in old schemas
            log.debug("fleet.query_resumed_runs_failed", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def _merge(
        self,
        scanned: list[str],
        skipped: list[str],
        runs: list[_RunRow],
        steps: list[_StepRow],
        failures: list[_FailureRow],
        resumed: list[_ResumedRow],
    ) -> FleetInsights:
        total_runs = sum(r.total for r in runs)
        total_done = sum(r.done for r in runs)
        total_cost = sum((r.cost for r in runs), Decimal(0))
        success_rate = (total_done / total_runs) if total_runs > 0 else 0.0

        # Resumed-run success rate (runs linked via resumed_from_id)
        resumed_total = sum(r.resumed_total for r in resumed)
        resumed_done = sum(r.resumed_done for r in resumed)
        retry_success_rate = (resumed_done / resumed_total) if resumed_total > 0 else 0.0

        # Step failure stats (merge across projects)
        step_totals: dict[str, list[int]] = {}
        for s in steps:
            entry = step_totals.setdefault(s.step_name, [0, 0])
            entry[0] += s.total
            entry[1] += s.failures
        step_stats = [
            StepFailureStat(
                step_name=name,
                total_count=vals[0],
                failure_count=vals[1],
                failure_rate=vals[1] / vals[0],
            )
            for name, vals in sorted(step_totals.items())
            if vals[0] > 0
        ]
        step_stats.sort(key=lambda s: s.failure_rate, reverse=True)

        # Failure clusters (normalized to collapse messages that differ only in IDs/paths)
        msg_counts: dict[str, tuple[int, set[str]]] = {}
        for f in failures:
            prefix = _normalize_error(f.message) if f.message else "(empty)"
            count, slugs = msg_counts.get(prefix, (0, set()))
            msg_counts[prefix] = (count + f.count, slugs | {f.slug})
        clusters = [
            FailureCluster(pattern=pattern, count=count, projects=sorted(slugs))
            for pattern, (count, slugs) in msg_counts.items()
        ]
        clusters.sort(key=lambda c: c.count, reverse=True)
        clusters = clusters[:20]

        # Cost by project (derived from run data)
        cost_by_project = [
            ProjectCostStat(
                slug=r.slug,
                run_count=r.total,
                total_cost_usd=round(r.cost, 6),
                avg_cost_per_run=round(r.cost / r.total, 6) if r.total > 0 else Decimal(0),
            )
            for r in runs
        ]
        cost_by_project.sort(key=lambda p: p.total_cost_usd, reverse=True)

        return FleetInsights(
            generated_at=time.time(),
            projects_scanned=scanned,
            projects_skipped=skipped,
            total_runs=total_runs,
            total_cost_usd=round(total_cost, 6),
            success_rate=round(success_rate, 4),
            retry_success_rate=round(retry_success_rate, 4),
            step_failure_stats=step_stats,
            failure_clusters=clusters,
            cost_by_project=cost_by_project,
        )

    async def _query_telemetry_events(self) -> tuple[list[_RunRow], list[_StepRow]]:
        """Query TelemetryEvent table for remote run data.

        Uses SQL aggregation for run-level totals. Step-level stats still
        require Python-side parsing of step_outcomes JSON values.
        Falls back to empty lists if the table does not exist (fresh DB).
        """
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import case, func, select

        from sova.db.session import get_session

        try:
            from sova.db.models import TelemetryEvent

            cutoff = datetime.now(timezone.utc) - timedelta(days=self._cfg.telemetry_window_days)
            run_rows = await _query_telemetry_run_aggregates(TelemetryEvent, get_session, func, case, select, cutoff)
            step_rows = await _query_telemetry_step_stats(TelemetryEvent, get_session, select, cutoff)

            return run_rows, step_rows
        except Exception:
            log.debug("fleet.telemetry_query_failed", exc_info=True)
            return [], []

    def _empty_insights(self) -> FleetInsights:
        return FleetInsights(
            generated_at=time.time(),
            projects_scanned=[],
            projects_skipped=[],
            total_runs=0,
            total_cost_usd=Decimal(0),
            success_rate=0.0,
            retry_success_rate=0.0,
            step_failure_stats=[],
            failure_clusters=[],
            cost_by_project=[],
        )


# ---------------------------------------------------------------------------
# Telemetry query helpers (module-level to reduce class cognitive complexity)
# ---------------------------------------------------------------------------


async def _query_telemetry_run_aggregates(
    model: type,
    get_session_fn: object,
    func: object,
    case: object,
    select: object,
    cutoff: object = None,
) -> list[_RunRow]:
    """Run SQL aggregation for telemetry run totals, grouped by machine_id + project_slug."""
    async with await get_session_fn() as session:  # type: ignore[operator]
        stmt = select(  # type: ignore[operator]
            model.machine_id,
            model.project_slug,
            func.count(model.id).label("total"),
            func.sum(case((model.status == "done", 1), else_=0)).label("done"),
            func.sum(case((model.status == "failed", 1), else_=0)).label("failed"),
            func.coalesce(func.sum(model.cost_usd), 0).label("cost"),
        )
        if cutoff is not None:
            stmt = stmt.where(model.received_at >= cutoff)
        stmt = stmt.group_by(model.machine_id, model.project_slug)
        result = await session.execute(stmt)
        rows = result.all()

    return [
        _RunRow(
            slug=f"remote:{row.machine_id}:{row.project_slug}",
            total=row.total,
            done=int(row.done),
            failed=int(row.failed),
            cost=Decimal(str(row.cost)),
        )
        for row in rows
    ]


async def _query_telemetry_step_stats(
    model: type,
    get_session_fn: object,
    select: object,
    cutoff: object = None,
) -> list[_StepRow]:
    """Parse step_outcomes JSON from telemetry events for step-level stats."""
    async with await get_session_fn() as session:  # type: ignore[operator]
        stmt = select(model.step_outcomes).where(model.step_outcomes.isnot(None))  # type: ignore[operator]
        if cutoff is not None:
            stmt = stmt.where(model.received_at >= cutoff)
        result = await session.execute(stmt)
        outcomes_list = result.scalars().all()

    step_stats: dict[str, list[int]] = {}
    for outcomes in outcomes_list:
        if not isinstance(outcomes, dict):
            continue
        for step_name, outcome in outcomes.items():
            try:
                if isinstance(outcome, str):
                    status = outcome
                elif isinstance(outcome, dict):
                    raw = outcome.get("status", "")
                    status = str(raw) if not isinstance(raw, str) else raw
                else:
                    continue
            except Exception:
                continue
            counts = step_stats.setdefault(step_name, [0, 0])
            counts[0] += 1
            if status == "failed":
                counts[1] += 1

    return [_StepRow(step_name=name, total=counts[0], failures=counts[1]) for name, counts in step_stats.items()]


# ---------------------------------------------------------------------------
# Internal row types for per-project query results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RunRow:
    slug: str
    total: int
    done: int
    failed: int
    cost: Decimal


@dataclass(frozen=True, slots=True)
class _StepRow:
    step_name: str
    total: int
    failures: int


@dataclass(frozen=True, slots=True)
class _FailureRow:
    slug: str
    message: str
    count: int


@dataclass(frozen=True, slots=True)
class _ResumedRow:
    resumed_total: int
    resumed_done: int


# Result tuple from _query_project
_ProjectResult = tuple[list[_RunRow], list[_StepRow], list[_FailureRow], list[_ResumedRow]]


# ---------------------------------------------------------------------------
# Step-to-area label mapping and issue draft builder
# ---------------------------------------------------------------------------

STEP_AREA_MAP: dict[str, str] = {
    "sync": "core",
    "assess": "core",
    "develop": "core",
    "simplify": "core",
    "self_review": "core",
    "commit": "core",
    "validate": "core",
    "capture_baseline": "core",
    "extract_memory": "core",
    "monitor_ci": "core",
    "scan_project": "core",
    "generate_tasks": "core",
    "validate_tasks": "core",
    "fetch_task": "adapters",
    "create_pr": "adapters",
    "push": "adapters",
    "wait_for_external_reviews": "adapters",
    "address_external_findings": "adapters",
    "resolve_external_reviews": "adapters",
    "research": "adapters",
    "spec": "adapters",
    "create_worktree": "agent",
    "ensure_worktree": "agent",
    "rebase": "agent",
    "rearrange_commits": "agent",
    "handoff_to_reviewer": "agent",
    "handoff_to_user": "agent",
    "address_review": "agent",
}

_DEFAULT_AREA = "core"


def build_issue_draft(step_name: str, insights: FleetInsights) -> dict[str, object] | None:
    """Build a pre-filled issue draft from fleet failure data for a given step.

    Returns ``{"title": ..., "body": ..., "labels": [...]}`` or None if the
    step has no failure data in the current insights.
    """
    stat = next((s for s in insights.step_failure_stats if s.step_name == step_name), None)
    if stat is None:
        return None

    area = STEP_AREA_MAP.get(step_name, _DEFAULT_AREA)
    rate_pct = f"{stat.failure_rate * 100:.1f}"

    title = f"fix({area}): {step_name} fails in {rate_pct}% of runs"

    clusters = [c for c in insights.failure_clusters if c.count > 0][:5]

    body_parts = [
        (
            f"## Problem\n\n"
            f"`{step_name}` fails in {rate_pct}% of runs "
            f"({stat.failure_count} failures out of {stat.total_count} executions) "
            f"across the fleet.\n"
        ),
    ]

    if clusters:
        body_parts.append("## Top error messages\n")
        for i, cluster in enumerate(clusters, 1):
            projects_str = ", ".join(cluster.projects) if cluster.projects else "unknown"
            body_parts.append(f'{i}. "{cluster.pattern}" ({cluster.count} occurrences, projects: {projects_str})')
        body_parts.append("")

    body_parts.append(
        "## Suggested investigation\n\n"
        f"- [ ] Review `sova/core/steps/{step_name}.py` for timeout or error handling gaps\n"
        "- [ ] Check whether the failure correlates with specific project configurations\n"
        "- [ ] Add targeted test coverage for the failure scenario\n\n"
        "*Proposed by SOVA fleet self-improvement loop*"
    )

    labels = ["type: bug", f"area: {area}"]

    return {"title": title, "body": "\n".join(body_parts), "labels": labels}
