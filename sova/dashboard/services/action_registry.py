"""Actions the user started from the dashboard: which are still running, and how each recent one ended.

A route registers an action when it starts it (``begin()``) and records the outcome when it ends
(``finish()``), or wraps the work in ``tracked_action()``. The registry lives in the dashboard process, not
in a page module, so a card keeps showing "Rebasing..." through a board refresh and a page reload. A success
is kept for ``SUCCESS_TTL_SECONDS`` so the card can show it briefly; a failure stays until the user dismisses
it (``dismiss()``), so an error never disappears unread. Every change sends ``actions_changed`` to the
project's sockets, so every open tab redraws at once.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.action_registry")

SUCCESS_TTL_SECONDS = 20.0
# Failures are kept until dismissed, but a project never holds more than this many finished actions.
_MAX_FINISHED = 50

RUNNING = "running"
DONE = "done"
FAILED = "failed"


@dataclass
class _Action:
    id: str
    kind: str
    label: str
    issue: int | None
    pr: int | None
    started_at: float
    status: str = RUNNING
    message: str = ""
    ended_at: float | None = None
    extra: dict = field(default_factory=dict)

    def payload(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "issue": self.issue,
            "pr": self.pr,
            "status": self.status,
            "message": self.message,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            **self.extra,
        }


_actions: dict[str, dict[str, _Action]] = {}


def resolve_project(project_dir: Path | None) -> Path | None:
    """The project an action belongs to. An unprefixed request (single-project mode) names none, and the status
    socket registers that project under the default project dir, so the registry keys and broadcasts the same way:
    a broadcast for ``None`` would find no socket and every tab would miss the change."""
    if project_dir is not None:
        return project_dir
    from sova.dashboard.services.agent_pool import get_default_project_dir

    return get_default_project_dir()


def _project_key(project_dir: Path | None) -> str:
    resolved = resolve_project(project_dir)
    return str(resolved.resolve()) if resolved is not None else ""


def reset_actions() -> None:
    """Forget every action. Test-only, like ``reset_board_cache()``."""
    _actions.clear()


async def _notify(project_dir: Path | None) -> None:
    """Tell the project's sockets the action list changed; best-effort, a page also reloads it on open."""
    try:
        from sova.dashboard.routers.agents import _ws_manager

        await _ws_manager.broadcast_event("actions_changed", resolve_project(project_dir))
    except Exception:  # noqa: BLE001 (a failed broadcast only delays the redraw until the next load)
        log.debug("actions.broadcast_failed", exc_info=True)


def _prune(actions: dict[str, _Action], now: float) -> None:
    for action_id, action in list(actions.items()):
        if action.status == DONE and action.ended_at is not None and now - action.ended_at > SUCCESS_TTL_SECONDS:
            del actions[action_id]
    finished = sorted(
        (a for a in actions.values() if a.status != RUNNING), key=lambda a: a.ended_at or 0.0, reverse=True
    )
    for stale in finished[_MAX_FINISHED:]:
        del actions[stale.id]


def running_action(project_dir: Path | None, kind: str, issue: int | None) -> dict | None:
    """The running action of this kind for this issue, if any (so a second click cannot start a duplicate)."""
    for action in _actions.get(_project_key(project_dir), {}).values():
        if action.status == RUNNING and action.kind == kind and action.issue == issue:
            return action.payload()
    return None


async def begin(
    project_dir: Path | None, *, kind: str, label: str, issue: int | None = None, pr: int | None = None
) -> str:
    """Register a running action and return its id."""
    action = _Action(id=uuid.uuid4().hex[:12], kind=kind, label=label, issue=issue, pr=pr, started_at=time.time())
    _actions.setdefault(_project_key(project_dir), {})[action.id] = action
    try:
        await _notify(project_dir)
    except asyncio.CancelledError:
        # The caller never receives the id, so no one else would record an outcome.
        _record(project_dir, action.id, ok=False, message="Cancelled before it finished")
        raise
    return action.id


def _record(project_dir: Path | None, action_id: str, *, ok: bool, message: str, fix: dict | None = None) -> bool:
    actions = _actions.get(_project_key(project_dir), {})
    action = actions.get(action_id)
    if action is None:
        return False
    action.status = DONE if ok else FAILED
    action.message = message
    if fix is not None:
        action.extra["fix"] = fix
    action.ended_at = time.time()
    _prune(actions, action.ended_at)
    return True


async def finish(
    project_dir: Path | None, action_id: str, *, ok: bool, message: str = "", fix: dict | None = None
) -> None:
    """Record how an action ended. An unknown id (already dismissed, or reset) is ignored.

    *fix* is a work item action (``sova.work.state``'s ``{id, label, style, handler, handler_args}`` shape) that
    remedies a failure; the card shows it as a button beside the message.
    """
    if _record(project_dir, action_id, ok=ok, message=message, fix=fix):
        await _notify(project_dir)


def list_actions(project_dir: Path | None) -> list[dict]:
    """Running actions and recent results, oldest first."""
    actions = _actions.get(_project_key(project_dir), {})
    _prune(actions, time.time())
    return [action.payload() for action in sorted(actions.values(), key=lambda a: a.started_at)]


async def dismiss(project_dir: Path | None, action_id: str) -> bool:
    """Drop a finished action's result. A running action is kept: dismissing it would hide work in progress."""
    actions = _actions.get(_project_key(project_dir), {})
    action = actions.get(action_id)
    if action is None or action.status == RUNNING:
        return False
    del actions[action_id]
    await _notify(project_dir)
    return True


def _error_message(exc: BaseException) -> str:
    """The card's text for a failed block: the route's own HTTP detail, else the catch-all handler's fixed text.

    Any other exception is a bug the catch-all answers as a 500 without its text, so the card never shows more.
    """
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("detail") or detail)
        return str(detail)
    return "Internal error"


class _Tracked:
    """The handle ``tracked_action()`` yields: the action's id, the success message to record, and ``forget``,
    set when the block raises for a refusal the page handles itself (a start with no free slot, which it queues
    instead), so no failure is left on the card."""

    def __init__(self, action_id: str) -> None:
        self.id = action_id
        self.message = ""
        self.forget = False


@asynccontextmanager
async def tracked_action(
    project_dir: Path | None, *, kind: str, label: str, issue: int | None = None, pr: int | None = None
) -> AsyncIterator[_Tracked]:
    """Register an action for the duration of the block: done when it returns, failed when it raises.

    The exception is re-raised unchanged, so the route still answers with its own error status; the failure
    is recorded with the HTTP detail as its message, or "Internal error" for any other exception.
    """
    handle = _Tracked(await begin(project_dir, kind=kind, label=label, issue=issue, pr=pr))
    try:
        yield handle
    except asyncio.CancelledError:
        # No await while cancelled: record the outcome synchronously; the next load shows it.
        _record(project_dir, handle.id, ok=False, message="Cancelled before it finished")
        raise
    except Exception as exc:
        if handle.forget:
            _actions.get(_project_key(project_dir), {}).pop(handle.id, None)
            await _notify(project_dir)
        else:
            await finish(project_dir, handle.id, ok=False, message=_error_message(exc))
        raise
    except BaseException:
        # Any other exit (the generator closed early): never leave the action running.
        _record(project_dir, handle.id, ok=False, message="Stopped before it finished")
        raise
    await finish(project_dir, handle.id, ok=True, message=handle.message)
