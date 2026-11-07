"""Process management for agent subprocesses.

Handles process lifecycle monitoring, stdout streaming for dashboard
display, and crash detection. Runtime-specific spawning logic lives
in the corresponding AgentRuntime implementation (see runtime.py).
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import TaskRun
from sova.utils.logging import get_logger

log = get_logger(component="ipc.control")


class ExitClassification(enum.StrEnum):
    """Classification of a process exit code."""

    SUCCESS = "success"
    ERROR = "error"
    CRASH = "crash"


@dataclasses.dataclass(frozen=True, slots=True)
class TerminationRecord:
    """Provenance for a deliberate ``stop()`` call, attached to the process wrapper.

    Recorded at the point the signal is sent (not observed at exit), since by
    the time the exit is noticed the sender is gone. ``cause`` identifies why
    (e.g. "manual_stop", "memory_pressure", a watchdog anomaly signal name),
    ``requester`` identifies who asked for it (e.g. "dashboard", "watchdog"),
    and ``signal`` is the POSIX signal number actually sent. ``detail`` carries
    free-form context, e.g. a note that the stop escalated to SIGKILL.
    """

    cause: str
    requester: str
    signal: int
    detail: str = ""


def normalize_signal_exit(returncode: int) -> int | None:
    """Return the signal number if *returncode* represents a signal-terminated exit.

    Handles both forms a caller may observe: asyncio's own convention (negative
    return code, e.g. ``-15`` for SIGTERM) and the shell convention some wrapper
    layers report instead (``128 + signal``, e.g. ``143`` for SIGTERM). Returns
    ``None`` for an ordinary exit code (0 or a ``1``-``127`` application error),
    which must never be misread as a signal.
    """
    if returncode < 0:
        return -returncode
    if returncode >= 128:
        sig = returncode - 128
        if 1 <= sig <= 64:
            return sig
    return None


# Exit code an adopted process reports when its outcome could not be determined (its kernel exit status is
# unavailable and it left no status, handoff or result line). Finalize records such a run as "interrupted"
# rather than "failed" and sends no failure notification: an unknown outcome is not a confirmed failure.
EXIT_OUTCOME_UNKNOWN = -1000

_ADOPTED_POLL_INTERVAL = 1.0


class AdoptedProcess:
    """Stand-in for ``asyncio.subprocess.Process`` for a live process this server did not spawn.

    A restarted server adopts an agent the previous server launched. That process is not our child, so
    its exit code cannot be waited on: ``wait()`` polls *is_alive* and, once the process is gone, takes
    the code from *resolve_exit_code* (derived from signals the agent left behind, not from the kernel).
    *is_alive* must be identity-aware, so a recycled PID reads as an exit and is never signalled.
    """

    def __init__(
        self,
        pid: int,
        *,
        is_alive: Callable[[], bool],
        resolve_exit_code: Callable[[], Awaitable[int]],
    ) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._is_alive = is_alive
        self._resolve_exit_code = resolve_exit_code

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def _signal(self, sig: signal.Signals) -> None:
        if self.returncode is None and self._is_alive():
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                pass  # exited between the liveness check and the signal: same as Popen.send_signal
            except PermissionError:
                # The PID now belongs to another user's process: never ours to signal, so it counts as not signalled.
                log.warning("ipc.adopted_signal_denied", pid=self.pid, signal=int(sig), exc_info=True)

    async def wait(self) -> int:
        while self.returncode is None:
            if not self._is_alive():
                self.returncode = await self._resolve_exit_code()
                break
            await asyncio.sleep(_ADOPTED_POLL_INTERVAL)
        return self.returncode


class _BaseAgentProcess:
    """Shared process-delegation logic for AgentProcess and FileAgentProcess."""

    def __init__(self, proc: asyncio.subprocess.Process | AdoptedProcess) -> None:
        self._proc = proc
        self.termination_record: TerminationRecord | None = None

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def is_running(self) -> bool:
        return self._proc.returncode is None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def wait(self) -> int:
        await self._proc.wait()
        return self._proc.returncode

    async def stop(self, timeout: float = 10.0, *, cause: str = "manual_stop", requester: str = "") -> None:
        """Terminate the process, recording why before the signal is sent.

        ``cause``/``requester`` become a ``TerminationRecord`` on this wrapper
        that ``_wait_and_finalize()`` reads once the process exits, so a
        deliberate stop can be told apart from an external kill (OOM, `pkill`,
        etc.) after the fact. Returns immediately with no record attached when
        the process is already not running: there is nothing to attribute.
        """
        if not self.is_running:
            return

        log.info("process.stop", pid=self.pid, cause=cause, requester=requester)
        self.termination_record = TerminationRecord(cause=cause, requester=requester, signal=int(signal.SIGTERM))
        self._proc.terminate()

        try:
            async with asyncio.timeout(timeout):
                await self._proc.wait()
        except TimeoutError:
            log.warning("process.kill", pid=self.pid, cause=cause, requester=requester)
            self.termination_record = dataclasses.replace(
                self.termination_record, detail="escalated to SIGKILL after timeout"
            )
            self._proc.kill()
            await self._proc.wait()

    @staticmethod
    def classify_exit(returncode: int) -> ExitClassification:
        if returncode == 0:
            return ExitClassification.SUCCESS
        if returncode < 128:
            return ExitClassification.ERROR
        return ExitClassification.CRASH

    async def wait_classified(self) -> tuple[int, ExitClassification]:
        code = await self.wait()
        return code, self.classify_exit(code)


class AgentProcess(_BaseAgentProcess):
    """Wrapper around an async subprocess running Claude CLI (pipe-based I/O)."""

    async def stdout_lines(self) -> AsyncIterator[str]:
        """Yield stdout lines as they arrive (for dashboard streaming)."""
        if self._proc.stdout is None:
            return

        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="replace").rstrip("\n")

    async def stderr_lines(self) -> AsyncIterator[str]:
        """Yield stderr lines as they arrive (for error capture)."""
        if self._proc.stderr is None:
            return

        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="replace").rstrip("\n")


_TAIL_POLL_INTERVAL = 0.1


def _ends_with_newline(path: Path, size: int) -> bool:
    """True when the byte at offset ``size - 1`` of *path* is a newline (a line boundary). Fails open to True."""
    try:
        with open(path, "rb") as fh:  # NOSONAR
            fh.seek(size - 1)
            return fh.read(1) == b"\n"
    except OSError:
        return True


class FileAgentProcess(_BaseAgentProcess):
    """AgentProcess variant that reads output from files instead of pipes.

    Used for dashboard-spawned agents where stdout/stderr are redirected to
    files. This decouples agent I/O from the parent process, allowing agents
    to survive dashboard restarts (no SIGPIPE when the parent dies).
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process | AdoptedProcess,
        *,
        stdout_path: Path,
        stderr_path: Path,
        tail_from_end: bool = False,
    ) -> None:
        super().__init__(proc)
        self._stdout_path = stdout_path
        self._stderr_path = stderr_path
        # An adopted process has already written output that an earlier server captured: start at the
        # end of each file, so only what it writes from now on is read.
        self._tail_from_end = tail_from_end

    @property
    def stdout_path(self) -> Path:
        return self._stdout_path

    @property
    def stderr_path(self) -> Path:
        return self._stderr_path

    async def stdout_lines(self) -> AsyncIterator[str]:
        async for line in self._tail_file(self._stdout_path):
            yield line

    async def stderr_lines(self) -> AsyncIterator[str]:
        async for line in self._tail_file(self._stderr_path):
            yield line

    async def _tail_file(self, path: Path) -> AsyncIterator[str]:
        """Tail a file, yielding complete lines as they appear.

        Stops when the process has exited and no new data remains.
        """
        if not path.exists():
            return

        try:
            fh = open(path, encoding="utf-8", errors="replace")  # NOSONAR
        except OSError:
            log.warning("tail.open_failed", path=str(path), exc_info=True)
            return

        try:
            # True while the first line read is the tail of one the agent was mid-way through writing
            # when tailing began: its head was never read, so what follows up to the newline is garbage.
            skip_partial_line = False
            if self._tail_from_end:
                end = fh.seek(0, os.SEEK_END)
                skip_partial_line = end > 0 and not _ends_with_newline(path, end)
            remainder = ""
            while True:
                chunk = fh.read(8192)
                if chunk:
                    remainder += chunk
                    while "\n" in remainder:
                        line, remainder = remainder.split("\n", 1)
                        if skip_partial_line:
                            skip_partial_line = False
                            continue
                        yield line
                else:
                    if self._proc.returncode is not None:
                        if remainder and not skip_partial_line:
                            yield remainder
                        return
                    await asyncio.sleep(_TAIL_POLL_INTERVAL)
        finally:
            fh.close()


