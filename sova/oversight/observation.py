"""Observation phase: collect a cross-project health snapshot.

Reads from three sources per project:
1. FleetService (soft import, optional)
2. Per-project SQLite databases (aiosqlite, read-only)
3. GitHub API via ``gh`` CLI

All collection is read-only. Failures are gracefully degraded: a project
that cannot be collected gets ``timed_out=True`` and ``failure_reason`` set
to ``"timeout"``, ``"db_error"``, or ``"error"`` depending on the cause.
The snapshot continues with partial data from other projects.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import aiosqlite

from sova.config.loader import load_config
from sova.config.registry import list_projects
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="oversight.observation")

_DB_FILENAME = "sova.db"
_DEFAULT_TIMEOUT = 30.0
_MIN_PER_PROJECT_TIMEOUT = 10.0
_GH_PAGINATION_LIMIT = 100
_GH_CLI_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentSlotSummary:
    """Agent slot capacity across the fleet."""

    total_max_slots: int = 0
    active_agents: int = 0


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Task run statistics for a single project."""

    total: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class PRSummary:
    """Open pull request summary."""

    number: int
    title: str
    state: str
    draft: bool = False


@dataclass(frozen=True, slots=True)
class IssueSummary:
    """Open issue summary."""

    number: int
    title: str
    labels: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProjectSnapshot:
    """Health snapshot for a single registered project.

    ``timed_out`` is True whenever collection failed for any reason.
    ``failure_reason`` distinguishes the cause: ``"timeout"`` (per-project
    budget exceeded), ``"db_error"`` (SQLite unreachable or locked),
    ``"error"`` (programming or filesystem error), or ``None`` on success.
    """

    slug: str
    path: str
    timed_out: bool = False
    failure_reason: str | None = None
    runs: RunSummary = field(default_factory=RunSummary)
    open_prs: list[PRSummary] = field(default_factory=list)
    open_issues: list[IssueSummary] = field(default_factory=list)


@dataclass(slots=True)
class OversightSnapshot:
    """Cross-project health snapshot collected during an oversight wake cycle."""

    collected_at: float = field(default_factory=time.time)
    projects: list[ProjectSnapshot] = field(default_factory=list)
    agent_slots: AgentSlotSummary = field(default_factory=AgentSlotSummary)
    partial: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------


async def build_snapshot(*, timeout: float = _DEFAULT_TIMEOUT) -> OversightSnapshot:
    """Collect a health snapshot across all registered projects.

    Args:
        timeout: Hard outer timeout in seconds for the entire collection.

    Returns:
        An ``OversightSnapshot`` with best-effort data per project.
    """
    registry = list_projects()
    if not registry:
        log.debug("oversight.observation.empty_registry")
        return OversightSnapshot()

    snapshot = OversightSnapshot()

    # Per-project time budget
    per_project = max(timeout / len(registry), _MIN_PER_PROJECT_TIMEOUT)

    try:
        async with asyncio.timeout(timeout):
            await _collect_all_projects(registry, per_project, snapshot)
    except (TimeoutError, asyncio.TimeoutError):
        log.warning(
            "oversight.observation.outer_timeout",
            timeout=timeout,
            collected=len(snapshot.projects),
            total=len(registry),
        )
        snapshot.partial = True

    # Agent slot summary: total_max_slots from FleetService config; active_agents from
    # per-project DB running counts (accurate) rather than FleetInsights.total_runs
    # (which is the all-time historical total, not current active agents).
    fleet_slots = await _collect_fleet_slots(registry)
    active_agents = sum(p.runs.running for p in snapshot.projects)
    snapshot.agent_slots = AgentSlotSummary(
        total_max_slots=fleet_slots.total_max_slots,
        active_agents=active_agents,
    )

    return snapshot


