"""Machine-wide leases that count the pipeline runs active right now.

Every pipeline run holds one lease file in ``~/.config/sova/leases/`` (beside the project registry) for
its lifetime, so any process on the machine can count the agents running across every project without
reading a project database. ``decide_pytest_workers()`` in ``sova/utils/env.py`` divides the cores
and memory by that count when it caps a check's pytest-xdist workers.

A lease is ``{pid}-{uuid}.json`` holding the holder's pid, process create time and process name: one file
per pipeline, not per process, so a process that runs several pipelines in-process (``sova parallel``, the
server's ``WatchLoop``) holds one lease for each and counts as that many agents. A supervisor that spawns an
agent it does not run the pipeline in (the dashboard's command agents) leases on the child's behalf with
``acquire_lease_for()``, which writes ``{pid}-{uuid}.supervised.json`` for the child's pid and releases it
when it reaps the child. A lease is live while that pid exists and both its create time and its name still
match (a reused pid fails the check); a dead or mismatched lease, and a corrupt one, is stale and is deleted
by the next count. A lease that cannot be read, or whose process cannot be inspected, is unknown rather than
stale: the count keeps the file, counts it as one live agent and logs it at DEBUG, so one uninspectable lease
(a pid reused by another user's process) does not degrade every spawn. Only an unreadable lease directory
raises ``OSError``. The count is an estimate by design: two agents starting in the same instant may each
miss the other.
"""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import psutil

from sova.utils.files import write_text_atomic
from sova.utils.logging import get_logger

log = get_logger(component="utils.agent_leases")

_LEASE_DIR = Path.home() / ".config" / "sova" / "leases"

# Set in the environment of a process that leases one pipeline at a time (`sova run`, `sova watch`), to its
# own pid. A nested `sova` subprocess sees it and skips acquiring, so an agent and the checks it runs count
# once; the count reads it to know whether the process asking is already covered by an ancestor's lease.
# Every descendant inherits it, including one that outlives the run (a dashboard server an agent restarted),
# so it is honoured only while the pid it names is one of this process's ancestors. A process that runs
# pipelines concurrently (`sova parallel`, the server) does not set it: the independent agents it spawns
# must lease for themselves.
LEASE_ENV_KEY = "SOVA_AGENT_LEASE_PID"

# The leases this process holds, mapped to whether each one set LEASE_ENV_KEY. In-process state: it decides
# when the environment marker is cleared, and whether the whole process is one pipeline (every held lease
# inherits), in which case any caller in it is already counted.
_held: dict[Path, bool] = {}
_held_lock = threading.Lock()

# The lease of the pipeline the current task or thread belongs to. A process that runs several pipelines at
# once (the server's WatchLoop, `sova parallel`) also serves callers that belong to none of them (a dashboard
# request spawning an independent agent), so "this process holds a lease" cannot say whether a caller is
# counted. asyncio tasks and `asyncio.to_thread` copy the context, so a check inside a pipeline sees its
# lease and a request handler does not.
_current_lease: ContextVar[Path | None] = ContextVar("sova_current_lease", default=None)

# Filename suffix of a lease taken on behalf of a child process (`acquire_lease_for()`).
_SUPERVISED_SUFFIX = ".supervised.json"

# psutil's create_time() is a float of seconds derived from the boot time, which a clock adjustment can
# shift; a recorded and a re-read value of one process differ by at most that rounding. A pid reused
# within this window is told apart by the process name recorded beside the create time.
_CREATE_TIME_TOLERANCE_SECONDS = 1.0


class LeaseUnreadableError(OSError):
    """A lease, or the process it names, exists but cannot be read: its state is unknown, not stale."""


def _identity(pid: int) -> tuple[float, str] | None:
    """The (create time, name) of *pid*, or None when no such process runs.

    Raises ``LeaseUnreadableError`` when the process exists but denies access, so the caller can tell an
    unknown process from a gone one.
    """
    try:
        proc = psutil.Process(pid)
        return proc.create_time(), proc.name()
    except psutil.NoSuchProcess:
        return None
    except (psutil.Error, OSError) as exc:
        raise LeaseUnreadableError(f"process {pid} cannot be inspected: {exc}") from exc


def _ancestor_pids() -> set[int] | None:
    """The pids of this process's ancestors, or None when they cannot be read."""
    try:
        return {parent.pid for parent in psutil.Process().parents()}
    except (psutil.Error, OSError):
        return None


