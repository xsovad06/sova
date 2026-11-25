"""Agent pool: data models, slot management, and project-scoped agent collections.

Owns the in-memory registry of running agents per project.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sova.config.context import get_project_dir, get_project_slug
from sova.config.loader import load_config
from sova.llm.phase_usage import PhaseTally
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.core.output import OutputWriter
    from sova.ipc.control import AgentProcess, FileAgentProcess
    from sova.ipc.runtime import StreamParser
    from sova.llm.models import CostSource
    from sova.monitoring.collector import ResourceCollector
    from sova.monitoring.writer import ResourceWriter

log = get_logger(component="dashboard.pool")

_DEFAULT_SLUG = "__default__"

# How many stderr lines a run without a dashboard writer keeps for finalization to store.
STDERR_TAIL_LINES = 500

MAX_RECENTLY_COMPLETED = 5
RECENTLY_COMPLETED_TTL = 30.0


@dataclass
class AgentState:
    """Per-agent process state (one per running agent)."""

    run_id: int
    issue: str
    role: str
    process: AgentProcess | FileAgentProcess
    output_lines: deque[str] = field(default_factory=lambda: deque(maxlen=5000))
    # Lines buffered over the run's life. Unlike len(output_lines) it keeps counting once the deque
    # evicts, so a log viewer can tell exactly how many lines a snapshot has that its stream also sent.
    output_seq: int = 0
    # The moment each line in ``output_lines`` was buffered, index-aligned with it (same cap). ``buffer()`` is the
    # only writer of ``output_lines``, ``output_times`` and ``output_seq``, so the three never drift apart.
    output_times: deque[datetime] = field(default_factory=lambda: deque(maxlen=5000))
    # Set by ``get_output()`` the first time it finds ``output_times`` out of step with ``output_lines``. Once the two
    # have drifted, equal lengths (both deques at the cap) no longer prove alignment, so no time is served again.
    times_misaligned: bool = False
    output_writer: OutputWriter | None = None
    # The last stderr lines of a run whose own engine persists its output (``output_writer`` is None): the dashboard
    # never stores that process's stderr as it arrives, so finalization writes these after the engine's rows.
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=STDERR_TAIL_LINES))
    reader_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    started_at: float = field(default_factory=time.monotonic)
    started_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_result_cost: float | None = None
    pr_number: int | None = None
    pre_run_sha: str | None = None
    # Local HEAD of the resolved worktree at spawn time, captured only for a
    # non-PR command run (no pr_number, so pre_run_sha is never set from a PR
    # headRefOid). Lets `_check_pr_diff_guard()` reach a baseline for a
    # command like `rearrange-commits` invoked without a PR number, where
    # pre_run_sha would otherwise be permanently None.
    pre_run_head_sha: str | None = None
    prompt: str | None = None
    project_dir: Path = field(default_factory=Path.cwd)
    # The worktree/directory the process actually runs in, resolved at spawn
    # time via `_resolve_issue_worktree()`. For a PR-based command this is
    # never `project_dir` (PR-based spawns abort if the two resolve equal),
    # so a git-diff guard must use this, not `project_dir`, to inspect the
    # PR's own branch rather than the primary checkout.
    worktree_path: Path | None = None
    resource_collector: ResourceCollector | None = None
    resource_writer: ResourceWriter | None = None
    resource_flush_task: asyncio.Task | None = None
    # Per-process runtime stream parser (e.g. CodexStreamParser), set at spawn
    # time from AgentRuntime.create_stream_parser(). None for runtimes with no
    # per-process state (Claude Code, Aider), which parse inline instead.
    stream_parser: StreamParser | None = None
    # Resolved once at spawn time from AgentRuntime.stream_reader_drain_timeout()
    # when stream_parser is not None; None means the runtime declared no
    # tuning of its own and the caller should use its own default.
    stream_reader_drain_timeout: float | None = None
    # Set when the runtime's own stream reports a terminal failure (e.g.
    # Codex's turn.failed/error), so a nonzero exit code is never required to
    # detect it. Consumed by _wait_and_finalize() to prevent a terminal
    # stream failure from ever finalizing as success.
    stream_failure: str | None = None
    # Runtime-reported session/thread id (e.g. Codex's thread id), recorded
    # for diagnostics only. Never implies a turn was resumed.
    runtime_session_id: str | None = None
    # Model the runtime's own terminal result reported running (e.g. Codex's
    # LLMResult.model), set from the "result" StreamEvent in
    # _parse_runtime_stream_line(). None until a terminal event arrives, so a
    # cost record written before one does falls back to its own default
    # rather than claiming a model this agent never confirmed running.
    runtime_model: str | None = None
    # Token usage from the runtime's own terminal result (e.g. Codex's
    # LLMResult.input_tokens/output_tokens/cache_*), set alongside
    # runtime_model in _parse_runtime_stream_line(). None until a terminal
    # event arrives, distinct from 0: a run that genuinely used zero tokens
    # of a kind is a different fact than one that never reported a terminal
    # event at all.
    last_result_input_tokens: int | None = None
    last_result_output_tokens: int | None = None
    last_result_cache_read_tokens: int | None = None
    last_result_cache_write_tokens: int | None = None
    # Codex-only breakdown of last_result_output_tokens (never a separate
    # addend; see LLMResult.reasoning_output_tokens). None on every
    # non-Codex runtime, which never populates the field at all.
    last_result_reasoning_tokens: int | None = None
    # Provenance of last_result_cost (e.g. CostSource.SUBSCRIPTION/UNKNOWN for
    # Codex), set alongside the token fields above. None until a terminal
    # event arrives, mirroring their own "never arrived" convention.
    last_result_cost_source: CostSource | None = None
    # The AgentRuntime this process was spawned through (e.g. "codex"), set at
    # spawn time from AgentRuntime.name. None for a direct-spawn role
    # (spawn_direct(), e.g. reviewer), which never goes through an
    # AgentRuntime at all. Used only as a cost-attribution fallback
    # (agent_db._record_cost()) when the runtime's own stream reported no
    # model of its own: honest attribution to the runtime that ran, rather
    # than the hardcoded Claude Code default.
    runtime_name: str | None = None
    # The Claude Code result events' num_turns (LLMResult.num_turns), summed
    # across a session's segments and written on the run's "agent"
    # CostRecord. None until a result reports it, and for Codex, whose stream
    # reports no turn count.
    last_result_num_turns: int | None = None
    # Per-phase model-call, token and time tally for a command:* run, fed by
    # _parse_stream_line() and persisted as TaskRun.phase_usage on finalize.
    # Built here from the role so the spawn path needs no change; None for
    # every other role.
    phase_tally: PhaseTally | None = None

    def __post_init__(self) -> None:
        if self.phase_tally is None and self.role.startswith("command:"):
            self.phase_tally = PhaseTally(started_at=self.started_at)

    def buffer(self, text: str, stamp: datetime) -> None:
        """Append one output line with its write time and count it: the only writer of the three output fields."""
        self.output_lines.append(text)
        self.output_times.append(stamp)
        self.output_seq += 1


@dataclass
class CompletedAgent:
    """Recently completed agent kept briefly for UI transition."""

    run_id: int
    issue: str
    role: str
    status: str
    cost: float
    completed_at: float = field(default_factory=time.monotonic)


@dataclass
class ProjectAgents:
    """Per-project collection of running agents."""

    agents: dict[int, AgentState] = field(default_factory=dict)
    recently_completed: deque[CompletedAgent] = field(
        default_factory=lambda: deque(maxlen=MAX_RECENTLY_COMPLETED),
    )
    max_concurrent: int = 2
    project_dir: Path = field(default_factory=Path.cwd)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_projects: dict[str, ProjectAgents] = {}
_projects_lock = threading.Lock()
_default_project_dir: Path | None = None


def _new_pool_project_dir(slug: str, *, explicit: bool) -> Path:
    """The directory a new pool for *slug* is created with.

    The ambient project directory belongs to the ambient slug only: an explicit *slug* naming another project (an
    auto-handoff follow-up for a different project than the request being served) takes its registered path, so
    the pool never runs one project's agents in another's directory, configuration and slot limit. With no
    registered path, the ambient directory is still never used when the ambient slug names a different project:
    the pool would be cached under this slug for good, so the default directory is used and a warning logged. An
    unnamed ambient context (no slug, as in single-project mode) keeps its directory, as before.
    """
    ambient = get_project_dir()
    ambient_slug = get_project_slug()
    foreign = explicit and ambient_slug != slug
    if slug == _DEFAULT_SLUG or (ambient is not None and not foreign):
        return ambient or _default_project_dir or Path.cwd()

    from sova.config.registry import get_project_path

    registered = get_project_path(slug)
    if registered is not None:
        return registered
    if foreign and ambient_slug is not None:
        log.warning("pool.unregistered_slug", slug=slug, ambient_slug=ambient_slug)
        return _default_project_dir or Path.cwd()
    return ambient or _default_project_dir or Path.cwd()


def _get_project_agents(slug: str | None = None) -> ProjectAgents:
    """Get or create agent collection for a project slug."""
    explicit = slug is not None
    if slug is None:
        slug = get_project_slug() or _DEFAULT_SLUG

    with _projects_lock:
        pa = _projects.get(slug)
        if pa is None:
            resolved = _new_pool_project_dir(slug, explicit=explicit).resolve()
            max_conc = read_max_parallel(resolved)
            pa = _projects.setdefault(
                slug,
                ProjectAgents(project_dir=resolved, max_concurrent=max_conc),
            )

    return pa


def read_max_parallel(project_dir: Path) -> int:
    """Read max_parallel_agents from config, falling back to dataclass default."""
    try:
        return load_config(project_dir).max_parallel_agents
    except Exception:  # noqa: BLE001 (config may fail for many reasons (missing file, bad TOML, import errors))
        log.warning("pool.max_parallel_config_failed", project_dir=str(project_dir), exc_info=True)
        return ProjectAgents.max_concurrent


def get_default_project_dir() -> Path | None:
    """Return the default project directory (single-project mode)."""
    return _default_project_dir


def set_project_dir(path: Path) -> None:
    """Set the default project directory (single-project mode)."""
    global _default_project_dir
    _default_project_dir = path
    pa = _get_project_agents(_DEFAULT_SLUG)
    pa.project_dir = path.resolve()


def sync_max_concurrent(project_dir: Path | None = None, slug: str | None = None) -> None:
    """Read max_parallel_agents from config and apply to the in-memory pool."""
    try:
        cfg = load_config(project_dir)
        pa = _get_project_agents(slug)
        if cfg.max_parallel_agents != pa.max_concurrent:
            log.info(
                "pool.max_concurrent.updated",
                old=pa.max_concurrent,
                new=cfg.max_parallel_agents,
            )
            pa.max_concurrent = cfg.max_parallel_agents
    except Exception:  # noqa: BLE001 (slot count keeps its previous value when config is unavailable)
        log.warning("pool.max_concurrent.sync_failed", exc_info=True)


def list_all_pools() -> dict[str, ProjectAgents]:
    """Return all initialized project pools (projects that have been accessed this session)."""
    with _projects_lock:
        return dict(_projects)


def _prune_completed(pa: ProjectAgents, now: float | None = None) -> None:
    """Remove expired entries from recently_completed."""
    if now is None:
        now = time.monotonic()
    while pa.recently_completed and (now - pa.recently_completed[0].completed_at) > RECENTLY_COMPLETED_TTL:
        pa.recently_completed.popleft()


def _evict_completed_for_issue(pa: ProjectAgents, issue: str) -> None:
    """Remove completed entries for *issue* so they don't linger when a new run starts.

    Must be called inside ``pa._lock``.
    """
    pa.recently_completed = deque(
        (ca for ca in pa.recently_completed if ca.issue != issue),
        maxlen=MAX_RECENTLY_COMPLETED,
    )
