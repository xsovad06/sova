"""Adopt agent processes that outlived the server that launched them.

Agents are spawned with ``start_new_session=True`` and file-redirected output, so a server restart
leaves them running. Startup recovery re-registers such a live, identity-matched run in the agent pool
instead of killing it, so the restarted server manages it like any run it started itself.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sova.core.state import TASK_RUN_TERMINAL
from sova.ipc.control import EXIT_OUTCOME_UNKNOWN, AdoptedProcess, FileAgentProcess
from sova.ipc.handoff import read_all_handoff_files
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.dashboard.services.agent_pool import ProjectAgents
    from sova.db.models import TaskRun

log = get_logger(component="dashboard.adopt")


def _pool_for(project_dir: Path | None) -> ProjectAgents:
    """The agent pool that owns *project_dir*, falling back to the default pool."""
    from sova.config.registry import find_slug_for_path
    from sova.dashboard.services.agent_pool import _get_project_agents, list_all_pools

    if project_dir is not None:
        resolved = project_dir.resolve()
        for pa in list_all_pools().values():
            if pa.project_dir.resolve() == resolved:
                return pa
        slug = find_slug_for_path(resolved)
        if slug:
            return _get_project_agents(slug)
    return _get_project_agents(None)


# Statuses a run can end in without having failed: the pipeline finished, or stopped on purpose for a human.
_NON_FAILURE_TERMINAL = frozenset({"done", "awaiting_approval", "paused"})

# How much of the end of a run's stdout is scanned for its final stream-json result event.
_RESULT_TAIL_BYTES = 256 * 1024


def _read_tail_lines(stdout_path: Path) -> list[str]:
    """The last lines of *stdout_path* (a bounded read), or [] when it cannot be read.

    A first line cut by the window is returned as is; callers treat a line that does not parse as noise.
    """
    try:
        with open(stdout_path, "rb") as fh:  # NOSONAR
            size = fh.seek(0, 2)
            fh.seek(max(0, size - _RESULT_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return tail.splitlines()


def _claude_result_is_error(lines: list[str]) -> bool | None:
    """``is_error`` of the last stream-json ``result`` event in *lines*, or None when there is none.

    Claude Code ends its stream with ``{"type":"result","is_error":false,...}``.
    """
    for line in reversed(lines):
        if '"result"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result" and isinstance(event.get("is_error"), bool):
            return event["is_error"]
    return None


def _runtime_result_is_error(lines: list[str], parser: object) -> bool | None:
    """Replay *lines* through a fresh runtime stream parser and report its terminal result.

    A runtime with a per-process parser (Codex: ``turn.completed`` / ``turn.failed``) has no Claude
    ``result`` line, so only its own parser can say how the run ended. True when the last terminal event
    was an error, False when it was a clean result, None when the tail holds no terminal event.
    """
    outcome: bool | None = None
    for line in lines:
        if not line.strip():
            continue
        try:
            event = parser.parse_line(line)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 (a line cut by the tail window or in a foreign format is noise)
            continue
        if event is not None and event.type == "result" and event.result is not None:
            outcome = event.result.stop_reason == "error"
    return outcome


def _read_result_is_error(stdout_path: Path, role: str = "") -> bool | None:
    """Whether the run recorded in *stdout_path* ended in an error, or None when its tail does not say.

    Asks the runtime's own stream parser first when the run has one (the runtime configured now, as at
    spawn), else reads Claude's stream-json ``result`` event. Only the file's tail is read.
    """
    lines = _read_tail_lines(stdout_path)
    if not lines:
        return None
    parser = _fresh_stream_parser(role)
    if parser is not None:
        return _runtime_result_is_error(lines, parser)
    return _claude_result_is_error(lines)


def _fresh_stream_parser(role: str) -> object | None:
    """A new per-process stream parser for *role*'s runtime, or None (direct spawn, Claude, or a failure)."""
    from sova.dashboard.services.agent_lifecycle import _DIRECT_SPAWN_ROLES
    from sova.ipc.runtime import get_runtime

    if role in _DIRECT_SPAWN_ROLES:
        return None
    try:
        return get_runtime().create_stream_parser()
    except Exception:  # noqa: BLE001 (no parser means the Claude tail read is used)
        log.warning("adopt.stream_parser_failed", role=role, exc_info=True)
        return None


