"""Mock runtime for deterministic agent testing.

Provides ``MockRuntime`` (an ``AgentRuntime`` implementation) and
``MockAgentProcess`` (duck-types ``AgentProcess``) so tests can exercise
agent lifecycle management without spawning real subprocesses.

This module is intentionally dependency-free (no pytest, no DB imports)
so it can be imported from any test without side effects.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

from sova.ipc.control import ExitClassification
from sova.ipc.runtime import AgentRuntime
from sova.llm.models import StreamEvent


class MockAgentProcess:
    """AgentProcess-compatible mock that simulates subprocess behavior."""

    def __init__(
        self,
        *,
        stdout_lines_data: list[str] | None = None,
        stderr_lines_data: list[str] | None = None,
        exit_code: int = 0,
        duration_seconds: float = 0.0,
        should_hang: bool = False,
        pid: int = 99999,
    ) -> None:
        self._stdout_lines_data = stdout_lines_data or []
        self._stderr_lines_data = stderr_lines_data or []
        self._exit_code = exit_code
        self._duration_seconds = duration_seconds
        self._should_hang = should_hang
        self._pid = pid
        self._returncode: int | None = None
        self._finished = asyncio.Event()
        self._hang_release = asyncio.Event()

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def is_running(self) -> bool:
        return self._returncode is None

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        if self._returncode is not None:
            return self._returncode

        if self._should_hang:
            await self._hang_release.wait()
        elif self._duration_seconds > 0:
            await asyncio.sleep(self._duration_seconds)

        self._returncode = self._exit_code
        self._finished.set()
        return self._returncode

    async def stop(self, timeout: float = 10.0) -> None:
        if self._returncode is not None:
            return
        self._hang_release.set()
        self._returncode = self._exit_code
        self._finished.set()

    async def stdout_lines(self) -> AsyncIterator[str]:
        for line in self._stdout_lines_data:
            yield line

    async def stderr_lines(self) -> AsyncIterator[str]:
        for line in self._stderr_lines_data:
            yield line

    @staticmethod
    def classify_exit(returncode: int) -> ExitClassification:
        from sova.ipc.control import AgentProcess

        return AgentProcess.classify_exit(returncode)

    async def wait_classified(self) -> tuple[int, ExitClassification]:
        code = await self.wait()
        return code, self.classify_exit(code)


class MockRuntime(AgentRuntime):
    """Test runtime that returns MockAgentProcess instances."""

    def __init__(
        self,
        *,
        stdout_lines: list[str] | None = None,
        stderr_lines: list[str] | None = None,
        exit_code: int = 0,
        duration_seconds: float = 0.0,
        should_hang: bool = False,
    ) -> None:
        self._stdout_lines = stdout_lines
        self._stderr_lines = stderr_lines
        self._exit_code = exit_code
        self._duration_seconds = duration_seconds
        self._should_hang = should_hang
        self._spawned: list[MockAgentProcess] = []
        self._last_prompt: str | None = None

    @property
    def name(self) -> str:
        return "mock"

    async def spawn(
        self,
        prompt: str,
        cwd: str | Path,
        *,
        env: dict[str, str] | None = None,
        model: str | None = None,
        max_budget_usd: Decimal | None = None,
    ) -> MockAgentProcess:
        self._last_prompt = prompt
        process = MockAgentProcess(
            stdout_lines_data=list(self._stdout_lines) if self._stdout_lines else None,
            stderr_lines_data=list(self._stderr_lines) if self._stderr_lines else None,
            exit_code=self._exit_code,
            duration_seconds=self._duration_seconds,
            should_hang=self._should_hang,
        )
        self._spawned.append(process)
        return process

    def parse_output(self, line: str) -> StreamEvent | None:
        stripped = line.strip()
        if not stripped:
            return None
        return StreamEvent(type="content", text=stripped)

    async def check_available(self) -> tuple[bool, str]:
        return True, "mock-runtime v0.0.0"

    @property
    def spawned_processes(self) -> list[MockAgentProcess]:
        return self._spawned

    @property
    def last_prompt(self) -> str | None:
        return self._last_prompt
