"""Agents API: multi-agent start, stop, status, and output streaming."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request
from starlette.responses import StreamingResponse

from sova.config.context import set_project_context
from sova.config.models import RUNTIME_MODEL_PIN_ROLES
from sova.core.state import TASK_RUN_TERMINAL
from sova.dashboard.security import validate_websocket_origin
from sova.dashboard.services import control_service, llm_suggestion_service, planner_issue_service, work_service
from sova.dashboard.services.action_registry import tracked_action
from sova.dashboard.services.agent_lifecycle import (
    COMMAND_INPUT_ERRORS,
    LLM_MODEL_OVERRIDE_ROLES,
    NO_FREE_SLOT,
    command_request_rejection,
)
from sova.dashboard.services.agent_validation import (
    STATE_NOT_READY,
    suitability_blocked_detail,
)
from sova.dashboard.services.feed_service import get_feed_service
from sova.dashboard.services.output_stream_service import get_output_stream_service
from sova.dashboard.services.snapshot_service import (
    get_snapshot,
    refresh_snapshot,
    set_change_listener,
)
from sova.dashboard.services.work_board_service import keep_board_keys_alive, refresh_board_snapshots
from sova.dashboard.services.work_item_service import set_graph_change_listener
from sova.dashboard.services.work_refresh_service import (
    RunMark,
    apply_run_changes,
    mark_runs,
    run_github_probe,
)
from sova.db.models import TaskRun
from sova.db.session import get_session
from sova.llm.backends import TIER_NAMES
from sova.utils.formatting import iso_utc
from sova.utils.logging import get_logger

router = APIRouter(tags=["agents"])
# require_same_origin_csrf (applied to `router` at include_router() time in app.py)
# expects an HTTP Request, not a WebSocket connection: FastAPI cannot satisfy that
# dependency for a websocket route, so the one here lives on its own router that
# app.py registers without the CSRF dependency. ws_agent_status checks the handshake
# Origin itself with validate_websocket_origin before accepting.
ws_router = APIRouter(tags=["agents"])
log = get_logger(component="dashboard.agents")

_STATUS_BROADCAST_INTERVAL = 2  # seconds
# The handoff is rebuilt every this many status ticks (6 s at the 2 s cadence): the sidebar used to poll it every 5 s
# per tab, and it only matters when a handoff file or a PR's review state changes.
_HANDOFF_EVERY_N_TICKS = 3
# The work items are rebuilt on this cadence while a socket is open (60 s at the 2 s cadence), which bounds how long
# a change made outside the dashboard (a PR merged on GitHub) stays unseen; a page request also rebuilds one that is
# older than its snapshot TTL.
_WORK_ITEMS_EVERY_N_TICKS = 30
_FEED_IDLE_TIMEOUT = 15.0  # seconds a feed forwarder waits for an event before re-checking its subscribers
# Seconds between GitHub probes (If-None-Match on the PR and issue lists) while a visible Work tab is connected: a
# change made on GitHub reaches the board within about this plus one board build. A 304 costs no rate limit.
_GITHUB_PROBE_INTERVAL = 15.0


def _normalize_project_key(project_dir: Path | None) -> Path | None:
    """Resolve a project_dir to the canonical form used as a _ConnectionManager group key.

    A broadcaster and a websocket subscriber can reach the same project through
    different unresolved forms (an AgentState built from a caller-supplied path vs.
    the single-project default set via set_project_dir()). Keying on the resolved
    path ensures both land in the same group instead of silently landing in two
    (one of which has no subscribers, so broadcast_event() drops the message).
    """
    return project_dir.resolve() if project_dir is not None else None


class _ConnectionManager:
    """Track active WebSocket connections per project and run a single producer per group.

    Instead of each connection fetching statuses independently, one background
    task per project_dir polls ``get_all_agent_statuses`` and broadcasts to all
    subscribers in that group.  This keeps backend work constant regardless of
    client count and correctly scopes data in multi-project mode.

    Besides the ``status_update`` tick, the same socket carries what the sidebar and pages used to poll: ``feed``
    (one activity-feed event, forwarded from the process-wide FeedService), ``activity`` (the unified running-agents
    payload) and ``handoff`` (the ``/handoff`` payload). The last two are sent only when their serialized payload
    differs from the previous one, so an idle project sends nothing. A client that has just connected asks for the
    current values over HTTP (its ``onOpen`` catch-up).

    The one per-connection state is whether the socket's tab shows the Work page (``work_visible``, which the page
    reports on open and on every visibility change): while at least one such socket is connected, one GitHub probe
    task per project runs (``work_refresh_service.run_github_probe``), and with none, nothing polls GitHub.
    """

    def __init__(self) -> None:
        # Keyed by project_dir (None for single-project mode)
        self._groups: dict[Path | None, list[WebSocket]] = {}
        self._producer_tasks: dict[Path | None, asyncio.Task] = {}
        self._feed_tasks: dict[Path | None, asyncio.Task] = {}
        # Producers past the loop and into their cleanup await: out of _producer_tasks (so a new socket gets a fresh
        # producer) but still owed a cancel at shutdown, which cancel_all() reads from here.
        self._draining: set[asyncio.Task] = set()
        # The URL slug the group's first socket connected under: the producer runs outside any request, so it
        # re-establishes the project context the HTTP routes it reuses expect.
        self._slugs: dict[Path | None, str | None] = {}
        # Sockets whose tab reported a visible Work page, and the GitHub probe they keep running, per project.
        self._work_viewers: dict[Path | None, set[WebSocket]] = {}
        self._probe_tasks: dict[Path | None, asyncio.Task] = {}

    @property
    def active_connections(self) -> list[WebSocket]:
        return [ws for conns in self._groups.values() for ws in conns]

    def connect(self, ws: WebSocket, project_dir: Path | None = None, slug: str | None = None) -> None:
        project_dir = _normalize_project_key(project_dir)
        group = self._groups.setdefault(project_dir, [])
        group.append(ws)
        if self._slugs.setdefault(project_dir, slug) is None and slug:
            self._slugs[project_dir] = slug
        task = self._producer_tasks.get(project_dir)
        if task is None or task.done():
            self._producer_tasks[project_dir] = asyncio.create_task(self._produce_loop(project_dir))
        feed_task = self._feed_tasks.get(project_dir)
        if feed_task is None or feed_task.done():
            self._feed_tasks[project_dir] = asyncio.create_task(self._feed_loop(project_dir))

    def disconnect(self, ws: WebSocket, project_dir: Path | None = None) -> None:
        project_dir = _normalize_project_key(project_dir)
        group = self._groups.get(project_dir, [])
        try:
            group.remove(ws)
        except ValueError:
            pass
        if not group:
            self._groups.pop(project_dir, None)
            self._slugs.pop(project_dir, None)
        self._work_viewers.get(project_dir, set()).discard(ws)
        self._sync_probe(project_dir)

    def set_work_visible(self, ws: WebSocket, project_dir: Path | None, visible: bool) -> None:
        """Record whether *ws*'s tab shows a visible Work page; the first one starts the probe, the last stops it."""
        project_dir = _normalize_project_key(project_dir)
        if ws not in self._groups.get(project_dir, []):
            return
        viewers = self._work_viewers.setdefault(project_dir, set())
        if visible:
            viewers.add(ws)
        else:
            viewers.discard(ws)
        self._sync_probe(project_dir)

    def _sync_probe(self, project_dir: Path | None) -> None:
        task = self._probe_tasks.get(project_dir)
        if self._work_viewers.get(project_dir):
            if task is None or task.done():
                self._probe_tasks[project_dir] = asyncio.create_task(
                    _run_probe(project_dir, self._slugs.get(project_dir))
                )
            return
        self._work_viewers.pop(project_dir, None)
        if task is not None:
            self._probe_tasks.pop(project_dir, None)
            task.cancel()

    async def cancel_all(self) -> None:
        """Cancel all producer and feed tasks. Called during lifespan shutdown."""
        tasks = [
            t
            for t in (
                *self._producer_tasks.values(),
                *self._feed_tasks.values(),
                *self._probe_tasks.values(),
                *self._draining,
            )
            if not t.done()
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._producer_tasks.clear()
        self._feed_tasks.clear()
        self._probe_tasks.clear()
        self._work_viewers.clear()
        self._draining.clear()
        self._groups.clear()
        self._slugs.clear()

    async def broadcast_event(self, event_type: str, project_dir: Path | None, extra: dict | None = None) -> None:
        """Send a one-shot event to all subscribers for a project.

        No-op when no connections exist for the project directory. *extra* merges
        additional fields onto the ``{"type": event_type}`` payload (e.g. the
        terminal status of an ``agent_finished`` run, which ``get_all_agent_statuses()``
        can never report since it only selects non-terminal TaskRuns).
        """
        project_dir = _normalize_project_key(project_dir)
        if not self._groups.get(project_dir):
            return
        payload = {"type": event_type}
        if extra:
            payload.update(extra)
        await self._broadcast(payload, project_dir)

    async def _broadcast(self, data: dict, project_dir: Path | None) -> None:
        sockets = list(self._groups.get(project_dir, []))
        if not sockets:
            return

        async def _send(ws: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(ws.send_json(data), timeout=2.0)
            except Exception:  # noqa: BLE001 (any send failure means the socket is dead; caller drops it)
                log.debug("agents.ws_send_failed", exc_info=True)
                return ws
            return None

        results = await asyncio.gather(*(_send(ws) for ws in sockets))
        for failed_ws in results:
            if failed_ws is not None:
                self.disconnect(failed_ws, project_dir)

    async def _produce_loop(self, project_dir: Path | None) -> None:
        """Single producer per project: fetch statuses, broadcast, and push changed activity and handoff."""
        from sova.dashboard.services.agent_status import (
            format_status_update,
            get_all_agent_statuses,
        )

        if project_dir is not None:
            # Always established from the group's own project_dir (never left to ambient state), inside this
            # task so it scopes only this producer's context copy. The slug is optional: an unprefixed socket
            # has none, and the services fall back to their default slug.
            set_project_context(project_dir, self._slugs.get(project_dir))
        last_sent: dict[str, str] = {}
        tick = 0
        # The active runs at the previous successful tick; None until the first one, which only records.
        run_marks: dict[int, RunMark] | None = None
        # The activity and handoff builds can wait seconds (metrics, DB, GitHub when the synthesis cache lapses),
        # so each runs beside the tick (inheriting this task's project context) rather than holding back every
        # status_update; a build still running at its next tick is left to finish instead of being doubled.
        side_tasks: dict[str, asyncio.Task] = {}

        def _spawn_once(kind: str, build: Callable[[], Awaitable[dict]]) -> None:
            running = side_tasks.get(kind)
            if running is None or running.done():
                side_tasks[kind] = asyncio.create_task(self._push_changed(project_dir, kind, build, last_sent))

        def _spawn_work_items_refresh() -> None:
            running = side_tasks.get("work_items")
            if running is None or running.done():
                side_tasks["work_items"] = asyncio.create_task(_refresh_work_items_snapshot(project_dir))

        try:
            while self._groups.get(project_dir):
                try:
                    statuses = await get_all_agent_statuses(project_dir=project_dir)
                    message = format_status_update(statuses)
                    run_marks = await _invalidate_on_run_transitions(project_dir, run_marks, mark_runs(statuses))
                except Exception:  # noqa: BLE001 (a status fetch failure must not tear down the WebSocket)
                    log.warning("Failed to fetch agent statuses for WebSocket broadcast", exc_info=True)
                    # A distinct type, not {"type": "status_update", "runs": []}: the
                    # client cannot otherwise tell a transient fetch failure apart
                    # from "no runs are active", and would wrongly prune all of its
                    # per-run tracking state on every outage.
                    message = {"type": "status_unavailable"}
                await self._broadcast(message, project_dir)
                if self._work_viewers.get(project_dir):
                    _keep_visible_boards_alive(project_dir)
                _spawn_once("activity", _build_activity_message)
                if tick % _HANDOFF_EVERY_N_TICKS == 0:
                    _spawn_once("handoff", _build_handoff_message)
                if tick % _WORK_ITEMS_EVERY_N_TICKS == 0:
                    _spawn_work_items_refresh()
                tick += 1
                await asyncio.sleep(_STATUS_BROADCAST_INTERVAL)
        finally:
            # Free this producer's slot before awaiting anything: connect() treats a not-yet-done registered task
            # as a live producer, so a socket arriving during the cleanup await below would otherwise get none.
            current = asyncio.current_task()
            if current is not None:
                self._draining.add(current)
            if self._producer_tasks.get(project_dir) is current:
                self._producer_tasks.pop(project_dir, None)
            try:
                pending = [t for t in side_tasks.values() if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    # Await the cancellation so each build's coroutine is closed here, not left pending for the
                    # loop's teardown (which surfaces as an unraisable "never awaited" error in later code).
                    await asyncio.gather(*pending, return_exceptions=True)
            finally:
                self._draining.discard(current)

    async def _push_changed(
        self,
        project_dir: Path | None,
        kind: str,
        build: Callable[[], Awaitable[dict]],
        last_sent: dict[str, str],
    ) -> None:
        """Broadcast the message *build* returns for *kind* unless it equals the previous one sent."""
        try:
            # Encoded once, the way the HTTP endpoint would: the digest and the bytes sent then cannot disagree, and
            # a datetime or Decimal in a payload cannot fail inside the broadcast and drop every socket.
            message = jsonable_encoder(await build())
        except Exception:  # noqa: BLE001 (a failed build must not stop the status tick for every client)
            log.warning("agents.ws_build_failed", kind=kind, exc_info=True)
            # A distinct type so the client marks the value unknown instead of keeping a stale one as current.
            message = {"type": f"{kind}_unavailable"}
        serialized = json.dumps(message, sort_keys=True)
        if last_sent.get(kind) == serialized:
            return
        last_sent[kind] = serialized
        if kind == "handoff" and message.get("type") == "handoff":
            # Compared by content, not by the message: a resend whose only difference is a clock-driven field, or
            # the same handoff again after a handoff_unavailable, must not rebuild every board snapshot.
            digest = _handoff_digest(message.get("data"))
            previous_digest = last_sent.get(_HANDOFF_DIGEST_KEY)
            last_sent[_HANDOFF_DIGEST_KEY] = digest
            if previous_digest is not None and previous_digest != digest:
                _refresh_snapshots_on_handoff_change(project_dir)
        await self._broadcast(message, project_dir)

    async def _feed_loop(self, project_dir: Path | None) -> None:
        """Forward the process-wide activity feed to this project's sockets as ``feed`` messages."""
        feed = get_feed_service()
        sub_id, queue = feed.subscribe()
        try:
            while self._groups.get(project_dir):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_FEED_IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    continue
                try:
                    await self._broadcast({"type": "feed", "event": event.to_dict()}, project_dir)
                except Exception:  # noqa: BLE001 (one bad event must not end the forwarder: the socket stays up)
                    log.warning("agents.ws_feed_forward_failed", exc_info=True)
        finally:
            feed.unsubscribe(sub_id)


def _resolve_group_project(project_dir: Path | None) -> Path:
    """A socket group's project: its own key, else the default project (the ``None`` group has no other)."""
    from sova.config.context import resolve_project_dir
    from sova.dashboard.services.agent_pool import get_default_project_dir

    return resolve_project_dir(project_dir, default_project_dir=get_default_project_dir())


async def _invalidate_on_run_transitions(
    project_dir: Path | None, previous: dict[int, RunMark] | None, current: dict[int, RunMark]
) -> dict[int, RunMark] | None:
    """Invalidate the board's caches when the active runs moved since the last tick; returns the marks to keep.

    Runs before the tick's ``status_update`` goes out, so the rebuild it starts begins before any client reloads
    on the changed set of runs. When something moved but no snapshot is being rebuilt to report it (none built
    yet, or every board key lapsed), the sockets are told here, so an open Work tab refetches and re-registers its
    board instead of keeping a stale Running column.

    A failure is logged and never costs the tick its status update. It keeps *previous*, not *current*, so the
    next tick sees the same transition and retries the invalidation rather than losing a CLI run's exit for good.
    """
    try:
        target = _resolve_group_project(project_dir)
        unannounced = apply_run_changes(target, previous, current)
    except Exception:  # noqa: BLE001 (the next tick re-detects the transition and retries it)
        log.warning("agents.run_transition_refresh_failed", exc_info=True)
        return previous
    if unannounced:
        await _broadcast_work_items_changed(target)
    return current


def _keep_visible_boards_alive(project_dir: Path | None) -> None:
    """Keep this project's board keys recent while a visible Work tab is connected (``keep_board_keys_alive``)."""
    try:
        keep_board_keys_alive(_resolve_group_project(project_dir))
    except Exception:  # noqa: BLE001 (a lapsed key is still caught by the unannounced-change broadcast)
        log.warning("agents.board_keep_alive_failed", exc_info=True)


# Where the producer keeps the digest of the last handoff content it sent, beside the serialized messages.
_HANDOFF_DIGEST_KEY = "handoff:digest"
# Clock-driven fields a handoff payload may carry: they advance without the handoff changing.
_VOLATILE_HANDOFF_KEYS = frozenset({"age_seconds", "elapsed_seconds", "elapsed_formatted", "snapshot"})


def _handoff_digest(data: object) -> str:
    """A digest of a handoff payload's content without its clock-driven fields, for change detection only."""

    def strip(value: object) -> object:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in _VOLATILE_HANDOFF_KEYS}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    body = json.dumps(strip(data), sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _refresh_snapshots_on_handoff_change(project_dir: Path | None) -> None:
    """Rebuild the work snapshots after the handoff changed: the board reads it, and a fresh snapshot would
    otherwise keep answering from before the change. A changed rebuild sends ``work_items_changed``."""
    from sova.dashboard.services.work_board_service import refresh_work_snapshots

    try:
        refresh_work_snapshots(_resolve_group_project(project_dir))
    except Exception:  # noqa: BLE001 (the work-items cadence rebuilds the board; the handoff still goes out)
        log.warning("agents.handoff_snapshot_refresh_failed", exc_info=True)


async def _run_probe(project_dir: Path | None, slug: str | None = None) -> None:
    """The GitHub probe for one project's visible Work tabs (the project resolved here, never left ambient).

    Like the producer, it sets its group's project context in its own task: a websocket scope has none, and the
    snapshot rebuilds a changed answer starts inherit this task's context.

    Anything raised outside the probe loop's own handler (resolving the project, setting the context) is logged
    here: an exception left on the task would only surface as "Task exception was never retrieved" at garbage
    collection, and a probe that stopped silently looks just like one that keeps getting 304s.
    """
    try:
        if project_dir is not None:
            set_project_context(project_dir, slug)
        await run_github_probe(
            _resolve_group_project(project_dir),
            interval=_GITHUB_PROBE_INTERVAL,
            # A change no snapshot rebuild reports (a lapsed board key) must still reach the visible tabs.
            on_unreported=_broadcast_work_items_changed,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 (the next visibility report starts a new probe; the board still has its cadence)
        log.error("agents.github_probe_crashed", project_dir=str(project_dir), exc_info=True)


async def _build_activity_message() -> dict:
    return {"type": "activity", "data": await get_active_agents()}


async def _build_handoff_message() -> dict:
    from sova.dashboard.services.handoff_service import build_handoff_snapshot

    # A fresh build, not the served snapshot: this is the producer that keeps the snapshot warm.
    return {"type": "handoff", "data": await build_handoff_snapshot()}


async def _refresh_work_items_snapshot(project_dir: Path | None) -> None:
    """Rebuild the work-items snapshot; a changed payload reaches the sockets via the change listener."""
    from sova.config.context import get_project_dir
    from sova.dashboard.services.work_item_service import build_work_items, normalize_work_items_for_digest

    target = project_dir if project_dir is not None else get_project_dir()
    try:
        # The board snapshots share the work items' maximum age: a key asked for recently is rebuilt on this cadence.
        refresh_board_snapshots(target)
    except Exception:  # noqa: BLE001 (the work-items rebuild below still runs; the next cadence retries the boards)
        log.warning("agents.board_snapshots_refresh_failed", exc_info=True)
    try:
        await refresh_snapshot(
            "work_items", target, lambda: build_work_items(target), normalize=normalize_work_items_for_digest
        )
    except Exception:  # noqa: BLE001 (a failed first build leaves no snapshot; the next page request reports it)
        log.warning("agents.work_items_refresh_failed", exc_info=True)


async def _on_snapshot_changed(kind: str, project_dir: Path | None) -> None:
    """Tell a project's sockets a snapshot changed: they refetch the payload from its HTTP endpoint.

    A board snapshot (``board:<owner>:<milestone>``) sends the same ``work_items_changed`` the Work page reloads on.
    """
    if kind == "work_items" or kind.startswith("board:"):
        await _broadcast_work_items_changed(project_dir)


async def _broadcast_work_items_changed(project_dir: Path | None) -> None:
    """Send ``work_items_changed`` to a project's sockets, and to the ``None`` group when it can only be this one.

    A snapshot or graph is keyed by the resolved project, but a socket connected without a slug registers under
    the resolved default project, or under ``None`` when no default is set (``agents_status_websocket()``).
    """
    from sova.config.context import resolve_project_dir_or_none
    from sova.dashboard.services.agent_pool import get_default_project_dir

    await _ws_manager.broadcast_event("work_items_changed", project_dir)
    if project_dir is None:
        return
    default = resolve_project_dir_or_none(default_project_dir=get_default_project_dir())
    if default is None or default.resolve() == project_dir.resolve():
        await _ws_manager.broadcast_event("work_items_changed", None)


async def _on_graph_changed(project_dir: Path) -> None:
    """A rebuilt dependency graph differs from the one a board answered from: rebuild the snapshots built from it.

    ``project_dir`` is the resolved project. A rebuilt snapshot that differs tells the sockets itself, so they
    reload once, onto the new board, instead of first refetching the snapshot from the old graph. Only when no
    snapshot is being rebuilt (none built yet, or the refresh failed) are the sockets told here.
    """
    from sova.dashboard.services.work_board_service import refresh_work_snapshots

    try:
        rebuilding = refresh_work_snapshots(project_dir)
    except Exception:  # noqa: BLE001 (the clients are told below instead; the next trigger rebuilds the snapshots)
        log.warning("agents.graph_snapshot_refresh_failed", exc_info=True)
        rebuilding = False
    if not rebuilding:
        await _broadcast_work_items_changed(project_dir)


_ws_manager = _ConnectionManager()
set_change_listener(_on_snapshot_changed)
set_graph_change_listener(_on_graph_changed)


class StartAgentRequest(BaseModel):
    issue: str = ""
    role: str | None = None
    force: bool = False
    budget_override: bool = False
    override_suitability: bool = False
    resume_run_id: int | None = None
    pr_number: int | None = None
    model: str | None = None
    # Per-run "Internal LLM model (this run only)": an LLM-layer model ID from
    # GET /api/models/available?layer=llm. Validated against that discovery at
    # submit time in start_agent() below, never persisted.
    llm_model_override: str | None = Field(default=None, max_length=200)

    @field_validator("llm_model_override")
    @classmethod
    def validate_llm_model_override(cls, v: str | None) -> str | None:
        if v is None:
            return v
        value = v.strip()
        return value if value else None

    @field_validator("issue")
    @classmethod
    def validate_issue(cls, v: str) -> str:
        value = v.strip()
        if value and not value.isdigit():
            raise ValueError("issue must be a numeric string or empty for issue-less runs")
        return value

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str | None) -> str | None:
        if v is None:
            return v
        value = v.strip().lower()
        if not value:
            raise ValueError("role cannot be empty")
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str | None) -> str | None:
        """Normalize a bare tier alias; any other ID is checked against runtime discovery at submit.

        A bare alias (``sova.llm.backends.TIER_NAMES``) is accepted without discovery:
        the auto-launch path and existing API callers send the complexity suggestion.

        The launch modal's Model select offers the active runtime's own catalog
        IDs (#1039), which this check alone cannot verify, so it hands them to
        ``validate_runtime_model()`` in the endpoint instead of rejecting them.
        """
        if v is None:
            return v
        value = v.strip()
        if not value:
            return None
        return value.lower() if value.lower() in TIER_NAMES else value


class RunCommandRequest(BaseModel):
    command: str
    args: dict | None = None


class PlannedTaskRequest(BaseModel):
    title: str
    description: str = ""
    component: str = ""
    priority: str = ""


class CreateIssuesRequest(BaseModel):
    tasks: list[PlannedTaskRequest] = Field(max_length=50)


class DismissInterruptedRequest(BaseModel):
    run_ids: list[int] = Field(min_length=1, max_length=100)
    superseded_by_relaunch: bool = False


@router.post(
    "/agents/planner/create-issues",
    responses={502: {"description": "Some issues could not be created on the tracker"}},
)
async def create_planner_issues(req: CreateIssuesRequest) -> dict:
    """Create GitHub issues from planner-proposed tasks.

    A 502 here may carry side effects: ``detail.results`` lists every task in request order, the created ones with
    their ``number`` and ``url``, so a caller reconciles instead of re-submitting the whole batch.
    """
    results = await planner_issue_service.create_planner_issues(req.tasks)
    if any(not r["ok"] for r in results):
        raise HTTPException(
            status_code=502,
            detail={"message": "Some issues could not be created on the tracker", "results": results},
        )
    return {
        "created": [
            {"number": r["number"], "url": r["url"], "title": r["title"], "dropped_labels": r["dropped_labels"]}
            for r in results
        ],
    }


@router.get("/agents/issue/{issue_number}/complexity")
async def get_issue_complexity(issue_number: str, role: str | None = None) -> dict:
    """Return complexity tier, suggested model and suitability warning for an issue (see the service)."""
    return await llm_suggestion_service.get_issue_complexity(issue_number, role)


@router.get("/agents/work-items")
async def get_work_items() -> dict:
    """Get unified work items with computed state and actions, from the last build.

    The payload answers at once from the snapshot; one older than its TTL is rebuilt in the background and a change
    reaches the sockets as ``work_items_changed``. ``snapshot`` reports the payload's age and whether its latest
    rebuild failed. Only the first request for a project waits for the build, and fails when it fails.
    """
    from sova.config.context import get_project_dir
    from sova.dashboard.services.work_item_service import build_work_items, normalize_work_items_for_digest

    project_dir = get_project_dir()
    snapshot = await get_snapshot(
        "work_items",
        project_dir,
        lambda: build_work_items(project_dir),
        normalize=normalize_work_items_for_digest,
    )
    return {**snapshot.payload, "snapshot": snapshot.meta()}


@router.get(
    "/agents/next-tasks",
    responses={
        500: {"description": "Failed to rank next tasks"},
        503: {"description": "Project configuration unavailable"},
    },
)
async def get_next_tasks(limit: Annotated[int, Query(ge=1, le=50)] = 5) -> dict:
    """Get the ranked "Up next" list: the autonomous issues an agent should pick up next, best first.

    Returns ``{tasks, unclassified_count}``. A failure to build the dependency graph is an
    error status, never an empty list: an empty list means there is nothing to do.
    """
    from sova.config.context import get_project_dir
    from sova.dashboard.services.work_item_service import GraphConfigError
    from sova.dashboard.services.work_item_service import get_next_tasks as _get_next_tasks

    try:
        return await _get_next_tasks(get_project_dir(), limit=limit)
    except GraphConfigError:
        log.error("agents.next_tasks_config_failed", exc_info=True)
        raise HTTPException(status_code=503, detail="Project configuration unavailable")


@router.get("/agents/active")
async def get_active_agents() -> dict:
    """Get all running + recently completed agents (dashboard + external)."""
    return await control_service.get_unified_agents()


@router.get("/agents/interrupted")
async def interrupted_runs() -> dict:
    """Get recently interrupted runs (from dashboard crash/restart)."""
    from sova.dashboard.services.agent_recovery import get_interrupted_runs_and_total

    runs, total = await get_interrupted_runs_and_total()
    return {"interrupted": runs, "total": total}


@router.post("/agents/interrupted/dismiss")
async def dismiss_interrupted(req: DismissInterruptedRequest) -> dict:
    """Mark the given interrupted runs as failed so they no longer show in the banner."""
    from sova.dashboard.services.agent_recovery import dismiss_interrupted_runs

    count = await dismiss_interrupted_runs(req.run_ids, preserve_diagnostic=req.superseded_by_relaunch)
    if count < 0:
        raise HTTPException(status_code=503, detail="Failed to dismiss interrupted runs")
    return {"dismissed": count}


@router.get("/agents/pipeline")
async def get_pipeline() -> dict:
    """Get the developer pipeline step names."""
    return {"steps": control_service.DEVELOPER_PIPELINE}


@router.get("/agents/{run_id}/output")
async def get_agent_output(run_id: int, since: int = 0) -> dict:
    """Get output lines for a specific agent.

    ``since`` counts the run's lifetime lines while the agent is in memory (see ``seq``), and is a stored
    ``line_number`` once it is not.

    ``seq`` is the agent's lifetime line count at the moment these lines were read (``None`` when the
    agent is not in memory, so nothing is still being produced). A log viewer that reconnects its SSE
    stream pairs it with the ``seq`` in that stream's ``connected`` event to drop exactly the lines the
    snapshot and the stream both carry. ``total`` is the cursor after the returned lines: for an agent in
    memory the lifetime line count (``seq``), for stored output ``since`` plus the lines returned.
    ``times`` is parallel to ``lines``: each line's write time as ISO 8601 UTC, or ``null`` for a line with
    no recorded time (a legacy log file).
    """
    seq = control_service.get_output_seq(run_id=run_id)
    lines, times = await control_service.get_output(since, run_id=run_id)
    # The deque holds only the tail of a long run, so ``since + len(lines)`` is not the next lifetime line number once
    # it evicted: the lines read end at ``seq`` (read with no await before get_output()).
    total = seq if seq is not None and lines else since + len(lines)
    return {"lines": lines, "times": [iso_utc(t) for t in times], "total": total, "seq": seq}


@router.get("/agents/{run_id}/output/stream")
async def stream_agent_output(run_id: int, request: Request) -> StreamingResponse:
    """SSE endpoint: streams output lines for a specific agent run in real time.

    Events: ``connected`` (carries ``seq``), ``output`` (a JSON object with ``time``, ISO 8601 UTC, and ``text``),
    ``done``, and ``gap``, sent when a slow reader's
    queue overflowed and a line was dropped; the client then discards the stream and re-reads the run.
    """
    oss = get_output_stream_service()
    sub_id, queue = oss.subscribe(run_id)
    # Read with no await after the subscribe: every line numbered from here on is pushed to this stream.
    seq = control_service.get_output_seq(run_id=run_id)
    connected = json.dumps({} if seq is None else {"seq": seq})

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            yield f"event: connected\ndata: {connected}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if oss.take_overflow(run_id, sub_id):
                        # A full queue dropped a line: the client cannot tell which, so it must re-read the run.
                        yield "event: gap\ndata: {}\n\n"
                    payload = json.dumps({"time": iso_utc(event.time), "text": event.text})
                    yield f"event: output\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    # Check if the run has reached a terminal state
                    is_done = await _check_run_terminal(run_id)
                    if is_done:
                        yield "event: done\ndata: {}\n\n"
                        break
                    yield ": keepalive\n\n"
        finally:
            oss.unsubscribe(run_id, sub_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _check_run_terminal(run_id: int) -> bool:
    """Check whether a run has reached a terminal status."""
    try:
        async with await get_session() as session:
            status = await work_service.get_run_status(session, run_id)
            return status in TASK_RUN_TERMINAL if status else True
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("output_stream.terminal_check_failed", run_id=run_id, exc_info=True)
        return False


@router.post(
    "/agents/start",
    responses={
        400: {"description": "Invalid request, or a model or internal LLM model override the run cannot honor"},
        409: {"description": "Agent already running for this issue, budget exceeded, or issue not meant for an agent"},
        503: {"description": "Model discovery unavailable to verify a model or internal LLM model override"},
    },
)
async def start_agent(req: StartAgentRequest) -> dict:
    """Start a new agent process.

    Every request here is a user's click (internal continuations call ``control_service.start_agent`` directly),
    so it is checked for suitability, a resume from the run page included: an epic or human-only issue returns
    the structured 409 whose message the page shows in a toast, and only ``override_suitability`` lets it start.
    """
    await _validate_start_request(req)
    role = req.role or "developer"
    async with tracked_action(
        _action_project_dir(),
        kind="start",
        label=f"Starting the {role} agent",
        issue=_issue_number(req.issue),
        pr=req.pr_number,
    ) as action:
        result = await control_service.start_agent(
            req.issue,
            role=req.role,
            force=req.force,
            budget_override=req.budget_override,
            enforce_suitability=True,
            override_suitability=req.override_suitability,
            resume_run_id=req.resume_run_id,
            pr_number=req.pr_number,
            model=req.model,
            llm_model_override=req.llm_model_override,
        )
        if isinstance(result, dict) and ("error" in result or result.get("status") == "error"):
            # The board queues the issue on a full pool and says so; a failure on its card would contradict that.
            action.forget = result.get("code") == NO_FREE_SLOT
            raise HTTPException(status_code=409, detail=_start_error_detail(result))
        action.message = f"Started the {role} agent"
    return result


def _action_project_dir() -> Path | None:
    """The project an action belongs to, as the board route resolves it."""
    from sova.config.context import get_project_dir

    return get_project_dir()


def _issue_number(raw: object) -> int | None:
    """An issue number from a request field (``"42"``, ``42``, ``""``), or None when it names none."""
    text = str(raw).strip() if raw is not None else ""
    return int(text) if text.isdigit() else None


async def _validate_start_request(req: StartAgentRequest) -> None:
    """Raise the 400 (or the discovery 503) for a start request the run could not honor."""
    if not req.issue and not req.role:
        raise HTTPException(status_code=400, detail="Either issue or role is required for starting an agent")
    try:
        if req.model is not None and req.model not in TIER_NAMES:
            effective_role = req.role or "developer"
            if effective_role in RUNTIME_MODEL_PIN_ROLES or effective_role not in LLM_MODEL_OVERRIDE_ROLES:
                await llm_suggestion_service.validate_runtime_model(req.model)
        if req.llm_model_override is not None:
            await llm_suggestion_service.validate_llm_model_override(req.role, req.llm_model_override)
    except llm_suggestion_service.ModelOverrideError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _start_error_detail(result: dict) -> dict | str:
    """The 409 detail for a failed start: structured for a budget, suitability, slot or state refusal, else text."""
    if result.get("code") == "budget_exceeded":
        return {
            "code": "budget_exceeded",
            "message": result["error"],
            "total_cost_usd": result.get("total_cost_usd"),
            "max_issue_budget": result.get("max_issue_budget"),
        }
    if result.get("code") == "suitability_blocked":
        return suitability_blocked_detail(result)
    if result.get("code") == NO_FREE_SLOT:
        return {"code": NO_FREE_SLOT, "message": result["error"], "running": result.get("running")}
    if result.get("code") == STATE_NOT_READY:
        return {
            "code": STATE_NOT_READY,
            "message": result["error"],
            "state": result.get("state"),
            "next_role": result.get("next_role"),
        }
    return result.get("detail") or result.get("error") or "Command failed"


@router.post(
    "/agents/{run_id}/stop",
    responses={409: {"description": "The run is not one this dashboard can stop (started elsewhere, or ended)"}},
)
async def stop_agent(run_id: int) -> dict:
    """Stop a specific running agent.

    A run the dashboard did not start (``sova run`` in a terminal, another server) is not in its pool, so
    the stop cannot reach it: that answers 409 with the reason, never a silent success.
    """
    issue, pr = await _run_issue_and_pr(run_id)
    async with tracked_action(_action_project_dir(), kind="stop", label="Stopping", issue=issue, pr=pr) as action:
        result = await control_service.stop_agent(run_id=run_id)
        if result.get("status") == "not_found":
            raise HTTPException(status_code=409, detail=await _unstoppable_reason(run_id))
        action.message = "Stopped"
    return result


async def _run_issue_and_pr(run_id: int) -> tuple[int | None, int | None]:
    """The issue and PR a run works on, for labelling its action; (None, None) when the lookup fails."""
    try:
        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
    except SQLAlchemyError:
        log.warning("agents.stop_run_lookup_failed", run_id=run_id, exc_info=True)
        return None, None
    if run is None:
        return None, None
    return _issue_number(run.issue_number), run.pr_number


async def _unstoppable_reason(run_id: int) -> str:
    """Why the dashboard cannot stop a run its pool does not hold."""
    try:
        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
    except SQLAlchemyError:
        log.warning("agents.stop_reason_lookup_failed", run_id=run_id, exc_info=True)
        return f"The dashboard is not running run {run_id}, so it cannot stop it"
    if run is None:
        return f"There is no run {run_id}"
    if run.status in TASK_RUN_TERMINAL:
        return f"Run {run_id} has already ended ({run.status})"
    return (
        f"Run {run_id} was started outside the dashboard (for example `sova run` in a terminal or another "
        "server), so the dashboard cannot stop it. Stop it where it runs."
    )


@router.post(
    "/agents/{run_id}/resume-from-approval",
    responses={
        404: {"description": "TaskRun not found"},
        409: {"description": "TaskRun not in awaiting_approval state"},
        500: {"description": "Agent spawn or internal error"},
    },
)
async def resume_from_approval(run_id: int) -> dict:
    """Resume a paused pipeline run after human approval."""
    issue, pr = await _run_issue_and_pr(run_id)
    async with tracked_action(
        _action_project_dir(), kind="approve", label="Approving and starting", issue=issue, pr=pr
    ) as action:
        result = await control_service.resume_from_approval(run_id)
        if result.get("error") == "not_found":
            raise HTTPException(status_code=404, detail=result["detail"])
        if result.get("error") == "conflict":
            raise HTTPException(status_code=409, detail=result["detail"])
        if isinstance(result, dict) and ("error" in result or result.get("status") == "error"):
            raise HTTPException(status_code=500, detail=result.get("detail", result["error"]))
        action.message = "Approved; the run continues"
    return result


@router.get("/agents/issue/{issue_number}/pr-status")
async def get_issue_pr_status(issue_number: str) -> dict:
    """Get PR status for an issue: approval state, CI, mergeability."""
    from sova.dashboard.services.agent_recovery import get_pr_status_for_issue

    return await get_pr_status_for_issue(issue_number)


@router.post(
    "/agents/command",
    responses={
        400: {"description": "Unknown command, or an argument outside its allowlist"},
        409: {"description": "The command could not be started"},
    },
)
async def run_command(req: RunCommandRequest) -> dict:
    """Execute a Claude Code command (e.g. /integrate-pr, /address-pr).

    `req.command` names a file resolved relative to the project's own and SOVA's
    own commands directories, so an unvalidated value is a path-traversal and
    prompt-injection vector (`_resolve_command_prompt` falls back to sending a
    bare `/{command}` to the headless, tool-using agent for anything it cannot
    find). start_command() refuses anything outside the known catalog, and any
    argument outside its allowlist, for every caller; this route checks the same
    rule before recording an action, so bad input is a 400 and never a failed
    action in the history.
    """
    args = req.args or {}
    rejection = command_request_rejection(req.command, args)
    if rejection is not None:
        raise HTTPException(status_code=400, detail=rejection["error"])
    pr = _issue_number(args.get("pr"))
    async with tracked_action(
        _action_project_dir(),
        kind="command",
        label=f"Starting /{req.command}",
        issue=_issue_number(args.get("issue")),
        pr=pr,
    ) as action:
        result = await control_service.start_command(req.command, args)
        if isinstance(result, dict) and result.get("code") in COMMAND_INPUT_ERRORS:
            raise HTTPException(status_code=400, detail=result["error"])
        if isinstance(result, dict) and ("error" in result or result.get("status") == "error"):
            raise HTTPException(status_code=409, detail=result.get("detail") or result.get("error") or "Command failed")
        action.message = f"Started /{req.command}"
    return result


def _ws_slug_project_dir(websocket: WebSocket) -> Path | None:
    """Return the project dir named by a ``/p/{slug}/api`` websocket path, or None when it names none.

    An unknown or malformed slug returns None, so the caller falls back to the
    default project exactly as an unprefixed socket does.
    """
    from sova.config.registry import get_project_path
    from sova.dashboard.middleware import SAFE_SLUG_RE

    slug = websocket.path_params.get("slug")
    if not slug or not SAFE_SLUG_RE.match(slug):
        return None
    project_path = get_project_path(slug)
    if project_path is None or not project_path.is_dir():
        return None
    return project_path


def _handle_client_message(websocket: WebSocket, project_dir: Path | None, text: str) -> None:
    """Apply a client message: ``{"type": "work_visible", "visible": bool}``; anything else is ignored."""
    try:
        message = json.loads(text)
    except ValueError:
        log.debug("agents.ws_client_message_unparsed")
        return
    if isinstance(message, dict) and message.get("type") == "work_visible":
        _ws_manager.set_work_visible(websocket, project_dir, message.get("visible") is True)


@ws_router.websocket("/ws/agents/status")
async def ws_agent_status(websocket: WebSocket) -> None:
    """Push real-time agent status updates over WebSocket.

    The connection registers with ``_ws_manager`` which runs a single
    background producer per project.  That producer fetches statuses once
    per interval and broadcasts ``{type: 'status_update', runs: [...]}``
    to every subscriber, keeping backend work constant regardless of
    client count.
    """
    from sova.config.context import resolve_project_dir_or_none
    from sova.dashboard.services.agent_pool import get_default_project_dir

    if not validate_websocket_origin(websocket):
        # Any page a dashboard user visits can open sockets in a loop, so a refused handshake never
        # logs above debug and the attacker-supplied Origin is truncated.
        if getattr(websocket.app.state, "csrf_fail_closed", False):
            # Startup already warned about this bind, and open tabs retry forever: keep the log quiet.
            log.debug("dashboard.ws_refused_fail_closed")
        else:
            log.debug("dashboard.ws_origin_rejected", origin=(websocket.headers.get("origin") or "")[:100])
        await websocket.close(code=1008)
        return

    await websocket.accept()
    # Capture project_dir at connection time so the producer loop uses the
    # correct scope even in multi-project mode. The per-request contextvar is
    # set by ProjectContextMiddleware only for HTTP requests under /p/; it is
    # never set for a websocket scope, so a /p/{slug}/api socket resolves its
    # slug itself (otherwise it would subscribe to the default project's runs),
    # and an unprefixed one falls back to the default project dir rather than
    # registering under None while broadcasters key on the real path. The cwd
    # is deliberately not a further fallback: it would key the connection under
    # a path no broadcaster uses.
    slug_project_dir = _ws_slug_project_dir(websocket)
    project_dir = slug_project_dir or resolve_project_dir_or_none(default_project_dir=get_default_project_dir())
    slug = websocket.path_params.get("slug") if slug_project_dir is not None else None
    _ws_manager.connect(websocket, project_dir, slug)
    try:
        # The producer task in _ws_manager handles sending updates. The one message a client sends is the Work
        # page's visibility report, which decides whether the GitHub probe runs.
        while True:
            _handle_client_message(websocket, project_dir, await websocket.receive_text())
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 (WebSocket teardown reports any transport failure and exits)
        log.exception("Unexpected error in WebSocket /ws/agents/status")
    finally:
        _ws_manager.disconnect(websocket, project_dir)
