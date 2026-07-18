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

from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import TaskRun
from sova.utils.logging import get_logger

log = get_logger(component="ipc.control")


class ExitClassification(enum.StrEnum):
    """Classification of a process exit code."""

    SUCCESS = "success"
    ERROR = "error"
    CRASH = "crash"


class AgentProcess:
    """Wrapper around an async subprocess running Claude CLI."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    @property
    def pid(self) -> int:
        """Process ID of the subprocess."""
        return self._proc.pid

    @property
    def is_running(self) -> bool:
        """Whether the subprocess is still alive."""
        return self._proc.returncode is None

    @property
    def returncode(self) -> int | None:
        """Exit code, or None if still running."""
        return self._proc.returncode

    async def wait(self) -> int:
        """Wait for the process to finish and return its exit code."""
        await self._proc.wait()
        return self._proc.returncode

    async def stop(self, timeout: float = 10.0) -> None:
        """Gracefully stop the process (SIGTERM, then SIGKILL after timeout)."""
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
            try:
                async with asyncio.timeout(5.0):
                    await self._proc.wait()
            except TimeoutError:
                # On macOS a process in uninterruptible sleep ignores SIGKILL.
                # Give up rather than blocking the event loop indefinitely.
                log.warning("process.sigkill_timeout", pid=self.pid)

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

    @staticmethod
    def classify_exit(returncode: int) -> ExitClassification:
        """Classify an exit code into SUCCESS, ERROR, or CRASH."""
        if returncode == 0:
            return ExitClassification.SUCCESS
        if returncode < 128:
            return ExitClassification.ERROR
        return ExitClassification.CRASH

    async def wait_classified(self) -> tuple[int, ExitClassification]:
        """Wait for the process and return (exit_code, classification)."""
        code = await self.wait()
        return code, self.classify_exit(code)


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
