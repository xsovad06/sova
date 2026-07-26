"""Process management for agent subprocesses.

Handles process lifecycle monitoring, stdout streaming for dashboard
display, and crash detection. Runtime-specific spawning logic lives
in the corresponding AgentRuntime implementation (see runtime.py).
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import AsyncIterator
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


class _BaseAgentProcess:
    """Shared process-delegation logic for AgentProcess and FileAgentProcess."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

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

    async def stop(self, timeout: float = 10.0) -> None:
        if not self.is_running:
            return

        log.info("process.stop", pid=self.pid)
        self._proc.terminate()

        try:
            async with asyncio.timeout(timeout):
                await self._proc.wait()
        except TimeoutError:
            log.warning("process.kill", pid=self.pid)
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


class FileAgentProcess(_BaseAgentProcess):
    """AgentProcess variant that reads output from files instead of pipes.

    Used for dashboard-spawned agents where stdout/stderr are redirected to
    files. This decouples agent I/O from the parent process, allowing agents
    to survive dashboard restarts (no SIGPIPE when the parent dies).
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        stdout_path: Path,
        stderr_path: Path,
    ) -> None:
        super().__init__(proc)
        self._stdout_path = stdout_path
        self._stderr_path = stderr_path

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
            remainder = ""
            while True:
                chunk = fh.read(8192)
                if chunk:
                    remainder += chunk
                    while "\n" in remainder:
                        line, remainder = remainder.split("\n", 1)
                        yield line
                else:
                    if self._proc.returncode is not None:
                        if remainder:
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