def _enclosing_lease_pid() -> int | None:
    """The pid ``LEASE_ENV_KEY`` names when it is one of this process's ancestors, else None.

    A marker naming this process is its own lease (the registry knows it), and one naming any other pid was
    inherited from a run this process has outlived, so both are ignored. When the ancestors cannot be read
    the marker is trusted, as it was before the check existed.
    """
    marker = os.environ.get(LEASE_ENV_KEY, "").strip()
    if not marker.isdigit():
        return None
    pid = int(marker)
    if pid == os.getpid():
        return None
    ancestors = _ancestor_pids()
    if ancestors is None:
        return pid
    return pid if pid in ancestors else None


def _parse_lease(text: str) -> tuple[int, float, str] | None:
    """The (pid, create time, name) a lease's text records, or None when the text is corrupt."""
    try:
        data = json.loads(text)
        pid = int(data["pid"])
        recorded = float(data["create_time"])
        name = data["name"]
    except (ValueError, TypeError, KeyError):
        return None
    if pid <= 0 or not math.isfinite(recorded) or not isinstance(name, str):
        return None
    return pid, recorded, name


def _live_lease_pid(path: Path) -> int | None:
    """The pid a lease file names when that process still runs, else None (the lease is stale).

    Raises ``LeaseUnreadableError`` when the file or its process cannot be read, so an unknown lease is
    neither counted as live nor deleted as stale.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    except OSError as exc:
        raise LeaseUnreadableError(f"lease {path} cannot be read: {exc}") from exc
    lease = _parse_lease(text)
    if lease is None:
        return None
    pid, recorded, name = lease
    current = _identity(pid)
    if current is None:
        return None
    create_time, current_name = current
    if abs(create_time - recorded) > _CREATE_TIME_TOLERANCE_SECONDS or current_name != name:
        return None
    return pid


def _live_leases() -> dict[Path, int | None]:
    """Every live lease file mapped to the pid that holds it, deleting every stale lease found on the way.

    A missing lease directory is zero leases. An unreadable directory raises ``OSError``: the caller decides
    what an unknown count means. A lease file or leased process that cannot be read is unknown, not stale:
    it is kept, counted as live (its pid is None) and logged at DEBUG. A lease released while the count runs
    is skipped. Pruning is best effort, so a failed delete is logged and the count still returns.
    """
    try:
        # iterdir(), not glob(): glob() swallows a permission error and reports an unreadable directory as empty.
        paths = sorted(p for p in _LEASE_DIR.iterdir() if p.suffix == ".json" and not p.name.startswith("."))
    except FileNotFoundError:
        return {}
    live: dict[Path, int | None] = {}
    for path in paths:
        try:
            pid = _live_lease_pid(path)
        except LeaseUnreadableError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                continue
            log.debug("agent_lease.unreadable_counted_live", path=str(path), reason=str(exc))
            live[path] = None
            continue
        if pid is not None:
            live[path] = pid
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("agent_lease.prune_failed", path=str(path), exc_info=True)
    return live


def live_lease_pids() -> set[int]:
    """The pids holding a live lease (see ``_live_leases``); a process with several pipelines appears once."""
    return {pid for pid in _live_leases().values() if pid is not None}


def _under_supervised_lease(live: dict[Path, int | None]) -> bool:
    """True when an ancestor of this process is leased by its supervisor (``acquire_lease_for``) and live."""
    supervised = {pid for path, pid in live.items() if pid is not None and path.name.endswith(_SUPERVISED_SUFFIX)}
    if not supervised:
        return False
    ancestors = _ancestor_pids()
    return ancestors is not None and not supervised.isdisjoint(ancestors)


def _caller_is_counted(live: dict[Path, int | None]) -> bool:
    """True when a live lease already stands for the caller's own work.

    The caller's pipeline lease (the context variable), any lease of a process that is one pipeline (every
    lease it holds is inherited, as in `sova run`), a marked ancestor's lease, or a supervisor's lease for an
    ancestor. A process that runs pipelines concurrently is none of these for a caller outside them.
    """
    current = _current_lease.get()
    if current is not None and current in live:
        return True
    with _held_lock:
        if any(inherits and path in live for path, inherits in _held.items()):
            return True
    enclosing = _enclosing_lease_pid()
    if enclosing is not None and enclosing in live.values():
        return True
    return _under_supervised_lease(live)


def active_agent_count() -> int:
    """Live leases plus the caller when nothing already covers it: the agents sharing the machine, at least 1.

    One agent per live lease file. A caller is covered when its own pipeline's lease is live (it runs inside
    one), or the process is a single pipeline, or an ancestor's lease is live (it is a nested subprocess of
    one); a spawn from an uncovered caller (the dashboard server between pipelines, or a request handler
    while its WatchLoop runs tasks) is one more agent than the leases show.
    Raises ``OSError`` when the lease directory cannot be read.
    """
    live = _live_leases()
    return len(live) + (0 if _caller_is_counted(live) else 1)


def acquire_lease(*, inherit: bool = True) -> Path | None:
    """Write a lease for one pipeline of this process; None when none was taken.

    The returned path is the handle for ``release_lease()``. With *inherit* the environment is marked so a
    nested `sova` subprocess skips its own lease; pass False from a process that runs several pipelines at
    once, whose independent children must lease for themselves.

    None when an ancestor already holds the lease for this process's work (the environment marks it), so a
    nested `sova` subprocess counts once with the run it belongs to. A read-only home directory or any write
    failure is logged and the run continues without a lease: the count then falls back to counting the
    spawn alone.
    """
    if _enclosing_lease_pid() is not None or _under_a_supervised_lease():
        return None
    pid = os.getpid()
    try:
        identity = _identity(pid)
    except LeaseUnreadableError:
        identity = None
    if identity is None:
        log.warning("agent_lease.acquire_failed", reason="own process identity unreadable")
        return None
    create_time, name = identity
    path = _LEASE_DIR / f"{pid}-{uuid.uuid4().hex}.json"
    try:
        _LEASE_DIR.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps({"pid": pid, "create_time": create_time, "name": name}))
    except OSError:
        log.warning("agent_lease.acquire_failed", path=str(path), exc_info=True)
        return None
    with _held_lock:
        _held[path] = inherit
    _current_lease.set(path)
    if inherit:
        os.environ[LEASE_ENV_KEY] = str(pid)
    return path


def _under_a_supervised_lease() -> bool:
    """``_under_supervised_lease()`` over a fresh count; an unreadable directory is not a lease."""
    try:
        return _under_supervised_lease(_live_leases())
    except OSError:
        return False


def acquire_lease_for(pid: int) -> Path | None:
    """Write a lease for the child process *pid* on its supervisor's behalf; None when none was taken.

    For an agent the caller spawned but does not run the pipeline in (a dashboard command agent: `claude -p`
    running the project's own checks). It records the child's pid, create time and name, so the lease is
    live as long as the child is and a crash is pruned like any dead lease. The child's own `sova` and check
    processes find it by walking their ancestors, so they count once with it. Release it with
    ``release_lease()`` when the child is reaped. Nothing is marked in this process: the caller stays an
    uncounted supervisor. A write failure is logged and the child runs without a lease.
    """
    try:
        identity = _identity(pid)
    except LeaseUnreadableError:
        identity = None
    if identity is None:
        log.warning("agent_lease.acquire_failed", reason="child process identity unreadable", child_pid=pid)
        return None
    create_time, name = identity
    path = _LEASE_DIR / f"{pid}-{uuid.uuid4().hex}{_SUPERVISED_SUFFIX}"
    try:
        _LEASE_DIR.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps({"pid": pid, "create_time": create_time, "name": name}))
    except OSError:
        log.warning("agent_lease.acquire_failed", path=str(path), exc_info=True)
        return None
    return path


def release_lease(path: Path | None) -> None:
    """Delete *path* and, once no held lease marks the environment, unmark it.

    Best effort: the next count prunes a lease left behind.
    """
    if path is None:
        return
    with _held_lock:
        held = _held.pop(path, None) is not None
        still_marked = any(_held.values())
    if held and not still_marked:
        os.environ.pop(LEASE_ENV_KEY, None)
    if _current_lease.get() == path:
        _current_lease.set(None)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("agent_lease.release_failed", path=str(path), exc_info=True)


@contextmanager
def pipeline_lease(*, inherit: bool = True) -> Iterator[Path | None]:
    """Hold one pipeline lease (``acquire_lease``) for the block and release it after."""
    lease = acquire_lease(inherit=inherit)
    try:
        yield lease
    finally:
        release_lease(lease)