async def _collect_all_projects(
    registry: dict[str, str],
    per_project_timeout: float,
    snapshot: OversightSnapshot,
) -> None:
    """Collect data for all projects concurrently with per-project timeouts."""
    projects = [ProjectSnapshot(slug=slug, path=path_str) for slug, path_str in registry.items()]

    async def _guarded(project: ProjectSnapshot) -> None:
        try:
            await asyncio.wait_for(
                _collect_project(project, Path(project.path)),
                timeout=per_project_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("oversight.observation.project_timeout", slug=project.slug)
            project.timed_out = True
            project.failure_reason = "timeout"
        except Exception:
            log.warning("oversight.observation.project_error", slug=project.slug, exc_info=True)
            project.timed_out = True
            project.failure_reason = "error"

    await asyncio.gather(*(_guarded(p) for p in projects))
    snapshot.projects.extend(projects)


async def _collect_project(project: ProjectSnapshot, project_path: Path) -> None:
    """Collect DB and GitHub data for a single project."""
    # DB data (no existence check: _collect_db_data handles missing files gracefully)
    db_path = project_path / ".claude" / _DB_FILENAME
    await _collect_db_data(project, db_path)

    # GitHub data
    try:
        cfg = load_config(project_path)
    except Exception:
        log.debug("oversight.observation.config_load_failed", slug=project.slug, exc_info=True)
        return

    if cfg.github_repo:
        await _collect_github_data(project, cfg.github_repo)


# ---------------------------------------------------------------------------
# DB collection (aiosqlite, read-only)
# ---------------------------------------------------------------------------


async def _collect_db_data(project: ProjectSnapshot, db_path: Path) -> None:
    """Query task_runs from a project's SQLite database."""
    uri = db_path.as_uri() + "?mode=ro"
    try:
        async with aiosqlite.connect(uri, uri=True, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("PRAGMA query_only = ON")

            # Schema check
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'") as cur:
                if await cur.fetchone() is None:
                    log.warning("oversight.observation.no_task_runs_table", slug=project.slug)
                    return

            sql = """
                SELECT
                    COUNT(id)                                              AS total,
                    SUM(CASE WHEN status = 'running'  THEN 1 ELSE 0 END)  AS running,
                    SUM(CASE WHEN status = 'done'     THEN 1 ELSE 0 END)  AS done,
                    SUM(CASE WHEN status = 'failed'   THEN 1 ELSE 0 END)  AS failed
                FROM task_runs
            """
            async with db.execute(sql) as cur:
                row = await cur.fetchone()

            project.runs = RunSummary(
                total=row["total"] or 0,
                running=int(row["running"] or 0),
                done=int(row["done"] or 0),
                failed=int(row["failed"] or 0),
            )
    except aiosqlite.OperationalError:
        log.warning("oversight.observation.db_operational_error", slug=project.slug, exc_info=True)
        project.timed_out = True
        project.failure_reason = "db_error"
    except Exception:
        log.warning("oversight.observation.db_error", slug=project.slug, exc_info=True)
        project.timed_out = True
        project.failure_reason = "error"


# ---------------------------------------------------------------------------
# GitHub collection (gh CLI)
# ---------------------------------------------------------------------------


async def _collect_github_data(project: ProjectSnapshot, repo: str) -> None:
    """Fetch open PRs and issues from GitHub via the ``gh`` CLI."""
    pr_task = _fetch_open_prs(repo)
    issue_task = _fetch_open_issues(repo)
    prs, issues = await asyncio.gather(pr_task, issue_task, return_exceptions=True)

    if isinstance(prs, list):
        project.open_prs = prs
    else:
        log.debug("oversight.observation.gh_prs_failed", slug=project.slug, error=str(prs))

    if isinstance(issues, list):
        project.open_issues = issues
    else:
        log.debug("oversight.observation.gh_issues_failed", slug=project.slug, error=str(issues))


async def _fetch_gh_items(resource: str, repo: str, json_fields: str) -> list[dict]:
    """Fetch items from GitHub via ``gh {resource} list``. Returns parsed JSON or ``[]``."""
    result = await run(
        "gh",
        resource,
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        str(_GH_PAGINATION_LIMIT),
        "--json",
        json_fields,
        timeout=_GH_CLI_TIMEOUT,
    )
    if not result.success:
        log.debug("oversight.observation.gh_list_failed", resource=resource, returncode=result.returncode)
        return []

    if not result.stdout.strip():
        return []

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log.debug("oversight.observation.gh_parse_failed", resource=resource)
        return []


async def _fetch_open_prs(repo: str) -> list[PRSummary]:
    """Fetch open pull requests via ``gh pr list``."""
    items = await _fetch_gh_items("pr", repo, "number,title,state,isDraft")
    return [
        PRSummary(
            number=item["number"],
            title=item.get("title", ""),
            state=item.get("state", "OPEN"),
            draft=item.get("isDraft", False),
        )
        for item in items
    ]


async def _fetch_open_issues(repo: str) -> list[IssueSummary]:
    """Fetch open issues via ``gh issue list``."""
    items = await _fetch_gh_items("issue", repo, "number,title,labels")
    return [
        IssueSummary(
            number=item["number"],
            title=item.get("title", ""),
            labels=[lbl.get("name", "") for lbl in item.get("labels", [])],
        )
        for item in items
    ]


# ---------------------------------------------------------------------------
# Fleet slot collection (soft import)
# ---------------------------------------------------------------------------


async def _collect_fleet_slots(registry: dict[str, str]) -> AgentSlotSummary:
    """Attempt to read agent slot data from FleetService.

    FleetService is tied to the dashboard lifecycle and may not be available
    when the oversight agent runs standalone. Falls back to default values.
    """
    try:
        from sova.dashboard.services.fleet_service import FleetService

        service = FleetService()
        insights = await service.get_insights()
        total_slots = 0
        for slug in insights.projects_scanned:
            try:
                path = registry.get(slug)
                if path:
                    cfg = load_config(Path(path))
                    total_slots += cfg.max_parallel_agents
            except Exception:
                log.debug("oversight.observation.fleet_slot_config_error", slug=slug, exc_info=True)
        return AgentSlotSummary(
            total_max_slots=total_slots,
            active_agents=0,
        )
    except ImportError:
        log.debug("oversight.observation.fleet_service_unavailable")
        return AgentSlotSummary()
    except Exception:
        log.debug("oversight.observation.fleet_slots_failed", exc_info=True)
        return AgentSlotSummary()
