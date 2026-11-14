"""Last-result snapshots for the dashboard's slow, tracker-backed payloads (work items, handoff).

A request for a snapshotted payload is answered at once from the last good result. A result older than its TTL is
rebuilt in the background (one build in flight per kind and project, shared by every caller) and the old one keeps
answering until the build lands. Only a payload that has never been built waits for the build, and a failed first
build raises: an empty or default payload is never returned in place of a failure.

A failed rebuild keeps the previous result and records the failed attempt, so the response can say its data is
older than the refresh interval (``Snapshot.meta()``). A rebuild whose payload differs from the previous one
notifies the registered change listener, which is how the status socket learns to send ``work_items_changed``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi.encoders import jsonable_encoder

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.snapshot")

# Seconds a snapshot answers before a request starts a background rebuild.
DEFAULT_SNAPSHOT_TTL_SECONDS = 5.0
# Cap on one build, so a hung tracker call cannot hold the single in-flight slot for ever.
SNAPSHOT_BUILD_TIMEOUT_SECONDS = 120.0

Build = Callable[[], Awaitable[dict]]
ChangeListener = Callable[[str, Path | None], Awaitable[None]]


@dataclass
class Snapshot:
    """The last good payload of one kind for one project, with the bookkeeping the response needs."""

    payload: dict
    digest: str
    built_at: float  # time.monotonic() of the build that produced the payload
    failed_at: float | None = None  # time.monotonic() of the latest failed rebuild after built_at
    last_error: str | None = None

    def meta(self) -> dict:
        """Volatile metadata, kept out of the digest: how old the payload is and whether its latest rebuild failed."""
        return {
            "age_seconds": round(time.monotonic() - self.built_at, 1),
            "refresh_failed": self.failed_at is not None,
            "last_error": self.last_error,
        }


_Key = tuple[str, str]
_snapshots: dict[_Key, Snapshot] = {}
_builds: dict[_Key, asyncio.Task[bool]] = {}
_generations: dict[_Key, int] = {}
_listener: ChangeListener | None = None


def _key(kind: str, project_dir: Path | None) -> _Key:
    return kind, str(project_dir.resolve()) if project_dir is not None else ""


def set_change_listener(listener: ChangeListener | None) -> None:
    """Register the coroutine called as ``listener(kind, project_dir)`` when a rebuild changed a payload."""
    global _listener
    _listener = listener


def _digest(payload: dict, normalize: Callable[[dict], dict] | None) -> str:
    # jsonable_encoder first, so the digest covers exactly what the HTTP endpoint would serialize.
    body = jsonable_encoder(normalize(payload) if normalize is not None else payload)
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


async def _notify(kind: str, project_dir: Path | None) -> None:
    if _listener is None:
        return
    try:
        await _listener(kind, project_dir)
    except Exception:  # noqa: BLE001 (a failed notification must not fail the rebuild that triggered it)
        log.warning("snapshot.notify_failed", kind=kind, exc_info=True)


async def _run_build(
    key: _Key,
    kind: str,
    project_dir: Path | None,
    build: Build,
    normalize: Callable[[dict], dict] | None,
    generation: int,
) -> bool:
    """Build once; store the result. Returns True when the stored payload changed (not on the first build).

    *generation* is the invalidation count when the build was requested, so a build that was queued but had not
    started yet when ``invalidate_snapshot()`` ran is also discarded.
    """
    previous = _snapshots.get(key)
    try:
        payload = await asyncio.wait_for(build(), timeout=SNAPSHOT_BUILD_TIMEOUT_SECONDS)
        digest = _digest(payload, normalize)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 (a failed rebuild keeps serving the last good payload)
        if previous is None:
            raise
        log.warning("snapshot.rebuild_failed", kind=kind, exc_info=True)
        previous.failed_at = time.monotonic()
        previous.last_error = f"{type(exc).__name__}: {exc}"[:200]
        return False
    if _generations.get(key, 0) != generation:
        # invalidate_snapshot() ran while this build was in flight: its result may predate the change.
        return False
    _snapshots[key] = Snapshot(payload=payload, digest=digest, built_at=time.monotonic())
    changed = previous is not None and previous.digest != digest
    # A rebuild that recovers from a failure also notifies: clients holding the old "refresh failed" metadata
    # would otherwise never refetch it, because an unchanged payload sends nothing.
    recovered = previous is not None and previous.failed_at is not None
    if changed or recovered:
        await _notify(kind, project_dir)
    return changed


def _start_build(
    kind: str,
    project_dir: Path | None,
    build: Build,
    normalize: Callable[[dict], dict] | None,
) -> asyncio.Task[bool]:
    key = _key(kind, project_dir)
    running = _builds.get(key)
    if running is not None and not running.done():
        return running
    task = asyncio.create_task(_run_build(key, kind, project_dir, build, normalize, _generations.get(key, 0)))
    _builds[key] = task

    def _done(finished: asyncio.Task[bool]) -> None:
        if _builds.get(key) is finished:
            _builds.pop(key, None)
        if not finished.cancelled() and finished.exception() is not None:
            # Waiters get the exception from the shared task; this retrieves it for a build nobody awaits.
            log.debug("snapshot.build_failed", kind=kind, error=str(finished.exception()))

    task.add_done_callback(_done)
    return task


async def refresh_snapshot(
    kind: str,
    project_dir: Path | None,
    build: Build,
    *,
    normalize: Callable[[dict], dict] | None = None,
) -> bool:
    """Rebuild now (joining a build already in flight); True when the payload changed since the last build."""
    task = _start_build(kind, project_dir, build, normalize)
    return await asyncio.shield(task)


async def get_snapshot(
    kind: str,
    project_dir: Path | None,
    build: Build,
    *,
    ttl: float = DEFAULT_SNAPSHOT_TTL_SECONDS,
    normalize: Callable[[dict], dict] | None = None,
) -> Snapshot:
    """The last good result at once, rebuilding in the background when older than *ttl*.

    The first call for a kind and project waits for the build and raises when it fails.
    """
    key = _key(kind, project_dir)
    snapshot = _snapshots.get(key)
    if snapshot is None:
        await refresh_snapshot(kind, project_dir, build, normalize=normalize)
        snapshot = _snapshots.get(key)
        if snapshot is None:
            raise RuntimeError(f"snapshot for {kind} was invalidated while it was building")
        return snapshot
    if time.monotonic() - snapshot.built_at > ttl:
        _start_build(kind, project_dir, build, normalize)
    return snapshot


def invalidate_snapshot(kind: str, project_dir: Path | None) -> None:
    """Drop a snapshot a write has made wrong, so the next read waits for a fresh build."""
    key = _key(kind, project_dir)
    _generations[key] = _generations.get(key, 0) + 1
    _snapshots.pop(key, None)


async def cancel_snapshot_builds() -> None:
    """Cancel every build in flight. Called during lifespan shutdown."""
    tasks = [t for t in _builds.values() if not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _builds.clear()


def reset_snapshots() -> None:
    """Forget every snapshot and build (the change listener stays registered). Test-only."""
    _snapshots.clear()
    _builds.clear()
    _generations.clear()
