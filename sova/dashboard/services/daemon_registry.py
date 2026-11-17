"""Daemon registries: which supervisor daemon runs for which project, the other background components, and the locks.

Services import from here rather than from ``sova.dashboard.app`` or a router: ``app.py`` populates these in its
lifespan and the services read them, so the dependency points one way. Holds the supervisor daemon registry and its
lifecycle locks, the per-project component registry (watchdog, monitors, PR throttle loops), the PR throttle starter
and the oversight agent holder.

The registry is a plain ``dict`` keyed by the resolved project path. ``app.py``'s lifespan owns the dict it
shares through ``set_daemon_registry()``, which REBINDS the module global, so every function here reads
``_daemon_registry`` at call time and never holds a reference across calls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from sova.config.context import get_project_dir, resolve_project_dir_or_none
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from sova.config.models import ProjectConfig
    from sova.dashboard.services.merge_queue_monitor import MergeQueueMonitor
    from sova.oversight.agent import OversightAgent
    from sova.supervisor.pr_monitor import PRMonitor
    from sova.supervisor.pr_throttle import PRThrottleLoop
    from sova.supervisor.watchdog import AgentWatchdog

log = get_logger(component="dashboard.service.daemon_registry")

_daemon_registry: dict[str, Any] = {}
# Lifecycle locks, keyed by the same resolved project path ``register_daemon()`` uses. Created on first use and
# dropped by ``clear_registries()`` at shutdown, so a lock never outlives the event loop that contended it.
_daemon_lifecycle_locks: dict[str, asyncio.Lock] = {}
_oversight_lifecycle_lock: asyncio.Lock | None = None


def get_registry() -> dict[str, Any]:
    """Return the live registry dict (the one ``set_daemon_registry()`` last installed)."""
    return _daemon_registry


def set_daemon_registry(registry: dict[str, Any]) -> None:
    """Called by the app lifespan to share its daemon registry."""
    global _daemon_registry  # noqa: PLW0603
    _daemon_registry = registry


def get_daemon(project_dir: Path | None = None) -> Any | None:
    """Get the daemon instance for the current project, if any.

    In single-project mode get_project_dir() returns None because no
    multi-project middleware sets the context var.  Fall back to the sole
    registry entry when exactly one daemon is registered.
    """
    pd = project_dir or get_project_dir()
    if pd is not None:
        return _daemon_registry.get(str(pd.resolve()))
    if len(_daemon_registry) == 1:
        return next(iter(_daemon_registry.values()))
    return None


def register_daemon(project_dir: Path, daemon: Any) -> None:
    """Record *daemon* as the one running for *project_dir*."""
    _daemon_registry[str(project_dir.resolve())] = daemon


def unregister_daemon(project_dir: Path | None) -> None:
    """Drop the daemon registered for *project_dir*.

    With no project dir, the sole registry entry is dropped (single-project mode); an ambiguous registry is
    left alone.
    """
    key = str(project_dir.resolve()) if project_dir else None
    if key is None and len(_daemon_registry) == 1:
        key = next(iter(_daemon_registry))
    if key is not None:
        _daemon_registry.pop(key, None)


def get_lifecycle_lock(project_dir: Path) -> asyncio.Lock:
    """Return a per-project async lock for serializing daemon lifecycle transitions.

    Keyed on the resolved path, the key ``register_daemon()`` uses, so every path that registers a daemon for a
    project takes the same lock. The project is required: a caller resolves it first, so the lock key and the
    registry key can never name different projects.
    """
    key = str(project_dir.resolve())
    if key not in _daemon_lifecycle_locks:
        _daemon_lifecycle_locks[key] = asyncio.Lock()
    return _daemon_lifecycle_locks[key]


def get_oversight_lifecycle_lock() -> asyncio.Lock:
    """Return the lock serializing oversight agent start, stop and reload (one agent per process)."""
    global _oversight_lifecycle_lock  # noqa: PLW0603
    if _oversight_lifecycle_lock is None:
        _oversight_lifecycle_lock = asyncio.Lock()
    return _oversight_lifecycle_lock


async def reload_daemon_config(project_dir: Path | None) -> None:
    """Hot-reload supervisor daemon config after settings update.

    Lifecycle-aware: stops the daemon when ``supervisor.enabled`` becomes
    False, starts a new one when it becomes True. Serialized per project
    to prevent concurrent enable/disable races.

    The project is resolved once (the argument, else the context var, else the single-project mode's startup
    project) and that one key serves the lock and the registry reads and writes, so a stopped daemon is always
    unregistered and a start never overwrites another project's entry. With no project in scope (a multi-project
    request that names none) nothing is started or stopped, in either direction: the root config decides nothing
    about a project's daemon. The ``/supervisor/start`` and ``/stop`` routes take the same lock.

    Raises on lifecycle failure so the caller can report it to the UI.
    """
    from sova.config.loader import load_config
    from sova.dashboard.services.agent_pool import get_default_project_dir

    project_dir = resolve_project_dir_or_none(project_dir, default_project_dir=get_default_project_dir())
    if project_dir is None:
        log.debug("settings.daemon_reload_skipped_no_project", registered=len(_daemon_registry))
        return

    async with get_lifecycle_lock(project_dir):
        cfg = await asyncio.to_thread(load_config, project_dir)
        daemon = get_daemon(project_dir)

        if not cfg.supervisor.enabled:
            if daemon is not None and daemon.running:
                await daemon.stop()
                unregister_daemon(project_dir)
                log.info("settings.daemon_stopped_on_disable")
            elif daemon is not None:
                daemon.reload_config(cfg)
        elif daemon is not None:
            daemon.reload_config(cfg)
        else:
            from sova.db.session import get_session_factory
            from sova.supervisor.daemon import SupervisorDaemon

            resolved = project_dir
            session_factory = await get_session_factory(resolved)
            new_daemon = SupervisorDaemon(config=cfg, project_dir=resolved, session_factory=session_factory)
            new_daemon.start()
            register_daemon(resolved, new_daemon)
            log.info("settings.daemon_started_on_enable")


# ----------------------------------------------------------------------
# Background component registry and PR throttle start
# ----------------------------------------------------------------------


class _DaemonComponents(TypedDict, total=False):
    """Per-project registry of background daemon instances for config reload."""

    watchdog: AgentWatchdog
    pr_monitors: list[PRMonitor]
    pr_throttles: list[PRThrottleLoop]
    merge_queue_monitors: list[MergeQueueMonitor]


_daemon_components: dict[str, _DaemonComponents] = {}


def get_daemon_components() -> dict[str, _DaemonComponents]:
    """Return the daemon component registry (keyed by resolved project path)."""
    return _daemon_components


def clear_registries() -> None:
    """Forget the component registry, the PR throttle tasks and the lifecycle locks (app shutdown, tests).

    Mutates in place, so a holder of one of these containers never keeps a stale one.
    """
    global _oversight_lifecycle_lock  # noqa: PLW0603
    _daemon_components.clear()
    _pr_throttle_tasks.clear()
    _daemon_lifecycle_locks.clear()
    _oversight_lifecycle_lock = None


def set_daemon_components(registry: dict[str, _DaemonComponents]) -> None:
    """Replace the daemon component registry."""
    global _daemon_components  # noqa: PLW0603
    _daemon_components = registry


# Throttle loop tasks started by ensure_pr_throttle_loop(), from the lifespan scan or a later settings
# change. Module level because the settings router starts loops long after the lifespan's locals are fixed.
_pr_throttle_tasks: list[asyncio.Task] = []


def get_pr_throttle_tasks() -> list[asyncio.Task]:
    """A snapshot of the throttle loop tasks started so far (the lifespan stops them at shutdown)."""
    return list(_pr_throttle_tasks)


async def ensure_pr_throttle_loop(project_dir: Path, cfg: ProjectConfig) -> bool:
    """Start *project_dir*'s PR throttle loop from its own config, unless one is already registered.

    The one start path for the lifespan scan and for the settings router when ``coderabbit_quota.enabled``
    is switched on at runtime. Idempotent: two loops on one project could pick the same pending entry
    and open a duplicate PR, so a registered loop for the resolved project directory makes this a no-op.
    Returns True when a loop was started.

    The loop is registered before the first ``await`` rather than guarded by a lock: the check and the
    registration then cannot interleave with a concurrent call, and a module-level ``asyncio.Lock`` would
    bind to whichever event loop first contended it. A start that fails (or is cancelled) while recovering
    entries unregisters the loop again, so a later call can retry.

    Startup recovery (``creating`` entries back to ``pending``) runs only when no other live process is
    already serving the project (``sibling_loop_alive``). A sibling (a ``make dev`` dashboard whose lifespan
    restarts on every reload, next to the daemon) may be inside ``gh pr create`` for such an entry, and
    resetting it would let this loop open the same PR a second time. A crashed process's leftover heartbeat
    does not count, so a restart after a crash still recovers.
    """
    if not cfg.coderabbit_quota.enabled:
        return False

    from sova.db.session import get_session
    from sova.supervisor.pr_throttle import PRThrottleLoop as _PRThrottleLoop
    from sova.supervisor.pr_throttle import recover_creating_entries, sibling_loop_alive

    key = str(project_dir.resolve())
    if _daemon_components.get(key, {}).get("pr_throttles"):
        return False

    async def _session_factory() -> AsyncSession:
        return await get_session(project_dir=project_dir)

    throttle = _PRThrottleLoop(
        _session_factory,
        cfg.coderabbit_quota,
        project_slug=cfg.github_repo,
        project_dir=project_dir,
    )
    # Read before this loop's first beat overwrites the file.
    sibling_alive = sibling_loop_alive(project_dir)
    registered = _daemon_components.setdefault(key, {}).setdefault("pr_throttles", [])
    registered.append(throttle)
    try:
        if sibling_alive:
            log.info("pr_throttle.recovery_skipped_sibling_alive", project=key)
        else:
            async with await get_session(project_dir=project_dir) as session, session.begin():
                await recover_creating_entries(session)
    except BaseException:
        registered.remove(throttle)
        raise
    _pr_throttle_tasks.append(asyncio.create_task(throttle.run()))
    log.info("pr_throttle.loop_started", project=key)
    return True


# ----------------------------------------------------------------------
# Oversight agent holder
# ----------------------------------------------------------------------

_oversight_agent: OversightAgent | None = None


def set_oversight_agent(agent: OversightAgent | None) -> None:
    """Called by app.py lifespan to share the oversight agent instance."""
    global _oversight_agent  # noqa: PLW0603
    _oversight_agent = agent


def get_oversight_agent() -> OversightAgent | None:
    """Return the shared oversight agent (the oversight router and the settings reload read it)."""
    return _oversight_agent