def _find_pr_completion_handoff(project_dir: Path, pr_number: int | None, started_at: datetime | None) -> bool:
    """True when a handoff for *pr_number* awaiting action was written after *started_at*.

    The issue-less counterpart of ``_find_completion_handoff``: a PR-only run (a reviewer on a PR with
    no issue) shares the legacy handoff file with other runs, so the handoff is selected by its PR.
    """
    if not pr_number:
        return False
    run_start = started_at or datetime.min.replace(tzinfo=timezone.utc)
    if run_start.tzinfo is None:
        run_start = run_start.replace(tzinfo=timezone.utc)
    try:
        for handoff in read_all_handoff_files(project_dir):
            if handoff.pr_number != pr_number or handoff.status != "awaiting_action":
                continue
            created = datetime.fromisoformat(handoff.created_at.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= run_start:
                return True
    except (OSError, ValueError):
        log.debug("adopt.pr_handoff_check_failed", pr=pr_number, exc_info=True)
    return False


def _is_adopted_alive(pid: int, started_at: datetime | None) -> bool:
    """Liveness of an adopted process: identity-matched and not an exited-but-unreaped zombie.

    A defunct process still has a PID and a create time, so the identity check alone reads it as alive
    and ``AdoptedProcess.wait()`` would never return. An untracked child of this dashboard is reaped
    here (asyncio's child watcher only reaps the PIDs it spawned itself).
    """
    from sova.dashboard.services.agent_recovery import _is_zombie_process

    if started_at is None:
        return False  # no creation time to match: the PID cannot be told from a recycled one, so it is never signalled
    if not _is_zombie_process(pid, started_at):
        return False
    try:
        import psutil

        try:
            proc = psutil.Process(pid)
            if proc.status() != psutil.STATUS_ZOMBIE:
                return True
            if proc.ppid() == os.getpid():
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass  # already reaped
            return False
        except psutil.NoSuchProcess:
            return False
    except Exception:  # noqa: BLE001 (a probe failure after the identity check passed leaves the run alive)
        log.debug("adopt.liveness_probe_failed", pid=pid, exc_info=True)
        return True


async def _derive_exit_code(
    run_id: int,
    issue: str | None,
    started_at: datetime | None,
    project_dir: Path,
    stdout_path: Path,
    *,
    role: str = "",
    pr_number: int | None = None,
) -> int:
    """Exit code for an adopted process that is gone, from what it left behind.

    The kernel's code is unavailable (it was never our child), so this reads, in order: the run's own
    status when terminal (the inner ``sova run`` finalizes it), the completion handoff (by issue, or by
    PR for an issue-less run), and the terminal result in the run's stdout, read through the runtime's
    own stream parser when it has one (a command run is only ever finalized from the exit code, so its
    row is still non-terminal). With none of them the outcome is unknown, which is
    ``EXIT_OUTCOME_UNKNOWN`` and not a failure: finalize records such a run as interrupted.
    """
    from sova.dashboard.services.agent_db import _get_task_run_status
    from sova.dashboard.services.agent_recovery import _find_completion_handoff

    status = await _get_task_run_status(run_id, project_dir)
    if status in _NON_FAILURE_TERMINAL:
        return 0
    if status in TASK_RUN_TERMINAL:
        return 1
    if issue:
        if await asyncio.to_thread(_find_completion_handoff, project_dir, issue, started_at) is not None:
            return 0
    elif await asyncio.to_thread(_find_pr_completion_handoff, project_dir, pr_number, started_at):
        return 0
    from sova.dashboard.services.agent_db import _COMMAND_ROLE_PREFIX

    # Only a command run's stdout is stream-json; a pipeline run's holds CLI output, which proves nothing
    # (the same rule as ``_derive_finished_outcome``, so a run gets one answer whenever it ended).
    if role.startswith(_COMMAND_ROLE_PREFIX):
        is_error = await asyncio.to_thread(_read_result_is_error, stdout_path, role)
        if is_error is not None:
            return 1 if is_error else 0
    log.warning("adopt.exit_outcome_unknown", run_id=run_id, db_status=status)
    return EXIT_OUTCOME_UNKNOWN


def _runtime_stream_setup(role: str, run_id: int) -> tuple[object | None, float | None, str | None]:
    """``(stream_parser, drain_timeout, runtime_name)`` for an adopted run, as ``start_agent`` sets them at spawn.

    A direct-spawn role never goes through an AgentRuntime, so it gets none. The runtime is the one
    configured now: a config switched between the spawn and the restart would parse with the wrong one,
    which is why a parser that cannot be built is logged and left as the inline Claude parser.
    """
    from sova.dashboard.services.agent_lifecycle import _DIRECT_SPAWN_ROLES
    from sova.ipc.runtime import get_runtime

    if role in _DIRECT_SPAWN_ROLES:
        return None, None, None
    try:
        runtime = get_runtime()
    except Exception:  # noqa: BLE001 (a runtime that cannot be built must not stop a live run being adopted)
        log.warning("adopt.stream_parser_failed", run_id=run_id, runtime=None, exc_info=True)
        return None, None, None
    parser = None
    drain_timeout = None
    try:
        parser = runtime.create_stream_parser()
        if parser is not None:
            drain_timeout = runtime.stream_reader_drain_timeout()
    except Exception:  # noqa: BLE001 (a parser failure must not stop a live run being adopted)
        log.warning("adopt.stream_parser_failed", run_id=run_id, runtime=runtime.name, exc_info=True)
        parser, drain_timeout = None, None
    return parser, drain_timeout, runtime.name


async def _resolve_adopted_worktree(run: TaskRun, root: Path, role: str) -> Path | None:
    """Worktree of an adopted PR-based command run, looked up without touching anything, else None.

    The agent is still running in that checkout, so this only reads: it never prepares, repairs, creates
    or switches a worktree (``_resolve_issue_worktree`` does all of those before a spawn, and would rewrite
    files under the live agent). The path comes from the run's own record (``worktree_path``), else the
    issue's ``.claude/worktrees/<issue>`` directory, else the worktree holding the run's recorded
    ``branch_name``. It does no network I/O, which keeps startup recovery fast.

    The pre-run baseline (``pre_run_sha``) died with the previous server, so the silent-revert and
    pin-downgrade guard (``_check_pr_diff_guard``) cannot run for this run whatever the worktree is;
    ``adopt.diff_guard_baseline_unavailable`` records that the check was skipped. The worktree is still
    resolved so the push-verification tiers run against the run's own checkout, not the primary one.
    """
    from sova.dashboard.services.agent_db import _COMMAND_ROLE_PREFIX
    from sova.git.worktree import find_worktree_by_branch

    if not role.startswith(_COMMAND_ROLE_PREFIX) or not run.pr_number:
        return None
    log.warning("adopt.diff_guard_baseline_unavailable", run_id=run.id, role=role, pr=run.pr_number)
    try:
        candidates: list[Path] = []
        recorded = run.worktree_path
        if isinstance(recorded, str) and recorded:
            candidates.append(Path(recorded))
        issue_id = (run.issue_number or "").lstrip("#").strip()
        if issue_id.isdigit():
            candidates.append(root / ".claude" / "worktrees" / issue_id)
        for candidate in candidates:
            if candidate.is_dir():
                return None if candidate.resolve() == root.resolve() else candidate
        branch = run.branch_name
        if isinstance(branch, str) and branch:
            found = await find_worktree_by_branch(branch, cwd=root)
            if found is not None and found.resolve() != root.resolve():
                return found
    except Exception:  # noqa: BLE001 (worktree lookup is best-effort: the run is adopted without one)
        log.warning("adopt.worktree_resolve_failed", run_id=run.id, exc_info=True)
    return None


# Adoption claims held by this process: (project dir, run id) -> descriptor of the locked claim file.
_adoption_claims: dict[tuple[str, int], int] = {}


def _claim_run(root: Path, run_id: int) -> bool:
    """Take the cross-process claim on adopting *run_id*. True when this process now owns the run.

    Pools are per process, so two servers on one project database (a service plus an interactive dashboard)
    would each adopt the same live agent and finalize it twice, spawning each follow-up run twice. An
    exclusive ``flock`` on ``.claude/agent-output/<run_id>.adopt.lock``, held until the run ends, lets exactly
    one of them win. The kernel drops it when the owner dies, so a crashed owner never blocks a successor,
    and it needs no schema change. Where ``flock`` is unavailable or the lock cannot be taken for another
    reason, the claim is skipped (True): adoption stays best-effort and never blocks a live run.
    """
    try:
        import fcntl
    except ImportError:
        return True
    key = (str(root), run_id)
    if key in _adoption_claims:
        return False
    fd = -1
    try:
        lock_path = root / ".claude" / "agent-output" / f"{run_id}.adopt.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        # The lock file cannot be created or opened (permissions, a read-only sandbox): best effort, no claim.
        log.warning("adopt.claim_unavailable", run_id=run_id, exc_info=True)
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            log.info("adopt.claimed_elsewhere", run_id=run_id)
            return False
        log.warning("adopt.claim_unavailable", run_id=run_id, exc_info=True)
        return True
    _adoption_claims[key] = fd
    return True


def _release_claim(root: Path, run_id: int) -> None:
    """Drop this process's adoption claim on *run_id*, if it holds one."""
    fd = _adoption_claims.pop((str(root), run_id), None)
    if fd is not None:
        try:
            os.close(fd)  # closing the descriptor releases the flock
        except OSError:
            log.debug("adopt.claim_release_failed", run_id=run_id, exc_info=True)


def hold_spawn_claim(root: Path, run_id: int, wait_task: "asyncio.Task[object]") -> None:
    """Take the adoption claim for a run this process just spawned, released when *wait_task* ends.

    Without it a live spawner is invisible to an adopter: a second server whose sweep misreads this
    process's agents as orphans (a ``--reload`` worker's cmdline is not recognized as SOVA) would find the
    lock free, adopt them and run every finalize, notification and auto-handoff a second time. Holding the
    lock from spawn makes the claim cover every run a live process manages, whatever its cmdline.
    """
    _claim_run(root, run_id)
    wait_task.add_done_callback(lambda _t: _release_claim(root, run_id))


async def adopt_orphaned_run(run: TaskRun, project_dir: Path | None) -> bool:
    """Register a live orphaned run in the agent pool and start managing it. True when adopted.

    The caller has already confirmed the PID is alive, matches the run's creation time and is an
    orphan of this dashboard. Output capture resumes by tailing the run's ``.stdout`` file from its
    current end, so what the agent wrote while no server was watching is not replayed (it stays in
    ``.claude/agent-output/<run_id>.stdout``). With no such file the run is adopted without capture.
    A run another process already adopted (``_claim_run``) or one with no recorded start time (its PID
    cannot be told from a recycled one) is left alone and reported as not adopted. Any failure is logged and
    leaves the run untouched and unmanaged.
    """
    from sova.core.output import OutputWriter
    from sova.dashboard.services.agent_finalize import _wait_and_finalize
    from sova.dashboard.services.agent_output import _read_output, _read_stderr
    from sova.dashboard.services.agent_pool import AgentState
    from sova.dashboard.services.agent_resource import _background_tasks, _start_resource_monitoring

    claimed = False
    root = project_dir
    run_id = getattr(run, "id", None)
    try:
        pa = _pool_for(project_dir)
        root = project_dir if project_dir is not None else pa.project_dir
        pid = int(run.pid)
        run_id = int(run.id)
        issue = run.issue_number
        started_at = run.started_at
        if started_at is None:
            log.warning("adopt.skipped_no_start_time", run_id=run_id, pid=pid)
            return False
        async with pa._lock:
            if run_id in pa.agents:
                return True
        if not _claim_run(root, run_id):
            return False
        claimed = True

        role = run.role or "developer"
        pr_number = run.pr_number
        stdout_path = root / ".claude" / "agent-output" / f"{run_id}.stdout"
        process = FileAgentProcess(
            AdoptedProcess(
                pid,
                is_alive=lambda: _is_adopted_alive(pid, started_at),
                resolve_exit_code=lambda: _derive_exit_code(
                    run_id, issue, started_at, root, stdout_path, role=role, pr_number=pr_number
                ),
            ),
            stdout_path=stdout_path,
            stderr_path=root / ".claude" / "agent-output" / f"{run_id}.stderr",
            tail_from_end=True,
        )
        stream_parser, drain_timeout, runtime_name = _runtime_stream_setup(role, run_id)
        agent = AgentState(
            run_id=run_id,
            issue=issue or "",
            role=role,
            stream_parser=stream_parser,
            stream_reader_drain_timeout=drain_timeout,
            runtime_name=runtime_name,
            process=process,
            output_writer=OutputWriter(root, run_id),
            pr_number=pr_number,
            project_dir=root,
            worktree_path=await _resolve_adopted_worktree(run, root, role),
        )
        # The run's real start, not the adoption moment, so the dashboard's elapsed time stays true.
        started_utc = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
        agent.started_at_utc = started_utc
        agent.started_at = time.monotonic() - max(0.0, (datetime.now(timezone.utc) - started_utc).total_seconds())
        async with pa._lock:
            pa.agents[run_id] = agent
    except Exception:  # noqa: BLE001 (adoption is best-effort: a failure must leave the live run alone)
        log.warning("adopt.failed", run_id=run_id, exc_info=True)
        if claimed:
            _release_claim(root, run_id)
        return False

    agent.reader_task = asyncio.create_task(_read_output(agent))
    agent.stderr_task = asyncio.create_task(_read_stderr(agent))
    _start_resource_monitoring(agent, root, pid)
    wait_task = asyncio.create_task(_wait_and_finalize(pa, agent))
    _background_tasks.add(wait_task)
    wait_task.add_done_callback(_background_tasks.discard)
    wait_task.add_done_callback(lambda _t: _release_claim(root, run_id))
    log.warning(
        "adopt.adopted",
        run_id=run_id,
        issue=issue,
        pid=pid,
        capture="file_tail_from_end" if process.stdout_path.exists() else "none",
    )
    return True
