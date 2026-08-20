"""Fleet Manager service: live cross-project aggregation for the command center.

On-demand service (no daemon) that aggregates live agent counts, queue depth,
and CodeRabbit quota across all registered projects. Reads from agent_pool
in-memory state and per-project SQLite DBs via raw aiosqlite (following the
FleetService._query_project() pattern).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from sova.config.registry import ProjectEntry, get_project_entries, list_projects
from sova.dashboard.services.agent_pool import list_all_pools, read_max_parallel
from sova.utils.logging import get_logger

log = get_logger(component="fleet_manager")

_DB_FILENAME = "sova.db"
_QUERY_TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class ProjectFleetStatus:
    """Live status for a single project in the fleet."""

    slug: str
    path: str
    fleet_priority: int
    active_agents: int
    max_concurrent: int
    queued_tasks: int
    coderabbit_reviews_in_window: int
    coderabbit_reviews_per_hour: int
    coderabbit_can_create: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FleetStatus:
    """Aggregated fleet status across all projects."""

    projects: list[ProjectFleetStatus]
    total_active_agents: int
    total_max_slots: int
    total_queued: int
    global_coderabbit_used: int
    global_coderabbit_limit: int
    global_coderabbit_can_create: bool


class FleetManagerService:
    """On-demand fleet status aggregation service."""

    async def get_fleet_status(self) -> FleetStatus:
        """Aggregate live status from all registered projects."""
        entries = get_project_entries()
        if not entries:
            return self._empty_status()

        pools = list_all_pools()
        project_statuses: list[ProjectFleetStatus] = []

        tasks = []
        for slug, entry in entries.items():
            pool = pools.get(slug)
            tasks.append(self._get_project_status(slug, entry, pool))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        all_cr_review_ids: set[str] = set()
        for result in results:
            if isinstance(result, tuple):
                status, cr_ids = result
                project_statuses.append(status)
                all_cr_review_ids.update(cr_ids)
            elif isinstance(result, Exception):
                log.warning("fleet_manager.project_error", error=str(result), exc_info=True)

        project_statuses.sort(key=lambda p: (p.fleet_priority, p.slug))

        total_active = sum(p.active_agents for p in project_statuses)
        total_max = sum(p.max_concurrent for p in project_statuses)
        total_queued = sum(p.queued_tasks for p in project_statuses)

        # Global CodeRabbit: deduplicate by review_id across projects
        global_cr_limit = max((p.coderabbit_reviews_per_hour for p in project_statuses), default=0)
        global_cr_used = len(all_cr_review_ids)
        global_cr_can_create = global_cr_limit == 0 or global_cr_used < global_cr_limit

        return FleetStatus(
            projects=project_statuses,
            total_active_agents=total_active,
            total_max_slots=total_max,
            total_queued=total_queued,
            global_coderabbit_used=global_cr_used,
            global_coderabbit_limit=global_cr_limit,
            global_coderabbit_can_create=global_cr_can_create,
        )

    def set_max_concurrent(self, slug: str, value: int) -> bool:
        """Update max_concurrent for a project (in-memory + TOML write-back).

        Returns True if successfully updated.
        """
        projects = list_projects()
        path_str = projects.get(slug)
        if path_str is None:
            return False

        # Update in-memory pool if it exists
        pools = list_all_pools()
        if slug in pools:
            pools[slug].max_concurrent = value

        # TOML write-back for persistence
        project_dir = Path(path_str)
        self._write_max_concurrent_to_toml(project_dir, value)
        return True

    async def _get_project_status(
        self,
        slug: str,
        entry: ProjectEntry,
        pool: object | None,
    ) -> tuple[ProjectFleetStatus, set[str]]:
        """Build status for a single project.

        Returns (status, cr_review_ids) where cr_review_ids is the set of
        CodeRabbit review IDs from this project's DB (for global dedup).
        """
        from sova.dashboard.services.agent_pool import ProjectAgents

        active_agents = 0
        max_concurrent = 2  # default

        if isinstance(pool, ProjectAgents):
            active_agents = len(pool.agents)
            max_concurrent = pool.max_concurrent
        else:
            max_concurrent = read_max_parallel(Path(entry.path))

        queued = 0
        cr_reviews = 0
        cr_review_ids: set[str] = set()
        cr_limit = self._read_coderabbit_limit(Path(entry.path))
        error: str | None = None

        db_path = Path(entry.path) / ".claude" / _DB_FILENAME
        if not db_path.exists():
            error = "DB not found"
        else:
            try:
                db_data = await asyncio.wait_for(
                    self._query_project_db(db_path),
                    timeout=_QUERY_TIMEOUT,
                )
                queued = db_data.get("queued", 0)
                cr_reviews = db_data.get("cr_reviews", 0)
                cr_review_ids = db_data.get("cr_review_ids", set())
            except (TimeoutError, asyncio.TimeoutError):
                error = "DB query timed out"
                log.warning("fleet_manager.db_timeout", slug=slug)
            except Exception as exc:
                error = f"DB error: {exc}"
                log.warning("fleet_manager.db_error", slug=slug, exc_info=True)

        cr_can_create = cr_limit == 0 or cr_reviews < cr_limit

        status = ProjectFleetStatus(
            slug=slug,
            path=entry.path,
            fleet_priority=entry.fleet_priority,
            active_agents=active_agents,
            max_concurrent=max_concurrent,
            queued_tasks=queued,
            coderabbit_reviews_in_window=cr_reviews,
            coderabbit_reviews_per_hour=cr_limit,
            coderabbit_can_create=cr_can_create,
            error=error,
        )
        return status, cr_review_ids

    async def _query_project_db(self, db_path: Path) -> dict:
        """Query a project's SQLite DB for queue depth and CodeRabbit quota.

        Returns dict with keys: queued (int), cr_reviews (int), cr_review_ids (set[str]).
        """
        uri = db_path.as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True, timeout=_QUERY_TIMEOUT) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("PRAGMA query_only = ON")

            result: dict = {"queued": 0, "cr_reviews": 0, "cr_review_ids": set()}

            # Queue depth query
            try:
                async with db.execute(
                    "SELECT COUNT(id) AS cnt FROM task_runs "
                    "WHERE status NOT IN ('done','failed','rejected','interrupted','paused')"
                ) as cur:
                    row = await cur.fetchone()
                    result["queued"] = (row["cnt"] or 0) if row else 0
            except Exception:
                log.debug("fleet_manager.queue_query_failed", db=str(db_path), exc_info=True)

            # CodeRabbit review IDs (for cross-project dedup)
            try:
                cr_ids: set[str] = set()
                async with db.execute(
                    "SELECT review_id FROM coderabbit_events"
                    " WHERE event_type = 'review'"
                    " AND recorded_at > datetime('now', '-60 minutes')"
                ) as cur:
                    async for row in cur:
                        cr_ids.add(row["review_id"])
                result["cr_reviews"] = len(cr_ids)
                result["cr_review_ids"] = cr_ids
            except Exception:
                # coderabbit_events table may not exist in older DBs
                log.debug("fleet_manager.cr_query_failed", db=str(db_path), exc_info=True)

        return result

    @staticmethod
    def _read_coderabbit_limit(project_dir: Path) -> int:
        """Read CodeRabbit reviews_per_hour from project config."""
        try:
            from sova.config.loader import load_config

            cfg = load_config(project_dir)
            return cfg.coderabbit_quota.reviews_per_hour or 0
        except Exception:
            return 0

    @staticmethod
    def _write_max_concurrent_to_toml(project_dir: Path, value: int) -> None:
        """Write max_parallel_agents back to sova.toml using tomlkit."""
        toml_path = project_dir / "sova.toml"
        if not toml_path.exists():
            return
        try:
            import tomlkit

            doc = tomlkit.parse(toml_path.read_text())
            doc["max_parallel_agents"] = value
            toml_path.write_text(tomlkit.dumps(doc))
        except ImportError:
            log.debug("fleet_manager.tomlkit_unavailable")
        except Exception:
            log.warning("fleet_manager.toml_write_failed", exc_info=True)

    @staticmethod
    def _empty_status() -> FleetStatus:
        return FleetStatus(
            projects=[],
            total_active_agents=0,
            total_max_slots=0,
            total_queued=0,
            global_coderabbit_used=0,
            global_coderabbit_limit=0,
            global_coderabbit_can_create=True,
        )