class ProcessTracker:
    """Tracks running agent processes by task_run_id.

    Used by the orchestrator to monitor and recover from crashes.
    """

    def __init__(self) -> None:
        self._processes: dict[int, AgentProcess] = {}

    def register(self, task_run_id: int, process: AgentProcess) -> None:
        """Register a process for a task run."""
        self._processes[task_run_id] = process
        log.info("tracker.register", run_id=task_run_id, pid=process.pid)

    def unregister(self, task_run_id: int) -> None:
        """Remove a process from tracking."""
        self._processes.pop(task_run_id, None)

    def get(self, task_run_id: int) -> AgentProcess | None:
        """Get the process for a task run, or None."""
        return self._processes.get(task_run_id)

    def list_active(self) -> list[tuple[int, AgentProcess]]:
        """List all currently running processes as (task_run_id, process) pairs."""
        return [(rid, proc) for rid, proc in self._processes.items() if proc.is_running]


async def mark_crashed(task_run_id: int, error_message: str, session: AsyncSession) -> None:
    """Mark a TaskRun as failed due to a process crash.

    Called when AgentProcess exits with a crash classification
    and no handoff was written.
    """
    async with session.begin():
        tr = await session.get(TaskRun, task_run_id)
        if tr is None:
            log.warning("mark_crashed.not_found", run_id=task_run_id)
            return
        tr.status = "failed"
        tr.error_message = error_message
        tr.ended_at = datetime.now(timezone.utc)
    log.info("mark_crashed", run_id=task_run_id, error=error_message)
